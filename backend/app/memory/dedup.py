from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from ..models import MemoryEntry, utc_now_iso
from .events import add_private_event_safely
from .vector import memory_vector_enabled, sha256_text
from .vector_provider import LlmVectorProvider

if TYPE_CHECKING:
    from ..tick_runner import TickRunner


def memory_write_dedup_enabled(*, runner: "TickRunner") -> bool:
    if not bool(getattr(runner._settings, "memory_write_dedup_enabled", True)):  # noqa: SLF001
        return False
    return memory_vector_enabled(settings=runner._settings, llm=runner._llm)  # noqa: SLF001


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    an = 0.0
    bn = 0.0
    for i, av in enumerate(a):
        bv = float(b[i])
        avf = float(av)
        dot += avf * bv
        an += avf * avf
        bn += bv * bv
    denom = (math.sqrt(an) or 0.0) * (math.sqrt(bn) or 0.0)
    if denom <= 0:
        return 0.0
    return dot / denom


def _trim_memory_text(text: str, *, max_len: int) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "…"
    return cleaned


def _merge_memory_meta_for_dedup(*, existing: dict[str, Any], incoming: dict[str, Any], sim: float, now: str) -> dict[str, Any]:
    meta = dict(existing or {})
    merged = meta.get("merged_sources")
    if not isinstance(merged, list):
        merged = []
    inc: dict[str, Any] = {}
    for k in ("ref_type", "ref_id", "message_id", "conversation_id", "thread_id", "send_batch_id", "action_type"):
        v = incoming.get(k) if isinstance(incoming, dict) else None
        if isinstance(v, str) and v.strip():
            inc[k] = v
    inc["merged_at"] = now
    inc["sim"] = round(float(sim), 6)
    merged.append(inc)
    meta["merged_sources"] = merged[-24:]
    meta["dedup_last_sim"] = round(float(sim), 6)
    meta["dedup_last_at"] = now

    keywords: list[str] = []
    for src in (existing, incoming):
        if not isinstance(src, dict):
            continue
        raw = src.get("keywords")
        if isinstance(raw, list):
            for v in raw:
                if isinstance(v, str) and v.strip():
                    keywords.append(v.strip())
    if keywords:
        deduped: list[str] = []
        seen: set[str] = set()
        for k in keywords:
            kk = k.casefold()
            if kk in seen:
                continue
            seen.add(kk)
            deduped.append(k)
            if len(deduped) >= 18:
                break
        meta["keywords"] = deduped

    if "merge_key" not in meta or not meta.get("merge_key"):
        mk = incoming.get("merge_key") if isinstance(incoming, dict) else None
        if isinstance(mk, str) and mk.strip():
            meta["merge_key"] = mk.strip()
    return meta


def _merge_memory_content_for_dedup(*, existing: str, incoming: str, max_len: int) -> str:
    a = (existing or "").strip()
    b = (incoming or "").strip()
    if not b:
        return _trim_memory_text(a, max_len=max_len)
    if not a:
        return _trim_memory_text(b, max_len=max_len)
    if b in a:
        return _trim_memory_text(a, max_len=max_len)
    if a in b:
        return _trim_memory_text(b, max_len=max_len)
    sep = "\n" if ("\n" in a or "\n" in b) else "；"
    merged = f"{a.rstrip('…')}{sep}{b}"
    return _trim_memory_text(merged, max_len=max_len)


async def dedup_merge_memories_on_write(
    *, runner: "TickRunner", entries: list[MemoryEntry]
) -> tuple[list[MemoryEntry], list[tuple[str, str, list[float]]]]:
    if not entries:
        return [], []
    if not memory_write_dedup_enabled(runner=runner):
        return entries, []

    model = (getattr(runner._settings, "openai_embedding_model", None) or "").strip()  # noqa: SLF001
    if not model:
        return entries, []
    if runner._llm is None:  # noqa: SLF001
        return entries, []
    provider = LlmVectorProvider(llm=runner._llm, settings=runner._settings)  # noqa: SLF001

    embed_secrets = bool(getattr(runner._settings, "memory_vector_embed_secrets", False))  # noqa: SLF001
    eligible: list[MemoryEntry] = []
    for e in entries:
        if e.kind == "secret" and not embed_secrets:
            continue
        if not isinstance(e.summary, str) or not e.summary.strip():
            continue
        eligible.append(e)
    if not eligible:
        return entries, []

    try:
        min_sim = float(getattr(runner._settings, "memory_write_dedup_min_sim", 0.9) or 0.9)  # noqa: SLF001
        scan_limit = int(getattr(runner._settings, "memory_write_dedup_scan_limit", 200) or 200)  # noqa: SLF001
        scan_limit = max(20, min(2000, scan_limit))
        max_age_days = int(getattr(runner._settings, "memory_write_dedup_max_age_days", 14) or 14)  # noqa: SLF001
        max_age_days = max(0, min(365, max_age_days))
        updated_after: str | None = None
        if max_age_days > 0:
            updated_after = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()

        vectors = await provider.embed_texts(model=model, inputs=[e.summary for e in eligible], timeout_s=30.0)
        vec_by_id: dict[str, list[float]] = {eligible[i].id: vectors[i] for i in range(min(len(eligible), len(vectors)))}

        merged_by_id: dict[str, MemoryEntry] = {}
        order: list[str] = []
        precomputed: list[tuple[str, str, list[float]]] = []

        for entry in entries:
            vec = vec_by_id.get(entry.id)
            if vec is None:
                # Not eligible for vector dedup; keep as-is.
                if entry.id not in merged_by_id:
                    merged_by_id[entry.id] = entry
                    order.append(entry.id)
                else:
                    merged_by_id[entry.id] = entry
                continue

            knn = await runner._store.knn_memory_summary_candidates_for_write_dedup(  # noqa: SLF001
                scope=entry.scope,
                owner_pc_id=entry.owner_pc_id,
                scope_id=entry.scope_id,
                kind=entry.kind,
                subject_id=entry.subject_id,
                model=model,
                query_vector=vec,
                scan_limit=scan_limit,
                updated_after=updated_after,
            )
            candidates: list[tuple[str, list[float], str]] = []
            candidates_knn: list[tuple[str, float, str]] = []
            if knn is not None:
                candidates_knn = knn
            else:
                candidates = await runner._store.list_memory_summary_embeddings_for_write_dedup(  # noqa: SLF001
                    scope=entry.scope,
                    owner_pc_id=entry.owner_pc_id,
                    scope_id=entry.scope_id,
                    kind=entry.kind,
                    subject_id=entry.subject_id,
                    model=model,
                    scan_limit=scan_limit,
                    updated_after=updated_after,
                )

            # Optional extra constraints for recent_event: require same conversation/thread if present.
            conv_required: str | None = None
            thread_required: str | None = None
            if entry.kind == "recent_event" and isinstance(entry.meta, dict):
                conv_required = entry.meta.get("conversation_id") if isinstance(entry.meta.get("conversation_id"), str) else None
                thread_required = entry.meta.get("thread_id") if isinstance(entry.meta.get("thread_id"), str) else None

            best_id: str | None = None
            best_sim = 0.0
            if candidates_knn:
                for mid, sim, meta_json in candidates_knn:
                    if mid == entry.id:
                        continue
                    if conv_required or thread_required:
                        try:
                            meta = json.loads(meta_json or "{}") if isinstance(meta_json, str) else {}
                        except Exception:  # noqa: BLE001
                            meta = {}
                        if conv_required and meta.get("conversation_id") != conv_required:
                            continue
                        if thread_required and meta.get("thread_id") != thread_required:
                            continue
                    if sim > best_sim:
                        best_sim = float(sim)
                        best_id = mid
            else:
                for mid, cvec, meta_json in candidates:
                    if mid == entry.id:
                        continue
                    if conv_required or thread_required:
                        try:
                            meta = json.loads(meta_json or "{}") if isinstance(meta_json, str) else {}
                        except Exception:  # noqa: BLE001
                            meta = {}
                        if conv_required and meta.get("conversation_id") != conv_required:
                            continue
                        if thread_required and meta.get("thread_id") != thread_required:
                            continue
                    sim = _cosine_sim(vec, cvec)
                    if sim > best_sim:
                        best_sim = sim
                        best_id = mid

            if best_id is None or best_sim < min_sim:
                if entry.id not in merged_by_id:
                    merged_by_id[entry.id] = entry
                    order.append(entry.id)
                else:
                    merged_by_id[entry.id] = entry
                precomputed.append((entry.id, sha256_text(entry.summary), vec))
                continue

            base = merged_by_id.get(best_id)
            if base is None:
                base = await runner._store.get_memory(best_id)  # noqa: SLF001
            if base is None:
                if entry.id not in merged_by_id:
                    merged_by_id[entry.id] = entry
                    order.append(entry.id)
                else:
                    merged_by_id[entry.id] = entry
                continue

            now = utc_now_iso()
            max_summary = max(1, int(runner._settings.memory_write_summary_chars))  # noqa: SLF001
            max_content = max(1, int(runner._settings.memory_write_content_chars))  # noqa: SLF001

            incoming_summary = entry.summary
            next_summary = base.summary
            if base.summary.strip() and base.summary.strip() in incoming_summary and len(incoming_summary) <= max_summary:
                next_summary = incoming_summary
                precomputed.append((best_id, sha256_text(next_summary), vec))
            else:
                # If the chosen summary equals the incoming summary, we can safely reuse the
                # incoming embedding to avoid a second identical embeddings request later.
                if sha256_text(next_summary) == sha256_text(incoming_summary):
                    precomputed.append((best_id, sha256_text(next_summary), vec))

            next_content = _merge_memory_content_for_dedup(
                existing=base.content,
                incoming=entry.content,
                max_len=max_content,
            )

            next_importance = max(int(base.importance), int(entry.importance))
            next_score = max(int(base.score), int(entry.score), next_importance)

            next_meta = _merge_memory_meta_for_dedup(
                existing=(base.meta if isinstance(base.meta, dict) else {}),
                incoming=(entry.meta if isinstance(entry.meta, dict) else {}),
                sim=float(best_sim),
                now=now,
            )

            merged = base.model_copy(
                update={
                    "updated_at": now,
                    "summary": next_summary,
                    "content": next_content,
                    "importance": next_importance,
                    "score": next_score,
                    "source_type": entry.source_type or base.source_type,
                    "revision": int(base.revision) + 1,
                    "meta": next_meta,
                }
            )

            merged_by_id[best_id] = merged
            if best_id not in order:
                order.append(best_id)

            await add_private_event_safely(
                runner=runner,
                pc_id=entry.owner_pc_id,
                type="memory_write_dedup_merge",
                summary=f"dedup merge: {entry.kind} -> {best_id}",
                consequences={
                    "merged_into_id": best_id,
                    "incoming_id": entry.id,
                    "kind": entry.kind,
                    "scope": entry.scope,
                    "scope_id": entry.scope_id,
                    "owner_pc_id": entry.owner_pc_id,
                    "subject_id": entry.subject_id,
                    "sim": round(float(best_sim), 6),
                    "source": {
                        "message_id": (entry.meta or {}).get("message_id") if isinstance(entry.meta, dict) else None,
                        "conversation_id": (entry.meta or {}).get("conversation_id") if isinstance(entry.meta, dict) else None,
                        "thread_id": (entry.meta or {}).get("thread_id") if isinstance(entry.meta, dict) else None,
                        "send_batch_id": (entry.meta or {}).get("send_batch_id") if isinstance(entry.meta, dict) else None,
                    },
                },
            )

        deduped = [merged_by_id[mid] for mid in order if mid in merged_by_id]
        return deduped, precomputed
    except Exception as exc:  # noqa: BLE001
        await add_private_event_safely(
            runner=runner,
            type="memory_write_dedup_error",
            summary=f"memory write dedup failed: {type(exc).__name__}",
            consequences={"error": f"{type(exc).__name__}: {exc}"},
        )
        return entries, []
