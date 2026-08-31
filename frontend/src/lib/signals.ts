import { ApiError, authFetch } from "./auth";
import { WS_URL } from "./config";

export type Timeframe = "M5" | "M15" | "H1" | "H4" | "D1";

export type Direction = "LONG" | "SHORT" | "FLAT";
export type DecisionStatus = "ANALYSIS" | "PAPER" | "BLOCKED";

// --- Signal (agent) ---------------------------------------------------------
export type AgentSignal = {
  agent_id: string;
  version?: string;
  symbol: string;
  timeframe: string;
  direction: Direction | string;
  confidence: number;
  bucket_ts: string;
  valid_until?: string | null;
  rationale?: string;
  features?: Record<string, unknown>;
  created_at?: string | null;
  run_id?: string | null;
  source: "rest" | "live";
};

// --- Decision ---------------------------------------------------------------
export type DecisionItem = {
  id?: unknown;
  run_id?: string;
  symbol: string;
  timeframe: string;
  bucket_ts: string;
  direction: Direction | string;
  agreement?: number;
  confidence?: number;
  coverage?: number;
  status: DecisionStatus | string;
  veto_code?: string | null;
  veto_reason?: string | null;
  inputs_hash?: string;
  weights?: Record<string, unknown>;
  code_versions?: Record<string, unknown>;
  rationale?: string | null;
  decision_at?: string;
  valid_until?: string | null;
  source: "rest" | "live";
};

// --- Risk evaluation --------------------------------------------------------
export type RiskGate = {
  code: string;
  ok: boolean;
  detail: string;
};

export type RiskEvaluation = {
  id?: unknown;
  symbol: string;
  timeframe: string;
  bucket_ts: string;
  position_size_units?: number | null;
  price?: number | null;
  atr?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
  rr_ratio?: number | null;
  risk_pct_account?: number | null;
  exposure_ok: boolean;
  correlation_ok: boolean;
  daily_loss_ok: boolean;
  drawdown_ok: boolean;
  passed: boolean;
  reasons: RiskGate[];
  evaluated_at: string;
};

// --- Stance summary ---------------------------------------------------------
export type StanceSummary = {
  long: number;
  short: number;
  flat: number;
  total: number;
};

function toQueryString(params: Record<string, string | number | boolean | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(
      ([key, value]) =>
        `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`,
    );
  return parts.length ? `?${parts.join("&")}` : "";
}

// --- REST fetchers ----------------------------------------------------------
export async function fetchLatestSignals(
  symbol: string,
  timeframe: string,
): Promise<AgentSignal[]> {
  const query = toQueryString({ symbol, timeframe, fresh_only: true });
  const rows = await authFetch<Array<Record<string, unknown>>>(
    `/api/v1/signals/latest${query}`,
    { method: "GET" },
  );
  return rows.map((r) => toAgentSignal(r, "rest"));
}

export async function fetchSignalHistory(
  symbol: string,
  timeframe: string,
  limit = 50,
): Promise<AgentSignal[]> {
  const query = toQueryString({ symbol, timeframe, limit });
  const rows = await authFetch<Array<Record<string, unknown>>>(`/api/v1/signals${query}`, {
    method: "GET",
  });
  return rows.map((r) => toAgentSignal(r, "rest"));
}

export async function fetchLatestDecision(
  symbol: string,
  timeframe: string,
): Promise<DecisionItem | null> {
  const query = toQueryString({ symbol, timeframe, fresh_only: true });
  const row = await authFetch<Record<string, unknown> | null>(`/api/v1/decisions/latest${query}`, {
    method: "GET",
  });
  return row ? toDecision(row, "rest") : null;
}

export async function fetchDecisionHistory(
  symbol: string,
  timeframe: string,
  limit = 50,
): Promise<DecisionItem[]> {
  const query = toQueryString({ symbol, timeframe, limit });
  const rows = await authFetch<Array<Record<string, unknown>>>(`/api/v1/decisions${query}`, {
    method: "GET",
  });
  return rows.map((r) => toDecision(r, "rest"));
}

export async function fetchRiskEvaluation(
  symbol: string,
  timeframe: string,
): Promise<RiskEvaluation | null> {
  const query = toQueryString({ symbol, timeframe, limit: 1 });
  const rows = await authFetch<Array<Record<string, unknown>>>(
    `/api/v1/decisions/risk-evaluations${query}`,
    { method: "GET" },
  );
  const row = rows[0];
  if (!row) return null;
  const reasons: RiskGate[] = Array.isArray(row["reasons"])
    ? (row["reasons"] as Array<Record<string, unknown>>).map((r) => ({
        code: String(r["code"] ?? ""),
        ok: Boolean(r["ok"]),
        detail: String(r["detail"] ?? ""),
      }))
    : [];
  return {
    id: row["id"],
    symbol: String(row["symbol"] ?? ""),
    timeframe: String(row["timeframe"] ?? ""),
    bucket_ts: String(row["bucket_ts"] ?? ""),
    position_size_units: numOrNull(row["position_size_units"]),
    price: numOrNull(row["price"]),
    atr: numOrNull(row["atr"]),
    stop_loss: numOrNull(row["stop_loss"]),
    take_profit: numOrNull(row["take_profit"]),
    rr_ratio: numOrNull(row["rr_ratio"]),
    risk_pct_account: numOrNull(row["risk_pct_account"]),
    exposure_ok: Boolean(row["exposure_ok"]),
    correlation_ok: Boolean(row["correlation_ok"]),
    daily_loss_ok: Boolean(row["daily_loss_ok"]),
    drawdown_ok: Boolean(row["drawdown_ok"]),
    passed: Boolean(row["passed"]),
    reasons,
    evaluated_at: String(row["evaluated_at"] ?? ""),
  };
}

// --- Conversion helpers -----------------------------------------------------
function toAgentSignal(raw: Record<string, unknown>, source: "rest" | "live"): AgentSignal {
  return {
    agent_id: str(raw["agent_id"] ?? raw["agent"] ?? "unknown") ?? "unknown",
    version: str(raw["version"]),
    symbol: String(raw["symbol"] ?? ""),
    timeframe: String(raw["timeframe"] ?? ""),
    direction: (str(raw["direction"]) || "FLAT") as Direction,
    confidence: numOr(raw["confidence"], 0),
    bucket_ts: str(raw["bucket_ts"]) ?? "",
    valid_until: str(raw["valid_until"]) ?? null,
    rationale: str(raw["rationale"]),
    features: raw["features"] as Record<string, unknown> | undefined,
    created_at: str(raw["created_at"]) ?? null,
    run_id: str(raw["run_id"]) ?? null,
    source,
  };
}

function toDecision(raw: Record<string, unknown>, source: "rest" | "live"): DecisionItem {
  const direction = str(raw["direction"]) ?? str(raw["fused_direction"]);
  return {
    id: raw["id"],
    run_id: str(raw["run_id"]),
    symbol: String(raw["symbol"] ?? ""),
    timeframe: String(raw["timeframe"] ?? ""),
    bucket_ts: str(raw["bucket_ts"]) ?? "",
    direction: (direction || "FLAT") as Direction,
    agreement: numOr(raw["agreement"], 0),
    confidence: numOr(raw["confidence"], 0),
    coverage: numOr(raw["coverage"], 0),
    status: (str(raw["status"]) || "ANALYSIS") as DecisionStatus,
    veto_code: str(raw["veto_code"]) ?? null,
    veto_reason: str(raw["veto_reason"]) ?? null,
    inputs_hash: str(raw["inputs_hash"]),
    weights: raw["weights"] as Record<string, unknown> | undefined,
    code_versions: raw["code_versions"] as Record<string, unknown> | undefined,
    rationale: str(raw["rationale"]),
    decision_at: str(raw["decision_at"]),
    valid_until: str(raw["valid_until"]) ?? null,
    source,
  };
}

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

function numOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

// --- Stable canonical identity ----------------------------------------------
// REST rows and live WS frames both carry (agent_id, symbol, timeframe, bucket_ts)
// for signals, and (symbol, timeframe, bucket_ts) for decisions. These are the
// canonical identities shared across both sources, so keying on them lets live
// frames deduplicate against REST history (the same event appearing after a
// reconnect refetch) without collapsing distinct events.
export function signalKey(signal: { agent_id: string; symbol: string; timeframe: string; bucket_ts: string }): string {
  return `sig:${signal.agent_id}:${signal.symbol}:${signal.timeframe}:${signal.bucket_ts}`;
}

export function decisionKey(decision: { symbol: string; timeframe: string; bucket_ts: string }): string {
  return `dec:${decision.symbol}:${decision.timeframe}:${decision.bucket_ts}`;
}

// --- Live event normalization (WS frames) -----------------------------------
// The realtime hub enriches every frame with event_type / produced_at / event_id
// (app/api/v1/realtime.py:_event_frame) next to the original event payload.
export function normalizeSignalEvent(payload: Record<string, unknown>): AgentSignal | null {
  if (str(payload["agent_id"]) === undefined && str(payload["agent"]) === undefined) return null;
  const producedAt = str(payload["produced_at"]) ?? new Date().toISOString();
  const bucket = str(payload["bucket_ts"]) ?? producedAt;
  const signal = toAgentSignal({ ...payload, bucket_ts: bucket }, "live");
  return signal;
}

export function normalizeDecisionEvent(payload: Record<string, unknown>): DecisionItem | null {
  if (str(payload["symbol"]) === undefined || str(payload["bucket_ts"]) === undefined) {
    return null;
  }
  const producedAt = str(payload["produced_at"]) ?? new Date().toISOString();
  const bucket = str(payload["bucket_ts"]) ?? producedAt;
  return toDecision({ ...payload, bucket_ts: bucket }, "live");
}

// --- Merging (REST history source of truth + live buffer) -------------------
export function mergeSignals(history: AgentSignal[], live: AgentSignal[]): AgentSignal[] {
  const historyKeys = new Set(history.map(signalKey));
  const liveOnly = live.filter((item) => !historyKeys.has(signalKey(item)));
  return [...liveOnly, ...history];
}

export function mergeDecisions(history: DecisionItem[], live: DecisionItem[]): DecisionItem[] {
  const historyKeys = new Set(history.map(decisionKey));
  const liveOnly = live.filter((item) => !historyKeys.has(decisionKey(item)));
  return [...liveOnly, ...history];
}

/** Per-pair stance: count of agents currently long/short/flat from latest signals. */
export function computeStance(signals: AgentSignal[]): StanceSummary {
  const summary: StanceSummary = { long: 0, short: 0, flat: 0, total: 0 };
  for (const signal of signals) {
    if (signal.direction === "LONG") summary.long += 1;
    else if (signal.direction === "SHORT") summary.short += 1;
    else summary.flat += 1;
    summary.total += 1;
  }
  return summary;
}

// --- WebSocket client -------------------------------------------------------
export type TicketOut = {
  ticket: string;
  ws_url: string;
  expires_in: number;
};

export type ConnectionState =
  | "connecting"
  | "open"
  | "reconnected"
  | "reconnecting"
  | "closed"
  | "error";

export type SignalsSocketCallbacks = {
  onOpen?: () => void;
  onReconnect?: () => void;
  onSignal: (signal: AgentSignal) => void;
  onDecision: (decision: DecisionItem) => void;
  onError: (reason: string) => void;
  onAuthError?: () => void;
  onClose: () => void;
};

type EventFrame = {
  type: "event";
  topic: string;
  data: Record<string, unknown>;
};

type ErrorFrame = {
  type: "error";
  reason?: string;
};

type SubscribedFrame = {
  type: "subscribed";
  topics: string[];
};

type ServerFrame = EventFrame | ErrorFrame | SubscribedFrame;

export class SignalsSocket {
  private socket: WebSocket | null = null;
  private closed = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempts = 0;
  private firstOpen = true;
  private cb: SignalsSocketCallbacks;

  constructor(cb: SignalsSocketCallbacks) {
    this.cb = cb;
  }

  async start(): Promise<void> {
    this.closed = false;
    await this.connect();
  }

  /** Obtain a fresh one-time ticket and open a socket bound to it. */
  private async connect(): Promise<void> {
    if (this.closed) return;
    let ticket: TicketOut;
    try {
      ticket = await authFetch<TicketOut>("/api/v1/ws/ticket", { method: "POST" });
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        this.cb.onAuthError?.();
        return;
      }
      this.cb.onError("Could not obtain a WebSocket ticket (API unreachable or unauthorized).");
      this.scheduleReconnect();
      return;
    }
    if (this.closed) return;
    this.open(`${WS_URL}${ticket.ws_url}`);
  }

  private open(ticketUrl: string): void {
    if (this.closed) return;
    const socket = new WebSocket(ticketUrl);
    this.socket = socket;

    socket.onopen = () => {
      this.reconnectAttempts = 0;
      if (this.firstOpen) {
        this.firstOpen = false;
        this.cb.onOpen?.();
      } else {
        this.cb.onReconnect?.();
      }
      socket.send(JSON.stringify({ type: "subscribe", topics: ["signals", "decisions"] }));
    };

    socket.onmessage = (event) => {
      let frame: ServerFrame;
      try {
        frame = JSON.parse(String(event.data)) as ServerFrame;
      } catch {
        return;
      }
      if (frame.type === "event") {
        if (frame.topic === "signals") {
          const signal = normalizeSignalEvent(frame.data);
          if (signal) this.cb.onSignal(signal);
        } else if (frame.topic === "decisions") {
          const decision = normalizeDecisionEvent(frame.data);
          if (decision) this.cb.onDecision(decision);
        }
      } else if (frame.type === "error") {
        this.cb.onError(frame.reason ?? "Unknown stream error");
      }
    };

    socket.onerror = () => {
      this.cb.onError("WebSocket connection error; retrying.");
      this.scheduleReconnect();
    };

    socket.onclose = () => {
      if (this.closed) {
        this.cb.onClose();
        return;
      }
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.closed || this.reconnectTimer !== null) return;
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 15000);
    this.reconnectAttempts += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      // Reconnect MUST use a FRESH one-time ticket: the previous ticket was
      // atomically consumed (GETDEL) by the socket that just closed, so reusing
      // its URL would be rejected with 4401 and re-enter a broken retry loop.
      void this.connect();
    }, delay);
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.close();
      this.socket = null;
    }
  }
}

