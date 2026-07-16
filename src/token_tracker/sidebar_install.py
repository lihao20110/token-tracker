"""安装随包分发的 Codex ``$tt-sidebar`` Skill 与提示词同步 Hook。"""

from __future__ import annotations

import json
import os
import stat
import sys
from importlib import resources

from .adapters.util import codex_home

CODEX_HOOKS = os.path.join(codex_home(), "hooks.json")

# Codex 官方用户级 Skill 目录是 $HOME/.agents/skills（不是 $CODEX_HOME/skills）。
SIDEBAR_SKILL_DIR = os.path.join(os.path.expanduser("~"), ".agents", "skills", "tt-sidebar")
_SKILL_PACKAGE = "token_tracker.skills.tt_sidebar"
_SKILL_FILES = ("SKILL.md", "agents/openai.yaml")
_SKILL_MARKER = "<!-- token-tracker-managed -->"
_HOOK_TOKEN = "token_tracker.sidebar_command prompt-hook --agent codex"


def _load_skill_resource(relative_path: str) -> str:
    node = resources.files(_SKILL_PACKAGE)
    for part in relative_path.split("/"):
        node = node / part
    return node.read_text(encoding="utf-8")


def build_module_command(python: str, action: str) -> str:
    """生成 Skill / Hook 共用的绝对解释器命令；不依赖 GUI Codex 的 PATH。"""
    if os.name == "nt":
        python = python.replace("\\", "/")
    return f'"{python}" -B -m token_tracker.sidebar_command {action}'


def render_skill(relative_path: str) -> str:
    content = _load_skill_resource(relative_path)
    if relative_path == "SKILL.md":
        command = build_module_command(sys.executable or "python3", "split")
        content = content.replace("__TT_SIDEBAR_COMMAND__", command)
    return content


def _write_text_atomic(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp, path)
    finally:
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass


def _write_json_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = 0o600
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass


def skill_managed() -> bool:
    try:
        with open(os.path.join(SIDEBAR_SKILL_DIR, "SKILL.md"), encoding="utf-8") as f:
            return _SKILL_MARKER in f.read()
    except OSError:
        return False


def skill_needs_sync() -> bool:
    skill_path = os.path.join(SIDEBAR_SKILL_DIR, "SKILL.md")
    if os.path.exists(skill_path) and not skill_managed():
        return False  # 同名用户 Skill 不归 tt 改，避免每次启动都触发更新
    for relative_path in _SKILL_FILES:
        path = os.path.join(SIDEBAR_SKILL_DIR, *relative_path.split("/"))
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() != render_skill(relative_path):
                    return True
        except OSError:
            return True
    return False


def install_skill() -> bool:
    skill_path = os.path.join(SIDEBAR_SKILL_DIR, "SKILL.md")
    if os.path.exists(skill_path) and not skill_managed():
        raise FileExistsError(skill_path)
    changed = False
    for relative_path in _SKILL_FILES:
        path = os.path.join(SIDEBAR_SKILL_DIR, *relative_path.split("/"))
        expected = render_skill(relative_path)
        try:
            with open(path, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            current = None
        if current != expected:
            _write_text_atomic(path, expected)
            changed = True
    return changed


def uninstall_skill() -> bool:
    if not skill_managed():
        return False
    changed = False
    for relative_path in reversed(_SKILL_FILES):
        path = os.path.join(SIDEBAR_SKILL_DIR, *relative_path.split("/"))
        try:
            os.remove(path)
            changed = True
        except FileNotFoundError:
            pass
    for directory in (os.path.join(SIDEBAR_SKILL_DIR, "agents"), SIDEBAR_SKILL_DIR):
        try:
            os.rmdir(directory)
        except OSError:
            pass  # 用户若加了其它文件就保留目录，只移除 tt 管理的 Skill 入口
    return changed


def _hook_handler() -> dict:
    return {
        "type": "command",
        "command": build_module_command(
            sys.executable or "python3", "prompt-hook --agent codex"
        ),
        "timeout": 2,
    }


def _is_hook_handler(handler) -> bool:
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    if _HOOK_TOKEN in command:
        return True
    # 迁移本地原型曾使用的绝对 prompt_hook.py 命令；只认完整 tt-sidebar + codex 特征。
    return "tt-sidebar" in command and "prompt_hook.py" in command and "--agent codex" in command


def _read_hooks() -> dict:
    if not os.path.exists(CODEX_HOOKS):
        return {}
    try:
        with open(CODEX_HOOKS, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(CODEX_HOOKS) from exc
    if not isinstance(data, dict):
        raise ValueError(CODEX_HOOKS)
    return data


def _without_hook(data: dict) -> tuple[dict, bool]:
    result = dict(data)
    hooks = result.get("hooks")
    if hooks is None:
        return result, False
    if not isinstance(hooks, dict):
        raise ValueError(CODEX_HOOKS)
    new_hooks = dict(hooks)
    groups = new_hooks.get("UserPromptSubmit")
    if groups is None:
        return result, False
    if not isinstance(groups, list):
        raise ValueError(CODEX_HOOKS)

    removed = False
    kept_groups = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept_groups.append(group)
            continue
        handlers = group["hooks"]
        kept_handlers = [handler for handler in handlers if not _is_hook_handler(handler)]
        if len(kept_handlers) == len(handlers):
            kept_groups.append(group)
            continue
        removed = True
        if kept_handlers:
            kept = dict(group)
            kept["hooks"] = kept_handlers
            kept_groups.append(kept)
    if kept_groups:
        new_hooks["UserPromptSubmit"] = kept_groups
    else:
        new_hooks.pop("UserPromptSubmit", None)
    if new_hooks:
        result["hooks"] = new_hooks
    else:
        result.pop("hooks", None)
    return result, removed


def _with_hook(data: dict) -> dict:
    result, _removed = _without_hook(data)
    hooks = result.get("hooks")
    if hooks is None:
        hooks = {}
    elif not isinstance(hooks, dict):
        raise ValueError(CODEX_HOOKS)
    else:
        hooks = dict(hooks)
    groups = hooks.get("UserPromptSubmit")
    if groups is None:
        groups = []
    elif not isinstance(groups, list):
        raise ValueError(CODEX_HOOKS)
    hooks["UserPromptSubmit"] = [*groups, {"hooks": [_hook_handler()]}]
    result["hooks"] = hooks
    return result


def hook_needs_sync() -> bool:
    try:
        data = _read_hooks()
        return data != _with_hook(data)
    except ValueError:
        return False  # 损坏的用户配置只在显式 setup 时提示，自动更新绝不覆盖


def install_hook() -> bool:
    data = _read_hooks()
    updated = _with_hook(data)
    if updated == data:
        return False
    _write_json_atomic(CODEX_HOOKS, updated)
    return True


def uninstall_hook() -> bool:
    if not os.path.exists(CODEX_HOOKS):
        return False
    data = _read_hooks()
    updated, removed = _without_hook(data)
    if not removed:
        return False
    if updated:
        _write_json_atomic(CODEX_HOOKS, updated)
    else:
        os.remove(CODEX_HOOKS)
    return True
