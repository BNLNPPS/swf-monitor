#!/usr/bin/env python3
"""Evaluate one coherent Snapper capture opportunity for SWF scopes."""

import argparse
import json
import os
import sys
from datetime import datetime

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.utils import timezone  # noqa: E402

from snapper_ai.capture import (  # noqa: E402
    aligned_boundary,
    capture_scope,
    report_capture_failure,
)


SCOPES = ('testbed', 'epicprod')


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


def _result_payload(scope, result):
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
    }


def main(argv):
    ap = argparse.ArgumentParser(
        description='Evaluate one SWF Snapper capture opportunity.')
    ap.add_argument('--scope', choices=(*SCOPES, 'all'), default='all')
    ap.add_argument('--manual', action='store_true',
                    help='Force a full snap at this boundary.')
    ap.add_argument('--boundary', type=_boundary,
                    help='Aligned ISO timestamp; current boundary by default.')
    ap.add_argument('--opportunity-seconds', type=int, default=10)
    ap.add_argument('--baseline-every', type=int, default=10)
    args = ap.parse_args(argv[1:])

    scopes = SCOPES if args.scope == 'all' else (args.scope,)
    boundary = args.boundary or aligned_boundary(
        timezone.now(), args.opportunity_seconds)
    reports = []
    failed = False
    for scope in scopes:
        try:
            result = capture_scope(
                scope=scope,
                boundary_at=boundary,
                capture_policy=f'{scope}-v1',
                opportunity_seconds=args.opportunity_seconds,
                baseline_every=args.baseline_every,
                manual=args.manual,
            )
        except Exception as exc:
            failed = True
            try:
                report_capture_failure(
                    scope=scope,
                    boundary_at=boundary,
                    error=str(exc),
                )
            except Exception as report_exc:
                reports.append({
                    'scope': scope,
                    'boundary_at': boundary.isoformat(),
                    'outcome': 'failed',
                    'error': str(exc),
                    'failure_report_error': str(report_exc),
                })
            else:
                reports.append({
                    'scope': scope,
                    'boundary_at': boundary.isoformat(),
                    'outcome': 'failed',
                    'error': str(exc),
                })
        else:
            reports.append(_result_payload(scope, result))

    print(json.dumps(reports, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
