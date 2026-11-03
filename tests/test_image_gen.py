import asyncio
from pathlib import Path

import pytest

from claude_wa import codex_engine, image_gen


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    # Never touch the host user's Codex auth or ~/.cache from tests.
    monkeypatch.setattr(codex_engine, "AUTH_SRC", tmp_path / "no-auth.json")
    monkeypatch.setattr(image_gen, "DEFAULT_HOME", tmp_path / "codex-images")


def _home_png(env, name):
    d = Path(env["CODEX_HOME"]) / "generated_images" / "thread"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"\x89PNG fake")


def test_generate_runs_codex_in_folder_and_counts(tmp_path, monkeypatch):
    calls = []

    async def fake_runner(args, cwd, env):
        calls.append((args, cwd, env))
        _home_png(env, "ig_1.png")  # where the built-in tool saves it
        return 0, "done"

    monkeypatch.setattr(image_gen, "DAILY_CAP", 2)
    d = tmp_path / "lane" / "images"
    out = asyncio.run(image_gen.generate(d, "kucing.png", "seekor kucing oren", runner=fake_runner))
    assert out == d / "kucing.png" and out.read_bytes().startswith(b"\x89PNG")
    args, cwd, env = calls[0]
    assert cwd == d and args[:4] == ["exec", "--skip-git-repo-check", "--sandbox", "read-only"]
    assert args[args.index("-C") + 1] == str(d)
    assert args[-2] == "--" and args[-1].startswith("$imagegen seekor kucing oren")
    assert image_gen.quota_left(d) == 1
    asyncio.run(image_gen.generate(d, "b.png", "x", runner=lambda a, c, e: _ok(e, "b.png")))
    try:
        asyncio.run(image_gen.generate(d, "c.png", "x", runner=lambda a, c, e: _ok(e, "c.png")))
    except RuntimeError as e:
        assert "daily image limit" in str(e)
    else:
        raise AssertionError("cap not enforced")


async def _ok(env, name):
    _home_png(env, name)
    return 0, "done"


def test_image_run_is_locked_down(tmp_path, monkeypatch):
    """Own CODEX_HOME (not the owner's), shell and other tools off on the
    command line and in its config, no MCP servers, approvals never."""
    seen = {}

    async def runner(args, cwd, env):
        seen.update(args=args, env=env)
        _home_png(env, "x.png")
        return 0, "done"

    monkeypatch.setenv("CODEX_HOME", "/home/someone/.codex")
    home = tmp_path / "data" / "codex-images" / "g"
    asyncio.run(image_gen.generate(tmp_path / "lane" / "images", "a.png", "x", runner=runner, home=home))
    a, env = seen["args"], seen["env"]
    assert env["CODEX_HOME"] == str(home)
    disabled = {a[i + 1] for i, v in enumerate(a) if v == "--disable"}
    assert {"shell_tool", "view_image", "apps", "plugins", "browser_use", "computer_use"} <= disabled
    assert "danger-full-access" not in a and "workspace-write" not in a
    cfg = (home / "config.toml").read_text()
    assert 'approval_policy = "never"' in cfg and 'sandbox_mode = "read-only"' in cfg
    assert "shell_tool = false" in cfg and "view_image = false" in cfg and 'web_search = "disabled"' in cfg
    assert "[mcp_servers." not in cfg
    # the picture was moved out of CODEX_HOME into the lane's images folder
    assert not list((home / "generated_images").rglob("*.png"))
    assert (tmp_path / "lane" / "images" / "a.png").exists()


def test_image_home_must_be_outside_the_lane(tmp_path):
    async def runner(args, cwd, env):
        raise AssertionError("must not run")

    with pytest.raises(ValueError):
        asyncio.run(image_gen.generate(tmp_path / "lane" / "images", "a.png", "x", runner=runner,
                                       home=tmp_path / "lane" / ".codex"))


def test_generate_reports_failure(tmp_path):
    async def failing(args, cwd, env):
        return 1, "boom"

    try:
        asyncio.run(image_gen.generate(tmp_path / "lane" / "images", "x.png", "y", runner=failing))
    except RuntimeError as e:
        assert "could not be generated" in str(e)
    else:
        raise AssertionError


def test_reference_adds_image_flag(tmp_path):
    seen = {}

    async def runner(args, cwd, env):
        seen["args"] = args
        _home_png(env, "e.png")
        return 0, ""

    ref = tmp_path / "foto.jpg"
    ref.write_bytes(b"\xff\xd8")
    asyncio.run(image_gen.generate(tmp_path / "lane" / "images", "edit.png", "buang mug", ref, runner=runner))
    a = seen["args"]
    assert "-i" in a and a[a.index("-i") + 1] == str(ref) and "Edit the attached image" in a[-1]
    # the image list is closed before the prompt (-i takes several values)
    assert a.index("--") > a.index("-i")


def test_jail_hides_homes_and_binds_only_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(image_gen, "JAIL", "require")
    monkeypatch.setattr(image_gen, "BWRAP", "/usr/bin/bwrap")
    home, img, ref = tmp_path / "h", tmp_path / "img", tmp_path / "ref.png"
    home.mkdir()
    p = image_gen.jail_prefix(img, {"CODEX_HOME": str(home)}, ["exec", "-i", str(ref)])
    assert p[0] == "/usr/bin/bwrap" and p[-1] == "--"
    assert ["--tmpfs", "/home"] == p[p.index("/home") - 1:p.index("/home") + 1]
    assert ["--bind", str(home), str(home)] == p[p.index(str(home)) - 1:p.index(str(home)) + 2]
    assert ["--bind", str(img), str(img)] == p[p.index(str(img)) - 1:p.index(str(img)) + 2]
    assert ["--ro-bind", str(ref), str(ref)] == p[p.index(str(ref)) - 1:p.index(str(ref)) + 2]


def test_jail_off_and_missing_bwrap(tmp_path, monkeypatch):
    env = {"CODEX_HOME": str(tmp_path)}
    monkeypatch.setattr(image_gen, "JAIL", "off")
    assert image_gen.jail_prefix(tmp_path, env, []) == []
    monkeypatch.setattr(image_gen, "BWRAP", None)
    monkeypatch.setattr(image_gen, "JAIL", "auto")
    assert image_gen.jail_prefix(tmp_path, env, []) == []
    monkeypatch.setattr(image_gen, "JAIL", "require")
    with pytest.raises(RuntimeError):
        image_gen.jail_prefix(tmp_path, env, [])


def test_dedicated_image_auth_replaces_engine_link(tmp_path, monkeypatch):
    own, ded = tmp_path / "own.json", tmp_path / "dedicated.json"
    own.write_text("{}")
    ded.write_text("{}")
    home = tmp_path / "codex-images" / "h"
    home.mkdir(parents=True)
    (home / "auth.json").symlink_to(own)
    monkeypatch.setattr(image_gen, "IMAGES_AUTH", str(ded))
    asyncio.run(image_gen.generate(tmp_path / "lane" / "images", "a.png", "x", home=home,
                                   runner=lambda a, c, e: _ok(e, "a.png")))
    assert (home / "auth.json").resolve() == ded.resolve()


@pytest.mark.parametrize("state", ["file", "dangling", "foreign"])
def test_dedicated_auth_replaces_any_existing_entry(tmp_path, monkeypatch, state):
    ded = tmp_path / "dedicated.json"
    ded.write_text("{}")
    home = tmp_path / "h"
    home.mkdir()
    link = home / "auth.json"
    if state == "file":
        link.write_text("{}")
    elif state == "dangling":
        link.symlink_to(tmp_path / "gone.json")
    else:
        (tmp_path / "other.json").write_text("{}")
        link.symlink_to(tmp_path / "other.json")
    monkeypatch.setattr(image_gen, "IMAGES_AUTH", str(ded))
    image_gen._link_dedicated_auth(home)
    assert link.is_symlink() and link.resolve() == ded.resolve()


def test_dedicated_auth_refuses_missing_or_engine_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(image_gen, "IMAGES_AUTH", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError):
        image_gen._link_dedicated_auth(tmp_path / "h")
    eng = tmp_path / "engine.json"
    eng.write_text("{}")
    monkeypatch.setattr(codex_engine, "AUTH_SRC", eng)
    monkeypatch.setattr(image_gen, "IMAGES_AUTH", str(eng))
    with pytest.raises(RuntimeError):
        image_gen._link_dedicated_auth(tmp_path / "h")
