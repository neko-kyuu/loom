import { Check, Copy, Lock, Pencil, Send, Trash2, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type Dispatch, type ReactNode, type RefObject, type SetStateAction } from "react";
import type { Actor, Message } from "../types";
import type { ProfilesState } from "../lib/profiles";
import { chatDisplayName, getProfile } from "../lib/profiles";
import { absoluteAssetUrl } from "../lib/api";
import { avatarLabel, formatTime, nameStyleCss } from "../lib/chatUi";

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard-unavailable");
    await navigator.clipboard.writeText(text);
    return true;
  } catch {}

  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      return document.execCommand("copy");
    } finally {
      document.body.removeChild(ta);
    }
  } catch {
    return false;
  }
}

export default function ChatFlow(props: {
  messages: Message[];
  profiles: ProfilesState;
  typingNames: string;
  endRef: RefObject<HTMLDivElement>;
  onOpenProfile: (actor: Actor, ev: { clientY: number; currentTarget: Element | null }) => void;
  onDeleteMessage?: (messageId: string) => void;
  onEditMessage?: (messageId: string, content: string) => void;
  directViewerPcId?: string | null;
  dmTargetsByBatchId?: Record<string, string[]>;
  onJumpToDm?: (pcId: string, sendBatchId: string) => void;
  scrollToSendBatchId?: string | null;
  onClearScrollToSendBatchId?: () => void;
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
  const [dmPickerOpenFor, setDmPickerOpenFor] = useState<string | null>(null);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingContent, setEditingContent] = useState<string>("");
  const [editingMinHeightPx, setEditingMinHeightPx] = useState<number>(0);
  const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null);
  const messagesWrapRef = useRef<HTMLDivElement | null>(null);
  const editTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const copyTimeoutRef = useRef<number | null>(null);
  const directViewerPcId = (props.directViewerPcId || "").trim() || null;
  const pillLabel = composer
    ? composer.pill.isDirect
      ? `direct${composer.pill.directSelectedCount ? `(${composer.pill.directSelectedCount})` : ""}`
      : composer.pill.normalLabel
    : "";

  useLayoutEffect(() => {
    if (!editingMessageId) return;
    const el = editTextareaRef.current;
    if (!el) return;
    el.style.height = "0px";
    const next = Math.max(editingMinHeightPx || 0, el.scrollHeight || 0);
    el.style.height = `${Math.max(48, next)}px`;
  }, [editingMessageId, editingContent, editingMinHeightPx]);

  useEffect(() => {
    if (!dmPickerOpenFor) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setDmPickerOpenFor(null);
    }
    function onDocMouseDown(e: MouseEvent) {
      const t = e.target as HTMLElement | null;
      if (!t) return;
      if (t.closest("[data-dm-picker-root='1']")) return;
      setDmPickerOpenFor(null);
    }
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onDocMouseDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onDocMouseDown);
    };
  }, [dmPickerOpenFor]);

  useEffect(() => {
    return () => {
      if (copyTimeoutRef.current) window.clearTimeout(copyTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    const batchId = props.scrollToSendBatchId;
    if (!batchId) return;
    const wrap = messagesWrapRef.current;
    if (!wrap) return;
    const nodes = wrap.querySelectorAll(`[data-send-batch-id="${batchId}"]`);
    if (!nodes.length) return;
    const el = nodes[nodes.length - 1] as HTMLElement;
    el.scrollIntoView({ block: "center" });
    props.onClearScrollToSendBatchId?.();
  }, [props.scrollToSendBatchId, props.messages.length]);
  return (
    <>
      <div className="messages" ref={messagesWrapRef}>
        {props.messages.map((m) => {
          const name = chatDisplayName(props.profiles, m.from_actor);
          const prof = getProfile(props.profiles, m.from_actor);
          const isDirectUserRecord = m.channel === "direct" && m.from_actor.kind === "user";
          const directMeta = (() => {
            if (!directViewerPcId) return null;
            if (m.channel !== "direct") return null;
            const isOutbound = m.from_actor.kind === "pc" && m.from_actor.id === directViewerPcId;
            if (isOutbound) {
              const toActor =
                (m.to || []).find((a) => a.kind === "pc" && Boolean(a.id) && a.id !== directViewerPcId) ||
                (m.to || []).find((a) => a.kind === "dm") ||
                null;
              if (!toActor) return "to: ?";
              const toName =
                chatDisplayName(props.profiles, toActor) || toActor.name || toActor.id || (toActor.kind === "dm" ? "DM" : toActor.kind);
              return `to: ${toName}`;
            }
            const fromName =
              chatDisplayName(props.profiles, m.from_actor) || m.from_actor.name || m.from_actor.id || (m.from_actor.kind === "dm" ? "DM" : m.from_actor.kind);
            return `from: ${fromName}`;
          })();
          const targetPcIds =
            isDirectUserRecord && m.send_batch_id ? props.dmTargetsByBatchId?.[m.send_batch_id] || [] : [];
          const canJumpSingle = Boolean(props.onJumpToDm && targetPcIds.length === 1 && m.send_batch_id);
          const canPick = Boolean(props.onJumpToDm && targetPcIds.length > 1 && m.send_batch_id);
          const jumpTitle = (() => {
            if (!isDirectUserRecord) return "";
            if ((canJumpSingle || canPick) && targetPcIds.length === 1) {
              const pcId = targetPcIds[0];
              const pcName = chatDisplayName(props.profiles, { kind: "pc", id: pcId });
              return `私聊 · 点击跳转到 DM → ${pcName}`;
            }
            if (targetPcIds.length > 1) return `私聊 · 点击选择跳转对象（${targetPcIds.length}）`;
            return "私聊";
          })();
          const pickerTargets = targetPcIds
            .map((pcId) => ({
              pcId,
              name: chatDisplayName(props.profiles, { kind: "pc", id: pcId })
            }))
            .sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN", { sensitivity: "base" }));
          const isEditing = editingMessageId === m.id;
          return (
            <div key={m.id} className="msgRow" data-send-batch-id={m.send_batch_id || undefined}>
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
                  {directMeta ? <span className="directMeta">{directMeta}</span> : null}
                  {isDirectUserRecord ? (
                    <span className="dmMarkWrap" data-dm-picker-root={dmPickerOpenFor === m.id ? "1" : undefined}>
                      {canJumpSingle ? (
                        <button
                          type="button"
                          className="dmMarkBtn clickable"
                          onClick={() => {
                            const pcId = targetPcIds[0];
                            props.onJumpToDm?.(pcId, m.send_batch_id!);
                          }}
                          aria-label="私聊（点击跳转）"
                          title={jumpTitle}
                        >
                          <Lock size={12} />
                        </button>
                      ) : canPick ? (
                        <>
                          <button
                            type="button"
                            className={`dmMarkBtn clickable ${dmPickerOpenFor === m.id ? "open" : ""}`}
                            onClick={() => setDmPickerOpenFor((cur) => (cur === m.id ? null : m.id))}
                            aria-label="私聊（选择跳转）"
                            title={jumpTitle}
                          >
                            <Lock size={12} />
                          </button>
                          {dmPickerOpenFor === m.id ? (
                            <div className="dmPicker" role="menu" aria-label="选择私聊对象">
                              {pickerTargets.map((t) => (
                                <button
                                  key={t.pcId}
                                  type="button"
                                  className="dmPickerItem"
                                  role="menuitem"
                                  onClick={() => {
                                    setDmPickerOpenFor(null);
                                    props.onJumpToDm?.(t.pcId, m.send_batch_id!);
                                  }}
                                >
                                  DM → {t.name}
                                </button>
                              ))}
                            </div>
                          ) : null}
                        </>
                      ) : (
                        <span className="dmMarkBtn" aria-label="私聊" title={jumpTitle}>
                          <Lock size={12} />
                        </span>
                      )}
                    </span>
                  ) : null}
                  <div className="time">{formatTime(m.timestamp)}</div>
                  {!isEditing ? (
                    <button
                      type="button"
                      className="msgActionBtn"
                      onClick={async () => {
                        const ok = await copyToClipboard(m.content || "");
                        if (!ok) return;
                        setCopiedMessageId(m.id);
                        if (copyTimeoutRef.current) window.clearTimeout(copyTimeoutRef.current);
                        copyTimeoutRef.current = window.setTimeout(() => {
                          setCopiedMessageId((cur) => (cur === m.id ? null : cur));
                        }, 1200);
                      }}
                      aria-label="复制到剪贴板"
                      title={copiedMessageId === m.id ? "已复制" : "复制"}
                    >
                      {copiedMessageId === m.id ? <Check size={12} /> : <Copy size={12} />}
                    </button>
                  ) : null}
                  {props.onEditMessage && (m.from_actor.kind === "pc" || m.from_actor.kind === 'dm') && !isEditing ? (
                    <button
                      type="button"
                      className="msgActionBtn"
                      onClick={(e) => {
                        setEditingMessageId(m.id);
                        setEditingContent(m.content || "");
                        const row = (e.currentTarget as HTMLElement | null)?.closest?.(".msgRow") as HTMLElement | null;
                        const contentEl = row?.querySelector?.(".content") as HTMLElement | null;
                        const h = contentEl?.getBoundingClientRect?.().height || 0;
                        setEditingMinHeightPx(h ? Math.ceil(h + 18) : 0);
                      }}
                      aria-label="编辑消息"
                      title="编辑"
                    >
                      <Pencil size={12} />
                    </button>
                  ) : null}
                  {props.onDeleteMessage && (m.from_actor.kind === "pc" || m.from_actor.kind === 'dm') && !isEditing ? (
                    <button
                      type="button"
                      className="msgActionBtn danger"
                      onClick={() => props.onDeleteMessage?.(m.id)}
                      aria-label="删除消息"
                      title="删除"
                    >
                      <Trash2 size={12} />
                    </button>
                  ) : null}
                </div>
                {isEditing ? (
                  <div className="msgEditArea">
                    <textarea
                      className="msgEditTextarea"
                      ref={editTextareaRef}
                      value={editingContent}
                      onChange={(e) => setEditingContent(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Escape") {
                          setEditingMessageId(null);
                          setEditingMinHeightPx(0);
                          return;
                        }
                        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                          const trimmed = (editingContent || "").trim();
                          if (trimmed) props.onEditMessage?.(m.id, trimmed);
                          setEditingMessageId(null);
                          setEditingMinHeightPx(0);
                          return;
                        }
                      }}
                      autoFocus
                    />
                    <div className="msgEditActions" role="toolbar" aria-label="编辑消息操作">
                      <button
                        type="button"
                        className="msgActionBtn"
                        onClick={() => {
                          const trimmed = (editingContent || "").trim();
                          if (trimmed) props.onEditMessage?.(m.id, trimmed);
                          setEditingMessageId(null);
                          setEditingMinHeightPx(0);
                        }}
                        aria-label="保存"
                        title="保存（⌘/Ctrl+Enter）"
                      >
                        <Check size={12} />
                      </button>
                      <button
                        type="button"
                        className="msgActionBtn danger"
                        onClick={() => {
                          setEditingMessageId(null);
                          setEditingMinHeightPx(0);
                        }}
                        aria-label="取消"
                        title="取消（Esc）"
                      >
                        <X size={12} />
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="content">{m.content}</div>
                )}
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
                if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                  e.preventDefault();
                  if (composer.canSend) composer.onSend();
                }
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
