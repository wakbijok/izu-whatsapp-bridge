import json

from claude_wa.profiles import OWNER, load_profiles, profile_for


def test_guest_profile_is_isolated(tmp_path, monkeypatch):
    persona = tmp_path / "p.md"
    persona.write_text("You are the guest's Izu.")
    cfg = tmp_path / "profiles.json"
    cfg.write_text(json.dumps({
        "+1 555 123 4567": {
            "user_name": "Guest", "persona_file": str(persona), "cwd": str(tmp_path / "work"),
            "permission_mode": "default", "tools": ["Read"], "allowed_tools": ["Read", "mcp__dmem"],
            "mcp_servers": {"dmem": {"command": "dmem", "args": ["mcp"], "env": {"DM_CONFIG": str(tmp_path / "dm.toml")}}},
            "isolated": True,
        }
    }))
    profiles = load_profiles(str(cfg))
    assert set(profiles) == {"15551234567"}
    p = profile_for({"15551234567", "111@lid"}, profiles)
    assert p.user_name == "Guest" and p.permission_mode == "default"
    o = p.session_options()
    assert o["system_prompt"] == "You are the guest's Izu."
    assert o["setting_sources"] == [] and o["strict_mcp_config"] is True
    assert o["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert o["tools"] == ["Read"]
    assert o["mcp_servers"]["dmem"]["env"]["DM_CONFIG"].endswith("dm.toml")
    assert (tmp_path / "work").is_dir()
    assert profile_for({"999"}, profiles) is OWNER
    assert OWNER.session_options() == {}


def test_no_profiles_file(monkeypatch):
    monkeypatch.setenv("BRIDGE_PROFILES_FILE", "")
    assert load_profiles() == {}


def test_owner_profile_keeps_full_access_but_gets_tools(tmp_path):
    cfg = tmp_path / "p.json"
    cfg.write_text(json.dumps({"15550001111": {"name": "owner", "files_dir": str(tmp_path / "files"),
                                              "documents": True, "images": True}}))
    p = load_profiles(str(cfg))["15550001111"]
    assert p.documents and p.images and not p.isolated and not p.guarded
    assert p.permission_mode is None and p.session_options() == {}
    assert p.files_dir == str(tmp_path / "files")


def test_guarded_profile_defaults_lock_engine_and_slashes(tmp_path):
    cfg = tmp_path / "p.json"
    cfg.write_text(json.dumps({
        "15550002222": {"cwd": str(tmp_path / "g"), "isolated": True, "engine": "codex"},
        "15550003333": {"cwd": str(tmp_path / "h"), "isolated": True, "engines": ["claude", "GPT"],
                        "cc_slashes": ["/compact"]},
        "15550004444": {"name": "owner", "files_dir": str(tmp_path / "f")},
    }))
    ps = load_profiles(str(cfg))
    g, h, o = ps["15550002222"], ps["15550003333"], ps["15550004444"]
    assert g.guarded and g.allowed_engines() == ("codex",) and not g.allows_engine("claude")
    assert g.allows_cc_slash("compact") and g.allows_cc_slash("clear") and not g.allows_cc_slash("config")
    assert h.allowed_engines() == ("claude", "codex")
    assert h.allows_cc_slash("compact") and not h.allows_cc_slash("context")
    assert o.allowed_engines() is None and o.allows_cc_slash("config") and o.allows_engine("codex")
