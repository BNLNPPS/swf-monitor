"""
Django management command to emulate the data agent for STF workflow testing.

This command listens for STF generation messages from daqsim and simulates
the data agent's workflow: receiving, processing, and completing STF files.
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


class DataAgentMessageListener(stomp.ConnectionListener):
    """ActiveMQ message listener for the emulated data agent."""
    
    def __init__(self, conn, agent_name):
        self.conn = conn
        self.agent_name = agent_name
        self.agent_type = AgentType.DATA
        
        # Ensure agent is registered in the database
        self.agent, created = SystemAgent.objects.get_or_create(
            instance_name=agent_name,
            defaults={
                'agent_type': self.agent_type,
                'status': 'OK',
                'workflow_enabled': True,
                'description': 'Emulated data agent for STF workflow testing',
                'last_heartbeat': timezone.now()
            }
        )
        if not created:
            self.agent.workflow_enabled = True
            self.agent.last_heartbeat = timezone.now()
            self.agent.save()
        
        logger.info(f"Data agent {agent_name} initialized and registered")

    def on_error(self, frame):
        logger.error(f'Data agent received error: {frame.body}')

    def on_message(self, frame):
        """Handle incoming messages from ActiveMQ."""
        logger.info(f'Data agent received message: {frame.body}')
        
        try:
            data = json.loads(frame.body)
            message_type = data.get('msg_type')
            
            if message_type == 'stf_gen':
                self._handle_stf_generation(data)
            else:
                logger.debug(f"Data agent ignoring message type: {message_type}")
                
        except json.JSONDecodeError:
            logger.error(f'Failed to decode JSON from message: {frame.body}')
        except Exception as e:
            logger.error(f'Error processing message: {e}')

    def _handle_stf_generation(self, stf_data):
        """Handle STF generation message from daqsim."""
        filename = stf_data.get('filename')
        if not filename:
            logger.error("STF message missing filename")
            return
            
        logger.info(f"Processing STF: {filename}")
        
        # Parse DAQ state and substate
        daq_state = stf_data.get('state', 'unknown')
        daq_substate = stf_data.get('substate', 'unknown')
        
        # Parse timestamps
        start_time_str = stf_data.get('start')
        end_time_str = stf_data.get('end')
        
        try:
            stf_start_time = datetime.strptime(start_time_str, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            stf_end_time = datetime.strptime(end_time_str, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            logger.error(f"Invalid timestamp format in STF message: {stf_data}")
            return
        
        # Create or update STF workflow record
        workflow, created = STFWorkflow.objects.get_or_create(
            filename=filename,
            defaults={
                'daq_state': daq_state,
                'daq_substate': daq_substate,
                'generated_time': timezone.now(),
                'stf_start_time': stf_start_time,
                'stf_end_time': stf_end_time,
                'current_status': WorkflowStatus.GENERATED,
                'current_agent': AgentType.DAQSIM,
                'stf_metadata': stf_data
            }
        )
        
        if not created:
            logger.warning(f"STF workflow already exists for {filename}")
            return
        
        # Record the workflow message
        WorkflowMessage.objects.create(
            workflow=workflow,
            message_type='stf_gen',
            sender_agent='daqsim',
            sender_type=AgentType.DAQSIM,
            recipient_agent=self.agent_name,
            recipient_type=self.agent_type,
            message_content=stf_data,
            is_successful=True
        )
        
        # Start data agent processing
        self._start_data_processing(workflow)

    def _start_data_processing(self, workflow):
        """Simulate data agent processing of an STF."""
        logger.info(f"Data agent starting processing for {workflow.filename}")
        
        # Create agent workflow stage
        stage = AgentWorkflowStage.objects.create(
            workflow=workflow,
            agent_name=self.agent_name,
            agent_type=self.agent_type,
            status=WorkflowStatus.DATA_RECEIVED,
            input_message=workflow.stf_metadata
        )
        
        # Update workflow status
        workflow.current_status = WorkflowStatus.DATA_RECEIVED
        workflow.current_agent = self.agent_type
        workflow.save()
        
        # Mark stage as received
        stage.mark_received(workflow.stf_metadata)
        
        # Update agent stats
        self.agent.update_stf_stats(increment_current=1)
        
        # Simulate processing time (1-5 seconds)
        processing_time = random.uniform(1.0, 5.0)
        logger.info(f"Data agent processing {workflow.filename} for {processing_time:.2f} seconds")
        
        # Mark as processing
        stage.mark_processing()
        workflow.current_status = WorkflowStatus.DATA_PROCESSING
        workflow.save()
        
        # Simulate actual processing work
        time.sleep(processing_time)
        
        # Complete processing
        self._complete_data_processing(workflow, stage)

    def _complete_data_processing(self, workflow, stage):
        """Complete data agent processing and notify processing agent."""
        logger.info(f"Data agent completing processing for {workflow.filename}")
        
        # Create output message for processing agent
        output_message = {
            'msg_type': 'data_ready',
            'filename': workflow.filename,
            'workflow_id': str(workflow.workflow_id),
            'daq_state': workflow.daq_state,
            'daq_substate': workflow.daq_substate,
            'data_location': f'/data/processed/{workflow.filename}',
            'processing_time': time.time() - stage.created_at.timestamp(),
            'agent_name': self.agent_name,
            'timestamp': timezone.now().isoformat()
        }
        
        # Mark stage as completed
        stage.mark_completed(output_message)
        
        # Update workflow status
        workflow.current_status = WorkflowStatus.DATA_COMPLETE
        workflow.save()
        
        # Update agent stats
        self.agent.update_stf_stats(increment_current=-1, increment_total=1)
        
        # Send message to processing agent
        self._send_to_processing_agent(output_message)
        
        # Record the outbound message
        WorkflowMessage.objects.create(
            workflow=workflow,
            stage=stage,
            message_type='data_ready',
            sender_agent=self.agent_name,
            sender_type=self.agent_type,
            recipient_agent='processing-agent',
            recipient_type=AgentType.PROCESSING,
            message_content=output_message,
            is_successful=True
        )

    def _send_to_processing_agent(self, message):
        """Send message to processing agent queue."""
        try:
            destination = 'processing_agent'
            self.conn.send(destination=destination, body=json.dumps(message))
            logger.info(f"Sent data_ready message to processing agent: {message['filename']}")
        except Exception as e:
            logger.error(f"Failed to send message to processing agent: {e}")

    def on_disconnected(self):
        logger.warning("Data agent disconnected from ActiveMQ. Attempting to reconnect...")


class Command(BaseCommand):
    help = 'Emulates a data agent for STF workflow testing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--agent-name',
            type=str,
            default='data-agent-emulated',
            help='Name of the emulated data agent instance'
        )
        parser.add_argument(
            '--queue',
            type=str,
            default='epictopic',
            help='ActiveMQ queue/topic to listen on'
        )

    def handle(self, *args, **options):
        agent_name = options['agent_name']
        queue = options['queue']
        
        self.stdout.write(self.style.SUCCESS(f'Starting emulated data agent: {agent_name}'))
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
        listener = DataAgentMessageListener(conn, agent_name)
        conn.set_listener('', listener)
        
        try:
            # Connect and subscribe
            conn.connect(user, password, wait=True)
            conn.subscribe(destination=queue, id=1, ack='auto')
            
            self.stdout.write(self.style.SUCCESS(f'Connected to ActiveMQ and subscribed to {queue}'))
            self.stdout.write('Data agent is running. Press Ctrl+C to stop.')
            
            # Keep running
            while True:
                time.sleep(1)
                
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('Data agent stopped by user'))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Error in data agent: {e}'))
            logger.error(f'Error in data agent: {e}')
        finally:
            if conn and conn.is_connected():
                conn.disconnect()
                self.stdout.write('Disconnected from ActiveMQ')