"""HTTP regression for attention feedback on a restored assistant prefix.

Uses an existing local NInfer model and a private server. The argv JSON is an
array or {"argv": [...]}; enable KVMem and choose a modest window (e.g. 1024+1024),
prefill chunks of 256 and max-context >= 8192. No reference answer is required:
native restore, sampling and selection traces are the correctness oracle.
"""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--argv-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gpu', required=True, help='CUDA device UUID')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    command = json.loads(args.argv_json.read_text(encoding='utf-8-sig'))
    command = list(command['argv'] if isinstance(command, dict) else command)

    def option(name, value):
        if name in command:
            command[command.index(name) + 1] = str(value)
        else:
            command.extend([name, str(value)])

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    option('--host', '127.0.0.1')
    option('--port', port)
    option('--device', 0)
    for flag in ('--assistant-prefill', '--no-thinking', '--no-webui', '--kvmem-verify-transfers'):
        if flag not in command:
            command.append(flag)
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=args.gpu, CUDA_DEVICE_ORDER='PCI_BUS_ID')
    env.pop('KVMEM_ATTENTION_VERIFY', None)
    env['PATH'] = str(Path(command[0]).parent) + os.pathsep + env.get('PATH', '')
    report = {'gpu': args.gpu, 'argv': command, 'status': 'running', 'checks': [], 'requests': []}
    log_path = args.output / 'server.log'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def save():
        (args.output / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')

    def check(name, condition):
        report['checks'].append({'name': name, 'pass': bool(condition)})
        save()
        if not condition:
            raise AssertionError(name)

    def call(path, body=None, timeout=180):
        request = urllib.request.Request(f'http://127.0.0.1:{port}' + path,
            None if body is None else json.dumps(body).encode(), {'Content-Type': 'application/json'})
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode()) from error

    def ask(name, messages, reset=False):
        offset = log_path.stat().st_size
        body = {'messages': messages, 'max_tokens': 16, 'temperature': 0, 'seed': 42,
                'enable_thinking': False, 'cache_reset': reset,
                'prompt_cache_key': 'assistant-prefill-regression',
                'kvmem': {'conversation_id': 'assistant-prefill-regression'}}
        (args.output / f'{name}.request.json').write_text(json.dumps(body, indent=2), encoding='utf-8')
        started = time.monotonic()
        response = call('/v1/chat/completions', body)
        trace = log_path.read_bytes()[offset:].decode('utf-8', errors='replace')
        (args.output / f'{name}.response.json').write_text(json.dumps(response, indent=2), encoding='utf-8')
        (args.output / f'{name}.trace.log').write_text(trace, encoding='utf-8')
        report['requests'].append({'phase': name, 'seconds': time.monotonic() - started,
                                   'usage': response.get('usage'),
                                   'finish_reason': response['choices'][0]['finish_reason']})
        # EOS is a valid completion: this regression checks accepted prompt
        # statistics, not whether this model chooses to extend a supplied answer.
        check(name + ' completes native evaluation', response['usage']['completion_tokens'] > 0)
        check(name + ' no native errors', not re.search(r'FATAL|CUDA error|state_exact=0|prefix_match=0', trace))
        return response, trace

    save()
    with log_path.open('w', encoding='utf-8') as log:
        server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while True:
                if server.poll() is not None:
                    raise RuntimeError('server exited before readiness')
                try:
                    call('/health', timeout=2)
                    break
                except (OSError, urllib.error.URLError, RuntimeError):
                    if time.monotonic() > deadline:
                        raise TimeoutError('server readiness')
                    time.sleep(.2)
            user = {'role': 'user', 'content': 'Write a numbered list of 100 rivers, with a sentence about each. Start with the Nile and keep going.'}
            prefix = '1. The Nile flows through Egypt.\n2. The Amazon'
            base, _ = ask('base', [user, {'role': 'assistant', 'content': prefix}], reset=True)
            generated = base['choices'][0]['message']['content'] or ''
            extended = prefix + generated + '\n' + 'The river flows past the village and the fields.\n' * 220 + 'The next river is'
            resumed, trace = ask('extended', [user, {'role': 'assistant', 'content': extended}])
            restores = re.findall(r'KVMEM_HISTORY_RESTORE frontier=(\d+) kind=\d+ same_query=(\d+) prefix_match=1 state_exact=1', trace)
            check('restores an evaluated same-user prefix', len(restores) == 1 and restores[0][1] == '1')
            frontier = int(restores[0][0])
            check('restore includes previously evaluated assistant text', frontier >= base['usage']['prompt_tokens'] - 1)
            usage = resumed['usage']
            check('HTTP confirms prefix reuse', usage['prompt_tokens_details']['cached_tokens'] > 0)
            check('new assistant suffix requires prefill', usage['prompt_tokens'] > frontier + 1024)
            check('same Query uses append policy', 'KVMEM_PREFILL_PLAN path=append_pressure' in trace)
            check('same Query is not rescored or replayed', not re.search(r'KVMEM_QUERY |KVMEM_QUERY_REPLAY |KVMEM_RETRIEVAL ', trace))
            samples = [(int(b), int(e), int(n)) for b, e, n in re.findall(
                r'KVMEM_ATTENTION phase=prefill begin=(\d+) end=(\d+) samples=(\d+)', trace)]
            check('new assistant suffix records attention samples', bool(samples) and all(n > 0 for _, _, n in samples))
            check('no samples from restored prefix or unaccepted output', all(
                frontier <= b < e <= usage['prompt_tokens'] for b, e, _ in samples))
            check('sample batches do not overlap', all(samples[i-1][1] <= samples[i][0] for i in range(1, len(samples))))
            check('append crosses a real Pressure boundary', 'KVMEM_PRESSURE phase=prefill' in trace)
            report.update(status='passed', restored_frontier=frontier, prefill_samples=samples)
            call('/health', timeout=2)
            print(json.dumps({'status': 'passed', 'frontier': frontier, 'samples': samples}), flush=True)
        except Exception as error:
            report.update(status='failed', error=str(error))
            raise
        finally:
            save()
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == '__main__':
    main()
