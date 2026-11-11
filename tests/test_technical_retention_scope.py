"""A runner-recorded technical constraint is a property of the project, not of one task.

The runner records an environment limit (missing toolchain, unavailable runtime)
against a single ``deliverable_id`` of a single coordination task. Coordination
tasks are opened per session, so the same limit has to be re-proved in every new
task — which in practice means launching a worker that is known in advance to
fail, only to recreate a record that already exists.

These tests pin the narrow relaxation: a sibling record of the SAME project is
accepted only for a deliverable that declares no executable checks, i.e. one the
router already resolved to the coordinator as unverifiable. A deliverable with a
working check stays delegable and is unaffected.
"""
from __future__ import annotations

import json

from codex_deepseek_team import coordination

from test_coordination import CoordinationCase

SIBLING = "task-" + "a" * 20
CURRENT = "task-" + "b" * 20
CODE = "dependency_unavailable"


class TechnicalRetentionScopeTests(CoordinationCase):
    def _sibling_with_constraint(self, code=CODE, source="runner"):
        directory = coordination._project_dir(self.repo) / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{SIBLING}.json").write_text(json.dumps({
            "id": SIBLING,
            "schema": 2,
            "constraints": [{
                "deliverable_id": "some-other-deliverable",
                "code": code,
                "source": source,
                "evidence": "preparation missing declared check runtime",
                "at": 0,
            }],
        }))

    @staticmethod
    def _task(constraints=()):
        return {"id": CURRENT, "schema": 2, "constraints": list(constraints)}

    @staticmethod
    def _deliverable(checks=()):
        return {
            "id": "kotlin-slice",
            "kind": "implementation",
            "checks": list(checks),
            "retention": {"code": CODE, "evidence": "toolchain absent in the sandbox"},
        }

    def test_without_root_behaviour_is_unchanged(self):
        self._sibling_with_constraint()
        self.assertFalse(coordination._retention_ok(self._task(), self._deliverable()))

    def test_sibling_constraint_accepted_when_no_checks_declared(self):
        self._sibling_with_constraint()
        self.assertTrue(
            coordination._retention_ok(self._task(), self._deliverable(), self.repo)
        )

    def test_sibling_constraint_rejected_when_checks_are_declared(self):
        """The narrow condition: a deliverable with a working check stays delegable."""
        self._sibling_with_constraint()
        self.assertFalse(
            coordination._retention_ok(
                self._task(), self._deliverable(checks=["node guard.test.js"]), self.repo
            )
        )

    def test_sibling_constraint_must_come_from_the_runner(self):
        self._sibling_with_constraint(source="coordinator")
        self.assertFalse(
            coordination._retention_ok(self._task(), self._deliverable(), self.repo)
        )

    def test_sibling_constraint_code_must_match(self):
        self._sibling_with_constraint(code="environment_incompatible")
        self.assertFalse(
            coordination._retention_ok(self._task(), self._deliverable(), self.repo)
        )

    def test_same_task_constraint_still_wins_without_root(self):
        """Regression: the original same-deliverable path is untouched."""
        task = self._task(constraints=[{
            "deliverable_id": "kotlin-slice",
            "code": CODE,
            "source": "runner",
            "evidence": "preparation failed",
            "at": 0,
        }])
        self.assertTrue(coordination._retention_ok(task, self._deliverable()))
