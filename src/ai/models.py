"""AI subsystem data models.

The AI app owns the human-in-the-loop automation machinery
(AI_PROPOSALS.md): proposals and their decisions. Executors — the
services that actually mutate domain state — stay in their domain apps
(e.g. ``pcs.services.dataset_propagation_set``); this app proposes,
reviews, and dispatches to them.
"""
from django.db import models


class Proposal(models.Model):
    """AI (or rule-based) proposal of a concrete action, pending or decided —
    the canonical record behind the AI proposal list (AI_PROPOSALS.md).

    A proposal is a frozen executable payload: the action identifier plus
    the exact validated arguments of the call it wants. Terminal rows are
    retained — the AI proposal list is where AI-proposed activity and its
    human decisions remain visible and queryable (per-proposer track
    records, approval and wrong-rates). Records the proposal targets carry a
    render projection in their metadata, written and cleared by the same
    services that write these rows; this table is the truth.
    """
    EXECUTOR_CHOICES = [('service', 'service'), ('agent_message', 'agent_message')]
    # proposed -> executed | denied | withdrawn | stale;
    # approved_pending_execution is reserved for agent_message executors.
    STATUS_CHOICES = [
        ('proposed', 'proposed'), ('approved_pending_execution',
        'approved_pending_execution'), ('executed', 'executed'),
        ('denied', 'denied'), ('withdrawn', 'withdrawn'), ('stale', 'stale'),
    ]
    # One shared review vocabulary with AI assessments, worst to best.
    QUALITY_CHOICES = [('wrong', 'wrong'), ('poor', 'poor'), ('ok', 'ok'),
                       ('good', 'good')]

    action = models.CharField(max_length=40)
    subject_type = models.CharField(max_length=40)
    subject_key = models.CharField(max_length=255, db_index=True)
    # The other side of a relation subject (e.g. the task of a
    # questionnaire-task match); empty for single-record subjects.
    counterpart_key = models.CharField(max_length=255, blank=True, default='')
    payload = models.JSONField(default=dict)
    comment = models.TextField()
    confidence = models.CharField(max_length=16, blank=True, default='')
    proposer = models.CharField(max_length=100)
    scan_version = models.IntegerField(default=1)
    batch_id = models.CharField(max_length=100, blank=True, default='',
                                db_index=True)
    executor = models.CharField(max_length=20, choices=EXECUTOR_CHOICES,
                                default='service')
    # Deterministic decide-time guard, e.g. {'prev_state': 'continue'}.
    precondition = models.JSONField(default=dict)
    input_hash = models.CharField(max_length=40, db_index=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES,
                              default='proposed', db_index=True)
    # Optional decision quality tag ('wrong' is the one-tap miscalibration
    # signal; it weighs against the proposer's track record).
    quality = models.CharField(max_length=10, choices=QUALITY_CHOICES,
                               blank=True, default='')
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.CharField(max_length=100, blank=True, default='')
    decided_at = models.DateTimeField(null=True, blank=True)
    # AppLog row id of the origin-stamped execution event.
    executed_log_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = 'ai_proposal'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['action', 'subject_key', 'status']),
        ]

    def __str__(self):
        return f'{self.action} {self.subject_key} [{self.status}]'
