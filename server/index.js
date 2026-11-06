'use strict';
const http = require('node:http');
const { randomUUID, randomBytes, timingSafeEqual } = require('node:crypto');

const HOST = '127.0.0.1';
const PORT = Number(process.env.PORT || 4317);
const TOKEN = process.env.CONNECTOR_TOKEN;
const EXTENSION_TOKEN = process.env.CONNECTOR_EXTENSION_TOKEN;
const HEARTBEAT_TTL_MS = 15_000;
const COMMAND_TTL_MS = 60_000;
const COMMAND_RETENTION_MS = 5 * 60_000;
const MAX_BODY_BYTES = 8 * 1024;
const MAX_COMMANDS = 128;
const MAX_PENDING_COMMANDS = 8;
const ALLOWED_SERVICES = new Set([
  'Workspace', 'ReplicatedStorage', 'ServerStorage', 'ServerScriptService',
  'StarterGui', 'StarterPack', 'StarterPlayer', 'Lighting', 'SoundService',
]);

function equalSecret(provided, expected) {
  if (typeof provided !== 'string' || typeof expected !== 'string') return false;
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}
function validOverview(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  if (keys.join(',') !== 'metadata,services') return false;
  const metadata = value.metadata;
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return false;
  if (Object.keys(metadata).sort().join(',') !== 'isStudio,placeId,universeId') return false;
  if (!Number.isSafeInteger(metadata.placeId) || metadata.placeId < 0) return false;
  if (!Number.isSafeInteger(metadata.universeId) || metadata.universeId < 0) return false;
  if (typeof metadata.isStudio !== 'boolean') return false;
  if (!Array.isArray(value.services) || value.services.length > ALLOWED_SERVICES.size) return false;
  const seen = new Set();
  for (const item of value.services) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return false;
    if (Object.keys(item).sort().join(',') !== 'className,directChildCount,name') return false;
    if (!ALLOWED_SERVICES.has(item.name) || seen.has(item.name)) return false;
    if (typeof item.className !== 'string' || item.className.length < 1 || item.className.length > 64) return false;
    if (!Number.isSafeInteger(item.directChildCount) || item.directChildCount < 0 || item.directChildCount > 100_000) return false;
    seen.add(item.name);
  }
  return true;
}
const MATERIALS = new Set(['Plastic', 'SmoothPlastic', 'Wood', 'WoodPlanks', 'Concrete', 'Brick', 'Metal', 'Grass', 'Ice', 'Glass', 'Neon', 'Sand', 'Slate', 'Marble']);
function validName(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 40
    && value.trim() === value && /^[A-Za-z0-9 _-]+$/.test(value);
}
function validRequestId(value) {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}
function safeOperation(command) {
  const fields = command.operation === 'create_part' ? { position: command.position, size: command.size, color: command.color, material: command.material, anchored: command.anchored, canCollide: command.canCollide } : {};
  return { requestId: command.requestId, operation: command.operation, name: command.name, ...fields, status: command.status, expiresAt: new Date(command.expiresAt).toISOString() };
}
function validVector(value, min, max, integer = false) {
  return !!value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === 'x,y,z'
    && ['x','y','z'].every(k => typeof value[k] === 'number' && Number.isFinite(value[k]) && value[k] >= min && value[k] <= max && (!integer || Number.isInteger(value[k])));
}
function validOperation(body) {
  if (!validName(body.name)) return false;
  if (body.operation === 'create_test_folder') return Object.keys(body).sort().join(',') === 'name,operation,requestId';
  return body.operation === 'create_part' && Object.keys(body).sort().join(',') === 'anchored,canCollide,color,material,name,operation,position,requestId,size'
    && validVector(body.position, -10000, 10000) && validVector(body.size, 0.1, 512) && validVector(body.color, 0, 255, true)
    && MATERIALS.has(body.material) && typeof body.anchored === 'boolean' && typeof body.canCollide === 'boolean';
}
function validExtensionOrigin(origin) {
  return typeof origin === 'string' && /^chrome-extension:\/\/[a-p]{32}$/.test(origin);
}
async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) {
      const error = new Error('body_too_large');
      error.statusCode = 413;
      throw error;
    }
    chunks.push(chunk);
  }
  let value;
  try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch {
    const error = new Error('invalid_json');
    error.statusCode = 400;
    throw error;
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    const error = new Error('invalid_body');
    error.statusCode = 400;
    throw error;
  }
  return value;
}
function createServer({ token = TOKEN, extensionToken = EXTENSION_TOKEN, now = Date.now, sessionTtlMs = HEARTBEAT_TTL_MS, commandTtlMs = COMMAND_TTL_MS } = {}) {
  let activeSession = null;
  const commands = new Map();
  const send = (res, code, body) => {
    const json = JSON.stringify(body);
    if (Buffer.byteLength(json) > MAX_BODY_BYTES) {
      res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
      res.end('{"error":"response_too_large"}');
      return;
    }
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(json);
  };
  const currentSession = () => {
    if (activeSession && activeSession.lastSeenAt !== null && now() - activeSession.lastSeenAt > sessionTtlMs) activeSession = null;
    return activeSession;
  };
  const liveSession = () => {
    const session = currentSession();
    return session && session.lastSeenAt !== null ? session : null;
  };
  const pruneCommands = () => {
    for (const [id, item] of commands) {
      if (item.status === 'pending' && now() > item.expiresAt) item.status = 'expired';
      if (now() - item.createdAt > COMMAND_RETENTION_MS) commands.delete(id);
    }
    while (commands.size >= MAX_COMMANDS) {
      const first = commands.keys().next().value;
      if (first === undefined) break;
      commands.delete(first);
    }
  };
  const getExtensionCommand = (id) => {
    const command = commands.get(id);
    if (!command) return null;
    if (command.status === 'pending' && now() > command.expiresAt) command.status = 'expired';
    const result = command.result ? { ...command.result } : null;
    return { requestId: id, operation: command.operation, name: command.name, status: command.status, createdAt: new Date(command.createdAt).toISOString(), expiresAt: new Date(command.expiresAt).toISOString(), result };
  };
  const route = (req) => {
    try { return new URL(req.url, 'http://127.0.0.1').pathname; }
    catch { return ''; }
  };
  const server = http.createServer(async (req, res) => {
    try {
      const origin = req.headers.origin;
      if (origin && validExtensionOrigin(origin)) {
        res.setHeader('Access-Control-Allow-Origin', origin);
        res.setHeader('Vary', 'Origin');
        res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
        res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type');
        res.setHeader('Access-Control-Max-Age', '600');
      }
      const path = route(req);
      if (req.method === 'OPTIONS' && path.startsWith('/v1/')) {
        if (!validExtensionOrigin(origin)) return send(res, 403, { error: 'origin_not_allowed' });
        res.writeHead(204, { 'Cache-Control': 'no-store' });
        return res.end();
      }
      if (req.method === 'GET' && path === '/health') {
        return send(res, 200, { ok: true, service: 'arena-roblox-connector' });
      }
      const auth = req.headers.authorization || '';
      const provided = auth.startsWith('Bearer ') ? auth.slice(7) : '';
      const pluginAuth = equalSecret(provided, token);
      const extensionAuth = equalSecret(provided, extensionToken);

      if (req.method === 'POST' && path === '/v1/operations') {
        if (!extensionAuth) return send(res, 401, { error: 'unauthorized' });
        pruneCommands();
        const body = await readJson(req);
        if (!validRequestId(body.requestId) || !validOperation(body)) {
          return send(res, 400, { error: 'invalid_operation' });
        }
        if (commands.has(body.requestId)) return send(res, 409, { error: 'duplicate_request' });
        const session = liveSession();
        if (!session) return send(res, 503, { error: 'studio_disconnected' });
        const pendingCount = [...commands.values()].filter(item => item.sessionId === session.id && item.status === 'pending').length;
        if (pendingCount >= MAX_PENDING_COMMANDS) return send(res, 429, { error: 'queue_full' });
        const createdAt = now();
        const command = {
          requestId: body.requestId,
          operation: body.operation,
          name: body.name,
          ...(body.operation === 'create_part' ? { position: body.position, size: body.size, color: body.color, material: body.material, anchored: body.anchored, canCollide: body.canCollide } : {}),
          sessionId: session.id,
          createdAt,
          expiresAt: createdAt + commandTtlMs,
          approvalToken: randomBytes(24).toString('base64url'),
          status: 'pending',
          result: null,
        };
        commands.set(command.requestId, command);
        return send(res, 202, { ok: true, requestId: command.requestId, status: command.status, expiresAt: new Date(command.expiresAt).toISOString() });
      }

      if (req.method === 'GET' && path === '/v1/extension/status') {
        if (!extensionAuth) return send(res, 401, { error: 'unauthorized' });
        const session = liveSession();
        const pendingCount = session ? [...commands.values()].filter(item => item.sessionId === session.id && item.status === 'pending').length : 0;
        return send(res, 200, { ok: true, studioConnected: Boolean(session), pendingCount });
      }
      const extensionCommandMatch = path.match(/^\/v1\/operations\/([0-9a-f-]{36})$/i);
      if (req.method === 'GET' && extensionCommandMatch) {
        if (!extensionAuth) return send(res, 401, { error: 'unauthorized' });
        pruneCommands();
        const command = getExtensionCommand(extensionCommandMatch[1]);
        if (!command) return send(res, 404, { error: 'request_not_found' });
        return send(res, 200, { ok: true, command });
      }

      if (!pluginAuth) return send(res, 401, { error: 'unauthorized' });
      if (req.method === 'GET' && path === '/v1/status') {
        const session = liveSession();
        return send(res, 200, {
          ok: true,
          studio: {
            connected: Boolean(session),
            sessionId: session ? session.id : null,
            lastHeartbeatAt: session ? new Date(session.lastSeenAt).toISOString() : null,
          },
          capabilities: ['status', 'project.overview', ...(extensionToken ? ['operation.create_test_folder', 'operation.create_part'] : [])],
          message: session ? 'A Studio plugin session is actively heartbeating.' : 'No active Studio plugin session.'
        });
      }
      if (req.method === 'POST' && path === '/v1/session/register') {
        const id = randomUUID();
        activeSession = { id, createdAt: now(), lastSeenAt: null, overview: null };
        return send(res, 201, { ok: true, sessionId: id, heartbeatIntervalMs: 5_000 });
      }
      if (req.method === 'POST' && path === '/v1/session/heartbeat') {
        const body = await readJson(req);
        const session = currentSession();
        if (!session) return send(res, 409, { error: 'session_expired', message: 'Register a new session.' });
        if (typeof body.sessionId !== 'string' || body.sessionId !== session.id) {
          return send(res, 409, { error: 'session_replaced', message: 'This plugin session is no longer active.' });
        }
        if (!validOverview(body.overview)) return send(res, 400, { error: 'invalid_overview' });
        session.lastSeenAt = now();
        session.overview = body.overview;
        pruneCommands();
        const command = [...commands.values()].find(item => item.sessionId === session.id && (item.status === 'pending' || item.status === 'approved'));
        return send(res, 200, {
          ok: true,
          sessionId: session.id,
          connected: true,
          lastHeartbeatAt: new Date(session.lastSeenAt).toISOString(),
          pendingOperation: command ? { ...safeOperation(command), approvalToken: command.approvalToken } : null,
        });
      }
      if (req.method === 'GET' && path === '/v1/project/overview') {
        const session = liveSession();
        if (!session) return send(res, 503, { error: 'studio_disconnected' });
        if (!session.overview) return send(res, 503, { error: 'overview_not_received' });
        return send(res, 200, {
          ok: true,
          sessionId: session.id,
          capturedAt: new Date(session.lastSeenAt).toISOString(),
          project: session.overview,
        });
      }
      const decisionMatch = path.match(/^\/v1\/operations\/([0-9a-f-]{36})\/decision$/i);
      if (req.method === 'POST' && decisionMatch) {
        const body = await readJson(req);
        if (Object.keys(body).sort().join(',') !== 'approvalToken,decision,sessionId'
          || !['approve', 'reject'].includes(body.decision)) return send(res, 400, { error: 'invalid_decision' });
        const session = liveSession();
        if (!session || body.sessionId !== session.id) return send(res, 409, { error: 'session_replaced_or_expired' });
        const command = commands.get(decisionMatch[1]);
        if (!command) return send(res, 404, { error: 'request_not_found' });
        if (command.sessionId !== session.id || !equalSecret(body.approvalToken, command.approvalToken)) return send(res, 403, { error: 'invalid_approval' });
        if (command.status === 'pending' && now() > command.expiresAt) command.status = 'expired';
        if (command.status === 'approved' && body.decision === 'approve') {
          return send(res, 200, { ok: true, approved: true, operation: command.operation, name: command.name });
        }
        if (command.status !== 'pending') return send(res, 409, { error: 'request_already_resolved', status: command.status });
        if (body.decision === 'reject') {
          command.status = 'rejected';
          command.result = { ok: false, message: 'Rejected by the user in Roblox Studio.' };
          return send(res, 200, { ok: true, approved: false, status: command.status });
        }
        if (body.decision !== 'approve') return send(res, 400, { error: 'invalid_decision' });
        command.status = 'approved';
        return send(res, 200, { ok: true, approved: true, operation: command.operation, name: command.name });
      }
      const resultMatch = path.match(/^\/v1\/operations\/([0-9a-f-]{36})\/result$/i);
      if (req.method === 'POST' && resultMatch) {
        const body = await readJson(req);
        const session = liveSession();
        if (!session || body.sessionId !== session.id) return send(res, 409, { error: 'session_replaced_or_expired' });
        const command = commands.get(resultMatch[1]);
        if (!command) return send(res, 404, { error: 'request_not_found' });
        if (command.sessionId !== session.id || !equalSecret(body.approvalToken, command.approvalToken)) return send(res, 403, { error: 'invalid_approval' });
        if (command.status === 'completed') return send(res, 200, { ok: true, accepted: true, duplicate: true });
        if (command.status !== 'approved') return send(res, 409, { error: 'request_not_approved' });
        const expectedKeys = body.ok === true
          ? 'approvalToken,createdName,message,ok,sessionId'
          : 'approvalToken,message,ok,sessionId';
        if (Object.keys(body).sort().join(',') !== expectedKeys
          || typeof body.ok !== 'boolean' || typeof body.message !== 'string' || body.message.length > 200
          || (body.ok && body.createdName !== command.name)) {
          return send(res, 400, { error: 'invalid_result' });
        }
        command.status = body.ok ? 'completed' : 'rejected';
        command.result = { ok: body.ok, message: body.message, createdName: body.ok ? body.createdName : null };
        return send(res, 200, { ok: true, accepted: true, duplicate: false });
      }
      return send(res, 404, { error: 'not_found' });
    } catch (error) {
      if (res.headersSent) return res.destroy();
      return send(res, error.statusCode || 500, { error: error.statusCode ? error.message : 'internal_error' });
    }
  });
  return server;
}

if (require.main === module) {
  if (!TOKEN || TOKEN.length < 32) {
    console.error('CONNECTOR_TOKEN must be configured with at least 32 characters.');
    process.exit(1);
  }
  if (!EXTENSION_TOKEN || EXTENSION_TOKEN.length < 32 || equalSecret(EXTENSION_TOKEN, TOKEN)) {
    console.error('CONNECTOR_EXTENSION_TOKEN must be a separate secret of at least 32 characters.');
    process.exit(1);
  }
  const server = createServer();
  server.requestTimeout = 5_000;
  server.headersTimeout = 6_000;
  server.listen(PORT, HOST, () => console.log(`Connector listening on http://${HOST}:${PORT} (loopback only)`));
  server.on('error', (err) => { console.error(`Connector server error: ${err.message}`); process.exitCode = 1; });
  process.on('SIGINT', () => server.close(() => process.exit(0)));
  process.on('SIGTERM', () => server.close(() => process.exit(0)));
}
module.exports = { createServer, HEARTBEAT_TTL_MS, COMMAND_TTL_MS, ALLOWED_SERVICES, validOverview, validName, validRequestId };