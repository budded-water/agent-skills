import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = (Path(__file__).resolve().parents[2] / "skills" / "requesting-slack-bot-review") / "scripts" / "review_round_budget.py"
SPEC = importlib.util.spec_from_file_location("review_round_budget", MODULE_PATH)
review_round_budget = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = review_round_budget
SPEC.loader.exec_module(review_round_budget)


class ReviewRoundBudgetTest(unittest.TestCase):
    def test_two_infrastructure_rounds_route_the_second_to_follow_up(self):
        first = review_round_budget.decide("infrastructure_only", 0)
        second = review_round_budget.decide(
            "infrastructure_only", first["consecutive_infrastructure_rounds"]
        )
        self.assertEqual(first["route"], "current_candidate")
        self.assertEqual(second["route"], "separate_follow_up")

    def test_finding_driven_round_has_precedence_and_resets_counter(self):
        result = review_round_budget.decide("finding_driven", 2)
        self.assertEqual(result["route"], "current_candidate")
        self.assertEqual(result["consecutive_infrastructure_rounds"], 0)

    def test_replay_infrastructure_infrastructure_finding_stays_current(self):
        first = review_round_budget.decide("infrastructure_only", 0)
        second = review_round_budget.decide(
            "infrastructure_only", first["consecutive_infrastructure_rounds"]
        )
        third = review_round_budget.decide(
            "finding_driven", second["consecutive_infrastructure_rounds"]
        )
        self.assertEqual(
            [first["route"], second["route"], third["route"]],
            ["current_candidate", "separate_follow_up", "current_candidate"],
        )
        self.assertEqual(third["consecutive_infrastructure_rounds"], 0)

    def test_negative_counter_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-negative"):
            review_round_budget.decide("infrastructure_only", -1)


if __name__ == "__main__":
    unittest.main()
