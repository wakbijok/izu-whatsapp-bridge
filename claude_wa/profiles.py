"""Per-sender profiles: which Claude session a WhatsApp number gets.

BRIDGE_PROFILES_FILE (JSON, kept out of the repo) maps a phone number (digits,
country code, no +) to a profile. Numbers without one use the owner defaults
from .env. A guest profile makes an isolated session: its own persona, cwd,
permission mode, MCP servers (e.g. its own memory tenant) and no host-user
settings, so nothing of the owner's context or memory reaches it.

  {
    "15551234567": {
      "user_name": "Guest",
      "persona_file": "config/persona-guest.local.md",
      "cwd": "~/guests/guest",
      "permission_mode": "default",
      "allowed_tools": ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "mcp__dmem"],
      "tools": ["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
      "mcp_servers": {"dmem": {"command": "dmem", "args": ["mcp"],
                               "env": {"DM_CONFIG": "config/dmem-guest.local.toml"}}},
      "isolated": true,
      "read_roots": ["~/guests/guest"],
      "block_private_web": true,
      "documents": true
    }
  }

`isolated` also turns on `guarded` (in-process PreToolUse guard: forbidden
tools, reads outside `read_roots`, and WebFetch to private hosts are denied)
and `block_private_web`; set either to false explicitly to opt out. The guard
is a Claude Code hook: on the Codex engine a lane relies on Codex's own
sandbox (workspace-write, no shell tool) instead.

A guarded lane also gets, unless its profile says otherwise:
- `engines`: only its own `engine` (no /engine switch); list both
  (["claude", "codex"]) to allow switching.
- `cc_slashes`: only the harmless Claude Code slashes in
  commands.GUEST_CC_SLASHES (/clear /compact /context /recap).
- received attachments are copied into `<files_dir or cwd>/received/`, so
  `read_roots` never needs the bridge's shared media cache.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from claude_wa.commands import GUEST_CC_SLASHES
from claude_wa.config import USER_NAME, repo_path

# Env the CLI reads directly (not from settings): without these an isolated
# session would still load the host user's auto-memory and CLAUDE.md files.
ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
}


@dataclass(frozen=True)
class Profile:
    name: str = "owner"
    user_name: str = USER_NAME
    system_prompt: str | None = None  # None = settings.system_prompt
    cwd: str | None = None  # None = settings.workdir
    permission_mode: str | None = None  # None = settings.permission_mode
    allowed_tools: tuple[str, ...] | None = None
    tools: tuple[str, ...] | None = None
    mcp_servers: dict | None = None  # None = BRIDGE_MCP_SERVERS_FILE
    env: dict = field(default_factory=dict)
    isolated: bool = False
    guarded: bool = False  # in-process tool guard (claude_wa.guard)
    read_roots: tuple[str, ...] | None = None  # dirs Read/Grep/Glob may touch
    block_private_web: bool = False  # WebFetch: public hosts only
    documents: bool = False  # in-process docs tools (create_document / send_file)
    images: bool = False  # create_image via the Codex CLI ($imagegen)
    files_dir: str | None = None  # where docs/images live (default: cwd)
    engine: str = "claude"  # claude | codex (default engine; /engine overrides per chat)
    model: str | None = None  # default model for this profile (None = settings.model)
    effort: str | None = None  # default effort for this profile (None = settings.effort)
    failure_text: str | None = None  # user-facing text when a turn fails (engine details stay in the log)
    memory_persona: bool = False  # persona from the tenant's memory each turn + per-message recall (living_memory)
    engines: tuple[str, ...] | None = None  # engines /engine may pick (None = guarded: own engine only; else any)
    cc_slashes: tuple[str, ...] | None = None  # CC slashes passed through (None = guarded: GUEST_CC_SLASHES; else all)

    def allowed_engines(self) -> tuple[str, ...] | None:
        """Engines this lane may run on; None = no limit (owner / unguarded)."""
        if self.engines is not None:
            return self.engines
        return (self.engine,) if self.guarded else None

    def allows_engine(self, engine: str) -> bool:
        allowed = self.allowed_engines()
        return allowed is None or engine in allowed

    def allows_cc_slash(self, name: str) -> bool:
        allowed = self.cc_slashes if self.cc_slashes is not None else (GUEST_CC_SLASHES if self.guarded else None)
        return allowed is None or name in allowed

    def session_options(self) -> dict:
        """ClaudeAgentOptions fields this profile pins (merged over the defaults)."""
        o: dict = {}
        if self.system_prompt is not None:
            o["system_prompt"] = self.system_prompt
        if self.cwd is not None:
            o["cwd"] = self.cwd
        if self.tools is not None:
            o["tools"] = list(self.tools)
        if self.mcp_servers is not None:
            o["mcp_servers"] = self.mcp_servers
        env = dict(self.env)
        if self.isolated:
            o["setting_sources"] = []
            o["strict_mcp_config"] = True
            env = {**ISOLATION_ENV, **env}
        if env:
            o["env"] = env
        return o


OWNER = Profile()


def _resolve_env(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        v = str(v)
        if k.endswith(("_FILE", "_CONFIG", "_DIR")) and v and not v.startswith("$"):
            v = str(repo_path(v))
        out[k] = v
    return out


def _mcp(servers: dict) -> dict:
    out = {}
    for name, spec in (servers or {}).items():
        spec = dict(spec)
        if "env" in spec:
            spec["env"] = _resolve_env(spec["env"])
        out[name] = spec
    return out


# What a user (or a profile's "engines" list) may call an engine.
ENGINE_ALIASES = {"claude": "claude", "gpt": "codex", "chatgpt": "codex", "codex": "codex", "openai": "codex"}


def _engines(raw) -> tuple[str, ...] | None:  # noqa: ANN001
    if raw is None:
        return None
    return tuple(dict.fromkeys(ENGINE_ALIASES[str(e).lower()] for e in raw if str(e).lower() in ENGINE_ALIASES))


def load_profiles(path: str | None = None) -> dict[str, Profile]:
    path = (path if path is not None else os.environ.get("BRIDGE_PROFILES_FILE", "")).strip()
    if not path:
        return {}
    data = json.loads(repo_path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("BRIDGE_PROFILES_FILE must hold a JSON object keyed by phone number")
    out: dict[str, Profile] = {}
    for number, p in data.items():
        key = "".join(ch for ch in str(number) if ch.isdigit())
        persona = p.get("persona_file")
        cwd = p.get("cwd")
        if cwd:
            cwd = str(Path(cwd).expanduser())
            Path(cwd).mkdir(parents=True, exist_ok=True)
        out[key] = Profile(
            name=p.get("name") or (p.get("user_name") or key).lower(),
            user_name=p.get("user_name") or USER_NAME,
            system_prompt=repo_path(persona).read_text(encoding="utf-8") if persona else p.get("system_prompt"),
            cwd=cwd,
            permission_mode=p.get("permission_mode"),
            allowed_tools=tuple(p["allowed_tools"]) if p.get("allowed_tools") else None,
            tools=tuple(p["tools"]) if p.get("tools") else None,
            mcp_servers=_mcp(p["mcp_servers"]) if "mcp_servers" in p else None,
            env=_resolve_env(p.get("env") or {}),
            isolated=bool(p.get("isolated", False)),
            guarded=bool(p.get("guarded", p.get("isolated", False))),
            read_roots=tuple(str(Path(r).expanduser()) for r in p["read_roots"]) if p.get("read_roots") else ((cwd,) if cwd else None),
            block_private_web=bool(p.get("block_private_web", p.get("isolated", False))),
            documents=bool(p.get("documents", False)),
            images=bool(p.get("images", False)),
            files_dir=str(Path(p["files_dir"]).expanduser()) if p.get("files_dir") else None,
            memory_persona=bool(p.get("memory_persona", False)),
            engine=str(p.get("engine") or "claude").lower(),
            model=p.get("model"),
            effort=p.get("effort"),
            failure_text=p.get("failure_text"),
            engines=_engines(p.get("engines")),
            cc_slashes=tuple(str(c).lstrip("/") for c in p["cc_slashes"]) if p.get("cc_slashes") is not None else None,
        )
    return out


def profile_for(aliases: set[str], profiles: dict[str, Profile]) -> Profile:
    for a in aliases:
        if a in profiles:
            return profiles[a]
    return OWNER
