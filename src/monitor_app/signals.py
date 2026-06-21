"""Entry versioning — snapshots on save.

Pre-save signal on Entry. When content or substantive data changes,
insert an EntryVersion row capturing the PRE-save state (so "version
history" means "what this entry looked like at this time").

Thread-local changed_by: views set it via `set_changed_by(who)` before
saving; defaults to 'unknown'. Skips pure operational-key updates (e.g.
bumping timestamp_modified) via _is_substantive_change().

Matches tjai's signals.py in spirit; simplified.
"""
from __future__ import annotations

import threading
import time

from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import Entry, EntryVersion


_local = threading.local()


def set_changed_by(who: str) -> None:
    _local.changed_by = who


def get_changed_by() -> str:
    return getattr(_local, 'changed_by', 'unknown')


# Data keys that change every save (e.g. engine heartbeats) and don't
# warrant a new version — match tjai's noise-filter list.
_OPERATIONAL_KEYS = {'last_seen', 'last_run', 'retry_count', 'cooldown_until'}


def _data_substantive(old: dict | None, new: dict | None) -> bool:
    """True if anything outside _OPERATIONAL_KEYS changed between old/new."""
    old = dict(old or {})
    new = dict(new or {})
    for k in _OPERATIONAL_KEYS:
        old.pop(k, None)
        new.pop(k, None)
    return old != new


@receiver(pre_save, sender=Entry)
def snapshot_on_change(sender, instance: Entry, **kwargs):
    if not instance.pk:
        return  # brand-new row — nothing to snapshot yet
    try:
        prev = Entry.objects.get(pk=instance.pk)
    except Entry.DoesNotExist:
        return

    content_changed = prev.content != instance.content
    title_changed = prev.title != instance.title
    data_changed = _data_substantive(prev.data, instance.data)
    if not (content_changed or title_changed or data_changed):
        return

    last = (EntryVersion.objects
            .filter(entry=prev)
            .order_by('-version_num')
            .first())
    next_num = (last.version_num + 1) if last else 1

    EntryVersion.objects.create(
        entry=prev,
        version_num=next_num,
        title=prev.title,
        content=prev.content,
        data=prev.data,
        changed_by=get_changed_by(),
        timestamp=time.time(),
    )
