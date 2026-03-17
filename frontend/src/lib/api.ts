import type {
  ForumThread,
  EventLogItem,
  LlmLogItem,
  LlmLogMeta,
  MemoryEntry,
  MemoryListResponse,
  PcActivityLogPage,
  TickLogPage
} from "../types";

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

export async function patchForumThread(
  threadId: string,
  patch: { pinned?: boolean; locked?: boolean }
): Promise<ForumThread> {
  const res = await jsonFetch<{ ok: true; thread: ForumThread }>(
    `${httpBase()}/api/forum/threads/${encodeURIComponent(threadId)}`,
    { method: "PATCH", body: JSON.stringify(patch) }
  );
  return res.thread;
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

export async function getTicks(opts?: {
  pcId?: string | null;
  cursor?: string | null;
  limit?: number;
}): Promise<TickLogPage> {
  const q = new URLSearchParams();
  if (opts?.pcId) q.set("pc_id", opts.pcId);
  if (opts?.cursor) q.set("cursor", opts.cursor);
  if (opts?.limit) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return await jsonFetch(`${httpBase()}/api/ticks${qs ? `?${qs}` : ""}`);
}

export async function getForumThreads(opts?: { channelId?: string | null; limit?: number }): Promise<{ items: ForumThread[] }> {
  const q = new URLSearchParams();
  if (opts?.channelId) q.set("channel_id", opts.channelId);
  if (opts?.limit) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return await jsonFetch(`${httpBase()}/api/forum/threads${qs ? `?${qs}` : ""}`);
}

export async function getLlmLogs(opts?: { limit?: number }): Promise<{ items: LlmLogMeta[] }> {
  const q = new URLSearchParams();
  if (opts?.limit) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return await jsonFetch(`${httpBase()}/api/llm-logs${qs ? `?${qs}` : ""}`);
}

export async function getLlmLog(logId: string): Promise<{ item: LlmLogItem }> {
  return await jsonFetch(`${httpBase()}/api/llm-logs/${encodeURIComponent(logId)}`);
}

export async function getEvents(opts?: { limit?: number }): Promise<{ items: EventLogItem[] }> {
  const q = new URLSearchParams();
  if (opts?.limit) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return await jsonFetch(`${httpBase()}/api/events${qs ? `?${qs}` : ""}`);
}

export async function getEvent(eventId: string): Promise<{ item: EventLogItem }> {
  return await jsonFetch(`${httpBase()}/api/events/${encodeURIComponent(eventId)}`);
}

export async function getMemories(opts?: {
  scope?: string | null;
  ownerPcId?: string | null;
  scopeId?: string | null;
  kind?: string | null;
  subjectId?: string | null;
  pinned?: boolean | null;
  deleted?: boolean | null;
  editState?: string | null;
  sourceType?: string | null;
  limit?: number;
}): Promise<MemoryListResponse> {
  const q = new URLSearchParams();
  if (opts?.scope) q.set("scope", opts.scope);
  if (opts?.ownerPcId) q.set("owner_pc_id", opts.ownerPcId);
  if (opts?.scopeId) q.set("scope_id", opts.scopeId);
  if (opts?.kind) q.set("kind", opts.kind);
  if (opts?.subjectId) q.set("subject_id", opts.subjectId);
  if (typeof opts?.pinned === "boolean") q.set("pinned", String(opts.pinned));
  if (typeof opts?.deleted === "boolean") q.set("deleted", String(opts.deleted));
  if (opts?.editState) q.set("edit_state", opts.editState);
  if (opts?.sourceType) q.set("source_type", opts.sourceType);
  if (opts?.limit) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return await jsonFetch(`${httpBase()}/api/memories${qs ? `?${qs}` : ""}`);
}

export async function patchMemory(memoryId: string, patch: Record<string, any>): Promise<MemoryEntry> {
  const res = await jsonFetch<{ ok: true; item: MemoryEntry }>(
    `${httpBase()}/api/memories/${encodeURIComponent(memoryId)}`,
    { method: "PATCH", body: JSON.stringify(patch) }
  );
  return res.item;
}

export async function createMemory(payload: {
  owner_pc_id: string;
  kind: "autobiography" | "secret";
  summary: string;
  content: string;
  importance?: number;
  pinned?: boolean;
}): Promise<MemoryEntry> {
  const res = await jsonFetch<{ ok: true; item: MemoryEntry }>(`${httpBase()}/api/memories`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
  return res.item;
}

export async function deleteMemory(memoryId: string): Promise<MemoryEntry> {
  const res = await jsonFetch<{ ok: true; item: MemoryEntry }>(
    `${httpBase()}/api/memories/${encodeURIComponent(memoryId)}`,
    { method: "DELETE" }
  );
  return res.item;
}

export function absoluteAssetUrl(pathOrUrl: string) {
  if (!pathOrUrl) return "";
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  if (pathOrUrl.startsWith("/")) return `${httpBase()}${pathOrUrl}`;
  return pathOrUrl;
}
