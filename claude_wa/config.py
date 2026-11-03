"""Settings from environment. Secrets stay in .env / EnvironmentFile."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# Prefer Claude Code subscription/session auth over API billing keys.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(_ROOT / ".env")


def repo_path(value: str) -> Path:
    """Env-supplied file path: ~ expanded, relative paths from the repo root."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else _ROOT / p


# Deployment identity lives in env / local files, never in code.
USER_NAME = os.environ.get("BRIDGE_USER_NAME", "").strip() or "the user"
COMPANION_BOT = os.environ.get("BRIDGE_COMPANION_BOT", "").strip()  # e.g. "Telegram @my_other_bot"


def _load_system_prompt() -> str:
    """BRIDGE_SYSTEM_PROMPT > BRIDGE_SYSTEM_PROMPT_FILE > generic default."""
    inline = os.environ.get("BRIDGE_SYSTEM_PROMPT", "").strip()
    if inline:
        return inline
    path = os.environ.get("BRIDGE_SYSTEM_PROMPT_FILE", "").strip()
    if path:
        return repo_path(path).read_text(encoding="utf-8")  # missing file = loud failure
    return _DEFAULT_SYSTEM_PROMPT


def _phones(raw: str) -> frozenset[str]:
    out: set[str] = set()
    for part in raw.split(","):
        p = part.strip().replace(" ", "").replace("-", "").lstrip("+")
        if p:
            out.add(p)
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    allowed_users: frozenset[str]
    workdir: str = field(default_factory=lambda: str(Path.home()))
    model: str = "claude-fable-5-1"
    fallback_model: str = "claude-sonnet-4-6"
    effort: str = "medium"
    permission_mode: str = "default"
    turn_timeout: int = 0
    hard_timeout: int = 3600
    idle_timeout: int = 1200
    progress_after: int = 0  # 0 = no "still working" messages (typing indicator only)
    heartbeat_every: int = 240
    bridge_port: int = 3100
    bridge_host: str = "127.0.0.1"
    session_dir: Path = field(default_factory=lambda: _ROOT / "data" / "wa-session")
    sessions_file: Path = field(default_factory=lambda: _ROOT / "data" / "sessions.json")
    bridge_dir: Path = field(default_factory=lambda: _ROOT / "vendor" / "whatsapp-bridge")
    mode: str = "bot"  # bot | self-chat
    poll_interval: float = 0.8
    system_prompt: str = ""
    reply_prefix: str = ""  # optional prefix on every reply

    @property
    def bridge_base(self) -> str:
        return f"http://{self.bridge_host}:{self.bridge_port}"

    @staticmethod
    def from_env() -> "Settings":
        allowed = _phones(os.environ.get("WA_ALLOWED_USERS", ""))
        if not allowed:
            raise SystemExit("missing WA_ALLOWED_USERS (phone number(s) with country code, no +)")
        prompt = _load_system_prompt()
        return Settings(
            allowed_users=allowed,
            workdir=os.path.expanduser(os.environ.get("BRIDGE_CWD", str(Path.home()))),
            model=os.environ.get("BRIDGE_MODEL", "claude-fable-5-1"),
            fallback_model=os.environ.get("BRIDGE_FALLBACK_MODEL", "claude-sonnet-4-6"),
            effort=os.environ.get("BRIDGE_EFFORT", "medium"),
            permission_mode=os.environ.get("PERMISSION_MODE", "default"),
            turn_timeout=int(os.environ.get("TURN_TIMEOUT", "0")),
            hard_timeout=int(os.environ.get("BRIDGE_HARD_TIMEOUT", "3600")),
            idle_timeout=int(os.environ.get("BRIDGE_IDLE_TIMEOUT", "1200")),
            progress_after=int(os.environ.get("PROGRESS_AFTER", "0")),
            heartbeat_every=int(os.environ.get("HEARTBEAT_EVERY", "240")),
            bridge_port=int(os.environ.get("WA_BRIDGE_PORT", "3100")),
            bridge_host=os.environ.get("WA_BRIDGE_HOST", "127.0.0.1"),
            session_dir=repo_path(os.environ.get("WA_SESSION_DIR", "data/wa-session")),
            sessions_file=Path(
                os.environ.get("WA_SESSIONS_FILE", str(_ROOT / "data" / "sessions.json"))
            ),
            bridge_dir=Path(
                os.environ.get(
                    "WA_BRIDGE_DIR", str(_ROOT / "vendor" / "whatsapp-bridge")
                )
            ),
            mode=os.environ.get("WA_MODE", "bot").strip().lower(),
            poll_interval=float(os.environ.get("WA_POLL_INTERVAL", "0.8")),
            system_prompt=prompt,
            reply_prefix=os.environ.get("WA_REPLY_PREFIX", ""),
        )


# Generic default. A deployment's own persona (name, hosts, tools, house rules)
# goes in BRIDGE_SYSTEM_PROMPT_FILE, which is kept out of the repo.
_DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant running on headless Claude Code, reached over WhatsApp. "
    "You have full tool access in your working directory. "
    "WhatsApp: tight replies, skip preamble, do the work with tools. No markdown "
    "tables (WhatsApp can't render them); prefer short bullets.\n"
    "DELIVERY (critical): the chat receives your FINAL message of the turn; "
    "earlier text blocks are not shown (except the mid-task answer below). "
    "Deliverable text (drafts, documents, verdicts, lists) MUST be in that final "
    "message itself. Never reply 'see above' — there is no above.\n"
    "MID-TASK MESSAGES: the user can message you while you are working; the "
    "message appears mid-turn. Your NEXT text block is sent to them immediately — "
    "make it the direct answer to that message (status, yes/no, etc.), then carry "
    "on with the task. The final message still carries the deliverable.\n"
    "SAFETY: before any destructive or irreversible action, verify a recent "
    "backup or remote copy exists and state what you checked.\n"
    "SUBAGENTS run synchronously in your turn — wait for their results and "
    "deliver them in the same reply; never promise a later report.\n"
    "HEAVY JOBS (migrations, large copies, long builds): launch them as a "
    "detached background process writing to a log file, reply with what you "
    "started and where the log is; background task completions are delivered "
    "to the chat automatically.\n"
)

# Model names differ per engine: each engine has its own aliases, and /model
# only accepts names valid for the lane's current engine.
MODEL_ALIASES = {
    "claude": {
        "fable": "claude-fable-5-1",
        "opus": "claude-opus-5-5",
        "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-5-5",
    },
    "codex": {
        "sol": "gpt-6.1-sol",
        "sol6.1": "gpt-6.1-sol",
        "sol-6.1": "gpt-6.1-sol",
        "gpt-6.1-sol": "gpt-6.1-sol",
        "astra": "gpt-6-astra",
        "gpt-6-astra": "gpt-6-astra",
    },
}
CODEX_DEFAULT_MODEL = os.environ.get("BRIDGE_CODEX_MODEL", "gpt-6.1-sol")


def is_engine_model(engine: str, model: str) -> bool:
    """True when `model` is a model id the engine can run."""
    if not model:
        return False
    if engine == "codex":
        return model in MODEL_ALIASES["codex"].values()
    return model.startswith("claude-")


def resolve_model(engine: str, name: str) -> str | None:
    """Alias or full id -> model id for `engine`, or None when not valid there."""
    name = (name or "").strip().lower()
    model = MODEL_ALIASES.get(engine, {}).get(name, name)
    return model if is_engine_model(engine, model) else None
