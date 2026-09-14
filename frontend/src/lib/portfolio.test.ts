import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  normalizeSummary,
  normalizePosition,
  normalizeOrder,
  normalizeEquityPoint,
  fetchPortfolioSummary,
  fetchPortfolioPositions,
  fetchPortfolioOrders,
  fetchPortfolioEquity,
} from "./portfolio";

const baseSummary = {
  as_of: "2026-01-01T00:00:00Z",
  cash: 99999.845,
  equity: 100200,
  open_pnl: 155,
  realized_pnl: 45,
  peak_equity: 100200,
  drawdown_pct: 0.0031,
  margin_used: 0,
  open_positions: 1,
  open_orders: 1,
};

const basePosition = {
  id: "11111111-1111-1111-1111-111111111111",
  order_id: "22222222-2222-2222-2222-222222222222",
  decision_id: "33333333-3333-3333-3333-333333333333",
  symbol: "EURUSD",
  timeframe: "H1",
  side: "LONG",
  units: 10000,
  entry_price: 1.085,
  entry_ts: "2026-01-01T00:00:00Z",
  stop_loss: 1.075,
  take_profit: 1.095,
  status: "OPEN",
  exit_price: null,
  exit_ts: null,
  exit_reason: null,
  gross_pnl: null,
  net_pnl: null,
};

const baseOrder = {
  id: "44444444-4444-4444-4444-444444444444",
  decision_id: null,
  symbol: "GBPUSD",
  timeframe: "H1",
  side: "SHORT",
  order_type: "NEXT_OPEN",
  status: "PENDING",
  units: 5000,
  requested_price: 1.27,
  filled_price: null,
  costs: 0,
  stop_loss: null,
  take_profit: null,
  filled_at: null,
  created_at: "2026-01-01T00:00:00Z",
};

describe("normalization", () => {
  it("normalizes a complete summary", () => {
    const summary = normalizeSummary(baseSummary);
    expect(summary.as_of).toBe("2026-01-01T00:00:00.000Z");
    expect(summary.equity).toBe(100200);
    expect(summary.cash).toBe(99999.845);
    expect(summary.open_positions).toBe(1);
    expect(summary.open_orders).toBe(1);
    expect(summary.drawdown_pct).toBeCloseTo(0.0031);
  });

  it("normalizes an empty/malformed summary to zeros", () => {
    const summary = normalizeSummary(null);
    expect(summary.as_of).toBeNull();
    expect(summary.equity).toBe(0);
    expect(summary.cash).toBe(0);
    expect(summary.open_positions).toBe(0);
    expect(summary.open_orders).toBe(0);
  });

  it("normalizes a complete position", () => {
    const pos = normalizePosition(basePosition);
    expect(pos.id).toBe(basePosition.id);
    expect(pos.decision_id).toBe(basePosition.decision_id);
    expect(pos.side).toBe("LONG");
    expect(pos.status).toBe("OPEN");
    expect(pos.entry_price).toBe(1.085);
    expect(pos.stop_loss).toBe(1.075);
    expect(pos.net_pnl).toBeNull();
  });

  it("defaults missing position fields safely", () => {
    const pos = normalizePosition({});
    expect(pos.side).toBe("LONG");
    expect(pos.status).toBe("OPEN");
    expect(pos.decision_id).toBeNull();
    expect(pos.entry_price).toBe(0);
    expect(pos.units).toBe(0);
    expect(pos.entry_ts).toBe("");
  });

  it("recognizes closed positions", () => {
    const pos = normalizePosition({ status: "CLOSED", side: "SHORT", net_pnl: 12.5 });
    expect(pos.status).toBe("CLOSED");
    expect(pos.side).toBe("SHORT");
    expect(pos.net_pnl).toBe(12.5);
  });

  it("normalizes a complete order", () => {
    const order = normalizeOrder(baseOrder);
    expect(order.id).toBe(baseOrder.id);
    expect(order.decision_id).toBeNull();
    expect(order.side).toBe("SHORT");
    expect(order.order_type).toBe("NEXT_OPEN");
    expect(order.status).toBe("PENDING");
    expect(order.requested_price).toBe(1.27);
    expect(order.filled_price).toBeNull();
    expect(order.filled_at).toBeNull();
  });

  it("normalizes filled and rejected order statuses", () => {
    expect(normalizeOrder({ status: "FILLED" }).status).toBe("FILLED");
    expect(normalizeOrder({ status: "REJECTED" }).status).toBe("REJECTED");
    expect(normalizeOrder({ status: "MIA" }).status).toBe("PENDING");
  });

  it("normalizes an equity point and defaults missing numbers", () => {
    const point = normalizeEquityPoint({
      ts: "2026-01-01T00:00:00Z",
      equity: 100200,
      drawdown_pct: 0.0031,
    });
    expect(point.ts).toBe("2026-01-01T00:00:00.000Z");
    expect(point.equity).toBe(100200);
    expect(point.cash).toBe(0);
    expect(point.drawdown_pct).toBeCloseTo(0.0031);
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

  it("fetchPortfolioSummary normalizes the response", async () => {
    fetchMock.mockResolvedValue(jsonResponse(baseSummary));
    const summary = await fetchPortfolioSummary();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/portfolio/summary",
      expect.objectContaining({ method: "GET" }),
    );
    expect(summary.equity).toBe(100200);
  });

  it("fetchPortfolioPositions serializes query params and normalizes rows", async () => {
    fetchMock.mockResolvedValue(jsonResponse([basePosition]));
    const positions = await fetchPortfolioPositions({ status: "OPEN", limit: 5 });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/portfolio/positions?status=OPEN&limit=5",
      expect.objectContaining({ method: "GET" }),
    );
    expect(positions.length).toBe(1);
    expect(positions[0].status).toBe("OPEN");
  });

  it("fetchPortfolioOrders omits the query string when empty", async () => {
    fetchMock.mockResolvedValue(jsonResponse([baseOrder]));
    const orders = await fetchPortfolioOrders();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/portfolio/orders",
      expect.objectContaining({ method: "GET" }),
    );
    expect(orders[0].order_type).toBe("NEXT_OPEN");
  });

  it("fetchPortfolioEquity normalizes an array", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse([{ ts: "2026-01-01T00:00:00Z", equity: 100, drawdown_pct: 0 }]),
    );
    const points = await fetchPortfolioEquity();
    expect(points[0].equity).toBe(100);
  });

  it("propagates ApiError from an unsuccessful response", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "unauthorized" }, 401));
    await expect(fetchPortfolioOrders()).rejects.toMatchObject({
      status: 401,
      name: "ApiError",
    });
  });

  it("treats empty array responses as empty lists", async () => {
    fetchMock.mockImplementation(async () => jsonResponse([]));
    expect(await fetchPortfolioPositions()).toEqual([]);
    expect(await fetchPortfolioOrders()).toEqual([]);
    expect(await fetchPortfolioEquity()).toEqual([]);
  });
});