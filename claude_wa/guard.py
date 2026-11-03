"""In-process tool guard for guest profiles (the model cannot bypass it).

A guest session only registers read-only tools, but Read/Grep/Glob can still
reach any file the host user can read (tokens in .env, kubeconfig, keys) and
WebFetch can reach internal services. The PreToolUse hook asks `decide()`
before every tool call and denies anything outside the profile's limits.
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit

PATH_TOOLS = {"Read": ("file_path",), "Grep": ("path",), "Glob": ("path",),
              "NotebookRead": ("notebook_path",), "LS": ("path",)}
WEB_TOOLS = {"WebFetch": "url"}
# Tools a guest profile may never use even if they somehow get registered.
FORBIDDEN = {"Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit",
             "Agent", "Task", "KillShell", "Monitor", "ScheduleWakeup", "CronCreate",
             "Workflow", "EnterWorktree", "SendMessage", "RemoteTrigger"}


def _inside(path: str, roots: tuple[Path, ...]) -> bool:
    try:
        p = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return any(p == r or r in p.parents for r in roots)


def _private_host(host: str) -> bool:
    h = host.strip("[]").lower()
    if not h or h in ("localhost",) or h.endswith((".local", ".internal", ".lan", ".home.arpa")):
        return True
    try:
        addrs = {ipaddress.ip_address(h)}
    except ValueError:
        try:
            addrs = {ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(h, None)}
        except socket.gaierror:
            return True  # unresolvable: do not let the CLI try
    return any(a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
               or a.is_multicast or a.is_unspecified for a in addrs)


def decide(profile, tool_name: str, tool_input: dict) -> str | None:  # noqa: ANN001
    """Reason to deny, or None to allow."""
    if not getattr(profile, "guarded", False):
        return None
    inp = tool_input or {}
    if tool_name in FORBIDDEN or tool_name.startswith("mcp__") and not _mcp_allowed(profile, tool_name):
        return f"{tool_name} is not available on this assistant."
    if tool_name in PATH_TOOLS:
        roots = tuple(Path(r).expanduser().resolve() for r in (profile.read_roots or ()))
        raw = next((inp.get(k) for k in PATH_TOOLS[tool_name] if inp.get(k)), None)
        if raw is None:
            raw = profile.cwd or "."  # Grep/Glob default to cwd
        if not roots or not _inside(str(raw), roots):
            return "Only files in this assistant's own folders can be read."
    if tool_name in WEB_TOOLS and getattr(profile, "block_private_web", False):
        url = str(inp.get(WEB_TOOLS[tool_name]) or "")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or _private_host(parts.hostname or ""):
            return "Only public web addresses can be fetched."
    return None


def _mcp_allowed(profile, tool_name: str) -> bool:  # noqa: ANN001
    servers = set((profile.mcp_servers or {}).keys())
    if getattr(profile, "documents", False) or getattr(profile, "images", False):
        servers.add("docs")
    return any(tool_name.startswith(f"mcp__{s}__") for s in servers)
