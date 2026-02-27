import logging
import atexit
import sys
from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)

class MonitorAppConfig(AppConfig):
    """
    Django app configuration for monitor_app.
    Automatically starts ActiveMQ integration when Django starts.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'monitor_app'
    
    def ready(self):
        """
        Called when Django has finished loading all apps.
        Initialize ActiveMQ connection if appropriate.
        """
        if self._should_connect_activemq():
            self._initialize_activemq()
    
    def _should_connect_activemq(self):
        """
        Determine if we should start the ActiveMQ connection.
        Only connect during normal Django operation, not during admin tasks.
        """
        # Don't connect during management commands that don't need ActiveMQ
        skip_commands = [
            'migrate', 'makemigrations', 'test', 'collectstatic',
            'shell', 'dbshell', 'check', 'diffsettings', 'help',
            'panda_bot',
        ]
        
        if len(sys.argv) > 1 and sys.argv[1] in skip_commands:
            logger.debug(f"Skipping ActiveMQ connection during '{sys.argv[1]}' command")
            return False
        
        # Check if ActiveMQ is configured
        if not getattr(settings, 'ACTIVEMQ_HOST', None):
            logger.info("ActiveMQ not configured - listener will not start")
            return False
        
        # Check if we're in a test environment
        if 'test' in sys.argv or hasattr(settings, 'TESTING'):
            logger.debug("Skipping ActiveMQ connection during testing")
            return False
        
        return True
    
    def _initialize_activemq(self):
        """Initialize ActiveMQ connection and register cleanup handlers"""
        try:
            from .activemq_connection import ActiveMQConnectionManager
            
            # Get the singleton connection manager
            manager = ActiveMQConnectionManager()
            
            # Attempt to connect
            if manager.connect():
                # Register cleanup function to run when Django shuts down
                atexit.register(self._cleanup_activemq, manager)
                logger.info("ActiveMQ integration initialized successfully")
            else:
                logger.warning("Failed to initialize ActiveMQ connection")
                
        except ImportError as e:
            logger.error(f"Could not import ActiveMQ components: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize ActiveMQ integration: {e}")
    
    def _cleanup_activemq(self, manager):
        """Clean up ActiveMQ connection on Django shutdown"""
        try:
            logger.info("Shutting down ActiveMQ integration...")
            manager.disconnect()
        except Exception as e:
            logger.error(f"Error during ActiveMQ cleanup: {e}")