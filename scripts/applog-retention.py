#!/usr/bin/env python3
"""AppLog retention sweep for the swf_applog table.

Deletes log rows older than the retention window, in bounded batches so
the table is never long-locked. Retention is per level: the default
keeps every level 30 days; --error-days widens the window for ERROR and
CRITICAL rows when a longer post-mortem trail is wanted.

Standalone, no Django. Database credentials come from the same DB_* env
vars the monitor settings read (source the deployment .env). Intended
for cron; every run logs row counts before and after. See
docs/CACHED_PRODUCTS.md for the page-serving half of the log-volume
story.
"""
import argparse
import logging
import os
import sys
import time

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s applog-retention %(message)s")
log = logging.getLogger(__name__)

BATCH_ROWS = 50000


def connect():
    return psycopg.connect(
        dbname=os.environ.get("DB_NAME", "swfdb"),
        user=os.environ.get("DB_USER", "admin"),
        password=os.environ["DB_PASSWORD"],
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
    )


def batched_delete(conn, where_sql, params, dry_run):
    """Delete matching rows in id-batches; returns rows deleted."""
    total = 0
    while True:
        with conn.cursor() as cur:
            if dry_run:
                cur.execute(
                    f"SELECT count(*) FROM swf_applog WHERE {where_sql}",
                    params)
                count = cur.fetchone()[0]
                log.info("dry run: %d rows match: %s", count, where_sql)
                return count
            cur.execute(
                f"DELETE FROM swf_applog WHERE id IN ("
                f"SELECT id FROM swf_applog WHERE {where_sql} "
                f"LIMIT {BATCH_ROWS})", params)
            deleted = cur.rowcount
        conn.commit()
        total += deleted
        if deleted:
            log.info("deleted batch of %d (total %d)", deleted, total)
        if deleted < BATCH_ROWS:
            return total
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30,
                        help="retention window in days (default 30)")
    parser.add_argument("--error-days", type=int, default=None,
                        help="retention for ERROR/CRITICAL rows "
                             "(default: same as --days)")
    parser.add_argument("--dry-run", action="store_true",
                        help="count matching rows, delete nothing")
    args = parser.parse_args()
    error_days = (args.error_days if args.error_days is not None
                  else args.days)

    try:
        conn = connect()
    except Exception as e:
        log.error("database connection failed: %s", e)
        return 1
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM swf_applog")
            before = cur.fetchone()[0]
        log.info("rows before: %d (retention %dd, errors %dd%s)",
                 before, args.days, error_days,
                 ", dry run" if args.dry_run else "")
        deleted = batched_delete(
            conn,
            "timestamp < now() - make_interval(days => %s) "
            "AND levelname NOT IN ('ERROR', 'CRITICAL')",
            (args.days,), args.dry_run)
        deleted += batched_delete(
            conn,
            "timestamp < now() - make_interval(days => %s) "
            "AND levelname IN ('ERROR', 'CRITICAL')",
            (error_days,), args.dry_run)
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM swf_applog")
                after = cur.fetchone()[0]
            log.info("done: removed %d rows, %d remain", deleted, after)
    except Exception as e:
        log.error("retention sweep failed: %s", e)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
