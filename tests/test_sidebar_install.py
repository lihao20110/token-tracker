import json
import tomllib

import pytest

from token_tracker import sidebar_install


def test_skill_render_uses_installed_python(monkeypatch):
    monkeypatch.setattr(sidebar_install.sys, "executable", "/opt/tt env/bin/python")
    rendered = sidebar_install.render_skill("SKILL.md")
    assert "__TT_SIDEBAR_COMMAND__" not in rendered
    assert '"/opt/tt env/bin/python" -B -m token_tracker.sidebar_command split' in rendered
    assert sidebar_install._SKILL_MARKER in rendered
    assert "ITERM_SESSION_ID" in rendered
    assert "TMUX_PANE" in rendered
    assert "sandbox_permissions" in rendered
    assert "require_escalated" in rendered
    assert "justification" in rendered
    assert "prefix_rule" in rendered
    assert "never approve Python generally" in rendered
    assert "Do not first try the iTerm2 launcher inside the sandbox" in rendered


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


def test_kimi_skill_install_update_uninstall_roundtrip(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".kimi-code" / "skills" / "tt-sidebar"
    monkeypatch.setattr(sidebar_install, "KIMI_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr(sidebar_install.sys, "executable", "/first/python")

    assert sidebar_install.kimi_skill_needs_sync()
    assert sidebar_install.install_kimi_skill()
    assert not sidebar_install.kimi_skill_needs_sync()
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "name: tt-sidebar" in content
    assert '"/first/python" -B -m token_tracker.sidebar_command split' in content
    assert sidebar_install._SKILL_MARKER in content
    assert not sidebar_install.install_kimi_skill()  # 幂等

    monkeypatch.setattr(sidebar_install.sys, "executable", "/new/python")
    assert sidebar_install.kimi_skill_needs_sync()
    assert sidebar_install.install_kimi_skill()
    assert '"/new/python"' in (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    assert sidebar_install.uninstall_kimi_skill()
    assert not (skill_dir / "SKILL.md").exists()
    assert not skill_dir.exists()


def test_kimi_hook_merges_into_config_toml_and_preserves_user(tmp_path, monkeypatch):
    path = tmp_path / ".kimi-code" / "config.toml"
    path.parent.mkdir()
    user_block = (
        'default_model = "kimi-code/k3"\n\n'
        '[[hooks]]\nevent = "PreToolUse"\nmatcher = "Bash"\ncommand = "node user.mjs"\ntimeout = 5\n'
    )
    path.write_text(user_block, encoding="utf-8")
    monkeypatch.setattr(sidebar_install, "KIMI_CONFIG", str(path))
    monkeypatch.setattr(sidebar_install.sys, "executable", "/venv/bin/python")

    assert sidebar_install.kimi_hooks_need_sync()
    assert sidebar_install.install_kimi_hooks()
    assert not sidebar_install.install_kimi_hooks()  # 幂等
    assert not sidebar_install.kimi_hooks_need_sync()

    content = path.read_text(encoding="utf-8")
    assert content.startswith(user_block)  # 用户配置原样保留，托管块追加在末尾
    parsed = tomllib.loads(content)
    assert parsed["default_model"] == "kimi-code/k3"
    tt_hooks = [h for h in parsed["hooks"] if "token_tracker" in h.get("command", "")]
    assert tt_hooks == [{
        "event": "UserPromptSubmit",
        "command": '"/venv/bin/python" -B -m token_tracker.sidebar_command prompt-hook --agent kimi',
        "timeout": 2,
    }]
    assert {"event": "PreToolUse", "matcher": "Bash", "command": "node user.mjs", "timeout": 5} in parsed["hooks"]

    # 解释器换了 → 需要重同步；旧托管块被替换而非叠加
    monkeypatch.setattr(sidebar_install.sys, "executable", "/new/python")
    assert sidebar_install.kimi_hooks_need_sync()
    assert sidebar_install.install_kimi_hooks()
    content = path.read_text(encoding="utf-8")
    assert content.count("prompt-hook --agent kimi") == 1
    assert '"/new/python"' in content

    assert sidebar_install.uninstall_kimi_hooks()
    assert path.read_text(encoding="utf-8") == user_block
    assert not sidebar_install.uninstall_kimi_hooks()


def test_kimi_hook_installs_into_missing_config(tmp_path, monkeypatch):
    path = tmp_path / ".kimi-code" / "config.toml"
    monkeypatch.setattr(sidebar_install, "KIMI_CONFIG", str(path))
    monkeypatch.setattr(sidebar_install.sys, "executable", "/venv/bin/python")

    assert sidebar_install.install_kimi_hooks()
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["hooks"] == [{
        "event": "UserPromptSubmit",
        "command": '"/venv/bin/python" -B -m token_tracker.sidebar_command prompt-hook --agent kimi',
        "timeout": 2,
    }]


def test_kimi_hook_refuses_corrupt_toml(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("default_model = [broken", encoding="utf-8")
    monkeypatch.setattr(sidebar_install, "KIMI_CONFIG", str(path))

    assert not sidebar_install.kimi_hooks_need_sync()
    with pytest.raises(ValueError):
        sidebar_install.install_kimi_hooks()
    assert path.read_text(encoding="utf-8") == "default_model = [broken"


def test_kimi_hook_collapses_normalized_duplicate_blocks(tmp_path, monkeypatch):
    """Kimi CLI 重写 config.toml 会把 literal string 归一化成 basic string（单引号→双引号），
    历史上旧正则识别不了导致托管块累积、每个提示词触发多次 hook。"""
    path = tmp_path / "config.toml"
    stale = (
        'default_model = "kimi-code/k3"\n\n'
        "[[hooks]]\n"
        'event = "UserPromptSubmit"\n'
        'command = "\\"/old/venv/bin/python3\\" -B -m token_tracker.sidebar_command prompt-hook --agent kimi"\n'
        "timeout = 2\n\n"
        "[[hooks]]\n"
        'event = "UserPromptSubmit"\n'
        'command = "\\"/another/python\\" -B -m token_tracker.sidebar_command prompt-hook --agent kimi"\n'
        "timeout = 2\n"
    )
    path.write_text(stale, encoding="utf-8")
    monkeypatch.setattr(sidebar_install, "KIMI_CONFIG", str(path))
    monkeypatch.setattr(sidebar_install.sys, "executable", "/venv/bin/python")

    assert sidebar_install.kimi_hooks_need_sync()
    assert sidebar_install.install_kimi_hooks()
    content = path.read_text(encoding="utf-8")
    assert content.count("prompt-hook --agent kimi") == 1  # 两个旧块被收敛成一个
    assert '"/venv/bin/python"' in content
    assert content.startswith('default_model = "kimi-code/k3"')  # 用户配置保留
    assert not sidebar_install.install_kimi_hooks()
    assert not sidebar_install.kimi_hooks_need_sync()

    assert sidebar_install.uninstall_kimi_hooks()
    assert "prompt-hook --agent kimi" not in path.read_text(encoding="utf-8")


def test_kimi_hook_normalized_current_block_is_up_to_date(tmp_path, monkeypatch):
    """双引号归一化后的当前块语义上已是最新：不再判为待同步，避免 tt 与 Kimi 互相重写抖动。"""
    path = tmp_path / "config.toml"
    normalized = (
        'default_model = "kimi-code/k3"\n\n'
        "[[hooks]]\n"
        'event = "UserPromptSubmit"\n'
        'command = "\\"/venv/bin/python\\" -B -m token_tracker.sidebar_command prompt-hook --agent kimi"\n'
        "timeout = 2\n"
    )
    path.write_text(normalized, encoding="utf-8")
    monkeypatch.setattr(sidebar_install, "KIMI_CONFIG", str(path))
    monkeypatch.setattr(sidebar_install.sys, "executable", "/venv/bin/python")

    assert not sidebar_install.kimi_hooks_need_sync()
    assert not sidebar_install.install_kimi_hooks()
    assert path.read_text(encoding="utf-8") == normalized  # 一字节都不动
