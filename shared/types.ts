export type AgentId = 'scout' | 'news' | 'sentiment' | 'charts' | 'risk' | 'decision';
export type Direction = 'BUY' | 'SELL' | 'WAIT';
export interface Candle { time: number; open: number; high: number; low: number; close: number; volume: number }
export interface Position { ticket: string; symbol: string; side: 'BUY' | 'SELL'; volume: number; openPrice: number; currentPrice: number; profit: number; stopLoss: number; takeProfit: number }
export interface MarketSnapshot {
  symbol: string; timestamp: number; bid: number; ask: number;
  brokerTimestamp?: number; brokerUtcOffsetMinutes?: number;
  point: number; tickSize: number; tickValue: number; volumeMin: number; volumeMax: number; volumeStep: number;
  balance: number; equity: number; accountId: string; accountMode: 'demo' | 'real';
  currency: string; dailyPnl: number; positions: Position[]; candles: Candle[];
  dailyPnlConservative?: boolean;
  dailyPnlUpperBound?: number;
  news?: { headline: string; impact: 'low' | 'medium' | 'high'; timestamp: number; blackout: boolean }[];
}
export interface AgentResult { id: AgentId; name: string; role: string; status: 'working' | 'ready' | 'blocked'; signal: Direction; confidence: number; summary: string; metrics: { label: string; value: string }[] }
export interface Evaluation { timestamp: number; direction: Direction; confidence: number; agents: AgentResult[]; reasons: string[]; order: Omit<TradeCommand, 'id' | 'createdAt' | 'expiresAt'> | null }
export interface Settings { enabled: boolean; riskPercent: number; maxDailyLossPercent: number; maxSpreadPoints: number; maxPositions: number; allowLive: boolean; requireNews: boolean }
export interface TradeCommand { id: string; createdAt: number; expiresAt: number; symbol: string; side: 'BUY' | 'SELL'; volume: number; stopLoss: number; takeProfit: number; maxSpreadPoints: number; accountId: string; accountMode: 'demo' | 'real'; referencePrice: number; maxDeviationPoints: number }
export interface Activity { id: string; timestamp: number; type: 'info' | 'success' | 'warning'; agent: string; message: string }
export interface Trade { id: string; timestamp: number; symbol: string; side: 'BUY' | 'SELL'; volume: number; price: number; profit: number; profitKnown?: boolean; status: 'open' | 'closed' | 'rejected'; source: 'demo' | 'mt5' }
export interface DashboardState { mode: 'demo' | 'mt5'; connected: boolean; bridgeLastSeen: number | null; snapshot: MarketSnapshot; settings: Settings; evaluation: Evaluation; activity: Activity[]; trades: Trade[]; pendingCommand: TradeCommand | null; version: string }
export interface Pairing { token: string; bridgeUrl: string; expiresAt: number }
export const DEFAULT_SETTINGS: Settings = { enabled: false, riskPercent: 0.5, maxDailyLossPercent: 2, maxSpreadPoints: 50, maxPositions: 1, allowLive: false, requireNews: true };
