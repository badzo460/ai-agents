import { DEFAULT_SETTINGS, type AgentResult, type Candle, type Direction, type Evaluation, type MarketSnapshot, type Settings } from './types';

export { DEFAULT_SETTINGS } from './types';
export const MAX_DEVIATION_POINTS = 20;
export const isGoldSymbol = (symbol: unknown): symbol is string => typeof symbol === 'string' && /^XAUUSD(?:[._-][A-Za-z0-9]+)?$/i.test(symbol);

/** Strict bounds also apply to API callers who bypass the controls in the UI. */
export function validateSettings(partial: Partial<Settings>): Settings {
  if (!partial || typeof partial !== 'object' || Array.isArray(partial)) throw new Error('Neispravne postavke.');
  const result = { ...DEFAULT_SETTINGS };
  const limits: Record<string, [number, number]> = { riskPercent: [0.1, 2], maxDailyLossPercent: [0.5, 10], maxSpreadPoints: [1, 500], maxPositions: [1, 5] };
  for (const [key, value] of Object.entries(partial)) {
    if (!(key in DEFAULT_SETTINGS)) throw new Error(`Nepoznata postavka: ${key}`);
    if (['enabled', 'allowLive', 'requireNews'].includes(key)) {
      if (typeof value !== 'boolean') throw new Error(`Postavka ${key} mora biti boolean.`);
    } else {
      const bounds = limits[key];
      if (typeof value !== 'number' || !Number.isFinite(value) || !bounds || value < bounds[0] || value > bounds[1] || (['maxPositions', 'maxSpreadPoints'].includes(key) && !Number.isInteger(value))) throw new Error(`Postavka ${key} izvan dopuštenih granica.`);
    }
    Object.assign(result, { [key]: value });
  }
  if (result.riskPercent > result.maxDailyLossPercent) throw new Error('Rizik naloga ne smije biti veći od dnevnog limita.');
  return result;
}

const positive = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n) && n > 0;
const finite = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n);
const round = (n: number, decimals = 2) => Number(n.toFixed(decimals));
const ema = (values: number[], period: number) => values.reduce((previous, value, index) => index ? previous + 2 / (period + 1) * (value - previous) : value, values[0] ?? 0);
function rsi(values: number[], period = 14) {
  let gains = 0, losses = 0;
  for (let i = values.length - period; i < values.length; i++) {
    const delta = values[i] - values[i - 1];
    gains += Math.max(delta, 0); losses += Math.max(-delta, 0);
  }
  return gains + losses === 0 ? 50 : losses === 0 ? 100 : 100 - 100 / (1 + gains / losses);
}
function atr(candles: Candle[], period = 14) {
  return candles.slice(-period).reduce((total, c, i) => {
    const previous = candles[candles.length - period + i - 1]?.close ?? c.open;
    return total + Math.max(c.high - c.low, Math.abs(c.high - previous), Math.abs(c.low - previous));
  }, 0) / period;
}
const result = (id: AgentResult['id'], name: string, role: string, signal: Direction, confidence: number, summary: string, metrics: AgentResult['metrics'] = [], blocked = false): AgentResult => ({ id, name, role, signal, confidence, summary, metrics, status: blocked ? 'blocked' : 'ready' });

/** No network, model claims, or side effects. This returns a proposal, never executes one. */
export function evaluate(snapshot: MarketSnapshot, suppliedSettings: Settings, now = Date.now()): Evaluation {
  const reasons: string[] = [];
  let settings: Settings;
  try { settings = validateSettings(suppliedSettings); }
  catch { settings = { ...DEFAULT_SETTINGS }; reasons.push('Neispravne postavke rizika.'); }
  const market = snapshot ?? {} as MarketSnapshot;
  const bars = Array.isArray(market.candles) ? market.candles.slice(-500) : [];
  const barsValid = bars.length >= 40 && bars.every((b, i) => b && [b.time, b.open, b.high, b.low, b.close].every(positive) && finite(b.volume) && b.volume >= 0 && b.high >= Math.max(b.open, b.close) && b.low <= Math.min(b.open, b.close) && b.low <= b.high && (!i || b.time > bars[i - 1].time)) && now / 1000 - bars.at(-1)!.time <= 20 * 60 && bars.at(-1)!.time <= now / 1000 + 60;
  const priceValid = [market.bid, market.ask, market.point, market.tickSize, market.tickValue, market.volumeMin, market.volumeMax, market.volumeStep, market.balance, market.equity].every(positive) && market.ask >= market.bid && market.volumeMin <= market.volumeMax && market.volumeStep <= market.volumeMax && finite(market.dailyPnl);
  const positionValid = Array.isArray(market.positions) && market.positions.every(p => p && typeof p.symbol === 'string' && ['BUY', 'SELL'].includes(p.side) && [p.volume, p.openPrice, p.currentPrice].every(positive) && [p.profit, p.stopLoss, p.takeProfit].every(finite));
  if (!isGoldSymbol(market.symbol)) reasons.push('Dopušten je samo XAUUSD ili XAUUSD s oznakom brokera (.m, _pro).');
  if (!positive(market.timestamp) || now - market.timestamp > 30_000 || market.timestamp - now > 5_000) reasons.push('Tržišni podaci su zastarjeli ili imaju neispravno vrijeme.');
  if (!barsValid) reasons.push('Potrebno je najmanje 40 ispravnih, svježih i poredanih svijeća.');
  if (!priceValid || !positionValid || typeof market.accountId !== 'string' || !market.accountId.trim() || !['demo', 'real'].includes(market.accountMode)) reasons.push('Nepotpuni podaci računa, cijene ili specifikacije brokera.');
  const closes = barsValid ? bars.map(b => b.close) : [];
  const fast = closes.length ? ema(closes, 9) : 0;
  const slow = closes.length ? ema(closes, 21) : 0;
  const volatility = barsValid ? atr(bars) : 0;
  const strength = volatility > 0 ? (fast - slow) / volatility : 0;
  const scoutSignal: Direction = strength > 0.15 ? 'BUY' : strength < -0.15 ? 'SELL' : 'WAIT';
  const scout = result('scout', 'SCOUT', 'Trend i tržišni režim', scoutSignal, Math.min(94, Math.round(50 + Math.abs(strength) * 22)), barsValid ? scoutSignal === 'WAIT' ? 'Trend je neodlučan; čekam jasniji smjer.' : `EMA 9/21 pokazuje ${scoutSignal === 'BUY' ? 'uzlazni' : 'silazni'} trend zlata.` : 'Čekam valjane tržišne podatke.', [{ label: 'EMA 9', value: fast ? fast.toFixed(2) : '—' }, { label: 'EMA 21', value: slow ? slow.toFixed(2) : '—' }], !barsValid);

  const news = Array.isArray(market.news) ? market.news : [];
  const validNews = news.filter(n => n && typeof n.headline === 'string' && ['low', 'medium', 'high'].includes(n.impact) && finite(n.timestamp) && n.timestamp <= now + 60_000 && now - n.timestamp <= 15 * 60_000 && typeof n.blackout === 'boolean');
  const newsBlackout = validNews.some(n => n.blackout);
  const missingNews = validNews.length === 0;
  if (newsBlackout) reasons.push('NEWS: aktivna zabrana trgovanja zbog događaja.');
  if (settings.requireNews && missingNews) reasons.push('NEWS: nema svježeg izvora vijesti; zaštita vijesti blokira nove naloge.');
  const newsAgent = result('news', 'NEWS', 'Kalendar i blokada događaja', 'WAIT', missingNews ? 0 : 100, newsBlackout ? 'Događaj visokog rizika. Novi nalozi su blokirani.' : missingNews ? settings.requireNews ? 'Izvor vijesti nije povezan. Zaštita je uključena.' : 'Bez izvora vijesti; zaštitu je korisnik isključio.' : validNews[0].headline, [{ label: 'Izvor', value: missingNews ? 'Nije povezan' : 'MT5 / simulacija' }, { label: 'Zaštita', value: settings.requireNews ? 'Uključena' : 'Isključena' }], newsBlackout || settings.requireNews && missingNews);

  const momentum = closes.length ? (closes.at(-1)! - closes.at(-9)!) / Math.max(volatility, market.point || 1) : 0;
  const sentimentSignal: Direction = momentum > 0.4 ? 'BUY' : momentum < -0.4 ? 'SELL' : 'WAIT';
  const sentiment = result('sentiment', 'SENTIMENT', 'Momentum cijene · bez društvenih izvora', sentimentSignal, closes.length ? Math.min(90, Math.round(50 + Math.abs(momentum) * 9)) : 0, closes.length ? `Momentum ${momentum >= 0 ? '+' : ''}${momentum.toFixed(2)} ATR. Procjena iz cijene, ne iz društvenih mreža.` : 'Čekam tržišne podatke.', [{ label: 'Momentum', value: closes.length ? `${momentum.toFixed(2)} ATR` : '—' }, { label: 'Izvor', value: 'Cijena XAUUSD' }], !barsValid);

  const relative = closes.length ? rsi(closes) : 50;
  const chartSignal: Direction = relative >= 48 && relative <= 72 && scoutSignal === 'BUY' ? 'BUY' : relative <= 52 && relative >= 28 && scoutSignal === 'SELL' ? 'SELL' : 'WAIT';
  const charts = result('charts', 'CHARTS', 'RSI, ATR i ulazni nivoi', chartSignal, closes.length ? chartSignal === 'WAIT' ? 45 : 76 : 0, !closes.length ? 'Čekam svijeće za tehničku analizu.' : relative > 72 || relative < 28 ? 'Ekstreman RSI; čekam povoljniji ulaz.' : `RSI ${relative.toFixed(1)} · ATR ${volatility.toFixed(2)}.`, [{ label: 'RSI 14', value: closes.length ? relative.toFixed(1) : '—' }, { label: 'ATR 14', value: closes.length ? volatility.toFixed(2) : '—' }], !barsValid);
  if (barsValid && (relative > 72 || relative < 28)) reasons.push('CHARTS: ekstreman RSI; čekam povoljniji ulaz.');

  const votes = [scoutSignal, sentimentSignal, chartSignal];
  const buyVotes = votes.filter(v => v === 'BUY').length, sellVotes = votes.filter(v => v === 'SELL').length;
  const proposed: Direction = buyVotes >= 2 && sellVotes === 0 ? 'BUY' : sellVotes >= 2 && buyVotes === 0 ? 'SELL' : 'WAIT';
  const spread = priceValid ? (market.ask - market.bid) / market.point : Infinity;
  if (spread > settings.maxSpreadPoints + 1e-7) reasons.push('Spread je veći od dopuštenog limita.');
  // An ambiguous broker day supplies lower/upper realized-P&L bounds. Use the
  // upper bound for the smallest possible opening balance and the lower bound
  // for consumed loss; substituting the lower bound into both can expand risk.
  const conservativePnl = market.dailyPnlConservative === true;
  const pnlBoundsValid = !conservativePnl || finite(market.dailyPnl) && finite(market.dailyPnlUpperBound) && market.dailyPnlUpperBound >= market.dailyPnl;
  const dailyBaseline = conservativePnl
    ? pnlBoundsValid ? Math.max(0, market.balance - market.dailyPnlUpperBound!) : 0
    : priceValid && positive(market.balance - market.dailyPnl) ? market.balance - market.dailyPnl : market.balance;
  if (conservativePnl && (!pnlBoundsValid || !positive(dailyBaseline))) reasons.push('Granice dnevnog rezultata ili početno stanje računa nisu valjani za izračun rizika.');
  // dailyPnl is the exact realized P&L or its conservative lower bound.
  // Floating losses reduce either budget immediately; floating gains do not.
  const lossIncludingFloating = priceValid ? market.dailyPnl + Math.min(0, market.equity - market.balance) : 0;
  if (positive(dailyBaseline) && lossIncludingFloating <= -dailyBaseline * settings.maxDailyLossPercent / 100) reasons.push('Dosegnut je maksimalni dnevni gubitak uključujući otvorene gubitke.');
  if (Array.isArray(market.positions) && market.positions.length >= settings.maxPositions) reasons.push('Dosegnut je limit otvorenih pozicija na računu.');
  if (positionValid && market.positions.some(p => p.symbol !== market.symbol || p.stopLoss <= 0)) reasons.push('Dodatni rizik postojećih pozicija nije moguće potvrditi (drugi simbol ili nedostaje SL).');
  if (market.accountMode === 'real' && !settings.allowLive) reasons.push('Stvarno trgovanje nije izričito omogućeno.');
  if (!settings.enabled) reasons.push('Agenti su pauzirani; novi nalozi su isključeni.');

  const entry = proposed === 'SELL' ? market.bid : market.ask;
  const distance = Math.max(volatility * 1.8, (market.ask - market.bid) * 3, (market.tickSize || 0) * 10);
  const quantize = (price: number, up: boolean) => Number((Math[up ? 'ceil' : 'floor'](price / market.tickSize) * market.tickSize).toFixed(8));
  const stopLoss = priceValid && barsValid ? quantize(entry + (proposed === 'SELL' ? distance : -distance), proposed === 'SELL') : 0;
  const takeProfit = priceValid && barsValid ? quantize(entry + (proposed === 'SELL' ? -distance * 2 : distance * 2), proposed !== 'SELL') : 0;
  const existingStopRisk = priceValid && positionValid ? market.positions.reduce((total, p) => total + Math.max(0, p.side === 'BUY' ? p.currentPrice - p.stopLoss : p.stopLoss - p.currentPrice) / market.tickSize * market.tickValue * p.volume, 0) : 0;
  const remainingDailyBudget = priceValid ? Math.max(0, dailyBaseline * settings.maxDailyLossPercent / 100 + Math.min(0, lossIncludingFloating) - existingStopRisk) : 0;
  const riskBudget = priceValid ? Math.min(market.equity * settings.riskPercent / 100, remainingDailyBudget) : 0;
  // Reserve the full adverse entry deviation the bridge is permitted to accept.
  // Stop execution gaps and fees still remain outside this price-risk estimate.
  const adverseEntryDistance = MAX_DEVIATION_POINTS * market.point;
  const riskPerLot = priceValid ? (Math.abs(entry - stopLoss) + adverseEntryDistance) / market.tickSize * market.tickValue : 0;
  const volume = priceValid && riskPerLot > 0 ? Number((Math.floor(Math.min(market.volumeMax, riskBudget / riskPerLot) / market.volumeStep + 1e-9) * market.volumeStep).toFixed(8)) : 0;
  if (priceValid && barsValid && (volume < market.volumeMin - 1e-9 || volume <= 0 || volume * riskPerLot > riskBudget + 1e-6 || stopLoss <= 0 || takeProfit <= 0)) reasons.push('Minimalni lot ili stop nivoi prelaze dopušteni rizik.');
  const riskBlocked = reasons.length > 0;
  const risk = result('risk', 'RISK', 'Veličina naloga i zaštitne granice', 'WAIT', riskBlocked ? 0 : 100, riskBlocked ? reasons[0] : `Rizik do ${settings.riskPercent}% · ${volume.toFixed(2)} lot · SL/TP obavezni.`, [{ label: 'Rizik / nalog', value: `${settings.riskPercent}%` }, { label: 'Spread', value: Number.isFinite(spread) ? `${round(spread)} pt` : '—' }], riskBlocked);
  const direction = riskBlocked ? 'WAIT' : proposed;
  const confidence = direction === 'WAIT' ? 0 : Math.round([scout, sentiment, charts].filter(a => a.signal === direction).reduce((sum, a) => sum + a.confidence, 0) / votes.filter(v => v === direction).length);
  if (proposed === 'WAIT') reasons.push('Nema suglasnosti najmanje dva tehnička agenta.');
  const decision = result('decision', 'DECISION', 'Konsenzus i prijedlog naloga', direction, confidence, direction === 'WAIT' ? reasons[0] ?? 'Čekam konsenzus.' : `${direction} XAUUSD · ${volume.toFixed(2)} lot · omjer rizika i dobiti 1:2.`, [{ label: 'Konsenzus', value: `${Math.max(buyVotes, sellVotes)}/3` }, { label: 'Nalog', value: direction }], riskBlocked);
  const order: Evaluation['order'] = direction !== 'WAIT' ? { symbol: market.symbol, side: direction, volume, stopLoss, takeProfit, maxSpreadPoints: settings.maxSpreadPoints, accountId: market.accountId, accountMode: market.accountMode, referencePrice: entry, maxDeviationPoints: MAX_DEVIATION_POINTS } : null;
  return { timestamp: now, direction, confidence, agents: [scout, newsAgent, sentiment, charts, risk, decision], reasons, order };
}
