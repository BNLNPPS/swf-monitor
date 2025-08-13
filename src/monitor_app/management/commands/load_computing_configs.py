"""
Management command to load PanDA queue and Rucio endpoint configurations from JSON files.
"""

import json
import os
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from monitor_app.models import PandaQueue, RucioEndpoint


class Command(BaseCommand):
    help = 'Load PanDA queue and Rucio endpoint configurations from JSON files'

    def add_arguments(self, parser):
        parser.add_argument(
            '--config-dir',
            type=str,
            help='Path to config directory containing JSON files',
            default=None
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing data before loading',
        )

    def handle(self, *args, **options):
        # Determine config directory
        if options['config_dir']:
            config_dir = Path(options['config_dir'])
        else:
            # Default to swf-testbed/config directory
            # Try to find it relative to the Django project
            testbed_root = Path(settings.BASE_DIR).parent.parent / 'swf-testbed'
            config_dir = testbed_root / 'config'
            
            if not config_dir.exists():
                # Try alternate path
                config_dir = Path('/direct/eic+u/wenauseic/github/swf-testbed/config')
        
        if not config_dir.exists():
            self.stdout.write(self.style.ERROR(f'Config directory not found: {config_dir}'))
            return
        
        self.stdout.write(f'Loading configurations from: {config_dir}')
        
        # Clear existing data if requested
        if options['clear']:
            self.stdout.write('Clearing existing data...')
            PandaQueue.objects.all().delete()
            RucioEndpoint.objects.all().delete()
            self.stdout.write(self.style.SUCCESS('Existing data cleared'))
        
        # Load PanDA queues
        panda_file = config_dir / 'panda_queues.json'
        if panda_file.exists():
            self.load_panda_queues(panda_file)
        else:
            self.stdout.write(self.style.WARNING(f'PanDA queues file not found: {panda_file}'))
        
        # Load Rucio endpoints
        rucio_file = config_dir / 'ddm_endpoints.json'
        if rucio_file.exists():
            self.load_rucio_endpoints(rucio_file)
        else:
            self.stdout.write(self.style.WARNING(f'Rucio endpoints file not found: {rucio_file}'))
        
        self.stdout.write(self.style.SUCCESS('Configuration loading complete'))

    def load_panda_queues(self, file_path):
        """Load PanDA queue configurations from JSON file."""
        self.stdout.write(f'Loading PanDA queues from {file_path}...')
        
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
            
            created_count = 0
            updated_count = 0
            
            for queue_name, config in data.items():
                # Extract key fields from config
                site = config.get('site', '')
                queue_type = config.get('type', '')
                
                # Determine status based on config
                status = 'active'  # Default to active
                if config.get('status') == 'offline':
                    status = 'offline'
                
                # Create or update queue
                queue, created = PandaQueue.objects.update_or_create(
                    queue_name=queue_name,
                    defaults={
                        'site': site,
                        'queue_type': queue_type,
                        'status': status,
                        'config_data': config,
                    }
                )
                
                if created:
                    created_count += 1
                else:
                    updated_count += 1
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'PanDA queues: {created_count} created, {updated_count} updated'
                )
            )
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error loading PanDA queues: {str(e)}')
            )

    def load_rucio_endpoints(self, file_path):
        """Load Rucio endpoint configurations from JSON file."""
        self.stdout.write(f'Loading Rucio endpoints from {file_path}...')
        
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
            
            created_count = 0
            updated_count = 0
            
            for endpoint_name, config in data.items():
                # Extract key fields from config
                site = config.get('rcsite', config.get('site', ''))
                is_tape = config.get('is_tape', False)
                
                # Determine endpoint type
                if is_tape:
                    endpoint_type = 'tape'
                elif config.get('is_cache'):
                    endpoint_type = 'cache'
                else:
                    endpoint_type = 'disk'
                
                # Check if active based on rc_site_state
                is_active = config.get('rc_site_state') == 'ACTIVE'
                
                # Create or update endpoint
                endpoint, created = RucioEndpoint.objects.update_or_create(
                    endpoint_name=endpoint_name,
                    defaults={
                        'site': site,
                        'endpoint_type': endpoint_type,
                        'is_tape': is_tape,
                        'is_active': is_active,
                        'config_data': config,
                    }
                )
                
                if created:
                    created_count += 1
                else:
                    updated_count += 1
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'Rucio endpoints: {created_count} created, {updated_count} updated'
                )
            )
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error loading Rucio endpoints: {str(e)}')
            )