export type Actor = {
  kind: "user" | "dm" | "pc";
  id?: string | null;
  name?: string | null;
};

export type Conversation = {
  id: string;
  kind: "broadcast" | "dm_to_pc" | "pc_to_pc";
  title: string;
  participants: Actor[];
};

export type Message = {
  id: string;
  timestamp: string;
  conversation_id: string;
  channel: "broadcast" | "direct";
  from_actor: Actor;
  to: Actor[];
  content: string;
  send_batch_id?: string | null;
};

export type WsServerToClient =
  | { type: "state"; payload: { conversations: Conversation[]; messages_by_conversation: Record<string, Message[]> } }
  | { type: "message"; payload: Message }
  | { type: "typing"; payload: { conversation_id: string; pc_id: string; value: boolean } }
  | { type: "queue"; payload: { paused: boolean; queued: number } }
  | { type: "error"; payload: { message: string } };

export type WsClientToServer =
  | { type: "hello" }
  | { type: "request_state" }
  | { type: "pause"; value: boolean }
  | { type: "resume" }
  | { type: "user_inject"; content: string; target: { kind: "broadcast" } | { kind: "direct"; pc_ids: string[] } };

