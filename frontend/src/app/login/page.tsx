"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { ApiError, authFetch, storeSession } from "@/lib/auth";
import type { AuthUser, LoginResult } from "@/lib/auth";

type Mode = "login" | "register";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const switchMode = (next: Mode) => {
    setMode(next);
    setError(null);
    setMessage(null);
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setMessage(null);
    setBusy(true);

    try {
      if (mode === "register") {
        const created = await authFetch<{ email: string }>("/api/v1/auth/register", {
          body: { email, password },
        });
        setMessage(`Account created for ${created.email}. You can now sign in.`);
        setMode("login");
        return;
      }

      const login = await authFetch<LoginResult>("/api/v1/auth/login", {
        form: { username: email, password },
      });
      const me = await authFetch<AuthUser>("/api/v1/auth/me", { method: "GET" });
      storeSession(login, me);
      router.replace("/alerts");
      router.refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Unexpected error. Please try again.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="w-full max-w-sm rounded-2xl border border-slate-800 bg-slate-900/60 p-8">
        <div className="mb-6 text-center">
          <h1 className="text-2xl font-bold tracking-tight">Forex AI System</h1>
          <p className="mt-1 text-sm text-slate-400">
            {mode === "login" ? "Sign in to view live alerts" : "Create an account"}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <label className="block">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-slate-400">
              Email
            </span>
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-slate-400">
              Password
            </span>
            <input
              type="password"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-500"
            />
          </label>

          {error && (
            <p className="rounded-lg border border-red-800 bg-red-950/50 px-3 py-2 text-sm text-red-300">
              {error}
            </p>
          )}
          {message && (
            <p className="rounded-lg border border-emerald-800 bg-emerald-950/50 px-3 py-2 text-sm text-emerald-300">
              {message}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-lg bg-slate-700 px-4 py-2 text-sm font-semibold text-slate-100 transition-colors hover:bg-slate-600 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy
              ? "Working…"
              : mode === "login"
                ? "Sign in"
                : "Create account"}
          </button>
        </form>

        <div className="mt-5 text-center text-sm">
          {mode === "login" ? (
            <button
              type="button"
              onClick={() => switchMode("register")}
              className="text-slate-400 underline-offset-2 hover:text-slate-200 hover:underline"
            >
              No account? Register
            </button>
          ) : (
            <button
              type="button"
              onClick={() => switchMode("login")}
              className="text-slate-400 underline-offset-2 hover:text-slate-200 hover:underline"
            >
              Already registered? Sign in
            </button>
          )}
        </div>

        <p className="mt-6 text-center text-xs text-slate-500">
          The first registered account is promoted to admin.
        </p>
      </div>
    </main>
  );
}
