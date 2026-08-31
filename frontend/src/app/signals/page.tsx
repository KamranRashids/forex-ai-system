"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  SignalsSocket,
  computeStance,
  decisionKey,
  fetchDecisionHistory,
  fetchLatestDecision,
  fetchLatestSignals,
  fetchRiskEvaluation,
  fetchSignalHistory,
  mergeDecisions,
  mergeSignals,
  signalKey,
  type AgentSignal,
  type DecisionItem,
  type Direction,
  type RiskEvaluation,
  type Timeframe,
} from "@/lib/signals";
import { ApiError, authFetch, clearSession, getStoredUser, getToken } from "@/lib/auth";
import { fetchServerMode, isSafeMode } from "@/lib/system";

type ConnState = "connecting" | "open" | "reconnecting" | "idle";

const TIMEFRAMES: Timeframe[] = ["M5", "M15", "H1", "H4", "D1"];
const SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD"];

const CONN_LABEL: Record<ConnState, string> = {
  connecting: "connecting",
  open: "live",
  reconnecting: "reconnecting",
  idle: "idle",
};

const CONN_DOT: Record<ConnState, string> = {
  connecting: "bg-amber-400 animate-pulse",
  open: "bg-emerald-400",
  reconnecting: "bg-amber-400 animate-pulse",
  idle: "bg-slate-500",
};

const DIRECTION_STYLES: Record<Direction, string> = {
  LONG: "text-emerald-300",
  SHORT: "text-red-300",
  FLAT: "text-slate-400",
};

const AGENT_NAME: Record<string, string> = {
  technical: "Technical",
  regime: "Regime",
  fundamental: "Fundamental",
  sentiment: "Sentiment",
};

function agentDisplayName(id: string): string {
  return AGENT_NAME[id] ?? id;
}

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

export default function SignalsPage() {
  const router = useRouter();
  const socketRef = useRef<SignalsSocket | null>(null);

  const [mode, setMode] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("EURUSD");
  const [timeframe, setTimeframe] = useState<Timeframe>("H1");

  const [latestSignals, setLatestSignals] = useState<AgentSignal[]>([]);
  const [latestDecision, setLatestDecision] = useState<DecisionItem | null>(null);
  const [riskEval, setRiskEval] = useState<RiskEvaluation | null>(null);
  const [signalHistory, setSignalHistory] = useState<AgentSignal[]>([]);
  const [decisionHistory, setDecisionHistory] = useState<DecisionItem[]>([]);
  const [liveSignals, setLiveSignals] = useState<AgentSignal[]>([]);
  const [liveDecisions, setLiveDecisions] = useState<DecisionItem[]>([]);

  const [loading, setLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [connState, setConnState] = useState<ConnState>("idle");
  const [authError, setAuthError] = useState(false);

  const user = getStoredUser();
  const selection = useRef({ symbol, timeframe });
  selection.current = { symbol, timeframe };

  const goToLogin = useCallback(() => {
    clearSession();
    router.replace("/login");
  }, [router]);

  useEffect(() => {
    if (authError) goToLogin();
  }, [authError, goToLogin]);

  const loadHistory = useCallback(async (sym: string, tf: string) => {
    setLoading(true);
    setHistoryError(null);
    try {
      const [signals, decision, risk, sigHist, decHist] = await Promise.all([
        fetchLatestSignals(sym, tf),
        fetchLatestDecision(sym, tf),
        fetchRiskEvaluation(sym, tf),
        fetchSignalHistory(sym, tf, 10),
        fetchDecisionHistory(sym, tf, 10),
      ]);
      setLatestSignals(signals);
      setLatestDecision(decision);
      setRiskEval(risk);
      setSignalHistory(sigHist);
      setDecisionHistory(decHist);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setAuthError(true);
      } else {
        setHistoryError(
          err instanceof ApiError ? err.message : "Could not load signal/decision data.",
        );
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getToken()) {
      goToLogin();
      return;
    }

    setConnState("connecting");
    let cancelled = false;

    (async () => {
      try {
        const health = await fetchServerMode();
        if (!cancelled) setMode(health.mode);
      } catch {
        /* mode badge unavailable; still render the view */
      }
    })();

    void loadHistory(selection.current.symbol, selection.current.timeframe);

    const socket = new SignalsSocket({
      onOpen: () => {
        setConnState("open");
        setStreamError(null);
      },
      onReconnect: () => {
        setConnState("reconnecting");
        // Refetch durable history so live + REST stay consistent after resume.
        const sel = selection.current;
        void loadHistory(sel.symbol, sel.timeframe);
      },
      onSignal: (signal) => {
        setStreamError(null);
        const sel = selection.current;
        if (signal.symbol !== sel.symbol || signal.timeframe !== sel.timeframe) return;
        setLiveSignals((prev) => {
          const next = [signal, ...prev.filter((item) => signalKey(item) !== signalKey(signal))];
          return next.slice(0, 200);
        });
      },
      onDecision: (decision) => {
        setStreamError(null);
        const sel = selection.current;
        if (decision.symbol !== sel.symbol || decision.timeframe !== sel.timeframe) return;
        setLiveDecisions((prev) => {
          const next = [
            decision,
            ...prev.filter((item) => decisionKey(item) !== decisionKey(decision)),
          ];
          return next.slice(0, 200);
        });
      },
      onError: (reason) => setStreamError(reason),
      onAuthError: () => setAuthError(true),
      onClose: () => setConnState("idle"),
    });
    socketRef.current = socket;
    void socket.start();

    return () => {
      cancelled = true;
      socket.close();
      socketRef.current = null;
    };
  }, [goToLogin, loadHistory]);

  const handleSelectSymbol = (sym: string) => {
    setSymbol(sym);
    setLiveSignals([]);
    setLiveDecisions([]);
    void loadHistory(sym, timeframe);
  };

  const handleSelectTimeframe = (tf: Timeframe) => {
    setTimeframe(tf);
    setLiveSignals([]);
    setLiveDecisions([]);
    void loadHistory(symbol, tf);
  };

  const handleLogout = async () => {
    try {
      await authFetch("/api/v1/auth/logout", { method: "POST" });
    } catch {
      /* best effort; session is cleared client-side regardless */
    }
    goToLogin();
  };

  const mergedSignals = mergeSignals(signalHistory, liveSignals);
  const mergedDecisions = mergeDecisions(decisionHistory, liveDecisions);
  const stance = computeStance(latestSignals);
  const safe = isSafeMode(mode ?? undefined);
  const vetoed = latestDecision?.status === "BLOCKED";

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-6xl flex-col p-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Signals & Decisions</h1>
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
            {mode === null ? "mode: …" : safe ? "SAFE MODE — paper only" : `mode: ${mode}`}
          </span>

          <span
            className="inline-flex items-center gap-1.5 rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-xs font-semibold text-slate-300"
            title="WebSocket stream state"
          >
            <span className={`h-2 w-2 rounded-full ${CONN_DOT[connState]}`} />
            {CONN_LABEL[connState]}
          </span>

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

      <div className="mb-6 flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-slate-400">
          Pair
          <select
            value={symbol}
            onChange={(e) => handleSelectSymbol(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
          >
            {SYMBOLS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-400">
          Timeframe
          <select
            value={timeframe}
            onChange={(e) => handleSelectTimeframe(e.target.value as Timeframe)}
            className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
          >
            {TIMEFRAMES.map((tf) => (
              <option key={tf} value={tf}>
                {tf}
              </option>
            ))}
          </select>
        </label>

        <div className="ml-auto flex items-center gap-3 rounded-xl border border-slate-800 bg-slate-900/60 px-4 py-2 text-sm">
          <span className="text-slate-400">Stance</span>
          <span className="font-medium text-emerald-300">{stance.long} long</span>
          <span className="font-medium text-red-300">{stance.short} short</span>
          <span className="font-medium text-slate-400">{stance.flat} flat</span>
        </div>
      </div>

      {streamError && !loading && (
        <p className="mb-4 rounded-lg border border-amber-800/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-300">
          Stream: {streamError}
        </p>
      )}
      {historyError && (
        <p className="mb-4 rounded-lg border border-red-800/60 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          Data: {historyError}
        </p>
      )}

      {loading && (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-slate-500">Loading signals & decisions…</p>
        </div>
      )}

      {!loading && (
        <div className="flex flex-col gap-6">
          {/* Latest agent signals */}
          <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
              Latest agent signals
            </h2>
            {latestSignals.length === 0 ? (
              <p className="text-sm text-slate-500">No signals yet for this pair/timeframe.</p>
            ) : (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {latestSignals.map((signal) => (
                  <article
                    key={signalKey(signal)}
                    className="rounded-xl border border-slate-800 bg-slate-900/60 p-4"
                  >
                    <div className="flex items-center justify-between">
                      <h3 className="text-sm font-semibold">
                        {agentDisplayName(signal.agent_id)}
                      </h3>
                      <span
                        className={`text-xs font-bold uppercase tracking-widest ${
                          DIRECTION_STYLES[(signal.direction as Direction) ?? "FLAT"]
                        }`}
                      >
                        {signal.direction}
                      </span>
                    </div>
                    <div className="mt-2 text-xs text-slate-400">
                      confidence{" "}
                      <span className="font-semibold text-slate-200">
                        {(signal.confidence * 100).toFixed(0)}%
                      </span>
                      {signal.version ? ` · v${signal.version}` : ""}
                    </div>
                    {signal.rationale && (
                      <p className="mt-2 text-xs leading-relaxed text-slate-400">
                        {signal.rationale}
                      </p>
                    )}
                    <p className="mt-2 text-[11px] text-slate-600">
                      {fmtTime(signal.valid_until ?? signal.bucket_ts)}
                    </p>
                  </article>
                ))}
              </div>
            )}
          </section>

          {/* Latest decision */}
          <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
              Latest fused decision
            </h2>
            {!latestDecision ? (
              <p className="text-sm text-slate-500">No decision yet for this pair/timeframe.</p>
            ) : (
              <article
                className={`rounded-xl border p-4 ${
                  vetoed
                    ? "border-red-800/60 bg-red-950/40 text-red-200"
                    : "border-slate-800 bg-slate-900/60"
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-3">
                      <span
                        className={`text-lg font-bold uppercase tracking-widest ${
                          DIRECTION_STYLES[(latestDecision.direction as Direction) ?? "FLAT"]
                        }`}
                      >
                        {latestDecision.direction}
                      </span>
                      <span
                        className={`rounded-full border px-2 py-0.5 text-xs font-semibold uppercase tracking-widest ${
                          latestDecision.status === "PAPER"
                            ? "border-emerald-800/60 bg-emerald-950/40 text-emerald-300"
                            : latestDecision.status === "BLOCKED"
                              ? "border-red-800/60 bg-red-950/40 text-red-300"
                              : "border-sky-800/60 bg-sky-950/40 text-sky-300"
                        }`}
                      >
                        {latestDecision.status}
                      </span>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-4 text-xs text-slate-400">
                      <span>
                        confidence{" "}
                        <span className="font-semibold text-slate-200">
                          {(latestDecision.confidence ?? 0) * 100}%
                        </span>
                      </span>
                      <span>
                        agreement{" "}
                        <span className="font-semibold text-slate-200">
                          {(latestDecision.agreement ?? 0) * 100}%
                        </span>
                      </span>
                      {latestDecision.coverage !== undefined && (
                        <span>
                          coverage{" "}
                          <span className="font-semibold text-slate-200">
                            {(latestDecision.coverage ?? 0) * 100}%
                          </span>
                        </span>
                      )}
                    </div>
                    {latestDecision.rationale && (
                      <p className="mt-2 text-sm text-slate-300">{latestDecision.rationale}</p>
                    )}
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1 text-xs text-slate-500">
                    <time dateTime={latestDecision.bucket_ts}>{fmtTime(latestDecision.bucket_ts)}</time>
                    {latestDecision.run_id && <span>run {latestDecision.run_id}</span>}
                  </div>
                </div>
                {vetoed && (latestDecision.veto_code || latestDecision.veto_reason) && (
                  <div className="mt-3 rounded-lg border border-red-800/60 bg-red-950/60 px-3 py-2 text-xs text-red-300">
                    <span className="font-semibold">Risk brake: </span>
                    {latestDecision.veto_code}
                    {latestDecision.veto_reason ? ` — ${latestDecision.veto_reason}` : ""}
                  </div>
                )}
              </article>
            )}
          </section>

          {/* Latest risk evaluation */}
          <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
              Latest risk evaluation
            </h2>
            {!riskEval ? (
              <p className="text-sm text-slate-500">No risk evaluation yet for this pair/timeframe.</p>
            ) : (
              <article
                className={`rounded-xl border p-4 ${
                  riskEval.passed
                    ? "border-slate-800 bg-slate-900/60"
                    : "border-amber-800/60 bg-amber-950/40"
                }`}
              >
                <div className="flex flex-wrap gap-6 text-xs text-slate-400">
                  <span>
                    size{" "}
                    <span className="font-semibold text-slate-200">
                      {fmtNum(riskEval.position_size_units)}
                    </span>
                  </span>
                  <span>
                    price{" "}
                    <span className="font-semibold text-slate-200">{fmtNum(riskEval.price, 5)}</span>
                  </span>
                  <span>
                    stop{" "}
                    <span className="font-semibold text-slate-200">{fmtNum(riskEval.stop_loss, 5)}</span>
                  </span>
                  <span>
                    target{" "}
                    <span className="font-semibold text-slate-200">
                      {fmtNum(riskEval.take_profit, 5)}
                    </span>
                  </span>
                  <span>
                    R:R{" "}
                    <span className="font-semibold text-slate-200">{fmtNum(riskEval.rr_ratio)}</span>
                  </span>
                  <span>
                    risk{" "}
                    <span className="font-semibold text-slate-200">
                      {fmtNum(riskEval.risk_pct_account, 2)}
                    </span>
                  </span>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {[
                    { label: "exposure", ok: riskEval.exposure_ok },
                    { label: "correlation", ok: riskEval.correlation_ok },
                    { label: "daily loss", ok: riskEval.daily_loss_ok },
                    { label: "drawdown", ok: riskEval.drawdown_ok },
                  ].map((g) => (
                    <span
                      key={g.label}
                      className={`rounded-full border px-2 py-0.5 text-[11px] ${
                        g.ok
                          ? "border-emerald-800/60 bg-emerald-950/40 text-emerald-300"
                          : "border-red-800/60 bg-red-950/40 text-red-300"
                      }`}
                    >
                      {g.label}: {g.ok ? "ok" : "fail"}
                    </span>
                  ))}
                </div>
                {riskEval.reasons.length > 0 && (
                  <ul className="mt-3 space-y-1 text-xs text-slate-400">
                    {riskEval.reasons.map((r, i) => (
                      <li key={`${r.code}-${i}`} className="flex items-start gap-2">
                        <span className={r.ok ? "text-emerald-400" : "text-red-400"}>
                          {r.ok ? "✓" : "✗"}
                        </span>
                        <span className="flex-1">
                          <span className="font-semibold">{r.code}</span>
                          {r.detail ? ` — ${r.detail}` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </article>
            )}
          </section>

          {/* History tables */}
          <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <div>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
                Signal history
              </h2>
              {mergedSignals.length === 0 ? (
                <p className="text-sm text-slate-500">No signal history.</p>
              ) : (
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-500">
                      <th className="py-1.5 pr-2 font-medium">Agent</th>
                      <th className="py-1.5 pr-2 font-medium">Dir</th>
                      <th className="py-1.5 pr-2 font-medium">Conf</th>
                      <th className="py-1.5 font-medium">Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mergedSignals.map((signal) => (
                      <tr key={signalKey(signal)} className="border-b border-slate-800/60">
                        <td className="py-1.5 pr-2 text-slate-300">
                          {agentDisplayName(signal.agent_id)}
                        </td>
                        <td
                          className={`py-1.5 pr-2 font-semibold uppercase ${
                            DIRECTION_STYLES[(signal.direction as Direction) ?? "FLAT"]
                          }`}
                        >
                          {signal.direction}
                        </td>
                        <td className="py-1.5 pr-2 text-slate-400">
                          {(signal.confidence * 100).toFixed(0)}%
                        </td>
                        <td className="py-1.5 text-slate-500">{fmtTime(signal.bucket_ts)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-widest text-slate-400">
                Decision history
              </h2>
              {mergedDecisions.length === 0 ? (
                <p className="text-sm text-slate-500">No decision history.</p>
              ) : (
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-500">
                      <th className="py-1.5 pr-2 font-medium">Dir</th>
                      <th className="py-1.5 pr-2 font-medium">Status</th>
                      <th className="py-1.5 pr-2 font-medium">Conf</th>
                      <th className="py-1.5 font-medium">Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mergedDecisions.map((decision) => (
                      <tr key={decisionKey(decision)} className="border-b border-slate-800/60">
                        <td
                          className={`py-1.5 pr-2 font-semibold uppercase ${
                            DIRECTION_STYLES[(decision.direction as Direction) ?? "FLAT"]
                          }`}
                        >
                          {decision.direction}
                        </td>
                        <td className="py-1.5 pr-2 text-slate-300">{decision.status}</td>
                        <td className="py-1.5 pr-2 text-slate-400">
                          {(decision.confidence ?? 0) * 100}%
                        </td>
                        <td className="py-1.5 text-slate-500">{fmtTime(decision.bucket_ts)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
