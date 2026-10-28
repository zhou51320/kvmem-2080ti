"""GPU regression for unlimited output across a small KVMem resident window.

Starts its own disposable worker with a 1024-token logical context and B+R=256.
Supply a local model and an idle GPU UUID; never downloads models or touches another service.
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
    parser.add_argument('--backend', choices=('ninfer', 'llamacpp'), required=True)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--gpu', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mtp', action='store_true')
    parser.add_argument('--default-cap', type=int, default=0)
    parser.add_argument('--host-mib', type=int, default=256)
    parser.add_argument('--disk-mib', type=int, default=0,
                        help='NInfer: enable an isolated SSD history directory under --output')
    parser.add_argument('--expect-history-cap', action='store_true',
                        help='NInfer: verify automatic output fits a smaller history budget; explicit oversized caps fail')
    parser.add_argument('--skip-long', action='store_true', help='Only fixed caps, long prompt and Responses checks')
    parser.add_argument('--long-prompt', action='store_true', help='Also probe long-prefix prefill at this very small window')
    args = parser.parse_args()
    if args.expect_history_cap and args.backend != 'ninfer':
        parser.error('--expect-history-cap is a NInfer admission regression')
    if args.disk_mib and args.backend != 'ninfer':
        parser.error('--disk-mib is currently a NInfer storage regression')
    args.output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=args.gpu, CUDA_DEVICE_ORDER='PCI_BUS_ID')
    common = ['--host','127.0.0.1','--port',str(port),'--kvmem-budget','128',
              '--kvmem-gen-reserve','128','--kvmem-sink-tokens','64',
              '--kvmem-recent-tokens','64','--kvmem-host-mib',str(args.host_mib),
              '--kvmem-query-max-tokens','64','--kvmem-query-replay-max-tokens','128']
    if args.backend == 'ninfer':
        command = [str(args.server),str(args.model),'--max-context','1024','--prefill-chunk','128',
                   '--kv-dtype','int8','--device-profile','off','--no-thinking',
                   '--spec','mtp' if args.mtp else 'none',*common]
        if args.mtp: command += ['--draft-tokens','2']
        if args.default_cap: command += ['--default-max-tokens',str(args.default_cap)]
        if args.disk_mib:
            command += ['--kvmem-disk-mib',str(args.disk_mib),
                        '--kvmem-disk-path',str((args.output/'history').resolve())]
    else:
        command = [str(args.server),'-m',str(args.model),'-c','1024','-b','128','-ub','128',
                   '-ngl','99','--no-think','--kv-dtype','q8_0',
                   '-n',str(args.default_cap or -1),*common]
    result = dict(backend=args.backend, gpu_uuid=args.gpu, argv=command, checks=[], passed=False)
    def request(path, payload=None):
        req=urllib.request.Request(url+path, data=None if payload is None else json.dumps(payload).encode(),
                                   headers={'Content-Type':'application/json'})
        try:
            with opener.open(req, timeout=180) as response:
                raw=response.read()
        except urllib.error.HTTPError as error:
            if path == '/health': raise
            raise RuntimeError(f'{path}: HTTP {error.code}: {error.read().decode()}') from error
        if payload and payload.get('stream'):
            events=[json.loads(line[6:]) for line in raw.decode().splitlines()
                    if line.startswith('data: ') and line != 'data: [DONE]']
            assert b'data: [DONE]' in raw
            usage=next(e['usage'] for e in reversed(events) if e.get('usage'))
            finish=next(c['finish_reason'] for e in reversed(events) for c in e.get('choices',[])
                        if c.get('finish_reason'))
            return dict(usage=usage, choices=[dict(finish_reason=finish)])
        return json.loads(raw)
    def check(name, condition, **details):
        result['checks'].append(dict(name=name, passed=bool(condition), **details))
        assert condition, result['checks'][-1]
        print('PASS '+name, flush=True)
    def history_error(payload):
        req=urllib.request.Request(url+'/v1/chat/completions', data=json.dumps(payload).encode(),
                                   headers={'Content-Type':'application/json'})
        try:
            with opener.open(req, timeout=30) as response:
                response.read()
        except urllib.error.HTTPError as error:
            body=json.load(error)
            return error.code == 400 and 'retained payload budget' in body.get('error',{}).get('message','')
        return False
    with (args.output/'server.log').open('wb') as log:
        process=subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                if process.poll() is not None: raise RuntimeError('Worker exited; see server.log')
                try:
                    request('/health'); break
                except (OSError, urllib.error.URLError): time.sleep(.2)
            else: raise TimeoutError('Worker startup')
            props=request('/props')['default_generation_settings']
            check('reported default retains unlimited sentinel',
                  props['params']['n_predict']==(args.default_cap or -1))
            prompt='Write every integer from 1 through 10000, in ascending order, separated by commas. Output only the complete list, never summarize or skip numbers.'
            base=dict(messages=[dict(role='user',content=prompt)], temperature=0, reasoning_effort='none')
            if args.backend=='ninfer': base['ignore_eos']=True
            cases=[('omitted',{},bool(not args.default_cap)),
                   ('explicit unlimited',{'max_tokens':-1,'stream':True,'stream_options':{'include_usage':True}},True),
                   ('completion unlimited',{'max_completion_tokens':-1},True),
                   ('explicit cap',{'max_tokens':7},False),
                   ('completion cap',{'max_completion_tokens':11},False)]
            for name, overrides, unlimited in cases:
                if args.skip_long and unlimited: continue
                response=request('/v1/chat/completions',dict(base,**overrides))
                usage=response['usage']; generated=usage['completion_tokens']
                if unlimited:
                    # This independent invariant fails r4: total evaluated history must cross B+R.
                    check(name+' crosses resident window', generated>256,
                          usage=usage,finish=response['choices'][0]['finish_reason'])
                    maximum=1024-usage['prompt_tokens']+(args.backend=='ninfer')
                    check(name+' respects logical context',generated<=maximum)
                    if args.expect_history_cap:
                        check(name+' stops at attainable history capacity',
                              generated < maximum and response['choices'][0]['finish_reason']=='length')
                        frontier=usage['prompt_tokens']+generated-1
                        check(name+' reserves whole native history pages',frontier % 64 == 0)
                else:
                    cap=overrides.get('max_tokens',overrides.get('max_completion_tokens',args.default_cap))
                    check(name+' preserves fixed limit',generated==cap,usage=usage)
            if args.expect_history_cap:
                short=dict(messages=[dict(role='user',content='Reply with only the digit 4.')],
                           temperature=0,reasoning_effort='none')
                for explicit in ({},{'max_tokens':-1}):
                    response=request('/v1/chat/completions',dict(short,**explicit))
                    check('short request remains admissible '+str(explicit),
                          response['choices'][0]['finish_reason']=='stop')
                check('explicit oversized cap still fails history admission',
                      history_error(dict(short,max_tokens=1000)))
                oversized=dict(short,messages=[dict(role='system',content='word '*980),
                                                *short['messages']],max_tokens=-1)
                check('prompt exceeding history still fails admission',history_error(oversized))
            # Long prompts also remain admissible when they exceed the resident window.
            # Keep the current Query within B; the long historical prefix exceeds B+R.
            if args.long_prompt:
                extended=dict(base,messages=[dict(role='system',content='word '*280),
                                             dict(role='user',content=prompt)],max_tokens=3)
                response=request('/v1/chat/completions',extended)
                check('prompt exceeds resident window',response['usage']['prompt_tokens']>256,
                      usage=response['usage'])
                check('long prompt preserves explicit cap',response['usage']['completion_tokens']==3)
            model=request('/v1/models')['data'][0]['id']
            response=request('/v1/responses',dict(model=model,input='Reply with the word OK.',max_output_tokens=8,
                                                  reasoning={'effort':'none'}))
            check('Responses API remains usable',response['usage']['output_tokens']<=8)
            result['passed']=True
        finally:
            if process.poll() is None: process.terminate()
            try: process.wait(timeout=20)
            except subprocess.TimeoutExpired: process.kill();process.wait()
            (args.output/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')


if __name__=='__main__': main()
