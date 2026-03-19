from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..models import Event

if TYPE_CHECKING:
    from ..tick_runner import TickRunner


async def add_event_safely(*, runner: "TickRunner", event: Event) -> None:
    try:
        await runner._store.add_event(event)  # noqa: SLF001
    except Exception:
        pass


async def add_private_event_safely(
    *,
    runner: "TickRunner",
    type: str,
    summary: str,
    pc_id: str | None = None,
    consequences: dict[str, Any] | None = None,
) -> None:
    await add_event_safely(
        runner=runner,
        event=Event(
            pc_id=pc_id,
            type=type,
            summary=summary,
            visibility="private",
            consequences=consequences or {},
        ),
    )

