import Link from "next/link";

const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function Home() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-8 p-8">
      <span className="rounded-full border border-amber-400/60 bg-amber-400/10 px-4 py-1.5 text-xs font-semibold uppercase tracking-widest text-amber-300">
        SAFE MODE — Paper trading only
      </span>

      <div className="text-center">
        <h1 className="text-4xl font-bold tracking-tight">Forex AI System</h1>
        <p className="mt-3 text-slate-400">Multi-agent Forex analysis platform</p>
      </div>

      <nav className="flex items-center gap-4">
        <Link
          href="/signals"
          className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-200 transition-colors hover:border-slate-500 hover:text-white"
        >
          Signals & Decisions
        </Link>
        <Link
          href="/alerts"
          className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-200 transition-colors hover:border-slate-500 hover:text-white"
        >
          Live Alerts
        </Link>
        <Link
          href="/login"
          className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-200 transition-colors hover:border-slate-500 hover:text-white"
        >
          Sign in
        </Link>
      </nav>

      <p className="max-w-xl text-center text-sm leading-relaxed text-slate-500">
        The live alerts dashboard shows system alerts in real time via an authenticated
        WebSocket stream. Live order execution does not exist anywhere in this system — the
        backend refuses to start in any mode other than{" "}
        <code className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-300">safe</code>.
      </p>

      <dl className="grid grid-cols-[auto_auto] gap-x-6 gap-y-2 rounded-xl border border-slate-800 bg-slate-900/60 p-6 text-sm">
        <dt className="font-medium text-slate-300">API</dt>
        <dd className="text-slate-400">{apiUrl}</dd>
        <dt className="font-medium text-slate-300">Status</dt>
        <dd className="text-slate-400">Phase 8 — authenticated alerts live view</dd>
        <dt className="font-medium text-slate-300">Trading mode</dt>
        <dd className="text-amber-300">paper / safe</dd>
      </dl>
    </main>
  );
}
