import { X } from "lucide-react";
import { useMemo, useState, type CSSProperties } from "react";
import type { Actor } from "../types";
import type { Profile, ProfilesState } from "../lib/profiles";
import { chatDisplayName, getProfile, statusDotColor } from "../lib/profiles";
import { absoluteAssetUrl } from "../lib/api";

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

export default function ProfileCard(props: {
  open: boolean;
  actor: Actor | null;
  anchor: { x: number; y: number } | null;
  profiles: ProfilesState;
  onClose: () => void;
  canDmPc: boolean;
  onDmPc: (pcId: string, content: string) => void;
}) {
  const [dmText, setDmText] = useState("");
  const profile = useMemo(() => (props.actor ? getProfile(props.profiles, props.actor) : null), [props.actor, props.profiles]);
  const title = props.actor ? (profile?.nickname || chatDisplayName(props.profiles, props.actor)) : "";
  const subtitle = props.actor ? (profile?.displayName || chatDisplayName(props.profiles, props.actor)) : "";

  const isPc = props.actor?.kind === "pc" && Boolean(props.actor.id);
  const pcId = props.actor?.id || "";

  if (!props.open || !props.actor) return null;

  const dot = statusDotColor(profile);
  const cardStyle: CSSProperties = props.anchor
    ? {
        left: Math.max(12, Math.min(window.innerWidth - 320, props.anchor.x)),
        top: Math.max(12, Math.min(window.innerHeight - 520, props.anchor.y)),
        position: "fixed",
        color: profile?.panelTextColor || "var(--text)"
      }
    : {
        left: "50%",
        top: "50%",
        transform: "translate(-50%, -50%)",
        position: "fixed",
        color: profile?.panelTextColor || "var(--text)"
      };

  return (
    <div className="profileOverlay" role="presentation" onClick={props.onClose}>
      <div
        className="profileModal anchored"
        role="dialog"
        aria-label="个人资料"
        style={cardStyle}
        onClick={(e) => {
          e.stopPropagation();
        }}
      >
        <div className="profileHeader" style={{ backgroundColor: profile?.panelBgColor || "var(--panel2)" }}>
          {profile?.panelCoverUrl ? (
            <div className="profileCover" style={{ backgroundImage: `url(${absoluteAssetUrl(profile.panelCoverUrl)})` }} />
          ) : (
            <div className="profileCover placeholder" />
          )}
          <div className="profileAvatarWrap">
            <div className="profileAvatar">
              <div className="avatarClip" aria-hidden="true">
                {profile?.avatarUrl ? (
                  <img
                    src={absoluteAssetUrl(profile.avatarUrl)}
                    alt={subtitle}
                    onError={(e) => {
                      (e.currentTarget as HTMLImageElement).style.display = "none";
                    }}
                  />
                ) : (
                  <span>{avatarLabel(subtitle)}</span>
                )}
              </div>
              {dot ? <span className="statusDot" style={{ background: dot }} /> : null}
            </div>
          </div>
        </div>

        <div className="profileBody" style={{ backgroundColor: profile?.panelBgColor || "var(--panel)" }}>
          <div className="profileTopRow">
            <div className="profileNames">
              <div className="profileNick" style={nameStyleCss(profile)}>
                {subtitle}
              </div>
              <div className="profileName">{title}</div>
              {profile?.tags?.length ? (
                <div className="profileTagsBlock">
                  <div className="profileTagsTitle">身份组</div>
                  <div className="profileTags">
                    {profile.tags.map((t) => (
                      <span key={t} className="tagPill">
                        {t}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
            <button className="iconBtn iconOnly" onClick={props.onClose} aria-label="关闭" title="关闭">
              <X size={16} />
            </button>
          </div>

          {isPc ? (
            <div className="profileDm">
              <div className="profileDmLabel">私信</div>
              <div className="profileDmRow">
                <input
                  className="profileDmInput"
                  value={dmText}
                  placeholder={props.canDmPc ? `发给 ${subtitle}…` : "未连接"}
                  disabled={!props.canDmPc}
                  onChange={(e) => setDmText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      const t = dmText.trim();
                      if (!t) return;
                      props.onDmPc(pcId, t);
                      setDmText("");
                    }
                  }}
                />
                <button
                  className="primary"
                  disabled={!props.canDmPc || !dmText.trim()}
                  onClick={() => {
                    const t = dmText.trim();
                    if (!t) return;
                    props.onDmPc(pcId, t);
                    setDmText("");
                  }}
                >
                  发送
                </button>
              </div>
              <div className="profileDmHint">提示：这会通过 DM 转发给 PC（等同于 direct）。</div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
