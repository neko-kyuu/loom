import type { PcActivityLogPage } from "../types";

const DEFAULT_HTTP_BASE = "http://localhost:8080";

export function httpBase() {
  const raw = (import.meta as any).env?.VITE_API_BASE as string | undefined;
  return raw && typeof raw === "string" ? raw : DEFAULT_HTTP_BASE;
}

export function wsUrl() {
  const raw = (import.meta as any).env?.VITE_WS_URL as string | undefined;
  return raw && typeof raw === "string" ? raw : "ws://localhost:8080/ws";
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers || {}) }
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status} ${res.statusText} ${text}`);
  }
  return (await res.json()) as T;
}

export async function getSettingsState(): Promise<{ appearance_state: any; profiles_state: any; channels_state: any }> {
  return await jsonFetch(`${httpBase()}/api/settings`);
}

export async function putAppearanceState(payload: any): Promise<void> {
  await jsonFetch(`${httpBase()}/api/settings/appearance`, { method: "PUT", body: JSON.stringify(payload) });
}

export async function putProfilesState(payload: any): Promise<void> {
  await jsonFetch(`${httpBase()}/api/settings/profiles`, { method: "PUT", body: JSON.stringify(payload) });
}

export async function putChannelsState(payload: any): Promise<void> {
  await jsonFetch(`${httpBase()}/api/settings/channels`, { method: "PUT", body: JSON.stringify(payload) });
}

export async function deleteForumThread(threadId: string): Promise<void> {
  await jsonFetch(`${httpBase()}/api/forum/threads/${encodeURIComponent(threadId)}`, { method: "DELETE" });
}

export async function uploadAssetDataUrl(dataUrl: string): Promise<{ id: string; url: string }> {
  return await jsonFetch(`${httpBase()}/api/assets`, { method: "POST", body: JSON.stringify({ data_url: dataUrl }) });
}

export async function getPcActivityLogs(opts?: {
  pcId?: string | null;
  cursor?: string | null;
  limit?: number;
}): Promise<PcActivityLogPage> {
  const q = new URLSearchParams();
  if (opts?.pcId) q.set("pc_id", opts.pcId);
  if (opts?.cursor) q.set("cursor", opts.cursor);
  if (opts?.limit) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return await jsonFetch(`${httpBase()}/api/pc-activity/logs${qs ? `?${qs}` : ""}`);
}

export function absoluteAssetUrl(pathOrUrl: string) {
  if (!pathOrUrl) return "";
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  if (pathOrUrl.startsWith("/")) return `${httpBase()}${pathOrUrl}`;
  return pathOrUrl;
}
