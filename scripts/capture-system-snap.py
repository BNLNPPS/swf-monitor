#!/usr/bin/env python3
"""Evaluate one coherent Snapper capture opportunity for SWF scopes."""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.utils import timezone  # noqa: E402

from monitor_app.models import SysConfig  # noqa: E402
from snapper_ai.capture import (  # noqa: E402
    aligned_boundary,
    capture_scope,
    report_capture_failure,
)
from snapper_ai.models import CaptureCursor  # noqa: E402


SCOPES = ('testbed', 'epicprod')
DEFAULT_OPPORTUNITY_SECONDS = 10
DEFAULT_BASELINE_EVERY = 10
DEFAULT_LOCK_TIMEOUT_MS = 5000


def _boundary(value):
    if not value:
        return None
    normalized = value[:-1] + '+00:00' if value.endswith('Z') else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not timezone.is_aware(parsed):
        raise argparse.ArgumentTypeError('boundary must include a timezone')
    return parsed


def _positive_setting(value, key, minimum=1):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{key} must be an integer, got {value!r}') from exc
    if value < minimum:
        raise ValueError(f'{key} must be at least {minimum}, got {value!r}')
    return value


def _scope_config(scope, args):
    opportunity_key = f'snapper_opportunity_seconds_{scope}'
    baseline_key = f'snapper_baseline_every_{scope}'
    policy_key = f'snapper_capture_policy_{scope}'
    opportunity = (
        args.opportunity_seconds
        if args.opportunity_seconds is not None
        else SysConfig.get_setting(
            opportunity_key, DEFAULT_OPPORTUNITY_SECONDS)
    )
    baseline = (
        args.baseline_every
        if args.baseline_every is not None
        else SysConfig.get_setting(baseline_key, DEFAULT_BASELINE_EVERY)
    )
    policy = (
        args.capture_policy
        if args.capture_policy is not None
        else SysConfig.get_setting(policy_key, f'{scope}-v1')
    )
    lock_timeout = (
        args.lock_timeout_ms
        if args.lock_timeout_ms is not None
        else SysConfig.get_setting(
            'snapper_lock_timeout_ms', DEFAULT_LOCK_TIMEOUT_MS)
    )
    return {
        'opportunity_seconds': _positive_setting(
            opportunity, opportunity_key, minimum=10),
        'baseline_every': _positive_setting(baseline, baseline_key),
        'capture_policy': str(policy or '').strip(),
        'lock_timeout_ms': _positive_setting(
            lock_timeout, 'snapper_lock_timeout_ms'),
    }


def _target_boundary(scope, args, opportunity_seconds):
    if args.boundary is not None:
        return args.boundary
    now = args.requested_at or timezone.now()
    boundary = aligned_boundary(now, opportunity_seconds)
    if not args.manual:
        return boundary
    latest = CaptureCursor.objects.filter(scope=scope).values_list(
        'latest_boundary_at', flat=True).first()
    if latest is not None and latest >= boundary:
        boundary += timedelta(seconds=opportunity_seconds)
    delay = (boundary - timezone.now()).total_seconds()
    if delay > 0:
        time.sleep(delay)
    return boundary


def _result_payload(scope, result, config):
    return {
        'scope': scope,
        'boundary_at': result.boundary_at.isoformat(),
        'outcome': result.outcome,
        'reasons': list(result.reasons),
        'changed_components': list(result.changed_components),
        'coverage_gap_started_at': (
            result.coverage_gap_started_at.isoformat()
            if result.coverage_gap_started_at else None
        ),
        'snap_id': str(result.snap.pk) if result.snap else None,
        'state_hash': result.snap.state_hash if result.snap else None,
        'capture_policy': config['capture_policy'],
        'opportunity_seconds': config['opportunity_seconds'],
        'baseline_every': config['baseline_every'],
    }


def main(argv):
    ap = argparse.ArgumentParser(
        description='Evaluate one SWF Snapper capture opportunity.')
    ap.add_argument('--scope', choices=(*SCOPES, 'all'), default='all')
    ap.add_argument('--manual', action='store_true',
                    help='Force a full snap at this boundary.')
    ap.add_argument('--boundary', type=_boundary,
                    help='Aligned ISO timestamp; current boundary by default.')
    ap.add_argument('--requested-at', type=_boundary,
                    help='Scheduler invocation time used to select the boundary.')
    ap.add_argument('--opportunity-seconds', type=int,
                    help='Override the scope SysConfig value.')
    ap.add_argument('--baseline-every', type=int,
                    help='Override the scope SysConfig value.')
    ap.add_argument('--capture-policy',
                    help='Override the scope SysConfig policy identifier.')
    ap.add_argument('--lock-timeout-ms', type=int,
                    help='Override the shared SysConfig lock timeout.')
    args = ap.parse_args(argv[1:])

    scopes = SCOPES if args.scope == 'all' else (args.scope,)
    reports = []
    failed = False
    for scope in scopes:
        boundary = None
        config = None
        try:
            config = _scope_config(scope, args)
            if not config['capture_policy']:
                raise ValueError(
                    f'snapper_capture_policy_{scope} must not be blank')
            boundary = _target_boundary(
                scope, args, config['opportunity_seconds'])
            result = capture_scope(
                scope=scope,
                boundary_at=boundary,
                capture_policy=config['capture_policy'],
                opportunity_seconds=config['opportunity_seconds'],
                baseline_every=config['baseline_every'],
                manual=args.manual,
                lock_timeout_ms=config['lock_timeout_ms'],
            )
        except Exception as exc:
            failed = True
            report = {
                'scope': scope,
                'boundary_at': boundary.isoformat() if boundary else None,
                'outcome': 'failed',
                'error': str(exc),
            }
            if boundary is not None:
                try:
                    report_capture_failure(
                        scope=scope,
                        boundary_at=boundary,
                        error=str(exc),
                    )
                except Exception as report_exc:
                    report['failure_report_error'] = str(report_exc)
            if config is not None:
                report.update({
                    'capture_policy': config['capture_policy'],
                    'opportunity_seconds': config['opportunity_seconds'],
                    'baseline_every': config['baseline_every'],
                })
            reports.append(report)
        else:
            reports.append(_result_payload(scope, result, config))

    print(json.dumps(reports, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
