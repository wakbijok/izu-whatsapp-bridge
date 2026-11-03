"""Lightweight tool-line buffer (no TG message edit on WA v1)."""
from __future__ import annotations


def describe_tool_use(name: str, tool_input: dict) -> str:
    tip = ""
    if isinstance(tool_input, dict):
        for k in ("file_path", "path", "pattern", "command", "query", "url"):
            v = tool_input.get(k)
            if v:
                tip = str(v)
                break
    tip = (tip[:80] + "…") if len(tip) > 80 else tip
    return f"{name}: {tip}" if tip else name
