from __future__ import annotations

import base64
import json
from typing import Any
from uuid import uuid4

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .db import SqliteStore
from .engine import DemoEngine
from .models import Actor, Message, WsClientToServer
from .settings import get_settings
from .ws import ConnectionManager


settings = get_settings()
store = SqliteStore(settings.sqlite_path)
ws_manager = ConnectionManager()
engine = DemoEngine(settings=settings, store=store, ws=ws_manager)

app = FastAPI(title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    await store.init()
    await store.upsert_conversations(engine.build_default_conversations())
    await engine.start()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/settings")
async def get_settings_state() -> dict[str, Any]:
    appearance_json = await store.get_setting_json("appearance_state")
    profiles_json = await store.get_setting_json("profiles_state")
    return {
        "appearance_state": json.loads(appearance_json) if appearance_json else None,
        "profiles_state": json.loads(profiles_json) if profiles_json else None,
    }


@app.put("/api/settings/appearance")
async def put_appearance_state(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    await store.set_setting_json("appearance_state", json.dumps(payload))
    return {"ok": True}


@app.put("/api/settings/profiles")
async def put_profiles_state(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    await store.set_setting_json("profiles_state", json.dumps(payload))
    return {"ok": True}


@app.post("/api/assets")
async def upload_asset(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """
    Accepts JSON:
      - { "mime": "image/png", "data_base64": "..." }  (base64 without prefix), or
      - { "data_url": "data:image/png;base64,..." }
    Returns: { id, url }
    """
    mime = payload.get("mime")
    data_b64 = payload.get("data_base64")
    data_url = payload.get("data_url")

    if data_url and isinstance(data_url, str) and data_url.startswith("data:"):
        try:
            header, b64 = data_url.split(",", 1)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"bad data_url: {e}") from e
        if ";base64" not in header:
            raise HTTPException(status_code=400, detail="data_url must be base64")
        mime = header[5:].split(";", 1)[0]
        data_b64 = b64

    if not isinstance(mime, str) or not mime:
        raise HTTPException(status_code=400, detail="mime is required")
    if not isinstance(data_b64, str) or not data_b64:
        raise HTTPException(status_code=400, detail="data_base64 is required")

    try:
        data = base64.b64decode(data_b64, validate=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"bad base64: {e}") from e

    asset_id = str(uuid4())
    await store.put_asset(asset_id, mime, data)
    return {"id": asset_id, "url": f"/api/assets/{asset_id}"}


@app.get("/api/assets/{asset_id}")
async def get_asset(asset_id: str) -> Response:
    asset = await store.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="not found")
    mime, data = asset
    return Response(content=data, media_type=mime)


async def _send_state(websocket: WebSocket) -> None:
    conversations = await store.list_conversations()
    messages_by_conv: dict[str, list[dict[str, Any]]] = {}
    for conv in conversations:
        msgs = await store.list_messages(conv.id, limit=200)
        messages_by_conv[conv.id] = [m.model_dump() for m in msgs]

    await websocket.send_json(
        {
            "type": "state",
            "payload": {
                "conversations": [c.model_dump() for c in conversations],
                "messages_by_conversation": messages_by_conv,
            },
        }
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await ws_manager.connect(websocket)
    try:
        await _send_state(websocket)
        while True:
            raw = await websocket.receive_text()
            try:
                data = WsClientToServer.model_validate_json(raw)
            except Exception as e:  # noqa: BLE001
                await websocket.send_json({"type": "error", "payload": {"message": f"bad request: {e}"}})
                continue

            if data.type in ("hello", "request_state"):
                await _send_state(websocket)
                continue

            if data.type == "pause":
                await engine.set_paused(bool(data.value))
                continue

            if data.type == "resume":
                await engine.set_paused(False)
                continue

            if data.type == "user_inject":
                content = (data.content or "").strip()
                target = data.target or {}
                if not content:
                    await websocket.send_json(
                        {"type": "error", "payload": {"message": "content is required"}}
                    )
                    continue

                # 1) user -> DM message (always goes to broadcast for visibility in demo)
                user_msg = Message(
                    conversation_id="broadcast",
                    channel="broadcast",
                    from_actor=Actor(kind="user", id="user", name="You"),
                    to=[Actor(kind="dm", id="dm", name="DM")],
                    content=content,
                )
                await store.add_message(user_msg)
                await ws_manager.broadcast({"type": "message", "payload": user_msg.model_dump()})

                # 2) DM routes message: broadcast or per-PC direct copies
                kind = target.get("kind") if isinstance(target, dict) else None
                if kind == "direct":
                    pc_ids = target.get("pc_ids") or []
                    if not isinstance(pc_ids, list) or not pc_ids:
                        await websocket.send_json(
                            {"type": "error", "payload": {"message": "target.pc_ids is required"}}
                        )
                        continue
                    send_batch_id = engine.new_send_batch_id()
                    for pc_id in pc_ids:
                        conv_id = f"dm_to_{pc_id}"
                        dm_msg = Message(
                            conversation_id=conv_id,
                            channel="direct",
                            from_actor=Actor(kind="dm", id="dm", name="DM"),
                            to=[Actor(kind="pc", id=pc_id)],
                            content=content,
                            send_batch_id=send_batch_id,
                        )
                        await store.add_message(dm_msg)
                        await ws_manager.broadcast({"type": "message", "payload": dm_msg.model_dump()})
                        await engine.enqueue_pc_reaction(
                            conversation_id=conv_id, pc_id=pc_id, prompt=content
                        )
                else:
                    dm_msg = Message(
                        conversation_id="broadcast",
                        channel="broadcast",
                        from_actor=Actor(kind="dm", id="dm", name="DM"),
                        to=[],
                        content=content,
                    )
                    await store.add_message(dm_msg)
                    await ws_manager.broadcast({"type": "message", "payload": dm_msg.model_dump()})
                    for pc in engine.pcs:
                        await engine.enqueue_pc_reaction(
                            conversation_id="broadcast", pc_id=pc.id, prompt=content
                        )

    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket)
