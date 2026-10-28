"""Real HTTP CPU Vision lazy-load/idle-release/reload check; requires an explicit worker and model.

Use a small text-layer fixture with the original Vision tower on an 8 GB GPU. This verifies
resource lifetime and protocol operation, not the semantic quality of a sliced text model.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import os
from pathlib import Path
import signal
import shutil
import urllib.error
import socket
import struct
import subprocess
import time
import urllib.request
import zlib


def png(rgb):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    raw = (b'\0' + bytes(rgb) * 64) * 64
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 64, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def process_memory(pid):
    if os.name != 'nt':
        return {}  # Residency counters remain portable; Windows adds process physical/private bytes.
    from ctypes import wintypes as w
    class Counters(ctypes.Structure):
        _fields_ = [('cb', w.DWORD), ('faults', w.DWORD)] + [
            (n, ctypes.c_size_t) for n in ('peak_ws', 'ws', 'peak_paged', 'paged', 'peak_nonpaged',
                                           'nonpaged', 'pagefile', 'peak_pagefile', 'private')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(Counters), w.DWORD]
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        return {}
    try:
        value = Counters()
        value.cb = ctypes.sizeof(value)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(value), value.cb):
            return {}
        return {'working_set_bytes': value.ws, 'private_bytes': value.private}
    finally:
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', required=True, type=Path)
    parser.add_argument('--model', required=True, type=Path)
    parser.add_argument('--gpu', required=True, help='GPU UUID, not an assumed numeric index')
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--idle-seconds', type=int, default=2)
    parser.add_argument('--fault-injection', action='store_true', help='Copy a single-file fixture and temporarily hide that copy to test load-failure recovery')
    args = parser.parse_args()
    assert args.idle_seconds >= 1
    args.out.mkdir(parents=True, exist_ok=False)
    model = args.model.resolve()
    if args.fault_injection:
        model = args.out.resolve() / 'runtime-model.ninfer'
        shutil.copyfile(args.model, model)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(route, body=None):
        request = urllib.request.Request(base + route, None if body is None else json.dumps(body).encode(),
                                         {'Content-Type': 'application/json'})
        with opener.open(request, timeout=240) as response:
            return json.load(response)
    command = [str(args.exe.resolve()), str(model), '--device', '0',
        '--host', '127.0.0.1', '--port', str(port), '--model-id', 'cpu-vision-lifecycle',
        '--max-context', '4096', '--prefill-chunk', '128', '--max-concurrency', '2',
        '--kv-dtype', 'int8', '--device-profile', 'off', '--ngram-draft-tokens', '0',
        '--vision-cpu', '--vision-max-merged', '64', '--cpu-vision-idle-seconds', str(args.idle_seconds),
        '--kvmem', '--kvmem-budget', '1024', '--kvmem-gen-reserve', '512', '--kvmem-host-mib', '512',
        '--kvmem-sessions', '2', '--kvmem-sink-tokens', '64', '--kvmem-recent-tokens', '64',
        '--cuda-graph-allowance-mib', '64', '--no-thinking', '--no-webui']
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=args.gpu, PYTHONUTF8='1')
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    result = {'gpu_uuid': args.gpu, 'command': command, 'events': [], 'complete': False}
    process = None
    with (args.out / 'server.log').open('w', encoding='utf-8') as log:
        try:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=flags)
            deadline = time.monotonic() + 180
            while True:
                assert process.poll() is None, 'worker exited at startup; inspect server.log'
                try:
                    call('/health')
                    break
                except OSError:
                    assert time.monotonic() < deadline, 'worker readiness timed out'
                    time.sleep(0.2)
            def snapshot(label):
                state = call('/props')['cpu_vision']
                result['events'].append({'phase': label, 'time': time.time(), 'cpu_vision': state,
                                         **process_memory(process.pid)})
                print(label, json.dumps(state), flush=True)
                return state
            def request(label, color=None, second_color=None):
                # A fresh first text part prevents image-prefix reuse from bypassing encoding.
                content = [{'type': 'text', 'text': f'Fixture phase {label}. '}]
                for image_color in (color, second_color):
                    if image_color is not None:
                        content.append({'type': 'image_url', 'image_url': {
                            'url': 'data:image/png;base64,' + base64.b64encode(png(image_color)).decode()}})
                content.append({'type': 'text', 'text': 'Reply with one word.'})
                started = time.monotonic()
                try:
                    response = call('/v1/chat/completions', {'model': 'cpu-vision-lifecycle', 'stream': False,
                        'messages': [{'role': 'user', 'content': content}], 'temperature': 0, 'seed': 42,
                        'max_tokens': 4})
                except urllib.error.HTTPError as error:
                    error.response_body = error.read()
                    (args.out / (label + '.error.json')).write_bytes(error.response_body)
                    error.add_note(error.response_body.decode('utf-8', errors='replace'))
                    error.close()
                    raise
                assert response.get('choices'), response
                (args.out / (label + '.response.json')).write_text(json.dumps(response, indent=2), encoding='utf-8')
                result.setdefault('requests', []).append({'label': label, 'seconds': time.monotonic() - started})
            result['execution'] = call('/props')['execution']
            devices = result['execution']['monitor_devices']
            assert [d['uuid'] for d in devices] == [args.gpu], devices
            state = snapshot('startup')
            assert state['weight_bytes'] == state['load_count'] == 0
            request('text-only')
            assert snapshot('after-text')['load_count'] == 0
            request('image-first', (220, 30, 30))
            state = snapshot('first-image-loaded')
            assert state['weight_bytes'] > 0 and state['load_count'] == 1
            first_bytes = state['weight_bytes']
            request('image-hot', (30, 30, 220))
            assert snapshot('hot-reuse')['load_count'] == 1
            request('image-pair', (180, 60, 20), (20, 60, 180))
            assert snapshot('multi-image-reuse')['load_count'] == 1
            # No requests or props polling until the timer has elapsed: the reaper must work alone.
            time.sleep(args.idle_seconds + 1)
            state = snapshot('idle-unloaded')
            assert state['weight_bytes'] == 0 and state['unload_count'] == 1
            request('text-after-idle')
            assert snapshot('text-stays-unloaded')['load_count'] == 1
            request('image-reload', (30, 220, 30))
            state = snapshot('reloaded')
            assert state['weight_bytes'] == first_bytes and state['load_count'] == 2
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda item: request(*item), [
                    ('image-concurrent-a', (210, 110, 20)), ('image-concurrent-b', (20, 110, 210))]))
            assert snapshot('concurrent-reuse')['load_count'] == 2
            time.sleep(args.idle_seconds + 1)
            state = snapshot('second-idle-unloaded')
            assert state['weight_bytes'] == 0 and state['unload_count'] == 2
            if args.fault_injection:
                offline = model.with_suffix('.offline')
                model.rename(offline)
                try:
                    try:
                        request('image-missing-model', (210, 210, 20))
                        raise AssertionError('missing CPU Vision source was silently accepted')
                    except urllib.error.HTTPError as error:
                        assert error.code == 503, error.code
                        result['expected_load_failure'] = json.loads(error.response_body)
                        error.close()
                    call('/health')
                    state = snapshot('failed-load-unpublished')
                    assert state['weight_bytes'] == 0 and state['load_count'] == 2
                finally:
                    offline.rename(model)
                request('text-after-load-failure')
                request('image-after-load-failure', (210, 20, 210))
                state = snapshot('load-recovered')
                assert state['weight_bytes'] == first_bytes and state['load_count'] == 3
            result['complete'] = True
        finally:
            if process is not None and process.poll() is None:
                try:
                    if os.name == 'nt':
                        process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        process.terminate()
                except OSError:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            result['worker_returncode'] = process.returncode if process else None
            (args.out / 'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
