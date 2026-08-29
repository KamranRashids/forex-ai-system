"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  AlertsSocket,
  type Alert,
  alertKey,
  fetchAlerts,
  mergeAlerts,
} from "@/lib/alerts";
import { ApiError, authFetch, clearSession, getStoredUser, getToken } from "@/lib/auth";
import { fetchServerMode, isSafeMode } from "@/lib/system";

type ConnState = "connecting" | "open" | "reconnecting" | "idle";

const SEVERITY_STYLES: Record<string, string> = {
  critical: "border-red-800/60 bg-red-950/40 text-red-300",
  warning: "border-amber-800/60 bg-amber-950/40 text-amber-300",
  info: "border-sky-800/60 bg-sky-950/40 text-sky-300",
};

const SEVERITY_DOT: Record<string, string> = {
  critical: "bg-red-500",
  warning: "bg-amber-500",
  info: "bg-sky-500",
};

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

export default function AlertsPage() {
  const router = useRouter();
  const socketRef = useRef<AlertsSocket | null>(null);

  const [mode, setMode] = useState<string | null>(null);
  const [history, setHistory] = useState<Alert[]>([]);
  const [live, setLive] = useState<Alert[]>([]);
  const [loading, setLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [connState, setConnState] = useState<ConnState>("idle");
  const [authError, setAuthError] = useState(false);

  const user = getStoredUser();

  const goToLogin = useCallback(() => {
    clearSession();
    router.replace("/login");
  }, [router]);

  useEffect(() => {
    if (authError) goToLogin();
  }, [authError, goToLogin]);

  const loadHistory = useCallback(async () => {
    setLoading(true);
    setHistoryError(null);
    try {
      const page = await fetchAlerts({ limit: 100 });
      setHistory(page.items);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setAuthError(true);
      } else {
        setHistoryError(
          err instanceof ApiError ? err.message : "Could not load alert history.",
        );
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Only mount the authenticated view when a session exists.
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
        /* mode badge unavailable; still render the feed */
      }
    })();

    void loadHistory();

    const socket = new AlertsSocket({
      onOpen: () => {
        setConnState("open");
        setStreamError(null);
      },
      onReconnect: () => {
        setConnState("reconnecting");
        void loadHistory();
      },
      onEvent: (alert) => {
        setStreamError(null);
        setLive((prev) => {
          const next = [alert, ...prev.filter((item) => alertKey(item) !== alertKey(alert))];
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

  const handleLogout = async () => {
    try {
      await authFetch("/api/v1/auth/logout", { method: "POST" });
    } catch {
      /* best effort; session is cleared client-side regardless */
    }
    goToLogin();
  };

  const rows = mergeAlerts(history, live);
  const safe = isSafeMode(mode ?? undefined);

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-6xl flex-col p-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Live Alerts</h1>
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

          <button
            type="button"
            onClick={handleLogout}
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-300 transition-colors hover:border-red-800 hover:text-red-300"
          >
            Sign out
          </button>
        </div>
      </header>

      {streamError && !loading && (
        <p className="mb-4 rounded-lg border border-amber-800/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-300">
          Stream: {streamError}
        </p>
      )}
      {historyError && (
        <p className="mb-4 rounded-lg border border-red-800/60 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          History: {historyError}
        </p>
      )}

      {loading && rows.length === 0 && (
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-slate-500">Loading alerts…</p>
        </div>
      )}

      {!loading && rows.length === 0 && (
        <div className="flex flex-1 flex-col items-center justify-center gap-2">
          <p className="text-sm text-slate-400">No alerts yet.</p>
          <p className="text-xs text-slate-600">
            Live alerts will appear here as soon as the system emits them.
          </p>
        </div>
      )}

      {rows.length > 0 && (
        <div className="flex flex-col gap-2">
          {rows.map((alert) => {
            const severity = alert.severity in SEVERITY_STYLES ? alert.severity : "info";
            const acked = Boolean(alert.acknowledged_at);
            return (
              <article
                key={alertKey(alert)}
                className={`rounded-xl border p-4 ${SEVERITY_STYLES[severity]}`}
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="flex items-start gap-3">
                    <span
                      className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${
                        SEVERITY_DOT[severity]
                      }`}
                    />
                    <div>
                      <h2 className="text-sm font-semibold">{alert.title}</h2>
                      {alert.message && (
                        <p className="mt-0.5 text-sm opacity-90">{alert.message}</p>
                      )}
                      <p className="mt-1 text-xs opacity-70">
                        {alert.event_type}
                        {alert.symbol ? ` · ${alert.symbol}` : ""}
                        {alert.timeframe ? ` · ${alert.timeframe}` : ""}
                      </p>
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1 text-xs">
                    <time className="opacity-70" dateTime={alert.occurred_at}>
                      {new Date(alert.occurred_at).toLocaleString()}
                    </time>
                    <span
                      className={
                        acked
                          ? "rounded-full border border-emerald-800/60 bg-emerald-950/40 px-2 py-0.5 text-emerald-300"
                          : "rounded-full border border-slate-700 bg-slate-800 px-2 py-0.5 text-slate-300"
                      }
                    >
                      {acked ? "acknowledged" : "new"}
                    </span>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </main>
  );
}
