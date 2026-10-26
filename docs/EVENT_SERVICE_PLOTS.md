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

Task range totals exclude a cancelled historical jobset copy when the same
file/process range has a completed consumer. Consumer outcomes remain visible;
a cancelled range without completed work remains cancelled. Normal ended
harness observations are labelled final records rather than running snapshots.

Resident particle-gun records distinguish initialization, event processing,
and setup/output handling within each unit. Simulation processing begins at
the first event callback, including Geant4's lazy first-run preparation in
initialization. Reconstruction uses the resident service's ready time and
the measured request interval. Both live snapshots and final records retain
these boundaries. Orange marks initialization; slate marks setup/output
handling; outcome colors apply to measured simulation and reconstruction.
Older aggregate intervals are mauve and carry an explicit unavailable
measurement, rather than being counted as measured event processing.

Unit occupancy counts the full assigned interval. Measured saved processing
counts only measured event-processing intervals whose output was saved,
divided by observed allocated core-time. The two quantities are shown
separately. Missing phase measurements never imply zero initialization.

Turnaround between units is the time on a core from the end of one unit's last
measured simulation or reconstruction interval to the start of the next unit's
first. The page gives its median, 90th percentile and longest value and the
number of transitions. A unit without measured processing breaks the
sequence, so two gaps are never joined into one. It covers per-unit setup
and output handling, the wait for the next unit and the simulation's own
per-request start (swf-epicprod EPICPROD_PAYLOAD.md, Resident Event Service
units).
