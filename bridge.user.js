// ==UserScript==
// @name         DeepSeek Web Local AI Bridge
// @namespace    http://localhost:8765/
// @version      1.1.0
// @description  Bridges chat.deepseek.com tab to Local OpenAI API Server (ws://127.0.0.1:8765/ws)
// @match        https://chat.deepseek.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(() => {
  if (window.__dsLocalBridgeV2) return;
  window.__dsLocalBridgeV2 = true;

  const WS_URL = 'ws://127.0.0.1:8765/ws';
  const DEFAULT_WASM_URL = 'https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm';
  const FALLBACK_VERSION = '2.5.0';

  let ws = null;
  let useDomMode = false;
  let activeAbort = null;
  let pendingDomReqId = null;
  let wasmModulePromise = null;
  let resolvedWasmUrl = null;

  // Fingerprint & anti-bot headers captured from DeepSeek's own frontend requests
  const capturedHeaders = {
    'x-client-platform': 'web',
    'x-client-version': FALLBACK_VERSION,
    'x-app-version': FALLBACK_VERSION,
  };

  function recordHeader(k, v) {
    if (!k || v === undefined || v === null) return;
    const lk = String(k).toLowerCase().trim();
    if (
      (lk.startsWith('x-client-') ||
        lk.startsWith('x-hif-') ||
        lk === 'x-device-id' ||
        lk === 'x-app-version' ||
        lk === 'authorization') &&
      lk !== 'x-ds-pow-response'
    ) {
      capturedHeaders[lk] = String(v);
    }
  }

  // 1. Hook window.fetch (both Request object headers and init.headers)
  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (input && typeof input === 'object' && input.headers instanceof Headers) {
        input.headers.forEach((v, k) => recordHeader(k, v));
      }
      if (init && init.headers) {
        if (init.headers instanceof Headers) {
          init.headers.forEach((v, k) => recordHeader(k, v));
        } else if (Array.isArray(init.headers)) {
          for (const [k, v] of init.headers) recordHeader(k, v);
        } else if (typeof init.headers === 'object') {
          for (const [k, v] of Object.entries(init.headers)) recordHeader(k, v);
        }
      }
      const resp = await origFetch(input, init);
      if (pendingDomReqId && url.includes('/api/v0/chat/completion') && resp.ok && resp.body) {
        const reqId = pendingDomReqId;
        pendingDomReqId = null;
        const clone = resp.clone();
        streamResponseToWs(reqId, clone.body, null).catch((err) => {
          sendWs({ type: 'error', req_id: reqId, message: String(err?.message || err) });
        });
      }
      return resp;
    } catch (err) {
      throw err;
    }
  };

  // 2. Hook XMLHttpRequest.prototype.setRequestHeader (captures x-device-id / x-hif-* if XHR is used)
  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    try {
      recordHeader(name, value);
    } catch {}
    return origSetHeader.apply(this, arguments);
  };

  function getAuthToken() {
    if (capturedHeaders['authorization']) {
      const t = capturedHeaders['authorization'].replace(/^Bearer\s+/i, '').trim();
      if (t && t !== 'null') return t;
    }
    try {
      const raw = localStorage.getItem('userToken');
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const val = typeof parsed === 'string' ? parsed : parsed?.value;
      return val && val !== 'null' ? String(val).trim() : null;
    } catch {
      return null;
    }
  }

  function tryGetDeviceId() {
    if (capturedHeaders['x-device-id']) return capturedHeaders['x-device-id'];
    try {
      if (window.SMSdk && typeof window.SMSdk.getDeviceId === 'function') {
        const id = window.SMSdk.getDeviceId();
        if (id) {
          capturedHeaders['x-device-id'] = String(id);
          return String(id);
        }
      }
    } catch {}
    return null;
  }

  function buildHeaders(extra = {}) {
    const token = getAuthToken();
    tryGetDeviceId();
    const h = {
      accept: 'application/json, text/plain, */*',
      'content-type': 'application/json',
      ...capturedHeaders,
      ...extra,
    };
    if (token) h['authorization'] = `Bearer ${token}`;
    return h;
  }

  async function fetchWithTimeout(url, options = {}, timeoutMs = 15000, outerSignal = null) {
    const controller = new AbortController();
    const onOuterAbort = () => controller.abort();
    if (outerSignal) {
      if (outerSignal.aborted) controller.abort();
      else outerSignal.addEventListener('abort', onOuterAbort, { once: true });
    }
    const timer = setTimeout(() => controller.abort(new Error(`Timeout after ${timeoutMs}ms: ${url}`)), timeoutMs);
    try {
      return await origFetch(url, { ...options, signal: controller.signal });
    } finally {
      clearTimeout(timer);
      if (outerSignal) outerSignal.removeEventListener('abort', onOuterAbort);
    }
  }

  async function findWasmUrl(signal) {
    if (resolvedWasmUrl) return resolvedWasmUrl;
    // 1. Check already loaded resources in performance entries (0ms)
    try {
      const resources = performance.getEntriesByType('resource') || [];
      for (const r of resources) {
        if (r.name && r.name.includes('sha3') && r.name.endsWith('.wasm')) {
          resolvedWasmUrl = r.name;
          return resolvedWasmUrl;
        }
      }
    } catch {}
    // 2. Check known official DEFAULT_WASM_URL first (fast & CORS-enabled)
    resolvedWasmUrl = DEFAULT_WASM_URL;
    return resolvedWasmUrl;
  }

  async function solvePowInPage(challenge, signal) {
    if (!wasmModulePromise) {
      const wasmUrl = await findWasmUrl(signal);
      wasmModulePromise = fetchWithTimeout(wasmUrl, { method: 'GET' }, 10000, signal)
        .then((r) => {
          if (!r.ok) throw new Error(`WASM fetch HTTP ${r.status}`);
          return r.arrayBuffer();
        })
        .then((bytes) => WebAssembly.compile(bytes))
        .catch((err) => {
          wasmModulePromise = null;
          throw err;
        });
    }
    const mod = await wasmModulePromise;
    const instance = await WebAssembly.instantiate(mod, { wbg: {} });
    const e = instance.exports;
    const alloc = e.__wbindgen_export_0 || e.__wbindgen_malloc;
    const addSp = e.__wbindgen_add_to_stack_pointer;
    if (typeof e.wasm_solve !== 'function' || typeof alloc !== 'function' || !e.memory) {
      throw new Error('Incompatible DeepSeek WASM exports');
    }
    const enc = new TextEncoder();
    const cBytes = enc.encode(String(challenge.challenge));
    const pBytes = enc.encode(`${challenge.salt}_${challenge.expire_at}_`);
    const cP = alloc(cBytes.length, 1) >>> 0;
    const pP = alloc(pBytes.length, 1) >>> 0;
    new Uint8Array(e.memory.buffer).set(cBytes, cP);
    new Uint8Array(e.memory.buffer).set(pBytes, pP);
    const sp = addSp(-16);
    e.wasm_solve(sp, cP, cBytes.length, pP, pBytes.length, Number(challenge.difficulty));
    const dv = new DataView(e.memory.buffer);
    const status = dv.getInt32(sp, true);
    const answer = dv.getFloat64(sp + 8, true);
    addSp(16);
    if (status === 0 || !Number.isFinite(answer)) {
      throw new Error(`PoW solve failed (status=${status})`);
    }
    return Math.floor(answer);
  }

  function checkEnvelopeError(json, httpStatus) {
    if (!json || typeof json !== 'object') return `Non-JSON response (HTTP ${httpStatus})`;
    if (typeof json.code === 'number' && json.code !== 0) {
      return json.msg || json.message || `Error code ${json.code}`;
    }
    const bizCode = json?.data?.biz_code;
    if (typeof bizCode === 'number' && bizCode !== 0) {
      return json?.data?.biz_msg || `biz_code ${bizCode}`;
    }
    return null;
  }

  async function createPowHeader(targetPath, signal, reqId) {
    sendLog(reqId, 'Requesting PoW challenge...');
    const resp = await fetchWithTimeout(
      '/api/v0/chat/create_pow_challenge',
      {
        method: 'POST',
        headers: buildHeaders(),
        body: JSON.stringify({ target_path: targetPath }),
      },
      12000,
      signal
    );
    const text = await resp.text();
    let json = null;
    try { json = JSON.parse(text); } catch {}
    const errMsg = checkEnvelopeError(json, resp.status);
    if (!resp.ok || errMsg) {
      throw new Error(`PoW challenge failed: ${errMsg || text.slice(0, 120)}`);
    }
    const ch = json?.data?.biz_data?.challenge;
    if (!ch?.challenge) throw new Error('Missing PoW challenge fields');
    sendLog(reqId, `Solving WASM PoW (difficulty=${ch.difficulty})...`);
    const t0 = Date.now();
    const answer = await solvePowInPage(ch, signal);
    sendLog(reqId, `PoW solved in ${Date.now() - t0}ms (answer=${answer})`);
    const powJson = JSON.stringify({
      algorithm: ch.algorithm,
      challenge: ch.challenge,
      salt: ch.salt,
      answer,
      signature: ch.signature,
      target_path: targetPath,
    });
    return btoa(powJson);
  }

  async function createChatSession(signal, reqId) {
    sendLog(reqId, 'Creating chat_session...');
    const resp = await fetchWithTimeout(
      '/api/v0/chat_session/create',
      {
        method: 'POST',
        headers: buildHeaders(),
        body: '{}',
      },
      12000,
      signal
    );
    const text = await resp.text();
    let json = null;
    try { json = JSON.parse(text); } catch {}
    const errMsg = checkEnvelopeError(json, resp.status);
    if (!resp.ok || errMsg) {
      throw new Error(`Session create failed: ${errMsg || text.slice(0, 120)}`);
    }
    const id = json?.data?.biz_data?.chat_session?.id || json?.data?.biz_data?.id;
    if (!id) throw new Error(`Create session missing id: ${text.slice(0, 120)}`);
    sendLog(reqId, `Created session_id=${id.slice(0, 8)}...`);
    return id;
  }

  async function streamResponseToWs(reqId, readableBody, sessionId) {
    const reader = readableBody.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        if (lines.length > 0) {
          sendWs({ type: 'sse_lines', req_id: reqId, session_id: sessionId, lines });
        }
      }
      if (buffer.trim()) {
        sendWs({ type: 'sse_lines', req_id: reqId, session_id: sessionId, lines: [buffer] });
      }
      sendWs({ type: 'done', req_id: reqId, session_id: sessionId });
    } finally {
      try { reader.releaseLock(); } catch {}
    }
  }

  // Anti-Ban & Subagent Optimization Guard Directive
  const ANTI_BAN_PROMPT_DIRECTIVE =
    '\n\n[ANTI-BAN & CONCURRENCY SAFEGUARDS:\n' +
    '1. Subagent Limit: Never spawn multiple parallel subagents. Prefer completing tasks directly in this session. If delegating to a subagent is strictly required, invoke at most ONE subagent at a time sequentially.\n' +
    '2. Action Batching: Combine related tool operations and commands into consolidated steps instead of multiple rapid back-and-forth micro-turns.\n' +
    '3. Search Moderation: Formulate focused, high-precision search queries rather than sending rapid repetitive searches.\n' +
    '4. Rate Pacing: Provide clear, comprehensive outputs to avoid rapid ping-pong turn loops.]';

  function applyAntiBanSafeguard(promptText) {
    if (!promptText || typeof promptText !== 'string') return promptText;
    if (
      promptText.includes('ANTI-BAN') ||
      promptText.includes('Subagent Limit') ||
      promptText.includes('Subagent & Concurrency Policy')
    ) {
      return promptText;
    }
    return promptText + ANTI_BAN_PROMPT_DIRECTIVE;
  }

  async function runDomCompletion(msg) {
    const { req_id: reqId, prompt, new_chat: newChat } = msg;
    const promptToSend = applyAntiBanSafeguard(prompt);
    sendLog(reqId, 'Running DOM Mode completion...');
    updateBadge('🔄 DOM Sending...', '#f59e0b');
    if (newChat) {
      const newBtn = document.querySelector('[data-testid="new-chat-button"], a[href="/"]');
      if (newBtn) newBtn.click();
      await new Promise((r) => setTimeout(r, 250));
    }
    const textarea = document.querySelector('textarea#chat-input, textarea');
    if (!textarea) throw new Error('Cannot find chat input textarea on page');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
    if (setter) setter.call(textarea, promptToSend);
    else textarea.value = promptToSend;
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    pendingDomReqId = reqId;
    await new Promise((r) => setTimeout(r, 100));
    const sendBtn =
      textarea.closest('div')?.parentElement?.querySelector('div[role="button"]:not([aria-disabled="true"])') ||
      document.querySelector('div[role="button"].ds-icon-button');
    if (sendBtn) {
      sendBtn.click();
    } else {
      textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
    }
  }

  async function handleCompletion(msg) {
    const {
      req_id: reqId,
      prompt,
      session_id: requestedSessionId,
      parent_message_id: parentMessageId,
      thinking_enabled: thinkingEnabled,
      search_enabled: searchEnabled,
    } = msg;

    try {
      if (!getAuthToken()) {
        sendWs({ type: 'error', req_id: reqId, message: 'Chưa tìm thấy userToken trên tab chat.deepseek.com.' });
        return;
      }

      if (useDomMode) {
        await runDomCompletion(msg);
        return;
      }

      if (activeAbort) {
        try { activeAbort.abort(); } catch {}
      }
      const controller = new AbortController();
      activeAbort = controller;
      updateBadge('⚡ Generating...', '#3b82f6');

      let sessionId = requestedSessionId || null;
      let parentId = parentMessageId ?? null;

      try {
        if (!sessionId) {
          sessionId = await createChatSession(controller.signal, reqId);
        }

        const promptToSend = applyAntiBanSafeguard(prompt);

        for (let attempt = 0; attempt < 2; attempt++) {
          const powHeader = await createPowHeader('/api/v0/chat/completion', controller.signal, reqId);
          sendLog(reqId, `POST /api/v0/chat/completion (session=${sessionId.slice(0, 8)}, parent=${parentId})...`);
          const resp = await fetchWithTimeout(
            '/api/v0/chat/completion',
            {
              method: 'POST',
              headers: buildHeaders({
                accept: 'text/event-stream',
                'x-ds-pow-response': powHeader,
              }),
              body: JSON.stringify({
                chat_session_id: sessionId,
                parent_message_id: parentId,
                prompt: promptToSend,
                ref_file_ids: [],
                thinking_enabled: Boolean(thinkingEnabled),
                search_enabled: Boolean(searchEnabled),
                model_type: 'default',
                action: null,
                preempt: false,
              }),
            },
            25000,
            controller.signal
          );

          const ctype = (resp.headers.get('content-type') || '').toLowerCase();
          if (!resp.ok || !ctype.includes('text/event-stream')) {
            const text = await resp.text().catch(() => '');
            let errJson = null;
            try { errJson = JSON.parse(text); } catch {}
            const bizMsg = checkEnvelopeError(errJson, resp.status) || text.slice(0, 160) || `HTTP ${resp.status}`;
            sendLog(reqId, `Non-SSE response (${resp.status}, ${ctype}): ${bizMsg}`);
            if (attempt === 0 && /session|会话/i.test(bizMsg)) {
              sessionId = await createChatSession(controller.signal, reqId);
              parentId = null;
              continue;
            }
            throw new Error(bizMsg);
          }

          sendLog(reqId, 'SSE stream connected, reading tokens...');
          await streamResponseToWs(reqId, resp.body, sessionId);
          break;
        }
        updateBadge(`🟢 Local AI (${useDomMode ? 'DOM' : 'API'})`, '#10b981');
      } catch (innerErr) {
        sendLog(reqId, `Error: ${innerErr?.message || innerErr}`);
        sendWs({ type: 'error', req_id: reqId, session_id: sessionId, message: String(innerErr?.message || innerErr) });
        updateBadge(`🟢 Local AI (${useDomMode ? 'DOM' : 'API'})`, '#10b981');
      }
    } catch (err) {
      sendLog(reqId, `Error: ${err?.message || err}`);
      sendWs({ type: 'error', req_id: reqId, message: String(err?.message || err) });
      updateBadge(`🟢 Local AI (${useDomMode ? 'DOM' : 'API'})`, '#10b981');
    }
  }

  function sendLog(reqId, message) {
    sendWs({ type: 'log', req_id: reqId, message });
  }

  function sendWs(payload) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }

  function connectWs() {
    try {
      ws = new WebSocket(WS_URL);
      ws.onopen = () => {
        updateBadge(`🟢 Local AI (${useDomMode ? 'DOM' : 'API'})`, '#10b981');
        sendWs({
          type: 'hello',
          has_token: Boolean(getAuthToken()),
          device_id: Boolean(tryGetDeviceId()),
          url: location.href,
        });
      };
      ws.onmessage = async (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'completion') {
            handleCompletion(msg);
          } else if (msg.type === 'abort' && activeAbort) {
            activeAbort.abort();
          } else if (msg.type === 'ping') {
            sendWs({ type: 'pong', has_token: Boolean(getAuthToken()) });
          } else if (msg.type === 'eval') {
            try {
              const res = await eval(msg.code);
              sendWs({ type: 'eval_result', id: msg.id, ok: true, result: res });
            } catch (e) {
              sendWs({ type: 'eval_result', id: msg.id, ok: false, error: String(e?.stack || e) });
            }
          }
        } catch (e) {
          console.error('[DS-Bridge]', e);
        }
      };
      ws.onclose = () => {
        updateBadge('⚪ Server Offline (8765)', '#6b7280');
        setTimeout(connectWs, 2000);
      };
      ws.onerror = () => {
        try { ws.close(); } catch {}
      };
    } catch {
      setTimeout(connectWs, 2000);
    }
  }

  let badgeEl = null;
  let lastBadgeState = { text: '⚪ Connecting...', color: '#6b7280' };

  function updateBadge(text, color) {
    lastBadgeState = { text, color };
    if (!document.body) return;
    if (!badgeEl) {
      badgeEl = document.createElement('div');
      badgeEl.style.cssText =
        'position:fixed;bottom:14px;right:14px;z-index:999999;padding:6px 12px;border-radius:999px;' +
        'font:600 12px/1.2 -apple-system,BlinkMacSystemFont,sans-serif;color:#fff;cursor:pointer;' +
        'box-shadow:0 2px 10px rgba(0,0,0,0.25);user-select:none;transition:all .2s;';
      badgeEl.title = 'DeepSeek Local AI Bridge | Click to toggle API/DOM Mode | 🛡️ Anti-Ban Safeguards Active';
      badgeEl.onclick = () => {
        useDomMode = !useDomMode;
        updateBadge(`🟢 Local AI (${useDomMode ? 'DOM' : 'API'})`, '#10b981');
      };
      document.body.appendChild(badgeEl);
    }
    badgeEl.textContent = text;
    badgeEl.style.background = color;
  }

  if (document.readyState === 'loading') {
    window.addEventListener('DOMContentLoaded', () => updateBadge(lastBadgeState.text, lastBadgeState.color));
  } else {
    updateBadge(lastBadgeState.text, lastBadgeState.color);
  }
  connectWs();
})();
