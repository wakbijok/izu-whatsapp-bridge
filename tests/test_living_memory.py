"""Living persona + per-message recall (claude_wa.living_memory)."""
import asyncio
import os
import stat

import claude_wa.living_memory as lm
from claude_wa.profiles import Profile


def fake_dmem(tmp_path, *, fail=False):
    p = tmp_path / "dmem"
    p.write_text(
        "#!/bin/sh\n"
        + ("exit 3\n" if fail else "")
        + 'case "$3" in\n'
        '  session_start) echo "# Azu - persona from memory (cfg=$DM_CONFIG)";;\n'
        '  user_prompt_submit) read x; echo "<daimon-memory>\\n- (memory) recalled for: $x\\n</daimon-memory>";;\n'
        "esac\n"
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def prof(**kw):
    return Profile(name="g", system_prompt="SEED: You are Izu. House rules.", memory_persona=True,
                   mcp_servers={"dmem": {"command": "dmem", "args": ["mcp"], "env": {"DM_CONFIG": "/x/t.toml"}}}, **kw)


def test_living_persona_uses_memory_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "DMEM", fake_dmem(tmp_path))
    env = lm.memory_env(prof())
    assert env == {"DM_CONFIG": "/x/t.toml"}
    out = asyncio.run(lm.living_persona(env, "SEED"))
    assert out.startswith("SEED") and "Azu - persona from memory (cfg=/x/t.toml)" in out
    assert "house rules above never change" in out
    monkeypatch.setattr(lm, "DMEM", str(tmp_path / "missing"))  # memory unreachable
    assert asyncio.run(lm.living_persona(env, "SEED")) == "SEED"
    assert lm.memory_env(Profile(memory_persona=False)) is None


def test_recall_wraps_only_real_recall(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "DMEM", fake_dmem(tmp_path))
    env = lm.memory_env(prof())
    r = asyncio.run(lm.recall_for(env, "boleh edit gambar tadi"))
    assert "<daimon-memory>" in r and "edit gambar tadi" in r and r.endswith("\n\n")
    assert asyncio.run(lm.recall_for(None, "x")) == ""
