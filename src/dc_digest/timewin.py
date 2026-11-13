"""把命令行里的时间写法解析成毫秒时间戳。

支持 `2026-09-08`、`2026-09-08T10:30`、`now`、`today`、`yesterday` 与
相对量 `-7d` / `-90m`。`--to` 只写日期时包含该整天，即结束于次日零点。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

RELATIVE_PATTERN = re.compile(r"^-(\d+(?:\.\d+)?)([mhdw])$")
DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}

DAY_MS = 86_400_000
# 单次时间窗的跨度上限：超过就只抓最近这一段，避免一口气索要几年的历史
MAX_WINDOW_HOURS = 48
MAX_WINDOW_MS = MAX_WINDOW_HOURS * 3_600_000


class TimeWindowError(RuntimeError):
    """时间写法无法解析或区间非法。"""


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise TimeWindowError(f"未知时区: {name}") from exc


def _to_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def parse_bound(raw: str, *, tz: ZoneInfo, now: datetime) -> int:
    text = raw.strip()
    if not text:
        raise TimeWindowError("时间不能为空")

    lowered = text.lower()
    if lowered == "now":
        return _to_ms(now)
    if lowered == "today":
        return _to_ms(now.replace(hour=0, minute=0, second=0, microsecond=0))
    if lowered == "yesterday":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return _to_ms(start)

    relative = RELATIVE_PATTERN.match(text)
    if relative:
        amount = float(relative.group(1))
        unit = relative.group(2)
        return _to_ms(now) - int(amount * UNIT_MS[unit])

    if DATE_ONLY_PATTERN.match(text):
        parsed = datetime.strptime(text, "%Y-%m-%d")
        return _to_ms(parsed.replace(tzinfo=tz))

    for fmt in DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return _to_ms(parsed)

    raise TimeWindowError(
        f"无法解析时间: {raw!r}（可用 2026-09-08 / 2026-09-08T10:30 / now / today / yesterday / -7d / -90m）"
    )


def parse_window(
    from_raw: str,
    to_raw: str,
    *,
    tz_name: str,
    now: datetime | None = None,
) -> tuple[int, int]:
    tz = zone(tz_name)
    reference = (now or datetime.now(tz)).astimezone(tz)
    start_ms = parse_bound(from_raw, tz=tz, now=reference)
    end_ms = parse_bound(to_raw, tz=tz, now=reference)
    if DATE_ONLY_PATTERN.match(to_raw.strip()):
        end_ms += DAY_MS
    if end_ms <= start_ms:
        raise TimeWindowError(
            f"时间窗非法: 起点 {from_raw} 不早于终点 {to_raw}"
        )
    return start_ms, end_ms


def clamp_window(
    start_ms: int, end_ms: int, *, max_ms: int = MAX_WINDOW_MS
) -> tuple[int, int]:
    """把过长的窗口收到最近 max_ms 内：起点后移，终点不动。"""
    if end_ms - start_ms <= max_ms:
        return start_ms, end_ms
    return end_ms - max_ms, end_ms


def format_ms(value: int, tz_name: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(value / 1000, zone(tz_name)).strftime(fmt)


def describes_single_day(start_ms: int, end_ms: int, tz_name: str) -> bool:
    """判断窗口是否恰好覆盖某个月的某个完整自然日。"""
    tz = zone(tz_name)
    start = datetime.fromtimestamp(start_ms / 1000, tz)
    end = datetime.fromtimestamp(end_ms / 1000, tz)
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return start == midnight and _to_ms(end) - start_ms == DAY_MS
