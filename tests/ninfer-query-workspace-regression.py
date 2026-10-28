from pathlib import Path
import argparse,json,os,socket,subprocess,time,urllib.request,urllib.error,re
cli=argparse.ArgumentParser(description='Native NInfer draft-workspace boundary regression; use existing small MTP fixture.')
cli.add_argument('--argv-json',type=Path,required=True)
cli.add_argument('--output',type=Path,required=True)
cli.add_argument('--gpu',required=True)
options=cli.parse_args();out=options.output;out.mkdir(parents=True,exist_ok=False)
a=json.loads(options.argv_json.read_text(encoding='utf-8-sig'))
if isinstance(a,dict):a=a['argv']
a=list(a)
a[a.index('--kvmem-budget')+1]='768';a[a.index('--kvmem-gen-reserve')+1]='128';a[a.index('--ngram-draft-tokens')+1]='31'
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
a+=['--port',str(port)];opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def call(path,body=None):
 with opener.open(urllib.request.Request(f'http://127.0.0.1:{port}'+path,None if body is None else json.dumps(body).encode(),{'Content-Type':'application/json'}),timeout=180) as r:return json.load(r)
with (out/'server.log').open('w') as log:
 p=subprocess.Popen(a,env=dict(os.environ,CUDA_VISIBLE_DEVICES=options.gpu,KVMEM_TRACE='1'),stdout=log,stderr=log)
 try:
  for _ in range(600):
   if p.poll() is not None:raise RuntimeError('server exited')
   try:call('/health');break
   except Exception:time.sleep(.1)
  else:raise TimeoutError('server readiness')
  for count in [855]:
   offset=(out/'server.log').stat().st_size
   m=[{'role':'user','content':' river'*count}];body={'messages':m,'max_tokens':2,'temperature':0,'seed':42,'enable_thinking':False,'cache_reset':True,'prompt_cache_key':'edge','kvmem':{'conversation_id':'edge'}}
   r=call('/v1/chat/completions',body);(out/f'{count}-base.json').write_text(json.dumps(r));before=(out/'server.log').read_bytes()[offset:].decode('utf-8',errors='replace');offset=(out/'server.log').stat().st_size;m+=[r['choices'][0]['message'],{'role':'user','content':'Why?'}];body.update(messages=m,cache_reset=False,max_tokens=512)
   r=call('/v1/chat/completions',body);(out/f'{count}-next.json').write_text(json.dumps(r));trace=(out/'server.log').read_bytes()[offset:].decode('utf-8',errors='replace')
   assert r['usage']['completion_tokens']==512, 'test must cross the append reserve during decode'
   assert trace.count('KVMEM_PRESSURE phase=decode')>=2, 'test must resume DecodePressure after Query replay'
   assert r['usage']['prompt_tokens']<896, 'test must remain below B+R'
   assert 'KVMEM_PRESSURE ' in before, 'test must create an already compressed prefix'
   assert r['usage']['prompt_tokens_details']['cached_tokens']>0, 'test must restore the prefix'
   assert 'KVMEM_QUERY scorer=page-softmax' in trace, 'new Query skipped on compressed prefix'
   assert 'KVMEM_QUERY_REPLAY from=' in trace, 'test must exercise admitted replay'
   assert not re.search(r'FATAL|CUDA error|has no GPU slot',trace)
   (out/'result.json').write_text(json.dumps({'status':'passed','gpu':options.gpu,'argv':a,'usage':r['usage']}))
   print('PASS new Query recalls a prefix compressed below B+R by native draft workspace',flush=True)
 finally:
  p.terminate();p.wait(timeout=20)
