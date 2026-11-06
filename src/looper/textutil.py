"""Small text helpers shared by assign (prompt adapt) and executor (templating)."""

from __future__ import annotations

import json
import re
from typing import Any

_VAR = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n...[{len(text) - limit} chars clipped]...\n" + text[-half:]


def render(template: str, ctx: dict[str, Any], limit: int = 20000) -> str:
    outputs: dict[str, str] = ctx.get("outputs", {})

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key.startswith("outputs."):
            val = outputs.get(key.split(".", 1)[1])
            return clip(val, limit) if val else "(none yet)"
        val = ctx.get(key)
        if val is None or val == "":
            return "(none yet)" if key in ("previous", "previous_artifact") else ""
        return clip(str(val), limit)

    return _VAR.sub(sub, template)


def parse_items(text: str, limit: int) -> list[str]:
    """Split an orchestrator/hypothesis output into subtasks: JSON array, else bullets/numbers."""
    text = text or ""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            items = [str(x if not isinstance(x, dict) else json.dumps(x)).strip() for x in data]
            items = [i for i in items if i]
            if items:
                return items[:limit]
        except json.JSONDecodeError:
            pass
    items = [mm.group(1).strip() for mm in re.finditer(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(.+)$", text, re.MULTILINE)]
    if items:
        return items[:limit]
    return [text.strip()] if text.strip() else []
