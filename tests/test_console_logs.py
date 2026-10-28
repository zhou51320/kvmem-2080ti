"""Model-free tests of the bounded shared stdout/stderr log endpoint."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('log_console', Path(__file__).resolve().parents[1] / 'scripts/local-console.py')
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control = console.Controller(console.ROOT, self.tmp.name, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_file_and_legacy_tail(self):
        snapshot = self.control.log_snapshot()
        self.assertFalse(snapshot['available'])
        self.assertEqual(snapshot['text'], '')
        self.assertEqual(self.control.log_tail(), '')
        self.assertNotIn('backend', snapshot)

    def test_all_backends_redaction_and_unchanged_snapshot(self):
        for backend, text in (
            ('llamacpp', 'slot print_timing: eval time = 64 ms\n'),
            ('ninfer', '[2026-10-24 15:00:00.012] [info] ninfer-serve: ready\n'),
            ('polystrata', '[strata] thinking: 128 tokens, 29.7 tok/s\r\n'),
        ):
            with self.subTest(backend=backend):
                self.control.log_metadata = {'backend': backend, 'model': 'model', 'started_at': 123}
                self.control.redactions.add('secret-key')
                self.control.log_path.write_text(text + 'error: secret-key\n', encoding='utf-8')
                result = self.control.log_snapshot()
                self.assertEqual(result['backend'], backend)
                self.assertNotIn('secret-key', result['text'])
                self.assertIn('<redacted>', result['text'])
                self.assertIsNone(self.control.log_snapshot(result['revision'])['text'])
                self.assertNotIn('secret-key', self.control.log_tail())

    def test_append_partial_utf8_and_key_are_not_published(self):
        self.control.process = MagicMock()
        self.control.process.poll.return_value = None
        self.control.redactions.add('private-api-key')
        self.control.log_path.write_bytes(b'INFO ready\nerror: private-api-')
        first = self.control.log_snapshot()
        self.assertEqual(first['text'], 'INFO ready\n')
        self.assertTrue(first['pending'])
        unchanged = self.control.log_snapshot(first['revision'])
        self.assertIsNone(unchanged['text'])
        self.assertTrue(unchanged['pending'])
        with self.control.log_path.open('ab') as stream:
            stream.write(b'key\n' + '中文'.encode('utf-8')[:2])
        second = self.control.log_snapshot(first['revision'])
        self.assertEqual(second['text'], 'INFO ready\nerror: <redacted>\n')
        with self.control.log_path.open('ab') as stream:
            stream.write('中文'.encode('utf-8')[2:] + b'\n')
        third = self.control.log_snapshot(second['revision'])
        self.assertIn('中文\n', third['text'])
        self.assertNotIn('\ufffd', third['text'])

    def test_stop_keeps_output_and_origin_and_flushes_last_line(self):
        self.control.log_metadata = {'backend': 'ninfer', 'model': 'original-model'}
        self.control.log_path.write_bytes(b'INFO ready\nERROR: final failure')
        self.control.process = MagicMock()
        self.control.process.poll.return_value = None
        live = self.control.log_snapshot()
        self.control.process.poll.return_value = 7
        exited = self.control.log_snapshot(live['revision'])
        self.assertIn('final failure', exited['text'])
        self.assertFalse(exited['running'])
        self.assertEqual(exited['exit_code'], 7)
        self.control.process = None
        self.control.stop()
        stopped = self.control.log_snapshot()
        self.assertEqual(stopped['model'], 'original-model')
        self.assertEqual(stopped['session'], live['session'])
        self.assertEqual(stopped['text'], exited['text'])

    def test_large_file_truncation_rotation_and_carriage_progress(self):
        self.control.log_path.write_bytes(('很长的历史行\n' * 40000 + 'progress 10%\rprogress 20%\r\n').encode('utf-8'))
        first = self.control.log_snapshot()
        self.assertTrue(first['truncated'])
        self.assertLessEqual(len(first['text'].encode()), 256 * 1024)
        self.assertNotIn('\ufffd', first['text'])
        self.assertTrue(first['text'].endswith('progress 20%\r\n'))
        self.control.log_path.write_bytes(b'new run\n')
        second = self.control.log_snapshot(first['revision'])
        self.assertEqual(second['text'], 'new run\n')
        self.assertFalse(second['truncated'])
        self.control.log_path.write_bytes(b'x' * 300000)
        oversized = self.control.log_snapshot()
        self.assertTrue(oversized['truncated'])
        self.assertEqual(oversized['text'], '')

    def test_http_contract_assets_and_same_origin_gate(self):
        self.control.log_path.write_bytes(b'INFO real file\n')
        server = console.Server(('127.0.0.1', 0), console.Handler)
        server.control = self.control
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with urllib.request.urlopen(base + '/api/launcher/logs') as response:
                body = json.load(response)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(body['text'], 'INFO real file\n')
            for asset in ('logs.js', 'logs.css'):
                with urllib.request.urlopen(base + '/monitor/' + asset) as response:
                    self.assertEqual(response.status, 200)
            with urllib.request.urlopen(base + '/api/launcher/logs?revision=' + body['revision']) as response:
                self.assertIsNone(json.load(response)['text'])
            request = urllib.request.Request(base + '/api/launcher/logs', headers={'Origin':'https://example.invalid'})
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(request)
            self.assertEqual(rejected.exception.code, 403)
            rejected.exception.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    @unittest.skipUnless(os.name == 'nt', 'Windows owned-process launcher')
    def test_owned_child_stdout_and_stderr_for_each_backend(self):
        # Exercise real process redirection, without loading a GPU model.
        for backend in ('ninfer', 'llamacpp', 'polystrata'):
            with self.subTest(backend=backend):
                argv = [console.sys.executable, '-u', '-c',
                        "import sys; print('INFO stdout ready'); print('ERROR stderr exit', file=sys.stderr)"]
                with console.socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    port = probe.getsockname()[1]
                # Current master inspects model assets before launch. This fixture
                # launches Python solely to test the real stdout/stderr capture.
                with patch.object(self.control, 'inspect_model', return_value={'can_start': True}), \
                     patch.object(self.control, 'plan', return_value={'argv': argv}):
                    self.control.start({'schema_version': 1, 'parameters': {'Backend': backend, 'Model': 'capture-fixture', 'Port': port}})
                try:
                    deadline = time.monotonic() + 10
                    while self.control.process and self.control.process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    result = self.control.log_snapshot()
                    self.assertFalse(result['running'])
                    self.assertEqual(result['backend'], backend)
                    self.assertIn('INFO stdout ready', result['text'])
                    self.assertIn('ERROR stderr exit', result['text'])
                finally:
                    self.control.stop()


if __name__ == '__main__':
    unittest.main()
