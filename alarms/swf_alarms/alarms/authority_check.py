"""Alarm: authority_check.

Every GitHub sign-in on epic-devcloud.org must end in a membership write
that this side accepted: swf-remote observes the person's `eic`
organisation membership and posts it to /api/user-authority/, which stamps
`eic_at` (docs/AUTHORITY.md). A sign-in that does not is a person who cannot
act and has not been told why. From the 9/9 backfill to 9/25 no sign-in wrote
anything — swf-remote kept no OAuth token to ask GitHub with — and nothing
said so; a collaborator in the organisation reported that he could not act.

Two detections, both per account, both cleared by a completed check:

- **check failed**: the sweep reported a check that reached no answer (no
  stored token, GitHub unanswered, the write refused). The record carries
  `check_failed` until the next observation replaces it.
- **never recorded**: an account that appeared here more than `grace_hours`
  ago, on or after `since`, with no authority record at all. This is the
  9/21 case, and it also catches a failure report that never arrived. Local
  accounts synced from inside the perimeter land here too until someone
  grants them rights, which is equally a person who cannot act.
"""
from __future__ import annotations

from ..common import Detection

PARAMS = {
    # Accounts that appeared before the guard existed were settled by the
    # 9/25 re-observation; the never-recorded check starts from there.
    "since": "2026-09-25",
    # A sign-in writes within seconds; an hour clear of it is a failure.
    "grace_hours": 1,
    # Service identities swf-remote presents over the tunnel. They are
    # auto-created as accounts here and hold no authority by design; the
    # list is swf-remote's ACCOUNT_USERNAME_BLACKLIST.
    "service_accounts": ["swf-remote-authority", "swf-remote-proxy",
                         "swf-remote-sync", "swf-sweeper", "swf-alarms"],
}

FAILED_QUERY = """
    SELECT username,
           prefs->'authority'->>'check_failed'    AS reason,
           prefs->'authority'->>'check_failed_at' AS failed_at,
           prefs->'authority'->>'eic_at'          AS eic_at,
           prefs->'authority'->>'github'          AS github
    FROM user_preference
    WHERE prefs->'authority'->>'check_failed_at' IS NOT NULL
    ORDER BY username
"""

UNRECORDED_QUERY = """
    SELECT u.username, u.date_joined
    FROM auth_user u
    LEFT JOIN user_preference p ON p.username = u.username
    WHERE u.date_joined >= %(since)s::date
      AND u.date_joined < now() - %(grace)s * interval '1 hour'
      AND (p.username IS NULL OR p.prefs->'authority' IS NULL)
      AND NOT (u.username = ANY(%(service)s))
    ORDER BY u.date_joined
"""

REMEDY = (
    "Reading is unaffected; acting is refused until a check completes. "
    "The person can sign in again at https://epic-devcloud.org/prod/ to "
    "retry. On ec2dev, swf-remote's log carries the cause "
    "(grep 'authority:'), and scripts/backfill_authority.py --eic-only "
    "--apply re-observes every GitHub account without touching rights. "
    "An account with no GitHub identity needs rights granted on the User "
    "admin page.")


def detect(client, params):
    grace = float(params.get("grace_hours", 1))
    with client.db_conn.cursor() as cur:
        cur.execute(FAILED_QUERY)
        failed = cur.fetchall()
        cur.execute(UNRECORDED_QUERY, {
            "since": params.get("since", "2026-09-25"),
            "grace": grace,
            "service": list(params.get("service_accounts", [])),
        })
        unrecorded = cur.fetchall()

    for row in failed:
        who = row["username"]
        login = row["github"] or who
        yield Detection(
            dedupe_key=f"authority_check:failed:{who}",
            subject=(f"sign-in membership check failed for {who} "
                     f"(GitHub {login}): {row['reason']}"),
            body_context=(
                f"The membership check at {who}'s sign-in "
                f"({row['failed_at']}) reached no answer: {row['reason']}. "
                f"Last completed check: {row['eic_at'] or 'never'}. "
                + REMEDY),
            extra_data={"username": who, "github": row["github"],
                        "reason": row["reason"],
                        "check_failed_at": row["failed_at"],
                        "eic_at": row["eic_at"]},
        )

    for row in unrecorded:
        who = row["username"]
        joined = row["date_joined"].isoformat()
        yield Detection(
            dedupe_key=f"authority_check:unrecorded:{who}",
            subject=f"account {who} has no authority record, {grace:g} h after it appeared",
            body_context=(
                f"{who} first reached swf-monitor at {joined} and no "
                "membership or rights have been written for the name: the "
                "sign-in's check neither completed nor reported a failure. "
                + REMEDY),
            extra_data={"username": who, "date_joined": joined},
        )
