#!/usr/bin/env python3
"""Run approved local SWF migrations via PostgreSQL peer authentication.

Applications keep a non-owner database login. The root deployment process
loads the release environment, then invokes Django as the postgres OS user.
No administrator password is stored in the application environment.
"""
import os
from pathlib import Path
import subprocess
import sys


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this migration helper as root during an approved deployment.')
    env_file = Path(os.environ.get('SWF_ENV_FILE', '/opt/swf-monitor/config/env/production.env'))
    env = os.environ.copy()
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.removeprefix('export ').split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    if env.get('DB_HOST', 'localhost') not in ('localhost', '127.0.0.1', '::1', ''):
        raise SystemExit('This helper requires the local SWF PostgreSQL server.')
    env.update(DB_USER='postgres', DB_PASSWORD='', DB_HOST='/var/run/postgresql',
               DJANGO_SETTINGS_MODULE='swf_monitor_project.settings', DJANGO_LOGGING_MODE='none')
    # Use exactly the environment read above. The postgres OS user must not need
    # read permission on application credential files discovered by AutoConfig.
    bootstrap = '''
import decouple
from decouple import Config, RepositoryEmpty
decouple.config.config = Config(RepositoryEmpty())
from django.core.management import execute_from_command_line
import sys
execute_from_command_line(['manage.py', 'migrate', '--noinput'] + sys.argv[1:])
'''
    return subprocess.call(['/usr/sbin/runuser', '-u', 'postgres', '--', sys.executable,
                            '-c', bootstrap, *sys.argv[1:]], env=env)


if __name__ == '__main__':
    sys.exit(main())
