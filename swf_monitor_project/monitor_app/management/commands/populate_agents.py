from django.core.management.base import BaseCommand
from swf_monitor_project.monitor_app.models import MonitoredItem

class Command(BaseCommand):
    help = 'Populates the database with initial agent data from the swf-testbed project.'

    agent_data = [
        {'name': 'swf-daqsim-agent', 'description': 'Simulates DAQ and EIC machine/detector influences.'},
        {'name': 'swf-data-agent', 'description': 'Central data handling agent, manages Rucio subscriptions and run datasets.'},
        {'name': 'swf-processing-agent', 'description': 'Prompt processing agent, configures and submits PanDA jobs.'},
        {'name': 'swf-fastmon-agent', 'description': 'Fast monitoring agent, consumes STF data for near real-time monitoring.'},
        {'name': 'swf-mcp-agent', 'description': 'MCP (presumably Model Context Protocol) agent. (Description to be updated based on further details from README)'},
        {'name': 'swf-monitor', 'description': 'This monitoring service itself.'},
    ]

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting to populate MonitoredItem data...'))
        
        for agent_info in self.agent_data:
            item, created = MonitoredItem.objects.get_or_create(
                name=agent_info['name'],
                defaults={'description': agent_info['description']}
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f'Successfully created MonitoredItem: {agent_info["name"]}'))
            else:
                self.stdout.write(self.style.WARNING(f'MonitoredItem already exists: {agent_info["name"]}'))
        
        self.stdout.write(self.style.SUCCESS('Finished populating MonitoredItem data.'))

