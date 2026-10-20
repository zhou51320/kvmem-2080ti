#!/usr/bin/env python3
"""Compare CLI output with RAM and forced public SSD storage; no speed claim."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import gpu_env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    gpu_env.add_gpu_argument(parser)
    parser.add_argument('-m', '--model', type=Path, required=True)
    parser.add_argument('--cli', type=Path)
    parser.add_argument('--tokens', type=int, default=8192, help='repeated input words')
    parser.add_argument('--host-mib', type=int, default=8, help='SSD run RAM budget; must fit the working window')
    parser.add_argument('--disk-mib', type=int, default=256)
    args = parser.parse_args()
    if args.tokens < 1024 or args.host_mib < 1 or args.disk_mib < 1:
        parser.error('use at least 1024 input words and positive budgets')
    gpu_env.configure(args.gpu)
    cli = args.cli or gpu_env.find_binary('llama-kvmem-cli')
    env = gpu_env.apply_gpu(os.environ.copy())
    results = []
    with tempfile.TemporaryDirectory(prefix='kvmem-public-ssd-') as directory:
        root = Path(directory)
        prompt = root / 'prompt.txt'
        prompt.write_text(' ping' * args.tokens + '\nContinue counting: 1, 2, 3,', encoding='utf-8')
        for ssd in (False, True):
            cmd = [str(cli), '-m', str(args.model), '-f', str(prompt), '-n', '32', '-ngl', '99',
                   '--tokens-only', '--kvmem', '--kvmem-method', 'retrieval',
                   '--kvmem-budget', '512', '--kvmem-gen-reserve', '256',
                   '--kvmem-block-tokens', '32', '--kvmem-host-mib', str(args.host_mib if ssd else 4096),
                   '-c', str(2 * args.tokens + 4096), '-b', '128', '--seed', '42']
            if ssd:
                cmd += ['--kvmem-disk-path', str(root / 'cache'), '--kvmem-disk-mib', str(args.disk_mib)]
            process = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                     encoding='utf-8', errors='replace', timeout=600)
            if process.returncode:
                raise RuntimeError(f'CLI failed ({process.returncode}): {process.stderr[-6000:]}')
            gpu_env.require_device(process.stderr, env)
            match = re.search(r'^KVMEM_STORAGE (.+)$', process.stderr, re.MULTILINE)
            if not match:
                raise RuntimeError('CLI did not report public storage accounting')
            storage = {key: int(value) for key, value in re.findall(r'(\w+)=(\d+)', match[1])}
            if storage['errors'] or storage['host_bytes'] > storage['host_budget'] or storage['disk_bytes'] > storage['disk_budget']:
                raise AssertionError('storage error or budget violation')
            results.append({'mode': 'ssd' if ssd else 'ram', 'output': process.stdout, 'storage': storage})
        if results[0]['output'] != results[1]['output'] or not results[1]['output'].strip():
            raise AssertionError('RAM/SSD generated token output differs or is empty')
        if not results[1]['storage']['read_bytes'] or not results[1]['storage']['written_bytes']:
            raise AssertionError('fixture did not exercise SSD reads and writes; increase input or reduce H')
        if list((root / 'cache').iterdir()):
            raise AssertionError('public SSD file survived CLI exit')
    print(json.dumps({'pass': True, 'output_equal': True, 'runs': results}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
