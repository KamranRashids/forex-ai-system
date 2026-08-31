import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  normalizeRun,
  normalizeTrade,
  normalizeEquityPoint,
  deriveRunRange,
  fmtCurrency,
  fmtPct,
  fmtNumber,
  fmtDateTime,
  buildEquityPath,
  buildDrawdownPath,
  buildPath,
  normalizeForComparison,
  fetchBacktests,
  fetchBacktestDetail,
  fetchBacktestTrades,
  fetchBacktestEquity,
} from "./backtests";

const baseRun = {
  id: "11111111-1111-1111-1111-111111111111",
  status: "COMPLETED",
  seed: 42,
  started_at: "2026-01-01T00:00:00Z",
  finished_at: "2026-01-02T00:00:00Z",
  metrics: {
    net_pnl: 1000,
    gross_pnl: 2000,
    total_costs: 50,
    num_trades: 10,
    win_rate: 0.6,
    profit_factor: 2.5,
    sharpe: 1.8,
    sortino: 2.1,
    max_drawdown_pct: 5.5,
    exposure_avg_pct: 120,
    bars: 500,
    degraded_runs: 1,
    coverage: [
      {
        symbol: "EURUSD",
        timeframe: "H1",
        expected_bars: 100,
        technical: 100,
        regime: 90,
        fundamental: 0,
        sentiment: 0,
        degraded: true,
      },
    ],
  },
  error: null,
};

describe("normalization", () => {
  it("normalizes a complete BacktestRun", () => {
    const run = normalizeRun(baseRun);
    expect(run.id).toBe(baseRun.id);
    expect(run.status).toBe("COMPLETED");
    expect(run.seed).toBe(42);
    expect(run.metrics.net_pnl).toBe(1000);
    expect(run.metrics.coverage[0].degraded).toBe(true);
    expect(run.error).toBeNull();
  });

  it("coerces unknown status to FAILED", () => {
    expect(normalizeRun({ ...baseRun, status: "WEIRD" }).status).toBe("FAILED");
    expect(normalizeRun({ ...baseRun, status: "running" }).status).toBe("RUNNING");
  });

  it("handles missing/null fields defensively", () => {
    const run = normalizeRun({});
    expect(run.id).toBe("");
    expect(run.status).toBe("FAILED");
    expect(run.metrics.net_pnl).toBe(0);
    expect(run.started_at).toBe("");
    expect(run.finished_at).toBeNull();
    expect(run.error).toBeNull();
  });

  it("handles malformed numeric values", () => {
    const run = normalizeRun({
      ...baseRun,
      seed: "not-a-number",
      metrics: { net_pnl: "banana", sharpe: null, win_rate: 0.5 },
    });
    expect(run.seed).toBe(0);
    expect(run.metrics.net_pnl).toBe(0);
    expect(run.metrics.sharpe).toBe(0);
    expect(run.metrics.win_rate).toBe(0.5);
  });

  it("handles malformed dates", () => {
    const run = normalizeRun({ ...baseRun, started_at: "garbage" });
    expect(run.started_at).toBe("");
  });

  it("normalizes coverage lists and non-array coverage", () => {
    const bad = normalizeRun({ ...baseRun, metrics: { ...baseRun.metrics, coverage: "nope" } });
    expect(bad.metrics.coverage).toEqual([]);
    const empty = normalizeRun({ ...baseRun, metrics: {} });
    expect(empty.metrics.coverage).toEqual([]);
  });

  it("normalizes a trade", () => {
    const trade = normalizeTrade({
      id: "t1",
      symbol: "EURUSD",
      timeframe: "H1",
      side: "SHORT",
      units: "1000",
      entry_ts: "2026-01-01T00:00:00Z",
      entry_price: "1.1000",
      exit_ts: "2026-01-01T01:00:00Z",
      exit_price: "1.0950",
      gross_pnl: 50,
      costs: 2,
      net_pnl: 48,
      exit_reason: "sl",
    });
    expect(trade.side).toBe("SHORT");
    expect(trade.units).toBe(1000);
    expect(trade.entry_price).toBe(1.1);
    expect(trade.net_pnl).toBe(48);
  });

  it("coerces unknown trade side to LONG", () => {
    expect(normalizeTrade({ side: "SIDEWAYS" }).side).toBe("LONG");
  });

  it("normalizes an equity point", () => {
    const p = normalizeEquityPoint({ id: "e1", ts: "2026-01-01T00:00:00Z", equity: "100000", drawdown_pct: "1.5" });
    expect(p.equity).toBe(100000);
    expect(p.drawdown_pct).toBe(1.5);
  });
});

describe("formatting", () => {
  it("fmtCurrency formats and guards NaN", () => {
    expect(fmtCurrency(1000)).toContain("1,000");
    expect(fmtCurrency(NaN)).toBe("—");
    expect(fmtCurrency(Infinity)).toBe("—");
  });

  it("fmtPct appends percent and guards invalid", () => {
    expect(fmtPct(5.55)).toBe("5.55%");
    expect(fmtPct(0)).toBe("0.00%");
    expect(fmtPct(NaN)).toBe("—");
  });

  it("fmtNumber formats and guards invalid", () => {
    expect(fmtNumber(1.2345)).toBe("1.23");
    expect(fmtNumber(NaN)).toBe("—");
  });

  it("fmtDateTime formats and guards invalid", () => {
    expect(fmtDateTime("2026-01-01T00:00:00Z")).not.toBe("—");
    expect(fmtDateTime(null)).toBe("—");
    expect(fmtDateTime("garbage")).toBe("—");
    expect(fmtDateTime("")).toBe("—");
  });
});

describe("run range", () => {
  it("derives range from equity timestamps", () => {
    const points = [
      { ts: "2026-01-01T00:00:00Z", equity: 100, drawdown_pct: 0 },
      { ts: "2026-01-02T00:00:00Z", equity: 110, drawdown_pct: 1 },
    ];
    expect(deriveRunRange(points)).toEqual({ start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z" });
  });

  it("returns null range for empty equity", () => {
    expect(deriveRunRange([])).toEqual({ start: null, end: null });
  });

  it("handles single-point equity", () => {
    expect(deriveRunRange([{ ts: "2026-01-01T00:00:00Z", equity: 100, drawdown_pct: 0 }])).toEqual({
      start: "2026-01-01T00:00:00Z",
      end: "2026-01-01T00:00:00Z",
    });
  });
});

describe("SVG paths", () => {
  it("buildEquityPath emits a finite path for a normal series", () => {
    const points = [
      { ts: "a", equity: 100, drawdown_pct: 0 },
      { ts: "b", equity: 200, drawdown_pct: 1 },
      { ts: "c", equity: 150, drawdown_pct: 2 },
    ];
    const d = buildEquityPath(points, 300, 100);
    expect(d).not.toBe("");
    expect(d).not.toMatch(/NaN|Infinity/);
  });

  it("returns empty string for empty points", () => {
    expect(buildEquityPath([], 300, 100)).toBe("");
  });

  it("handles single-point equity with a finite fallback line", () => {
    const d = buildEquityPath([{ ts: "a", equity: 55, drawdown_pct: 0 }], 300, 100);
    expect(d).not.toBe("");
    expect(d).not.toMatch(/NaN|Infinity/);
  });

  it("handles constant (flat) equity without NaN/Infinity", () => {
    const points = [1, 1, 1, 1].map((equity, i) => ({
      ts: String(i),
      equity,
      drawdown_pct: 0,
    }));
    const d = buildEquityPath(points, 300, 100);
    expect(d).not.toMatch(/NaN|Infinity/);
  });

  it("handles malformed values safely", () => {
    const points = [
      { ts: "a", equity: NaN, drawdown_pct: 0 },
      { ts: "b", equity: 100, drawdown_pct: 1 },
    ];
    const d = buildEquityPath(points, 300, 100);
    expect(d).not.toMatch(/NaN|Infinity/);
  });

  it("buildDrawdownPath works and avoids NaN/Infinity", () => {
    const points = [
      { ts: "a", equity: 100, drawdown_pct: 0 },
      { ts: "b", equity: 90, drawdown_pct: 10 },
    ];
    const d = buildDrawdownPath(points, 300, 100);
    expect(d).not.toBe("");
    expect(d).not.toMatch(/NaN|Infinity/);
  });

  it("buildPath guards non-finite dimensions", () => {
    const d = buildPath([1, 2, 3], NaN, Infinity);
    expect(d).not.toMatch(/NaN|Infinity/);
  });
});

describe("comparison normalization", () => {
  const run = (id: string, equities: number[]) => ({
    id,
    label: `run-${id}`,
    equity: equities.map((equity, i) => ({ ts: String(i), equity, drawdown_pct: 0 })),
  });

  it("normalizes at least two runs to percentage-change series", () => {
    const result = normalizeForComparison([run("a", [100, 110, 121]), run("b", [200, 220, 242])]);
    expect(result.length).toBe(2);
    expect(result[0].points[0].y).toBeCloseTo(0);
    expect(result[0].points[2].y).toBeCloseTo(21);
    expect(result[1].points[2].y).toBeCloseTo(21);
    expect(result[0].points[0].x).toBe(0);
    expect(result[0].points[2].x).toBe(100);
  });

  it("handles different curve lengths", () => {
    const result = normalizeForComparison([run("a", [100, 110]), run("b", [100, 105, 110, 120])]);
    expect(result.length).toBe(2);
    expect(result[0].points.length).toBe(2);
    expect(result[1].points.length).toBe(4);
    expect(result[0].points[1].x).toBe(100);
    expect(result[1].points[1].x).toBeCloseTo(33.33, 1);
  });

  it("returns empty for fewer than two runs", () => {
    expect(normalizeForComparison([run("a", [100])])).toEqual([]);
    expect(normalizeForComparison([])).toEqual([]);
  });

  it("handles empty and one-point runs gracefully", () => {
    const result = normalizeForComparison([run("a", []), run("b", [100, 110])]);
    expect(result.length).toBe(0);
    const result2 = normalizeForComparison([run("a", [100]), run("b", [100, 110])]);
    expect(result2.length).toBe(2);
    expect(result2[0].points[0].x).toBe(0);
  });

  it("handles constant equity (zero return) without division blow-ups", () => {
    const result = normalizeForComparison([run("a", [100, 100, 100]), run("b", [100, 101])]);
    expect(result.length).toBe(2);
    expect(result[0].points.every((p) => Number.isFinite(p.y))).toBe(true);
  });

  it("handles malformed values by skipping invalid bases", () => {
    const runA = { id: "a", label: "a", equity: [{ ts: "0", equity: NaN, drawdown_pct: 0 }, { ts: "1", equity: 100, drawdown_pct: 0 }] };
    const runB = run("b", [100, 110]);
    const result = normalizeForComparison([runA, runB]);
    expect(result.length).toBe(0);
  });

  it("normalizes negative equity deltas", () => {
    const result = normalizeForComparison([run("a", [100, 90]), run("b", [100, 110])]);
    expect(result[0].points[1].y).toBeCloseTo(-10);
  });
});

describe("mocked REST fetchers", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  function jsonResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fetchBacktests normalizes an array of runs", async () => {
    fetchMock.mockResolvedValue(jsonResponse([baseRun]));
    const runs = await fetchBacktests(20);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/backtests?limit=20",
      expect.objectContaining({ method: "GET" }),
    );
    expect(runs.length).toBe(1);
    expect(runs[0].status).toBe("COMPLETED");
  });

  it("fetchBacktestDetail normalizes a single run", async () => {
    fetchMock.mockResolvedValue(jsonResponse(baseRun));
    const run = await fetchBacktestDetail(baseRun.id);
    expect(run.seed).toBe(42);
  });

  it("fetchBacktestTrades normalizes an array", async () => {
    fetchMock.mockResolvedValue(jsonResponse([{ id: "t1", symbol: "EURUSD", side: "LONG", net_pnl: 5 }]));
    const trades = await fetchBacktestTrades(baseRun.id);
    expect(trades[0].side).toBe("LONG");
    expect(trades[0].net_pnl).toBe(5);
  });

  it("fetchBacktestEquity normalizes an array", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse([{ ts: "2026-01-01T00:00:00Z", equity: 100, drawdown_pct: 0 }]),
    );
    const points = await fetchBacktestEquity(baseRun.id);
    expect(points[0].equity).toBe(100);
  });

  it("propagates ApiError from an unsuccessful response", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "not found" }, 401));
    await expect(fetchBacktestDetail(baseRun.id)).rejects.toMatchObject({
      status: 401,
      name: "ApiError",
    });
  });

  it("treats empty array responses as empty lists", async () => {
    fetchMock.mockImplementation(async () => jsonResponse([]));
    expect(await fetchBacktests()).toEqual([]);
    expect(await fetchBacktestTrades("x")).toEqual([]);
    expect(await fetchBacktestEquity("x")).toEqual([]);
  });
});
