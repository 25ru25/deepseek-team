"""Passive per-request provider usage ledger (Fork SN).

The host-side relay already streams every provider response through
``response.read1``. :class:`UsageTap` looks at the same bytes, line by line,
and keeps only the token counters that the provider reports in its own usage
objects. It never stores request or response content, never delays the
stream, and silently gives up on anything it does not understand.

One JSON line per billed provider request is appended to
``~/.local/state/codex-deepseek/usage/ledger.jsonl`` (directory 0700, file
0600, ``flock``-serialised append). ``DEEPSEEK_TEAM_USAGE_LEDGER`` overrides
the file; ``off`` disables writing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path

MAX_LINE = 256 * 1024          # SSE lines longer than this cannot carry usage we need
MAX_JSON_BODY = 8 * 1024 * 1024  # non-streamed bodies are parsed only up to this size
SKIPPED_ENDPOINTS = ('/anthropic/v1/messages/count_tokens',)


def ledger_path():
    override = os.environ.get('DEEPSEEK_TEAM_USAGE_LEDGER')
    if override == 'off':
        return None
    if override:
        return Path(override)
    return Path.home() / '.local' / 'state' / 'codex-deepseek' / 'usage' / 'ledger.jsonl'


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class UsageTap:
    """Incremental, content-free usage extractor for one provider response."""

    def __init__(self, content_type=''):
        self.stream = 'text/event-stream' in (content_type or '').lower()
        self.model = None
        self.input_miss = None
        self.input_hit = None
        self.output = None
        self._partial = bytearray()
        self._skip_line = False
        self._body = bytearray()
        self._body_overflow = False

    # -- feeding -----------------------------------------------------------
    def feed(self, data):
        try:
            if self.stream:
                self._feed_sse(data)
            elif not self._body_overflow:
                if len(self._body) + len(data) > MAX_JSON_BODY:
                    self._body_overflow = True
                    self._body.clear()
                else:
                    self._body += data
        except Exception:  # noqa: BLE001 - accounting must never disturb the relay
            pass

    def close(self):
        try:
            if self.stream:
                if self._partial and not self._skip_line:
                    self._line(bytes(self._partial))
            elif self._body and not self._body_overflow:
                value = json.loads(bytes(self._body))
                if isinstance(value, dict):
                    self._object(value)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._partial.clear()
            self._body.clear()

    def _feed_sse(self, data):
        start = 0
        while True:
            end = data.find(b'\n', start)
            if end < 0:
                rest = data[start:]
                if not self._skip_line:
                    if len(self._partial) + len(rest) > MAX_LINE:
                        self._partial.clear()
                        self._skip_line = True
                    else:
                        self._partial += rest
                return
            if self._skip_line:
                self._skip_line = False
            else:
                piece = data[start:end]
                if self._partial:
                    self._partial += piece
                    line = bytes(self._partial)
                    self._partial.clear()
                else:
                    line = bytes(piece)
                if len(line) <= MAX_LINE:
                    self._line(line)
            start = end + 1

    def _line(self, line):
        line = line.rstrip(b'\r')
        if not line.startswith(b'data:'):
            return
        # Cheap pre-filter: content deltas are never decoded.
        if b'usage' not in line and b'"model"' not in line:
            return
        try:
            value = json.loads(line[5:].strip())
        except (ValueError, RecursionError):
            return
        if isinstance(value, dict):
            self._event(value)

    # -- interpretation ----------------------------------------------------
    def _event(self, event):
        kind = event.get('type')
        if kind == 'message_start' and isinstance(event.get('message'), dict):
            self._object(event['message'])
        elif kind == 'message_delta':
            self._usage(event.get('usage'))
        elif kind in ('response.completed', 'response.incomplete', 'response.failed') \
                and isinstance(event.get('response'), dict):
            self._object(event['response'])
        else:
            self._object(event)  # chat-completions style chunks carry usage at top level

    def _object(self, value):
        model = value.get('model')
        if isinstance(model, str) and model and len(model) <= 200:
            self.model = model
        self._usage(value.get('usage'))

    def _usage(self, usage):
        if not isinstance(usage, dict):
            return
        output = _int(usage.get('output_tokens'))
        if output is None:
            output = _int(usage.get('completion_tokens'))
        if output is not None:
            self.output = output
        # Anthropic: input_tokens excludes cache reads and cache writes.
        read = _int(usage.get('cache_read_input_tokens'))
        if read is not None or 'cache_creation_input_tokens' in usage:
            plain = _int(usage.get('input_tokens'))
            write = _int(usage.get('cache_creation_input_tokens')) or 0
            if plain is not None:
                self.input_miss = plain + write
            if read is not None:
                self.input_hit = read
            return
        # DeepSeek chat completions.
        hit = _int(usage.get('prompt_cache_hit_tokens'))
        miss = _int(usage.get('prompt_cache_miss_tokens'))
        if hit is not None or miss is not None:
            self.input_hit, self.input_miss = hit or 0, miss or 0
            return
        # OpenAI Responses / chat: input_tokens (or prompt_tokens) include cached ones.
        total = _int(usage.get('input_tokens'))
        if total is None:
            total = _int(usage.get('prompt_tokens'))
        details = usage.get('input_tokens_details') or usage.get('prompt_tokens_details')
        cached = _int(details.get('cached_tokens')) if isinstance(details, dict) else None
        if total is not None:
            cached = min(cached or 0, total)
            self.input_hit, self.input_miss = cached, total - cached

    @property
    def found(self):
        return any(v is not None for v in (self.input_miss, self.input_hit, self.output))

    def counters(self):
        return {'input_miss': self.input_miss or 0, 'input_hit': self.input_hit or 0,
                'output': self.output or 0}


def request_model(body):
    """Model name from a request body (only the ``model`` key is read)."""
    try:
        value = json.loads(body)
        model = value.get('model') if isinstance(value, dict) else None
        return model if isinstance(model, str) and len(model) <= 200 else None
    except Exception:  # noqa: BLE001
        return None


def runtime_summary(runtime, raw):
    """Aggregate usage reported by a worker CLI itself (direct read-only path).

    Returns ``[(model, counters, requests)]``; empty when nothing is reported.
    """
    rows = []
    try:
        if runtime in ('claude', 'qwen'):
            value = json.loads(raw)
            if not isinstance(value, dict):
                return []
            turns = _int(value.get('num_turns'))
            per_model = value.get('modelUsage')
            if isinstance(per_model, dict) and per_model:
                for model, item in per_model.items():
                    if not isinstance(item, dict):
                        continue
                    tap = UsageTap()
                    tap._usage({'input_tokens': item.get('inputTokens'),
                                'cache_read_input_tokens': item.get('cacheReadInputTokens'),
                                'cache_creation_input_tokens': item.get('cacheCreationInputTokens'),
                                'output_tokens': item.get('outputTokens')})
                    if tap.found:
                        rows.append((str(model)[:200], tap.counters(), None))
            if not rows:
                tap = UsageTap()
                tap._object(value)
                if tap.found:
                    rows.append((tap.model, tap.counters(), None))
            if len(rows) == 1:
                rows[0] = (rows[0][0], rows[0][1], turns)
        elif runtime == 'codex':
            hit = miss = out = 0
            turns = 0
            for line in raw.splitlines():
                if 'turn.completed' not in line:
                    continue
                event = json.loads(line)
                usage = event.get('usage') if isinstance(event, dict) else None
                if not isinstance(usage, dict):
                    continue
                total = _int(usage.get('input_tokens')) or 0
                cached = min(_int(usage.get('cached_input_tokens')) or 0, total)
                hit += cached
                miss += total - cached
                out += _int(usage.get('output_tokens')) or 0
                turns += 1
            if turns:
                rows.append((None, {'input_miss': miss, 'input_hit': hit, 'output': out}, turns))
    except Exception:  # noqa: BLE001
        return []
    return rows


def context(**extra):
    """Process-level attribution: never content, only identifiers."""
    value = {
        'coordinator_session': os.environ.get('CLAUDE_CODE_SESSION_ID') or None,
        'project': None,
    }
    try:
        value['project'] = str(Path.cwd())
    except OSError:
        pass
    for key, item in extra.items():
        if item is not None:
            value[key] = str(item)
    return value


def append(record, path=None):
    """Append one record; any failure is swallowed."""
    try:
        path = ledger_path() if path is None else Path(path)
        if path is None:
            return False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_CLOEXEC', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, line)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001
        return False


def record(*, endpoint, model, counters, status, ctx, source, requests=None):
    value = dict(ctx or {})
    value.update(ts=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                 endpoint=endpoint, model=model, status=status, source=source,
                 **counters)
    if requests is not None:
        value['requests'] = requests
    return value
