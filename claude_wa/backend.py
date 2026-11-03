"""Claude Code backend — the only model executor this gateway shells to."""
from __future__ import annotations

import asyncio
from pathlib import Path
import json
import logging
import os
import shutil
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
)
from claude_wa.config import CODEX_DEFAULT_MODEL, Settings, is_engine_model, repo_path
from claude_wa.codex_engine import CodexClient
from claude_wa.guard import decide as guard_decide
from claude_wa.living_memory import living_persona, memory_env
from claude_wa.profiles import OWNER, Profile
from claude_wa.progress import describe_tool_use
from claude_wa.sessions import SessionStore

log = logging.getLogger("claude-wa.backend")

AUTO_TOOLS = ["Read", "Grep", "Glob"]


def load_mcp_servers() -> dict:
    path = os.environ.get("BRIDGE_MCP_SERVERS_FILE", "").strip()
    if not path:
        return {}
    data = json.loads(repo_path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("BRIDGE_MCP_SERVERS_FILE must hold a JSON object")
    return data


class ClaudeBackend:
    """One persistent ClaudeSDKClient per session key (chat or alert).

    Every client gets a *pump*: one task that reads the CLI's output stream for
    the client's whole life and hands each message to ``on_message``. Reading
    only inside a user turn (receive_response) desyncs the chat: Claude Code
    runs turns on its own when a background task finishes, and each unread
    ResultMessage makes the next user turn pop a stale reply ("lag-N").

    Opening and closing a key's client is serialized by a per-key lock, so two
    callers can never start two CLIs on one session. Stream callbacks only fire
    for the key's *current* client; a client that was dropped on purpose is
    silent (the caller of drop() owns the consequences).
    """

    def __init__(
        self,
        settings: Settings,
        store: SessionStore,
        *,
        on_tool: Callable[[int, str], Awaitable[None]] | None = None,
        on_message: Callable[[int, Any], Awaitable[None]] | None = None,
        on_pump_end: Callable[[int, BaseException | None], Awaitable[None]] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.on_tool = on_tool
        self.on_message = on_message
        self.on_pump_end = on_pump_end
        self.clients: dict[int, ClaudeSDKClient] = {}
        self.pumps: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.freshly_resumed: dict[int, bool] = {}
        self.effort_by_key: dict[int, str] = {}
        self.model_by_key: dict[int, str] = {}
        self._approver_factory = None  # set by Bridge for gated mode
        self.profiles: dict[int, Profile] = {}  # session key -> profile (set by Bridge)
        self.extra_mcp = None  # callable(session_key, profile) -> dict of extra MCP servers
        self.engine_by_key: dict = {}  # /engine override per chat (persisted in the store)
        self.codex_on_file = None  # callable(session_key) -> async fn(path) for generated images

    def set_approver_factory(self, factory) -> None:
        self._approver_factory = factory

    def effort_for(self, session_key: int) -> str:
        p = self.profiles.get(session_key, OWNER)
        return (self.effort_by_key.get(session_key) or self.store.get(f"effort:{session_key}")
                or p.effort or self.settings.effort)

    def _remember_model(self, session_key: int, model: str) -> None:
        """A /model choice is kept per engine and survives restarts."""
        self.model_by_key[session_key] = model
        self.store.set(f"model:{self.engine_for(session_key)}:{session_key}", model)

    def model_for(self, session_key: int) -> str:
        """The model the lane runs on ITS CURRENT ENGINE: a chosen or profile
        model only counts when that engine can run it."""
        p = self.profiles.get(session_key, OWNER)
        engine = self.engine_for(session_key)
        saved = self.store.get(f"model:{engine}:{session_key}")
        for m in (self.model_by_key.get(session_key), saved, p.model, self.settings.model):
            if m and is_engine_model(engine, m):
                return m
        return CODEX_DEFAULT_MODEL if engine == "codex" else self.settings.model

    def engine_for(self, session_key: int) -> str:
        p = self.profiles.get(session_key, OWNER)
        chosen = self.engine_by_key.get(session_key) or self.store.get(f"engine:{session_key}")
        # A stored choice only counts while the profile allows it (a switch
        # saved before the profile was locked down must not outlive it).
        if chosen and p.allows_engine(chosen):
            self.engine_by_key[session_key] = chosen
            return chosen
        return p.engine

    async def set_engine(self, session_key: int, engine: str) -> str:
        self.engine_by_key[session_key] = engine
        self.store.set(f"engine:{session_key}", engine)
        self.model_by_key.pop(session_key, None)  # model names differ per engine
        async with self._lock(session_key):
            await self._drop(session_key, forget_saved=False)
        return engine

    def _session_store_key(self, session_key: int) -> str:
        return f"codex:{session_key}" if self.engine_for(session_key) == "codex" else str(session_key)

    def _lock(self, session_key: int) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def client_for(self, session_key: int) -> ClaudeSDKClient:
        cl = self.clients.get(session_key)
        if cl is not None:
            return cl
        async with self._lock(session_key):
            return await self._open(session_key)

    async def _open(self, session_key: int) -> ClaudeSDKClient:
        cl = self.clients.get(session_key)
        if cl is not None:
            return cl
        profile = self.profiles.get(session_key, OWNER)
        effort = self.effort_for(session_key)
        model = self.model_for(session_key)
        if self.engine_for(session_key) == "codex":
            return await self._open_codex(session_key, profile, model, effort)
        opts: dict = dict(
            model=model,
            fallback_model=self.settings.fallback_model,
            effort=effort,
            system_prompt=self.settings.system_prompt,
            cwd=self.settings.workdir,
            include_partial_messages=False,
            # Default SDK limit is 1MB per JSON line; long tool results / resumed
            # transcripts blow past it and kill the turn with a silent-looking hang.
            max_buffer_size=int(
                os.environ.get("BRIDGE_MAX_BUFFER_SIZE", str(16 * 1024 * 1024))
            ),
            # Echo each prompt (with the uuid we sent) when the CLI consumes it:
            # the bridge's only reliable way to attribute a ResultMessage to the
            # message(s) it answers, incl. ones folded into a running turn.
            extra_args={"replay-user-messages": None},
        )
        # Extra MCP servers (e.g. a credential broker so the agent requests
        # actions, never secret values) come from a local JSON file:
        # {"name": {"command": "...", "args": [...], "env": {...}}}
        mcp = load_mcp_servers()
        if mcp:
            opts["mcp_servers"] = mcp
        opts.update(profile.session_options())
        menv = memory_env(profile)
        if menv and profile.system_prompt:
            # Living persona: what the agent saved in memory, read at session open.
            opts["system_prompt"] = await living_persona(menv, profile.system_prompt)
        if self.extra_mcp is not None:
            extra = self.extra_mcp(session_key, profile)
            if extra:
                opts["mcp_servers"] = {**opts.get("mcp_servers", {}), **extra}
        saved = self.store.get(session_key)
        if saved:
            opts["resume"] = saved
            self.freshly_resumed[session_key] = True
            log.info("resuming session %s for key %s", saved, session_key)

        perm = profile.permission_mode or self.settings.permission_mode
        if perm == "bypassPermissions":
            opts["permission_mode"] = "bypassPermissions"
        else:
            opts["permission_mode"] = perm
            allowed = list(profile.allowed_tools or AUTO_TOOLS)
            if (profile.documents or profile.images) and "mcp__docs" not in allowed:
                allowed.append("mcp__docs")
            opts["allowed_tools"] = allowed
            if self._approver_factory:
                opts["can_use_tool"] = self._approver_factory(session_key)

        async def _on_pre_tool(inp, tool_use_id, ctx):  # noqa: ANN001, ARG001
            why = guard_decide(profile, str(inp.get("tool_name", "")), inp.get("tool_input") or {})
            if why:
                log.info("key %s: guard denied %s: %s", session_key, inp.get("tool_name"), why)
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": why}}
            if self.on_tool is not None:
                await self.on_tool(
                    session_key,
                    describe_tool_use(
                        str(inp.get("tool_name", "?")), inp.get("tool_input") or {}
                    ),
                )
            # Subagents run synchronously: the reply to a message should carry
            # the subagent's result rather than a promise of a later report.
            if str(inp.get("tool_name")) in ("Agent", "Task"):
                ti = dict(inp.get("tool_input") or {})
                if ti.get("run_in_background") is not False:
                    ti["run_in_background"] = False
                    return {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "updatedInput": ti}}
            return {}

        async def _on_subagent_start(inp, tool_use_id, ctx):  # noqa: ANN001, ARG001
            if self.on_tool is not None:
                await self.on_tool(session_key, "Agent: delegated task started")
            return {}

        opts["hooks"] = {
            "PreToolUse": [HookMatcher(hooks=[_on_pre_tool])],
            "SubagentStart": [HookMatcher(hooks=[_on_subagent_start])],
        }
        cl = ClaudeSDKClient(options=ClaudeAgentOptions(**opts))
        await cl.connect()
        self.clients[session_key] = cl
        self.pumps[session_key] = asyncio.create_task(self._pump(session_key, cl))
        log.info(
            "opened Claude session key=%s profile=%s model=%s effort=%s",
            session_key,
            profile.name,
            model,
            effort,
        )
        return cl

    async def _open_codex(self, session_key: int, profile: Profile, model: str, effort: str):
        root = Path(profile.files_dir or profile.cwd or self.settings.workdir).expanduser()
        docs_env = None
        if profile.documents:
            docs_env = {"DOCS_ROOT": str(root), "DOCS_CHAT_ID": str(session_key),
                        "WA_BRIDGE_BASE": self.settings.bridge_base}
        saved = self.store.get(f"codex:{session_key}")
        home = Path(self.settings.sessions_file).parent / "codex" / "".join(c if c.isalnum() else "_" for c in str(session_key))
        mcp = dict(profile.mcp_servers or {})
        if not profile.isolated and "dmem" not in mcp and shutil.which("dmem"):
            # Owner lanes: on Claude the memory arrives through the host user's
            # own config; Codex sees only what we list, so add it explicitly
            # (default dmem config = the host user's tenant).
            mcp["dmem"] = {"command": shutil.which("dmem"), "args": ["mcp"]}
        cl = CodexClient(cwd=root, home=home, system_prompt=profile.system_prompt or self.settings.system_prompt,
                         model=model, effort=effort, mcp_servers=mcp, docs_env=docs_env,
                         thread_id=saved, on_file=self.codex_on_file(session_key) if self.codex_on_file else None,
                         failure_text=profile.failure_text,
                         prompt_provider=self._persona_provider(profile))
        await cl.connect()
        self.clients[session_key] = cl
        self.pumps[session_key] = asyncio.create_task(self._pump(session_key, cl))
        log.info("opened Codex session key=%s profile=%s model=%s effort=%s resume=%s",
                 session_key, profile.name, model, effort, bool(saved))
        return cl

    def _persona_provider(self, profile: Profile):
        """Codex rewrites its instructions every turn: rebuild them from memory each time."""
        menv = memory_env(profile)
        seed = profile.system_prompt or self.settings.system_prompt
        if not menv or not seed:
            return None
        return lambda: living_persona(menv, seed)

    async def set_effort(self, session_key: int, effort: str) -> str:
        """Persist effort and reopen client (CC has no headless /effort slash)."""
        self.effort_by_key[session_key] = effort
        self.store.set(f"effort:{session_key}", effort)  # survives restarts, like /engine
        async with self._lock(session_key):
            # Keep resume id so context continues; drop live client only.
            await self._drop(session_key, forget_saved=False)
            await self._open(session_key)
        return effort

    async def set_model_name(
        self, session_key: int, model: str, *, allow_reopen: bool = True
    ) -> str:
        cl = self.clients.get(session_key)
        if cl is None:
            # Nothing live to reopen: record it, and apply to a client that is
            # connecting right now (lock) instead of dropping it.
            self._remember_model(session_key, model)
            async with self._lock(session_key):
                cl = self.clients.get(session_key)
            if cl is not None:
                await cl.set_model(model)
            return model
        if cl is not None:
            try:
                await cl.set_model(model)
                self._remember_model(session_key, model)
                return model
            except Exception:  # noqa: BLE001
                if not allow_reopen:
                    raise
                log.warning("set_model failed; reopening client", exc_info=True)
        self._remember_model(session_key, model)
        async with self._lock(session_key):
            await self._drop(session_key, forget_saved=False)
            await self._open(session_key)
        return model

    async def _pump(self, session_key: int, cl: ClaudeSDKClient) -> None:
        err: BaseException | None = None
        try:
            async for msg in cl.receive_messages():
                if self.on_message is None or self.clients.get(session_key) is not cl:
                    continue
                try:
                    await self.on_message(session_key, msg)
                except Exception:  # noqa: BLE001
                    log.exception("on_message failed key=%s", session_key)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            err = e
            log.warning("stream for key=%s failed: %s", session_key, e)
        else:
            log.warning("stream for key=%s ended (CLI exited)", session_key)
        current = self.clients.get(session_key) is cl
        if current:
            # Forget it first so the next write reopens a fresh client.
            self.clients.pop(session_key, None)
            if self.pumps.get(session_key) is asyncio.current_task():
                self.pumps.pop(session_key, None)
            if self.on_pump_end is not None:
                try:
                    await self.on_pump_end(session_key, err)
                except Exception:  # noqa: BLE001
                    log.exception("on_pump_end failed key=%s", session_key)
        try:
            await cl.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def drop(self, session_key: int, *, forget_saved: bool = False) -> None:
        async with self._lock(session_key):
            await self._drop(session_key, forget_saved=forget_saved)

    async def _drop(self, session_key: int, *, forget_saved: bool = False) -> None:
        pump = self.pumps.pop(session_key, None)
        if pump is not None and pump is not asyncio.current_task() and not pump.done():
            pump.cancel()
        cl = self.clients.pop(session_key, None)
        self.freshly_resumed.pop(session_key, None)
        if forget_saved:
            self.store.set(session_key, None)
        if cl is not None:
            try:
                await cl.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def interrupt(self, session_key: int) -> bool:
        cl = self.clients.get(session_key)
        if cl is None:
            return False
        await cl.interrupt()
        return True
