import { isGoldSymbol } from './engine';
import type { Candle, MarketSnapshot, TradeCommand } from './types';

const cents = (n: number) => Number(n.toFixed(2));
const pnl = (snapshot: MarketSnapshot, volume: number, entry: number, exit: number, side: string) => cents((exit - entry) * (side === 'BUY' ? 1 : -1) / snapshot.tickSize * snapshot.tickValue * volume);

/** Synthetic prices only. The deterministic series is never labelled a live feed. */
export function createDemoSnapshot(now = Date.now()): MarketSnapshot {
  const minute = Math.floor(now / 300_000) * 300;
  const candles: Candle[] = [];
  let previous = 3348;
  for (let i = 0; i < 100; i++) {
    const close = cents(3348 + i * 0.12 + Math.sin(i * 0.9) * 0.6);
    candles.push({ time: minute - (99 - i) * 300, open: previous, high: cents(Math.max(previous, close) + 0.3 + (i % 4) * 0.05), low: cents(Math.min(previous, close) - 0.27 - (i % 3) * 0.07), close, volume: 40 + (i * 17) % 120 });
    previous = close;
  }
  return { symbol: 'XAUUSD', timestamp: now, bid: previous, ask: cents(previous + 0.18), point: 0.01, tickSize: 0.01, tickValue: 1, volumeMin: 0.01, volumeMax: 100, volumeStep: 0.01, balance: 10_000, equity: 10_000, accountId: 'PAPER-XAU-001', accountMode: 'demo', currency: 'USD', dailyPnl: 0, positions: [], candles, news: [{ headline: 'DEMO · simulirani kalendar, bez aktivnih blokada.', impact: 'low', timestamp: now, blackout: false }] };
}

/** One simulated tick; stop/target exits use executable bid/ask and booked P&L. */
export function stepDemo(snapshot: MarketSnapshot, now = Date.now()): MarketSnapshot {
  const next = structuredClone(snapshot);
  const phase = now / 8_000;
  const change = 0.04 + Math.sin(phase) * 0.2 + Math.sin(phase * 0.37) * 0.12;
  next.bid = cents(Math.max(1, next.bid + change));
  next.ask = cents(next.bid + 0.18);
  next.timestamp = now;
  next.news = [{ headline: 'DEMO · simulirani kalendar, bez aktivnih blokada.', impact: 'low', timestamp: now, blackout: false }];
  const minute = Math.floor(now / 300_000) * 300;
  const last = next.candles.at(-1)!;
  if (minute > last.time) {
    next.candles.push({ time: minute, open: last.close, high: Math.max(last.close, next.bid), low: Math.min(last.close, next.bid), close: next.bid, volume: 1 });
    next.candles = next.candles.slice(-150);
  } else {
    last.high = Math.max(last.high, next.bid); last.low = Math.min(last.low, next.bid); last.close = next.bid; last.volume++;
  }
  if (Math.floor(snapshot.timestamp / 86_400_000) !== Math.floor(now / 86_400_000)) next.dailyPnl = 0;
  let realized = 0;
  next.positions = next.positions.filter(position => {
    const exit = position.side === 'BUY' ? next.bid : next.ask;
    const hitStop = position.side === 'BUY' ? exit <= position.stopLoss : exit >= position.stopLoss;
    const hitTarget = position.side === 'BUY' ? exit >= position.takeProfit : exit <= position.takeProfit;
    position.currentPrice = exit;
    position.profit = pnl(next, position.volume, position.openPrice, exit, position.side);
    if (hitStop || hitTarget) { realized += position.profit; return false; }
    return true;
  });
  next.balance = cents(next.balance + realized);
  next.dailyPnl = cents(next.dailyPnl + realized);
  next.equity = cents(next.balance + next.positions.reduce((sum, p) => sum + p.profit, 0));
  return next;
}

export function executeDemoOrder(snapshot: MarketSnapshot, command: TradeCommand): MarketSnapshot {
  const now = snapshot.timestamp;
  if (!isGoldSymbol(command.symbol) || command.symbol !== snapshot.symbol || command.accountId !== snapshot.accountId || command.accountMode !== 'demo' || snapshot.accountMode !== 'demo' || typeof command.id !== 'string' || !command.id || !Number.isFinite(command.expiresAt) || !Number.isFinite(command.createdAt) || command.expiresAt < now || command.expiresAt <= command.createdAt || command.createdAt > now + 5000) throw new Error('Nalog ne pripada aktivnoj demo sesiji ili je istekao.');
  if (snapshot.positions.some(p => p.ticket === command.id)) return structuredClone(snapshot);
  const entry = command.side === 'BUY' ? snapshot.ask : snapshot.bid;
  if (!['BUY', 'SELL'].includes(command.side) || ![command.volume, command.stopLoss, command.takeProfit, command.referencePrice, command.maxSpreadPoints, command.maxDeviationPoints].every(n => Number.isFinite(n) && n > 0) || command.volume < snapshot.volumeMin || command.volume > snapshot.volumeMax || Math.abs(command.volume / snapshot.volumeStep - Math.round(command.volume / snapshot.volumeStep)) > 1e-6 || (snapshot.ask - snapshot.bid) / snapshot.point > command.maxSpreadPoints + 1e-7 || Math.abs(entry - command.referencePrice) / snapshot.point > command.maxDeviationPoints + 1e-7 || (command.side === 'BUY' ? command.stopLoss >= snapshot.bid || command.takeProfit <= snapshot.ask : command.stopLoss <= snapshot.ask || command.takeProfit >= snapshot.bid)) throw new Error('Demo nalog ima neispravne cijene, spread ili volumen.');
  const next = structuredClone(snapshot);
  const currentPrice = command.side === 'BUY' ? next.bid : next.ask;
  next.positions.push({ ticket: command.id, symbol: command.symbol, side: command.side, volume: command.volume, openPrice: entry, currentPrice, profit: pnl(next, command.volume, entry, currentPrice, command.side), stopLoss: command.stopLoss, takeProfit: command.takeProfit });
  next.equity = cents(next.balance + next.positions.reduce((sum, p) => sum + p.profit, 0));
  return next;
}
