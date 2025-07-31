"""
Django management command to create or update the testuser account for testing.
Requires SWF_TESTUSER_PASSWORD environment variable to be set.
"""

import os
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User


class Command(BaseCommand):
    help = 'Create or update testuser account for automated testing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--password',
            type=str,
            help='Override password (default: use SWF_TESTUSER_PASSWORD env var)',
        )

    def handle(self, *args, **options):
        username = 'testuser'
        
        # Get password from argument or environment variable
        password = options.get('password') or os.getenv('SWF_TESTUSER_PASSWORD')
        
        if not password:
            raise CommandError(
                'Password required. Either:\n'
                '  1. Set SWF_TESTUSER_PASSWORD environment variable, or\n'
                '  2. Use --password argument\n'
                'Example: export SWF_TESTUSER_PASSWORD="your_secure_password"'
            )
        
        try:
            # Try to get existing user
            user = User.objects.get(username=username)
            user.set_password(password)
            user.save()
            self.stdout.write(
                self.style.SUCCESS(f'Updated password for existing user "{username}"')
            )
        except User.DoesNotExist:
            # Create new user
            user = User.objects.create_user(
                username=username,
                password=password,
                email='testuser@example.com'
            )
            self.stdout.write(
                self.style.SUCCESS(f'Created new user "{username}"')
            )
        
        self.stdout.write(f'Username: {username}')
        password_source = "command argument" if options.get('password') else "SWF_TESTUSER_PASSWORD environment variable"
        self.stdout.write(f'Password source: {password_source}')