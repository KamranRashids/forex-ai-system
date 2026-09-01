import { authFetch } from "./auth";

export type Candle = {
  symbol: string;
  timeframe: string;
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type LatestPrice = {
  symbol: string;
  price: number;
  bucket_start: string | null;
  synthetic: boolean;
};

const TIMEFRAME_MINUTES: Record<string, number> = {
  M5: 5,
  M15: 15,
  H1: 60,
  H4: 240,
  D1: 1440,
};

function numOr(raw: unknown, fallback: number): number {
  if (raw === null || raw === undefined) return fallback;
  const n = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

function str(raw: unknown): string {
  return raw === null || raw === undefined ? "" : String(raw);
}

export function normalizeCandle(raw: Record<string, unknown>): Candle {
  return {
    symbol: str(raw["symbol"]),
    timeframe: str(raw["timeframe"]),
    ts: str(raw["ts"]),
    open: numOr(raw["open"], 0),
    high: numOr(raw["high"], 0),
    low: numOr(raw["low"], 0),
    close: numOr(raw["close"], 0),
    volume: numOr(raw["volume"], 0),
  };
}

export function recentCandlesStart(timeframe: string, limit = 8, now = new Date()): string {
  const minutes = TIMEFRAME_MINUTES[timeframe] ?? 60;
  const windowMs = Math.round(minutes * limit * 60_000 * 0.9);
  return new Date(now.getTime() - windowMs).toISOString();
}

export async function fetchCandles(
  symbol: string,
  timeframe: string,
  options: { start?: string; end?: string; limit?: number } = {},
): Promise<Candle[]> {
  const params = new URLSearchParams({ symbol, timeframe });
  if (options.start) params.set("start", options.start);
  if (options.end) params.set("end", options.end);
  if (options.limit) params.set("limit", String(options.limit));
  const rows = await authFetch<Array<Record<string, unknown>>>(
    `/api/v1/market/candles?${params.toString()}`,
    { method: "GET" },
  );
  return (rows ?? []).map(normalizeCandle);
}