import { useEffect, useMemo, useRef, useState } from "react";
import type { Conversation, ForumThread, Message, WsServerToClient } from "./types";
import { createWs } from "./lib/ws";
import { Hash, List, MessageCircle, Settings, Pause, Play, Trash2, X } from "lucide-react";
import SettingsModal, { type SettingsTabId } from "./components/SettingsModal";
import AppearanceTab from "./components/settings/AppearanceTab";
import ChannelsTab from "./components/settings/ChannelsTab";
import ProfileTab from "./components/settings/ProfileTab";
import ProfileNavList from "./components/settings/ProfileNavList";
import ProfileCard from "./components/ProfileCard";
import ChatFlow from "./components/ChatFlow";
import {
  applyAppearance,
  getInitialAppearanceState,
  persistAppearanceState,
  parseAppearanceStatePayload,
  serializeAppearanceState,
  type Appearance,
  type AppearanceState,
  type CustomTheme
} from "./lib/appearance";
import type { Actor } from "./types";
import type { ProfilesState } from "./lib/profiles";
import {
  chatDisplayName,
  ensureProfiles,
  getProfile,
  loadProfiles,
  parseProfilesPayload,
  persistProfiles,
  statusDotColor
} from "./lib/profiles";
import { avatarLabel, formatTime } from "./lib/chatUi";
import {
  absoluteAssetUrl,
  deleteForumThread,
  getSettingsState,
  putAppearanceState,
  putChannelsState,
  putProfilesState,
  wsUrl
} from "./lib/api";
import {
  loadChannelsState,
  parseChannelsStatePayload,
  persistChannelsState,
  type ChannelsState
} from "./lib/channels";

function conversationLabel(c: Conversation, profiles: ProfilesState) {
  if (c.kind === "broadcast") return "#broadcast";
  if (c.kind === "forum") return c.title;
  if (c.kind === "dm_to_pc") {
    const pc = c.participants.find((p) => p.kind === "pc");
    if (!pc) return c.title;
    const name = chatDisplayName(profiles, pc);
    return name ? `@${name}` : c.title;
  }
  if (c.kind === "pc_to_pc") {
    const pcs = c.participants.filter((p) => p.kind === "pc");
    if (!pcs.length) return c.title;
    const names = pcs.map((p) => `@${chatDisplayName(profiles, p)}`);
    return names.join(" ↔ ");
  }
  return c.title;
}

function conversationIcon(c: Conversation) {
  if (c.kind === "broadcast") return <Hash size={16} />;
  if (c.kind === "forum") return <List size={16} />;
  return <MessageCircle size={16} />;
}

function buildDmTargetsByBatchId(messagesByConversation: Record<string, Message[]>): Record<string, string[]> {
  const byBatch: Record<string, Set<string>> = {};
  for (const msgs of Object.values(messagesByConversation)) {
    for (const m of msgs) {
      const batchId = m.send_batch_id;
      if (!batchId) continue;
      const pcIds = (m.to || [])
        .filter((a) => a.kind === "pc" && Boolean(a.id))
        .map((a) => a.id!)
        .filter(Boolean);
      if (!pcIds.length) continue;
      if (!byBatch[batchId]) byBatch[batchId] = new Set<string>();
      for (const pcId of pcIds) byBatch[batchId].add(pcId);
    }
  }
  const out: Record<string, string[]> = {};
  for (const [batchId, set] of Object.entries(byBatch)) out[batchId] = [...set.values()];
  return out;
}

export default function App() {
  const [connected, setConnected] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [messagesByConv, setMessagesByConv] = useState<Record<string, Message[]>>({});
  const [dmTargetsByBatchId, setDmTargetsByBatchId] = useState<Record<string, string[]>>({});
  const [pendingScroll, setPendingScroll] = useState<{ conversationId: string; sendBatchId: string } | null>(null);
  const [activeConvId, setActiveConvId] = useState<string>("broadcast");
  const [typingByConv, setTypingByConv] = useState<Record<string, Set<string>>>({});
  const [queueState, setQueueState] = useState<{ paused: boolean; queued: number } | null>(null);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTabId>("appearance");
  const [profileSettingsSelectedId, setProfileSettingsSelectedId] = useState<string>("user");

  const [appearanceInit] = useState(() => getInitialAppearanceState());
  const [customThemes, setCustomThemes] = useState<CustomTheme[]>(appearanceInit.customThemes);
  const [appearance, setAppearance] = useState<Appearance>(appearanceInit.appearance);

  const [profilesInit] = useState<ProfilesState>(() => loadProfiles());
  const [profiles, setProfiles] = useState<ProfilesState>(profilesInit);
  const [profileViewer, setProfileViewer] = useState<{ actor: Actor; anchor: { x: number; y: number } | null } | null>(null);
  const [remoteSyncDone, setRemoteSyncDone] = useState(false);

  const [channelsInit] = useState<ChannelsState>(() => loadChannelsState());
  const [channels, setChannels] = useState<ChannelsState>(channelsInit);

  const [activeForumThreadId, setActiveForumThreadId] = useState<string | null>(null);
  const [forumDetailOnly, setForumDetailOnly] = useState(false);
  const [forumThreadsByChannel, setForumThreadsByChannel] = useState<Record<string, ForumThread[]>>({});
  const [forumPostsByThread, setForumPostsByThread] = useState<Record<string, Message[]>>({});
  const [threadComposerContent, setThreadComposerContent] = useState("");
  const [threadUiError, setThreadUiError] = useState<string | null>(null);

  const [userName] = useState("You");
  const [content, setContent] = useState("");
  const [uiError, setUiError] = useState<string | null>(null);

  const [directSelectedPcIds, setDirectSelectedPcIds] = useState<string[]>([]);
  const [directSelectionTouched, setDirectSelectionTouched] = useState(false);
  const [threadDirectSelectedPcIds, setThreadDirectSelectedPcIds] = useState<string[]>([]);
  const [threadDirectSelectionTouched, setThreadDirectSelectionTouched] = useState(false);

  const wsRef = useRef<ReturnType<typeof createWs> | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const threadEndRef = useRef<HTMLDivElement | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const saveProfilesTimerRef = useRef<number | null>(null);
  const saveChannelsTimerRef = useRef<number | null>(null);

  useEffect(() => {
    applyAppearance(appearance, customThemes);
    persistAppearanceState({ appearance, customThemes });
  }, [appearance, customThemes, remoteSyncDone]);

  const pcs = useMemo(() => {
    const byId = new Map<string, { id: string; name: string }>();
    for (const conv of conversations) {
      for (const p of conv.participants) {
        if (p.kind === "pc" && p.id) byId.set(p.id, { id: p.id, name: p.name || p.id });
      }
    }
    const pcOrder = (id: string) => {
      const m = id.match(/(\d+)\s*$/);
      return m ? Number(m[1]) : Number.POSITIVE_INFINITY;
    };
    return [...byId.values()].sort((a, b) => {
      const ao = pcOrder(a.id);
      const bo = pcOrder(b.id);
      if (ao !== bo) return ao - bo;
      return a.id.localeCompare(b.id);
    });
  }, [conversations]);

  const profileActors = useMemo(() => {
    const actors: Actor[] = [{ kind: "user", id: "user", name: userName }, { kind: "dm", id: "dm", name: "DM" }];
    for (const pc of pcs) actors.push({ kind: "pc", id: pc.id, name: pc.name });
    return actors;
  }, [pcs, userName]);

  useEffect(() => {
    setProfiles((prev) => ensureProfiles(prev, profileActors));
  }, [profileActors]);

  useEffect(() => {
    const pcIds = new Set(profileActors.filter((a) => a.kind === "pc" && a.id).map((a) => a.id as string));
    if (profileSettingsSelectedId === "user") return;
    if (profileSettingsSelectedId === "dm") return;
    if (pcIds.has(profileSettingsSelectedId)) return;
    setProfileSettingsSelectedId("user");
  }, [profileActors, profileSettingsSelectedId]);

  useEffect(() => {
    persistProfiles(profiles);
  }, [profiles]);

  useEffect(() => {
    persistChannelsState(channels);
  }, [channels]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const remote = await getSettingsState();
        if (cancelled) return;
        if (remote.appearance_state) {
          const parsed = parseAppearanceStatePayload(remote.appearance_state);
          if (parsed) {
            setAppearance(parsed.appearance);
            setCustomThemes(parsed.customThemes);
          }
        }
        if (remote.profiles_state) {
          const parsed = parseProfilesPayload(remote.profiles_state);
          setProfiles((prev) => ({ byId: { ...prev.byId, ...parsed.byId } }));
        }
        if (remote.channels_state) {
          const parsed = parseChannelsStatePayload(remote.channels_state);
          if (parsed) setChannels(parsed);
        }
      } catch {
        // ignore (fallback to localStorage)
      } finally {
        if (!cancelled) setRemoteSyncDone(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!remoteSyncDone) return;
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => {
      const payload: AppearanceState = serializeAppearanceState({ appearance, customThemes });
      void putAppearanceState(payload).catch(() => {
        // ignore
      });
    }, 600);
    return () => {
      if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    };
  }, [appearance, customThemes]);

  useEffect(() => {
    if (!remoteSyncDone) return;
    if (saveProfilesTimerRef.current) window.clearTimeout(saveProfilesTimerRef.current);
    saveProfilesTimerRef.current = window.setTimeout(() => {
      void putProfilesState({ profiles: Object.values(profiles.byId) })
        .then(() => {
          wsRef.current?.send({ type: "request_state" });
        })
        .catch(() => {
          // ignore
        });
    }, 600);
    return () => {
      if (saveProfilesTimerRef.current) window.clearTimeout(saveProfilesTimerRef.current);
    };
  }, [profiles, remoteSyncDone]);

  useEffect(() => {
    if (!remoteSyncDone) return;
    if (saveChannelsTimerRef.current) window.clearTimeout(saveChannelsTimerRef.current);
    saveChannelsTimerRef.current = window.setTimeout(() => {
      void putChannelsState(channels)
        .then(() => {
          wsRef.current?.send({ type: "request_state" });
        })
        .catch(() => {
          // ignore
        });
    }, 600);
    return () => {
      if (saveChannelsTimerRef.current) window.clearTimeout(saveChannelsTimerRef.current);
    };
  }, [channels, remoteSyncDone]);

  const pcIndex = useMemo(() => {
    const byId = new Map<string, { id: string; name: string }>();
    const byName = new Map<string, { id: string; name: string }>();
    for (const pc of pcs) {
      byId.set(pc.id.toLowerCase(), pc);
      byName.set(pc.name.toLowerCase(), pc);
    }
    return { byId, byName };
  }, [pcs]);

  const channelConversations = useMemo(() => conversations.filter((c) => c.kind === "broadcast" || c.kind === "forum"), [conversations]);
  const directConversations = useMemo(() => conversations.filter((c) => c.kind !== "broadcast" && c.kind !== "forum"), [conversations]);

  function jumpToDm(pcId: string, sendBatchId: string) {
    const convId = `dm_to_${pcId}`;
    setActiveConvId(convId);
    setPendingScroll({ conversationId: convId, sendBatchId });
  }

  useEffect(() => {
    const { ws, send } = createWs(wsUrl(), (msg: WsServerToClient) => {
      if (msg.type === "state") {
        setConversations(msg.payload.conversations);
        setMessagesByConv(msg.payload.messages_by_conversation);
        setDmTargetsByBatchId(buildDmTargetsByBatchId(msg.payload.messages_by_conversation));
        setForumThreadsByChannel(msg.payload.forum_threads_by_channel || {});
        setForumPostsByThread(msg.payload.forum_posts_by_thread || {});
        return;
      }
      if (msg.type === "message") {
        const m = msg.payload;
        setMessagesByConv((prev) => {
          const next = { ...prev };
          const list = next[m.conversation_id] ? [...next[m.conversation_id]] : [];
          list.push(m);
          next[m.conversation_id] = list;
          return next;
        });
        if (m.send_batch_id) {
          const pcIds = (m.to || [])
            .filter((a) => a.kind === "pc" && Boolean(a.id))
            .map((a) => a.id!)
            .filter(Boolean);
          if (pcIds.length) {
            setDmTargetsByBatchId((prev) => {
              let changed = false;
              const existing = prev[m.send_batch_id!] || [];
              const set = new Set(existing);
              for (const pcId of pcIds) {
                if (!set.has(pcId)) {
                  set.add(pcId);
                  changed = true;
                }
              }
              if (!changed) return prev;
              return { ...prev, [m.send_batch_id!]: [...set.values()] };
            });
          }
        }
        if (m.thread_id) {
          setForumPostsByThread((prev) => {
            const list = prev[m.thread_id!] ? [...prev[m.thread_id!]] : [];
            list.push(m);
            return { ...prev, [m.thread_id!]: list };
          });
          setForumThreadsByChannel((prev) => {
            const threads = prev[m.conversation_id];
            if (!threads) return prev;
            const next = threads.map((t) =>
              t.id === m.thread_id
                ? {
                    ...t,
                    last_activity_at: m.timestamp,
                    reply_count: Math.max(0, (t.reply_count || 0) + 1)
                  }
                : t
            );
            return { ...prev, [m.conversation_id]: next };
          });
        }
        return;
      }
      if (msg.type === "typing") {
        setTypingByConv((prev) => {
          const next: Record<string, Set<string>> = { ...prev };
          const set = new Set(next[msg.payload.conversation_id] || []);
          if (msg.payload.value) set.add(msg.payload.pc_id);
          else set.delete(msg.payload.pc_id);
          next[msg.payload.conversation_id] = set;
          return next;
        });
        return;
      }
      if (msg.type === "queue") {
        setQueueState(msg.payload);
      }
    });

    wsRef.current = { ws, send };
    ws.addEventListener("open", () => setConnected(true));
    ws.addEventListener("close", () => setConnected(false));
    return () => ws.close();
  }, []);

  useEffect(() => {
    if (!conversations.length) return;
    if (conversations.some((c) => c.id === activeConvId)) return;
    setActiveConvId("broadcast");
  }, [activeConvId, conversations]);

  useEffect(() => {
    const conv = conversations.find((c) => c.id === activeConvId);
    if (conv?.kind !== "forum") return;
    setActiveForumThreadId(null);
    setForumDetailOnly(false);
  }, [activeConvId, conversations]);

  useEffect(() => {
    if (!activeForumThreadId) return;
    const conv = conversations.find((c) => c.id === activeConvId);
    if (conv?.kind !== "forum") return;
    const threads = forumThreadsByChannel[conv.id] || [];
    if (threads.some((t) => t.id === activeForumThreadId)) return;
    setActiveForumThreadId(null);
    setForumDetailOnly(false);
  }, [activeConvId, activeForumThreadId, conversations, forumThreadsByChannel]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [activeConvId, messagesByConv[activeConvId]?.length]);

  const activeMessages = messagesByConv[activeConvId] || [];
  const activeConv = conversations.find((c) => c.id === activeConvId);
  const forumThreadsRaw = activeConv?.kind === "forum" ? forumThreadsByChannel[activeConv.id] || [] : [];
  const forumThreads = useMemo(
    () => [...forumThreadsRaw].sort((a, b) => b.last_activity_at.localeCompare(a.last_activity_at)),
    [forumThreadsRaw]
  );
  const activeForumThread = activeForumThreadId ? forumThreads.find((t) => t.id === activeForumThreadId) || null : null;
  const activeForumPosts = activeForumThread ? forumPostsByThread[activeForumThread.id] || [] : [];

  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [activeForumThreadId, activeForumPosts.length]);

  const typingSet = typingByConv[activeConvId] || new Set<string>();
  const typingNames = pcs
    .filter((p) => typingSet.has(p.id))
    .map((p) => p.name)
    .join("、");

  const directDraft = useMemo(() => parseDirectDraft(content, pcIndex), [content, pcIndex]);
  const isDirectDraft = directDraft.isDirect;
  const directEffectivePcIds = [...new Set((directSelectedPcIds.length ? directSelectedPcIds : directDraft.typedPcIds).filter(Boolean))];
  const canSend =
    connected &&
    (isDirectDraft ? directEffectivePcIds.length > 0 && directDraft.message.length > 0 : content.trim().length > 0);

  const threadDirectDraft = useMemo(() => parseDirectDraft(threadComposerContent, pcIndex), [threadComposerContent, pcIndex]);
  const threadIsDirectDraft = threadDirectDraft.isDirect;
  const threadDirectEffectivePcIds = [
    ...new Set((threadDirectSelectedPcIds.length ? threadDirectSelectedPcIds : threadDirectDraft.typedPcIds).filter(Boolean))
  ];
  const canSendThreadPost =
    connected &&
    Boolean(activeConv?.kind === "forum" && activeForumThread) &&
    (threadIsDirectDraft
      ? threadDirectEffectivePcIds.length > 0 && threadDirectDraft.message.length > 0
      : threadComposerContent.trim().length > 0);

  useEffect(() => {
    if (!isDirectDraft) {
      if (directSelectedPcIds.length) setDirectSelectedPcIds([]);
      if (directSelectionTouched) setDirectSelectionTouched(false);
      return;
    }
    if (directSelectionTouched) return;
    if (!directDraft.typedPcIds.length) return;
    setDirectSelectedPcIds((prev) => (prev.join("|") === directDraft.typedPcIds.join("|") ? prev : directDraft.typedPcIds));
  }, [directDraft.typedPcIds, directSelectionTouched, directSelectedPcIds.length, isDirectDraft]);

  useEffect(() => {
    if (!threadIsDirectDraft) {
      if (threadDirectSelectedPcIds.length) setThreadDirectSelectedPcIds([]);
      if (threadDirectSelectionTouched) setThreadDirectSelectionTouched(false);
      return;
    }
    if (threadDirectSelectionTouched) return;
    if (!threadDirectDraft.typedPcIds.length) return;
    setThreadDirectSelectedPcIds((prev) =>
      prev.join("|") === threadDirectDraft.typedPcIds.join("|") ? prev : threadDirectDraft.typedPcIds
    );
  }, [
    threadDirectDraft.typedPcIds,
    threadDirectSelectionTouched,
    threadDirectSelectedPcIds.length,
    threadIsDirectDraft
  ]);

  async function deleteThread(threadId: string) {
    const ok = window.confirm("删除该 thread？其下帖子将一并删除。");
    if (!ok) return;
    try {
      await deleteForumThread(threadId);
      setForumPostsByThread((prev) => {
        const next = { ...prev };
        delete next[threadId];
        return next;
      });
      setForumThreadsByChannel((prev) => {
        const next: typeof prev = {};
        for (const [channelId, threads] of Object.entries(prev)) {
          next[channelId] = threads.filter((t) => t.id !== threadId);
        }
        return next;
      });
      if (activeForumThreadId === threadId) {
        setActiveForumThreadId(null);
        setForumDetailOnly(false);
      }
      wsRef.current?.send({ type: "request_state" });
    } catch {
      // ignore (keep UI as-is)
    }
  }

  function sendThreadPost() {
    if (!wsRef.current) return;
    const text = threadComposerContent.trim();
    if (!text) return;
    if (!activeConv || activeConv.kind !== "forum") {
      setThreadUiError("当前不在论坛频道");
      return;
    }
    if (!activeForumThread) {
      setThreadUiError("请先选择一个 thread");
      return;
    }
    setThreadUiError(null);

    const draft = parseDirectDraft(text, pcIndex);
    if (draft.isDirect) {
      const pcIds = [
        ...new Set((threadDirectSelectedPcIds.length ? threadDirectSelectedPcIds : draft.typedPcIds).filter(Boolean))
      ];
      if (!pcIds.length) {
        setThreadUiError("请选择至少一个私聊对象");
        return;
      }
      if (!draft.message) {
        setThreadUiError("请输入消息内容");
        return;
      }
      wsRef.current.send({
        type: "user_inject",
        content: draft.message,
        target: { kind: "direct", pc_ids: pcIds },
        channel_id: activeConv.id,
        thread_id: activeForumThread.id
      });
      setThreadComposerContent("");
      setThreadDirectSelectedPcIds([]);
      setThreadDirectSelectionTouched(false);
      return;
    }

    wsRef.current.send({
      type: "user_inject",
      content: text,
      target: { kind: "broadcast" },
      channel_id: activeConv.id,
      thread_id: activeForumThread.id
    });
    setThreadComposerContent("");
  }

  function parseDirectDraft(
    text: string,
    pcIndex: { byId: Map<string, { id: string; name: string }>; byName: Map<string, { id: string; name: string }> }
  ): { isDirect: true; message: string; typedPcIds: string[] } | { isDirect: false; message: ""; typedPcIds: [] } {
    const trimmed = text.trim();
    if (!trimmed.toLowerCase().startsWith("/direct")) return { isDirect: false, message: "", typedPcIds: [] };
    const rest = trimmed.slice("/direct".length).trim();

    if (!rest) return { isDirect: true, message: "", typedPcIds: [] };

    const lookupPcId = (token: string) => {
      const normalized = token.trim().replace(/^@/, "");
      if (!normalized) return null;
      const key = normalized.toLowerCase();
      const pc = pcIndex.byId.get(key) || pcIndex.byName.get(key);
      return pc ? pc.id : null;
    };

    const parseTargetsLoose = (segment: string) => {
      const ids: string[] = [];
      for (const raw of segment.split(/[\s,，、]+/g)) {
        const id = lookupPcId(raw);
        if (id) ids.push(id);
      }
      return [...new Set(ids)];
    };

    const colonIndex = rest.search(/[:：]/);
    if (colonIndex >= 0) {
      const before = rest.slice(0, colonIndex).trim();
      const after = rest.slice(colonIndex + 1).trim();
      const typed = before ? parseTargetsLoose(before) : [];
      if (!before || typed.length > 0) {
        return { isDirect: true, message: after, typedPcIds: typed };
      }
    }

    const tokens = rest.split(/\s+/);
    const typedPcIds: string[] = [];
    let i = 0;
    while (i < tokens.length) {
      const t = tokens[i];
      if (t === "--") {
        i++;
        break;
      }
      const parts = t
        .split(/[,\uFF0C，、]+/g)
        .map((x) => x.trim())
        .filter(Boolean);
      if (!parts.length) break;

      const matched: string[] = [];
      let allMatch = true;
      for (const part of parts) {
        const id = lookupPcId(part);
        if (!id) {
          allMatch = false;
          break;
        }
        matched.push(id);
      }
      if (!allMatch) break;
      typedPcIds.push(...matched);
      i++;
    }

    const uniqueTyped = [...new Set(typedPcIds)];
    const message = uniqueTyped.length ? tokens.slice(i).join(" ").trim() : rest.trim();
    return { isDirect: true, message, typedPcIds: uniqueTyped };
  }

  function sendInject() {
    if (!wsRef.current) return;
    const text = content.trim();
    if (!text) return;
    setUiError(null);

    const draft = parseDirectDraft(text, pcIndex);
    if (draft.isDirect) {
      const pcIds = [...new Set((directSelectedPcIds.length ? directSelectedPcIds : draft.typedPcIds).filter(Boolean))];
      if (!pcIds.length) {
        setUiError("请选择至少一个私聊对象");
        return;
      }
      if (!draft.message) {
        setUiError("请输入消息内容");
        return;
      }
      wsRef.current.send({
        type: "user_inject",
        content: draft.message,
        target: { kind: "direct", pc_ids: pcIds }
      });
      setContent("");
      setDirectSelectedPcIds([]);
      setDirectSelectionTouched(false);
      return;
    }

    wsRef.current.send({ type: "user_inject", content: text, target: { kind: "broadcast" } });
    setContent("");
  }

  function setPaused(paused: boolean) {
    wsRef.current?.send(paused ? { type: "pause", value: true } : { type: "resume" });
  }

  function dmPc(pcId: string, text: string) {
    if (!wsRef.current) return;
    wsRef.current.send({ type: "user_inject", content: text, target: { kind: "direct", pc_ids: [pcId] } });
  }

  const userProfile = useMemo(() => getProfile(profiles, { kind: "user", id: "user", name: userName }), [profiles, userName]);

  function openProfile(actor: Actor, ev?: { clientY: number; currentTarget: Element | null }) {
    const target = ev?.currentTarget as HTMLElement | null;
    const rect = target?.getBoundingClientRect();
    const anchor = rect && ev ? { x: rect.right + 6, y: ev.clientY } : null;
    setProfileViewer({ actor, anchor });
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <div className="brandLeft">
            <strong>loom demo</strong>
          </div>
          <div className="brandRight">
            <div className="queue">
              {queueState ? `队列 ${queueState.queued} · ${queueState.paused ? "已暂停" : "运行中"}` : "队列 -"}
            </div>
          </div>
        </div>

        <div className="convList" role="list">
          <div className="convGroup">
            <div className="groupTitle">频道</div>
            {channelConversations.map((c) => (
              <button
                key={c.id}
                className={`convItem ${c.id === activeConvId ? "active" : ""}`}
                onClick={() => setActiveConvId(c.id)}
              >
                <span className="convItemIcon" aria-hidden="true">
                  {conversationIcon(c)}
                </span>
                <span className="convItemLabel">{conversationLabel(c, profiles)}</span>
              </button>
            ))}
          </div>
          <div className="convGroup">
            <div className="groupTitle">私聊频道</div>
            {directConversations.map((c) => (
              <button
                key={c.id}
                className={`convItem ${c.id === activeConvId ? "active" : ""}`}
                onClick={() => setActiveConvId(c.id)}
              >
                <span className="convItemIcon" aria-hidden="true">
                  {conversationIcon(c)}
                </span>
                <span className="convItemLabel">{conversationLabel(c, profiles)}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="userBox">
          <div className="userLeft">
            <button
              className="avatarBtn"
              onClick={(e) => openProfile({ kind: "user", id: "user", name: userName }, e)}
              title="查看个人资料"
            >
              <div className="userAvatar" aria-hidden="true">
                <div className="avatarClip" aria-hidden="true">
                  {userProfile?.avatarUrl ? (
                    <img
                      src={absoluteAssetUrl(userProfile.avatarUrl)}
                      alt={userProfile.displayName}
                      onError={(e) => ((e.currentTarget as HTMLImageElement).style.display = "none")}
                    />
                  ) : (
                    avatarLabel(userProfile?.displayName || userName)
                  )}
                </div>
                {statusDotColor(userProfile) ? (
                  <span className="statusDot" style={{ background: statusDotColor(userProfile) as string }} />
                ) : null}
              </div>
            </button>
            <div className="userName">
              <div className="userNameMain">{userProfile?.nickname || userProfile?.displayName || userName}</div>
              <div className="userNameSub">{connected ? "已连接" : "未连接"}</div>
            </div>
          </div>
          <div className="userRight">
            <button
              className="iconBtn iconOnly"
              onClick={() => {
                setSettingsOpen(true);
                setSettingsTab("appearance");
              }}
              aria-label="设置"
              title="设置"
            >
              <Settings size={18} />
            </button>
          </div>
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <h1>{activeConv ? conversationLabel(activeConv, profiles) : activeConvId}</h1>
          <div className="controls">
            <button
              className="iconBtn iconOnly"
              disabled={!connected || queueState?.paused === true}
              onClick={() => {
                setPaused(true)
              }}
              aria-label="暂停"
              title="暂停"
            >
              <Pause size={18} />
            </button>
            <button
              className="iconBtn iconOnly"
              disabled={!connected || queueState?.paused === false}
              onClick={() => {
                setPaused(false)
              }}
              aria-label="暂停"
              title="暂停"
            >
              <Play size={18} />
            </button>
          </div>
        </div>

        {activeConv?.kind === "forum" ? (
          <div className={`forumShell ${activeForumThread ? (forumDetailOnly ? "detailOnly" : "split") : ""}`}>
            {forumDetailOnly && activeForumThread ? null : (
              <div className="threadList">
                <div className="threadListInner">
                  {forumThreads.map((t) => {
                    const posts = forumPostsByThread[t.id] || [];
                    const last = posts.length ? posts[posts.length - 1] : null;
                    return (
                      <button
                        key={t.id}
                        className={`threadItem ${t.id === activeForumThreadId ? "active" : ""}`}
                        onClick={() => {
                          setActiveForumThreadId(t.id);
                          setForumDetailOnly(false);
                          setThreadUiError(null);
                        }}
                      >
                        <div className="threadItemActions" role="toolbar" aria-label="thread 操作">
                          <button
                            className="threadActionBtn danger"
                            onClick={(e) => {
                              e.preventDefault();
                              e.stopPropagation();
                              void deleteThread(t.id);
                            }}
                            aria-label="删除 thread"
                            title="删除"
                          >
                            <Trash2 size={16} />
                          </button>
                        </div>
                        <div className="threadTitle">{t.title}</div>
                        <div className="threadMeta">
                          <span>{formatTime(t.last_activity_at)}</span>
                          <span>·</span>
                          <span>{t.reply_count} 回复</span>
                        </div>
                        {last ? <div className="threadPreview">{last.content}</div> : null}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {activeForumThread ? (
              <div className="threadDetail">
                <div className="threadDetailHeader">
                  <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
                    <div style={{ minWidth: 0 }}>
                      <h2 className="threadDetailTitle">{activeForumThread.title}</h2>
                      <div className="threadDetailSub">
                        由 {chatDisplayName(profiles, activeForumThread.created_by)} 创建 · 更新 {formatTime(activeForumThread.last_activity_at)} ·{" "}
                        {activeForumThread.reply_count} 回复
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: 8, flex: "0 0 auto" }}>
                      <button
                        className="smallBtn"
                        onClick={() => {
                          setForumDetailOnly((v) => !v);
                        }}
                      >
                        {forumDetailOnly ? "显示列表" : "完整视图"}
                      </button>
                      <button
                        className="iconBtn iconOnly"
                        onClick={() => {
                          setActiveForumThreadId(null);
                          setForumDetailOnly(false);
                        }}
                        aria-label="返回"
                        title="返回"
                      >
                        <X size={18} />
                      </button>
                    </div>
                  </div>
                </div>
                <ChatFlow
                  messages={activeForumPosts}
                  profiles={profiles}
                  typingNames={typingNames}
                  endRef={threadEndRef}
                  onOpenProfile={(actor, e) => openProfile(actor, e)}
                  dmTargetsByBatchId={dmTargetsByBatchId}
                  onJumpToDm={jumpToDm}
                  scrollToSendBatchId={
                    pendingScroll && pendingScroll.conversationId === activeConvId ? pendingScroll.sendBatchId : null
                  }
                  onClearScrollToSendBatchId={() => setPendingScroll(null)}
                  composer={{
                    className: "composer threadComposer",
                    value: threadComposerContent,
                    onChange: (next) => {
                      setThreadComposerContent(next);
                      if (threadUiError) setThreadUiError(null);
                    },
                    placeholder: connected
                      ? threadIsDirectDraft
                        ? "私聊内容…（/direct 后面写要发送的文字）"
                        : "在 thread 下发言…"
                      : "正在连接后端…",
                    error: threadUiError,
                    onClearError: () => {
                      if (threadUiError) setThreadUiError(null);
                    },
                    canSend: canSendThreadPost,
                    onSend: sendThreadPost,
                    pill: {
                      isDirect: threadIsDirectDraft,
                      normalLabel: activeConv?.title || "",
                      directSelectedCount: threadDirectSelectedPcIds.length
                    },
                    hint: (
                      <>
                        <span>
                          发送：<code>⌘/Ctrl + Enter</code>
                        </span>
                        <span style={{ opacity: 0.85 }}>
                          · 私聊：<code>/direct</code> 后勾选对象（或 <code>/direct Alice：你好</code>）
                        </span>
                      </>
                    ),
                    directPicker: {
                      pcs,
                      selectedPcIds: threadDirectSelectedPcIds,
                      setSelectedPcIds: setThreadDirectSelectedPcIds,
                      setSelectionTouched: setThreadDirectSelectionTouched
                    }
                  }}
                />
              </div>
            ) : null}
          </div>
        ) : (
          <ChatFlow
            messages={activeMessages}
            profiles={profiles}
            typingNames={typingNames}
            endRef={messagesEndRef}
            onOpenProfile={(actor, e) => openProfile(actor, e)}
            dmTargetsByBatchId={dmTargetsByBatchId}
            onJumpToDm={jumpToDm}
            scrollToSendBatchId={
              pendingScroll && pendingScroll.conversationId === activeConvId ? pendingScroll.sendBatchId : null
            }
            onClearScrollToSendBatchId={() => setPendingScroll(null)}
            composer={
              activeConv?.kind === "dm_to_pc" || activeConv?.kind === "pc_to_pc"
                ? undefined
                : {
                    value: content,
                    onChange: (next) => {
                      setContent(next);
                      if (uiError) setUiError(null);
                    },
                    placeholder: connected
                      ? isDirectDraft
                        ? "私聊内容…（/direct 后面写要发送的文字）"
                        : "输入消息（默认广播）…"
                      : "正在连接后端…",
                    error: uiError,
                    onClearError: () => {
                      if (uiError) setUiError(null);
                    },
                    canSend,
                    onSend: sendInject,
                    pill: { isDirect: isDirectDraft, normalLabel: "#broadcast", directSelectedCount: directSelectedPcIds.length },
                    hint: (
                      <>
                        私聊：<code>/direct</code> 后勾选对象（或 <code>/direct Alice：你好</code>）
                      </>
                    ),
                    directPicker: {
                      pcs,
                      selectedPcIds: directSelectedPcIds,
                      setSelectedPcIds: setDirectSelectedPcIds,
                      setSelectionTouched: setDirectSelectionTouched
                    }
                  }
            }
          />
        )}
      </main>

      <SettingsModal
        open={settingsOpen}
        tab={settingsTab}
        onTabChange={setSettingsTab}
        onClose={() => setSettingsOpen(false)}
        profileNav={
          <ProfileNavList
            actors={profileActors}
            profiles={profiles}
            selectedId={profileSettingsSelectedId}
            onSelectId={setProfileSettingsSelectedId}
          />
        }
      >
        {settingsTab === "appearance" ? (
          <AppearanceTab
            open={settingsOpen}
            appearance={appearance}
            setAppearance={setAppearance}
            customThemes={customThemes}
            setCustomThemes={setCustomThemes}
            onRequestClose={() => setSettingsOpen(false)}
          />
        ) : settingsTab === "channels" ? (
          <ChannelsTab
            open={settingsOpen}
            channels={channels}
            setChannels={setChannels}
            onRequestClose={() => setSettingsOpen(false)}
          />
        ) : settingsTab === "profile" ? (
          <ProfileTab
            open={settingsOpen}
            actors={profileActors}
            profiles={profiles}
            setProfiles={setProfiles}
            selectedId={profileSettingsSelectedId}
            onRequestClose={() => setSettingsOpen(false)}
          />
        ) : null}
      </SettingsModal>

      <ProfileCard
        open={Boolean(profileViewer)}
        actor={profileViewer?.actor ?? null}
        anchor={profileViewer?.anchor ?? null}
        profiles={profiles}
        onClose={() => setProfileViewer(null)}
        canDmPc={connected}
        onDmPc={(pcId, text) => dmPc(pcId, text)}
      />
    </div>
  );
}
