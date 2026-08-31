import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Hoisted so both the (hoisted) vi.mock factories and the test bodies can
// reference the shared values without hitting temporal-dead-zone errors.
const { mockWsUrl, ticket } = vi.hoisted(() => ({
  mockWsUrl: "ws://test-host",
  ticket: (n: number) => `ticket-${n}`,
}));

vi.mock("./auth", () => {
  class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }
  return {
    ApiError,
    authFetch: vi.fn(),
  };
});

vi.mock("./config", () => ({
  WS_URL: mockWsUrl,
  API_URL: "http://api-host",
}));

import {
  SignalsSocket,
  type SignalsSocketCallbacks,
  computeStance,
  decisionKey,
  mergeDecisions,
  mergeSignals,
  normalizeDecisionEvent,
  normalizeSignalEvent,
  signalKey,
  type AgentSignal,
  type DecisionItem,
} from "./signals";

const { authFetch, ApiError } = await import("./auth");

/** Manual WebSocket stand-in with test-controlled lifecycle events. */
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
  }

  open(): void {
    this.onopen?.();
  }

  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  error(): void {
    this.onerror?.();
  }

  teardown(): void {
    this.onclose?.();
  }
}

function makeCallbacks(): {
  cb: SignalsSocketCallbacks;
  calls: Record<string, unknown[]>;
} {
  const calls: Record<string, unknown[]> = {
    open: [],
    reconnect: [],
    signal: [],
    decision: [],
    error: [],
    auth: [],
    close: [],
  };
  const cb: SignalsSocketCallbacks = {
    onOpen: () => calls.open.push(true),
    onReconnect: () => calls.reconnect.push(true),
    onSignal: (s) => calls.signal.push(s),
    onDecision: (d) => calls.decision.push(d),
    onError: (r) => calls.error.push(r),
    onAuthError: () => calls.auth.push(true),
    onClose: () => calls.close.push(true),
  };
  return { cb, calls };
}

async function flushMicrotasks(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

function mockTicket(n: number): Record<string, unknown> {
  return {
    ticket: ticket(n),
    ws_url: `/api/v1/ws/stream?ticket=${ticket(n)}`,
    expires_in: 30,
  };
}

const signalFrame = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  type: "event",
  topic: "signals",
  data: {
    event_id: "sig-event-1",
    event_type: "signal.emitted",
    produced_at: "2026-01-01T00:00:00Z",
    agent_id: "technical",
    symbol: "EURUSD",
    timeframe: "H1",
    direction: "LONG",
    confidence: 0.8,
    bucket_ts: "2026-01-01T00:00:00.000Z",
    valid_until: "2026-01-01T01:00:00.000Z",
    rationale: "trend confluence",
    ...overrides,
  },
});

const decisionFrame = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  type: "event",
  topic: "decisions",
  data: {
    event_id: "dec-event-1",
    event_type: "decision.emitted",
    produced_at: "2026-01-01T00:00:00Z",
    symbol: "EURUSD",
    timeframe: "H1",
    bucket_ts: "2026-01-01T00:00:00.000Z",
    direction: "LONG",
    status: "PAPER",
    confidence: 0.7,
    agreement: 0.9,
    coverage: 0.75,
    veto_code: "",
    inputs_hash: "abc123",
    ...overrides,
  },
});

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.useRealTimers();
  vi.resetModules();
  vi.clearAllMocks();
  vi.mocked(authFetch).mockResolvedValue(mockTicket(1));
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  MockWebSocket.instances = [];
  (globalThis as { WebSocket?: unknown }).WebSocket = undefined;
});

function installWebSocket(): void {
  (globalThis as { WebSocket?: unknown }).WebSocket = MockWebSocket as unknown as typeof WebSocket;
}

describe("normalize*Event (live WS frames)", () => {
  it("maps a signals frame into an AgentSignal with live source", () => {
    const s = normalizeSignalEvent(signalFrame().data as Record<string, unknown>);
    expect(s).not.toBeNull();
    expect(s?.agent_id).toBe("technical");
    expect(s?.direction).toBe("LONG");
    expect(s?.confidence).toBe(0.8);
    expect(s?.source).toBe("live");
    expect(s?.symbol).toBe("EURUSD");
    expect(s?.timeframe).toBe("H1");
  });

  it("returns null for a non-signal payload", () => {
    expect(normalizeSignalEvent({ symbol: "EURUSD" })).toBeNull();
  });

  it("maps a decisions frame into a DecisionItem with live source", () => {
    const d = normalizeDecisionEvent(decisionFrame().data as Record<string, unknown>);
    expect(d).not.toBeNull();
    expect(d?.symbol).toBe("EURUSD");
    expect(d?.status).toBe("PAPER");
    expect(d?.direction).toBe("LONG");
    expect(d?.coverage).toBe(0.75);
    expect(d?.source).toBe("live");
  });

  it("returns null when a decisions frame lacks symbol or bucket", () => {
    expect(normalizeDecisionEvent({ status: "PAPER" })).toBeNull();
  });
});

describe("signalKey / decisionKey (canonical identity)", () => {
  const restSignal = {
    agent_id: "technical",
    symbol: "EURUSD",
    timeframe: "H1",
    bucket_ts: "2026-01-01T00:00:00Z",
  } as unknown as AgentSignal;
  const liveSignal = { ...restSignal, source: "live" } as unknown as AgentSignal;

  it("keys REST and live signals identically (cross-source dedup)", () => {
    expect(signalKey(restSignal)).toBe(signalKey(liveSignal));
    expect(signalKey(restSignal)).toBe("sig:technical:EURUSD:H1:2026-01-01T00:00:00Z");
  });

  it("distinguishes distinct signals from the same agent across buckets", () => {
    const other = { ...restSignal, bucket_ts: "2026-01-01T00:15:00Z" } as unknown as AgentSignal;
    expect(signalKey(restSignal)).not.toBe(signalKey(other));
  });

  const restDecision = {
    symbol: "EURUSD",
    timeframe: "H1",
    bucket_ts: "2026-01-01T00:00:00Z",
  } as unknown as DecisionItem;
  const liveDecision = { ...restDecision, source: "live" } as unknown as DecisionItem;

  it("keys REST and live decisions identically", () => {
    expect(decisionKey(restDecision)).toBe(decisionKey(liveDecision));
    expect(decisionKey(restDecision)).toBe("dec:EURUSD:H1:2026-01-01T00:00:00Z");
  });
});

describe("mergeSignals / mergeDecisions (REST + live dedup)", () => {
  it("drops live signals already present in history", () => {
    const rest: AgentSignal[] = [
      {
        agent_id: "technical",
        symbol: "EURUSD",
        timeframe: "H1",
        bucket_ts: "2026-01-01T00:00:00Z",
        direction: "LONG",
        confidence: 0.8,
        source: "rest",
      },
    ];
    const live: AgentSignal[] = [
      {
        agent_id: "technical",
        symbol: "EURUSD",
        timeframe: "H1",
        bucket_ts: "2026-01-01T00:00:00Z",
        direction: "LONG",
        confidence: 0.8,
        source: "live",
      },
      {
        agent_id: "sentiment",
        symbol: "EURUSD",
        timeframe: "H1",
        bucket_ts: "2026-01-01T00:00:00Z",
        direction: "SHORT",
        confidence: 0.5,
        source: "live",
      },
    ];
    const merged = mergeSignals(rest, live);
    const keys = merged.map(signalKey);
    // The live technical signal (same canonical identity as history) is dropped;
    // only the live sentiment signal survives from the buffer.
    expect(keys).toContain(signalKey(rest[0]));
    expect(keys).toContain(signalKey(live[1]));
    expect(keys.filter((k) => k === signalKey(rest[0]))).toHaveLength(1);
    expect(keys).toHaveLength(2);
  });

  it("drops live decisions already present in history", () => {
    const rest: DecisionItem[] = [
      {
        symbol: "EURUSD",
        timeframe: "H1",
        bucket_ts: "2026-01-01T00:00:00Z",
        direction: "LONG",
        status: "PAPER",
        source: "rest",
      },
    ];
    const live: DecisionItem[] = [
      {
        symbol: "EURUSD",
        timeframe: "H1",
        bucket_ts: "2026-01-01T00:00:00Z",
        direction: "LONG",
        status: "PAPER",
        source: "live",
      },
      {
        symbol: "EURUSD",
        timeframe: "H1",
        bucket_ts: "2026-01-01T00:15:00Z",
        direction: "FLAT",
        status: "ANALYSIS",
        source: "live",
      },
    ];
    const merged = mergeDecisions(rest, live);
    const keys = merged.map(decisionKey);
    // The live decision at the same bucket as history is dropped; only the newer
    // live decision survives from the buffer.
    expect(keys).toContain(decisionKey(rest[0]));
    expect(keys).toContain(decisionKey(live[1]));
    expect(keys.filter((k) => k === decisionKey(rest[0]))).toHaveLength(1);
    expect(keys).toHaveLength(2);
  });
});

describe("computeStance", () => {
  it("counts long/short/flat across the latest signals", () => {
    const signals: AgentSignal[] = [
      { agent_id: "a", symbol: "EURUSD", timeframe: "H1", bucket_ts: "x", direction: "LONG", confidence: 1, source: "rest" },
      { agent_id: "b", symbol: "EURUSD", timeframe: "H1", bucket_ts: "x", direction: "LONG", confidence: 1, source: "rest" },
      { agent_id: "c", symbol: "EURUSD", timeframe: "H1", bucket_ts: "x", direction: "SHORT", confidence: 1, source: "rest" },
      { agent_id: "d", symbol: "EURUSD", timeframe: "H1", bucket_ts: "x", direction: "FLAT", confidence: 1, source: "rest" },
    ];
    expect(computeStance(signals)).toEqual({ long: 2, short: 1, flat: 1, total: 4 });
    expect(computeStance([])).toEqual({ long: 0, short: 0, flat: 0, total: 0 });
  });
});

describe("SignalsSocket reconnect/subscribe/delivery", () => {
  it("acquires a ticket, opens, and subscribes to signals+decisions", async () => {
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new SignalsSocket(cb);
    await socket.start();
    await flushMicrotasks();

    expect(vi.mocked(authFetch)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(authFetch)).toHaveBeenCalledWith("/api/v1/ws/ticket", { method: "POST" });
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(MockWebSocket.instances[0].url).toBe(
      `${mockWsUrl}/api/v1/ws/stream?ticket=${ticket(1)}`,
    );

    MockWebSocket.instances[0].open();
    expect(calls.open).toEqual([true]);
    expect(MockWebSocket.instances[0].sent).toEqual([
      JSON.stringify({ type: "subscribe", topics: ["signals", "decisions"] }),
    ]);
    socket.close();
  });

  it("reconnects using a FRESH ticket after a disconnect (defect regression)", async () => {
    vi.useFakeTimers();
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new SignalsSocket(cb);
    await socket.start();
    await flushMicrotasks();
    MockWebSocket.instances[0].open();

    expect(vi.mocked(authFetch)).toHaveBeenCalledTimes(1);
    const firstUrl = MockWebSocket.instances[0].url;

    vi.mocked(authFetch).mockResolvedValueOnce(mockTicket(2));
    MockWebSocket.instances[0].teardown();
    await vi.advanceTimersByTimeAsync(1000);
    await flushMicrotasks();

    expect(MockWebSocket.instances).toHaveLength(2);
    const secondUrl = MockWebSocket.instances[1].url;
    expect(vi.mocked(authFetch)).toHaveBeenCalledTimes(2); // fresh ticket acquired
    expect(secondUrl).not.toBe(firstUrl); // must NOT reuse the consumed ticket URL
    expect(secondUrl).toBe(`${mockWsUrl}/api/v1/ws/stream?ticket=${ticket(2)}`);

    MockWebSocket.instances[1].open();
    expect(calls.reconnect).toEqual([true]);
    socket.close();
  });

  it("stops reconnecting after an auth failure (no infinite loop)", async () => {
    vi.useFakeTimers();
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new SignalsSocket(cb);

    vi.mocked(authFetch)
      .mockResolvedValueOnce(mockTicket(1))
      .mockRejectedValueOnce(new ApiError(401, "Unauthorized"));

    await socket.start();
    await flushMicrotasks();
    MockWebSocket.instances[0].open();

    MockWebSocket.instances[0].teardown();
    await vi.advanceTimersByTimeAsync(1000);
    await flushMicrotasks();

    expect(vi.mocked(authFetch)).toHaveBeenCalledTimes(2);
    expect(calls.auth).toEqual([true]);
    socket.close();
  });

  it("retries with a backoff when a ticket cannot be obtained (transient error)", async () => {
    vi.useFakeTimers();
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new SignalsSocket(cb);

    vi.mocked(authFetch)
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(mockTicket(1));

    await socket.start();
    await flushMicrotasks();
    expect(calls.error).toEqual([
      "Could not obtain a WebSocket ticket (API unreachable or unauthorized).",
    ]);

    await vi.advanceTimersByTimeAsync(1000);
    await flushMicrotasks();

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(vi.mocked(authFetch)).toHaveBeenCalledTimes(2);
    socket.close();
  });

  it("delivers live signal and decision events, and forwards error frames", async () => {
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new SignalsSocket(cb);
    await socket.start();
    await flushMicrotasks();
    MockWebSocket.instances[0].open();

    MockWebSocket.instances[0].receive(signalFrame());
    expect(calls.signal).toHaveLength(1);
    expect((calls.signal[0] as AgentSignal).agent_id).toBe("technical");

    MockWebSocket.instances[0].receive(decisionFrame());
    expect(calls.decision).toHaveLength(1);
    expect((calls.decision[0] as DecisionItem).status).toBe("PAPER");

    MockWebSocket.instances[0].receive({ type: "error", reason: "ticket expired" });
    expect(calls.error).toEqual(["ticket expired"]);
    socket.close();
  });
});
