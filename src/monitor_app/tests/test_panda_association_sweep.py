from django.test import SimpleTestCase

from monitor_app.management.commands.sweep_panda_associations import (
    _should_auto_intake,
)


class AssociationSweepIntakeTests(SimpleTestCase):
    def test_only_missing_pcs_identity_enters_direct_intake(self):
        self.assertTrue(_should_auto_intake(
            "no exact PCS match for PanDA taskname 'group.EIC.sample'"))
        self.assertFalse(_should_auto_intake(
            "PanDA taskname 'group.EIC.sample' already records jediTaskID 1"))
        self.assertFalse(_should_auto_intake(
            "ambiguous PCS match for PanDA taskname 'group.EIC.sample'"))
