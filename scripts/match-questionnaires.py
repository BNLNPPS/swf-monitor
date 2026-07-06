#!/usr/bin/env python
"""Match production-request questionnaires to catalog tasks (LLM-assisted).

The questionnaire is free text (generator, process, beams, purpose as the
requester wrote them); the catalog side is composed task names built from
tag codes. The delegate model is handed the COMPLETE tag map inline —
every physics/evgen/simu/reco tag with its real content — plus the task
catalog and a batch of requests in one call, so it never has to (and is
forbidden to) guess what a code means: tag codes are opaque sequential
ids, and inferring anything from their numerals is explicitly banned.
That rule exists because the first run, given only composed names, matched
on digit coincidences ("e9 ≈ 9 GeV") — a delegate starved of facts
manufactures them.

Deterministic guards bound the model: proposed names must resolve against
the actual catalog, existing matches are never modified or re-proposed
(additive only), and confidence gates status — high/medium land as
accepted (removable in the UI), low lands as suggested (visible on the
request page, never counted).

Each new match logs a questionnaire_match_found action-stream event
(normal, live) — new matches are events. The run summary is printed as
JSON for the calling agent handler.

Scan set: questionnaires with no matches at all, plus every questionnaire
when the task catalog has grown since the last run (new tasks are new
match candidates; the high-water task id lives in PersistentState).
Requests are batched CHUNK per call — one model, educated once with the
map, serves the whole batch. --all forces a full rescan; --dry-run
proposes without writing; --limit N bounds the scan (testing).
"""
import argparse
import json
import os
import sys

MODEL = os.environ.get('EPICPROD_MATCHER_MODEL', 'claude-opus-4-8')
STATE_KEY = 'epicprod_automatch_last_task_id'
MATCHED_BY = 'automatch'
CHUNK = 25
MAX_TOKENS = 8192

SYSTEM_PROMPT = """\
You match ePIC production requests to the production tasks that realize them.
Requests are free text from physicists (generator, process, beam energies,
purpose). Tasks are named by composed identifiers built from tag codes; the
TAG MAP below defines what every code actually is — generator, physics
process, Q2 range, final state, beams.

HARD RULE: tag codes (pNNNN, eN, sN, rN) are opaque sequential identifiers.
Their numeric values carry NO physics meaning — never infer beam energy,
process, or anything else from the numerals or their adjacency. A code
means ONLY what the tag map says it means.

One request often maps to several tasks (beam-energy variants, campaign
re-runs). Match only when generator, process, and beams genuinely
correspond per the map — an empty result is better than a forced match."""


def compact_params(params):
    if not isinstance(params, dict) or not params:
        return ''
    return ', '.join(f"{k}={v}" for k, v in sorted(params.items())
                     if v not in (None, '', [], {}))


def build_tag_map():
    from pcs.models import EvgenTag, PhysicsTag, RecoTag, SimuTag
    lines = ["TAG MAP — the only source of meaning for tag codes:",
             "", "Physics tags (p):"]
    for t in PhysicsTag.objects.select_related('category').order_by('tag_number'):
        cat = t.category.name if t.category else ''
        detail = compact_params(t.parameters) or t.description
        lines.append(f"{t.tag_label}: {cat} | {detail}")
    lines.append("")
    lines.append("Event-generator tags (e):")
    for t in EvgenTag.objects.order_by('tag_number'):
        detail = t.description or compact_params(t.parameters)
        lines.append(f"{t.tag_label}: {detail}")
    lines.append("")
    lines.append("Simulation tags (s):")
    for t in SimuTag.objects.order_by('tag_number'):
        lines.append(f"{t.tag_label}: {t.description or compact_params(t.parameters)}")
    lines.append("")
    lines.append("Reconstruction tags (r):")
    for t in RecoTag.objects.order_by('tag_number'):
        lines.append(f"{t.tag_label}: {t.description or compact_params(t.parameters)}")
    return "\n".join(lines)


def build_catalog(tasks):
    lines = ["TASK CATALOG (composed name | dataset | sample | status | campaign):"]
    for task in tasks:
        display = task.composed_name or task.name
        if not display:
            continue
        ds = task.dataset
        dataset_name = (ds.dataset_name or '') if ds else ''
        sample = (ds.sample_name or '') if ds else ''
        campaign = task.campaign.name if task.campaign else ''
        lines.append(f"{display} | {dataset_name} | {sample} | {task.status} | {campaign}")
    return "\n".join(lines)


def build_requests_block(chunk, existing_names_by_id):
    lines = ["PRODUCTION REQUESTS to match:"]
    for q in chunk:
        lines.append("")
        lines.append(f"REQUEST id={q.pk}")
        lines.append(f"submitted: {q.submitted_at.date().isoformat()}")
        lines.append(f"description: {q.description.strip() or '(none)'}")
        lines.append(f"repository: {q.repository.strip() or '(none)'}")
        lines.append(f"events requested: {q.nevents.strip() or '(none)'}")
        already = existing_names_by_id.get(q.pk) or set()
        if already:
            lines.append("already matched (do not repeat): "
                         + '; '.join(sorted(already)))
    lines.append(
        "\nReturn ONLY a JSON array, no prose, one entry per request that has "
        "matches (omit requests with none):\n"
        '[{"request_id": <id>, "matches": [{"task_name": "<exact composed '
        'name from the catalog>", "confidence": "high|medium|low", '
        '"reason": "<one short sentence citing the map facts>"}]}]\n'
        "high = certainly this request; medium = very likely; low = plausible "
        "but uncertain.")
    return "\n".join(lines)


def parse_batch(text):
    start, end = text.find('['), text.rfind(']')
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [e for e in parsed if isinstance(e, dict) and e.get('request_id')]


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
    max_task_id = 0
    for task in tasks:
        max_task_id = max(max_task_id, task.pk)
        if task.composed_name:
            by_name[task.composed_name] = task
        if task.name:
            by_name[task.name] = task

    tag_map = build_tag_map()
    catalog = build_catalog(tasks)

    last_seen = int(PersistentState.get_state().get(STATE_KEY) or 0)
    catalog_grew = max_task_id > last_seen

    scan = []
    existing_by_id = {}
    names_by_id = {}
    for q in Questionnaire.objects.all().order_by('id'):
        existing = [m for m in ((q.data or {}).get('prod_matches') or [])
                    if isinstance(m, dict)]
        if not (args.all or catalog_grew or not existing):
            continue
        scan.append(q)
        existing_by_id[q.pk] = existing
        names_by_id[q.pk] = {m.get('task_name') for m in existing
                             if m.get('task_name')}
    if args.limit > 0:
        scan = scan[:args.limit]

    client = anthropic.Anthropic()
    summary = {"scanned": len(scan), "llm_calls": 0, "new_matches": 0,
               "accepted": 0, "suggested": 0, "unknown_names": 0,
               "model": MODEL, "dry_run": bool(args.dry_run)}

    for i in range(0, len(scan), CHUNK):
        chunk = scan[i:i + CHUNK]
        by_pk = {q.pk: q for q in chunk}
        prompt = "\n\n".join([tag_map, catalog,
                              build_requests_block(chunk, names_by_id)])
        response = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}])
        summary["llm_calls"] += 1
        reply = ''.join(b.text for b in (response.content or [])
                        if getattr(b, 'type', '') == 'text')

        for entry in parse_batch(reply):
            q = by_pk.get(int(entry['request_id'])) if str(
                entry['request_id']).isdigit() else None
            if q is None:
                continue
            existing = existing_by_id[q.pk]
            existing_ids = {m.get('task_id') for m in existing}
            added = []
            for match in entry.get('matches') or []:
                if not isinstance(match, dict):
                    continue
                task = by_name.get(str(match.get('task_name') or '').strip())
                if task is None:
                    summary["unknown_names"] += 1
                    continue
                if task.pk in existing_ids:
                    continue
                confidence = str(match.get('confidence') or '').strip().lower()
                if confidence not in ('high', 'medium', 'low'):
                    confidence = 'low'
                status = ('accepted' if confidence in ('high', 'medium')
                          else 'suggested')
                record = {
                    'task_id': task.pk,
                    'task_name': task.composed_name or task.name,
                    'legacy_name': task.name,
                    'confidence': confidence,
                    'status': status,
                    'reason': str(match.get('reason') or '').strip()[:300],
                    'matched_by': MATCHED_BY,
                    'matched_at': timezone.now().isoformat(),
                }
                existing.append(record)
                existing_ids.add(task.pk)
                added.append(record)
                summary["new_matches"] += 1
                summary["accepted" if status == 'accepted' else "suggested"] += 1

            if added and not args.dry_run:
                data = dict(q.data or {})
                data['prod_matches'] = existing
                q.data = data
                q.save(update_fields=['data', 'updated_at'])
                for record in added:
                    log_epicprod_action(
                        'ops-agent', 'questionnaire_match_found',
                        subject_type='campaign_task',
                        subject_key=record['task_name'],
                        username=args.created_by,
                        sublevel='normal', live_default=True,
                        questionnaire=q.pk, confidence=record['confidence'],
                        match_status=record['status'],
                        summary=f"request #{q.pk} ({record['confidence']}): "
                                f"{record['reason'][:120]}")
            elif added:
                for record in added:
                    print(f"DRY RUN: request #{q.pk} -> {record['task_name']} "
                          f"({record['confidence']}, {record['status']}): "
                          f"{record['reason']}", file=sys.stderr)

    if not args.dry_run:
        PersistentState.update_state({STATE_KEY: max_task_id})
        if summary["new_matches"]:
            rebuild_questionnaire_match_cache(updated_by=args.created_by)

    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
