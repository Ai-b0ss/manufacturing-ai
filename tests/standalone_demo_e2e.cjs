// Reproducible end-to-end smoke test for standalone `python app.py` with synthetic data.
// Spawns an isolated loopback app.py + ephemeral SQLite DB + real Headless Chrome (no mocks).
'use strict';

const assert = require('node:assert/strict');
const cp = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PYTHON = process.env.PYTHON_EXE || 'python';

const sleep = ms => new Promise(r => setTimeout(r, ms));

function startApp(tempDir) {
  return new Promise((resolve, reject) => {
    const dbPath = path.join(tempDir, 'smoke.sqlite3');
    const missingModel = path.join(tempDir, 'nonexistent-model.gguf');
    const missingServer = path.join(tempDir, 'nonexistent-llama.exe');
    const proc = cp.spawn(
      PYTHON,
      [path.join(ROOT, 'app.py'), '--host', '127.0.0.1', '--port', '0', '--db', dbPath],
      {
        cwd: ROOT,
        env: {
          ...process.env,
          FACTORY_AI_MODEL: missingModel,
          FACTORY_AI_SERVER: missingServer,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      }
    );
    let stdout = '';
    let stderr = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        try { proc.kill(); } catch {}
        reject(new Error('app.py startup timeout: ' + stdout + '\n' + stderr));
      }
    }, 15000);

    proc.stdout.setEncoding('utf8');
    proc.stderr.setEncoding('utf8');
    proc.stdout.on('data', chunk => {
      stdout += chunk;
      const ready = stdout.match(/FACTORY_MVP_READY (http:\/\/127\.0\.0\.1:\d+)/);
      const creds = stdout.match(/CREDENTIALS_FILE (.+)/);
      if (!settled && ready && creds) {
        settled = true;
        clearTimeout(timer);
        resolve({ proc, baseUrl: ready[1].trim(), credsPath: creds[1].trim() });
      }
    });
    proc.stderr.on('data', chunk => { stderr += chunk; });
    proc.on('exit', code => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new Error(`app.py exited early (${code}): ${stderr}`));
      }
    });
  });
}

(async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'factory-standalone-e2e-'));
  const chromeProfile = path.join(tempDir, 'chrome-profile');
  fs.mkdirSync(chromeProfile, { recursive: true });

  const syntheticTxt = path.join(tempDir, 'synthetic_shaft_note.txt');
  fs.writeFileSync(
    syntheticTxt,
    'УЧЕБНАЯ ПАМЯТКА. Деталь ВАЛ-7741. Предельный зазор подшипника 0,14 мм. Контроль ОТК обязателен.\n',
    'utf8'
  );

  let srv, chrome, ws;
  const pending = new Map();
  const exceptions = [];
  let seq = 1;

  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = seq++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('CDP timeout: ' + method));
    }, 20000);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params }));
  });

  const js = async expr => {
    const r = await cdp('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) {
      throw new Error('JS exception: ' + JSON.stringify(r.exceptionDetails).slice(0, 600));
    }
    return r.result.value;
  };

  const until = async (expr, timeout = 25000) => {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      try {
        if (await js(expr)) return true;
      } catch {}
      await sleep(120);
    }
    throw new Error('Condition timed out: ' + expr);
  };

  try {
    srv = await startApp(tempDir);
    const healthResp = await fetch(srv.baseUrl + '/api/health');
    assert.equal(healthResp.status, 200);
    const health = await healthResp.json();
    assert.equal(health.status, 'ok');
    assert.equal(health.mode, 'synthetic-local-demo');

    const credsText = fs.readFileSync(srv.credsPath, 'utf8');
    assert(credsText.includes(srv.baseUrl), 'demo_credentials.txt must contain actual bound URL');
    const engMatch = credsText.match(/engineer \/ (\S+)/);
    assert(engMatch, 'engineer credentials must exist in demo_credentials.txt');
    const engineerPassword = engMatch[1];

    chrome = cp.spawn(CHROME, [
      '--headless=new', '--no-first-run', '--no-default-browser-check',
      '--disable-gpu', '--disable-extensions', '--remote-debugging-port=0',
      '--window-size=1280,900', '--user-data-dir=' + chromeProfile, 'about:blank',
    ], { stdio: 'ignore', windowsHide: true });

    let port = 0;
    for (let i = 0; i < 100; i++) {
      const f = path.join(chromeProfile, 'DevToolsActivePort');
      if (fs.existsSync(f)) {
        port = Number(fs.readFileSync(f, 'utf8').split(/\r?\n/)[0]);
        if (port) break;
      }
      await sleep(100);
    }
    assert(port > 0, 'Chrome DevTools port missing');

    const tabs = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    const tab = tabs.find(t => t.type === 'page');
    ws = new WebSocket(tab.webSocketDebuggerUrl.replace('localhost', '127.0.0.1'));
    await new Promise((res, rej) => { ws.addEventListener('open', res, { once: true }); ws.addEventListener('error', rej, { once: true }); });
    ws.addEventListener('message', ev => {
      const msg = JSON.parse(ev.data);
      if (msg.method === 'Runtime.exceptionThrown') {
        exceptions.push(msg.params?.exceptionDetails?.text || 'Runtime exception');
      }
      if (msg.id && pending.has(msg.id)) {
        const entry = pending.get(msg.id);
        pending.delete(msg.id);
        clearTimeout(entry.timer);
        msg.error ? entry.reject(new Error(msg.error.message)) : entry.resolve(msg.result);
      }
    });

    await cdp('Runtime.enable');
    await cdp('Page.enable');
    await cdp('DOM.enable');

    // 1. Open standalone app.py UI -> verify demo mode auto-detection and role selector
    await cdp('Page.navigate', { url: srv.baseUrl + '/' });
    await until(`document.readyState === 'complete' && document.querySelectorAll('[data-demo-role]').length === 4`);
    const authState = await js(`({
      unifiedHidden: document.getElementById('unified-login').hidden,
      roles: [...document.querySelectorAll('[data-demo-role]')].map(b => b.dataset.demoRole),
      vw: document.documentElement.clientWidth,
      sw: document.documentElement.scrollWidth
    })`);
    assert.equal(authState.unifiedHidden, true, 'Keycloak button must be hidden in standalone demo mode');
    assert.deepEqual(authState.roles, ['technologist', 'engineer', 'finance', 'director']);
    assert(authState.sw <= authState.vw + 1, 'No horizontal overflow on auth screen');
    const authContrastBad = await js(`(() => {
      const parse = s => { const m = s && s.match(/rgba?\\(([\\d.]+),\\s*([\\d.]+),\\s*([\\d.]+)(?:,\\s*([\\d.]+))?\\)/); return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : [0, 0, 0, 0]; };
      const blend = (fg, bg) => { const a = fg[3] + bg[3] * (1 - fg[3]); return a === 0 ? [255, 255, 255, 1] : [(fg[0] * fg[3] + bg[0] * bg[3] * (1 - fg[3])) / a, (fg[1] * fg[3] + bg[1] * bg[3] * (1 - fg[3])) / a, (fg[2] * fg[3] + bg[2] * bg[3] * (1 - fg[3])) / a, a]; };
      const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
      const effBg = el => { const ch = []; for (let c = el; c && c.nodeType === 1; c = c.parentElement) ch.unshift(parse(getComputedStyle(c).backgroundColor)); let bg = [255, 255, 255, 1]; for (const x of ch) if (x[3] > 0) bg = blend(x, bg); return bg; };
      const bad = [], seen = new Set(), walk = document.createTreeWalker(document.getElementById('auth'), NodeFilter.SHOW_TEXT);
      while (walk.nextNode()) {
        const txt = walk.currentNode.nodeValue.trim(); if (!txt) continue;
        const el = walk.currentNode.parentElement; if (!el || seen.has(el)) continue; seen.add(el);
        if (el.closest('[hidden], [aria-hidden="true"]')) continue;
        const st = getComputedStyle(el); if (st.display === 'none' || st.visibility === 'hidden') continue;
        const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue;
        const bg = effBg(el), fg = blend(parse(st.color), bg), l1 = lum(fg), l2 = lum(bg);
        const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        const px = parseFloat(st.fontSize), bold = (parseInt(st.fontWeight, 10) || 400) >= 700;
        const min = (px >= 24 || (px >= 18.66 && bold)) ? 3.0 : 4.5;
        if (ratio < min) bad.push({ tag: el.tagName, cls: String(el.className), ratio: +ratio.toFixed(2), min, text: txt.slice(0, 40) });
      }
      return bad;
    })()`);
    assert.deepEqual(authContrastBad, [], 'Demo auth screen must pass WCAG AA contrast: ' + JSON.stringify(authContrastBad));

    // 2. Demo-login as technologist
    await js(`document.querySelector('[data-demo-role="technologist"]').click()`);
    await until(`!document.getElementById('workspace').hidden && document.getElementById('profile-role').textContent.includes('Технолог')`);

    // 3. Search documents & open DOC-001 modal
    await js(`document.querySelector('[data-page="documents"]').click()`);
    await until(`!document.getElementById('page-documents').hidden && document.querySelectorAll('#search-results .doc-card').length >= 5`);
    await js(`document.getElementById('search-input').value = '12Х18Н10Т'; document.getElementById('search-form').requestSubmit()`);
    await until(`document.querySelectorAll('#search-results .doc-card').length === 1`);
    await js(`document.querySelector('#search-results .doc-open').click()`);
    await until(`document.getElementById('document-modal').open`);
    const docText = await js(`document.getElementById('modal-text').textContent`);
    assert(docText.includes('12Х18Н10Т'), 'DOC-001 modal must show material text');
    await js(`document.getElementById('close-modal').click()`);
    await until(`!document.getElementById('document-modal').open`);

    // 4. Verify part applicability is enabled in standalone mode & safe for technologist
    await js(`document.querySelector('[data-page="part"]').click()`);
    await until(`!document.getElementById('page-part').hidden && !!document.querySelector('#part-result .part-result-card')`);
    const techPart = await js(`document.getElementById('part-result').textContent`);
    assert(techPart.includes('Редакция не подтверждена'), 'Technologist must see unverified state');
    assert(!techPart.includes('ИИ-017'), 'Change notice must never leak to technologist');

    // 5. Open Assistant -> verify missing LLM disables start button, upload controls enabled
    await js(`document.querySelector('[data-page="assistant"]').click()`);
    await until(`!document.getElementById('page-assistant').hidden && document.getElementById('ai-status').dataset.status === 'missing'`);
    const assistantControls = await js(`({
      startDisabled: document.getElementById('ai-start').disabled,
      stopDisabled: document.getElementById('ai-stop').disabled,
      uploadDisabled: document.getElementById('ai-upload').disabled,
      demoDisabled: document.getElementById('ai-demo-run').disabled,
    })`);
    assert.deepEqual(assistantControls, {
      startDisabled: true,
      stopDisabled: true,
      uploadDisabled: false,
      demoDisabled: false,
    });

    // 6. Run guided demo (#ai-demo-run): real sample PDF upload + real Windows OCR + retrieval_only fallback
    await js(`document.getElementById('ai-demo-run').click()`);
    await until(`!!document.querySelector('#ai-answer .ai-answer-text') && document.querySelectorAll('#ai-sources .ai-source').length >= 1`, 45000);
    const demoOutcome = await js(`({
      uploadNote: document.getElementById('ai-upload-result').textContent,
      answerLabel: document.querySelector('#ai-answer .ai-answer-label')?.textContent || '',
      answerText: document.querySelector('#ai-answer .ai-answer-text')?.textContent || '',
      warnings: [...document.querySelectorAll('#ai-answer .ai-warning-line')].map(n => n.textContent),
      sources: [...document.querySelectorAll('#ai-sources .ai-source')].map(n => n.textContent),
      hasScanButton: !!document.querySelector('#ai-sources .scan-open'),
    })`);
    assert(demoOutcome.uploadNote.includes('Оригинал и текст сохранены'), 'First sample upload must save cleanly: ' + demoOutcome.uploadNote);
    assert(demoOutcome.answerText.includes('Найдены разрешённые фрагменты'), 'Missing LLM must degrade gracefully to retrieval_only: ' + demoOutcome.answerText);
    assert(demoOutcome.sources.some(s => s.includes('25 бар') && s.includes('OCR') && s.includes('Страница 2')), 'OCR page 2 with 25 bar must be returned');
    assert.equal(demoOutcome.hasScanButton, true, 'OCR PDF source must offer page preview button');

    // 7. Click "Открыть страницу PDF" -> verify real JPEG renders from /api/page-preview
    await js(`document.querySelector('#ai-sources .scan-open').click()`);
    await until(`document.getElementById('scan-preview-modal').open && document.getElementById('scan-preview-image').complete && document.getElementById('scan-preview-image').naturalWidth > 100`, 20000);
    await js(`document.getElementById('scan-preview-close').click()`);
    await until(`!document.getElementById('scan-preview-modal').open`);

    // 8. Re-run guided demo -> verify duplicate detection
    await js(`document.getElementById('ai-demo-run').click()`);
    await until(`document.getElementById('ai-upload-result').textContent.includes('Этот файл уже загружен')`, 25000);

    // 9. Exact Cyrillic identifier query ("12Х18Н10Т") -> NO false homoglyph warning!
    await js(`document.getElementById('ai-question').value = '12Х18Н10Т'; document.getElementById('ai-question-form').requestSubmit()`);
    await until(`document.querySelectorAll('#ai-sources .ai-source').length >= 1 && !document.getElementById('ai-ask').disabled`, 15000);
    const cyrillicWarnings = await js(`[...document.querySelectorAll('#ai-answer .ai-warning-line')].map(n => n.textContent)`);
    assert(!cyrillicWarnings.some(w => w.includes('похожих букв')), 'Exact Cyrillic 12Х18Н10Т must not trigger false homoglyph warning: ' + JSON.stringify(cyrillicWarnings));

    // 10. Unknown identifier -> clean no_sources response
    await js(`document.getElementById('ai-question').value = 'НЕСУЩЕСТВУЮЩИЙ-99999'; document.getElementById('ai-question-form').requestSubmit()`);
    await until(`document.querySelector('#ai-answer .ai-answer-text')?.textContent?.includes('не найден') && document.getElementById('ai-sources').hidden`, 15000);

    // 11. Upload synthetic TXT file, query it, then delete via modal
    const docNode = await cdp('DOM.getDocument', { depth: 1 });
    const fileInput = await cdp('DOM.querySelector', { nodeId: docNode.root.nodeId, selector: '#ai-file' });
    await cdp('DOM.setFileInputFiles', { nodeId: fileInput.nodeId, files: [syntheticTxt] });
    await js(`document.getElementById('ai-upload').click()`);
    await until(`document.getElementById('ai-upload-result').textContent.includes('Оригинал и текст сохранены')`, 15000);
    await js(`document.getElementById('ai-question').value = 'ВАЛ-7741'; document.getElementById('ai-question-form').requestSubmit()`);
    await until(`document.getElementById('ai-sources').textContent.includes('0,14 мм')`, 15000);
    await js(`window.confirm = () => true; document.querySelector('#ai-sources .ai-source-open').click()`);
    await until(`document.getElementById('document-modal').open && !document.getElementById('modal-delete').hidden`);
    await js(`document.getElementById('modal-delete').click()`);
    await until(`!document.getElementById('document-modal').open && document.getElementById('ai-upload-result').textContent.includes('удалены')`, 15000);

    // 12. Logout -> verify unauthenticated /api/me returns 401
    await js(`document.getElementById('switch-demo').click()`);
    await until(`!document.getElementById('auth').hidden && document.getElementById('workspace').hidden`);
    const afterLogout = await js(`(async () => (await fetch('/api/me')).status)()`);
    assert.equal(afterLogout, 401);

    // 13. Password login: wrong password shows alert error, right password logs in as engineer
    await js(`
      document.querySelector('details.technical-login').open = true;
      document.getElementById('username').value = 'engineer';
      document.getElementById('password').value = 'wrong-password';
      document.getElementById('login-form').requestSubmit();
    `);
    await until(`document.getElementById('login-error').textContent.length > 0`);
    await js(`
      document.getElementById('username').value = 'engineer';
      document.getElementById('password').value = ${JSON.stringify(engineerPassword)};
      document.getElementById('login-form').requestSubmit();
    `);
    await until(`!document.getElementById('workspace').hidden && document.getElementById('profile-role').textContent.includes('Инженер')`);

    // 14. Engineer verifies part & lots (LOT-2025-12 -> ДОРАБОТАТЬ, LOT-2026-10 -> REVISION_MATCHES)
    await js(`document.querySelector('[data-page="part"]').click()`);
    await until(`!document.getElementById('page-part').hidden && document.querySelectorAll('#part-result .part-evidence-item').length === 2`);
    const oldLotText = await js(`document.getElementById('part-result').textContent`);
    assert(oldLotText.includes('ДОРАБОТАТЬ') && oldLotText.includes('Партию нужно проверить'), 'Old lot must flag engineer review');
    await js(`document.getElementById('lot-id').value = 'LOT-2026-10'; document.getElementById('part-form').requestSubmit()`);
    await until(`document.getElementById('part-result').textContent.includes('СООТВЕТСТВИЕ УЧЕБНЫХ ЗАПИСЕЙ')`);

    // 15. Mobile width overflow check (390px)
    await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await sleep(120);
    const mobileDims = await js(`({ vw: document.documentElement.clientWidth, sw: document.documentElement.scrollWidth })`);
    assert(mobileDims.sw <= mobileDims.vw + 1, 'No horizontal overflow at 390px: ' + JSON.stringify(mobileDims));
    await cdp('Emulation.clearDeviceMetricsOverride');

    assert.equal(exceptions.length, 0, 'Uncaught JS exceptions: ' + JSON.stringify(exceptions));
    console.log(JSON.stringify({
      ok: true,
      baseUrl: srv.baseUrl,
      verified: [
        'standalone_demo_mode_detection',
        'role_login_and_password_login',
        'wrong_password_rejected',
        'document_search_and_modal',
        'part_acl_and_lot_revision_chain',
        'sample_pdf_upload_ocr_and_duplicate_detection',
        'retrieval_only_fallback_without_llm',
        'pdf_page_preview_jpeg',
        'exact_cyrillic_identifier_no_false_homoglyph',
        'unknown_identifier_no_sources',
        'synthetic_txt_upload_query_and_delete',
        'logout_revokes_session',
        'desktop_and_mobile_zero_overflow',
        'zero_js_exceptions',
      ],
    }, null, 2));
  } finally {
    for (const item of pending.values()) clearTimeout(item.timer);
    if (ws) try { ws.close(); } catch {}
    if (chrome) try { chrome.kill(); } catch {}
    if (srv?.proc) try { srv.proc.kill(); } catch {}
    await sleep(400);
    try { fs.rmSync(tempDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 150 }); } catch {}
  }
})().catch(err => {
  console.error(err.stack || err);
  process.exitCode = 1;
});
