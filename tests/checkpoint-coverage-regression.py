"""Exercise native checkpoint coverage through ordinary document and question turns.

Diagnostic timings include tracing; they are not throughput measurements.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request
import urllib.error

def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--backend',choices=['llama','ninfer'],required=True)
    cli.add_argument('--protocol',choices=['chat','responses'],help='default: llama chat, NInfer Responses with an explicit session key')
    cli.add_argument('--exe',type=Path,required=True)
    cli.add_argument('--model',type=Path,required=True)
    cli.add_argument('--gpu',default=os.environ.get('KVMEM_TEST_GPU'),help='explicitly authorized GPU UUID')
    cli.add_argument('--gpu-layers',type=int,default=99,help='llama layers to offload; record partial offload separately')
    cli.add_argument('--timeout',type=float,default=180,help='HTTP and startup timeout in seconds')
    cli.add_argument('--mmproj',type=Path,help='matching llama vision projector for the optional image fixture')
    cli.add_argument('--image',type=Path,help='PNG fixture repeated unchanged across document edits')
    cli.add_argument('--out',type=Path,required=True)
    cli.add_argument('--mode',choices=['legacy','spread-ram','spread-disk'],required=True)
    cli.add_argument('--interval',type=int,default=512)
    cli.add_argument('--min-gap',type=int,help='override minimum history spacing; backend default is zero (disabled)')
    cli.add_argument('--context',type=int,default=16384)
    cli.add_argument('--lines',type=int,default=320)
    cli.add_argument('--host-mib',type=int,default=1024)
    cli.add_argument('--kv-host-mib',type=int,default=512)
    cli.add_argument('--disk-mib',type=int,default=2048)
    cli.add_argument('--count',type=int,default=12)
    cli.add_argument('--hot',type=int,default=2)
    cli.add_argument('--logical-mib',type=int,default=2048)
    cli.add_argument('--mtp',action='store_true')
    cli.add_argument('--mtp-state',choices=['replay','snapshots'],help='explicit llama MTP state path')
    cli.add_argument('--attention-verify',action='store_true',help='compare native sampled attention with the dense FP64 oracle')
    cli.add_argument('--expect-attention-stage',action='store_true',help='require the partial-offload cross-device Q path')
    cli.add_argument('--timing-only',action='store_true',help='disable diagnostic tracing; run after correctness qualification')
    cli.add_argument('--tool-continuation',action='store_true',help='verify an ordinary tool result does not trigger cold recall')
    cli.add_argument('--budget-pressure',action='store_true',help='require skipped optional captures and verify the Host peak bound')
    cli.add_argument('--expect-disk-pressure',action='store_true',help='require a full SSD tier to retain usable RAM checkpoints')
    cli.add_argument('--expect-capacity-error',action='store_true',help='require safe capacity rejection on the question, then a successful short turn')
    args=cli.parse_args()
    args.protocol=args.protocol or ('responses' if args.backend=='ninfer' else 'chat')
    if not args.gpu:cli.error('--gpu or KVMEM_TEST_GPU must specify the authorized device UUID')
    if args.expect_attention_stage and (not args.attention_verify or args.timing_only):
        cli.error('--expect-attention-stage requires --attention-verify and diagnostic tracing')
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    deadline=time.monotonic()+15
    occupancy_samples=[]
    while True:
        occupancy=subprocess.check_output(['nvidia-smi','-i',args.gpu,'--query-gpu=uuid,name,memory.used,utilization.gpu',
                                           '--format=csv,noheader,nounits'],text=True).strip()
        occupancy_samples.append(occupancy)
        (out/'gpu.txt').write_text('\n'.join(occupancy_samples),encoding='utf-8')
        fields=occupancy.split(',')
        if int(fields[2])<=512 and int(fields[3])<=5:break
        if time.monotonic()>=deadline:
            raise RuntimeError('requested GPU is occupied; test not started')
        time.sleep(1)
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    command=[str(args.exe.resolve())]
    if args.backend=='llama':
        command+=['-m',str(args.model.resolve()),'-c',str(args.context),'-ngl',str(args.gpu_layers),'-b','128','-ub','128',
                  '--no-ui','--no-think','--kv-dtype','q8_0','--spec-type','draft-mtp' if args.mtp else 'none',
                  ]
        if not args.timing_only: command+=['--kvmem-trace']
        if args.mmproj: command+=['--mmproj',str(args.mmproj.resolve())]
        if args.mtp:command+=['--spec-draft-n-max','3']
        if args.mtp_state:command+=['--kvmem-mtp-state',args.mtp_state]
    else:
        command+=[str(args.model.resolve()),'--max-context',str(args.context),'--max-concurrency','1',
                  '--prefill-chunk','128','--kv-dtype','int8','--spec','mtp' if args.mtp else 'none',
                  '--device-profile','off','--model-id','checkpoint-test']
        if not args.timing_only: command+=['--kvmem-verify-transfers']
        if args.mtp:command+=['--draft-tokens','3','--ngram-draft-tokens','0']
    command+=['--host','127.0.0.1','--port',str(port),'--kvmem-budget','1024',
              '--kvmem-gen-reserve','256','--kvmem-host-mib',str(args.kv_host_mib),'--kvmem-sessions','2']
    if args.mode=='spread-disk':
        spill=out/'spill';spill.mkdir()
        command+=['--kvmem-disk-mib',str(args.disk_mib),'--kvmem-disk-path',str(spill)]
    env={k:v for k,v in os.environ.items() if not k.startswith(('KVMEM_','GGML_','NINFER_','LLAMA_ARG_'))}
    env.update(CUDA_VISIBLE_DEVICES=args.gpu,CUDA_DEVICE_ORDER='PCI_BUS_ID',
               KVMEM_CHECKPOINT_POLICY='legacy' if args.mode=='legacy' else 'spread',
               KVMEM_CHECKPOINT_INTERVAL=str(args.interval),KVMEM_CHECKPOINT_COUNT=str(args.count),
               KVMEM_CHECKPOINT_HOT=str(min(args.hot,args.count)),KVMEM_CHECKPOINT_MIB=str(args.logical_mib),
               KVMEM_CHECKPOINT_HOST_MIB=str(args.host_mib))
    if args.min_gap is not None:env['KVMEM_CHECKPOINT_MIN_GAP']=str(args.min_gap)
    if args.attention_verify:env['KVMEM_ATTENTION_VERIFY']='1'
    env['PATH']=str(args.exe.resolve().parent)+os.pathsep+env['PATH']
    with args.exe.open('rb') as executable:
        executable_sha256=hashlib.file_digest(executable,'sha256').hexdigest()
    (out/'manifest.json').write_text(json.dumps({'argv':command,'gpu':occupancy,'protocol':args.protocol,
        'exe_sha256':executable_sha256,
        'policy_env':{k:v for k,v in env.items() if k.startswith('KVMEM_')},
        'diagnostics':not args.timing_only,
        'timing_scope':'stream wall time and first nonempty text delta; diagnostics disabled' if args.timing_only else 'request wall time including diagnostics'},indent=2),encoding='utf-8')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    api_path='/v1/responses' if args.protocol=='responses' else '/v1/chat/completions'
    def wire_request(body):
        if args.protocol=='chat':return body
        items=[]
        for message in body['messages']:
            if message.get('tool_calls'):
                if message.get('content'):items.append({'role':'assistant','content':message['content']})
                for call in message['tool_calls']:
                    items.append({'type':'function_call','call_id':call['id'],**call['function']})
            elif message['role']=='tool':
                items.append({'type':'function_call_output','call_id':message['tool_call_id'],'output':message['content']})
            else:
                content=message['content']
                if isinstance(content,list):
                    content=[{'type':'input_text','text':p['text']} if p['type']=='text' else
                             {'type':'input_image','image_url':p['image_url']['url']} for p in content]
                items.append({'role':message['role'],'content':content})
        return {'model':body['model'],'input':items,'max_output_tokens':body['max_tokens'],
                'temperature':0,'reasoning':{'effort':'none'},'prompt_cache_key':body['prompt_cache_key'],
                'store':False,'stream':args.timing_only}
    def normalized(response):
        if args.protocol=='chat':return response
        text=''.join(part.get('text','') for item in response.get('output',[]) if item.get('type')=='message'
                     for part in item.get('content',[]) if part.get('type')=='output_text')
        usage=response['usage'];cached=usage.get('input_tokens_details',{}).get('cached_tokens',0)
        return {'choices':[{'message':{'role':'assistant','content':text}}],
                'usage':{'prompt_tokens':usage['input_tokens'],'completion_tokens':usage['output_tokens'],
                         'prompt_cache_hit_tokens':cached,'total_tokens':usage['total_tokens']}}
    def call(path,body=None):
        request=urllib.request.Request(f'http://127.0.0.1:{port}'+path,
            None if body is None else json.dumps(body).encode(),{'Content-Type':'application/json'})
        try:
            with opener.open(request,timeout=args.timeout) as r:return json.load(r)
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode(errors='replace')) from error
    def timed_stream(body, name, started):
        request=urllib.request.Request(f'http://127.0.0.1:{port}'+api_path,
            json.dumps(body).encode(),{'Content-Type':'application/json'})
        message={'role':'assistant','content':''};usage=None;first_text=None;done=False;completed=None
        try:
            with opener.open(request,timeout=args.timeout) as response, (out/f'{name}-response.sse').open('wb') as raw:
                for line in response:
                    raw.write(line)
                    if not line.startswith(b'data:'):continue
                    data=line[5:].strip()
                    if data==b'[DONE]':done=True;break
                    event=json.loads(data)
                    if 'error' in event:raise RuntimeError(json.dumps(event['error']))
                    if args.protocol=='responses':
                        if event.get('type')=='response.output_text.delta' and event.get('delta'):
                            if first_text is None:first_text=time.perf_counter()-started
                        if event.get('type') in ('response.completed','response.incomplete'):
                            completed=event['response'];usage=completed.get('usage');done=True
                        if event.get('type')=='response.failed':
                            raise RuntimeError(json.dumps(event))
                        continue
                    if event.get('usage'):usage=event['usage']
                    for choice in event.get('choices',[]):
                        delta=choice.get('delta',{})
                        if delta.get('tool_calls'):raise RuntimeError('timing fixture unexpectedly generated tool calls')
                        for field in ('content','reasoning_content'):
                            if delta.get(field):
                                if first_text is None:first_text=time.perf_counter()-started
                                message[field]=message.get(field,'')+delta[field]
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode(errors='replace')) from error
        if not done or usage is None or first_text is None:
            raise RuntimeError('timing stream lacks completion, usage or first text delta')
        return completed or {'choices':[{'message':message}],'usage':usage},first_text
    rows=[];log_path=out/'server.log'
    with log_path.open('wb') as log:
        child=subprocess.Popen(command,env=env,stdout=log,stderr=log,
            creationflags=0x08000000 if os.name=='nt' else 0)
        try:
            deadline=time.monotonic()+args.timeout
            while True:
                if child.poll() is not None:raise RuntimeError(f'server exited {child.returncode}; see {log_path}')
                try:call('/health');break
                except (OSError,RuntimeError):
                    if time.monotonic()>deadline:raise TimeoutError('server readiness')
                    time.sleep(.2)
            def chat(name,messages,session='coverage',max_tokens=8):
                body={'model':'checkpoint-test','messages':messages,'max_tokens':max_tokens,'temperature':0,
                      'seed':42,'enable_thinking':False,'prompt_cache_key':session,'kvmem':{'conversation_id':session}}
                if args.timing_only:body.update(stream=True,stream_options={'include_usage':True})
                body=wire_request(body)
                (out/f'{name}-request.json').write_text(json.dumps(body,ensure_ascii=False,indent=2),encoding='utf-8')
                offset=log_path.stat().st_size;started=time.perf_counter()
                first_text=None
                try:
                    if args.timing_only:result,first_text=timed_stream(body,name,started)
                    else:result=call(api_path,body)
                except (RuntimeError,OSError) as error:
                    (out/f'{name}-error.json').write_text(json.dumps({'error':str(error)}),encoding='utf-8')
                    (out/f'{name}-trace.log').write_text(log_path.read_bytes()[offset:].decode('utf-8',errors='replace'),encoding='utf-8')
                    raise
                elapsed=time.perf_counter()-started
                if args.protocol=='responses':
                    (out/f'{name}-response-native.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                    result=normalized(result)
                (out/f'{name}-response.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                trace=log_path.read_bytes()[offset:].decode('utf-8',errors='replace')
                (out/f'{name}-trace.log').write_text(trace,encoding='utf-8')
                assert result['usage']['completion_tokens']>0,result
                assert not re.search(r'CUDA error|FATAL|multimodal_error|stale query checkpoint|bytes differ',trace),trace[-3000:]
                hits=re.findall(r'prefix_hit_rows=(\d+)|KVMEM_HISTORY_RESTORE frontier=(\d+)',trace)
                reused=int(next((a or b for a,b in reversed(hits)),0))
                if args.timing_only:
                    usage=result['usage']
                    reused=usage.get('prompt_cache_hit_tokens',usage.get('prompt_tokens_details',{}).get('cached_tokens',reused))
                directory=[{'row':int(r),'cold':bool(int(c)),'bytes':int(b)} for r,c,b in
                           re.findall(r'KVMEM_CHECKPOINT_DIRECTORY frontier=(\d+) cold=(\d+) bytes=(\d+)',trace)]
                row={'case':name,'wall_seconds':elapsed,'reuse':reused,'usage':result['usage'],
                     'first_text_seconds':first_text,
                     'directory':directory,'checkpoint_read':bool(re.search(r'(?:CHECKPOINT|COVERAGE)_TIER read_bytes=',trace))}
                rows.append(row)
                (out/'results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
                print(f'{name}: reuse={reused} seconds={elapsed:.3f} points={len(directory)} cold_read={row["checkpoint_read"]}',flush=True)
                return result['choices'][0]['message']
            system={'role':'system','content':'Read the reference notes. Acknowledge notes with only Noted. Answer later questions briefly from the notes.'}
            if args.lines<32: raise ValueError('--lines must be at least 32')
            lines=[f'Record {i:04d}: the coastal survey team checked the tide gauge and logged a calm evening.' for i in range(args.lines)]
            lines[len(lines)//2-5]='Station Alder uses an ultraviolet spectrometer. Its supervisor is Mara Lin.'
            document='Reference notes:\n'+'\n'.join(lines)
            def document_content(text):
                if not args.image:return text
                encoded=base64.b64encode(args.image.read_bytes()).decode('ascii')
                return [{'type':'image_url','image_url':{'url':'data:image/png;base64,'+encoded}},
                        {'type':'text','text':text}]
            initial=[system,{'role':'user','content':document_content(document)}]
            acknowledgement=chat('document',initial)
            question=initial+[acknowledgement,{'role':'user','content':'Which instrument is assigned to Station Alder?'}]
            if args.expect_capacity_error:
                try: chat('question',question,max_tokens=24)
                except RuntimeError as error:
                    assert 'checkpoint Host budget exhausted' in str(error),error
                else: raise AssertionError('expected capacity rejection was not exercised')
                chat('after-capacity',[system,{'role':'user','content':'Say hello.'}])
                trace=log_path.read_text(encoding='utf-8',errors='replace')
                assert 'multimodal_rollback_failed' not in trace
                assert 'KVMEM_CHECKPOINT_ROLLBACK' in trace
                peaks=[int(x) for x in re.findall(r'peak_bytes=(\d+)',trace)]
                assert peaks and max(peaks)<=args.host_mib*1024*1024
                (out/'capacity.json').write_text(json.dumps({'rejected':True,'recovered':True,'peak_bytes':max(peaks)}))
                print('PASS capacity rejection, rollback and next request',flush=True)
                return
            chat('question',question,max_tokens=24)
            if not args.budget_pressure and (args.count>=4 or args.mode=='legacy'):
                assert rows[-1]['reuse']>0,'question turn did not reuse document prefix'
            if not args.timing_only:
                trace=(out/'question-trace.log').read_text(encoding='utf-8')
                if args.backend=='llama':
                    feature=re.search(r'feature=\[(\d+),(\d+)\) query_source=user .*q_rows=(\d+)',trace)
                    assert feature,'question did not capture the actual user Query span'
                    begin,end,captured=map(int,feature.groups())
                    declared=re.search(r'n_prompt=\d+ query=\[(\d+),(\d+)\)',trace)
                    assert declared and (begin,end)==tuple(map(int,declared.groups()))
                else:
                    plan=re.search(r'KVMEM_PREFILL_PLAN .*query_begin=(\d+) query_end=(\d+).*capture_query=1',trace)
                    assert plan,'question did not plan native Query capture'
                    begin,end=map(int,plan.groups())
                    captures=[int(n) for n,a,b in re.findall(r'KVMEM_STATISTICS frontier=\d+ query_tokens=(\d+) query_begin=(\d+) query_end=(\d+)',trace)
                              if (int(a),int(b))==(begin,end)]
                    # Statistics lines are sparse: a chunk can cross Query end.
                    # Scoring/replay reports the completed native capture too.
                    captures += list(map(int,re.findall(r'KVMEM_QUERY scorer=.* rows=(\d+) blocks=',trace)))
                    captures += list(map(int,re.findall(r'KVMEM_QUERY_REPLAY .*query_tokens=(\d+)',trace)))
                    captured=max(captures,default=0)
                assert 0<captured==end-begin<=64,'Query includes more than the short question'
                assert begin>=rows[0]['usage']['prompt_tokens']-64 and end<=rows[-1]['usage']['prompt_tokens']
                (out/'query-span.json').write_text(json.dumps({'begin':begin,'end':end,'captured_rows':captured}),encoding='utf-8')
            if args.tool_continuation:
                tool_turn=question+[
                    {'role':'assistant','content':'','tool_calls':[{'id':'lookup_1','type':'function',
                     'function':{'name':'lookup_status','arguments':'{}'}}]},
                    {'role':'tool','tool_call_id':'lookup_1','content':'The local status check is complete.'}]
                chat('tool-continuation',tool_turn,max_tokens=24)
                if not args.timing_only:
                    trace=(out/'tool-continuation-trace.log').read_text(encoding='utf-8')
                    assert 'path=append_pressure' in trace,'tool continuation did not use resident-only append'
                    assert 'KVMEM_QUERY_REPLAY from=' not in trace,'tool continuation recalled cold history'
                    if args.backend=='llama':
                        assert re.search(r'query_source=none .*q_rows=0 replay_rows=0',trace),'tool continuation reused cold Query features'
            tail=initial+[acknowledgement,{'role':'user','content':'Name the supervisor of Station Alder.'}]
            chat('tail-edit',tail,max_tokens=24)
            chat('switch-away',[system,{'role':'user','content':'Say hello.'}],session='other')
            chat('switch-back',question,max_tokens=24)
            if not args.budget_pressure and (args.count>=4 or args.mode=='legacy'):
                assert rows[-1]['reuse']>0,'session switch lost verified prefix'
                if args.backend=='ninfer' and args.mode!='legacy' and args.protocol=='chat':
                    assert rows[-1]['reuse']>rows[0]['usage']['prompt_tokens']*.9,'anonymous short branch consumed the old long history'
            changed=lines.copy();edit_at=len(lines)//2+10
            changed[edit_at]=f'Record {edit_at:04d}: the coastal survey team checked a new tide gauge.'
            edited=[system,{'role':'user','content':document_content('Reference notes:\n'+'\n'.join(changed))}]
            chat('middle-edit',edited)
            if args.mode!='legacy' and args.count>=4 and not args.timing_only and not args.budget_pressure and (args.backend=='llama' or args.protocol=='responses'):
                assert rows[-1]['reuse']>128,'middle edit fell back to only the root'
                points=rows[0]['directory'];prompt=rows[0]['usage']['prompt_tokens']
                assert any(prompt*.2<p['row']<prompt*.8 for p in points),'history has no middle coverage'
                assert len(points)<=args.count,'combined fixed/archive record count exceeded'
            changed[15]='Record 0015: the weather was overcast near the tide station.'
            chat('front-edit',[system,{'role':'user','content':document_content('Reference notes:\n'+'\n'.join(changed))}])
            if args.mode!='legacy':
                assert all(len(r['directory'])<=args.count for r in rows),'quiescent directory exceeds count budget'
                if args.backend=='ninfer' and not args.timing_only:
                    trace=log_path.read_text(encoding='utf-8',errors='replace')
                    charges=[tuple(map(int,x)) for x in re.findall(r'KVMEM_CHECKPOINT_HOST charged_bytes=(\d+) budget_bytes=(\d+)',trace)]
                    assert charges and all(used<=limit for used,limit in charges),'checkpoint Host charge exceeded budget'
                    metadata=[tuple(map(int,x)) for x in re.findall(r'metadata_bytes=(\d+) admitted_metadata_bytes=(\d+)',trace)]
                    assert metadata and all(actual<=reserved for actual,reserved in metadata),'checkpoint metadata exceeded preallocation admission'
                    (out/'checkpoint-host.json').write_text(json.dumps({'peak_charged_bytes':max(x[0] for x in charges),
                        'budget_bytes':args.host_mib*1024*1024,'captures':len(metadata)},indent=2))
            if args.mode=='spread-disk' and args.count>2 and not args.timing_only and not args.budget_pressure and not args.expect_disk_pressure:
                assert any(r['checkpoint_read'] for r in rows),'test never loaded a cold checkpoint'
            if args.expect_disk_pressure:
                trace=log_path.read_text(encoding='utf-8',errors='replace')
                assert 'KV SSD capacity exhausted' in trace,'disk capacity pressure was not exercised'
            if args.budget_pressure:
                trace=log_path.read_text(encoding='utf-8',errors='replace')
                assert re.search(r'COVERAGE (?:capture )?skipped[:=].*budget',trace),'optional capture pressure was not exercised'
                peaks=[int(x) for x in re.findall(r'KVMEM_CHECKPOINT_COMMIT live_bytes=\d+ peak_bytes=(\d+)',trace)]
                if args.backend=='llama':
                    assert peaks and max(peaks)<=args.host_mib*1024*1024,'checkpoint Host peak exceeded budget'
            if args.image and args.backend=='llama' and not args.timing_only:
                trace=(out/'document-trace.log').read_text(encoding='utf-8')
                spans=[(int(a),int(b)) for a,b in re.findall(r'multimodal_decode .*rows=\[(\d+),(\d+)\).*image=1',trace)]
                assert spans,'image fixture did not execute vision'
                begin=min(a for a,b in spans);end=max(b for a,b in spans)
                assert not any(begin<p['row']<end for p in rows[0]['directory']),'checkpoint splits image group'
            if args.attention_verify:
                trace=log_path.read_text(encoding='utf-8',errors='replace')
                errors=[float(v) for v in re.findall(r'KVMEM_ATTENTION_ORACLE layer=\d+ rows=\d+ max_abs=(\S+)',trace)]
                assert errors,'attention oracle did not execute'
                stages=re.findall(r'KVMEM_ATTENTION_STAGE layer=(\d+) bytes=(\d+) source=(\S+) destination=(\S+)',trace)
                if args.expect_attention_stage:assert stages,'partial-offload query staging was not exercised'
                (out/'attention-qualification.json').write_text(json.dumps({
                    'oracle_calls':len(errors),'max_abs':max(errors),'staging_calls':len(stages),
                    'max_stage_bytes':max((int(s[1]) for s in stages),default=0),
                    'routes':sorted({(s[2],s[3]) for s in stages})},indent=2),encoding='utf-8')
            answer=json.loads((out/'question-response.json').read_text(encoding='utf-8'))['choices'][0]['message'].get('content') or ''
            quality={'question':'Which instrument is assigned to Station Alder?',
                     'contains_expected_instrument':'spectrometer' in answer.lower(),'answer':answer,
                     'protocol':'ordinary document request followed by separate question; no answer supplied in chat'}
            (out/'quality.json').write_text(json.dumps(quality,indent=2),encoding='utf-8')
        finally:
            child.terminate()
            try:child.wait(15)
            except subprocess.TimeoutExpired:child.kill();child.wait()
    print('PASS checkpoint coverage protocol and native reuse',flush=True)
if __name__=='__main__':main()
