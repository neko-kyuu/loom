import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import type { PcActivityLogItem } from "../types";
import { formatTime } from "../lib/chatUi";
import { getPcActivityLogs } from "../lib/api";

export default function PcActivityLogModal(props: {
  open: boolean;
  onClose: () => void;
  pcId?: string | null;
}) {
  const [items, setItems] = useState<PcActivityLogItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const pcId = (props.pcId || "").trim() || null;

  const title = useMemo(() => {
    return pcId ? `活动日志 · ${pcId}` : "活动日志";
  }, [pcId]);

  const loadPage = useCallback(
    async (cursor: string | null) => {
      const page = await getPcActivityLogs({ pcId, cursor, limit: 50 });
      return page;
    },
    [pcId]
  );

  const loadMore = useCallback(async () => {
    if (!props.open) return;
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
  }, [props.open, loading, hasMore, nextCursor, loadPage]);

  useEffect(() => {
    if (!props.open) return;
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
  }, [props.open, pcId]);

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
  }, [props.open, loadMore]);

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
          <div className="activityTitle">{title}</div>
          <button className="iconBtn iconOnly" onClick={props.onClose} aria-label="关闭" title="关闭">
            <X size={18} />
          </button>
        </div>

        <div className="activityBody" ref={scrollerRef}>
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
        </div>
      </div>
    </div>
  );
}
