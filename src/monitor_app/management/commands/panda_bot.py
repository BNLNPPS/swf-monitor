"""Django management command to run the PanDA Mattermost bot."""

import logging
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the PanDA Mattermost bot'

    def handle(self, *args, **options):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(name)s %(levelname)s %(message)s',
        )

        from monitor_app.db_log_handler import DbLogHandler

        bot_logger = logging.getLogger('panda_bot')
        bot_logger.setLevel(logging.DEBUG)
        db_handler = DbLogHandler(app_name='panda_bot', instance_name='panda-bot-mattermost')
        db_handler.setLevel(logging.INFO)
        bot_logger.addHandler(db_handler)

        from monitor_app.panda.bot import PandaBot

        bot = PandaBot()
        bot.start()
