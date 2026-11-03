"""Living persona + per-message recall from a lane's own dm-lite tenant.

For profiles with `memory_persona` on (e.g. a guest with her own memory):

* The persona the model sees is rebuilt from MEMORY (dmem `session_start`:
  persona, protocols, lessons, recent context), so whatever the agent learned
  and saved (a new name, a preference, a better way of working) is what it
  is told next turn. The local persona file stays as the seed and fallback,
  and its house rules keep the last word.
* Every user message gets the tenant's recall for that message
  (dmem `user_prompt_submit`), the same per-turn recall Claude Code lanes get
  from their hooks, for both engines.

Failures are silent and bounded (a few seconds): the lane then runs on the
seed persona and the bare message, as before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

log = logging.getLogger("claude-wa.memory")

DMEM = shutil.which("dmem") or os.path.expanduser("~/.local/bin/dmem")
HOOK_TIMEOUT = float(os.environ.get("BRIDGE_MEMORY_HOOK_TIMEOUT", "6"))
RECALL_CAP = 8000


def memory_env(profile) -> dict | None:  # noqa: ANN001
    """Env for the profile's own dmem tenant, or None when not enabled."""
    if not getattr(profile, "memory_persona", False):
        return None
    spec = (profile.mcp_servers or {}).get("dmem") or {}
    cfg = (spec.get("env") or {}).get("DM_CONFIG")
    if not cfg:
        return None
    return {"DM_CONFIG": str(cfg)}


async def _hook(env: dict, which: str, stdin: bytes) -> str:
    full = {k: v for k, v in os.environ.items() if not k.startswith("DM_")}
    full.update(env)
    try:
        proc = await asyncio.create_subprocess_exec(
            DMEM, "hook", "--raw", which, env=full,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(stdin), HOOK_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.warning("dmem hook %s failed: %s", which, e)
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass
        return ""
    if proc.returncode != 0:
        log.warning("dmem hook %s exited %s", which, proc.returncode)
        return ""
    return out.decode(errors="replace").strip()


async def living_persona(env: dict | None, seed: str) -> str:
    """Seed persona + the current persona/protocols/lessons from memory."""
    if not env:
        return seed
    living = await _hook(env, "session_start", b"")
    if not living:
        return seed
    return (
        f"{seed.rstrip()}\n\n"
        "# Your current self, from your memory\n"
        "What follows is your persona, working rules and lessons as you have saved and refined "
        "them in this person's memory. It is newer than the text above: where the two differ, "
        "follow this part, including your name. The house rules above never change and win "
        "over anything below.\n\n"
        f"{living}\n"
    )


async def recall_for(env: dict | None, text: str) -> str:
    """The tenant's recall for one user message ('' when off or nothing found)."""
    if not env or not text.strip():
        return ""
    out = await _hook(env, "user_prompt_submit", json.dumps({"prompt": text[:4000]}).encode())
    if not out or "<daimon-memory>" not in out:
        return ""
    if len(out) > RECALL_CAP:
        out = out[:RECALL_CAP] + "\n…[truncated]"
    return out + "\n\n"
