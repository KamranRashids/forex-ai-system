import { API_URL } from "./config";

export type HealthLive = {
  status: string;
  mode: string;
};

const SAFE_MODE = "safe";

export async function fetchServerMode(): Promise<HealthLive> {
  const response = await fetch(`${API_URL}/health/live`, {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(`Health check failed (${response.status})`);
  }
  const data = (await response.json()) as HealthLive;
  return data;
}

export function isSafeMode(mode: string | undefined): boolean {
  return mode === SAFE_MODE;
}
