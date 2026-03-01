import type { WsClientToServer, WsServerToClient } from "../types";

export function createWs(url: string, onMessage: (msg: WsServerToClient) => void) {
  const ws = new WebSocket(url);

  ws.addEventListener("open", () => {
    const hello: WsClientToServer = { type: "hello" };
    ws.send(JSON.stringify(hello));
  });

  ws.addEventListener("message", (ev) => {
    try {
      const msg = JSON.parse(String(ev.data)) as WsServerToClient;
      onMessage(msg);
    } catch {
      // ignore
    }
  });

  return {
    ws,
    send(payload: WsClientToServer) {
      ws.send(JSON.stringify(payload));
    }
  };
}

