import plistlib
from pathlib import Path

import pytest

from dc_digest.cli import build_parser
from dc_digest.schedule import (
    LABEL,
    ScheduleError,
    build_plist,
    log_dir,
    login_shell_path,
    path_dirs,
    rotate_logs,
    summarizer_path_dirs,
)


def _plist(tmp_path: Path, **overrides) -> dict:
    arguments = {
        "uv_path": "/Users/example/.local/bin/uv",
        "project_root": Path("/Users/example/bin/dc-digest"),
        "interval_hours": 3,
        "log_dir": tmp_path / "logs",
    }
    arguments.update(overrides)
    return plistlib.loads(build_plist(**arguments))


def test_plist_runs_project_with_prune_every_three_hours(tmp_path: Path) -> None:
    data = _plist(tmp_path)
    assert data["Label"] == LABEL
    assert data["StartInterval"] == 3 * 3600
    assert data["RunAtLoad"] is False
    assert data["WorkingDirectory"] == "/Users/example/bin/dc-digest"
    assert data["ProgramArguments"] == [
        "/Users/example/.local/bin/uv",
        "run",
        "--project",
        "/Users/example/bin/dc-digest",
        "dc-digest",
        "run",
        "--prune",
    ]


def test_plist_writes_logs_under_data_directory(tmp_path: Path) -> None:
    data = _plist(tmp_path)
    # stdout 与 stderr 合并成一个文件，无人值守时只看一处
    assert data["StandardOutPath"] == data["StandardErrorPath"]
    assert data["StandardOutPath"].endswith("logs/launchd.log")
    assert (tmp_path / "logs").is_dir()


def test_plist_exposes_uv_on_path(tmp_path: Path) -> None:
    data = _plist(tmp_path)
    path = data["EnvironmentVariables"]["PATH"]
    assert path.split(":")[0] == "/Users/example/.local/bin"


def test_plist_rejects_invalid_interval(tmp_path: Path) -> None:
    with pytest.raises(ScheduleError):
        _plist(tmp_path, interval_hours=0)


def test_run_command_accepts_prune_flag() -> None:
    args = build_parser().parse_args(["run", "--prune"])
    assert args.prune is True
    assert build_parser().parse_args(["run"]).prune is False


def test_schedule_command_defaults_to_three_hours() -> None:
    args = build_parser().parse_args(["schedule", "install"])
    assert args.interval_hours == 3
    assert args.action == "install"


def test_log_dir_sits_next_to_database(tmp_path: Path) -> None:
    assert log_dir(tmp_path / "data" / "messages.db") == tmp_path / "data" / "logs"


def test_rotate_logs_keeps_recent_tail(tmp_path: Path) -> None:
    directory = tmp_path / "logs"
    directory.mkdir()
    log = directory / "launchd.log"
    log.write_bytes(b"x" * 5000 + b"RECENT")
    rotated = rotate_logs(directory, max_bytes=1000, keep_bytes=100)
    assert rotated == ["launchd.log"]
    payload = log.read_bytes()
    assert len(payload) == 100
    assert payload.endswith(b"RECENT")


def test_rotate_logs_leaves_small_log_untouched(tmp_path: Path) -> None:
    directory = tmp_path / "logs"
    directory.mkdir()
    log = directory / "launchd.log"
    log.write_text("短日志", encoding="utf-8")
    assert rotate_logs(directory, max_bytes=1000, keep_bytes=100) == []
    assert log.read_text(encoding="utf-8") == "短日志"


def test_rotate_logs_ignores_missing_directory(tmp_path: Path) -> None:
    assert rotate_logs(tmp_path / "absent") == []


def test_plist_puts_summarizer_directory_on_path(tmp_path: Path) -> None:
    data = _plist(tmp_path, extra_path_dirs=["/Users/example/bin"])
    entries = data["EnvironmentVariables"]["PATH"].split(":")
    # uv 目录在前，摘要命令目录紧随其后，launchd 精简的 PATH 才不会漏掉 spi
    assert entries[:2] == ["/Users/example/.local/bin", "/Users/example/bin"]


def test_plist_path_entries_are_deduplicated(tmp_path: Path) -> None:
    data = _plist(tmp_path, extra_path_dirs=["/usr/bin", "/Users/example/bin"])
    entries = data["EnvironmentVariables"]["PATH"].split(":")
    assert entries.count("/usr/bin") == 1
    # 传入的目录排在系统目录之前
    assert entries.index("/Users/example/bin") < entries.index("/usr/local/bin")


def test_summarizer_path_dirs_follows_resolved_command(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda command: "/opt/tools/bin/spi")
    assert summarizer_path_dirs("spi")[0] == "/opt/tools/bin"


def test_plist_always_includes_home_bin(tmp_path: Path) -> None:
    """spi 之类的本地命令常在 ~/bin，PATH 里必须无条件带上，不能依赖解析结果。"""
    data = _plist(tmp_path, extra_path_dirs=[])
    entries = data["EnvironmentVariables"]["PATH"].split(":")
    assert str(Path.home() / "bin") in entries


def test_login_shell_path_returns_absolute_dirs() -> None:
    dirs = login_shell_path("/bin/sh")
    assert dirs
    assert all(entry.startswith("/") for entry in dirs)


def test_login_shell_path_tolerates_broken_shell() -> None:
    assert login_shell_path("/nonexistent/shell-binary") == []


def test_path_dirs_keeps_summarizer_first_and_deduplicates(monkeypatch) -> None:
    monkeypatch.setattr("dc_digest.schedule.summarizer_path_dirs", lambda command: ["/opt/tools/bin"])
    monkeypatch.setattr(
        "dc_digest.schedule.login_shell_path", lambda: ["/usr/bin", "/opt/tools/bin", "/nvm/bin"]
    )
    dirs = path_dirs("spi")
    assert dirs[:3] == ["/opt/tools/bin", "/usr/bin", "/nvm/bin"]
    assert dirs.count("/opt/tools/bin") == 1
