"""Device-selection and isolated-worker contracts; no GPU or model required."""
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import gpu_env
import mtp_kv_ab

A = 'GPU-00000000-0000-0000-0000-000000000001'
B = 'GPU-00000000-0000-0000-0000-000000000002'
DEVICES = [dict(index='0', uuid=A, name='Example GPU A', bus='00000000:02:00.0', memory_mib=8192),
           dict(index='1', uuid=B, name='Example GPU B', bus='00000000:01:00.0', memory_mib=16384)]


class DeviceSelectionTest(unittest.TestCase):
    def test_explicit_identity_and_ambiguous_defaults(self):
        choose = gpu_env.select_device
        self.assertEqual(choose(DEVICES, {}, '0', {})['uuid'], A)
        self.assertEqual(choose(DEVICES, {}, B, {})['index'], '1')
        self.assertEqual(choose(DEVICES[:1], {}, None, {})['uuid'], A)
        for selector in (None, 'auto', '9', 'GPU-00000000'):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                choose(DEVICES, {}, selector, {})

    def test_inherited_masks_are_not_silently_reinterpreted(self):
        choose = gpu_env.select_device
        self.assertEqual(choose(DEVICES, {'CUDA_VISIBLE_DEVICES': B}, None, {})['uuid'], B)
        # CUDA ordinals in PCI order differ from nvidia-smi indices here.
        env = {'CUDA_VISIBLE_DEVICES': '0', 'CUDA_DEVICE_ORDER': 'PCI_BUS_ID'}
        self.assertEqual(choose(DEVICES, env, None, {})['uuid'], B)
        self.assertEqual(choose(DEVICES, env, '0', {})['uuid'], A)
        for mask in ('', '-1', '0,1', '0'):
            with self.subTest(mask=mask), self.assertRaises(ValueError):
                choose(DEVICES, {'CUDA_VISIBLE_DEVICES': mask}, None, {})

    def test_local_profiles_and_environment_precedence(self):
        config = {'default_gpu': 'large', 'gpu_profiles': {'large': B, 'small': A}}
        choose = gpu_env.select_device
        self.assertEqual(choose(DEVICES, {}, None, config)['uuid'], B)
        self.assertEqual(choose(DEVICES, {'CUDA_VISIBLE_DEVICES': A}, None, config)['uuid'], A)
        self.assertEqual(choose(DEVICES, {}, 'small', config)['uuid'], A)
        with self.assertRaises(ValueError):
            choose(DEVICES, {}, 'loop', {'gpu_profiles': {'loop': 'again', 'again': 'loop'}})

    def test_explicit_override_stays_bound_for_nested_helpers(self):
        with patch.object(gpu_env, 'inventory', return_value=DEVICES), \
                patch.object(gpu_env, 'local_config', return_value={}):
            first = gpu_env.apply_gpu({'KVMEM_GPU': '1', 'PATH': 'inherited'}, '0')
            again = gpu_env.apply_gpu(first.copy())
        self.assertEqual(first['CUDA_VISIBLE_DEVICES'], A)
        self.assertEqual(again['CUDA_VISIBLE_DEVICES'], A)
        gpu_env.require_device('Device 0: Example GPU A; offloaded 4/4 layers to GPU', again)
        with self.assertRaises(SystemExit):
            gpu_env.require_device('Device 0: Example GPU B; offloaded 4/4 layers to GPU', again)

    def test_inventory_parses_device_identity(self):
        csv = f'7, {B}, Example GPU B, 00000000:01:00.0, 16384\n'
        with patch.object(gpu_env.shutil, 'which', return_value='nvidia-smi'), \
                patch.object(gpu_env.subprocess, 'run', return_value=argparse.Namespace(stdout=csv)) as run:
            device = gpu_env.inventory({'PATH': 'inherited'})[0]
        self.assertEqual((device['index'], device['uuid']), ('7', B))
        self.assertIn('--format=csv,noheader,nounits', run.call_args.args[0])

    def test_binary_discovery_accepts_windows_suffix_in_a_fresh_build_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'build/bin').mkdir(parents=True)
            executable = root/'build/bin/llama-kvmem-cli.exe'
            executable.touch()
            with patch.object(gpu_env, 'ROOT', root):
                self.assertEqual(gpu_env.find_binary('llama-kvmem-cli'), executable)

    def test_toolchain_paths_retain_the_callers_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'cuda/bin').mkdir(parents=True)
            (root/'cuda/lib64').mkdir()
            (root/'build/bin').mkdir(parents=True)
            original = {'PATH': 'existing', 'LD_LIBRARY_PATH': 'original-libraries',
                        'CUDA_HOME': str(root/'cuda'), 'KEEP_THIS': 'unchanged'}
            with patch.object(gpu_env, 'ROOT', root):
                updated = gpu_env.toolchain_env(original.copy())
                twice = gpu_env.toolchain_env(updated.copy())
            self.assertEqual(updated, twice)
            self.assertTrue(updated['PATH'].endswith('existing'))
            self.assertTrue(updated['LD_LIBRARY_PATH'].endswith('original-libraries'))
            self.assertEqual(updated['KEEP_THIS'], 'unchanged')
            self.assertEqual(original['PATH'], 'existing')

    def test_shell_values_round_trip_as_literals(self):
        value = "Example GPU's $(false); literal"
        output = io.StringIO()
        with patch.object(sys, 'argv', ['gpu_env.py', '--shell', '0']), \
                patch.object(gpu_env, 'apply_gpu', return_value={'KVMEM_GPU_NAME': value}), \
                redirect_stdout(output):
            gpu_env.main()
        self.assertEqual(shlex.split(output.getvalue()), ['export', 'KVMEM_GPU_NAME=' + value])


class IsolatedBenchmarkTest(unittest.TestCase):
    def args(self):
        return argparse.Namespace(server=Path('worker'), model=Path('model.gguf'), server_args='[]',
            context=2048, batch=128, kv_dtype='q8_0', drafts=3, budget=1024, reserve=256)

    def test_worker_uses_only_caller_paths_and_assigned_port(self):
        args = self.args()
        command = mtp_kv_ab.worker_command(args, 'f16', 12345)
        self.assertEqual(command[0], 'worker')
        self.assertEqual(command[command.index('--port') + 1], '12345')
        self.assertEqual(command[command.index('-m') + 1], 'model.gguf')
        self.assertEqual(command[command.index('--spec-kv-dtype') + 1], 'f16')
        for extra in ('{}', '[7]', '["--port", "1"]', '["--host=0.0.0.0"]'):
            args.server_args = extra
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                mtp_kv_ab.worker_command(args, 'f16', 12345)

    def test_owned_worker_is_stopped_even_if_failure_sampling_also_fails(self):
        from unittest.mock import MagicMock, Mock
        owned, sampler = Mock(), Mock()
        owned.pid = 1234
        owned.poll.return_value = None
        sampler.finish.side_effect = RuntimeError('sampling failed')
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(mtp_kv_ab.subprocess, 'Popen', return_value=owned), \
                patch.object(mtp_kv_ab, 'Sampler', return_value=sampler), \
                patch.object(mtp_kv_ab.OPENER, 'open', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'sampling failed'):
                # The empty startup log fails pool validation before any inference.
                mtp_kv_ab.trial(Path(directory)/'trial', 'f16', ['worker'], b'{}', {}, None, None,
                               'http://127.0.0.1:1')
        owned.terminate.assert_called_once_with()
        owned.wait.assert_called_once()
        owned.kill.assert_not_called()

    def test_all_migrated_commands_support_help_without_gpu_discovery(self):
        names = ('identity_canary', 'long_ctx_vram', 'mtp_canary', 'mtp_vram_canary',
                 'multi_needle', 'needle_recall', 'ssd_spill', 'prefill_speed',
                 'reselect_overlap', 'server_smoke', 'speed_canary', 'tools_60k_27b',
                 'chat_sampling_canary', 'mtp_kv_ab')
        for name in names:
            with self.subTest(name=name):
                result = subprocess.run([sys.executable, '-B', str(ROOT/'scripts'/f'{name}.py'), '--help'],
                    capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--gpu', result.stdout)


@unittest.skipUnless(os.name != 'nt' and shutil.which('bash'), 'Requires a POSIX Bash runtime')
class BashIntegrationTest(unittest.TestCase):
    def executable(self, path, content):
        path.write_text('#!/bin/sh\n' + content, encoding='utf-8')
        path.chmod(0o755)
        return str(path)

    def test_shell_selector_uses_discovery_without_evaluating_device_names(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            name = "Example GPU's $(false); literal"
            row = f'0, {A}, {name}, 00000000:01:00.0, 8192'
            self.executable(base/'nvidia-smi', 'printf "%s\\n" ' + shlex.quote(row) + '\n')
            env = dict(os.environ, PATH=str(base)+os.pathsep+os.environ.get('PATH', ''),
                       PYTHON=sys.executable, KVMEM_LOCAL_CONFIG=str(base/'absent.json'))
            code = 'source "$1" 0 && printf "%s\\n%s\\n" "$CUDA_VISIBLE_DEVICES" "$KVMEM_GPU_NAME"'
            result = subprocess.run(['bash', '-c', code, 'check', str(ROOT/'scripts/gpu.sh')],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [A, name])

    def test_cuda_builder_discovers_environment_and_defaults_to_eight_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'cuda/bin').mkdir(parents=True)
            nvcc = self.executable(base/'cuda/bin/nvcc', 'exit 0\n')
            fake_python = self.executable(base/'prepare-only', 'exit 0\n')
            cmake = self.executable(base/'cmake', 'printf "%s\\n" "$*" >> "$CALLS"\n')
            env = dict(os.environ, CUDA_HOME=str(base/'cuda'), CMAKE=cmake,
                       PYTHON=fake_python, CALLS=str(base/'calls'), BUILD_DIR=str(base/'build'))
            env.pop('CU13', None)
            env.pop('NPROC', None)
            result = subprocess.run(['bash', str(ROOT/'scripts/build-cuda.sh')],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (base/'calls').read_text()
            self.assertIn('-DCMAKE_CUDA_COMPILER=' + nvcc, calls)
            self.assertIn('--parallel 8', calls)


if __name__ == '__main__':
    unittest.main()
