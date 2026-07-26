import json

from token_tracker import windows_terminal


def test_install_windows_terminal_kimi_watch_action(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"actions": [{"id": "User.Other", "keys": "ctrl+x"}], "keybindings": [{"id": "User.TokenTrackerKimiWatch", "keys": "ctrl+alt+k"}]}), encoding="utf-8")
    monkeypatch.setattr(windows_terminal, "_settings_path", lambda: settings)
    monkeypatch.setattr(windows_terminal.sys, "executable", r"C:\Python\python.exe")

    assert windows_terminal.install_kimi_watch_action() is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert len(data["actions"]) == 2
    assert data["keybindings"] == [{"id": "User.TokenTrackerKimiWatch", "keys": "ctrl+alt+k"}]
    action = data["actions"][-1]
    assert action["id"] == "User.TokenTrackerKimiWatch"
    assert action["command"]["action"] == "splitPane"
    assert action["command"]["split"] == "right"
    assert action["command"]["size"] == 0.20
    assert "C:/Python/python.exe" in action["command"]["commandline"]

    assert windows_terminal.install_kimi_watch_action() is False
    assert windows_terminal.uninstall_kimi_watch_action() is True
    assert json.loads(settings.read_text(encoding="utf-8")) == {"actions": [{"id": "User.Other", "keys": "ctrl+x"}], "keybindings": []}
    assert windows_terminal.uninstall_kimi_watch_action() is False


def test_windows_terminal_does_not_overwrite_invalid_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("{ invalid json", encoding="utf-8")
    monkeypatch.setattr(windows_terminal, "_settings_path", lambda: settings)

    assert windows_terminal.install_kimi_watch_action() is False
    assert settings.read_text(encoding="utf-8") == "{ invalid json"


def test_settings_path_prefers_portable_terminal(tmp_path, monkeypatch):
    local = tmp_path / "local"
    settings = local / "Microsoft" / "Windows Terminal" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}", encoding="utf-8")
    portable = tmp_path / "terminal"
    portable.mkdir()
    (portable / "defaults.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(windows_terminal.shutil, "which", lambda _name: str(portable / "wt.exe"))

    assert windows_terminal._settings_path() == settings
