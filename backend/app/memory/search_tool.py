from __future__ import annotations

import math
from typing import Any, TYPE_CHECKING

from .vector import memory_vector_enabled
from .vector_provider import LlmVectorProvider
from ..text_utils import clean_keywords

if TYPE_CHECKING:
    from ..tick_runner import TickRunner


def memory_search_tool_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search memories for the current PC (private + optional public + optional validated direct scope).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pc_id": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12},
                    "include_public": {"type": "boolean", "default": True},
                    "direct_scope_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
                },
                "required": ["pc_id", "keywords"],
            },
        },
    }


def _validate_direct_scope_id(runner: "TickRunner", *, pc_id: str, direct_scope_id: str) -> str | None:
    scope_id = direct_scope_id.strip()
    if not scope_id:
        return None

    if scope_id == f"dm_to_{pc_id}":
        return scope_id

    if scope_id.startswith("pc_pair:"):
        parts = scope_id.split(":")
        if len(parts) != 3:
            return None
        left = parts[1].strip()
        right = parts[2].strip()
        if not left or not right:
            return None
        if pc_id not in {left, right}:
            return None
        other = right if left == pc_id else left
        if not any(p.id == other for p in runner._engine.pcs):  # noqa: SLF001
            return None
        left2, right2 = sorted([pc_id, other])
        return f"pc_pair:{left2}:{right2}"

    return None


async def handle_memory_search_tool(*, runner: "TickRunner", pc_id: str, args: dict[str, Any]) -> dict[str, Any]:
    tool_pc_id = runner._clean_str(args.get("pc_id"))  # noqa: SLF001
    keywords_raw = args.get("keywords")
    include_public_raw = args.get("include_public")
    direct_scope_raw = args.get("direct_scope_id")
    limit_raw = args.get("limit")

    if tool_pc_id != pc_id:
        return {"ok": False, "error": {"code": "PC_MISMATCH", "message": "pc_id mismatch"}}
    if not isinstance(keywords_raw, list):
        return {"ok": False, "error": {"code": "BAD_ARGS", "message": "keywords must be an array"}}

    cleaned_keywords = clean_keywords(keywords_raw, max_items=12)
    if not cleaned_keywords:
        return {"ok": False, "error": {"code": "BAD_ARGS", "message": "keywords must not be empty"}}

    include_public = True
    if isinstance(include_public_raw, bool):
        include_public = include_public_raw

    limit = 20
    if isinstance(limit_raw, (int, float)):
        limit = int(limit_raw)
    limit = max(1, min(60, limit))

    direct_scope_id: str | None = None
    if isinstance(direct_scope_raw, str) and direct_scope_raw.strip():
        direct_scope_id = _validate_direct_scope_id(runner, pc_id=pc_id, direct_scope_id=direct_scope_raw)
        if direct_scope_id is None:
            return {"ok": False, "error": {"code": "BAD_ARGS", "message": "invalid direct_scope_id"}}

    query_text = " ".join(cleaned_keywords).strip()

    lex_limit = int(getattr(runner._settings, "memory_hybrid_lex_candidates", 40) or 40)  # noqa: SLF001
    lex_limit = max(10, min(200, lex_limit))
    lex_candidates = await runner._store.search_memories(  # noqa: SLF001
        keywords=cleaned_keywords,
        owner_pc_id=pc_id,
        include_public=include_public,
        direct_scope_id=direct_scope_id,
        limit=max(lex_limit, limit),
    )

    vector_used = False
    vector_error: str | None = None
    vector_sims: dict[str, float] = {}
    vector_model = (getattr(runner._settings, "openai_embedding_model", None) or "").strip()  # noqa: SLF001
    min_sim = float(getattr(runner._settings, "memory_vector_min_sim", 0.72) or 0.72)  # noqa: SLF001
    top_k = int(getattr(runner._settings, "memory_vector_top_k", 30) or 30)  # noqa: SLF001
    scan_limit = int(getattr(runner._settings, "memory_vector_scan_limit", 1200) or 1200)  # noqa: SLF001
    if memory_vector_enabled(settings=runner._settings, llm=runner._llm) and vector_model:  # noqa: SLF001
        try:
            if runner._llm is None:  # noqa: SLF001
                raise RuntimeError("missing llm")
            provider = LlmVectorProvider(llm=runner._llm, settings=runner._settings)  # noqa: SLF001
            qvec = (await provider.embed_texts(model=vector_model, inputs=[query_text], timeout_s=30.0))[0]
            wanted_k = max(1, min(120, top_k))
            knn = await runner._store.knn_memory_summary_similarities(  # noqa: SLF001
                owner_pc_id=pc_id,
                include_public=include_public,
                direct_scope_id=direct_scope_id,
                model=vector_model,
                query_vector=qvec,
                k=wanted_k,
            )
            if knn is not None:
                for mid, sim in knn:
                    if sim >= min_sim:
                        vector_sims[mid] = float(sim)
                vector_used = True
            else:
                qnorm = math.sqrt(sum(v * v for v in qvec)) or 0.0
                if qnorm > 0:
                    rows = await runner._store.list_memory_summary_embeddings_for_vector_search(  # noqa: SLF001
                        owner_pc_id=pc_id,
                        include_public=include_public,
                        direct_scope_id=direct_scope_id,
                        model=vector_model,
                        scan_limit=max(50, scan_limit),
                    )
                    scored: list[tuple[str, float]] = []
                    for mid, vec in rows:
                        if not vec or len(vec) != len(qvec):
                            continue
                        dot = 0.0
                        vnorm = 0.0
                        for i, val in enumerate(vec):
                            dot += float(val) * float(qvec[i])
                            vnorm += float(val) * float(val)
                        denom = qnorm * (math.sqrt(vnorm) or 0.0)
                        if denom <= 0:
                            continue
                        sim = dot / denom
                        if sim >= min_sim:
                            scored.append((mid, sim))
                    scored.sort(key=lambda x: x[1], reverse=True)
                    for mid, sim in scored[:wanted_k]:
                        vector_sims[mid] = float(sim)
                    vector_used = True
        except Exception as exc:  # noqa: BLE001
            vector_error = f"{type(exc).__name__}: {exc}"

    candidate_ids: list[str] = []
    seen_ids: set[str] = set()
    for m in lex_candidates:
        if m.id not in seen_ids:
            seen_ids.add(m.id)
            candidate_ids.append(m.id)
    for mid in sorted(vector_sims.keys(), key=lambda k: vector_sims[k], reverse=True):
        if mid not in seen_ids:
            seen_ids.add(mid)
            candidate_ids.append(mid)

    candidates_by_id = {m.id: m for m in lex_candidates}
    missing_ids = [mid for mid in candidate_ids if mid not in candidates_by_id]
    if missing_ids:
        fetched = await runner._store.list_memories_by_ids(missing_ids)  # noqa: SLF001
        for m in fetched:
            candidates_by_id[m.id] = m

    max_kw = max(1, len(cleaned_keywords))
    w_sim = float(getattr(runner._settings, "memory_hybrid_w_sim", 1.0) or 1.0)  # noqa: SLF001
    w_lex = float(getattr(runner._settings, "memory_hybrid_w_lex", 0.35) or 0.35)  # noqa: SLF001
    w_score = float(getattr(runner._settings, "memory_hybrid_w_score", 0.05) or 0.05)  # noqa: SLF001
    w_pinned = float(getattr(runner._settings, "memory_hybrid_w_pinned", 0.2) or 0.2)  # noqa: SLF001

    def count_lex_hits(memory: Any) -> int:
        blob = f"{memory.summary}\n{memory.content}".casefold()
        hits = 0
        for kw in cleaned_keywords:
            if kw.casefold() in blob:
                hits += 1
        return hits

    ranked: list[tuple[float, str]] = []
    per_id_meta: dict[str, dict[str, Any]] = {}
    for mid in candidate_ids:
        memory = candidates_by_id.get(mid)
        if memory is None:
            continue
        sim = float(vector_sims.get(mid, 0.0))
        hits = count_lex_hits(memory)
        lex_ratio = hits / max_kw
        score_term = max(-3.0, min(10.0, float(memory.score))) / 10.0
        pinned_term = 1.0 if memory.pinned else 0.0
        final = (w_sim * sim) + (w_lex * lex_ratio) + (w_score * score_term) + (w_pinned * pinned_term)
        ranked.append((final, mid))

        via: list[str] = []
        if mid in vector_sims:
            via.append("vec")
        if hits > 0:
            via.append("lex")
        per_id_meta[mid] = {
            "final": round(final, 6),
            "sim": round(sim, 6),
            "lex_hits": hits,
            "score": memory.score,
            "via": via,
        }

    ranked.sort(
        key=lambda x: (
            x[0],
            1 if candidates_by_id.get(x[1]) and candidates_by_id[x[1]].pinned else 0,
            candidates_by_id.get(x[1]).score if candidates_by_id.get(x[1]) else 0,
            str(candidates_by_id.get(x[1]).updated_at if candidates_by_id.get(x[1]) else ""),
        ),
        reverse=True,
    )
    memories = [candidates_by_id[mid] for _, mid in ranked if mid in candidates_by_id][: max(1, limit)]

    items: list[dict[str, Any]] = []
    # Keep memory_search lightweight; the v4 executor has a tight total tool-output budget
    # shared across all tool calls in this tick.
    budget_chars = 6_000
    used_chars = 0
    omitted = 0
    for memory in memories:
        content = memory.content
        summary = memory.summary
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(summary, str):
            summary = ""

        approx = len(content) + len(summary) + 160
        if approx > budget_chars and not items:
            # Avoid blowing up tool output with a single gigantic memory; skip it.
            omitted += 1
            continue
        if used_chars + approx > budget_chars:
            omitted += 1
            continue

        items.append(
            {
                "id": memory.id,
                "scope": memory.scope,
                "scope_id": memory.scope_id,
                "kind": memory.kind,
                "summary": summary,
                "content": content,
                "updated_at": memory.updated_at,
                "rank": per_id_meta.get(memory.id),
            }
        )
        used_chars += approx

    truncated = omitted > 0
    try:
        await runner._store.touch_memories([it["id"] for it in items if isinstance(it, dict) and it.get("id")])  # noqa: SLF001
    except Exception:
        pass

    return {
        "ok": True,
        "data": {
            "items": items,
            "truncated": truncated,
            "omitted_count": omitted,
            "meta": {
                "query_text": query_text,
                "candidates": {"lex": len(lex_candidates), "vec": len(vector_sims), "union": len(candidate_ids)},
                "vector": {
                    "enabled": memory_vector_enabled(settings=runner._settings, llm=runner._llm),  # noqa: SLF001
                    "used": vector_used,
                    "model": vector_model or None,
                    "min_sim": min_sim,
                    "top_k": top_k,
                    "scan_limit": scan_limit,
                    "error": vector_error,
                },
                "weights": {"sim": w_sim, "lex": w_lex, "score": w_score, "pinned": w_pinned},
            },
        },
    }
