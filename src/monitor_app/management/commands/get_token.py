from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token

class Command(BaseCommand):
    help = 'Create or retrieve an API token for a user.'

    def add_arguments(self, parser):
        parser.add_argument('username', type=str, help='The username for the token.')
        parser.add_argument('--create-user', action='store_true', help='Create the user if they do not exist.')

    def handle(self, *args, **options):
        username = options['username']
        create_user = options['create_user']
        
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            if create_user:
                self.stdout.write(self.style.SUCCESS(f'User "{username}" not found. Creating new user.'))
                user = User.objects.create_user(username=username)
            else:
                raise CommandError(f'User "{username}" does not exist. Use --create-user to create it.')

        token, created = Token.objects.get_or_create(user=user)
        
        if created:
            self.stdout.write(self.style.SUCCESS(f'New token created for user "{username}": {token.key}'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Token for user "{username}" is: {token.key}'))
