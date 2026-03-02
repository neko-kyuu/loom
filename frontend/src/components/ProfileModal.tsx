import { Send } from "lucide-react";
import type { CSSProperties, KeyboardEvent, MouseEventHandler } from "react";
import { absoluteAssetUrl } from "../lib/api";
import type { Profile } from "../lib/profiles";
import { statusDotColor } from "../lib/profiles";

function nameStyleCss(p: Profile | null): CSSProperties {
  if (!p) return {};
  const font =
    p.nameStyle.font === "serif"
      ? "ui-serif, Georgia, Cambria, \"Times New Roman\", Times, serif"
      : p.nameStyle.font === "mono"
        ? "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace"
        : "ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial";

  if (p.nameStyle.colorMode === "gradient") {
    return {
      fontFamily: font,
      backgroundImage: `linear-gradient(90deg, ${p.nameStyle.gradientFrom}, ${p.nameStyle.gradientTo})`,
      WebkitBackgroundClip: "text",
      color: "transparent"
    };
  }
  return { fontFamily: font, color: p.nameStyle.solid };
}

function avatarLabel(name?: string | null) {
  const s = (name || "?").trim();
  return s ? s.slice(0, 1).toUpperCase() : "?";
}

export type ProfileModalDm =
  | { kind: "none" }
  | { kind: "preview"; placeholder: string }
  | {
      kind: "interactive";
      value: string;
      onChange: (next: string) => void;
      placeholder: string;
      canSend: boolean;
      onSend: () => void;
      hint?: string;
    };

export default function ProfileModal(props: {
  profile: Profile | null;
  title: string;
  subtitle: string;
  className?: string;
  style?: CSSProperties;
  ariaLabel?: string;
  onClick?: MouseEventHandler<HTMLDivElement>;
  dm?: ProfileModalDm;
}) {
  const rootClass = ["profileModal", props.className].filter(Boolean).join(" ");
  const dot = statusDotColor(props.profile);
  const dm = props.dm || { kind: "none" as const };

  const dmSendDisabled = dm.kind !== "interactive" ? true : !dm.canSend || !dm.value.trim();
  const rootStyle: CSSProperties = {
    ...props.style,
    ...({
      "--profileBorderColor": props.profile?.panelBgColor || "var(--panel)"
    } as unknown as CSSProperties)
  };

  return (
    <div
      className={rootClass}
      role={props.ariaLabel ? "dialog" : undefined}
      aria-label={props.ariaLabel}
      style={rootStyle}
      onClick={props.onClick}
    >
      <div className="profileHeader" style={{ backgroundColor: props.profile?.panelBgColor || "var(--panel2)" }}>
        {props.profile?.panelCoverUrl ? (
          <div
            className="profileCover"
            style={{
              backgroundImage: `url(${absoluteAssetUrl(props.profile.panelCoverUrl)})`,
              backgroundColor: props.profile.panelCoverColor || undefined
            }}
          />
        ) : props.profile?.panelCoverColor ? (
          <div className="profileCover" style={{ backgroundColor: props.profile.panelCoverColor }} />
        ) : (
          <div className="profileCover placeholder" />
        )}
        <div className="profileAvatarWrap">
          <div className="profileAvatar">
            <div className="avatarClip" aria-hidden="true">
              {props.profile?.avatarUrl ? (
                <img
                  src={absoluteAssetUrl(props.profile.avatarUrl)}
                  alt={props.subtitle}
                  onError={(e) => {
                    (e.currentTarget as HTMLImageElement).style.display = "none";
                  }}
                />
              ) : (
                <span>{avatarLabel(props.subtitle)}</span>
              )}
            </div>
            {dot ? <span className="statusDot profileStatusDot" style={{ background: dot }} /> : null}
          </div>
        </div>
      </div>

      <div className="profileBody" style={{ backgroundColor: props.profile?.panelBgColor || "var(--panel)" }}>
        <div className="profileTopRow">
          <div className="profileNames">
            <div className="profileNick" style={nameStyleCss(props.profile)}>
              {props.title}
            </div>
            <div className="profileName" style={{ color: props.profile?.panelTextColor || "var(--muted)" }}>
              {props.subtitle}
            </div>
            {props.profile?.tags?.length ? (
              <div className="profileTagsBlock">
                <div className="profileTags">
                  {props.profile.tags.map((t) => (
                    <span key={t} className="tagPill">
                      {t}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        </div>

        {dm.kind === "preview" ? (
          <div className="profileDm">
            <div className="profileDmRow">
              <input className="profileDmInput" value="" placeholder={dm.placeholder} readOnly />
              <button className="primary" disabled aria-label="发送" title="发送">
                <Send size={18} />
              </button>
            </div>
          </div>
        ) : dm.kind === "interactive" ? (
          <div className="profileDm">
            <div className="profileDmRow">
              <input
                className="profileDmInput"
                value={dm.value}
                placeholder={dm.placeholder}
                disabled={!dm.canSend}
                onChange={(e) => dm.onChange(e.target.value)}
                onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    if (dmSendDisabled) return;
                    dm.onSend();
                  }
                }}
              />
              <button
                className="primary"
                disabled={dmSendDisabled}
                onClick={() => {
                  if (dmSendDisabled) return;
                  dm.onSend();
                }}
                aria-label="发送"
                title="发送"
              >
                <Send size={18} />
              </button>
            </div>
            {dm.hint ? <div className="profileDmHint">{dm.hint}</div> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
