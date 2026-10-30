# Standalone scripts

The doers in `scripts/` that read or write the monitor's records (the
segfault catalog's inventory, dig, reproductions and studies, the
reconciliations, the reporters' ingests and the like) are standalone
Python scripts that configure Django themselves. They are run by hand,
from cron, or by the production operations agent as `run_in_background`
doers (swf-epicprod `docs/EPICPROD_OPS_AGENT.md`); none is a Django
management command. The logic they apply lives in `monitor_app`
modules, which the pages, the MCP tools and the REST API share; a script
is the entry point that runs it with its arguments and records the run.

## Shape

The header, before any import from the project:

```python
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.... import ...  # noqa: E402
```

Then:

- **Arguments** by `argparse`, the description taken from the
  docstring's first line. A script that writes offers `--dry-run`,
  which prints what it would do and writes nothing.
- **Output**: progress and errors to stderr through `logging`
  (`basicConfig(level=INFO, stream=sys.stderr)`); the last stdout line
  one JSON summary of the run, which the agent or a session reads.
- **Exit status** 0 on success, 1 on an error.
- **The action stream**: a run that changed something, or failed,
  records one action through
  `monitor_app.epicprod_logging.log_epicprod_action(instance, action,
  outcome=..., sublevel=..., live_default=..., message=..., **fields)`
  (`docs/ACTION_STREAM.md`); a failure is an action with outcome
  `error`, never a silent exit. `live_default=True` only for what
  belongs on epicprod-live (`docs/NOTICE_ROUTING.md`).
- **The docstring** states what the script does, where its design is
  documented, and the invocation by hand.

## Running one by hand

On swf-testbed, from the source directory, with the testbed's virtual
environment's interpreter (after `~/.env`, `python3` on the path is not
that interpreter):

```bash
cd /data/wenauseic/github/swf-monitor/src
../../swf-testbed/.venv/bin/python ../scripts/<script>.py [arguments]
```

A run longer than a few seconds is started in the background and its
summary read when it ends. A script that works on another host reaches
it through that host's documented route (for npps0, the npps0 skill's
`npps0.sh`), never by a credential of its own.
