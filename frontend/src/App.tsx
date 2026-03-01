import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { Conversation, Message, WsServerToClient } from "./types";
import { createWs } from "./lib/ws";
import { Settings } from "lucide-react";
import SettingsModal, { type SettingsTabId } from "./components/SettingsModal";
import AppearanceTab from "./components/settings/AppearanceTab";
import ProfileTab from "./components/settings/ProfileTab";
import ProfileNavList from "./components/settings/ProfileNavList";
import ProfileCard from "./components/ProfileCard";
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
import type { Profile, ProfilesState } from "./lib/profiles";
import {
  chatDisplayName,
  ensureProfiles,
  getProfile,
  loadProfiles,
  parseProfilesPayload,
  persistProfiles,
  statusDotColor
} from "./lib/profiles";
import { absoluteAssetUrl, getSettingsState, putAppearanceState, putProfilesState, wsUrl } from "./lib/api";

function formatTime(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);

  const pad2 = (n: number) => String(n).padStart(2, "0");
  const hhmm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (d >= startOfToday) return hhmm;
  if (d >= startOfYesterday) return `昨日 ${hhmm}`;
  const ymd = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  return `${ymd} ${hhmm}`;
}

function avatarLabel(name?: string | null) {
  const s = (name || "?").trim();
  return s ? s.slice(0, 1).toUpperCase() : "?";
}

function conversationLabel(c: Conversation, profiles: ProfilesState) {
  if (c.kind === "broadcast") return "#broadcast";
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

function nameStyleCss(profile: Profile | null): CSSProperties {
  if (!profile) return {};
  const font =
    profile.nameStyle.font === "serif"
      ? "ui-serif, Georgia, Cambria, \"Times New Roman\", Times, serif"
      : profile.nameStyle.font === "mono"
        ? "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace"
        : "ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial";

  if (profile.nameStyle.colorMode === "gradient") {
    return {
      fontFamily: font,
      backgroundImage: `linear-gradient(90deg, ${profile.nameStyle.gradientFrom}, ${profile.nameStyle.gradientTo})`,
      WebkitBackgroundClip: "text",
      color: "transparent"
    };
  }
  return { fontFamily: font, color: profile.nameStyle.solid };
}

export default function App() {
  const [connected, setConnected] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [messagesByConv, setMessagesByConv] = useState<Record<string, Message[]>>({});
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

  const [userName] = useState("You");
  const [content, setContent] = useState("");
  const [uiError, setUiError] = useState<string | null>(null);

  const wsRef = useRef<ReturnType<typeof createWs> | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const saveProfilesTimerRef = useRef<number | null>(null);

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
    return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
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
      void putProfilesState({ profiles: Object.values(profiles.byId) }).catch(() => {
        // ignore
      });
    }, 600);
    return () => {
      if (saveProfilesTimerRef.current) window.clearTimeout(saveProfilesTimerRef.current);
    };
  }, [profiles, remoteSyncDone]);

  const pcIndex = useMemo(() => {
    const byId = new Map<string, { id: string; name: string }>();
    const byName = new Map<string, { id: string; name: string }>();
    for (const pc of pcs) {
      byId.set(pc.id.toLowerCase(), pc);
      byName.set(pc.name.toLowerCase(), pc);
    }
    return { byId, byName };
  }, [pcs]);

  const broadcastConversations = useMemo(
    () => conversations.filter((c) => c.kind === "broadcast"),
    [conversations]
  );
  const directConversations = useMemo(
    () => conversations.filter((c) => c.kind !== "broadcast"),
    [conversations]
  );

  useEffect(() => {
    const { ws, send } = createWs(wsUrl(), (msg: WsServerToClient) => {
      if (msg.type === "state") {
        setConversations(msg.payload.conversations);
        setMessagesByConv(msg.payload.messages_by_conversation);
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
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [activeConvId, messagesByConv[activeConvId]?.length]);

  const activeMessages = messagesByConv[activeConvId] || [];
  const activeConv = conversations.find((c) => c.id === activeConvId);

  const typingSet = typingByConv[activeConvId] || new Set<string>();
  const typingNames = pcs
    .filter((p) => typingSet.has(p.id))
    .map((p) => p.name)
    .join("、");

  const isDirectDraft = content.trim().toLowerCase().startsWith("/direct");
  const canSend = connected && content.trim().length > 0;

  function parseDirect(text: string): { ok: true; pcIds: string[]; message: string } | { ok: false; error: string } {
    const trimmed = text.trim();
    if (!trimmed.toLowerCase().startsWith("/direct")) return { ok: false, error: "not_direct" };
    const rest = trimmed.slice("/direct".length).trim();
    if (!rest) return { ok: false, error: "用法：/direct Alice 你好（可多个目标）" };

    const tokens = rest.split(/\s+/);
    const pcIds: string[] = [];
    let i = 0;
    while (i < tokens.length) {
      const t = tokens[i];
      if (t === ":" || t === "：" || t === "--") {
        i++;
        break;
      }
      const parts = t
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean)
        .map((x) => x.replace(/^@/, ""));

      if (!parts.length) break;

      const matched: string[] = [];
      let allMatch = true;
      for (const part of parts) {
        const key = part.toLowerCase();
        const pc = pcIndex.byId.get(key) || pcIndex.byName.get(key);
        if (!pc) {
          allMatch = false;
          break;
        }
        matched.push(pc.id);
      }
      if (!allMatch) break;
      pcIds.push(...matched);
      i++;
    }

    const message = tokens.slice(i).join(" ").replace(/^[:：]\s*/, "").trim();
    const unique = [...new Set(pcIds)];
    if (!unique.length) return { ok: false, error: "请在 /direct 后写目标，例如：/direct Alice 你好" };
    if (!message) return { ok: false, error: "请在目标后写消息内容，例如：/direct Alice 你好" };
    return { ok: true, pcIds: unique, message };
  }

  function sendInject() {
    if (!wsRef.current) return;
    const text = content.trim();
    if (!text) return;
    setUiError(null);

    const direct = parseDirect(text);
    if (direct.ok) {
      wsRef.current.send({
        type: "user_inject",
        content: direct.message,
        target: { kind: "direct", pc_ids: direct.pcIds }
      });
      setContent("");
      return;
    }
    if (!direct.ok && direct.error !== "not_direct") {
      setUiError(direct.error);
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
            <div className="groupTitle">广播频道</div>
            {broadcastConversations.map((c) => (
              <button
                key={c.id}
                className={`convItem ${c.id === activeConvId ? "active" : ""}`}
                onClick={() => setActiveConvId(c.id)}
              >
                {conversationLabel(c, profiles)}
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
                {conversationLabel(c, profiles)}
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
              <div className="userNameSubName">{userProfile?.displayName || userName}</div>
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
            <button onClick={() => setPaused(true)} disabled={!connected || queueState?.paused === true}>
              暂停
            </button>
            <button onClick={() => setPaused(false)} disabled={!connected || queueState?.paused === false}>
              继续
            </button>
          </div>
        </div>

        <div className="messages">
          {activeMessages.map((m) => {
            const name = chatDisplayName(profiles, m.from_actor);
            const p = getProfile(profiles, m.from_actor);
            return (
              <div key={m.id} className="msgRow">
                <button className="avatarBtn" onClick={(e) => openProfile(m.from_actor, e)} title="查看资料">
                  <div className="avatar" title={name}>
                    {p?.avatarUrl ? (
                      <img
                        src={absoluteAssetUrl(p.avatarUrl)}
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
                    <div className="name" style={nameStyleCss(p)}>
                      {name}
                    </div>
                    <div className="time">{formatTime(m.timestamp)}</div>
                  </div>
                  <div className="content">{m.content}</div>
                </div>
              </div>
            );
          })}
          <div ref={messagesEndRef} />
        </div>

        {typingNames ? <div className="typing">… {typingNames} 正在输入…</div> : <div className="typing" />}

        <div className="composer">
          <div className="composerInner">
            <div className="composerMeta">
              <div className={`pill ${isDirectDraft ? "direct" : ""}`}>{isDirectDraft ? "direct" : "#broadcast"}</div>
              <div className="hint">
                私聊：<code>/direct</code> 例如 <code>/direct Alice 你好</code>（可多目标）
              </div>
            </div>
            <textarea
              value={content}
              placeholder={connected ? "输入消息（默认广播）…" : "正在连接后端…"}
              onChange={(e) => {
                setContent(e.target.value);
                if (uiError) setUiError(null);
              }}
              onKeyDown={(e) => {
                if ((e.ctrlKey || e.metaKey) && e.key === "Enter") sendInject();
              }}
            />
            {uiError ? <div className="error">{uiError}</div> : null}
          </div>
          <button className="primary sendBtn" onClick={sendInject} disabled={!canSend}>
            发送
          </button>
        </div>
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
