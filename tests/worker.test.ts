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
test('bridge rejects non-gold symbols, malformed candles and prices', () => {
  const now = Date.now(), good = createDemoSnapshot(now);
  assert.equal(parseSnapshot(good as unknown as Record<string, unknown>, now).symbol, good.symbol);
  assert.throws(() => parseSnapshot({ ...good, symbol: 'EURUSD' }, now));
  assert.throws(() => parseSnapshot({ ...good, candles: [] }, now));
  assert.throws(() => parseSnapshot({ ...good, ask: good.bid - 1 }, now));
  assert.throws(() => parseSnapshot({ ...good, tickValue: NaN }, now));
  assert.throws(() => parseSnapshot({ ...good, candles: good.candles.map((c, i) => i === 0 ? { ...c, high: 0 } : c) }, now));
});
test('daily P&L conservative flag is optional and strictly boolean when supplied', () => {
  const now = Date.now(), good = createDemoSnapshot(now);
  assert.equal(parseSnapshot(good as unknown as Record<string, unknown>, now).dailyPnlConservative, undefined);
  for (const dailyPnlConservative of [true, false]) assert.equal(parseSnapshot({ ...good, dailyPnlConservative, dailyPnlUpperBound: good.dailyPnl }, now).dailyPnlConservative, dailyPnlConservative);
  for (const dailyPnlConservative of ['true', 'false', 0, 1, null, {}, []]) assert.throws(() => parseSnapshot({ ...good, dailyPnlConservative }, now));
});
test('conservative daily P&L requires a finite ordered upper bound and legacy exact values stay compatible', () => {
  const now = Date.now(), good = createDemoSnapshot(now);
  for (const dailyPnlConservative of [true, false, undefined]) {
    const snapshot = { ...good, dailyPnl: -100, dailyPnlUpperBound: 200, ...(dailyPnlConservative === undefined ? {} : { dailyPnlConservative }) };
    assert.equal(parseSnapshot(snapshot, now).dailyPnlUpperBound, 200);
  }
  assert.equal(parseSnapshot({ ...good, dailyPnlConservative: false }, now).dailyPnlUpperBound, undefined);
  assert.throws(() => parseSnapshot({ ...good, dailyPnlConservative: true }, now));
  for (const dailyPnlUpperBound of [-1, -1e12 - 1, 1e12 + 1, NaN, Infinity, '0', null]) {
    for (const dailyPnlConservative of [true, false]) assert.throws(() => parseSnapshot({ ...good, dailyPnlConservative, dailyPnlUpperBound }, now));
  }
});
test('quote telemetry retains historical and bounded future times but rejects impossible timestamps', () => {
  const now = Date.now(), good = createDemoSnapshot(now);
  for (const timestamp of [now - 3 * 86400000, now - 60000, now + 60000, now + 86400000]) {
    assert.equal(parseSnapshot({ ...good, timestamp }, now).timestamp, timestamp);
  }
  for (const timestamp of [0, -1, Date.UTC(2000, 0, 1) - 1, now + 86400001, NaN, Infinity, String(now), null]) {
    assert.throws(() => parseSnapshot({ ...good, timestamp }, now));
  }
  const futureBars = good.candles.map(candle => ({ ...candle, time: candle.time + 3600 }));
  assert.deepEqual(parseSnapshot({ ...good, timestamp: now + 3600000, candles: futureBars }, now).candles, futureBars);
  assert.throws(() => parseSnapshot({ ...good, candles: good.candles.map(candle => ({ ...candle, time: candle.time + 87000 })) }, now));
});
test('explicit broker UTC metadata must be paired, bounded and match the normalized quote time', () => {
  const now = Date.now(), good = createDemoSnapshot(now);
  for (const brokerUtcOffsetMinutes of [-720, 0, 180, 840]) {
    const brokerTimestamp = now + brokerUtcOffsetMinutes * 60000;
    const parsed = parseSnapshot({ ...good, brokerTimestamp, brokerUtcOffsetMinutes }, now);
    assert.equal(parsed.timestamp, now);
    assert.equal(parsed.brokerTimestamp, brokerTimestamp);
    assert.equal(parsed.brokerUtcOffsetMinutes, brokerUtcOffsetMinutes);
  }
  assert.equal(parseSnapshot({ ...good, brokerTimestamp: now + 180 * 60000 + 1, brokerUtcOffsetMinutes: 180 }, now).timestamp, now);
  for (const metadata of [
    { brokerTimestamp: now },
    { brokerUtcOffsetMinutes: 0 },
    { brokerTimestamp: now, brokerUtcOffsetMinutes: 180 },
    { brokerTimestamp: now + 180 * 60000 + 2, brokerUtcOffsetMinutes: 180 },
    ...[-780, 900, 150, 180.5, NaN, Infinity, '180', null].map(brokerUtcOffsetMinutes => ({ brokerTimestamp: now, brokerUtcOffsetMinutes })),
    ...[0, -1, NaN, Infinity, String(now), null].map(brokerTimestamp => ({ brokerTimestamp, brokerUtcOffsetMinutes: 0 })),
  ]) assert.throws(() => parseSnapshot({ ...good, ...metadata }, now));
});
test('command safety blocks disconnected, paused, stale, real and cooling-down accounts', () => {
  const now = Date.now(), snapshot = createDemoSnapshot(now), settings = { ...DEFAULT_SETTINGS, enabled: true };
  const state: DashboardState = { mode: 'mt5', connected: true, bridgeLastSeen: now, snapshot, settings, evaluation: evaluate(snapshot, settings, now), activity: [], trades: [], pendingCommand: null, version: 'test' };
  assert.equal(canIssueCommand(state, now, 0), true);
  assert.equal(canIssueCommand({ ...state, connected: false }, now, 0), false);
  assert.equal(canIssueCommand({ ...state, settings: { ...settings, enabled: false } }, now, 0), false);
  assert.equal(canIssueCommand({ ...state, bridgeLastSeen: now - 60000 }, now, 0), false);
  assert.equal(canIssueCommand({ ...state, snapshot: { ...snapshot, timestamp: now - 30001 } }, now, 0), false);
  assert.equal(canIssueCommand({ ...state, snapshot: { ...snapshot, timestamp: now + 5001 } }, now, 0), false);
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

test('stale and future quotes keep account telemetry connected while automation cannot issue orders', async (t) => {
  const now = 1_800_000_000_000;
  t.mock.method(Date, 'now', () => now);
  for (const { timestamp, candleOffset = 0 } of [
    { timestamp: now - 3 * 86400000 },
    { timestamp: now - 30001 },
    { timestamp: now + 5001 },
    { timestamp: now + 3600000 },
    { timestamp: now + 3600000, candleOffset: 3600 },
    { timestamp: now, candleOffset: 3600 },
  ]) {
    const { call } = fixture();
    const pair = await (await call('/api/connection/pair', {})).json() as { token: string };
    const auth = { authorization: `Bearer ${pair.token}` };
    const fresh = { ...createDemoSnapshot(now), accountId: '123456@PUPrime-Demo', balance: 12345.67, equity: 12345.67 };
    assert.ok(evaluate(fresh, { ...DEFAULT_SETTINGS, enabled: true }, now).order, 'control data should permit an order when its quote is fresh');
    const snapshot = { ...fresh, timestamp, candles: fresh.candles.map(candle => ({ ...candle, time: candle.time + candleOffset })) };
    assert.equal((await call('/api/bridge/snapshot', snapshot, auth)).status, 200);
    const enable = await call('/api/settings', { enabled: true });
    assert.equal(enable.status, 200, 'bridge heartbeat remains usable even when a quote cannot be traded');
    assert.equal((await call('/api/bridge/snapshot', snapshot, auth)).status, 200);
    const state = await (await call('/api/state')).json() as DashboardState;
    assert.equal(state.mode, 'mt5');
    assert.equal(state.connected, true);
    assert.equal(state.bridgeLastSeen, now);
    assert.equal(state.snapshot.accountId, fresh.accountId);
    assert.equal(state.snapshot.balance, 12345.67);
    assert.equal(state.snapshot.timestamp, timestamp, 'quote time must never be rewritten to the heartbeat time');
    assert.deepEqual(state.snapshot.candles, snapshot.candles, 'broker bar timestamps must also remain unchanged');
    assert.equal(state.settings.enabled, true);
    assert.equal(state.evaluation.direction, 'WAIT');
    assert.equal(state.evaluation.order, null);
    assert.ok(state.evaluation.reasons.some(reason => /zastarjeli|vrijeme|svijeća/.test(reason)));
    assert.equal(state.pendingCommand, null);
    if (timestamp !== now) assert.equal(canIssueCommand(state, now, 0), false);
    const delivery = await (await call('/api/bridge/command', undefined, auth)).json() as { command: unknown };
    assert.equal(delivery.command, null);
    const invalid = await call('/api/bridge/snapshot', { ...snapshot, timestamp: 0 }, auth);
    assert.equal(invalid.status, 400);
    const unchanged = await (await call('/api/state')).json() as DashboardState;
    assert.equal(unchanged.snapshot.timestamp, timestamp, 'malformed telemetry must not overwrite the last valid snapshot');
  }
});

test('explicit +3h broker normalization permits only genuinely fresh bounded orders', async (t) => {
  const now = 1_800_000_000_000;
  t.mock.method(Date, 'now', () => now);
  for (const age of [0, 60000]) {
    const { call } = fixture();
    const pair = await (await call('/api/connection/pair', {})).json() as { token: string };
    const auth = { authorization: `Bearer ${pair.token}` };
    const normalized = now - age;
    const snapshot = { ...createDemoSnapshot(now), symbol: 'XAUUSD.s', accountId: '123456@PUPrime-Demo', timestamp: normalized, brokerTimestamp: normalized + 3 * 3600000, brokerUtcOffsetMinutes: 180 };
    assert.equal((await call('/api/bridge/snapshot', snapshot, auth)).status, 200);
    assert.equal((await call('/api/settings', { enabled: true })).status, 200);
    assert.equal((await call('/api/bridge/snapshot', snapshot, auth)).status, 200);
    const state = await (await call('/api/state')).json() as DashboardState;
    assert.equal(state.connected, true);
    assert.equal(state.snapshot.timestamp, normalized);
    assert.equal(state.snapshot.brokerTimestamp, snapshot.brokerTimestamp);
    assert.equal(state.snapshot.brokerUtcOffsetMinutes, 180);
    const { command } = await (await call('/api/bridge/command', undefined, auth)).json() as { command: DashboardState['pendingCommand'] };
    if (age === 0) {
      assert.ok(state.evaluation.order);
      assert.ok(command, 'normalizing an explicitly configured broker clock should preserve a fresh quote');
      assert.equal(command.symbol, 'XAUUSD.s');
      const reservedLoss = (Math.abs(command.referencePrice - command.stopLoss) + command.maxDeviationPoints * snapshot.point) / snapshot.tickSize * snapshot.tickValue * command.volume;
      assert.ok(reservedLoss <= snapshot.equity * state.settings.riskPercent / 100 + 1e-6);
      assert.equal('brokerUtcOffsetMinutes' in command, false, 'timezone metadata does not change the order protocol');
    } else {
      assert.equal(state.evaluation.direction, 'WAIT');
      assert.equal(state.evaluation.order, null);
      assert.equal(state.pendingCommand, null);
      assert.equal(command, null, 'an old raw quote remains old after applying the explicit offset');
      assert.equal(canIssueCommand(state, now, 0), false);
    }
  }
});

test('bridge API rejects mismatched or incomplete UTC metadata without overwriting valid telemetry', async (t) => {
  const now = 1_800_000_000_000;
  t.mock.method(Date, 'now', () => now);
  const { call } = fixture();
  const pair = await (await call('/api/connection/pair', {})).json() as { token: string };
  const auth = { authorization: `Bearer ${pair.token}` };
  const snapshot = { ...createDemoSnapshot(now), accountId: '123456@PUPrime-Demo', brokerTimestamp: now + 3 * 3600000, brokerUtcOffsetMinutes: 180, dailyPnlConservative: true, dailyPnlUpperBound: 0 };
  const accepted = await call('/api/bridge/snapshot', snapshot, auth);
  assert.equal(accepted.status, 200);
  assert.deepEqual(await accepted.json(), { ok: true, riskAccountingVersion: 2 });
  for (const metadata of [
    { brokerTimestamp: snapshot.brokerTimestamp + 2 },
    { brokerUtcOffsetMinutes: 150 },
    { brokerUtcOffsetMinutes: 900 },
    { brokerUtcOffsetMinutes: -780 },
    { brokerUtcOffsetMinutes: undefined },
    { brokerTimestamp: undefined },
    { dailyPnlConservative: 'true' },
    { dailyPnlConservative: null },
    { dailyPnlUpperBound: undefined },
    { dailyPnlUpperBound: -1 },
    { dailyPnlUpperBound: '0' },
    { dailyPnlUpperBound: null },
  ]) assert.equal((await call('/api/bridge/snapshot', { ...snapshot, ...metadata }, auth)).status, 400);
  const state = await (await call('/api/state')).json() as DashboardState;
  assert.equal(state.connected, true);
  assert.equal(state.snapshot.brokerTimestamp, snapshot.brokerTimestamp);
  assert.equal(state.snapshot.brokerUtcOffsetMinutes, 180);
  assert.equal(state.snapshot.dailyPnlConservative, true);
  assert.equal(state.snapshot.dailyPnlUpperBound, 0);
});

test('risk accounting capability v2 never inflates daily risk from an ambiguous P&L lower bound', async (t) => {
  const now = 1_800_000_000_000;
  t.mock.method(Date, 'now', () => now);
  const { call } = fixture();
  const pair = await (await call('/api/connection/pair', {})).json() as { token: string };
  const auth = { authorization: `Bearer ${pair.token}` };
  const snapshot = { ...createDemoSnapshot(now), accountId: '123456@PUPrime-Demo', brokerTimestamp: now + 3 * 3600000, brokerUtcOffsetMinutes: 180, dailyPnl: 100, dailyPnlUpperBound: 10000, dailyPnlConservative: true };
  assert.equal((await call('/api/bridge/snapshot', snapshot, auth)).status, 200);
  assert.equal((await call('/api/settings', { enabled: true })).status, 200);
  const response = await call('/api/bridge/snapshot', snapshot, auth);
  assert.deepEqual(await response.json(), { ok: true, riskAccountingVersion: 2 });
  const state = await (await call('/api/state')).json() as DashboardState;
  assert.equal(state.snapshot.dailyPnlUpperBound, 10000);
  assert.equal(state.evaluation.order, null, 'balance minus upper P&L gives zero opening balance; current balance must not replace it');
  assert.equal(state.pendingCommand, null);
  assert.deepEqual(await (await call('/api/bridge/command', undefined, auth)).json(), { command: null });
});

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
