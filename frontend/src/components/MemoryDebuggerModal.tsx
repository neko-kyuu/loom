import { useCallback, useEffect, useMemo, useState } from "react";
import { Pin, Plus, RefreshCcw, Save, ShieldAlert, UserSquare2, X } from "lucide-react";
import type { MemoryEntry } from "../types";
import { createMemory, deleteMemory, getMemories, patchMemory } from "../lib/api";
import { formatTime } from "../lib/chatUi";

type PcOption = { id: string; name: string };
type ManualKind = "autobiography" | "secret";
type MemoryKind = MemoryEntry["kind"];
type MemoryEditState = MemoryEntry["edit_state"];
type PanelMode = "view" | "edit" | "create" | "empty";

type Draft = {
  id: string | null;
  scope: MemoryEntry["scope"];
  scope_id: string;
  owner_pc_id: string;
  kind: MemoryKind;
  summary: string;
  content: string;
  importance: number;
  pinned: boolean;
  subject_type: string;
  subject_id: string;
  edit_state: MemoryEditState;
  deleted_at: string | null;
  revision: number;
};

const ALL_KINDS: MemoryKind[] = ["autobiography", "secret", "relationship", "recent_event"];
const CREATE_KINDS: ManualKind[] = ["autobiography", "secret"];
const EDIT_STATE_OPTIONS: MemoryEditState[] = ["normal", "user_edited", "user_locked"];
const KIND_LABELS: Record<MemoryKind, string> = {
  autobiography: "自传",
  secret: "秘密",
  relationship: "关系",
  recent_event: "近期事件",
};
const SCOPE_LABELS: Record<MemoryEntry["scope"], string> = {
  pc: "角色私有",
  public: "公共",
  direct: "私聊",
};
const EDIT_STATE_LABELS: Record<MemoryEditState, string> = {
  normal: "正常",
  user_edited: "人工修改",
  user_locked: "人工锁定",
  deleted: "已删除",
};
const SOURCE_TYPE_LABELS: Record<string, string> = {
  manual: "手工",
  llm_write: "模型写入",
  deterministic_write: "规则写入",
  migrated: "迁移导入",
};

function kindLabel(kind: MemoryKind): string {
  return KIND_LABELS[kind] || kind;
}

function scopeLabel(scope: MemoryEntry["scope"]): string {
  return SCOPE_LABELS[scope] || scope;
}

function editStateLabel(state: MemoryEditState): string {
  return EDIT_STATE_LABELS[state] || state;
}

function sourceTypeLabel(sourceType: string | null | undefined): string {
  if (!sourceType) return "—";
  return SOURCE_TYPE_LABELS[sourceType] || sourceType;
}

function makeEmptyDraft(pcs: PcOption[], kind: ManualKind = "autobiography"): Draft {
  return {
    id: null,
    scope: "pc",
    scope_id: "",
    owner_pc_id: pcs[0]?.id || "",
    kind,
    summary: "",
    content: "",
    importance: 1,
    pinned: false,
    subject_type: "",
    subject_id: "",
    edit_state: "normal",
    deleted_at: null,
    revision: 0,
  };
}

function copyMemoryToDraft(memory: MemoryEntry, pcs: PcOption[]): Draft {
  return {
    id: memory.id,
    scope: memory.scope,
    scope_id: memory.scope_id || "",
    owner_pc_id: memory.owner_pc_id || pcs[0]?.id || "",
    kind: memory.kind,
    summary: memory.summary,
    content: memory.content,
    importance: memory.importance,
    pinned: memory.pinned,
    subject_type: memory.subject_type || "",
    subject_id: memory.subject_id || "",
    edit_state: memory.edit_state,
    deleted_at: memory.deleted_at || null,
    revision: memory.revision,
  };
}

function findPcName(pcs: PcOption[], pcId: string | null | undefined): string {
  if (!pcId) return "—";
  return pcs.find((pc) => pc.id === pcId)?.name || pcId;
}

function mergeKeyOf(memory: MemoryEntry | null): string {
  const value = memory?.meta?.merge_key;
  return typeof value === "string" && value.trim() ? value.trim() : "—";
}

function formatNullableTime(value: string | null | undefined): string {
  return value ? formatTime(value) : "—";
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
  const [showDeleted, setShowDeleted] = useState(false);
  const [editStateFilter, setEditStateFilter] = useState<string>("");
  const [sourceTypeFilter, setSourceTypeFilter] = useState<string>("");

  const [draft, setDraft] = useState<Draft>(() => makeEmptyDraft(props.pcs));
  const [panelMode, setPanelMode] = useState<PanelMode>("empty");

  const selected = useMemo(() => items.find((item) => item.id === selectedId) || null, [items, selectedId]);
  const isEditing = panelMode === "edit" || panelMode === "create";
  const canSaveDraft = !draft.deleted_at;
  const currentMetaMemory = panelMode === "create" ? null : selected;

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
        deleted: showDeleted,
        editState: editStateFilter || null,
        sourceType: sourceTypeFilter || null,
        limit: 200,
      });
      setItems(res.items || []);
      setTurnNo(res.turn_no ?? null);
      setDecayInfo(res.decay ?? null);
      setSelectedId((cur) => {
        if (cur && (res.items || []).some((item) => item.id === cur)) {
          return cur;
        }
        if (panelMode === "create" || panelMode === "empty") {
          return null;
        }
        return res.items?.[0]?.id || null;
      });
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, [props.open, scopeFilter, kindFilter, ownerPcFilter, pinnedOnly, showDeleted, editStateFilter, sourceTypeFilter, panelMode]);

  const dashText = useCallback((value: string | number | null | undefined) => {
    if (value == null) return "-";
    if (typeof value === "string") return value.trim() ? value : "-";
    return String(value);
  }, []);

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
    if (!selected || panelMode === "create") return;
    setDraft(copyMemoryToDraft(selected, props.pcs));
  }, [panelMode, props.open, props.pcs, selected]);

  const beginCreate = useCallback(
    (kind: ManualKind = "autobiography") => {
      setSelectedId(null);
      setDraft(makeEmptyDraft(props.pcs, kind));
      setPanelMode("create");
    },
    [props.pcs]
  );

  const resetDraft = useCallback(() => {
    if (panelMode === "create") {
      setDraft(makeEmptyDraft(props.pcs, draft.kind === "secret" ? "secret" : "autobiography"));
      return;
    }
    if (selected && draft.id === selected.id) {
      setDraft(copyMemoryToDraft(selected, props.pcs));
      return;
    }
    setDraft((prev) => {
      const empty = makeEmptyDraft(props.pcs, prev.kind === "secret" ? "secret" : "autobiography");
      return {
        ...empty,
        owner_pc_id: prev.owner_pc_id || empty.owner_pc_id,
      };
    });
  }, [draft.id, props.pcs, selected]);

  const startEditingMemory = useCallback(
    (memory: MemoryEntry) => {
      setSelectedId(memory.id);
      setDraft(copyMemoryToDraft(memory, props.pcs));
      setPanelMode("edit");
    },
    [props.pcs]
  );

  const showMemoryDetails = useCallback(
    (memoryId: string) => {
      setSelectedId(memoryId);
      setPanelMode("view");
    },
    []
  );

  const togglePin = useCallback(
    async (memory: MemoryEntry) => {
      setSaving(true);
      setError(null);
      try {
        const updated = await patchMemory(memory.id, { pinned: !memory.pinned });
        setItems((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
        if (selectedId === updated.id) {
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

  const removeMemory = useCallback(
    async (memory: MemoryEntry) => {
      if (!window.confirm(`确认软删除这条记忆？\n\n${memory.summary}`)) return;
      setSaving(true);
      setError(null);
      try {
        const updated = await deleteMemory(memory.id);
        setItems((prev) => prev.map((item) => (item.id === updated.id ? updated : item)).filter((item) => (showDeleted ? true : !item.deleted_at)));
        if (selectedId === updated.id) {
          setSelectedId(null);
          setDraft(makeEmptyDraft(props.pcs));
          setPanelMode("empty");
        }
        await loadMemories();
      } catch (e: any) {
        setError(String(e?.message || e));
      } finally {
        setSaving(false);
      }
    },
    [loadMemories, props.pcs, selectedId, showDeleted]
  );

  const saveDraft = useCallback(async () => {
    if (!draft.summary.trim() || !draft.content.trim()) {
      setError("请填写摘要和正文");
      return;
    }
    if (!draft.id && !draft.owner_pc_id.trim()) {
      setError("新建时必须选择 PC");
      return;
    }
    if (draft.scope === "pc" && !draft.owner_pc_id.trim()) {
      setError("pc scope 记忆必须有 owner_pc_id");
      return;
    }
    if (!canSaveDraft) {
      setError("已删除记忆暂不支持恢复，请仅浏览");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const next = draft.id
        ? await patchMemory(draft.id, {
            owner_pc_id: draft.scope === "pc" ? draft.owner_pc_id || null : null,
            kind: draft.kind,
            summary: draft.summary,
            content: draft.content,
            importance: draft.importance,
            pinned: draft.pinned,
            subject_type: draft.subject_type || null,
            subject_id: draft.subject_id || null,
            edit_state: draft.edit_state,
          })
        : await createMemory({
            owner_pc_id: draft.owner_pc_id,
            kind: draft.kind as ManualKind,
            summary: draft.summary,
            content: draft.content,
            importance: draft.importance,
            pinned: draft.pinned,
          });
      await loadMemories();
      setSelectedId(next.id);
      setDraft(copyMemoryToDraft(next, props.pcs));
      setPanelMode("view");
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setSaving(false);
    }
  }, [canSaveDraft, draft, loadMemories, props.pcs]);

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
                <option value="pc">角色私有</option>
                <option value="public">公共</option>
                <option value="direct">私聊</option>
              </select>
              <select className="customSelect" value={kindFilter} onChange={(e) => setKindFilter(e.target.value)}>
                <option value="">全部 kind</option>
                {ALL_KINDS.map((kind) => (
                  <option key={kind} value={kind}>{kindLabel(kind)}</option>
                ))}
              </select>
              <select className="customSelect" value={ownerPcFilter} onChange={(e) => setOwnerPcFilter(e.target.value)}>
                <option value="">全部 PC</option>
                {props.pcs.map((pc) => (
                  <option key={pc.id} value={pc.id}>{pc.name}</option>
                ))}
              </select>
              <select className="customSelect" value={editStateFilter} onChange={(e) => setEditStateFilter(e.target.value)}>
                <option value="">全部编辑状态</option>
                <option value="normal">正常</option>
                <option value="user_edited">人工修改</option>
                <option value="user_locked">人工锁定</option>
                <option value="deleted">已删除</option>
              </select>
              <select className="customSelect" value={sourceTypeFilter} onChange={(e) => setSourceTypeFilter(e.target.value)}>
                <option value="">全部来源</option>
                <option value="manual">手工</option>
                <option value="llm_write">模型写入</option>
                <option value="deterministic_write">规则写入</option>
                <option value="migrated">迁移导入</option>
              </select>
              <label className="memoryCheck">
                <input type="checkbox" checked={pinnedOnly} onChange={(e) => setPinnedOnly(e.target.checked)} />
                <span>仅 pinned</span>
              </label>
              <label className="memoryCheck">
                <input type="checkbox" checked={showDeleted} onChange={(e) => setShowDeleted(e.target.checked)} />
                <span>显示已删除</span>
              </label>
            </div>
            <div className="memoryList">
              {loading ? <div className="memoryEmpty">加载中…</div> : null}
              {!loading && !items.length ? <div className="memoryEmpty">暂无记忆</div> : null}
              {items.map((item) => (
                <div
                  key={item.id}
                  className={`memoryRow ${selectedId === item.id ? "active" : ""} ${item.deleted_at ? "deleted" : ""}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => showMemoryDetails(item.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      showMemoryDetails(item.id);
                    }
                  }}
                >
                    <div className="memoryRowTop">
                    <span className={`memoryBadge kind-${item.kind}`}>{kindLabel(item.kind)}</span>
                    {item.deleted_at ? <span className="memoryBadge state-deleted">已删除</span> : null}
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
                    <span>{scopeLabel(item.scope)}</span>
                    {!item.deleted_at ? (
                      <>
                        <span>·</span>
                        <button
                          className="memoryInlineAction"
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            startEditingMemory(item);
                          }}
                        >
                          编辑
                        </button>
                        <button
                          className="memoryInlineAction danger"
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            void removeMemory(item);
                          }}
                        >
                          删除
                        </button>
                      </>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="memoryPane memoryDetailPane memoryInspectorPane">
            <div className="memoryPaneHead">
              <div>
                <div className="memoryPaneTitle">
                  {panelMode === "create" ? "新建记忆" : panelMode === "edit" ? "编辑记忆" : "记忆详情"}
                </div>
                <div className="memorySubTitle">
                  {panelMode === "create"
                    ? "填写空白记忆"
                    : panelMode === "edit"
                      ? `正在修改：${scopeLabel(draft.scope)} / ${kindLabel(draft.kind)} · 修订号 ${draft.revision}`
                      : selected
                        ? `查看：${scopeLabel(selected.scope)} / ${kindLabel(selected.kind)}`
                        : "从左侧选择一条记忆，或点击新建"}
                </div>
              </div>
              <div className="memoryDetailActions">
                {panelMode === "view" && selected && !selected.deleted_at ? (
                  <>
                    <button className="smallBtn" onClick={() => void togglePin(selected)}>
                      {selected.pinned ? "取消置顶" : "置顶"}
                    </button>
                    <button className="smallBtn" onClick={() => startEditingMemory(selected)}>编辑</button>
                    <button className="smallBtn danger" onClick={() => void removeMemory(selected)}>删除</button>
                  </>
                ) : null}
                {(panelMode === "edit" || panelMode === "create") ? (
                  <>
                    <button className="smallBtn" onClick={resetDraft}>
                      <Plus size={14} />
                      <span>{panelMode === "edit" ? "还原" : "清空"}</span>
                    </button>
                    <button className="smallBtn strong" disabled={saving || !canSaveDraft} onClick={() => void saveDraft()}>
                      <Save size={14} />
                      <span>{draft.id ? "保存修改" : "录入"}</span>
                    </button>
                  </>
                ) : null}
                {panelMode === "view" ? (
                  <>
                    <button className="smallBtn" onClick={() => beginCreate("autobiography")}>
                      <UserSquare2 size={14} />
                      <span>新建自传</span>
                    </button>
                    <button className="smallBtn" onClick={() => beginCreate("secret")}>
                      <ShieldAlert size={14} />
                      <span>新建秘密</span>
                    </button>
                  </>
                ) : null}
              </div>
            </div>
            {panelMode !== "empty" ? (
              <div className="memoryDetailCard">
                <div className="memoryDetailLayout">
                  <div className="memoryDetailSide">
                    <div className="memoryDetailGroup">
                      <div className="memoryDetailGroupTitle">基础信息</div>
                      <div className="formRow">
                        <div className="formLabel">摘要</div>
                        {isEditing ? (
                          <input
                            className="customSelect"
                            value={draft.summary}
                            disabled={!!draft.deleted_at}
                            onChange={(e) => setDraft((prev) => ({ ...prev, summary: e.target.value }))}
                            placeholder="短摘要，便于检索"
                          />
                        ) : (
                          <div className="memoryStaticValue">{dashText(selected?.summary)}</div>
                        )}
                      </div>
                      <div className="formRow">
                        <div className="formLabel">范围</div>
                        {isEditing ? (
                          <input className="customSelect" value={scopeLabel(draft.scope)} readOnly />
                        ) : (
                          <div className="memoryStaticValue">{selected ? scopeLabel(selected.scope) : "-"}</div>
                        )}
                      </div>
                      <div className="formRow">
                        <div className="formLabel">类型</div>
                        {isEditing ? (
                          <select
                            className="customSelect"
                            value={draft.kind}
                            disabled={!!draft.deleted_at}
                            onChange={(e) => setDraft((prev) => ({ ...prev, kind: e.target.value as MemoryKind }))}
                          >
                            {(draft.id ? ALL_KINDS : CREATE_KINDS).map((kind) => (
                              <option key={kind} value={kind}>{kindLabel(kind)}</option>
                            ))}
                          </select>
                        ) : (
                          <div className="memoryStaticValue">{selected ? kindLabel(selected.kind) : "-"}</div>
                        )}
                      </div>
                      <div className="formRow">
                        <div className="formLabel">归属角色</div>
                        {isEditing ? (
                          <select
                            className="customSelect"
                            value={draft.owner_pc_id}
                            disabled={draft.scope !== "pc" || !!draft.deleted_at}
                            onChange={(e) => setDraft((prev) => ({ ...prev, owner_pc_id: e.target.value }))}
                          >
                            {props.pcs.map((pc) => (
                              <option key={pc.id} value={pc.id}>{pc.name}</option>
                            ))}
                          </select>
                        ) : (
                          <div className="memoryStaticValue">{findPcName(props.pcs, selected?.owner_pc_id)}</div>
                        )}
                      </div>
                      <div className="formRow">
                        <div className="formLabel">范围 ID</div>
                        {isEditing ? (
                          <input className="customSelect" value={draft.scope_id} readOnly placeholder="范围 ID 不可修改" />
                        ) : (
                          <div className="memoryStaticValue">{dashText(selected?.scope_id)}</div>
                        )}
                      </div>
                      <div className="formRow">
                        <div className="formLabel">关联对象类型</div>
                        {isEditing ? (
                          <input
                            className="customSelect"
                            value={draft.subject_type}
                            disabled={!!draft.deleted_at}
                            onChange={(e) => setDraft((prev) => ({ ...prev, subject_type: e.target.value }))}
                            placeholder="例如：pc / dm"
                          />
                        ) : (
                          <div className="memoryStaticValue">{dashText(selected?.subject_type)}</div>
                        )}
                      </div>
                      <div className="formRow">
                        <div className="formLabel">关联对象 ID</div>
                        {isEditing ? (
                          <input
                            className="customSelect"
                            value={draft.subject_id}
                            disabled={!!draft.deleted_at}
                            onChange={(e) => setDraft((prev) => ({ ...prev, subject_id: e.target.value }))}
                            placeholder="关联对象 id"
                          />
                        ) : (
                          <div className="memoryStaticValue">{dashText(selected?.subject_id)}</div>
                        )}
                      </div>
                      <div className="formRow">
                        <div className="formLabel">置顶</div>
                        <div className="memoryInlineFields">
                          <label className="memoryCheck">
                            <input
                              type="checkbox"
                              checked={isEditing ? draft.pinned : !!selected?.pinned}
                              disabled={!isEditing || !!draft.deleted_at}
                              onChange={(e) => setDraft((prev) => ({ ...prev, pinned: e.target.checked }))}
                            />
                            <span>置顶此记忆</span>
                          </label>
                        </div>
                      </div>
                    </div>

                    <div className="memoryDetailGroup">
                      <div className="memoryDetailGroupTitle">来源与分值</div>
                      <div className="formRow">
                        <div className="formLabel">来源</div>
                        <div className="memoryStaticValue">{sourceTypeLabel(currentMetaMemory?.source_type)}</div>
                      </div>
                      <div className="formRow">
                        <div className="formLabel">合并键</div>
                        <div className="memoryStaticValue">{mergeKeyOf(currentMetaMemory)}</div>
                      </div>
                      <div className="formRow">
                        <div className="formLabel">分数</div>
                        <div className="memoryStaticValue">{dashText(currentMetaMemory?.score)}</div>
                      </div>
                      <div className="formRow">
                        <div className="formLabel">重要度</div>
                        {isEditing ? (
                          <div className="memoryInlineFields">
                            <input
                              className="customSelect"
                              type="number"
                              min={0}
                              max={10}
                              value={draft.importance}
                              disabled={!!draft.deleted_at}
                              onChange={(e) => setDraft((prev) => ({ ...prev, importance: Number(e.target.value || 0) }))}
                            />
                          </div>
                        ) : (
                          <div className="memoryStaticValue">{dashText(selected?.importance)}</div>
                        )}
                      </div>
                      <div className="formRow">
                        <div className="formLabel">访问次数</div>
                        <div className="memoryStaticValue">{dashText(currentMetaMemory?.access_count)}</div>
                      </div>
                    </div>
                  </div>

                  <div className="memoryDetailMain">
                    <div className="memoryDetailGroup">
                      <div className="memoryDetailGroupTitle">生命周期</div>
                      <div className="formRow">
                        <div className="formLabel">编辑状态</div>
                        {isEditing ? (
                          <select
                            className="customSelect"
                            value={draft.deleted_at ? "deleted" : draft.edit_state}
                            disabled={!draft.id || !!draft.deleted_at}
                            onChange={(e) => setDraft((prev) => ({ ...prev, edit_state: e.target.value as MemoryEditState }))}
                          >
                            {draft.deleted_at ? <option value="deleted">已删除</option> : null}
                            {EDIT_STATE_OPTIONS.map((state) => (
                              <option key={state} value={state}>{editStateLabel(state)}</option>
                            ))}
                          </select>
                        ) : (
                          <div className="memoryStaticValue">{selected ? editStateLabel(selected.edit_state) : "-"}</div>
                        )}
                      </div>
                      <div className="formRow">
                        <div className="formLabel">修订号</div>
                        <div className="memoryStaticValue">{dashText(isEditing ? draft.revision : selected?.revision)}</div>
                      </div>
                      <div className="formRow">
                        <div className="formLabel">更新时间</div>
                        <div className="memoryStaticValue">{formatNullableTime(isEditing ? currentMetaMemory?.updated_at : selected?.updated_at)}</div>
                      </div>
                      <div className="formRow">
                        <div className="formLabel">删除时间</div>
                        <div className="memoryStaticValue">{formatNullableTime(isEditing ? draft.deleted_at : selected?.deleted_at)}</div>
                      </div>
                    </div>
                    <div className="memoryDetailGroup">
                      <div className="memoryDetailGroupTitle">正文</div>
                      <div className="formRow memoryTextareaRow">
                        <div className="formLabel">正文</div>
                        {isEditing ? (
                          <div className="memoryContentPanel memoryContentPanelEditing">
                            <textarea
                              className="memoryTextarea"
                              value={draft.content}
                              disabled={!!draft.deleted_at}
                              onChange={(e) => setDraft((prev) => ({ ...prev, content: e.target.value }))}
                              placeholder="记忆正文"
                            />
                          </div>
                        ) : (
                          <div className="memoryContentPanel">
                            <pre className="memoryContent">{dashText(selected?.content)}</pre>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
                {panelMode === "view" && selected?.deleted_at ? <div className="memoryHint">此条已软删除，当前版本仅支持浏览，不支持恢复。</div> : null}
                {panelMode === "create" ? <div className="memoryHint">新建入口仍限定为角色私有下的自传 / 秘密；其余类型先通过修改已有条目处理。</div> : null}
              </div>
            ) : (
              <div className="memoryEmpty">从左侧选择一条记忆查看详情，或点击“新建”开始录入。</div>
            )}
          </section>
        </div>

        {error ? <div className="memoryError">{error}</div> : null}
      </div>
    </div>
  );
}
