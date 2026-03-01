export type PresetThemeId = "light" | "gray" | "darkgray" | "black" | "milktea" | "colorful";

export type CustomThemeColors = {
  bg: string;
  panel: string;
  text: string;
  accent: string;
};

export type CustomTheme = {
  id: string;
  name: string;
  colors: CustomThemeColors;
};

export type Appearance =
  | { mode: "preset"; preset: PresetThemeId }
  | { mode: "custom"; customId: string };

export type AppearanceState = { appearance: Appearance; customThemes: CustomTheme[] };

export const PRESET_OPTIONS: Array<{ id: PresetThemeId; label: string; swatches: string[] }> = [
  { id: "light", label: "明亮", swatches: ["#f7f7fb", "#ffffff", "#5865f2"] },
  { id: "gray", label: "灰", swatches: ["#f2f3f5", "#ffffff", "#99aab5"] },
  { id: "darkgray", label: "深灰（默认）", swatches: ["#0f1115", "#151823", "#5865f2"] },
  { id: "black", label: "黑", swatches: ["#05060a", "#0b0d14", "#7c3aed"] },
  { id: "milktea", label: "奶茶", swatches: ["#f3efe7", "#ffffff", "#b7791f"] },
  { id: "colorful", label: "多彩", swatches: ["#0b1220", "#111827", "#22c55e"] }
];

export const DEFAULT_CUSTOM: CustomThemeColors = {
  bg: "#0f1115",
  panel: "#151823",
  text: "#e9ecf1",
  accent: "#5865f2"
};

const STORAGE_CUSTOM_THEMES = "loom.customThemes.v2";
const STORAGE_APPEARANCE = "loom.appearance.v2";
const LEGACY_APPEARANCE = "loom.appearance";

function clamp01(n: number) {
  return Math.min(1, Math.max(0, n));
}

function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const s = hex.trim().replace(/^#/, "");
  if (s.length === 3) {
    const r = parseInt(s[0] + s[0], 16);
    const g = parseInt(s[1] + s[1], 16);
    const b = parseInt(s[2] + s[2], 16);
    if ([r, g, b].some((x) => Number.isNaN(x))) return null;
    return { r, g, b };
  }
  if (s.length === 6) {
    const r = parseInt(s.slice(0, 2), 16);
    const g = parseInt(s.slice(2, 4), 16);
    const b = parseInt(s.slice(4, 6), 16);
    if ([r, g, b].some((x) => Number.isNaN(x))) return null;
    return { r, g, b };
  }
  return null;
}

export function isHexColor(value: string) {
  return hexToRgb(value) !== null;
}

export function safeHex(value: string, fallback: string) {
  return isHexColor(value) ? value : fallback;
}

function mixHex(a: string, b: string, t: number) {
  const ra = hexToRgb(a);
  const rb = hexToRgb(b);
  if (!ra || !rb) return a;
  const tt = clamp01(t);
  const r = Math.round(ra.r + (rb.r - ra.r) * tt);
  const g = Math.round(ra.g + (rb.g - ra.g) * tt);
  const bb = Math.round(ra.b + (rb.b - ra.b) * tt);
  const toHex = (n: number) => n.toString(16).padStart(2, "0");
  return `#${toHex(r)}${toHex(g)}${toHex(bb)}`;
}

function rgbaFromHex(hex: string, alpha: number) {
  const rgb = hexToRgb(hex);
  if (!rgb) return `rgba(0,0,0,${clamp01(alpha)})`;
  return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${clamp01(alpha)})`;
}

function newId() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `t_${Date.now()}_${Math.random()}`;
}

function getNextCustomThemeName(themes: CustomTheme[]) {
  let max = 0;
  for (const t of themes) {
    const m = t.name.match(/(\d+)$/);
    if (m) max = Math.max(max, parseInt(m[1]!, 10));
  }
  return `自定义 ${max + 1}`;
}

function parseCustomThemes(raw: string | null): CustomTheme[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    const out: CustomTheme[] = [];
    for (const item of parsed) {
      if (!item || typeof item !== "object") continue;
      const obj = item as any;
      if (typeof obj.id !== "string" || typeof obj.name !== "string") continue;
      const colors = obj.colors as any;
      if (!colors || typeof colors !== "object") continue;
      const bg = typeof colors.bg === "string" ? colors.bg : DEFAULT_CUSTOM.bg;
      const panel = typeof colors.panel === "string" ? colors.panel : DEFAULT_CUSTOM.panel;
      const text = typeof colors.text === "string" ? colors.text : DEFAULT_CUSTOM.text;
      const accent = typeof colors.accent === "string" ? colors.accent : DEFAULT_CUSTOM.accent;
      out.push({ id: obj.id, name: obj.name, colors: { bg, panel, text, accent } });
    }
    return out;
  } catch {
    return [];
  }
}

function parseAppearance(raw: string | null): Appearance | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as any;
    if (parsed?.mode === "preset" && typeof parsed.preset === "string") {
      if (PRESET_OPTIONS.some((p) => p.id === parsed.preset)) return { mode: "preset", preset: parsed.preset };
    }
    if (parsed?.mode === "custom" && typeof parsed.customId === "string") {
      return { mode: "custom", customId: parsed.customId };
    }
    return null;
  } catch {
    return null;
  }
}

export function getInitialAppearanceState(): AppearanceState {
  const themes = parseCustomThemes(localStorage.getItem(STORAGE_CUSTOM_THEMES));
  const appearanceV2 = parseAppearance(localStorage.getItem(STORAGE_APPEARANCE));
  if (appearanceV2) {
    if (appearanceV2.mode === "custom" && !themes.some((t) => t.id === appearanceV2.customId)) {
      return { appearance: { mode: "preset", preset: "darkgray" }, customThemes: themes };
    }
    return { appearance: appearanceV2, customThemes: themes };
  }

  // Legacy migration from v1: {mode:"preset"} | {mode:"custom", colors:{...}}
  try {
    const legacyRaw = localStorage.getItem(LEGACY_APPEARANCE);
    if (legacyRaw) {
      const legacy = JSON.parse(legacyRaw) as any;
      if (legacy?.mode === "preset" && PRESET_OPTIONS.some((p) => p.id === legacy.preset)) {
        const next = { appearance: { mode: "preset", preset: legacy.preset } as Appearance, customThemes: themes };
        localStorage.setItem(STORAGE_APPEARANCE, JSON.stringify(next.appearance));
        return next;
      }
      if (legacy?.mode === "custom" && legacy.colors) {
        const colors = legacy.colors as Partial<CustomThemeColors>;
        const theme: CustomTheme = {
          id: newId(),
          name: getNextCustomThemeName(themes),
          colors: {
            bg: typeof colors.bg === "string" ? colors.bg : DEFAULT_CUSTOM.bg,
            panel: typeof colors.panel === "string" ? colors.panel : DEFAULT_CUSTOM.panel,
            text: typeof colors.text === "string" ? colors.text : DEFAULT_CUSTOM.text,
            accent: typeof colors.accent === "string" ? colors.accent : DEFAULT_CUSTOM.accent
          }
        };
        const nextThemes = [...themes, theme];
        const nextAppearance: Appearance = { mode: "custom", customId: theme.id };
        localStorage.setItem(STORAGE_CUSTOM_THEMES, JSON.stringify(nextThemes));
        localStorage.setItem(STORAGE_APPEARANCE, JSON.stringify(nextAppearance));
        return { appearance: nextAppearance, customThemes: nextThemes };
      }
    }
  } catch {
    // ignore
  }

  return { appearance: { mode: "preset", preset: "darkgray" }, customThemes: themes };
}

export function resolveCustomColors(appearance: Appearance, themes: CustomTheme[]): CustomThemeColors {
  if (appearance.mode !== "custom") return DEFAULT_CUSTOM;
  const theme = themes.find((t) => t.id === appearance.customId);
  return theme?.colors ?? DEFAULT_CUSTOM;
}

export function applyAppearance(appearance: Appearance, themes: CustomTheme[]) {
  const root = document.documentElement;
  if (appearance.mode === "preset") {
    root.dataset.theme = appearance.preset;
    root.style.removeProperty("--bg");
    root.style.removeProperty("--panel");
    root.style.removeProperty("--panel2");
    root.style.removeProperty("--text");
    root.style.removeProperty("--muted");
    root.style.removeProperty("--accent");
    root.style.removeProperty("--border");
    return;
  }

  root.dataset.theme = "custom";
  const c = resolveCustomColors(appearance, themes);
  const bg = safeHex(c.bg, DEFAULT_CUSTOM.bg);
  const panel = safeHex(c.panel, DEFAULT_CUSTOM.panel);
  const text = safeHex(c.text, DEFAULT_CUSTOM.text);
  const accent = safeHex(c.accent, DEFAULT_CUSTOM.accent);
  root.style.setProperty("--bg", bg);
  root.style.setProperty("--panel", panel);
  root.style.setProperty("--panel2", mixHex(panel, bg, 0.55));
  root.style.setProperty("--text", text);
  root.style.setProperty("--muted", mixHex(text, bg, 0.55));
  root.style.setProperty("--accent", accent);
  root.style.setProperty("--border", rgbaFromHex(text, 0.12));
}

export function persistAppearanceState(state: { appearance: Appearance; customThemes: CustomTheme[] }) {
  localStorage.setItem(STORAGE_CUSTOM_THEMES, JSON.stringify(state.customThemes));
  localStorage.setItem(STORAGE_APPEARANCE, JSON.stringify(state.appearance));
}

export function serializeAppearanceState(state: AppearanceState): AppearanceState {
  return { appearance: state.appearance, customThemes: state.customThemes };
}

export function parseAppearanceStatePayload(payload: any): AppearanceState | null {
  if (!payload || typeof payload !== "object") return null;
  const appearanceRaw = payload.appearance;
  const customThemesRaw = payload.customThemes;
  const appearance = (() => {
    if (!appearanceRaw || typeof appearanceRaw !== "object") return null;
    if (appearanceRaw.mode === "preset" && typeof appearanceRaw.preset === "string") {
      return PRESET_OPTIONS.some((p) => p.id === appearanceRaw.preset)
        ? ({ mode: "preset", preset: appearanceRaw.preset } as Appearance)
        : null;
    }
    if (appearanceRaw.mode === "custom" && typeof appearanceRaw.customId === "string") {
      return { mode: "custom", customId: appearanceRaw.customId } as Appearance;
    }
    return null;
  })();
  if (!appearance) return null;
  const themes = Array.isArray(customThemesRaw) ? customThemesRaw : [];
  const customThemes: CustomTheme[] = [];
  for (const t of themes) {
    if (!t || typeof t !== "object") continue;
    const id = typeof (t as any).id === "string" ? (t as any).id : null;
    const name = typeof (t as any).name === "string" ? (t as any).name : null;
    const colors = (t as any).colors;
    if (!id || !name || !colors || typeof colors !== "object") continue;
    const bg = typeof colors.bg === "string" ? colors.bg : DEFAULT_CUSTOM.bg;
    const panel = typeof colors.panel === "string" ? colors.panel : DEFAULT_CUSTOM.panel;
    const text = typeof colors.text === "string" ? colors.text : DEFAULT_CUSTOM.text;
    const accent = typeof colors.accent === "string" ? colors.accent : DEFAULT_CUSTOM.accent;
    customThemes.push({ id, name, colors: { bg, panel, text, accent } });
  }
  if (appearance.mode === "custom" && !customThemes.some((t) => t.id === appearance.customId)) {
    return { appearance: { mode: "preset", preset: "darkgray" }, customThemes };
  }
  return { appearance, customThemes };
}

export function createNewCustomTheme(existing: CustomTheme[], base?: CustomThemeColors): CustomTheme {
  return {
    id: newId(),
    name: getNextCustomThemeName(existing),
    colors: { ...(base ?? DEFAULT_CUSTOM) }
  };
}

export function duplicateCustomTheme(theme: CustomTheme): CustomTheme {
  return { id: newId(), name: `${theme.name} 复制`, colors: { ...theme.colors } };
}
