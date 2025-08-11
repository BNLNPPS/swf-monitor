import json
import logging
import threading
import time
from django.utils import timezone
from django.db import connection
from .models import SystemAgent
from .workflow_models import WorkflowMessage, STFWorkflow

try:
    import stomp
except ImportError:
    stomp = None

class WorkflowMessageProcessor(stomp.ConnectionListener if stomp else object):
    """
    ActiveMQ message processor that handles both heartbeat and workflow messages.
    Runs asynchronously within Django without blocking the web server.
    """
    
    def __init__(self, connection_manager):
        self.logger = logging.getLogger(__name__)
        self.connection_manager = connection_manager
        self.reconnect_delay = 10  # seconds
        
    def on_message(self, frame):
        """Process incoming ActiveMQ messages"""
        try:
            # Close any lingering database connections to prevent connection leaks
            connection.close()
            
            data = json.loads(frame.body)
            
            if self._is_heartbeat_message(data):
                self._process_heartbeat(data)
            elif self._is_workflow_message(data):
                self._process_workflow_message(data, frame)
            else:
                self.logger.debug(f"Unrecognized message format: {frame.body}")
                
        except json.JSONDecodeError:
            self.logger.error(f"Failed to decode JSON from message: {frame.body}")
        except Exception as e:
            self.logger.error(f"Error processing message: {e}")
            self.logger.debug(f"Message body: {frame.body}")
    
    def on_error(self, frame):
        """Handle ActiveMQ errors"""
        self.logger.error(f"ActiveMQ error: {frame.body}")
    
    def on_disconnected(self):
        """Handle disconnection from ActiveMQ"""
        self.logger.warning("Disconnected from ActiveMQ - scheduling reconnection")
        
        # Schedule reconnection in a separate thread to avoid blocking
        def delayed_reconnect():
            time.sleep(self.reconnect_delay)
            if not self.connection_manager.is_connected():
                self.connection_manager.reconnect()
        
        thread = threading.Thread(target=delayed_reconnect, daemon=True)
        thread.start()
    
    def _is_heartbeat_message(self, data):
        """Check if message is an agent heartbeat"""
        return 'agent_name' in data and 'status' in data
    
    def _is_workflow_message(self, data):
        """Check if message is a workflow message"""
        return 'msg_type' in data
    
    def _process_heartbeat(self, data):
        """Process agent heartbeat messages to update SystemAgent records"""
        agent_name = data.get('agent_name')
        status = data.get('status')
        
        if not agent_name:
            self.logger.warning(f"Heartbeat message missing agent_name: {data}")
            return
        
        try:
            agent, created = SystemAgent.objects.get_or_create(
                instance_name=agent_name,
                defaults={
                    'agent_type': 'Unknown',
                    'status': status if status else 'UNKNOWN',
                    'last_heartbeat': timezone.now(),
                    'workflow_enabled': True  # All agents are workflow-enabled by default
                }
            )
            
            if not created:
                if status:
                    agent.status = status
                agent.last_heartbeat = timezone.now()
                # Ensure existing agents are marked as workflow-enabled
                if not agent.workflow_enabled:
                    agent.workflow_enabled = True
                agent.save()
            
            self.logger.debug(f"Updated SystemAgent {agent_name} with status {agent.status}")
            
        except Exception as e:
            self.logger.error(f"Error processing heartbeat for agent {agent_name}: {e}")
    
    def _process_workflow_message(self, data, frame):
        """Process workflow messages and store them in WorkflowMessage model"""
        try:
            msg_type = data.get('msg_type')
            run_id = data.get('run_id')
            filename = data.get('filename')
            
            # Try to find related workflow
            workflow = self._find_related_workflow(run_id, filename)
            
            # Determine sender and recipient based on message type and content
            sender_agent = data.get('processed_by') or self._infer_sender_from_message_type(msg_type)
            recipient_agent = self._infer_recipient_from_message_type(msg_type)
            
            # Create WorkflowMessage record
            workflow_message = WorkflowMessage.objects.create(
                workflow=workflow,
                message_type=msg_type,
                sender_agent=sender_agent,
                recipient_agent=recipient_agent,
                message_content=data,
                sent_at=timezone.now(),
                queue_name=getattr(frame, 'destination', 'epictopic'),
                is_successful=True  # Assume successful since we received it
            )
            
            self.logger.info(f"Stored workflow message: {msg_type} for run {run_id}, filename {filename}")
            
        except Exception as e:
            self.logger.error(f"Error processing workflow message: {e}")
            self.logger.debug(f"Message data: {data}")
    
    def _find_related_workflow(self, run_id, filename):
        """Find related STFWorkflow record"""
        if not run_id:
            return None
        
        try:
            # Try exact match first (run_id + filename)
            if filename:
                workflow = STFWorkflow.objects.filter(
                    run_id=run_id, filename=filename
                ).first()
                if workflow:
                    return workflow
            
            # Fallback to run_id only
            return STFWorkflow.objects.filter(run_id=run_id).first()
            
        except Exception as e:
            self.logger.debug(f"Could not find workflow for run_id={run_id}, filename={filename}: {e}")
            return None
    
    def _infer_sender_from_message_type(self, msg_type):
        """Infer the sender agent based on message type"""
        sender_map = {
            'run_imminent': 'daq-simulator',
            'start_run': 'daq-simulator', 
            'stf_gen': 'daq-simulator',
            'pause_run': 'daq-simulator',
            'resume_run': 'daq-simulator',
            'end_run': 'daq-simulator',
            'data_ready': 'data-agent',
            'processing_complete': 'processing-agent'
        }
        return sender_map.get(msg_type, 'unknown')
    
    def _infer_recipient_from_message_type(self, msg_type):
        """Infer the recipient agent based on message type"""
        recipient_map = {
            'run_imminent': 'all-agents',
            'start_run': 'all-agents',
            'stf_gen': 'data-agent',
            'pause_run': 'all-agents', 
            'resume_run': 'all-agents',
            'end_run': 'all-agents',
            'data_ready': 'processing-agent',
            'processing_complete': 'monitoring-agent'
        }
        return recipient_map.get(msg_type, 'unknown')