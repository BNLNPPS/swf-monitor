#!/usr/bin/env python
"""Match production-request questionnaires to catalog tasks (LLM-assisted).

The questionnaire is free text (generator, process, beams, purpose as the
requester wrote them); the catalog side is composed task names carrying the
same physics compactly. An LLM proposes matches; deterministic guards make
them safe: proposed names must resolve against the actual catalog, existing
matches are never modified or re-proposed (additive only), and confidence
gates the status — high/medium land as accepted (removable in the UI),
low lands as suggested (visible on the questionnaire page, never counted).

Each new match is logged to the epicprod action stream as
questionnaire_match_found (normal, live) — new matches are events. The run
summary is printed as JSON for the calling agent handler.

Scan set: questionnaires with no matches at all, plus every questionnaire
when the task catalog has grown since the last run (new tasks are new match
candidates; the high-water task id lives in PersistentState). --all forces
a full rescan; --dry-run proposes without writing.
"""
import argparse
import json
import os
import sys

MODEL = os.environ.get('EPICPROD_MATCHER_MODEL', 'claude-sonnet-5')
STATE_KEY = 'epicprod_automatch_last_task_id'
MATCHED_BY = 'automatch'

SYSTEM_PROMPT = """\
You match ePIC production requests to the production tasks that realize them.
Requests are free text from physicists (generator, process, beam energies,
purpose). Tasks are named by composed identifiers carrying campaign,
generator+version, physics process, beam energies, and radiation settings.
One request often maps to several tasks (beam-energy variants, campaign
re-runs). Match only when generator/process/beams genuinely correspond —
an empty result is better than a forced match."""


def build_user_prompt(questionnaire, already, catalog_lines):
    parts = [
        "PRODUCTION REQUEST",
        f"submitted: {questionnaire.submitted_at.date().isoformat()}",
        f"description: {questionnaire.description.strip() or '(none)'}",
        f"repository: {questionnaire.repository.strip() or '(none)'}",
        f"events requested: {questionnaire.nevents.strip() or '(none)'}",
    ]
    if already:
        parts.append("\nALREADY MATCHED (do not repeat these):")
        parts.extend(f"- {name}" for name in sorted(already))
    parts.append("\nTASK CATALOG (one per line: composed name | status | campaign):")
    parts.extend(catalog_lines)
    parts.append(
        "\nReturn ONLY a JSON array, no prose:\n"
        '[{"task_name": "<exact composed name from the catalog>", '
        '"confidence": "high|medium|low", "reason": "<one short sentence>"}]\n'
        "Empty array [] if nothing matches. high = certainly this request; "
        "medium = very likely; low = plausible but uncertain.")
    return "\n".join(parts)


def parse_matches(text):
    start, end = text.find('['), text.rfind(']')
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [m for m in parsed if isinstance(m, dict) and m.get('task_name')]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--created-by", default="automatch")
    parser.add_argument("--all", action="store_true",
                        help="rescan every questionnaire")
    parser.add_argument("--limit", type=int, default=0,
                        help="scan at most N questionnaires (testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="propose matches without writing anything")
    args = parser.parse_args()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "swf_monitor_project.settings")
    import django
    django.setup()
    import anthropic
    from django.utils import timezone
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import PersistentState
    from pcs.models import ProdTask, Questionnaire
    from pcs.services import rebuild_questionnaire_match_cache

    tasks = list(ProdTask.objects.select_related('dataset', 'campaign')
                 .order_by('id'))
    by_name = {}
    catalog_lines = []
    max_task_id = 0
    for task in tasks:
        max_task_id = max(max_task_id, task.pk)
        display = task.composed_name or task.name
        if not display:
            continue
        if task.composed_name:
            by_name[task.composed_name] = task
        if task.name:
            by_name[task.name] = task
        campaign = task.campaign.name if task.campaign else ''
        catalog_lines.append(f"{display} | {task.status} | {campaign}")

    last_seen = int(PersistentState.get_state().get(STATE_KEY) or 0)
    catalog_grew = max_task_id > last_seen

    questionnaires = list(Questionnaire.objects.all().order_by('id'))
    scan = []
    for q in questionnaires:
        existing = (q.data or {}).get('prod_matches') or []
        if args.all or catalog_grew or not existing:
            scan.append(q)
    if args.limit > 0:
        scan = scan[:args.limit]

    client = anthropic.Anthropic()
    summary = {"scanned": 0, "llm_calls": 0, "new_matches": 0, "accepted": 0,
               "suggested": 0, "unknown_names": 0, "model": MODEL,
               "dry_run": bool(args.dry_run)}

    for q in scan:
        data = dict(q.data or {})
        existing = [m for m in (data.get('prod_matches') or [])
                    if isinstance(m, dict)]
        existing_ids = {m.get('task_id') for m in existing}
        existing_names = {m.get('task_name') for m in existing if m.get('task_name')}
        summary["scanned"] += 1

        prompt = build_user_prompt(q, existing_names, catalog_lines)
        response = client.messages.create(
            model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}])
        summary["llm_calls"] += 1
        reply = ''.join(b.text for b in (response.content or [])
                        if getattr(b, 'type', '') == 'text')
        proposed = parse_matches(reply)

        added = []
        for match in proposed:
            task = by_name.get(str(match.get('task_name') or '').strip())
            if task is None:
                summary["unknown_names"] += 1
                continue
            if task.pk in existing_ids:
                continue
            confidence = str(match.get('confidence') or '').strip().lower()
            if confidence not in ('high', 'medium', 'low'):
                confidence = 'low'
            status = 'accepted' if confidence in ('high', 'medium') else 'suggested'
            entry = {
                'task_id': task.pk,
                'task_name': task.composed_name or task.name,
                'legacy_name': task.name,
                'confidence': confidence,
                'status': status,
                'reason': str(match.get('reason') or '').strip()[:300],
                'matched_by': MATCHED_BY,
                'matched_at': timezone.now().isoformat(),
            }
            existing.append(entry)
            existing_ids.add(task.pk)
            added.append(entry)
            summary["new_matches"] += 1
            summary["accepted" if status == 'accepted' else "suggested"] += 1

        if added and not args.dry_run:
            data['prod_matches'] = existing
            q.data = data
            q.save(update_fields=['data', 'updated_at'])
            for entry in added:
                log_epicprod_action(
                    'ops-agent', 'questionnaire_match_found',
                    subject_type='campaign_task', subject_key=entry['task_name'],
                    username=args.created_by,
                    sublevel='normal', live_default=True,
                    questionnaire=q.pk, confidence=entry['confidence'],
                    match_status=entry['status'],
                    summary=f"request #{q.pk} ({entry['confidence']}): "
                            f"{entry['reason'][:120]}")
        elif added:
            for entry in added:
                print(f"DRY RUN: request #{q.pk} -> {entry['task_name']} "
                      f"({entry['confidence']}, {entry['status']}): "
                      f"{entry['reason']}", file=sys.stderr)

    if not args.dry_run:
        PersistentState.update_state({STATE_KEY: max_task_id})
        if summary["new_matches"]:
            rebuild_questionnaire_match_cache(updated_by=args.created_by)

    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
