"""Batch PanDA-association sweep — the scheduled counterpart of the lazy path.

Associations between PanDA tasks and PCS campaign tasks are otherwise created
only when a person views a task through the monitor (or at PCS submission).
This command applies the same reconciliation to every recent EIC PanDA task,
so directly submitted production is pulled into the catalog no matter who
ignores the UI. Run nightly by the prod-ops agent's catalog_sync chain; run
once with a wide --days window as the backfill.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ('Associate recent PanDA tasks with PCS campaign tasks '
            '(batch form of the lazy per-view reconciliation).')

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=14,
                            help='PanDA task modification window (default 14)')
        parser.add_argument('--limit', type=int, default=1000,
                            help='max PanDA tasks to examine (default 1000)')
        parser.add_argument('--no-intake', action='store_true',
                            help='associate only; skip auto-intake of '
                                 'unmatched group.EIC production tasknames')

    def handle(self, *args, **opts):
        from monitor_app.panda.queries import list_tasks
        from pcs.services import (intake_direct_panda_task,
                                  reconcile_panda_task_association)

        result = list_tasks(days=opts['days'], workinggroup='EIC',
                            limit=opts['limit'])
        tasks = result.get('tasks', []) if isinstance(result, dict) else result

        checked = new = existing = unmatched = intaken = 0
        for panda_task in tasks:
            pcs_task, row, reason = reconcile_panda_task_association(panda_task)
            checked += 1
            if row is None and not opts['no_intake']:
                # Commissioning/migration policy: directly submitted
                # group.EIC production is auto-intaken into the catalog,
                # then associated by the normal reconciler.
                task, intake_reason = intake_direct_panda_task(panda_task)
                if task is not None:
                    intaken += 1
                    self.stdout.write(
                        f"intaken jediTaskID={panda_task.get('jeditaskid')} "
                        f"-> {task.name}")
                    pcs_task, row, reason = reconcile_panda_task_association(panda_task)
            if row is None:
                unmatched += 1
                self.stdout.write(
                    f"unmatched jediTaskID={panda_task.get('jeditaskid')}: {reason}")
            elif reason == 'existing jediTaskID association':
                existing += 1
            else:
                new += 1
                name = pcs_task.composed_name if pcs_task else '?'
                self.stdout.write(
                    f"associated jediTaskID={panda_task.get('jeditaskid')} "
                    f"-> {name} ({reason})")

        # Summary is the last stdout line; the agent records it in the
        # action stream.
        self.stdout.write(
            f"checked={checked} new={new} existing={existing} "
            f"intaken={intaken} unmatched={unmatched} days={opts['days']}")
