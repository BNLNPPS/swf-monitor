"""
Django management command to emulate the processing agent for STF workflow testing.

This command listens for data_ready messages from the data agent and simulates
the processing agent's workflow: receiving, processing, and completing STF files.
"""

import json
import logging
import time
import random
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from monitor_app.models import (
    STFWorkflow, AgentWorkflowStage, WorkflowMessage, SystemAgent,
    DAQState, DAQSubstate, WorkflowStatus, AgentType
)
import stomp
import ssl
import os

logger = logging.getLogger(__name__)


class ProcessingAgentMessageListener(stomp.ConnectionListener):
    """ActiveMQ message listener for the emulated processing agent."""
    
    def __init__(self, conn, agent_name):
        self.conn = conn
        self.agent_name = agent_name
        self.agent_type = AgentType.PROCESSING
        
        # Ensure agent is registered in the database
        self.agent, created = SystemAgent.objects.get_or_create(
            instance_name=agent_name,
            defaults={
                'agent_type': self.agent_type,
                'status': 'OK',
                'workflow_enabled': True,
                'description': 'Emulated processing agent for STF workflow testing',
                'last_heartbeat': timezone.now()
            }
        )
        if not created:
            self.agent.workflow_enabled = True
            self.agent.last_heartbeat = timezone.now()
            self.agent.save()
        
        logger.info(f"Processing agent {agent_name} initialized and registered")

    def on_error(self, frame):
        logger.error(f'Processing agent received error: {frame.body}')

    def on_message(self, frame):
        """Handle incoming messages from ActiveMQ."""
        logger.info(f'Processing agent received message: {frame.body}')
        
        try:
            data = json.loads(frame.body)
            message_type = data.get('msg_type')
            
            if message_type == 'data_ready':
                self._handle_data_ready(data)
            else:
                logger.debug(f"Processing agent ignoring message type: {message_type}")
                
        except json.JSONDecodeError:
            logger.error(f'Failed to decode JSON from message: {frame.body}')
        except Exception as e:
            logger.error(f'Error processing message: {e}')

    def _handle_data_ready(self, data_message):
        """Handle data_ready message from data agent."""
        filename = data_message.get('filename')
        workflow_id = data_message.get('workflow_id')
        
        if not filename or not workflow_id:
            logger.error("data_ready message missing filename or workflow_id")
            return
            
        logger.info(f"Processing agent handling data_ready for: {filename}")
        
        # Find the workflow
        try:
            workflow = STFWorkflow.objects.get(workflow_id=workflow_id)
        except STFWorkflow.DoesNotExist:
            logger.error(f"Workflow not found for ID: {workflow_id}")
            return
        
        # Record the workflow message
        WorkflowMessage.objects.create(
            workflow=workflow,
            message_type='data_ready',
            sender_agent=data_message.get('agent_name', 'data-agent'),
            sender_type=AgentType.DATA,
            recipient_agent=self.agent_name,
            recipient_type=self.agent_type,
            message_content=data_message,
            is_successful=True
        )
        
        # Start processing agent work
        self._start_processing_work(workflow, data_message)

    def _start_processing_work(self, workflow, data_message):
        """Simulate processing agent work on an STF."""
        logger.info(f"Processing agent starting work on {workflow.filename}")
        
        # Create agent workflow stage
        stage = AgentWorkflowStage.objects.create(
            workflow=workflow,
            agent_name=self.agent_name,
            agent_type=self.agent_type,
            status=WorkflowStatus.PROCESSING_RECEIVED,
            input_message=data_message
        )
        
        # Update workflow status
        workflow.current_status = WorkflowStatus.PROCESSING_RECEIVED
        workflow.current_agent = self.agent_type
        workflow.save()
        
        # Mark stage as received
        stage.mark_received(data_message)
        
        # Update agent stats
        self.agent.update_stf_stats(increment_current=1)
        
        # Simulate processing time (3-10 seconds for more complex processing)
        processing_time = random.uniform(3.0, 10.0)
        logger.info(f"Processing agent working on {workflow.filename} for {processing_time:.2f} seconds")
        
        # Mark as processing
        stage.mark_processing()
        workflow.current_status = WorkflowStatus.PROCESSING_PROCESSING
        workflow.save()
        
        # Simulate processing work
        time.sleep(processing_time)
        
        # Complete processing
        self._complete_processing_work(workflow, stage)

    def _complete_processing_work(self, workflow, stage):
        """Complete processing agent work and notify fastmon agent."""
        logger.info(f"Processing agent completing work on {workflow.filename}")
        
        # Create output message for fastmon agent
        output_message = {
            'msg_type': 'processing_complete',
            'filename': workflow.filename,
            'workflow_id': str(workflow.workflow_id),
            'daq_state': workflow.daq_state,
            'daq_substate': workflow.daq_substate,
            'processed_data_location': f'/results/processed/{workflow.filename}',
            'processing_results': {
                'events_processed': random.randint(1000, 10000),
                'quality_score': random.uniform(0.8, 1.0),
                'processing_time_seconds': time.time() - stage.created_at.timestamp()
            },
            'agent_name': self.agent_name,
            'timestamp': timezone.now().isoformat()
        }
        
        # Mark stage as completed
        stage.mark_completed(output_message)
        
        # Update workflow status
        workflow.current_status = WorkflowStatus.PROCESSING_COMPLETE
        workflow.save()
        
        # Update agent stats
        self.agent.update_stf_stats(increment_current=-1, increment_total=1)
        
        # Send message to fastmon agent
        self._send_to_fastmon_agent(output_message)
        
        # Record the outbound message
        WorkflowMessage.objects.create(
            workflow=workflow,
            stage=stage,
            message_type='processing_complete',
            sender_agent=self.agent_name,
            sender_type=self.agent_type,
            recipient_agent='fastmon-agent',
            recipient_type=AgentType.FASTMON,
            message_content=output_message,
            is_successful=True
        )

    def _send_to_fastmon_agent(self, message):
        """Send message to fastmon agent queue."""
        try:
            destination = 'fastmon_agent'
            self.conn.send(destination=destination, body=json.dumps(message))
            logger.info(f"Sent processing_complete message to fastmon agent: {message['filename']}")
        except Exception as e:
            logger.error(f"Failed to send message to fastmon agent: {e}")

    def on_disconnected(self):
        logger.warning("Processing agent disconnected from ActiveMQ. Attempting to reconnect...")


class Command(BaseCommand):
    help = 'Emulates a processing agent for STF workflow testing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--agent-name',
            type=str,
            default='processing-agent-emulated',
            help='Name of the emulated processing agent instance'
        )
        parser.add_argument(
            '--queue',
            type=str,
            default='processing_agent',
            help='ActiveMQ queue to listen on'
        )

    def handle(self, *args, **options):
        agent_name = options['agent_name']
        queue = options['queue']
        
        self.stdout.write(self.style.SUCCESS(f'Starting emulated processing agent: {agent_name}'))
        self.stdout.write(f'Listening on queue: {queue}')
        
        # Configure ActiveMQ connection
        self._setup_activemq_connection(agent_name, queue)

    def _setup_activemq_connection(self, agent_name, queue):
        """Set up ActiveMQ connection and start listening."""
        # Check for local development mode
        local_mode = os.environ.get('MQ_LOCAL') == '1'
        
        if local_mode:
            self.stdout.write(self.style.SUCCESS("Using local development mode (no SSL)"))
            host = 'localhost'
            port = 61616
            user = 'admin'
            password = 'admin'
            use_ssl = False
        else:
            # Production settings
            host = getattr(settings, 'ACTIVEMQ_HOST', 'localhost')
            port = getattr(settings, 'ACTIVEMQ_PORT', 61612)
            user = getattr(settings, 'ACTIVEMQ_USER', 'admin')
            password = getattr(settings, 'ACTIVEMQ_PASSWORD', 'admin')
            use_ssl = getattr(settings, 'ACTIVEMQ_USE_SSL', False)
        
        # Create connection
        conn = stomp.Connection(host_and_ports=[(host, port)], vhost=host, try_loopback_connect=False)
        
        # Set SSL if needed
        if use_ssl:
            ca_certs = getattr(settings, 'ACTIVEMQ_SSL_CA_CERTS', None)
            if ca_certs:
                conn.set_ssl(for_hosts=[(host, port)], ca_certs=ca_certs)
        
        # Set up message listener
        listener = ProcessingAgentMessageListener(conn, agent_name)
        conn.set_listener('', listener)
        
        try:
            # Connect and subscribe
            conn.connect(user, password, wait=True)
            conn.subscribe(destination=queue, id=1, ack='auto')
            
            self.stdout.write(self.style.SUCCESS(f'Connected to ActiveMQ and subscribed to {queue}'))
            self.stdout.write('Processing agent is running. Press Ctrl+C to stop.')
            
            # Keep running
            while True:
                time.sleep(1)
                
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('Processing agent stopped by user'))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Error in processing agent: {e}'))
            logger.error(f'Error in processing agent: {e}')
        finally:
            if conn and conn.is_connected():
                conn.disconnect()
                self.stdout.write('Disconnected from ActiveMQ')