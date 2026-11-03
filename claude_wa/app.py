"""App entry: start Baileys transport + poll loop + Claude turns."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from claude_wa.bridge import Bridge
from claude_wa.config import Settings
from claude_wa.transport import WhatsAppTransport

log = logging.getLogger("claude-wa")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def _main_async() -> None:
    settings = Settings.from_env()
    transport = WhatsAppTransport(settings)
    bridge = Bridge(settings, transport)
    stop = asyncio.Event()

    def _stop(*_a):
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    await transport.start()
    log.info(
        "claude-wa v0.2 starting — mode=%s port=%s allowed=%d",
        settings.mode,
        settings.bridge_port,
        len(settings.allowed_users),
    )

    try:
        while not stop.is_set():
            if not transport.alive():
                log.error("Baileys process died — exiting for systemd restart")
                break
            try:
                msgs = await transport.poll_messages()
            except Exception:  # noqa: BLE001
                log.warning("poll failed", exc_info=True)
                await asyncio.sleep(2)
                continue
            for ev in msgs:
                try:
                    # fire-and-forget per chat via inbox locks
                    asyncio.create_task(bridge.handle_event(ev))
                except Exception:  # noqa: BLE001
                    log.exception("handle_event")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await transport.stop()
        log.info("claude-wa stopped")


def main() -> None:
    _setup_logging()
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
