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
(normal, recorded but not live). The live channel gets exactly one line
per run with new matches — a questionnaire_new_matches summary event
whose record page lists every new match with confidence and reason. The
run summary is printed as JSON for the calling agent handler.

Rescanning is event-driven, never habitual: an LLM's second answer to the
same question differs from its first, so re-asking unchanged questions
harvests variance — noise in the live stream, misleading reporting, wasted
tokens. Each questionnaire carries a stamp of what it was last scanned
against (data['automatch_scan']: prompt version, task-catalog high-water
id, request content hash) and is asked again only when one of those inputs
changed — the request was edited, the catalog grew, or the matcher prompt
was revised (bump PROMPT_VERSION with any prompt change; that triggers one
deliberate re-pass). A night with no changes makes no LLM call and emits
no events. --all forces a full rescan; --dry-run proposes without writing;
--limit N bounds the scan (testing). Requests are batched CHUNK per call —
one model, educated once with the map, serves the whole batch.

The delegate runs through the Claude Code CLI (``claude -p``) under the
account's subscription login — never the metered API. The API key is
stripped from the call environment so a misconfigured CLI cannot fall back
to per-token billing: this automated path exhausted the monthly API budget
once (2026-07-15) and must not be able to again.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

MODEL = os.environ.get('EPICPROD_MATCHER_MODEL', 'claude-opus-4-8')
STATE_KEY = 'epicprod_automatch_last_task_id'
MATCHED_BY = 'automatch'
CHUNK = 25
CLAUDE_BIN = os.environ.get('EPICPROD_MATCHER_CLAUDE') or (
    shutil.which('claude') or os.path.expanduser('~/.local/bin/claude'))
# Per delegate call; 4 chunk calls must fit the agent handler's 1800s budget.
CALL_TIMEOUT = 420
# Bump with ANY change to SYSTEM_PROMPT or the request/catalog presentation:
# a changed matcher is a changed question, and every questionnaire earns one
# deliberate re-pass against it.
PROMPT_VERSION = 2
SCAN_STAMP_KEY = 'automatch_scan'

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

HARD RULE: beam energies and species must match EXACTLY per the tag map.
9x100 never matches 10x100; ep never matches en; eAu never matches eHe3.
If the requested beams do not exist in the catalog, return no match for
that request — never propose the nearest beam.

One request often maps to several tasks (beam-energy variants, campaign
re-runs). Match only when generator, process, and beams genuinely
correspond per the map — an empty result is better than a forced match."""


def scan_content_hash(q):
    """Hash of the request fields the matcher reads — a change means the
    question changed and the questionnaire earns a rescan."""
    basis = '|'.join([q.description or '', q.repository or '', q.nevents or ''])
    return hashlib.sha256(basis.encode('utf-8')).hexdigest()[:16]


def norm_energy(value):
    """'10' == '10.0' == 10; unparseable -> ''."""
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return ''
    return str(int(f)) if f == int(f) else str(f)


def task_beams(task):
    """(electron, hadron, species) from the task's physics tag — DB truth."""
    ds = task.dataset
    p = (ds.physics_tag.parameters
         if ds is not None and ds.physics_tag_id else {}) or {}
    return (norm_energy(p.get('beam_energy_electron')),
            norm_energy(p.get('beam_energy_hadron')),
            str(p.get('beam_species') or '').strip().lower())


def beam_verdict(match, task):
    """Deterministic beam gate: 'ok', 'reject', or 'unverified'.

    The model states the request's beams; the task's beams come from its
    physics tag. Exact equality is required — the prompt alone does not
    hold this line (observed proposing cross-beam matches at medium
    confidence while confessing the mismatch in its own reason).
    """
    t_e, t_h, t_species = task_beams(task)
    req = str(match.get('request_beams') or '').strip().lower()
    req_species = str(match.get('request_species') or '').strip().lower()
    if not (t_e and t_h):
        return 'unverified'          # tag carries no beams — cannot verify
    if not req or 'x' not in req:
        return 'unverified'          # request states no beams — cannot verify
    r_e, _, r_h = req.partition('x')
    if (norm_energy(r_e), norm_energy(r_h)) != (t_e, t_h):
        return 'reject'
    if req_species and t_species and req_species != t_species:
        return 'reject'
    return 'ok'


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
        '"request_beams": "<electron>x<hadron> exactly as the request states '
        'them, or \\"\\" if the request does not state beams", '
        '"request_species": "<beam species the request states (ep, en, eAu, '
        'eHe3, ...), or \\"\\">", '
        '"reason": "<one short sentence citing the map facts>"}]}]\n'
        "high = certainly this request; medium = very likely; low = plausible "
        "but uncertain.")
    return "\n".join(lines)


def call_delegate(prompt):
    """One subscription-billed delegate call via the Claude Code CLI.

    Tools and setting sources are disabled — this is a pure text
    completion, and the account's Claude Code hooks/config must not fire.
    A non-zero exit or timeout raises: the script fails loudly and the
    agent handler logs the error to the action stream.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')}
    cmd = [CLAUDE_BIN, '-p', '--model', MODEL,
           '--system-prompt', SYSTEM_PROMPT,
           '--tools', '', '--setting-sources', '']
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=CALL_TIMEOUT, env=env)
    if p.returncode != 0:
        raise RuntimeError(
            f"claude -p failed rc={p.returncode}: {(p.stderr or '')[:500]}")
    return p.stdout or ''


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
    from django.utils import timezone
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import PersistentState
    from pcs.models import ProdTask, Questionnaire
    from pcs.services import rebuild_questionnaire_match_cache

    tasks = list(ProdTask.objects
                 .select_related('dataset__physics_tag', 'campaign')
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

    scan = []
    existing_by_id = {}
    names_by_id = {}
    skipped_unchanged = 0
    for q in Questionnaire.objects.all().order_by('id'):
        existing = [m for m in ((q.data or {}).get('prod_matches') or [])
                    if isinstance(m, dict)]
        # Event-driven rescan: skip unless the request content, the task
        # catalog, or the matcher prompt changed since this questionnaire
        # was last scanned. Re-asking an unchanged question harvests LLM
        # variance, not information.
        stamp = (q.data or {}).get(SCAN_STAMP_KEY) or {}
        unchanged = (
            stamp.get('prompt_version') == PROMPT_VERSION
            and int(stamp.get('task_high_water') or 0) >= max_task_id
            and stamp.get('content_hash') == scan_content_hash(q)
        )
        if unchanged and not args.all:
            skipped_unchanged += 1
            continue
        scan.append(q)
        existing_by_id[q.pk] = existing
        names_by_id[q.pk] = {m.get('task_name') for m in existing
                             if m.get('task_name')}
    if args.limit > 0:
        scan = scan[:args.limit]

    summary = {"scanned": len(scan), "skipped_unchanged": skipped_unchanged,
               "llm_calls": 0, "new_matches": 0,
               "accepted": 0, "suggested": 0, "unknown_names": 0,
               "beam_rejected": 0, "beam_unverified_demoted": 0,
               "model": MODEL, "transport": "claude-cli-subscription",
               "prompt_version": PROMPT_VERSION,
               "dry_run": bool(args.dry_run)}
    run_added = []

    for i in range(0, len(scan), CHUNK):
        chunk = scan[i:i + CHUNK]
        by_pk = {q.pk: q for q in chunk}
        prompt = "\n\n".join([tag_map, catalog,
                              build_requests_block(chunk, names_by_id)])
        reply = call_delegate(prompt)
        summary["llm_calls"] += 1

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
                verdict = beam_verdict(match, task)
                if verdict == 'reject':
                    summary["beam_rejected"] += 1
                    print(f"BEAM REJECT: request #{q.pk} -> "
                          f"{match.get('task_name')} request_beams="
                          f"{match.get('request_beams')!r} species="
                          f"{match.get('request_species')!r}", file=sys.stderr)
                    continue
                confidence = str(match.get('confidence') or '').strip().lower()
                if confidence not in ('high', 'medium', 'low'):
                    confidence = 'low'
                status = ('accepted' if confidence in ('high', 'medium')
                          else 'suggested')
                if verdict == 'unverified' and status == 'accepted':
                    summary["beam_unverified_demoted"] += 1
                    status = 'suggested'
                record = {
                    'task_id': task.pk,
                    'task_name': task.composed_name or task.name,
                    'legacy_name': task.name,
                    'confidence': confidence,
                    'status': status,
                    'request_beams': str(match.get('request_beams') or '').strip(),
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
                run_added.extend((q.pk, record) for record in added)
                for record in added:
                    log_epicprod_action(
                        'ops-agent', 'questionnaire_match_found',
                        subject_type='campaign_task',
                        subject_key=record['task_name'],
                        username=args.created_by,
                        sublevel='normal', live_default=False,
                        questionnaire=q.pk, confidence=record['confidence'],
                        match_status=record['status'],
                        summary=f"request #{q.pk} ({record['confidence']}): "
                                f"{record['reason'][:120]}")
            elif added:
                for record in added:
                    print(f"DRY RUN: request #{q.pk} -> {record['task_name']} "
                          f"({record['confidence']}, {record['status']}): "
                          f"{record['reason']}", file=sys.stderr)

        # Stamp every scanned questionnaire — matched or not — with what it
        # was scanned against, so it stays quiet until an input changes.
        if not args.dry_run:
            for q in chunk:
                data = dict(q.data or {})
                data[SCAN_STAMP_KEY] = {
                    'prompt_version': PROMPT_VERSION,
                    'task_high_water': max_task_id,
                    'content_hash': scan_content_hash(q),
                    'scanned_at': timezone.now().isoformat(),
                }
                q.data = data
                q.save(update_fields=['data', 'updated_at'])

    if run_added:
        # The one live line for the whole run; its record page lists every
        # new match (multi-line message renders in the record's pre block).
        one_line = (f"{summary['new_matches']} new matches "
                    f"({summary['accepted']} accepted, "
                    f"{summary['suggested']} suggested) "
                    f"from {summary['scanned']} requests scanned")
        detail = [one_line] + [
            f"request #{qpk} -> {r['task_name']} "
            f"({r['confidence']}, {r['status']}): {r['reason']}"
            for qpk, r in run_added]
        log_epicprod_action(
            'ops-agent', 'questionnaire_new_matches',
            username=args.created_by,
            sublevel='normal', live_default=True,
            message="\n".join(detail), summary=one_line,
            requests_matched=len({qpk for qpk, _ in run_added}))

    if not args.dry_run:
        PersistentState.update_state({STATE_KEY: max_task_id})
        if summary["new_matches"]:
            rebuild_questionnaire_match_cache(updated_by=args.created_by)

    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
