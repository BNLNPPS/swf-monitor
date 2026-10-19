#!/usr/bin/env python3
"""log-grant.py — a presigned upload grant for one production job's log.

A pilot whose log transfer failed holds the log in the devcloud stage-out
bucket until production operations move it to its dataset (swf-epicprod
docs/LOG_STAGEOUT_FALLBACK.md). The pilot carries no credential: it asks the
grant endpoint, which has checked the job against the PanDA record, and the
production operations agent runs this doer to sign a POST for exactly the
object the log goes to. The grant is written to the shared cache the endpoint
serves it from.

The object key is ``logs/<site>/<log dataset>/<lfn>``. The grant allows that
one key, a size between 1 byte and LOG_MAX_BYTES, and lasts 12 hours.

Standalone; also usable by hand::

    ../swf-testbed/.venv/bin/python scripts/log-grant.py --pandaid 3674202 \\
        --lfn group.EIC....log.tgz --site UM_GREX_PanDA_1 \\
        --dataset group.EIC...._log.121307.121307

The last stdout line is a JSON summary; progress goes to stderr.
Exit codes: 0 ok · 2 bad arguments · 5 no usable credential · 1 other failure.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time

BUCKET = os.environ.get('EPICPROD_STAGEOUT_BUCKET', 'epic-devcloud-stageout')
REGION = os.environ.get('EPICPROD_STAGEOUT_REGION', 'us-east-1')
PROFILE = os.environ.get('EPICPROD_STAGEOUT_AWS_PROFILE', 'epic-stageout')
GRANT_LIFETIME_S = 12 * 3600
LOG_MAX_BYTES = 1024 ** 3
GRANT_DIR = os.path.join(os.environ.get('SWF_TMP_DIR', '/data/swf-tmp'), 'log-grants')

NAME = re.compile(r'^[A-Za-z0-9_.\-]{1,250}$')


def grant_path(pandaid):
    return os.path.join(GRANT_DIR, f'{int(pandaid)}.json')


def write_atomic(path, record):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.grant-')
    try:
        with os.fdopen(fd, 'w') as fp:
            json.dump(record, fp)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pandaid', type=int, required=True)
    parser.add_argument('--lfn', required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--dataset', required=True)
    args = parser.parse_args()

    for field in ('lfn', 'site', 'dataset'):
        if not NAME.match(getattr(args, field)):
            print(f'log-grant: {field} {getattr(args, field)!r} is not a plain name', file=sys.stderr)
            print(json.dumps({'ok': False, 'error': f'bad {field}'}))
            return 2

    key = f'logs/{args.site}/{args.dataset}/{args.lfn}'
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        print(f'log-grant: boto3 unavailable: {exc}', file=sys.stderr)
        print(json.dumps({'ok': False, 'error': 'boto3 unavailable'}))
        return 5
    try:
        client = boto3.Session(profile_name=PROFILE).client('s3', region_name=REGION)
        post = client.generate_presigned_post(
            BUCKET, key, Conditions=[['content-length-range', 1, LOG_MAX_BYTES]],
            ExpiresIn=GRANT_LIFETIME_S)
    except (BotoCoreError, ClientError) as exc:
        print(f'log-grant: signing failed with profile {PROFILE}: {exc}', file=sys.stderr)
        print(json.dumps({'ok': False, 'error': f'signing failed: {exc}'}))
        return 5

    record = {'pandaid': args.pandaid, 'lfn': args.lfn, 'key': key,
              'url': post['url'], 'fields': post['fields'],
              'expires_at': int(time.time()) + GRANT_LIFETIME_S}
    write_atomic(grant_path(args.pandaid), record)
    print(f'log-grant: {args.pandaid} {key}', file=sys.stderr)
    print(json.dumps({'ok': True, 'pandaid': args.pandaid, 'key': key}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
