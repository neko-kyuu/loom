from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import uuid4

from .db import SqliteStore
from .models import utc_now_iso


DEFAULT_LLM_RPM_LIMIT = 5


def openai_chat_completions_url(base_url: str) -> str:
    b = base_url.strip().rstrip("/")
    return f"{b}/chat/completions"


def openai_embeddings_url(base_url: str) -> str:
    b = base_url.strip().rstrip("/")
    return f"{b}/embeddings"


class LlmRateLimit:
    """
    A simple rolling-window RPM limiter.

    Guarantees: at most `rpm` acquisitions in any trailing `window_s` seconds.
    """

    def __init__(self, *, rpm: int, window_s: float = 60.0) -> None:
        if rpm <= 0:
            raise ValueError("rpm must be > 0")
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self._rpm = rpm
        self._window_s = window_s
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._timestamps and (now - self._timestamps[0]) >= self._window_s:
                    self._timestamps.popleft()

                if len(self._timestamps) < self._rpm:
                    self._timestamps.append(now)
                    return

                sleep_s = self._window_s - (now - self._timestamps[0])

            await asyncio.sleep(max(0.0, sleep_s))


@dataclass(frozen=True)
class _QueuedRequest:
    request_id: str
    url: str
    apikey: str
    payload: dict[str, Any]
    timeout_s: float
    future: asyncio.Future[dict[str, Any]]


class LlmRequestManager:
    """
    Global queue + RPM limit.

    - Enqueued requests start in FIFO order.
    - Only in-flight requests are cancellable via cancel_inflight().
    - Requests still waiting in the queue will NOT be cancelled.
    """

    def __init__(self, *, rpm_limit: int = DEFAULT_LLM_RPM_LIMIT) -> None:
        self._rpm = LlmRateLimit(rpm=rpm_limit)
        self._queue: asyncio.Queue[_QueuedRequest] = asyncio.Queue()
        self._inflight: set[asyncio.Task[None]] = set()
        self._runner_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._runner_task is None or self._runner_task.done():
                self._runner_task = asyncio.create_task(self._runner(), name="llm-request-runner")

    async def enqueue(
        self,
        *,
        url: str,
        apikey: str,
        payload: dict[str, Any],
        timeout_s: float = 60.0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_started()

        rid = request_id or str(uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        await self._queue.put(
            _QueuedRequest(
                request_id=rid,
                url=url,
                apikey=apikey,
                payload=payload,
                timeout_s=timeout_s,
                future=fut,
            )
        )
        return await fut

    async def cancel_inflight(self) -> int:
        tasks = list(self._inflight)
        for t in tasks:
            t.cancel()
        return len(tasks)

    async def _runner(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._rpm.wait()
                task = asyncio.create_task(self._run_one(item), name=f"llm-request-{item.request_id}")
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
            finally:
                self._queue.task_done()

    async def _run_one(self, item: _QueuedRequest) -> None:
        try:
            result = await _post_json(
                url=item.url,
                apikey=item.apikey,
                payload=item.payload,
                timeout_s=item.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            if not item.future.done():
                item.future.set_exception(exc)
        else:
            if not item.future.done():
                item.future.set_result(result)


GLOBAL_LLM_MANAGER = LlmRequestManager(rpm_limit=DEFAULT_LLM_RPM_LIMIT)


class LlmParsedOutput(TypedDict):
    kind: Literal["structured", "markdown"]
    structured: Any | None
    markdown: str | None


class LlmChatResult(TypedDict):
    request_id: str
    created_at: str
    duration_ms: int
    status_code: int | None
    raw: Any | None
    parsed: LlmParsedOutput


def _strip_markdown_code_fence(text: str) -> str:
    s = text.strip()
    if not (s.startswith("```") and s.endswith("```")):
        return s
    inner = s[3:-3]
    inner = inner.lstrip()
    if "\n" in inner:
        first, rest = inner.split("\n", 1)
        if first.strip() in {"json", "javascript", "js"}:
            return rest.strip()
    return inner.strip()


def _try_parse_json(text: str) -> Any | None:
    s = _strip_markdown_code_fence(text)
    if not s:
        return None

    def _loads_maybe_twice(payload: str) -> Any | None:
        try:
            v = json.loads(payload)
        except Exception:  # noqa: BLE001
            return None
        if isinstance(v, str):
            vv = v.strip()
            if vv.startswith("{") or vv.startswith("["):
                try:
                    return json.loads(vv)
                except Exception:  # noqa: BLE001
                    return v
        return v

    parsed = _loads_maybe_twice(s)
    if parsed is not None:
        return parsed

    # Common LLM failure mode: produces "almost-JSON" with unescaped quotes/newlines
    # inside string values (e.g. `..."英文引号内"...`), which breaks strict JSON.
    ss = s.lstrip()
    if not (ss.startswith("{") or ss.startswith("[")):
        return None

    repaired_chars: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(s):
        if not in_string:
            repaired_chars.append(ch)
            if ch == '"':
                in_string = True
                escaped = False
            continue

        if escaped:
            repaired_chars.append(ch)
            escaped = False
            continue

        if ch == "\\":
            repaired_chars.append(ch)
            escaped = True
            continue

        if ch == "\n":
            repaired_chars.append("\\n")
            continue
        if ch == "\r":
            repaired_chars.append("\\n")
            continue
        if ch == "\t":
            repaired_chars.append("\\t")
            continue

        if ch == '"':
            j = i + 1
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            nxt = s[j] if j < len(s) else ""
            # If it doesn't look like the end of a JSON string, treat it as an
            # unescaped literal quote and escape it.
            if nxt and nxt not in {":", ",", "}", "]"}:
                repaired_chars.append('\\"')
            else:
                repaired_chars.append('"')
                in_string = False
            continue

        repaired_chars.append(ch)

    repaired = "".join(repaired_chars)
    if repaired == s:
        return None
    return _loads_maybe_twice(repaired)


def _normalize_markdown(text: str) -> str:
    s = text
    for _ in range(6):
        if "\\\\n" not in s:
            break
        s = s.replace("\\\\n", "\\n")
    return s


def parse_llm_response(raw: Any) -> LlmParsedOutput:
    """
    - Structured: tool_calls present OR assistant content is valid JSON.
    - Markdown: everything else (frontend can render it as Markdown).
    """
    if not isinstance(raw, dict):
        return {"kind": "markdown", "structured": None, "markdown": _normalize_markdown(str(raw))}

    non_json = raw.get("_non_json")
    if isinstance(non_json, str):
        return {"kind": "markdown", "structured": None, "markdown": _normalize_markdown(non_json)}

    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0]
        if isinstance(c0, dict):
            msg = c0.get("message")
            if isinstance(msg, dict):
                tool_calls = msg.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    return {"kind": "structured", "structured": tool_calls, "markdown": None}
                content = msg.get("content")
                if isinstance(content, str):
                    parsed = _try_parse_json(content)
                    if parsed is not None:
                        return {"kind": "structured", "structured": parsed, "markdown": None}
                    return {"kind": "markdown", "structured": None, "markdown": _normalize_markdown(content)}

    # fallback: if the response itself is JSON-ish, keep it structured
    return {"kind": "structured", "structured": raw, "markdown": None}


class LlmService:
    def __init__(self, *, store: SqliteStore, manager: LlmRequestManager = GLOBAL_LLM_MANAGER) -> None:
        self._store = store
        self._manager = manager

    async def cancel_inflight(self) -> int:
        return await self._manager.cancel_inflight()

    async def chat(
        self,
        *,
        url: str,
        apikey: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        timeout_s: float = 60.0,
        request_id: str | None = None,
    ) -> LlmChatResult:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools is not None:
            payload["tools"] = tools
        if extra:
            payload.update(extra)

        rid = request_id or str(uuid4())
        created_at = utc_now_iso()
        started = time.monotonic()

        status_code: int | None = None
        raw: Any | None = None
        error: str | None = None

        try:
            resp = await self._manager.enqueue(
                url=url,
                apikey=apikey,
                payload=payload,
                timeout_s=timeout_s,
                request_id=rid,
            )
            status_code = resp.get("status_code") if isinstance(resp, dict) else None
            raw = resp.get("json") if isinstance(resp, dict) else None
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            response_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None
            await asyncio.shield(
                self._store.add_llm_log(
                    log_id=rid,
                    created_at=created_at,
                    model=model,
                    request_json=json.dumps(payload, ensure_ascii=False),
                    response_json=response_json,
                    status_code=status_code,
                    error=error,
                    duration_ms=duration_ms,
                )
            )

        parsed = parse_llm_response(raw)
        return {
            "request_id": rid,
            "created_at": created_at,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "status_code": status_code,
            "raw": raw,
            "parsed": parsed,
        }

    async def embeddings(
        self,
        *,
        url: str,
        apikey: str,
        model: str,
        inputs: list[str],
        timeout_s: float = 60.0,
        request_id: str | None = None,
    ) -> list[list[float]]:
        if not isinstance(inputs, list) or not inputs:
            raise ValueError("inputs must be a non-empty list")

        payload: dict[str, Any] = {"model": model, "input": inputs}

        rid = request_id or str(uuid4())
        created_at = utc_now_iso()
        started = time.monotonic()

        status_code: int | None = None
        raw: Any | None = None
        error: str | None = None

        try:
            resp = await self._manager.enqueue(
                url=url,
                apikey=apikey,
                payload=payload,
                timeout_s=timeout_s,
                request_id=rid,
            )
            status_code = resp.get("status_code") if isinstance(resp, dict) else None
            raw = resp.get("json") if isinstance(resp, dict) else None
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            response_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None
            await asyncio.shield(
                self._store.add_llm_log(
                    log_id=rid,
                    created_at=created_at,
                    model=model,
                    request_json=json.dumps(payload, ensure_ascii=False),
                    response_json=response_json,
                    status_code=status_code,
                    error=error,
                    duration_ms=duration_ms,
                )
            )

        if not isinstance(raw, dict):
            raise RuntimeError("embeddings response is not a JSON object")
        data = raw.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError("embeddings response missing data[]")

        by_index: dict[int, list[float]] = {}
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            idx_raw = item.get("index", i)
            try:
                idx = int(idx_raw)
            except Exception:
                idx = i
            emb = item.get("embedding")
            if not isinstance(emb, list) or not emb:
                continue
            out: list[float] = []
            ok = True
            for v in emb:
                if not isinstance(v, (int, float)):
                    ok = False
                    break
                out.append(float(v))
            if not ok:
                continue
            by_index[idx] = out

        out_vectors: list[list[float]] = []
        for idx in range(len(inputs)):
            vec = by_index.get(idx)
            if vec is None:
                raise RuntimeError(f"embeddings response missing vector at index={idx}")
            out_vectors.append(vec)
        return out_vectors


async def _post_json(*, url: str, apikey: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    try:
        import httpx  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("httpx is required for external LLM requests (pip/uv add httpx).") from exc

    headers = {"Authorization": f"Bearer {apikey}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, headers=headers, json=payload)
        status = resp.status_code
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {"_non_json": resp.text}
        return {"status_code": status, "json": data}
