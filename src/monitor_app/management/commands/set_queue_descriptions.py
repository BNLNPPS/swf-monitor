"""Seed operator-written descriptions for PanDA compute queues.

Descriptions live in ``PandaQueue.metadata['description']``. The CRIC sync
writers replace ``config_data`` and leave ``metadata`` untouched, so text
seeded here survives every refresh. The queue detail page allows editing,
so by default this command fills only queues with no description; pass
``--force`` to overwrite from this file.

Each line answers what the queue is for and, where sibling queues exist,
how it differs from them.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from monitor_app.models import PandaQueue

DESCRIPTIONS = {
    # BNL grid and production
    'BNL_OSG_EPIC_PROD_1': (
        'Main ePIC production queue at BNL, reached through OSG. Carries the '
        'bulk of campaign simulation and reconstruction.'
    ),
    'BNL_EPIC_PROD_1': (
        'Earlier BNL production queue, direct rather than through OSG. '
        'Superseded by BNL_OSG_EPIC_PROD_1 for campaign work.'
    ),
    'BNL_PanDA_1': (
        'General-purpose BNL queue for user analysis and ad-hoc work, with a '
        '4-day limit. Not a campaign production queue.'
    ),
    'BNL_PanDA_test': (
        'Test twin of BNL_PanDA_1 for trying queue and pilot changes before '
        'they reach the production queues.'
    ),
    'BNL_OSG_PanDA_1': (
        'Short-job OSG queue at BNL, 3-hour limit. Suited to quick work that '
        'should not wait behind long production jobs.'
    ),
    'BNL_OSG_PanDA_CI': (
        'Continuous-integration queue: automated software validation jobs, '
        '3-hour limit.'
    ),
    'BNL_OSG_PanDA_pilotest': (
        'Reserved for pilot software testing. Used only when a new pilot '
        'release needs exercising against a real queue.'
    ),

    # GPU
    'BNL_NPPS_GPU': (
        'NPPS GPU server npps0 (2x RTX 4090), for the GPU optical-photon '
        'simulation. Outside the SCDF perimeter with a standalone pilot and '
        'object-store stage-out, so it doubles as the volunteer-computing '
        'model. See VOLUNTEER_GPU_PLAN in swf-epicprod.'
    ),
    'NERSC_Perlmutter_epic_gpu_mps': (
        'Perlmutter GPU queue running NVIDIA MPS, which lets several small '
        'jobs share one A100 concurrently instead of time-slicing it. Cloned '
        'from the gpu_test queue and now the busier of the two GPU queues.'
    ),
    'NERSC_Perlmutter_epic_gpu_test': (
        'Identical to NERSC_Perlmutter_epic_gpu_mps in schedconfig: every '
        'field matches, and the MPS queue was cloned from this one. Any '
        'difference between them is in the NERSC-side batch submission, not '
        'in PanDA. The earlier of the two GPU queues and now the quieter.'
    ),

    # NERSC Perlmutter CPU
    'NERSC_Perlmutter_epic': (
        'Main Perlmutter production queue for ePIC, 4-day limit. Second '
        'largest source of campaign processing after BNL.'
    ),
    'NERSC_Perlmutter_epic_mcore': (
        'Multi-core variant of the main Perlmutter queue, for jobs that use '
        'a whole node. Currently idle.'
    ),
    'NERSC_Perlmutter_epic_test': (
        'Test queue on Perlmutter for validating configuration and workflow '
        'changes before they reach the production queue.'
    ),
    'NERSC_Perlmutter_epic_test_mcore': (
        'Multi-core test queue on Perlmutter. Rarely used.'
    ),
    'NERSC_Perlmutter_epic_dev': (
        'Development queue on Perlmutter for work in progress, kept separate '
        'from both production and the shared test queue.'
    ),
    'NERSC_Perlmutter_iri': (
        'Perlmutter queue for the Integrated Research Infrastructure '
        'programme, which targets near-real-time processing of experimental '
        'data on HPC. Occasional use.'
    ),

    # Cloud
    'BNL_ePIC_GOOGLE': (
        'ePIC queue on Google Cloud, elastic and paid. Large memory and disk '
        'per job; used when demand exceeds the free resources.'
    ),
    'BNL_ePIC_GOOGLE_test': (
        'Test twin of the Google Cloud queue, for validating cloud '
        'configuration without spending on production volume.'
    ),

    # Echelon-1 and partner sites
    'E1_BNL': (
        'Echelon-1 queue at BNL for the streaming workflow testbed, handling '
        'prompt processing of simulated data-taking.'
    ),
    'E1_JLAB': (
        'Echelon-1 queue at Jefferson Lab, the second facility in the '
        'streaming workflow testbed alongside E1_BNL.'
    ),
    'UM_GREX_PanDA_1': (
        'University of Manitoba GREX cluster, a Canadian contribution to ePIC '
        'production. Large memory and disk per job, 4-day limit.'
    ),
}


# Tier as the facility providing the cycles, which is what a reader wants and
# what schedconfig does not give: its value follows the parent site, so nearly
# every ePIC queue reports T1 whatever the hardware behind it. BNL and JLab are
# the Tier-1 facilities, Manitoba GREX is Tier-2, and everything else is
# opportunistic capacity ("Opp"): NERSC allocations and commercial cloud.
TIERS = {
    'BNL_EPIC_PROD_1': 'T1',
    'BNL_NPPS_GPU': 'T1',
    'BNL_OSG_EPIC_PROD_1': 'T1',
    'BNL_OSG_PanDA_1': 'T1',
    'BNL_OSG_PanDA_CI': 'T1',
    'BNL_OSG_PanDA_pilotest': 'T1',
    'BNL_PanDA_1': 'T1',
    'BNL_PanDA_test': 'T1',
    'E1_BNL': 'T1',
    'E1_JLAB': 'T1',
    'UM_GREX_PanDA_1': 'T2',
    'BNL_ePIC_GOOGLE': 'Opp',
    'BNL_ePIC_GOOGLE_test': 'Opp',
    'NERSC_Perlmutter_epic': 'Opp',
    'NERSC_Perlmutter_epic_dev': 'Opp',
    'NERSC_Perlmutter_epic_gpu_mps': 'Opp',
    'NERSC_Perlmutter_epic_gpu_test': 'Opp',
    'NERSC_Perlmutter_epic_mcore': 'Opp',
    'NERSC_Perlmutter_epic_test': 'Opp',
    'NERSC_Perlmutter_epic_test_mcore': 'Opp',
    'NERSC_Perlmutter_iri': 'Opp',
}


class Command(BaseCommand):
    help = 'Seed queue descriptions and facility tiers into PandaQueue.metadata'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Overwrite descriptions that already exist',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing',
        )

    def handle(self, *args, **options):
        force = options['force']
        dry_run = options['dry_run']
        written = skipped = 0
        missing = 0

        for queue_name, text in DESCRIPTIONS.items():
            queue = PandaQueue.objects.filter(queue_name=queue_name).first()
            if queue is None and dry_run:
                self.stdout.write(f'would create and set: {queue_name}')
                written += 1
                continue
            if queue is None:
                # Most queues are known only from schedconfig; the local row
                # exists to carry annotation, so create it on demand.
                queue = PandaQueue(queue_name=queue_name, config_data={}, metadata={})

            metadata = dict(queue.metadata or {})
            existing = (metadata.get('description') or '').strip()
            tier = TIERS.get(queue_name)
            tier_current = (metadata.get('tier') or '').strip()
            tier_stale = bool(tier) and tier_current != tier
            if existing and not force and not tier_stale:
                skipped += 1
                continue
            if existing == text and not tier_stale:
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(f'would set: {queue_name}')
                written += 1
                continue

            if tier:
                metadata['tier'] = tier
            metadata['description'] = text
            metadata['description_updated_by'] = 'set_queue_descriptions'
            metadata['description_updated_at'] = timezone.now().isoformat()
            queue.metadata = metadata
            if queue._state.adding:
                queue.save()
            else:
                queue.save(update_fields=['metadata', 'updated_at'])
            written += 1

        verb = 'would write' if dry_run else 'wrote'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {written}, skipped {skipped}, missing {missing}'
        ))
