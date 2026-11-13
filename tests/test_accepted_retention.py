"""An accepted coordinator deliverable is history too, not retained active work.

Release 0.10.5 exempted a *cancelled* coordinator deliverable from the retention
complaint: a cancellation abandons the work, so there is nothing left to
delegate and the complaint has no subject. The same is true of the other
terminal outcome. ``accepted`` means the work is finished and verified, and a
finished deliverable cannot be handed to a worker either.

Leaving ``accepted`` out made the complaint permanently unsatisfiable on a plan
that is still being revised: ``scope`` and ``features`` of a started deliverable
are immutable and a plan revision may not drop it, so an accepted
coordinator-retained slice keeps failing validation on every later revision of
the same task, and the PreToolUse hook that reads those issues blocks every
subsequent source edit.

The exemption deliberately keeps both guards that the cancellation case already
had: outcome freshness (a later mutation invalidates the result, and the
complaint returns) and nonempty evidence (a bare ledger outcome cannot waive
distribution). The ratio checks are untouched, so the 75% profile is not
weakened — only the unanswerable complaint is dropped.
"""
from __future__ import annotations

from codex_deepseek_team import coordination

from test_coordination import CoordinationCase

TASK = "task-" + "c" * 20
COMPLAINT = "worker-eligible implementation retained by coordinator"


class AcceptedRetentionTests(CoordinationCase):
    FEATURES = {
        "domain": "python", "operation": "fix", "localization": "known",
        "coupling": "local", "verification": "none", "clarity": "clear",
        "risk": "low", "scope_size": "small",
    }

    def _write_task(self, result):
        """One worker slice (so the profile check passes) and one retained slice."""
        item = {
            "id": "retained-impl", "kind": "implementation",
            "scope": ["src/example.py"], "executor": "coordinator",
            "acceptance": ["behavior verified"], "dependencies": [], "checks": [],
            "features": dict(self.FEATURES),
        }
        if result is not None:
            item["result"] = result
        task = {
            "id": TASK, "schema": 2, "classification": "substantial",
            "policy": {"delegation_level": 75, "access": "full-access",
                       "effective_access": "full-access", "effort": "auto",
                       "enabled": True},
            "saved_policy": {"delegation_level": 75, "access": "full-access",
                             "effective_access": "full-access", "effort": "auto",
                             "enabled": True},
            "constraints": [],
            "assignments": [{"id": "as-" + "d" * 16,
                             "deliverable_id": "worker-slice",
                             "status": "succeeded"}],
            "deliverables": [
                {"id": "worker-slice", "kind": "test",
                 "scope": ["tests/test_example.py"], "executor": "worker",
                 "acceptance": ["guard added"], "dependencies": [],
                 "checks": ["python3 -m pytest tests/test_example.py -q"],
                 "features": dict(self.FEATURES)},
                item,
            ],
        }
        directory = coordination._project_dir(self.repo) / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        coordination._atomic(directory / f"{TASK}.json", task)
        return task

    def _retention_complaints(self, result):
        self._write_task(result)
        return [row for row in coordination.validate_task(self.repo, TASK)
                if COMPLAINT in row]

    # ── the change itself ────────────────────────────────────────────────────
    def test_accepted_with_evidence_raises_no_complaint(self):
        self.assertEqual(self._retention_complaints(
            {"outcome": "accepted", "evidence": "checks passed on the stand"}), [])

    def test_cancelled_with_evidence_stays_exempt(self):
        """The 0.10.5 behaviour is carried over unchanged, not replaced."""
        self.assertEqual(self._retention_complaints(
            {"outcome": "cancelled", "evidence": "superseded by a newer slice"}), [])

    # ── the guards the exemption must NOT waive ──────────────────────────────
    def test_accepted_without_evidence_still_complains(self):
        """A bare ledger outcome cannot waive distribution."""
        self.assertEqual(len(self._retention_complaints({"outcome": "accepted"})), 1)

    def test_accepted_with_blank_evidence_still_complains(self):
        self.assertEqual(len(self._retention_complaints(
            {"outcome": "accepted", "evidence": "   "})), 1)

    def test_invalidated_accepted_result_complains_again(self):
        """Freshness: a later mutation in scope reopens the question."""
        self._write_task({"outcome": "accepted", "evidence": "checks passed"})
        task = coordination.load_task(self.repo, TASK)
        item = next(row for row in task["deliverables"]
                    if row["id"] == "retained-impl")
        item["result_invalidated_at"] = 1
        coordination._atomic(
            coordination._project_dir(self.repo) / "tasks" / f"{TASK}.json", task)
        self.assertEqual(len([row for row in coordination.validate_task(self.repo, TASK)
                              if COMPLAINT in row]), 1)

    def test_pending_deliverable_still_complains(self):
        """Control: without a terminal result the complaint is the old one."""
        self.assertEqual(len(self._retention_complaints(None)), 1)

    # ── differentiability ────────────────────────────────────────────────────
    def test_previous_predicate_would_have_complained_on_accepted(self):
        """Counterfactual: pin that the accepted case is new, not already green.

        Without it the first test could pass for some unrelated reason — for
        example if the complaint never fired on this fixture at all.
        """
        accepted = {"outcome": "accepted", "evidence": "checks passed"}
        task = self._write_task(accepted)
        item = next(row for row in task["deliverables"]
                    if row["id"] == "retained-impl")
        self.assertTrue(coordination._current_terminal_result(task, item))
        self.assertNotEqual(coordination._deliverable_outcome(task, item), "cancelled")
