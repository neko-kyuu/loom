import { useEffect, useMemo, useRef, useState } from "react";
import type { Conversation, ForumThread, Message, WsServerToClient } from "./types";
import { createWs } from "./lib/ws";
import { Brain, Hash, List, MessageCircle, Settings, Pause, Play, ScrollText, Trash2, X, Pin, Lock, Unlock, MoreHorizontal } from "lucide-react";
import SettingsModal, { type SettingsTabId } from "./components/SettingsModal";
import PcActivityLogModal from "./components/PcActivityLogModal";
import MemoryDebuggerModal from "./components/MemoryDebuggerModal";
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
  patchForumThread,
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

type DirectThreadKey = "__all__" | "__unknown__" | "dm" | string;
const THREAD_TYPING_MIN_VISIBLE_MS = 900;

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

function messageHasActor(m: Message, pred: (a: Actor) => boolean): boolean {
  if (pred(m.from_actor)) return true;
  for (const a of m.to || []) if (pred(a)) return true;
  return false;
}

function messageHasPcId(m: Message, pcId: string): boolean {
  const id = (pcId || "").trim();
  if (!id) return false;
  return messageHasActor(m, (a) => a.kind === "pc" && Boolean(a.id) && a.id === id);
}

function messageHasDm(m: Message): boolean {
  return messageHasActor(m, (a) => a.kind === "dm");
}

function directPeerKeyFromMessage(m: Message, viewerPcId: string): DirectThreadKey {
  if (messageHasDm(m)) return "dm";
  const otherPcIds: string[] = [];
  const pushPc = (a: Actor) => {
    if (a.kind !== "pc") return;
    const pid = (a.id || "").trim();
    if (!pid) return;
    if (pid === viewerPcId) return;
    if (!otherPcIds.includes(pid)) otherPcIds.push(pid);
  };
  pushPc(m.from_actor);
  for (const a of m.to || []) pushPc(a);
  return otherPcIds[0] || "__unknown__";
}

function messageMatchesDirectThread(m: Message, viewerPcId: string, key: DirectThreadKey): boolean {
  if (key === "__all__") return true;
  if (key === "dm") return messageHasDm(m);
  if (key === "__unknown__") return !messageHasDm(m) && directPeerKeyFromMessage(m, viewerPcId) === "__unknown__";
  return messageHasPcId(m, key);
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

function applyTypingUpdate(
  prev: Record<string, Set<string>>,
  key: string | undefined,
  pcId: string,
  value: boolean
): Record<string, Set<string>> {
  if (!key) return prev;
  const next: Record<string, Set<string>> = { ...prev };
  const set = new Set(next[key] || []);
  if (value) set.add(pcId);
  else set.delete(pcId);
  if (set.size) next[key] = set;
  else delete next[key];
  return next;
}

export default function App() {
  const [connected, setConnected] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [messagesByConv, setMessagesByConv] = useState<Record<string, Message[]>>({});
  const [dmTargetsByBatchId, setDmTargetsByBatchId] = useState<Record<string, string[]>>({});
  const [pendingScroll, setPendingScroll] = useState<{ conversationId: string; sendBatchId: string } | null>(null);
  const [activeConvId, setActiveConvId] = useState<string>("broadcast");
  const [typingByConv, setTypingByConv] = useState<Record<string, Set<string>>>({});
  const [typingByThread, setTypingByThread] = useState<Record<string, Set<string>>>({});
  const [queueState, setQueueState] = useState<{ paused: boolean; queued: number } | null>(null);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTabId>("appearance");
  const [profileSettingsSelectedId, setProfileSettingsSelectedId] = useState<string>("user");
  const [activityLogOpen, setActivityLogOpen] = useState(false);
  const [memoryDebuggerOpen, setMemoryDebuggerOpen] = useState(false);

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
  const [directThreadByConvId, setDirectThreadByConvId] = useState<Record<string, DirectThreadKey>>({});
  const [directDetailOnly, setDirectDetailOnly] = useState(false);

  const wsRef = useRef<ReturnType<typeof createWs> | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const threadEndRef = useRef<HTMLDivElement | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const saveProfilesTimerRef = useRef<number | null>(null);
  const saveChannelsTimerRef = useRef<number | null>(null);
  const threadTypingShownAtRef = useRef<Record<string, number>>({});
  const threadTypingHideTimersRef = useRef<Record<string, number>>({});

  useEffect(() => {
    return () => {
      for (const timerId of Object.values(threadTypingHideTimersRef.current)) {
        window.clearTimeout(timerId);
      }
      threadTypingHideTimersRef.current = {};
    };
  }, []);

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
  const channelConversationGroups = useMemo(() => {
    const defaultGroup = "未分组";
    const byGroup = new Map<string, Conversation[]>();
    for (const c of channelConversations) {
      const g = (c.group || "").trim();
      const key = g || defaultGroup;
      const list = byGroup.get(key);
      if (list) list.push(c);
      else byGroup.set(key, [c]);
    }
    const locale = "zh-Hans-CN";
    const collator = new Intl.Collator(locale, { numeric: true, sensitivity: "base" });
    const sortConv = (a: Conversation, b: Conversation) => {
      if (a.kind !== b.kind) return a.kind === "broadcast" ? -1 : 1;
      return collator.compare(conversationLabel(a, profiles), conversationLabel(b, profiles));
    };
    for (const list of byGroup.values()) list.sort(sortConv);
    const entries = [...byGroup.entries()];
    entries.sort(([ga], [gb]) => {
      if (ga === defaultGroup && gb !== defaultGroup) return -1;
      if (gb === defaultGroup && ga !== defaultGroup) return 1;
      return collator.compare(ga, gb);
    });
    return entries.map(([group, list]) => ({ group, conversations: list }));
  }, [channelConversations, profiles]);

  function jumpToDm(pcId: string, sendBatchId: string) {
    const convId = `dm_to_${pcId}`;
    setActiveConvId(convId);
    setDirectThreadByConvId((prev) => ({ ...prev, [convId]: "dm" }));
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
      if (msg.type === "message_deleted") {
        const ids = (msg.payload?.message_ids || []).filter(Boolean);
        if (!ids.length) return;
        const set = new Set(ids);
        setMessagesByConv((prev) => {
          let changed = false;
          const next: Record<string, Message[]> = {};
          for (const [cid, list] of Object.entries(prev)) {
            const filtered = (list || []).filter((m) => !set.has(m.id));
            if (filtered.length !== (list || []).length) changed = true;
            next[cid] = filtered;
          }
          return changed ? next : prev;
        });
        setForumPostsByThread((prev) => {
          let changed = false;
          const next: Record<string, Message[]> = {};
          for (const [tid, list] of Object.entries(prev)) {
            const filtered = (list || []).filter((m) => !set.has(m.id));
            if (filtered.length !== (list || []).length) changed = true;
            next[tid] = filtered;
          }
          return changed ? next : prev;
        });
        wsRef.current?.send({ type: "request_state" });
        return;
      }
      if (msg.type === "message_edited") {
        const ids = (msg.payload?.message_ids || []).filter(Boolean);
        const content = (msg.payload?.content || "").trim();
        if (!ids.length || !content) return;
        const set = new Set(ids);
        setMessagesByConv((prev) => {
          let changed = false;
          const next: Record<string, Message[]> = {};
          for (const [cid, list] of Object.entries(prev)) {
            let listChanged = false;
            const updated = (list || []).map((m) => {
              if (!set.has(m.id)) return m;
              if (m.content === content) return m;
              listChanged = true;
              return { ...m, content };
            });
            if (listChanged) changed = true;
            next[cid] = updated;
          }
          return changed ? next : prev;
        });
        setForumPostsByThread((prev) => {
          let changed = false;
          const next: Record<string, Message[]> = {};
          for (const [tid, list] of Object.entries(prev)) {
            let listChanged = false;
            const updated = (list || []).map((m) => {
              if (!set.has(m.id)) return m;
              if (m.content === content) return m;
              listChanged = true;
              return { ...m, content };
            });
            if (listChanged) changed = true;
            next[tid] = updated;
          }
          return changed ? next : prev;
        });
        return;
      }
      if (msg.type === "forum_thread") {
        const t = msg.payload.thread;
        setForumThreadsByChannel((prev) => {
          const channelId = t.channel_id;
          const existing = prev[channelId] ? [...prev[channelId]] : [];
          const idx = existing.findIndex((x) => x.id === t.id);
          if (idx >= 0) existing[idx] = t;
          else existing.push(t);
          return { ...prev, [channelId]: existing };
        });
        return;
      }
      if (msg.type === "typing") {
        setTypingByConv((prev) => applyTypingUpdate(prev, msg.payload.conversation_id, msg.payload.pc_id, msg.payload.value));
        if (msg.payload.thread_id) {
          const threadTypingKey = `${msg.payload.thread_id}:${msg.payload.pc_id}`;
          const pendingTimer = threadTypingHideTimersRef.current[threadTypingKey];
          if (pendingTimer) {
            window.clearTimeout(pendingTimer);
            delete threadTypingHideTimersRef.current[threadTypingKey];
          }
          if (msg.payload.value) {
            threadTypingShownAtRef.current[threadTypingKey] = Date.now();
            setTypingByThread((prev) => applyTypingUpdate(prev, msg.payload.thread_id, msg.payload.pc_id, true));
          } else {
            const shownAt = threadTypingShownAtRef.current[threadTypingKey] || 0;
            const remaining = Math.max(0, THREAD_TYPING_MIN_VISIBLE_MS - (Date.now() - shownAt));
            const clearTyping = () => {
              delete threadTypingShownAtRef.current[threadTypingKey];
              delete threadTypingHideTimersRef.current[threadTypingKey];
              setTypingByThread((prev) => applyTypingUpdate(prev, msg.payload.thread_id, msg.payload.pc_id, false));
            };
            if (remaining <= 0) clearTyping();
            else {
              threadTypingHideTimersRef.current[threadTypingKey] = window.setTimeout(clearTyping, remaining);
            }
          }
        }
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
    setDirectDetailOnly(false);
  }, [activeConvId]);

  useEffect(() => {
    if (!activeForumThreadId) return;
    const conv = conversations.find((c) => c.id === activeConvId);
    if (conv?.kind !== "forum") return;
    const threads = forumThreadsByChannel[conv.id] || [];
    if (threads.some((t) => t.id === activeForumThreadId)) return;
    setActiveForumThreadId(null);
    setForumDetailOnly(false);
  }, [activeConvId, activeForumThreadId, conversations, forumThreadsByChannel]);

  const activeMessages = messagesByConv[activeConvId] || [];
  const activeConv = conversations.find((c) => c.id === activeConvId);

  const directViewerPcId = activeConv?.kind === "dm_to_pc" ? activeConv.id.replace(/^dm_to_/, "") : null;
  const directThreads = useMemo(() => {
    if (activeConv?.kind !== "dm_to_pc" || !directViewerPcId) return [];
    const viewerId = directViewerPcId;
    const byKey = new Map<DirectThreadKey, { lastTs: string; preview: string; count: number }>();
    const update = (key: DirectThreadKey, m: Message) => {
      const ts = m.timestamp || "";
      const prev = byKey.get(key);
      const content = (m.content || "").trim();
      const preview = content.length > 140 ? content.slice(0, 140) + "…" : content;
      if (!prev) {
        byKey.set(key, { lastTs: ts, preview, count: 1 });
        return;
      }
      prev.count += 1;
      if (ts && (!prev.lastTs || ts > prev.lastTs)) {
        prev.lastTs = ts;
        prev.preview = preview;
      }
    };

    for (const m of activeMessages) {
      update("__all__", m);
      if (!messageHasPcId(m, viewerId)) {
        update("__unknown__", m);
        continue;
      }
      const peerKey = directPeerKeyFromMessage(m, viewerId);
      update(peerKey, m);
    }

    const threads: { key: DirectThreadKey; lastTs: string; preview: string; count: number }[] = [];
    for (const [key, v] of byKey.entries()) threads.push({ key, ...v });

    const keyOrder = (k: DirectThreadKey) => {
      if (k === "__all__") return 0;
      if (k === "dm") return 1;
      if (k === "__unknown__") return 99;
      return 2;
    };
    threads.sort((a, b) => {
      const ao = keyOrder(a.key);
      const bo = keyOrder(b.key);
      if (ao !== bo) return ao - bo;
      if (a.lastTs !== b.lastTs) return (b.lastTs || "").localeCompare(a.lastTs || "");
      return String(a.key).localeCompare(String(b.key));
    });
    return threads;
  }, [activeConv?.kind, activeMessages, directViewerPcId]);

  const directSelectedThreadKey = useMemo(() => {
    if (activeConv?.kind !== "dm_to_pc") return "__all__" as DirectThreadKey;
    const keys = new Set(directThreads.map((t) => t.key));
    const stored = directThreadByConvId[activeConvId];
    if (stored && keys.has(stored)) return stored;
    if (keys.has("dm")) return "dm";
    if (keys.has("__all__")) return "__all__";
    return "__unknown__";
  }, [activeConv?.kind, activeConvId, directThreadByConvId, directThreads]);

  const displayedMessages = useMemo(() => {
    if (activeConv?.kind !== "dm_to_pc" || !directViewerPcId) return activeMessages;
    return activeMessages.filter((m) => messageMatchesDirectThread(m, directViewerPcId, directSelectedThreadKey));
  }, [activeConv?.kind, activeMessages, directSelectedThreadKey, directViewerPcId]);

  useEffect(() => {
    if (activeConv?.kind === "forum") return;
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [activeConvId, activeConv?.kind, directSelectedThreadKey, displayedMessages.length]);
  const forumThreadsRaw = activeConv?.kind === "forum" ? forumThreadsByChannel[activeConv.id] || [] : [];
  const forumThreads = useMemo(
    () =>
      [...forumThreadsRaw].sort((a, b) => {
        const pin = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
        if (pin) return pin;
        return b.last_activity_at.localeCompare(a.last_activity_at);
      }),
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
    !Boolean(activeForumThread?.locked) &&
    (threadIsDirectDraft
      ? threadDirectEffectivePcIds.length > 0 && threadDirectDraft.message.length > 0
      : threadComposerContent.trim().length > 0);

  async function setThreadPinned(thread: ForumThread, pinned: boolean) {
    try {
      const updated = await patchForumThread(thread.id, { pinned });
      setForumThreadsByChannel((prev) => {
        const list = prev[updated.channel_id] || [];
        const idx = list.findIndex((x) => x.id === updated.id);
        if (idx < 0) return prev;
        const next = [...list];
        next[idx] = updated;
        return { ...prev, [updated.channel_id]: next };
      });
    } catch {
      // ignore (keep UI as-is)
    }
  }

  async function setThreadLocked(thread: ForumThread, locked: boolean) {
    try {
      const updated = await patchForumThread(thread.id, { locked });
      setForumThreadsByChannel((prev) => {
        const list = prev[updated.channel_id] || [];
        const idx = list.findIndex((x) => x.id === updated.id);
        if (idx < 0) return prev;
        const next = [...list];
        next[idx] = updated;
        return { ...prev, [updated.channel_id]: next };
      });
      if (locked) {
        setThreadUiError("该 thread 已锁定，无法回复");
      }
    } catch {
      // ignore (keep UI as-is)
    }
  }

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

  function deleteMessage(messageId: string) {
    if (!wsRef.current) return;
    const ok = window.confirm("删除该消息？");
    if (!ok) return;
    wsRef.current.send({ type: "delete_message", message_id: messageId });
  }

  function editMessage(messageId: string, content: string) {
    if (!wsRef.current) return;
    const trimmed = (content || "").trim();
    if (!trimmed) return;
    wsRef.current.send({ type: "edit_message", message_id: messageId, content: trimmed });
  }

  function sendThreadPost() {
    if (!wsRef.current) return;
    if (activeForumThread?.locked) {
      setThreadUiError("该 thread 已锁定，无法回复");
      return;
    }
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
            {channelConversationGroups.map((g) => (
              <div key={g.group} className="convSubGroup">
                <div className="groupSubTitle">{g.group}</div>
                {g.conversations.map((c) => (
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
              disabled={!connected}
              onClick={() => setMemoryDebuggerOpen(true)}
              aria-label="记忆调试器"
              title="记忆调试器"
            >
              <Brain size={18} />
            </button>
            <button
              className="iconBtn iconOnly"
              disabled={!connected}
              onClick={() => setActivityLogOpen(true)}
              aria-label="日志"
              title="日志"
            >
              <ScrollText size={18} />
            </button>
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
              aria-label="继续"
              title="继续"
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
                    const typingPc = pcs.find((p) => (typingByThread[t.id] || new Set<string>()).has(p.id)) || null;
                    const typingActor = typingPc ? ({ kind: "pc", id: typingPc.id, name: typingPc.name } as const) : null;
                    const typingProfile = typingActor ? getProfile(profiles, typingActor) : null;
                    const typingName = typingActor ? chatDisplayName(profiles, typingActor) : "";
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
	                            className={`threadActionBtn ${t.pinned ? "on" : ""}`}
	                            disabled={!connected}
	                            onClick={(e) => {
	                              e.preventDefault();
	                              e.stopPropagation();
	                              void setThreadPinned(t, !t.pinned);
	                            }}
	                            aria-label={t.pinned ? "取消置顶" : "置顶 thread"}
	                            title={t.pinned ? "取消置顶" : "置顶"}
	                          >
	                            <Pin size={16} />
	                          </button>
	                          <button
	                            className={`threadActionBtn ${t.locked ? "on" : ""}`}
	                            disabled={!connected}
	                            onClick={(e) => {
	                              e.preventDefault();
	                              e.stopPropagation();
	                              void setThreadLocked(t, !t.locked);
	                            }}
	                            aria-label={t.locked ? "解锁 thread" : "锁定 thread"}
	                            title={t.locked ? "解锁（允许回复）" : "锁定（禁止回复）"}
	                          >
	                            {t.locked ? <Lock size={16} /> : <Unlock size={16} />}
	                          </button>
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
                        <div className={`threadMeta ${typingActor ? "typing" : ""}`}>
                          <span>{formatTime(t.last_activity_at)}</span>
                          <span>·</span>
                          <span>{t.reply_count} 回复</span>
                          {typingActor ? (
                            <>
                              <span className="threadTypingSep">·</span>
                              <span className="threadTypingAvatar" aria-hidden="true">
                                {typingProfile?.avatarUrl ? (
                                  <img
                                    src={absoluteAssetUrl(typingProfile.avatarUrl)}
                                    alt=""
                                    onError={(e) => ((e.currentTarget as HTMLImageElement).style.display = "none")}
                                  />
                                ) : (
                                  avatarLabel(typingName)
                                )}
                              </span>
                              <MoreHorizontal size={14} className="threadTypingDots" aria-hidden="true" />
                              <span className="threadTypingText">{typingName} 正在输入…</span>
                            </>
                          ) : null}
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
                  onDeleteMessage={deleteMessage}
                  onEditMessage={editMessage}
                  directViewerPcId={null}
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
                      ? activeForumThread?.locked
                        ? "该 thread 已锁定，无法回复"
                        : threadIsDirectDraft
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
          activeConv?.kind === "dm_to_pc" && directViewerPcId ? (
            <div className={`forumShell ${directDetailOnly ? "detailOnly" : "split"}`}>
              {directDetailOnly ? null : (
                <div className="threadList">
                  <div className="threadListInner">
                    {directThreads.length ? (
                      directThreads.map((t) => {
                        const isActive = t.key === directSelectedThreadKey;
                        const title = (() => {
                          if (t.key === "__all__") return "全部";
                          if (t.key === "dm") return "DM";
                          if (t.key === "__unknown__") return "未识别";
                          const name = chatDisplayName(profiles, { kind: "pc", id: t.key });
                          return name ? `@${name}` : `@${t.key}`;
                        })();
                        return (
                          <button
                            key={t.key}
                            className={`threadItem ${isActive ? "active" : ""}`}
                            onClick={() => {
                              setDirectThreadByConvId((prev) => ({ ...prev, [activeConvId]: t.key }));
                              setDirectDetailOnly(false);
                            }}
                          >
                            <div className="threadTitle">{title}</div>
                            <div className="threadMeta">
                              <span>{t.lastTs ? formatTime(t.lastTs) : "—"}</span>
                              <span>·</span>
                              <span>{t.count} 条</span>
                            </div>
                            {t.preview ? <div className="threadPreview">{t.preview}</div> : null}
                          </button>
                        );
                      })
                    ) : (
                      <div className="forumEmpty">暂无私聊记录</div>
                    )}
                  </div>
                </div>
              )}

              <div className="threadDetail">
                <div className="threadDetailHeader">
                  <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
                    <div style={{ minWidth: 0 }}>
                      <h2 className="threadDetailTitle">{conversationLabel(activeConv, profiles)}</h2>
                      <div className="threadDetailSub">
                        {directSelectedThreadKey === "__all__"
                          ? "全部私聊记录（含 PC↔PC 复制）"
                          : directSelectedThreadKey === "dm"
                            ? "DM ↔ PC"
                            : directSelectedThreadKey === "__unknown__"
                              ? "未识别来源/对象"
                              : `${chatDisplayName(profiles, { kind: "pc", id: directSelectedThreadKey }) || directSelectedThreadKey} ↔ PC`}
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: 8, flex: "0 0 auto" }}>
                      <button
                        className="smallBtn"
                        onClick={() => {
                          setDirectDetailOnly((v) => !v);
                        }}
                      >
                        {directDetailOnly ? "显示列表" : "完整视图"}
                      </button>
                    </div>
                  </div>
                </div>
                <ChatFlow
                  messages={displayedMessages}
                  profiles={profiles}
                  typingNames={typingNames}
                  endRef={messagesEndRef}
                  onOpenProfile={(actor, e) => openProfile(actor, e)}
                  onDeleteMessage={deleteMessage}
                  onEditMessage={editMessage}
                  directViewerPcId={directViewerPcId}
                  dmTargetsByBatchId={dmTargetsByBatchId}
                  onJumpToDm={jumpToDm}
                  scrollToSendBatchId={
                    pendingScroll && pendingScroll.conversationId === activeConvId ? pendingScroll.sendBatchId : null
                  }
                  onClearScrollToSendBatchId={() => setPendingScroll(null)}
                />
              </div>
            </div>
          ) : (
            <ChatFlow
              messages={activeMessages}
              profiles={profiles}
              typingNames={typingNames}
              endRef={messagesEndRef}
              onOpenProfile={(actor, e) => openProfile(actor, e)}
              onDeleteMessage={deleteMessage}
              onEditMessage={editMessage}
              directViewerPcId={activeConv?.kind === "dm_to_pc" ? activeConv.id.replace(/^dm_to_/, "") : null}
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
                      pill: {
                        isDirect: isDirectDraft,
                        normalLabel: "#broadcast",
                        directSelectedCount: directSelectedPcIds.length
                      },
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
          )
        )}
      </main>

      <PcActivityLogModal open={activityLogOpen} onClose={() => setActivityLogOpen(false)} />
      <MemoryDebuggerModal open={memoryDebuggerOpen} onClose={() => setMemoryDebuggerOpen(false)} pcs={pcs} />

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
