"""epicprod action logging — the filterable action stream in AppLog.

Convention: ``app_name='epicprod'`` is the epicprod ACTION stream. Every
state-changing or operationally significant action — a catalog button press,
an MCP action tool, an ops-agent handler, a sweep, a submission, a report
generation — records one row here, regardless of which process performed it.
Process and infrastructure logs stay under their own app names; this stream
answers "what happened, and how did it go", and is the primary corpus
for LLM assessment and reporting.

``instance_name`` names the component performing the action: 'web',
'ops-agent', 'mcp', 'catalog-sync', 'submit', 'report'.

Structured fields live in ``extra_data``; reserved keys are ``action``,
``subject_type``, ``subject_key``, ``username``, ``outcome``, and
``duration_ms``. Additional numeric counts (rows_added=..., tasks_matched=...)
are stored alongside them. Timed actions (sweeps in particular) must pass
``duration_ms``.

This helper writes via the ORM and serves in-process Django contexts (web
tier, MCP ASGI worker, management commands). Out-of-process callers such as
the ops agent record actions through the existing REST log endpoint with the
same app_name and field conventions.

Retrieval: the ``epicprod_list_actions`` MCP tool (filtered and summarized),
``swf_list_logs(app_name='epicprod')`` (raw), and the Logs UI filtered on
app_name.

Publication axes (log level is separate and keeps its universal meaning):

``sublevel`` — the event's declared importance (high | normal | low),
set at the call site, AUTHORITATIVE: changing it means changing the event.
It says which humans an event reaches — high reaches everyone including
email/digest audiences, normal reaches live-page watchers, low reaches only
the deliberately verbose viewer.

``live_default`` — the event's declared RECOMMENDATION for the live stream,
a special category: "interesting to some humans, now". The effective live
decision is the ``epicprod_live_policy`` override registry in SysConfig
(runtime attention knob, flipped on the live-policy page) over the default.

A channel = its importance threshold applied to live events:
``live_stream_q(min_sublevel=...)`` is the one filter every channel uses —
the deep live view takes all live events; an email digest takes live events
at importance high.
"""

import logging
import os
import threading

from django.utils import timezone

logger = logging.getLogger(__name__)

EPICPROD_APP_NAME = 'epicprod'

LIVE_POLICY_STATE_KEY = 'epicprod_live_policy'

SUBLEVEL_VALUES = ('high', 'normal', 'low')
SUBLEVEL_ORDER = {'high': 2, 'normal': 1, 'low': 0}

# Catalog of known actions: each action's importance and live default when it
# succeeds, by their meaning (docs/ACTION_STREAM.md, Publication axes): high =
# a problem a person should look at now, normal = what people and the front
# do to production, low = routine mechanics, never live by default. The write
# applies it (publication, from AppLog.save), whoever writes the record; a
# failure is high. ``outcomes`` gives one outcome its own declaration.
# description: the plain-English one-liner answering "what is this action",
# rendered on the log entry page and the live-policy page, so a stream reader
# is never left guessing what an event was.
ACTION_DEFAULTS = {
    # the PanDA database's Event Service range rows (swf-epicprod
    # docs/ES_EVENT_CLEANUP.md)
    'es_event_cleanup': {
        'sublevel': 'low', 'live': False,
        'description': "The daily removal of the Event Service range rows "
                       "(JEDI_Events) of tasks done or aborted more than three "
                       "days ago: the rows removed and the tasks.",
    },
    # the pressure front (swf-epicprod docs/CONTINUOUS_PRODUCTION.md,
    # The dispatcher)
    'front_decision': {
        'sublevel': 'low', 'live': False,
        'outcomes': {'fed': {'sublevel': 'normal', 'live': True}},
        'description': "One queue's decision of the pressure front's cycle: "
                       "feed or hold, with the reason, the depth in hours, "
                       "the set points and the gates; recorded on every "
                       "feed, on a change of state or reason, and hourly.",
    },
    'front_cycle': {
        'sublevel': 'low', 'live': False,
        'description': "One cycle of the pressure front over the regulated "
                       "queues, with each queue's state.",
    },
    # OSG pool throttles (docs/OSG_SUBMIT_REPORTER.md, Fair-share limit)
    'osg_throttle': {
        'sublevel': 'low', 'live': False,
        'description': "A pool our OSG pilots run in held our submitter at "
                       "its fair-share limit, so it gives our pilots no new "
                       "slots (state held, informational: OSG's normal "
                       "mode of operation), or released it after "
                       "three consecutive clear readings (state lifted); "
                       "read from each pool negotiator's last cycle by the "
                       "OSG submit host reporter.",
    },
    # the node guard (site-canary docs/NODE_GUARD.md)
    'node_guard_decision': {
        'sublevel': 'normal', 'live': True,
        'outcomes': {'would_exclude': {'sublevel': 'low', 'live': False}},
        'description': "One node's verdict from the node guard's cycle: a "
                       "black hole tripped (would_exclude in shadow mode, "
                       "excluded live) or cleared after a trip, with the "
                       "evidence; recorded on a trip, on the clear after "
                       "one, and hourly while tripped.",
    },
    'node_guard_cycle': {
        'sublevel': 'low', 'live': False,
        'description': "One cycle of the node guard over the window's "
                       "terminal production jobs: jobs, hosts, judged and "
                       "tripped, and any queue read as a storm.",
    },
    # the preemption close-out (swf-epicprod swf_epicprod/es_closeout.py)
    'es_closeout': {
        'sublevel': 'low', 'live': False,
        'description': "One Event Service job at a preemptible queue closed "
                       "after its node was lost: the ranges of every close "
                       "that stood credited and the job finished, or why "
                       "the server refused.",
    },
    'es_closeout_cycle': {
        'sublevel': 'low', 'live': False,
        'description': "One cycle of the preemption close-out over the "
                       "running Event Service jobs of the listed queues: "
                       "jobs, quiet, closed, failed.",
    },
    'worker_record_capture': {
        'sublevel': 'low', 'live': False,
        'description': "One pass copying the NERSC queues' harvester worker "
                       "records into our store before PanDA's three-month "
                       "window drops them: read, created, updated, unchanged.",
    },
    'harvester_stdout_capture': {
        'sublevel': 'low', 'live': False,
        'description': "One pass copying the harvester's stdout of the jobs "
                       "worth keeping at the cache-stdout queues into our "
                       "store before the PanDA cache purges it: candidates, "
                       "captured, gone, failed, skipped.",
    },
    'node_guard_set': {
        'sublevel': 'normal', 'live': True,
        'description': "A person's decision on one node of the guard's "
                       "record: clear, pin, unpin, or a black hole opened "
                       "by hand.",
    },
    # alarm dashboard: pings (docs/PINGS.md)
    'ping_create': {
        'sublevel': 'normal', 'live': True,
        'description': "Enter a ping, a dated obligation the alarm system "
                       "raises ahead of its due date, on the alarm "
                       "dashboard.",
    },
    # Production now: a person's informational line (docs/COMP_SNAPSHOT.md)
    'prod_now_notice': {
        'sublevel': 'normal', 'live': False,
        'description': "Put an informational line, such as a production "
                       "milestone, on Production now for a set number of "
                       "hours.",
    },
    # Help menu: What's new (docs/WHATS_NEW.md)
    'whats_new_create': {
        'sublevel': 'normal', 'live': True,
        'description': "Add an item to What's new, the list of developments "
                       "users, operators and developers should know about; "
                       "posted to epicprod-live and shown on the live "
                       "screen at INFO.",
    },
    'whats_new_archive': {
        'sublevel': 'normal', 'live': False,
        'description': "Take a What's new item off the list, or return it.",
    },
    'ping_fulfil': {
        'sublevel': 'normal', 'live': True,
        'description': "Record a ping's obligation as fulfilled; its alarm "
                       "event clears on the engine's next tick.",
    },
    'prodconfig_vo': {
        'sublevel': 'normal', 'live': False,
        'description': "Change a production configuration's VO for future "
                       "submissions; existing task identities stay unchanged.",
    },
    'standard_prodconfig_create': {
        'sublevel': 'normal', 'live': True,
        'description': "Create a campaign edition's Standard Production "
                       "configuration from the template, by the script or "
                       "an approved standard_config proposal, the remedy of "
                       "a campaign configuration ping.",
    },
    'registered_sample_intake': {
        'sublevel': 'normal', 'live': True,
        'description': "Take a registered EVGEN sample nobody requested into "
                       "the record: its EVGEN-stage record, the production "
                       "edition on the campaign's release pair, the request "
                       "with requestor, target and priority, and the draft "
                       "task, by hand or by an approved registered_sample "
                       "proposal.",
    },
    'prodtask_adopt': {
        'sublevel': 'normal', 'live': True,
        'description': "Move a name-matched legacy task into PCS: its latest "
                       "PanDA try recorded as the submission and the edition's "
                       "Standard Production configuration bound, so the retry "
                       "family applies. Nothing is submitted.",
    },
    'walltime_queue_cap': {
        'sublevel': 'normal', 'live': True,
        'description': "A residual rerun's declared walltime held under its "
                       "queue's maxtime at submission (JEDI_INTEGRATION.md "
                       "§ Residual rerun), carrying severity for notice "
                       "routing: warning when the attempt's own jobs ran "
                       "longer than the queue declares (the queue "
                       "declaration is wrong), info when the estimate alone "
                       "was capped.",
    },
    # ops agent
    'task_submit': {
        'sublevel': 'normal', 'live': True,
        'description': "Submit a campaign task to PanDA through the prun doer "
                       "under the production credential and record the "
                       "returned JEDI task id.",
    },
    'evgen_task_submit': {
        'sublevel': 'normal', 'live': True,
        'description': "Submit an external-EVGEN campaign task to PanDA/JEDI "
                       "via the client API: build the task parameters, "
                       "assemble and upload the sandbox, record the JEDI "
                       "task id.",
    },
    'panda_task_operation': {
        'sublevel': 'normal', 'live': True,
        'description': "Run a native PanDA operation on an existing JEDI "
                       "task: pause or resume the task, raise the allowed "
                       "attempt count, or retry failed work.",
    },
    'rucio_sweep': {
        'sublevel': 'low', 'live': False,
        'description': "Refresh the JLab Rucio output snapshot for the "
                       "current (and last) campaign and rematch produced "
                       "RECO/FULL datasets onto each task's recorded outputs.",
    },
    'campaign_instancing': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator-fired campaign instancing: populate the "
                       "target campaign's working catalog from the source "
                       "campaign's continuing physics configurations — "
                       "adopting ingested editions and minting planned "
                       "ones, dispositions consumed, unresolved "
                       "identities left to curation.",
    },
    'campaign_promoted': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator-clicked campaign lifecycle rotation: the "
                       "named (producing) campaign becomes current, the "
                       "incumbent current becomes last, the incumbent last "
                       "becomes past.",
    },
    'past_import': {
        'sublevel': 'low', 'live': False,
        'description': "Pull the eic/epic-prod bookkeeping clone and re-run "
                       "the idempotent FULL/RECO past-campaign output "
                       "ingest, keeping every campaign's recorded "
                       "production content current with what the "
                       "production team publishes.",
    },
    'rucio_reconcile': {
        'sublevel': 'low', 'live': False,
        'description': "Firsthand reconciliation of a producing "
                       "campaign's catalog records against its fetched "
                       "Rucio snapshot: known DIDs updated, unknown DIDs "
                       "resolved to physics configurations and attached "
                       "to existing editions or created, unresolved left "
                       "to curation.",
    },
    'rucio_arrivals': {
        'sublevel': 'low', 'live': False,
        'description': "New files landed in JLab Rucio since the last "
                       "arrivals sweep, counted by campaign and location "
                       "across all versions — the signal behind the "
                       "derived 'producing' campaign status. Emitted on "
                       "every sweep, so the assessment's arrival coverage "
                       "reads a quiet interval as a measured zero; a sweep "
                       "that found nothing is logged low and off the live "
                       "view.",
    },
    'rucio_arrivals_sweep': {
        'sublevel': 'low', 'live': False,
        'description': "The clockwork arrivals-sweep step itself (outcome "
                       "and duration); the arrivals, when any, are the "
                       "rucio_arrivals event.",
    },
    'evgen_sweep': {
        'sublevel': 'low', 'live': False,
        'description': "Assimilate the JLab Rucio EVGEN input inventory "
                       "(epic:/EVGEN/*) and resolve each catalog request to "
                       "the registered input dataset(s) that realize it.",
    },
    'catalog_import': {
        'sublevel': 'low', 'live': False,
        'description': "Import the production CSV manifest catalog: create "
                       "or update campaign task records from the source rows.",
    },
    'association_sweep': {
        'sublevel': 'low', 'live': False,
        'description': "Associate recent PanDA tasks with campaign tasks; "
                       "unmatched direct group.EIC submissions are "
                       "auto-intaken as adopted tasks.",
    },
    'catalog_sync': {
        'sublevel': 'low', 'live': False,
        'description': "Nightly composite chain (cron 02:15): csv import, "
                       "epic-prod past ingest, questionnaire import, "
                       "association sweep, Rucio output snapshot, Rucio "
                       "arrivals sweep, EVGEN assimilation, questionnaire "
                       "automatch, match cache, progress refresh. This "
                       "record is the catalog-freshness timestamp.",
    },
    'agent_shutdown': {
        'sublevel': 'normal', 'live': True,
        'description': "Deliberate stop of the production ops agent via the "
                       "message-bus back door; systemd leaves it stopped.",
    },
    'payload_log_fetch': {
        'sublevel': 'low', 'live': False,
        'description': "Fetch one PanDA job's payload log tarball from Rucio "
                       "over xrootd and extract it into the shared cache for "
                       "the job page.",
    },
    'inventory_sync': {
        'sublevel': 'low', 'live': False,
        'description': "Refresh the monitor's ePIC production job/file "
                       "inventory and parsed failure diagnosis for a PanDA "
                       "job.",
    },
    'segfault_inventory': {
        'sublevel': 'low', 'live': False,
        'description': "The crash-class inventory pass "
                       "(scripts/segfault-inventory.py): payload crashes "
                       "(exit 134/135/136/139) read from the PanDA record "
                       "for a window and written as EpicProdJob rows with "
                       "their manifest row, then the crash signatures of "
                       "the tasks touched; one event per run carrying the "
                       "window, jobs seen, rows added and updated, rows "
                       "unresolved, tasks and signatures (swf-epicprod "
                       "SEGFAULT_DIAGNOSIS.md).",
    },
    'segfault_dig': {
        'sublevel': 'low', 'live': False,
        'description': "A crash signature's dig (scripts/segfault-dig.py): "
                       "one representative crashed job's payload log "
                       "fetched through the doer and its backtrace read "
                       "onto the signature; on operator request from the "
                       "signature page, or the nightly automatic dig of "
                       "the largest signatures never dug (ten a night). "
                       "Carries the trace status and crashing frame, or "
                       "the reason the log was unavailable.",
    },
    'segfault_dig_request': {
        'sublevel': 'low', 'live': False,
        'description': "Operator request of a signature's dig from its "
                       "page, queued to the production operations agent; "
                       "the dig itself is the segfault_dig event.",
    },
    'segfault_reproduce': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator request to reproduce a crash signature: "
                       "the crashed job's own manifest row run again as "
                       "payload canaries on the production queue and the "
                       "reference queue BNL_NPPS_GPU, queued to the canary "
                       "agent; the outcome (reproduced, site_dependent, "
                       "not_reproduced, inconclusive) settles on the "
                       "signature from the runs' payload reports.",
    },
    'segfault_es_reproduction': {
        'sublevel': 'normal', 'live': False,
        'description': "An Event Service unit signature's crash request "
                       "replayed on npps0 (scripts/segfault-es-reproduce.py): "
                       "the request run again through run.sh with the "
                       "resident simulation and, on a resident death, as a "
                       "cold npsim under gdb; carries the outcome (crashed, "
                       "completed, inconclusive), the signal and the frame "
                       "(swf-epicprod SEGFAULT_DIAGNOSIS.md, Event Service units).",
    },
    'segfault_reproduction_reconcile': {
        'sublevel': 'low', 'live': False,
        'description': "Reproduction requests reconciled with their canary "
                       "runs after a probe collection (the canary agent, "
                       "scripts/segfault-reproductions-reconcile.py): each "
                       "open request takes its run's identity and outcome, "
                       "a reported production and reference pair settles the "
                       "signature's reproduction outcome, and a settled "
                       "reproduction with a trace queues its diagnosis once. "
                       "Recorded when something changed.",
    },
    'segfault_diagnose_request': {
        'sublevel': 'low', 'live': False,
        'description': "Operator request of a crash signature's LLM study "
                       "from its page, queued to the production operations "
                       "agent; the submission is segfault_diagnosis_triggered "
                       "and the result segfault_diagnosis.",
    },
    'live_watch_triggered': {
        'sublevel': 'low', 'live': False,
        'description': "A live watch run submitted to corun-ai "
                       "(scripts/live-watch-trigger.py; swf-epicprod "
                       "EPICPROD_ASSESSMENTS.md, The live watch): the channel's "
                       "posts and the action record's failure groups as a hidden "
                       "bundle page, the run created with the live_watch "
                       "definition; outcome error when the submission failed.",
    },
    'live_watch': {
        'sublevel': 'low', 'live': False,
        'description': "A live watch run's result enforced "
                       "(scripts/live-watch-enforce.py): the verdict, the noise "
                       "in the channel and the recurring real problems; notify "
                       "set when what it found changed from the last registered "
                       "run, which is then registered as an assessment; live "
                       "only for a changed alarm.",
    },
    'segfault_diagnosis_triggered': {
        'sublevel': 'low', 'live': False,
        'description': "A crash signature's LLM study submitted to corun-ai "
                       "(scripts/segfault-diagnosis-trigger.py): the evidence "
                       "bundle stored as a hidden page and the run created "
                       "with the segfault_diagnosis definition; outcome error "
                       "when the submission failed.",
    },
    'segfault_diagnosis': {
        'sublevel': 'normal', 'live': True,
        'description': "A crash signature's diagnosis enforced and registered "
                       "(scripts/segfault-diagnosis-enforce.py): the "
                       "classification (software_defect, configuration, "
                       "event_shaped, platform, unresolved), the operator "
                       "action and the narration, carrying severity for notice "
                       "routing (warning for a software defect or a "
                       "configuration finding, info otherwise); outcome repair "
                       "for the one bounded repair run, error for a "
                       "quarantined or failed run.",
    },
    'segfault_finding_set': {
        'sublevel': 'low', 'live': False,
        'description': "A segfault finding created or updated: the curated "
                       "reading of one crashing frame in the entry store "
                       "(kind finding, context segfault), versioned and "
                       "stamped; through the MCP tool or REST.",
    },
    'segfault_package': {
        'sublevel': 'low', 'live': False,
        'description': "A crashed job's reproduction package built for a "
                       "software expert (scripts/segfault-repro-package.py): "
                       "the run script, the payload, the row, the "
                       "environment, the trace and the job records, as a "
                       "tarball the signature page serves.",
    },
    'system_status_refresh': {
        'sublevel': 'low', 'live': False,
        'description': "Periodic refresh of the cached System page status "
                       "rows for services, agents, and external monitor "
                       "endpoints.",
    },
    'release_overdue': {
        'sublevel': 'normal', 'live': False,
        'description': "The release cadence check found a core repo's work "
                       "branch past release_overdue_days without a merge to "
                       "main, or main carrying merges the work branch lacks "
                       "for as long (docs/SYSTEM_STATUS.md, release-cadence). "
                       "Recorded once per Eastern day while it stands; "
                       "Capcom receives it as a warning.",
    },
    'snapper_publication_too_large': {
        'sublevel': 'normal', 'live': False,
        'description': "A Snapper component publication rejected at "
                       "snapper-ai's 1 MiB data limit, the one size limit "
                       "on component data; the component's record stops "
                       "advancing until its projection fits. Capcom "
                       "receives it as an alarm.",
    },
    'questionnaire_import': {
        'sublevel': 'low', 'live': False,
        'description': "Import the PWG/DSC production-request questionnaire "
                       "CSV: create or update request records.",
    },
    'questionnaire_automatch': {
        'sublevel': 'low', 'live': False,
        'description': "LLM matching of production requests to catalog tasks "
                       "using the full tag map; each new match logs its own "
                       "questionnaire_match_found record, and a run with new "
                       "matches posts one live questionnaire_new_matches line.",
    },
    'questionnaire_match_found': {
        'sublevel': 'normal', 'live': False,
        'description': "One new request-to-task match from the automatch, "
                       "with confidence and reason; high/medium confidence "
                       "lands accepted, low lands suggested.",
    },
    'questionnaire_new_matches': {
        'sublevel': 'normal', 'live': True,
        'description': "One-line summary of an automatch run's new "
                       "request-to-task matches; the record lists every new "
                       "match with confidence and reason.",
    },
    'questionnaire_match': {
        'sublevel': 'low', 'live': False,
        'description': "Rebuild the questionnaire-to-task match cache read "
                       "by request and task pages.",
    },
    'progress_refresh': {
        'sublevel': 'low', 'live': False,
        'description': "Rebuild current-campaign progress data and its "
                       "rendered progress table cache.",
    },
    # web and MCP
    'sysconfig_edit': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator edit of the SysConfig system-configuration "
                       "document on the System page.",
    },
    'live_policy_edit': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator change to an action's live-stream override "
                       "on the live-policy page.",
    },
    'live_cleared': {
        'sublevel': 'low', 'live': False,
        'description': "Live records taken off the computing centre's "
                       "epicprod-live screen once the operator's question "
                       "about them is answered: the record ids and why. The "
                       "records themselves are unchanged.",
    },
    'host_reporter_install': {
        'sublevel': 'normal', 'live': True,
        'description': "A host reporter installed on a service host it "
                       "observes (docs/PANDA_SERVER_REPORTER.md, "
                       "docs/OSG_SUBMIT_REPORTER.md): token, environment "
                       "file, and schedule, recorded before the install "
                       "is taken.",
    },
    'workflow_execution_completed': {
        'sublevel': 'low', 'live': False,
        'description': "A testbed workflow execution reached a terminal "
                       "state (completed or failed); carries the run's "
                       "notice stamp for notice routing.",
    },
    'notice_subscription_edit': {
        'sublevel': 'normal', 'live': False,
        'description': "A notice-routing subscription was created, changed, "
                       "or removed through the subscriptions API.",
    },
    'campaign_plan_set': {
        'sublevel': 'normal', 'live': True,
        'description': "Set or remove campaign-plan entries — the "
                       "plan-membership records of a campaign's assembly "
                       "(CONTINUOUS_PRODUCTION.md).",
    },
    'assessment_register': {
        'sublevel': 'normal', 'live': True,
        'description': "Register an AI assessment of a production object "
                       "(campaign task, PanDA task, job, or queue).",
    },
    'assessment_link': {
        'sublevel': 'low', 'live': False,
        'description': "Link an existing AI assessment to a production "
                       "object's record.",
    },
    'task_set_status': {
        'sublevel': 'normal', 'live': True,
        'description': "Campaign task lifecycle transition (draft, ready, "
                       "submitted, completed, failed) with rule enforcement.",
    },
    'task_intake': {
        'sublevel': 'normal', 'live': True,
        'description': "Idempotent intake of a production request into a "
                       "draft campaign task.",
    },
    'dataset_intake': {
        'sublevel': 'normal', 'live': True,
        'description': "Idempotent find-or-create of a dataset from a source "
                       "location such as a CSV manifest.",
    },
    'task_link_input': {
        'sublevel': 'low', 'live': False,
        'description': "Link an existing dataset as a campaign task's input "
                       "by DID.",
    },
    'dataset_propagation_set': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator change of dataset propagation disposition "
                       "(continue, hold, final) with required comment; one "
                       "event per single or bulk action, carrying the "
                       "changed count and the selecting filter. Carries an "
                       "origin stamp when executing an approved AI proposal.",
    },
    'proposal_created': {
        'sublevel': 'normal', 'live': True,
        'description': "AI proposal pending human review; one event per "
                       "propose call with the proposed count and batch. A "
                       "session's proposal is normal and live; a rule "
                       "proposer's (credential-expiry, certificate-expiry, "
                       "campaign-config, campaign-assembly) records low and "
                       "not live, the decision being the live event.",
    },
    'proposal_denied': {
        'sublevel': 'normal', 'live': True,
        'description': "Human denial of pending AI proposals; denial memory "
                       "prevents re-proposal until the proposer's inputs "
                       "change.",
    },
    'proposal_expired': {
        'sublevel': 'normal', 'live': True,
        'description': "Withdrawal of pending AI proposals (recurring-scan "
                       "heartbeat refresh or operator clear), with count.",
    },
    'proposal_deleted': {
        'sublevel': 'normal', 'live': False,
        'description': "Operator deletion of AI proposal list rows — "
                       "housekeeping for test or noise rows; removes audit "
                       "rows, logged with count.",
    },
    'narrative_edited': {
        'sublevel': 'normal', 'live': False,
        'description': "Expert revision of a campaign narrative document — "
                       "a new corun-ai version of the entry.",
    },
    'narrative_commented': {
        'sublevel': 'normal', 'live': True,
        'description': "Comment posted on a campaign narrative — the "
                       "non-intrusive contribution path beside editing.",
    },
    # web entity lifecycle and operator actions
    'campaign_set_current': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator designation of the current campaign.",
    },
    'evgen_mark_obsolete': {
        'sublevel': 'normal', 'live': True,
        'description': "PWG triage mark on EVGEN entries: set or clear the "
                       "obsolete flag on the ticked paths, with the setter, "
                       "time, and reason recorded.",
    },
    'evgen_mark_priority': {
        'sublevel': 'low', 'live': False,
        'description': "PWG priority set on EVGEN entries (0 clears, 1 is "
                       "highest), with the setter and time recorded. "
                       "Bookkeeping, not news: verbose viewers only.",
    },
    'campaign_set_last': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator designation of the last (previous) campaign.",
    },
    'questionnaire_match_add': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator addition of a request-to-task match on the "
                       "request page.",
    },
    'questionnaire_match_remove': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator removal of a request-to-task match.",
    },
    'category_create': {
        'sublevel': 'normal', 'live': True,
        'description': "Create a physics tag category.",
    },
    'tag_create': {
        'sublevel': 'low', 'live': False,
        'description': "Create a configuration tag (physics, evgen, simu, "
                       "reco, or background).",
    },
    'tag_edit': {
        'sublevel': 'low', 'live': False,
        'description': "Edit a draft configuration tag.",
    },
    'tag_lock': {
        'sublevel': 'normal', 'live': True,
        'description': "Lock a configuration tag — a permanent, one-way "
                       "transition to immutability.",
    },
    'tag_delete': {
        'sublevel': 'normal', 'live': True,
        'description': "Delete a draft configuration tag.",
    },
    'dataset_create': {
        'sublevel': 'normal', 'live': True,
        'description': "Create a dataset: a composed tag identity plus any "
                       "sample-variant discriminator.",
    },
    'dataset_block_add': {
        'sublevel': 'low', 'live': False,
        'description': "Add the next Rucio block (.bN) to a dataset.",
    },
    'config_create': {
        'sublevel': 'low', 'live': False,
        'description': "Create a production config template.",
    },
    'config_edit': {
        'sublevel': 'low', 'live': False,
        'description': "Edit a production config template.",
    },
    'task_delete': {
        'sublevel': 'normal', 'live': True,
        'description': "Delete a campaign task.",
    },
    'assessment_quality_set': {
        'sublevel': 'normal', 'live': True,
        'description': "Operator quality review (good, poor, wrong) recorded "
                       "on an AI assessment.",
    },
    'queues_update': {
        'sublevel': 'normal', 'live': True,
        'description': "Update PanDA queue records from the GitHub source.",
    },
    'endpoints_update': {
        'sublevel': 'normal', 'live': True,
        'description': "Update Rucio storage endpoint records from the "
                       "GitHub source.",
    },
    # editions: the software tags and their repair (swf-epicprod PCS.md)
    'edition_bind': {
        'sublevel': 'normal', 'live': False,
        'description': "Bind the release's simulation and reconstruction "
                       "tags to an edition at submission; rides with the "
                       "submission's own record.",
    },
    'physics_tag_rederive': {
        'sublevel': 'low', 'live': False,
        'description': "Physics tags brought up to the path parser's reading "
                       "of their datasets' task names "
                       "(swf-epicprod tools/pcs/rederive-physics-tags.py): "
                       "the tags corrected, with the axes added, and the "
                       "tags left because their corrected parameters equal "
                       "another tag's; one record per applying run.",
    },
    'edition_fold': {
        'sublevel': 'low', 'live': False,
        'description': "One edition per identity restored where a physics "
                       "tag was made twice (swf-epicprod "
                       "scripts/fold_duplicate_tag_editions.py): placeholder "
                       "editions folded into the editions of the same "
                       "identity, the editions moved to the earlier tag, "
                       "evgen tags corrected from the path; the record of a "
                       "repair pass, one per run.",
    },
    'edition_rebind': {
        'sublevel': 'low', 'live': False,
        'description': "Rebind a campaign family's editions from the import "
                       "and intake anchors to the tags the binding rule "
                       "gives; the record of a repair pass, one per run.",
    },
    'edition_merge': {
        'sublevel': 'low', 'live': False,
        'description': "Fold a duplicate edition into the record that holds "
                       "its identity; the record of a repair.",
    },
    'dataset_edit': {
        'sublevel': 'normal', 'live': False,
        'description': "Edit an edition's tags on the datasets compose page; "
                       "the name recomposes and the former name still "
                       "resolves.",
    },
    'trial_cost': {
        'sublevel': 'low', 'live': False,
        'description': "Record a trial's measured cost on the edition it "
                       "proves; bookkeeping, read from the edition.",
    },
    # Actions recorded before they were catalogued (review of 2026-10-28)
    'production_finding': {
        'sublevel': 'high', 'live': True,
        'description': "A material production finding or resolution, posted "
                       "to epicprod-live for the people and the assessments "
                       "(docs/NOTICE_ROUTING.md, Findings and resolutions).",
    },
    'task_failure_guard': {
        'sublevel': 'high', 'live': True,
        'description': "The front's task guard asked to pause a task whose "
                       "jobs fail, with the trip's evidence: production "
                       "stopped a task by itself.",
    },
    'task_failure_guard_shadow': {
        'sublevel': 'low', 'live': False,
        'description': "What the task guard's shadow rule would have paused; "
                       "it pauses nothing.",
    },
    'idle_move': {
        'sublevel': 'low', 'live': False,
        'description': "The front's request to move a task's unstarted work "
                       "to an idle queue; the Move itself is the task "
                       "operation record.",
    },
    'google_production': {
        'sublevel': 'low', 'live': False,
        'outcomes': {'submitted': {'sublevel': 'normal', 'live': True}},
        'description': "One pass of the Google feeder; submitted when it "
                       "released work to the Google Event Service queue.",
    },
    'perlmutter_production': {
        'sublevel': 'low', 'live': False,
        'outcomes': {'submitted': {'sublevel': 'normal', 'live': True}},
        'description': "One pass of the Perlmutter feeder; submitted when it "
                       "released work to the Perlmutter Event Service queue.",
    },
    'gke_pilot_cycle': {
        'sublevel': 'low', 'live': False,
        'description': "One cycle of the GKE pilot controller that supplies "
                       "the Google queue's workers.",
    },
    'stash_drain': {
        'sublevel': 'low', 'live': False,
        'description': "The failover stash's drain pass, registering stashed "
                       "outputs in JLab Rucio.",
    },
    'log_rescue': {
        'sublevel': 'low', 'live': False,
        'description': "The hourly pass moving logs held in the stage-out "
                       "bucket into BNL Rucio.",
    },
    'registrar': {
        'sublevel': 'low', 'live': False,
        'description': "The hourly pass completing pending output "
                       "registrations.",
    },
    'storage_door_cycle': {
        'sublevel': 'low', 'live': False,
        'description': "One cycle of the storage door checks.",
    },
    # removing datasets from a disk RSE (swf-epicprod docs/STORAGE.md,
    # Removing datasets from an RSE)
    'rse_deletion_preview': {
        'sublevel': 'low', 'live': False,
        'description': "An operator's preview of removing a dataset pattern "
                       "from a disk RSE: the datasets matched and those a "
                       "complete tape copy makes removable. Changes nothing.",
    },
    'rse_deletion': {
        'sublevel': 'high', 'live': True,
        'description': "An operator's removal of the datasets matching a "
                       "pattern from a disk RSE, only where tape holds a "
                       "complete copy: who, the counts and bytes, and the "
                       "removed datasets named. Immediate and irreversible "
                       "on disk; the tape copies are untouched.",
    },
    'evgen_register': {
        'sublevel': 'low', 'live': False,
        'description': "Register an EVGEN dataset's files in Rucio.",
    },
    'evgen_register_request': {
        'sublevel': 'low', 'live': False,
        'description': "A request to register an EVGEN dataset, queued for "
                       "the operations agent.",
    },
    'evgen_events': {
        'sublevel': 'low', 'live': False,
        'description': "Count an EVGEN dataset's events.",
    },
    'dataset_expected_events_set': {
        'sublevel': 'low', 'live': False,
        'description': "Set a dataset's expected events; bookkeeping of the "
                       "EVGEN intake.",
    },
    'task_evgen_input': {
        'sublevel': 'low', 'live': False,
        'description': "Link a task to its EVGEN input; bookkeeping.",
    },
    'segfault_notice': {
        'sublevel': 'low', 'live': False,
        'description': "The nightly notice of crash signatures needing a "
                       "reproduction or a reading.",
    },
    'segfault_study': {
        'sublevel': 'low', 'live': False,
        'description': "Queue studies of traced crash signatures.",
    },
    'prodtask_priority_set': {
        'sublevel': 'normal', 'live': True,
        'description': "Set a production task's priority.",
    },
    'prod_request_priority_set': {
        'sublevel': 'normal', 'live': True,
        'description': "Set a production request's priority.",
    },
    'prodtask_trial': {
        'sublevel': 'normal', 'live': True,
        'description': "Compose a trial of a production task at a site.",
    },
    'prodtask_unplace': {
        'sublevel': 'normal', 'live': True,
        'description': "Return a placed task whose work has not started to "
                       "the front, which places it again.",
    },
    'worker_limit': {
        'sublevel': 'normal', 'live': True,
        'description': "Set or clear an operator's worker (core) limit at a "
                       "Harvester queue, with its reason.",
    },
    'feed_auto_on': {
        'sublevel': 'normal', 'live': True,
        'description': "The front turned a queue's feed back on by its "
                       "automatic return, with the evidence.",
    },
    'attempt_manifest_repair': {
        'sublevel': 'normal', 'live': True,
        'description': "Repair a task's attempt manifest from its submitted "
                       "input.",
    },
    'osg_direct_pilot_deployment': {
        'sublevel': 'normal', 'live': True,
        'description': "Direct OSG production pilots deployed and verified "
                       "running production work.",
    },
    'osg_image_dispatch_promoted': {
        'sublevel': 'normal', 'live': True,
        'description': "OSG production moved to pilots that declare their "
                       "payload image, with the trials behind the move.",
    },
    'native_image_dispatch_trial': {
        'sublevel': 'normal', 'live': True,
        'description': "A trial of pilots that declare their payload image, "
                       "with its outcome.",
    },
    'osg_rerun_log_collision': {
        'sublevel': 'high', 'live': True,
        'description': "A finding that a rerun reused older log names, with "
                       "the failures it caused.",
    },
    'google_operating_admission': {
        'sublevel': 'normal', 'live': True,
        'description': "The Google queue admitted among the front's "
                       "operating sites, with how its work is fed.",
    },
    'physics_config_requestors_set': {
        'sublevel': 'normal', 'live': True,
        'description': "Set the requesters of physics configurations.",
    },
    'resolution': {
        'sublevel': 'normal', 'live': True,
        'description': "The resolution of an earlier finding, posted to "
                       "epicprod-live: cause, fix, deployment and verified "
                       "recovery.",
    },
}


FAILURE_LEVELS = frozenset({'ERROR', 'CRITICAL'})
_uncatalogued = set()


def publication(extra, levelname=''):
    """Set an action record's importance (``sublevel``) and live default by
    their meaning (ACTION_STREAM.md, Importance), in place, at the write:
    a failure (an error, timeout, partial or unrecorded outcome, or a record
    at ERROR) is high and live, a maintenance pass's failure normal; any
    other record takes its action's catalog declaration, the outcome's own
    where the catalog declares one. A low record is never live by default.
    An action the catalog lacks keeps its call site's values and is logged
    once, so it is catalogued."""
    from .live_notices import FAILURE_OUTCOMES, MAINTENANCE
    action = str(extra.get('action') or '')
    outcome = str(extra.get('outcome') or '')
    declared = ACTION_DEFAULTS.get(action)
    if outcome in FAILURE_OUTCOMES or levelname in FAILURE_LEVELS:
        sublevel, live = ('normal' if action in MAINTENANCE else 'high'), True
    elif declared:
        entry = dict(declared, **(declared.get('outcomes') or {}).get(outcome, {}))
        sublevel, live = entry['sublevel'], entry['live']
    else:
        if action not in _uncatalogued:
            _uncatalogued.add(action)
            logger.warning('epicprod action %s is not in ACTION_DEFAULTS: its importance '
                           'stays as its call site declared it', action)
        return extra
    extra['sublevel'] = sublevel
    extra['live_default'] = bool(live) and sublevel != 'low'
    return extra


def action_description(action):
    """Plain-English one-liner for a known action id, or ''."""
    return (ACTION_DEFAULTS.get(str(action)) or {}).get('description', '')

RESERVED_KEYS = ('action', 'subject_type', 'subject_key', 'username',
                 'outcome', 'duration_ms', 'sublevel', 'live_default')


def log_epicprod_action(instance, action, *, subject_type='', subject_key='',
                        username='', outcome='ok', duration_ms=None,
                        sublevel='low', live_default=False, message='',
                        level=logging.INFO, **counts):
    """Record one epicprod action in the AppLog action stream.

    Never raises: a failure to record is logged to the module logger and the
    calling action proceeds — the action log must not break the action.

    Args:
        instance: component performing the action ('web', 'ops-agent', 'mcp',
            'catalog-sync', 'submit', 'report').
        action: short action identifier, e.g. 'rucio_sweep', 'task_submit',
            'assessment_register'.
        subject_type: acted-on object type when there is one (canonical
            assessment subject types where applicable).
        subject_key: acted-on object key (composed name, JEDI id, queue, ...).
        username: human or service account driving the action.
        outcome: 'ok' or 'error' (conventional; free-form refinements allowed).
        duration_ms: measured execution time; required in spirit for sweeps
            and other timed operations.
        sublevel: the importance for an action the catalog lacks; a
            catalogued action's comes from ACTION_DEFAULTS at the write
            (publication), and a failure's is high.
        live_default: the event's declared RECOMMENDATION for the live
            stream ("interesting to some humans, now"). The effective live
            decision is the epicprod_live_policy override (runtime attention
            knob) over this default.
        message: optional human-readable one-liner; composed if omitted.
        level: python logging level; use logging.ERROR for failed actions.
        **counts: numeric counts worth recording (rows_added=..., etc.);
            reserved keys are ignored if passed here.

    Returns:
        The created AppLog row id, or None if the write failed.
    """
    from .models import AppLog

    if sublevel not in SUBLEVEL_VALUES:
        logger.warning('epicprod action %s: unknown sublevel %r, using low',
                       action, sublevel)
        sublevel = 'low'
    extra = {
        'action': str(action),
        'outcome': str(outcome),
        'sublevel': sublevel,
        'live_default': bool(live_default),
    }
    if subject_type:
        extra['subject_type'] = str(subject_type)
    if subject_key:
        extra['subject_key'] = str(subject_key)
    if username:
        extra['username'] = str(username)
    if duration_ms is not None:
        try:
            extra['duration_ms'] = int(duration_ms)
        except (TypeError, ValueError):
            logger.warning('epicprod action %s: non-numeric duration_ms %r',
                           action, duration_ms)
    for key, value in counts.items():
        if key not in RESERVED_KEYS:
            extra[key] = value

    if not message:
        subject = f"{subject_type}:{subject_key}" if subject_key else ''
        message = ' '.join(part for part in (str(action), subject, str(outcome)) if part)

    try:
        row = AppLog.objects.create(
            app_name=EPICPROD_APP_NAME,
            instance_name=str(instance),
            timestamp=timezone.now(),
            level=int(level),
            levelname=logging.getLevelName(int(level)),
            message=message,
            module='epicprod_logging',
            funcname=str(action),
            lineno=0,
            process=os.getpid(),
            thread=threading.get_ident(),
            extra_data=extra,
        )
        return row.id
    except Exception:
        logger.exception('epicprod action log write failed: %s %s',
                         instance, action)
        return None


def get_live_policy():
    """Return the live override registry: {action_id: bool}.

    Stored under LIVE_POLICY_STATE_KEY in SysConfig (system configuration
    lives in the DB, adjustable via the live-policy page). The runtime
    attention knob: an action absent from the registry follows its records'
    live_default recommendation. Reads defensively: a missing table
    (pre-migration) yields an empty policy with a logged warning rather than
    breaking the caller.
    """
    from .models import SysConfig

    try:
        policy = SysConfig.get_setting(LIVE_POLICY_STATE_KEY, {}) or {}
    except Exception as exc:
        logger.warning('live policy read failed (empty policy used): %s', exc)
        return {}
    if not isinstance(policy, dict):
        return {}
    return {str(k): bool(v) for k, v in policy.items()}


def set_live_policy_entry(action, value, username=''):
    """Set (True/False) or clear (None) an action's live override."""
    from .models import SysConfig

    policy = get_live_policy()
    key = str(action)
    if value is None:
        policy.pop(key, None)
    else:
        policy[key] = bool(value)
    SysConfig.update_config({LIVE_POLICY_STATE_KEY: policy}, username=username)
    return policy


def live_policy_rows():
    """Rows for the live-policy page: every known action with its declared
    sublevel and live default, any live override, and the effective state.

    Known = the ACTION_DEFAULTS catalog plus any action observed in the
    stream (the newest record supplies declarations for actions not yet in
    the catalog).
    """
    from .models import AppLog

    policy = get_live_policy()
    declarations = {k: dict(v) for k, v in ACTION_DEFAULTS.items()}
    observed = (AppLog.objects.filter(app_name=EPICPROD_APP_NAME)
                .exclude(extra_data__action__isnull=True)
                .values_list('extra_data__action', flat=True)
                .distinct())
    for action in observed:
        if action and action not in declarations:
            latest = (AppLog.objects.filter(app_name=EPICPROD_APP_NAME,
                                            extra_data__action=action)
                      .order_by('-id').first())
            extra = latest.extra_data if latest and isinstance(latest.extra_data, dict) else {}
            stamped = extra.get('sublevel')
            declarations[action] = {
                'sublevel': stamped if stamped in SUBLEVEL_VALUES else 'low',
                'live': bool(extra.get('live_default')),
            }

    rows = []
    for action in sorted(declarations):
        decl = declarations[action]
        override = policy.get(action)
        effective = override if override is not None else decl['live']
        rows.append({
            'action': action,
            'description': decl.get('description', ''),
            'sublevel': decl['sublevel'],
            'live_default': decl['live'],
            'state': 'default' if override is None else ('live' if override else 'quiet'),
            'effective': bool(effective),
        })
    return rows


def live_stream_q(min_sublevel=None):
    """Q filter selecting live action records, optionally importance-gated.

    Live = the runtime override (epicprod_live_policy) over the record's
    declared live_default. A channel applies its importance threshold through
    min_sublevel: the deep live view passes None (all live events); an email
    digest passes 'high'. The single source of stream semantics for every
    channel.
    """
    from django.db.models import Q

    policy = get_live_policy()
    force_on = [a for a, v in policy.items() if v]
    force_off = [a for a, v in policy.items() if not v]

    q = Q(extra_data__live_default=True)
    if force_off:
        q &= ~Q(extra_data__action__in=force_off)
    if force_on:
        q |= Q(extra_data__action__in=force_on)
    if min_sublevel in SUBLEVEL_VALUES:
        min_rank = SUBLEVEL_ORDER[min_sublevel]
        ge_values = [v for v in SUBLEVEL_VALUES if SUBLEVEL_ORDER[v] >= min_rank]
        q &= Q(extra_data__sublevel__in=ge_values)
    return Q(app_name=EPICPROD_APP_NAME) & q
