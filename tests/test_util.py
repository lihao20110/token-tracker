import os
import time
from datetime import UTC, datetime, timedelta

from token_tracker.adapters import claude, codex
from token_tracker.adapters.util import file_may_have_events_since, project_from_cwd


def test_project_from_cwd_git_root(tmp_path):
    # 仓库根 + 子目录都归到仓库根（向上找 .git）
    repo = tmp_path / "infohunter"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "official"
    sub.mkdir()
    assert project_from_cwd(str(repo)) == "infohunter"
    assert project_from_cwd(str(sub)) == "infohunter"


def test_project_from_cwd_subdir_deleted(tmp_path):
    # 子目录已删、仓库根还在 → dirname 向上仍命中 .git，归到仓库根
    repo = tmp_path / "infohunter"
    (repo / ".git").mkdir(parents=True)
    gone = repo / "official"  # 故意不创建，模拟已删
    assert project_from_cwd(str(gone)) == "infohunter"


def test_project_from_cwd_git_file(tmp_path):
    # worktree / submodule 的 .git 是文件而非目录，也应识别为仓库根
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere\n")
    assert project_from_cwd(str(repo / "sub")) == "myrepo"


def test_project_from_cwd_non_git_fallback(tmp_path):
    # 非 git 目录 → 回退最后一段
    d = tmp_path / "loose" / "folder"
    d.mkdir(parents=True)
    assert project_from_cwd(str(d)) == "folder"


def test_file_may_have_events_since_uses_mtime(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text("{}\n")
    now = datetime.now(UTC)
    os.utime(path, (time.time() - 7200, time.time() - 7200))
    assert file_may_have_events_since(path, now - timedelta(hours=1)) is False
    assert file_may_have_events_since(path, None) is True


def test_claude_window_skips_stale_files_before_parsing(tmp_path, monkeypatch):
    base = tmp_path / "projects"
    base.mkdir()
    stale = base / "stale.jsonl"
    recent = base / "recent.jsonl"
    stale.write_text("{}\n")
    recent.write_text("{}\n")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    parsed: list[str] = []
    monkeypatch.setattr(claude, "_get_claude_dirs", lambda: [str(base)])
    monkeypatch.setattr(claude, "_parse_jsonl", lambda path, *args: parsed.append(path.name))

    claude.load_entries(hours_back=1)

    assert parsed == ["recent.jsonl"]


def test_codex_window_skips_stale_files_before_parsing(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    stale = sessions / "stale.jsonl"
    recent = sessions / "recent.jsonl"
    stale.write_text("{}\n")
    recent.write_text("{}\n")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    parsed: list[str] = []
    monkeypatch.setattr(codex, "SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(codex, "_load_thread_models", lambda: {})
    monkeypatch.setattr(codex, "_parse_jsonl", lambda path, *args: parsed.append(path.name))

    codex.load_entries(hours_back=1)

    assert parsed == ["recent.jsonl"]
