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

import { AlertsSocket, type AlertsSocketCallbacks, alertKey, mergeAlerts } from "./alerts";

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

function makeCallbacks(): { cb: AlertsSocketCallbacks; calls: Record<string, unknown[]> } {
  const calls: Record<string, unknown[]> = { open: [], reconnect: [], event: [], error: [], auth: [], close: [] };
  const cb: AlertsSocketCallbacks = {
    onOpen: () => calls.open.push(true),
    onReconnect: () => calls.reconnect.push(true),
    onEvent: (a) => calls.event.push(a),
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

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.useRealTimers();
  vi.resetModules();
  vi.clearAllMocks();
  vi.mocked(authFetch).mockResolvedValue({
    ticket: ticket(1),
    ws_url: `/api/v1/ws/stream?ticket=${ticket(1)}`,
    expires_in: 30,
  });
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

describe("AlertsSocket reconnect/resume/dedup", () => {
  it("acquires an initial ticket and opens with it (initial ticket acquisition)", async () => {
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new AlertsSocket(cb);
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
    // On open the client subscribes to the alerts topic.
    expect(MockWebSocket.instances[0].sent).toEqual([
      JSON.stringify({ type: "subscribe", topics: ["alerts"] }),
    ]);
    socket.close();
  });

  it("reconnects using a FRESH ticket after a disconnect (defect regression)", async () => {
    vi.useFakeTimers();
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new AlertsSocket(cb);
    await socket.start();
    await flushMicrotasks();
    MockWebSocket.instances[0].open();

    expect(vi.mocked(authFetch)).toHaveBeenCalledTimes(1);
    const firstUrl = MockWebSocket.instances[0].url;

    // Simulate the socket dropping. A NEW ticket must be fetched and used.
    vi.mocked(authFetch).mockResolvedValueOnce({
      ticket: ticket(2),
      ws_url: `/api/v1/ws/stream?ticket=${ticket(2)}`,
      expires_in: 30,
    });
    MockWebSocket.instances[0].teardown();
    await vi.advanceTimersByTimeAsync(1000); // first backoff delay
    await flushMicrotasks();

    expect(MockWebSocket.instances).toHaveLength(2);
    const secondUrl = MockWebSocket.instances[1].url;
    expect(vi.mocked(authFetch)).toHaveBeenCalledTimes(2); // fresh ticket acquired
    expect(secondUrl).not.toBe(firstUrl); // must NOT reuse the consumed ticket URL
    expect(secondUrl).toBe(`${mockWsUrl}/api/v1/ws/stream?ticket=${ticket(2)}`);

    MockWebSocket.instances[1].open();
    expect(calls.reconnect).toEqual([true]); // first open fired onOpen, this fires onReconnect

    socket.close();
  });

  it("stops reconnecting after an auth failure (no infinite loop)", async () => {
    vi.useFakeTimers();
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new AlertsSocket(cb);

    // First ticket succeeds; reconnect (post-disconnect) fails auth.
    vi.mocked(authFetch)
      .mockResolvedValueOnce({
        ticket: ticket(1),
        ws_url: `/api/v1/ws/stream?ticket=${ticket(1)}`,
        expires_in: 30,
      })
      .mockRejectedValueOnce(new ApiError(401, "Unauthorized"));

    await socket.start();
    await flushMicrotasks();
    MockWebSocket.instances[0].open();

    MockWebSocket.instances[0].teardown();
    await vi.advanceTimersByTimeAsync(1000);
    await flushMicrotasks();

    expect(vi.mocked(authFetch)).toHaveBeenCalledTimes(2);
    expect(calls.auth).toEqual([true]); // 401 → onAuthError, no reconnect scheduled
    socket.close();
  });

  it("retries with a backoff when a ticket cannot be obtained (transient error)", async () => {
    vi.useFakeTimers();
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new AlertsSocket(cb);

    vi.mocked(authFetch)
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce({
        ticket: ticket(1),
        ws_url: `/api/v1/ws/stream?ticket=${ticket(1)}`,
        expires_in: 30,
      });

    await socket.start();
    await flushMicrotasks();
    expect(calls.error).toEqual(["Could not obtain a WebSocket ticket (API unreachable or unauthorized)."]);

    await vi.advanceTimersByTimeAsync(1000);
    await flushMicrotasks();

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(vi.mocked(authFetch)).toHaveBeenCalledTimes(2);
    socket.close();
  });

  it("delivers live events and handles invalid/ticket-expiry error frames", async () => {
    installWebSocket();
    const { cb, calls } = makeCallbacks();
    const socket = new AlertsSocket(cb);
    await socket.start();
    await flushMicrotasks();
    MockWebSocket.instances[0].open();

    MockWebSocket.instances[0].receive({
      type: "event",
      topic: "alerts",
      data: { event_id: "evt-1", event_type: "alert.staleness", severity: "warning", title: "Stale" },
    });
    expect(calls.event).toHaveLength(1);
    expect((calls.event[0] as { event_id: string }).event_id).toBe("evt-1");

    MockWebSocket.instances[0].receive({ type: "error", reason: "ticket expired" });
    expect(calls.error).toEqual(["ticket expired"]);
    socket.close();
  });
});

describe("alertKey / mergeAlerts (stable event_id dedup + resume/history)", () => {
  it("keys live and REST alerts by the canonical durable event_id", () => {
    const a = { event_id: "evt-1", event_type: "alert.x", occurred_at: "2026-01-01T00:00:00Z", message: "a" } as never;
    const b = { event_id: "evt-1", event_type: "alert.x", occurred_at: "2026-01-01T00:00:00Z", message: "b" } as never;
    expect(alertKey(a)).toBe(alertKey(b));
    expect(alertKey(a)).toBe("id:evt-1");
  });

  it("does not collapse distinct events that share no event_id", () => {
    const a = { event_id: "evt-1", event_type: "alert.x", occurred_at: "2026-01-01T00:00:00Z" } as never;
    const b = { event_id: "evt-2", event_type: "alert.x", occurred_at: "2026-01-01T00:00:00Z" } as never;
    expect(alertKey(a)).not.toBe(alertKey(b));
  });

  it("mergeAlerts drops live items already present in history (resume after reconnect refetch)", () => {
    const rest = [
      { event_id: "A", event_type: "alert.s", occurred_at: "2026-01-01T00:00:00Z", source: "rest", title: "a", severity: "info" },
    ] as never[];
    const live = [
      { event_id: "A", event_type: "alert.s", occurred_at: "2026-01-01T00:00:00Z", source: "live", title: "a", severity: "info" },
      { event_id: "B", event_type: "alert.r", occurred_at: "2026-01-01T00:01:00Z", source: "live", title: "b", severity: "warning" },
    ] as never[];
    const merged = mergeAlerts(rest as never[], live as never[]);
    // A is already in history → only B is kept from the live buffer.
    expect(merged.map((x) => (x as { event_id: string }).event_id)).toEqual(["B", "A"]);
  });
});
