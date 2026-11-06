'use strict';
(() => {
  const P = globalThis.ArenaCommandParser;
  if (!P) return;
  const INSTRUCTION = [
    'Arena Roblox Connector handoff format (request only; it is not authorization):',
    'Answer normally. If and only if I explicitly request a test Folder or one basic Part, include exactly one fenced JSON command. Put exactly one JSON object between the marker lines; do not include both examples. Never output code or any other operation.',
    'Use these exact marker lines around your single JSON object:',
    P.START,
    P.END,
    'Folder schema example: {"operation":"create_test_folder","name":"ArenaTest"}',
    'Part schema example: {"operation":"create_part","name":"ArenaPlatform","position":{"x":0,"y":5,"z":0},"size":{"x":12,"y":1,"z":12},"color":{"x":80,"y":160,"z":220},"material":"SmoothPlastic","anchored":true,"canCollide":true}',
    'The only supported materials are Plastic, SmoothPlastic, Wood, WoodPlanks, Concrete, Brick, Metal, Grass, Ice, Glass, Neon, Sand, Slate, and Marble. Position components must be finite numbers from -10000 to 10000; size components finite numbers from 0.1 to 512; RGB components integers from 0 to 255. Use booleans for anchored and canCollide.',
    'A Folder or one basic Part will be created directly under Workspace only after I approve it in the Roblox Studio connector panel. Do not claim it was created before the connector reports success.'
  ].join('\n');
  const host = document.createElement('div');
  host.id = 'arena-roblox-connector-extension';
  const shadow = host.attachShadow({ mode: 'closed' });
  const style = document.createElement('style');
  style.textContent = `
    .panel{position:fixed;z-index:2147483647;right:16px;bottom:16px;width:min(360px,calc(100vw - 32px));box-sizing:border-box;padding:12px;border:1px solid #56647a;border-radius:12px;background:#171d29;color:#f3f6fb;box-shadow:0 8px 32px #0008;font:13px/1.4 system-ui,sans-serif}
    .top{display:flex;align-items:center;gap:8px;font-weight:700;font-size:14px}.top input{accent-color:#71a8ff}.status{margin-top:7px;color:#c4cee0;white-space:pre-wrap}.small{margin-top:6px;color:#9eabc1;font-size:11px}.draft{display:none;margin-top:8px;padding:6px 9px;border:0;border-radius:6px;background:#3b72b8;color:#fff;cursor:pointer}
  `;
  const panel = document.createElement('div');
  panel.className = 'panel';
  const top = document.createElement('label');
  top.className = 'top';
  const toggle = document.createElement('input');
  toggle.type = 'checkbox';
  const title = document.createElement('span');
  title.textContent = 'Enable Arena Direct handoff';
  top.append(toggle, title);
  const status = document.createElement('div');
  status.className = 'status';
  const note = document.createElement('div');
  note.className = 'small';
  note.textContent = 'One typed operation only. Approval is always required in Roblox Studio. No chat history is sent to the connector.';
  const draftButton = document.createElement('button');
  draftButton.className = 'draft';
  draftButton.type = 'button';
  draftButton.textContent = 'Draft result in Arena composer';
  panel.append(top, status, note, draftButton);
  shadow.append(style, panel);
  (document.documentElement || document.body).appendChild(host);

  let enabled = false;
  let latestCommand = null;
  let scanTimer = null;
  const handled = new WeakSet();
  const streamState = new WeakMap();
  let observer = null;

  function mode() {
    if (!/^\/(text|c)\//.test(location.pathname)) return 'unsupported';
    for (const combo of document.querySelectorAll('button[role="combobox"]')) {
      if (combo.offsetParent === null) continue;
      const label = (combo.innerText || combo.textContent || '').trim().toLowerCase();
      if (/\bdirect\b/.test(label)) return 'direct';
      if (/\b(battle|agent|side by side)\b/.test(label)) return 'unsupported';
    }
    // The fresh-chat path is Direct unless an explicit visible mode selector says otherwise.
    if (/^\/text\/direct(?:\/|$)/i.test(location.pathname)) return 'direct';
    return 'unknown';
  }
  function assistantTurns() {
    const list = document.querySelector('ol.flex-col-reverse')
      || [...document.querySelectorAll('ol')].find(el => el.querySelector('.prose'));
    if (!list) return [];
    return [...list.children].filter(item => {
      const carousel = item.querySelector('[role="region"][aria-roledescription="carousel"]');
      return (item.classList.contains('mx-auto') || !!carousel) && !!item.querySelector('.prose')
        && !item.classList.contains('justify-end');
    }).reverse();
  }
  function bodyFor(item) {
    const slide = item.querySelector('[aria-roledescription="slide"]');
    return slide ? slide.querySelector('.prose') : item.querySelector('.prose');
  }
  function stopIsVisible() {
    return [...document.querySelectorAll('button')].some(button => {
      if (button.offsetParent === null) return false;
      const label = `${button.getAttribute('aria-label') || ''} ${button.innerText || ''}`;
      return /stop generation|stop generating|arr[êe]ter/i.test(label);
    });
  }
  function isVisibleCodeBlock(pre) {
    if (!pre.isConnected || pre.closest('[aria-hidden="true"]')) return false;
    const style = getComputedStyle(pre);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    const rect = pre.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0
      && rect.bottom > 0 && rect.right > 0
      && rect.top < window.innerHeight && rect.left < window.innerWidth;
  }
  function codeBlocksFor(body) {
    return [...body.querySelectorAll('pre')].map(pre => ({
      element: pre,
      text: pre.textContent || '',
      visible: isVisibleCodeBlock(pre),
    }));
  }
  function renderStatus(text) { status.textContent = text; }
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const h = [...bytes].map(v => v.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }
  function setTextareaValue(editor, value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    if (!setter) return false;
    setter.call(editor, value);
    editor.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  }
  function appendInstruction() {
    if (!enabled || mode() !== 'direct') return;
    const editor = document.querySelector('form textarea');
    if (!editor) {
      renderStatus('Handoff enabled, but the expected Direct-mode composer was not found. No prompt was changed.');
      return;
    }
    if (editor.value.includes('Arena Roblox Connector handoff format')) return;
    const prefix = editor.value.trimEnd();
    if (!prefix) return;
    if (!setTextareaValue(editor, `${prefix}${prefix ? '\n\n' : ''}${INSTRUCTION}`)) {
      renderStatus('Could not safely identify the Arena composer; no request will be sent.');
    } else {
      renderStatus('Handoff instruction appended visibly to the outgoing prompt. No credential is included.');
    }
  }
  function installPromptHooks() {
    document.querySelectorAll('form').forEach(form => {
      if (form.dataset.arenaConnectorHook) return;
      form.dataset.arenaConnectorHook = '1';
      form.addEventListener('submit', appendInstruction, true);
      form.addEventListener('keydown', event => {
        if (event.target instanceof HTMLTextAreaElement && event.key === 'Enter'
          && !event.shiftKey && !event.isComposing) appendInstruction();
      }, true);
      form.addEventListener('pointerdown', event => {
        const button = event.target instanceof Element ? event.target.closest('button') : null;
        if (button && /send message|envoyer/i.test(button.getAttribute('aria-label') || '')) appendInstruction();
      }, true);
    });
  }
  function statusFor(command, requestId) {
    const labels = {
      pending: `Queued for Studio approval: Workspace/${command.name}.`,
      approved: `Approved in Studio; applying Workspace/${command.name}…`,
      completed: `Studio reports: created Workspace/${command.name}.`,
      rejected: `Studio reports: rejected or not created. ${command.result?.message || ''}`,
      expired: 'Request expired before a decision; no change was made.',
    };
    renderStatus(`${labels[command.status] || `Request status: ${command.status}.`}\nRequest ${requestId.slice(0, 8)}`);
    if (command.status === 'completed' || command.status === 'rejected' || command.status === 'expired') {
      latestCommand = { text: labels[command.status] || command.status };
      draftButton.style.display = 'inline-block';
    }
  }
  async function pollOperation(requestId) {
    const result = await chrome.runtime.sendMessage({ type: 'operation_status', requestId });
    if (!result?.ok || !result.command) {
      renderStatus(result?.error === 'request_not_found'
        ? 'Request status is unavailable (server restarted or record expired). Check Workspace before retrying.'
        : `Could not refresh request status: ${result?.error || 'connector unavailable'}`);
      return;
    }
    statusFor(result.command, requestId);
    if (result.command.status === 'pending' || result.command.status === 'approved') setTimeout(() => pollOperation(requestId), 3000);
  }
  async function submitCommand(command) {
    const requestId = uuid();
    renderStatus(`Structured request detected: ${command.operation === 'create_part' ? 'create Part' : 'create Folder'} \"${command.name}\". Submitting only the typed command for Studio approval…`);
    const result = await chrome.runtime.sendMessage({ type: 'submit_command', requestId, command });
    if (!result?.ok) {
      renderStatus(`Not submitted: ${result?.error || 'connector unavailable'}. No Studio change was made.`);
      return;
    }
    const accepted = result.command;
    if (accepted?.status) {
      statusFor({ ...command, status: accepted.status }, requestId);
      setTimeout(() => pollOperation(requestId), 1000);
    } else if (accepted?.command) {
      statusFor(accepted.command, requestId);
      setTimeout(() => pollOperation(requestId), 1000);
    }
  }
  function scan() {
    installPromptHooks();
    if (!enabled) {
      renderStatus('Off. Turn on handoff to append a visible command-format instruction and monitor new Direct replies.');
      return;
    }
    const currentMode = mode();
    if (currentMode !== 'direct') {
      renderStatus(currentMode === 'unsupported'
        ? 'Handoff is limited to Arena Direct chat. No responses or prompts are processed in this mode.'
        : 'Could not confirm Arena Direct mode. Handoff is paused (fail-closed).');
      return;
    }
    const turns = assistantTurns();
    if (!turns.length) {
      renderStatus('Arena Direct is active. Waiting for a new assistant response.');
      return;
    }
    const item = turns[turns.length - 1];
    if (handled.has(item)) return;
    const body = bodyFor(item);
    if (!body) return;
    const blocks = codeBlocksFor(body);
    const signature = P.renderedResponseSignature(body.textContent || '', blocks);
    let state = streamState.get(item);
    const time = Date.now();
    if (!state || state.signature !== signature) {
      state = { signature, changedAt: time };
      streamState.set(item, state);
      return;
    }
    if (stopIsVisible() || time - state.changedAt < 1800) return;
    if (!P.claimOnce(handled, item)) return;
    const parsed = P.parseVisibleCommandBlocks(blocks);
    if (parsed.kind === 'none') return;
    if (parsed.kind !== 'command') {
      const stats = parsed.stats || {};
      renderStatus(`A visible command marker failed strict validation (${parsed.error}; visible code blocks=${stats.visibleCodeBlocks ?? 0}, command blocks=${stats.commandBlocks ?? 0}, repeated DOM references=${stats.duplicateNodeReferences ?? 0}, nested equivalent copies=${stats.nestedEquivalentCopies ?? 0}). Nothing was sent.`);
      return;
    }
    submitCommand(parsed.command);
  }

  toggle.addEventListener('change', async () => {
    enabled = toggle.checked;
    const stored = await chrome.runtime.sendMessage({ type: 'set_handoff_enabled', enabled });
    if (!stored?.ok) {
      enabled = false;
      toggle.checked = false;
      renderStatus('Could not save the handoff setting in the extension worker.');
      return;
    }
    if (enabled) {
      // Baseline current assistant turns so loading/reopening a conversation never replays history.
      assistantTurns().forEach(item => handled.add(item));
      renderStatus(mode() === 'direct'
        ? 'Enabled. New Direct prompts will visibly include the handoff format; only new replies are inspected.'
        : 'Enabled, but waiting until Arena Direct can be confirmed.');
    } else {
      renderStatus('Off. No Arena response text is monitored.');
    }
  });
  draftButton.addEventListener('click', () => {
    if (!latestCommand) return;
    const editor = document.querySelector('form textarea');
    if (!editor) { renderStatus('Arena composer not found; result remains available in this extension panel.'); return; }
    const prefix = editor.value.trimEnd();
    setTextareaValue(editor, `${prefix}${prefix ? '\n\n' : ''}${latestCommand.text}`);
    renderStatus('Result added as an unsent draft. Review it before sending to Arena.');
  });
  chrome.runtime.sendMessage({ type: 'get_handoff_enabled' }).then(value => {
    enabled = value?.ok === true && value.enabled === true;
    toggle.checked = enabled;
    if (enabled) assistantTurns().forEach(item => handled.add(item));
    scan();
  }).catch(() => renderStatus('Could not read the handoff setting from the extension worker.'));
  observer = new MutationObserver(() => scan());
  observer.observe(document.documentElement, { subtree: true, childList: true, characterData: true });
  scanTimer = setInterval(scan, 750);
  window.addEventListener('pagehide', () => {
    if (observer) observer.disconnect();
    if (scanTimer) clearInterval(scanTimer);
  }, { once: true });
})();
