"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ApiError, authFetch, clearSession, getStoredUser, getToken } from "@/lib/auth";
import {
  buildDrawdownPath,
  buildEquityPath,
  fmtCurrency,
  fmtDateTime,
  fmtNumber,
  fmtPct,
} from "@/lib/backtests";
import {
  fetchPortfolioEquity,
  fetchPortfolioOrders,
  fetchPortfolioPositions,
  fetchPortfolioSummary,
  type PortfolioEquityPoint,
  type PortfolioOrder,
  type PortfolioPosition,
  type PortfolioSummary,
} from "@/lib/portfolio";
import { fetchServerMode, isSafeMode } from "@/lib/system";

const CHART_W = 640;
const CHART_H = 180;

const ORDER_STATUS_STYLES: Record<string, string> = {
  PENDING: "border-sky-800/60 bg-sky-950/40 text-sky-300",
  FILLED: "border-emerald-800/60 bg-emerald-950/40 text-emerald-300",
  CANCELLED: "border-slate-700 bg-slate-800 text-slate-400",
  REJECTED: "border-red-800/60 bg-red-950/40 text-red-300",
};

function orderStatusPill(status: string): string {
  return ORDER_STATUS_STYLES[status] ?? ORDER_STATUS_STYLES.PENDING;
}

export default function PortfolioPage() {
  const router = useRouter();

  const [mode, setMode] = useState<string | null>(null);
  const [user] = useState(() => getStoredUser());

  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [positions, setPositions] = useState<PortfolioPosition[]>([]);
  const [orders, setOrders] = useState<PortfolioOrder[]>([]);
  const [equity, setEquity] = useState<PortfolioEquityPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);

  const goToLogin = useCallback(() => {
    clearSession();
    router.replace("/login");
  }, [router]);

  useEffect(() => {
    if (!getToken()) {
      goToLogin();
      return;
    }

    (async () => {
      try {
        const health = await fetchServerMode();
        setMode(health.mode);
      } catch {
        /* mode badge unavailable; still render */
      }
    })();

    (async () => {
      try {
        const [summaryData, positionsData, ordersData, equityData] = await Promise.all([
          fetchPortfolioSummary(),
          fetchPortfolioPositions({ limit: 100 }),
          fetchPortfolioOrders({ limit: 50 }),
          fetchPortfolioEquity({ limit: 500 }),
        ]);
        setSummary(summaryData);
        setPositions(positionsData);
        setOrders(ordersData);
        setEquity(equityData);
      } catch (err) {
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          goToLogin();
          return;
        }
        setPageError(err instanceof ApiError ? err.message : "Could not load portfolio.");
      } finally {
        setLoading(false);
      }
    })();

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const safe = isSafeMode(mode ?? undefined);
  const empty = summary !== null && summary.equity === 0 && positions.length === 0 && orders.length === 0;

  const handleLogout = async () => {
    try {
      await authFetch("/api/v1/auth/logout", { method: "POST" });
    } catch {
      /* best-effort; session cleared regardless */
    }
    goToLogin();
  };

  const s = summary;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-6xl flex-col p-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Portfolio</h1>
          <p className="text-sm text-slate-400">
            {user ? `Signed in as ${user.email} (${user.role})` : "Signed in"}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <span
            className={
              safe
                ? "rounded-full border border-amber-400/60 bg-amber-400/10 px-3 py-1 text-xs font-semibold uppercase tracking-widest text-amber-300"
                : "rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-xs font-semibold uppercase tracking-widest text-slate-300"
            }
            title="Mode reported by the backend /health/live endpoint"
          >
            {mode === null
              ? "mode: …"
              : safe
                ? "SAFE MODE — paper only"
                : `mode: ${mode}`}
          </span>

          <span
            className="rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-xs font-semibold text-slate-400"
            title="This page is observe-only — it reads the paper ledger and never initiates trades."
          >
            observe-only
          </span>

          <a
            href="/backtests"
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-300 transition-colors hover:border-slate-500 hover:text-white"
          >
            Backtests
          </a>
          <a
            href="/signals"
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-300 transition-colors hover:border-slate-500 hover:text-white"
          >
            Signals
          </a>
          <a
            href="/alerts"
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-300 transition-colors hover:border-slate-500 hover:text-white"
          >
            Live Alerts
          </a>

          <button
            type="button"
            onClick={handleLogout}
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-300 transition-colors hover:border-red-800 hover:text-red-300"
          >
            Sign out
          </button>
        </div>
      </header>

      {pageError && (
        <p className="mb-4 rounded-lg border border-red-800/60 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          Portfolio: {pageError}
        </p>
      )}

      {loading ? (
        <p className="text-sm text-slate-500">Loading portfolio…</p>
      ) : empty ? (
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-6 text-sm text-slate-400">
          No paper activity yet — the SAFE MODE ledger is empty. Orders, positions and equity
          snapshots appear here once the paper executor (a later phase) begins trading.
        </div>
      ) : (
        <div className="flex flex-col gap-6">
          {/* Summary */}
          <section>
            <div className="mb-2 flex items-baseline justify-between gap-4">
              <h2 className="text-sm font-semibold uppercase tracking-widest text-slate-400">
                Account
              </h2>
              <span className="text-xs text-slate-500">
                as of {s ? fmtDateTime(s.as_of) : "—"}
              </span>
            </div>
            <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-slate-800 bg-slate-800 sm:grid-cols-4">
              {[
                { label: "Equity", value: fmtCurrency(s?.equity ?? 0) },
                { label: "Cash", value: fmtCurrency(s?.cash ?? 0) },
                {
                  label: "Open PnL",
                  value: fmtCurrency(s?.open_pnl ?? 0),
                  tone: (s?.open_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-red-300",
                },
                {
                  label: "Realized PnL",
                  value: fmtCurrency(s?.realized_pnl ?? 0),
                  tone: (s?.realized_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-red-300",
                },
                { label: "Peak equity", value: fmtCurrency(s?.peak_equity ?? 0) },
                { label: "Max drawdown", value: fmtPct((s?.drawdown_pct ?? 0) * 100) },
                { label: "Open positions", value: fmtNumber(s?.open_positions ?? 0, 0) },
                { label: "Open orders", value: fmtNumber(s?.open_orders ?? 0, 0) },
              ].map((cell) => (
                <div key={cell.label} className="bg-slate-900/60 px-3 py-2">
                  <div className="text-[10px] uppercase tracking-widest text-slate-500">
                    {cell.label}
                  </div>
                  <div className={`mt-0.5 text-sm font-semibold ${cell.tone ?? "text-slate-200"}`}>
                    {cell.value}
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* Equity / drawdown */}
          <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
              Equity
            </h2>
            {equity.length === 0 ? (
              <p className="text-sm text-slate-500">No equity history yet.</p>
            ) : (
              <div className="flex flex-col gap-4">
                <svg
                  viewBox={`0 0 ${CHART_W} ${CHART_H}`}
                  className="h-40 w-full rounded-xl border border-slate-800 bg-slate-900/40"
                  preserveAspectRatio="none"
                  role="img"
                  aria-label="Paper equity curve"
                >
                  <path
                    d={buildEquityPath(equity, CHART_W, CHART_H)}
                    fill="none"
                    stroke="#34d399"
                    strokeWidth={2}
                  />
                </svg>
                <svg
                  viewBox={`0 0 ${CHART_W} ${CHART_H}`}
                  className="h-40 w-full rounded-xl border border-slate-800 bg-slate-900/40"
                  preserveAspectRatio="none"
                  role="img"
                  aria-label="Paper drawdown curve"
                >
                  <path
                    d={buildDrawdownPath(equity, CHART_W, CHART_H)}
                    fill="none"
                    stroke="#f87171"
                    strokeWidth={2}
                  />
                </svg>
              </div>
            )}
          </section>

          {/* Positions */}
          <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
              Positions
            </h2>
            {positions.length === 0 ? (
              <p className="text-sm text-slate-500">No positions.</p>
            ) : (
              <div className="overflow-x-auto rounded-xl border border-slate-800">
                <table className="w-full text-left text-xs">
                  <thead className="bg-slate-900/60 text-slate-400">
                    <tr>
                      <th className="px-3 py-2">Symbol</th>
                      <th className="px-3 py-2">TF</th>
                      <th className="px-3 py-2">Side</th>
                      <th className="px-3 py-2">Units</th>
                      <th className="px-3 py-2">Entry</th>
                      <th className="px-3 py-2">Stop</th>
                      <th className="px-3 py-2">Target</th>
                      <th className="px-3 py-2">Status</th>
                      <th className="px-3 py-2">Net PnL</th>
                    </tr>
                  </thead>
                  <tbody className="text-slate-300">
                    {positions.map((p) => (
                      <tr
                        key={p.id}
                        className="border-t border-slate-800"
                        title={p.decision_id ? `decision ${p.decision_id}` : "no decision link"}
                      >
                        <td className="px-3 py-2">{p.symbol}</td>
                        <td className="px-3 py-2">{p.timeframe}</td>
                        <td className={`px-3 py-2 font-semibold ${p.side === "LONG" ? "text-emerald-300" : "text-red-300"}`}>
                          {p.side}
                        </td>
                        <td className="px-3 py-2">{fmtNumber(p.units, 0)}</td>
                        <td className="px-3 py-2">
                          <div>{fmtNumber(p.entry_price, 5)}</div>
                          <div className="text-[10px] text-slate-600">{fmtDateTime(p.entry_ts)}</div>
                        </td>
                        <td className="px-3 py-2">
                          {p.stop_loss === null ? "—" : fmtNumber(p.stop_loss, 5)}
                        </td>
                        <td className="px-3 py-2">
                          {p.take_profit === null ? "—" : fmtNumber(p.take_profit, 5)}
                        </td>
                        <td className="px-3 py-2">
                          <span
                            className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest ${
                              p.status === "OPEN"
                                ? "border-sky-800/60 bg-sky-950/40 text-sky-300"
                                : "border-slate-700 bg-slate-800 text-slate-400"
                            }`}
                          >
                            {p.status}
                          </span>
                        </td>
                        <td className={`px-3 py-2 font-semibold ${(p.net_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-red-300"}`}>
                          {p.net_pnl === null ? "—" : fmtCurrency(p.net_pnl)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Orders */}
          <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
              Orders
            </h2>
            {orders.length === 0 ? (
              <p className="text-sm text-slate-500">No orders.</p>
            ) : (
              <div className="overflow-x-auto rounded-xl border border-slate-800">
                <table className="w-full text-left text-xs">
                  <thead className="bg-slate-900/60 text-slate-400">
                    <tr>
                      <th className="px-3 py-2">Symbol</th>
                      <th className="px-3 py-2">TF</th>
                      <th className="px-3 py-2">Side</th>
                      <th className="px-3 py-2">Type</th>
                      <th className="px-3 py-2">Status</th>
                      <th className="px-3 py-2">Units</th>
                      <th className="px-3 py-2">Requested</th>
                      <th className="px-3 py-2">Filled</th>
                      <th className="px-3 py-2">Costs</th>
                      <th className="px-3 py-2">Created</th>
                    </tr>
                  </thead>
                  <tbody className="text-slate-300">
                    {orders.map((o) => (
                      <tr
                        key={o.id}
                        className="border-t border-slate-800"
                        title={o.decision_id ? `decision ${o.decision_id}` : "no decision link"}
                      >
                        <td className="px-3 py-2">{o.symbol}</td>
                        <td className="px-3 py-2">{o.timeframe}</td>
                        <td className={`px-3 py-2 font-semibold ${o.side === "LONG" ? "text-emerald-300" : "text-red-300"}`}>
                          {o.side}
                        </td>
                        <td className="px-3 py-2">{o.order_type}</td>
                        <td className="px-3 py-2">
                          <span
                            className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest ${orderStatusPill(
                              o.status,
                            )}`}
                          >
                            {o.status}
                          </span>
                        </td>
                        <td className="px-3 py-2">{fmtNumber(o.units, 0)}</td>
                        <td className="px-3 py-2">
                          {o.requested_price === null ? "—" : fmtNumber(o.requested_price, 5)}
                        </td>
                        <td className="px-3 py-2">
                          {o.filled_price === null ? "—" : fmtNumber(o.filled_price, 5)}
                        </td>
                        <td className="px-3 py-2">{fmtCurrency(o.costs)}</td>
                        <td className="px-3 py-2 text-[10px] text-slate-600">
                          {fmtDateTime(o.created_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      )}
    </main>
  );
}