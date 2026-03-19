from __future__ import annotations

from collections.abc import Iterable


def clean_keywords(keywords: Iterable[object], *, max_items: int | None = None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    limit = None if max_items is None else max(0, int(max_items))

    for raw in keywords:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if limit is not None and len(cleaned) >= limit:
            break

    return cleaned

