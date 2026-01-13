import ssl
import logging
import threading
from django.conf import settings

try:
    import stomp
except ImportError:
    stomp = None

class ActiveMQConnectionManager:
    """
    Singleton connection manager for ActiveMQ integration.
    Handles connection lifecycle and ensures single connection per Django instance.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, 'initialized'):
            return
        self.conn = None
        self.listener = None
        self.initialized = True
        self.logger = logging.getLogger(__name__)
    
    def connect(self):
        """Establish connection to ActiveMQ"""
        if stomp is None:
            self.logger.error("stomp.py library not available - cannot connect to ActiveMQ")
            return False
            
        if self.conn and self.conn.is_connected():
            self.logger.debug("ActiveMQ already connected")
            return True
            
        try:
            host = getattr(settings, 'ACTIVEMQ_HOST', 'localhost')
            port = getattr(settings, 'ACTIVEMQ_PORT', 61612)
            
            self.logger.info(f"Connecting to ActiveMQ at {host}:{port}")
            
            # Create connection matching working example agents
            # Use heartbeats parameter like swf-common-lib does
            heartbeats = (5000, 10000)  # (client, server) heartbeats in milliseconds
            self.conn = stomp.Connection(
                host_and_ports=[(host, port)],
                vhost=host, 
                try_loopback_connect=False,
                heartbeats=heartbeats
            )
            
            # Configure SSL if enabled - MUST be done before set_listener
            if getattr(settings, 'ACTIVEMQ_USE_SSL', False):
                self._configure_ssl(host, port)
            
            # Set up message listener
            from .activemq_processor import WorkflowMessageProcessor
            self.listener = WorkflowMessageProcessor(self)
            self.conn.set_listener('', self.listener)
            
            # Connect and subscribe with proper STOMP version and headers
            user = getattr(settings, 'ACTIVEMQ_USER', 'admin')
            password = getattr(settings, 'ACTIVEMQ_PASSWORD', 'admin')
            topic = getattr(settings, 'ACTIVEMQ_HEARTBEAT_TOPIC', 'epictopic')
            
            self.conn.connect(
                user,
                password,
                wait=True,
                version='1.1',
                headers={
                    'client-id': 'swf-monitor-django'
                }
            )

            # Subscribe to workflow topic (broadcast messages from agents)
            self.conn.subscribe(destination=topic, id=1, ack='auto')

            self.logger.info(f"Successfully connected to ActiveMQ and subscribed to {topic}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to connect to ActiveMQ: {e}")
            self.conn = None
            return False
    
    def _configure_ssl(self, host, port):
        """Configure SSL for ActiveMQ connection"""
        try:
            ssl_ca_certs = getattr(settings, 'ACTIVEMQ_SSL_CA_CERTS', '')
            ssl_cert_file = getattr(settings, 'ACTIVEMQ_SSL_CERT_FILE', '')
            ssl_key_file = getattr(settings, 'ACTIVEMQ_SSL_KEY_FILE', '')
            
            if ssl_ca_certs:
                ssl_args = {
                    'ca_certs': ssl_ca_certs,
                    'ssl_version': ssl.PROTOCOL_TLS_CLIENT
                }
                
                # Add client cert and key if provided
                if ssl_cert_file and ssl_key_file:
                    ssl_args['certfile'] = ssl_cert_file
                    ssl_args['keyfile'] = ssl_key_file
                
                self.conn.transport.set_ssl(
                    for_hosts=[(host, port)],
                    **ssl_args
                )
                self.logger.info(f"SSL configured with CA certs: {ssl_ca_certs}")
            else:
                self.logger.warning("SSL enabled but no CA certificate file specified")
                
        except Exception as e:
            self.logger.error(f"Failed to configure SSL: {e}")
            raise
    
    def disconnect(self):
        """Disconnect from ActiveMQ"""
        if self.conn and self.conn.is_connected():
            try:
                self.conn.disconnect()
                self.logger.info("Disconnected from ActiveMQ")
            except Exception as e:
                self.logger.error(f"Error disconnecting from ActiveMQ: {e}")
        self.conn = None
        self.listener = None
    
    def reconnect(self):
        """Attempt to reconnect to ActiveMQ"""
        self.logger.info("Attempting to reconnect to ActiveMQ...")
        self.disconnect()
        return self.connect()
    
    def is_connected(self):
        """Check if connection is active"""
        return self.conn and self.conn.is_connected()

    def send_message(self, destination: str, body: str) -> bool:
        """
        Send a message to a destination queue/topic.

        Args:
            destination: Queue or topic name (e.g., '/queue/workflow_control')
            body: Message body (typically JSON string)

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.is_connected():
            if not self.connect():
                self.logger.error("Cannot send message - not connected to ActiveMQ")
                return False

        try:
            self.conn.send(destination=destination, body=body)
            self.logger.info(f"Sent message to {destination}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to send message to {destination}: {e}")
            return False