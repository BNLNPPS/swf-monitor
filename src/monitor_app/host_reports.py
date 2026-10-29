"""Ingest for host reporters.

A reporter runs on a host whose state only that host can see and pushes
a record here on its own schedule (docs/OSG_SUBMIT_REPORTER.md,
docs/PANDA_SERVER_REPORTER.md). Push rather than poll is not a
preference: the submit host refuses ssh except through the facility
gateway with agent forwarding, so nothing here can reach it.

The record lands in the cached-product store under a key naming the
host. That store already holds a keyed JSON value with the time it was
built, which is exactly a reporter's record and its freshness, so a
reporter needs no table of its own and no migration. A reader gets the
record and its age together, and an absent or stale record is a fact
the reader can state rather than an empty page.
"""
import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import (SessionAuthentication,
                                           TokenAuthentication)
from rest_framework.decorators import (api_view, authentication_classes,
                                       permission_classes)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

logger = logging.getLogger(__name__)

# The harvester records (docs/HARVESTER_REPORTER.md), one per harvester
# instance; every reader of harvester records takes this list.
# `osgsub01-harvester` is the harvester reporter on osgsub01, whose own key
# belongs to the OSG submit reporter. `perlmutter-harvester` is the
# Perlmutter harvester's record, built on this host from what that
# harvester reports to the PanDA server.
HARVESTER_HOSTS = ('pandaharvester01', 'pandaharvester02', 'osgsub01-harvester',
                   'perlmutter-harvester')

# Hosts that may report. A reporter writes one key, so an unknown host
# is refused rather than allowed to create keys in the shared store.
# `bnl-scdf` is a pool rather than a host: the pool reporter runs here
# and reports what its collector answers (docs/POOL_REPORTER.md), which
# is the same kind of record from the same kind of reporter.
REPORTING_HOSTS = {'osgsub01', 'pandaserver01', 'bnl-scdf', *HARVESTER_HOSTS}

# A record is a summary, not a payload. Anything larger is a reporter
# defect and is refused with its size named, rather than stored.
MAX_RECORD_BYTES = 512 * 1024


def product_key(host):
    """The cached-product key holding one host's latest record."""
    return 'host_report:{}'.format(host)


def latest(host):
    """One host's record with its age, or None when it has never reported.

    Returns ``{host, record, reported_at, age_seconds}``. Callers render
    the age: a reporter that has stopped is otherwise indistinguishable
    from one that is merely quiet.
    """
    from .models import CachedProduct
    row = CachedProduct.objects.filter(key=product_key(host)).first()
    if row is None or not row.value:
        return None
    age = None
    if row.built_at:
        age = (timezone.now() - row.built_at).total_seconds()
    return {'host': host, 'record': row.value,
            'reported_at': row.built_at, 'age_seconds': age}


class RecordRefused(ValueError):
    """A record the store does not take; ``status`` is the HTTP answer."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def store(host, record, by=''):
    """Store ``record`` whole as ``host``'s latest; the one write path, for
    the ingest and for a reporter running on this host. Returns
    ``(reported_at, bytes)``; raises RecordRefused."""
    from .models import CachedProduct

    if host not in REPORTING_HOSTS:
        raise RecordRefused("unknown reporting host '{}'".format(host),
                            status.HTTP_404_NOT_FOUND)
    if not isinstance(record, dict) or not record:
        raise RecordRefused('the record must be a non-empty object',
                            status.HTTP_400_BAD_REQUEST)
    size = len(str(record))
    if size > MAX_RECORD_BYTES:
        raise RecordRefused('record of {} bytes exceeds the {} byte limit'.format(
            size, MAX_RECORD_BYTES), status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    now = timezone.now()
    CachedProduct.objects.update_or_create(
        key=product_key(host),
        defaults={'value': record, 'built_at': now, 'building_since': None},
    )
    logger.info('host report stored for %s (%d bytes) from %s', host, size, by or '?')
    return now, size


@api_view(['POST', 'GET'])
# Token first and explicitly: the project declares no default
# authenticators, so DRF would fall back to session and basic only and
# never look at a reporter's token.
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def host_report(request, host):
    """Store a host reporter's record, or return the one stored.

    POST body is the record itself, as the reporter composed it. It is
    stored whole and unedited: a reporter delivers its own failures as
    fields, and rewriting the record here would lose them.
    """
    if host not in REPORTING_HOSTS:
        return Response(
            {'error': "unknown reporting host '{}'".format(host)},
            status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        found = latest(host)
        if found is None:
            return Response({'host': host, 'record': None,
                             'detail': 'this host has never reported'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(found)

    record = request.data
    try:
        now, size = store(host, record, getattr(request.user, 'username', ''))
    except RecordRefused as exc:
        return Response({'error': str(exc)}, status=exc.status_code)
    if host == 'osgsub01':
        # a pool's throttle on our pilots is an incident (osg_throttle.py);
        # reading it never fails the ingest
        try:
            from .osg_throttle import observe
            observe(record, getattr(request.user, 'username', ''))
        except Exception:
            logger.exception('OSG throttle reading failed for the %s record', host)
    return Response({'host': host, 'stored': True, 'reported_at': now,
                     'bytes': size},
                    status=status.HTTP_201_CREATED)
