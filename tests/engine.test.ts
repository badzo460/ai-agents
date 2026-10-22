import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluate, isGoldSymbol, validateSettings, MAX_DEVIATION_POINTS } from '../shared/engine';
import { createDemoSnapshot, executeDemoOrder, stepDemo } from '../shared/demo';
import { DEFAULT_SETTINGS, type MarketSnapshot, type TradeCommand } from '../shared/types';

const NOW = Date.UTC(2026, 9, 20, 12);
const settings = { ...DEFAULT_SETTINGS, enabled: true };

function trending(): MarketSnapshot {
  const snapshot = createDemoSnapshot(NOW);
  snapshot.candles = snapshot.candles.map((c, i) => {
    const open = 3300 + i * 0.12 + Math.sin((i - 1) * 0.9) * 0.6;
    const close = 3300 + i * 0.12 + Math.sin(i * 0.9) * 0.6;
    return { ...c, open, close, high: Math.max(open, close) + 0.3, low: Math.min(open, close) - 0.3 };
  });
  snapshot.bid = snapshot.candles.at(-1)!.close;
  snapshot.ask = snapshot.bid + 0.18;
  return snapshot;
}

function order(snapshot = trending()): TradeCommand {
  const proposed = evaluate(snapshot, settings, NOW).order;
  assert.ok(proposed, 'Fixture must generate an actual order proposal');
  return { ...proposed, id: 'test-order-1', createdAt: NOW, expiresAt: NOW + 30_000 };
}

test('XAUUSD allowlist accepts explicit broker suffixes and rejects other instruments', () => {
  for (const symbol of ['XAUUSD', 'xauusd', 'XAUUSD.m', 'XAUUSD_pro', 'XAUUSD-ECN']) assert.equal(isGoldSymbol(symbol), true);
  for (const symbol of ['EURUSD', 'XAGUSD', 'BTCUSD', 'XAUUSDm', 'XAUUSD/EURUSD', '', null]) assert.equal(isGoldSymbol(symbol), false);
});

test('six agents produce a bounded, broker-aligned XAUUSD order with stop and target', () => {
  const snapshot = trending();
  const evaluation = evaluate(snapshot, settings, NOW);
  assert.deepEqual(evaluation.agents.map(a => a.id), ['scout', 'news', 'sentiment', 'charts', 'risk', 'decision']);
  assert.equal(evaluation.direction, 'BUY');
  assert.ok(evaluation.order);
  const proposal = evaluation.order;
  assert.ok(proposal.stopLoss < snapshot.bid);
  assert.ok(proposal.takeProfit > snapshot.ask);
  assert.ok(proposal.volume >= snapshot.volumeMin);
  assert.ok(Math.abs(proposal.volume / snapshot.volumeStep - Math.round(proposal.volume / snapshot.volumeStep)) < 1e-6);
  const risk = (snapshot.ask + proposal.maxDeviationPoints * snapshot.point - proposal.stopLoss) / snapshot.tickSize * snapshot.tickValue * proposal.volume;
  assert.ok(risk <= snapshot.equity * settings.riskPercent / 100 + 1e-6);
});

test('pause, real account lock, and other instruments cannot produce orders', () => {
  const snapshot = trending();
  assert.equal(evaluate(snapshot, DEFAULT_SETTINGS, NOW).order, null);
  assert.equal(evaluate({ ...snapshot, accountMode: 'real' }, settings, NOW).order, null);
  assert.equal(evaluate({ ...snapshot, symbol: 'EURUSD' }, settings, NOW).order, null);
  assert.ok(evaluate({ ...snapshot, accountMode: 'real' }, { ...settings, allowLive: true }, NOW).order);
});

test('stale prices, stale candles, malformed numbers and inverted bars fail closed', () => {
  const snapshot = trending();
  const invalid = [
    { ...snapshot, timestamp: NOW - 30_001 },
    { ...snapshot, timestamp: NOW + 5_001 },
    { ...snapshot, candles: snapshot.candles.map(c => ({ ...c, time: c.time - 21 * 60 })) },
    { ...snapshot, ask: NaN }, { ...snapshot, tickValue: 0 },
    { ...snapshot, candles: [] },
    { ...snapshot, candles: snapshot.candles.map(c => ({ ...c, low: c.high + 1 })) },
    { ...snapshot, positions: undefined } as unknown as MarketSnapshot,
  ];
  for (const value of invalid) assert.equal(evaluate(value, settings, NOW).order, null);
});

test('missing, stale and blackout news prevent execution unless missing-news protection explicitly disabled', () => {
  const snapshot = trending();
  assert.equal(evaluate({ ...snapshot, news: undefined }, settings, NOW).order, null);
  assert.equal(evaluate({ ...snapshot, news: snapshot.news!.map(n => ({ ...n, timestamp: NOW - 901_000 })) }, settings, NOW).order, null);
  assert.equal(evaluate({ ...snapshot, news: snapshot.news!.map(n => ({ ...n, blackout: true })) }, { ...settings, requireNews: false }, NOW).order, null);
  assert.ok(evaluate({ ...snapshot, news: undefined }, { ...settings, requireNews: false }, NOW).order);
});

test('spread, daily drawdown and existing positions on any symbol block new trades', () => {
  const snapshot = trending();
  assert.equal(evaluate({ ...snapshot, ask: snapshot.bid + 1 }, settings, NOW).order, null);
  assert.equal(evaluate({ ...snapshot, balance: 9800, equity: 9800, dailyPnl: -200 }, settings, NOW).order, null);
  assert.equal(evaluate({ ...snapshot, positions: [{ ticket: 'external', symbol: 'EURUSD', side: 'BUY', volume: 0.01, openPrice: 1.1, currentPrice: 1.1, profit: 0, stopLoss: 1, takeProfit: 1.2 }] }, settings, NOW).order, null);
});

test('minimum broker lot cannot override risk budget and remaining daily loss budget caps size', () => {
  const snapshot = trending();
  assert.equal(evaluate({ ...snapshot, volumeMin: 1, volumeStep: 1 }, settings, NOW).order, null);
  const small = evaluate({ ...snapshot, balance: 9805, equity: 9805, dailyPnl: -195 }, settings, NOW).order;
  assert.ok(small);
  const risk = (snapshot.ask + small.maxDeviationPoints * snapshot.point - small.stopLoss) / snapshot.tickSize * snapshot.tickValue * small.volume;
  assert.ok(risk <= 5 + 1e-6);
});

test('BUY and SELL sizing reserves the entire permitted adverse entry deviation', () => {
  for (const side of ['BUY', 'SELL'] as const) {
    const snapshot = trending();
    if (side === 'SELL') {
      snapshot.candles = snapshot.candles.map(c => ({ ...c, open: 6700 - c.open, close: 6700 - c.close, high: 6700 - c.low, low: 6700 - c.high }));
      snapshot.bid = snapshot.candles.at(-1)!.close; snapshot.ask = snapshot.bid + 0.18;
    }
    const proposal = evaluate(snapshot, settings, NOW).order;
    assert.ok(proposal);
    assert.equal(proposal.side, side);
    assert.equal(proposal.maxDeviationPoints, MAX_DEVIATION_POINTS);
    const worstEntry = proposal.referencePrice + (side === 'BUY' ? 1 : -1) * proposal.maxDeviationPoints * snapshot.point;
    const worstAllowedPriceRisk = Math.abs(worstEntry - proposal.stopLoss) / snapshot.tickSize * snapshot.tickValue * proposal.volume;
    const configuredBudget = snapshot.equity * settings.riskPercent / 100;
    assert.ok(worstAllowedPriceRisk <= configuredBudget + 1e-6, `${side}: worst allowed entry must respect configured budget`);
    const nextLotRisk = Math.abs(worstEntry - proposal.stopLoss) / snapshot.tickSize * snapshot.tickValue * (proposal.volume + snapshot.volumeStep);
    assert.ok(nextLotRisk > configuredBudget, 'lot floor should use the adverse-entry risk, not reference-entry risk');
  }
});

test('floating losses and already reserved position risk reduce or block daily budget', () => {
  const snapshot = trending();
  assert.equal(evaluate({ ...snapshot, equity: 9799 }, settings, NOW).order, null);
  const openRisk = { ticket: 'reserved', symbol: 'XAUUSD', side: 'BUY' as const, volume: 1, openPrice: snapshot.bid, currentPrice: snapshot.bid, profit: 0, stopLoss: snapshot.bid - 2, takeProfit: snapshot.bid + 4 };
  assert.equal(evaluate({ ...snapshot, positions: [openRisk] }, { ...settings, maxPositions: 2 }, NOW).order, null);
  assert.equal(evaluate({ ...snapshot, positions: [{ ...openRisk, stopLoss: 0 }] }, { ...settings, maxPositions: 2 }, NOW).order, null);
});

test('conservative positive-day bounds never enlarge the exact upper-bound daily budget', () => {
  const snapshot = { ...trending(), volumeMin: 0.0001, volumeStep: 0.0001 };
  const dailyLimited = { ...settings, riskPercent: 0.5, maxDailyLossPercent: 0.5 };
  const exact = evaluate({ ...snapshot, dailyPnl: 200 }, dailyLimited, NOW).order;
  const bounded = evaluate({ ...snapshot, dailyPnl: 100, dailyPnlUpperBound: 200, dailyPnlConservative: true }, dailyLimited, NOW).order;
  assert.ok(exact);
  assert.ok(bounded);
  assert.ok(bounded.volume <= exact.volume, 'under-counted gains must not inflate the opening balance or lot size');
  const worstEntry = bounded.referencePrice + bounded.maxDeviationPoints * snapshot.point;
  const risk = Math.abs(worstEntry - bounded.stopLoss) / snapshot.tickSize * snapshot.tickValue * bounded.volume;
  assert.ok(risk <= (snapshot.balance - 200) * dailyLimited.maxDailyLossPercent / 100 + 1e-6);
});

test('conservative bounds spanning losses and gains reserve loss against the smallest opening balance', () => {
  const snapshot = { ...trending(), volumeMin: 0.0001, volumeStep: 0.0001 };
  const dailyLimited = { ...settings, riskPercent: 0.5, maxDailyLossPercent: 0.5 };
  const lower = -30, upper = 40;
  for (const floatingLoss of [0, 10]) {
    const account = { ...snapshot, equity: snapshot.balance - floatingLoss };
    const bounded = evaluate({ ...account, dailyPnl: lower, dailyPnlUpperBound: upper, dailyPnlConservative: true }, dailyLimited, NOW).order;
    assert.ok(bounded);
    for (const actualPnl of [lower, 0, upper]) {
      const exact = evaluate({ ...account, dailyPnl: actualPnl }, dailyLimited, NOW).order;
      assert.ok(exact);
      assert.ok(bounded.volume <= exact.volume, 'bounded risk must be no greater than any plausible exact daily result');
    }
    const worstEntry = bounded.referencePrice + bounded.maxDeviationPoints * snapshot.point;
    const risk = Math.abs(worstEntry - bounded.stopLoss) / snapshot.tickSize * snapshot.tickValue * bounded.volume;
    const conservativeBudget = (snapshot.balance - upper) * dailyLimited.maxDailyLossPercent / 100 + lower - floatingLoss;
    assert.ok(risk <= conservativeBudget + 1e-6);
  }
  const exhausted = evaluate({ ...snapshot, equity: snapshot.balance - 20, dailyPnl: lower, dailyPnlUpperBound: upper, dailyPnlConservative: true }, dailyLimited, NOW);
  assert.equal(exhausted.order, null, 'floating losses can exhaust the bounded daily budget');
});

test('conservative daily accounting rejects missing, nonfinite, inverted or nonpositive-baseline bounds', () => {
  const snapshot = trending();
  for (const upper of [undefined, NaN, Infinity, -Infinity, 99, snapshot.balance, snapshot.balance + 1]) {
    const evaluation = evaluate({ ...snapshot, dailyPnl: 100, dailyPnlUpperBound: upper, dailyPnlConservative: true }, settings, NOW);
    assert.equal(evaluation.direction, 'WAIT');
    assert.equal(evaluation.order, null);
    assert.ok(evaluation.reasons.some(reason => reason.includes('Granice dnevnog rezultata')));
  }
});

test('absent conservative-accounting flag preserves exact legacy daily-P&L behavior', () => {
  const snapshot = { ...trending(), dailyPnl: 100 };
  const exact = evaluate(snapshot, settings, NOW);
  const explicitExact = evaluate({ ...snapshot, dailyPnlConservative: false }, settings, NOW);
  assert.ok(exact.order);
  assert.deepEqual(explicitExact, exact);
});

test('extreme RSI vetoes entries even when trend and price momentum agree', () => {
  const snapshot = trending();
  snapshot.candles = snapshot.candles.map((c, i) => ({ ...c, open: 3300 + i * .2, close: 3300.1 + i * .2, high: 3300.3 + i * .2, low: 3299.9 + i * .2 }));
  snapshot.bid = snapshot.candles.at(-1)!.close; snapshot.ask = snapshot.bid + .18;
  const evaluation = evaluate(snapshot, settings, NOW);
  assert.equal(evaluation.agents.find(a => a.id === 'scout')!.signal, 'BUY');
  assert.equal(evaluation.agents.find(a => a.id === 'sentiment')!.signal, 'BUY');
  assert.equal(evaluation.order, null);
  assert.ok(evaluation.reasons.some(r => r.includes('RSI')));
});

test('settings reject non-finite, misleading and unsafe inputs', () => {
  assert.equal(validateSettings({}).enabled, false);
  for (const value of [{ riskPercent: 3 }, { riskPercent: NaN }, { riskPercent: 0 }, { maxPositions: 1.2 }, { maxSpreadPoints: Infinity }, { allowLive: 'true' }, { unknown: 1 }, { riskPercent: 2, maxDailyLossPercent: 1 }]) assert.throws(() => validateSettings(value as never));
});

test('paper order executes once, charges spread and rejects invalid account/symbol/expiry', () => {
  const snapshot = trending();
  const command = order(snapshot);
  const filled = executeDemoOrder(snapshot, command);
  assert.equal(filled.positions.length, 1);
  assert.ok(filled.equity < filled.balance, 'spread is an immediate floating loss');
  assert.equal(executeDemoOrder(filled, command).positions.length, 1);
  assert.equal(snapshot.positions.length, 0, 'pure function leaves original untouched');
  for (const change of [{ accountId: 'other' }, { symbol: 'EURUSD' }, { expiresAt: NOW - 1 }, { expiresAt: NaN }, { volume: 0.001 }, { maxSpreadPoints: NaN }, { referencePrice: NaN }]) assert.throws(() => executeDemoOrder(snapshot, { ...command, ...change }));
});

test('paper positions close at SL and TP, realizing loss and gain', () => {
  for (const outcome of ['loss', 'gain'] as const) {
    const snapshot = trending();
    const filled = executeDemoOrder(snapshot, order(snapshot));
    const position = filled.positions[0];
    filled.bid = outcome === 'gain' ? position.takeProfit + 1 : position.stopLoss - 1;
    filled.ask = filled.bid + 0.18;
    const next = stepDemo(filled, NOW + 5_000);
    assert.equal(next.positions.length, 0);
    assert.equal(next.balance, next.equity);
    assert.ok(outcome === 'gain' ? next.balance > 10_000 : next.balance < 10_000);
    assert.ok(outcome === 'gain' ? next.dailyPnl > 0 : next.dailyPnl < 0);
  }
});
