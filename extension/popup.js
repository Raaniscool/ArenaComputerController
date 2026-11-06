'use strict';
const tokenInput = document.getElementById('token');
const status = document.getElementById('status');
chrome.storage.local.get('connectorTokenConfigured').then(value => {
  if (value.connectorTokenConfigured === true) tokenInput.placeholder = 'Configured (enter a new token to rotate)';
});
document.getElementById('save').addEventListener('click', async () => {
  const token = tokenInput.value.trim();
  if (token.length < 32) {
    status.textContent = 'Enter a token with at least 32 characters.';
    return;
  }
  const saved = await chrome.runtime.sendMessage({ type: 'save_extension_token', token });
  if (!saved?.ok) {
    status.textContent = 'Token could not be saved by the extension worker.';
    return;
  }
  tokenInput.value = '';
  tokenInput.placeholder = 'Configured (enter a new token to rotate)';
  status.textContent = 'Saved. The token is not sent to the page content script.';
});
document.getElementById('check').addEventListener('click', async () => {
  status.textContent = 'Checking loopback connector…';
  const result = await chrome.runtime.sendMessage({ type: 'connector_status' });
  if (!result || !result.ok) {
    status.textContent = `Connector unavailable: ${result?.error || result?.body?.error || 'check token and connector'}`;
    return;
  }
  const data = result.body;
  const lines = [`Connector reachable. Studio: ${data.studioConnected ? 'connected' : 'not connected'}; pending approvals: ${data.pendingCount}.`];
  const stored = await chrome.storage.local.get('recentOperationIds');
  const ids = Array.isArray(stored.recentOperationIds) ? stored.recentOperationIds.slice(0, 5) : [];
  for (const requestId of ids) {
    const current = await chrome.runtime.sendMessage({ type: 'operation_status', requestId });
    if (!current?.ok || !current.command) {
      lines.push(`Request ${requestId.slice(0, 8)}: status unavailable (expired or server restarted).`);
      continue;
    }
    const command = current.command;
    const outcome = command.result?.message ? ` — ${command.result.message}` : '';
    lines.push(`${command.status}: Workspace/${command.name}${outcome}`);
  }
  status.textContent = lines.join('\n');
});
