import asyncio
import json
from pathlib import Path

from claude_wa.bridge import (
    SLASH_RE,
    _normalize_phone,
    expand_whatsapp_identifiers,
    matches_allowed_user,
)
from claude_wa.inbox import Coalescer


def test_normalize_phone():
    assert _normalize_phone("+1 555-123 4567") == "15551234567"
    assert _normalize_phone("15551234567@s.whatsapp.net") == "15551234567"
    assert _normalize_phone("15551234567:12@s.whatsapp.net") == "15551234567"


def test_slash():
    m = SLASH_RE.match("/effort high")
    assert m
    assert m.group(1).lower() == "effort"
    assert m.group(2).strip() == "high"
    m2 = SLASH_RE.match("/code_review")
    assert m2 and m2.group(1) == "code_review"


def test_lid_allowlist(tmp_path: Path):
    (tmp_path / "lid-mapping-111122223333444_reverse.json").write_text(json.dumps("15551234567"))
    assert matches_allowed_user("111122223333444@lid", frozenset({"15551234567"}), tmp_path)
    assert "15551234567" in expand_whatsapp_identifiers("111122223333444@lid", tmp_path)
    assert not matches_allowed_user("999988887777666@lid", frozenset({"15551234567"}), tmp_path)


def test_coalesce_batches_rapid_messages():
    """Messages within the coalesce window land in ONE prompt (forward batch)."""

    async def _run():
        out = []
        c = Coalescer(lambda chat, p, att=(): out.append(p), coalesce_seconds=0.1)
        c.add("c1", "msg one")
        await asyncio.sleep(0.03)
        c.add("c1", "msg two")
        await asyncio.sleep(0.03)
        c.add("c1", "msg three")
        await asyncio.sleep(0.3)
        return out

    out = asyncio.run(_run())
    assert len(out) == 1
    assert "msg one" in out[0] and "msg three" in out[0]
    assert "answer ONCE" in out[0]


def test_flush_keeps_order_before_slash():
    async def _run():
        out = []
        c = Coalescer(lambda chat, p, att=(): out.append(p), coalesce_seconds=5)
        c.add("c1", "text first")
        c.flush("c1")
        out.append("/compact")
        return out

    assert asyncio.run(_run()) == ["text first", "/compact"]
