export type Actor = {
  kind: "user" | "dm" | "pc";
  id?: string | null;
  name?: string | null;
};

export type Conversation = {
  id: string;
  kind: "broadcast" | "dm_to_pc" | "pc_to_pc" | "forum";
  title: string;
  description?: string | null;
  group?: string | null;
  participants: Actor[];
};

export type Message = {
  id: string;
  timestamp: string;
  conversation_id: string;
  channel: "broadcast" | "direct";
  thread_id?: string | null;
  from_actor: Actor;
  to: Actor[];
  content: string;
  send_batch_id?: string | null;
};

export type WsServerToClient =
  | {
      type: "state";
      payload: {
        conversations: Conversation[];
        messages_by_conversation: Record<string, Message[]>;
        forum_threads_by_channel?: Record<string, ForumThread[]>;
        forum_posts_by_thread?: Record<string, Message[]>;
      };
    }
  | { type: "message"; payload: Message }
  | { type: "message_deleted"; payload: { message_ids: string[] } }
  | { type: "message_edited"; payload: { message_ids: string[]; content: string } }
  | { type: "forum_thread"; payload: { thread: ForumThread } }
  | { type: "typing"; payload: { conversation_id: string; pc_id: string; value: boolean } }
  | { type: "queue"; payload: { paused: boolean; queued: number } }
  | { type: "error"; payload: { message: string } };

export type WsClientToServer =
  | { type: "hello" }
  | { type: "request_state" }
  | { type: "pause"; value: boolean }
  | { type: "resume" }
  | { type: "delete_message"; message_id: string }
  | { type: "edit_message"; message_id: string; content: string }
  | {
      type: "user_inject";
      content: string;
      target: { kind: "broadcast" } | { kind: "direct"; pc_ids: string[] };
      channel_id?: string;
      thread_id?: string;
    }
  | { type: "forum_post"; channel_id: string; thread_id: string; content: string };

export type ForumThread = {
  id: string;
  channel_id: string;
  title: string;
  created_at: string;
  created_by: Actor;
  last_activity_at: string;
  reply_count: number;
  pinned: boolean;
  locked: boolean;
};

export type ForumPost = {
  id: string;
  thread_id: string;
  timestamp: string;
  from_actor: Actor;
  content: string;
};

export type PcActivityLogItem = {
  id: string;
  pc_id: string;
  timestamp: string;
  summary: string;
};

export type PcActivityLogPage = {
  items: PcActivityLogItem[];
  next_cursor: string | null;
};

export type LlmLogMeta = {
  id: string;
  created_at: string;
  model?: string | null;
  status_code?: number | null;
  error?: string | null;
  duration_ms?: number | null;
};

export type LlmLogItem = LlmLogMeta & {
  request_json: string;
  response_json?: string | null;
};
