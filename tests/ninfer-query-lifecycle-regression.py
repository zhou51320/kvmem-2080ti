"""Real HTTP Query/decode interleaving and partial-prefill cancellation regression.

Requires ninfer-serve built with BUILD_TESTING=ON and
NINFER_KVMEM_QUERY_TEST_HOOKS=ON. The test-only gate pauses at a normal
prefill-step boundary; HTTP ingress, cancellation, scheduling, restore and GPU
execution still use the native service. Run separately for each Query scorer.
KVMem rejects concurrent prefill; check that guard, then exercise supported
Query-prefill/peer-decode overlap instead of bypassing the product restriction.
The source document is sent first; questions are separate ordinary requests.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--argv-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gpu', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    gate = args.output / 'gate'
    gate.mkdir()
    command = json.loads(args.argv_json.read_text(encoding='utf-8-sig'))
    command = list(command['argv'] if isinstance(command, dict) else command)

    def option(key, value):
        if key in command:
            command[command.index(key) + 1] = str(value)
        else:
            command.extend([key, str(value)])

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    for key, value in {'--port': port, '--max-context': 8192, '--prefill-chunk': 2048,
                       '--max-concurrency': 2, '--kvmem-query-max-tokens': 512,
                       '--decode-rounds-per-prefill': 1,
                       '--kvmem-budget': 768, '--kvmem-gen-reserve': 2048,
                       '--kvmem-sessions': 16, '--kvmem-host-mib': 512,
                       '--request-log-jsonl': args.output / 'requests.jsonl'}.items():
        option(key, value)
    if '--concurrent-prefill' in command:
        command.remove('--concurrent-prefill')
    for flag in ['--kvmem-verify-transfers']:
        if flag not in command:
            command.append(flag)
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=args.gpu, CUDA_DEVICE_ORDER='PCI_BUS_ID',
               NINFER_QUERY_TEST_GATE=str(gate.resolve()), KVMEM_TRACE='1')
    env['PATH'] = str(Path(command[0]).parent) + os.pathsep + env.get('PATH', '')
    env.pop('KVMEM_ATTENTION_VERIFY', None)
    log_path = args.output / 'server.log'
    report = {'gpu': args.gpu, 'argv': list(command), 'checks': [], 'comparisons': [],
              'scope': 'test-gated native HTTP lifecycle; no performance measurement'}

    def save(name, value):
        (args.output / (name + '.json')).write_text(json.dumps(value, indent=2), encoding='utf-8')

    def check(name, passed):
        report['checks'].append({'name': name, 'pass': bool(passed)})
        save('results', report)
        if not passed:
            raise AssertionError(name)
        print('PASS ' + name, flush=True)

    def call(path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=180)
        try:
            connection.request('GET' if body is None else 'POST', path,
                               None if body is None else json.dumps(body),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read().decode()
            if response.status != 200:
                raise RuntimeError(f'{response.status}: {raw}')
            return raw if path == '/metrics' else json.loads(raw)
        finally:
            connection.close()

    def metric(name):
        match = re.search(r'^' + re.escape(name) + r'\s+(\S+)', call('/metrics'), re.M)
        if not match:
            raise AssertionError('missing metric ' + name)
        return float(match[1])

    def wait_for(predicate, message, seconds=120):
        deadline = time.monotonic() + seconds
        while not predicate():
            if server.poll() is not None:
                raise RuntimeError('server exited: ' + message)
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            time.sleep(.02)

    class Job:
        def __init__(self, name, session, messages, reset=False, maximum=8):
            self.name = name
            self.body = {'messages': messages, 'temperature': 0, 'seed': 42, 'max_tokens': maximum,
                         'ignore_eos': maximum > 8,
                         'enable_thinking': False, 'cache_reset': reset,
                         'prompt_cache_key': session, 'kvmem': {'conversation_id': session}}
            save(name + '.request', self.body)
            self.response = None
            self.error = None
            self.raw_socket = None
            self.done = threading.Event()
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.started = time.monotonic()
            self.thread.start()

        def run(self):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=180)
            try:
                connection.connect()
                self.raw_socket = connection.sock
                connection.request('POST', '/v1/chat/completions', json.dumps(self.body),
                                   {'Content-Type': 'application/json'})
                response = connection.getresponse()
                raw = response.read().decode()
                if response.status != 200:
                    raise RuntimeError(f'{response.status}: {raw}')
                self.response = json.loads(raw)
                save(self.name + '.response', self.response)
            except Exception as error:
                self.error = repr(error)
                save(self.name + '.transport-error', {'error': self.error})
            finally:
                connection.close()
                save(self.name + '.timing', {'elapsed_seconds': time.monotonic() - self.started})
                self.done.set()

        def finish(self):
            wait_for(self.done.is_set, self.name + ' completion')
            if self.error:
                raise RuntimeError(self.name + ': ' + self.error)
            check(self.name + ' completed', self.response['usage']['completion_tokens'] > 0)
            usage, timings = self.response['usage'], self.response['timings']
            check(self.name + ' completed prefill accounting',
                  timings['cache_n'] + timings['prompt_n'] == usage['prompt_tokens'] and
                  timings['cache_n'] == usage.get('prompt_tokens_details', {}).get('cached_tokens', 0))
            return self.response

        def disconnect(self):
            self.raw_socket.shutdown(socket.SHUT_RDWR)
            self.raw_socket.close()
            # Windows may retain the HTTP reader's duplicated socket until the
            # worker leaves the gate. Native cancellation, not local thread
            # completion, is the assertion after releasing that boundary.
            check(self.name + ' disconnect sent before HTTP output', self.response is None)

    def trace(offset, name):
        text = log_path.read_bytes()[offset:].decode('utf-8', errors='replace')
        (args.output / (name + '.trace.log')).write_text(text, encoding='utf-8')
        events = []
        for line in text.splitlines():
            if line.startswith('KVMEM_QUERY_TEST '):
                events.append(dict(re.findall(r'(\w+)=(\S+)', line)))
        return events

    def signature(events, lane=None):
        scored = [event for event in events if event['phase'] == 'scored' and
                  (lane is None or int(event['lane']) == lane)]
        selected = [event for event in events if event['phase'] == 'selected' and
                    (lane is None or int(event['lane']) == lane)]
        # LRFU re-scores the accepted post-replay index using the same frozen Q.
        check('complete Query selection and consistent frozen features', scored and len(selected) == 1 and
              len({(event['rows'], event['hash']) for event in scored}) == 1)
        return {key: scored[0][key] for key in ['rows', 'hash']} | {
            'scores': [event['scores'] for event in scored], 'pages': selected[0]['pages']}

    def compare(name, actual, expected):
        report['comparisons'].append({'name': name, 'actual': actual, 'expected': expected})
        check(name + ' rows, accepted features, scores and pages equal independent request', actual == expected)

    document = ''.join(f'Record {i}: the valley river is blue and flows past the forest.\n'
                       for i in range(240)) + '\nAcknowledge briefly; the question follows separately.'

    def ingest(session, dataset='A'):
        # Distinct documents prevent the peer from leasing the very same native
        # cached endpoint, which would force a cold recomputation in only one
        # member of the comparison and change its attention view.
        text = document if dataset == 'A' else 'Independent reference B.\n' + document.replace('blue', 'green')
        messages = [{'role': 'user', 'content': text}]
        response = Job(session + '-document', session, messages, reset=True).finish()
        return messages + [response['choices'][0]['message']]

    def align_question(messages, word, target):
        check('alignment filler is one token per repetition',
              len(call('/tokenize', {'content': word * 16})['tokens']) == 16)
        count = 0
        for _ in range(8):
            question = word * count + '\nWhat color is the river in the reference?'
            trial = messages + [{'role': 'user', 'content': question}]
            prompt = call('/apply-template', {'messages': trial, 'enable_thinking': False})['prompt']
            length = len(call('/tokenize', {'content': prompt})['tokens'])
            if length == target:
                return question
            count += target - length
            if count < 1:
                raise AssertionError('document is too long for target geometry')
        raise AssertionError('could not align Query across the 2048-token boundary')

    def question_job(name, session, messages, question, maximum=8):
        return Job(name, session, messages + [{'role': 'user', 'content': question}], maximum=maximum)

    def arm():
        for path in gate.iterdir():
            path.unlink()
        (gate / 'arm').touch()

    def paused(job):
        wait_for(lambda: (gate / 'ready').exists() or job.done.is_set(), 'partial Query gate')
        check(job.name + ' stopped at partial Query boundary', (gate / 'ready').exists() and not job.done.is_set())
        lane, accepted, total = map(int, (gate / 'ready').read_text().split())
        check(job.name + ' captured a strict prefix of all 512 rows', 0 < accepted < total == 512)
        (gate / 'arm').unlink()
        return lane

    def release():
        (gate / 'release').touch()

    save('results', report)
    unsupported = subprocess.run(command + ['--concurrent-prefill'], env=env,
                                 capture_output=True, timeout=20)
    unsupported_log = (unsupported.stdout + unsupported.stderr).decode('utf-8', errors='replace')
    (args.output / 'concurrent-prefill-rejected.log').write_text(unsupported_log, encoding='utf-8')
    check('KVMem rejects unsupported simultaneous Query prefills',
          unsupported.returncode != 0 and 'KVMem requires' in unsupported_log)
    with log_path.open('w', encoding='utf-8') as log:
        server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            def healthy():
                try:
                    return bool(call('/health'))
                except (OSError, http.client.HTTPException, RuntimeError):
                    return False
            wait_for(healthy, 'server readiness')
            base = ingest('reference-A')
            # 6400 total tokens: the 512-row last-user tail straddles 6144.
            question_a = align_question(base, ' Review', 6400)
            question_edit = align_question(base, ' Check', 6400)
            question_b = 'What color is the river? Answer briefly.'
            questions = {'A': question_a, 'B': question_b, 'edit': question_edit}
            references = {}
            bases = {'A': base, 'edit': base}
            for label, question in questions.items():
                session = 'reference-' + label
                messages = base if label == 'A' else ingest(session, 'B' if label == 'B' else 'A')
                if label == 'B':
                    bases['B'] = messages
                check(session + ' document response stable', messages == bases[label])
                offset = log_path.stat().st_size
                response = question_job(session + '-question', session, messages, question).finish()
                check(session + ' reuses native document prefix', response['usage'].get(
                    'prompt_tokens_details', {}).get('cached_tokens', 0) > 0)
                references[label] = signature(trace(offset, session))
            check('edited Query differs from original', references['edit']['hash'] != references['A']['hash'])

            for mode in ['interleave', 'cancel-retry', 'cancel-edit']:
                # A previous completed identical question is a legitimate exact
                # cache hit and would skip Query capture. Isolate each scenario
                # from the controls and other scenarios, preserving all logs.
                server.terminate()
                server.wait(timeout=20)
                log_path = args.output / (mode + '.server.log')
                option('--request-log-jsonl', args.output / (mode + '.requests.jsonl'))
                with log_path.open('w', encoding='utf-8') as scenario_log:
                    server = subprocess.Popen(command, env=env, stdout=scenario_log, stderr=subprocess.STDOUT)
                wait_for(healthy, mode + ' server readiness')
                messages_a, messages_b = ingest(mode + '-A'), ingest(mode + '-B', 'B')
                check(mode + ' document inputs and responses match controls', messages_a == base and messages_b == bases['B'])
                cancelled_before = metric('ninfer:cancelled_prefills_total')
                cancelled_tokens_before = metric('ninfer:cancelled_prefill_computed_tokens_total')
                offset = log_path.stat().st_size
                job_b = question_job(mode + '-question-B', mode + '-B', messages_b, question_b, maximum=256)
                wait_for(lambda: 'phase=scored' in log_path.read_bytes()[offset:].decode('utf-8', errors='replace')
                         or job_b.done.is_set(), 'B entered native decode')
                check(mode + ' B is still active', not job_b.done.is_set())
                arm()
                job_a = question_job(mode + '-question-A', mode + '-A', messages_a, question_a)
                lane_a = paused(job_a)
                wait_for(lambda: metric('ninfer:requests_admitted') == 2, 'both real HTTP requests admitted', 10)
                if mode.startswith('cancel'):
                    job_a.disconnect()
                    time.sleep(.25)  # service cancellation polling is asynchronous
                else:
                    # Both lanes are occupied at the gate: this request must never start prefill.
                    queued_before = metric('ninfer:waiting_cancelled_requests_total')
                    request_log = args.output / (mode + '.requests.jsonl')

                    def started_requests():
                        return sum(json.loads(line).get('event') == 'request_start'
                                   for line in request_log.read_text(encoding='utf-8').splitlines()
                                   if line.endswith('}'))

                    started_before = started_requests()
                    queued = question_job(mode + '-queued', mode + '-queued', messages_a, question_a)
                    # request_start is written after Engine::submit has enqueued the request.
                    # The deferred gauge is a worker-published snapshot and cannot refresh at this gate.
                    wait_for(lambda: started_requests() == started_before + 1 and
                             metric('ninfer:requests_admitted') == 3,
                             'third request waiting for admission', 10)
                    queued.disconnect()
                    time.sleep(.25)
                release()
                job_b.finish()
                if mode == 'interleave':
                    job_a.finish()
                    wait_for(lambda: metric('ninfer:waiting_cancelled_requests_total') > queued_before,
                             'queued cancellation consumed', 10)
                    wait_for(queued.done.is_set, 'queued cancelled HTTP reader completion', 10)
                    request_log = args.output / (mode + '.requests.jsonl')

                    def queued_records():
                        return [record for line in request_log.read_text(encoding='utf-8').splitlines()
                                if line.endswith('}') and (record := json.loads(line)).get('event') == 'request_done'
                                and record['result']['finish_reason'] == 'cancelled']

                    wait_for(lambda: bool(queued_records()), 'queued cancellation request log', 10)
                    queued_result = queued_records()[0]['result']
                    check('queued cancellation has no executed or reused prompt tokens',
                          queued_result['prompt_tokens'] == 6400 and
                          queued_result['computed_prefill_tokens'] == 0 and
                          queued_result['prefix_cache_hit_tokens'] == 0 and
                          queued_result['prefix_reuse_path'] == 'root' and
                          queued_result['completion_tokens'] == 0)
                    save('queued.accounting', queued_records()[0])
                else:
                    wait_for(lambda: metric('ninfer:cancelled_prefills_total') > cancelled_before,
                             'native prefill cancellation', 10)
                    wait_for(job_a.done.is_set, 'cancelled HTTP reader completion', 10)
                    check(mode + ' produced no completed response', job_a.response is None)
                events = trace(offset, mode)
                # A cancellation may release its lane before B is admitted.
                # Query geometry, unlike a recycled lane number, distinguishes them.
                b_scored = [event for event in events if event['phase'] == 'scored' and
                            event['rows'] == references['B']['rows']]
                check(mode + ' B really scored its own Query', b_scored and len({event['lane'] for event in b_scored}) == 1)
                lane_b = int(b_scored[0]['lane'])
                compare(mode + '-B', signature(events, lane_b), references['B'])
                if mode == 'interleave':
                    check('interleaved requests own different lanes', lane_b != lane_a)
                    compare(mode + '-A', signature(events, lane_a), references['A'])
                    partial = next(i for i, event in enumerate(events) if event['phase'] == 'accepted' and
                                   int(event['lane']) == lane_a and int(event['rows']) < 512)
                    final = next(i for i, event in enumerate(events) if event['phase'] == 'accepted' and
                                 int(event['lane']) == lane_a and int(event['rows']) == 512)
                    check('native A(partial Query) -> B(accepted decode) -> A(complete Query) interleaving',
                          any(event['phase'] == 'decode' and int(event['lane']) == lane_b
                              for event in events[partial+1:final]))
                else:
                    a_events = [event for event in events if event['phase'] == 'accepted' and
                                int(event['query_end'])-int(event['query_begin']) == 512]
                    check(mode + ' no completed or scored Query from cancelled A',
                          a_events and all(event['phase'] == 'accepted' and 0 < int(event['rows']) < 512
                                           for event in a_events) and not any(
                              event['phase'] == 'scored' and int(event['rows']) == 512 for event in events))
                    # Independently derive the logical work from native restore/accepted frontiers,
                    # rather than treating all input after the cached prefix as executed.
                    native_text = (args.output / (mode + '.trace.log')).read_text(encoding='utf-8')
                    before_a = native_text.split('KVMEM_QUERY_TEST phase=accepted lane=' + str(lane_a), 1)[0]
                    restored = int(re.findall(r'KVMEM_HISTORY_RESTORE frontier=(\d+)', before_a)[-1])
                    frontier = max(int(event['end']) for event in a_events)
                    computed = frontier - restored
                    request_log = args.output / (mode + '.requests.jsonl')

                    def cancelled_records():
                        if not request_log.exists():
                            return []
                        return [record for line in request_log.read_text(encoding='utf-8').splitlines()
                                if line.endswith('}') and (record := json.loads(line)).get('event') == 'request_done'
                                and record['result']['finish_reason'] == 'cancelled']

                    wait_for(lambda: bool(cancelled_records()), mode + ' cancellation request log', 10)
                    records = cancelled_records()
                    check(mode + ' exactly one cancelled request logged', len(records) == 1)
                    result = records[0]['result']
                    check(mode + ' preserves actual admitted reuse',
                          result['prefix_cache_hit_tokens'] == restored > 0 and
                          result['prefix_reuse_path'] != 'root')
                    check(mode + ' counts only accepted prefill tokens',
                          result['computed_prefill_tokens'] == computed > 0 and
                          restored + computed == frontier < result['prompt_tokens'] == 6400)
                    check(mode + ' request and engine cancellation counters agree',
                          metric('ninfer:cancelled_prefill_computed_tokens_total') - cancelled_tokens_before == computed)
                    durations = records[0]['timings_seconds']
                    check(mode + ' retains partial prefill duration',
                          0 < durations['prefill'] <= durations['total'] and durations['decode'] == 0)
                    save(mode + '.accounting', {'restored': restored, 'accepted_frontier': frontier,
                                              'computed': computed, 'request_result': result})
                    label = 'A' if mode == 'cancel-retry' else 'edit'
                    offset = log_path.stat().st_size
                    response = question_job(mode + '-recovered', mode + '-A', messages_a, questions[label]).finish()
                    check(mode + ' recovery reuses a native prefix', response['usage'].get(
                        'prompt_tokens_details', {}).get('cached_tokens', 0) > 0)
                    compare(mode + '-recovered', signature(trace(offset, mode + '-recovered')), references[label])
            check('service remains healthy', bool(call('/health')))
            report['status'] = 'passed'
        except Exception as error:
            report['status'] = 'failed'
            report['error'] = repr(error)
            raise
        finally:
            release()
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            save('results', report)


if __name__ == '__main__':
    main()
