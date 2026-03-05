import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import type { LlmLogItem, LlmLogMeta, PcActivityLogItem } from "../types";
import { formatTime } from "../lib/chatUi";
import { getLlmLog, getLlmLogs, getPcActivityLogs } from "../lib/api";
import MarkdownLite from "./MarkdownLite";

export default function PcActivityLogModal(props: {
  open: boolean;
  onClose: () => void;
  pcId?: string | null;
}) {
  const [tab, setTab] = useState<"activity" | "llm">("activity");

  const [items, setItems] = useState<PcActivityLogItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
    return pcId ? `活动日志 · ${pcId}` : "活动日志";
  }, [pcId, tab]);

  const loadPage = useCallback(
    async (cursor: string | null) => {
      const page = await getPcActivityLogs({ pcId, cursor, limit: 50 });
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
                      <div className="activityTime" title={it.timestamp}>
                        {formatTime(it.timestamp)}
                      </div>
                      <div className="activitySummary">{it.summary}</div>
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
