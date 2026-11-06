'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createServer } = require('../server');
const TOKEN = 'a'.repeat(44);
const EXTENSION_TOKEN = 'b'.repeat(48);
const overview = {
  metadata: { placeId: 123, universeId: 456, isStudio: true },
  services: [
    { name: 'Workspace', className: 'Workspace', directChildCount: 4 },
    { name: 'ReplicatedStorage', className: 'ReplicatedStorage', directChildCount: 2 },
  ],
};
async function withServer(fn, options = {}) {
  const server = createServer({ token: TOKEN, extensionToken: EXTENSION_TOKEN, ...options });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try { await fn(base); } finally { await new Promise(resolve => server.close(resolve)); }
}
const auth = (token = TOKEN) => ({ Authorization: `Bearer ${token}` });
const extensionAuth = () => auth(EXTENSION_TOKEN);
const jsonRequest = body => ({ headers: { 'Content-Type': 'application/json', ...auth() }, body: JSON.stringify(body) });
const extensionJsonRequest = body => ({ headers: { 'Content-Type': 'application/json', ...extensionAuth() }, body: JSON.stringify(body) });
async function register(base) {
  const res = await fetch(`${base}/v1/session/register`, { method: 'POST', headers: auth() });
  assert.equal(res.status, 201);
  return (await res.json()).sessionId;
}
async function heartbeat(base, sessionId, project = overview) {
  return fetch(`${base}/v1/session/heartbeat`, { method: 'POST', ...jsonRequest({ sessionId, overview: project }) });
}

test('health remains unauthenticated and status requires bearer authentication', async () => withServer(async base => {
  const health = await fetch(`${base}/health`);
  assert.equal(health.status, 200);
  assert.equal((await health.json()).ok, true);
  assert.equal((await fetch(`${base}/v1/status`)).status, 401);
  assert.equal((await fetch(`${base}/v1/status`, { headers: auth('wrong') })).status, 401);
}));
test('authenticated server status is disconnected until a plugin registers', async () => withServer(async base => {
  const res = await fetch(`${base}/v1/status`, { headers: auth() });
  const data = await res.json();
  assert.equal(data.studio.connected, false);
  assert.equal(data.studio.sessionId, null);
}));
test('registration is authenticated and returns a unique session id', async () => withServer(async base => {
  assert.equal((await fetch(`${base}/v1/session/register`, { method: 'POST' })).status, 401);
  const first = await register(base);
  const second = await register(base);
  assert.match(first, /^[0-9a-f-]{36}$/i);
  assert.notEqual(first, second);
}));
test('registering without a heartbeat does not count as connected', async () => withServer(async base => {
  await register(base);
  const status = await (await fetch(`${base}/v1/status`, { headers: auth() })).json();
  assert.equal(status.studio.connected, false);
  assert.equal((await fetch(`${base}/v1/project/overview`, { headers: auth() })).status, 503);
}));
test('heartbeat and project overview require authentication', async () => withServer(async base => {
  assert.equal((await fetch(`${base}/v1/session/heartbeat`, { method: 'POST' })).status, 401);
  assert.equal((await fetch(`${base}/v1/project/overview`)).status, 401);
}));
test('heartbeat authenticates, validates session and overview contract, and status becomes live', async () => withServer(async base => {
  const sessionId = await register(base);
  assert.equal((await heartbeat(base, sessionId)).status, 200);
  const status = await (await fetch(`${base}/v1/status`, { headers: auth() })).json();
  assert.equal(status.studio.connected, true);
  assert.equal(status.studio.sessionId, sessionId);
  const result = await (await fetch(`${base}/v1/project/overview`, { headers: auth() })).json();
  assert.deepEqual(result.project, overview);
  assert.equal(result.sessionId, sessionId);
}));
test('old session cannot heartbeat after a newer registration replaces it', async () => withServer(async base => {
  const oldId = await register(base);
  const newId = await register(base);
  const oldResult = await heartbeat(base, oldId);
  assert.equal(oldResult.status, 409);
  assert.equal((await oldResult.json()).error, 'session_replaced');
  assert.equal((await heartbeat(base, newId)).status, 200);
}));
test('stale heartbeat expires and overview is unavailable', async () => {
  let now = 1_000_000;
  await withServer(async base => {
    const sessionId = await register(base);
    assert.equal((await heartbeat(base, sessionId)).status, 200);
    now += 15_001;
    const status = await (await fetch(`${base}/v1/status`, { headers: auth() })).json();
    assert.equal(status.studio.connected, false);
    assert.equal((await fetch(`${base}/v1/project/overview`, { headers: auth() })).status, 503);
    const stale = await heartbeat(base, sessionId);
    assert.equal(stale.status, 409);
    assert.equal((await stale.json()).error, 'session_expired');
  }, { now: () => now, sessionTtlMs: 15_000 });
});
test('overview rejects unallowlisted services, duplicate names, invalid counts and extra fields', async () => withServer(async base => {
  const sessionId = await register(base);
  for (const invalid of [
    { ...overview, extra: true },
    { ...overview, services: [{ name: 'Players', className: 'Players', directChildCount: 1 }] },
    { ...overview, services: [overview.services[0], overview.services[0]] },
    { ...overview, services: [{ name: 'Workspace', className: 'Workspace', directChildCount: -1 }] },
  ]) {
    const res = await heartbeat(base, sessionId, invalid);
    assert.equal(res.status, 400);
    assert.equal((await res.json()).error, 'invalid_overview');
  }
}));
test('oversized and malformed request bodies are rejected', async () => withServer(async base => {
  const sessionId = await register(base);
  const oversized = await fetch(`${base}/v1/session/heartbeat`, {
    method: 'POST', headers: { ...auth(), 'Content-Type': 'application/json' }, body: 'x'.repeat(9000),
  });
  assert.equal(oversized.status, 413);
  const malformed = await fetch(`${base}/v1/session/heartbeat`, {
    method: 'POST', headers: { ...auth(), 'Content-Type': 'application/json' }, body: '{',
  });
  assert.equal(malformed.status, 400);
  assert.ok(sessionId);
}));
test('extension and Studio credentials are separate and extension status is data-minimized', async () => withServer(async base => {
  assert.equal((await fetch(`${base}/v1/extension/status`, { headers: auth() })).status, 401);
  assert.equal((await fetch(`${base}/v1/status`, { headers: extensionAuth() })).status, 401);
  const result = await fetch(`${base}/v1/extension/status`, { headers: extensionAuth() });
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), { ok: true, studioConnected: false, pendingCount: 0 });
}));

test('only the typed create-folder operation is queued, and a duplicate request id is rejected', async () => withServer(async base => {
  const sessionId = await register(base);
  assert.equal((await heartbeat(base, sessionId)).status, 200);
  const requestId = 'c31af39d-6ed0-4cab-9d4f-85ee4dd58106';
  const body = { requestId, operation: 'create_test_folder', name: 'Arena Test' };
  assert.equal((await fetch(`${base}/v1/operations`, { method: 'POST', headers: auth(), body: JSON.stringify(body) })).status, 401);
  for (const invalid of [
    { ...body, operation: 'execute_luau' },
    { ...body, name: 'bad/name' },
    { ...body, name: ' leading' },
    { ...body, extra: 'code' },
    { ...body, requestId: 'not-a-uuid' },
  ]) {
    const rejected = await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(invalid) });
    assert.equal(rejected.status, 400);
  }
  const queued = await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(body) });
  assert.equal(queued.status, 202);
  const duplicate = await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(body) });
  assert.equal(duplicate.status, 409);
  assert.equal((await duplicate.json()).error, 'duplicate_request');
  const status = await (await fetch(`${base}/v1/operations/${requestId}`, { headers: extensionAuth() })).json();
  assert.equal(status.command.status, 'pending');
  assert.equal(Object.hasOwn(status.command, 'approvalToken'), false);
}));

test('create_part validates its exact schema, bounds, types, material and duplicate IDs', async () => withServer(async base => {
  const sessionId = await register(base); await heartbeat(base, sessionId);
  const requestId = 'd31af39d-6ed0-4cab-9d4f-85ee4dd58106';
  const body = { requestId, operation: 'create_part', name: 'ArenaPlatform', position: { x: 0, y: 5, z: -10 }, size: { x: 12, y: 1, z: 12 }, color: { x: 80, y: 160, z: 220 }, material: 'SmoothPlastic', anchored: true, canCollide: true };
  const invalid = [
    { ...body, name: '../bad' }, { ...body, position: { x: 10001, y: 0, z: 0 } },
    { ...body, position: { x: '0', y: 0, z: 0 } }, { ...body, position: { x: NaN, y: 0, z: 0 } },
    { ...body, size: { x: 0, y: 1, z: 1 } }, { ...body, size: { x: 513, y: 1, z: 1 } },
    { ...body, color: { x: 256, y: 0, z: 0 } }, { ...body, color: { x: 1.5, y: 0, z: 0 } },
    { ...body, material: 'Air' }, { ...body, anchored: 'true' }, { ...body, canCollide: 0 },
    { ...body, extra: true }, { ...body, size: undefined },
  ];
  for (const value of invalid) assert.equal((await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(value) })).status, 400);
  assert.equal((await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest({ ...body, name: 'x'.repeat(41) }) })).status, 400);
  assert.equal((await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest({ ...body, name: 'x'.repeat(9000) }) })).status, 413);
  const queued = await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(body) });
  assert.equal(queued.status, 202);
  const pending = await (await heartbeat(base, sessionId)).json();
  assert.deepEqual({ operation: pending.pendingOperation.operation, name: pending.pendingOperation.name, position: pending.pendingOperation.position, size: pending.pendingOperation.size, color: pending.pendingOperation.color, material: pending.pendingOperation.material, anchored: pending.pendingOperation.anchored, canCollide: pending.pendingOperation.canCollide }, body && (({ operation, name, position, size, color, material, anchored, canCollide }) => ({ operation, name, position, size, color, material, anchored, canCollide }))(body));
  assert.equal((await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(body) })).status, 409);
}));

test('create_part requires a live Studio session and expires without approval', async () => {
  let now = 1000;
  await withServer(async base => {
    const requestId = 'e31af39d-6ed0-4cab-9d4f-85ee4dd58106';
    const part = { requestId, operation: 'create_part', name: 'ExpiryBlock', position: { x: 0, y: 0, z: 0 }, size: { x: 2, y: 2, z: 2 }, color: { x: 10, y: 20, z: 30 }, material: 'Plastic', anchored: true, canCollide: true };
    assert.equal((await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(part) })).status, 503);
    const sessionId = await register(base); await heartbeat(base, sessionId);
    assert.equal((await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(part) })).status, 202);
    now += 60_001;
    assert.equal((await (await fetch(`${base}/v1/operations/${requestId}`, { headers: extensionAuth() })).json()).command.status, 'expired');
    const rejectionId = 'f31af39d-6ed0-4cab-9d4f-85ee4dd58106';
    const rejection = await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest({ ...part, requestId: rejectionId, name: 'RejectBlock' }) });
    assert.equal(rejection.status, 503);
  }, { now: () => now });
});

test('Studio rejection of create_part is terminal and reports no applied mutation', async () => withServer(async base => {
  const sessionId = await register(base); await heartbeat(base, sessionId);
  const requestId = 'a31af39d-6ed0-4cab-9d4f-85ee4dd58106';
  const body = { requestId, operation: 'create_part', name: 'RejectedBlock', position: { x: 0, y: 1, z: 0 }, size: { x: 1, y: 1, z: 1 }, color: { x: 0, y: 255, z: 0 }, material: 'Plastic', anchored: true, canCollide: true };
  assert.equal((await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(body) })).status, 202);
  const pending = await (await heartbeat(base, sessionId)).json();
  const response = await fetch(`${base}/v1/operations/${requestId}/decision`, { method: 'POST', ...jsonRequest({ sessionId, approvalToken: pending.pendingOperation.approvalToken, decision: 'reject' }) });
  assert.equal(response.status, 200);
  const status = await (await fetch(`${base}/v1/operations/${requestId}`, { headers: extensionAuth() })).json();
  assert.equal(status.command.status, 'rejected');
  assert.equal(status.command.result.ok, false);
}));

test('command requires a fresh Studio heartbeat and expires when no approval arrives', async () => {
  let now = 5000;
  await withServer(async base => {
    const requestId = 'c31af39d-6ed0-4cab-9d4f-85ee4dd58106';
    const body = { requestId, operation: 'create_test_folder', name: 'ArenaTest' };
    const disconnected = await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(body) });
    assert.equal(disconnected.status, 503);
    const sessionId = await register(base);
    await heartbeat(base, sessionId);
    const queued = await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(body) });
    assert.equal(queued.status, 202);
    now += 60_001;
    const expired = await fetch(`${base}/v1/operations/${requestId}`, { headers: extensionAuth() });
    assert.equal((await expired.json()).command.status, 'expired');
    const duplicate = await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(body) });
    assert.equal(duplicate.status, 409);
  }, { now: () => now });
});

test('Studio-only approval token gates the allowlisted mutation and result returns to extension', async () => withServer(async base => {
  const sessionId = await register(base);
  await heartbeat(base, sessionId);
  const requestId = 'c31af39d-6ed0-4cab-9d4f-85ee4dd58106';
  const body = { requestId, operation: 'create_test_folder', name: 'ArenaTest' };
  assert.equal((await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest(body) })).status, 202);
  const heartbeatResponse = await heartbeat(base, sessionId);
  const pending = await heartbeatResponse.json();
  assert.equal(pending.pendingOperation.requestId, requestId);
  assert.ok(pending.pendingOperation.approvalToken);
  const decisionUrl = `${base}/v1/operations/${requestId}/decision`;
  assert.equal((await fetch(decisionUrl, { method: 'POST', ...extensionJsonRequest({ sessionId, approvalToken: pending.pendingOperation.approvalToken, decision: 'approve' }) })).status, 401);
  const missingApproval = await fetch(decisionUrl, { method: 'POST', ...jsonRequest({ sessionId, approvalToken: 'wrong', decision: 'approve' }) });
  assert.equal(missingApproval.status, 403);
  const approvedResponse = await fetch(decisionUrl, { method: 'POST', ...jsonRequest({ sessionId, approvalToken: pending.pendingOperation.approvalToken, decision: 'approve' }) });
  assert.equal(approvedResponse.status, 200);
  assert.deepEqual(await approvedResponse.json(), { ok: true, approved: true, operation: 'create_test_folder', name: 'ArenaTest' });
  const resultUrl = `${base}/v1/operations/${requestId}/result`;
  const resultBody = { sessionId, approvalToken: pending.pendingOperation.approvalToken, ok: true, message: 'Created one Folder under Workspace.', createdName: 'ArenaTest' };
  assert.equal((await fetch(resultUrl, { method: 'POST', ...jsonRequest(resultBody) })).status, 200);
  const result = await (await fetch(`${base}/v1/operations/${requestId}`, { headers: extensionAuth() })).json();
  assert.equal(result.command.status, 'completed');
  assert.equal(result.command.result.createdName, 'ArenaTest');
}));

test('user rejection is terminal and never reports an applied mutation', async () => withServer(async base => {
  const sessionId = await register(base);
  await heartbeat(base, sessionId);
  const requestId = 'c31af39d-6ed0-4cab-9d4f-85ee4dd58106';
  await fetch(`${base}/v1/operations`, { method: 'POST', ...extensionJsonRequest({ requestId, operation: 'create_test_folder', name: 'ArenaTest' }) });
  const pending = await (await heartbeat(base, sessionId)).json();
  const decision = await fetch(`${base}/v1/operations/${requestId}/decision`, { method: 'POST', ...jsonRequest({ sessionId, approvalToken: pending.pendingOperation.approvalToken, decision: 'reject' }) });
  assert.equal(decision.status, 200);
  const status = await (await fetch(`${base}/v1/operations/${requestId}`, { headers: extensionAuth() })).json();
  assert.equal(status.command.status, 'rejected');
  assert.equal(status.command.result.ok, false);
  assert.equal((await fetch(`${base}/v1/operations/${requestId}/decision`, { method: 'POST', ...jsonRequest({ sessionId, approvalToken: pending.pendingOperation.approvalToken, decision: 'approve' }) })).status, 409);
}));

test('extension preflight is limited to chrome-extension origins', async () => withServer(async base => {
  const good = await fetch(`${base}/v1/operations`, { method: 'OPTIONS', headers: { Origin: 'chrome-extension://abcdefghijklmnopabcdefghijklmnop' } });
  assert.equal(good.status, 204);
  assert.equal(good.headers.get('access-control-allow-origin'), 'chrome-extension://abcdefghijklmnopabcdefghijklmnop');
  const bad = await fetch(`${base}/v1/operations`, { method: 'OPTIONS', headers: { Origin: 'https://arena.ai' } });
  assert.equal(bad.status, 403);
}));

test('unsupported operations and paths are not exposed', async () => withServer(async base => {
  assert.equal((await fetch(`${base}/v1/execute`, { headers: auth() })).status, 404);
  assert.equal((await fetch(`${base}/v1/session/register`, { method: 'DELETE', headers: auth() })).status, 404);
  assert.equal((await fetch(`${base}/v1/project/overview`, { method: 'POST', headers: auth() })).status, 404);
}));
