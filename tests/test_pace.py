import pytest

from dc_digest.config import FetchPolicy
from dc_digest.pace import BudgetExhausted, Pacer


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.value += seconds


def _policy(**overrides: object) -> FetchPolicy:
    base: dict[str, object] = {
        "page_size": 50,
        "max_requests_per_run": 3,
        "max_messages_per_run": 200,
        "max_minutes_per_run": 10.0,
        "min_page_delay": 1.0,
        "max_page_delay": 2.0,
        "pause_every_pages": 2,
        "pause_min": 3.0,
        "pause_max": 4.0,
        "channel_cooldown_min": 5.0,
        "channel_cooldown_max": 6.0,
    }
    base.update(overrides)
    return FetchPolicy(**base)


def _pacer(**overrides: object) -> tuple[Pacer, FakeClock]:
    fake = FakeClock()
    pacer = Pacer(_policy(**overrides), sleeper=fake.sleep, clock=fake.clock)
    pacer.begin()
    return pacer, fake


def test_request_budget_stops_run() -> None:
    pacer, _ = _pacer()
    for _ in range(3):
        pacer.require_capacity()
        pacer.after_page(10)
    with pytest.raises(BudgetExhausted):
        pacer.require_capacity()


def test_message_budget_stops_run() -> None:
    pacer, _ = _pacer(max_requests_per_run=10, max_messages_per_run=100)
    pacer.require_capacity()
    pacer.after_page(100)
    with pytest.raises(BudgetExhausted):
        pacer.require_capacity()


def test_wall_clock_budget_stops_run() -> None:
    pacer, fake = _pacer(max_requests_per_run=100, max_minutes_per_run=1.0)
    fake.value = 59.5
    with pytest.raises(BudgetExhausted):
        pacer.require_capacity()


def test_page_delay_and_long_pause_cadence() -> None:
    pacer, fake = _pacer()
    pacer.after_page(10)
    assert 1.0 <= fake.sleeps[-1] <= 2.0
    pacer.after_page(10)
    assert 3.0 <= fake.sleeps[-1] <= 4.0
    assert [label for _, label, _ in pacer.timeline] == ["page-delay", "long-pause"]


def test_first_channel_has_no_cooldown() -> None:
    pacer, fake = _pacer()
    pacer.before_channel()
    assert fake.sleeps == []
    pacer.before_channel()
    assert 5.0 <= fake.sleeps[-1] <= 6.0


def test_delays_stay_within_configured_range() -> None:
    pacer, fake = _pacer(pause_every_pages=50)
    for _ in range(10):
        pacer.after_page(1)
    assert all(1.0 <= value <= 2.0 for value in fake.sleeps)
