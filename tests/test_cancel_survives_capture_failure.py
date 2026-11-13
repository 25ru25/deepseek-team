"""A refused capture must not lock a cancelled copy forever.

`_capture` is strict on purpose: it refuses when the copy's Git administrative
state has moved since preparation, because a patch taken from a tree whose
baseline changed is not the worker's work. On ACCEPTANCE that strictness is
right — the patch is the deliverable.

On CANCELLATION it is not. Cancelling gives the result up by definition, so
there is nothing the patch could be wrong about, and the refusal removes the
only remaining way out of the copy: acceptance is refused for the same reason,
cancellation raises before the record is written, and nothing supported deletes
the tree. In practice one ordinary read inside the copy — `git status`, which
writes `.git/index` — is enough to reach that state and strand the copy.

These tests pin both halves: a cancellation survives a refused capture and says
so in `changes.patch`, while acceptance keeps failing exactly as before.
"""
import json
from unittest import mock

from tests.test_workspace_lifecycle import LifecycleFixture


class CancelSurvivesCaptureFailureTests(LifecycleFixture):
    def completed(self):
        """A finished copy with tracked, binary and untracked changes.

        Same shape as the fixture in test_workspace_gc, repeated here rather
        than inherited so this module does not re-run that module's cases.
        """
        self.require_capture()
        copy = self.copy()
        (copy.path / 'app.txt').write_text('accepted fix\n')
        (copy.path / 'data.bin').write_bytes(b'\x00\xfe\x81new\x00')
        (copy.path / 'new.bin').write_bytes(b'\x00untracked\xff')
        with copy.lock():
            copy.begin('execution')
            copy.finish('succeeded')
        return copy
    REFUSAL = 'Git administrative state changed; result capture refused.'

    def _stale_git_digest(self, copy):
        """Reproduce the real trigger: the copy's Git metadata moved."""
        copy.metadata['git_digest'] = 'stale-' + (copy.metadata.get('git_digest') or '')
        (copy.directory / 'record.json').write_text(json.dumps(dict(copy.metadata)))

    # ── the change itself ────────────────────────────────────────────────────
    def test_cancellation_survives_a_refused_capture(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        self._stale_git_digest(copy)
        gc.cancel(copy, reason='explicit rejection')
        self.assertFalse(copy.path.exists(), 'the execution tree was not reclaimed')
        stored = (copy.directory / 'changes.patch').read_bytes()
        self.assertTrue(stored.startswith(b'# result capture refused on cancellation:'),
                        f'the reason was not preserved: {stored[:120]!r}')
        self.assertIn(b'Git administrative state changed', stored)

    def test_the_refusal_reason_is_kept_in_the_file_not_only_in_a_log(self):
        gc = self.api('workspace_gc')
        copy = self.completed()
        with mock.patch.object(gc, '_capture',
                               side_effect=RuntimeError('fixture refused capture')):
            gc.cancel(copy)
        self.assertIn(b'fixture refused capture',
                      (copy.directory / 'changes.patch').read_bytes())

    # ── unchanged behaviour, pinned so the relaxation stays narrow ───────────
    def test_acceptance_still_fails_on_a_refused_capture(self):
        """On acceptance the patch IS the deliverable; strictness stays."""
        gc = self.api('workspace_gc')
        copy = self.completed()
        self._stale_git_digest(copy)
        with self.assertRaises(Exception) as caught:
            gc.finalize(copy, reason='accepted')
        self.assertIn('result capture refused', str(caught.exception))
        self.assertTrue(copy.path.exists(),
                        'the tree must survive a refused acceptance for inspection')

    def test_a_clean_cancellation_still_stores_the_real_patch(self):
        """Control: without a refusal the stored patch is the captured one."""
        gc = self.api('workspace_gc')
        copy = self.completed()
        gc.cancel(copy)
        stored = (copy.directory / 'changes.patch').read_bytes()
        self.assertNotIn(b'result capture refused', stored)

    def test_cancelled_phase_is_recorded_as_before(self):
        gc, lifecycle = self.api('workspace_gc'), self.api()
        copy = self.completed()
        self._stale_git_digest(copy)
        gc.cancel(copy)
        self.assertEqual(lifecycle.read(copy)['phase'], 'cancelled')
