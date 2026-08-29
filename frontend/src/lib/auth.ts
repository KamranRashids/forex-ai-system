import { API_URL } from "./config";

const TOKEN_KEY = "forex.token";
const USER_KEY = "forex.user";

export type AuthUser = {
  id: string;
  email: string;
  role: string;
  is_active: boolean;
  created_at: string;
};

export type LoginResult = {
  access_token: string;
  token_type: string;
  expires_at: string;
};

const isBrowser = () => typeof window !== "undefined";

export function getToken(): string | null {
  if (!isBrowser()) return null;
  return window.sessionStorage.getItem(TOKEN_KEY);
}

export function getStoredUser(): AuthUser | null {
  if (!isBrowser()) return null;
  const raw = window.sessionStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

export function isAuthenticated(): boolean {
  return getToken() !== null;
}

export function storeSession(login: LoginResult, user: AuthUser): void {
  if (!isBrowser()) return;
  window.sessionStorage.setItem(TOKEN_KEY, login.access_token);
  window.sessionStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearSession(): void {
  if (!isBrowser()) return;
  window.sessionStorage.removeItem(TOKEN_KEY);
  window.sessionStorage.removeItem(USER_KEY);
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function parseJson<T>(response: Response): Promise<T> {
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(response.status, `Invalid JSON from ${response.url}`);
  }
}

type AuthFetchOptions = Omit<RequestInit, "body"> & {
  body?: BodyInit | Record<string, unknown> | null;
  form?: Record<string, string>;
};

export async function authFetch<T>(
  path: string,
  options: AuthFetchOptions = {},
): Promise<T> {
  const token = getToken();
  const headers = new Headers(options.headers);

  if (options.form) {
    const body = new URLSearchParams();
    for (const [key, value] of Object.entries(options.form)) body.set(key, value);
    headers.set("Content-Type", "application/x-www-form-urlencoded");
    const response = await fetch(`${API_URL}${path}`, {
      ...options,
      method: options.method ?? "POST",
      headers,
      body: body.toString(),
      credentials: "include",
    });
    return handleResponse<T>(response, path);
  }

  if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  if (token) headers.set("Authorization", `Bearer ${token}`);

  const body =
    options.body && typeof options.body === "object"
      ? JSON.stringify(options.body)
      : (options.body as BodyInit | null);

  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers,
    body,
    credentials: "include",
  });
  return handleResponse<T>(response, path);
}

async function handleResponse<T>(response: Response, path: string): Promise<T> {
  if (response.ok) return parseJson<T>(response);

  let detail = `Request failed (${response.status})`;
  try {
    const body = await parseJson<{ detail?: string }>(response);
    if (body?.detail) detail = String(body.detail);
  } catch {
    /* keep default */
  }

  if (response.status === 401 || response.status === 403) {
    clearSession();
  }

  const err = new ApiError(response.status, detail);
  err.status = response.status;
  void path;
  throw err;
}
