"""Image generation for a guest lane by delegating to the Codex CLI, whose
built-in `$imagegen` runs on the ChatGPT subscription. The lane's Claude never
executes anything: the bridge runs `codex exec` in the lane's images folder
and sends the resulting PNG on WhatsApp.

The run never sees the host user's Codex setup: it gets its own CODEX_HOME
(outside the lane folder, config rewritten before every run) with only a
symlink to auth.json, no MCP servers, approvals never, a read-only sandbox,
the shell / exec / view_image / apps / plugins / browser tools off, and (with
bubblewrap installed) every home directory hidden behind an empty tmpfs. The
built-in image tool saves under CODEX_HOME/generated_images; the bridge moves
the new picture into the images folder itself, so the model writes nothing."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from claude_wa import codex_engine
from claude_wa.codex_engine import write_config
from claude_wa.config import CODEX_DEFAULT_MODEL

log = logging.getLogger("claude-wa.images")

CODEX = shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
TIMEOUT = 420  # s; a generation takes ~1-3 min
DAILY_CAP = int(os.environ.get("BRIDGE_IMAGES_PER_DAY", "12"))
SANDBOX = "read-only"  # the strictest `codex exec --sandbox` value
# Codex features the image run never needs (names from `codex features list`).
# codex-cli 0.166 still reports unified_exec on after this, and a read-only
# sandbox still reads the whole host, so run_codex also jails the process
# with bubblewrap (jail_prefix) to hide every home directory.
# code_mode_host must stay on: the built-in image tool runs through it (verified
# 0.166), which is also why a guest prompt can still reach a code tool here.
DISABLED = ("shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot",
            "multi_agent", "goals", "hooks", "skill_search", "tool_suggest", "view_image", "apps",
            "plugins", "browser_use", "browser_use_external", "in_app_browser", "computer_use", "memories")
DEFAULT_HOME = Path(os.environ.get("BRIDGE_IMAGES_CODEX_HOME", "~/.cache/claude-wa/codex-images")).expanduser()


def _quota_path(d: Path) -> Path:
    return d / ".quota.json"


def quota_left(d: Path) -> int:
    today = time.strftime("%Y-%m-%d")
    try:
        q = json.loads(_quota_path(d).read_text())
    except (OSError, ValueError):
        q = {}
    return DAILY_CAP - (q.get("count", 0) if q.get("day") == today else 0)


def _count(d: Path) -> None:
    today = time.strftime("%Y-%m-%d")
    try:
        q = json.loads(_quota_path(d).read_text())
    except (OSError, ValueError):
        q = {}
    n = q.get("count", 0) + 1 if q.get("day") == today else 1
    _quota_path(d).write_text(json.dumps({"day": today, "count": n}))


def codex_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_", "ANTHROPIC_", "DM_", "BRIDGE_", "TELEGRAM_", "WA_", "ALERT_", "CODEX_"))}
    env["CODEX_HOME"] = str(home)
    return env


JAIL = os.environ.get("BRIDGE_IMAGES_JAIL", "require")  # require | auto | off
# A dedicated ChatGPT login for image runs, so a guest-influenced run never
# holds the owner's credential. Unset = the engine's auth (CODEX_AUTH_FILE).
IMAGES_AUTH = os.environ.get("BRIDGE_IMAGES_AUTH_FILE", "")
BWRAP = shutil.which("bwrap")
# Host trees hidden from the image run (a read-only sandbox still READS everything).
HIDDEN = ("/home", "/root", "/mnt", "/media", "/srv")


def jail_prefix(cwd: Path, env: dict, args: list[str]) -> list[str]:
    """bubblewrap argv that hides HIDDEN behind empty tmpfs and binds back only
    the codex install, the run's CODEX_HOME, auth.json, the images folder and
    a reference image. Empty when jailing is off or bwrap is missing (auto)."""
    if JAIL == "off" or (not BWRAP and JAIL == "auto"):
        if JAIL == "auto":
            log.warning("bwrap not found: image run is not filesystem-jailed")
        return []
    if not BWRAP:
        raise RuntimeError("BRIDGE_IMAGES_JAIL=require but bwrap is not installed")
    home = Path(env["CODEX_HOME"])
    auth = home / "auth.json"
    p = [BWRAP, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
         "--die-with-parent", "--unshare-pid"]
    for d in HIDDEN:
        if os.path.isdir(d):
            p += ["--tmpfs", d]
    rel = Path(CODEX).resolve().parent.parent  # the release dir: binary + its helpers
    p += ["--ro-bind", str(rel), str(rel)]
    p += ["--bind", str(home), str(home), "--bind", str(cwd), str(cwd)]
    if auth.is_symlink() and auth.resolve().exists():
        # rw: codex refreshes the token in place.
        p += ["--bind", str(auth.resolve()), str(auth.resolve())]
    if "-i" in args:
        ref = args[args.index("-i") + 1]
        p += ["--ro-bind", ref, ref]
    return p + ["--chdir", str(cwd), "--"]


async def run_codex(args: list[str], cwd: Path, env: dict, timeout: int = TIMEOUT) -> tuple[int, str]:
    """Run the Codex CLI non-interactively; returns (rc, tail of output)."""
    jail = jail_prefix(cwd, env, args)
    # Jailed, the resolved binary runs so its helpers resolve beside it.
    exe = str(Path(CODEX).resolve()) if jail else CODEX
    proc = await asyncio.create_subprocess_exec(
        *jail, exe, *args, cwd=str(cwd), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode(errors="replace")[-600:]


def _new_png(home: Path, since: float) -> Path | None:
    root = home / "generated_images"
    if not root.is_dir():
        return None
    found = []
    for p in root.rglob("*.png"):
        try:
            if p.stat().st_mtime >= since - 1:
                found.append((p.stat().st_mtime, p))
        except OSError:
            pass
    return max(found)[1] if found else None


def _link_dedicated_auth(home: Path) -> None:
    """Point home/auth.json at IMAGES_AUTH, whatever is there now (regular file,
    dangling or foreign link). Refuses a missing file or the engine's own auth."""
    want = Path(IMAGES_AUTH).expanduser().resolve()
    if not want.is_file():
        raise RuntimeError("BRIDGE_IMAGES_AUTH_FILE does not exist")
    if want == codex_engine.AUTH_SRC.resolve():
        raise RuntimeError("BRIDGE_IMAGES_AUTH_FILE must be a separate login, not the engine's auth")
    home.mkdir(parents=True, exist_ok=True)
    link = home / "auth.json"
    if link.is_symlink() or link.exists():
        if not (link.is_symlink() and link.resolve() == want):
            link.unlink()
    if not link.is_symlink():
        link.symlink_to(want)
    if link.resolve() != want:
        raise RuntimeError("image auth link does not resolve to BRIDGE_IMAGES_AUTH_FILE")


async def generate(images_dir: Path, name: str, prompt: str, reference: Path | None = None,
                   runner=run_codex, home: Path | None = None) -> Path:  # noqa: ANN001
    images_dir.mkdir(parents=True, exist_ok=True)
    home = (home or DEFAULT_HOME / "".join(c if c.isalnum() else "_" for c in str(images_dir.resolve()))).expanduser()
    lane = images_dir.resolve().parent
    if home.resolve() == lane or lane in home.resolve().parents:
        raise ValueError("the image CODEX_HOME must not be inside the lane's folder")
    if quota_left(images_dir) <= 0:
        raise RuntimeError(f"daily image limit ({DAILY_CAP}) reached; try again tomorrow")
    out = images_dir / name
    out.unlink(missing_ok=True)
    if IMAGES_AUTH:
        _link_dedicated_auth(home)
    # Rewritten every run: nothing a previous run left there carries over.
    write_config(home, images_dir, model=CODEX_DEFAULT_MODEL, effort="medium", mcp_servers={},
                 docs_env=None, web_search="disabled", sandbox=SANDBOX, disable=DISABLED)
    rule = ("Use the built-in image generation tool once. Do not run commands, read or write files, "
            "or use any other tool; the image is collected automatically. Reply with only 'done'.")
    task = f"$imagegen {prompt.strip()}\n\n{rule}"
    if reference is not None:
        task = f"$imagegen Edit the attached image: {prompt.strip()}\n\n{rule}"
    args = ["exec", "--skip-git-repo-check", "--sandbox", SANDBOX]
    for f in DISABLED:
        args += ["--disable", f]
    args += ["-C", str(images_dir)]
    if reference is not None:
        args += ["-i", str(reference)]
    args += ["--", task]
    started = time.time()
    rc, tail = await runner(args, images_dir, codex_env(home))
    made = _new_png(home, started)
    if made is not None:
        shutil.move(str(made), str(out))
    if not out.exists():
        log.warning("codex image failed rc=%s: %s", rc, tail)
        raise RuntimeError("the image could not be generated this time")
    _count(images_dir)
    return out
