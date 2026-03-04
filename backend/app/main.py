from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .db import SqliteStore
from .demo_forum import DemoForumSeedConfig, build_demo_forum_seed
from .engine import DemoEngine, ForumChannel
from .llm import LlmService
from .models import Actor, Message, WsClientToServer
from .state import build_state_message
from .settings import get_settings
from .tick_runner import TickRunner
from .ws import ConnectionManager


settings = get_settings()
store = SqliteStore(settings.sqlite_path)
ws_manager = ConnectionManager()
llm = LlmService(store=store)
engine = DemoEngine(settings=settings, store=store, ws=ws_manager, llm=llm)
tick_runner = TickRunner(store=store, ws=ws_manager, engine=engine, settings=settings, llm=llm, tick_s=60.0)

app = FastAPI(title=settings.app_name)
logger = logging.getLogger(__name__)
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
    profiles_json = await store.get_setting_json("profiles_state")
    if profiles_json:
        try:
            engine.apply_profiles_state(json.loads(profiles_json))
        except Exception:  # noqa: BLE001
            pass
    channels_json = await store.get_setting_json("channels_state")
    if channels_json:
        try:
            channels_state = json.loads(channels_json)
        except Exception:  # noqa: BLE001
            channels_state = None
    else:
        channels_state = None

    if not isinstance(channels_state, dict):
        channels_state = {
            "broadcast": {"description": "闲聊/广播频道（固定存在，不可删除）"},
            "forums": [
                {
                    "id": "forum_rose_garden_salon", 
                    "title": "# 🍽️ 玫瑰茶会花园", 
                    "description": "贵族们的下午茶圣地，只为最尊贵的味蕾！在此尽情描写龙舌酱的鲜美、凤凰蛋挞的酥脆、百年酒窖的陈香……精灵贵族请优雅落座，人类贵族请别把茶洒在蕾丝上。"},
                {
                    "id": "forum_guilds_complex", 
                    "title": "# 🏛️ 城市公会建筑群", 
                    "description": "米克斯塔的平民行政中心！调查员公会、制图师公会、炼金行会、商会及其各个子支……这个城市拥有相当先进的公会体制和制度。"
                },
            ]
        }
        await store.set_setting_json("channels_state", json.dumps(channels_state))

    forums_raw = channels_state.get("forums") if isinstance(channels_state, dict) else []
    forum_channels: list[ForumChannel] = []
    seen_forum_ids: set[str] = set()
    if isinstance(forums_raw, list):
        for item in forums_raw:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            title = item.get("title")
            if not isinstance(cid, str) or not cid.strip():
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            cid_s = cid.strip()
            if cid_s == "broadcast" or cid_s.startswith("dm_to_"):
                continue
            if cid_s in seen_forum_ids:
                continue
            t = title.strip()
            if not t.startswith("#"):
                t = f"#{t}"
            seen_forum_ids.add(cid_s)
            desc = item.get("description")
            forum_channels.append(ForumChannel(id=cid_s, title=t, description=desc if isinstance(desc, str) else None))

    broadcast_desc = None
    b = channels_state.get("broadcast") if isinstance(channels_state, dict) else None
    if isinstance(b, dict):
        d = b.get("description")
        if isinstance(d, str):
            broadcast_desc = d

    await store.sync_conversations(
        engine.build_conversations(forum_channels=forum_channels, broadcast_description=broadcast_desc)
    )

    # demo seed: threads + posts for forum channels
    seed_cfg: DemoForumSeedConfig | None = None
    seed_path = settings.demo_forum_seed_path
    if isinstance(seed_path, str) and seed_path.strip():
        p = Path(seed_path)
        if p.exists() and p.is_file():
            try:
                seed_cfg = DemoForumSeedConfig.model_validate_json(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                logger.warning("Failed to load demo forum seed config: %s", str(p), exc_info=True)

    now = datetime.now(timezone.utc)
    all_forum_posts: list[Message] = []
    for ch in forum_channels:
        announcements = None
        if seed_cfg:
            ch_seed = seed_cfg.channels.get(ch.id)
            if ch_seed and ch_seed.announcements is not None:
                announcements = ch_seed.announcements
            else:
                defaults = seed_cfg.defaults
                if defaults and defaults.announcements is not None:
                    announcements = defaults.announcements
        threads, posts = build_demo_forum_seed(
            channel_id=ch.id,
            channel_title=ch.title,
            pcs=[(p.id, p.name) for p in engine.pcs],
            now=now,
            announcements=announcements,
        )
        await store.upsert_forum_threads(threads)
        all_forum_posts.extend(posts)
    for m in all_forum_posts:
        await store.add_message_ignore(m)
    await engine.start()
    await tick_runner.start()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/settings")
async def get_settings_state() -> dict[str, Any]:
    appearance_json = await store.get_setting_json("appearance_state")
    profiles_json = await store.get_setting_json("profiles_state")
    channels_json = await store.get_setting_json("channels_state")
    return {
        "appearance_state": json.loads(appearance_json) if appearance_json else None,
        "profiles_state": json.loads(profiles_json) if profiles_json else None,
        "channels_state": json.loads(channels_json) if channels_json else None,
    }


@app.put("/api/settings/appearance")
async def put_appearance_state(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    await store.set_setting_json("appearance_state", json.dumps(payload))
    return {"ok": True}


@app.put("/api/settings/profiles")
async def put_profiles_state(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    await store.set_setting_json("profiles_state", json.dumps(payload))
    engine.apply_profiles_state(payload)

    channels_json = await store.get_setting_json("channels_state")
    channels_state: Any | None
    if channels_json:
        try:
            channels_state = json.loads(channels_json)
        except Exception:  # noqa: BLE001
            channels_state = None
    else:
        channels_state = None

    broadcast_desc = None
    forums_raw = None
    if isinstance(channels_state, dict):
        b = channels_state.get("broadcast")
        if isinstance(b, dict):
            d = b.get("description")
            if isinstance(d, str):
                broadcast_desc = d
        forums_raw = channels_state.get("forums")

    forum_channels: list[ForumChannel] = []
    seen_forum_ids: set[str] = set()
    if isinstance(forums_raw, list):
        for item in forums_raw:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            title = item.get("title")
            if not isinstance(cid, str) or not cid.strip():
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            cid_s = cid.strip()
            if cid_s == "broadcast" or cid_s.startswith("dm_to_"):
                continue
            if cid_s in seen_forum_ids:
                continue
            seen_forum_ids.add(cid_s)
            t = title.strip()
            if not t.startswith("#"):
                t = f"#{t}"
            desc = item.get("description")
            forum_channels.append(ForumChannel(id=cid_s, title=t, description=desc if isinstance(desc, str) else None))

    await store.sync_conversations(engine.build_conversations(forum_channels=forum_channels, broadcast_description=broadcast_desc))
    return {"ok": True}


@app.put("/api/settings/channels")
async def put_channels_state(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    broadcast_out: dict[str, str] = {}
    broadcast_in = payload.get("broadcast")
    if isinstance(broadcast_in, dict):
        d = broadcast_in.get("description")
        if isinstance(d, str):
            broadcast_out["description"] = d

    forums_raw = payload.get("forums")
    forums_out: list[dict[str, str]] = []
    forum_channels: list[ForumChannel] = []
    seen_forum_ids: set[str] = set()

    if isinstance(forums_raw, list):
        for item in forums_raw:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            title = item.get("title")
            if not isinstance(cid, str) or not cid.strip():
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            desc = item.get("description")
            t = title.strip()
            if not t.startswith("#"):
                t = f"#{t}"
            cid_s = cid.strip()
            if cid_s == "broadcast" or cid_s.startswith("dm_to_"):
                continue
            if cid_s in seen_forum_ids:
                continue
            seen_forum_ids.add(cid_s)
            out = {"id": cid_s, "title": t}
            if isinstance(desc, str) and desc:
                out["description"] = desc
            forums_out.append(out)
            forum_channels.append(ForumChannel(id=cid_s, title=t, description=desc if isinstance(desc, str) else None))

    state = {"broadcast": broadcast_out, "forums": forums_out}
    await store.set_setting_json("channels_state", json.dumps(state))
    await store.sync_conversations(
        engine.build_conversations(
            forum_channels=forum_channels,
            broadcast_description=broadcast_out.get("description") or None,
        )
    )
    return {"ok": True}


@app.delete("/api/forum/threads/{thread_id}")
async def delete_forum_thread(thread_id: str) -> dict[str, Any]:
    await store.delete_forum_thread(thread_id)
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
    await websocket.send_json(await build_state_message(store=store))


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

            if data.type == "delete_message":
                message_id = (data.message_id or "").strip()
                if not message_id:
                    await websocket.send_json({"type": "error", "payload": {"message": "message_id is required"}})
                    continue

                msg = await store.get_message(message_id)
                if msg is not None and msg.from_actor.kind != "pc":
                    await websocket.send_json(
                        {"type": "error", "payload": {"message": "only pc messages can be deleted in this version"}}
                    )
                    continue

                ids_to_delete: set[str] = {message_id}
                thread_ids: set[str] = set()
                if msg is not None:
                    if msg.send_batch_id:
                        batch_msgs = await store.list_messages_by_send_batch_id(msg.send_batch_id, limit=500)
                        for bm in batch_msgs:
                            if bm.from_actor.kind != "pc":
                                continue
                            ids_to_delete.add(bm.id)
                            if bm.thread_id:
                                thread_ids.add(bm.thread_id)
                    else:
                        if msg.thread_id:
                            thread_ids.add(msg.thread_id)

                    await store.delete_messages_by_ids(ids_to_delete)
                    await store.delete_pc_activity_by_message_ids(ids_to_delete)
                    for tid in thread_ids:
                        await store.rebuild_forum_thread_meta(tid)

                await ws_manager.broadcast(
                    {"type": "message_deleted", "payload": {"message_ids": sorted(ids_to_delete)}}
                )
                continue

            if data.type == "edit_message":
                message_id = (data.message_id or "").strip()
                if not message_id:
                    await websocket.send_json({"type": "error", "payload": {"message": "message_id is required"}})
                    continue
                new_content = (data.content or "").strip()
                if not new_content:
                    await websocket.send_json({"type": "error", "payload": {"message": "content is required"}})
                    continue

                msg = await store.get_message(message_id)
                if msg is None:
                    await websocket.send_json({"type": "error", "payload": {"message": "message not found"}})
                    continue
                if msg.from_actor.kind != "pc":
                    await websocket.send_json(
                        {"type": "error", "payload": {"message": "only pc messages can be edited in this version"}}
                    )
                    continue

                msgs_to_update: list[Message] = []
                if msg.send_batch_id:
                    batch_msgs = await store.list_messages_by_send_batch_id(msg.send_batch_id, limit=500)
                    for bm in batch_msgs:
                        if bm.from_actor.kind != "pc":
                            continue
                        msgs_to_update.append(bm.model_copy(update={"content": new_content}))
                else:
                    msgs_to_update.append(msg.model_copy(update={"content": new_content}))

                if msgs_to_update:
                    await store.update_messages_payload(msgs_to_update)
                ids = sorted({m.id for m in msgs_to_update}) or [message_id]
                await ws_manager.broadcast(
                    {"type": "message_edited", "payload": {"message_ids": ids, "content": new_content}}
                )
                continue

            if data.type == "forum_post":
                content = (data.content or "").strip()
                if not content:
                    await websocket.send_json({"type": "error", "payload": {"message": "content is required"}})
                    continue
                channel_id = (data.channel_id or "").strip()
                thread_id = (data.thread_id or "").strip()
                if not channel_id or not thread_id:
                    await websocket.send_json(
                        {"type": "error", "payload": {"message": "channel_id and thread_id are required"}}
                    )
                    continue

                convs = await store.list_conversations()
                conv = next((c for c in convs if c.id == channel_id), None)
                if conv is None or conv.kind != "forum":
                    await websocket.send_json({"type": "error", "payload": {"message": "forum channel not found"}})
                    continue

                thread = await store.get_forum_thread(thread_id)
                if thread is None or thread.channel_id != channel_id:
                    await websocket.send_json({"type": "error", "payload": {"message": "thread not found"}})
                    continue

                msg = Message(
                    conversation_id=channel_id,
                    channel="broadcast",
                    thread_id=thread_id,
                    from_actor=Actor(kind="user", id="user", name="You"),
                    to=[],
                    content=content,
                )
                await store.append_message(msg)
                await ws_manager.broadcast({"type": "message", "payload": msg.model_dump()})
                continue

            if data.type == "user_inject":
                content = (data.content or "").strip()
                target = data.target or {}
                if not content:
                    await websocket.send_json(
                        {"type": "error", "payload": {"message": "content is required"}}
                    )
                    continue

                origin_channel_id = (data.channel_id or "").strip() if data.channel_id else ""
                origin_thread_id = (data.thread_id or "").strip() if data.thread_id else ""
                if (origin_channel_id and not origin_thread_id) or (origin_thread_id and not origin_channel_id):
                    await websocket.send_json(
                        {
                            "type": "error",
                            "payload": {"message": "channel_id and thread_id must be both provided"},
                        }
                    )
                    continue

                if origin_channel_id:
                    convs = await store.list_conversations()
                    origin_conv = next((c for c in convs if c.id == origin_channel_id), None)
                    if origin_conv is None or origin_conv.kind != "forum":
                        await websocket.send_json(
                            {"type": "error", "payload": {"message": "origin forum channel not found"}}
                        )
                        continue
                    thread = await store.get_forum_thread(origin_thread_id)
                    if thread is None or thread.channel_id != origin_channel_id:
                        await websocket.send_json({"type": "error", "payload": {"message": "origin thread not found"}})
                        continue

                kind = target.get("kind") if isinstance(target, dict) else None
                user_msg_channel = "direct" if kind == "direct" else "broadcast"
                send_batch_id = engine.new_send_batch_id() if kind == "direct" else None

                # 1) user -> DM message
                # NOTE: We still store it in the origin conversation/thread (or "broadcast" by default)
                # for traceability/visibility, but the `channel` reflects whether it's a direct DM intent.
                user_msg = Message(
                    conversation_id=origin_channel_id or "broadcast",
                    channel=user_msg_channel,
                    thread_id=origin_thread_id or None,
                    from_actor=Actor(kind="user", id="user", name="You"),
                    to=[Actor(kind="dm", id="dm", name="DM")],
                    content=content,
                    send_batch_id=send_batch_id,
                )
                await store.append_message(user_msg)
                await ws_manager.broadcast({"type": "message", "payload": user_msg.model_dump()})

                # 2) DM routes message: broadcast or per-PC direct copies
                if kind == "direct":
                    pc_ids = target.get("pc_ids") or []
                    if not isinstance(pc_ids, list) or not pc_ids:
                        await websocket.send_json(
                            {"type": "error", "payload": {"message": "target.pc_ids is required"}}
                        )
                        continue
                    pc_target = None
                    if len(pc_ids) == 1 and isinstance(pc_ids[0], str):
                        pc_target = next((p for p in engine.pcs if p.id == pc_ids[0]), None)
                    try:
                        dm_content = await engine.dm_forward(
                            content=content,
                            pc=pc_target,
                            conversation_id=origin_channel_id or "broadcast",
                            thread_id=origin_thread_id or None,
                        )
                    except Exception:  # noqa: BLE001
                        dm_content = content

                    enqueue_tasks = []
                    for pc_id in pc_ids:
                        conv_id = f"dm_to_{pc_id}"
                        dm_msg = Message(
                            conversation_id=conv_id,
                            channel="direct",
                            thread_id=origin_thread_id or None,
                            from_actor=Actor(kind="dm", id="dm", name="DM"),
                            to=[Actor(kind="pc", id=pc_id)],
                            content=dm_content,
                            send_batch_id=send_batch_id,
                        )
                        await store.append_message(dm_msg)
                        await ws_manager.broadcast({"type": "message", "payload": dm_msg.model_dump()})
                        enqueue_tasks.append(
                            engine.enqueue_pc_reaction(
                                conversation_id=conv_id,
                                pc_id=pc_id,
                                prompt=dm_content,
                                thread_id=origin_thread_id or None,
                            )
                        )
                    if enqueue_tasks:
                        await asyncio.gather(*enqueue_tasks)
                else:
                    try:
                        dm_content = await engine.dm_forward(
                            content=content,
                            pc=None,
                            conversation_id=origin_channel_id or "broadcast",
                            thread_id=origin_thread_id or None,
                        )
                    except Exception:  # noqa: BLE001
                        dm_content = content
                    dm_msg = Message(
                        conversation_id=origin_channel_id or "broadcast",
                        channel="broadcast",
                        thread_id=origin_thread_id or None,
                        from_actor=Actor(kind="dm", id="dm", name="DM"),
                        to=[],
                        content=dm_content,
                    )
                    await store.append_message(dm_msg)
                    await ws_manager.broadcast({"type": "message", "payload": dm_msg.model_dump()})
                    await asyncio.gather(
                        *(
                            engine.enqueue_pc_reaction(
                                conversation_id=origin_channel_id or "broadcast",
                                pc_id=pc.id,
                                prompt=dm_content,
                                thread_id=origin_thread_id or None,
                            )
                            for pc in engine.pcs
                        )
                    )

    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket)
