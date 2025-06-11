import stomp
import time
import logging
import json # Add this import
from django.conf import settings
from django.utils import timezone # Add this import
from .models import MonitoredItem # Add this import

logger = logging.getLogger(__name__)

class MessageListener(stomp.ConnectionListener):
    def __init__(self, conn):
        self.conn = conn

    def on_error(self, frame):
        logger.error(f'Received an error: {frame.body}')

    def on_message(self, frame):
        logger.info(f'Received a message: {frame.body}')
        try:
            data = json.loads(frame.body)
            agent_name = data.get('agent_name')
            status = data.get('status')
            # Optional: agent_url and description could also be updated if provided
            # agent_url = data.get('agent_url')
            # description = data.get('description')

            if agent_name: # Status can be optional if we only update heartbeat
                try:
                    item, created = MonitoredItem.objects.get_or_create(
                        name=agent_name,
                        defaults={
                            'status': status if status else MonitoredItem.STATUS_UNKNOWN,
                            'last_heartbeat': timezone.now()
                        }
                    )
                    if not created:
                        if status:
                            item.status = status
                        item.last_heartbeat = timezone.now()
                        # if agent_url:
                        #     item.agent_url = agent_url
                        # if description:
                        #     item.description = description
                        item.save()
                    logger.info(f'Updated/Created MonitoredItem for {agent_name} with status {item.status}')
                except Exception as e:
                    logger.error(f'Error processing message for agent {agent_name}: {e}')
            else:
                logger.warning(f'Message received without agent_name: {frame.body}')
        except json.JSONDecodeError:
            logger.error(f'Failed to decode JSON from message: {frame.body}')
        except Exception as e:
            logger.error(f'An unexpected error occurred in on_message: {e}')


    def on_disconnected(self):
        logger.warning("Disconnected from ActiveMQ. Attempting to reconnect...")
        connect_and_subscribe(self.conn)

def connect_and_subscribe(conn):
    try:
        conn.connect(settings.ACTIVEMQ_USER, settings.ACTIVEMQ_PASSWORD, wait=True)
        # Example: Subscribe to a topic where agents send heartbeats
        conn.subscribe(destination=settings.ACTIVEMQ_HEARTBEAT_TOPIC, id=1, ack='auto')
        logger.info(f"Successfully connected and subscribed to {settings.ACTIVEMQ_HEARTBEAT_TOPIC}")
    except stomp.exception.ConnectFailedException as e:
        logger.error(f"Failed to connect to ActiveMQ: {e}. Retrying in 10 seconds...")
        time.sleep(10)
        connect_and_subscribe(conn) # Recursive call to retry
    except Exception as e:
        logger.error(f"An unexpected error occurred during connection/subscription: {e}")
        time.sleep(10)
        connect_and_subscribe(conn) # Recursive call to retry

def start_activemq_listener():
    # These would come from your Django settings.py
    activemq_host = getattr(settings, 'ACTIVEMQ_HOST', 'localhost')
    activemq_port = getattr(settings, 'ACTIVEMQ_PORT', 61613)
    
    conn = stomp.Connection([(activemq_host, activemq_port)])
    conn.set_listener('', MessageListener(conn))
    
    connect_and_subscribe(conn)
    
    # Keep the listener running in a loop
    # In a real Django app, you'd run this in a background thread or a separate management command process.
    # For now, this is a simplified loop for demonstration.
    try:
        while True:
            time.sleep(1) # Keep the main thread alive
    except KeyboardInterrupt:
        logger.info("Listener shutting down...")
    finally:
        if conn.is_connected():
            conn.disconnect()
            logger.info("Disconnected from ActiveMQ.")

# To integrate this into Django, you would typically:
# 1. Add ACTIVEMQ_HOST, ACTIVEMQ_PORT, ACTIVEMQ_USER, ACTIVEMQ_PASSWORD, ACTIVEMQ_HEARTBEAT_TOPIC to your settings.py.
# 2. Create a management command (e.g., `python manage.py listen_activemq`) that calls start_activemq_listener.
# 3. Run this management command as a separate, long-running process.
