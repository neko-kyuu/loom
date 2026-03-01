import type { CSSProperties } from "react";
import type { Profile } from "./profiles";

export function formatTime(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);

  const pad2 = (n: number) => String(n).padStart(2, "0");
  const hhmm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (d >= startOfToday) return hhmm;
  if (d >= startOfYesterday) return `昨日 ${hhmm}`;
  const ymd = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  return `${ymd} ${hhmm}`;
}

export function avatarLabel(name?: string | null) {
  const s = (name || "?").trim();
  return s ? s.slice(0, 1).toUpperCase() : "?";
}

export function nameStyleCss(profile: Profile | null): CSSProperties {
  if (!profile) return {};
  const font =
    profile.nameStyle.font === "serif"
      ? "ui-serif, Georgia, Cambria, \"Times New Roman\", Times, serif"
      : profile.nameStyle.font === "mono"
        ? "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace"
        : "ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial";

  if (profile.nameStyle.colorMode === "gradient") {
    return {
      fontFamily: font,
      backgroundImage: `linear-gradient(90deg, ${profile.nameStyle.gradientFrom}, ${profile.nameStyle.gradientTo})`,
      WebkitBackgroundClip: "text",
      color: "transparent"
    };
  }
  return { fontFamily: font, color: profile.nameStyle.solid };
}

