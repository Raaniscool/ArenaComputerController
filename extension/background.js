'use strict';
importScripts('command-parser.js');
const API = 'http://127.0.0.1:4317';
const TOKEN_KEY = 'connectorExtensionToken';
const COMMAND = globalThis.ArenaCommandParser;

async function getToken() {
  const stored = await chrome.storage.local.get(TOKEN_KEY);
  return typeof stored[TOKEN_KEY] === 'string' ? stored[TOKEN_KEY] : '';
}
async function connectorFetch(path, options = {}) {
  const token = await getToken();
  if (token.length < 32) return { ok: false, error: 'extension_token_not_configured' };
  try {
    const response = await fetch(`${API}${path}`, {
      ...options,
      cache: 'no-store',
      credentials: 'omit',
      redirect: 'error',
      headers: {
        Authorization: `Bearer ${token}`,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...options.headers,
      },
    });
    let body = {};
    try { body = await response.json(); } catch {}
    return { ok: response.ok && body.ok !== false, statusCode: response.status, body };
  } catch {
    return { ok: false, error: 'connector_unreachable' };
  }
}
function isArenaSender(sender) {
  if (sender.id !== chrome.runtime.id || typeof sender.url !== 'string') return false;
  try {
    const url = new URL(sender.url);
    return url.protocol === 'https:' && url.hostname === 'arena.ai'
      && (url.pathname.startsWith('/text/') || url.pathname.startsWith('/c/'));
  } catch { return false; }
}
function isPopupSender(sender) {
  if (sender.id !== chrome.runtime.id || typeof sender.url !== 'string') return false;
  try {
    const url = new URL(sender.url);
    return url.protocol === 'chrome-extension:' && url.host === chrome.runtime.id && url.pathname === '/popup.html';
  } catch { return false; }
}
function validRequestId(value) {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}
async function rememberRequest(requestId) {
  const stored = await chrome.storage.local.get('recentOperationIds');
  const ids = Array.isArray(stored.recentOperationIds) ? stored.recentOperationIds.filter(validRequestId) : [];
  await chrome.storage.local.set({ recentOperationIds: [requestId, ...ids.filter(id => id !== requestId)].slice(0, 5) });
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message.type !== 'string') return false;
  (async () => {
    if (message.type === 'save_extension_token') {
      if (!isPopupSender(sender) || typeof message.token !== 'string' || message.token.length < 32) {
        sendResponse({ ok: false, error: 'token_not_allowed' });
        return;
      }
      await chrome.storage.local.set({ [TOKEN_KEY]: message.token, connectorTokenConfigured: true });
      sendResponse({ ok: true });
      return;
    }
    if (message.type === 'get_handoff_enabled') {
      if (!isArenaSender(sender)) { sendResponse({ ok: false }); return; }
      const stored = await chrome.storage.local.get('arenaHandoffEnabled');
      sendResponse({ ok: true, enabled: stored.arenaHandoffEnabled === true });
      return;
    }
    if (message.type === 'set_handoff_enabled') {
      if (!isArenaSender(sender) || typeof message.enabled !== 'boolean') { sendResponse({ ok: false }); return; }
      await chrome.storage.local.set({ arenaHandoffEnabled: message.enabled });
      sendResponse({ ok: true, enabled: message.enabled });
      return;
    }
    if (message.type === 'connector_status') {
      sendResponse(await connectorFetch('/v1/extension/status'));
      return;
    }
    if (message.type === 'submit_command') {
      if (!isArenaSender(sender)) {
        sendResponse({ ok: false, error: 'sender_not_allowed' });
        return;
      }
      const command = COMMAND && COMMAND.validateCommand(message.command);
      if (!command || !validRequestId(message.requestId)) {
        sendResponse({ ok: false, error: 'operation_not_allowed' });
        return;
      }
      await rememberRequest(message.requestId);
      const result = await connectorFetch('/v1/operations', {
        method: 'POST',
        body: JSON.stringify({ requestId: message.requestId, ...command }),
      });
      if (result.statusCode === 409) {
        const existing = await connectorFetch(`/v1/operations/${message.requestId}`);
        if (existing.ok && existing.body.command) {
          sendResponse({ ok: true, duplicate: true, command: existing.body.command });
          return;
        }
      }
      sendResponse(result.ok
        ? { ok: true, command: result.body }
        : { ok: false, error: result.body.error || result.error || 'request_rejected' });
      return;
    }
    if (message.type === 'operation_status') {
      if ((!isArenaSender(sender) && !isPopupSender(sender)) || !validRequestId(message.requestId)) {
        sendResponse({ ok: false, error: 'request_not_allowed' });
        return;
      }
      const result = await connectorFetch(`/v1/operations/${message.requestId}`);
      sendResponse(result.ok ? { ok: true, command: result.body.command } : { ok: false, error: result.body.error || result.error });
      return;
    }
    sendResponse({ ok: false, error: 'message_not_allowed' });
  })().catch(() => sendResponse({ ok: false, error: 'extension_error' }));
  return true;
});
