"""Public export invariants: identities change, measurements and hashes do not."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import public_evidence as public

# Assemble deliberately non-synthetic identities without placing one in source.
DEVICE = 'GPU-' + '-'.join(('12345678', '1234', '1234', '1234', '123456789abc'))
OTHER = DEVICE[:-1] + 'd'
LINUX = '/home/' + 'private-person' + '/kvmem_llamacpp/build/bin/worker'
WINDOWS = 'C:' + '\\Users\\' + 'private-person' + '\\AppData\\Local\\KVMem\\trial\\worker.exe'


class PublicEvidenceTest(unittest.TestCase):
    def payload(self):
        return {'device': DEVICE, 'peer': OTHER, 'repeat': DEVICE,
                'command': [WINDOWS, LINUX], 'model': 'Example-27B',
                'binary_sha256': 'abc123', 'tokens': 32, 'ms': 901.069,
                'nested': {'accepted': [8, 7, 6], 'pass': True}}

    def test_export_preserves_metrics_hashes_and_identity_relationships(self):
        original = self.payload()
        raw = json.dumps(original).encode()
        result = public.public_json(original, raw)
        self.assertEqual(result['device'], result['repeat'])
        self.assertNotEqual(result['device'], result['peer'])
        for key in ('model', 'binary_sha256', 'tokens', 'ms', 'nested'):
            self.assertEqual(result[key], original[key])
        self.assertEqual(original['device'], DEVICE)
        self.assertEqual(result['_sanitization']['original_sha256'], hashlib.sha256(raw).hexdigest())
        encoded = json.dumps(result)
        self.assertNotIn('private-person', encoded)
        self.assertNotIn(DEVICE, encoded)
        self.assertEqual(public.violations(encoded), [])

    def test_report_writer_keeps_exact_original_in_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            public.write_report(base/'public.json', self.payload(), base/'private')
            result = json.loads((base/'public.json').read_text(encoding='utf-8'))
            backup = next((base/'private').glob('*.json')).read_bytes()
            self.assertEqual(json.loads(backup), self.payload())
            self.assertEqual(hashlib.sha256(backup).hexdigest(), result['_sanitization']['original_sha256'])

    def test_guard_reports_categories_without_repeating_private_values(self):
        credential = 'ghp_' + 'a' * 36
        findings = public.violations('\n'.join((DEVICE, WINDOWS, credential)))
        self.assertEqual([kind for _, kind in findings],
                         ['physical GPU UUID', 'private home/workspace path', 'credential-like value'])
        self.assertNotIn(credential, repr(findings))
        self.assertEqual(public.violations('-' + LINUX, patch=True), [])
        self.assertEqual(public.violations('+' + LINUX, patch=True)[0][1], 'private home/workspace path')

    def test_real_git_inventory_ignores_private_evidence_and_checks_public_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '--quiet', str(root)], check=True, capture_output=True)
            (root/'.gitignore').write_text('/private-evidence/\n', encoding='utf-8')
            (root/'private-evidence').mkdir()
            (root/'private-evidence/original.txt').write_text(DEVICE + '\n' + WINDOWS, encoding='utf-8')
            (root/'public.txt').write_text(DEVICE + '\n' + WINDOWS, encoding='utf-8')
            findings = public.check_tree(root)
            self.assertEqual(len(findings), 2)
            self.assertTrue(all(item.startswith('public.txt:') for item in findings))
            (root/'public.txt').write_text(public.Sanitizer().text(DEVICE + '\n' + WINDOWS), encoding='utf-8')
            self.assertEqual(public.check_tree(root), [])

    def test_credential_blocks_export_but_private_evidence_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with self.assertRaises(ValueError):
                public.write_report(base/'public.json', {'description': 'ghp_' + 'a' * 36}, base/'private')
            self.assertFalse((base/'public.json').exists())
            self.assertEqual(len(list((base/'private').glob('*.json'))), 1)

    def test_credential_fields_and_argument_vectors_are_redacted(self):
        raw = {'api_key': 'example-value', 'argv': ['worker', '--api-key', 'example-value',
               '--api-key-file=private-file', '--port', '12345'], 'tokens': 32}
        result = public.Sanitizer().value(raw)
        self.assertEqual(result['api_key'], '<redacted>')
        self.assertNotIn('example-value', json.dumps(result))
        self.assertNotIn('private-file', json.dumps(result))
        self.assertEqual(result['argv'][-2:], ['--port', '12345'])
        self.assertEqual(result['tokens'], 32)

    def test_generic_examples_and_public_credit_are_not_private_machine_data(self):
        content = 'GPU-00000000-0000-0000-0000-000000000001 /home/user/example C:/models/model.gguf\n'
        content += 'Author <contributor@example.org> C:/Program Files/Compiler'
        self.assertEqual(public.violations(content), [])
        self.assertEqual(public.Sanitizer().text(content), content)

    def test_private_home_with_spaces_is_fully_anonymized(self):
        source = 'C:' + '\\Users\\' + 'Private Person' + '\\work\\model.gguf'
        rendered = public.Sanitizer().text(source)
        self.assertNotIn('Private', rendered)
        self.assertNotIn('Person', rendered)
        self.assertIn('model.gguf', rendered)
        self.assertEqual(public.violations(rendered), [])

    def test_cli_preserves_input_and_refuses_in_place_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory)/'raw.json', Path(directory)/'public.json'
            raw = json.dumps(self.payload()).encode()
            source.write_bytes(raw)
            command = [sys.executable, '-B', str(ROOT/'scripts/public_evidence.py'), str(source), '--output']
            result = subprocess.run(command + [str(output)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(public.violations(output.read_text(encoding='utf-8')), [])
            refused = subprocess.run(command + [str(source)], capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(source.read_bytes(), raw)

    def test_http_canary_exports_without_a_local_gpu_dependency(self):
        response = {'usage': {'prompt_tokens': 5, 'completion_tokens': 2},
                    'timings': {'prompt_n': 5, 'predicted_n': 2, 'prompt_ms': 1,
                                'predicted_ms': 2, 'predicted_per_second': 500}}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def send_json(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                self.send_json({'model_alias': 'example', 'gpu_uuid': DEVICE, 'path': WINDOWS})
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_json(response)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)/'report.json'
                env = dict(os.environ, CUDA_VISIBLE_DEVICES='-1', KVMEM_GPU='unavailable', KVMEM_API_KEY='')
                result = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/speed_canary.py'),
                    '--url', f'http://127.0.0.1:{server.server_port}', '--runs', '1', '--warmup', '0',
                    '--prompt-repeats', '1', '-n', '2', '--output', str(output)],
                    env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(report['runs'][0]['native'], response)
                self.assertEqual(report['runs'][0]['gen_n'], 2)
                self.assertEqual(public.violations(output.read_text(encoding='utf-8')), [])
                digest = report['_sanitization']['original_sha256']
                original = (ROOT/'private-evidence'/f'{digest}.json').read_bytes()
                self.assertEqual(hashlib.sha256(original).hexdigest(), digest)
                self.assertIn(DEVICE, original.decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
