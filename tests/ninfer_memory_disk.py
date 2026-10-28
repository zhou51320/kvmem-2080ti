#!/usr/bin/env python3
"""Run the process-local SSD lifecycle twice in separate processes sharing one directory."""
import argparse
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--drafts', type=int, default=0, choices=range(16))
    parser.add_argument('--kv', default='int8')
    parser.add_argument('--execution', choices=('eager', 'graph'), default='graph')
    parser.add_argument('--ngram', type=int, default=0)
    parser.add_argument('--policy', choices=('fixed', 'adaptive'), default='fixed')
    parser.add_argument('--timeout', type=float, default=600)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='memory-disk-', dir=args.output_dir.resolve()))
    spill = run_dir / 'spill'
    spill.mkdir()
    # The Engine owns its open spill file, not the directory or other users' files.
    marker = spill / 'unrelated.txt'
    marker.write_bytes(b'preserve this file\n')
    command = [str(args.binary.resolve()), str(args.model.resolve()), str(args.drafts),
               args.kv, str(spill), args.execution, str(args.ngram), args.policy]
    print(f'Evidence: {run_dir}', flush=True)
    for phase in ('first-process', 'restart'):
        log = run_dir / f'{phase}.log'
        with log.open('wb') as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=args.timeout, check=False)
        if result.returncode:
            raise RuntimeError(f'{phase} failed ({result.returncode}); see {log}')
        if 'MEMORY_DISK PASS process_local=1 cleanup=1' not in log.read_text(encoding='utf-8', errors='replace'):
            raise RuntimeError(f'{phase} did not complete the current lifecycle test; see {log}')
        if set(spill.iterdir()) != {marker} or marker.read_bytes() != b'preserve this file\n':
            raise RuntimeError(f'{phase} leaked spill files or changed an unrelated file')
        print(f'{phase}: PASS (cold start, live SSD, reuse, cancellation, clear, cleanup)', flush=True)
    print('MEMORY_DISK_RESTART PASS: both fresh processes started without retained history', flush=True)


if __name__ == '__main__':
    main()
