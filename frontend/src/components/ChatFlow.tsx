import { Send } from "lucide-react";
import type { Dispatch, ReactNode, RefObject, SetStateAction } from "react";
import type { Actor, Message } from "../types";
import type { ProfilesState } from "../lib/profiles";
import { chatDisplayName, getProfile } from "../lib/profiles";
import { absoluteAssetUrl } from "../lib/api";
import { avatarLabel, formatTime, nameStyleCss } from "../lib/chatUi";

export default function ChatFlow(props: {
  messages: Message[];
  profiles: ProfilesState;
  typingNames: string;
  endRef: RefObject<HTMLDivElement>;
  onOpenProfile: (actor: Actor, ev: { clientY: number; currentTarget: Element | null }) => void;
  composer?: {
    className?: string;
    value: string;
    onChange: (next: string) => void;
    placeholder: string;
    error: string | null;
    onClearError: () => void;
    canSend: boolean;
    onSend: () => void;
    pill: { isDirect: boolean; normalLabel: string; directSelectedCount: number };
    hint: ReactNode;
    directPicker?: {
      pcs: { id: string; name: string }[];
      selectedPcIds: string[];
      setSelectedPcIds: Dispatch<SetStateAction<string[]>>;
      setSelectionTouched: Dispatch<SetStateAction<boolean>>;
    };
  };
}) {
  const composer = props.composer;
  const directPicker = composer?.directPicker;
  const directEnabled = Boolean(directPicker && composer?.pill.isDirect);
  const composerClassName = composer?.className || "composer";
  const pillLabel = composer
    ? composer.pill.isDirect
      ? `direct${composer.pill.directSelectedCount ? `(${composer.pill.directSelectedCount})` : ""}`
      : composer.pill.normalLabel
    : "";

  return (
    <>
      <div className="messages">
        {props.messages.map((m) => {
          const name = chatDisplayName(props.profiles, m.from_actor);
          const prof = getProfile(props.profiles, m.from_actor);
          return (
            <div key={m.id} className="msgRow">
              <button className="avatarBtn" onClick={(e) => props.onOpenProfile(m.from_actor, e)} title="查看资料">
                <div className="avatar" title={name}>
                  {prof?.avatarUrl ? (
                    <img
                      src={absoluteAssetUrl(prof.avatarUrl)}
                      alt={name}
                      onError={(e) => ((e.currentTarget as HTMLImageElement).style.display = "none")}
                    />
                  ) : (
                    avatarLabel(name)
                  )}
                </div>
              </button>
              <div>
                <div className="msgHead">
                  <div className="name" style={nameStyleCss(prof)}>
                    {name}
                  </div>
                  <div className="time">{formatTime(m.timestamp)}</div>
                </div>
                <div className="content">{m.content}</div>
              </div>
            </div>
          );
        })}
        <div ref={props.endRef} />
      </div>

      {props.typingNames ? <div className="typing">… {props.typingNames} 正在输入…</div> : <div className="typing" />}

      {composer ? (
        <div className={composerClassName}>
          <div className="composerInner">
            <div className="composerMeta">
              <div className={`pill ${composer.pill.isDirect ? "direct" : ""}`}>{pillLabel}</div>
              <div className="hint">{composer.hint}</div>
            </div>

            {directEnabled && directPicker ? (
              <div className="directPicker">
                <div className="directPickerHead">
                  <div className="directPickerTitle">私聊对象</div>
                  <div className="directPickerActions">
                    <button
                      type="button"
                      className="smallBtn"
                      onClick={() => {
                        directPicker.setSelectionTouched(true);
                        directPicker.setSelectedPcIds(directPicker.pcs.map((p) => p.id));
                        composer.onClearError();
                      }}
                    >
                      全选
                    </button>
                    <button
                      type="button"
                      className="smallBtn danger"
                      onClick={() => {
                        directPicker.setSelectionTouched(true);
                        directPicker.setSelectedPcIds([]);
                        composer.onClearError();
                      }}
                    >
                      清空
                    </button>
                  </div>
                </div>
                <div className="directPickerList">
                  {directPicker.pcs.length ? (
                    directPicker.pcs.map((pc) => {
                      const checked = directPicker.selectedPcIds.includes(pc.id);
                      const actor: Actor = { kind: "pc", id: pc.id, name: pc.name };
                      const prof = getProfile(props.profiles, actor);
                      return (
                        <label key={pc.id} className={`directPickerItem ${checked ? "checked" : ""}`}>
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={(e) => {
                              directPicker.setSelectionTouched(true);
                              directPicker.setSelectedPcIds((prev) =>
                                e.target.checked
                                  ? prev.includes(pc.id)
                                    ? prev
                                    : [...prev, pc.id]
                                  : prev.filter((x) => x !== pc.id)
                              );
                              composer.onClearError();
                            }}
                          />
                          <span className="directPickerName" style={nameStyleCss(prof)}>
                            {pc.name}
                          </span>
                        </label>
                      );
                    })
                  ) : (
                    <div className="directPickerEmpty">暂无可选角色</div>
                  )}
                </div>
              </div>
            ) : null}

            <textarea
              value={composer.value}
              placeholder={composer.placeholder}
              onChange={(e) => {
                composer.onChange(e.target.value);
                composer.onClearError();
              }}
              onKeyDown={(e) => {
                if ((e.ctrlKey || e.metaKey) && e.key === "Enter") composer.onSend();
              }}
            />

            {composer.error ? <div className="error">{composer.error}</div> : null}
          </div>

          <button
            className="primary sendBtn"
            disabled={!composer.canSend}
            onClick={composer.onSend}
            aria-label="发送"
            title="发送"
          >
            <Send size={18} />
          </button>
        </div>
      ) : null}
    </>
  );
}
