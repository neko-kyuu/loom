import type { Actor } from "../types";

export type NameFont = "system" | "serif" | "mono";
export type NameColorMode = "solid" | "gradient";

export type MoodStatus = "none" | "green" | "yellow" | "red" | "gray" | "custom";

export type NameStyle = {
  font: NameFont;
  colorMode: NameColorMode;
  solid: string; // #rrggbb
  gradientFrom: string; // #rrggbb
  gradientTo: string; // #rrggbb
};

export type ProfileKind = "user" | "pc" | "dm";

export type Profile = {
  id: string; // actor id: user/dm/pc_*
  kind: ProfileKind;
  displayName: string; // chat name
  nickname: string; // profile title
  tags: string[]; // badge tags
  avatarUrl: string;
  panelCoverUrl: string;
  panelBgColor: string; // #rrggbb
  panelTextColor: string; // #rrggbb
  nameStyle: NameStyle;
  status: MoodStatus;
  statusColor: string; // used when status=custom
};

export type ProfilesState = {
  byId: Record<string, Profile>;
};

const STORAGE_PROFILES = "loom.profiles.v1";

const DEFAULT_STYLE: NameStyle = {
  font: "system",
  colorMode: "solid",
  solid: "#e9ecf1",
  gradientFrom: "#5865f2",
  gradientTo: "#22c55e"
};

const DEFAULT_PANEL_BG = "#151823";
const DEFAULT_PANEL_TEXT = "#e9ecf1";

function safeString(v: unknown, fallback: string) {
  return typeof v === "string" ? v : fallback;
}

function safeStringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x) => typeof x === "string").map((x) => x.trim()).filter(Boolean);
}

function safeProfileKind(v: unknown): ProfileKind | null {
  return v === "user" || v === "pc" || v === "dm" ? v : null;
}

function safeFont(v: unknown): NameFont {
  return v === "serif" || v === "mono" ? v : "system";
}

function safeColorMode(v: unknown): NameColorMode {
  return v === "gradient" ? "gradient" : "solid";
}

function safeStatus(v: unknown): MoodStatus {
  return v === "green" || v === "yellow" || v === "red" || v === "gray" || v === "custom" ? v : "none";
}

function parseProfile(raw: any): Profile | null {
  if (!raw || typeof raw !== "object") return null;
  const id = safeString(raw.id, "");
  const kind = safeProfileKind(raw.kind);
  if (!id || !kind) return null;
  const nameStyleRaw = raw.nameStyle || {};
  const nameStyle: NameStyle = {
    font: safeFont(nameStyleRaw.font),
    colorMode: safeColorMode(nameStyleRaw.colorMode),
    solid: safeString(nameStyleRaw.solid, DEFAULT_STYLE.solid),
    gradientFrom: safeString(nameStyleRaw.gradientFrom, DEFAULT_STYLE.gradientFrom),
    gradientTo: safeString(nameStyleRaw.gradientTo, DEFAULT_STYLE.gradientTo)
  };
  return {
    id,
    kind,
    displayName: safeString(raw.displayName, id),
    nickname: safeString(raw.nickname, safeString(raw.displayName, id)),
    tags: safeStringArray(raw.tags),
    avatarUrl: safeString(raw.avatarUrl, ""),
    panelCoverUrl: safeString(raw.panelCoverUrl, ""),
    panelBgColor: safeString(raw.panelBgColor, DEFAULT_PANEL_BG),
    panelTextColor: safeString(raw.panelTextColor, DEFAULT_PANEL_TEXT),
    nameStyle,
    status: safeStatus(raw.status),
    statusColor: safeString(raw.statusColor, "#22c55e")
  };
}

export function loadProfiles(): ProfilesState {
  try {
    const raw = localStorage.getItem(STORAGE_PROFILES);
    if (!raw) return { byId: {} };
    const parsed = JSON.parse(raw) as any;
    if (!parsed || typeof parsed !== "object") return { byId: {} };
    const arr = Array.isArray(parsed.profiles) ? parsed.profiles : [];
    const byId: Record<string, Profile> = {};
    for (const item of arr) {
      const p = parseProfile(item);
      if (p) byId[p.id] = p;
    }
    return { byId };
  } catch {
    return { byId: {} };
  }
}

export function persistProfiles(state: ProfilesState) {
  const profiles = Object.values(state.byId);
  localStorage.setItem(STORAGE_PROFILES, JSON.stringify({ profiles }));
}

export function serializeProfiles(state: ProfilesState): { profiles: Profile[] } {
  return { profiles: Object.values(state.byId) };
}

export function parseProfilesPayload(payload: any): ProfilesState {
  if (!payload || typeof payload !== "object") return { byId: {} };
  const arr = Array.isArray(payload.profiles) ? payload.profiles : [];
  const byId: Record<string, Profile> = {};
  for (const item of arr) {
    const p = parseProfile(item);
    if (p) byId[p.id] = p;
  }
  return { byId };
}

export function defaultProfileForActor(actor: Actor): Profile {
  const id = actor.kind === "pc" ? actor.id || "pc" : actor.kind === "dm" ? "dm" : "user";
  const baseName = actor.name || id;
  const kind: ProfileKind = actor.kind === "pc" ? "pc" : actor.kind === "dm" ? "dm" : "user";
  const style: NameStyle =
    kind === "pc"
      ? { ...DEFAULT_STYLE, colorMode: "gradient", solid: "#e9ecf1", gradientFrom: "#5865f2", gradientTo: "#22c55e" }
      : kind === "dm"
        ? { ...DEFAULT_STYLE, colorMode: "solid", solid: "#aab1c0" }
        : { ...DEFAULT_STYLE, colorMode: "solid", solid: "#e9ecf1" };

  return {
    id,
    kind,
    displayName: baseName,
    nickname: baseName,
    tags: [],
    avatarUrl: "",
    panelCoverUrl: "",
    panelBgColor: DEFAULT_PANEL_BG,
    panelTextColor: DEFAULT_PANEL_TEXT,
    nameStyle: style,
    status: kind === "pc" ? "green" : kind === "dm" ? "gray" : "green",
    statusColor: "#22c55e"
  };
}

export function ensureProfiles(state: ProfilesState, actors: Actor[]): ProfilesState {
  let changed = false;
  const byId = { ...state.byId };
  for (const a of actors) {
    const id = a.kind === "pc" ? a.id : a.kind === "dm" ? "dm" : "user";
    if (!id) continue;
    if (!byId[id]) {
      byId[id] = defaultProfileForActor({ ...a, id });
      changed = true;
    }
  }
  return changed ? { byId } : state;
}

export function actorId(actor: Actor): string {
  if (actor.kind === "pc") return actor.id || "";
  if (actor.kind === "dm") return "dm";
  return "user";
}

export function getProfile(state: ProfilesState, actor: Actor): Profile | null {
  const id = actorId(actor);
  if (!id) return null;
  return state.byId[id] || null;
}

export function chatDisplayName(state: ProfilesState, actor: Actor): string {
  return getProfile(state, actor)?.displayName || actor.name || actor.id || (actor.kind === "dm" ? "DM" : "Unknown");
}

export function statusDotColor(profile: Profile | null): string | null {
  if (!profile) return null;
  if (profile.status === "none") return null;
  if (profile.status === "custom") return profile.statusColor || "#22c55e";
  if (profile.status === "green") return "#22c55e";
  if (profile.status === "yellow") return "#eab308";
  if (profile.status === "red") return "#ef4444";
  return "#94a3b8";
}
