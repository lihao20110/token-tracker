import json

import pytest

from token_tracker import sidebar_install


def test_skill_render_uses_installed_python(monkeypatch):
    monkeypatch.setattr(sidebar_install.sys, "executable", "/opt/tt env/bin/python")
    rendered = sidebar_install.render_skill("SKILL.md")
    assert "__TT_SIDEBAR_COMMAND__" not in rendered
    assert '"/opt/tt env/bin/python" -B -m token_tracker.sidebar_command split' in rendered
    assert sidebar_install._SKILL_MARKER in rendered


def test_skill_install_update_uninstall_roundtrip(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".agents" / "skills" / "tt-sidebar"
    monkeypatch.setattr(sidebar_install, "SIDEBAR_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr(sidebar_install.sys, "executable", "/first/python")

    assert sidebar_install.skill_needs_sync()
    assert sidebar_install.install_skill()
    assert not sidebar_install.skill_needs_sync()
    assert "name: tt-sidebar" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "$tt-sidebar" in (skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")
    assert not sidebar_install.install_skill()  # 幂等

    monkeypatch.setattr(sidebar_install.sys, "executable", "/new/python")
    assert sidebar_install.skill_needs_sync()
    assert sidebar_install.install_skill()
    assert '"/new/python"' in (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    assert sidebar_install.uninstall_skill()
    assert not (skill_dir / "SKILL.md").exists()
    assert not skill_dir.exists()


def test_skill_does_not_overwrite_user_owned_skill(tmp_path, monkeypatch):
    skill_dir = tmp_path / "tt-sidebar"
    skill_dir.mkdir()
    skill = skill_dir / "SKILL.md"
    skill.write_text("---\nname: tt-sidebar\ndescription: mine\n---\n", encoding="utf-8")
    monkeypatch.setattr(sidebar_install, "SIDEBAR_SKILL_DIR", str(skill_dir))

    assert not sidebar_install.skill_needs_sync()
    with pytest.raises(FileExistsError):
        sidebar_install.install_skill()
    assert skill.read_text(encoding="utf-8").endswith("description: mine\n---\n")
    assert not sidebar_install.uninstall_skill()


def test_hook_merge_idempotent_and_uninstall_preserves_user(tmp_path, monkeypatch):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    user_group = {
        "hooks": [{"type": "command", "command": "python3 user.py", "timeout": 9}],
    }
    path.write_text(json.dumps({"meta": "keep", "hooks": {"UserPromptSubmit": [user_group]}}), encoding="utf-8")
    monkeypatch.setattr(sidebar_install, "CODEX_HOOKS", str(path))
    monkeypatch.setattr(sidebar_install.sys, "executable", "/venv/bin/python")

    assert sidebar_install.managed_hooks_need_sync(None)
    assert sidebar_install.install_managed_hooks(None)
    assert not sidebar_install.install_managed_hooks(None)
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data["hooks"]["UserPromptSubmit"]
    assert data["meta"] == "keep"
    assert groups[0] == user_group
    command = groups[1]["hooks"][0]["command"]
    assert command == '"/venv/bin/python" -B -m token_tracker.sidebar_command prompt-hook --agent codex'

    assert sidebar_install.uninstall_managed_hooks()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "meta": "keep", "hooks": {"UserPromptSubmit": [user_group]},
    }


def test_managed_hooks_merge_both_events_and_uninstall_preserves_user(tmp_path, monkeypatch):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    user_prompt = {
        "hooks": [{"type": "command", "command": "python3 user-prompt.py", "timeout": 9}],
    }
    user_stop = {
        "hooks": [{"type": "command", "command": "python3 user-stop.py", "timeout": 7}],
    }
    path.write_text(
        json.dumps({
            "meta": "keep",
            "hooks": {"UserPromptSubmit": [user_prompt], "Stop": [user_stop]},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(sidebar_install, "CODEX_HOOKS", str(path))
    monkeypatch.setattr(sidebar_install.sys, "executable", "/installed/python")
    statusline_command = '"/installed/python" "/cfg/token-tracker/codex-statusline.py"'

    assert sidebar_install.managed_hooks_need_sync(statusline_command)
    assert sidebar_install.install_managed_hooks(statusline_command)
    assert not sidebar_install.install_managed_hooks(statusline_command)
    assert not sidebar_install.managed_hooks_need_sync(statusline_command)
    assert sidebar_install.statusline_hook_present()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["meta"] == "keep"
    assert data["hooks"]["UserPromptSubmit"][0] == user_prompt
    assert data["hooks"]["Stop"][0] == user_stop
    assert data["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"] == (
        '"/installed/python" -B -m token_tracker.sidebar_command prompt-hook --agent codex'
    )
    assert data["hooks"]["Stop"][1]["hooks"][0] == {
        "type": "command",
        "command": statusline_command,
        "timeout": 10,
    }

    assert sidebar_install.uninstall_managed_hooks()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "meta": "keep",
        "hooks": {"UserPromptSubmit": [user_prompt], "Stop": [user_stop]},
    }


def test_managed_hooks_disable_statusline_keeps_prompt_hook(tmp_path, monkeypatch):
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps({
            "hooks": {
                "Stop": [{
                    "hooks": [{
                        "type": "command",
                        "command": '"/old/python" "/old/tt-statusline.py"',
                        "timeout": 10,
                    }],
                }],
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(sidebar_install, "CODEX_HOOKS", str(path))
    monkeypatch.setattr(sidebar_install.sys, "executable", "/installed/python")

    assert sidebar_install.install_managed_hooks(None)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "Stop" not in data["hooks"]
    assert len(data["hooks"]["UserPromptSubmit"]) == 1
    assert not sidebar_install.statusline_hook_present()


def test_hook_migrates_local_prototype(tmp_path, monkeypatch):
    path = tmp_path / "hooks.json"
    legacy = (
        "/old/python -B /project/.agents/skills/tt-sidebar/scripts/prompt_hook.py "
        "--agent codex"
    )
    path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": legacy, "timeout": 2},
    ]}]}}), encoding="utf-8")
    monkeypatch.setattr(sidebar_install, "CODEX_HOOKS", str(path))

    assert sidebar_install.install_managed_hooks(None)
    raw = path.read_text(encoding="utf-8")
    assert "prompt_hook.py" not in raw
    assert raw.count("token_tracker.sidebar_command") == 1


def test_hook_refuses_corrupt_json(tmp_path, monkeypatch):
    path = tmp_path / "hooks.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(sidebar_install, "CODEX_HOOKS", str(path))

    assert not sidebar_install.managed_hooks_need_sync(None)
    with pytest.raises(ValueError):
        sidebar_install.install_managed_hooks(None)
    assert path.read_text(encoding="utf-8") == "{broken"
