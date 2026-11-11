"""A worker report that does not fit says so instead of ending mid-sentence.

The report is what the coordinator acts on. A silent cut is indistinguishable
from a short report: nothing tells the reader that a numbered list ended at item
eleven because the worker stopped there, or because the ledger stopped storing
it. The bound is unchanged here; only the silence is removed.
"""
from __future__ import annotations

import unittest

from codex_deepseek_team import coordination


class ResultTruncationNoticeTests(unittest.TestCase):
    LIMIT = coordination.RESULT_SUMMARY_LIMIT

    def test_a_short_report_is_stored_verbatim(self):
        text = "finished; two checks passed"
        self.assertEqual(coordination._bounded_result(text), text)

    def test_a_report_at_the_limit_is_untouched(self):
        text = "x" * self.LIMIT
        self.assertEqual(coordination._bounded_result(text), text)

    def test_a_longer_report_says_how_much_was_dropped(self):
        text = "y" * (self.LIMIT + 137)
        stored = coordination._bounded_result(text)
        self.assertLessEqual(len(stored), self.LIMIT)
        self.assertIn("result truncated by the ledger", stored)
        self.assertIn("137", stored)

    def test_the_notice_never_pushes_the_value_over_the_bound(self):
        for extra in (1, 10, 10_000):
            stored = coordination._bounded_result("z" * (self.LIMIT + extra))
            self.assertLessEqual(len(stored), self.LIMIT)

    def test_a_non_string_report_is_still_bounded(self):
        stored = coordination._bounded_result(["a" * self.LIMIT, "b" * self.LIMIT])
        self.assertLessEqual(len(stored), self.LIMIT)
        self.assertIn("result truncated by the ledger", stored)


if __name__ == "__main__":
    unittest.main()
