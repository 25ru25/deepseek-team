"""Fork SN: the worker admission waits for memory instead of starting into OOM."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import memory_gate


class Refusal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class MemoryGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.meminfo = self.root / 'meminfo'
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.config = dict(memory_gate.DEFAULTS)

    def available(self, mb):
        self.meminfo.write_text(f'MemTotal:  8126464 kB\nMemFree:  100 kB\nMemAvailable: {mb * 1024} kB\n')

    def process(self, pid, *argv):
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')

    def wait(self, clock, **kw):
        return memory_gate.wait(Refusal, config=self.config, sleep=clock.sleep,
                                monotonic=clock.monotonic, force=True,
                                meminfo=self.meminfo, proc=self.proc, **kw)

    def test_enough_memory_and_no_gradle_starts_at_once(self):
        self.available(4096)
        self.process(10, '/usr/bin/python3', 'x.py')
        clock = Clock()
        self.assertEqual(self.wait(clock), 0.0)
        self.assertEqual(clock.sleeps, [])

    def test_low_memory_waits_then_proceeds_when_memory_returns(self):
        self.available(1000)
        clock = Clock()
        notices = []

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.now >= 90:
                self.available(5000)
        memory_gate.wait(Refusal, config=self.config, sleep=sleep, monotonic=clock.monotonic,
                         force=True, meminfo=self.meminfo, proc=self.proc, on_wait=notices.append)
        self.assertEqual(clock.sleeps, [30, 30, 30])
        self.assertEqual(len(notices), 1)
        self.assertIn('MemAvailable 1000 MB < 3072 MB', notices[0][0])

    def test_gradle_daemon_or_wrapper_blocks_until_limit_then_refuses(self):
        self.available(6000)
        for argv in (('/usr/lib/jvm/bin/java', '-cp', 'x', 'org.gradle.launcher.daemon.bootstrap.GradleDaemon', '8.10'),
                     ('/bin/sh', './gradlew', 'test'),
                     ('java', 'org.gradle.wrapper.GradleWrapperMain', 'build')):
            with self.subTest(argv=argv[-1]):
                for child in self.proc.iterdir():
                    (child / 'cmdline').unlink(); child.rmdir()
                self.process(4242, *argv)
                clock = Clock()
                with self.assertRaises(Refusal) as caught:
                    self.wait(clock)
                self.assertEqual(caught.exception.code, 75)
                self.assertIn('no memory', caught.exception.message)
                self.assertIn('4242', caught.exception.message)
                self.assertEqual(sum(clock.sleeps), 1200)
                self.assertEqual(clock.sleeps[0], 30)

    def test_deadline_and_validate_are_honoured(self):
        self.available(100)
        clock = Clock()
        checks = []
        with self.assertRaises(Refusal):
            self.wait(clock, deadline=60, validate=lambda: checks.append(clock.now))
        self.assertEqual(checks, [30, 60])

    def test_config_file_disable_and_bad_values(self):
        path = self.root / 'memory-gate.toml'
        path.write_text('min_available_mb = 1024\nmax_wait_seconds = 60\nblock_on_gradle = false\n'
                        'poll_seconds = "fast"\n')
        config = memory_gate.load_config(path)
        self.assertEqual((config['min_available_mb'], config['max_wait_seconds'],
                          config['block_on_gradle'], config['poll_seconds']), (1024, 60, False, 30))
        path.write_text('not = [toml')
        self.assertEqual(memory_gate.load_config(path), memory_gate.DEFAULTS)
        self.config['enabled'] = False
        self.available(1)
        self.assertEqual(self.wait(Clock()), 0.0)
        with mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_MEMORY_GATE': 'off'}):
            self.assertEqual(memory_gate.wait(Refusal), 0.0)

    def test_worker_subprocess_of_the_unittest_runner_is_exempt(self):
        def proc_entry(pid, ppid, *argv):
            (self.proc / str(pid)).mkdir()
            (self.proc / str(pid) / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')
            (self.proc / str(pid) / 'stat').write_text(f'{pid} (x) S {ppid} 0 0')
        with mock.patch.object(memory_gate.os, 'getppid', return_value=30), \
                mock.patch.dict(memory_gate.sys.modules, {'unittest': None}):
            del memory_gate.sys.modules['unittest']
            proc_entry(30, 20, '/bin/sh', '-c', 'deepseek-team worker')
            proc_entry(20, 1, '/usr/bin/python3', '-m', 'unittest', 'discover', '-s', 'tests')
            self.assertTrue(memory_gate._test_runner_active(self.proc))
            (self.proc / '20' / 'cmdline').write_bytes(b'claude\0--resume\0unittest\0')
            self.assertFalse(memory_gate._test_runner_active(self.proc))

    def test_unreadable_meminfo_does_not_block(self):
        self.assertEqual(memory_gate.mem_available_mb(self.root / 'missing'), None)
        self.assertEqual(self.wait(Clock()), 0.0)


if __name__ == '__main__':
    unittest.main()
