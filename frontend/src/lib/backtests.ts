import { ApiError, authFetch } from "./auth";

// --- Domain models ----------------------------------------------------------
export type BacktestStatus = "RUNNING" | "COMPLETED" | "FAILED";

export type CoverageEntry = {
  symbol: string;
  timeframe: string;
  expected_bars: number;
  technical: number;
  regime: number;
  fundamental: number;
  sentiment: number;
  degraded: boolean;
};

export type Metrics = {
  net_pnl: number;
  gross_pnl: number;
  total_costs: number;
  num_trades: number;
  win_rate: number;
  profit_factor: number;
  sharpe: number;
  sortino: number;
  max_drawdown_pct: number;
  exposure_avg_pct: number;
  bars: number;
  degraded_runs: number;
  coverage: CoverageEntry[];
};

export type BacktestRun = {
  id: string;
  status: BacktestStatus;
  metrics: Metrics;
  error: string | null;
  seed: number;
  started_at: string;
  finished_at: string | null;
};

export type BacktestTrade = {
  id: string;
  symbol: string;
  timeframe: string;
  side: "LONG" | "SHORT";
  units: number;
  entry_ts: string;
  entry_price: number;
  exit_ts: string;
  exit_price: number;
  gross_pnl: number;
  costs: number;
  net_pnl: number;
  exit_reason: string;
};

export type EquityPoint = {
  id?: string;
  ts: string;
  equity: number;
  drawdown_pct: number;
};

export type RunRange = {
  start: string | null;
  end: string | null;
};

function str(value: unknown): string | undefined {
  if (typeof value === "string" && value.length > 0) return value;
  return undefined;
}

function numOr(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

/** Parse a date-time string; returns empty string when malformed/absent. */
function isoOr(value: unknown): string {
  const raw = str(value);
  if (raw === undefined) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString();
}

function boolOr(value: unknown): boolean {
  return value === true || value === "true" || value === 1;
}

// --- Normalizers ------------------------------------------------------------
function normalizeCoverage(value: unknown): CoverageEntry[] {
  if (!Array.isArray(value)) return [];
  return value.map((raw) => {
    const entry = (raw ?? {}) as Record<string, unknown>;
    return {
      symbol: str(entry["symbol"]) ?? "",
      timeframe: str(entry["timeframe"]) ?? "",
      expected_bars: numOr(entry["expected_bars"], 0),
      technical: numOr(entry["technical"], 0),
      regime: numOr(entry["regime"], 0),
      fundamental: numOr(entry["fundamental"], 0),
      sentiment: numOr(entry["sentiment"], 0),
      degraded: boolOr(entry["degraded"]),
    };
  });
}

export function normalizeMetrics(raw: Record<string, unknown> | null | undefined): Metrics {
  const value = raw ?? {};
  return {
    net_pnl: numOr(value["net_pnl"], 0),
    gross_pnl: numOr(value["gross_pnl"], 0),
    total_costs: numOr(value["total_costs"], 0),
    num_trades: numOr(value["num_trades"], 0),
    win_rate: numOr(value["win_rate"], 0),
    profit_factor: numOr(value["profit_factor"], 0),
    sharpe: numOr(value["sharpe"], 0),
    sortino: numOr(value["sortino"], 0),
    max_drawdown_pct: numOr(value["max_drawdown_pct"], 0),
    exposure_avg_pct: numOr(value["exposure_avg_pct"], 0),
    bars: numOr(value["bars"], 0),
    degraded_runs: numOr(value["degraded_runs"], 0),
    coverage: normalizeCoverage(value["coverage"]),
  };
}

function normalizeStatus(value: unknown): BacktestStatus {
  const raw = str(value)?.toUpperCase();
  if (raw === "RUNNING" || raw === "COMPLETED" || raw === "FAILED") return raw;
  return "FAILED";
}

export function normalizeRun(raw: Record<string, unknown>): BacktestRun {
  return {
    id: str(raw["id"]) ?? "",
    status: normalizeStatus(raw["status"]),
    metrics: normalizeMetrics(raw["metrics"] as Record<string, unknown> | null | undefined),
    error: str(raw["error"]) ?? null,
    seed: numOr(raw["seed"], 0),
    started_at: isoOr(raw["started_at"]),
    finished_at: raw["finished_at"] === null || raw["finished_at"] === undefined
      ? null
      : isoOr(raw["finished_at"]),
  };
}

export function normalizeTrade(raw: Record<string, unknown>): BacktestTrade {
  const sideRaw = str(raw["side"])?.toUpperCase();
  return {
    id: str(raw["id"]) ?? "",
    symbol: str(raw["symbol"]) ?? "",
    timeframe: str(raw["timeframe"]) ?? "",
    side: sideRaw === "SHORT" ? "SHORT" : "LONG",
    units: numOr(raw["units"], 0),
    entry_ts: isoOr(raw["entry_ts"]),
    entry_price: numOr(raw["entry_price"], 0),
    exit_ts: isoOr(raw["exit_ts"]),
    exit_price: numOr(raw["exit_price"], 0),
    gross_pnl: numOr(raw["gross_pnl"], 0),
    costs: numOr(raw["costs"], 0),
    net_pnl: numOr(raw["net_pnl"], 0),
    exit_reason: str(raw["exit_reason"]) ?? "",
  };
}

export function normalizeEquityPoint(raw: Record<string, unknown>): EquityPoint {
  return {
    id: str(raw["id"]),
    ts: isoOr(raw["ts"]),
    equity: numOr(raw["equity"], 0),
    drawdown_pct: numOr(raw["drawdown_pct"], 0),
  };
}

// --- REST fetchers ----------------------------------------------------------
export async function fetchBacktests(limit = 20): Promise<BacktestRun[]> {
  const rows = await authFetch<Array<Record<string, unknown>>>(`/api/v1/backtests?limit=${limit}`, {
    method: "GET",
  });
  return (rows ?? []).map(normalizeRun);
}

export async function fetchBacktestDetail(id: string): Promise<BacktestRun> {
  const row = await authFetch<Record<string, unknown>>(`/api/v1/backtests/${id}`, {
    method: "GET",
  });
  return normalizeRun(row ?? {});
}

export async function fetchBacktestTrades(id: string): Promise<BacktestTrade[]> {
  const rows = await authFetch<Array<Record<string, unknown>>>(`/api/v1/backtests/${id}/trades`, {
    method: "GET",
  });
  return (rows ?? []).map(normalizeTrade);
}

export async function fetchBacktestEquity(id: string): Promise<EquityPoint[]> {
  const rows = await authFetch<Array<Record<string, unknown>>>(`/api/v1/backtests/${id}/equity`, {
    method: "GET",
  });
  return (rows ?? []).map(normalizeEquityPoint);
}

export { ApiError };

// --- Pure formatters --------------------------------------------------------
export function fmtCurrency(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

export function fmtPct(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

export function fmtNumber(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

export function fmtDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

// --- Run range (from equity timestamps) ------------------------------------
export function deriveRunRange(points: EquityPoint[]): RunRange {
  if (!Array.isArray(points) || points.length === 0) return { start: null, end: null };
  const first = points[0];
  const last = points[points.length - 1];
  return { start: first.ts || null, end: last.ts || null };
}

// --- SVG path builders ------------------------------------------------------
const DRAW_PAD = 4;

function finiteCoord(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback;
}

/**
 * Build an SVG path `d` for a series, mapping each point's numeric index to
 * x and each value to y across [width x height]. Never emits NaN/Infinity.
 * A single point (or flat series) degrades to a horizontal line at `height/2`
 * so there is always a visible, finite path.
 */
export function buildPath(
  values: number[],
  width: number,
  height: number,
): string {
  const w = Number.isFinite(width) && width > 0 ? width : 100;
  const h = Number.isFinite(height) && height > 0 ? height : 40;
  const safeValues = values.map((v) => (Number.isFinite(v) ? v : 0));
  if (safeValues.length === 0) return "";

  const min = Math.min(...safeValues);
  const max = Math.max(...safeValues);
  const span = max - min;
  const pad = Math.min(DRAW_PAD, h / 2);

  const xAt = (i: number): number =>
    safeValues.length === 1 ? w / 2 : (i / (safeValues.length - 1)) * w;

  const yAt = (v: number): number => {
    if (span === 0) return h / 2;
    return pad + ((max - v) / span) * (h - pad * 2);
  };

  return safeValues
    .map((v, i) => {
      const x = finiteCoord(xAt(i), w / 2);
      const y = finiteCoord(yAt(v), h / 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
}

export function buildEquityPath(points: EquityPoint[], width: number, height: number): string {
  return buildPath(points.map((p) => p.equity), width, height);
}

export function buildDrawdownPath(points: EquityPoint[], width: number, height: number): string {
  return buildPath(points.map((p) => p.drawdown_pct), width, height);
}

// --- Comparison normalization ----------------------------------------------
export type ComparisonCurve = {
  id: string;
  label: string;
  points: Array<{ x: number; y: number }>;
};

/**
 * Normalize each run's equity curve to a percentage-change series from its own
 * first value, then map onto the index axis so runs of differing lengths and
 * timestamps can be overlaid without implying shared time points.
 * Returns an empty array when fewer than `min` valid runs are supplied.
 */
export function normalizeForComparison(
  runs: Array<{ id: string; label: string; equity: EquityPoint[] }>,
  min = 2,
): ComparisonCurve[] {
  if (!Array.isArray(runs) || runs.length < min) return [];
  const output: ComparisonCurve[] = [];
  for (const run of runs) {
    const raw = run.equity ?? [];
    if (!raw.length) continue;
    const base = raw[0].equity;
    if (!Number.isFinite(base)) continue;
    const values = raw.map((p) => p.equity);
    const series = values.map((v) => ({
      x: 0,
      y: Number.isFinite(v) ? ((v - base) / base) * 100 : 0,
    }));
    const n = series.length;
    const scaled = series.map((p, i) => ({
      x: n === 1 ? 0 : (i / (n - 1)) * 100,
      y: Number.isFinite(p.y) ? p.y : 0,
    }));
    output.push({ id: run.id, label: run.label, points: scaled });
  }
  return output.length >= min ? output : [];
}
