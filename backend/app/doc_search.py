from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypedDict

from .db import SqliteStore
from .mcp_stdio import McpServerConfig, McpStdioClient
from .settings import Settings


class DocSearchItem(TypedDict, total=False):
    title: str
    snippet: str
    score: float


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _maybe_json_load(s: str) -> Any | None:
    ss = (s or "").strip()
    if not ss:
        return None
    try:
        return json.loads(ss)
    except Exception:
        return None


def _extract_mcp_result_payload(resp: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(resp, dict):
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("isError") is True:
        return None
    if "results" in result and isinstance(result.get("results"), list):
        return result

    content = result.get("content")
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
        joined = "\n".join(t.strip() for t in texts if t.strip())
        parsed = _maybe_json_load(joined)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"results": parsed}
        if joined:
            return {"text": joined}

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    if isinstance(structured, list):
        return {"results": structured}
    return result


def _extract_resource_payload(resp: dict[str, Any]) -> Any | None:
    if not isinstance(resp, dict):
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("isError") is True:
        return None
    contents = result.get("contents")
    if isinstance(contents, list) and contents:
        first = contents[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            parsed = _maybe_json_load(first["text"])
            return parsed if parsed is not None else first["text"]
        return first
    content = result.get("content")
    if isinstance(content, list) and content:
        first2 = content[0]
        if isinstance(first2, dict) and isinstance(first2.get("text"), str):
            parsed2 = _maybe_json_load(first2["text"])
            return parsed2 if parsed2 is not None else first2["text"]
        return first2
    return result


@dataclass(frozen=True)
class DocSearchLimits:
    max_limit: int = 8
    max_text_chars: int = 1200
    max_results: int = 15
    max_hops: int = 1


class DocSearchService:
    def __init__(
        self,
        *,
        store: SqliteStore,
        settings: Settings,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._client: McpStdioClient | None = None
        self._client_lock = asyncio.Lock()

    def enabled(self) -> bool:
        if not bool(getattr(self._settings, "doc_search_enabled", False)):
            return False
        cmd = getattr(self._settings, "doc_search_mcp_command", None)
        return isinstance(cmd, list) and all(isinstance(x, str) and x.strip() for x in cmd)

    async def refresh_public_index(self) -> dict[str, Any]:
        if not self.enabled():
            payload = {"enabled": False, "updated_at": _now_iso_utc(), "error": "doc_search disabled"}
            await self._store.set_setting_json("doc_public_index", json.dumps(payload, ensure_ascii=False))
            return payload

        try:
            client = await self._get_client()
            stats_resp = await client.read_resource(uri="stats://graphrag")
            config_resp = await client.read_resource(uri="config://graphrag")
            stats = _extract_resource_payload(stats_resp)
            cfg = _extract_resource_payload(config_resp)
            payload = {"enabled": True, "updated_at": _now_iso_utc(), "stats": stats, "config": cfg}
        except Exception as exc:  # noqa: BLE001
            payload = {"enabled": True, "updated_at": _now_iso_utc(), "error": str(exc)}

        await self._store.set_setting_json("doc_public_index", json.dumps(payload, ensure_ascii=False))
        return payload

    async def search(
        self,
        *,
        query_text: str,
        limit: int = 5,
        text_chars: int = 600,
        hops: int = 0,
    ) -> dict[str, Any]:
        q = (query_text or "").strip()
        if not q:
            return {"ok": True, "data": {"enabled": self.enabled(), "results": []}, "meta": {"reason": "empty query"}}

        if not self.enabled():
            return {"ok": True, "data": {"enabled": False, "results": []}, "meta": {"reason": "disabled"}}

        lim = DocSearchLimits(
            max_limit=int(getattr(self._settings, "doc_search_max_limit", 8) or 8),
            max_text_chars=int(getattr(self._settings, "doc_search_max_text_chars", 1200) or 1200),
            max_results=int(getattr(self._settings, "doc_search_max_results", 15) or 15),
            max_hops=int(getattr(self._settings, "doc_search_max_hops", 1) or 1),
        )

        limit2 = int(limit) if isinstance(limit, (int, float)) else 5
        limit2 = max(1, min(lim.max_limit, limit2))

        text_chars2 = int(text_chars) if isinstance(text_chars, (int, float)) else 600
        text_chars2 = max(120, min(lim.max_text_chars, text_chars2))

        hops2 = int(hops) if isinstance(hops, (int, float)) else 0
        hops2 = max(0, min(lim.max_hops, hops2))

        max_results = max(limit2, min(lim.max_results, max(10, limit2 * 3)))

        client = await self._get_client()
        resp = await client.call_tool(
            name="graphrag_search",
            arguments={
                "query": q,
                "top_k": limit2,
                "max_results": max_results,
                "text_chars": text_chars2,
                "hops": hops2,
                "direction": "both",
            },
        )

        payload = _extract_mcp_result_payload(resp)
        results_raw: Any = payload.get("results") if isinstance(payload, dict) else None
        items: list[DocSearchItem] = []
        if isinstance(results_raw, list):
            for r in results_raw:
                if not isinstance(r, dict):
                    continue
                score = r.get("score")
                score_f = float(score) if isinstance(score, (int, float)) else None
                min_score = getattr(self._settings, "doc_search_min_score", None)
                if isinstance(min_score, (int, float)) and score_f is not None and score_f < float(min_score):
                    continue

                source = r.get("source")
                title = r.get("title")
                node_id = r.get("node_id")
                text = r.get("text")
                meta = r.get("metadata")
                path = source if isinstance(source, str) else ""
                heading_path = ""
                if isinstance(meta, dict):
                    hp = meta.get("heading_path")
                    if isinstance(hp, str):
                        heading_path = hp
                    p2 = meta.get("path")
                    if isinstance(p2, str) and p2.strip():
                        path = p2
                if not heading_path and isinstance(title, str):
                    heading_path = title

                snippet = text if isinstance(text, str) else ""
                snippet = snippet.strip()
                if len(snippet) > text_chars2:
                    snippet = snippet[:text_chars2].rstrip() + "…"

                # Keep path/node_id only as internal keys for filtering/dedup; do not return them to the model.
                item: dict[str, Any] = {
                    "title": heading_path.strip() or (title.strip() if isinstance(title, str) else ""),
                    "score": score_f if score_f is not None else 0.0,
                    "snippet": snippet,
                    "_path": path.strip(),
                    "_node_id": str(node_id) if node_id is not None else "",
                }
                items.append(item)  # type: ignore[arg-type]

        allowed_prefixes = getattr(self._settings, "doc_search_allowed_path_prefixes", None)
        if isinstance(allowed_prefixes, list) and allowed_prefixes:
            allowed = [p for p in allowed_prefixes if isinstance(p, str) and p.strip()]
            if allowed:
                items = [it for it in items if any(str(it.get("_path") or "").startswith(p) for p in allowed)]

        seen_paths: set[str] = set()
        unique: list[DocSearchItem] = []
        for it in items:
            p = str(it.get("_path") or "").strip()
            key = p or str(it.get("_node_id") or "").strip()
            if not key:
                continue
            if key in seen_paths:
                continue
            seen_paths.add(key)
            it2: dict[str, Any] = {k: v for k, v in it.items() if not str(k).startswith("_")}
            unique.append(it2)  # type: ignore[arg-type]
            if len(unique) >= limit2:
                break

        return {
            "ok": True,
            "data": {
                "enabled": True,
                "query_text": q,
                "results": unique,
            },
            "meta": {
                "backend": "mcp:graphrag_search",
                "limit": limit2,
                "text_chars": text_chars2,
                "hops": hops2,
                "raw_count": len(items),
                "returned": len(unique),
            },
        }

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        self._client = None

    async def _get_client(self) -> McpStdioClient:
        async with self._client_lock:
            if self._client is not None:
                return self._client

            cmd = getattr(self._settings, "doc_search_mcp_command", None)
            if not isinstance(cmd, list) or not cmd:
                raise ValueError("doc_search_mcp_command missing")

            cwd = getattr(self._settings, "doc_search_mcp_cwd", None)
            cwd2 = cwd if isinstance(cwd, str) and cwd.strip() else None

            self._client = McpStdioClient(
                config=McpServerConfig(
                    command=[str(x) for x in cmd],
                    cwd=cwd2,
                    init_timeout_s=float(getattr(self._settings, "doc_search_mcp_init_timeout_s", 15.0) or 15.0),
                    request_timeout_s=float(getattr(self._settings, "doc_search_mcp_request_timeout_s", 45.0) or 45.0),
                ),
                logger=self._logger,
            )
            return self._client
