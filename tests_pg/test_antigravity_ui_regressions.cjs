// Independent UI & async race regressions for Unified Storage and CAD (Node + Headless Chrome).
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const cp = require('node:child_process');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const appSrc = fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8');
const htmlSrc = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');

async function testUploadDeleteAndRevokePolling() {
  const start = appSrc.indexOf('async function loadUploadJobStatus(){');
  const end = appSrc.indexOf('async function aiUpload(', start);
  assert(start > 0 && end > start);

  const note = { textContent: '', children: [], append(x) { this.children.push(x); } };
  const state = { me: { id: 'engineer', csrf: 'c1' }, uploadDocId: 'upload-11111111111111111111111111111111', sessionSerial: 7 };
  let resolver;
  let nextResponse = new Promise(r => { resolver = r; });
  const request = () => nextResponse;
  const ctx = {
    state,
    request,
    el: (tag, className, text) => ({ tag, className, text }),
    $: id => { assert.equal(id, 'ai-upload-result'); return note; },
  };
  vm.runInNewContext(appSrc.slice(start, end) + '\nthis.poll = loadUploadJobStatus;', ctx);

  // 1. Delete clears uploadDocId while poll is in flight -> late 'indexed' response must NOT append
  const inflight = ctx.poll();
  if (state.uploadDocId === 'upload-11111111111111111111111111111111') state.uploadDocId = null;
  note.textContent = 'Файл и его текст удалены.';
  resolver({ index_status: 'indexed' });
  await inflight;
  assert.equal(note.children.length, 0, 'Deleted upload poll must not append indexed banner');
  assert.equal(state.uploadDocId, null);

  // 2. Transient 503 keeps uploadDocId for retry, whereas 404 (revoked/deleted) stops polling
  state.uploadDocId = 'upload-22222222222222222222222222222222';
  const err503 = new Error('UNAVAILABLE');
  err503.status = 503;
  nextResponse = Promise.reject(err503);
  await ctx.poll();
  assert.equal(state.uploadDocId, 'upload-22222222222222222222222222222222', '503 must remain retryable');

  const err404 = new Error('DOCUMENT_NOT_FOUND');
  err404.status = 404;
  nextResponse = Promise.reject(err404);
  await ctx.poll();
  assert.equal(state.uploadDocId, null, '404 must terminate stale/revoked upload polling');

  const err403 = new Error('FORBIDDEN');
  err403.status = 403;
  state.uploadDocId = 'upload-33333333333333333333333333333333';
  nextResponse = Promise.reject(err403);
  await ctx.poll();
  assert.equal(state.uploadDocId, null, '403 must terminate revoked upload polling');
  console.log('PASS Antigravity upload polling: delete race fenced, 503 retries, 403/404 stop infinite polling');
}

async function testHeadlessChromeUiRegressions() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ag-ui-chrome-'));
  const chrome = cp.spawn(CHROME, [
    '--headless=new', '--no-first-run', '--no-default-browser-check',
    '--disable-gpu', '--disable-extensions', '--remote-debugging-port=0',
    '--window-size=1280,800', '--user-data-dir=' + profile, 'about:blank',
  ], { windowsHide: true, stdio: 'ignore' });

  let ws, seq = 1;
  const pending = new Map();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const cmd = (method, params = {}) => new Promise((resolve, reject) => {
    const id = seq++;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout ' + method)); }, 15000);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const js = async expr => {
    const r = await cmd('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error('JS exception: ' + JSON.stringify(r.exceptionDetails).slice(0, 500));
    return r.result.value;
  };

  try {
    let port = 0;
    for (let i = 0; i < 100; i++) {
      const f = path.join(profile, 'DevToolsActivePort');
      if (fs.existsSync(f)) {
        port = Number(fs.readFileSync(f, 'utf8').split(/\r?\n/)[0]);
        if (port) break;
      }
      await sleep(100);
    }
    assert(port > 0, 'Chrome DevTools port not found');
    const tabs = await (await fetch('http://127.0.0.1:' + port + '/json')).json();
    const tab = tabs.find(t => t.type === 'page');
    ws = new WebSocket(tab.webSocketDebuggerUrl.replace('localhost', '127.0.0.1'));
    await new Promise((res, rej) => { ws.addEventListener('open', res, { once: true }); ws.addEventListener('error', rej, { once: true }); });
    ws.addEventListener('message', ev => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) {
        const entry = pending.get(msg.id);
        pending.delete(msg.id);
        clearTimeout(entry.timer);
        msg.error ? entry.reject(new Error(msg.error.message)) : entry.resolve(msg.result);
      }
    });
    await cmd('Runtime.enable');
    await cmd('Page.enable');

    // Serve actual shipped index.html + style.css + design-preview.css + assets from ephemeral 127.0.0.1 loopback (Secure Context)
    const mime = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
    const sanitizedHtml = htmlSrc.replace(/<script[^>]*src="\/app\.js"[^>]*><\/script>/g, '');
    const server = http.createServer((req, res) => {
      const cleanUrl = (req.url || '/').split('?')[0];
      if (cleanUrl === '/') {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(sanitizedHtml);
        return;
      }
      const fp = path.join(ROOT, cleanUrl.replace(/^\/+/, ''));
      if (fp.startsWith(ROOT) && fs.existsSync(fp) && fs.statSync(fp).isFile()) {
        res.writeHead(200, { 'Content-Type': mime[path.extname(fp)] || 'application/octet-stream' });
        res.end(fs.readFileSync(fp));
      } else {
        res.writeHead(204);
        res.end();
      }
    });
    await new Promise(r => server.listen(0, '127.0.0.1', r));
    const srvPort = server.address().port;
    try {
      await cmd('Page.navigate', { url: `http://127.0.0.1:${srvPort}/` });
      for (let wait = 0; wait < 100; wait++) {
        const ready = await js(`document.readyState === 'complete' && document.styleSheets.length >= 3`).catch(() => false);
        if (ready) break;
        await sleep(100);
      }

    // Check semantic HTML & accessibility invariants on real index.html
    const a11y = await js(`({
      lang: document.documentElement.lang,
      positiveTabindex: document.querySelectorAll('[tabindex]:not([tabindex="0"]):not([tabindex="-1"])').length,
      dialogs: document.querySelectorAll('dialog').length,
      targetHintLive: document.getElementById('private-kit-target-hint')?.getAttribute('aria-live')
    })`);
    assert.equal(a11y.lang, 'ru');
    assert.equal(a11y.positiveTabindex, 0);
    assert.equal(a11y.dialogs, 2);
    assert.equal(a11y.targetHintLive, 'polite');

    // Mock fetch to evaluate full app.js in headless Chrome against deterministic role states
    await js(`
      const origFetch = window.fetch.bind(window);
      window.__mockRole = 'technologist';
      window.__mockWritable = [];
      window.__deleteStatus = 404;
      window.__alerts = [];
      window.confirm = () => true;
      window.alert = msg => { window.__alerts.push(String(msg)); };
      window.__savedKits = [{ kit_id: 'kit-00000000000000000000000000000001', part: 'focuser_drawtube', latest_revision: 1 }];
      window.__savedCalls = [];
      window.fetch = async (url, opts = {}) => {
        const u = String(url);
        if (u.includes('/assets/')) return origFetch(url, opts);
        if (u.endsWith('/unified/api/me')) {
          return new Response(JSON.stringify({ id: window.__mockRole, role: 'Технолог', scopes: ['engineering', 'finance'], csrf: 'csrf-1' }), { status: 200 });
        }
        if (u.endsWith('/unified/api/upload-capabilities')) {
          return new Response(JSON.stringify({ enabled: window.__mockWritable.length > 0, writable_scopes: window.__mockWritable, max_bytes: 8388608 }), { status: 200 });
        }
        if (u.endsWith('/unified/api/ai/status')) {
          return new Response(JSON.stringify({ status: 'stopped', message: 'Модель выключена.' }), { status: 200 });
        }
        if (u.includes('/unified/api/documents') || u.includes('/unified/api/search')) {
          return new Response(JSON.stringify({
            items: [{
              id: 'upload-99999999999999999999999999999999',
              title: 'Паспорт насоса Н-125 (очень_длинное_наименование_без_пробелов_АБВГ.712345.004-02)',
              designation: 'АБВГ.712345.004-02-ИСПОЛНЕНИЕ-2026',
              type: 'uploaded',
              revision: '1',
              snippet: 'Испытательное давление 125 бар.',
              has_ocr: true
            }],
            total: 1
          }), { status: 200 });
        }
        if (u.includes('/unified/api/metrics')) {
          return new Response(JSON.stringify({ items: [{ order: 'Заказ №104', planned: 120000, actual: 135000, variance: 15000 }] }), { status: 200 });
        }
        if (u.includes('/unified/api/uploads/') && opts.method === 'DELETE') {
          return new Response(JSON.stringify({ detail: 'DOCUMENT_NOT_FOUND' }), { status: window.__deleteStatus });
        }
        if (u.includes('/unified/api/document?id=')) {
          return new Response(JSON.stringify({
            id: 'upload-99999999999999999999999999999999',
            title: 'Паспорт насоса Н-125',
            designation: 'Н-125',
            type: 'uploaded',
            revision: '1',
            text: 'Испытательное давление 125 бар.',
            can_delete: window.__mockWritable.includes('engineering')
          }), { status: 200 });
        }
        if (u.endsWith('/unified/api/ask')) {
          return new Response(JSON.stringify({
            answer: 'Испытательное давление 125 бар [1]',
            mode: 'local_llm_advisory',
            citations: [1],
            sources: [{
              number: 1,
              doc_id: 'upload-99999999999999999999999999999999',
              title: 'Паспорт насоса Н-125',
              page: 2,
              excerpt: 'Испытательное давление 125 бар.',
              extraction_method: 'ocr',
              cited: true
            }],
            citation_warning: false,
            verification_required: true
          }), { status: 200 });
        }
        if (u.includes('/revisions')) {
          return new Response(JSON.stringify({
            kit_id: window.__savedKits[0].kit_id,
            part: window.__savedKits[0].part,
            revisions: [{
              revision_no: 2,
              report: {
                sources: [
                  { role: 'bom', name: 'materials_bom_specification_2026.csv' },
                  { role: 'drawing', name: 'focuser_drawtube_assembly_drawing_rev2.pdf' },
                  { role: 'model', name: 'focuser_drawtube_assembly_3d_model_rev2.STEP' }
                ]
              }
            }]
          }), { status: 200 });
        }
        if (u.endsWith('/unified/api/engineering/kits') && (!opts.method || opts.method === 'GET')) {
          return new Response(JSON.stringify({ items: window.__savedKits, manufacturing_authorization: false }), { status: 200 });
        }
        if (u.endsWith('/unified/api/engineering/kits') && opts.method === 'POST') {
          window.__savedCalls.push(opts.headers);
          return new Response(JSON.stringify({ kit_id: window.__savedKits[0].kit_id, revision_id: 'kitrev-1', revision_no: 2, reused: false, part: 'focuser_drawtube', sources: [] }), { status: 201 });
        }
        return new Response(JSON.stringify({ items: [], sources: 0, bom_lines: 0, links: 0, groups: [], pdf: 0, recognized_title_blocks: 0, valid_cad_breps: 0, step: 0, state: 'NOT_FOUND', checks: [], candidates: [] }), { status: 200 });
      };
    `);
    await js(appSrc);
    await sleep(150);

    // Verify DEF-05: Read-only role disables upload, try-sample, AND hero demo-run; trySampleDemo & aiAsk keep them disabled!
    await js(`navigate('assistant')`);
    await sleep(100);
    const roInitial = await js(`({
      upload: document.getElementById('ai-upload').disabled,
      sample: document.getElementById('ai-try-sample').disabled,
      hero: document.getElementById('ai-demo-run').disabled
    })`);
    assert.deepEqual(roInitial, { upload: true, sample: true, hero: true }, 'Read-only role must disable all three upload triggers');

    await js(`trySampleDemo(false)`);
    await js(`document.getElementById('ai-question').value = 'Проверка перегрева'; aiAsk()`);
    await sleep(100);
    const roAfterAsk = await js(`({
      upload: document.getElementById('ai-upload').disabled,
      sample: document.getElementById('ai-try-sample').disabled,
      hero: document.getElementById('ai-demo-run').disabled
    })`);
    assert.deepEqual(roAfterAsk, { upload: true, sample: true, hero: true }, 'Read-only upload buttons must stay disabled after trySampleDemo and aiAsk');

    // Verify DEF-12: Unified 'upload-...' OCR source renders 'Открыть страницу PDF' and targets /unified/api/page-preview
    const scanButtonExists = await js(`!!document.querySelector('#ai-sources .scan-open')`);
    assert.equal(scanButtonExists, true, 'Unified upload-... OCR source must render scan preview button');
    await js(`document.querySelector('#ai-sources .scan-open').click()`);
    const scanState = await js(`({
      open: document.getElementById('scan-preview-modal').open,
      src: document.getElementById('scan-preview-image').getAttribute('src'),
      overscroll: getComputedStyle(document.getElementById('scan-preview-modal')).overscrollBehavior
    })`);
    assert.equal(scanState.open, true);
    assert.equal(scanState.src, '/unified/api/page-preview?id=upload-99999999999999999999999999999999&page=2');
    assert.equal(scanState.overscroll, 'contain');
    await js(`document.getElementById('scan-preview-modal').close()`);

    // Verify DEF-13a: Read-only role opening uploaded document sees download link but NOT #modal-delete
    await js(`openDocument('upload-99999999999999999999999999999999')`);
    await sleep(50);
    const roModal = await js(`({
      open: document.getElementById('document-modal').open,
      downloadHidden: document.getElementById('modal-download').hidden,
      deleteHidden: document.getElementById('modal-delete').hidden,
      overscroll: getComputedStyle(document.getElementById('document-modal')).overscrollBehavior
    })`);
    assert.deepEqual(roModal, { open: true, downloadHidden: false, deleteHidden: true, overscroll: 'contain' },
      'Read-only role must not see #modal-delete in document modal');
    await js(`document.getElementById('document-modal').close()`);

    // Switch to writable engineer role and verify #modal-delete becomes visible + CAD target sync
    await js(`
      window.__mockRole = 'engineer';
      window.__mockWritable = ['engineering'];
    `);
    await js(`refresh()`);
    await sleep(100);
    await js(`openDocument('upload-99999999999999999999999999999999')`);
    await sleep(50);
    const rwModalDeleteHidden = await js(`document.getElementById('modal-delete').hidden`);
    assert.equal(rwModalDeleteHidden, false, 'Writable engineer must see #modal-delete');
    await js(`document.getElementById('document-modal').close()`);
    await js(`navigate('engineering')`);
    await sleep(150);

    // Verify DEF-13b: Manual selection of existing kit when #private-kit-part is blank updates hint immediately
    await js(`
      document.getElementById('private-kit-part').value = '';
      privateKitReport = null;
      const sel = document.getElementById('private-kit-version-target');
      sel.value = 'kit-00000000000000000000000000000001';
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    `);
    const manualHint = await js(`document.getElementById('private-kit-target-hint').textContent`);
    assert(manualHint.includes('редакция № 2'), 'Manual kit selection with blank part must update hint: ' + manualHint);

    // Simulate analyzed report with untrimmed/cased part name -> privateKitSave must accept matching normalized part!
    await js(`
      privateKitReport = { part: ' FOCUSER_DRAWTUBE ', sources: [{ sha256: 'a'.repeat(64), name: 'a.csv', role: 'bom' }] };
      privateKitPackage = new Blob(['CZKIT1\\n']);
      syncPrivateKitTarget();
    `);
    const selectedBeforeSave = await js(`document.getElementById('private-kit-version-target').value`);
    assert.equal(selectedBeforeSave, 'kit-00000000000000000000000000000001');
    await js(`privateKitSave()`);
    await sleep(100);
    const saveState = await js(`document.getElementById('private-kit-save-state').textContent`);
    assert(saveState.includes('Сохранено:'), 'Untrimmed matching part must save cleanly instead of false mismatch: ' + saveState);

    // Changing #private-kit-part input immediately clears stale kit selection and updates hint
    await js(`
      const input = document.getElementById('private-kit-part');
      input.value = 'brand_new_shaft';
      input.dispatchEvent(new Event('input', { bubbles: true }));
    `);
    const afterInput = await js(`({
      target: document.getElementById('private-kit-version-target').value,
      hint: document.getElementById('private-kit-target-hint').textContent
    })`);
    assert.equal(afterInput.target, '');
    assert(afterInput.hint.includes('новый комплект'), 'Hint must update immediately on part input change: ' + afterInput.hint);

    // Verify DEF-13 nuance: non-uploader co-writer receiving 404 on #modal-delete gets clear Russian message
    await js(`openDocument('upload-99999999999999999999999999999999')`);
    await sleep(60);
    await js(`document.getElementById('modal-delete').click()`);
    await sleep(60);
    const deleteAlert = await js(`window.__alerts.at(-1) || ''`);
    assert(deleteAlert.includes('только автору загрузки'), 'Co-writer 404 delete must explain uploader-only rule: ' + deleteAlert);
    await js(`document.getElementById('document-modal').close()`);

    // Verify DEF-16a: ALL views (home, documents with long unbroken designations, assistant with answer/sources/SHA-256, engineering with long CAD content, metrics, auth, and document modal) have 0 overflow at 1280px, 390px, and 320px
    const explicitLabels = await js(`['private-kit-bom', 'private-kit-pdf', 'private-kit-step'].every(id => !!document.querySelector('label[for="' + id + '"]'))`);
    assert.equal(explicitLabels, true, 'All CAD file inputs must have explicit <label for="..."> association');
    await js(`(async () => {
      await navigate('engineering');
      renderPrivateKit({
        part: 'focuser_drawtube_assembly_long_code_2026',
        bom: { row: 2, material: '6061-T6' },
        model: { face_count: 14, bbox_mm: [40, 40, 120] },
        drawing: { dimension_count: 3, more_dimensions: true },
        sources: [
          { role: 'bom', name: 'materials_bom_specification_2026.csv', sha256: 'a'.repeat(64) },
          { role: 'drawing', name: 'focuser_drawtube_assembly_drawing_rev2.pdf', sha256: 'b'.repeat(64) },
          { role: 'model', name: 'focuser_drawtube_assembly_3d_model_rev2.STEP', sha256: 'c'.repeat(64) }
        ],
        dimension_checks: [{ label: 'Ø40', page: 1, numeric_candidates: [{ value: 40 }] }]
      });
      await loadSavedKitRevisions(window.__savedKits[0]);
    })()`);
    await sleep(80);
    for (const pageId of ['home', 'documents', 'engineering', 'assistant', 'metrics']) {
      if (pageId !== 'engineering') {
        await js(`navigate(${JSON.stringify(pageId)})`);
        await sleep(80);
      }
      if (pageId === 'assistant') {
        await js(`(async () => {
          document.getElementById('ai-question').value = 'Какое испытательное давление?';
          await aiAsk();
          const note = document.getElementById('ai-upload-result');
          note.replaceChildren(el('strong', '', 'Оригинал и текст сохранены.'), el('p', 'ai-hint', 'SHA-256: ' + 'a'.repeat(64) + ' · индекс: pending'));
        })()`);
        await sleep(60);
      }
      for (const [w, h, mob] of [[1280, 900, false], [390, 844, true], [320, 700, true]]) {
        await cmd('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: mob });
        await sleep(60);
        const dims = await js(`({ vw: document.documentElement.clientWidth, sw: document.documentElement.scrollWidth })`);
        assert(dims.sw <= dims.vw + 1, `Horizontal overflow on #page-${pageId} at ${w}px: ${JSON.stringify(dims)}`);
      }
    }
    await js(`showAuth()`);
    for (const [w, h, mob] of [[1280, 900, false], [390, 844, true], [320, 700, true]]) {
      await cmd('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: mob });
      await sleep(50);
      const dims = await js(`({ vw: document.documentElement.clientWidth, sw: document.documentElement.scrollWidth })`);
      assert(dims.sw <= dims.vw + 1, `Horizontal overflow on #auth at ${w}px: ${JSON.stringify(dims)}`);
    }
    await js(`refresh()`);
    await sleep(80);
    await js(`openDocument('upload-99999999999999999999999999999999')`);
    await sleep(60);
    await js(`document.getElementById('modal-text').textContent = 'Текст_без_пробелов_' + 'X'.repeat(90)`);
    for (const [w, h, mob] of [[390, 844, true], [320, 700, true]]) {
      await cmd('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: mob });
      await sleep(50);
      const dims = await js(`(() => { const d = document.getElementById('document-modal'); return { docVw: document.documentElement.clientWidth, docSw: document.documentElement.scrollWidth, dlgCw: d.clientWidth, dlgSw: d.scrollWidth }; })()`);
      assert(dims.docSw <= dims.docVw + 1 && dims.dlgSw <= dims.dlgCw + 1, `Horizontal overflow in #document-modal at ${w}px: ${JSON.stringify(dims)}`);
    }
    await js(`document.getElementById('document-modal').close()`);
    await cmd('Emulation.clearDeviceMetricsOverride');

    // Verify DEF-16b: WCAG AA text contrast (>= 4.5:1 normal, >= 3.0:1 large) across all views, status badges, auth & modals
    await js(`
      window.__checkContrast = () => {
        const parse = s => { const m = s && s.match(/rgba?\\(([\\d.]+),\\s*([\\d.]+),\\s*([\\d.]+)(?:,\\s*([\\d.]+))?\\)/); return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : [0, 0, 0, 0]; };
        const blend = (fg, bg) => { const a = fg[3] + bg[3] * (1 - fg[3]); return a === 0 ? [255, 255, 255, 1] : [(fg[0] * fg[3] + bg[0] * bg[3] * (1 - fg[3])) / a, (fg[1] * fg[3] + bg[1] * bg[3] * (1 - fg[3])) / a, (fg[2] * fg[3] + bg[2] * bg[3] * (1 - fg[3])) / a, a]; };
        const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
        const effBg = el => { const ch = []; for (let c = el; c && c.nodeType === 1; c = c.parentElement) ch.unshift(parse(getComputedStyle(c).backgroundColor)); let bg = [247, 248, 251, 1]; for (const x of ch) if (x[3] > 0) bg = blend(x, bg); return bg; };
        const bad = [], seen = new Set(), walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walk.nextNode()) {
          const txt = walk.currentNode.nodeValue.trim(); if (!txt) continue;
          const el = walk.currentNode.parentElement; if (!el || seen.has(el)) continue; seen.add(el);
          if (el.closest('[hidden], [aria-hidden="true"], :disabled, button[disabled], select[disabled], input[disabled]')) continue;
          const st = getComputedStyle(el); if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) < 1) continue;
          const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue;
          const bg = effBg(el), fg = blend(parse(st.color), bg), l1 = lum(fg), l2 = lum(bg);
          const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
          const px = parseFloat(st.fontSize), bold = (parseInt(st.fontWeight, 10) || 400) >= 700;
          const min = (px >= 24 || (px >= 18.66 && bold)) ? 3.0 : 4.5;
          if (ratio < min) bad.push({ tag: el.tagName, id: el.id, cls: String(el.className), ratio: +ratio.toFixed(2), min, text: txt.slice(0, 40) });
        }
        return bad;
      };
    `);
    for (const p of ['home', 'documents', 'assistant', 'engineering', 'metrics']) {
      await js(`navigate(${JSON.stringify(p)})`);
      await sleep(80);
      if (p === 'documents') {
        assert.deepEqual(await js(`window.__checkContrast()`), [], 'Documents cards must pass WCAG AA contrast');
        await js(`document.getElementById('search-results').replaceChildren(el('div','loading','Идёт поиск…'), el('div','empty-state','Ничего не найдено'), el('div','message-error','Не удалось получить данные: Ошибка 503'))`);
        assert.deepEqual(await js(`window.__checkContrast()`), [], 'Documents loading/empty/error states must pass WCAG AA contrast');
        await js(`loadDocuments('')`);
        await sleep(60);
      }
      if (p === 'assistant') {
        await js(`(async () => { document.getElementById('ai-question').value = 'Какое испытательное давление?'; await aiAsk(); })()`);
        await sleep(60);
        assert.deepEqual(await js(`window.__checkContrast()`), [], 'Assistant answer/sources/warnings/disclaimer must pass WCAG AA contrast');
        for (const stVal of ['ready', 'starting', 'error']) {
          await js(`document.getElementById('ai-status').dataset.status = ${JSON.stringify(stVal)}; document.getElementById('ai-status').textContent = ${JSON.stringify(stVal)}`);
          assert.deepEqual(await js(`window.__checkContrast()`), [], `WCAG AA contrast failure for ai-status=${stVal}`);
        }
        await js(`openScanPreview('upload-99999999999999999999999999999999', 2, 'Паспорт насоса Н-125'); document.getElementById('scan-preview-error').textContent = 'Изображение страницы не открылось.'; document.getElementById('scan-preview-error').hidden = false;`);
        await sleep(60);
        assert.deepEqual(await js(`window.__checkContrast()`), [], 'Scan preview modal + error state must pass WCAG AA contrast');
        await js(`document.getElementById('scan-preview-modal').close()`);
      }
      const failures = await js(`window.__checkContrast()`);
      assert.deepEqual(failures, [], `WCAG AA contrast failure on page ${p}: ${JSON.stringify(failures)}`);
    }
    await js(`showAuth(); document.getElementById('login-error').textContent = 'Ошибка входа SSO'`);
    await sleep(40);
    assert.deepEqual(await js(`window.__checkContrast()`), [], 'Auth screen must pass WCAG AA contrast');
    await js(`refresh()`);
    await sleep(100);

    // Verify DEF-16c: Real keyboard navigation (Tab, Shift+Tab, Enter, Escape), >= 3:1 :focus-visible outline, aria-current & SPA title
    const pressKey = async (key, code, vk, modifiers = 0) => {
      const extra = key === 'Enter' ? { text: '\r', unmodifiedText: '\r' } : {};
      await cmd('Input.dispatchKeyEvent', { type: 'keyDown', key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers, ...extra });
      await cmd('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers });
    };
    await js(`navigate('documents')`);
    await sleep(100);
    await js(`document.querySelector('.chips button:last-child').focus()`);
    await pressKey('Tab', 'Tab', 9, 0);
    const kbDocBtn = await js(`(() => {
      const el = document.activeElement, st = getComputedStyle(el);
      return { cls: el.className, outlineStyle: st.outlineStyle, outlineWidth: st.outlineWidth, outlineColor: st.outlineColor, ariaCurrent: document.querySelector('.nav-item.active')?.getAttribute('aria-current'), title: document.title };
    })()`);
    assert.equal(kbDocBtn.cls, 'doc-open');
    assert.equal(kbDocBtn.outlineStyle, 'solid');
    assert.equal(kbDocBtn.outlineColor, 'rgb(37, 73, 213)');
    assert.equal(kbDocBtn.ariaCurrent, 'page');
    assert(kbDocBtn.title.startsWith('Документы'), 'SPA title must reflect current section: ' + kbDocBtn.title);

    // Verify Shift+Tab moves focus backward and Tab moves it forward again
    await pressKey('Tab', 'Tab', 9, 8);
    assert.equal(await js(`document.activeElement === document.querySelector('.chips button:last-child')`), true, 'Shift+Tab must return focus to previous chip button');
    await pressKey('Tab', 'Tab', 9, 0);

    await pressKey('Enter', 'Enter', 13, 0);
    await sleep(80);
    const kbModalOpen = await js(`document.getElementById('document-modal').open && document.getElementById('document-modal').contains(document.activeElement)`);
    assert.equal(kbModalOpen, true, 'Enter on .doc-open must open modal and move focus inside');
    const modalContrast = await js(`window.__checkContrast()`);
    assert.deepEqual(modalContrast, [], 'Document modal must pass WCAG AA contrast');
    await pressKey('Escape', 'Escape', 27, 0);
    await sleep(80);
    const kbModalClosed = await js(`!document.getElementById('document-modal').open && document.activeElement?.classList.contains('doc-open')`);
    assert.equal(kbModalClosed, true, 'Escape must close modal and restore focus to trigger');

    await js(`navigate('assistant')`);
    await sleep(80);
    await js(`document.getElementById('ai-demo-run').focus()`);
    for (let i = 0; i < 15; i++) {
      await pressKey('Tab', 'Tab', 9, 0);
      if (await js(`document.activeElement?.id === 'ai-question'`)) break;
    }
    const kbTextarea = await js(`(() => { const el = document.activeElement, st = getComputedStyle(el); return { id: el.id, outlineStyle: st.outlineStyle, outlineColor: st.outlineColor }; })()`);
    assert.equal(kbTextarea.id, 'ai-question');
    assert.equal(kbTextarea.outlineStyle, 'solid', '#ai-question must preserve visible :focus-visible outline');
    assert.equal(kbTextarea.outlineColor, 'rgb(37, 73, 213)');

    console.log('PASS Antigravity Headless Chrome UI regressions: read-only button guards, CAD normalized save, 1280/390/320px zero-overflow across all views & modal, WCAG AA contrast across views/dynamic states/modals/auth, keyboard Tab/Shift+Tab/Enter/Escape');
    } finally {
      server.close();
    }
  } finally {
    for (const item of pending.values()) clearTimeout(item.timer);
    if (ws) try { ws.close(); } catch {}
    try { chrome.kill(); } catch {}
    await sleep(400);
    try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 4, retryDelay: 150 }); } catch {}
  }
}

(async () => {
  await testUploadDeleteAndRevokePolling();
  await testHeadlessChromeUiRegressions();
})().catch(err => {
  console.error(err.stack || err);
  process.exitCode = 1;
});
