from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


def try_parse_json_loose(text: str) -> Any | None:
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("```") and s.endswith("```"):
        inner = s[3:-3].lstrip()
        if "\n" in inner:
            first, rest = inner.split("\n", 1)
            if first.strip() in {"json", "javascript", "js"}:
                s = rest.strip()
            else:
                s = inner.strip()
        else:
            s = inner.strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class ToolCallLimits:
    max_tool_rounds: int = 3
    max_tool_calls_per_round: int = 2
    max_total_tool_output_chars: int = 12_000


ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def run_tool_calling_loop(
    *,
    llm_chat: Callable[..., Awaitable[dict[str, Any]]],
    url: str,
    apikey: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_handler: ToolHandler,
    limits: ToolCallLimits | None = None,
) -> dict[str, Any]:
    """
    OpenAI-compatible tool-calling loop.

    Contract:
    - On tool call: append assistant(tool_calls) + tool results, then continue.
    - On final: return the parsed JSON object (must be a dict), otherwise return noop.
    - Any tool error (ok:false) => immediately return noop (no recovery loop).
    """
    lim = limits or ToolCallLimits()
    total_tool_output_chars = 0

    for _round in range(max(0, int(lim.max_tool_rounds)) + 1):
        res = await llm_chat(
            url=url,
            apikey=apikey,
            model=model,
            messages=messages,
            tools=tools,
        )

        raw = res.get("raw") if isinstance(res, dict) else None
        choices = raw.get("choices") if isinstance(raw, dict) else None
        if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
            return {"type": "noop", "reason": "llm response missing choices"}

        msg = choices[0].get("message")
        if not isinstance(msg, dict):
            return {"type": "noop", "reason": "llm response missing message"}

        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            if len(tool_calls) > max(0, int(lim.max_tool_calls_per_round)):
                return {"type": "noop", "reason": "too many tool calls in one round"}

            # Preserve the assistant tool_calls message as required by the API contract.
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})

            for tc in tool_calls:
                if not isinstance(tc, dict):
                    return {"type": "noop", "reason": "invalid tool_call shape"}
                tc_id = tc.get("id")
                fn = tc.get("function")
                if not isinstance(tc_id, str) or not tc_id.strip():
                    return {"type": "noop", "reason": "tool_call missing id"}
                if not isinstance(fn, dict):
                    return {"type": "noop", "reason": "tool_call missing function"}
                name = fn.get("name")
                args_raw = fn.get("arguments")
                if not isinstance(name, str) or not name.strip():
                    return {"type": "noop", "reason": "tool_call missing function.name"}
                if not isinstance(args_raw, str):
                    return {"type": "noop", "reason": "tool_call missing function.arguments"}

                try:
                    args = json.loads(args_raw)
                except Exception:  # noqa: BLE001
                    return {"type": "noop", "reason": "tool_call arguments json parse failed"}
                if not isinstance(args, dict):
                    return {"type": "noop", "reason": "tool_call arguments must be an object"}

                tool_out = await tool_handler(name, args)
                if not (isinstance(tool_out, dict) and isinstance(tool_out.get("ok"), bool)):
                    return {"type": "noop", "reason": "tool handler returned invalid envelope"}
                if tool_out.get("ok") is False:
                    return {"type": "noop", "reason": "tool execution failed"}

                content = json.dumps(tool_out, ensure_ascii=False)
                total_tool_output_chars += len(content)
                if total_tool_output_chars > max(0, int(lim.max_total_tool_output_chars)):
                    return {"type": "noop", "reason": "tool output budget exceeded"}

                messages.append({"role": "tool", "tool_call_id": tc_id, "content": content})

            continue

        content = msg.get("content")
        if isinstance(content, str):
            parsed = try_parse_json_loose(content)
            if isinstance(parsed, dict):
                return parsed
            return {"type": "noop", "reason": "llm returned non-json content"}

        return {"type": "noop", "reason": "llm returned empty content without tool calls"}

    return {"type": "noop", "reason": "tool rounds exceeded"}

