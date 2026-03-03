from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Actor, ForumThread, Message


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def build_demo_forum_seed(
    *,
    channel_id: str,
    channel_title: str,
    pcs: list[tuple[str, str]],
    now: datetime,
) -> tuple[list[ForumThread], list[Message]]:
    dm = Actor(kind="dm", id="dm", name="DM")
    pc_actors = [Actor(kind="pc", id=pc_id, name=name) for pc_id, name in pcs]

    def pc(i: int) -> Actor | None:
        return pc_actors[i] if i < len(pc_actors) else None

    t1 = f"{channel_id}:t1"

    posts: list[Message] = []

    def add_post(*, thread_id: str, post_id: str, dt: datetime, from_actor: Actor, content: str) -> None:
        posts.append(
            Message(
                id=post_id,
                timestamp=_iso(dt),
                conversation_id=channel_id,
                channel="broadcast",
                thread_id=thread_id,
                from_actor=from_actor,
                to=[],
                content=content,
            )
        )

    add_post(
        thread_id=t1,
        post_id=f"{t1}:p1",
        dt=now - timedelta(hours=36),
        from_actor=dm,
        content=f"这里是 {channel_title}（论坛频道）。每个 thread 是一个可追溯的剧情片段。",
    )

    def thread_from_posts(thread_id: str, title: str) -> ForumThread:
        thread_posts = [p for p in posts if p.thread_id == thread_id]
        created_at = thread_posts[0].timestamp if thread_posts else _iso(now)
        last_activity_at = thread_posts[-1].timestamp if thread_posts else created_at
        reply_count = max(0, len(thread_posts) - 1)
        return ForumThread(
            id=thread_id,
            channel_id=channel_id,
            title=title,
            created_at=created_at,
            created_by=dm,
            last_activity_at=last_activity_at,
            reply_count=reply_count,
        )

    threads = [
        thread_from_posts(t1, "【公告】发帖指南 - 请勿回复")
    ]
    threads.sort(key=lambda t: t.last_activity_at, reverse=True)

    return threads, posts

