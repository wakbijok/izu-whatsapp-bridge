"""HTTP client for the vendored Baileys bridge (from NousResearch hermes-agent, MIT)."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import aiohttp

from claude_wa.config import Settings

log = logging.getLogger("claude-wa.transport")


class WhatsAppTransport:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._proc: subprocess.Popen | None = None
        self._log_fh = None

    @property
    def base(self) -> str:
        return self.settings.bridge_base

    async def start(self) -> None:
        s = self.settings
        s.session_dir.mkdir(parents=True, exist_ok=True)
        bridge_js = s.bridge_dir / "bridge.js"
        if not bridge_js.exists():
            raise SystemExit(f"bridge.js missing: {bridge_js}")
        node_modules = s.bridge_dir / "node_modules"
        if not node_modules.exists():
            log.info("npm install in %s …", s.bridge_dir)
            r = subprocess.run(
                ["npm", "install", "--no-fund", "--no-audit", "--progress=false"],
                cwd=str(s.bridge_dir),
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                raise SystemExit(f"npm install failed:\n{r.stderr[-2000:]}")

        env = os.environ.copy()
        env["WHATSAPP_MODE"] = s.mode
        env["WHATSAPP_ALLOWED_USERS"] = ",".join(sorted(s.allowed_users))
        # Pairing policy not needed; explicit allowlist.
        env.pop("WHATSAPP_DM_POLICY", None)

        log_path = s.session_dir.parent / "bridge.log"
        self._log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
        cmd = [
            "node",
            str(bridge_js),
            "--port",
            str(s.bridge_port),
            "--session",
            str(s.session_dir),
            "--mode",
            s.mode,
        ]
        log.info("starting Baileys bridge: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(s.bridge_dir),
            env=env,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
        )
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60, connect=5)
        )
        # Wait for health / connected
        for i in range(60):
            if self._proc.poll() is not None:
                raise SystemExit(
                    f"Baileys bridge exited early code={self._proc.returncode}. "
                    f"See {log_path}. If Logged out: run scripts/pair.sh"
                )
            try:
                health = await self.health()
                st = str(health.get("status") or "")
                if st == "connected" or health.get("connected") is True:
                    log.info("Baileys bridge connected: %s", health)
                    return
                if i % 10 == 0:
                    log.info("waiting for WA connect… health=%s", health)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1)
        log.warning("bridge health not connected after 60s — continue polling anyway")

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        if self._proc and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_fh = None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def health(self) -> dict[str, Any]:
        assert self._session
        async with self._session.get(f"{self.base}/health") as resp:
            return await resp.json(content_type=None)

    async def poll_messages(self) -> list[dict[str, Any]]:
        assert self._session
        async with self._session.get(f"{self.base}/messages") as resp:
            if resp.status != 200:
                text = await resp.text()
                log.warning("poll /messages %s: %s", resp.status, text[:200])
                return []
            data = await resp.json(content_type=None)
            return data if isinstance(data, list) else []

    async def send_text(self, chat_id: str, message: str, *, reply_to: str | None = None) -> dict:
        assert self._session
        body: dict[str, Any] = {"chatId": chat_id, "message": message}
        if reply_to:
            body["replyTo"] = reply_to
        async with self._session.post(f"{self.base}/send", json=body) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"send failed {resp.status}: {data}")
            return data

    async def send_media(self, chat_id: str, file_path: str, caption: str | None = None) -> dict:
        assert self._session
        body: dict[str, Any] = {"chatId": chat_id, "filePath": file_path,
                                "fileName": Path(file_path).name}
        if caption:
            body["caption"] = caption
        async with self._session.post(f"{self.base}/send-media", json=body) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"send-media failed {resp.status}: {data}")
            return data

    async def typing(self, chat_id: str) -> None:
        assert self._session
        try:
            async with self._session.post(
                f"{self.base}/typing", json={"chatId": chat_id}
            ) as resp:
                await resp.read()
        except Exception:  # noqa: BLE001
            pass
