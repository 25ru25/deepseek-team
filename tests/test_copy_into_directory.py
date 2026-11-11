"""`cp SRC... DIR/` writes the files it creates, not the directory.

The scope check compares written paths against declared ones. A declared scope
names files, so reporting the destination DIRECTORY made an otherwise legitimate
copy unmappable: every path the command touches was declared, and the mutation
was refused anyway.

Only the provable shapes are expanded — an explicit trailing slash, or more than
one source, which cannot be a rename. `cp a b` stays a single destination,
because deciding whether `b` is an existing directory would mean touching the
filesystem, and this parser reads the command only.
"""
from __future__ import annotations

import unittest

from codex_deepseek_team.shell_mutation import classify_shell_mutation_scoped as classify


class CopyIntoDirectoryTests(unittest.TestCase):
    @staticmethod
    def _paths(command):
        is_mutation, paths, uncertain = classify(command)
        return is_mutation, sorted(paths), uncertain

    def test_trailing_slash_names_the_created_file(self):
        is_mutation, paths, _ = self._paths("cp src/a.py tests/unit/")
        self.assertTrue(is_mutation)
        self.assertEqual(paths, ["tests/unit/a.py"])

    def test_several_sources_name_every_created_file(self):
        _, paths, _ = self._paths("cp src/a.py src/b.py tests/unit/")
        self.assertEqual(paths, ["tests/unit/a.py", "tests/unit/b.py"])

    def test_a_plain_rename_still_names_the_destination(self):
        _, paths, _ = self._paths("cp src/a.py tests/unit/a.py")
        self.assertEqual(paths, ["tests/unit/a.py"])

    def test_a_single_source_without_a_slash_is_left_alone(self):
        """`b` could be a file or a directory; the parser does not guess."""
        _, paths, _ = self._paths("cp src/a.py build")
        self.assertEqual(paths, ["build"])

    def test_a_directory_source_keeps_its_own_name(self):
        _, paths, _ = self._paths("cp -r src/pkg/ build/")
        self.assertEqual(paths, ["build/pkg"])

    def test_a_lone_operand_writes_nothing(self):
        _, paths, _ = self._paths("cp src/a.py")
        self.assertEqual(paths, [])


if __name__ == "__main__":
    unittest.main()
