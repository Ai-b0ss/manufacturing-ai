/* Real Headless Chrome E2E driver for isolated v0.6 Unified full-stack acceptance.
 *
 * Drives real Keycloak OIDC PKCE S256 login (two roles), verifies backendMode==='unified',
 * uploads scanned PDF with WinRT OCR, checks outbox -> Qdrant indexing status, verifies
 * real MiniCPM5-2B cited answer + page-preview JPEG, refusal on unknown query,
 * live ACL revocation fail-closed behavior, and RP-initiated logout back to #auth.
 *
 * All URLs/ports/credentials come strictly from ephemeral staging environment variables.
 */
'use strict';

const assert = require('node:assert/strict');
const cp = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const CHROME = process.env.FACTORY_CHROME_EXE || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE = process.env.FACTORY_P2_APP_ORIGIN || 'http://localhost:19137';
const KC_PORT = process.env.FACTORY_P2_KEYCLOAK_PORT;
const CREDS_DIR = process.env.FACTORY_P2_CREDS_DIR;
const PY = process.env.FACTORY_STAGE_PYTHON;
const SAMPLE_PDF = path.join(ROOT, 'samples', 'factory_scanned_pump_demo.pdf');
const MODE = process.argv[2] || 'full';

if (!KC_PORT || !CREDS_DIR || !PY) {
  throw new Error('Missing required ephemeral env vars: FACTORY_P2_KEYCLOAK_PORT, FACTORY_P2_CREDS_DIR, FACTORY_STAGE_PYTHON');
}
if (KC_PORT === '18080') {
  throw new Error('REFUSE_LIVE_KEYCLOAK_PORT_18080');
}

const CREDS = JSON.parse(fs.readFileSync(path.join(CREDS_DIR, 'keycloak-demo-users.json'), 'utf8'));
const KC_ORIGIN = `http://127.0.0.1:${KC_PORT}`;
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function withChrome(fn) {
  const tmpBase = process.env.FACTORY_E2E_TMP || process.env.TEMP || os.tmpdir();
  const profile = fs.mkdtempSync(path.join(tmpBase, 'factory-v06-e2e-chrome-'));
  const chrome = cp.spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--proxy-bypass-list=<-loopback>', '--remote-debugging-port=0',
    '--window-size=1440,900', '--user-data-dir=' + profile, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let ws = null;
  let seq = 1;
  const pending = new Map();
  const exceptions = [];

  const cmd = (method, params = {}, timeout = 30000) => new Promise((resolve, reject) => {
    const id = seq++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP timeout: ${method}`));
    }, timeout);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params }));
  });

  const js = async (expression, timeout = 30000) => {
    const r = await cmd('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, timeout);
    if (r.exceptionDetails) {
      throw new Error('JS exception: ' + JSON.stringify(r.exceptionDetails).slice(0, 500));
    }
    return r.result ? r.result.value : undefined;
  };

  const until = async (expression, ms = 35000) => {
    const started = Date.now();
    while (Date.now() - started < ms) {
      try {
        if (await js(expression, 10000)) return true;
      } catch {}
      await sleep(150);
    }
    const href = await js('location.href').catch(() => 'unknown');
    const title = await js('document.title').catch(() => '');
    const snippet = await js('(document.body && document.body.innerText || "").slice(0, 300)').catch(() => '');
    throw new Error(`Condition timed out (${href} | title=${title} | body=${snippet}): ${expression.slice(0, 140)}`);
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
    assert(port > 0, 'Chrome DevToolsActivePort not found');
    const tabs = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    const tab = tabs.find(t => t.type === 'page') || tabs[0];
    ws = new WebSocket(tab.webSocketDebuggerUrl.replace('localhost', '127.0.0.1'));
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', rej, { once: true });
    });
    ws.addEventListener('message', ev => {
      const msg = JSON.parse(ev.data);
      if (msg.method === 'Runtime.exceptionThrown') {
        exceptions.push(msg.params?.exceptionDetails?.text || 'unknown JS exception');
      }
      if (msg.method === 'Page.javascriptDialogOpening') {
        cmd('Page.handleJavaScriptDialog', { accept: true }).catch(() => {});
      }
      if (msg.id && pending.has(msg.id)) {
        const item = pending.get(msg.id);
        pending.delete(msg.id);
        clearTimeout(item.timer);
        if (msg.error) item.reject(new Error(JSON.stringify(msg.error)));
        else item.resolve(msg.result);
      }
    });
    await cmd('Page.enable');
    await cmd('Runtime.enable');
    await cmd('DOM.enable');
    return await fn({ cmd, js, until, exceptions });
  } finally {
    for (const item of pending.values()) clearTimeout(item.timer);
    if (ws) try { ws.close(); } catch {}
    try { chrome.kill(); } catch {}
    await sleep(450);
    try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 4, retryDelay: 200 }); } catch {}
  }
}

async function loginRole({ cmd, js, until }, username) {
  await cmd('Page.navigate', { url: BASE + '/' });
  await until(`location.href.startsWith(${JSON.stringify(BASE + '/')}) && document.readyState === 'complete' && document.getElementById('unified-login')?.dataset.ready === '1'`, 25000);
  const mode = await js('state.backendMode');
  assert.equal(mode, 'unified', `Expected backendMode='unified', got ${mode}`);
  await js("document.getElementById('unified-login').click()");
  await until(`location.href.startsWith(${JSON.stringify(KC_ORIGIN + '/')}) && !!document.querySelector('#kc-form-login')`, 45000);
  const authUrl = new URL(await js('location.href'));
  assert.equal(authUrl.searchParams.get('client_id'), 'factory-web-audit');
  assert.equal(authUrl.searchParams.get('code_challenge_method'), 'S256');
  await js(`(() => {
    document.querySelector('#username').value = ${JSON.stringify(username)};
    document.querySelector('#password').value = ${JSON.stringify(CREDS[username])};
    document.querySelector('#kc-form-login').requestSubmit();
  })()`);
  await until(`location.href.startsWith(${JSON.stringify(BASE + '/')}) && !document.getElementById('workspace').hidden`, 30000);
  const me = await js(`(async () => { const r = await fetch('/unified/api/me', {credentials:'same-origin'}); return { status: r.status, data: await r.json() }; })()`);
  assert.equal(me.status, 200);
  assert.equal(me.data.id, username);
  return me.data;
}

async function logoutRole({ js, until }) {
  await js("document.getElementById('switch-demo').click()");
  await until(`location.href.startsWith(${JSON.stringify(BASE + '/')}) && !document.getElementById('auth').hidden`, 30000);
  const afterMe = await js(`(async () => { const r = await fetch('/unified/api/me', {credentials:'same-origin'}); return r.status; })()`);
  assert.equal(afterMe, 401, 'Session must be revoked after RP logout');
}

async function runEngineerFlow() {
  return withChrome(async ctx => {
    const { cmd, js, until, exceptions } = ctx;
    const out = { role: 'engineer', checks: [] };

    const me = await loginRole(ctx, 'engineer');
    out.checks.push({ step: 'kc_pkce_login_engineer', ok: true, scopes: me.scopes });

    // Navigate to assistant and upload real scanned PDF (with WinRT OCR on page 2)
    await js("document.querySelector('[data-page=assistant]').click()");
    await until("!document.getElementById('page-assistant').hidden && !document.getElementById('ai-upload').disabled", 20000);
    await js("document.getElementById('ai-scope').value = 'engineering';");
    await js("document.getElementById('ai-title').value = 'Учебный паспорт насоса Н-125';");
    const docNode = await cmd('DOM.getDocument', { depth: 1 });
    const fileInput = await cmd('DOM.querySelector', { nodeId: docNode.root.nodeId, selector: '#ai-file' });
    assert(fileInput.nodeId > 0, '#ai-file not found');
    await cmd('DOM.setFileInputFiles', { nodeId: fileInput.nodeId, files: [SAMPLE_PDF] });
    await js("document.getElementById('ai-file').dispatchEvent(new Event('change', {bubbles:true})); document.getElementById('ai-upload').click();");
    await until("document.getElementById('ai-upload-result').textContent.includes('Оригинал и текст сохранены')", 90000);
    const docId = await js('state.uploadDocId');
    assert(/^upload-[0-9a-f]{32}$/.test(docId || ''), `Invalid uploadDocId: ${docId}`);
    out.document_id = docId;
    out.checks.push({ step: 'browser_upload_scanned_pdf_ocr', ok: true, doc_id: docId });

    // Run real indexer worker (E5 embeddings -> native Qdrant)
    const workerOut = cp.execFileSync(PY, ['-m', 'tools.run_ingest_indexer', '--once'], {
      cwd: ROOT, env: process.env, encoding: 'utf8', timeout: 180000, windowsHide: true,
    });
    assert(workerOut.includes('result=indexed'), `Worker did not index upload: ${workerOut}`);
    await js('loadUploadJobStatus()');
    await until("document.getElementById('ai-upload-result').textContent.includes('проиндексирован')", 20000);
    out.checks.push({ step: 'outbox_worker_indexed_qdrant', ok: true });

    // Hybrid search finds uploaded OCR PDF
    const searchRes = await js(`(async () => {
      const r = await fetch('/unified/api/search?q=' + encodeURIComponent('Н-125'), {credentials:'same-origin'});
      return { status: r.status, data: await r.json() };
    })()`, 95000);
    assert.equal(searchRes.status, 200);
    const match = (searchRes.data.items || []).find(x => x.id === docId);
    assert(match && match.has_ocr === true, `Uploaded OCR doc missing from hybrid search: ${JSON.stringify(searchRes.data)}`);
    out.checks.push({ step: 'hybrid_search_finds_ocr_pdf', ok: true, hits: searchRes.data.total });

    // Real page preview JPEG for OCR page 2
    const previewRes = await js(`(async () => {
      const r = await fetch('/unified/api/page-preview?id=' + encodeURIComponent(${JSON.stringify(docId)}) + '&page=2', {credentials:'same-origin'});
      const buf = new Uint8Array(await r.arrayBuffer());
      return { status: r.status, ct: r.headers.get('content-type'), len: buf.length, jpeg: buf[0] === 0xff && buf[1] === 0xd8 };
    })()`, 30000);
    assert.equal(previewRes.status, 200);
    assert.equal(previewRes.ct, 'image/jpeg');
    assert.equal(previewRes.jpeg, true);
    out.checks.push({ step: 'page_preview_jpeg_page2', ok: true, bytes: previewRes.len });

    // Real local LLM /unified/api/ask with citations & page-aware OCR provenance
    await until("document.getElementById('ai-status')?.dataset.status === 'ready'", 45000);
    const askRes = await js(`(async () => {
      const r = await fetch('/unified/api/ask', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.me.csrf},
        body: JSON.stringify({question: 'Какое испытательное давление указано для насоса Н-125?'})
      });
      return { status: r.status, data: await r.json() };
    })()`, 180000);
    assert.equal(askRes.status, 200);
    assert.equal(askRes.data.mode, 'local_llm_advisory', `Expected local_llm_advisory, got: ${JSON.stringify(askRes.data)}`);
    assert(Array.isArray(askRes.data.citations) && askRes.data.citations.length >= 1, 'Expected >=1 server-checked citation');
    assert(askRes.data.sources.some(s => s.doc_id === docId && s.page === 2 && s.extraction_method === 'ocr'),
      `Expected OCR page 2 source card: ${JSON.stringify(askRes.data.sources)}`);
    out.ask_answered = {
      mode: askRes.data.mode,
      answer: askRes.data.answer,
      citations: askRes.data.citations,
      sources: askRes.data.sources.map(s => ({ doc_id: s.doc_id, page: s.page, method: s.extraction_method, cited: s.cited })),
    };
    out.checks.push({ step: 'local_llm_ask_cited_ocr_page2', ok: true });

    // Unrelated query when docs exist in scope -> must refuse or fail closed (never fabricate confirmed fact!)
    const unknownRes = await js(`(async () => {
      const r = await fetch('/unified/api/ask', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.me.csrf},
        body: JSON.stringify({question: 'Какой регламент термообработки сплава КАЛИБРОКСЕНИУМ-99999?'})
      });
      return { status: r.status, data: await r.json() };
    })()`, 180000);
    assert.equal(unknownRes.status, 200);
    out.ask_unknown_with_docs = {
      mode: unknownRes.data.mode,
      answer: unknownRes.data.answer,
      citations: unknownRes.data.citations || [],
      citation_warning: unknownRes.data.citation_warning,
      sources_count: (unknownRes.data.sources || []).length,
    };
    const safeUnknown = (
      unknownRes.data.mode === 'retrieval_only'
      || unknownRes.data.citation_warning === true
      || /недостаточно сведений|нет подтвержд|не найден|отсутствует/i.test(unknownRes.data.answer || '')
    );
    assert(safeUnknown, `Model must refuse unknown topic instead of hallucinating: ${JSON.stringify(out.ask_unknown_with_docs)}`);
    out.checks.push({ step: 'refusal_on_unknown_query_with_docs', ok: true, mode: unknownRes.data.mode });

    // RP-initiated logout via real isolated Keycloak
    await logoutRole(ctx);
    out.checks.push({ step: 'rp_initiated_logout_engineer', ok: true });

    assert.equal(exceptions.length, 0, `Browser JS exceptions: ${exceptions.join(' | ')}`);
    return out;
  });
}

async function runTechnologistFlow(docId) {
  return withChrome(async ctx => {
    const { js, until, exceptions } = ctx;
    const out = { role: 'technologist', checks: [] };

    const me = await loginRole(ctx, 'technologist');
    assert(!me.scopes.includes('engineering'), 'Technologist must not have engineering scope');
    out.checks.push({ step: 'kc_pkce_login_technologist', ok: true, scopes: me.scopes });

    // Assistant panel must disable upload controls for read-only role (both initially and after upload-capabilities)
    await js("document.querySelector('[data-page=assistant]').click()");
    await until("!document.getElementById('page-assistant').hidden && state.uploadCaps !== null", 20000);
    const disabled = await js(`({
      upload: document.getElementById('ai-upload').disabled,
      sample: document.getElementById('ai-try-sample').disabled,
      hero: document.getElementById('ai-demo-run').disabled
    })`);
    assert.deepEqual(disabled, { upload: true, sample: true, hero: true });
    out.checks.push({ step: 'readonly_role_upload_disabled_in_ui', ok: true });

    // Cannot search, read, download, preview, or ask about engineer's document
    const probe = await js(`(async () => {
      const id = ${JSON.stringify(docId)};
      const sr = await (await fetch('/unified/api/search?q=' + encodeURIComponent('Н-125'), {credentials:'same-origin'})).json();
      const doc = (await fetch('/unified/api/document?id=' + encodeURIComponent(id), {credentials:'same-origin'})).status;
      const orig = (await fetch('/unified/api/uploads/' + encodeURIComponent(id) + '/original', {credentials:'same-origin'})).status;
      const prev = (await fetch('/unified/api/page-preview?id=' + encodeURIComponent(id) + '&page=2', {credentials:'same-origin'})).status;
      const askResp = await fetch('/unified/api/ask', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.me.csrf},
        body: JSON.stringify({question: 'Какое испытательное давление указано для насоса Н-125?'})
      });
      return {
        search_total: sr.total,
        doc_status: doc,
        orig_status: orig,
        prev_status: prev,
        ask_status: askResp.status,
        ask: await askResp.json()
      };
    })()`, 60000);
    assert.equal(probe.search_total, 0);
    assert.equal(probe.doc_status, 404);
    assert.equal(probe.orig_status, 404);
    assert.equal(probe.prev_status, 404);
    assert.equal(probe.ask_status, 200);
    assert.equal(probe.ask.mode, 'retrieval_only');
    assert.deepEqual(probe.ask.sources, []);
    assert(probe.ask.answer.includes('нет подтверждённого ответа'), `Expected no-source refusal: ${JSON.stringify(probe.ask)}`);
    out.checks.push({ step: 'technologist_acl_isolation_and_no_source_refusal', ok: true });

    await logoutRole(ctx);
    out.checks.push({ step: 'rp_initiated_logout_technologist', ok: true });

    assert.equal(exceptions.length, 0, `Browser JS exceptions: ${exceptions.join(' | ')}`);
    return out;
  });
}

(async () => {
  let result;
  if (MODE === 'engineer') {
    result = await runEngineerFlow();
  } else if (MODE === 'technologist') {
    const docId = process.argv[3];
    if (!docId) throw new Error('Missing docId argument for technologist mode');
    result = await runTechnologistFlow(docId);
  } else {
    throw new Error(`Unknown mode: ${MODE}`);
  }
  console.log(JSON.stringify({ ok: true, ...result }));
})().catch(err => {
  console.error(err.stack || String(err));
  process.exitCode = 1;
});
