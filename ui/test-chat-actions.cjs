// Model-free browser regression: last-turn actions must fork history, never append
// the superseded answer or lose the user's independent composer draft.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { expect } = require(path.join(path.dirname(require.resolve(process.env.PLAYWRIGHT_MODULE || 'playwright')), 'test'));
const directory = path.resolve(process.env.UI_DIST || path.join(__dirname, '../build/share/kvmem/ui'));
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2' };
const server = http.createServer((request, response) => {
  const filename = path.resolve(directory, '.' + decodeURIComponent(new URL(request.url, 'http://localhost').pathname));
  const target = filename === directory ? path.join(directory, 'index.html') : filename;
  if (!target.startsWith(directory + path.sep) || !fs.existsSync(target) || !fs.statSync(target).isFile()) { response.writeHead(404); return response.end(); }
  response.setHeader('Content-Type', types[path.extname(target)] || 'application/octet-stream');
  fs.createReadStream(target).pipe(response);
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {}) });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [], sent = [];
  let held = null, hold = false, fail = false;
  const ctx = { schema_version: 1, state: 'ready', instance: 'actions-test', backend: 'ninfer', model: 'fixture', effective: {}, capabilities: { vision: true }, limit: 8192 };
  const response = number => ({ contentType: 'text/event-stream', body: 'data: ' + JSON.stringify({ choices: [{ delta: { content: 'Answer ' + number, reasoning_content: 'Reason ' + number }, finish_reason: 'stop' }] }) + '\n\ndata: [DONE]\n\n' });
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/launcher/info', route => route.fulfill({ json: { token: 'fixture-token' } }));
  await page.route('**/api/chat/context', route => route.fulfill({ json: ctx }));
  await page.route('**/api/chat/requests/*', route => route.fulfill({ json: { state: 'running' } }));
  await page.route('**/api/chat/cancel', route => route.fulfill({ json: { cancelled: true } }));
  await page.route('**/api/chat/completions', async route => {
    sent.push(route.request().postDataJSON());
    if (hold) { held = route; return; }
    if (fail) return route.fulfill({ status: 500, json: { error: 'Fixture failure' } });
    await route.fulfill(response(sent.length));
  });
  const button = name => page.getByRole('button', { name, exact: true });
  const input = page.getByLabel('消息输入', { exact: true });
  const ready = async () => { const lastUser = page.locator('article.user').last(); await expect(lastUser.getByRole('button', { name: '重新生成', exact: true })).toBeEnabled(); await expect(lastUser.getByRole('button', { name: '重新编辑', exact: true })).toBeEnabled(); };
  const rows = () => page.evaluate(async () => {
    const request = indexedDB.open('kvmem-chat');
    const db = await new Promise((resolve, reject) => { request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
    try { return await new Promise((resolve, reject) => { const req = db.transaction('messages').objectStore('messages').getAll(); req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); }); }
    finally { db.close(); }
  });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await expect(page.getByText('fixture', { exact: true })).toBeVisible();
    await input.fill('First question'); await button('发送').click(); await ready();
    await input.fill('Second question'); await button('发送').click(); await ready();
    const original = await rows();
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.SCREENSHOT_DIR, 'user-message-actions.png'), fullPage: true });
    }
    await input.fill('Unsent draft');
    await button('重新生成').click(); await ready();
    assert.equal(sent.length, 3);
    assert.deepEqual(sent[2].messages, sent[1].messages, 'regeneration reuses only the original prompt prefix, including earlier reasoning');
    await expect(page.locator('article')).toHaveCount(4);
    await expect(page.locator('article').last()).toContainText('Answer 3');
    await expect(input).toHaveValue('Unsent draft');
    assert.equal((await rows()).length, 5, 'superseded answer remains stored');
    await page.reload(); await ready();
    await expect(page.locator('article')).toHaveCount(4);
    await expect(page.locator('article').last()).toContainText('Answer 3');

    await button('重新编辑').click();
    const editor = page.getByLabel('重新编辑', { exact: true });
    await expect(editor).toHaveValue('Second question');
    await editor.fill(''); await expect(button('保存并重新生成')).toBeDisabled();
    await editor.fill('Cancelled edit'); await editor.press('Escape');
    assert.equal(sent.length, 3); await expect(input).toHaveValue('Unsent draft');
    await expect(page.locator('article.user').last()).toContainText('Second question');
    await button('重新编辑').click(); await editor.fill('Revised question');
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.SCREENSHOT_DIR, 'desktop-edit.png'), fullPage: true });
    }
    await button('保存并重新生成').click(); await ready();
    assert.deepEqual(sent[3].messages, [...sent[1].messages.slice(0, -1), { role: 'user', content: 'Revised question' }]);
    await expect(input).toHaveValue('Unsent draft');
    await expect(page.locator('article')).toHaveCount(4);
    const stored = await rows();
    for (const old of original) assert.deepEqual(stored.find(m => m.id === old.id)?.content, old.content, 'old content is immutable');
    assert.equal(stored.length, 7);
    await page.reload(); await ready();
    await expect(page.locator('article.user').last()).toContainText('Revised question');
    await expect(page.locator('article')).toHaveCount(4);

    // A failed or cancelled generation can be retried without creating extra users.
    fail = true; await button('重新生成').click(); await ready();
    await expect(page.locator('article').last()).toContainText('生成失败');
    fail = false; hold = true; await button('重新生成').click();
    await expect(button('停止生成')).toBeVisible();
    await expect.poll(() => held !== null).toBe(true);
    assert.equal(await button('重新生成').count(), 0, 'no retry controls while streaming');
    await button('停止生成').click(); await ready();
    await expect(page.locator('article').last()).toContainText('已停止或中断');
    await held.fulfill(response(sent.length)).catch(() => {}); held = null; hold = false;
    await button('重新生成').click(); await ready();
    assert.deepEqual(sent.at(-1).messages, sent[3].messages);
    await expect(page.locator('article')).toHaveCount(4);

    // Image-only prompts retain their payload during retry/edit, independently of draft images.
    await button('新建对话').click();
    await page.locator('.composer-wrap input[type=file]').setInputFiles({ name: 'pixel.png', mimeType: 'image/png', buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPAkAAAAASUVORK5CYII=', 'base64') });
    await button('发送').click(); await ready();
    const imagePrompt = sent.at(-1).messages;
    await button('重新生成').click(); await ready();
    assert.deepEqual(sent.at(-1).messages, imagePrompt);
    await button('重新编辑').click(); await editor.fill('Describe the image');
    await button('保存并重新生成').click(); await ready();
    assert.equal(sent.at(-1).messages[0].content[0].text, 'Describe the image');
    assert.deepEqual(sent.at(-1).messages[0].content[1], imagePrompt[0].content[1]);
    await expect(page.locator('article')).toHaveCount(2);
    await page.setViewportSize({ width: 390, height: 844 });
    await button('重新编辑').click(); await expect(editor).toBeVisible();
    await expect(button('保存并重新生成')).toBeInViewport();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.SCREENSHOT_DIR, 'mobile-edit.png'), fullPage: true });
    await button('取消').click();
    await page.evaluate(() => window.postMessage({ type: 'kvmem-appearance', language: 'en' }, location.origin));
    await expect(button('Regenerate')).toBeEnabled();
    await button('Edit message').click();
    await expect(button('Save & regenerate')).toBeVisible(); await button('Cancel').click();
    await page.evaluate(() => window.postMessage({ type: 'kvmem-appearance', language: 'zh-CN' }, location.origin));
    await page.setViewportSize({ width: 1280, height: 900 });
    await button('新建对话').click();
    ctx.capabilities.vision = false;
    await page.reload();
    await expect(button('发送')).toBeDisabled();
    const drop = async (files, target = 'textarea[aria-label="消息输入"]') => {
      const transfer = await page.evaluateHandle(files => {
        const data = new DataTransfer();
        for (const file of files) data.items.add(new File([file.bytes ? new Uint8Array(file.bytes) : file.content], file.name, { type: file.type || '' }));
        return data;
      }, files);
      await page.locator(target).dispatchEvent('dragenter', { dataTransfer: transfer });
      await page.locator(target).dispatchEvent('dragover', { dataTransfer: transfer });
      const prevented = await page.locator(target).evaluate((node, dataTransfer) => {
        const event = new DragEvent('drop', { dataTransfer, bubbles: true, cancelable: true });
        node.dispatchEvent(event); return event.defaultPrevented;
      }, transfer);
      assert.equal(prevented, true, 'file drops cannot navigate away from chat');
      await transfer.dispose();
    };
    const countBeforeDrop = sent.length;
    const source = '你好\nconst x = "<script>alert(1)</script>";\n';
    await input.fill('Explain these files');
    await drop([{ name: 'notes.txt', content: 'reference text' }, { name: 'demo.ts', content: source }]);
    await expect(button('移除文件 demo.ts')).toBeVisible();
    await expect(input).toHaveValue('Explain these files');
    assert.equal(sent.length, countBeforeDrop, 'drop only stages attachments');
    await drop([{ name: 'bad.exe', content: 'binary' }]);
    await expect(page.getByRole('alert')).toContainText('仅支持');
    await drop([{ name: 'big.txt', content: 'x'.repeat(1024 * 1024 + 1) }]);
    await expect(page.getByRole('alert')).toContainText('1 MiB');
    await drop([{ name: 'image.png', content: 'x', type: 'image/png' }]);
    await expect(page.getByRole('alert')).toContainText('不支持图片');
    await drop([{ name: 'a.txt', content: 'a' }, { name: 'b.txt', content: 'b' }, { name: 'c.txt', content: 'c' }]);
    await expect(page.getByRole('alert')).toContainText('最多添加 4');
    await expect(page.locator('.composer-wrap .attachments button')).toHaveCount(2);
    await button('发送').click(); await ready();
    const filePrompt = sent.at(-1).messages;
    assert.equal(typeof filePrompt[0].content, 'string', 'non-vision model receives plain text');
    assert.ok(filePrompt[0].content.includes(source));
    assert.ok(filePrompt[0].content.includes('notes.txt'));
    await expect(page.locator('.text-attachment')).toHaveCount(2);
    await page.reload(); await ready();
    await expect(page.locator('.text-attachment')).toHaveCount(2);
    await button('重新生成').click(); await ready();
    assert.deepEqual(sent.at(-1).messages, filePrompt);
    await button('重新编辑').click();
    await button('移除文件 notes.txt').click();
    await drop([{ name: 'extra.py', content: 'print(42)' }], '#edit-last-message');
    await expect(button('移除文件 extra.py')).toBeVisible();
    await button('保存并重新生成').click(); await ready();
    assert.ok(sent.at(-1).messages[0].content.includes('print(42)'));
    assert.ok(!sent.at(-1).messages[0].content.includes('reference text'));
    await button('新建对话').click();
    // An attachment-only message and file picker follow the same path as a drop.
    await page.locator('.composer-wrap input[type=file]').setInputFiles({ name: 'only.json', mimeType: 'application/json', buffer: Buffer.from('{"ok":true}') });
    await expect(button('发送')).toBeEnabled();
    await button('发送').click(); await ready();
    assert.ok(sent.at(-1).messages[0].content.includes('{"ok":true}'));
    await page.setViewportSize({ width: 390, height: 844 });
    await drop([{ name: 'long-file-name-for-mobile-code-preview.ts', content: source }]);
    await expect(button('移除文件 long-file-name-for-mobile-code-preview.ts')).toBeVisible();
    await expect(button('发送')).toBeInViewport();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.SCREENSHOT_DIR, 'mobile-file-drop.png'), fullPage: true });
    // Reject drops during an active response without navigating or queuing them.
    hold = true; await button('发送').click(); await expect(button('停止生成')).toBeVisible();
    await expect.poll(() => held !== null).toBe(true);
    await drop([{ name: 'during.txt', content: 'must not be attached' }]);
    await expect(button('移除文件 during.txt')).toHaveCount(0);
    await button('停止生成').click(); await ready();
    await held.fulfill(response(sent.length)).catch(() => {}); held = null; hold = false;
    ctx.capabilities.vision = true;
    await page.setViewportSize({ width: 1280, height: 900 });
    await button('新建对话').click(); await page.reload();
    await expect(input).toBeVisible();
    await drop([
      { name: 'pixel.png', type: 'image/png', bytes: Array.from(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPAkAAAAASUVORK5CYII=', 'base64')) },
      { name: 'caption.md', content: 'Image context' },
    ]);
    await expect(button('移除图片 pixel.png')).toBeVisible();
    await expect(button('移除文件 caption.md')).toBeVisible();
    await button('发送').click(); await ready();
    assert.equal(sent.at(-1).messages[0].content[1].type, 'image_url');
    assert.ok(sent.at(-1).messages[0].content[0].text.includes('Image context'));
    await page.evaluate(() => {
      const original = File.prototype.arrayBuffer;
      File.prototype.arrayBuffer = async function () {
        await new Promise(resolve => { window.releaseFileRead = resolve; });
        File.prototype.arrayBuffer = original;
        return original.call(this);
      };
    });
    await drop([{ name: 'slow.txt', content: 'read completes before sending' }]);
    await expect(button('发送')).toBeDisabled();
    await expect(button('重新生成')).toBeDisabled();
    await page.evaluate(() => window.releaseFileRead());
    await expect(button('移除文件 slow.txt')).toBeVisible();
    await expect(button('发送')).toBeEnabled();
    assert.deepEqual(errors, []);
    console.log('PASS last-turn regeneration/edit, branching and reload, draft preservation, blank/cancel guards, failures, interruption, image-only input, English and mobile');
    console.log('PASS file drop/picker, text-only model, attachment-only prompt, edit/retry/reload persistence, invalid/oversize/count/vision/busy guards, mobile layout');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
