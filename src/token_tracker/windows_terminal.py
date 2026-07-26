"""Managed Windows Terminal action for the Kimi live watcher."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

_ACTION_ID = "User.TokenTrackerKimiWatch"
_ACTION_NAME = "Token Tracker: Kimi Watch Pane"


def _settings_path() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    root = Path(local_app_data)
    portable_settings = root / "Microsoft" / "Windows Terminal" / "settings.json"
    wt_path = shutil.which("wt.exe")
    if wt_path and (Path(wt_path).parent / "defaults.json").is_file() and portable_settings.is_file():
        return portable_settings
    candidates = (
        root / "Packages" / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / "LocalState" / "settings.json",
        root / "Packages" / "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe" / "LocalState" / "settings.json",
        portable_settings,
    )
    return next((path for path in candidates if path.is_file()), None)
def _action() -> dict[str, object]:
    python = sys.executable.replace("\\", "/")
    return {
        "id": _ACTION_ID,
        "name": _ACTION_NAME,
        "command": {
            "action": "splitPane",
            "split": "right",
            "size": 0.20,
            "commandline": f'cmd.exe /k ""{python}" -B -m token_tracker.cli kimi-watch"',
        },
    }


def _binding() -> dict[str, str]:
    return {"id": _ACTION_ID, "keys": "ctrl+alt+k"}


def _read_settings(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_settings(path: Path, data: dict[str, object]) -> None:
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _without_managed_action(items: list[object]) -> list[object]:
    return [item for item in items if not (isinstance(item, dict) and item.get("id") == _ACTION_ID)]


def install_kimi_watch_action() -> bool:
    """Install the in-window watcher and remove the legacy duplicate binding."""
    path = _settings_path()
    if path is None:
        return False
    data = _read_settings(path)
    if data is None:
        return False
    actions = data.get("actions", [])
    keybindings = data.get("keybindings")
    if not isinstance(actions, list) or (keybindings is not None and not isinstance(keybindings, list)):
        return False
    updated_actions = [*_without_managed_action(actions), _action()]
    updated_keybindings = [*_without_managed_action(keybindings or []), _binding()]
    if actions == updated_actions and keybindings == updated_keybindings:
        return False
    data["actions"] = updated_actions
    data["keybindings"] = updated_keybindings
    _write_settings(path, data)
    return True


def uninstall_kimi_watch_action() -> bool:
    """Remove only the Token Tracker watcher action and its legacy shortcut."""
    path = _settings_path()
    if path is None:
        return False
    data = _read_settings(path)
    if data is None:
        return False
    actions = data.get("actions")
    keybindings = data.get("keybindings")
    if not isinstance(actions, list) or (keybindings is not None and not isinstance(keybindings, list)):
        return False
    updated_actions = _without_managed_action(actions)
    updated_keybindings = _without_managed_action(keybindings) if keybindings is not None else None
    if actions == updated_actions and keybindings == updated_keybindings:
        return False
    data["actions"] = updated_actions
    if updated_keybindings is not None:
        data["keybindings"] = updated_keybindings
    _write_settings(path, data)
    return True
