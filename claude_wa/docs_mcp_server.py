"""Stdio MCP server exposing the lane's document tools to a non-Claude engine
(Codex). Same behaviour as the in-process docs server for Claude lanes.

Env: DOCS_ROOT (lane folder), DOCS_CHAT_ID (WhatsApp chat JID),
WA_BRIDGE_BASE (node bridge, default http://127.0.0.1:3100).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
from mcp.server.fastmcp import FastMCP

from claude_wa.docs_tools import FORMATS, _inside, render, safe_name

ROOT = Path(os.environ.get("DOCS_ROOT", ".")).expanduser().resolve()
EXTRA = [Path(r).expanduser().resolve() for r in os.environ.get("DOCS_EXTRA_ROOTS", "").split(os.pathsep) if r]
CHAT = os.environ.get("DOCS_CHAT_ID", "")
BASE = os.environ.get("WA_BRIDGE_BASE", "http://127.0.0.1:3100")

mcp = FastMCP("docs")


async def send_media(path: str, caption: str | None) -> None:
    body = {"chatId": CHAT, "filePath": path, "fileName": Path(path).name}
    if caption:
        body["caption"] = caption
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.post(f"{BASE}/send-media", json=body) as r:
            if r.status >= 400:
                raise RuntimeError(f"send-media failed {r.status}: {await r.text()}")


@mcp.tool()
async def create_document(filename: str, content_markdown: str, format: str = "docx", caption: str = "") -> str:
    """Create a file from Markdown and send it to the user as a WhatsApp attachment.
    format: docx (default, Word), pdf, xlsx (each Markdown table becomes a sheet; a heading
    before it names the sheet; numbers stay numbers), pptx (each heading starts a slide, the
    lines under it are its bullets; a first lone heading is the title slide), md, txt.
    The file is kept in your folder; create it again with the same filename to edit it.
    Use real Markdown (headings, lists, tables) — it becomes proper formatting."""
    fmt = (format or "docx").lower().lstrip(".")
    if fmt not in FORMATS:
        return f"error: format must be one of {', '.join(FORMATS)}"
    name = safe_name(filename, fmt)
    out = ROOT / name
    try:
        await asyncio.to_thread(render, content_markdown or "", fmt, out)
    except Exception as e:  # noqa: BLE001
        return f"error: could not create the document: {e}"
    try:
        await send_media(str(out), caption.strip() or None)
    except Exception as e:  # noqa: BLE001
        return f"error: document saved as {name} but sending failed: {e}"
    return f"sent {name} ({out.stat().st_size} bytes) and kept a copy in your folder"


@mcp.tool()
async def send_file(filename: str, caption: str = "") -> str:
    """Send a file from your folder to the user as a WhatsApp attachment (a file they
    sent earlier, or one you created)."""
    p = Path(filename).expanduser()
    p = (p if p.is_absolute() else ROOT / p).resolve()
    if not p.is_file() or not any(_inside(p, r) for r in [ROOT, *EXTRA]):
        return "error: no such file in your folder"
    try:
        await send_media(str(p), caption.strip() or None)
    except Exception as e:  # noqa: BLE001
        return f"error: sending failed: {e}"
    return f"sent {p.name}"


@mcp.tool()
async def list_files() -> str:
    """List the files in your folder."""
    files = sorted(p.name for p in ROOT.iterdir() if p.is_file())
    return "\n".join(files) if files else "(empty)"


if __name__ == "__main__":
    mcp.run()
