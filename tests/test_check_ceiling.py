"""The per-command ceiling for declared checks is explicit, not fixed at 120 s.

A declared check may legitimately run for minutes — a compiler, a full test
suite, an integration stand. While the automatic runner's ceiling was fixed at
120 s, any such check was excluded from delegation by construction: the work had
to stay with the coordinator even when the sandbox could run it.

The default is unchanged. `0` means "no per-command ceiling", and is expressed as
a large finite value because the runner computes `min(timeout, deadline - now)`
and feeds the result to a selector, where zero kills the command immediately and
infinity is not a valid timeout.
"""
from __future__ import annotations

import argparse
import unittest

from codex_deepseek_team import managed


class CheckCeilingTests(unittest.TestCase):
    @staticmethod
    def _args(**kwargs):
        return argparse.Namespace(**kwargs)

    def test_default_is_the_historical_ceiling(self):
        self.assertEqual(managed._check_ceiling(self._args()), 120.0)
        self.assertEqual(managed._check_ceiling(self._args(check_timeout=120)), 120.0)

    def test_an_explicit_value_is_used_as_given(self):
        self.assertEqual(managed._check_ceiling(self._args(check_timeout=600)), 600.0)
        self.assertEqual(managed._check_ceiling(self._args(check_timeout=45.5)), 45.5)

    def test_zero_removes_the_per_command_ceiling_without_becoming_zero(self):
        value = managed._check_ceiling(self._args(check_timeout=0))
        self.assertGreater(value, 120.0)
        self.assertTrue(value == value and value != float("inf"))  # finite

    def test_unusable_values_fall_back_to_the_default(self):
        for bad in (-1, "slow", None, object()):
            self.assertEqual(managed._check_ceiling(self._args(check_timeout=bad)), 120.0)


if __name__ == "__main__":
    unittest.main()
