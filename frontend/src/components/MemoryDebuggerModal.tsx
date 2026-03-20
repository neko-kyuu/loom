import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Edit, Info, Pin, RefreshCcw, Save, Trash2, X } from "lucide-react";
import type { MemoryEntry } from "../types";
import { deleteMemory, getMemories, patchMemory } from "../lib/api";
import { formatTime } from "../lib/chatUi";

type PcOption = { id: string; name: string };
type MemoryKind = MemoryEntry["kind"];

type SourceSelection =
  | { scope: "public" }
  | { scope: "direct" }
  | { scope: "pc"; pcId: string };

type MemoryCluster = {
  key: string;
  kind: MemoryKind;
  representative: MemoryEntry;
  members: MemoryEntry[];
};

const ALL_KINDS: MemoryKind[] = ["autobiography", "secret", "relationship", "recent_event"];
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

function kindLabel(kind: MemoryKind): string {
  return KIND_LABELS[kind] || kind;
}

function scopeLabel(scope: MemoryEntry["scope"]): string {
  return SCOPE_LABELS[scope] || scope;
}

function findPcName(pcs: PcOption[], pcId: string | null | undefined): string {
  if (!pcId) return "—";
  return pcs.find((pc) => pc.id === pcId)?.name || pcId;
}

function mergeKeyOf(memory: MemoryEntry): string {
  const value = memory?.meta?.merge_key;
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function mergedSourcesCountOf(memory: MemoryEntry): number {
  const merged = memory?.meta?.merged_sources;
  return Array.isArray(merged) ? merged.length : 0;
}

function formatNullableTime(value: string | null | undefined): string {
  return value ? formatTime(value) : "—";
}

function compareTimestampDesc(a: string | null | undefined, b: string | null | undefined): number {
  return String(b || "").localeCompare(String(a || ""));
}

function clusterItems(items: MemoryEntry[]): Record<MemoryKind, MemoryCluster[]> {
  const mapByKind: Record<MemoryKind, Map<string, MemoryEntry[]>> = {
    autobiography: new Map(),
    secret: new Map(),
    relationship: new Map(),
    recent_event: new Map(),
  };

  for (const item of items) {
    const baseKey = mergeKeyOf(item) || item.id;
    const key = `${item.scope}:${item.owner_pc_id || ""}:${item.scope_id || ""}:${baseKey}`;
    const bucket = mapByKind[item.kind];
    const existing = bucket.get(key);
    if (existing) existing.push(item);
    else bucket.set(key, [item]);
  }

  const result: Record<MemoryKind, MemoryCluster[]> = {
    autobiography: [],
    secret: [],
    relationship: [],
    recent_event: [],
  };

  for (const kind of ALL_KINDS) {
    const clusters: MemoryCluster[] = [];
    for (const [key, members] of mapByKind[kind].entries()) {
      const sortedMembers = [...members].sort((a, b) => {
        if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
        if (a.score !== b.score) return b.score - a.score;
        return compareTimestampDesc(a.updated_at, b.updated_at);
      });
      clusters.push({
        key,
        kind,
        representative: sortedMembers[0],
        members: sortedMembers,
      });
    }

    clusters.sort((a, b) => {
      const ra = a.representative;
      const rb = b.representative;
      if (ra.pinned !== rb.pinned) return ra.pinned ? -1 : 1;
      if (ra.score !== rb.score) return rb.score - ra.score;
      return compareTimestampDesc(ra.updated_at, rb.updated_at);
    });
    result[kind] = clusters;
  }

  return result;
}

export default function MemoryDebuggerModal(props: {
  open: boolean;
  onClose: () => void;
  pcs: PcOption[];
}) {
  const [selection, setSelection] = useState<SourceSelection>({ scope: "public" });
  const [items, setItems] = useState<MemoryEntry[]>([]);
  const [turnNo, setTurnNo] = useState<number | null>(null);
  const [decayInfo, setDecayInfo] = useState<{ interval_ticks: number; k: number; threshold: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailsKey, setDetailsKey] = useState<string | null>(null);
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [showDeleted, setShowDeleted] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<{ key: string; memoryId: string; summary: string; content: string; pinned: boolean } | null>(null);
  const [editSizing, setEditSizing] = useState<{ key: string; summaryHeight: number; contentHeight: number } | null>(null);

  const summaryRefByKey = useRef<Map<string, HTMLDivElement | null>>(new Map());
  const contentRefByKey = useRef<Map<string, HTMLDivElement | null>>(new Map());

  const loadMemories = useCallback(async () => {
    if (!props.open) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getMemories({
        scope: selection.scope,
        ownerPcId: selection.scope === "pc" ? selection.pcId : null,
        pinned: pinnedOnly ? true : null,
        deleted: showDeleted,
        limit: 200,
      });
      setItems(res.items || []);
      setTurnNo(res.turn_no ?? null);
      setDecayInfo(res.decay ?? null);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, [props.open, selection, pinnedOnly, showDeleted]);

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
    if (selection.scope !== "pc") return;
    if (props.pcs.some((pc) => pc.id === selection.pcId)) return;
    const fallback = props.pcs[0]?.id;
    if (fallback) setSelection({ scope: "pc", pcId: fallback });
    else setSelection({ scope: "public" });
  }, [props.open, props.pcs, selection]);

  const clustersByKind = useMemo(() => clusterItems(items), [items]);

  const selectedLabel = useMemo(() => {
    if (selection.scope === "pc") {
      return `PC · ${findPcName(props.pcs, selection.pcId)}`;
    }
    return scopeLabel(selection.scope);
  }, [props.pcs, selection]);

  const cancelEdit = useCallback(() => {
    setEditingKey(null);
    setEditDraft(null);
    setEditSizing(null);
  }, []);

  const beginEdit = useCallback(
    (key: string, memory: MemoryEntry) => {
      const summaryHeight = summaryRefByKey.current.get(key)?.getBoundingClientRect().height || 0;
      const contentHeight = contentRefByKey.current.get(key)?.getBoundingClientRect().height || 0;
      setEditingKey(key);
      setEditDraft({ key, memoryId: memory.id, summary: memory.summary, content: memory.content, pinned: memory.pinned });
      setEditSizing({ key, summaryHeight, contentHeight });
    },
    []
  );

  const saveEdit = useCallback(async () => {
    if (!editDraft) return;
    const summary = editDraft.summary.trim();
    const content = editDraft.content.trim();
    if (!summary || !content) {
      setError("请填写摘要和正文");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await patchMemory(editDraft.memoryId, { summary, content, pinned: editDraft.pinned });
      setItems((prev) => prev.map((it) => (it.id === updated.id ? updated : it)));
      cancelEdit();
      await loadMemories();
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setSaving(false);
    }
  }, [cancelEdit, editDraft, loadMemories]);

  const togglePin = useCallback(async (key: string, memory: MemoryEntry) => {
    const nextPinned = editDraft && editDraft.key === key ? !editDraft.pinned : !memory.pinned;
    setSaving(true);
    setError(null);
    try {
      const updated = await patchMemory(memory.id, { pinned: nextPinned });
      setItems((prev) => prev.map((it) => (it.id === updated.id ? updated : it)));
      if (editDraft?.key === key && editDraft.memoryId === updated.id) {
        setEditDraft((prev) => (prev ? { ...prev, pinned: updated.pinned } : prev));
      }
      if (pinnedOnly) {
        if (editDraft?.key === key && editDraft.memoryId === updated.id && !updated.pinned) {
          cancelEdit();
        }
        await loadMemories();
      }
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setSaving(false);
    }
  }, [cancelEdit, editDraft, loadMemories, pinnedOnly]);

  const removeMemory = useCallback(
    async (memory: MemoryEntry) => {
      if (!window.confirm(`确认软删除这条记忆？\n\n${memory.summary}`)) return;
      setSaving(true);
      setError(null);
      try {
        if (editDraft?.memoryId === memory.id) {
          cancelEdit();
        }
        await deleteMemory(memory.id);
        await loadMemories();
      } catch (e: any) {
        setError(String(e?.message || e));
      } finally {
        setSaving(false);
      }
    },
    [cancelEdit, editDraft, loadMemories]
  );

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
              {selectedLabel}
              {turnNo != null ? ` · turn #${turnNo}` : ""}
              {decayInfo ? ` · decay ${decayInfo.interval_ticks} ticks / k=${decayInfo.k} / threshold=${decayInfo.threshold}` : ""}
              {loading ? " · 加载中…" : ""}
              {saving ? " · 保存中…" : ""}
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
          <section className="memoryPane memorySourcesPane">
            <div className="memoryPaneHead">
              <div className="memoryPaneTitle">来源</div>
              <div className="memoryPaneMeta">public → direct → PC</div>
            </div>

            <div className="memoryPaneScroll">
              <div className="memoryBentoColumn">
                <button
                  type="button"
                  className={`memoryBentoSelect ${selection.scope === "public" ? "active" : ""}`}
                  onClick={() => {
                    setDetailsKey(null);
                    cancelEdit();
                    setSelection({ scope: "public" });
                  }}
                >
                  <div className="memoryBentoSelectTitle">公共</div>
                  <div className="memoryBentoSelectSub">scope=public</div>
                </button>

                <button
                  type="button"
                  className={`memoryBentoSelect ${selection.scope === "direct" ? "active" : ""}`}
                  onClick={() => {
                    setDetailsKey(null);
                    cancelEdit();
                    setSelection({ scope: "direct" });
                  }}
                >
                  <div className="memoryBentoSelectTitle">私聊</div>
                  <div className="memoryBentoSelectSub">scope=direct</div>
                </button>

                <div className="memoryBentoDivider">PC</div>
                <div className="memoryBentoPcGrid">
                  {props.pcs.map((pc) => (
                    <button
                      key={pc.id}
                      type="button"
                      className={`memoryBentoSelect ${selection.scope === "pc" && selection.pcId === pc.id ? "active" : ""}`}
                      onClick={() => {
                        setDetailsKey(null);
                        cancelEdit();
                        setSelection({ scope: "pc", pcId: pc.id });
                      }}
                      title={pc.id}
                    >
                      <div className="memoryBentoSelectTitle">{pc.name}</div>
                      <div className="memoryBentoSelectSub">pc</div>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </section>

          <section className="memoryPane memoryClustersPane">
            <div className="memoryPaneHead">
              <div className="memoryPaneTitle">聚类记忆</div>
              <div className="memoryPaneActions">
                <button
                  type="button"
                  className={`iconBtn iconOnly ${pinnedOnly ? "on" : ""}`}
                  onClick={() => setPinnedOnly((v) => !v)}
                  aria-label={pinnedOnly ? "显示全部" : "仅 pinned"}
                  title={pinnedOnly ? "显示全部" : "仅 pinned"}
                >
                  <Pin size={18} />
                </button>
                <button
                  type="button"
                  className={`iconBtn iconOnly ${showDeleted ? "on" : ""}`}
                  onClick={() => setShowDeleted((v) => !v)}
                  aria-label={showDeleted ? "隐藏已删除" : "显示已删除"}
                  title={showDeleted ? "隐藏已删除" : "显示已删除"}
                >
                  <Trash2 size={18} />
                </button>
              </div>
            </div>

            <div className="memoryPaneScroll">
              <div className="memoryKindStack">
                {ALL_KINDS.map((kind) => {
                  const clusters = clustersByKind[kind] || [];
                  return (
                    <div key={kind} className="memoryKindSection">
                      <div className="memoryKindHeader">
                        <span className={`memoryBadge kind-${kind}`}>{kindLabel(kind)}</span>
                        <span className="memoryKindMeta">
                          {clusters.length} 个盒子 · {clusters.reduce((sum, it) => sum + it.members.length, 0)} 条记忆
                        </span>
                      </div>

                      {clusters.length ? (
                        <div className="memoryBentoGrid">
                          {clusters.map((cluster) => {
                            const mem = cluster.representative;
                            const key = cluster.key;
                            const open = detailsKey === key;
                            const editing = editingKey === key && editDraft?.key === key;
                            const draftSummary = editing ? editDraft.summary : mem.summary;
                            const draftContent = editing ? editDraft.content : mem.content;
                            const draftPinned = editing ? editDraft.pinned : mem.pinned;
                            const mergeKey = mergeKeyOf(mem);
                            const mergedCount = mergedSourcesCountOf(mem);
                            return (
                              <div key={key} className={`memoryBentoCard ${mem.deleted_at ? "deleted" : ""}`}>
                                <div className="memoryBentoTop">
                                  <div className="memoryBentoSummaryRow">
                                    {editing ? (
                                      <textarea
                                        className="memoryBentoTextarea memoryBentoSummaryTextarea"
                                        value={draftSummary}
                                        onChange={(e) => setEditDraft((prev) => (prev ? { ...prev, summary: e.target.value } : prev))}
                                        style={
                                          editSizing?.key === key && editSizing.summaryHeight
                                            ? { height: `${editSizing.summaryHeight}px` }
                                            : undefined
                                        }
                                        aria-label="编辑摘要"
                                      />
                                    ) : (
                                      <div
                                        className="memoryBentoSummary"
                                        title={mem.summary}
                                        ref={(el) => {
                                          summaryRefByKey.current.set(key, el);
                                        }}
                                      >
                                        {mem.summary || "（无摘要）"}
                                      </div>
                                    )}
                                    {cluster.members.length > 1 ? (
                                      <span className="memoryBentoBadge" title="同簇多条">
                                        ×{cluster.members.length}
                                      </span>
                                    ) : null}
                                  </div>
                                  <div className="memoryBentoActions">
                                    <button
                                      type="button"
                                      className={`memoryBentoActionIcon ${draftPinned ? "on" : ""}`}
                                      onClick={() => void togglePin(key, mem)}
                                      aria-label={draftPinned ? "取消置顶" : "置顶"}
                                      title={draftPinned ? "取消置顶" : "置顶"}
                                      disabled={saving}
                                    >
                                      <Pin size={16} />
                                    </button>
                                    {!editing ? (
                                      <button
                                        type="button"
                                        className="memoryBentoActionIcon"
                                        onClick={() => beginEdit(key, mem)}
                                        aria-label="编辑"
                                        title="编辑"
                                        disabled={saving || !!mem.deleted_at}
                                      >
                                        <Edit size={16} />
                                      </button>
                                    ) : (
                                      <button
                                        type="button"
                                        className="memoryBentoActionIcon"
                                        onClick={cancelEdit}
                                        aria-label="取消编辑"
                                        title="取消编辑"
                                        disabled={saving}
                                      >
                                        <X size={16} />
                                      </button>
                                    )}
                                    {editing ? (
                                      <button
                                        type="button"
                                        className="memoryBentoActionIcon on"
                                        onClick={() => void saveEdit()}
                                        aria-label="保存编辑"
                                        title="保存编辑"
                                        disabled={saving || !!mem.deleted_at}
                                      >
                                        <Save size={16} />
                                      </button>
                                    ) : null}
                                    <button
                                      type="button"
                                      className="memoryBentoActionIcon danger"
                                      onClick={() => void removeMemory(mem)}
                                      aria-label="软删除"
                                      title="软删除"
                                      disabled={saving || !!mem.deleted_at}
                                    >
                                      <Trash2 size={16} />
                                    </button>
                                    <button
                                      type="button"
                                      className={`memoryBentoActionIcon ${open ? "on" : ""}`}
                                      onClick={() => setDetailsKey((cur) => (cur === key ? null : key))}
                                      aria-label={open ? "收起详情" : "展开详情"}
                                      title={open ? "收起详情" : "展开详情"}
                                    >
                                      <Info size={16} />
                                    </button>
                                  </div>
                                </div>

                                {editing ? (
                                  <textarea
                                    className="memoryBentoTextarea"
                                    value={draftContent}
                                    onChange={(e) => setEditDraft((prev) => (prev ? { ...prev, content: e.target.value } : prev))}
                                    style={
                                      editSizing?.key === key && editSizing.contentHeight
                                        ? { height: `${editSizing.contentHeight}px` }
                                        : undefined
                                    }
                                    aria-label="编辑正文"
                                  />
                                ) : (
                                  <div
                                    className="memoryBentoContent"
                                    ref={(el) => {
                                      contentRefByKey.current.set(key, el);
                                    }}
                                  >
                                    {mem.content}
                                  </div>
                                )}
                                <div className="memoryBentoFooter">
                                  <span className="memoryBentoMeta">
                                    {mem.scope === "pc" ? `PC · ${findPcName(props.pcs, mem.owner_pc_id)}` : scopeLabel(mem.scope)}
                                  </span>
                                  <span className="memoryBentoMeta">{formatNullableTime(mem.updated_at)}</span>
                                </div>

                                {open ? (
                                  <div className="memoryBentoDetails">
                                    <div className="memoryBentoDetailGrid">
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">id</div>
                                        <div className="memoryBentoDetailValue">{mem.id}</div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">scope</div>
                                        <div className="memoryBentoDetailValue">
                                          {mem.scope}
                                          {mem.scope_id ? <span className="memoryBentoDetailMuted"> · {mem.scope_id}</span> : null}
                                        </div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">owner</div>
                                        <div className="memoryBentoDetailValue">{findPcName(props.pcs, mem.owner_pc_id)}</div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">importance / score</div>
                                        <div className="memoryBentoDetailValue">
                                          {mem.importance} / {mem.score}
                                        </div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">access</div>
                                        <div className="memoryBentoDetailValue">
                                          {mem.access_count}
                                          {mem.last_accessed_at ? <span className="memoryBentoDetailMuted"> · {formatNullableTime(mem.last_accessed_at)}</span> : null}
                                        </div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">created / updated</div>
                                        <div className="memoryBentoDetailValue">
                                          {formatNullableTime(mem.created_at)}{" "}
                                          <span className="memoryBentoDetailMuted">·</span> {formatNullableTime(mem.updated_at)}
                                        </div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">edit_state</div>
                                        <div className="memoryBentoDetailValue">
                                          {mem.edit_state}
                                          {mem.deleted_at ? <span className="memoryBentoDetailMuted"> · deleted</span> : null}
                                        </div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">revision</div>
                                        <div className="memoryBentoDetailValue">{mem.revision}</div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">source_type</div>
                                        <div className="memoryBentoDetailValue">{mem.source_type || "—"}</div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">merge_key</div>
                                        <div className="memoryBentoDetailValue">{mergeKey || "—"}</div>
                                      </div>
                                      <div className="memoryBentoDetailRow">
                                        <div className="memoryBentoDetailLabel">merged_sources</div>
                                        <div className="memoryBentoDetailValue">{mergedCount ? `+${mergedCount}` : "0"}</div>
                                      </div>
                                      {cluster.members.length > 1 ? (
                                        <div className="memoryBentoDetailRow">
                                          <div className="memoryBentoDetailLabel">members</div>
                                          <div className="memoryBentoDetailValue">
                                            {cluster.members.map((m) => m.summary).filter(Boolean).slice(0, 6).join(" · ") || "—"}
                                            {cluster.members.length > 6 ? <span className="memoryBentoDetailMuted"> · …</span> : null}
                                          </div>
                                        </div>
                                      ) : null}
                                    </div>

                                    {mem.meta && Object.keys(mem.meta).length ? (
                                      <pre className="memoryBentoMetaJson">{JSON.stringify(mem.meta, null, 2)}</pre>
                                    ) : (
                                      <div className="memoryHint">meta 为空</div>
                                    )}
                                  </div>
                                ) : null}
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <div className="memoryEmpty">暂无该 kind 记忆</div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        </div>

        {error ? <div className="memoryError">{error}</div> : null}
      </div>
    </div>
  );
}
