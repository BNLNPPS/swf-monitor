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
import threading
import time
from datetime import timedelta

from django.db import close_old_connections
from django.utils import timezone

logger = logging.getLogger(__name__)

# A build slot older than this is considered abandoned (a worker died
# mid-build) and may be reclaimed.
BUILD_LOCK_TIMEOUT_SECONDS = 600


def _claim(key):
    """Atomically claim the build slot for a key. True when claimed."""
    from django.db.models import Q

    from .models import CachedProduct

    now = timezone.now()
    stale_lock = now - timedelta(seconds=BUILD_LOCK_TIMEOUT_SECONDS)
    CachedProduct.objects.get_or_create(key=key)
    claimed = (CachedProduct.objects
               .filter(key=key)
               .filter(Q(building_since__isnull=True)
                       | Q(building_since__lt=stale_lock))
               .update(building_since=now))
    return claimed == 1


def _build_and_store(key, builder):
    """Run the builder and store its product; the lock always clears.

    Every failure is logged with the key — a broken builder must surface
    in the log, never present as silently-stale data.
    """
    from .models import CachedProduct

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


def get_product(key, builder, ttl_seconds, refresh=False):
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

    if row is None or row.built_at is None:
        # Another worker holds the first-fill lock; nothing to serve yet.
        return {'value': None, 'built_at': None, 'age_seconds': None,
                'refreshing': True, 'built_now': False}

    age = (timezone.now() - row.built_at).total_seconds()
    refreshing = row.building_since is not None
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
