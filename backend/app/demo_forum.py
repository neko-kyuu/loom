from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from .models import Actor, ForumThread, Message


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class DemoForumAnnouncementSeed(BaseModel):
    """
    A single seeded (announcement) thread with a single initial post.

    Notes:
    - Prefer setting `key` to keep thread IDs stable even if you reorder items.
    - `{channel_title}` / `{channel_id}` placeholders are supported in `title` and `content`.
    """

    key: str | None = None
    thread_id: str | None = None
    title: str
    content: str
    hours_ago: float | None = None


class DemoForumChannelSeed(BaseModel):
    # None => inherit from defaults (if any); [] => seed nothing for this channel
    announcements: list[DemoForumAnnouncementSeed] | None = None


class DemoForumSeedConfig(BaseModel):
    defaults: DemoForumChannelSeed | None = None
    channels: dict[str, DemoForumChannelSeed] = Field(default_factory=dict)


def _sanitize_seed_key(key: str) -> str:
    s = (key or "").strip().lower()
    out: list[str] = []
    for ch in s:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch in {"_", "-"}:
            out.append(ch)
        else:
            out.append("_")
    cleaned = "".join(out).strip("_")
    return cleaned or "ann"


def _apply_placeholders(text: str, *, channel_id: str, channel_title: str) -> str:
    return (
        text.replace("{channel_id}", channel_id)
        .replace("{channel_title}", channel_title)
        .replace("{channel}", channel_title)
    )


def build_demo_forum_seed(
    *,
    channel_id: str,
    channel_title: str,
    pcs: list[tuple[str, str]],
    now: datetime,
    announcements: list[DemoForumAnnouncementSeed] | None = None,
) -> tuple[list[ForumThread], list[Message]]:
    dm = Actor(kind="dm", id="dm", name="DM")
    pc_actors = [Actor(kind="pc", id=pc_id, name=name) for pc_id, name in pcs]

    def pc(i: int) -> Actor | None:
        return pc_actors[i] if i < len(pc_actors) else None

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

    if announcements is None:
        t1 = f"{channel_id}:t1"
        add_post(
            thread_id=t1,
            post_id=f"{t1}:p1",
            dt=now - timedelta(hours=36),
            from_actor=dm,
            content=f"这里是 {channel_title}（论坛频道）。每个 thread 是一个可追溯的剧情片段。",
        )
    else:
        seen_thread_ids: set[str] = set()
        seeded: list[tuple[str, str]] = []
        for i, a in enumerate(announcements):
            thread_id = a.thread_id.strip() if isinstance(a.thread_id, str) and a.thread_id.strip() else None
            if not thread_id:
                if isinstance(a.key, str) and a.key.strip():
                    thread_id = f"{channel_id}:ann:{_sanitize_seed_key(a.key)}"
                else:
                    thread_id = f"{channel_id}:t{i + 1}"
            if thread_id in seen_thread_ids:
                thread_id = f"{thread_id}_{i + 1}"
            seen_thread_ids.add(thread_id)

            title = _apply_placeholders(a.title, channel_id=channel_id, channel_title=channel_title)
            content = _apply_placeholders(a.content, channel_id=channel_id, channel_title=channel_title)
            hours_ago = a.hours_ago if a.hours_ago is not None else (36 + i)
            seeded.append((thread_id, title))
            add_post(
                thread_id=thread_id,
                post_id=f"{thread_id}:p1",
                dt=now - timedelta(hours=float(hours_ago)),
                from_actor=dm,
                content=content,
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
            pinned=False,
            locked=False,
        )

    threads: list[ForumThread] = []
    if announcements is None:
        t1 = f"{channel_id}:t1"
        threads.append(thread_from_posts(t1, "【公告】发帖指南 - 请勿回复"))
    else:
        for thread_id, title in seeded:
            threads.append(thread_from_posts(thread_id, title))
    threads.sort(key=lambda t: t.last_activity_at, reverse=True)

    return threads, posts
