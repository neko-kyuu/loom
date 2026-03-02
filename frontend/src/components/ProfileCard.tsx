import { useMemo, useState, type CSSProperties } from "react";
import type { Actor } from "../types";
import type { ProfilesState } from "../lib/profiles";
import { chatDisplayName, getProfile } from "../lib/profiles";
import ProfileModal from "./ProfileModal";

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

  function sendDm() {
    if (!isPc) return;
    const t = dmText.trim();
    if (!t) return;
    props.onDmPc(pcId, t);
    setDmText("");
  }

  return (
    <div className="profileOverlay" role="presentation" onClick={props.onClose}>
      <ProfileModal
        className="anchored"
        ariaLabel="个人资料"
        profile={profile}
        title={title}
        subtitle={subtitle}
        style={cardStyle}
        onClick={(e) => {
          e.stopPropagation();
        }}
        dm={
          isPc
            ? {
                kind: "interactive",
                value: dmText,
                onChange: setDmText,
                placeholder: props.canDmPc ? `私信 ${subtitle}…` : "未连接",
                canSend: props.canDmPc,
                onSend: sendDm,
                hint: "提示：这会通过 DM 转发给 PC（等同于 direct）。"
              }
            : { kind: "none" }
        }
      />
    </div>
  );
}
