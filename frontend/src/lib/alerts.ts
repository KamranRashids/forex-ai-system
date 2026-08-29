import { ApiError, authFetch } from "./auth";
import { WS_URL } from "./config";

export type Severity = "info" | "warning" | "critical";

export type Alert = {
  id: string;
  event_id: string;
  event_type: string;
  source: string;
  severity: Severity;
  title: string;
  message: string;
  symbol?: string | null;
  timeframe?: string | null;
  producer?: string | null;
  correlation_id?: string | null;
  occurred_at: string;
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
  created_at: string;
};

export type AlertsPage = {
  items: Alert[];
  total: number;
  limit: number;
  offset: number;
};

export type AlertListParams = {
  limit?: number;
  offset?: number;
  source?: string;
  event_type?: string;
  acknowledged?: boolean;
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

export async function fetchAlerts(params: AlertListParams = {}): Promise<AlertsPage> {
  const query = toQueryString({
    limit: params.limit ?? 100,
    offset: params.offset ?? 0,
    source: params.source,
    event_type: params.event_type,
    acknowledged: params.acknowledged,
  });
  return authFetch<AlertsPage>(`/api/v1/alerts${query}`, { method: "GET" });
}

type TicketOut = {
  ticket: string;
  ws_url: string;
  expires_in: number;
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

export type ConnectionState =
  | "connecting"
  | "open"
  | "reconnected"
  | "reconnecting"
  | "closed"
  | "error";

export type AlertsSocketCallbacks = {
  onOpen?: () => void;
  onReconnect?: () => void;
  onEvent: (alert: Alert) => void;
  onError: (reason: string) => void;
  onAuthError?: () => void;
  onClose: () => void;
};

const stringVal = (value: unknown): string | undefined =>
  typeof value === "string" && value.length > 0 ? value : undefined;

function isoString(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  return undefined;
}

export function normalizeEvent(payload: Record<string, unknown>): Alert {
  const eventType =
    stringVal(payload["event_type"]) ?? stringVal(payload["type"]) ?? "unknown";
  const producedAt = isoString(payload["produced_at"]) ?? new Date().toISOString();
  const severity = (stringVal(payload["severity"]) ?? "info") as Severity;
  const fallbackId = `${eventType}:${producedAt}:LIVE`;

  return {
    id: stringVal(payload["event_id"]) ?? fallbackId,
    event_id: stringVal(payload["event_id"]) ?? fallbackId,
    event_type: eventType,
    source: stringVal(payload["source"]) ?? "live",
    severity,
    title: stringVal(payload["title"]) ?? stringVal(payload["subject"]) ?? eventType,
    message: stringVal(payload["message"]) ?? stringVal(payload["detail"]) ?? "",
    symbol: stringVal(payload["symbol"]),
    timeframe: stringVal(payload["timeframe"]),
    producer: stringVal(payload["producer"]),
    correlation_id: stringVal(payload["correlation_id"]),
    occurred_at: producedAt,
    acknowledged_at: stringVal(payload["acknowledged_at"]),
    acknowledged_by: stringVal(payload["acknowledged_by"]),
    created_at: producedAt,
  };
}

// The synthetic placeholder identity used only when a live frame is missing a
// real backend event_id (defensive fallback; production frames always carry one).
function isSyntheticId(eventId: string): boolean {
  return eventId.endsWith(":LIVE");
}

export function alertKey(alert: Alert): string {
  // The backend injects the canonical durable event_id into live WS frames,
  // matching the REST AlertOut.event_id, so it is the collision-safe identity
  // across both sources. Distinct events never share an event_id, so keying on
  // it can neither collapse different alerts nor duplicate the same one after a
  // reconnect refetch. Fall back to a composite only for defensive synthetic ids.
  if (!isSyntheticId(alert.event_id)) {
    return `id:${alert.event_id}`;
  }
  return `${alert.event_type}::${alert.occurred_at}::${alert.symbol ?? ""}`;
}

// Merge a REST history (durable source of truth, ordered newest-first) with the
// live WS buffer. Live items are kept only when they are not already present in
// the history (by alertKey), avoiding duplicates after a reconnect refetch.
export function mergeAlerts(history: Alert[], live: Alert[]): Alert[] {
  const historyKeys = new Set(history.map(alertKey));
  const liveOnly = live.filter((item) => !historyKeys.has(alertKey(item)));
  return [...liveOnly, ...history];
}

export class AlertsSocket {
  private socket: WebSocket | null = null;
  private closed = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempts = 0;
  private firstOpen = true;
  private cb: AlertsSocketCallbacks;
  private ticketUrl: string | null = null;

  constructor(cb: AlertsSocketCallbacks) {
    this.cb = cb;
  }

  async start(): Promise<void> {
    this.closed = false;
    try {
      const ticket = await authFetch<TicketOut>("/api/v1/ws/ticket", { method: "POST" });
      this.ticketUrl = `${WS_URL}${ticket.ws_url}`;
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        this.cb.onAuthError?.();
        return;
      }
      this.cb.onError("Could not obtain a WebSocket ticket (API unreachable or unauthorized).");
      return;
    }
    this.open();
  }

  private open(): void {
    if (this.closed || !this.ticketUrl) return;
    const socket = new WebSocket(this.ticketUrl);
    this.socket = socket;

    socket.onopen = () => {
      if (this.firstOpen) {
        this.firstOpen = false;
        this.cb.onOpen?.();
      } else {
        this.cb.onReconnect?.();
      }
      socket.send(JSON.stringify({ type: "subscribe", topics: ["alerts"] }));
    };

    socket.onmessage = (event) => {
      let frame: ServerFrame;
      try {
        frame = JSON.parse(String(event.data)) as ServerFrame;
      } catch {
        return;
      }
      if (frame.type === "event" && frame.topic === "alerts") {
        this.cb.onEvent(normalizeEvent(frame.data));
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
      this.open();
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
