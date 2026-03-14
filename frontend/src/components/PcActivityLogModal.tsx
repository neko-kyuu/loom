import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Wrench, X } from "lucide-react";
import type { LlmLogItem, LlmLogMeta, TickLogItem } from "../types";
import { formatTime } from "../lib/chatUi";
import { getForumThreads, getLlmLog, getLlmLogs, getSettingsState, getTicks } from "../lib/api";
import MarkdownLite from "./MarkdownLite";
import { chatDisplayName, parseProfilesPayload, type ProfilesState } from "../lib/profiles";
import { parseChannelsStatePayload, type ChannelsState } from "../lib/channels";
import type { Actor, ForumThread } from "../types";

export default function PcActivityLogModal(props: {
  open: boolean;
  onClose: () => void;
  pcId?: string | null;
}) {
  const [tab, setTab] = useState<"activity" | "llm">("activity");

  const [items, setItems] = useState<TickLogItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [profilesState, setProfilesState] = useState<ProfilesState>({ byId: {} });
  const [channelsState, setChannelsState] = useState<ChannelsState>({ broadcast: { description: "" }, forums: [] });
  const [threadById, setThreadById] = useState<Record<string, ForumThread>>({});

  const [llmItems, setLlmItems] = useState<LlmLogMeta[]>([]);
  const [llmLoading, setLlmLoading] = useState(false);
  const [llmError, setLlmError] = useState<string | null>(null);
  const [selectedLlmId, setSelectedLlmId] = useState<string | null>(null);
  const [selectedLlm, setSelectedLlm] = useState<LlmLogItem | null>(null);
  const [llmDetailLoading, setLlmDetailLoading] = useState(false);
  const [llmDetailError, setLlmDetailError] = useState<string | null>(null);

  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const pcId = (props.pcId || "").trim() || null;

  const title = useMemo(() => {
    if (tab === "llm") return "LLM 日志";
    return pcId ? `行动审计 · ${pcId}` : "行动审计";
  }, [pcId, tab]);

  const loadPage = useCallback(
    async (cursor: string | null) => {
      const page = await getTicks({ pcId, cursor, limit: 50 });
      return page;
    },
    [pcId]
  );

  const loadMore = useCallback(async () => {
    if (!props.open) return;
    if (tab !== "activity") return;
    if (loading) return;
    if (!hasMore) return;
    setLoading(true);
    setError(null);
    try {
      const page = await loadPage(nextCursor);
      setItems((prev) => prev.concat(page.items || []));
      setNextCursor(page.next_cursor || null);
      setHasMore(Boolean(page.next_cursor) && (page.items || []).length > 0);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, [props.open, tab, loading, hasMore, nextCursor, loadPage]);

  useEffect(() => {
    if (!props.open) return;
    setTab("activity");
  }, [props.open]);

  useEffect(() => {
    if (!props.open) return;
    if (tab !== "activity") return;
    setItems([]);
    setNextCursor(null);
    setHasMore(true);
    setError(null);
    setExpandedId(null);
    setLoading(true);
    void (async () => {
      try {
        const page = await loadPage(null);
        setItems(page.items || []);
        setNextCursor(page.next_cursor || null);
        setHasMore(Boolean(page.next_cursor) && (page.items || []).length > 0);
      } catch (e: any) {
        setError(String(e?.message || e));
      } finally {
        setLoading(false);
      }
    })();
  }, [props.open, pcId, tab]);

  useEffect(() => {
    if (!props.open) return;
    if (tab !== "activity") return;
    void (async () => {
      try {
        const settings = await getSettingsState();
        setProfilesState(parseProfilesPayload(settings.profiles_state));
        const ch = parseChannelsStatePayload(settings.channels_state);
        setChannelsState(ch ?? { broadcast: { description: "" }, forums: [] });
      } catch {
        // ignore: audit UI falls back to ids
      }

      try {
        const res = await getForumThreads({ limit: 2000 });
        const byId: Record<string, ForumThread> = {};
        for (const t of res.items || []) {
          if (t && typeof t.id === "string" && t.id.trim()) byId[t.id] = t;
        }
        setThreadById(byId);
      } catch {
        // ignore
      }
    })();
  }, [props.open, tab]);

  const pcName = useCallback(
    (id: string) => {
      const pid = (id || "").trim();
      if (!pid) return "";
      const actor: Actor = { kind: pid === "dm" ? "dm" : "pc", id: pid, name: pid === "dm" ? "DM" : pid };
      return chatDisplayName(profilesState, actor);
    },
    [profilesState]
  );

  const forumTitleById = useMemo(() => {
    const out: Record<string, string> = {};
    for (const f of channelsState.forums || []) {
      if (!f || typeof f.id !== "string") continue;
      const title = typeof f.title === "string" ? f.title.trim() : "";
      if (f.id.trim() && title) out[f.id.trim()] = title;
    }
    return out;
  }, [channelsState]);

  const threadLabel = useCallback(
    (threadId: string) => {
      const tid = (threadId || "").trim();
      if (!tid) return "";
      const t = threadById[tid];
      if (!t) return `thread=${tid}`;
      const forumTitle = forumTitleById[(t.channel_id || "").trim()] || (t.channel_id || "").trim() || "#forum";
      const tt = (t.title || "").trim();
      return `${forumTitle} · ${tt || tid}`;
    },
    [threadById, forumTitleById]
  );

  const finalAction = useCallback((raw: any): any | null => {
    if (!raw || typeof raw !== "object") return null;
    if (raw.action && typeof raw.action === "object") return raw.action;
    return raw;
  }, []);

  const toolAudit = useCallback((raw: any): { tools: { name: string; args: any }[] } => {
    const empty = { tools: [] as { name: string; args: any }[] };
    if (!raw || typeof raw !== "object") return empty;
    const audit = (raw as any).audit;
    if (!audit || typeof audit !== "object") return empty;
    const tools = (audit as any).tools;
    if (!Array.isArray(tools)) return empty;
    const cleaned = tools
      .map((it: any) => {
        const name = typeof it?.name === "string" ? it.name : "";
        const args = it?.args;
        return name ? { name, args } : null;
      })
      .filter(Boolean) as { name: string; args: any }[];
    return { tools: cleaned };
  }, []);

  const describeAction = useCallback((act: any): { title: string; detail?: string; snippet?: string } => {
    if (!act || typeof act !== "object") return { title: "unknown" };
    const t = typeof act.type === "string" ? act.type : "unknown";
    if (t === "create_thread") {
      const title = typeof act.title === "string" ? act.title : "";
      const channelId = typeof act.channel_id === "string" ? act.channel_id : "";
      const forumTitle = forumTitleById[channelId] || channelId || "#forum";
      return { title: "create_thread", detail: title ? `${forumTitle} · ${title}` : forumTitle };
    }
    if (t === "reply") {
      const threadId = typeof act.thread_id === "string" ? act.thread_id : "";
      const content = typeof act.content === "string" ? act.content.trim() : "";
      const snippet = content ? content.slice(0, 120) + (content.length > 120 ? "…" : "") : "";
      const target = threadId ? threadLabel(threadId) : "";
      return { title: "reply", detail: target || undefined, snippet: snippet || undefined };
    }
    if (t === "dm") {
      const to = typeof act.to_pc_id === "string" ? act.to_pc_id : "";
      const content = typeof act.content === "string" ? act.content.trim() : "";
      const snippet = content ? content.slice(0, 120) + (content.length > 120 ? "…" : "") : "";
      const toName = to ? `@${pcName(to) || to}` : "@DM";
      return { title: "dm", detail: toName, snippet: snippet || undefined };
    }
    if (t === "noop") {
      const reason = typeof act.reason === "string" ? act.reason.trim() : "";
      return { title: "noop", detail: reason || undefined };
    }
    return { title: t };
  }, [forumTitleById, pcName, threadLabel]);

  const trimLine = useCallback((raw: any, maxLen: number) => {
    const text = String(raw ?? "").replace(/\s+/g, " ").trim();
    if (!text) return "";
    if (text.length <= maxLen) return text;
    return text.slice(0, maxLen) + "…";
  }, []);

  const prettyArgs = useCallback((value: any) => {
    if (value === null || value === undefined) return "";
    try {
      const text = JSON.stringify(value, null, 2);
      if (text.length <= 2400) return text;
      return text.slice(0, 2400) + "\n…";
    } catch {
      const s = String(value);
      if (s.length <= 2400) return s;
      return s.slice(0, 2400) + "…";
    }
  }, []);

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
    if (tab !== "activity") return;
    const root = scrollerRef.current;
    const sentinel = sentinelRef.current;
    if (!root || !sentinel) return;

    const obs = new IntersectionObserver(
      (ents) => {
        if (ents.some((e) => e.isIntersecting)) void loadMore();
      },
      { root, rootMargin: "240px 0px 240px 0px", threshold: 0.01 }
    );
    obs.observe(sentinel);
    return () => obs.disconnect();
  }, [props.open, tab, loadMore]);

  const loadLlmList = useCallback(async () => {
    if (!props.open) return;
    setLlmLoading(true);
    setLlmError(null);
    try {
      const res = await getLlmLogs({ limit: 100 });
      const next = res.items || [];
      setLlmItems(next);
      setSelectedLlmId((cur) => cur || (next[0]?.id || null));
    } catch (e: any) {
      setLlmError(String(e?.message || e));
    } finally {
      setLlmLoading(false);
    }
  }, [props.open]);

  useEffect(() => {
    if (!props.open) return;
    if (tab !== "llm") return;
    void loadLlmList();
  }, [props.open, tab, loadLlmList]);

  useEffect(() => {
    if (!props.open) return;
    if (tab !== "llm") return;
    if (!selectedLlmId) {
      setSelectedLlm(null);
      return;
    }
    setSelectedLlm(null);
    setLlmDetailLoading(true);
    setLlmDetailError(null);
    void (async () => {
      try {
        const res = await getLlmLog(selectedLlmId);
        setSelectedLlm(res.item || null);
      } catch (e: any) {
        setSelectedLlm(null);
        setLlmDetailError(String(e?.message || e));
      } finally {
        setLlmDetailLoading(false);
      }
    })();
  }, [props.open, tab, selectedLlmId]);

  const prettyJson = useCallback((raw: string | null | undefined) => {
    const text = (raw || "").trim();
    if (!text) return "";
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      return raw || "";
    }
  }, []);

  const extractContentMarkdown = useCallback((raw: string | null | undefined) => {
    const text = (raw || "").trim();
    if (!text) return "";
    let root: any;
    try {
      root = JSON.parse(text);
    } catch {
      return "";
    }

    const contents: string[] = [];
    const walk = (node: any) => {
      if (!node) return;
      if (Array.isArray(node)) {
        for (const v of node) walk(v);
        return;
      }
      if (typeof node !== "object") return;
      for (const [k, v] of Object.entries(node)) {
        if (k === "content" && typeof v === "string") {
          const s = v;
          if (s.trim()) contents.push(s);
          continue;
        }
        walk(v);
      }
    };
    walk(root);

    if (!contents.length) return "";
    if (contents.length === 1) return contents[0];
    return contents.slice(0, 12).join("\n\n---\n\n");
  }, []);

  const jsonOrContentPreview = useCallback(
    (raw: string | null | undefined) => {
      const content = extractContentMarkdown(raw);
      if (content) return content;
      return `\`\`\`json\n${prettyJson(raw)}\n\`\`\``;
    },
    [extractContentMarkdown, prettyJson]
  );

  if (!props.open) return null;
  return (
    <div className="activityOverlay" role="presentation" onClick={props.onClose}>
      <div
        className="activityModal"
        role="dialog"
        aria-label={title}
        onClick={(e) => {
          e.stopPropagation();
        }}
      >
        <div className="activityHeader">
          <div className="activityHeaderLeft">
            <div className="activityTitle">{title}</div>
            <div className="activityTabs" role="tablist" aria-label="日志类型">
              <button
                type="button"
                className={`activityTab ${tab === "activity" ? "active" : ""}`}
                role="tab"
                aria-selected={tab === "activity"}
                onClick={() => setTab("activity")}
              >
                活动
              </button>
              <button
                type="button"
                className={`activityTab ${tab === "llm" ? "active" : ""}`}
                role="tab"
                aria-selected={tab === "llm"}
                onClick={() => setTab("llm")}
              >
                LLM
              </button>
            </div>
          </div>
          <button className="iconBtn iconOnly" onClick={props.onClose} aria-label="关闭" title="关闭">
            <X size={18} />
          </button>
        </div>

        <div className={`activityBody ${tab === "llm" ? "llmMode" : ""}`} ref={scrollerRef}>
          {tab === "activity" ? (
            <>
              {items.length ? (
                <div className="activityList">
                  {items.map((it) => (
                    <div className="activityRow" key={it.id}>
                      <div className="activityTime" title={it.started_at}>
                        {formatTime(it.started_at)}
                      </div>
                      <div className="activitySummary">
                        {(() => {
                          const act = finalAction(it.action);
                          const audit = toolAudit(it.action);
                          const desc = describeAction(act);
                          const isOpen = expandedId === it.id;
                          const toolsCount = audit.tools.length;
                          return (
                            <>
                              <div className="activityRowHead">
                                <div className="activityRowMain">
                                  <div className="activityRowTitle">
                                    {pcName(it.pc_id) || it.pc_id} · {desc.title}
                                  </div>
                                  <div className="activityRowSub">
                                    {desc.detail ? <span>{desc.detail}</span> : null}
                                    {desc.snippet ? <span className="activityRowSnippet">{desc.snippet}</span> : null}
                                    <span>
                                      {typeof it.duration_ms === "number" ? `${it.duration_ms}ms` : ""}
                                      {it.status ? ` · ${it.status}` : ""}
                                      {toolsCount ? ` · tools=${toolsCount}` : " · tools=0"}
                                    </span>
                                    {it.error ? (
                                      <span className="activityRowError">错误：{trimLine(it.error, 360)}</span>
                                    ) : null}
                                  </div>
                                </div>

                                <button
                                  type="button"
                                  className="iconBtn iconOnly"
                                  aria-label={isOpen ? "收起审计" : "展开审计"}
                                  title={isOpen ? "收起审计" : "展开审计"}
                                  onClick={() => setExpandedId((cur) => (cur === it.id ? null : it.id))}
                                >
                                  {isOpen ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
                                </button>
                              </div>

                              {isOpen ? (
                                <div className="activityAudit">
                                  <div className="auditHead">
                                    <div className="auditTitle">
                                      <Wrench size={14} /> 工具调用
                                    </div>
                                  </div>
                                  {audit.tools.length ? (
                                    <div className="auditTools">
                                      {audit.tools.map((t, idx) => (
                                        <div className="auditTool" key={`${it.id}:${idx}:${t.name}`}>
                                          <div className="auditToolName">{t.name}</div>
                                          <pre className="auditToolArgs">{prettyArgs(t.args)}</pre>
                                        </div>
                                      ))}
                                    </div>
                                  ) : (
                                    <div className="auditEmpty">本次 action 未使用工具。</div>
                                  )}
                                </div>
                              ) : null}
                            </>
                          );
                        })()}
                      </div>
                    </div>
                  ))}
                </div>
              ) : loading ? (
                <div className="activityEmpty">加载中…</div>
              ) : (
                <div className="activityEmpty">暂无日志</div>
              )}

              {error ? <div className="error">加载失败：{error}</div> : null}
              {loading && items.length ? <div className="activityHint">加载中…</div> : null}
              {!loading && !hasMore && items.length ? <div className="activityHint">已到底</div> : null}
              <div ref={sentinelRef} style={{ height: 1 }} />
            </>
          ) : (
            <div className="llmShell">
              <div className="llmList">
                <div className="llmListHead">
                  <div className="llmListTitle">请求列表</div>
                  <button
                    type="button"
                    className="smallBtn"
                    onClick={() => {
                      void loadLlmList();
                    }}
                    disabled={llmLoading}
                  >
                    刷新
                  </button>
                </div>
                {llmItems.length ? (
                  <div className="llmListInner">
                    {llmItems.map((it) => {
                      const active = it.id === selectedLlmId;
                      const status = it.error ? "ERR" : it.status_code ? String(it.status_code) : "";
                      return (
                        <button
                          key={it.id}
                          type="button"
                          className={`llmRow ${active ? "active" : ""}`}
                          onClick={() => setSelectedLlmId(it.id)}
                          title={it.id}
                        >
                          <div className="llmRowTop">
                            <div className="llmTime">{formatTime(it.created_at)}</div>
                            <div className={`llmStatus ${it.error ? "err" : ""}`}>{status}</div>
                          </div>
                          <div className="llmRowSub">
                            <div className="llmModel">{it.model || "?"}</div>
                            <div className="llmDur">{typeof it.duration_ms === "number" ? `${it.duration_ms}ms` : ""}</div>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                ) : llmLoading ? (
                  <div className="activityEmpty">加载中…</div>
                ) : (
                  <div className="activityEmpty">暂无日志</div>
                )}
                {llmError ? <div className="error">加载失败：{llmError}</div> : null}
              </div>

              <div className="llmDetail">
                {selectedLlm ? (
                  <>
                    <div className="llmDetailHead">
                      <div className="llmDetailTitle">{selectedLlm.model || "LLM"}</div>
                      <div className="llmDetailMeta">
                        <span title={selectedLlm.created_at}>{formatTime(selectedLlm.created_at)}</span>
                        <span>{selectedLlm.status_code ? `HTTP ${selectedLlm.status_code}` : ""}</span>
                        <span>{typeof selectedLlm.duration_ms === "number" ? `${selectedLlm.duration_ms}ms` : ""}</span>
                      </div>
                    </div>

                    {selectedLlm.error ? <div className="llmErr">错误：{selectedLlm.error}</div> : null}
                    {llmDetailError ? <div className="error">加载失败：{llmDetailError}</div> : null}
                    {llmDetailLoading ? <div className="activityEmpty">加载中…</div> : null}

                    <div className="llmBlocks">
                      <div className="llmBlock">
                        <div className="llmBlockTitle">request_json</div>
                        <MarkdownLite className="requestRoot" source={jsonOrContentPreview(selectedLlm.request_json)} />
                      </div>
                      <div className="llmBlock">
                        <div className="llmBlockTitle">response_json</div>
                        <MarkdownLite source={`\`\`\`json\n${prettyJson(selectedLlm.response_json || "")}\n\`\`\``} />
                      </div>
                    </div>
                  </>
                ) : llmDetailLoading ? (
                  <div className="activityEmpty">加载中…</div>
                ) : llmDetailError ? (
                  <div className="error">加载失败：{llmDetailError}</div>
                ) : selectedLlmId ? (
                  <div className="activityEmpty">暂无详情</div>
                ) : (
                  <div className="activityEmpty">请选择一条日志</div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
