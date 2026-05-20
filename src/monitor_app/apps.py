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
        Only the production WSGI process should subscribe — not runserver,
        management commands, or other dev instances. This prevents duplicate
        message processing when multiple Django processes share the same DB.
        """
        # Only connect under mod_wsgi (the production Apache process)
        if 'mod_wsgi' not in sys.modules:
            context = sys.argv[1] if len(sys.argv) > 1 else 'unknown'
            logger.info(f"ActiveMQ: skipping (not WSGI, context={context})")
            return False

        if not getattr(settings, 'ACTIVEMQ_HOST', None):
            logger.info("ActiveMQ not configured - listener will not start")
            return False

        return True
    
    def _initialize_activemq(self):
        """Initialize ActiveMQ connection and register cleanup handlers"""
        try:
            from .activemq_connection import ActiveMQConnectionManager
            
            # Get the singleton connection manager
            manager = ActiveMQConnectionManager()

            # This process owns the listener; connect() should subscribe to
            # the topic. In other processes (uvicorn worker, bots) this flag
            # stays False and send_message()'s lazy connect stays send-only.
            manager._should_subscribe = True

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