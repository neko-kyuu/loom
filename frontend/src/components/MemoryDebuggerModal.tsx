import { useCallback, useEffect, useMemo, useState } from "react";
import { Pin, Plus, RefreshCcw, Save, ShieldAlert, UserSquare2, X } from "lucide-react";
import type { MemoryEntry } from "../types";
import { createMemory, getMemories, patchMemory } from "../lib/api";
import { formatTime } from "../lib/chatUi";

type PcOption = { id: string; name: string };
type ManualKind = "autobiography" | "secret";

type Draft = {
  id: string | null;
  owner_pc_id: string;
  kind: ManualKind;
  summary: string;
  content: string;
  importance: number;
  pinned: boolean;
};

function makeEmptyDraft(pcs: PcOption[], kind: ManualKind = "autobiography"): Draft {
  return {
    id: null,
    owner_pc_id: pcs[0]?.id || "",
    kind,
    summary: "",
    content: "",
    importance: 1,
    pinned: false,
  };
}

function copyMemoryToDraft(memory: MemoryEntry, pcs: PcOption[]): Draft {
  return {
    id: memory.id,
    owner_pc_id: memory.owner_pc_id || pcs[0]?.id || "",
    kind: memory.kind as ManualKind,
    summary: memory.summary,
    content: memory.content,
    importance: memory.importance,
    pinned: memory.pinned,
  };
}

function isManualEditable(memory: MemoryEntry | null | undefined): memory is MemoryEntry {
  return Boolean(memory && memory.scope === "pc" && (memory.kind === "autobiography" || memory.kind === "secret"));
}

export default function MemoryDebuggerModal(props: {
  open: boolean;
  onClose: () => void;
  pcs: PcOption[];
}) {
  const [items, setItems] = useState<MemoryEntry[]>([]);
  const [turnNo, setTurnNo] = useState<number | null>(null);
  const [decayInfo, setDecayInfo] = useState<{ interval_ticks: number; k: number; threshold: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const [scopeFilter, setScopeFilter] = useState<string>("");
  const [kindFilter, setKindFilter] = useState<string>("");
  const [ownerPcFilter, setOwnerPcFilter] = useState<string>("");
  const [pinnedOnly, setPinnedOnly] = useState(false);

  const [draft, setDraft] = useState<Draft>(() => makeEmptyDraft(props.pcs));

  const selected = useMemo(() => items.find((item) => item.id === selectedId) || null, [items, selectedId]);

  const loadMemories = useCallback(async () => {
    if (!props.open) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getMemories({
        scope: scopeFilter || null,
        kind: kindFilter || null,
        ownerPcId: ownerPcFilter || null,
        pinned: pinnedOnly ? true : null,
        limit: 200,
      });
      setItems(res.items || []);
      setTurnNo(res.turn_no ?? null);
      setDecayInfo(res.decay ?? null);
      setSelectedId((cur) => (cur && (res.items || []).some((item) => item.id === cur) ? cur : res.items?.[0]?.id || null));
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, [props.open, scopeFilter, kindFilter, ownerPcFilter, pinnedOnly]);

  useEffect(() => {
    if (!props.open) return;
    void loadMemories();
  }, [props.open, loadMemories]);

  useEffect(() => {
    if (!props.open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") props.onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [props.open, props.onClose]);

  useEffect(() => {
    if (!props.open) return;
    if (!props.pcs.length) return;
    setDraft((prev) => {
      if (prev.owner_pc_id) return prev;
      return { ...prev, owner_pc_id: props.pcs[0].id };
    });
  }, [props.open, props.pcs]);

  useEffect(() => {
    if (!props.open) return;
    if (!isManualEditable(selected)) return;
    setDraft(copyMemoryToDraft(selected, props.pcs));
  }, [props.open, props.pcs, selected]);

  const beginCreate = useCallback(
    (kind: ManualKind = "autobiography") => {
      setDraft(makeEmptyDraft(props.pcs, kind));
    },
    [props.pcs]
  );

  const resetDraft = useCallback(() => {
    if (isManualEditable(selected) && draft.id === selected.id) {
      setDraft(copyMemoryToDraft(selected, props.pcs));
      return;
    }
    setDraft((prev) => {
      const empty = makeEmptyDraft(props.pcs, prev.kind);
      return {
        ...empty,
        owner_pc_id: prev.owner_pc_id || empty.owner_pc_id,
        pinned: prev.pinned,
      };
    });
  }, [draft.id, props.pcs, selected]);

  const loadSelectedIntoDraft = useCallback(() => {
    if (!isManualEditable(selected)) return;
    setDraft(copyMemoryToDraft(selected, props.pcs));
  }, [selected, props.pcs]);

  const togglePin = useCallback(
    async (memory: MemoryEntry) => {
      setSaving(true);
      setError(null);
      try {
        const updated = await patchMemory(memory.id, { pinned: !memory.pinned });
        setItems((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
        if (selectedId === updated.id && isManualEditable(updated)) {
          setDraft((prev) => ({ ...prev, pinned: updated.pinned }));
        }
      } catch (e: any) {
        setError(String(e?.message || e));
      } finally {
        setSaving(false);
      }
    },
    [selectedId]
  );

  const saveDraft = useCallback(async () => {
    if (!draft.owner_pc_id.trim() || !draft.summary.trim() || !draft.content.trim()) {
      setError("请填写 PC、摘要和正文");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const next = draft.id
        ? await patchMemory(draft.id, {
            owner_pc_id: draft.owner_pc_id,
            kind: draft.kind,
            summary: draft.summary,
            content: draft.content,
            importance: draft.importance,
            pinned: draft.pinned,
          })
        : await createMemory({
            owner_pc_id: draft.owner_pc_id,
            kind: draft.kind,
            summary: draft.summary,
            content: draft.content,
            importance: draft.importance,
            pinned: draft.pinned,
          });
      await loadMemories();
      setSelectedId(next.id);
      setDraft({
        id: next.id,
        owner_pc_id: next.owner_pc_id || draft.owner_pc_id,
        kind: next.kind as ManualKind,
        summary: next.summary,
        content: next.content,
        importance: next.importance,
        pinned: next.pinned,
      });
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setSaving(false);
    }
  }, [draft, loadMemories]);

  if (!props.open) return null;

  return (
    <div className="memoryOverlay" role="presentation" onClick={props.onClose}>
      <div
        className="memoryModal"
        role="dialog"
        aria-label="记忆调试器"
        onClick={(e) => {
          e.stopPropagation();
        }}
      >
        <div className="memoryHeader">
          <div>
            <div className="memoryTitle">记忆调试器</div>
            <div className="memorySubTitle">
              {turnNo != null ? `turn #${turnNo}` : "turn 未知"}
              {decayInfo ? ` · decay ${decayInfo.interval_ticks} ticks / k=${decayInfo.k} / threshold=${decayInfo.threshold}` : ""}
            </div>
          </div>
          <div className="memoryHeaderActions">
            <button className="iconBtn iconOnly" onClick={() => void loadMemories()} aria-label="刷新" title="刷新">
              <RefreshCcw size={18} />
            </button>
            <button className="iconBtn iconOnly" onClick={props.onClose} aria-label="关闭" title="关闭">
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="memoryBody">
          <section className="memoryPane memoryListPane">
            <div className="memoryPaneHead">
              <div className="memoryPaneTitle">浏览</div>
              <button className="smallBtn" onClick={() => beginCreate("autobiography")}>新建</button>
            </div>
            <div className="memoryFilters">
              <select className="customSelect" value={scopeFilter} onChange={(e) => setScopeFilter(e.target.value)}>
                <option value="">全部 scope</option>
                <option value="pc">pc</option>
                <option value="public">public</option>
                <option value="direct">direct</option>
              </select>
              <select className="customSelect" value={kindFilter} onChange={(e) => setKindFilter(e.target.value)}>
                <option value="">全部 kind</option>
                <option value="autobiography">autobiography</option>
                <option value="secret">secret</option>
                <option value="relationship">relationship</option>
                <option value="recent_event">recent_event</option>
              </select>
              <select className="customSelect" value={ownerPcFilter} onChange={(e) => setOwnerPcFilter(e.target.value)}>
                <option value="">全部 PC</option>
                {props.pcs.map((pc) => (
                  <option key={pc.id} value={pc.id}>{pc.name}</option>
                ))}
              </select>
              <label className="memoryCheck">
                <input type="checkbox" checked={pinnedOnly} onChange={(e) => setPinnedOnly(e.target.checked)} />
                <span>仅 pinned</span>
              </label>
            </div>
            <div className="memoryList">
              {loading ? <div className="memoryEmpty">加载中…</div> : null}
              {!loading && !items.length ? <div className="memoryEmpty">暂无记忆</div> : null}
              {items.map((item) => {
                const editable = isManualEditable(item);
                return (
                  <div
                    key={item.id}
                    className={`memoryRow ${selectedId === item.id ? "active" : ""}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelectedId(item.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelectedId(item.id);
                      }
                    }}
                  >
                    <div className="memoryRowTop">
                      <span className={`memoryBadge kind-${item.kind}`}>{item.kind}</span>
                      <button
                        className={`memoryPinBtn ${item.pinned ? "on" : ""}`}
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          void togglePin(item);
                        }}
                        aria-label={item.pinned ? "取消 pin" : "pin 记忆"}
                        title={item.pinned ? "取消 pin" : "pin"}
                      >
                        <Pin size={14} />
                      </button>
                    </div>
                    <div className="memorySummary">{item.summary}</div>
                    <div className="memoryMetaLine">
                      <span>{item.scope}</span>
                      <span>·</span>
                      <span>{item.owner_pc_id || item.scope_id || "—"}</span>
                      <span>·</span>
                      <span>S{item.score}</span>
                      {editable ? (
                        <>
                          <span>·</span>
                          <button
                            className="memoryInlineAction"
                            onClick={(e) => {
                              e.preventDefault();
                              e.stopPropagation();
                              setSelectedId(item.id);
                              setDraft(copyMemoryToDraft(item, props.pcs));
                            }}
                          >
                            编辑
                          </button>
                        </>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="memoryPane memoryDetailPane">
            <div className="memoryPaneHead">
              <div className="memoryPaneTitle">详情</div>
              {selected ? (
                <div className="memoryDetailActions">
                  <button className="smallBtn" onClick={() => void togglePin(selected)}>
                    {selected.pinned ? "Unpin" : "Pin"}
                  </button>
                  {isManualEditable(selected) ? (
                    <button className="smallBtn" onClick={loadSelectedIntoDraft}>编辑此条</button>
                  ) : null}
                </div>
              ) : null}
            </div>
            {selected ? (
              <div className="memoryDetailCard">
                <div className="memoryDetailTitle">{selected.summary}</div>
                <div className="memoryDetailMeta">
                  <span>{selected.scope}</span>
                  <span>{selected.kind}</span>
                  <span>owner: {selected.owner_pc_id || "—"}</span>
                  <span>score: {selected.score}</span>
                  <span>importance: {selected.importance}</span>
                  <span>access: {selected.access_count}</span>
                  <span>updated: {formatTime(selected.updated_at)}</span>
                </div>
                <pre className="memoryContent">{selected.content}</pre>
                {!isManualEditable(selected) ? (
                  <div className="memoryHint">此条目前仅支持只读；可手工修改的只有 `pc` 范围的 `autobiography / secret`。</div>
                ) : null}
              </div>
            ) : (
              <div className="memoryEmpty">选择一条记忆查看详情</div>
            )}
          </section>

          <section className="memoryPane memoryEditorPane">
            <div className="memoryPaneHead">
              <div>
                <div className="memoryPaneTitle">手工录入 / 修改</div>
                <div className="memorySubTitle">{draft.id ? "正在修改已选记忆" : "正在新建草稿"}</div>
              </div>
              <div className="memoryEditorTypeActions">
                <button className="smallBtn" onClick={() => beginCreate("autobiography")}>
                  <UserSquare2 size={14} />
                  <span>自传</span>
                </button>
                <button className="smallBtn" onClick={() => beginCreate("secret")}>
                  <ShieldAlert size={14} />
                  <span>秘密</span>
                </button>
              </div>
            </div>
            <div className="memoryEditorForm">
              <div className="formRow">
                <div className="formLabel">PC</div>
                <select
                  className="customSelect"
                  value={draft.owner_pc_id}
                  onChange={(e) => setDraft((prev) => ({ ...prev, owner_pc_id: e.target.value }))}
                >
                  {props.pcs.map((pc) => (
                    <option key={pc.id} value={pc.id}>{pc.name}</option>
                  ))}
                </select>
              </div>
              <div className="formRow">
                <div className="formLabel">类型</div>
                <select
                  className="customSelect"
                  value={draft.kind}
                  onChange={(e) => setDraft((prev) => ({ ...prev, kind: e.target.value as ManualKind }))}
                >
                  <option value="autobiography">autobiography</option>
                  <option value="secret">secret</option>
                </select>
              </div>
              <div className="formRow">
                <div className="formLabel">摘要</div>
                <input
                  className="customSelect"
                  value={draft.summary}
                  onChange={(e) => setDraft((prev) => ({ ...prev, summary: e.target.value }))}
                  placeholder="短摘要，便于检索"
                />
              </div>
              <div className="formRow memoryEditorRowTop">
                <div className="formLabel">重要度</div>
                <div className="memoryInlineFields">
                  <input
                    className="customSelect"
                    type="number"
                    min={0}
                    max={10}
                    value={draft.importance}
                    onChange={(e) => setDraft((prev) => ({ ...prev, importance: Number(e.target.value || 0) }))}
                  />
                  <label className="memoryCheck">
                    <input
                      type="checkbox"
                      checked={draft.pinned}
                      onChange={(e) => setDraft((prev) => ({ ...prev, pinned: e.target.checked }))}
                    />
                    <span>pinned</span>
                  </label>
                </div>
              </div>
              <div className="memoryEditorBlock">
                <div className="formLabel">正文</div>
                <textarea
                  className="textarea memoryTextarea"
                  value={draft.content}
                  onChange={(e) => setDraft((prev) => ({ ...prev, content: e.target.value }))}
                  placeholder="写完整一些的自传 / 秘密内容"
                />
              </div>
              <div className="memoryEditorFooter">
                <button className="smallBtn" onClick={resetDraft}>
                  <Plus size={14} />
                  <span>{draft.id ? "还原" : "重置草稿"}</span>
                </button>
                <button className="smallBtn strong" disabled={saving} onClick={() => void saveDraft()}>
                  <Save size={14} />
                  <span>{draft.id ? "保存修改" : "录入"}</span>
                </button>
              </div>
            </div>
          </section>
        </div>

        {error ? <div className="memoryError">{error}</div> : null}
      </div>
    </div>
  );
}
