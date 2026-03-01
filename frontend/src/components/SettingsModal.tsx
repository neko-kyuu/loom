import type { ReactNode } from "react";

export type SettingsTabId = "appearance" | "profile" | "channels";

export default function SettingsModal(props: {
  open: boolean;
  tab: SettingsTabId;
  onTabChange: (tab: SettingsTabId) => void;
  onClose: () => void;
  profileNav?: ReactNode;
  children: ReactNode;
}) {
  if (!props.open) return null;
  return (
    <div className="modalOverlay" role="presentation" onClick={props.onClose}>
      <div
        className="modal"
        role="dialog"
        aria-label="设置"
        onClick={(e) => {
          e.stopPropagation();
        }}
      >
        <div className="modalNav">
          <div className="modalNavTitle">设置</div>
          <button
            className={`navItem ${props.tab === "channels" ? "active" : ""}`}
            onClick={() => props.onTabChange("channels")}
          >
            频道
          </button>
          <button
            className={`navItem ${props.tab === "appearance" ? "active" : ""}`}
            onClick={() => props.onTabChange("appearance")}
          >
            外观
          </button>
          <button
            className={`navItem ${props.tab === "profile" ? "active" : ""}`}
            onClick={() => props.onTabChange("profile")}
          >
            个人资料
          </button>
          {props.tab === "profile" && props.profileNav ? <div className="navExpand">{props.profileNav}</div> : null}
        </div>
        <div className="modalMain">{props.children}</div>
      </div>
    </div>
  );
}
