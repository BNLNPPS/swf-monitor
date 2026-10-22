"""Uniform handling of long-build caching (docs/CACHED_PRODUCTS.md).

A cached product is any expensive-to-build, read-often result. The
contract: a request always serves the stored product immediately,
stamped with its build time — nothing expensive builds in the request
path. Staleness triggers a background rebuild behind the response; an
explicit update request rebuilds synchronously because the user chose
to wait. ``building_since`` is the cross-worker in-flight lock, so
concurrent requests never stampede a rebuild.

Pure-database builders run here in a background thread. Credentialed or
very heavy builds belong on the prod-ops agent with an SSE completion
push (EPICPROD_OPS_AGENT.md) — this module is the light half of the one
pattern.
"""

import logging
import os
import threading
import time
import zlib
from datetime import timedelta

from django.db import close_old_connections, connection
from django.utils import timezone

logger = logging.getLogger(__name__)

# A build slot older than this is abandoned whatever else is known: the
# backstop behind the holder check below.
BUILD_LOCK_TIMEOUT_SECONDS = 600

# A build holds a Postgres session advisory lock for its key while it
# runs. The database releases it when the building process's connection
# closes, so a slot whose builder died (a process recycle at deploy kills
# the background thread before its ``finally`` clears the slot) reads as
# abandoned at once. A slot younger than the grace is live without the
# check: the claim precedes the builder taking the lock by a thread start.
CLAIM_GRACE_SECONDS = 30
ADVISORY_NAMESPACE = 7311

# An explicit update that finds another worker mid-build waits up to this
# long for that build to land before giving up and serving what exists.
REFRESH_WAIT_SECONDS = 15


RELEASE_ROOT = os.environ.get('SWF_RELEASE_ROOT', '/opt/swf-monitor')


def stores():
    """True when this process runs the deployed release, the one writer of
    the shared store (CACHED_PRODUCTS.md, Contract). A development tree
    reads what the release stored and never writes over it."""
    root = os.path.realpath(RELEASE_ROOT).rstrip('/') + '/'
    return os.path.realpath(__file__).startswith(root)


def _advisory_key(key):
    """The key's advisory lock id, an int4 under ADVISORY_NAMESPACE."""
    return zlib.crc32(key.encode()) - 2 ** 31


def _hold(key):
    """Take the key's build lock on this connection: True when taken,
    False while another live build holds it."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s, %s)',
                       [ADVISORY_NAMESPACE, _advisory_key(key)])
        return bool(cursor.fetchone()[0])


def _release(key):
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_unlock(%s, %s)',
                       [ADVISORY_NAMESPACE, _advisory_key(key)])


def _holder_alive(key):
    """True while a live database session holds the key's build lock."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory'"
            " AND classid = %s AND objid = %s AND objsubid = 2 AND granted)",
            [ADVISORY_NAMESPACE, _advisory_key(key) % 2 ** 32])
        return bool(cursor.fetchone()[0])


def _lock_live(row):
    """True while the row's build slot is held by a live build.

    A slot is abandoned when no live session holds its build lock past the
    claim grace, or when it is older than ``BUILD_LOCK_TIMEOUT_SECONDS``.
    Every reader of ``building_since`` goes through this rule, so an
    abandoned slot never presents as an in-flight rebuild and never blocks
    the next one.
    """
    if row is None or row.building_since is None:
        return False
    age = (timezone.now() - row.building_since).total_seconds()
    if age >= BUILD_LOCK_TIMEOUT_SECONDS:
        return False
    return age < CLAIM_GRACE_SECONDS or _holder_alive(row.key)


def _claim(key):
    """Atomically claim the build slot for a key. True when claimed. An
    abandoned slot is taken over by compare-and-set on its stamp, so two
    readers that find it abandoned do not both claim it."""
    from .models import CachedProduct

    now = timezone.now()
    row, _ = CachedProduct.objects.get_or_create(key=key)
    if row.building_since is None:
        return CachedProduct.objects.filter(
            key=key, building_since__isnull=True).update(building_since=now) == 1
    if _lock_live(row):
        return False
    return CachedProduct.objects.filter(
        key=key, building_since=row.building_since).update(building_since=now) == 1


def _build_and_store(key, builder):
    """Run the builder and store its product; the lock always clears.

    Every failure is logged with the key — a broken builder must surface
    in the log, never present as silently-stale data.
    """
    from .models import CachedProduct

    if not _hold(key):
        # A takeover raced a build that had not yet taken its lock: that
        # build is alive and clears the slot when it lands.
        logger.warning('cached product %s: a live build holds the lock', key)
        return
    started = time.monotonic()
    try:
        value = builder()
        CachedProduct.objects.filter(key=key).update(
            value=value,
            built_at=timezone.now(),
            build_seconds=round(time.monotonic() - started, 3),
            building_since=None,
        )
    except Exception:
        logger.exception('cached product build failed: %s', key)
        CachedProduct.objects.filter(key=key).update(building_since=None)
        raise
    finally:
        _release(key)


def _background_build(key, builder):
    def run():
        close_old_connections()
        try:
            _build_and_store(key, builder)
        except Exception:
            pass  # already logged with the key in _build_and_store
        finally:
            close_old_connections()

    thread = threading.Thread(
        target=run, name=f'cached-product-{key[:40]}', daemon=True)
    thread.start()


def get_product(key, builder, ttl_seconds, refresh=False,
                async_first_fill=False):
    """Serve a cached product; rebuild by the contract above.

    Returns ``{'value', 'built_at', 'age_seconds', 'refreshing',
    'built_now'}``. The first-ever fill and an explicit ``refresh`` build
    synchronously (there is nothing to serve, or the user asked and
    waits); a stale product returns immediately while a background
    rebuild runs.
    """
    from .models import CachedProduct

    row = CachedProduct.objects.filter(key=key).first()
    have_product = row is not None and row.built_at is not None

    if not stores():
        # A development tree: the stored product however old, else (or on
        # an explicit refresh) its own build, never written.
        if have_product and not refresh:
            return {'value': row.value, 'built_at': row.built_at,
                    'age_seconds': round(
                        (timezone.now() - row.built_at).total_seconds(), 1),
                    'refreshing': False, 'built_now': False}
        return {'value': builder(), 'built_at': timezone.now(),
                'age_seconds': 0.0, 'refreshing': False, 'built_now': True}

    # A large focus product must not hold the page request open on its first
    # build. The report already understands a truthful empty/refreshing shell
    # and follows it up after three seconds; build behind that response.
    if async_first_fill and not refresh and not have_product:
        if _claim(key):
            _background_build(key, builder)
        return {'value': None, 'built_at': None, 'age_seconds': None,
                'refreshing': True, 'built_now': False}

    if (refresh or not have_product) and _claim(key):
        _build_and_store(key, builder)
        row = CachedProduct.objects.filter(key=key).first()
        return {
            'value': row.value,
            'built_at': row.built_at,
            'age_seconds': 0.0,
            'refreshing': False,
            'built_now': True,
        }

    if refresh and have_product:
        # Explicit update, but another worker holds the build lock. The
        # caller chose to wait, so wait briefly for that build to land
        # rather than returning stale data as if it were the update.
        prior_built = row.built_at
        deadline = time.monotonic() + REFRESH_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.5)
            row = CachedProduct.objects.filter(key=key).first()
            if row is not None and row.built_at is not None \
                    and row.built_at != prior_built:
                return {
                    'value': row.value,
                    'built_at': row.built_at,
                    'age_seconds': round(
                        (timezone.now() - row.built_at).total_seconds(), 1),
                    'refreshing': False,
                    'built_now': False,
                }
            if not _lock_live(row) and _claim(key):
                # The other build ended without landing a newer product
                # (failure or lock expiry). The caller asked for a
                # synchronous update, so build it here after all.
                _build_and_store(key, builder)
                row = CachedProduct.objects.filter(key=key).first()
                return {
                    'value': row.value,
                    'built_at': row.built_at,
                    'age_seconds': 0.0,
                    'refreshing': False,
                    'built_now': True,
                }
        # The in-flight build outlasted the wait; fall through and report
        # the stored product with refreshing status honestly.

    if row is None or row.built_at is None:
        # Another worker holds the first-fill lock; nothing to serve yet.
        return {'value': None, 'built_at': None, 'age_seconds': None,
                'refreshing': True, 'built_now': False}

    age = (timezone.now() - row.built_at).total_seconds()
    refreshing = _lock_live(row)
    if age > ttl_seconds and not refreshing and _claim(key):
        _background_build(key, builder)
        refreshing = True
    return {
        'value': row.value,
        'built_at': row.built_at,
        'age_seconds': round(age, 1),
        'refreshing': refreshing,
        'built_now': False,
    }
