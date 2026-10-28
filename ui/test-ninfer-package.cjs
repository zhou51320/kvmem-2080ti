/* A package's backend list governs the real controller and launch UI. No model is loaded. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'kvmem-ninfer-package-'));
const backendRoot = path.join(temporary, 'package');
const scripts = path.join(backendRoot, 'scripts', 'windows');
fs.mkdirSync(scripts, {recursive:true});
fs.copyFileSync(path.join(root, 'scripts/windows/start-backend.ps1'), path.join(scripts, 'start-backend.ps1'));
fs.copyFileSync(path.join(root, 'scripts/speculative-capabilities.json'), path.join(backendRoot, 'scripts/speculative-capabilities.json'));
fs.writeFileSync(path.join(backendRoot, 'console-build.json'), JSON.stringify({supported_backends:['ninfer']}));
const bundledWorker=path.join(backendRoot,'workers','ninfer','ninfer-kvmem-server.exe');
fs.mkdirSync(path.dirname(bundledWorker),{recursive:true});fs.writeFileSync(bundledWorker,'fixture');
const port = Number(process.env.CONSOLE_TEST_PORT || 18447);
const base = `http://127.0.0.1:${port}`;
const child = spawn(process.env.PYTHON || 'python', ['-B', path.join(root, 'scripts/local-console.py'),
    '--backend-root', backendRoot, '--state-dir', path.join(temporary, 'state'), '--port', String(port),
    '--prefer-bundled-worker'],
    {windowsHide:true});
let output = '', browser;
child.stderr.on('data', data => output += data);
child.stdout.on('data', data => output += data);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function ready() {
    let lastFailure = '';
    for (let attempt = 0; attempt < 300; ++attempt) {
        try {
            const response = await fetch(base + '/api/launcher/info');
            if (response.ok) return response.json();
            lastFailure = `${response.status}: ${await response.text()}`;
        } catch (error) { lastFailure = error.message; }
        if (child.exitCode !== null) throw Error(output);
        await delay(100);
    }
    throw Error('Console startup timed out: ' + lastFailure + '\n' + output);
}
(async () => {
    const info = await ready();
    assert.deepEqual(info.backends, ['ninfer']);
    assert.equal(info.prefer_bundled_worker, true);
    assert.equal(info.workers.ninfer.path, bundledWorker);
    assert.equal(info.draft.parameters.Backend, 'ninfer');
    const refused = await fetch(base + '/api/launcher/preview', {method:'POST',
        headers:{'Content-Type':'application/json', 'X-KVMem-Token':info.token},
        body:JSON.stringify({schema_version:1,parameters:{Backend:'llamacpp'}})});
    assert.equal(refused.status, 400);
    browser = await chromium.launch({headless:true,args:['--disable-gpu'],
        ...(process.env.CHROMIUM ? {executablePath:process.env.CHROMIUM} : {})});
    const context = await browser.newContext({viewport:{width:1440,height:900}});
    await context.addInitScript(() => localStorage.setItem('kvmem-launch-draft-v2', JSON.stringify({
        version:2,parameters:{Backend:'llamacpp',Model:'old.gguf'},raw:{}})));
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/monitor/#config');
    await page.locator('[data-param=Backend]').waitFor();
    assert.deepEqual(await page.locator('[data-param=Backend] option').evaluateAll(options => options.map(o => o.value)), ['ninfer']);
    assert.equal(await page.locator('[data-param=Backend]').inputValue(), 'ninfer');
    assert.notEqual(await page.locator('[data-param=Model]').inputValue(), 'old.gguf');
    const staleContext = await browser.newContext({viewport:{width:1440,height:900}});
    await staleContext.addInitScript(() => localStorage.setItem('kvmem-launch-draft-v2', JSON.stringify({
        version:2,parameters:{Backend:'ninfer',Model:'old.ninfer',Worker:'C:/old/package/bin/ninfer-serve.exe',StageLayers:[]},raw:{}})));
    const stalePage=await staleContext.newPage();
    await stalePage.goto(base+'/monitor/#config');await stalePage.locator('[data-param=Worker]').waitFor({state:'attached'});
    assert.equal(await stalePage.locator('[data-param=Worker]').inputValue(),bundledWorker);
    assert.equal(await stalePage.locator('[data-param=Model]').inputValue(),'old.ninfer');
    await staleContext.close();
    for (const language of ['zh-CN','en']) {
        await page.locator('[data-language-select]').selectOption(language);
        assert.deepEqual(await page.locator('[data-param=Backend] option').evaluateAll(options => options.map(o => o.value)), ['ninfer']);
    }
    for (const width of [1440,390]) {
        await page.setViewportSize({width,height:900});
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await page.setViewportSize({width:1440,height:900});
    if (process.env.SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
        await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'ninfer-only-en.png'),fullPage:true});
    }
    assert.deepEqual(errors, []);
    console.log('PASS: NInfer-only API/UI, old-draft fallback, both languages and responsive layout');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
    if (browser) await browser.close();
    if (child.exitCode === null) { const exited = once(child,'exit'); child.kill(); await exited; }
    assert.equal(path.dirname(path.resolve(temporary)),path.resolve(os.tmpdir()));
    assert.ok(path.basename(temporary).startsWith('kvmem-ninfer-package-'));
    fs.rmSync(temporary,{recursive:true,force:true});
});
