import assert from 'node:assert/strict';
import test from 'node:test';
import worker, { TradingRoom, allowedOrigin, canIssueCommand, createSession, localDevelopment, parseSettings, parseSnapshot, verifySession, type Env } from '../worker/index';
import { DEFAULT_SETTINGS, type DashboardState } from '../shared/types';
import { createDemoSnapshot } from '../shared/demo';
import { evaluate } from '../shared/engine';

test('local authentication bypass cannot be enabled on public hosts', () => {
  assert.equal(localDevelopment(new Request('https://example.com/api/state'), { DEV_MODE: 'true' }), false);
  assert.equal(localDevelopment(new Request('http://localhost:8787/api/state'), { DEV_MODE: 'true' }), true);
  assert.equal(localDevelopment(new Request('http://localhost:8787/api/state'), {}), false);
});
test('mutation origin must match the configured public origin exactly', () => {
  assert.equal(allowedOrigin(new Request('https://gold.example/api/settings', { headers: { origin: 'https://evil.example' } }), {}), false);
  assert.equal(allowedOrigin(new Request('https://gold.example/api/settings'), {}), false);
  assert.equal(allowedOrigin(new Request('https://gold.example/api/settings', { headers: { origin: 'https://gold.example' } }), {}), true);
});
test('Vite origin is allowed only for explicit local development', () => {
  const request = new Request('http://127.0.0.1:8787/api/settings', { headers: { origin: 'http://127.0.0.1:5173' } });
  assert.equal(allowedOrigin(request, { DEV_MODE: 'true' }), true);
  assert.equal(allowedOrigin(request, {}), false);
  assert.equal(allowedOrigin(new Request('https://gold.example/api/settings', { headers: { origin: 'http://localhost:5173' } }), { DEV_MODE: 'true' }), false);
  assert.equal(allowedOrigin(new Request('http://localhost:8787/api/settings', { headers: { origin: 'https://attacker.invalid' } }), { DEV_MODE: 'true' }), false);
});
test('sessions reject tampering, different secrets and expiration', async () => {
  const now = Date.now(), secret = 'test-secret-with-at-least-thirty-two-characters';
  const token = await createSession(secret, now);
  assert.equal(await verifySession(token, secret, now + 1000), true);
  assert.equal(await verifySession(token.slice(0, -1) + (token.endsWith('0') ? '1' : '0'), secret, now + 1000), false);
  assert.equal(await verifySession(token, 'wrong-secret', now + 1000), false);
  assert.equal(await verifySession(token, secret, now + 9 * 3600000), false);
});
test('risk settings reject invalid limits and require exact live confirmation', () => {
  assert.throws(() => parseSettings({ riskPercent: 99 }, DEFAULT_SETTINGS));
  assert.throws(() => parseSettings({ maxPositions: 1.5 }, DEFAULT_SETTINGS));
  assert.throws(() => parseSettings({ allowLive: true }, DEFAULT_SETTINGS));
  assert.equal(parseSettings({ allowLive: true, liveConfirmation: 'LIVE XAUUSD' }, DEFAULT_SETTINGS).allowLive, true);
});
test('bridge rejects non-gold symbols, stale data, malformed candles and prices', () => {
  const now = Date.now(), good = createDemoSnapshot(now);
  assert.equal(parseSnapshot(good as unknown as Record<string, unknown>, now).symbol, good.symbol);
  assert.throws(() => parseSnapshot({ ...good, symbol: 'EURUSD' }, now));
  assert.throws(() => parseSnapshot({ ...good, timestamp: now - 60000 }, now));
  assert.throws(() => parseSnapshot({ ...good, candles: [] }, now));
  assert.throws(() => parseSnapshot({ ...good, ask: good.bid - 1 }, now));
  assert.throws(() => parseSnapshot({ ...good, tickValue: NaN }, now));
  assert.throws(() => parseSnapshot({ ...good, candles: good.candles.map((c, i) => i === 0 ? { ...c, high: 0 } : c) }, now));
});
test('command safety blocks disconnected, paused, stale, real and cooling-down accounts', () => {
  const now = Date.now(), snapshot = createDemoSnapshot(now), settings = { ...DEFAULT_SETTINGS, enabled: true };
  const state: DashboardState = { mode: 'mt5', connected: true, bridgeLastSeen: now, snapshot, settings, evaluation: evaluate(snapshot, settings, now), activity: [], trades: [], pendingCommand: null, version: 'test' };
  assert.equal(canIssueCommand(state, now, 0), true);
  assert.equal(canIssueCommand({ ...state, connected: false }, now, 0), false);
  assert.equal(canIssueCommand({ ...state, settings: { ...settings, enabled: false } }, now, 0), false);
  assert.equal(canIssueCommand({ ...state, bridgeLastSeen: now - 60000 }, now, 0), false);
  assert.equal(canIssueCommand({ ...state, snapshot: { ...snapshot, accountMode: 'real' } }, now, 0), false);
  assert.equal(canIssueCommand(state, now, now - 100), false);
  assert.equal(canIssueCommand({ ...state, mode: 'demo' }, now, 0), false);
});

function fixture(development = true) {
  const store = new Map<string, unknown>();
  const ctx = {
    blockConcurrencyWhile: (callback: () => Promise<void>) => callback(),
    storage: {
      get: async (key: string) => structuredClone(store.get(key)),
      put: async (key: string, value: unknown) => { store.set(key, structuredClone(value)); },
      delete: async (key: string) => store.delete(key),
    },
  } as unknown as DurableObjectState;
  const env = {
    DEV_MODE: development ? 'true' : undefined,
    APP_PASSWORD: 'test-owner-password-12345',
    SESSION_SECRET: 'test-session-secret-at-least-thirty-two-characters',
    ROOM: { idFromName: () => 'room', get: () => room },
  } as unknown as Env;
  const room = new TradingRoom(ctx, env);
  const origin = development ? 'http://localhost:8787' : 'https://gold.example';
  const call = (path: string, data?: unknown, headers: Record<string, string> = {}) => worker.fetch(new Request(origin + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: { ...(data !== undefined ? { origin, 'content-type': 'application/json' } : {}), ...headers },
    body: data === undefined ? undefined : JSON.stringify(data),
  }), env);
  return { call, env };
}

test('HTTP API keeps account private and enforces login, origin and signed session', async () => {
  const { call, env } = fixture(false);
  assert.equal((await call('/api/state')).status, 401);
  assert.equal((await call('/api/login', { password: env.APP_PASSWORD }, { origin: 'https://evil.example' })).status, 403);
  assert.equal((await call('/api/login', { password: 'wrong' })).status, 401);
  const login = await call('/api/login', { password: env.APP_PASSWORD });
  assert.equal(login.status, 200);
  const cookie = login.headers.get('set-cookie')!;
  assert.match(cookie, /HttpOnly; SameSite=Strict/);
  assert.match(cookie, /Secure/);
  assert.equal((await call('/api/state', undefined, { cookie })).status, 200);
  env.SESSION_SECRET = undefined;
  assert.equal((await call('/api/state', undefined, { cookie })).status, 503);
});

test('paired bridge serializes concurrent snapshots, requires acknowledgment and pins the account', async () => {
  const { call } = fixture();
  const pairResponse = await call('/api/connection/pair', {});
  assert.equal(pairResponse.status, 200);
  const pair = await pairResponse.json() as { token: string; bridgeUrl: string };
  assert.match(pair.bridgeUrl, /\/api\/bridge$/);
  const auth = { authorization: `Bearer ${pair.token}` };
  assert.equal((await call('/api/bridge/command')).status, 401);
  const snapshot = { ...createDemoSnapshot(), accountId: 'mt5-demo-123' };
  assert.equal((await call('/api/bridge/snapshot', snapshot, auth)).status, 200);
  const enable = await call('/api/settings', { enabled: true });
  assert.equal(enable.status, 200);
  const concurrent = await Promise.all(Array.from({ length: 5 }, () => call('/api/bridge/snapshot', snapshot, auth)));
  assert.ok(concurrent.every(response => response.status === 200));
  const deliveries = await Promise.all(Array.from({ length: 4 }, async () => (await (await call('/api/bridge/command', undefined, auth)).json()) as { command: { id: string } | null }));
  assert.equal(deliveries.filter(delivery => delivery.command).length, 1, 'only one bridge can claim a command');
  const first = deliveries.find(delivery => delivery.command)!;
  assert.ok(first.command, 'fresh enabled demo account should receive a single command');
  const second = await (await call('/api/bridge/command', undefined, auth)).json() as typeof first;
  assert.equal(second.command, null, 'delivered command must never be returned to another bridge');
  const state = await (await call('/api/state')).text();
  assert.equal(state.includes(pair.token), false, 'pairing token is never exposed in dashboard state');
  assert.equal((await call('/api/bridge/result', { commandId: first.command.id, status: 'rejected', message: 'test rejection' }, auth)).status, 200);
  const paused = await (await call('/api/state')).json() as DashboardState;
  assert.equal(paused.settings.enabled, false);
  assert.equal(paused.pendingCommand, null);
  assert.equal((await call('/api/bridge/snapshot', { ...snapshot, accountId: 'other-account' }, auth)).status, 409);
  assert.equal((await call('/api/bridge/snapshot', { ...snapshot, symbol: 'XAUUSD.m' }, auth)).status, 409);
  assert.equal((await call('/api/connection/disconnect', {})).status, 200);
  assert.equal((await call('/api/bridge/command', undefined, auth)).status, 401);
});

test('an in-flight fill remains auditable after pause and result acknowledgment is idempotent', async () => {
  const { call } = fixture();
  const pair = await (await call('/api/connection/pair', {})).json() as { token: string };
  const auth = { authorization: `Bearer ${pair.token}` };
  const snapshot = { ...createDemoSnapshot(), accountId: 'mt5-fill-race' };
  await call('/api/bridge/snapshot', snapshot, auth);
  await call('/api/settings', { enabled: true });
  await call('/api/bridge/snapshot', snapshot, auth);
  const { command } = await (await call('/api/bridge/command', undefined, auth)).json() as { command: { id: string; volume: number; referencePrice: number } };
  assert.ok(command);
  await call('/api/emergency-stop', {});
  assert.equal((await call('/api/bridge/result', { commandId: command.id, status: 'filled', ticket: '100', volume: command.volume * 2 }, auth)).status, 400);
  const fill = { commandId: command.id, status: 'filled', ticket: '100', price: command.referencePrice, volume: command.volume / 2 };
  assert.equal((await call('/api/bridge/result', fill, auth)).status, 200);
  const duplicate = await (await call('/api/bridge/result', fill, auth)).json() as { duplicate: boolean };
  assert.equal(duplicate.duplicate, true);
  let state = await (await call('/api/state')).json() as DashboardState;
  assert.equal(state.settings.enabled, false);
  assert.equal(state.trades.length, 1);
  assert.equal(state.trades[0].volume, command.volume / 2);
  const livePosition = { ticket: '100', symbol: snapshot.symbol, side: 'BUY' as const, volume: command.volume / 2, openPrice: command.referencePrice, currentPrice: snapshot.bid, profit: 12.34, stopLoss: snapshot.bid - 2, takeProfit: snapshot.bid + 4 };
  await call('/api/bridge/snapshot', { ...snapshot, timestamp: Date.now() + 1, positions: [livePosition] }, auth);
  state = await (await call('/api/state')).json() as DashboardState;
  assert.equal(state.trades[0].profit, 12.34);
  assert.equal(state.trades[0].profitKnown, true);
  await call('/api/bridge/snapshot', { ...snapshot, timestamp: Date.now() + 2 }, auth);
  state = await (await call('/api/state')).json() as DashboardState;
  assert.equal(state.trades[0].status, 'closed');
  assert.equal(state.trades[0].profitKnown, false, 'closing balance is not invented without MT5 deal history');
});

test('lost single-delivery response pauses automation until acknowledgment or re-pairing', async (t) => {
  let now = 1_800_000_000_000;
  t.mock.method(Date, 'now', () => now);
  const { call } = fixture();
  const pair = await (await call('/api/connection/pair', {})).json() as { token: string };
  const auth = { authorization: `Bearer ${pair.token}` };
  const snapshot = { ...createDemoSnapshot(now), accountId: 'mt5-unknown-delivery' };
  await call('/api/bridge/snapshot', snapshot, auth);
  await call('/api/settings', { enabled: true });
  await call('/api/bridge/snapshot', snapshot, auth);
  const { command } = await (await call('/api/bridge/command', undefined, auth)).json() as { command: { id: string } };
  assert.ok(command);
  now += 31_000;
  let state = await (await call('/api/state')).json() as DashboardState;
  assert.equal(state.settings.enabled, false);
  assert.equal(state.pendingCommand, null);
  await call('/api/bridge/snapshot', { ...snapshot, timestamp: now }, auth);
  assert.equal((await call('/api/settings', { enabled: true })).status, 409);
  assert.equal((await call('/api/bridge/result', { commandId: command.id, status: 'rejected', message: 'confirmed never submitted' }, auth)).status, 200);
  assert.equal((await call('/api/settings', { enabled: true })).status, 200);
  now += 24 * 3600000;
  assert.equal((await call('/api/bridge/command', undefined, auth)).status, 401);
  state = await (await call('/api/state')).json() as DashboardState;
  assert.equal(state.connected, false);
});
