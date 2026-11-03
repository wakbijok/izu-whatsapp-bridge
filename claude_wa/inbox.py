"""Inbound coalesce window (str chat keys).

WhatsApp multi-select forwards arrive one message at a time, ~1-2 s apart,
but are a single intent. Messages arriving within COALESCE_SECONDS of each
other are batched into ONE prompt and handed to the chat's lane, which writes
it to Claude Code (into the running turn if one is in progress). Interrupting
is explicit only (/stop).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

log = logging.getLogger("claude-wa.inbox")

MAX_PENDING = 8
COALESCE_SECONDS = 2.0
ChatKey = str


def batch_prompt(batch: list[str]) -> str:
    if len(batch) == 1:
        return batch[0]
    parts = "\n\n".join(f"[message {i + 1}/{len(batch)}] {m}" for i, m in enumerate(batch))
    return (
        "(Several messages in a row — likely a forwarded batch or one idea split "
        "across messages. Read them ALL as one context and answer ONCE.)\n\n"
        f"{parts}"
    )


class Coalescer:
    def __init__(
        self,
        submit: Callable[..., object],  # submit(chat, text, attachments); synchronous: keeps order
        *,
        coalesce_seconds: float = COALESCE_SECONDS,
    ):
        self._submit = submit
        self._window = max(0.0, coalesce_seconds)
        self._pending: dict[ChatKey, list[str]] = {}
        self._attach: dict[ChatKey, list[str]] = {}
        self._timers: dict[ChatKey, asyncio.TimerHandle] = {}

    def add(self, chat_id: ChatKey, text: str, attachments: tuple = ()) -> None:
        q = self._pending.setdefault(chat_id, [])
        q.append(text)
        if attachments:
            self._attach.setdefault(chat_id, []).extend(attachments)
        if len(q) > MAX_PENDING:
            dropped = len(q) - MAX_PENDING
            del q[:-MAX_PENDING]
            log.info("chat %s: dropped %d old pending msgs", chat_id, dropped)
        t = self._timers.pop(chat_id, None)
        if t is not None:
            t.cancel()  # a newer message extends the window
        if self._window <= 0:
            self.flush(chat_id)
            return
        loop = asyncio.get_running_loop()
        self._timers[chat_id] = loop.call_later(self._window, self.flush, chat_id)

    def flush(self, chat_id: ChatKey) -> None:
        t = self._timers.pop(chat_id, None)
        if t is not None:
            t.cancel()
        batch = self._pending.pop(chat_id, None)
        att = tuple(self._attach.pop(chat_id, []))
        if batch:
            self._submit(chat_id, batch_prompt(batch), att)

    def discard(self, chat_id: ChatKey) -> None:
        t = self._timers.pop(chat_id, None)
        if t is not None:
            t.cancel()
        self._pending.pop(chat_id, None)
        self._attach.pop(chat_id, None)
