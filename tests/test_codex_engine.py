import asyncio
from pathlib import Path
from types import SimpleNamespace

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, UserMessage

from claude_wa.codex_engine import CodexClient, write_config


def test_write_config_is_sandboxed_and_lists_servers(tmp_path):
    cfg = write_config(tmp_path / "home", tmp_path / "work", model="gpt-6.1-sol", effort="max",
                       mcp_servers={"dmem": {"command": "dmem", "args": ["mcp"], "env": {"DM_CONFIG": "/x.toml"}}},
                       docs_env={"DOCS_ROOT": str(tmp_path), "DOCS_CHAT_ID": "c@s.whatsapp.net"})
    t = cfg.read_text()
    assert 'model = "gpt-6.1-sol"' in t and 'model_reasoning_effort = "xhigh"' in t
    assert 'approval_policy = "never"' in t and 'sandbox_mode = "workspace-write"' in t
    assert "shell_tool = false" in t and 'web_search = "cached"' in t
    assert f'[projects."{tmp_path / "work"}"]' in t and 'trust_level = "trusted"' in t
    assert "[mcp_servers.dmem]" in t and 'DM_CONFIG = "/x.toml"' in t and 'default_tools_approval_mode = "approve"' in t
    assert "[mcp_servers.docs]" in t and "claude_wa.docs_mcp_server" in t and "DOCS_CHAT_ID" in t


def test_home_inside_workspace_is_refused(tmp_path):
    cl = CodexClient(cwd=tmp_path, home=tmp_path / ".codex", system_prompt="", model="m", effort="low",
                     mcp_servers={}, docs_env=None, thread_id=None)
    try:
        asyncio.run(cl.connect())
    except ValueError as e:
        assert "writable" in str(e)
    else:
        raise AssertionError("home inside workspace must be refused")


def _events(thread="t1", text="hello from codex", image=None):
    ev = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"},
          {"type": "item.completed", "item": {"type": "agent_message", "text": text}}]
    if image:
        ev.append({"type": "item.completed", "item": {"type": "image_generation", "saved_path": image}})
    ev.append({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
    return ev


def test_turn_emits_claude_shaped_stream_and_resumes(tmp_path):
    calls = []
    sent = []

    async def runner(args, cwd, env, prompt):
        calls.append((args, env.get("CODEX_HOME")))
        pic = tmp_path / "home" / "generated_images" / "pic.png"
        pic.parent.mkdir(parents=True, exist_ok=True)
        pic.write_bytes(b"\x89PNG")
        return _events(text=f"echo:{prompt}", image=str(pic) if "pic" in prompt else None)

    async def on_file(path):
        sent.append(path)

    async def main():
        (tmp_path / "work").mkdir()
        (tmp_path / "a.jpg").write_bytes(b"\xff\xd8")
        cl = CodexClient(cwd=tmp_path / "work", home=tmp_path / "home", system_prompt="persona", model="gpt-6.1-sol",
                         effort="medium", mcp_servers={}, docs_env=None, thread_id=None, on_file=on_file, runner=runner)
        await cl.connect()
        assert (tmp_path / "work" / "AGENTS.md").read_text() == "persona"
        (tmp_path / "work" / "AGENTS.md").write_text("tampered")  # model edits are reverted each turn
        got = []

        async def pump():
            async for m in cl.receive_messages():
                got.append(m)

        t = asyncio.create_task(pump())

        async def one(text, uid):
            yield {"message": {"content": text}, "uuid": uid,
                   "_attachments": [str(tmp_path / "a.jpg"), "/nonexistent/x.jpg"] if "pic" in text else []}

        await cl.query(one("hi", "u1"))
        await asyncio.sleep(0.1)
        await cl.query(one("draw a pic", "u2"))
        await asyncio.sleep(0.1)
        await cl.disconnect()
        await t
        def k(m):
            if isinstance(m, ResultMessage):
                return ("ResultMessage", m.result)
            if isinstance(m, SystemMessage):
                return ("SystemMessage", m.subtype)
            if isinstance(m, UserMessage):
                return ("UserMessage", m.uuid)
            return (type(m).__name__, None)

        kinds = [k(m) for m in got]
        assert kinds[:4] == [("SystemMessage", "init"), ("UserMessage", "u1"), ("AssistantMessage", None), ("ResultMessage", "echo:hi")]
        assert kinds[4:8] == [("SystemMessage", "init"), ("UserMessage", "u2"), ("AssistantMessage", None), ("ResultMessage", "echo:draw a pic")]
        # second turn resumed the thread, carried the attachment, and the image was sent
        assert calls[0][0][:1] == ["exec"] and "resume" not in calls[0][0]
        a1 = calls[1][0]
        assert a1[0] == "exec" and a1[-2] == "--"
        r = a1.index("resume")
        assert a1[r + 1] == "t1"
        # images must come after `resume <id>` (-i takes several values and
        # would swallow the subcommand), so every -i is past the thread id
        assert all(i > r + 1 for i, x in enumerate(a1) if x == "-i")
        assert "-i" in a1 and str(tmp_path / "a.jpg") in a1 and "/nonexistent/x.jpg" not in a1
        assert a1.index("-C") < a1.index("resume")
        assert (tmp_path / "work" / "AGENTS.md").read_text() == "persona"
        assert calls[1][1] == str(tmp_path / "home")
        assert sent == [str(tmp_path / "home" / "generated_images" / "pic.png")]

    asyncio.run(main())


def test_failed_turn_yields_error_result(tmp_path):
    async def runner(args, cwd, env, prompt):
        raise RuntimeError("codex exited 1: boom")

    async def main():
        cl = CodexClient(cwd=tmp_path / "w", home=tmp_path / "h", system_prompt="", model="m", effort="low",
                         mcp_servers={}, docs_env=None, thread_id=None, runner=runner)
        await cl.connect()
        got = []

        async def pump():
            async for m in cl.receive_messages():
                got.append(m)

        t = asyncio.create_task(pump())
        await cl.query("x")
        await asyncio.sleep(0.1)
        await cl.disconnect()
        await t
        r = [m for m in got if isinstance(m, ResultMessage)][0]
        assert r.is_error and "boom" not in r.result and "send that again" in r.result

    asyncio.run(main())


def test_generated_images_are_sent_and_links_stripped(tmp_path):
    sent = []

    async def on_file(path):
        sent.append(path)

    home = tmp_path / "home"
    img = home / "generated_images" / "th" / "exec-1.png"

    async def runner(args, cwd, env, prompt):
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"\x89PNG")
        return [{"type": "thread.started", "thread_id": "t"},
                {"type": "item.completed", "item": {"type": "Extension", "kind": "image_gen.generation", "id": "exec-1"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": f"Siap!\n\n![poster]({img})\n\nSuka tak?"}},
                {"type": "turn.completed", "usage": {}}]

    async def main():
        cl = CodexClient(cwd=tmp_path / "w", home=home, system_prompt="", model="m", effort="low",
                         mcp_servers={}, docs_env=None, thread_id=None, on_file=on_file, runner=runner)
        await cl.connect()
        got = []

        async def pump():
            async for m in cl.receive_messages():
                got.append(m)

        t = asyncio.create_task(pump())
        await cl.query("lukis poster")
        await asyncio.sleep(0.2)
        await cl.disconnect()
        await t
        assert sent == [str(img)]  # sent once, not twice
        texts = [b.text for m in got if isinstance(m, AssistantMessage) for b in m.content]
        assert texts == ["Siap!\n\n\n\nSuka tak?"] or texts == ["Siap!\n\nSuka tak?"]
        assert str(img) not in [m.result for m in got if isinstance(m, ResultMessage)][0]

    asyncio.run(main())


def test_links_outside_lane_folders_are_not_sent(tmp_path):
    sent = []

    async def on_file(path):
        sent.append(path)

    secret = tmp_path / "elsewhere" / "id.png"
    secret.parent.mkdir()
    secret.write_bytes(b"\x89PNG")

    async def runner(args, cwd, env, prompt):
        return [{"type": "item.completed", "item": {"type": "agent_message", "text": f"![x]({secret})"}},
                {"type": "item.completed", "item": {"type": "image_generation", "saved_path": str(secret)}},
                {"type": "turn.completed", "usage": {}}]

    async def main():
        cl = CodexClient(cwd=tmp_path / "w", home=tmp_path / "h", system_prompt="", model="m", effort="low",
                         mcp_servers={}, docs_env=None, thread_id=None, on_file=on_file, runner=runner)
        await cl.connect()

        async def pump():
            async for _ in cl.receive_messages():
                pass

        t = asyncio.create_task(pump())
        await cl.query("x")
        await asyncio.sleep(0.2)
        await cl.disconnect()
        await t
        assert sent == []

    asyncio.run(main())


def test_prompt_provider_rewrites_instructions_each_turn(tmp_path):
    work, home = tmp_path / "work", tmp_path / "home"
    versions = iter(["persona v1 (Izu)", "persona v2 (Azu)"])
    seen = []

    async def provider():
        return next(versions)

    async def runner(args, cwd, env, prompt):
        seen.append((work / "AGENTS.md").read_text())
        return _events()

    async def main():
        cl = CodexClient(cwd=work, home=home, system_prompt="seed", model="m", effort="low", mcp_servers={},
                         docs_env=None, thread_id=None, runner=runner, prompt_provider=provider)
        await cl.connect()
        for _ in range(2):
            await cl._turn("hi", "", [])

    asyncio.run(main())
    assert seen == ["persona v1 (Izu)", "persona v2 (Azu)"]


def test_old_pictures_are_never_resent_after_restart(tmp_path):
    """Regression (04-11-2026): after a reboot every picture on disk was re-sent,
    because mtimes were compared with time.monotonic()."""
    import os, time
    work, home = tmp_path / "work", tmp_path / "home"
    gi = home / "generated_images" / "x"
    gi.mkdir(parents=True)
    old = gi / "old.png"
    old.write_bytes(b"\x89PNG")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    sent = []

    async def on_file(p):
        sent.append(p)

    async def runner(args, cwd, env, prompt):
        (gi / "new.png").write_bytes(b"\x89PNG")  # made during this turn
        return _events()

    async def main():
        cl = CodexClient(cwd=work, home=home, system_prompt="", model="m", effort="low", mcp_servers={},
                         docs_env=None, thread_id="t1", runner=runner, on_file=on_file)
        await cl.connect()
        await cl._turn("hi", "", [])

    asyncio.run(main())
    assert [os.path.basename(p) for p in sent] == ["new.png"]
