from pathlib import Path

import pytest

from dc_digest.store import MediaRecord, Message, Store


def _message(message_id: int, ts_ms: int, channel_id: int = 1, content: str = "hi") -> Message:
    return Message(
        id=message_id,
        channel_id=channel_id,
        author_id=42,
        author="alice",
        ts_ms=ts_ms,
        content=content,
        attachments=(),
    )


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "messages.db")


def test_upsert_is_idempotent(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        message = _message(10, 1_000)
        store.upsert([message])
        store.upsert([message])
        rows = store.messages_in_window([1], 0, 2_000)
    assert len(rows) == 1


def test_window_query_filters_and_orders(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.upsert([_message(3, 300), _message(1, 100), _message(2, 200), _message(4, 400)])
        rows = store.messages_in_window([1], 150, 400)
    assert [row.id for row in rows] == [2, 3]


def test_sync_window_resets_cursor_on_new_window(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.sync_window(1, 0, 1_000)
        store.save_progress(1, cursor_id=500, cursor_ms=500, complete=False)
        state = store.sync_window(1, 1_200, 2_000)
    assert state.cursor_id is None
    assert state.cursor_ms == 2_000


def test_sync_window_keeps_cursor_when_window_extends(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.sync_window(1, 0, 1_000)
        store.save_progress(1, cursor_id=500, cursor_ms=500, complete=False)
        state = store.sync_window(1, 0, 1_500)
    assert state.cursor_id == 500
    assert state.cursor_ms == 500
    assert state.window_to_ms == 1_500


def test_completed_window_is_not_reused(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.sync_window(1, 0, 1_000)
        store.save_progress(1, cursor_id=500, cursor_ms=500, complete=True)
        state = store.sync_window(1, 0, 1_200)
    assert state.cursor_id is None
    assert state.complete is False


def test_run_records(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        run = store.start_run(0, 1_000)
        store.finish_run(run.id, status="ok", requests=3, messages=120)
        assert store.last_finished_run_to_ms() == 1_000


def test_save_progress_requires_window(tmp_path: Path) -> None:
    with _store(tmp_path) as store, pytest.raises(RuntimeError):
        store.save_progress(99, cursor_id=None, cursor_ms=0, complete=False)


def test_reset_media_marks_pending_and_returns_paths(tmp_path: Path) -> None:
    media = MediaRecord(
        message_id=10, ordinal=0, kind="image", url="https://cdn/a.png", filename="a.png"
    )
    with _store(tmp_path) as store:
        store.upsert([_message(10, 500)])
        store.upsert_media([media])
        store.mark_media(10, 0, status="ok", local_path="/tmp/dc-digest-media/1/10-0-a.png")
        store.upsert_media([media])  # 重复入库不得覆盖已下载状态
        assert store.media_counts([1], 0, 1_000) == {"ok": 1}
        paths = store.reset_media_window([1], 0, 1_000)
        assert paths == ["/tmp/dc-digest-media/1/10-0-a.png"]
        assert store.media_counts([1], 0, 1_000) == {"pending": 1}
        assert store.window_images([1], 0, 1_000) == []


def test_window_external_links(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.upsert([_message(10, 500)])
        store.upsert_media(
            [
                MediaRecord(
                    message_id=10,
                    ordinal=0,
                    kind="external",
                    url="https://evil.example.com/a.png",
                    filename="a.png",
                )
            ]
        )
        assert store.window_external_links([1], 0, 1_000) == [
            (10, "https://evil.example.com/a.png")
        ]


def test_prune_removes_old_messages_and_media(tmp_path: Path) -> None:
    now = 1_000 * 86_400_000
    old = now - 200 * 86_400_000
    fresh = now - 10 * 86_400_000
    with _store(tmp_path) as store:
        store.upsert([_message(1, old), _message(2, fresh)])
        store.upsert_media(
            [
                MediaRecord(
                    message_id=1,
                    ordinal=0,
                    kind="image",
                    url="https://cdn.discordapp.com/old.png",
                    filename="old.png",
                )
            ]
        )
        dry = store.prune(180, now_ms=now, dry_run=True)
        assert dry["messages"] == 1 and dry["media"] == 1
        assert len(store.messages_in_window([1], 0, now)) == 2

        real = store.prune(180, now_ms=now)
        assert real["messages"] == 1 and real["media"] == 1
        remaining = store.messages_in_window([1], 0, now)
    assert [message.id for message in remaining] == [2]


def test_external_media_goes_straight_to_skipped(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.upsert([_message(10, 500)])
        store.upsert_media(
            [
                MediaRecord(
                    message_id=10,
                    ordinal=0,
                    kind="external",
                    url="https://evil.example.com/a.png",
                    filename="a.png",
                ),
                MediaRecord(
                    message_id=10,
                    ordinal=1,
                    kind="image",
                    url="https://cdn.discordapp.com/b.png",
                    filename="b.png",
                ),
            ]
        )
        assert store.media_counts([1], 0, 1_000) == {"pending": 1}
        assert store.window_external_links([1], 0, 1_000) == [
            (10, "https://evil.example.com/a.png")
        ]


def test_brief_ledger_tracks_written_briefs(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        assert store.last_briefed_to_ms() is None
        first = store.record_brief(1_000, 2_000, tmp_path / "a.md")
        assert store.last_briefed_to_ms() == 2_000
        store.record_brief(2_001, 3_000, None)  # 没写出文件，不算完成
        assert store.last_briefed_to_ms() == 2_000
        store.record_brief(2_001, 3_000, tmp_path / "b.md")
        assert store.last_briefed_to_ms() == 3_000
        store.mark_brief_pushed(first)


def test_brief_ledger_keeps_newest_end(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.record_brief(5_000, 6_000, tmp_path / "late.md")
        store.record_brief(1_000, 2_000, tmp_path / "backfill.md")  # 事后补一段旧窗口
        assert store.last_briefed_to_ms() == 6_000


def test_append_run_note_keeps_original_reason(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        run = store.start_run(0, 100)
        store.finish_run(run.id, status="ok", requests=1, messages=5, note="")
        store.append_run_note(run.id, "简报未完成：spi 退出码 1")
        store.append_run_note(run.id, "已通过飞书提醒")
        row = store._conn.execute("SELECT note FROM runs WHERE id = ?", (run.id,)).fetchone()
    assert row["note"] == "简报未完成：spi 退出码 1 | 已通过飞书提醒"


def test_last_finished_run_uses_furthest_window(tmp_path: Path) -> None:
    """补抓旧窗口会新增在最后一行，但不能把下次起点拽回过去。"""
    with _store(tmp_path) as store:
        newer = store.start_run(5_000, 9_000)
        store.finish_run(newer.id, status="ok", requests=1, messages=1)
        backfill = store.start_run(1_000, 2_000)  # 事后补一段更早的窗口
        store.finish_run(backfill.id, status="ok", requests=1, messages=1)
        assert store.last_finished_run_to_ms() == 9_000


def test_last_finished_run_ignores_failed_runs(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        ok = store.start_run(1_000, 2_000)
        store.finish_run(ok.id, status="ok", requests=1, messages=1)
        failed = store.start_run(3_000, 4_000)
        store.finish_run(failed.id, status="error", requests=0, messages=0)
        assert store.last_finished_run_to_ms() == 2_000
