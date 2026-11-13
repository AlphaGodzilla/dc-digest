"""图片附件的解析与拟人化下载。

抓取阶段只记录附件元数据（status=pending），下载阶段再按节奏把图片落到本地。
摘要阶段直接引用本地文件，因此 spi 不需要联网也不会碰 Discord CDN。

外部链接一律不访问：只有 Discord 自家 CDN 上的附件会被下载，其余域名记为 kind="external"，
仅以文本形式进入 transcript，避免替攻击者发起请求。
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .config import MediaConfig
from .discord_api import (
    AttachmentTooLarge,
    DiscordClient,
    DiscordError,
    DiscordRateLimited,
)
from .pace import Pacer
from .store import MediaRecord, PendingMedia, Store

LOGGER = logging.getLogger("dc_digest")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
UNSAFE_FILENAME = re.compile(r"[^0-9A-Za-z._-]+")


def is_image(content_type: str | None, filename: str) -> bool:
    if content_type and content_type.lower().startswith("image/"):
        return True
    suffix = Path(filename).suffix.lower()
    return suffix in IMAGE_EXTENSIONS


def is_trusted_media_url(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    """只有白名单域名下的附件才允许下载，其余视为外部链接。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in allowed_hosts)


def sanitize_filename(filename: str) -> str:
    cleaned = UNSAFE_FILENAME.sub("_", filename.strip()) or "file"
    return cleaned[-80:]


def collect_media(
    message_id: int, payload: dict, allowed_hosts: tuple[str, ...]
) -> list[MediaRecord]:
    """挑出附件与 embed 内嵌图：白名单域名上的图片可下载，其余记为 external 链接。"""
    records: list[MediaRecord] = []
    ordinal = 0

    for attachment in payload.get("attachments") or []:
        url = attachment.get("url")
        if not url:
            continue
        filename = attachment.get("filename") or "file"
        content_type = attachment.get("content_type")
        if not is_image(content_type, filename):
            kind = "other"
        elif is_trusted_media_url(url, allowed_hosts):
            kind = "image"
        else:
            kind = "external"
        records.append(
            MediaRecord(
                message_id=message_id,
                ordinal=ordinal,
                kind=kind,
                url=url,
                filename=filename,
                content_type=content_type,
                size_bytes=attachment.get("size"),
            )
        )
        ordinal += 1

    for embed in payload.get("embeds") or []:
        for key in ("image", "thumbnail"):
            media = embed.get(key) or {}
            url = media.get("url") or media.get("proxy_url")
            if not url:
                continue
            records.append(
                MediaRecord(
                    message_id=message_id,
                    ordinal=ordinal,
                    kind=(
                        "external"
                        if not is_trusted_media_url(url, allowed_hosts)
                        else ("image" if is_image(None, url) else "other")
                    ),
                    url=url,
                    filename=f"embed-{key}-{ordinal}",
                    content_type=None,
                    size_bytes=None,
                )
            )
            ordinal += 1

    return records


@dataclass
class MediaSyncOutcome:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_total: int = 0
    requests: int = 0
    stop_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"下载 {self.downloaded} 张，跳过 {self.skipped} 张，失败 {self.failed} 张，"
            f"合计 {self.bytes_total / 1024:.0f} KB"
        )


def reset_missing_cache(
    store: Store, channel_ids: list[int], from_ms: int, to_ms: int
) -> int:
    """本地图片只是缓存：文件不在时把对应行打回待下载，等待重新拉取。"""
    images = store.window_images(channel_ids, from_ms, to_ms)
    missing = [image for image in images if not Path(image.local_path).exists()]
    for image in missing:
        store.reset_media(
            image.message_id, image.ordinal, note="本地缓存已清理，待重新下载"
        )
    if missing:
        LOGGER.info("%d 张图片的本地缓存已不在，标记为待重新下载", len(missing))
    return len(missing)


class MediaDownloader:
    def __init__(
        self,
        client: DiscordClient,
        config: MediaConfig,
        *,
        media_dir: Path,
        pacer: Pacer,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._media_dir = media_dir
        self._pacer = pacer
        self._sleeper = sleeper

    def _target_path(self, item: PendingMedia) -> Path:
        name = f"{item.message_id}-{item.ordinal}-{sanitize_filename(item.filename)}"
        return self._media_dir / str(item.channel_id) / name

    def sync(
        self,
        store: Store,
        channel_ids: list[int],
        from_ms: int,
        to_ms: int,
        *,
        max_files: int | None = None,
        max_bytes: int | None = None,
    ) -> MediaSyncOutcome:
        outcome = MediaSyncOutcome()
        pending = store.pending_media(channel_ids, from_ms, to_ms)
        if not pending:
            LOGGER.info("窗口内没有待下载的图片")
            return outcome

        file_budget = max_files if max_files is not None else self._config.max_files_per_run
        byte_budget = max_bytes if max_bytes is not None else self._config.max_bytes_per_run
        LOGGER.info(
            "窗口内待下载图片 %d 张，本轮上限 %d 张 / %.0f MB",
            len(pending),
            file_budget,
            byte_budget / 1_000_000,
        )

        for index, item in enumerate(pending):
            if outcome.requests >= file_budget:
                outcome.stop_reason = f"本轮图片数已达上限 {file_budget}"
                break
            if outcome.bytes_total >= byte_budget:
                outcome.stop_reason = f"本轮图片体积已达上限 {byte_budget // 1_000_000} MB"
                break
            if index > 0:
                self._pacer.wait_between(
                    self._config.min_download_delay,
                    self._config.max_download_delay,
                    "media-delay",
                )
            try:
                content, content_type = self._client.download(
                    item.url, max_bytes=self._config.max_bytes_per_file
                )
            except AttachmentTooLarge:
                store.mark_media(
                    item.message_id,
                    item.ordinal,
                    status="skipped",
                    note=f"超过单文件上限 {self._config.max_bytes_per_file // 1_000_000} MB",
                )
                outcome.skipped += 1
                continue
            except DiscordRateLimited as exc:
                outcome.stop_reason = f"下载限流：{exc}"
                store.mark_media(item.message_id, item.ordinal, status="pending", note=str(exc))
                break
            except DiscordError as exc:
                if exc.status in {401, 403}:
                    raise
                store.mark_media(item.message_id, item.ordinal, status="failed", note=str(exc))
                outcome.failed += 1
                outcome.notes.append(f"{item.filename}: {exc}")
                continue

            outcome.requests += 1
            if not content:
                store.mark_media(item.message_id, item.ordinal, status="skipped", note="空文件")
                outcome.skipped += 1
                continue
            if not content_type:
                content_type = item.content_type or ""
            if not is_image(content_type, item.filename):
                store.mark_media(
                    item.message_id, item.ordinal, status="skipped", note="不是图片"
                )
                outcome.skipped += 1
                continue

            path = self._target_path(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            store.mark_media(
                item.message_id,
                item.ordinal,
                status="ok",
                local_path=str(path),
                sha256=digest,
                size_bytes=len(content),
            )
            outcome.downloaded += 1
            outcome.bytes_total += len(content)
            LOGGER.info(
                "已下载 %s（%d KB）",
                path.name,
                len(content) // 1024,
            )
        return outcome
