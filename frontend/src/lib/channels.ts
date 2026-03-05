export type ForumChannelConfig = {
  id: string;
  title: string; // "#trade"
  description: string;
  group?: string;
};

export type ChannelsState = {
  broadcast: { description: string; group?: string };
  forums: ForumChannelConfig[];
};

const STORAGE_CHANNELS = "loom.channels.v1";

function safeString(v: unknown, fallback: string) {
  return typeof v === "string" ? v : fallback;
}

function normalizeTitle(title: string) {
  const t = title.trim();
  if (!t) return "";
  return t.startsWith("#") ? t : `#${t}`;
}

function normalizeGroup(group: string) {
  return group.trim();
}

export function parseChannelsStatePayload(raw: any): ChannelsState | null {
  if (!raw || typeof raw !== "object") return null;
  const arr = Array.isArray(raw.forums) ? raw.forums : [];
  const broadcastRaw = (raw as any).broadcast;
  const broadcastGroup = broadcastRaw && typeof broadcastRaw === "object" ? normalizeGroup(safeString((broadcastRaw as any).group, "")) : "";
  const broadcast =
    broadcastRaw && typeof broadcastRaw === "object"
      ? {
          description: safeString((broadcastRaw as any).description, ""),
          group: broadcastGroup || undefined,
        }
      : { description: "", group: undefined };
  const seen = new Set<string>();
  const forums: ForumChannelConfig[] = [];
  for (const item of arr) {
    if (!item || typeof item !== "object") continue;
    const id = safeString((item as any).id, "").trim();
    const title = normalizeTitle(safeString((item as any).title, ""));
    const description = safeString((item as any).description, "");
    const group = normalizeGroup(safeString((item as any).group, ""));
    if (!id || !title) continue;
    if (seen.has(id)) continue;
    seen.add(id);
    forums.push({ id, title, description, group: group || undefined });
  }
  return { broadcast, forums };
}

export function loadChannelsState(): ChannelsState {
  try {
    const raw = localStorage.getItem(STORAGE_CHANNELS);
    if (!raw) return { broadcast: { description: "" }, forums: [] };
    const parsed = JSON.parse(raw) as any;
    const state = parseChannelsStatePayload(parsed);
    return state ?? { broadcast: { description: "" }, forums: [] };
  } catch {
    return { broadcast: { description: "" }, forums: [] };
  }
}

export function persistChannelsState(state: ChannelsState) {
  try {
    localStorage.setItem(STORAGE_CHANNELS, JSON.stringify(state));
  } catch {
    // ignore
  }
}

export function newForumChannelId() {
  const t = Date.now().toString(36);
  const r = Math.random().toString(36).slice(2, 6);
  return `forum_${t}_${r}`;
}

export function updateForumChannelTitle(state: ChannelsState, id: string, nextTitle: string): ChannelsState {
  const title = normalizeTitle(nextTitle);
  return {
    broadcast: state.broadcast,
    forums: state.forums.map((c) => (c.id === id ? { ...c, title } : c))
  };
}

export function updateForumChannelDescription(state: ChannelsState, id: string, nextDescription: string): ChannelsState {
  return {
    broadcast: state.broadcast,
    forums: state.forums.map((c) => (c.id === id ? { ...c, description: nextDescription } : c))
  };
}

export function updateForumChannelGroup(state: ChannelsState, id: string, nextGroup: string): ChannelsState {
  const group = normalizeGroup(nextGroup);
  return {
    broadcast: state.broadcast,
    forums: state.forums.map((c) => (c.id === id ? { ...c, group: group || undefined } : c))
  };
}

export function updateBroadcastDescription(state: ChannelsState, nextDescription: string): ChannelsState {
  return { broadcast: { ...state.broadcast, description: nextDescription }, forums: state.forums };
}

export function updateBroadcastGroup(state: ChannelsState, nextGroup: string): ChannelsState {
  const group = normalizeGroup(nextGroup);
  return { broadcast: { ...state.broadcast, group: group || undefined }, forums: state.forums };
}
