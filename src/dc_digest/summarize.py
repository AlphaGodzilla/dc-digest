"""调用 spi 把 transcript 总结成简报。

单块时一次调用给结果；超过单次预算时按块先摘要再合并。
图片按块分配给对应调用，作为 `@path` 参数附在命令行末尾（spi 以 stdin 收 prompt、stdout 出 Markdown）。
子进程失败即抛错，不做降级。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import SummarizeConfig
from .render import Chunk

LOGGER = logging.getLogger("dc_digest")

EMPTY_IMAGES = "（本段没有可用的图片）"


class SummarizeError(RuntimeError):
    """摘要子进程失败或返回空输出。"""


@dataclass(frozen=True)
class ImageInput:
    """一张可用的附图：编号、本地路径与给模型看的一行说明。"""

    index: int
    path: Path
    manifest_line: str


def _read_template(path: Path) -> str:
    if not path.exists():
        raise SummarizeError(f"prompt 模板不存在: {path}")
    return path.read_text(encoding="utf-8")


def _fill(template: str, **values: str) -> str:
    text = template
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _manifest(images: Sequence[ImageInput]) -> str:
    if not images:
        return EMPTY_IMAGES
    return "\n".join(image.manifest_line for image in images)


def _select(images: dict[int, ImageInput], indices: Sequence[int], limit: int) -> list[ImageInput]:
    selected = [images[index] for index in indices if index in images]
    if len(selected) > limit:
        LOGGER.warning("本段图片 %d 张超过单次上限 %d，只保留前 %d 张", len(selected), limit, limit)
        selected = selected[:limit]
    return selected


FALLBACK_BIN_DIRS = ("~/bin", "~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")


def resolve_command(command: str) -> str:


    """把命令解析成可执行路径。

    launchd 给任务的 PATH 只有 uv 目录加几个系统目录，用户在 shell 里能跑的
    spi 常常不在其中；这里除了 PATH 再兜底几个常见的本地 bin 目录。
    """
    if os.sep in command:
        return command
    dirs = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    dirs += [str(Path(entry).expanduser()) for entry in FALLBACK_BIN_DIRS]
    found = shutil.which(command, path=os.pathsep.join(dirs))
    return found or command



def summarizer_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """给摘要子进程补全 PATH。

    spi 是 bash 包装脚本，内部还要 exec pi（在 ~/.local/bin）；只找到 spi
    而不补 PATH，子进程一样会 "pi: not found"。
    """
    env = dict(os.environ if base is None else base)
    entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    for candidate in FALLBACK_BIN_DIRS:
        expanded = str(Path(candidate).expanduser())
        if expanded not in entries:
            entries.append(expanded)
    env["PATH"] = os.pathsep.join(entries)
    return env


def build_argv(config: SummarizeConfig, images: Sequence[ImageInput] = ()) -> tuple[str, ...]:
    """拼出子进程命令行：基础参数在前，图片以 @path 追加。"""
    return (*config.argv, *[f"@{image.path}" for image in images])


def call_summarizer(
    config: SummarizeConfig, prompt: str, images: Sequence[ImageInput] = ()
) -> str:
    argv = build_argv(config, images)
    resolved = resolve_command(argv[0])
    if resolved != argv[0]:
        argv = (resolved, *argv[1:])
    LOGGER.info(
        "调用摘要子进程: %s（输入 %d 字符，附 %d 张图）",
        " ".join(argv),
        len(prompt),
        len(images),
    )
    try:
        result = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            env=summarizer_env(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise SummarizeError(
            f"找不到摘要命令 {config.command!r}：已在 PATH 与 {', '.join(FALLBACK_BIN_DIRS)} 中查找。"
            "定时任务的 PATH 比登录 shell 精简，建议在 config.toml 的 [summarize] command 写成绝对路径，"
            "或重新执行 schedule install 让任务带上完整 PATH"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SummarizeError(
            f"{config.command} 超过 {config.timeout_seconds:.0f}s 未返回"
        ) from exc
    if result.returncode != 0:
        raise SummarizeError(
            f"{config.command} 退出码 {result.returncode}\n{result.stderr.strip()[-1200:]}"
        )
    output = result.stdout.strip()
    if not output:
        raise SummarizeError(f"{config.command} 没有输出\n{result.stderr.strip()[-1200:]}")
    return output


def summarize(
    config: SummarizeConfig,
    *,
    window_text: str,
    channel_text: str,
    total_messages: int,
    note: str,
    chunks: Sequence[Chunk],
    images: dict[int, ImageInput] | None = None,
    max_images_per_call: int = 8,
) -> str:
    if not chunks:
        raise SummarizeError("没有可摘要的内容")
    available = images or {}

    single = len(chunks) == 1 and len(chunks[0].text) <= config.max_chars_per_call
    if single:
        chunk = chunks[0]
        selected = _select(available, chunk.image_indices, max_images_per_call)
        prompt = _fill(
            _read_template(config.digest_prompt),
            range=window_text,
            channels=channel_text,
            messages=str(total_messages),
            note=note,
            images=_manifest(selected),
            transcript=chunk.text,
        )
        return call_summarizer(config, prompt, selected)

    chunk_template = _read_template(config.chunk_prompt)
    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        selected = _select(available, chunk.image_indices, max_images_per_call)
        LOGGER.info(
            "分段摘要 %d/%d（%s，%d 条，%d 张图）",
            index,
            len(chunks),
            chunk.label,
            chunk.message_count,
            len(selected),
        )
        prompt = _fill(
            chunk_template,
            range=chunk.label,
            messages=str(chunk.message_count),
            images=_manifest(selected),
            transcript=chunk.text,
        )
        partial = call_summarizer(config, prompt, selected)
        partials.append(
            f"### 分段 {index}（{chunk.label}，{chunk.message_count} 条）\n{partial}"
        )

    reduce_prompt = _fill(
        _read_template(config.reduce_prompt),
        range=window_text,
        channels=channel_text,
        messages=str(total_messages),
        note=note,
        images=_manifest(list(available.values())),
        partials="\n\n".join(partials),
    )
    return call_summarizer(config, reduce_prompt)
