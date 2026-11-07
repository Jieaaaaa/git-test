import unittest

from model_policy import FINALIZATION_RESERVE_SECONDS, request_budget


class ModelPolicyTests(unittest.TestCase):
    def test_budget_tracks_long_remaining_deadline_without_fixed_cap(self):
        budget = request_budget(deadline_at=220, now=100)
        self.assertEqual(budget, 220 - 100 - FINALIZATION_RESERVE_SECONDS)
        self.assertGreater(budget, 70)

    def test_budget_keeps_only_the_finalization_reserve(self):
        budget = request_budget(deadline_at=115, now=100)
        self.assertEqual(budget, 12)

    def test_fractional_phase_budget_leaves_time_for_followup_calls(self):
        budget = request_budget(deadline_at=220, now=100, fraction=0.72)
        self.assertEqual(budget, (220 - 100 - FINALIZATION_RESERVE_SECONDS) * 0.72)
        self.assertLess(budget, request_budget(deadline_at=220, now=100))

    def test_budget_expires_after_reserve_is_consumed(self):
        with self.assertRaises(TimeoutError):
            request_budget(deadline_at=103, now=100)


if __name__ == "__main__":
    unittest.main()
