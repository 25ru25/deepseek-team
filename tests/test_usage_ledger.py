"""Fork SN: passive usage accounting in the host relay."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

from codex_deepseek_team import relay, usage_ledger
from codex_deepseek_team.relay import UnixHTTPConnection


ANTHROPIC_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"m1","model":"deepseek-flash",'
    b'"usage":{"input_tokens":120,"cache_read_input_tokens":3000,"output_tokens":1}}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"secret usage words"}}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":45}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n')

RESPONSES_SSE = (
    b'event: response.created\n'
    b'data: {"type":"response.created","response":{"model":"deepseek-flash","usage":null}}\n\n'
    b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n'
    b'event: response.completed\n'
    b'data: {"type":"response.completed","response":{"model":"deepseek-v4-pro","usage":'
    b'{"input_tokens":1000,"input_tokens_details":{"cached_tokens":800},"output_tokens":70}}}\n\n')


def feed_in_chunks(tap, data, size):
    for start in range(0, len(data), size):
        tap.feed(data[start:start + size])
    tap.close()


class UsageTapTests(unittest.TestCase):
    def test_anthropic_stream_at_every_chunk_boundary(self):
        for size in (1, 2, 7, 64, 65536):
            with self.subTest(size=size):
                tap = usage_ledger.UsageTap('text/event-stream; charset=utf-8')
                feed_in_chunks(tap, ANTHROPIC_SSE, size)
                self.assertEqual(tap.model, 'deepseek-flash')
                self.assertEqual(tap.counters(), {'input_miss': 120, 'input_hit': 3000, 'output': 45})

    def test_anthropic_cache_creation_counts_as_miss(self):
        tap = usage_ledger.UsageTap('application/json')
        tap.feed(json.dumps({'model': 'deepseek-flash', 'content': [], 'usage': {
            'input_tokens': 10, 'cache_creation_input_tokens': 5,
            'cache_read_input_tokens': 7, 'output_tokens': 3}}).encode())
        tap.close()
        self.assertEqual(tap.counters(), {'input_miss': 15, 'input_hit': 7, 'output': 3})

    def test_responses_stream_and_json(self):
        tap = usage_ledger.UsageTap('text/event-stream')
        feed_in_chunks(tap, RESPONSES_SSE.replace(b'\n', b'\r\n'), 5)
        self.assertEqual(tap.model, 'deepseek-v4-pro')
        self.assertEqual(tap.counters(), {'input_miss': 200, 'input_hit': 800, 'output': 70})
        tap = usage_ledger.UsageTap('application/json')
        tap.feed(b'{"model":"deepseek-flash","output":[],"usage":{"input_tokens":50,'
                 b'"input_tokens_details":{"cached_tokens":20},"output_tokens":9}}')
        tap.close()
        self.assertEqual(tap.counters(), {'input_miss': 30, 'input_hit': 20, 'output': 9})

    def test_garbage_and_oversized_lines_are_ignored(self):
        tap = usage_ledger.UsageTap('text/event-stream')
        noise = (b'data: {not json usage\n' + b'data: ' + b'x' * (usage_ledger.MAX_LINE + 10)
                 + b'usage\n' + b'\xff\xfe\n' + b'data: [1,2]\n' + b'data: "usage"\n')
        feed_in_chunks(tap, noise + ANTHROPIC_SSE, 333)
        self.assertEqual(tap.counters(), {'input_miss': 120, 'input_hit': 3000, 'output': 45})
        for body in (b'', b'not json', b'[1]', b'{"usage":"x"}', b'{"usage":{"input_tokens":-1}}'):
            with self.subTest(body=body):
                tap = usage_ledger.UsageTap('application/json')
                tap.feed(body)
                tap.close()
                self.assertFalse(tap.found)
                self.assertEqual(tap.counters(), {'input_miss': 0, 'input_hit': 0, 'output': 0})

    def test_oversized_json_body_is_not_buffered(self):
        tap = usage_ledger.UsageTap('application/json')
        with mock.patch.object(usage_ledger, 'MAX_JSON_BODY', 16):
            tap.feed(b'{"usage":{"output_tokens":5}}')
        tap.close()
        self.assertFalse(tap.found)
        self.assertEqual(len(tap._body), 0)

    def test_runtime_summaries(self):
        claude = json.dumps({'type': 'result', 'is_error': False, 'result': 'ok', 'num_turns': 3,
                             'modelUsage': {'deepseek-flash': {'inputTokens': 11, 'outputTokens': 4,
                                            'cacheReadInputTokens': 90, 'cacheCreationInputTokens': 0}}})
        self.assertEqual(usage_ledger.runtime_summary('claude', claude),
                         [('deepseek-flash', {'input_miss': 11, 'input_hit': 90, 'output': 4}, 3)])
        codex = '\n'.join([json.dumps({'type': 'turn.completed', 'usage': {
            'input_tokens': 100, 'cached_input_tokens': 60, 'output_tokens': 8}})] * 2)
        self.assertEqual(usage_ledger.runtime_summary('codex', codex),
                         [(None, {'input_miss': 80, 'input_hit': 120, 'output': 16}, 2)])
        self.assertEqual(usage_ledger.runtime_summary('claude', 'garbage'), [])


class LedgerFileTests(unittest.TestCase):
    def test_private_append_and_off_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage' / 'ledger.jsonl'
            for n in range(2):
                self.assertTrue(usage_ledger.append({'n': n}, path))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual([json.loads(x)['n'] for x in path.read_text().splitlines()], [0, 1])
            with mock.patch.dict(os.environ, {'DEEPSEEK_TEAM_USAGE_LEDGER': 'off'}):
                self.assertIsNone(usage_ledger.ledger_path())
                self.assertFalse(usage_ledger.append({'n': 3}))

    def test_unwritable_ledger_is_silent(self):
        self.assertFalse(usage_ledger.append({'n': 1}, '/proc/forbidden/ledger.jsonl'))


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    body = ANTHROPIC_SSE
    content_type = 'text/event-stream'

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.send_response(200)
        self.send_header('Content-Type', self.content_type)
        self.end_headers()
        self.wfile.write(self.body)


class RelayLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), _Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)

    def post(self, socket_path, path):
        connection = UnixHTTPConnection(str(socket_path))
        connection.request('POST', path, b'{"model":"deepseek-flash[1m]","messages":[]}',
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return data

    def test_relay_streams_unchanged_and_records_usage_without_content(self):
        ledger = Path(self.tmp.name) / 'ledger.jsonl'
        socket_path = Path(self.tmp.name) / 'p.sock'
        upstream = ('http', '127.0.0.1', self.upstream.server_port)
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'sess-1'}):
            with relay.ProviderRelay(socket_path, 'k', upstream, usage_context={'task': 'T1'},
                                     usage_ledger_file=ledger):
                self.assertEqual(self.post(socket_path, '/anthropic/v1/messages'), ANTHROPIC_SSE)
                self.post(socket_path, '/anthropic/v1/messages/count_tokens')
        rows = [json.loads(x) for x in ledger.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['endpoint'], row['model'], row['input_miss'], row['input_hit'],
                          row['output'], row['status'], row['coordinator_session'], row['task']),
                         ('/anthropic/v1/messages', 'deepseek-flash', 120, 3000, 45, 200, 'sess-1', 'T1'))
        self.assertNotIn('secret', ledger.read_text())

    def test_fixture_upstream_without_ledger_file_writes_nothing(self):
        socket_path = Path(self.tmp.name) / 'p.sock'
        upstream = ('http', '127.0.0.1', self.upstream.server_port)
        with relay.ProviderRelay(socket_path, 'k', upstream) as provider:
            self.assertIsNone(provider.server.usage_sink)
            self.post(socket_path, '/anthropic/v1/messages')


if __name__ == '__main__':
    unittest.main()
