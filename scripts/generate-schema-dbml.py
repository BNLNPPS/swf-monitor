#!/usr/bin/env python3
"""Generate the database schema DBML to stdout.

Wraps django-dbml's management command with a workaround: the library
(1.1.2) looks index fields up verbatim, so an index declared with a
descending field ('-timestamp_created') raises KeyError. The ordering
prefix is stripped from every model index before generation — the
diagram loses only the sort direction.

Usage: python scripts/generate-schema-dbml.py > testbed-schema.dbml
(the workflow also strips the generator's Last-Updated timestamp line
so an unchanged schema produces no diff).
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.apps import apps  # noqa: E402
from django.core.management import call_command  # noqa: E402

for model in apps.get_models():
    for index in model._meta.indexes:
        index.fields = [f.lstrip('-') for f in index.fields]

call_command('dbml')
