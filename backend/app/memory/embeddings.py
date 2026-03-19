from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import MemoryEntry, utc_now_iso
from .events import add_private_event_safely
from .vector import memory_vector_enabled, sha256_text
from .vector_provider import LlmVectorProvider

if TYPE_CHECKING:
    from ..tick_runner import TickRunner


async def maybe_upsert_memory_summary_embeddings(*, runner: "TickRunner", memories: list[MemoryEntry]) -> None:
    if not memory_vector_enabled(settings=runner._settings, llm=runner._llm):  # noqa: SLF001
        return
    if not memories:
        return

    embed_secrets = bool(getattr(runner._settings, "memory_vector_embed_secrets", False))  # noqa: SLF001
    model = (getattr(runner._settings, "openai_embedding_model", None) or "").strip()  # noqa: SLF001
    if not model:
        return
    if runner._llm is None:  # noqa: SLF001
        return
    provider = LlmVectorProvider(llm=runner._llm, settings=runner._settings)  # noqa: SLF001

    candidates: list[MemoryEntry] = []
    hashes: dict[str, str] = {}
    for memory in memories:
        if memory.deleted_at or memory.edit_state == "deleted":
            continue
        if memory.kind == "secret" and not embed_secrets:
            continue
        if not isinstance(memory.summary, str) or not memory.summary.strip():
            continue
        h = sha256_text(memory.summary)
        hashes[memory.id] = h
        candidates.append(memory)

    if not candidates:
        return

    try:
        existing = await runner._store.get_memory_summary_embedding_hashes(  # noqa: SLF001
            memory_ids=[m.id for m in candidates],
            model=model,
        )
        to_embed = [m for m in candidates if existing.get(m.id) != hashes.get(m.id)]
        if not to_embed:
            return

        vectors = await provider.embed_texts(model=model, inputs=[m.summary for m in to_embed], timeout_s=60.0)
        now = utc_now_iso()
        await runner._store.upsert_memory_summary_embeddings(  # noqa: SLF001
            model=model,
            items=[(m.id, hashes[m.id], vectors[i]) for i, m in enumerate(to_embed)],
            updated_at=now,
        )
    except Exception as exc:  # noqa: BLE001
        await add_private_event_safely(
            runner=runner,
            type="memory_embedding_error",
            summary=f"memory summary embedding failed: {type(exc).__name__}",
            consequences={"error": f"{type(exc).__name__}: {exc}"},
        )
