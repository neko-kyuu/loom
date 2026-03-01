import { useMemo } from "react";
import type { Actor } from "../../types";
import type { ProfilesState } from "../../lib/profiles";
import { defaultProfileForActor, getProfile } from "../../lib/profiles";
import { absoluteAssetUrl } from "../../lib/api";

function avatarLabel(name?: string | null) {
  const s = (name || "?").trim();
  return s ? s.slice(0, 1).toUpperCase() : "?";
}

export default function ProfileNavList(props: {
  actors: Actor[];
  profiles: ProfilesState;
  selectedId: string;
  onSelectId: (id: string) => void;
}) {
  const items = useMemo(() => {
    const user = props.actors.find((a) => a.kind === "user") || ({ kind: "user", id: "user", name: "You" } as Actor);
    const dm = props.actors.find((a) => a.kind === "dm") || ({ kind: "dm", id: "dm", name: "DM" } as Actor);
    const pcs = props.actors.filter((a) => a.kind === "pc" && a.id);
    return { user, dm, pcs };
  }, [props.actors]);

  return (
    <div className="profileNavList">
      <div className="navSubTitle">用户</div>
      {(() => {
        const id = "user";
        const p = getProfile(props.profiles, items.user) || defaultProfileForActor(items.user);
        const active = props.selectedId === id;
        return (
          <button key={id} className={`profileItem navProfileItem ${active ? "active" : ""}`} onClick={() => props.onSelectId(id)}>
            <div className="avatar mini">
              {p.avatarUrl ? (
                <img
                  src={absoluteAssetUrl(p.avatarUrl)}
                  alt={p.displayName}
                  onError={(e) => ((e.currentTarget as HTMLImageElement).style.display = "none")}
                />
              ) : (
                <span>{avatarLabel(p.displayName)}</span>
              )}
            </div>
            <div className="profileItemText">
              <div className="profileItemName">{p.displayName}</div>
            </div>
          </button>
        );
      })()}

      <div className="navSubTitle">系统</div>
      {(() => {
        const id = "dm";
        const p = getProfile(props.profiles, items.dm) || defaultProfileForActor(items.dm);
        const active = props.selectedId === id;
        return (
          <button key={id} className={`profileItem navProfileItem ${active ? "active" : ""}`} onClick={() => props.onSelectId(id)}>
            <div className="avatar mini">
              {p.avatarUrl ? (
                <img
                  src={absoluteAssetUrl(p.avatarUrl)}
                  alt={p.displayName}
                  onError={(e) => ((e.currentTarget as HTMLImageElement).style.display = "none")}
                />
              ) : (
                <span>{avatarLabel(p.displayName)}</span>
              )}
            </div>
            <div className="profileItemText">
              <div className="profileItemName">{p.displayName}</div>
            </div>
          </button>
        );
      })()}

      <div className="navSubTitle">PC</div>
      {items.pcs.map((a) => {
        const id = a.id || "";
        const p = getProfile(props.profiles, a) || defaultProfileForActor(a);
        const active = props.selectedId === id;
        return (
          <button key={id} className={`profileItem navProfileItem ${active ? "active" : ""}`} onClick={() => props.onSelectId(id)}>
            <div className="avatar mini">
              {p.avatarUrl ? (
                <img
                  src={absoluteAssetUrl(p.avatarUrl)}
                  alt={p.displayName}
                  onError={(e) => ((e.currentTarget as HTMLImageElement).style.display = "none")}
                />
              ) : (
                <span>{avatarLabel(p.displayName)}</span>
              )}
            </div>
            <div className="profileItemText">
              <div className="profileItemName">{p.displayName}</div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
