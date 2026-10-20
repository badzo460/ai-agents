import { DEFAULT_SETTINGS, type Activity, type DashboardState, type MarketSnapshot, type Settings, type TradeCommand } from '../shared/types';
import { evaluate, isGoldSymbol, validateSettings } from '../shared/engine';
import { createDemoSnapshot, executeDemoOrder, stepDemo } from '../shared/demo';

export interface Env {
  ROOM: DurableObjectNamespace;
  ASSETS: Fetcher;
  APP_PASSWORD?: string;
  SESSION_SECRET?: string;
  DEV_MODE?: string;
  PUBLIC_ORIGIN?: string;
}

const COOKIE = 'aurum_session';
const SESSION_MS = 8 * 60 * 60 * 1000;
const BRIDGE_MS = 24 * 60 * 60 * 1000;
const FRESH_MS = 30_000;
const encoder = new TextEncoder();
const json = (data: unknown, status = 200, headers: HeadersInit = {}) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...headers } });
class ApiError extends Error { constructor(message: string, readonly status = 400) { super(message); } }

export function localDevelopment(request: Request, env: Pick<Env, 'DEV_MODE'>): boolean {
  return env.DEV_MODE === 'true' && ['localhost', '127.0.0.1', '[::1]'].includes(new URL(request.url).hostname);
}
export function allowedOrigin(request: Request, env: Pick<Env, 'PUBLIC_ORIGIN' | 'DEV_MODE'>): boolean {
  const origin = request.headers.get('origin');
  const target = env.PUBLIC_ORIGIN ? new URL(env.PUBLIC_ORIGIN).origin : new URL(request.url).origin;
  if (origin === target) return true;
  // Vite forwards development API requests to Wrangler on a separate port.
  // This exception is unavailable on public hosts and in production.
  return localDevelopment(request, env) && ['http://127.0.0.1:5173', 'http://localhost:5173'].includes(origin || '');
}
export async function sha256(value: string): Promise<string> {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value)))].map(b => b.toString(16).padStart(2, '0')).join('');
}
function equal(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return mismatch === 0;
}
async function signature(payload: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return [...new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(payload)))].map(b => b.toString(16).padStart(2, '0')).join('');
}
export async function createSession(secret: string, now = Date.now()): Promise<string> {
  const payload = `${now + SESSION_MS}.${crypto.randomUUID()}`;
  return `${payload}.${await signature(payload, secret)}`;
}
export async function verifySession(token: string, secret: string, now = Date.now()): Promise<boolean> {
  const parts = token.split('.');
  if (parts.length !== 3 || !/^\d{13}$/.test(parts[0]) || Number(parts[0]) <= now || Number(parts[0]) > now + SESSION_MS) return false;
  return equal(await signature(`${parts[0]}.${parts[1]}`, secret), parts[2]);
}
async function authenticated(request: Request, env: Env): Promise<boolean> {
  if (localDevelopment(request, env)) return true;
  if (!env.SESSION_SECRET) return false;
  const token = request.headers.get('cookie')?.split(';').map(v => v.trim()).find(v => v.startsWith(`${COOKIE}=`))?.slice(COOKIE.length + 1);
  return !!token && verifySession(token, env.SESSION_SECRET);
}
async function body(request: Request): Promise<Record<string, unknown>> {
  if (Number(request.headers.get('content-length') || 0) > 262144) throw new ApiError('Zahtjev je prevelik.', 413);
  const reader = request.body?.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  if (reader) {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      length += chunk.value.byteLength;
      if (length > 262144) { await reader.cancel(); throw new ApiError('Zahtjev je prevelik.', 413); }
      chunks.push(chunk.value);
    }
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  const raw = new TextDecoder().decode(bytes);
  try {
    const data: unknown = JSON.parse(raw || '{}');
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error();
    return data as Record<string, unknown>;
  } catch { throw new ApiError('Neispravan JSON zahtjev.'); }
}
const number = (value: unknown, min: number, max: number): value is number => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;
const string = (value: unknown, max = 128): value is string => typeof value === 'string' && value.length > 0 && value.length <= max;

export function parseSettings(input: Record<string, unknown>, current: Settings): Settings {
  const next = { ...current };
  for (const key of ['enabled', 'allowLive', 'requireNews'] as const) {
    if (key in input) {
      if (typeof input[key] !== 'boolean') throw new ApiError(`Neispravna postavka: ${key}.`);
      next[key] = input[key];
    }
  }
  for (const [key, min, max] of [['riskPercent', 0.1, 2], ['maxDailyLossPercent', 0.5, 10], ['maxSpreadPoints', 1, 500], ['maxPositions', 1, 5]] as const) {
    if (key in input) {
      if (!number(input[key], min, max) || (['maxPositions', 'maxSpreadPoints'].includes(key) && !Number.isInteger(input[key]))) throw new ApiError(`Neispravna postavka: ${key}.`);
      next[key] = input[key];
    }
  }
  if (next.allowLive && !current.allowLive && input.liveConfirmation !== 'LIVE XAUUSD') throw new ApiError('Za stvarni račun upišite LIVE XAUUSD.');
  try { return validateSettings(next); } catch { throw new ApiError('Rizik naloga ne smije biti veći od dnevnog limita.'); }
}

export function parseSnapshot(input: Record<string, unknown>, now = Date.now()): MarketSnapshot {
  if (!string(input.symbol, 32) || !isGoldSymbol(input.symbol)) throw new ApiError('Dozvoljen je isključivo XAUUSD i njegov brokerski sufiks.');
  if (!number(input.timestamp, now - FRESH_MS, now + 5000)) throw new ApiError('MT5 snimka je zastarjela ili vrijeme računala nije usklađeno.');
  if (!string(input.accountId, 128) || !['demo', 'real'].includes(String(input.accountMode)) || !string(input.currency, 8)) throw new ApiError('Nedostaju valjani podaci računa.');
  for (const key of ['bid', 'ask', 'point', 'tickSize', 'tickValue', 'volumeMin', 'volumeMax', 'volumeStep'] as const) if (!number(input[key], Number.MIN_VALUE, 1e9)) throw new ApiError(`Neispravan MT5 podatak: ${key}.`);
  if ((input.ask as number) < (input.bid as number) || (input.volumeMin as number) > (input.volumeMax as number)) throw new ApiError('Neispravan bid/ask ili raspon volumena.');
  for (const key of ['balance', 'equity'] as const) if (!number(input[key], 0, 1e12)) throw new ApiError(`Neispravan MT5 podatak: ${key}.`);
  if (!number(input.dailyPnl, -1e12, 1e12)) throw new ApiError('Neispravan dnevni rezultat.');
  if (!Array.isArray(input.candles) || input.candles.length < 30 || input.candles.length > 500) throw new ApiError('Potrebno je između 30 i 500 završenih svijeća.');
  let previous = 0;
  for (const candle of input.candles) {
    if (!candle || typeof candle !== 'object' || !number(candle.time, previous + 1, Math.floor(now / 1000) + 60)) throw new ApiError('Neispravna vremena svijeća.');
    for (const key of ['open', 'high', 'low', 'close'] as const) if (!number(candle[key], Number.MIN_VALUE, 1e9)) throw new ApiError('Neispravna cijena svijeće.');
    if (!number(candle.volume, 0, 1e12) || candle.low > Math.min(candle.open, candle.close) || candle.high < Math.max(candle.open, candle.close) || candle.high < candle.low) throw new ApiError('Neispravan OHLC raspon.');
    previous = candle.time;
  }
  if (!Array.isArray(input.positions) || input.positions.length > 200) throw new ApiError('Neispravne pozicije.');
  for (const position of input.positions) {
    if (!position || typeof position !== 'object' || !string(position.ticket, 64) || !string(position.symbol, 32) || !['BUY', 'SELL'].includes(position.side)) throw new ApiError('Neispravna pozicija.');
    for (const key of ['volume', 'openPrice', 'currentPrice'] as const) if (!number(position[key], Number.MIN_VALUE, 1e12)) throw new ApiError('Neispravna vrijednost pozicije.');
    for (const key of ['stopLoss', 'takeProfit'] as const) if (!number(position[key], 0, 1e12)) throw new ApiError('Neispravna zaštita pozicije.');
    if (!number(position.profit, -1e12, 1e12)) throw new ApiError('Neispravan rezultat pozicije.');
  }
  if (input.news !== undefined) {
    if (!Array.isArray(input.news) || input.news.length > 100) throw new ApiError('Neispravne vijesti.');
    for (const news of input.news) if (!news || typeof news !== 'object' || !string(news.headline, 300) || !['low', 'medium', 'high'].includes(news.impact) || !number(news.timestamp, 0, now + 86400000) || typeof news.blackout !== 'boolean') throw new ApiError('Neispravna vijest.');
  }
  return input as unknown as MarketSnapshot;
}

export function canIssueCommand(state: DashboardState, now: number, lastCommandAt: number): boolean {
  return state.settings.enabled && state.connected && state.mode === 'mt5' && state.bridgeLastSeen !== null &&
    now - state.bridgeLastSeen <= FRESH_MS && now - state.snapshot.timestamp <= FRESH_MS && state.snapshot.timestamp <= now + 5000 &&
    isGoldSymbol(state.snapshot.symbol) && (state.snapshot.accountMode === 'demo' || state.settings.allowLive) &&
    state.snapshot.positions.length < state.settings.maxPositions && !state.pendingCommand && now - lastCommandAt >= 60_000;
}

interface StoredRoom { state: DashboardState; tokenHash: string | null; tokenExpiresAt: number; lastCommandAt: number; boundAccount: string | null; boundMode: 'demo' | 'real' | null; boundSymbol: string | null; issuedCommands: TradeCommand[]; resolvedCommandIds: string[]; dispatchedCommandIds: string[] }

export class TradingRoom {
  private record!: StoredRoom;
  private ready: Promise<void>;
  private tail: Promise<unknown> = Promise.resolve();
  constructor(private ctx: DurableObjectState, private env: Env) {
    this.ready = ctx.blockConcurrencyWhile(async () => {
      const saved = await ctx.storage.get<StoredRoom>('room');
      if (saved) {
        this.record = saved;
        this.record.issuedCommands ??= [];
        this.record.resolvedCommandIds ??= [];
        this.record.dispatchedCommandIds ??= [];
      }
      else {
        const snapshot = createDemoSnapshot();
        const settings = { ...DEFAULT_SETTINGS };
        this.record = { state: { mode: 'demo', connected: true, bridgeLastSeen: null, snapshot, settings, evaluation: evaluate(snapshot, settings), activity: [], trades: [], pendingCommand: null, version: '0.1.0' }, tokenHash: null, tokenExpiresAt: 0, lastCommandAt: 0, boundAccount: null, boundMode: null, boundSymbol: null, issuedCommands: [], resolvedCommandIds: [], dispatchedCommandIds: [] };
        this.event('info', 'SYSTEM', 'Kontrolna soba je pokrenuta. Prikazani podaci su simulirani.');
        await this.persist();
      }
    });
  }
  fetch(request: Request): Promise<Response> {
    const run = this.tail.then(async () => { await this.ready; try { return await this.handle(request); } catch (error) { return json({ error: error instanceof ApiError ? error.message : 'Interna pogreška. Pokušajte ponovno.' }, error instanceof ApiError ? error.status : 500); } });
    this.tail = run.catch(() => undefined);
    return run;
  }
  private event(type: Activity['type'], agent: string, message: string) {
    this.record.state.activity.unshift({ id: crypto.randomUUID(), timestamp: Date.now(), type, agent, message });
    this.record.state.activity = this.record.state.activity.slice(0, 80);
  }
  private persist() { return this.ctx.storage.put('room', this.record); }
  private unresolvedDispatched() { return this.record.issuedCommands.filter(command => this.record.dispatchedCommandIds.includes(command.id) && !this.record.resolvedCommandIds.includes(command.id)); }
  private refresh(now = Date.now()) {
    const state = this.record.state;
    if (state.settings.enabled && this.unresolvedDispatched().some(command => command.expiresAt <= now)) {
      state.settings.enabled = false;
      this.event('warning', 'MT5', 'Izvršenje dostavljenog naloga nije potvrđeno. Automatika je zaustavljena; provjerite MT5 i ponovno uparite most ako potvrda nedostaje.');
    }
    if (state.mode === 'mt5') state.connected = this.record.tokenExpiresAt > now && !!state.bridgeLastSeen && now - state.bridgeLastSeen <= FRESH_MS;
    state.evaluation = evaluate(state.snapshot, state.settings, now);
    if (state.pendingCommand && (state.pendingCommand.expiresAt <= now || !state.settings.enabled || !state.connected || !state.evaluation.order || state.snapshot.positions.length >= state.settings.maxPositions)) {
      state.pendingCommand = null;
      this.event('warning', 'RISK', 'Neizvršeni nalog je istekao ili je automatika zaustavljena.');
    }
  }
  private queue(now = Date.now()) {
    const state = this.record.state;
    this.refresh(now);
    if (!canIssueCommand(state, now, this.record.lastCommandAt) || this.unresolvedDispatched().length || !state.evaluation.order) return;
    state.pendingCommand = { ...state.evaluation.order, id: crypto.randomUUID(), createdAt: now, expiresAt: now + FRESH_MS };
    this.record.issuedCommands.unshift(state.pendingCommand);
    this.record.issuedCommands = this.record.issuedCommands.slice(0, 100);
    this.record.lastCommandAt = now;
    this.event('info', 'DECISION', `${state.pendingCommand.side} nalog za ${state.snapshot.symbol} čeka provjeru MT5 mosta.`);
  }
  private async bridgeAuth(request: Request) {
    const token = request.headers.get('authorization')?.match(/^Bearer ([A-Za-z0-9_-]{32,})$/)?.[1];
    if (!token || !this.record.tokenHash || this.record.tokenExpiresAt <= Date.now() || !equal(await sha256(token), this.record.tokenHash)) throw new ApiError('MT5 token nije valjan ili je istekao. Ponovno uparite most.', 401);
    if (this.record.state.mode !== 'mt5') throw new ApiError('MT5 način nije aktivan.', 409);
  }
  private async handle(request: Request): Promise<Response> {
    const url = new URL(request.url), path = url.pathname, now = Date.now();
    // Consume every forwarded POST body exactly once before responding, including
    // {} payloads on control routes and rejected bridge authentication requests.
    const payload = request.method === 'POST' ? await body(request) : {};
    if (path === '/api/login' && request.method === 'POST') {
      const ip = request.headers.get('cf-connecting-ip') || 'local';
      const key = `login:${await sha256(ip)}`;
      const attempts = await this.ctx.storage.get<{ count: number; until: number }>(key);
      if (attempts && attempts.until > now && attempts.count >= 10) throw new ApiError('Previše pokušaja prijave. Pokušajte za 15 minuta.', 429);
      const input = payload;
      const pass = typeof input.password === 'string' && input.password.length <= 512 ? input.password : '';
      if (!this.env.APP_PASSWORD || !this.env.SESSION_SECRET || !equal(await sha256(pass), await sha256(this.env.APP_PASSWORD))) {
        await this.ctx.storage.put(key, { count: attempts && attempts.until > now ? attempts.count + 1 : 1, until: attempts && attempts.until > now ? attempts.until : now + 900000 });
        throw new ApiError('Neispravna lozinka.', 401);
      }
      await this.ctx.storage.delete(key);
      const secure = url.protocol === 'https:' ? '; Secure' : '';
      return json({ ok: true }, 200, { 'set-cookie': `${COOKIE}=${await createSession(this.env.SESSION_SECRET)}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800${secure}` });
    }
    if (path === '/api/logout' && request.method === 'POST') return json({ ok: true }, 200, { 'set-cookie': `${COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0${url.protocol === 'https:' ? '; Secure' : ''}` });
    if (path.startsWith('/api/bridge/')) {
      await this.bridgeAuth(request);
      if (path === '/api/bridge/snapshot' && request.method === 'POST') {
        const snapshot = parseSnapshot(payload, now);
        if (this.record.boundAccount && (this.record.boundAccount !== snapshot.accountId || this.record.boundMode !== snapshot.accountMode || this.record.boundSymbol && this.record.boundSymbol !== snapshot.symbol)) {
          this.record.state.settings.enabled = false;
          this.record.state.settings.allowLive = false;
          this.record.state.pendingCommand = null;
          this.event('warning', 'RISK', 'MT5 račun se promijenio. Ponovno uparite most.');
          await this.persist();
          throw new ApiError('Promjena MT5 računa nije dozvoljena bez ponovnog uparivanja.', 409);
        }
        this.record.boundAccount = snapshot.accountId;
        this.record.boundMode = snapshot.accountMode;
        this.record.boundSymbol = snapshot.symbol;
        for (const trade of this.record.state.trades) {
          if (trade.source !== 'mt5' || trade.status !== 'open') continue;
          const position = snapshot.positions.find(position => position.ticket === trade.id);
          if (position) { trade.profit = position.profit; trade.profitKnown = true; }
          else if (this.record.state.snapshot.timestamp > trade.timestamp) {
            trade.status = 'closed';
            trade.profitKnown = false;
            this.event('info', 'MT5', `Pozicija ${trade.id} više nije otvorena. Konačan rezultat provjerite u MT5 povijesti.`);
          }
        }
        this.record.state.snapshot = snapshot;
        this.record.state.bridgeLastSeen = now;
        this.record.state.connected = true;
        this.queue(now);
        await this.persist();
        return json({ ok: true });
      }
      if (path === '/api/bridge/command' && request.method === 'GET') {
        this.refresh(now);
        const command = this.record.state.pendingCommand;
        const deliver = command && !this.record.dispatchedCommandIds.includes(command.id) ? command : null;
        if (deliver) {
          this.record.dispatchedCommandIds.unshift(deliver.id);
          this.record.dispatchedCommandIds = this.record.dispatchedCommandIds.slice(0, 100);
        }
        await this.persist();
        return json({ command: deliver });
      }
      if (path === '/api/bridge/result' && request.method === 'POST') {
        const input = payload;
        if (typeof input.commandId !== 'string') throw new ApiError('Nedostaje identifikator naloga.');
        if (this.record.resolvedCommandIds.includes(input.commandId)) return json({ ok: true, duplicate: true });
        const command = this.record.issuedCommands.find(command => command.id === input.commandId);
        if (!command) throw new ApiError('Nepoznat identifikator izdanog naloga.', 409);
        if (!['filled', 'rejected'].includes(String(input.status))) throw new ApiError('Neispravan rezultat naloga.');
        if (input.volume !== undefined && !number(input.volume, Number.MIN_VALUE, command.volume + 1e-8)) throw new ApiError('Neispravan izvršeni volumen.');
        if (this.record.state.pendingCommand?.id === command.id) this.record.state.pendingCommand = null;
        const filled = input.status === 'filled';
        this.record.resolvedCommandIds.unshift(command.id);
        this.record.resolvedCommandIds = this.record.resolvedCommandIds.slice(0, 100);
        this.record.state.trades.unshift({ id: typeof input.ticket === 'string' ? input.ticket.slice(0, 64) : command.id, timestamp: now, symbol: command.symbol, side: command.side, volume: typeof input.volume === 'number' ? input.volume : command.volume, price: number(input.price, Number.MIN_VALUE, 1e9) ? input.price : command.referencePrice, profit: 0, profitKnown: !filled, status: filled ? 'open' : 'rejected', source: 'mt5' });
        this.record.state.trades = this.record.state.trades.slice(0, 100);
        this.event(filled ? 'success' : 'warning', 'MT5', `${filled ? 'MT5 je potvrdio izvršenje' : 'MT5 je odbio nalog'}: ${typeof input.message === 'string' ? input.message.slice(0, 250) : command.id}`);
        if (!filled) this.record.state.settings.enabled = false;
        await this.persist();
        return json({ ok: true });
      }
      throw new ApiError('Nepostojeći MT5 endpoint.', 404);
    }
    if (path === '/api/state' && request.method === 'GET') {
      this.refresh(now);
      await this.persist();
      return json(this.record.state);
    }
    if (request.method !== 'POST') throw new ApiError('Nepostojeći endpoint.', 404);
    const state = this.record.state;
    if (path === '/api/demo/step') {
      if (state.mode !== 'demo') throw new ApiError('Simulacija nije aktivna.', 409);
      const previousPositions = state.snapshot.positions;
      state.snapshot = stepDemo(state.snapshot, now);
      for (const previous of previousPositions) if (!state.snapshot.positions.some(position => position.ticket === previous.ticket)) {
        const trade = state.trades.find(trade => trade.id === previous.ticket);
        if (trade) {
          trade.status = 'closed';
          const exit = previous.side === 'BUY' ? state.snapshot.bid : state.snapshot.ask;
          trade.profit = Number(((exit - previous.openPrice) * (previous.side === 'BUY' ? 1 : -1) / state.snapshot.tickSize * state.snapshot.tickValue * previous.volume).toFixed(2));
        }
        this.event('info', 'PAPER', 'Simulirana pozicija zatvorena je na zaštitnoj granici.');
      }
      this.refresh(now);
      if (state.settings.enabled && state.evaluation.order && now - this.record.lastCommandAt >= 60000) {
        const command: TradeCommand = { ...state.evaluation.order, id: crypto.randomUUID(), createdAt: now, expiresAt: now + FRESH_MS };
        state.snapshot = executeDemoOrder(state.snapshot, command);
        this.record.lastCommandAt = now;
        const position = state.snapshot.positions.at(-1);
        if (position) state.trades.unshift({ id: position.ticket, timestamp: now, symbol: command.symbol, side: command.side, volume: command.volume, price: command.referencePrice, profit: 0, status: 'open', source: 'demo' });
        this.event('success', 'PAPER', `Simulirani ${command.side}: ${command.volume} lota XAUUSD. Stvarni nalog nije poslan.`);
      }
      for (const trade of state.trades) { const position = state.snapshot.positions.find(position => position.ticket === trade.id); if (position) trade.profit = position.profit; }
      state.trades = state.trades.slice(0, 100);
    } else if (path === '/api/demo/reset') {
      if (state.mode !== 'demo') throw new ApiError('Najprije odspojite MT5.', 409);
      state.snapshot = createDemoSnapshot(now);
      state.settings = { ...DEFAULT_SETTINGS };
      state.trades = [];
      state.pendingCommand = null;
      this.record.lastCommandAt = 0;
      this.event('info', 'PAPER', 'Simulirani račun vraćen je na početno stanje.');
    } else if (path === '/api/settings') {
      const input = payload;
      const settings = parseSettings(input, state.settings);
      if (settings.enabled && this.unresolvedDispatched().some(command => command.expiresAt <= now)) throw new ApiError('Nedostaje potvrda izvršenja. Provjerite MT5 i ponovno uparite most prije nastavka.', 409);
      if (settings.enabled && state.mode === 'mt5' && (!state.connected || !state.bridgeLastSeen || now - state.bridgeLastSeen > FRESH_MS)) throw new ApiError('Najprije povežite aktivni MT5 most.', 409);
      if (settings.enabled && state.mode === 'mt5' && state.snapshot.accountMode === 'real' && !settings.allowLive) throw new ApiError('Stvarni račun traži izričitu potvrdu trgovanja.', 409);
      state.settings = settings;
      state.pendingCommand = null;
      this.event('info', 'RISK', settings.enabled ? 'Automatika je uključena; svaki nalog prolazi provjere rizika.' : 'Automatika je pauzirana. Postojeće pozicije ostaju otvorene.');
    } else if (path === '/api/emergency-stop') {
      state.settings.enabled = false;
      state.settings.allowLive = false;
      state.pendingCommand = null;
      this.event('warning', 'RISK', 'Zaustavljeno je slanje novih naloga. Postojeće pozicije pratite u MT5.');
    } else if (path === '/api/connection/pair') {
      const token = [...crypto.getRandomValues(new Uint8Array(32))].map(v => v.toString(16).padStart(2, '0')).join('');
      this.record.tokenHash = await sha256(token);
      this.record.tokenExpiresAt = now + BRIDGE_MS;
      this.record.boundAccount = null;
      this.record.boundMode = null;
      this.record.boundSymbol = null;
      this.record.issuedCommands = [];
      this.record.resolvedCommandIds = [];
      this.record.dispatchedCommandIds = [];
      state.settings.enabled = false;
      state.settings.allowLive = false;
      state.mode = 'mt5';
      state.connected = false;
      state.bridgeLastSeen = null;
      state.pendingCommand = null;
      state.trades = [];
      state.snapshot = { ...createDemoSnapshot(now), symbol: 'XAUUSD', accountId: 'Čeka se MT5', accountMode: 'demo', bid: 0, ask: 0, balance: 0, equity: 0, dailyPnl: 0, positions: [], candles: [], news: undefined };
      this.event('info', 'MT5', 'Token za most je izrađen. Čeka se stvarna snimka MT5 računa.');
      this.refresh(now);
      await this.persist();
      return json({ token, bridgeUrl: new URL('/api/bridge', this.env.PUBLIC_ORIGIN || request.url).toString(), expiresAt: this.record.tokenExpiresAt });
    } else if (path === '/api/connection/disconnect') {
      this.record.tokenHash = null;
      this.record.tokenExpiresAt = 0;
      this.record.boundAccount = null;
      this.record.boundMode = null;
      this.record.boundSymbol = null;
      this.record.issuedCommands = [];
      this.record.resolvedCommandIds = [];
      this.record.dispatchedCommandIds = [];
      state.mode = 'demo';
      state.connected = true;
      state.bridgeLastSeen = null;
      state.snapshot = createDemoSnapshot(now);
      state.settings.enabled = false;
      state.settings.allowLive = false;
      state.pendingCommand = null;
      state.trades = [];
      this.event('info', 'MT5', 'Most je odspojen i token poništen. Aktivna je simulacija.');
    } else if (path === '/api/agent/run') {
      this.refresh(now);
      this.event('info', 'DECISION', `Analiza: ${state.evaluation.direction}. ${state.evaluation.reasons[0] || 'Čeka se potvrda agenata.'}`);
    } else throw new ApiError('Nepostojeći endpoint.', 404);
    this.refresh(now);
    await this.persist();
    return json(state);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      const path = new URL(request.url).pathname;
      if (!path.startsWith('/api/')) return env.ASSETS.fetch(request);
      const development = localDevelopment(request, env);
      if (!development && (!env.APP_PASSWORD || env.APP_PASSWORD.length < 12 || !env.SESSION_SECRET || env.SESSION_SECRET.length < 32)) return json({ error: 'Prije javnog pokretanja postavite APP_PASSWORD (najmanje 12 znakova) i SESSION_SECRET (najmanje 32 znaka) kroz Cloudflare secrets.' }, 503);
      const bridge = path.startsWith('/api/bridge/');
      if (!bridge && !['GET', 'HEAD'].includes(request.method) && !allowedOrigin(request, env)) return json({ error: 'Nedozvoljeno porijeklo zahtjeva.' }, 403);
      if (!bridge && path !== '/api/login' && !(await authenticated(request, env))) return json({ error: 'Potrebna je prijava.' }, 401);
      const id = env.ROOM.idFromName('private-xauusd-room');
      return await env.ROOM.get(id).fetch(request);
    } catch {
      return json({ error: 'Poslužitelj nije dostupan. Provjerite konfiguraciju.' }, 500);
    }
  },
} satisfies ExportedHandler<Env>;
