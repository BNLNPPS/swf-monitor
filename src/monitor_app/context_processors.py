"""Global template context for lightweight monitor state."""

from .system_status import status_summary


def system_status_nav(request):
    summary = status_summary()
    return {
        'system_status_overall': summary.get('overall_status', 'unknown'),
        'system_status_reason': summary.get('overall_reason', ''),
        'system_status_latest_checked_at': summary.get('latest_checked_at'),
    }
