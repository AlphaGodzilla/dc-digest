from pathlib import Path

import pytest

from dc_digest.config import MediaConfig
from dc_digest.discord_api import AttachmentTooLarge, DiscordError, DiscordRateLimited
from dc_digest.media import (
    MediaDownloader,
    collect_media,
    is_image,
    is_trusted_media_url,
    sanitize_filename,
)
from dc_digest.pace import Pacer
from dc_digest.store import MediaRecord, Message, Store


class StubClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.value += seconds


class StubMediaClient:
    """按 URL 返回预置内容；可指定哪些 URL 触发异常。"""

    def __init__(
        self,
        payloads: dict[str, tuple[bytes, str]],
        *,
        too_large: set[str] | None = None,
        missing: set[str] | None = None,
        rate_limited: set[str] | None = None,
    ) -> None:
        self.payloads = payloads
        self.too_large = too_large or set()
        self.missing = missing or set()
        self.rate_limited = rate_limited or set()
        self.requested: list[str] = []

    def download(self, url: str, *, max_bytes: int) -> tuple[bytes, str]:
        self.requested.append(url)
        if url in self.rate_limited:
            raise DiscordRateLimited(url, 30.0)
        if url in self.too_large:
            raise AttachmentTooLarge(max_bytes)
        if url in self.missing:
            raise DiscordError(f"附件已失效（404）: {url}", 404)
        return self.payloads[url]


def _pacer(**overrides: float) -> tuple[Pacer, StubClock]:
    from dc_digest.config import FetchPolicy

    clock = StubClock()
    policy = FetchPolicy(
        min_page_delay=1.0,
        max_page_delay=2.0,
        pause_every_pages=100,
        pause_min=1.0,
        pause_max=2.0,
    )
    pacer = Pacer(policy, sleeper=clock.sleep, clock=clock.clock)
    pacer.begin()
    return pacer, clock


def _message(message_id: int, ts_ms: int, channel_id: int = 1) -> Message:
    return Message(
        id=message_id,
        channel_id=channel_id,
        author_id=1,
        author="alice",
        ts_ms=ts_ms,
        content="看看截图",
    )


def _seed(store: Store, records: list[MediaRecord], ts_ms: int = 1_000) -> None:
    store.upsert([_message(records[0].message_id, ts_ms)])
    store.upsert_media(records)


def test_is_image_by_content_type_and_suffix() -> None:
    assert is_image("image/png", "x")
    assert is_image(None, "p.png")
    assert is_image(None, "p.JPEG")
    assert not is_image("application/pdf", "doc.pdf")
    assert not is_image(None, "readme.txt")


ALLOWED = ("discordapp.com", "discordapp.net", "discord.com")


def test_collect_media_classifies_cdn_images_and_external_links() -> None:
    payload = {
        "attachments": [
            {
                "url": "https://cdn.discordapp.com/a.png",
                "filename": "a.png",
                "content_type": "image/png",
                "size": 100,
            },
            {
                "url": "https://evil.example.com/b.png",
                "filename": "b.png",
                "content_type": "image/png",
                "size": 200,
            },
            {
                "url": "https://cdn.discordapp.com/c.pdf",
                "filename": "c.pdf",
                "content_type": "application/pdf",
                "size": 300,
            },
        ],
        "embeds": [
            {"image": {"url": "https://media.discordapp.net/embed.png"}},
            {"image": {"url": "https://tracker.example.net/pixel.png"}},
        ],
    }
    records = collect_media(10, payload, ALLOWED)
    assert [record.ordinal for record in records] == [0, 1, 2, 3, 4]
    assert [record.kind for record in records] == [
        "image",
        "external",
        "other",
        "image",
        "external",
    ]


def test_is_trusted_media_url_rules() -> None:
    assert is_trusted_media_url("https://cdn.discordapp.com/x.png", ALLOWED)
    assert is_trusted_media_url("https://media.discordapp.net/x.png", ALLOWED)
    assert not is_trusted_media_url("https://discordapp.com.evil.com/x.png", ALLOWED)
    assert not is_trusted_media_url("https://evil.com/x.png", ALLOWED)
    assert not is_trusted_media_url("not-a-url", ALLOWED)


def test_sanitize_filename_strips_unsafe_chars() -> None:
    assert sanitize_filename("截图 2026/11:13.png") == "_2026_11_13.png"
    assert sanitize_filename("") == "file"


def test_sync_downloads_and_marks_ok(tmp_path: Path) -> None:
    pacer, clock = _pacer()
    store = Store(tmp_path / "messages.db")
    _seed(
        store,
        [
            MediaRecord(10, 0, "image", "https://cdn/a.png", "a.png", "image/png", 5),
            MediaRecord(10, 1, "image", "https://cdn/b.webp", "b.webp", "image/webp", 5),
        ],
    )
    client = StubMediaClient(
        {
            "https://cdn/a.png": (b"PNGDATA", "image/png"),
            "https://cdn/b.webp": (b"WEBPDATA", "image/webp"),
        }
    )
    downloader = MediaDownloader(client, MediaConfig(), media_dir=tmp_path / "media", pacer=pacer)
    outcome = downloader.sync(store, [1], 0, 2_000)

    assert outcome.downloaded == 2
    assert outcome.failed == 0
    images = store.window_images([1], 0, 2_000)
    assert [Path(image.local_path).name for image in images] == ["10-0-a.png", "10-1-b.webp"]
    assert all(Path(image.local_path).exists() for image in images)
    assert clock.sleeps  # 下载之间有节流


def test_sync_skips_oversized_and_marks_failed_on_missing(tmp_path: Path) -> None:
    pacer, _ = _pacer()
    store = Store(tmp_path / "messages.db")
    _seed(
        store,
        [
            MediaRecord(10, 0, "image", "https://cdn/big.png", "big.png", "image/png", 999),
            MediaRecord(10, 1, "image", "https://cdn/gone.png", "gone.png", "image/png", 10),
        ],
    )
    client = StubMediaClient({}, too_large={"https://cdn/big.png"}, missing={"https://cdn/gone.png"})
    downloader = MediaDownloader(client, MediaConfig(), media_dir=tmp_path / "media", pacer=pacer)
    outcome = downloader.sync(store, [1], 0, 2_000)

    assert outcome.skipped == 1
    assert outcome.failed == 1
    assert store.media_counts([1], 0, 2_000) == {"failed": 1, "skipped": 1}
    assert store.window_images([1], 0, 2_000) == []


def test_sync_stops_on_rate_limit(tmp_path: Path) -> None:
    pacer, _ = _pacer()
    store = Store(tmp_path / "messages.db")
    _seed(
        store,
        [
            MediaRecord(10, 0, "image", "https://cdn/a.png", "a.png", "image/png", 5),
            MediaRecord(10, 1, "image", "https://cdn/b.png", "b.png", "image/png", 5),
        ],
    )
    client = StubMediaClient({}, rate_limited={"https://cdn/a.png"})
    downloader = MediaDownloader(client, MediaConfig(), media_dir=tmp_path / "media", pacer=pacer)
    outcome = downloader.sync(store, [1], 0, 2_000)

    assert outcome.downloaded == 0
    assert outcome.stop_reason is not None
    assert store.media_counts([1], 0, 2_000) == {"pending": 2}


def test_sync_respects_file_budget(tmp_path: Path) -> None:
    pacer, _ = _pacer()
    store = Store(tmp_path / "messages.db")
    records = [
        MediaRecord(10, index, "image", f"https://cdn/{index}.png", f"{index}.png", "image/png", 5)
        for index in range(3)
    ]
    _seed(store, records)
    client = StubMediaClient({record.url: (b"DATA", "image/png") for record in records})
    downloader = MediaDownloader(client, MediaConfig(max_files_per_run=2), media_dir=tmp_path / "media", pacer=pacer)
    outcome = downloader.sync(store, [1], 0, 2_000)

    assert outcome.downloaded == 2
    assert outcome.stop_reason is not None
    assert len(client.requested) == 2
    assert store.media_counts([1], 0, 2_000) == {"ok": 2, "pending": 1}


def test_sync_raises_on_permission_error(tmp_path: Path) -> None:
    pacer, _ = _pacer()
    store = Store(tmp_path / "messages.db")
    _seed(store, [MediaRecord(10, 0, "image", "https://cdn/a.png", "a.png", "image/png", 5)])

    class ForbiddenClient(StubMediaClient):
        def download(self, url: str, *, max_bytes: int) -> tuple[bytes, str]:
            raise DiscordError("无权下载附件（403）", 403)

    downloader = MediaDownloader(
        ForbiddenClient({}), MediaConfig(), media_dir=tmp_path / "media", pacer=pacer
    )
    with pytest.raises(DiscordError):
        downloader.sync(store, [1], 0, 2_000)


def test_reset_missing_cache_marks_pending(tmp_path: Path) -> None:
    from dc_digest.media import reset_missing_cache

    pacer, _ = _pacer()
    store = Store(tmp_path / "messages.db")
    _seed(store, [MediaRecord(10, 0, "image", "https://cdn/a.png", "a.png", "image/png", 5)])
    gone = tmp_path / "cache" / "10-0-a.png"
    store.mark_media(10, 0, status="ok", local_path=str(gone))
    assert store.media_counts([1], 0, 2_000) == {"ok": 1}

    assert reset_missing_cache(store, [1], 0, 2_000) == 1
    assert store.media_counts([1], 0, 2_000) == {"pending": 1}

    # 重新下载后同一 URL 仍可复用
    client = StubMediaClient({"https://cdn/a.png": (b"DATA", "image/png")})
    downloader = MediaDownloader(client, MediaConfig(), media_dir=gone.parent, pacer=pacer)
    outcome = downloader.sync(store, [1], 0, 2_000)
    assert outcome.downloaded == 1
    cached = store.window_images([1], 0, 2_000)
    assert len(cached) == 1
    assert Path(cached[0].local_path).exists()


def test_external_links_are_never_downloaded(tmp_path: Path) -> None:
    pacer, _ = _pacer()
    store = Store(tmp_path / "messages.db")
    _seed(
        store,
        [
            MediaRecord(10, 0, "external", "https://evil.example.com/a.png", "a.png"),
            MediaRecord(10, 1, "image", "https://cdn.discordapp.com/b.png", "b.png"),
        ],
    )
    client = StubMediaClient({"https://cdn.discordapp.com/b.png": (b"DATA", "image/png")})
    downloader = MediaDownloader(client, MediaConfig(), media_dir=tmp_path / "media", pacer=pacer)
    outcome = downloader.sync(store, [1], 0, 2_000)

    assert outcome.downloaded == 1
    assert client.requested == ["https://cdn.discordapp.com/b.png"]
    assert store.window_external_links([1], 0, 2_000) == [
        (10, "https://evil.example.com/a.png")
    ]
