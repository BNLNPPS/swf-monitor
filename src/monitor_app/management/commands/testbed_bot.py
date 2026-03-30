"""Django management command to run the SWF Testbed Mattermost bot."""

import logging
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the SWF Testbed Mattermost bot'

    def handle(self, *args, **options):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(name)s %(levelname)s %(message)s',
        )

        from monitor_app.db_log_handler import DbLogHandler

        bot_logger = logging.getLogger('testbed_bot')
        bot_logger.setLevel(logging.DEBUG)
        db_handler = DbLogHandler(app_name='testbed_bot', instance_name='testbed-bot-mattermost')
        db_handler.setLevel(logging.INFO)
        bot_logger.addHandler(db_handler)

        from monitor_app.testbed_bot.bot import TestbedBot

        bot = TestbedBot()
        bot.start()
