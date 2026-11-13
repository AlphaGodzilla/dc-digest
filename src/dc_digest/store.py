"""SQLite 仓储：消息、抓取游标与运行记录。

消息按 id 做 upsert，重复抓取不会产生重复行；抓取游标按频道保存，预算用尽时可续传。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    author_id INTEGER,
    author TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    content TEXT NOT NULL,
    attachments TEXT NOT NULL DEFAULT '[]',
    reply_author TEXT,
    edited_ts_ms INTEGER
);
CREATE INDEX IF NOT EXISTS messages_channel_ts ON messages(channel_id, ts_ms);
CREATE TABLE IF NOT EXISTS channel_state (
    channel_id INTEGER PRIMARY KEY,
    window_from_ms INTEGER NOT NULL,
    window_to_ms INTEGER NOT NULL,
    cursor_id INTEGER,
    cursor_ms INTEGER NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    window_from_ms INTEGER NOT NULL,
    window_to_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS message_media (
    message_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT,
    size_bytes INTEGER,
    local_path TEXT,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (message_id, ordinal)
);
CREATE INDEX IF NOT EXISTS message_media_status ON message_media(status);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Message:
    id: int
    channel_id: int
    author_id: int | None
    author: str
    ts_ms: int
    content: str
    attachments: tuple[str, ...] = ()
    reply_author: str | None = None
    edited_ts_ms: int | None = None


@dataclass(frozen=True)
class MediaRecord:
    """附件元数据：抓取阶段入库，下载阶段补齐本地路径。"""

    message_id: int
    ordinal: int
    kind: str
    url: str
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class PendingMedia:
    """待下载的图片：带消息上下文，用于决定落盘路径与限流。"""

    message_id: int
    ordinal: int
    channel_id: int
    url: str
    filename: str
    content_type: str | None
    size_bytes: int | None


@dataclass(frozen=True)
class WindowImage:
    """窗口内已下载成功的图片：供摘要阶段生成编号与附图清单。"""

    message_id: int
    ordinal: int
    channel_id: int
    ts_ms: int
    author: str
    local_path: str
    filename: str


@dataclass(frozen=True)
class ChannelState:
    channel_id: int
    window_from_ms: int
    window_to_ms: int
    cursor_id: int | None
    cursor_ms: int
    complete: bool


@dataclass(frozen=True)
class RunRecord:
    id: int
    window_from_ms: int
    window_to_ms: int
    status: str


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # ---- 消息 ----

    def upsert(self, messages: Iterable[Message]) -> int:
        rows = [
            (
                message.id,
                message.channel_id,
                message.author_id,
                message.author,
                message.ts_ms,
                message.content,
                json.dumps(list(message.attachments), ensure_ascii=False),
                message.reply_author,
                message.edited_ts_ms,
            )
            for message in messages
        ]
        if not rows:
            return 0
        self._conn.executemany(
            """
            INSERT INTO messages (id, channel_id, author_id, author, ts_ms, content, attachments, reply_author, edited_ts_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                attachments = excluded.attachments,
                reply_author = excluded.reply_author,
                edited_ts_ms = excluded.edited_ts_ms
            """,
            rows,
        )
        self._conn.commit()
        return len(rows)

    def messages_in_window(
        self, channel_ids: Sequence[int], from_ms: int, to_ms: int
    ) -> list[Message]:
        placeholders = ",".join("?" for _ in channel_ids)
        cursor = self._conn.execute(
            f"""
            SELECT id, channel_id, author_id, author, ts_ms, content, attachments, reply_author, edited_ts_ms
            FROM messages
            WHERE channel_id IN ({placeholders}) AND ts_ms >= ? AND ts_ms < ?
            ORDER BY ts_ms ASC, id ASC
            """,
            (*channel_ids, from_ms, to_ms),
        )
        return [
            Message(
                id=row["id"],
                channel_id=row["channel_id"],
                author_id=row["author_id"],
                author=row["author"],
                ts_ms=row["ts_ms"],
                content=row["content"],
                attachments=tuple(json.loads(row["attachments"])),
                reply_author=row["reply_author"],
                edited_ts_ms=row["edited_ts_ms"],
            )
            for row in cursor.fetchall()
        ]

    # ---- 附件 ----

    def upsert_media(self, records: Iterable[MediaRecord]) -> int:
        """写入附件元数据；已下载的本地状态不被覆盖。"""
        rows = [
            (
                record.message_id,
                record.ordinal,
                record.kind,
                record.url,
                record.filename,
                record.content_type,
                record.size_bytes,
                "skipped" if record.kind == "external" else "pending",
                "外部链接，仅作文本，不下载" if record.kind == "external" else None,
                _now_iso(),
            )
            for record in records
        ]
        if not rows:
            return 0
        self._conn.executemany(
            """
            INSERT INTO message_media
                (message_id, ordinal, kind, url, filename, content_type, size_bytes, status, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, ordinal) DO UPDATE SET
                kind = excluded.kind,
                url = excluded.url,
                filename = excluded.filename,
                content_type = excluded.content_type,
                size_bytes = excluded.size_bytes,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        self._conn.commit()
        return len(rows)

    def pending_media(
        self, channel_ids: Sequence[int], from_ms: int, to_ms: int
    ) -> list[PendingMedia]:
        placeholders = ",".join("?" for _ in channel_ids)
        cursor = self._conn.execute(
            f"""
            SELECT media.message_id, media.ordinal, media.url, media.filename,
                   media.content_type, media.size_bytes, message.channel_id
            FROM message_media AS media
            JOIN messages AS message ON message.id = media.message_id
            WHERE media.kind = 'image' AND media.status = 'pending'
              AND message.channel_id IN ({placeholders})
              AND message.ts_ms >= ? AND message.ts_ms < ?
            ORDER BY message.ts_ms ASC, media.message_id ASC, media.ordinal ASC
            """,
            (*channel_ids, from_ms, to_ms),
        )
        return [
            PendingMedia(
                message_id=row["message_id"],
                ordinal=row["ordinal"],
                channel_id=row["channel_id"],
                url=row["url"],
                filename=row["filename"],
                content_type=row["content_type"],
                size_bytes=row["size_bytes"],
            )
            for row in cursor.fetchall()
        ]

    def window_images(
        self, channel_ids: Sequence[int], from_ms: int, to_ms: int
    ) -> list[WindowImage]:
        placeholders = ",".join("?" for _ in channel_ids)
        cursor = self._conn.execute(
            f"""
            SELECT media.message_id, media.ordinal, media.local_path, media.filename,
                   message.channel_id, message.ts_ms, message.author
            FROM message_media AS media
            JOIN messages AS message ON message.id = media.message_id
            WHERE media.kind = 'image' AND media.status = 'ok' AND media.local_path IS NOT NULL
              AND message.channel_id IN ({placeholders})
              AND message.ts_ms >= ? AND message.ts_ms < ?
            ORDER BY message.ts_ms ASC, media.message_id ASC, media.ordinal ASC
            """,
            (*channel_ids, from_ms, to_ms),
        )
        return [
            WindowImage(
                message_id=row["message_id"],
                ordinal=row["ordinal"],
                channel_id=row["channel_id"],
                ts_ms=row["ts_ms"],
                author=row["author"],
                local_path=row["local_path"],
                filename=row["filename"],
            )
            for row in cursor.fetchall()
        ]

    def window_external_links(
        self, channel_ids: Sequence[int], from_ms: int, to_ms: int
    ) -> list[tuple[int, str]]:
        """窗口内被判定为外部链接的 URL：只作为文本交给模型，绝不下载。"""
        placeholders = ",".join("?" for _ in channel_ids)
        cursor = self._conn.execute(
            f"""
            SELECT media.message_id, media.url
            FROM message_media AS media
            JOIN messages AS message ON message.id = media.message_id
            WHERE media.kind = 'external'
              AND message.channel_id IN ({placeholders})
              AND message.ts_ms >= ? AND message.ts_ms < ?
            ORDER BY message.ts_ms ASC, media.message_id ASC, media.ordinal ASC
            """,
            (*channel_ids, from_ms, to_ms),
        )
        return [(row["message_id"], row["url"]) for row in cursor.fetchall()]

    def media_counts(
        self, channel_ids: Sequence[int], from_ms: int, to_ms: int
    ) -> dict[str, int]:
        placeholders = ",".join("?" for _ in channel_ids)
        cursor = self._conn.execute(
            f"""
            SELECT media.status AS status, COUNT(*) AS total
            FROM message_media AS media
            JOIN messages AS message ON message.id = media.message_id
            WHERE media.kind = 'image'
              AND message.channel_id IN ({placeholders})
              AND message.ts_ms >= ? AND message.ts_ms < ?
            GROUP BY media.status
            """,
            (*channel_ids, from_ms, to_ms),
        )
        return {row["status"]: int(row["total"]) for row in cursor.fetchall()}

    def reset_media(self, message_id: int, ordinal: int, *, note: str) -> None:
        """把一条附件打回待下载状态：本地文件只是缓存，URL 才是真相源。"""
        self._conn.execute(
            """
            UPDATE message_media
            SET status = 'pending', local_path = NULL, sha256 = NULL, note = ?, updated_at = ?
            WHERE message_id = ? AND ordinal = ?
            """,
            (note, _now_iso(), message_id, ordinal),
        )
        self._conn.commit()

    def reset_media_window(
        self, channel_ids: Sequence[int], from_ms: int, to_ms: int
    ) -> list[str]:
        """清空窗口内附件缓存状态，返回被清掉的本地路径，便于调用方删文件。"""
        placeholders = ",".join("?" for _ in channel_ids)
        cursor = self._conn.execute(
            f"""
            SELECT media.message_id, media.ordinal, media.local_path
            FROM message_media AS media
            JOIN messages AS message ON message.id = media.message_id
            WHERE media.kind = 'image' AND media.local_path IS NOT NULL
              AND message.channel_id IN ({placeholders})
              AND message.ts_ms >= ? AND message.ts_ms < ?
            """,
            (*channel_ids, from_ms, to_ms),
        )
        rows = cursor.fetchall()
        paths = [row["local_path"] for row in rows if row["local_path"]]
        for row in rows:
            self._conn.execute(
                """
                UPDATE message_media
                SET status = 'pending', local_path = NULL, sha256 = NULL, note = ?, updated_at = ?
                WHERE message_id = ? AND ordinal = ?
                """,
                ("缓存已清理", _now_iso(), row["message_id"], row["ordinal"]),
            )
        self._conn.commit()
        return paths

    def mark_media(
        self,
        message_id: int,
        ordinal: int,
        *,
        status: str,
        local_path: str | None = None,
        sha256: str | None = None,
        size_bytes: int | None = None,
        note: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE message_media
            SET status = ?, local_path = ?, sha256 = ?, size_bytes = ?, note = ?, updated_at = ?
            WHERE message_id = ? AND ordinal = ?
            """,
            (
                status,
                local_path,
                sha256,
                size_bytes,
                note,
                _now_iso(),
                message_id,
                ordinal,
            ),
        )
        self._conn.commit()

    # ---- 抓取游标 ----

    def state(self, channel_id: int) -> ChannelState | None:
        row = self._conn.execute(
            "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            return None
        return ChannelState(
            channel_id=row["channel_id"],
            window_from_ms=row["window_from_ms"],
            window_to_ms=row["window_to_ms"],
            cursor_id=row["cursor_id"],
            cursor_ms=row["cursor_ms"],
            complete=bool(row["complete"]),
        )

    def sync_window(self, channel_id: int, from_ms: int, to_ms: int) -> ChannelState:
        """对齐抓取窗口。未抓完且游标仍落在新窗口内时保留游标继续向前走。"""
        existing = self.state(channel_id)
        if (
            existing is not None
            and not existing.complete
            and existing.cursor_id is not None
            and from_ms < existing.cursor_ms <= to_ms
        ):
            state = ChannelState(
                channel_id=channel_id,
                window_from_ms=from_ms,
                window_to_ms=to_ms,
                cursor_id=existing.cursor_id,
                cursor_ms=existing.cursor_ms,
                complete=False,
            )
        else:
            state = ChannelState(
                channel_id=channel_id,
                window_from_ms=from_ms,
                window_to_ms=to_ms,
                cursor_id=None,
                cursor_ms=to_ms,
                complete=False,
            )
        self._write_state(state)
        return state

    def save_progress(self, channel_id: int, cursor_id: int | None, cursor_ms: int, complete: bool) -> None:
        current = self.state(channel_id)
        if current is None:
            raise RuntimeError(f"频道 {channel_id} 尚无抓取窗口，不能保存进度")
        self._write_state(
            ChannelState(
                channel_id=channel_id,
                window_from_ms=current.window_from_ms,
                window_to_ms=current.window_to_ms,
                cursor_id=cursor_id,
                cursor_ms=cursor_ms,
                complete=complete,
            )
        )

    def _write_state(self, state: ChannelState) -> None:
        self._conn.execute(
            """
            INSERT INTO channel_state (channel_id, window_from_ms, window_to_ms, cursor_id, cursor_ms, complete, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                window_from_ms = excluded.window_from_ms,
                window_to_ms = excluded.window_to_ms,
                cursor_id = excluded.cursor_id,
                cursor_ms = excluded.cursor_ms,
                complete = excluded.complete,
                updated_at = excluded.updated_at
            """,
            (
                state.channel_id,
                state.window_from_ms,
                state.window_to_ms,
                state.cursor_id,
                state.cursor_ms,
                1 if state.complete else 0,
                _now_iso(),
            ),
        )
        self._conn.commit()

    # ---- 运行记录 ----

    def start_run(self, from_ms: int, to_ms: int) -> RunRecord:
        cursor = self._conn.execute(
            "INSERT INTO runs (started_at, window_from_ms, window_to_ms, status) VALUES (?, ?, ?, 'running')",
            (_now_iso(), from_ms, to_ms),
        )
        self._conn.commit()
        return RunRecord(id=int(cursor.lastrowid), window_from_ms=from_ms, window_to_ms=to_ms, status="running")

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        requests: int,
        messages: int,
        note: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE runs SET finished_at = ?, status = ?, requests = ?, messages = ?, note = ?
            WHERE id = ?
            """,
            (_now_iso(), status, requests, messages, note, run_id),
        )
        self._conn.commit()

    def last_finished_run_to_ms(self) -> int | None:
        row = self._conn.execute(
            "SELECT window_to_ms FROM runs WHERE status IN ('ok', 'incomplete') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row["window_to_ms"]) if row else None

    # ---- 留存清理 ----

    def prune(
        self,
        retention_days: int,
        *,
        now_ms: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """删除超过留存期的消息与附件记录；dry_run 只统计不删除。"""
        if retention_days < 0:
            raise ValueError("retention_days 不能为负")
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        cutoff = now - retention_days * 86_400_000

        message_rows = self._conn.execute(
            "SELECT COUNT(*) AS total FROM messages WHERE ts_ms < ?", (cutoff,)
        ).fetchone()
        media_rows = self._conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM message_media AS media
            JOIN messages AS message ON message.id = media.message_id
            WHERE message.ts_ms < ?
            """,
            (cutoff,),
        ).fetchone()
        counts = {
            "messages": int(message_rows["total"]),
            "media": int(media_rows["total"]),
            "cutoff_ms": cutoff,
        }
        if dry_run:
            return counts
        self._conn.execute(
            """
            DELETE FROM message_media
            WHERE message_id IN (SELECT id FROM messages WHERE ts_ms < ?)
            """,
            (cutoff,),
        )
        self._conn.execute("DELETE FROM messages WHERE ts_ms < ?", (cutoff,))
        self._conn.commit()
        return counts
