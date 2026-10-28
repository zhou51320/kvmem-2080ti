"""Focused user-policy, rewrite, tool-continuation and session regression.

Uses an existing local model and a temporary loopback server; no downloads.
Set CUDA_VISIBLE_DEVICES to the selected GPU UUID before running.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prompt', type=Path, required=True)
    parser.add_argument('--explicit-user', action='store_true')
    parser.add_argument('--mtp', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    server = str(args.server.resolve())
    evidence = {'device': os.environ.get('CUDA_VISIBLE_DEVICES'), 'checks': [], 'requests': []}

    def check(name, condition):
        evidence['checks'].append({'name': name, 'pass': bool(condition)})
        (args.output / 'results.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        if not condition:
            raise AssertionError(name)
        print('PASS ' + name, flush=True)

    for value, message in [('legacy', 'has been removed'), ('invalid', 'only user is supported')]:
        result = subprocess.run([server, '--kvmem-query-policy', value], capture_output=True, timeout=30)
        check('reject policy ' + value, result.returncode != 0 and message.encode() in result.stderr)
    help_result = subprocess.run([server, '--kvmem-query-policy', 'user', '--help'], capture_output=True, timeout=30)
    check('accept explicit user compatibility option', help_result.returncode == 0)

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    command = [server, '-m', str(args.model.resolve()), '-c', '8192', '-b', '128', '-ub', '128',
               '-ngl', '99', '-n', '1024', '--host', '127.0.0.1', '--port', str(port), '--kvmem',
               '--kvmem-budget', '768', '--kvmem-gen-reserve', '128', '--kvmem-sink-tokens', '64',
               '--kvmem-recent-tokens', '64', '--kv-dtype', 'q8_0', '--no-think', '--no-webui']
    if args.explicit_user:
        command += ['--kvmem-query-policy', 'user']
    if args.mtp:
        command += ['--spec-type', 'draft-mtp', '--spec-draft-n-max', '4']
    evidence['command'] = command
    env = dict(os.environ, KVMEM_TRACE='1')
    env.pop('KVMEM_ATTENTION_VERIFY', None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, payload=None, timeout=180):
        req = urllib.request.Request('http://127.0.0.1:' + str(port) + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=timeout) as response:
            return json.load(response)

    with (args.output / 'server.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None:
                    raise RuntimeError('server exited before readiness')
                try:
                    request('/health', timeout=2)
                    break
                except (urllib.error.URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('server readiness')
                    time.sleep(.25)

            def ask(phase, messages, maximum=16, conversation='A', reset=False):
                response = request('/v1/chat/completions', {'messages': messages, 'temperature': 0,
                    'seed': 42, 'max_tokens': maximum, 'enable_thinking': False, 'cache_reset': reset,
                    'kvmem': {'conversation_id': conversation}})
                evidence['requests'].append({'phase': phase, 'response': response})
                check(phase + ' completes', bool(response.get('choices')) and response['usage']['completion_tokens'] > 0)
                return response

            messages = [{'role': 'system', 'content': 'Follow the latest user instruction.'},
                        {'role': 'user', 'content': args.prompt.read_text(encoding='utf-8')},
                        {'role': 'assistant', 'content': 'I have read the records.'},
                        {'role': 'user', 'content': 'List the integers from 1 to 2000 with a full sentence about rivers after each. Continue until entry 2000.'}]
            ask('long', messages, 128)
            rewrite = messages[:-1] + [{'role': 'user', 'content': 'Reply with exactly REWRITE-OK.'}]
            changed = ask('rewrite', rewrite)
            check('rewrite reuses a prefix', changed['usage'].get('prompt_cache_hit_tokens', 0) > 0)
            tool = rewrite + [{'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'read1', 'type': 'function', 'function': {'name': 'read_records', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'read1', 'content': 'The records confirm REWRITE-OK.'}]
            ask('tool-continuation', tool)
            ask('other-session', [{'role': 'user', 'content': 'Reply with B-OK.'}], conversation='B')
            resumed = ask('resume-session', tool)
            check('session resume reuses a prefix', resumed['usage'].get('prompt_cache_hit_tokens', 0) > 0)
            ask('reset-shorter', [{'role': 'user', 'content': 'Reply with RESET-OK.'}], reset=True)
            check('server remains healthy', request('/health') is not None)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    trace = (args.output / 'server.log').read_text(encoding='utf-8')
    check('tool continuation uses append plus Pressure', 'path=append_pressure' in trace and 'cached_q_reselect' not in trace)


if __name__ == '__main__':
    main()
