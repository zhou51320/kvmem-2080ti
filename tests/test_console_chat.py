"""Core transport invariants, without loading a GPU model."""
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from console_chat import ChatBridge, context_payload, request_metrics

class Upstream(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.received.append((self.path,data,dict(self.headers)))
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        if data.get('wait'):
            self.server.waiting.set()
            self.connection.settimeout(4)
            try: self.server.closed.set() if not self.connection.recv(1) else None
            except (ConnectionResetError, OSError): self.server.closed.set()
            return
        events=[{'id':'native-id','choices':[{'delta':{'reasoning_content':'think'}}]},
                {'id':'native-id','choices':[{'delta':{'content':'answer'},'finish_reason':'length'}],
                 'timings':{'predicted_n':8,'predicted_per_second':10}}]
        for event in events:self.wfile.write(b'data: '+json.dumps(event).encode()+b'\n\n');self.wfile.flush()
        if not data.get('truncate'):self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()

class Control:
    def __init__(self,port):
        self.lock=threading.RLock();self.instance='one';self.port=port;self.backend='polystrata'
    def status(self):return {'state':'ready','instance':self.instance,'url':f'http://127.0.0.1:{self.port}', 'active_config':{'parameters':{'Backend':self.backend}}}
    def monitor_authorization(self):return 'Bearer managed-test-key'

class Downstream(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        try:self.server.bridge.stream(self,data)
        except ValueError as e:self.send_error(400,str(e))

class TransportTests(unittest.TestCase):
    def setUp(self):
        self.up=ThreadingHTTPServer(('127.0.0.1',0),Upstream);self.up.received=[];self.up.waiting=threading.Event();self.up.closed=threading.Event()
        self.control=Control(self.up.server_port);self.bridge=ChatBridge(self.control)
        self.down=ThreadingHTTPServer(('127.0.0.1',0),Downstream);self.down.bridge=self.bridge
        for server in [self.up,self.down]:threading.Thread(target=server.serve_forever,daemon=True).start()
    def tearDown(self):
        self.bridge.cancel_all()
        for server in [self.down,self.up]:server.shutdown();server.server_close()
    def request(self,id='r1',instance='one',**extra):
        conn=http.client.HTTPConnection('127.0.0.1',self.down.server_port,timeout=5)
        headers={'Content-Type':'application/json','X-KVMem-Request':id}
        if instance:headers['X-KVMem-Instance']=instance
        conn.request('POST','/api/chat/completions',json.dumps({'messages':[{'role':'user','content':'hi'}],**extra}),headers)
        return conn,conn.getresponse()
    def test_stream_preserves_native_fields_and_metadata(self):
        c,r=self.request();body=r.read();c.close()
        self.assertEqual(r.status,200);self.assertIn(b'answer',body);self.assertIn(b'predicted_per_second',body)
        self.assertTrue(body.endswith(b'data: [DONE]\n\n'))
        for _ in range(20):
            if self.bridge.records['r1'].get('finished_at_ms'):break
            time.sleep(.01)
        record=self.bridge.records['r1'];self.assertEqual(record['state'],'completed');self.assertEqual(record['finish_reason'],'length')
        self.assertNotIn('messages',record);self.assertNotIn('answer',json.dumps(record))
        path,data,headers=self.up.received[0];self.assertEqual(path,'/v1/chat/completions');self.assertEqual(data['_kvmem_request_id'],'r1');self.assertEqual(headers['Authorization'],'Bearer managed-test-key')
    def test_restart_and_missing_identity_rejected_before_upstream(self):
        for epoch in ['old',None]:
            c,r=self.request(instance=epoch);self.assertEqual(r.status,400);r.read();c.close()
        self.assertFalse(self.up.received)
    def test_eof_is_an_error(self):
        c,r=self.request(truncate=True);body=r.read();c.close();self.assertIn(b'Engine stream ended before DONE',body)
        self.assertEqual(self.bridge.records['r1']['state'],'error')
    def test_cancel_while_waiting_closes_upstream(self):
        c,r=self.request(wait=True);self.assertTrue(self.up.waiting.wait(1))
        self.bridge.cancel('r1');self.assertTrue(self.up.closed.wait(2));r.read();c.close()
    def test_disconnect_while_waiting_closes_upstream(self):
        c,r=self.request(wait=True);self.assertTrue(self.up.waiting.wait(1))
        r.close();c.close();self.assertTrue(self.up.closed.wait(2))
    def test_disconnect_during_headers_releases_upstream(self):
        handler=MagicMock()
        handler.headers={'X-KVMem-Request':'headers-aborted','X-KVMem-Instance':'one'}
        handler.end_headers.side_effect=ConnectionAbortedError('browser closed before headers')
        self.bridge.stream(handler,{'messages':[{'role':'user','content':'hi'}],'wait':True})
        self.assertTrue(self.up.closed.wait(2))
        self.assertFalse(self.bridge.active)
        self.assertEqual(self.bridge.records['headers-aborted']['state'],'cancelled')
    def test_tool_execution_rejected(self):
        c,r=self.request(tools=[]);self.assertEqual(r.status,400);r.read();c.close();self.assertFalse(self.up.received)
    def test_ninfer_budget_maps_to_native_field(self):
        self.control.backend='ninfer'
        c,r=self.request(reasoning_budget_tokens=16);r.read();c.close()
        payload=self.up.received[0][1];self.assertEqual(payload['thinking_budget'],16);self.assertNotIn('reasoning_budget_tokens',payload)
        c,r=self.request(id='invalid',reasoning_budget_tokens=0);self.assertEqual(r.status,400);r.read();c.close()

class ContextTests(unittest.TestCase):
    def test_exact_native_id_join_and_ambiguous_rows(self):
        control=Control(1);control.backend='llamacpp';bridge=ChatBridge(control)
        bridge.records['browser']={'id':'browser','instance':'one','native_id':'chatcmpl-abc','state':'answering'}
        row={'id':'42','native_request_id':'chatcmpl-abc','status':'generating','decode_tps':10,
             'decode_tps_live':20,'decode_tps_window_ms':3000,'decode_tps_sampled_at_ms':time.time()*1000}
        bridge.get_json=MagicMock(return_value={'active':[row],'recent':[]})
        result=bridge.record('browser')['metrics']
        self.assertEqual(result['decode_tps'],20)
        self.assertEqual(result['decode_tps_mean'],10)
        control.backend='ninfer'
        self.assertEqual(bridge.record('browser')['metrics']['decode_tps'],20)
        bridge.get_json.return_value={'active':[row,dict(row,id='43')],'recent':[]}
        self.assertNotIn('metrics',bridge.record('browser'))
        bridge.get_json.return_value={'active':[dict(row,native_request_id='other')],'recent':[]}
        self.assertNotIn('metrics',bridge.record('browser'))

    def test_live_speed_scope_freshness_and_native_sse_fallback(self):
        row={'id':'1','status':'generating','decode_tps':10,'decode_tps_live':20,
             'decode_tps_window_ms':3000,'decode_tps_sampled_at_ms':time.time()*1000-20000}
        snapshot={'active':[row],'recent':[]}
        for backend in ('llamacpp','ninfer'):
            value=request_metrics({},snapshot,row,backend)
            self.assertIsNone(value['decode_tps'])
            self.assertEqual(value['decode_tps_mean'],10)
        self.assertEqual(request_metrics({},snapshot,row,'polystrata')['decode_tps'],10)
        value=request_metrics({'state':'completed','finish_reason':'length','timings':{
            'prompt_ms':200,'predicted_ms':1000,'predicted_per_second':12,'draft_n':20,'draft_n_accepted':10}}, {}, None,'ninfer')
        self.assertIsNone(value['decode_tps'])
        self.assertEqual(value['decode_tps_mean'],12)
        self.assertEqual(value['prefill_ms'],200)
        self.assertEqual(value['drafts_accepted'],10)
        self.assertEqual(value['finish_reason'],'length')

    def test_native_props_fallbacks_and_unknown_thinking(self):
        for backend in ('ninfer', 'llamacpp'):
            state={'state':'ready','instance':'one','url':'http://localhost:99',
                   'active_config':{'parameters':{'Backend':backend}}}
            props={'model_alias':'custom-model','default_generation_settings':{
                'n_ctx':8192,'params':{'n_predict':64,'temperature':.2}},
                'kvmem':{'enabled':True,'selected_tokens':768}}
            result=context_payload(state,props)
            self.assertEqual(result['model'],'custom-model')
            self.assertEqual(result['limit'],8192)
            self.assertEqual(result['effective']['max_tokens'],64)
            self.assertEqual(result['effective']['temperature'],.2)
            self.assertNotIn('enable_thinking',result['effective'])

    def test_engine_defaults_win_and_draft_is_not_read(self):
        state={'state':'ready','instance':'one','url':'http://localhost:99','active_config':{'parameters':{'Backend':'polystrata','MaxTokens':128}},'draft':{'parameters':{'MaxTokens':4}}}
        props={'model_alias':'test','default_generation_settings':{'n_ctx':4096,'params':{'max_tokens':64}}}
        result=context_payload(state,props,{'effective':{'max_tokens':48,'reasoning_effort':'none','reasoning_budget_tokens':12,'preserve_thinking':True}})
        self.assertEqual(result['effective']['max_tokens'],48);self.assertFalse(result['effective']['enable_thinking']);self.assertTrue(result['effective']['preserve_thinking'])
        self.assertEqual(result['history_export_url'],'http://localhost:99/kvmem-history-export')

if __name__=='__main__':unittest.main()
