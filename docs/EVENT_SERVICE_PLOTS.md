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
A terminal job without a normal harness-end marker retains the shipped record's
cut, interrupted units and unshipped output. Registered closes remain saved.

The report transport and range accounting are documented in swf-epicprod
`docs/JOB_REPORTING.md` and `docs/NODE_EVENT_DISPATCHER.md`.
