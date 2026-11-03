from pathlib import Path

from claude_wa.guard import decide
from claude_wa.profiles import OWNER, Profile


def guest(tmp_path):
    return Profile(name="g", cwd=str(tmp_path), guarded=True, read_roots=(str(tmp_path),),
                   block_private_web=True, mcp_servers={"dmem": {}})


def test_owner_is_never_guarded():
    assert decide(OWNER, "Bash", {"command": "reboot"}) is None


def test_reads_limited_to_roots(tmp_path):
    g = guest(tmp_path)
    (tmp_path / "note.txt").write_text("x")
    assert decide(g, "Read", {"file_path": str(tmp_path / "note.txt")}) is None
    assert decide(g, "Read", {"file_path": str(Path.home() / ".ssh" / "config")})
    assert decide(g, "Read", {"file_path": str(tmp_path / ".." / "other" / "x")})
    assert decide(g, "Grep", {"pattern": "token", "path": "/etc"})
    assert decide(g, "Glob", {"pattern": "**/*.env"}) is None  # defaults to cwd


def test_forbidden_and_foreign_mcp(tmp_path):
    g = guest(tmp_path)
    for t in ("Bash", "Write", "Edit", "Agent"):
        assert decide(g, t, {})
    assert decide(g, "mcp__dmem__recall", {}) is None
    assert decide(g, "mcp__other__run", {})


def test_webfetch_public_only(tmp_path):
    g = guest(tmp_path)
    assert decide(g, "WebFetch", {"url": "http://10.0.0.5/"})
    assert decide(g, "WebFetch", {"url": "http://localhost:3100/send"})
    assert decide(g, "WebFetch", {"url": "http://gitlab.local/"})
    assert decide(g, "WebFetch", {"url": "file:///etc/passwd"})
    assert decide(g, "WebFetch", {"url": "https://1.1.1.1/"}) is None
