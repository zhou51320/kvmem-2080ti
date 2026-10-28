/* Linux UI contract against real export/start APIs and a model-free worker. */
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const {spawnSync}=require('node:child_process');
module.exports=async({browser,base,info,state})=>{
 let fixture;
 if(process.env.LINUX_UI_FIXTURE)fixture=JSON.parse(process.env.LINUX_UI_FIXTURE);
 else {
  const script=`import json,sys
from pathlib import Path
sys.path.insert(0,'tests')
from test_model_health import ninfer
from test_launcher_services import WORKER
root=Path(sys.argv[1]);model=root/'ui-test.ninfer';ninfer(model)
worker=root/'ui-worker';worker.write_text(WORKER);worker.chmod(0o700)
print(json.dumps(dict(model=str(model),worker=str(worker))))`;
  const run=spawnSync(process.env.PYTHON||'python',['-c',script,state],{cwd:path.resolve(__dirname,'..'),encoding:'utf8',windowsHide:true});
  assert.equal(run.status,0,run.stderr);fixture=JSON.parse(run.stdout);
 }
 const gpu=fixture.gpu||{uuid:'GPU-00000000-0000-0000-0000-000000000001',name:'Model-free UI fixture (no GPU loaded)'};
 const context=await browser.newContext({viewport:{width:1366,height:1000}}),page=await context.newPage(),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/api/launcher/info',route=>route.fulfill({json:{...info,gpus:[gpu],draft:{schema_version:1,parameters:{...info.draft.parameters,
   Backend:'ninfer',Model:fixture.model,Worker:fixture.worker,Port:18495,ModelAlias:'literal $HOME ; test',
   Vision:false,SpeculativeMode:'none',DraftTokens:0,MtpDrafts:0,...(fixture.parameters||{})}}}}));
 try {
  await page.goto(base+'/monitor/?lang=en#config');
  const field=name=>page.locator(`[data-param=${name}]`);
  await field('Backend').waitFor();await field('Backend').selectOption('ninfer');
  assert.equal(await page.getByRole('button',{name:'Generate and start',exact:true}).count(),1);
  assert.equal(await page.locator('[data-browse-model]').isVisible(),false);
  await field('Model').fill(fixture.model);
  assert.equal(await field('Worker').inputValue(),fixture.worker);
  await page.locator('[data-gpu-choice]').first().check();
  await page.locator('[data-action=export]').click();
  await page.locator('[data-export-profile]').fill('linux-ui-browser');
  await page.locator('[data-export-generate]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-export-start]').disabled).catch(async error=>{
    throw new Error(error.message+'; export response: '+await page.locator('[data-export-result]').textContent());
  });
  const text=await page.locator('[data-export-result]').textContent();
  assert.match(text,/\/revisions\/[a-f0-9]{32}\/run\.sh/);
  assert.match(text,/kvmem-linux-ui-browser\.service/);
  await page.locator('[data-export-start]').click();
  await page.waitForFunction(()=>document.querySelector('[data-runtime-state]').dataset.state==='ready',null,{timeout:fixture.real_model?180000:30000});
  const live=await (await fetch(base+'/api/launcher/status')).json();
  assert.equal(live.owner,'temporary');assert.equal(live.generated_revision.profile_id,'linux-ui-browser');
  assert.equal(live.active_config.parameters.ModelAlias,'literal $HOME ; test');
  if(fixture.real_model){
    const payload={model:'literal $HOME ; test',messages:[{role:'user',content:'Reply with only the two letters OK.'}],temperature:0,max_tokens:16};
    const context=await (await fetch(base+'/api/chat/context')).json();
    const response=await fetch(base+'/api/chat/completions',{method:'POST',headers:{'Content-Type':'application/json',
      'X-KVMem-Token':info.token,'X-KVMem-Instance':live.instance,'X-KVMem-Request':'linux-ui-gpu-smoke'},body:JSON.stringify(payload)});
    const stream=await response.text();assert.equal(response.status,200,stream);
    assert.match(stream,/data: \[DONE\]/);
    const chunks=stream.split('\n').filter(line=>line.startsWith('data: {')).map(line=>JSON.parse(line.slice(6)));
    const answer=chunks.flatMap(chunk=>chunk.choices||[]).map(choice=>choice.delta?.content||'').join('');
    assert.match(answer,/OK/i,stream);
    const status=await (await fetch(base+'/api/launcher/status')).json();
    const logs=await (await fetch(base+'/api/launcher/logs')).json();
    if(fixture.evidence)fs.writeFileSync(fixture.evidence,JSON.stringify({phase:'ui',gpu:gpu.uuid,payload,answer,stream,context,status,logs},null,2));
    console.log('PASS: '+gpu.name+' real-model chat through the UI proxy: '+answer);
  }
  await page.locator('[data-action=stop]').click();
  await page.waitForFunction(()=>document.querySelector('[data-runtime-state]').dataset.state==='stopped');
  await page.locator('[data-services-open]').click();
  await page.getByText('No services are registered. Generate a launch script first.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Close',exact:true}).click();
  let disconnected=false;
  await page.route('**/api/launcher/services',route=>route.fulfill({json:{capabilities:{available:true,user:true,linger:true},
    selected:disconnected?null:{profile:'removed-service',scope:'system'},
    services:[{profile_id:'removed-service',scope:'system',error:'Installed unit is missing'}]}}));
  await page.route('**/api/launcher/service',route=>{
    assert.deepEqual(route.request().postDataJSON(),{action:'disconnect',profile_id:'removed-service',scope:'system'});
    disconnected=true;return route.fulfill({json:{disconnected:true}});
  });
  await page.locator('[data-services-open]').click();
  await page.locator('[data-service-action=disconnect]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-service-action=disconnect]'));
  assert.ok(disconnected);
  await page.getByRole('button',{name:'Close',exact:true}).click();
  await page.locator('[data-language-select]').selectOption('zh-CN');
  assert.equal(await page.getByRole('button',{name:'生成并启动',exact:true}).count(),1);
  await page.setViewportSize({width:390,height:900});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'mobile layout should fit');
  assert.deepEqual(errors,[]);
  console.log('PASS: Linux paths, script export, exact revision start/stop, source status, service management, bilingual and mobile UI');
 }finally{await context.close();}
};
