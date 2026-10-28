// Isolated browser regression for request-scoped metrics; no model required.
const fs=require('fs'),path=require('path'),http=require('http'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base=path.resolve(process.env.UI_DIST || path.join(__dirname,'../build/share/kvmem/ui'));
const server=http.createServer((req,res)=>{const p=path.join(base,req.url==='/'?'index.html':decodeURIComponent(req.url)); if(!fs.existsSync(p)){res.writeHead(404);return res.end();}res.setHeader('Content-Type',p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':'text/html');fs.createReadStream(p).pipe(res);});
(async()=>{
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM ? {executablePath:process.env.CHROMIUM} : {})});
try {
 const page=await browser.newPage();let completions=[],polls=[];
 await page.route('**/api/launcher/info',r=>r.fulfill({json:{token:'test',status:{state:'ready',instance:'one'}}}));
 await page.route('**/api/chat/context',r=>r.fulfill({json:{state:'ready',instance:'one',backend:'ninfer',model:'audit-model',context:8192,limit:8192,effective:{},capabilities:{}}}));
 await page.route('**/api/chat/cancel',r=>r.fulfill({json:{cancelled:true}}));
 await page.route('**/api/chat/completions',r=>{completions.push(r);});
 await page.route('**/api/chat/requests/*',r=>{polls.push(r);});
 await page.goto('http://127.0.0.1:'+server.address().port);
 await page.getByText('audit-model',{exact:true}).waitFor();
 const send=async()=>{await page.getByLabel('消息输入',{exact:true}).fill('test');await page.getByRole('button',{name:/^发送/}).click();};
 const until=async(fn)=>{for(let n=0;n<100;n++){if(fn())return;await new Promise(r=>setTimeout(r,50));}throw Error('timeout');};
 await send();await until(()=>polls.length>0 && completions.length>0);
 const oldId=completions[0].request().headers()['x-kvmem-request'];
 await completions[0].fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({choices:[{delta:{content:'done'},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n'});
 await page.getByRole('button',{name:/^发送/}).waitFor();
 await send();await until(()=>completions.length>1);
 const newId=completions[1].request().headers()['x-kvmem-request'];
 assert.notEqual(oldId,newId);
 const lateResponse=page.waitForResponse(r=>r.url().endsWith('/api/chat/requests/'+oldId));
 await polls[0].fulfill({json:{id:oldId,state:'thinking',started_at_ms:Date.now()-99000,metrics:{decode_tps:123.4,decode_tps_mean:98.7}}});
 await (await lateResponse).finished();await page.waitForTimeout(100);
 assert.doesNotMatch(await page.locator('.live-line').textContent(),/123.4|98.7|99s/,'ignore old request metrics');
 await until(()=>polls.some(r=>r.request().url().endsWith(newId)));
 const current=polls.find(r=>r.request().url().endsWith(newId));
 await current.fulfill({json:{id:newId,state:'thinking',started_at_ms:Date.now(),metrics:{decode_tps:12.3,decode_tps_mean:9.8}}});
 await page.waitForFunction(()=>document.querySelector('.live-line')?.textContent.includes('12.3'));
 let n=polls.length;await until(()=>polls.length>n);
 await polls[n].fulfill({json:{id:oldId,state:'thinking',metrics:{decode_tps:999}}});
 await page.waitForTimeout(100);
 assert.doesNotMatch(await page.locator('.live-line').textContent(),/999/,'reject mismatched response ID');
 n=polls.length;await until(()=>polls.length>n);await polls[n].abort();
 n=polls.length;await until(()=>polls.length>n);
 await polls[n].fulfill({json:{id:newId,state:'answering',metrics:{decode_tps:23.4}}});
 await page.waitForFunction(()=>document.querySelector('.live-line')?.textContent.includes('23.4'));
 console.log('PASS: delayed previous request, mismatched response ID, current metrics and recovery after failed poll');
}finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
