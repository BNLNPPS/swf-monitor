# Event Service core plots

Task and job pages use the same Event Service report reader and SVG renderer.
Finished-job metadata takes precedence. When metadata is absent, the reader uses
the harness snapshot filed beside the production job. Task plots include active
jobs and archived jobs, with at most eight timelines shown.

The existing five-minute close-out doer refreshes running jobs' snapshots before
its normal pass. Its `--records-only` option refreshes observations without
changing PanDA job state. The hourly report sweep retains ended jobs' records.

A running timeline ends at the snapshot timestamp. In-flight units and units
awaiting an output close retain their separate states; they are not marked lost.
The page displays the observation age and warns after two five-minute collection intervals.
A terminal job without a normal harness-end marker is an incomplete observation.
The worker-loss code 1256 is required before a shipped snapshot marks a cut,
interrupted units or output lost on the node. Missing final metadata alone does
not establish that outcome. Registered closes remain saved.
The close summary separates started, saved, unfinished and failed closes.
Its duration median includes completed closes only. An unfinished close carries
its elapsed time at the observation, and new payload snapshots identify its
current merge, validation or registration stage.
That last independent observation also supplies the timeline end when PanDA's
archived end time precedes shipped payload activity, as can happen when a job
is reaped after losing its heartbeat.

The report transport and range accounting are documented in swf-epicprod
`docs/JOB_REPORTING.md` and `docs/NODE_EVENT_DISPATCHER.md`.
