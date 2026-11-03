"""Codex engine: the OpenAI Codex CLI (ChatGPT subscription) as a lane backend.

A CodexClient quacks like ClaudeSDKClient for the parts the bridge uses:
connect / query / receive_messages / interrupt / disconnect / set_model, and
emits the same message objects (SystemMessage init, UserMessage echo with our
uuid, AssistantMessage, ResultMessage), so lanes, routing, watchdog, guard
and delivery are unchanged. Each turn is one `codex exec` (or `codex exec
resume <thread>`) process; prompts sent while a turn runs are queued and run
as the next turn (Codex cannot fold a message into a running turn).

Per lane, CODEX_HOME lives OUTSIDE the writable lane folder (the bridge's
data dir), and config.toml plus AGENTS.md are rewritten before every turn, so
nothing the model writes with its file tool can change its own limits or
persona for the next turn. The config is a generated config.toml (model,
effort, approval never, workspace-write sandbox, no shell tool, cached web
search, trusted project = the lane folder, MCP servers: the profile's memory
server plus the docs stdio server) and a symlink to the host user's auth.json.
The persona is written to AGENTS.md in the lane folder.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, UserMessage

log = logging.getLogger("claude-wa.codex")

CODEX = shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
AUTH_SRC = Path(os.environ.get("CODEX_AUTH_FILE", "~/.codex/auth.json")).expanduser()
TURN_TIMEOUT = int(os.environ.get("CODEX_TURN_TIMEOUT", "900"))
EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
LOCAL_IMG = re.compile(r"!\[[^\]]*\]\((/[^)\s]+\.(?:png|jpe?g|webp))\)|(?<![\w/])(/[^\s)]+/generated_images/[^\s)]+\.png)")


def _toml_str(s: str) -> str:
    return json.dumps(s)  # JSON string escaping is valid TOML basic-string escaping


def write_config(home: Path, cwd: Path, *, model: str | None, effort: str, mcp_servers: dict,
                 docs_env: dict | None, web_search: str = "cached", sandbox: str = "workspace-write",
                 disable: tuple[str, ...] = ()) -> Path:
    """`disable`: extra Codex features to turn off on top of the shell tool."""
    home.mkdir(parents=True, exist_ok=True)
    link = home / "auth.json"
    if not link.exists() and AUTH_SRC.exists():
        link.symlink_to(AUTH_SRC)
    lines = [f"model = {_toml_str(model)}"] if model else []
    lines += [
        f"model_reasoning_effort = {_toml_str(EFFORTS.get(effort, 'medium'))}",
        'approval_policy = "never"',
        f"sandbox_mode = {_toml_str(sandbox)}",
        f"web_search = {_toml_str(web_search)}",
        "include_apps_instructions = false",
        "",
        "[features]",
        "shell_tool = false",
        *(f"{f} = false" for f in disable if f != "shell_tool"),
        "",
        f"[projects.{_toml_str(str(cwd))}]",
        'trust_level = "trusted"',
    ]
    servers = dict(mcp_servers or {})
    if docs_env is not None:
        servers["docs"] = {
            "command": str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"),
            "args": ["-m", "claude_wa.docs_mcp_server"],
            "env": {**docs_env, "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
                    "DOCS_EXTRA_ROOTS": str(home / "generated_images")},
        }
    for name, spec in servers.items():
        lines += ["", f"[mcp_servers.{name}]", f"command = {_toml_str(str(spec['command']))}",
                  "args = [" + ", ".join(_toml_str(str(a)) for a in spec.get("args", [])) + "]"]
        if spec.get("env"):
            lines.append("env = { " + ", ".join(f"{k} = {_toml_str(str(v))}" for k, v in spec["env"].items()) + " }")
        lines.append('default_tools_approval_mode = "approve"')
    cfg = home / "config.toml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg


class CodexClient:
    def __init__(self, *, cwd: Path, home: Path, system_prompt: str, model: str, effort: str,
                 mcp_servers: dict, docs_env: dict | None, thread_id: str | None,
                 on_file=None, runner=None, failure_text: str | None = None,
                 prompt_provider=None):  # noqa: ANN001
        self.cwd = cwd
        self.home = home
        self.thread_id = thread_id
        self.on_file = on_file
        self.options = SimpleNamespace(model=model, effort=effort, system_prompt=system_prompt,
                                       cwd=str(cwd), permission_mode="codex", setting_sources=[],
                                       strict_mcp_config=True, mcp_servers=dict(mcp_servers or {}),
                                       resume=thread_id, extra_args={}, tools=[], hooks={})
        self._mcp_servers = mcp_servers
        self._docs_env = docs_env
        self._queue: asyncio.Queue = asyncio.Queue()
        self._turn_lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._closed = False
        self._runner = runner  # test hook: async (args, cwd, env, prompt) -> list[dict] events
        self.pending_attachments: list[str] = []
        self._sent_files: set[str] = set()
        # What the user sees when a turn fails (details go to the log, not the chat).
        # async () -> str | None: the current system prompt, rebuilt before each turn.
        self.prompt_provider = prompt_provider
        self.failure_text = failure_text or "⚠️ Something went wrong on my side — please send that again."

    # ---- lifecycle ----
    def _refresh(self) -> None:
        """Re-assert config and persona (the lane folder is model-writable)."""
        self.cwd.mkdir(parents=True, exist_ok=True)
        (self.cwd / "AGENTS.md").write_text(self.options.system_prompt or "", encoding="utf-8")
        write_config(self.home, self.cwd, model=self.options.model, effort=self.options.effort,
                     mcp_servers=self._mcp_servers, docs_env=self._docs_env)

    async def connect(self) -> None:
        if self.home.resolve() == self.cwd.resolve() or self.cwd.resolve() in self.home.resolve().parents:
            raise ValueError("CODEX_HOME must not be inside the lane's writable folder")
        self._refresh()

    async def disconnect(self) -> None:
        self._closed = True
        await self.interrupt()
        self._queue.put_nowait(None)

    async def receive_messages(self):
        while True:
            m = await self._queue.get()
            if m is None:
                return
            if isinstance(m, BaseException):
                raise m
            yield m

    async def set_model(self, model: str) -> None:
        self.options.model = model
        self._refresh()

    async def interrupt(self) -> None:
        p = self._proc
        if p is not None and p.returncode is None:
            try:
                p.terminate()
            except ProcessLookupError:
                pass

    # ---- turns ----
    async def query(self, prompt) -> None:  # noqa: ANN001
        if isinstance(prompt, str):
            msgs = [{"message": {"content": prompt}, "uuid": ""}]
        else:
            msgs = [m async for m in prompt]
        for m in msgs:
            text = m.get("message", {}).get("content", "")
            uid = m.get("uuid", "") or ""
            attachments = list(m.get("_attachments") or [])
            asyncio.get_running_loop().create_task(self._turn(text, uid, attachments))

    def _emit(self, msg) -> None:  # noqa: ANN001
        if not self._closed:
            self._queue.put_nowait(msg)

    async def _turn(self, text: str, uid: str, attachments: list[str]) -> None:
        async with self._turn_lock:
            if self._closed:
                return
            self._emit(SystemMessage(subtype="init", data={"engine": "codex"}))
            self._emit(UserMessage(content=text, uuid=uid or None))
            started = time.monotonic()
            if self.prompt_provider is not None:
                try:
                    sp = await self.prompt_provider()
                    if sp:
                        self.options.system_prompt = sp
                except Exception:  # noqa: BLE001
                    log.warning("prompt provider failed; keeping the last persona", exc_info=True)
            self._refresh()
            attachments = [a for a in attachments if os.path.isabs(a) and os.path.isfile(a)]
            try:
                events = await self._run(text, attachments)
            except Exception as e:  # noqa: BLE001
                log.warning("codex turn failed: %s", e)
                self._emit(ResultMessage(subtype="error_during_execution", duration_ms=0, duration_api_ms=0,
                                         is_error=True, num_turns=1, session_id=self.thread_id or "",
                                         result=self.failure_text))
                return
            final = ""
            for ev in events:
                t = ev.get("type")
                it = ev.get("item") or {}
                if t == "thread.started" and ev.get("thread_id"):
                    self.thread_id = ev["thread_id"]
                elif t == "item.completed" and it.get("type") == "agent_message":
                    final = it.get("text") or ""
                elif t == "item.completed" and (
                    it.get("type") == "image_generation" or str(it.get("kind", "")).startswith("image_gen")
                ):
                    await self._send_file(it.get("saved_path"))
                elif t == "error":
                    final = final or f"⚠️ {ev.get('message') or ev}"
            # Codex writes generated pictures under CODEX_HOME/generated_images and
            # the --json stream does not always name them: send whatever is new.
            for p in self._new_images(started):
                await self._send_file(p)
            # Local image links in the text are useless on a phone: send the file
            # (if not already sent) and drop the link.
            def _link(m):
                path = m.group(1) or m.group(2)
                asyncio.get_running_loop().create_task(self._send_file(path))
                return ""
            final = LOCAL_IMG.sub(_link, final).strip()
            if final:
                self._emit(AssistantMessage(content=[TextBlock(text=final)], model=self.options.model))
            ms = int((time.monotonic() - started) * 1000)
            self._emit(ResultMessage(subtype="success", duration_ms=ms, duration_api_ms=ms, is_error=False,
                                     num_turns=1, session_id=self.thread_id or "", result=final))

    def _new_images(self, since: float) -> list[str]:
        root = self.home / "generated_images"
        if not root.is_dir():
            return []
        out = []
        for p in root.rglob("*.png"):
            try:
                if p.stat().st_mtime >= since - 1 and str(p) not in self._sent_files:
                    out.append(str(p))
            except OSError:
                pass
        return sorted(out)

    async def _send_file(self, path) -> None:  # noqa: ANN001
        if not path or self.on_file is None:
            return
        path = str(path).replace("file://", "")
        if path in self._sent_files or not os.path.isfile(path):
            return
        # Only the lane's own artefacts: a link in the reply must not be able
        # to exfiltrate arbitrary host files.
        p = Path(path).resolve()
        roots = (self.home / "generated_images", self.cwd)
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp") or not any(
            p == r.resolve() or r.resolve() in p.parents for r in roots
        ):
            log.warning("refusing to send %s: outside the lane's folders", path)
            return
        self._sent_files.add(path)
        try:
            await self.on_file(path)
        except Exception:  # noqa: BLE001
            log.exception("sending generated image failed")

    async def _run(self, text: str, attachments: list[str]) -> list[dict]:
        # Global options go before the `resume` subcommand.
        args = ["exec", "--skip-git-repo-check", "--json", "-C", str(self.cwd)]
        if self.thread_id:
            args += ["resume", self.thread_id]
        # Images go AFTER `resume <id>`: -i takes several values, so placed
        # before the subcommand it swallowed "resume" and the thread id as
        # image paths, and every photo silently started a new conversation.
        # `--` ends the image list (and makes a '-' message a prompt).
        for a in attachments:
            args += ["-i", a]
        args += ["--", text]
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE_", "ANTHROPIC_", "DM_", "BRIDGE_", "TELEGRAM_", "WA_", "ALERT_"))}
        env["CODEX_HOME"] = str(self.home)
        if self._runner is not None:
            return await self._runner(args, self.cwd, env, text)
        self._proc = await asyncio.create_subprocess_exec(
            CODEX, *args, cwd=str(self.cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(self._proc.communicate(), TURN_TIMEOUT)
        except asyncio.TimeoutError:
            self._proc.kill()
            raise RuntimeError("Codex did not finish in time") from None
        finally:
            rc = self._proc.returncode
            self._proc = None
        events = []
        for line in out.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        if rc not in (0, None) and not any(e.get("type") == "turn.completed" for e in events):
            tail = err.decode(errors="replace")[-400:]
            raise RuntimeError(f"codex exited {rc}: {tail or 'no output'}")
        return events
