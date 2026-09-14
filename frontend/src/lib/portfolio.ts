import { ApiError, authFetch } from "./auth";

// --- Domain models ----------------------------------------------------------
export type PortfolioSummary = {
  as_of: string | null;
  cash: number;
  equity: number;
  open_pnl: number;
  realized_pnl: number;
  peak_equity: number;
  drawdown_pct: number;
  margin_used: number;
  open_positions: number;
  open_orders: number;
};

export type PortfolioPosition = {
  id: string;
  order_id: string;
  decision_id: string | null;
  symbol: string;
  timeframe: string;
  side: "LONG" | "SHORT";
  units: number;
  entry_price: number;
  entry_ts: string;
  stop_loss: number | null;
  take_profit: number | null;
  status: "OPEN" | "CLOSED";
  exit_price: number | null;
  exit_ts: string | null;
  exit_reason: string | null;
  gross_pnl: number | null;
  net_pnl: number | null;
};

export type PortfolioOrder = {
  id: string;
  decision_id: string | null;
  symbol: string;
  timeframe: string;
  side: "LONG" | "SHORT";
  order_type: "NEXT_OPEN";
  status: "PENDING" | "FILLED" | "CANCELLED" | "REJECTED";
  units: number;
  requested_price: number | null;
  filled_price: number | null;
  costs: number;
  stop_loss: number | null;
  take_profit: number | null;
  filled_at: string | null;
  created_at: string;
};

export type PortfolioEquityPoint = {
  ts: string;
  cash: number;
  equity: number;
  open_pnl: number;
  realized_pnl: number;
  peak_equity: number;
  drawdown_pct: number;
  margin_used: number;
};

export type PortfolioQuery = {
  status?: string;
  limit?: number;
};

function str(value: unknown): string | undefined {
  if (typeof value === "string" && value.length > 0) return value;
  return undefined;
}

function strOrNull(value: unknown): string | null {
  return str(value) ?? null;
}

function numOr(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function numOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

/** Parse a date-time string; returns empty string when malformed/absent. */
function isoOr(value: unknown): string {
  const raw = str(value);
  if (raw === undefined) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString();
}

function isoOrNull(value: unknown): string | null {
  const parsed = isoOr(value);
  return parsed === "" ? null : parsed;
}

function sideOf(value: unknown): "LONG" | "SHORT" {
  return str(value)?.toUpperCase() === "SHORT" ? "SHORT" : "LONG";
}

// --- Normalizers ------------------------------------------------------------
export function normalizeSummary(raw: Record<string, unknown> | null | undefined): PortfolioSummary {
  const value = raw ?? {};
  return {
    as_of: isoOrNull(value["as_of"]),
    cash: numOr(value["cash"], 0),
    equity: numOr(value["equity"], 0),
    open_pnl: numOr(value["open_pnl"], 0),
    realized_pnl: numOr(value["realized_pnl"], 0),
    peak_equity: numOr(value["peak_equity"], 0),
    drawdown_pct: numOr(value["drawdown_pct"], 0),
    margin_used: numOr(value["margin_used"], 0),
    open_positions: numOr(value["open_positions"], 0),
    open_orders: numOr(value["open_orders"], 0),
  };
}

export function normalizePosition(raw: Record<string, unknown>): PortfolioPosition {
  const statusRaw = str(raw["status"])?.toUpperCase();
  return {
    id: str(raw["id"]) ?? "",
    order_id: str(raw["order_id"]) ?? "",
    decision_id: strOrNull(raw["decision_id"]),
    symbol: str(raw["symbol"]) ?? "",
    timeframe: str(raw["timeframe"]) ?? "",
    side: sideOf(raw["side"]),
    units: numOr(raw["units"], 0),
    entry_price: numOr(raw["entry_price"], 0),
    entry_ts: isoOr(raw["entry_ts"]),
    stop_loss: numOrNull(raw["stop_loss"]),
    take_profit: numOrNull(raw["take_profit"]),
    status: statusRaw === "CLOSED" ? "CLOSED" : "OPEN",
    exit_price: numOrNull(raw["exit_price"]),
    exit_ts: isoOrNull(raw["exit_ts"]),
    exit_reason: strOrNull(raw["exit_reason"]),
    gross_pnl: numOrNull(raw["gross_pnl"]),
    net_pnl: numOrNull(raw["net_pnl"]),
  };
}

export function normalizeOrder(raw: Record<string, unknown>): PortfolioOrder {
  const statusRaw = str(raw["status"])?.toUpperCase();
  return {
    id: str(raw["id"]) ?? "",
    decision_id: strOrNull(raw["decision_id"]),
    symbol: str(raw["symbol"]) ?? "",
    timeframe: str(raw["timeframe"]) ?? "",
    side: sideOf(raw["side"]),
    order_type: "NEXT_OPEN",
    status:
      statusRaw === "FILLED" ||
      statusRaw === "CANCELLED" ||
      statusRaw === "REJECTED"
        ? statusRaw
        : "PENDING",
    units: numOr(raw["units"], 0),
    requested_price: numOrNull(raw["requested_price"]),
    filled_price: numOrNull(raw["filled_price"]),
    costs: numOr(raw["costs"], 0),
    stop_loss: numOrNull(raw["stop_loss"]),
    take_profit: numOrNull(raw["take_profit"]),
    filled_at: isoOrNull(raw["filled_at"]),
    created_at: isoOr(raw["created_at"]),
  };
}

export function normalizeEquityPoint(raw: Record<string, unknown>): PortfolioEquityPoint {
  return {
    ts: isoOr(raw["ts"]),
    cash: numOr(raw["cash"], 0),
    equity: numOr(raw["equity"], 0),
    open_pnl: numOr(raw["open_pnl"], 0),
    realized_pnl: numOr(raw["realized_pnl"], 0),
    peak_equity: numOr(raw["peak_equity"], 0),
    drawdown_pct: numOr(raw["drawdown_pct"], 0),
    margin_used: numOr(raw["margin_used"], 0),
  };
}

// --- REST fetchers ----------------------------------------------------------
function buildQuery(query: PortfolioQuery): string {
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  if (query.limit) params.set("limit", String(query.limit));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export async function fetchPortfolioSummary(): Promise<PortfolioSummary> {
  const row = await authFetch<Record<string, unknown>>("/api/v1/portfolio/summary", {
    method: "GET",
  });
  return normalizeSummary(row ?? {});
}

export async function fetchPortfolioPositions(
  query: PortfolioQuery = {},
): Promise<PortfolioPosition[]> {
  const rows = await authFetch<Array<Record<string, unknown>>>(
    `/api/v1/portfolio/positions${buildQuery(query)}`,
    { method: "GET" },
  );
  return (rows ?? []).map(normalizePosition);
}

export async function fetchPortfolioOrders(
  query: PortfolioQuery = {},
): Promise<PortfolioOrder[]> {
  const rows = await authFetch<Array<Record<string, unknown>>>(
    `/api/v1/portfolio/orders${buildQuery(query)}`,
    { method: "GET" },
  );
  return (rows ?? []).map(normalizeOrder);
}

export async function fetchPortfolioEquity(
  query: PortfolioQuery = {},
): Promise<PortfolioEquityPoint[]> {
  const rows = await authFetch<Array<Record<string, unknown>>>(
    `/api/v1/portfolio/equity${buildQuery(query)}`,
    { method: "GET" },
  );
  return (rows ?? []).map(normalizeEquityPoint);
}

export { ApiError };