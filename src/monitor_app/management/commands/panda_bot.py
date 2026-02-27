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
        logging.getLogger('panda_bot').setLevel(logging.DEBUG)

        from monitor_app.panda.bot import PandaBot

        bot = PandaBot()
        bot.start()
