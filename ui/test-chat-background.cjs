// Real local SSE transport with controllable chunks: switching views must never
// cancel the stream or redirect its data, errors or lastModified into another chat.
const assert = require('node:assert/strict'), fs = require('node:fs'), http = require('node:http'), path = require('node:path');
const modulePath = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = require(modulePath);
const { expect } = require(path.join(path.dirname(require.resolve(modulePath)), 'test'));
const directory = path.resolve(process.env.UI_DIST || path.join(__dirname, '../build/share/kvmem/ui'));
const requests = [], cancellations = [];
let hold = false;
const json = (res, data) => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); };
const chunk = (request, text) => request.response.write('data: ' + JSON.stringify({ choices: [{ delta: { content: text } }] }) + '\n\n');
const finish = request => request.response.end('data: ' + JSON.stringify({ choices: [{ delta: {}, finish_reason: 'stop' }] }) + '\n\ndata: [DONE]\n\n');
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/api/launcher/info') return json(res, { token: 'test' });
  if (url.pathname === '/api/chat/context') return json(res, { state: 'ready', instance: 'fixture', backend: 'ninfer', model: 'fixture', effective: {}, capabilities: {}, limit: 8192 });
  if (url.pathname.startsWith('/api/chat/requests/')) return json(res, { id: url.pathname.split('/').pop(), state: 'answering', metrics: { decode_tps: 12.3 } });
  if (req.method === 'POST') {
    let body = ''; for await (const value of req) body += value;
    if (url.pathname === '/api/chat/cancel') {
      const { id } = JSON.parse(body); cancellations.push(id);
      requests.find(request => request.id === id)?.response.end();
      return json(res, { cancelled: true });
    }
    if (url.pathname === '/api/chat/completions') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
      const request = { id: req.headers['x-kvmem-request'], response: res, body: JSON.parse(body), closed: false };
      requests.push(request); res.on('close', () => { request.closed = true; });
      chunk(request, hold ? 'Live first' : 'Saved answer');
      if (!hold) finish(request);
      return;
    }
  }
  const file = path.resolve(directory, '.' + (url.pathname === '/' ? '/index.html' : decodeURIComponent(url.pathname)));
  if (!file.startsWith(directory + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { res.writeHead(404); return res.end(); }
  const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
  res.setHeader('Content-Type', types[path.extname(file)] || 'application/octet-stream'); fs.createReadStream(file).pipe(res);
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {}) });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } }), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const button = name => page.getByRole('button', { name, exact: true });
  const input = page.getByLabel('消息输入', { exact: true });
  const history = name => page.locator('.history-item').filter({ hasText: name });
  const done = () => expect(button('重新生成')).toBeEnabled();
  const stored = () => page.evaluate(async () => {
    const req = indexedDB.open('kvmem-chat');
    const db = await new Promise((resolve, reject) => { req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); });
    try {
      const rows = name => new Promise((resolve, reject) => { const req = db.transaction(name).objectStore(name).getAll(); req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); });
      return { conversations: await rows('conversations'), messages: await rows('messages') };
    } finally { db.close(); }
  });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await expect(page.getByText('fixture', { exact: true })).toBeVisible();
    await input.fill('History B'); await button('发送').click(); await done();
    const original = await stored(), other = original.conversations.find(c => c.name === 'History B');
    await input.fill('B unsent draft');
    await page.locator('.composer-wrap input[type=file]').setInputFiles({ name: 'draft.txt', mimeType: 'text/plain', buffer: Buffer.from('draft attachment') });
    await button('新建对话').click();
    hold = true;
    await input.fill('Generating A'); await button('发送').click();
    await expect(page.locator('.history-row.generating')).toContainText('Generating A');
    await expect(page.locator('article').last()).toContainText('Live first');
    const running = requests.at(-1);
    await expect(history('History B')).toBeEnabled(); await history('History B').click();
    await expect(page.locator('article').last()).toContainText('Saved answer');
    await expect(button('停止生成')).toHaveCount(0);
    await expect(page.locator('.live-line')).toHaveCount(0);
    await expect(button('发送')).toBeDisabled();
    await expect(button('返回生成中的会话')).toBeVisible();
    chunk(running, ' middle');
    await page.waitForTimeout(150);
    assert.equal(running.closed, false); assert.ok(!cancellations.includes(running.id));
    await expect(page.locator('article')).toHaveCount(2);
    assert.ok(!(await page.locator('.messages').textContent()).includes('Live first'));
    await button('返回生成中的会话').click();
    await expect(page.locator('article').last()).toContainText('Live first middle');
    await expect(button('停止生成')).toBeVisible();
    await expect(page.getByText('已停止或中断', { exact: true })).toHaveCount(0);
    await history('History B').click();
    await expect(input).toHaveValue('B unsent draft');
    await expect(button('移除文件 draft.txt')).toBeVisible();
    if (process.env.SCREENSHOT_DIR) { fs.mkdirSync(process.env.SCREENSHOT_DIR, { recursive: true }); await page.screenshot({ path: path.join(process.env.SCREENSHOT_DIR, 'background-history.png'), fullPage: true }); }
    chunk(running, ' final'); finish(running);
    await expect(page.locator('.history-row.generating')).toHaveCount(0);
    await expect(button('返回生成中的会话')).toHaveCount(0);
    await expect(page.locator('.history-row.selected')).toContainText('History B');
    await expect(page.locator('article').last()).toContainText('Saved answer');
    await expect(input).toHaveValue('B unsent draft');
    const after = await stored();
    assert.deepEqual(after.conversations.find(c => c.id === other.id), other, 'background completion must not touch the viewed conversation');
    assert.deepEqual(after.messages.filter(m => m.convId === other.id), original.messages.filter(m => m.convId === other.id));
    const owner = after.conversations.find(c => c.name === 'Generating A');
    assert.equal(after.messages.find(m => m.id === owner.currNode).content, 'Live first middle final');
    await history('Generating A').click(); await done();
    await expect(page.locator('article').last()).toContainText('Live first middle final');
    await page.reload(); await done(); await expect(page.locator('article').last()).toContainText('Live first middle final');

    // An error while browsing B stays attached to A, including on returning to A.
    await button('重新生成').click(); await expect(button('停止生成')).toBeVisible();
    await expect.poll(() => requests.length).toBe(3);
    await history('History B').click();
    requests.at(-1).response.end('data: '+JSON.stringify({error:'Owner-only error'})+'\n\n');
    await expect(page.locator('.history-row.generating')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
    await history('Generating A').click(); await done();
    await expect(page.getByRole('alert')).toContainText('Owner-only error');
    await expect(page.locator('article').last()).toContainText('生成失败');

    // Mobile history selection retains its running marker, and stop still targets A.
    await button('重新生成').click(); await expect(button('停止生成')).toBeVisible();
    await expect.poll(() => requests.length).toBe(4);
    const stopped = requests.at(-1);
    await page.setViewportSize({ width: 390, height: 844 });
    await button('聊天历史').click();
    await expect(page.locator('.history-row.generating')).toBeVisible();
    await history('History B').click(); await expect(button('返回生成中的会话')).toBeVisible();
    await button('返回生成中的会话').click();
    await expect(page.locator('article').last()).toContainText('Live first');
    await button('停止生成').click(); await done();
    await expect(page.locator('.history-row.generating')).toHaveCount(0);
    await expect(page.locator('article').last()).toContainText('已停止或中断');
    assert.ok(cancellations.includes(stopped.id));
    assert.deepEqual(errors, []);
    console.log('PASS live stream survives desktop/mobile history switching; owner-only content/errors/metadata; draft attachments; highlight lifecycle; reload; return and stop');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => { for (const request of requests) request.response.end(); server.close(); });
