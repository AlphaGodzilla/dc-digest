"""拟人化取数节奏。

真实客户端只会慢慢往上翻页，因此这里统一控制页间间隔、长停顿、跨频道冷却与单次预算，
并对 429 采取「结束本轮、下次续传」而不是立即重试。所有等待都计入墙钟预算。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import FetchPolicy


class BudgetExhausted(RuntimeError):
    """本轮抓取预算用尽，调用方应保存游标并结束。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Pacer:
    policy: FetchPolicy
    rng: random.Random = field(default_factory=random.Random)
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    requests: int = 0
    messages: int = 0
    pages_since_pause: int = 0
    channels_visited: int = 0
    slept_seconds: float = 0.0
    started_at: float | None = None
    timeline: list[tuple[float, str, float]] = field(default_factory=list)

    def begin(self) -> None:
        self.started_at = self.clock()

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return self.clock() - self.started_at

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.policy.max_minutes_per_run * 60.0 - self.elapsed_seconds)

    def require_capacity(self) -> None:
        """发起下一次请求前检查预算，触顶即抛 BudgetExhausted。"""
        if self.requests >= self.policy.max_requests_per_run:
            raise BudgetExhausted(
                f"本轮请求数已达上限 {self.policy.max_requests_per_run}"
            )
        if self.messages >= self.policy.max_messages_per_run:
            raise BudgetExhausted(
                f"本轮消息数已达上限 {self.policy.max_messages_per_run}"
            )
        if self.remaining_seconds <= self.policy.max_page_delay:
            raise BudgetExhausted(
                f"本轮墙钟预算 {self.policy.max_minutes_per_run:.0f} 分钟即将用尽"
            )

    def _between(self, low: float, high: float) -> float:
        # 三角分布：多数间隔偏短，偶尔出现接近上限的停顿，避免固定周期特征
        peak = low + (high - low) * 0.35
        return round(self.rng.triangular(low, high, peak), 3)

    def _sleep(self, seconds: float, label: str) -> None:
        if seconds <= 0:
            return
        self.sleeper(seconds)
        self.slept_seconds += seconds
        self.timeline.append((round(self.elapsed_seconds, 3), label, round(seconds, 3)))

    def before_channel(self) -> None:
        """切换频道前的冷却，模拟「看完一个频道再点开下一个」。"""
        if self.channels_visited > 0:
            self._sleep(
                self._between(self.policy.channel_cooldown_min, self.policy.channel_cooldown_max),
                "channel-cooldown",
            )
        self.channels_visited += 1

    def after_page(self, message_count: int) -> None:
        """记录一页请求的消耗，并按节奏插入间隔或长停顿。"""
        self.requests += 1
        self.messages += message_count
        self.pages_since_pause += 1
        if self.pages_since_pause >= self.policy.pause_every_pages:
            self.pages_since_pause = 0
            self._sleep(self._between(self.policy.pause_min, self.policy.pause_max), "long-pause")
            return
        self._sleep(
            self._between(self.policy.min_page_delay, self.policy.max_page_delay),
            "page-delay",
        )

    def wait_between(self, low: float, high: float, label: str) -> None:
        """按给定区间随机等待，供列表类一次性请求复用同一套节奏。"""
        self._sleep(self._between(low, high), label)

    def wait_fixed(self, seconds: float, label: str) -> None:
        """等待固定时长，用于限流退避或分段发送间隔。"""
        self._sleep(seconds, label)

    def summary(self) -> str:
        return (
            f"请求 {self.requests} 次，消息 {self.messages} 条，"
            f"等待 {self.slept_seconds:.0f}s，墙钟 {self.elapsed_seconds:.0f}s"
        )
