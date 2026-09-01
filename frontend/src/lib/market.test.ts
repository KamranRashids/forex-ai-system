import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiHost } = vi.hoisted(() => ({
  apiHost: "http://api-host",
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
  API_URL: apiHost,
  WS_URL: "ws://test-host",
}));

import {
  fetchCandles,
  normalizeCandle,
  recentCandlesStart,
  type Candle,
} from "./market";

const { authFetch } = await import("./auth");

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("normalizeCandle", () => {
  it("maps raw rows into a typed Candle", () => {
    const row: Record<string, unknown> = {
      symbol: "EURUSD",
      timeframe: "H1",
      ts: "2026-09-01T07:00:00Z",
      open: "1.08530",
      high: "1.08600",
      low: "1.08480",
      close: "1.08560",
      volume: 1234.5,
    };
    expect(normalizeCandle(row)).toEqual({
      symbol: "EURUSD",
      timeframe: "H1",
      ts: "2026-09-01T07:00:00Z",
      open: 1.0853,
      high: 1.086,
      low: 1.0848,
      close: 1.0856,
      volume: 1234.5,
    });
  });

  it("falls back on missing or malformed numeric fields", () => {
    const candle = normalizeCandle({ ts: "2026-09-01T07:00:00Z" } as Record<string, unknown>);
    expect(candle.open).toBe(0);
    expect(candle.high).toBe(0);
    expect(candle.close).toBe(0);
    expect(candle.volume).toBe(0);
    expect(candle.symbol).toBe("");
  });
});

describe("recentCandlesStart", () => {
  const now = new Date("2026-09-01T12:00:00Z");

  it("opens a longer window for larger timeframes", () => {
    const m5 = Date.parse(recentCandlesStart("M5", 8, now));
    const h1 = Date.parse(recentCandlesStart("H1", 8, now));
    expect(m5).toBeGreaterThan(h1);
  });

  it("is anchored to the supplied now", () => {
    const start = Date.parse(recentCandlesStart("H1", 8, now));
    expect(start).toBeLessThan(now.getTime());
    // H1 × 8 bars → a ~7.2h window (0.9 scale), always inside [6h, 8h] ago.
    expect(start).toBeGreaterThan(now.getTime() - 8 * 60 * 60 * 1000);
    expect(start).toBeLessThan(now.getTime() - 6 * 60 * 60 * 1000);
  });
});

describe("fetchCandles", () => {
  it("calls the candles endpoint with symbol/timeframe and normalizes rows", async () => {
    const rows = [
      {
        symbol: "EURUSD",
        timeframe: "H1",
        ts: "2026-09-01T07:00:00Z",
        open: "1.08530",
        high: "1.08600",
        low: "1.08480",
        close: "1.08560",
        volume: 100,
      },
    ];
    vi.mocked(authFetch).mockResolvedValue(rows);

    const candles: Candle[] = await fetchCandles("EURUSD", "H1");
    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(authFetch).toHaveBeenCalledWith("/api/v1/market/candles?symbol=EURUSD&timeframe=H1", {
      method: "GET",
    });
    expect(candles).toHaveLength(1);
    expect(candles[0].close).toBe(1.0856);
    expect(candles[0].ts).toBe("2026-09-01T07:00:00Z");
  });

  it("includes start/end/limit query params when provided", async () => {
    vi.mocked(authFetch).mockResolvedValue([]);
    await fetchCandles("GBPUSD", "M15", { start: "2026-09-01T00:00:00Z", end: "2026-09-01T08:00:00Z", limit: 8 });
    const url = vi.mocked(authFetch).mock.calls[0][0] as string;
    expect(url).toContain("symbol=GBPUSD");
    expect(url).toContain("timeframe=M15");
    expect(url).toContain("start=2026-09-01T00%3A00%3A00Z");
    expect(url).toContain("end=2026-09-01T08%3A00%3A00Z");
    expect(url).toContain("limit=8");
  });

  it("returns an empty array when the API returns null", async () => {
    vi.mocked(authFetch).mockResolvedValue(null);
    await expect(fetchCandles("EURUSD", "H1")).resolves.toEqual([]);
  });
});