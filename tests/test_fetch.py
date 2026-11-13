from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import pytest

from dc_digest.cli import _fetch
from dc_digest.config import (
    AlertsConfig,
    Channel,
    Config,
    DiscordConfig,
    FeishuConfig,
    FetchPolicy,
    HttpConfig,
    MediaConfig,
    StoreConfig,
    SummarizeConfig,
)
from dc_digest.discord_api import DiscordError, DiscordRateLimited, snowflake_from_ms
from dc_digest.pace import Pacer
from dc_digest.store import Store

TZ = "Asia/Shanghai"
START_MS = 1_757_293_200_000  # 2026-09-08 09:00 (Asia/Shanghai)
MINUTE = 60_000


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.value += seconds


def _payload(sequence: int, ts_ms: int) -> dict:
    return {
        "id": str(snowflake_from_ms(ts_ms) + sequence),
        "type": 0,
        "author": {"id": "42", "username": "alice", "global_name": None},
        "timestamp": datetime.fromtimestamp(ts_ms / 1000, UTC).isoformat(),
        "content": f"消息 {sequence}",
        "attachments": [],
    }


def _history(count: int, start_ms: int = START_MS, step_ms: int = MINUTE) -> list[dict]:
    return [_payload(index, start_ms + index * step_ms) for index in range(count)]


class StubClient:
    """按 Discord 的 before 分页语义取页：返回比 before_id 更早的前 limit 条。"""

    def __init__(
        self,
        histories: dict[int, list[dict]],
        *,
        rate_limit_calls: set[int] | None = None,
        retry_after: float = 30.0,
        non_advancing: bool = False,
    ) -> None:
        self.histories = {
            channel_id: sorted(messages, key=lambda item: int(item["id"]), reverse=True)
            for channel_id, messages in histories.items()
        }
        self.calls = 0
        self.rate_limit_calls = rate_limit_calls or set()
        self.retry_after = retry_after
        self.non_advancing = non_advancing

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def messages(self, channel_id: int, *, before_id: int | None = None, limit: int = 50) -> list[dict]:
        self.calls += 1
        if self.calls in self.rate_limit_calls:
            raise DiscordRateLimited(f"/channels/{channel_id}/messages", self.retry_after)
        history = self.histories[channel_id]
        if self.non_advancing:
            return history[:limit]
        if before_id is None:
            return history[:limit]
        return [item for item in history if int(item["id"]) < before_id][:limit]


def _config(*, page_size: int = 50, max_requests: int = 10) -> Config:
    return Config(
        path=Path("/tmp/dc-fetch-test.toml"),
        timezone=TZ,
        database=Path("/tmp/dc-fetch-test.db"),
        output_dir=Path("/tmp/dc-fetch-test"),
        media_dir=Path("/tmp/dc-fetch-test-media"),
        http=HttpConfig(),
        store=StoreConfig(),
        alerts=AlertsConfig(),
        discord=DiscordConfig(),
        media=MediaConfig(enabled=False),
        fetch=FetchPolicy(
            page_size=page_size,
            max_requests_per_run=max_requests,
            max_messages_per_run=10_000,
            max_minutes_per_run=30.0,
            min_page_delay=1.0,
            max_page_delay=2.0,
            pause_every_pages=5,
            pause_min=3.0,
            pause_max=4.0,
            channel_cooldown_min=5.0,
            channel_cooldown_max=6.0,
        ),
        summarize=SummarizeConfig(),
        feishu=FeishuConfig(enabled=False),
        channels=(),
    )


def _pacer(config: Config) -> tuple[Pacer, FakeClock]:
    fake = FakeClock()
    return Pacer(config.fetch, sleeper=fake.sleep, clock=fake.clock), fake


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "messages.db")


def _window() -> tuple[int, int]:
    return START_MS, START_MS + 120 * MINUTE


def test_fetch_pages_through_window(tmp_path: Path) -> None:
    config = _config()
    pacer, fake = _pacer(config)
    from_ms, to_ms = _window()
    client = StubClient({1: _history(120)})
    with _store(tmp_path) as store:
        outcome = _fetch(
            config,
            store,
            [Channel(channel_id=1, name="#a")],
            from_ms,
            to_ms,
            client_factory=lambda: client,
            pacer=pacer,
        )
        rows = store.messages_in_window([1], from_ms, to_ms)
        state = store.state(1)
    assert outcome.requests == 3
    assert outcome.messages == 120
    assert len(rows) == 120
    assert outcome.incomplete == []
    assert state is not None and state.complete is True
    assert fake.sleeps  # 节奏控制确实产生了等待


def test_fetch_stops_at_budget_and_resumes(tmp_path: Path) -> None:
    config = _config(max_requests=2)
    from_ms, to_ms = _window()
    client = StubClient({1: _history(120)})
    with _store(tmp_path) as store:
        pacer, _ = _pacer(config)
        first = _fetch(
            config,
            store,
            [Channel(channel_id=1, name="#a")],
            from_ms,
            to_ms,
            client_factory=lambda: client,
            pacer=pacer,
        )
        state_after_budget = store.state(1)
        pacer2, _ = _pacer(config)
        second = _fetch(
            config,
            store,
            [Channel(channel_id=1, name="#a")],
            from_ms,
            to_ms,
            client_factory=lambda: client,
            pacer=pacer2,
        )
        rows = store.messages_in_window([1], from_ms, to_ms)
        state = store.state(1)
    assert first.incomplete == ["#a"]
    assert first.stop_reason is not None
    assert state_after_budget is not None and state_after_budget.complete is False
    assert second.incomplete == []
    assert len(rows) == 120
    assert state is not None and state.complete is True


def test_fetch_stops_on_long_rate_limit(tmp_path: Path) -> None:
    config = _config()
    from_ms, to_ms = _window()
    client = StubClient({1: _history(10)}, rate_limit_calls={1}, retry_after=120.0)
    with _store(tmp_path) as store:
        pacer, _ = _pacer(config)
        outcome = _fetch(
            config,
            store,
            [Channel(channel_id=1, name="#a")],
            from_ms,
            to_ms,
            client_factory=lambda: client,
            pacer=pacer,
        )
    assert outcome.incomplete == ["#a"]
    assert outcome.stop_reason is not None and "退避" in outcome.stop_reason


def test_fetch_waits_out_short_rate_limit(tmp_path: Path) -> None:
    config = _config(page_size=10)
    from_ms, to_ms = _window()
    client = StubClient({1: _history(20)}, rate_limit_calls={2}, retry_after=2.0)
    with _store(tmp_path) as store:
        pacer, _ = _pacer(config)
        outcome = _fetch(
            config,
            store,
            [Channel(channel_id=1, name="#a")],
            from_ms,
            to_ms,
            client_factory=lambda: client,
            pacer=pacer,
        )
        rows = store.messages_in_window([1], from_ms, to_ms)
    assert outcome.incomplete == []
    assert len(rows) == 20
    assert "rate-limit" in [label for _, label, _ in pacer.timeline]


def test_non_advancing_pagination_raises(tmp_path: Path) -> None:
    config = _config(page_size=10)
    from_ms, to_ms = _window()
    client = StubClient({1: _history(30)}, non_advancing=True)
    with _store(tmp_path) as store:
        pacer, _ = _pacer(config)
        with pytest.raises(DiscordError):
            _fetch(
                config,
                store,
                [Channel(channel_id=1, name="#a")],
                from_ms,
                to_ms,
                client_factory=lambda: client,
                pacer=pacer,
            )


def test_second_channel_marked_incomplete_after_stop(tmp_path: Path) -> None:
    config = _config(max_requests=1)
    from_ms, to_ms = _window()
    client = StubClient({1: _history(120), 2: _history(30)})
    with _store(tmp_path) as store:
        pacer, _fake = _pacer(config)
        outcome = _fetch(
            config,
            store,
            [Channel(channel_id=1, name="#a"), Channel(channel_id=2, name="#b")],
            from_ms,
            to_ms,
            client_factory=lambda: client,
            pacer=pacer,
        )
        assert store.state(2) is None
    assert outcome.incomplete == ["#a", "#b"]


def test_channels_are_separated_by_cooldown(tmp_path: Path) -> None:
    config = _config()
    from_ms, to_ms = _window()
    client = StubClient({1: _history(10), 2: _history(10)})
    with _store(tmp_path) as store:
        pacer, _ = _pacer(config)
        outcome = _fetch(
            config,
            store,
            [Channel(channel_id=1, name="#a"), Channel(channel_id=2, name="#b")],
            from_ms,
            to_ms,
            client_factory=lambda: client,
            pacer=pacer,
        )
    assert outcome.incomplete == []
    assert "channel-cooldown" in [label for _, label, _ in pacer.timeline]


def test_window_start_is_respected(tmp_path: Path) -> None:
    config = _config(page_size=10)
    start = START_MS
    to_ms = start + 60 * MINUTE
    client = StubClient({1: _history(200)})
    with _store(tmp_path) as store:
        pacer, _ = _pacer(config)
        outcome = _fetch(
            config,
            store,
            [Channel(channel_id=1, name="#a")],
            start,
            to_ms,
            client_factory=lambda: client,
            pacer=pacer,
        )
        state = store.state(1)
    assert outcome.incomplete == []
    assert state is not None and state.complete is True


def test_failed_fetch_records_error_run(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import replace

    from dc_digest import cli

    config = replace(_config(), database=tmp_path / "messages.db")
    args = cli.build_parser().parse_args(["fetch", "--channel", "1"])

    def boom(*_args, **_kwargs):
        raise DiscordError("token 无效或已过期（401）", 401)

    monkeypatch.setattr(cli, "_fetch", boom)
    monkeypatch.setattr(cli, "_notify_failure", lambda *a, **k: None)
    with pytest.raises(DiscordError):
        cli.cmd_fetch(config, args)
    with Store(config.database) as store:
        row = store._conn.execute("SELECT status, note FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "error"
    assert "401" in row["note"]


def test_resolve_window_clamps_long_span_to_48h() -> None:
    from types import SimpleNamespace

    from dc_digest import cli

    store = SimpleNamespace(last_finished_run_to_ms=lambda: None)
    args = cli.build_parser().parse_args(["fetch", "--from=-7d", "--to=now"])
    from_ms, to_ms = cli._resolve_window(args, _config(), store)
    assert to_ms - from_ms == 48 * 3_600_000


def test_resolve_window_keeps_short_span() -> None:
    from types import SimpleNamespace

    from dc_digest import cli

    store = SimpleNamespace(last_finished_run_to_ms=lambda: None)
    args = cli.build_parser().parse_args(["fetch", "--from=-3h", "--to=now"])
    from_ms, to_ms = cli._resolve_window(args, _config(), store)
    assert to_ms - from_ms == 3 * 3_600_000


def test_resolve_window_without_clamp_keeps_history_for_digest() -> None:
    from types import SimpleNamespace

    from dc_digest import cli

    store = SimpleNamespace(last_finished_run_to_ms=lambda: None)
    args = cli.build_parser().parse_args(["digest", "--from=-7d", "--to=now"])
    from_ms, to_ms = cli._resolve_window(args, _config(), store, clamp=False)
    assert to_ms - from_ms == 7 * 86_400_000


def test_brief_window_follows_last_brief_not_last_fetch() -> None:
    """摘要失败时抓取照常推进，但简报窗口要回到上次落盘的位置补缺口。"""
    from types import SimpleNamespace

    from dc_digest import cli

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    last_fetch = now_ms - 6 * 3_600_000
    last_brief = now_ms - 9 * 3_600_000  # 比抓取落后，说明有三小时没出简报
    store = SimpleNamespace(
        last_briefed_to_ms=lambda: last_brief,
        last_finished_run_to_ms=lambda: last_fetch,
    )
    args = cli.build_parser().parse_args(["run"])
    fetch_from, _ = cli._resolve_window(args, _config(), store)
    brief_from, brief_to = cli._resolve_window(args, _config(), store, kind="brief", clamp=False)
    assert fetch_from == last_fetch
    assert brief_from == last_brief
    assert brief_to > brief_from


def test_brief_window_falls_back_to_last_fetch() -> None:
    from types import SimpleNamespace

    from dc_digest import cli

    last_fetch = int(datetime.now(UTC).timestamp() * 1000) - 3 * 3_600_000
    store = SimpleNamespace(
        last_briefed_to_ms=lambda: None,
        last_finished_run_to_ms=lambda: last_fetch,
    )
    args = cli.build_parser().parse_args(["run"])
    brief_from, _ = cli._resolve_window(args, _config(), store, kind="brief")
    assert brief_from == last_fetch


def test_run_keeps_fetch_status_and_notes_brief_failure(tmp_path: Path, monkeypatch) -> None:
    """摘要失败：抓取记账照常 ok，简报台账不推进，原因写进 note。"""
    from dataclasses import replace

    from dc_digest import cli
    from dc_digest.summarize import SummarizeError

    config = replace(_config(), database=tmp_path / "messages.db")
    args = cli.build_parser().parse_args(["run", "--no-push", "--channel", "1"])
    fetched = cli.FetchOutcome(requests=1, messages=3, incomplete=[], stop_reason=None)
    monkeypatch.setattr(cli, "_fetch", lambda *a, **k: fetched)
    monkeypatch.setattr(cli, "_notify_failure", lambda *a, **k: None)
    monkeypatch.setattr(
        cli, "_digest", lambda *a, **k: (_ for _ in ()).throw(SummarizeError("spi 退出码 1"))
    )
    with pytest.raises(SummarizeError):
        cli.cmd_run(config, args)

    with Store(config.database) as store:
        run = store._conn.execute("SELECT status, note FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        briefs = store._conn.execute("SELECT COUNT(*) AS n FROM briefs").fetchone()["n"]
    assert run["status"] == "ok"
    assert "简报未完成" in run["note"]
    assert briefs == 0


def test_resolve_window_after_brief_gap_covers_missing_range(tmp_path: Path) -> None:
    """上一次简报没落盘时，下一次简报窗口要回到旧位置，而不是从抓取终点开始。"""
    from dataclasses import replace
    from datetime import UTC, datetime

    from dc_digest import cli

    config = replace(_config(), database=tmp_path / "messages.db")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    fetch_to = now_ms - 6 * 3_600_000
    with Store(config.database) as store:
        run = store.start_run(fetch_to - 3_600_000, fetch_to)
        store.finish_run(run.id, status="ok", requests=1, messages=10)
        args = cli.build_parser().parse_args(["run"])
        brief_from, brief_to = cli._resolve_window(args, config, store, kind="brief")
    assert brief_from == fetch_to  # 没有更早的简报时，接到上次抓取终点
    assert brief_to > brief_from
