"""把消息渲染成紧凑 transcript，并按单次调用预算分块。

transcript 只保留模型判断所需的信息：时间、昵称、正文、附件名与回复对象。
正文压缩为单行，附件只留文件名，链接原样保留以便简报引用。
窗口内已下载的图片按出现顺序编号，在对应消息行尾标记 `[图片 N]`；
被判为外部链接（非 Discord CDN）的 URL 标记 `[外部链接: …]`，只作文本，从不访问。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .store import Message
from .timewin import zone

WHITESPACE = re.compile(r"\s+")
MAX_LINKS_PER_MESSAGE = 3


@dataclass(frozen=True)
class RenderPolicy:
    max_content_chars: int = 1000
    # Discord 生成的链接预览（如 X 推文）通常已截断，这里再收一道以免挤占预算
    max_preview_chars: int = 300
    include_attachments: bool = True
    include_reply: bool = True


@dataclass(frozen=True)
class Chunk:
    label: str
    text: str
    message_count: int
    image_indices: tuple[int, ...] = ()


def _clean(text: str, limit: int) -> str:
    collapsed = WHITESPACE.sub(" ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _preview_lines(message: Message, limit: int) -> list[str]:
    """把 Discord 已生成的链接预览渲染成一行行文本。

    预览由 Discord 抓取并随消息下发，读它不产生对外请求；正文缺失时预览也会缺，
    因此这里只做展示，不做任何补抓。
    """
    lines: list[str] = []
    for embed in message.embeds:
        head = " · ".join(part for part in (embed.site, embed.author) if part)
        body = _clean(" ".join(part for part in (embed.title, embed.description) if part), limit)
        if not head and not body:
            continue
        label = f"[链接预览 {head}]" if head else "[链接预览]"
        lines.append(f"{label} {body}".rstrip())
    return lines


def _image_suffix(labels: Sequence[int]) -> str:
    if not labels:
        return ""
    return " [图片 " + ", ".join(str(label) for label in labels) + "]"


def _link_suffix(urls: Sequence[str]) -> str:
    seen: list[str] = []
    for url in urls:
        if url not in seen:
            seen.append(url)
    if not seen:
        return ""
    shown = seen[:MAX_LINKS_PER_MESSAGE]
    suffix = " [外部链接: " + " ".join(shown) + "]"
    if len(seen) > len(shown):
        suffix += f"（另有 {len(seen) - len(shown)} 个未列出）"
    return suffix


def render_message(
    message: Message,
    tz_name: str,
    policy: RenderPolicy,
    image_labels: Sequence[int] = (),
    external_links: Sequence[str] = (),
) -> str:
    moment = datetime.fromtimestamp(message.ts_ms / 1000, zone(tz_name))
    parts = [f"[{moment:%H:%M}] {message.author}:"]
    if policy.include_reply and message.reply_author:
        parts.append(f"(回复 {message.reply_author})")
    body = _clean(message.content, policy.max_content_chars)
    if body:
        parts.append(body)
    if policy.include_attachments and message.attachments:
        parts.append("[附件: " + ", ".join(message.attachments) + "]")
    line = " ".join(parts) + _image_suffix(image_labels) + _link_suffix(external_links)
    previews = _preview_lines(message, policy.max_preview_chars)
    if not previews:
        return line
    return "\n".join([line, *previews])


def render_transcript(
    messages: Sequence[Message],
    tz_name: str,
    policy: RenderPolicy,
    images_by_message: Mapping[int, Sequence[int]] | None = None,
    links_by_message: Mapping[int, Sequence[str]] | None = None,
) -> str:
    labels_by_message = images_by_message or {}
    links = links_by_message or {}
    lines: list[str] = []
    current_day: str | None = None
    for message in messages:
        day = datetime.fromtimestamp(message.ts_ms / 1000, zone(tz_name)).strftime("%Y-%m-%d")
        if day != current_day:
            if lines:
                lines.append("")
            lines.append(f"--- {day} ---")
            current_day = day
        lines.append(
            render_message(
                message,
                tz_name,
                policy,
                labels_by_message.get(message.id, ()),
                links.get(message.id, ()),
            )
        )
    return "\n".join(lines)


def chunk_messages(
    messages: Sequence[Message],
    tz_name: str,
    policy: RenderPolicy,
    max_chars: int,
    images_by_message: Mapping[int, Sequence[int]] | None = None,
    links_by_message: Mapping[int, Sequence[str]] | None = None,
) -> list[Chunk]:
    """按字符预算切块，尽量让每块落在自然的分段处，并带上该块涉及的图片编号。"""
    if not messages:
        return []
    labels_by_message = images_by_message or {}
    links = links_by_message or {}
    chunks: list[Chunk] = []
    buffer: list[Message] = []
    buffer_lines: list[str] = []
    buffer_chars = 0
    buffer_images: list[int] = []
    current_day: str | None = None

    def flush() -> None:
        nonlocal buffer, buffer_lines, buffer_chars, buffer_images, current_day
        if not buffer:
            return
        start = datetime.fromtimestamp(buffer[0].ts_ms / 1000, zone(tz_name))
        end = datetime.fromtimestamp(buffer[-1].ts_ms / 1000, zone(tz_name))
        label = f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}"
        chunks.append(
            Chunk(
                label=label,
                text="\n".join(buffer_lines),
                message_count=len(buffer),
                image_indices=tuple(buffer_images),
            )
        )
        buffer = []
        buffer_lines = []
        buffer_chars = 0
        buffer_images = []
        current_day = None

    for message in messages:
        labels = labels_by_message.get(message.id, ())
        line = render_message(
            message, tz_name, policy, labels, links.get(message.id, ())
        )
        day = datetime.fromtimestamp(message.ts_ms / 1000, zone(tz_name)).strftime("%Y-%m-%d")
        extra = len(line) + 1
        if day != current_day:
            extra += len(day) + 10
        if buffer and buffer_chars + extra > max_chars:
            flush()
        if day != current_day:
            if buffer_lines:
                buffer_lines.append("")
            buffer_lines.append(f"--- {day} ---")
            current_day = day
        buffer.append(message)
        buffer_lines.append(line)
        buffer_chars += extra
        buffer_images.extend(labels)
    flush()
    return chunks
