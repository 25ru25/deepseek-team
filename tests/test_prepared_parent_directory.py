"""A prepared variable may name the common parent of several copy destinations.

`workspace prepare` validates every `--env` value against the copy before any
write: the value must resolve strictly inside the owned copy AND name an
existing directory, either on disk or among the staged `--copy` destinations.

The staged check only walked DOWNWARD — it asked whether the value lies inside
a declared destination. A value that lies ABOVE them, that is their common
parent, matched nothing and the recipe was refused, although the parent is
created by the move itself and its chain is proven safe before any write.

The shape that hits this is ordinary. A JVM toolchain wants one variable for a
Gradle home with two caches underneath:

    --copy <host>/wrapper=gradle-home/wrapper
    --copy <host>/modules-2=gradle-home/caches/modules-2
    --env  GRADLE_USER_HOME={workspace}/gradle-home

`gradle-home` is neither on disk yet nor inside either destination, so the
recipe can never be replayed.

These tests pin both directions: the parent is accepted, and nothing else
becomes acceptable — a sibling, a path outside every destination and a staged
entry whose own source is missing are all still refused.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_deepseek_team.toolchains import _staged_directory_exists


class PreparedParentDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Two declared destinations under a common parent that does not exist yet.
        self.wrapper_src = self.root / "staging" / "wrapper"
        self.modules_src = self.root / "staging" / "modules-2"
        self.wrapper_src.mkdir(parents=True)
        self.modules_src.mkdir(parents=True)
        self.staged = [
            (self.root / "copy" / "gradle-home" / "wrapper", self.wrapper_src),
            (self.root / "copy" / "gradle-home" / "caches" / "modules-2",
             self.modules_src),
        ]

    def exists(self, value: str) -> bool:
        return _staged_directory_exists(value, self.staged)

    # ── the change itself ────────────────────────────────────────────────────
    def test_common_parent_of_two_destinations_is_accepted(self):
        self.assertTrue(self.exists(str(self.root / "copy" / "gradle-home")))

    def test_intermediate_parent_of_one_destination_is_accepted(self):
        self.assertTrue(
            self.exists(str(self.root / "copy" / "gradle-home" / "caches")))

    # ── unchanged behaviour, pinned so the relaxation stays narrow ───────────
    def test_destination_itself_is_still_accepted(self):
        self.assertTrue(
            self.exists(str(self.root / "copy" / "gradle-home" / "wrapper")))

    def test_path_below_a_destination_follows_the_staged_source(self):
        (self.wrapper_src / "dists").mkdir()
        self.assertTrue(self.exists(
            str(self.root / "copy" / "gradle-home" / "wrapper" / "dists")))
        self.assertFalse(self.exists(
            str(self.root / "copy" / "gradle-home" / "wrapper" / "absent")))

    def test_sibling_outside_every_destination_is_refused(self):
        self.assertFalse(self.exists(str(self.root / "copy" / "elsewhere")))

    def test_parent_is_refused_when_the_staged_source_is_missing(self):
        """The parent is only real because something real moves into it."""
        staged = [(self.root / "copy" / "gradle-home" / "wrapper",
                   self.root / "staging" / "never-created")]
        self.assertFalse(_staged_directory_exists(
            str(self.root / "copy" / "gradle-home"), staged))

    def test_copy_root_itself_is_accepted_only_through_a_staged_source(self):
        self.assertTrue(self.exists(str(self.root / "copy")))
        self.assertFalse(_staged_directory_exists(str(self.root / "copy"), []))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
