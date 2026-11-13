"""launchd 定时任务：生成、安装与查看 LaunchAgent。

plist 写在 ~/Library/LaunchAgents 下，内容含本机绝对路径，因此不进仓库；
仓库里只有这段生成逻辑。
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

LABEL = "local.dc-digest"
DOMAIN_PREFIX = "gui"
# 定时任务把 stdout 与 stderr 合并到同一个日志文件，便于无人值守时只看一处
LOG_FILE_NAME = "launchd.log"
LOG_MAX_BYTES = 2_000_000
LOG_KEEP_BYTES = 200_000


class ScheduleError(RuntimeError):
    """launchd 操作失败。"""


def _path_entries(uv_path: str, extra_path_dirs: Sequence[str]) -> list[str]:
    """定时任务的 PATH：uv、摘要命令目录、~ 下的 bin 在前，系统目录兜底，去重保序。"""
    entries = [str(Path(uv_path).parent), *extra_path_dirs, str(Path.home() / "bin")]
    entries += [
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return ordered


SHELL_PATH_TIMEOUT = 20.0


def login_shell_path(shell: str | None = None) -> list[str]:
    """取登录 shell 的 PATH 作为定时任务的 PATH 快照。

    launchd 启动的任务只有极简 PATH；spi 是 bash 脚本、内部 exec 的 pi 又依赖
    node，node 往往在 nvm 这类版本管理器目录里。安装时把用户 shell 的 PATH
    抄进来，比在代码里硬编码猜测目录可靠。
    """
    shell = shell or os.environ.get("SHELL") or "/bin/zsh"
    try:
        result = subprocess.run(
            [shell, "-lic", 'printf %s "$PATH"'],
            capture_output=True,
            text=True,
            timeout=SHELL_PATH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return []
    return [entry for entry in lines[-1].split(":") if entry.startswith("/")]


def path_dirs(command: str) -> list[str]:
    """定时任务 PATH 的附加目录：摘要命令所在目录、登录 shell 的 PATH、~/bin。"""
    candidates = [
        *summarizer_path_dirs(command),
        *login_shell_path(),
        str(Path.home() / "bin"),
    ]
    ordered: list[str] = []
    for entry in candidates:
        if entry not in ordered:
            ordered.append(entry)
    return ordered


def summarizer_path_dirs(command: str) -> list[str]:
    """找出摘要命令所在的目录，让 launchd 精简的 PATH 也能找到它。"""
    dirs: list[str] = []
    resolved = shutil.which(command)
    if resolved:
        dirs.append(str(Path(resolved).parent))
    home_bin = Path.home() / "bin"
    if home_bin.is_dir() and str(home_bin) not in dirs:
        dirs.append(str(home_bin))
    return dirs

def log_dir(database_path: Path) -> Path:
    """日志目录：与库同级的 logs/，定时任务的 stdout 与 stderr 都写在这里。"""
    return database_path.parent / "logs"


def rotate_logs(
    directory: Path, *, max_bytes: int = LOG_MAX_BYTES, keep_bytes: int = LOG_KEEP_BYTES
) -> list[str]:
    """把超限的日志就地截到尾部 keep_bytes，避免无人值守长期运行撑爆磁盘。

    launchd 以追加模式持有文件句柄，截断后仍从新末尾继续写，因此在启动时重写是安全的。
    """
    if not directory.is_dir():
        return []
    rotated: list[str] = []
    for path in sorted(directory.glob("*.log")):
        try:
            if path.stat().st_size <= max_bytes:
                continue
            with path.open("rb") as handle:
                handle.seek(-keep_bytes, os.SEEK_END)
                tail = handle.read()
            with path.open("wb") as handle:
                handle.write(tail)
        except OSError as exc:
            raise ScheduleError(f"轮转日志失败：{path}：{exc}") from exc
        rotated.append(path.name)
    return rotated


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def uid() -> int:
    return os.getuid()


def domain() -> str:
    return f"{DOMAIN_PREFIX}/{uid()}"


def uv_executable() -> str:
    found = shutil.which("uv")
    if not found:
        raise ScheduleError(
            "找不到 uv 可执行文件：定时任务需要通过 uv 启动项目环境，先把 uv 加进 PATH"
        )
    return found


def build_plist(
    *,
    uv_path: str,
    project_root: Path,
    interval_hours: int,
    log_dir: Path,
    extra_path_dirs: Sequence[str] = (),
) -> bytes:
    """生成 LaunchAgent 的 XML。命令等价于 uv run --project <repo> dc-digest run --prune。"""
    if interval_hours < 1:
        raise ScheduleError("interval_hours 必须 >= 1")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(log_dir / LOG_FILE_NAME)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            uv_path,
            "run",
            "--project",
            str(project_root),
            "dc-digest",
            "run",
            "--prune",
        ],
        "WorkingDirectory": str(project_root),
        "StartInterval": interval_hours * 3600,
        "RunAtLoad": False,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": {
            "PATH": ":".join(_path_entries(uv_path, extra_path_dirs)),
        },
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML)


def install(plist_bytes: bytes, *, path: Path) -> Path:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plist_bytes)
    except OSError as exc:
        raise ScheduleError(
            f"无法写入 {path}：{exc}；受限环境禁止写 ~/Library 时，先用 schedule print 导出 plist 再手动复制到该路径"
        ) from exc
    # 已存在时先卸载，保证 install 可重复执行
    subprocess.run(
        ["launchctl", "bootout", f"{domain()}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain(), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ScheduleError(
            f"launchctl bootstrap 失败（{result.returncode}）：{result.stderr.strip()}"
        )
    return path


def uninstall(*, path: Path) -> bool:
    subprocess.run(
        ["launchctl", "bootout", f"{domain()}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if path.exists():
        path.unlink()
        return True
    return False


def status() -> str:
    result = subprocess.run(
        ["launchctl", "print", f"{domain()}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return "未加载（launchctl 里没有这个任务）"
    lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith(("state =", "last exit code =", "runs =", "next fire"))
    ]
    return "；".join(lines) if lines else "已加载"
