"""Plan refusals name everything that is wrong, and name it once.

Both messages covered here are written for a human who is editing a plan by hand:

* a feature card usually has more than one wrong value, and failing on the first
  one turns a single fix into a sequence of round trips;
* "scope and features are immutable" is a correct refusal, but without the field
  names the author has to diff the plan against private ledger state by hand.
"""
from __future__ import annotations

from codex_deepseek_team import coordination, routing_models

from test_coordination import CoordinationCase

import unittest


class FeatureCardDiagnosticsTests(unittest.TestCase):
    CARD = {
        "domain": "python", "operation": "fix", "localization": "known",
        "coupling": "local", "verification": "none", "clarity": "clear",
        "risk": "low", "scope_size": "small", "kind": "implementation",
    }

    def test_single_invalid_field_keeps_the_original_message(self):
        card = dict(self.CARD, domain="kotlin-but-wrong")
        with self.assertRaises(routing_models.RoutingError) as caught:
            routing_models.validate_features(card)
        message = str(caught.exception)
        self.assertIn("Invalid feature domain;", message)
        self.assertIn("allowed values:", message)

    def test_every_invalid_field_is_named_at_once(self):
        card = dict(self.CARD, domain="nope", operation="nope", verification="nope")
        with self.assertRaises(routing_models.RoutingError) as caught:
            routing_models.validate_features(card)
        message = str(caught.exception)
        for key in ("domain", "operation", "verification"):
            self.assertIn(key, message)
        self.assertIn("(3)", message)

    def test_a_valid_card_still_passes(self):
        self.assertEqual(
            routing_models.validate_features(dict(self.CARD))["domain"], "python")


class ImmutabilityDiagnosticsTests(CoordinationCase):
    FEATURES = {
        "domain": "python", "operation": "fix", "localization": "known",
        "coupling": "local", "verification": "none", "clarity": "clear",
        "risk": "low", "scope_size": "small",
    }

    def _planned_task(self):
        task = coordination.open_task(
            self.repo, session_id="sess-diag", turn_id="turn-diag",
            prompt="implement substantial feature", policy=self.policy(),
        )
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": [
                {"id": "worker-review", "kind": "review",
                 "scope": ["tests/test_worker.py"], "executor": "worker",
                 "acceptance": ["findings reviewed"], "dependencies": [], "checks": []},
                {"id": "impl", "kind": "implementation", "scope": ["src/one.py"],
                 "executor": "auto", "features": dict(self.FEATURES),
                 "acceptance": ["behavior verified"], "dependencies": [], "checks": []},
            ],
        })
        coordination.observe_coordinator_result(
            self.repo, task["id"], "impl", "cancelled",
            "superseded before retaining this worker-eligible implementation")
        return task

    def test_refusal_names_the_field_that_moved(self):
        task = self._planned_task()
        with self.assertRaises(coordination.CoordinationError) as caught:
            coordination.plan_task(self.repo, task["id"], {
                "classification": "substantial",
                "deliverables": [
                    {"id": "worker-review", "kind": "review",
                     "scope": ["tests/test_worker.py"], "executor": "worker",
                     "acceptance": ["findings reviewed"], "dependencies": [], "checks": []},
                    {"id": "impl", "kind": "implementation", "scope": ["src/TWO.py"],
                     "executor": "auto", "features": dict(self.FEATURES),
                     "acceptance": ["behavior verified"], "dependencies": [], "checks": []},
                ],
            })
        message = str(caught.exception)
        self.assertIn("immutable", message)          # the refusal itself is unchanged
        self.assertIn("Changed:", message)
        self.assertIn("scope", message)              # and it now says WHAT moved


if __name__ == "__main__":
    unittest.main()
