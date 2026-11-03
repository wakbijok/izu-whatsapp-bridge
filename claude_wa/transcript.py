"""Bridge-kept conversation transcript per chat, so a chat can continue across
engine switches (the harness owns the history, not the model's session).

One JSONL file per chat under data/transcripts/: {"t": ts, "role": "user"|
"assistant", "text": ...}. Only text that reached the chat is recorded.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

MAX_TURNS = 20  # turns carried over on a handoff
MAX_CHARS = 7000
KEEP_LINES = 400  # file is trimmed to this many entries


class Transcript:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / ("".join(c if c.isalnum() else "_" for c in key) + ".jsonl")

    def add(self, key: str, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        p = self._path(key)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "role": role, "text": text[:4000]}, ensure_ascii=False) + "\n")
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
            if len(lines) > KEEP_LINES * 2:
                p.write_text("\n".join(lines[-KEEP_LINES:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def recent(self, key: str, turns: int = MAX_TURNS, chars: int = MAX_CHARS) -> list[dict]:
        p = self._path(key)
        if not p.exists():
            return []
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        rows = rows[-turns * 2:]
        while rows and sum(len(r["text"]) for r in rows) > chars:
            rows.pop(0)
        return rows

    def clear(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def handoff_block(self, key: str, engine_name: str) -> str:
        rows = self.recent(key)
        if not rows:
            return ""
        body = "\n".join(f"{'User' if r['role'] == 'user' else 'You'}: {r['text']}" for r in rows)
        return (
            f"[Conversation so far — you are continuing this chat on {engine_name} after an engine "
            "switch. Earlier replies were yours (same assistant, same person); carry on naturally and "
            "do not re-introduce yourself.]\n"
            f'"""\n{body}\n"""\n\n'
        )
