from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .prompt_texts import PROMPT_TEXTS


_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_PROMPTS_PATH = Path(__file__).with_name("prompts.json")


@lru_cache
def _load_prompts() -> dict[str, Any]:
    data = json.loads(_PROMPTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("pc_config/prompts.json must be a JSON object")
    return data


def _render_text(template: str, vars: Mapping[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in vars:
            raise KeyError(f"missing prompt var: {key}")
        return str(vars[key])

    return _VAR_PATTERN.sub(repl, template)


def render_prompt_messages(template_id: str, vars: Mapping[str, str]) -> list[dict[str, Any]]:
    """
    Render a message list from pc_config/prompts.json.

    Template format:
      { "some.id": [ {"role": "...", "content": "...{{var}}..."}, ... ] }

    Also supports referencing python constants:
      { "some.id": [ {"role": "...", "content_var": "SOME_PROMPT_NAME"}, ... ] }
    """
    raw = _load_prompts().get(template_id)
    if not isinstance(raw, list):
        raise KeyError(f"unknown prompt template: {template_id}")

    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"prompt template item must be an object: {template_id}[{i}]")
        role = item.get("role")
        content = item.get("content")
        content_var = item.get("content_var")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"prompt message missing role: {template_id}[{i}]")

        if content is not None and content_var is not None:
            raise ValueError(f"prompt message has both content and content_var: {template_id}[{i}]")

        template: str | None = None
        if isinstance(content, str):
            template = content
        elif isinstance(content_var, str):
            if content_var not in PROMPT_TEXTS:
                raise KeyError(f"unknown content_var: {content_var} ({template_id}[{i}])")
            template = PROMPT_TEXTS[content_var]

        if not isinstance(template, str):
            raise ValueError(f"prompt message missing content: {template_id}[{i}]")

        msg = {k: v for k, v in item.items() if k != "content_var"}
        msg["content"] = _render_text(template, vars)
        out.append(msg)

    return out
