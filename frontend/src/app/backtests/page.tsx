"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, authFetch, clearSession, getStoredUser, getToken } from "@/lib/auth";
import {
  buildDrawdownPath,
  buildEquityPath,
  buildPath,
  deriveRunRange,
  fetchBacktestDetail,
  fetchBacktestEquity,
  fetchBacktests,
  fetchBacktestTrades,
  fmtCurrency,
  fmtDateTime,
  fmtNumber,
  fmtPct,
  normalizeForComparison,
  type BacktestRun,
  type BacktestTrade,
  type EquityPoint,
} from "@/lib/backtests";
import { fetchServerMode, isSafeMode } from "@/lib/system";

const CHART_W = 640;
const CHART_H = 180;

const STATUS_STYLES: Record<string, string> = {
  COMPLETED: "border-emerald-800/60 bg-emerald-950/40 text-emerald-300",
  RUNNING: "border-sky-800/60 bg-sky-950/40 text-sky-300",
  FAILED: "border-red-800/60 bg-red-950/40 text-red-300",
};

function statusPill(status: string): string {
  return STATUS_STYLES[status] ?? STATUS_STYLES.FAILED;
}

function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

export default function BacktestsPage() {
  const router = useRouter();

  const [mode, setMode] = useState<string | null>(null);
  const [user] = useState(() => getStoredUser());

  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [selected, setSelected] = useState<BacktestRun | null>(null);
  const [trades, setTrades] = useState<BacktestTrade[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [compareCurves, setCompareCurves] = useState<
    Array<{ id: string; label: string; equity: EquityPoint[] }>
  >([]);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);

  const [loadingList, setLoadingList] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [tradesError, setTradesError] = useState<string | null>(null);
  const [equityError, setEquityError] = useState<string | null>(null);

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
        const loaded = await fetchBacktests(50);
        setRuns(loaded);
      } catch (err) {
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          goToLogin();
          return;
        }
        setListError(err instanceof ApiError ? err.message : "Could not load backtest runs.");
      } finally {
        setLoadingList(false);
      }
    })();

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openRun = useCallback(async (id: string) => {
    setSelected(null);
    setTrades([]);
    setEquity([]);
    setDetailError(null);
    setTradesError(null);
    setEquityError(null);
    setLoadingDetail(true);
    try {
      const [run, tradeRows, equityRows] = await Promise.all([
        fetchBacktestDetail(id),
        fetchBacktestTrades(id),
        fetchBacktestEquity(id),
      ]);
      setSelected(run);
      setTrades(tradeRows);
      setEquity(equityRows);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        goToLogin();
        return;
      }
      setDetailError(err instanceof ApiError ? err.message : "Could not load backtest details.");
    } finally {
      setLoadingDetail(false);
    }
  }, [goToLogin]);

  const handleToggleCompare = (id: string) => {
    setCompareIds((prev) => {
      const next = prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id];
      if (next.length < 2) setCompareCurves([]);
      return next;
    });
  };

  useEffect(() => {
    if (compareIds.length < 2) return;
    let cancelled = false;
    setCompareLoading(true);
    setCompareError(null);
    (async () => {
      try {
        const futures = compareIds.map(async (id) => {
          const run = runs.find((r) => r.id === id);
          const equityRows = await fetchBacktestEquity(id);
          return { id, label: run ? shortId(run.id) : shortId(id), equity: equityRows };
        });
        const loaded = await Promise.all(futures);
        if (!cancelled) setCompareCurves(loaded);
      } catch (err) {
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          goToLogin();
          return;
        }
        if (!cancelled) {
          setCompareError(err instanceof ApiError ? err.message : "Could not load comparison data.");
        }
      } finally {
        if (!cancelled) setCompareLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compareIds]);

  const compareRuns = useMemo(
    () => normalizeForComparison(compareCurves, 2),
    [compareCurves],
  );

  const safe = isSafeMode(mode ?? undefined);
  const selectedRange = deriveRunRange(equity);

  const handleLogout = async () => {
    try {
      await authFetch("/api/v1/auth/logout", { method: "POST" });
    } catch {
      /* best-effort; session cleared regardless */
    }
    goToLogin();
  };

  const m = selected?.metrics;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-6xl flex-col p-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Backtests</h1>
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
            title="This page is observe-only — it reads backtest results and never initiates trades."
          >
            observe-only
          </span>

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

      {listError && (
        <p className="mb-4 rounded-lg border border-red-800/60 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          Runs: {listError}
        </p>
      )}
      {detailError && (
        <p className="mb-4 rounded-lg border border-red-800/60 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          Detail: {detailError}
        </p>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[300px_1fr]">
        {/* Run list */}
        <section>
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
            Runs
          </h2>
          {loadingList ? (
            <p className="text-sm text-slate-500">Loading runs…</p>
          ) : runs.length === 0 ? (
            <p className="text-sm text-slate-500">
              No backtest runs found. Run a backtest from the CLI to populate this list.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {runs.map((run) => (
                <li key={run.id}>
                  <button
                    type="button"
                    onClick={() => void openRun(run.id)}
                    className={`w-full rounded-xl border p-3 text-left transition-colors ${
                      selected?.id === run.id
                        ? "border-slate-500 bg-slate-800/70"
                        : "border-slate-800 bg-slate-900/60 hover:border-slate-600"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-xs text-slate-300">{shortId(run.id)}</span>
                      <span
                        className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest ${statusPill(
                          run.status,
                        )}`}
                      >
                        {run.status}
                      </span>
                    </div>
                    <div className="mt-1 flex items-center justify-between gap-2 text-xs text-slate-400">
                      <span>{fmtDateTime(run.started_at)}</span>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleToggleCompare(run.id);
                        }}
                        className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold ${
                          compareIds.includes(run.id)
                            ? "border-sky-700 bg-sky-950/60 text-sky-300"
                            : "border-slate-700 text-slate-400 hover:text-slate-200"
                        }`}
                        title="Toggle for comparison"
                      >
                        compare
                      </button>
                    </div>
                    <div className="mt-1 text-xs">
                      <span className={run.metrics.net_pnl >= 0 ? "text-emerald-300" : "text-red-300"}>
                        {fmtCurrency(run.metrics.net_pnl)}
                      </span>
                      <span className="ml-2 text-slate-500">
                        {run.metrics.num_trades} trades · w{fmtPct(run.metrics.win_rate * 100, 0)}
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* Detail */}
        <section>
          {loadingDetail ? (
            <p className="text-sm text-slate-500">Loading run details…</p>
          ) : !selected ? (
            <p className="text-sm text-slate-500">Select a run to view its details.</p>
          ) : (
            <div className="flex flex-col gap-6">
              {/* Basic info */}
              <div className="flex flex-wrap items-center gap-3 text-sm">
                <span
                  className={`rounded-full border px-2 py-0.5 text-xs font-semibold uppercase tracking-widest ${statusPill(
                    selected.status,
                  )}`}
                >
                  {selected.status}
                </span>
                <span className="font-mono text-xs text-slate-400">{selected.id}</span>
                <span className="text-slate-400">seed {selected.seed}</span>
                <span className="text-xs text-slate-500">
                  started {fmtDateTime(selected.started_at)}
                </span>
                <span className="text-xs text-slate-500">
                  finished {fmtDateTime(selected.finished_at)}
                </span>
              </div>

              <div className="text-xs text-slate-400">
                Effective range:{" "}
                {selectedRange.start && selectedRange.end ? (
                  <span>
                    {fmtDateTime(selectedRange.start)} → {fmtDateTime(selectedRange.end)}
                  </span>
                ) : (
                  "Range unavailable"
                )}
              </div>

              {selected.status === "FAILED" && selected.error && (
                <p className="rounded-lg border border-red-800/60 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                  Error: {selected.error}
                </p>
              )}

              {/* Metrics */}
              <section>
                <h3 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
                  Metrics
                </h3>
                <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-slate-800 bg-slate-800 sm:grid-cols-4">
                  {[
                    { label: "Net PnL", value: fmtCurrency(m?.net_pnl ?? 0), tone: (m?.net_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-red-300" },
                    { label: "Gross PnL", value: fmtCurrency(m?.gross_pnl ?? 0) },
                    { label: "Total costs", value: fmtCurrency(m?.total_costs ?? 0) },
                    { label: "Trades", value: fmtNumber(m?.num_trades ?? 0, 0) },
                    { label: "Win rate", value: fmtPct((m?.win_rate ?? 0) * 100) },
                    { label: "Profit factor", value: fmtNumber(m?.profit_factor ?? 0, 2) },
                    { label: "Sharpe", value: fmtNumber(m?.sharpe ?? 0, 2) },
                    { label: "Sortino", value: fmtNumber(m?.sortino ?? 0, 2) },
                    { label: "Max drawdown", value: fmtPct(m?.max_drawdown_pct ?? 0) },
                    { label: "Avg exposure", value: fmtPct(m?.exposure_avg_pct ?? 0) },
                    { label: "Bars", value: fmtNumber(m?.bars ?? 0, 0) },
                    { label: "Degraded runs", value: fmtNumber(m?.degraded_runs ?? 0, 0) },
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

              {/* Coverage */}
              <section>
                <h3 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
                  Coverage
                </h3>
                {!m || m.coverage.length === 0 ? (
                  <p className="text-sm text-slate-500">No coverage data.</p>
                ) : (
                  <div className="overflow-x-auto rounded-xl border border-slate-800">
                    <table className="w-full text-left text-xs">
                      <thead className="bg-slate-900/60 text-slate-400">
                        <tr>
                          <th className="px-3 py-2">Symbol</th>
                          <th className="px-3 py-2">TF</th>
                          <th className="px-3 py-2">Expected</th>
                          <th className="px-3 py-2">Technical</th>
                          <th className="px-3 py-2">Regime</th>
                          <th className="px-3 py-2">Fundamental</th>
                          <th className="px-3 py-2">Sentiment</th>
                          <th className="px-3 py-2">Status</th>
                        </tr>
                      </thead>
                      <tbody className="text-slate-300">
                        {m.coverage.map((c, i) => (
                          <tr key={`${c.symbol}-${c.timeframe}-${i}`} className="border-t border-slate-800">
                            <td className="px-3 py-2">{c.symbol}</td>
                            <td className="px-3 py-2">{c.timeframe}</td>
                            <td className="px-3 py-2">{fmtNumber(c.expected_bars, 0)}</td>
                            <td className="px-3 py-2">{fmtNumber(c.technical, 0)}</td>
                            <td className="px-3 py-2">{fmtNumber(c.regime, 0)}</td>
                            <td className="px-3 py-2">{fmtNumber(c.fundamental, 0)}</td>
                            <td className="px-3 py-2">{fmtNumber(c.sentiment, 0)}</td>
                            <td className="px-3 py-2">
                              {c.degraded ? (
                                <span className="font-semibold text-amber-300">degraded</span>
                              ) : (
                                <span className="text-slate-400">full</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              {/* Equity / drawdown */}
              <section>
                <h3 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
                  Equity
                </h3>
                {tradesError && (
                  <p className="mb-2 text-xs text-red-300">Trades: {tradesError}</p>
                )}
                {equityError && (
                  <p className="mb-2 text-xs text-red-300">Equity: {equityError}</p>
                )}
                {equity.length === 0 ? (
                  <p className="text-sm text-slate-500">No equity data for this run.</p>
                ) : (
                  <div className="flex flex-col gap-4">
                    <svg
                      viewBox={`0 0 ${CHART_W} ${CHART_H}`}
                      className="h-40 w-full rounded-xl border border-slate-800 bg-slate-900/40"
                      preserveAspectRatio="none"
                      role="img"
                      aria-label="Equity curve"
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
                      aria-label="Drawdown curve"
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

              {/* Trades */}
              <section>
                <h3 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
                  Trades
                </h3>
                {trades.length === 0 ? (
                  <p className="text-sm text-slate-500">No trades for this run.</p>
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
                          <th className="px-3 py-2">Exit</th>
                          <th className="px-3 py-2">Net PnL</th>
                          <th className="px-3 py-2">Exit</th>
                        </tr>
                      </thead>
                      <tbody className="text-slate-300">
                        {trades.map((t) => (
                          <tr key={t.id} className="border-t border-slate-800">
                            <td className="px-3 py-2">{t.symbol}</td>
                            <td className="px-3 py-2">{t.timeframe}</td>
                            <td className={`px-3 py-2 font-semibold ${t.side === "LONG" ? "text-emerald-300" : "text-red-300"}`}>
                              {t.side}
                            </td>
                            <td className="px-3 py-2">{fmtNumber(t.units, 0)}</td>
                            <td className="px-3 py-2">
                              <div>{fmtNumber(t.entry_price, 5)}</div>
                              <div className="text-[10px] text-slate-600">{fmtDateTime(t.entry_ts)}</div>
                            </td>
                            <td className="px-3 py-2">
                              <div>{fmtNumber(t.exit_price, 5)}</div>
                              <div className="text-[10px] text-slate-600">{fmtDateTime(t.exit_ts)}</div>
                            </td>
                            <td className={`px-3 py-2 font-semibold ${t.net_pnl >= 0 ? "text-emerald-300" : "text-red-300"}`}>
                              {fmtCurrency(t.net_pnl)}
                            </td>
                            <td className="px-3 py-2">{t.exit_reason}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </div>
          )}
        </section>
      </div>

      {/* Comparison */}
      <section className="mt-8">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
          Compare runs
        </h2>
        {compareIds.length < 2 ? (
          <p className="text-sm text-slate-500">
            Toggle <span className="font-semibold text-slate-300">compare</span> on{" "}
            {compareIds.length === 0 ? "at least two" : "one more"} run to compare normalized
            equity curves.
          </p>
        ) : compareError ? (
          <p className="rounded-lg border border-red-800/60 bg-red-950/40 px-3 py-2 text-sm text-red-300">
            Compare: {compareError}
          </p>
        ) : compareLoading ? (
          <p className="text-sm text-slate-500">Loading comparison…</p>
        ) : compareRuns.length < 2 ? (
          <p className="text-sm text-slate-500">
            The selected runs do not contain enough equity data to compare.
          </p>
        ) : (
          <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
            <p className="mb-2 text-xs text-slate-500">
              Normalized to each run&apos;s own starting value (%); axes are index positions, not
              shared timestamps.
            </p>
            <svg
              viewBox={`0 0 ${CHART_W} ${CHART_H}`}
              className="h-48 w-full"
              preserveAspectRatio="none"
              role="img"
              aria-label="Normalized equity comparison"
            >
              {compareRuns.map((curve, i) => (
                <path
                  key={curve.id}
                  d={buildPath(
                    curve.points.map((p) => p.y),
                    CHART_W,
                    CHART_H,
                  )}
                  fill="none"
                  stroke={["#34d399", "#60a5fa", "#fbbf24", "#f472b6"][i % 4]}
                  strokeWidth={2}
                />
              ))}
            </svg>
            <div className="mt-2 flex flex-wrap gap-3">
              {compareRuns.map((curve, i) => (
                <span key={curve.id} className="flex items-center gap-1.5 text-xs text-slate-300">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: ["#34d399", "#60a5fa", "#fbbf24", "#f472b6"][i % 4] }}
                  />
                  {curve.label}
                </span>
              ))}
            </div>
          </div>
        )}
      </section>
    </main>
  );
}
