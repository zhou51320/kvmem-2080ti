import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

# Explicit model path: this regression never downloads weights.
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--binary',required=True)
parser.add_argument('--model',required=True)
parser.add_argument('--out',type=Path,required=True)
parser.add_argument('--gpu',required=True)
parser.add_argument('--port',type=int,default=18866)
parser.add_argument('--tag',default='')
parser.add_argument('--modes',nargs='+',choices=['none','snapshots','replay'],default=['none','snapshots','replay'])
args=parser.parse_args()
root=args.out.resolve()
root.mkdir(parents=True,exist_ok=True)
tag=('-'+args.tag) if args.tag else ''
gpu=args.gpu
model=Path(args.model).resolve()
if not model.is_file(): parser.error('--model must be an existing complete MTP GGUF')
env={k:v for k,v in os.environ.items() if not k.startswith(('KVMEM_','LLAMA_','GGML_','CUDA_VISIBLE_DEVICES'))}
env['CUDA_VISIBLE_DEVICES']=gpu
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
port=args.port
base=f'http://127.0.0.1:{port}'
def api(path, body=None):
    request=urllib.request.Request(base+path, data=None if body is None else json.dumps(body).encode(), headers={'Content-Type':'application/json'})
    with opener.open(request,timeout=240) as response: return json.load(response)
report={'gpu':gpu,'scope':'correctness only','model':str(model),'modes':[]}
def save(): (root/f'llama-service{tag}-validation.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')

for mode in args.modes:
    with socket.socket() as probe: probe.bind(('127.0.0.1',port))
    cmd=[args.binary,'-m',str(model),
         '--host','127.0.0.1','--port',str(port),'--no-ui','-np','2','-c','8192',
         '-b','128','-ub','128','-t','4','-tb','8','-ngl','99','--device','CUDA0',
         '--kv-dtype','q8_0','--kvmem-budget','384','--kvmem-gen-reserve','512',
         '--kvmem-block-tokens','64','--kvmem-host-mib','1024','--kvmem-sessions','2',
         '--no-think','--temp','0','--presence-penalty','0','--kvmem-mtp-state',
         'snapshots' if mode=='none' else mode]
    if mode!='none': cmd+=['--spec-type','draft-mtp','--spec-draft-n-max','4']
    run={'mode':mode,'command':cmd,'status':'running','responses':[],'max_busy':0}
    report['modes'].append(run); save()
    logpath=root/f'llama-service{tag}-{mode}.log'
    with logpath.open('wb') as log:
        proc=subprocess.Popen(cmd,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+180
            while True:
                if proc.poll() is not None: raise RuntimeError(f'{mode} server exited at startup')
                try:
                    if api('/health').get('status')=='ok': break
                except (OSError, ValueError): pass
                if time.monotonic()>deadline: raise RuntimeError('server startup timeout')
                time.sleep(.5)
            print(mode+': ready',flush=True)
            words=('ORCHID','CEDAR')
            messages=[]
            for word in words:
                text=('Background archive: stars are catalogued by their position and brightness. '*110+
                      f'\nWrite 1000 numbered lines. Each line must contain only its number and the word {word}. '
                      'Start immediately with line 1, and continue without explanations or a conclusion.')
                messages.append([{'role':'user','content':text}])
            def generate(lane, limit, concurrent=False):
                body={'model':'local','messages':messages[lane],'temperature':0,'presence_penalty':0,
                      'max_tokens':limit,'cache_reset':True,'conversation_id':f'{mode}-{lane}'}
                result=api('/v1/chat/completions',body)
                record={'lane':lane,'concurrent':concurrent,'limit':limit,'response':result}
                choice=result['choices'][0]
                content=choice['message']['content']
                assert choice['finish_reason']=='length' and result['usage']['completion_tokens']==limit,record
                assert words[lane] in content and words[1-lane] not in content,record
                stats=result.get('kvmem',{}).get('speculative',{})
                if mode!='none':
                    assert 0 < stats['accepted_tokens'] <= stats['drafted_tokens'],stats
                return record
            isolated=[generate(0,129),generate(1,257)]
            run['responses']+=isolated; save()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                pending=[pool.submit(generate,lane,limit,True) for lane,limit in ((0,129),(1,257))]
                while not all(f.done() for f in pending):
                    run['max_busy']=max(run['max_busy'],sum(s['is_processing'] for s in api('/slots')))
                    time.sleep(.05)
                concurrent_rows=[f.result() for f in pending]
            run['responses']+=concurrent_rows
            assert run['max_busy']==2,run['max_busy']
            run['isolated_concurrent_exact']=[a['response']['choices'][0]['message']['content']==b['response']['choices'][0]['message']['content'] for a,b in zip(isolated,concurrent_rows)]
            # A follow-up must recover the per-conversation marker after both lanes completed.
            for lane in range(2):
                previous=concurrent_rows[lane]['response']['choices'][0]['message']
                history=messages[lane]+[previous,{'role':'user','content':'Reply with just the word you repeated, without the numbering.'}]
                result=api('/v1/chat/completions',{'model':'local','messages':history,'temperature':0,
                    'max_tokens':16,'conversation_id':f'{mode}-{lane}'})
                content=result['choices'][0]['message']['content']
                assert words[lane] in content and words[1-lane] not in content,result
                run['responses'].append({'lane':lane,'followup':True,'response':result})
            assert api('/health')['status']=='ok'
            assert not any(s['is_processing'] for s in api('/slots'))
            run['props']=api('/props')
            run['functional_checks']='pass'
            run['commit_errors']=[line for line in logpath.read_text(errors='replace').splitlines() if 'lane commit failed' in line]
            run['status']='failed' if run['commit_errors'] else 'pass'; save()
            print(mode+': functional checks PASS; commit errors='+str(len(run['commit_errors'])),flush=True)
        except BaseException as error:
            run['status']='failed'; run['error']=str(error); save()
            raise
        finally:
            proc.terminate()
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
report['status']='pass' if all(r['status']=='pass' for r in report['modes']) else 'failed'; save()

if report['status'] != 'pass': raise SystemExit(1)
