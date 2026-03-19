from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class McpServerConfig:
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    init_timeout_s: float = 15.0
    request_timeout_s: float = 45.0


class McpStdioClient:
    def __init__(self, *, config: McpServerConfig, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)

        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def call_tool(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def read_resource(self, *, uri: str) -> dict[str, Any]:
        return await self.request("resources/read", {"uri": uri})

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(method, str) or not method.strip():
            raise ValueError("method required")

        async with self._lock:
            await self._ensure_started_locked()

            rid = self._next_id
            self._next_id += 1
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[rid] = fut

            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                payload["params"] = params

            try:
                await self._send_locked(payload)
            except Exception:
                self._pending.pop(rid, None)
                raise

        try:
            return await asyncio.wait_for(fut, timeout=float(self._config.request_timeout_s))
        finally:
            async with self._lock:
                self._pending.pop(rid, None)

    async def _ensure_started_locked(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return

        if not self._config.command:
            raise ValueError("mcp command missing")

        self._proc = await asyncio.create_subprocess_exec(
            *self._config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._config.cwd,
            env=self._config.env,
        )
        self._stdout_task = asyncio.create_task(self._stdout_loop(), name="mcp-stdio-stdout")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="mcp-stdio-stderr")

        init_params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "loom-backend", "version": "0.0.1"},
        }
        await self._send_locked({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": init_params})

        fut0 = self._pending.get(0)
        if fut0 is None:
            raise RuntimeError("mcp initialize future missing")

        try:
            init_res = await asyncio.wait_for(fut0, timeout=float(self._config.init_timeout_s))
        except Exception:
            await self._close_locked()
            raise
        finally:
            self._pending.pop(0, None)

        if init_res.get("error") is not None:
            await self._close_locked()
            raise RuntimeError("mcp initialize failed")

        await self._send_locked({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _close_locked(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._stdout_task = None
        self._stderr_task = None

        proc = self._proc
        self._proc = None
        if proc is None:
            return

        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except TimeoutError:
                    proc.kill()
        except Exception:
            pass

    async def _send_locked(self, msg: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("mcp process not running")
        raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + raw)
        await self._proc.stdin.drain()

        msg_id = msg.get("id")
        if isinstance(msg_id, int) and msg_id not in self._pending:
            loop = asyncio.get_running_loop()
            self._pending[msg_id] = loop.create_future()

    async def _stdout_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        while True:
            try:
                msg = await self._read_framed_json(proc.stdout)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._logger.warning("mcp stdout read error: %s", exc)
                await self.close()
                return

            if not isinstance(msg, dict):
                continue

            msg_id = msg.get("id")
            if isinstance(msg_id, int):
                async with self._lock:
                    fut = self._pending.get(msg_id)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            try:
                line = await proc.stderr.readline()
            except asyncio.CancelledError:
                return
            except Exception:
                return
            if not line:
                return
            s = line.decode("utf-8", errors="replace").rstrip("\n")
            if s:
                self._logger.debug("mcp stderr: %s", s)

    @staticmethod
    async def _read_framed_json(stream: asyncio.StreamReader) -> Any:
        header_bytes = await stream.readuntil(b"\r\n\r\n")
        header_text = header_bytes.decode("ascii", errors="replace")
        length: int | None = None
        for line in header_text.split("\r\n"):
            if not line:
                continue
            k, _, v = line.partition(":")
            if k.strip().lower() == "content-length":
                try:
                    length = int(v.strip())
                except Exception:
                    length = None
        if length is None or length < 0:
            raise ValueError("missing content-length")
        body = await stream.readexactly(length)
        return json.loads(body.decode("utf-8"))
