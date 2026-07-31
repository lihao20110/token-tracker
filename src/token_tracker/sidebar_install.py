"""安装随包分发的 Codex/Kimi ``tt-sidebar`` Skill 与 Token Tracker 用户级 Hooks。"""

from __future__ import annotations

import json
import os
import stat
import sys
import tomllib
from collections.abc import Callable
from importlib import resources

from .adapters.util import codex_home, kimi_home

CODEX_HOOKS = os.path.join(codex_home(), "hooks.json")
KIMI_CONFIG = os.path.join(kimi_home(), "config.toml")

# Codex 官方用户级 Skill 目录是 $HOME/.agents/skills（不是 $CODEX_HOME/skills）。
SIDEBAR_SKILL_DIR = os.path.join(os.path.expanduser("~"), ".agents", "skills", "tt-sidebar")
_SKILL_PACKAGE = "token_tracker.skills.tt_sidebar"
_SKILL_FILES = ("SKILL.md", "agents/openai.yaml")
_SKILL_MARKER = "<!-- token-tracker-managed -->"
_PROMPT_HOOK_TOKEN = "token_tracker.sidebar_command prompt-hook --agent codex"
_STATUSLINE_HOOK_TOKENS = ("codex-statusline.py", "tt-statusline.py")

# Kimi 官方用户级 Skill 目录是 $KIMI_CODE_HOME/skills（也扫描 ~/.agents/skills，
# 但那是 Codex 侧同名 Skill 的位置；Kimi 专属副本放自己的目录、优先级更高）。
KIMI_SKILL_DIR = os.path.join(kimi_home(), "skills", "tt-sidebar")
_KIMI_SKILL_PACKAGE = "token_tracker.skills.tt_sidebar_kimi"
_KIMI_SKILL_FILES = ("SKILL.md",)


def _load_skill_resource(relative_path: str, package: str = _SKILL_PACKAGE) -> str:
    node = resources.files(package)
    for part in relative_path.split("/"):
        node = node / part
    return node.read_text(encoding="utf-8")


def build_module_command(python: str, action: str) -> str:
    """生成 Skill / Hook 共用的绝对解释器命令；不依赖 GUI Codex 的 PATH。"""
    if os.name == "nt":
        python = python.replace("\\", "/")
    return f'"{python}" -B -m token_tracker.sidebar_command {action}'


def render_skill(relative_path: str, package: str = _SKILL_PACKAGE) -> str:
    content = _load_skill_resource(relative_path, package)
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


def _skill_managed(skill_dir: str) -> bool:
    try:
        with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as f:
            return _SKILL_MARKER in f.read()
    except OSError:
        return False


def _skill_needs_sync(skill_dir: str, package: str, files: tuple[str, ...]) -> bool:
    skill_path = os.path.join(skill_dir, "SKILL.md")
    if os.path.exists(skill_path) and not _skill_managed(skill_dir):
        return False  # 同名用户 Skill 不归 tt 改，避免每次启动都触发更新
    for relative_path in files:
        path = os.path.join(skill_dir, *relative_path.split("/"))
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() != render_skill(relative_path, package):
                    return True
        except OSError:
            return True
    return False


def _install_skill(skill_dir: str, package: str, files: tuple[str, ...]) -> bool:
    skill_path = os.path.join(skill_dir, "SKILL.md")
    if os.path.exists(skill_path) and not _skill_managed(skill_dir):
        raise FileExistsError(skill_path)
    changed = False
    for relative_path in files:
        path = os.path.join(skill_dir, *relative_path.split("/"))
        expected = render_skill(relative_path, package)
        try:
            with open(path, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            current = None
        if current != expected:
            _write_text_atomic(path, expected)
            changed = True
    return changed


def _uninstall_skill(skill_dir: str, files: tuple[str, ...]) -> bool:
    if not _skill_managed(skill_dir):
        return False
    changed = False
    for relative_path in reversed(files):
        path = os.path.join(skill_dir, *relative_path.split("/"))
        try:
            os.remove(path)
            changed = True
        except FileNotFoundError:
            pass
    for directory in (os.path.join(skill_dir, "agents"), skill_dir):
        try:
            os.rmdir(directory)
        except OSError:
            pass  # 用户若加了其它文件就保留目录，只移除 tt 管理的 Skill 入口
    return changed


def skill_managed() -> bool:
    return _skill_managed(SIDEBAR_SKILL_DIR)


def skill_needs_sync() -> bool:
    return _skill_needs_sync(SIDEBAR_SKILL_DIR, _SKILL_PACKAGE, _SKILL_FILES)


def install_skill() -> bool:
    return _install_skill(SIDEBAR_SKILL_DIR, _SKILL_PACKAGE, _SKILL_FILES)


def uninstall_skill() -> bool:
    return _uninstall_skill(SIDEBAR_SKILL_DIR, _SKILL_FILES)


def _prompt_hook_handler() -> dict:
    return {
        "type": "command",
        "command": build_module_command(
            sys.executable or "python3", "prompt-hook --agent codex"
        ),
        "timeout": 2,
    }


def _statusline_hook_handler(command: str) -> dict:
    return {
        "type": "command",
        "command": command,
        "timeout": 10,
    }


def _is_prompt_hook_handler(handler) -> bool:
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    if _PROMPT_HOOK_TOKEN in command:
        return True
    # 迁移本地原型曾使用的绝对 prompt_hook.py 命令；只认完整 tt-sidebar + codex 特征。
    return "tt-sidebar" in command and "prompt_hook.py" in command and "--agent codex" in command


def _is_statusline_hook_handler(handler) -> bool:
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    return isinstance(command, str) and any(token in command for token in _STATUSLINE_HOOK_TOKENS)


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


def _without_event_handler(
    data: dict,
    event: str,
    is_managed: Callable[[object], bool],
) -> tuple[dict, bool]:
    result = dict(data)
    hooks = result.get("hooks")
    if hooks is None:
        return result, False
    if not isinstance(hooks, dict):
        raise ValueError(CODEX_HOOKS)
    new_hooks = dict(hooks)
    groups = new_hooks.get(event)
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
        kept_handlers = [handler for handler in handlers if not is_managed(handler)]
        if len(kept_handlers) == len(handlers):
            kept_groups.append(group)
            continue
        removed = True
        if kept_handlers:
            kept = dict(group)
            kept["hooks"] = kept_handlers
            kept_groups.append(kept)
    if kept_groups:
        new_hooks[event] = kept_groups
    else:
        new_hooks.pop(event, None)
    if new_hooks:
        result["hooks"] = new_hooks
    else:
        result.pop("hooks", None)
    return result, removed


def _with_event_handler(
    data: dict,
    event: str,
    handler: dict,
    is_managed: Callable[[object], bool],
) -> dict:
    result, _removed = _without_event_handler(data, event, is_managed)
    hooks = result.get("hooks")
    if hooks is None:
        hooks = {}
    elif not isinstance(hooks, dict):
        raise ValueError(CODEX_HOOKS)
    else:
        hooks = dict(hooks)
    groups = hooks.get(event)
    if groups is None:
        groups = []
    elif not isinstance(groups, list):
        raise ValueError(CODEX_HOOKS)
    hooks[event] = [*groups, {"hooks": [handler]}]
    result["hooks"] = hooks
    return result


def _without_prompt_hook(data: dict) -> tuple[dict, bool]:
    return _without_event_handler(data, "UserPromptSubmit", _is_prompt_hook_handler)


def _with_prompt_hook(data: dict) -> dict:
    return _with_event_handler(
        data,
        "UserPromptSubmit",
        _prompt_hook_handler(),
        _is_prompt_hook_handler,
    )


def _without_statusline_hook(data: dict) -> tuple[dict, bool]:
    return _without_event_handler(data, "Stop", _is_statusline_hook_handler)


def _with_statusline_hook(data: dict, command: str) -> dict:
    return _with_event_handler(
        data,
        "Stop",
        _statusline_hook_handler(command),
        _is_statusline_hook_handler,
    )


def _with_managed_hooks(data: dict, statusline_command: str | None) -> dict:
    result = _with_prompt_hook(data)
    result, _removed = _without_statusline_hook(result)
    if statusline_command is not None:
        result = _with_statusline_hook(result, statusline_command)
    return result


def managed_hooks_need_sync(statusline_command: str | None) -> bool:
    """两个 Token Tracker Hook 是否需要统一同步到用户级 hooks.json。"""
    try:
        data = _read_hooks()
        return data != _with_managed_hooks(data, statusline_command)
    except ValueError:
        return False  # 损坏配置只在显式 setup 时提示，自动更新绝不覆盖


def statusline_hook_present() -> bool:
    """hooks.json 是否含 Token Tracker 的 Stop handler（命令版本是否最新由同步检查负责）。"""
    try:
        _updated, removed = _without_statusline_hook(_read_hooks())
        return removed
    except ValueError:
        return False


def install_managed_hooks(statusline_command: str | None) -> bool:
    """原子合并 UserPromptSubmit 与可选 Stop；保留用户其它事件、分组和 handler。"""
    data = _read_hooks()
    updated = _with_managed_hooks(data, statusline_command)
    if updated == data:
        return False
    _write_json_atomic(CODEX_HOOKS, updated)
    return True


def uninstall_managed_hooks() -> bool:
    """同时移除 Token Tracker 的两个 handler，保留 hooks.json 中全部用户配置。"""
    if not os.path.exists(CODEX_HOOKS):
        return False
    data = _read_hooks()
    updated, prompt_removed = _without_prompt_hook(data)
    updated, statusline_removed = _without_statusline_hook(updated)
    if not prompt_removed and not statusline_removed:
        return False
    if updated:
        _write_json_atomic(CODEX_HOOKS, updated)
    else:
        os.remove(CODEX_HOOKS)
    return True


# --- Kimi Code（config.toml 的 [[hooks]] + $KIMI_CODE_HOME/skills） ---

_KIMI_HOOK_COMMAND_ACTION = "prompt-hook --agent kimi"
# tt 托管块的唯一身份标识是 command 里的这串 token；引号风格、键顺序、附加键都不参与判定。
# Kimi CLI 自己重写 config.toml 时会把 literal string 归一化成 basic string（单引号变双引号），
# 所以识别必须按「[[hooks]] 块内含 token」而不是逐字节匹配 tt 写入时的格式。
_KIMI_HOOK_TOKEN = "token_tracker.sidebar_command prompt-hook --agent kimi"


def kimi_skill_managed() -> bool:
    return _skill_managed(KIMI_SKILL_DIR)


def kimi_skill_needs_sync() -> bool:
    return _skill_needs_sync(KIMI_SKILL_DIR, _KIMI_SKILL_PACKAGE, _KIMI_SKILL_FILES)


def install_kimi_skill() -> bool:
    return _install_skill(KIMI_SKILL_DIR, _KIMI_SKILL_PACKAGE, _KIMI_SKILL_FILES)


def uninstall_kimi_skill() -> bool:
    return _uninstall_skill(KIMI_SKILL_DIR, _KIMI_SKILL_FILES)


def _kimi_hook_block() -> str:
    command = build_module_command(sys.executable or "python3", _KIMI_HOOK_COMMAND_ACTION)
    return (
        "[[hooks]]\n"
        'event = "UserPromptSubmit"\n'
        f"command = '{command}'\n"
        "timeout = 2\n"
    )


def _read_kimi_config() -> str:
    """读 Kimi config.toml 原文；不存在返回空串；TOML 损坏抛 ValueError（不静默覆盖用户配置）。"""
    if not os.path.exists(KIMI_CONFIG):
        return ""
    try:
        with open(KIMI_CONFIG, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return ""
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(KIMI_CONFIG) from exc
    return content


def _kimi_managed_line_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """托管 [[hooks]] 块的行区间 [start, end)。按行扫描而不是正则整块匹配：
    空行归属（前一个块的尾部 vs 后一个块的分隔）不会有歧义，删除后能精确还原用户文本。"""
    starts = [i for i, line in enumerate(lines) if line.lstrip().startswith("[")]
    ranges = []
    for index, start in enumerate(starts):
        if lines[start].strip() != "[[hooks]]":
            continue
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        if _KIMI_HOOK_TOKEN in "".join(lines[start:end]):
            ranges.append((start, end))
    return ranges


def _kimi_hook_blocks(content: str) -> list[str]:
    """所有 tt 托管的 [[hooks]] 块文本（含历史被 Kimi 归一化成双引号的旧块）。"""
    lines = content.splitlines(keepends=True)
    return ["".join(lines[start:end]) for start, end in _kimi_managed_line_ranges(lines)]


def _kimi_block_entry(block: str) -> dict | None:
    """块文本本身就是合法 TOML 片段，直接解析取首个表项；解析失败返回 None。"""
    try:
        data = tomllib.loads(block)
    except tomllib.TOMLDecodeError:
        return None
    hooks = data.get("hooks")
    if isinstance(hooks, list) and hooks and isinstance(hooks[0], dict):
        return hooks[0]
    return None


def _kimi_hook_up_to_date(content: str) -> bool:
    """语义判同步：恰好一个托管块且 event/command 与当前解释器一致。
    不做文本级比较——引号风格被 Kimi 归一化后也算最新，避免 tt 与 Kimi 互相重写抖动。"""
    blocks = _kimi_hook_blocks(content)
    if len(blocks) != 1:
        return False
    entry = _kimi_block_entry(blocks[0])
    if entry is None:
        return False
    expected = build_module_command(sys.executable or "python3", _KIMI_HOOK_COMMAND_ACTION)
    return entry.get("event") == "UserPromptSubmit" and entry.get("command") == expected


def _without_kimi_hook(content: str) -> tuple[str, bool]:
    lines = content.splitlines(keepends=True)
    ranges = _kimi_managed_line_ranges(lines)
    if not ranges:
        return content, False
    removed: set[int] = set()
    for start, end in ranges:
        # 块前的空行是安装时补的分隔空行，随块一并移除
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        removed.update(range(start, end))
    updated = "".join(line for i, line in enumerate(lines) if i not in removed)
    return updated, True


def _with_kimi_hook(content: str) -> str:
    """已是最新就原样返回；否则移除全部旧托管块后在文件末尾追加当前块。
    [[hooks]] 是顶级 array-of-tables 头，追加在 EOF 永远是合法 TOML，用户其它配置原样保留。"""
    if _kimi_hook_up_to_date(content):
        return content
    stripped, _removed = _without_kimi_hook(content)
    block = _kimi_hook_block()
    if not stripped.strip():
        return block
    return stripped.rstrip("\n") + "\n\n" + block


def kimi_hooks_need_sync() -> bool:
    try:
        content = _read_kimi_config()
        return content != _with_kimi_hook(content)
    except ValueError:
        return False  # 损坏配置只在显式 setup 时提示，自动更新绝不覆盖


def install_kimi_hooks() -> bool:
    content = _read_kimi_config()
    updated = _with_kimi_hook(content)
    if updated == content:
        return False
    _write_text_atomic(KIMI_CONFIG, updated)
    return True


def uninstall_kimi_hooks() -> bool:
    if not os.path.exists(KIMI_CONFIG):
        return False
    content = _read_kimi_config()
    updated, removed = _without_kimi_hook(content)
    if not removed:
        return False
    _write_text_atomic(KIMI_CONFIG, updated)
    return True
