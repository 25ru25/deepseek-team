"""A tool's own cache under a prepared root must not be captured as worker output.

`workspace prepare --env` may point a variable at the COMMON PARENT of several
copy destinations — a Gradle home above its wrapper and its module cache, say.
The destinations themselves are excluded from result capture, because they are
coordinator-prepared source rather than worker output. The parent was not.

A tool running inside the sandbox writes into that parent: Gradle fills
`caches/`, `daemon/`, `native/` and `kotlin-profile/` under its home on the
first build. Capture then adds all of it to `changes.patch`, the patch exceeds
`MAX_PATCH_BYTES`, and the copy can no longer be finalized by any supported
route — not by acceptance, not by cancellation.

These tests pin the narrow shape: the first copy-relative component of a value
that resolves strictly inside the copy, and nothing else. A value pointing
outside the copy, a relative value, the copy root itself and a non-string value
all contribute nothing, so nothing outside the prepared area is hidden from
capture.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_deepseek_team.toolchains import _prepared_root_excludes


class PreparedRootExcludesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "copy"
        self.root.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def excludes(self, environment):
        return _prepared_root_excludes(self.root, environment)

    # ── the change itself ────────────────────────────────────────────────────
    def test_placeholder_value_excludes_its_first_component(self):
        self.assertEqual(
            self.excludes({"GRADLE_USER_HOME": "{workspace}/gradle-home"}),
            ["/gradle-home/"])

    def test_deep_value_still_excludes_only_the_first_component(self):
        """The root is what a tool fills, so the root is what must be excluded."""
        self.assertEqual(
            self.excludes({"GRADLE_USER_HOME": "{workspace}/gradle-home/caches"}),
            ["/gradle-home/"])

    def test_path_list_contributes_every_element_inside_the_copy(self):
        self.assertEqual(
            self.excludes({"PATH": "{workspace}/tools/bin:/usr/bin:{workspace}/jdk/bin"}),
            ["/tools/", "/jdk/"])

    # ── nothing outside the prepared area is hidden ──────────────────────────
    def test_value_outside_the_copy_contributes_nothing(self):
        self.assertEqual(self.excludes({"JAVA_HOME": "/usr/lib/jvm/temurin"}), [])

    def test_relative_value_contributes_nothing(self):
        self.assertEqual(self.excludes({"M2_HOME": "relative/path"}), [])

    def test_copy_root_itself_contributes_nothing(self):
        """`.` has no first component, so there is nothing to exclude."""
        self.assertEqual(self.excludes({"GRADLE_USER_HOME": "{workspace}"}), [])

    def test_non_string_value_is_ignored(self):
        self.assertEqual(self.excludes({"BROKEN": None}), [])

    def test_empty_environment_contributes_nothing(self):
        self.assertEqual(self.excludes({}), [])

    # ── glob metacharacters in a directory name stay literal ─────────────────
    def test_special_characters_are_escaped(self):
        self.assertEqual(
            self.excludes({"KOTLIN_HOME": "{workspace}/weird[1]/bin"}),
            ["/weird\\[1\\]/"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
