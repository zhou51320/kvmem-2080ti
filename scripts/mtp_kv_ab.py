#!/usr/bin/env python3
"""Compare MTP F16/Q8/Q5 using isolated, caller-configured workers.

Each trial owns a fresh worker and sends the same caller-provided cold request.
"""
import argparse
import csv
import ctypes
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import statistics
import subprocess
import threading
import time
import urllib.request

import gpu_env

ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Memory(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint), ('total', ctypes.c_ulonglong),
                ('reserved', ctypes.c_ulonglong), ('free', ctypes.c_ulonglong), ('used', ctypes.c_ulonglong)]


class Sampler(threading.Thread):
    def __init__(self, nvml, device, folder):
        super().__init__(daemon=True)
        self.nvml, self.device, self.folder = nvml, device, folder
        self.phase = 'loading'
        self.stop_event = threading.Event()
        self.rows = []
        self.errors = []

    def run(self):
        start = time.monotonic()
        offset = 0
        with (self.folder / 'vram.csv').open('w') as fh:
            writer = csv.writer(fh)
            writer.writerow(['elapsed_s', 'phase', 'used_mib', 'free_mib', 'reserved_mib',
                             'temperature_c', 'sm_clock_mhz', 'power_w'])
            while not self.stop_event.is_set():
                if self.phase == 'prefill':
                    with (self.folder / 'server.stderr.log').open() as log:
                        log.seek(offset)
                        tail = log.read()
                        offset = log.tell()
                    if 'KVMEM_CHAT_PREFILL ' in tail:
                        self.phase = 'decode'
                m = Memory()
                m.version = ctypes.sizeof(Memory) | (2 << 24)
                status = self.nvml.nvmlDeviceGetMemoryInfo_v2(self.device, ctypes.byref(m))
                if status:
                    self.errors.append(status)
                else:
                    extra = []
                    for fn, args, scale in (
                        (self.nvml.nvmlDeviceGetTemperature, [self.device, 0], 1),
                        (self.nvml.nvmlDeviceGetClockInfo, [self.device, 1], 1),
                        (self.nvml.nvmlDeviceGetPowerUsage, [self.device], 1000),
                    ):
                        value = ctypes.c_uint()
                        extra.append(value.value / scale if fn(*args, ctypes.byref(value)) == 0 else None)
                    row = [time.monotonic() - start, self.phase, m.used / 2**20,
                           m.free / 2**20, m.reserved / 2**20, *extra]
                    self.rows.append(row)
                    writer.writerow(row)
                    fh.flush()
                self.stop_event.wait(0.05)

    def finish(self):
        self.stop_event.set()
        self.join()
        phases = {}
        for phase in sorted({r[1] for r in self.rows}):
            rows = [r for r in self.rows if r[1] == phase]
            phases[phase] = {'peak_mib': max(r[2] for r in rows), 'last_mib': rows[-1][2],
                             'min_free_mib': min(r[3] for r in rows), 'samples': len(rows)}
            for i, name in ((5, 'temperature_c'), (6, 'sm_clock_mhz'), (7, 'power_w')):
                values = [r[i] for r in rows if r[i] is not None]
                if values:
                    phases[phase][name + '_mean'] = statistics.fmean(values)
        return {'peak_vram_mib': max(r[2] for r in self.rows), 'phase_metrics': phases,
                'sample_count': len(self.rows), 'sampling_errors': self.errors,
                'max_sample_gap_ms': max((b[0] - a[0] for a, b in zip(self.rows, self.rows[1:])), default=0) * 1000}


def stop_server(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def trial(folder, dtype, argv, request_bytes, env, nvml, device, url, expected_prompt=None):
    folder.mkdir()
    result = {'mtp_kv_type': dtype, 'argv': argv, 'request_sha256': hashlib.sha256(request_bytes).hexdigest()}
    sampler = Sampler(nvml, device, folder)
    proc = None
    sampler.start()
    try:
        with (folder / 'server.stdout.log').open('w') as out, (folder / 'server.stderr.log').open('w') as err:
            proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        result['pid'] = proc.pid
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            assert proc.poll() is None, 'server exited during startup'
            try:
                with OPENER.open(url + '/health', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError('server health timeout')
        startup = (folder / 'server.stderr.log').read_text()
        pool = next(s for s in startup.splitlines() if 'KVMEM_TRACE mtp_pool' in s)
        assert f'type_k={dtype} type_v={dtype}' in pool, pool
        target = next(s for s in startup.splitlines() if 'slot-pool cells=' in s)
        result['mtp_pool_log'] = pool
        result['target_pool_log'] = target
        sampler.phase = 'idle_ready'
        time.sleep(1)
        sampler.phase = 'prefill'
        print('START', folder.name, 'pid', proc.pid, flush=True)
        request = urllib.request.Request(url + '/v1/chat/completions', data=request_bytes,
                                         headers={'Content-Type': 'application/json'})
        start = time.monotonic()
        first = None
        content, reasoning = [], []
        usage, finish, done = None, None, False
        with OPENER.open(request, timeout=1800) as response, (folder / 'response.sse').open('w') as out:
            for line in response:
                line = line.decode()
                out.write(line)
                out.flush()
                if not line.startswith('data: '):
                    continue
                payload = line[6:].strip()
                if payload == '[DONE]':
                    done = True
                    break
                chunk = json.loads(payload)
                assert 'error' not in chunk, chunk
                if chunk.get('usage'):
                    usage = chunk['usage']
                for choice in chunk.get('choices', []):
                    delta = choice.get('delta', {})
                    c, r = delta.get('content') or '', delta.get('reasoning_content') or ''
                    if (c or r) and first is None:
                        first = time.monotonic()
                        print('FIRST_TOKEN', folder.name, 'ttft_s', round(first - start, 3), flush=True)
                    content.append(c)
                    reasoning.append(r)
                    finish = choice.get('finish_reason') or finish
        result.update(client_wall_s=time.monotonic() - start, ttft_s=None if first is None else first - start,
                      usage=usage, finish_reason=finish, done=done)
        sampler.phase = 'idle_after'
        time.sleep(1)
        code, thought = ''.join(content), ''.join(reasoning)
        (folder / 'generated.cpp').write_text(code)
        (folder / 'reasoning.txt').write_text(thought)
        result['code_sha256'] = hashlib.sha256(code.encode()).hexdigest()
        result['reasoning_sha256'] = hashlib.sha256(thought.encode()).hexdigest()
        log = (folder / 'server.stderr.log').read_text()
        pf = re.findall(r'KVMEM_CHAT_PREFILL ms=([\d.]+) n_prompt=(\d+)', log)[-1]
        gen = re.findall(r'KVMEM_GEN_WALL n=(\d+) ms=([\d.]+) toks=([\d.]+)', log)[-1]
        sp = re.findall(r'spec_stats n_gen=(\d+) n_drafted=(\d+) n_accept=(\d+) n_restore=(\d+) accept_pct=([\d.]+)', log)[-1]
        result.update(prompt_tokens=int(pf[1]), prefill_wall_ms=float(pf[0]), prefill_tps=int(pf[1]) * 1000 / float(pf[0]),
                      generated_tokens=int(gen[0]), decode_wall_ms=float(gen[1]), decode_tps=float(gen[2]),
                      drafted=int(sp[1]), accepted=int(sp[2]), accept_pct=float(sp[4]))
        result['mtp_follow'] = [s for s in log.splitlines() if 'KVMEM_TRACE mtp_follow' in s]
        assert done and usage and usage.get('prompt_cache_hit_tokens') == 0, 'incomplete response or cache hit'
        if expected_prompt is not None:
            assert int(pf[1]) == expected_prompt, 'input token count changed'
        assert all('n_no_raw=0' in s for s in result['mtp_follow']), 'MTP cold-block restore missed data'
    except Exception as exc:
        result['error'] = repr(exc)
        raise
    finally:
        try:
            result.update(sampler.finish())
        finally:
            if proc is not None:
                stop_server(proc)
            (folder / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print('RESULT', folder.name, json.dumps({k: result[k] for k in (
        'prefill_tps', 'decode_tps', 'generated_tokens', 'accept_pct', 'peak_vram_mib')}), flush=True)
    return result


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def worker_command(args, dtype, port):
    extra = json.loads(args.server_args)
    if not isinstance(extra, list) or not all(isinstance(v, str) for v in extra):
        raise ValueError('--server-args must be a JSON array of strings')
    controlled = {'-m', '--model', '--host', '--port', '--spec-kv-dtype'}
    if any(v.split('=', 1)[0] in controlled for v in extra):
        raise ValueError('--server-args overrides a controlled worker argument')
    return [str(args.server), '-m', str(args.model), '--host', '127.0.0.1', '--port', str(port),
            '-c', str(args.context), '-b', str(args.batch), '-ngl', '99', '--kv-dtype', args.kv_dtype,
            '--spec-type', 'draft-mtp', '--spec-draft-n-max', str(args.drafts),
            '--spec-kv-dtype', dtype, '--kvmem', '--kvmem-budget', str(args.budget),
            '--kvmem-gen-reserve', str(args.reserve), '--verbosity', '4', *extra]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--rounds', type=int, default=2)
    ap.add_argument('--request', type=Path, required=True)
    ap.add_argument('--server', type=Path, required=True)
    ap.add_argument('--model', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--context', type=int, default=262144)
    ap.add_argument('--batch', type=int, default=512)
    ap.add_argument('--budget', type=int, default=32000)
    ap.add_argument('--reserve', type=int, default=12000)
    ap.add_argument('--drafts', type=int, default=2)
    ap.add_argument('--kv-dtype', default='q5_0')
    ap.add_argument('--expected-prompt-tokens', type=int)
    ap.add_argument('--server-args', default='[]', help='Additional worker arguments as a JSON array')
    gpu_env.add_gpu_argument(ap)
    args = ap.parse_args()
    if args.rounds < 1:
        ap.error('--rounds must be positive')
    for name in ('server', 'model', 'request'):
        path = getattr(args, name).resolve()
        if not path.is_file():
            ap.error(f'missing {name}: {path}')
        setattr(args, name, path)
    worker_command(args, 'f16', 1)  # Validate arguments before discovery or starting workers.
    env = gpu_env.apply_gpu(os.environ.copy(), args.gpu)
    env['KVMEM_TRACE'] = '1'
    env.pop('KVMEM_PERF', None)
    request_bytes = args.request.read_bytes()
    args.output.mkdir(parents=True, exist_ok=False)
    nvml = ctypes.CDLL('nvml.dll' if os.name == 'nt' else 'libnvidia-ml.so.1')
    if nvml.nvmlInit_v2() != 0:
        raise RuntimeError('NVML initialization failed')
    summary = {'gpu_uuid': env['KVMEM_GPU_UUID'], 'gpu_name': env['KVMEM_GPU_NAME'],
               'rounds': args.rounds, 'server_sha256': hashlib.sha256(args.server.read_bytes()).hexdigest(),
               'trials': []}
    try:
        device = ctypes.c_void_p()
        if nvml.nvmlDeviceGetHandleByUUID(env['KVMEM_GPU_UUID'].encode(), ctypes.byref(device)) != 0:
            raise RuntimeError('Selected GPU is unavailable to NVML')
        order = [t for r in range(args.rounds) for t in
                 (('f16', 'q8_0', 'q5_0') if r % 2 == 0 else ('q5_0', 'q8_0', 'f16'))]
        for index, dtype in enumerate(order, 1):
            port = free_port()
            argv = worker_command(args, dtype, port)
            row = trial(args.output / f'{index:02d}_{dtype}', dtype, argv, request_bytes,
                        env, nvml, device, f'http://127.0.0.1:{port}', args.expected_prompt_tokens)
            summary['trials'].append(row)
            (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    finally:
        nvml.nvmlShutdown()
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
