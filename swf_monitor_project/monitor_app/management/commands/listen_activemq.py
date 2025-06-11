\
from django.core.management.base import BaseCommand
from swf_monitor_project.monitor_app.activemq_listener import start_activemq_listener
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Starts the ActiveMQ listener to monitor agent heartbeats.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting ActiveMQ listener...'))
        try:
            start_activemq_listener()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('ActiveMQ listener stopped by user.'))
        except Exception as e:
            logger.error(f"Failed to start ActiveMQ listener: {e}")
            self.stderr.write(self.style.ERROR(f'Failed to start ActiveMQ listener: {e}'))
