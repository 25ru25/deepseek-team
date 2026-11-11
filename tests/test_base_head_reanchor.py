"""The planning HEAD follows the plan while nothing is allocated.

`base_head` is written once, when the task is opened, and allocation refuses any
copy whose source HEAD has moved since. A coordination task lives across a whole
session, so a coordinator that commits its own work mid-session kills every
remaining worker launch in that task. These tests pin both sides: the head is
re-anchored by a plan revision while no copy exists, and it is left alone as
soon as one does.
"""
from __future__ import annotations

import subprocess

from codex_deepseek_team import coordination

from test_coordination import CoordinationCase


class BaseHeadReanchorTests(CoordinationCase):
    FEATURES = {
        "domain": "python", "operation": "fix", "localization": "known",
        "coupling": "local", "verification": "none", "clarity": "clear",
        "risk": "low", "scope_size": "small",
    }

    def _deliverables(self, scope="src/one.py"):
        return [
            {"id": "worker-review", "kind": "review", "scope": ["tests/test_worker.py"],
             "executor": "worker", "acceptance": ["findings reviewed"],
             "dependencies": [], "checks": []},
            {"id": "impl", "kind": "implementation", "scope": [scope],
             "executor": "auto", "features": dict(self.FEATURES),
             "acceptance": ["behavior verified"], "dependencies": [], "checks": []},
        ]

    def _open_and_plan(self):
        task = coordination.open_task(
            self.repo, session_id="sess-head", turn_id="turn-head",
            prompt="implement substantial feature", policy=self.policy(),
        )
        coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial", "deliverables": self._deliverables(),
        })
        return task

    def _commit(self, name):
        (self.repo / name).write_text("VALUE = 2\n")
        subprocess.run(["git", "-C", str(self.repo), "add", name], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=Test", "-c",
             "user.email=test@example.test", "commit", "-qm", "later"], check=True)

    def test_revision_reanchors_the_head_while_nothing_is_allocated(self):
        task = self._open_and_plan()
        before = coordination.load_task(self.repo, task["id"])["base_head"]
        self._commit("b.py")
        revised = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": self._deliverables(scope="src/two.py"),
        })
        self.assertNotEqual(revised["base_head"], before)
        self.assertEqual(revised["base_head"], coordination._head(self.repo))

    def test_an_allocated_copy_freezes_the_head(self):
        task = self._open_and_plan()
        stored = coordination.load_task(self.repo, task["id"])
        for row in stored["assignments"]:
            row["workspace_id"] = "0" * 32          # a copy exists for this assignment
        coordination._atomic(coordination._task_path(self.repo, task["id"]), stored)
        before = stored["base_head"]
        self._commit("c.py")
        revised = coordination.plan_task(self.repo, task["id"], {
            "classification": "substantial",
            "deliverables": self._deliverables(scope="src/three.py"),
        })
        self.assertEqual(revised["base_head"], before)
