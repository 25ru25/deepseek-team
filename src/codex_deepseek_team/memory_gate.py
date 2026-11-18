"""Fork SN: wait for memory before a worker launch.

On a small host a DeepSeek worker that starts next to a Gradle build (or with
little free memory) gets the whole tree OOM-killed. Before the worker takes
credentials the admission waits while ``MemAvailable`` is below a threshold or
a Gradle daemon/wrapper process is alive, polling every ``poll_seconds`` up to
``max_wait_seconds``; then it refuses with an explicit "no memory" error
instead of starting.

Configuration: ``~/.config/deepseek-team/memory-gate.toml`` (all keys optional)::

    enabled = true
    min_available_mb = 3072
    max_wait_seconds = 1200
    poll_seconds = 30
    block_on_gradle = true

``DEEPSEEK_TEAM_MEMORY_GATE=off`` disables the gate for one process.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time

DEFAULTS = {'enabled': True, 'min_available_mb': 3072, 'max_wait_seconds': 1200,
            'poll_seconds': 30, 'block_on_gradle': True}
GRADLE_MARKERS = (b'org.gradle.launcher.daemon.bootstrap.GradleDaemon',
                  b'org.gradle.wrapper.GradleWrapperMain')
GRADLE_PROGRAMS = ('gradlew', 'gradle')


def config_path() -> Path:
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'deepseek-team' / 'memory-gate.toml'


def load_config(path: Path | None = None) -> dict:
    values = dict(DEFAULTS)
    path = config_path() if path is None else path
    try:
        import tomllib
        with open(path, 'rb') as stream:
            loaded = tomllib.load(stream)
    except FileNotFoundError:
        return values
    except Exception:  # noqa: BLE001 - a broken file keeps safe defaults
        return values
    for key, default in DEFAULTS.items():
        value = loaded.get(key)
        if type(value) is type(default) and (not isinstance(value, int) or value >= 0):
            values[key] = value
    return values


def mem_available_mb(meminfo: Path = Path('/proc/meminfo')) -> int | None:
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def gradle_processes(proc: Path = Path('/proc')) -> list[int]:
    own = os.getpid()
    found = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == own:
            continue
        try:
            cmdline = (entry / 'cmdline').read_bytes()
        except OSError:
            continue
        if not cmdline:
            continue
        argv0 = os.path.basename(cmdline.split(b'\0', 1)[0]).decode(errors='replace')
        args = cmdline.split(b'\0')
        if (any(marker in cmdline for marker in GRADLE_MARKERS) or argv0 in GRADLE_PROGRAMS
                or (argv0 in ('sh', 'bash', 'dash') and len(args) > 1
                    and os.path.basename(args[1]).decode(errors='replace') == 'gradlew')):
            found.append(int(entry.name))
    return sorted(found)


def blockers(config: dict, *, meminfo: Path = Path('/proc/meminfo'),
             proc: Path = Path('/proc')) -> list[str]:
    reasons = []
    available = mem_available_mb(meminfo)
    if available is not None and available < config['min_available_mb']:
        reasons.append(f'MemAvailable {available} MB < {config["min_available_mb"]} MB')
    if config['block_on_gradle']:
        pids = gradle_processes(proc)
        if pids:
            reasons.append('Gradle daemon/build running (pid ' + ', '.join(map(str, pids[:5])) + ')')
    return reasons


def _test_runner_active(proc: Path = Path('/proc')) -> bool:
    """The test suite runs fixture workers against the real host, where a
    parallel Gradle build must not stall them. The package never imports
    unittest; the suite does, and its worker subprocesses descend from a
    ``python -m unittest`` process. Gate tests pass ``force=True``."""
    if 'unittest' in sys.modules:
        return True
    pid = os.getppid()
    for _ in range(16):
        if pid <= 1:
            return False
        try:
            argv = (proc / str(pid) / 'cmdline').read_bytes().split(b'\0')
            if any(argv[i] == b'-m' and argv[i + 1] == b'unittest' for i in range(len(argv) - 1)):
                return True
            stat = (proc / str(pid) / 'stat').read_text()
            pid = int(stat.rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def wait(error_factory, *, config: dict | None = None, validate=None, deadline=None,
         on_wait=None, sleep=time.sleep, monotonic=time.monotonic, force: bool = False,
         meminfo: Path = Path('/proc/meminfo'), proc: Path = Path('/proc')) -> float:
    """Block until memory is available; returns seconds waited.

    ``error_factory(code, message)`` builds the refusal (75 = temporary failure).
    """
    if not force and (os.environ.get('DEEPSEEK_TEAM_MEMORY_GATE') == 'off' or _test_runner_active()):
        return 0.0
    config = load_config() if config is None else config
    if not config['enabled']:
        return 0.0
    started = monotonic()
    announced = False
    while True:
        reasons = blockers(config, meminfo=meminfo, proc=proc)
        if not reasons:
            return monotonic() - started
        waited = monotonic() - started
        if waited >= config['max_wait_seconds'] or (deadline is not None and monotonic() >= deadline):
            raise error_factory(75, (
                'Not enough memory to start a DeepSeek worker (no memory): '
                + '; '.join(reasons) + f'. Waited {int(waited)} s. The worker was NOT started; '
                'finish or stop the Gradle build (./gradlew --stop) or free memory, then relaunch. '
                f'Thresholds: {config_path()}.'))
        if not announced and on_wait is not None:
            on_wait(reasons)
            announced = True
        sleep(max(1, min(config['poll_seconds'], config['max_wait_seconds'] - waited)))
        if validate is not None:
            validate()
