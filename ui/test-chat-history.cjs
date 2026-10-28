// Node 24 strips the standalone TypeScript module's types; no model or browser required.
const assert=require('node:assert/strict');
(async()=>{
 const {normalizeHistory,activeBranch}=await import('./lightweight/history.ts');
 globalThis.location={origin:'http://localhost:18199'};
 const sample={source:'old-origin',conversations:[{conv:{id:'c',currNode:'b'},messages:[
  {id:'a',role:'user',content:'hello',parent:null,children:['b','c']},
  {id:'b',role:'assistant',content:'answer',parent:'a',children:[],toolCalls:'[{"name":"test"}]',reasoningContent:'reasoning'},
  {id:'c',role:'assistant',content:'other branch',parent:'a',children:[],uiStatus:'pending'}]}]};
 const original=structuredClone(sample),a=await normalizeHistory(sample),b=await normalizeHistory(sample);
 assert.deepEqual(sample,original,'import must not modify its source');
 assert.equal(a.items[0].conv.id,b.items[0].conv.id,'repeat imports keep stable IDs');
 assert.equal(a.items[0].messages.length,3,'all branches survive');
 assert.equal(a.items[0].conv.currNode,a.items[0].messages[1].id);
 assert.equal(a.items[0].messages[1].toolCalls,sample.conversations[0].messages[1].toolCalls);
 assert.equal(a.items[0].messages[1].reasoningContent,'reasoning');assert(a.warnings>0);
 assert.equal(a.items[0].messages[2].uiStatus,'interrupted');
 assert.deepEqual(activeBranch(sample.conversations[0].messages,'b').map(m=>m.id),['a','b']);
 assert.deepEqual(activeBranch(sample.conversations[0].messages,'c').map(m=>m.id),['a','c']);
 assert.deepEqual(activeBranch([],null),[]);
 assert.throws(()=>activeBranch(sample.conversations[0].messages,'missing'),/missing/);
 const loop=structuredClone(sample);loop.conversations[0].messages[0].parent='b';
 await assert.rejects(()=>normalizeHistory(loop),/cycle/);
 assert.throws(()=>activeBranch(loop.conversations[0].messages,'b'),/cycle/);
 const missing=structuredClone(sample);missing.conversations[0].messages[1].parent='missing';
 await assert.rejects(()=>normalizeHistory(missing),/parent is missing/);
 const empty=await normalizeHistory({conversations:[{conv:{id:'empty',currNode:''},messages:[]}]});
 assert.equal(empty.items[0].conv.currNode,null);
 const poly=await normalizeHistory({format:'polystrata',source:'old-poly',messages:[
  {role:'user',text:'image',images:[{name:'missing.png'}]},
  {role:'assistant',text:'partial',reasoning:'thought',stopped:true}]});
 assert.equal(poly.items[0].messages[1].uiStatus,'interrupted');
 assert.equal(poly.items[0].messages[0].legacySource.images[0].name,'missing.png');assert(poly.warnings);
 const textFile={type:'text',name:'notes.md',size:4,content:'正文\n'};
 const withFile=await normalizeHistory({conversations:[{conv:{id:'files',currNode:'u'},messages:[
  {id:'u',role:'user',content:'Read this',parent:null,children:[],extra:[textFile]}]}]});
 assert.equal(withFile.warnings,0,'supported text attachments remain usable after import');
 assert.deepEqual(withFile.items[0].messages[0].extra,[textFile]);
 console.log('PASS history identity, branches, reasoning, original payloads, interruption and invalid graphs');
})().catch(error=>{console.error(error);process.exitCode=1;});
