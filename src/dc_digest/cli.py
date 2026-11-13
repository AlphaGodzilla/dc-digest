"""命令行入口。

guilds  列出可读取消息的服务器与频道 ID
fetch   按时间窗抓取消息入库（拟人节奏，预算内可续传）
digest  从库里取窗口消息，调用 spi 生成简报并落盘
run     fetch + digest + 飞书推送
state   查看各频道抓取游标
doctor  检查配置与凭证是否就绪
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import schedule
from .alerts import format_degraded, format_failure
from .config import (
    PROJECT_ROOT,
    Channel,
    Config,
    ConfigError,
    keychain_secret,
    load_config,
)
from .discord_api import (
    TEXT_CHANNEL_TYPES,
    USER_AGENT,
    DiscordClient,
    DiscordError,
    DiscordRateLimited,
    ms_from_snowflake,
    snowflake_from_ms,
)
from .feishu import FeishuError, FeishuSender, load_credentials
from .media import (
    MediaDownloader,
    MediaSyncOutcome,
    collect_media,
    reset_missing_cache,
)
from .pace import BudgetExhausted, Pacer
from .render import RenderPolicy, chunk_messages
from .store import Message, Store, WindowImage
from .summarize import (
    ImageInput,
    SummarizeError,
    resolve_command,
    summarize,
)
from .timewin import (
    MAX_WINDOW_HOURS,
    TimeWindowError,
    clamp_window,
    describes_single_day,
    format_ms,
    parse_window,
    zone,
)

LOGGER = logging.getLogger("dc_digest")

KEPT_MESSAGE_TYPES = {0, 19, 20}  # 默认、回复、斜杠命令回复
EXIT_ERROR = 2


# ---------- 通用辅助 ----------


def _select_channels(config: Config, raw_ids: Sequence[str] | None) -> list[Channel]:
    if not raw_ids:
        if not config.channels:
            raise ConfigError(
                "config.toml 的 [[channels]] 为空：先用 `dc-digest guilds` 取频道 ID 并填进去"
            )
        return list(config.channels)
    known = {channel.channel_id: channel for channel in config.channels}
    selected: list[Channel] = []
    for raw in raw_ids:
        try:
            channel_id = int(raw)
        except ValueError:
            raise ConfigError(f"--channel 需要纯数字频道 ID，当前为 {raw!r}") from None
        selected.append(known.get(channel_id, Channel(channel_id=channel_id, name=f"channel-{channel_id}")))
    return selected


def _resolve_window(
    args: argparse.Namespace, config: Config, store: Store, *, clamp: bool = True
) -> tuple[int, int]:
    now = datetime.now(zone(config.timezone))
    last_to = store.last_finished_run_to_ms()
    if args.from_ is None and args.to is None:
        if last_to is not None:
            return _bounded(last_to, int(now.timestamp() * 1000), config, clamp=clamp)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return _bounded(
            int((today_start - timedelta(days=1)).timestamp() * 1000),
            int(today_start.timestamp() * 1000),
            config,
            clamp=clamp,
        )
    from_raw = args.from_
    if from_raw is None:
        if last_to is None:
            raise TimeWindowError("没有历史运行记录，必须显式指定 --from")
        from_raw = format_ms(last_to, config.timezone, "%Y-%m-%dT%H:%M")
    start_ms, end_ms = parse_window(from_raw, args.to or "now", tz_name=config.timezone, now=now)
    return _bounded(start_ms, end_ms, config, clamp=clamp)


def _bounded(
    start_ms: int, end_ms: int, config: Config, *, clamp: bool = True
) -> tuple[int, int]:
    """抓取窗口跨度不超过 MAX_WINDOW_HOURS，超了就把起点后移、终点不动。

    digest 只读库、不碰网络，所以传 clamp=False 保留任意历史窗口。
    """
    if not clamp:
        return start_ms, end_ms
    clamped, end = clamp_window(start_ms, end_ms)
    if clamped != start_ms:
        LOGGER.warning(
            "窗口跨度超过 %d 小时，起点从 %s 调整到 %s",
            MAX_WINDOW_HOURS,
            format_ms(start_ms, config.timezone),
            format_ms(clamped, config.timezone),
        )
    return clamped, end


def _parse_iso_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _to_message(channel_id: int, payload: dict) -> Message:
    author = payload.get("author") or {}
    display_name = author.get("global_name") or author.get("username") or "unknown"
    author_id = author.get("id")
    referenced = payload.get("referenced_message") or {}
    reply_author = (referenced.get("author") or {}).get("username") if referenced else None
    edited = payload.get("edited_timestamp")
    attachments = tuple(
        item.get("filename") or "file" for item in payload.get("attachments") or []
    )
    return Message(
        id=int(payload["id"]),
        channel_id=channel_id,
        author_id=int(author_id) if author_id else None,
        author=display_name,
        ts_ms=_parse_iso_ms(payload["timestamp"]),
        content=payload.get("content") or "",
        attachments=attachments,
        reply_author=reply_author,
        edited_ts_ms=_parse_iso_ms(edited) if edited else None,
    )


def _is_kept_message(payload: dict) -> bool:
    return payload.get("type", 0) in KEPT_MESSAGE_TYPES


# ---------- fetch ----------


@dataclass
class FetchOutcome:
    requests: int
    messages: int
    incomplete: list[str]
    stop_reason: str | None


def _fetch(
    config: Config,
    store: Store,
    channels: Sequence[Channel],
    from_ms: int,
    to_ms: int,
    *,
    client_factory: Callable[[], DiscordClient] | None = None,
    pacer: Pacer | None = None,
) -> FetchOutcome:
    policy = config.fetch
    pacer = pacer or Pacer(policy)
    pacer.begin()
    incomplete: list[str] = []
    stop_reason: str | None = None
    stored = 0
    rate_limited = 0
    make_client = client_factory or (
        lambda: DiscordClient(
            config.discord.token(),
            auth=config.discord.auth,
            proxy=config.http.proxy or None,
        )
    )

    with make_client() as client:
        for channel in channels:
            if stop_reason is not None:
                incomplete.append(channel.name)
                continue
            state = store.sync_window(channel.channel_id, from_ms, to_ms)
            if state.complete:
                LOGGER.info("%s 在窗口内已抓完，跳过", channel.name)
                continue
            pacer.before_channel()
            cursor_ms = state.cursor_ms
            cursor_id = (
                state.cursor_id
                if state.cursor_id is not None
                else snowflake_from_ms(cursor_ms + 1)
            )
            while True:
                try:
                    pacer.require_capacity()
                except BudgetExhausted as exc:
                    stop_reason = exc.reason
                    incomplete.append(channel.name)
                    break
                try:
                    page = client.messages(
                        channel.channel_id,
                        before_id=cursor_id,
                        limit=policy.page_size,
                    )
                except DiscordRateLimited as exc:
                    rate_limited += 1
                    LOGGER.warning("%s", exc)
                    too_long = exc.retry_after > 90
                    if too_long or rate_limited >= policy.rate_limit_stop_after:
                        stop_reason = (
                            f"触发限流，退避 {policy.rate_limit_backoff_minutes:.0f} 分钟后再续传"
                        )
                        store.save_progress(channel.channel_id, cursor_id, cursor_ms, False)
                        incomplete.append(channel.name)
                        break
                    pacer.wait_fixed(exc.retry_after + pacer.rng.uniform(1.0, 3.0), "rate-limit")
                    continue

                if not page:
                    store.save_progress(channel.channel_id, cursor_id, cursor_ms, True)
                    LOGGER.info("%s 已到达频道起点", channel.name)
                    break

                rows = [
                    _to_message(channel.channel_id, item)
                    for item in page
                    if _is_kept_message(item)
                ]
                stored += store.upsert(rows)
                store.upsert_media(
                    [
                        record
                        for item in page
                        if _is_kept_message(item)
                        for record in collect_media(
                            int(item["id"]), item, config.media.allowed_hosts
                        )
                    ]
                )
                pacer.after_page(len(page))

                oldest_id = min(int(item["id"]) for item in page)
                oldest_ms = min(ms_from_snowflake(int(item["id"])) for item in page)
                if cursor_id is not None and oldest_id >= cursor_id:
                    raise DiscordError(
                        f"{channel.name} 分页未前进（{oldest_id} >= {cursor_id}），停止以免重复抓取"
                    )
                cursor_id, cursor_ms = oldest_id, oldest_ms
                done = len(page) < policy.page_size or oldest_ms <= from_ms
                store.save_progress(channel.channel_id, cursor_id, cursor_ms, done)
                LOGGER.info(
                    "%s 取回 %d 条，向前推进到 %s",
                    channel.name,
                    len(page),
                    format_ms(oldest_ms, config.timezone),
                )
                if done:
                    break

    LOGGER.info("抓取结束：%s", pacer.summary())
    if pacer.timeline:
        LOGGER.debug(
            "等待明细：%s",
            ", ".join(f"{label}@{at}s={seconds}s" for at, label, seconds in pacer.timeline),
        )
    return FetchOutcome(
        requests=pacer.requests,
        messages=stored,
        incomplete=incomplete,
        stop_reason=stop_reason,
    )


# ---------- digest ----------


@dataclass
class DigestOutcome:
    path: Path | None
    markdown: str | None
    note: str
    total_messages: int


def _build_images(
    config: Config, channels: Sequence[Channel], window_images: Sequence[WindowImage]
) -> tuple[dict[int, ImageInput], dict[int, list[int]]]:
    """把已下载的图片按时间顺序编号，并建立「消息 → 图片编号」索引。"""
    names = {channel.channel_id: channel.name for channel in channels}
    images: dict[int, ImageInput] = {}
    labels_by_message: dict[int, list[int]] = {}
    for image in window_images:
        path = Path(image.local_path)
        if not path.exists():
            LOGGER.warning("图片文件缺失，跳过：%s", path)
            continue
        index = len(images) + 1
        images[index] = ImageInput(
            index=index,
            path=path,
            manifest_line=(
                f"图片 {index}：{names.get(image.channel_id, image.channel_id)} "
                f"{format_ms(image.ts_ms, config.timezone, '%m-%d %H:%M')} "
                f"{image.author} 发的 {image.filename}"
            ),
        )
        labels_by_message.setdefault(image.message_id, []).append(index)
    return images, labels_by_message


def _media_sync(
    config: Config,
    store: Store,
    channels: Sequence[Channel],
    from_ms: int,
    to_ms: int,
) -> MediaSyncOutcome:
    """下载窗口内缺失的图片；本地缓存失效的行会被先打回待下载。"""
    channel_ids = [channel.channel_id for channel in channels]
    reset_missing_cache(store, channel_ids, from_ms, to_ms)
    pacer = Pacer(config.fetch)
    pacer.begin()
    with DiscordClient(
        config.discord.token(), auth=config.discord.auth, proxy=config.http.proxy or None
    ) as client:
        downloader = MediaDownloader(
            client, config.media, media_dir=config.media_dir, pacer=pacer
        )
        outcome = downloader.sync(store, channel_ids, from_ms, to_ms)
    LOGGER.info("图片同步：%s", outcome.summary())
    if outcome.stop_reason:
        LOGGER.warning("图片同步提前结束：%s", outcome.stop_reason)
    for note in outcome.notes[:5]:
        LOGGER.warning("图片失败：%s", note)
    return outcome


def _ensure_media(
    config: Config,
    store: Store,
    channels: Sequence[Channel],
    from_ms: int,
    to_ms: int,
) -> None:
    """摘要前保证图片可用：文件缺失的先打回待下载，只有确实有待下载项才联网。"""
    channel_ids = [channel.channel_id for channel in channels]
    reset_missing_cache(store, channel_ids, from_ms, to_ms)
    pending = store.media_counts(channel_ids, from_ms, to_ms).get("pending", 0)
    if not pending:
        return
    LOGGER.info("窗口内还有 %d 张图片待下载，先补齐再摘要", pending)
    _media_sync(config, store, channels, from_ms, to_ms)


def _channel_complete(store: Store, channel: Channel, from_ms: int, to_ms: int) -> bool:
    state = store.state(channel.channel_id)
    if state is None:
        return False
    return (
        state.complete
        and state.window_from_ms <= from_ms
        and state.window_to_ms >= to_ms
    )


def _frontmatter(
    config: Config,
    channels: Sequence[Channel],
    from_ms: int,
    to_ms: int,
    total_messages: int,
    *,
    complete: bool,
    note: str,
) -> str:
    names = ", ".join(channel.name for channel in channels)
    generated = datetime.now(zone(config.timezone)).strftime("%Y-%m-%d %H:%M:%S")
    return (
        "---\n"
        f"window_from: {format_ms(from_ms, config.timezone, '%Y-%m-%dT%H:%M')}\n"
        f"window_to: {format_ms(to_ms, config.timezone, '%Y-%m-%dT%H:%M')}\n"
        f"timezone: {config.timezone}\n"
        f"channels: {names}\n"
        f"messages: {total_messages}\n"
        f"fetch_complete: {'true' if complete else 'false'}\n"
        f"fetch_note: {json.dumps(note, ensure_ascii=False)}\n"
        f"generated: {generated}\n"
        "---\n\n"
    )


def _output_name(from_ms: int, to_ms: int, tz_name: str) -> str:
    if describes_single_day(from_ms, to_ms, tz_name):
        return f"{format_ms(from_ms, tz_name, '%Y-%m-%d')}.md"
    start = format_ms(from_ms, tz_name, "%Y-%m-%d_%H%M")
    end = format_ms(to_ms, tz_name, "%Y-%m-%d_%H%M")
    return f"{start}__{end}.md"


def _digest(
    config: Config,
    store: Store,
    channels: Sequence[Channel],
    from_ms: int,
    to_ms: int,
    *,
    dry_run: bool,
    skip_media: bool = False,
) -> DigestOutcome:
    channel_ids = [channel.channel_id for channel in channels]
    if config.media.enabled and not skip_media and not dry_run and config.media.ensure_on_digest:
        _ensure_media(config, store, channels, from_ms, to_ms)
    messages = store.messages_in_window(channel_ids, from_ms, to_ms)
    missing = [
        channel.name
        for channel in channels
        if not _channel_complete(store, channel, from_ms, to_ms)
    ]
    notes: list[str] = []
    if missing:
        notes.append(
            "以下频道在窗口内未抓取完整："
            + "、".join(missing)
            + "；再次运行 fetch 可续传补齐，本份简报可能缺内容"
        )

    window_images: list[WindowImage] = []
    pending_images = 0
    if config.media.enabled:
        pending_images = store.media_counts(channel_ids, from_ms, to_ms).get("pending", 0)
        window_images = store.window_images(channel_ids, from_ms, to_ms)
        if pending_images:
            notes.append(f"窗口内还有 {pending_images} 张图片未下载，先运行 media 可补齐")
        if len(window_images) > config.media.max_images_per_digest:
            notes.append(
                f"可用图片 {len(window_images)} 张，超出单份上限 "
                f"{config.media.max_images_per_digest} 张，本次只使用前 {config.media.max_images_per_digest} 张"
            )
            window_images = window_images[: config.media.max_images_per_digest]
    note = "；".join(notes) if notes else "所有目标频道在窗口内抓取完整"
    if not messages:
        LOGGER.warning("窗口内没有消息，跳过简报")
        return DigestOutcome(path=None, markdown=None, note=note, total_messages=0)

    window_text = (
        f"{format_ms(from_ms, config.timezone)} ~ {format_ms(to_ms, config.timezone)}"
        f"（{config.timezone}）"
    )
    images, labels_by_message = _build_images(config, channels, window_images)
    links_by_message: dict[int, list[str]] = {}
    for message_id, url in store.window_external_links(channel_ids, from_ms, to_ms):
        links_by_message.setdefault(message_id, []).append(url)
    if links_by_message:
        LOGGER.info(
            "窗口内有 %d 条消息带外部链接，仅以文本入简报，不做任何访问",
            len(links_by_message),
        )
    chunks = chunk_messages(
        messages,
        config.timezone,
        RenderPolicy(),
        config.summarize.max_chars_per_call,
        labels_by_message,
        links_by_message,
    )
    LOGGER.info(
        "窗口内 %d 条消息、%d 张图，切成 %d 块（每块上限 %d 字符）",
        len(messages),
        len(images),
        len(chunks),
        config.summarize.max_chars_per_call,
    )
    if dry_run:
        LOGGER.info("dry-run：跳过摘要调用，首块前 500 字符如下：\n%s", chunks[0].text[:500])
        return DigestOutcome(path=None, markdown=None, note=note, total_messages=len(messages))

    markdown = summarize(
        config.summarize,
        window_text=window_text,
        channel_text="、".join(channel.name for channel in channels),
        total_messages=len(messages),
        note=note,
        chunks=chunks,
        images=images,
        max_images_per_call=config.media.max_images_per_call,
    )
    output_path = config.output_dir / _output_name(from_ms, to_ms, config.timezone)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        _frontmatter(
            config,
            channels,
            from_ms,
            to_ms,
            len(messages),
            complete=not missing,
            note=note,
        )
        + markdown
        + "\n"
    )
    output_path.write_text(body, encoding="utf-8")
    LOGGER.info("简报已写入 %s", output_path)
    return DigestOutcome(path=output_path, markdown=markdown, note=note, total_messages=len(messages))


def _now_text(config: Config) -> str:
    return datetime.now(zone(config.timezone)).strftime("%Y-%m-%d %H:%M:%S")


def _window_text(config: Config, from_ms: int, to_ms: int) -> str:
    start = format_ms(from_ms, config.timezone)
    end = format_ms(to_ms, config.timezone)
    return f"{start} ~ {end}（{config.timezone}）"


def _send_alert(config: Config, *, title: str, body: str) -> None:
    """尽力发送提醒：提醒自身失败只记日志，绝不掩盖原始故障。"""
    if not (config.alerts.enabled and config.feishu.enabled):
        LOGGER.info("提醒已关闭（alerts.enabled 或 feishu.enabled 为 false），仅记录日志")
        return
    try:
        with FeishuSender(
            config.feishu, proxy=config.http.proxy or None
        ) as sender:
            parts = sender.send(title=title, markdown=body)
        LOGGER.warning("已通过飞书发出提醒（%d 条）", len(parts))
    except Exception as exc:  # noqa: BLE001 - 兜底日志，避免掩盖原始故障
        LOGGER.error("飞书提醒发送失败：%s", exc)


def _notify_failure(
    config: Config,
    *,
    command: str,
    exc: BaseException,
    window_text: str | None = None,
    no_alert: bool = False,
) -> None:
    if no_alert:
        LOGGER.info("已指定 --no-alert，跳过失败提醒")
        return
    body = format_failure(
        command=command,
        window_text=window_text,
        exc=exc,
        now_text=_now_text(config),
    )
    _send_alert(config, title=f"dc-digest 失败：{command}", body=body)


def _notify_degraded(
    config: Config,
    *,
    command: str,
    window_text: str,
    reason: str,
    incomplete: Sequence[str],
    no_alert: bool = False,
) -> None:
    if no_alert:
        return
    body = format_degraded(
        command=command,
        window_text=window_text,
        reason=reason,
        incomplete=list(incomplete),
        now_text=_now_text(config),
    )
    _send_alert(config, title=f"dc-digest 抓取不完整：{command}", body=body)


def _push(config: Config, *, title: str, markdown: str, dry_run: bool) -> None:
    if not config.feishu.enabled:
        LOGGER.info("feishu.enabled=false，跳过推送")
        return
    with FeishuSender(
        config.feishu, dry_run=dry_run, proxy=config.http.proxy or None
    ) as sender:
        sender.send(title=title, markdown=markdown)


# ---------- 子命令 ----------


def cmd_guilds(config: Config, args: argparse.Namespace) -> int:
    pacer = Pacer(config.fetch)
    pacer.begin()
    with DiscordClient(
        config.discord.token(), auth=config.discord.auth, proxy=config.http.proxy or None
    ) as client:
        me = client.me()
        LOGGER.info("当前账号：%s（id=%s）", me.get("username"), me.get("id"))
        if args.guild is not None:
            for channel in client.guild_channels(args.guild):
                if channel.get("type") in TEXT_CHANNEL_TYPES:
                    print(f"{channel['id']}  #{channel['name']}")
            return 0

        guilds = client.guilds()
        if len(guilds) == 1:
            guild = guilds[0]
            LOGGER.info("只有一个服务器：%s（id=%s），直接列出频道", guild["name"], guild["id"])
            pacer.wait_between(1.5, 3.0, "list-delay")
            for channel in client.guild_channels(int(guild["id"])):
                if channel.get("type") in TEXT_CHANNEL_TYPES:
                    print(f"{channel['id']}  #{channel['name']}")
            return 0

        print("服务器列表（用 --guild <服务器id> 展开它的频道）：")
        for guild in guilds:
            print(f"{guild['id']}  {guild['name']}")
    return 0


def cmd_fetch(config: Config, args: argparse.Namespace) -> int:
    channels = _select_channels(config, args.channel)
    with Store(config.database) as store:
        from_ms, to_ms = _resolve_window(args, config, store)
        LOGGER.info(
            "抓取窗口 %s ~ %s",
            format_ms(from_ms, config.timezone),
            format_ms(to_ms, config.timezone),
        )
        if args.dry_run:
            LOGGER.info("dry-run：不发起网络请求，目标频道 %s", "、".join(c.name for c in channels))
            return 0
        run = store.start_run(from_ms, to_ms)
        try:
            outcome = _fetch(config, store, channels, from_ms, to_ms)
        except BaseException as exc:
            store.finish_run(
                run.id, status="error", requests=0, messages=0, note=str(exc)[:500]
            )
            raise
        status = "ok" if not outcome.incomplete else "incomplete"
        store.finish_run(
            run.id,
            status=status,
            requests=outcome.requests,
            messages=outcome.messages,
            note=outcome.stop_reason or "",
        )
        if outcome.stop_reason:
            LOGGER.warning("本轮提前结束：%s", outcome.stop_reason)
            _notify_degraded(
                config,
                command="fetch",
                window_text=_window_text(config, from_ms, to_ms),
                reason=outcome.stop_reason,
                incomplete=outcome.incomplete,
                no_alert=getattr(args, "no_alert", False),
            )
        LOGGER.info("入库 %d 条，未完成频道 %d 个", outcome.messages, len(outcome.incomplete))
    return 0


def cmd_digest(config: Config, args: argparse.Namespace) -> int:
    channels = _select_channels(config, args.channel)
    with Store(config.database) as store:
        # digest 只读库，不做抓取窗口上限，允许对任意历史区间出简报
        from_ms, to_ms = _resolve_window(args, config, store, clamp=False)
        outcome = _digest(
            config, store, channels, from_ms, to_ms, dry_run=args.dry_run,
            skip_media=args.no_media,
        )
        if outcome.path is None:
            return 0
        print(outcome.path)
        if args.push:
            title = (
                f"Discord 简报 {format_ms(from_ms, config.timezone, '%m-%d %H:%M')}"
                f" ~ {format_ms(to_ms, config.timezone, '%m-%d %H:%M')}"
            )
            _push(config, title=title, markdown=outcome.markdown or "", dry_run=args.dry_run)
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    channels = _select_channels(config, args.channel)
    with Store(config.database) as store:
        from_ms, to_ms = _resolve_window(args, config, store)
        LOGGER.info(
            "运行窗口 %s ~ %s，频道 %d 个",
            format_ms(from_ms, config.timezone),
            format_ms(to_ms, config.timezone),
            len(channels),
        )
        if not args.dry_run:
            run = store.start_run(from_ms, to_ms)
            try:
                fetched = _fetch(config, store, channels, from_ms, to_ms)
            except BaseException as exc:
                store.finish_run(
                    run.id, status="error", requests=0, messages=0, note=str(exc)[:500]
                )
                raise
            store.finish_run(
                run.id,
                status="ok" if not fetched.incomplete else "incomplete",
                requests=fetched.requests,
                messages=fetched.messages,
                note=fetched.stop_reason or "",
            )
            if fetched.stop_reason:
                LOGGER.warning("抓取提前结束：%s", fetched.stop_reason)
                _notify_degraded(
                    config,
                    command="run",
                    window_text=_window_text(config, from_ms, to_ms),
                    reason=fetched.stop_reason,
                    incomplete=fetched.incomplete,
                no_alert=getattr(args, "no_alert", False),
                )
            if config.media.enabled and not args.no_media:
                _media_sync(config, store, channels, from_ms, to_ms)
        else:
            LOGGER.info("dry-run：跳过网络抓取与图片下载")
        outcome = _digest(
            config, store, channels, from_ms, to_ms, dry_run=args.dry_run,
            skip_media=args.no_media,
        )
        if outcome.path is not None:
            print(outcome.path)
            if not args.no_push:
                title = (
                    f"Discord 简报 {format_ms(from_ms, config.timezone, '%m-%d %H:%M')}"
                    f" ~ {format_ms(to_ms, config.timezone, '%m-%d %H:%M')}"
                )
                _push(config, title=title, markdown=outcome.markdown or "", dry_run=args.dry_run)
        if args.prune and not args.dry_run:
            counts = store.prune(config.store.retention_days)
            LOGGER.info(
                "留存清理：删除 %d 条消息、%d 条附件记录（留存 %d 天）",
                counts["messages"],
                counts["media"],
                config.store.retention_days,
            )
    return 0


def cmd_media(config: Config, args: argparse.Namespace) -> int:
    if not config.media.enabled:
        LOGGER.warning("media.enabled=false，跳过图片下载")
        return 0
    channels = _select_channels(config, args.channel)
    with Store(config.database) as store:
        from_ms, to_ms = _resolve_window(args, config, store)
        channel_ids = [channel.channel_id for channel in channels]
        if args.clean:
            cached = store.reset_media_window(channel_ids, from_ms, to_ms)
            removed = 0
            for raw in cached:
                path = Path(raw)
                if path.exists():
                    path.unlink()
                    removed += 1
            LOGGER.info("清理缓存：删除 %d 个文件，%d 行打回待下载", removed, len(cached))
            return 0
        if args.dry_run:
            pending = store.pending_media(channel_ids, from_ms, to_ms)
            LOGGER.info(
                "dry-run：窗口 %s ~ %s 内待下载图片 %d 张",
                format_ms(from_ms, config.timezone),
                format_ms(to_ms, config.timezone),
                len(pending),
            )
            for item in pending[:10]:
                print(f"{item.filename}  {item.url}")
            return 0
        outcome = _media_sync(config, store, channels, from_ms, to_ms)
        print(outcome.summary())
        if outcome.stop_reason:
            _notify_degraded(
                config,
                command="media",
                window_text=_window_text(config, from_ms, to_ms),
                reason=outcome.stop_reason,
                incomplete=[],
                no_alert=getattr(args, "no_alert", False),
            )
    return 0


def cmd_schedule(config: Config, args: argparse.Namespace) -> int:
    path = schedule.plist_path()
    log_dir = schedule.log_dir(config.database)
    extra_dirs = schedule.path_dirs(config.summarize.command)

    def make_plist() -> bytes:
        return schedule.build_plist(
            uv_path=schedule.uv_executable(),
            project_root=PROJECT_ROOT,
            interval_hours=args.interval_hours,
            log_dir=log_dir,
            extra_path_dirs=extra_dirs,
        )

    if args.action == "print":
        print(make_plist().decode("utf-8"))
        return 0

    if args.action == "install":
        if not shutil.which(config.summarize.command):
            LOGGER.warning(
                "找不到摘要命令 %r：定时任务的 PATH 比登录 shell 精简，"
                "建议把 [summarize] command 改成绝对路径后再安装",
                config.summarize.command,
            )
        schedule.install(make_plist(), path=path)
        print(f"已安装 {path}（每 {args.interval_hours} 小时运行一次）")
        print(f"PATH 追加     : {'、'.join(extra_dirs) if extra_dirs else '未解析到摘要命令目录'}")
        print(f"状态          : {schedule.status()}")
        return 0

    if args.action == "uninstall":
        removed = schedule.uninstall(path=path)
        print("已卸载定时任务" if removed else "没有已安装的定时任务")
        return 0

    print(f"plist   : {path}（{'存在' if path.exists() else '不存在'}）")
    print(f"状态    : {schedule.status()}")
    return 0


def cmd_prune(config: Config, args: argparse.Namespace) -> int:
    retention = args.retention_days if args.retention_days is not None else config.store.retention_days
    with Store(config.database) as store:
        counts = store.prune(retention, dry_run=args.dry_run)
    prefix = "dry-run：" if args.dry_run else ""
    print(
        f"{prefix}留存 {retention} 天，"
        f"清理 {counts['messages']} 条消息、{counts['media']} 条附件记录"
        f"（截止 {format_ms(counts['cutoff_ms'], config.timezone)}）"
    )
    return 0


def cmd_state(config: Config, args: argparse.Namespace) -> int:
    with Store(config.database) as store:
        rows = [
            (channel, store.state(channel.channel_id))
            for channel in _select_channels(config, args.channel)
        ]
    if not rows:
        print("没有配置任何频道")
        return 0
    for channel, state in rows:
        if state is None:
            print(f"{channel.name:<24} 从未抓取")
            continue
        print(
            f"{channel.name:<24} 窗口 {format_ms(state.window_from_ms, config.timezone)} ~ "
            f"{format_ms(state.window_to_ms, config.timezone)}  "
            f"游标 {format_ms(state.cursor_ms, config.timezone)}  "
            f"{'完整' if state.complete else '未完成'}"
        )
    return 0


def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    print(f"配置文件      : {config.path}")
    print(f"时区          : {config.timezone}")
    print(f"数据库        : {config.database}")
    print(f"简报目录      : {config.output_dir}")
    print(f"图片缓存      : {config.media_dir}（临时目录，可随时重下）")
    auth_shape = "Bot <token>" if config.discord.auth == "bot" else "<token>（裸 token）"
    print(
        f"请求头        : Authorization: {auth_shape}、Accept: application/json、"
        f"User-Agent: {USER_AGENT}（不伪造客户端指纹）"
    )
    token_present = bool(os.environ.get(config.discord.token_env)) or keychain_secret(
        config.discord.token_keychain_service
    ) is not None
    print(f"Discord token : {'就绪' if token_present else '缺失'}（auth={config.discord.auth}）")
    try:
        credentials = load_credentials(config.feishu.credentials_file)
        receiver = config.feishu.receiver_value or credentials.default_receiver
        print(
            "飞书凭证      : 就绪"
            f"（appId {len(credentials.app_id)} 字符，接收方 {'已配置' if receiver else '缺失'}）"
        )
    except FeishuError as exc:
        print(f"飞书凭证      : {exc}")
    print(f"目标频道      : {len(config.channels)} 个")
    for prompt in (
        config.summarize.digest_prompt,
        config.summarize.chunk_prompt,
        config.summarize.reduce_prompt,
    ):
        print(f"prompt 模板   : {'就绪' if prompt.exists() else '缺失'} {prompt}")
    print(f"摘要命令      : {' '.join(config.summarize.argv)}")
    resolved = resolve_command(config.summarize.command)
    if Path(resolved).is_file():
        print(f"摘要命令解析  : 就绪 {resolved}（PATH 里没有也会兜底找 ~/bin 等目录）")
    else:
        print(f"摘要命令解析  : 找不到 {config.summarize.command}，摘要会失败")
    return 0


# ---------- 参数解析 ----------


def _add_common(parser: argparse.ArgumentParser) -> None:
    # 默认值用 SUPPRESS：这些开关同时挂在主解析器与子解析器上，
    # 否则子解析器的默认值会覆盖用户在子命令之前给出的值
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="配置文件路径，默认项目根目录的 config.toml",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="输出调试日志",
    )
    parser.add_argument(
        "--no-alert",
        action="store_true",
        default=argparse.SUPPRESS,
        help="本次运行不发送失败/降级提醒",
    )


def _add_window(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--from",
        dest="from_",
        help="窗口起点：2026-09-08 / 2026-09-08T10:30 / now / today / yesterday / -7d",
    )
    parser.add_argument("--to", help="窗口终点，只写日期时包含该整天")
    parser.add_argument(
        "--channel",
        action="append",
        help="只处理指定频道 ID，可重复；不传则用 config.toml 的 [[channels]]",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dc-digest", description="按时间窗抓取 Discord 消息并生成简报")
    _add_common(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    guilds = subparsers.add_parser("guilds", help="列出服务器与文字频道 ID", parents=[_common_parser()])
    guilds.add_argument("--guild", type=int, help="展开指定服务器的频道")
    guilds.set_defaults(handler=cmd_guilds)

    fetch = subparsers.add_parser("fetch", help="按时间窗抓取消息入库", parents=[_common_parser()])
    _add_window(fetch)
    fetch.add_argument("--dry-run", action="store_true", help="只打印计划，不发起请求")
    fetch.set_defaults(handler=cmd_fetch)

    digest = subparsers.add_parser("digest", help="生成简报并落盘", parents=[_common_parser()])
    _add_window(digest)
    digest.add_argument("--dry-run", action="store_true", help="不调用摘要模型，只打印统计")
    digest.add_argument("--push", action="store_true", help="生成后立刻推送到飞书")
    digest.add_argument("--no-media", action="store_true", help="跳过图片补齐与多模态摘要")
    digest.set_defaults(handler=cmd_digest)

    run = subparsers.add_parser("run", help="抓取 + 生成 + 推送", parents=[_common_parser()])
    _add_window(run)
    run.add_argument("--dry-run", action="store_true", help="不联网、不调用模型，只打印计划")
    run.add_argument("--no-push", action="store_true", help="只落盘，不推送飞书")
    run.add_argument("--no-media", action="store_true", help="跳过图片下载")
    run.add_argument(
        "--prune", action="store_true", help="跑完后按 store.retention_days 清理历史"
    )
    run.set_defaults(handler=cmd_run)

    media = subparsers.add_parser("media", help="下载窗口内的图片附件", parents=[_common_parser()])
    _add_window(media)
    media.add_argument("--dry-run", action="store_true", help="只列出待下载图片，不下载")
    media.add_argument("--clean", action="store_true", help="删除窗口内本地缓存并把记录打回待下载")
    media.set_defaults(handler=cmd_media)

    prune = subparsers.add_parser("prune", help="按留存期清理库内历史", parents=[_common_parser()])
    prune.add_argument("--retention-days", type=int, help="覆盖 config.toml 的 store.retention_days")
    prune.add_argument("--dry-run", action="store_true", help="只统计不删除")
    prune.set_defaults(handler=cmd_prune)

    schedule_cmd = subparsers.add_parser(
        "schedule", help="launchd 定时任务：install / uninstall / status / print", parents=[_common_parser()]
    )
    schedule_cmd.add_argument(
        "action", choices=["install", "uninstall", "status", "print"]
    )
    schedule_cmd.add_argument(
        "--interval-hours", type=int, default=3, help="执行间隔小时数，默认 3"
    )
    schedule_cmd.set_defaults(handler=cmd_schedule)

    state = subparsers.add_parser("state", help="查看抓取游标", parents=[_common_parser()])
    state.add_argument("--channel", action="append", help="只看指定频道 ID")
    state.set_defaults(handler=cmd_state)

    doctor = subparsers.add_parser("doctor", help="检查配置与凭证", parents=[_common_parser()])
    doctor.set_defaults(handler=cmd_doctor)
    return parser


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)
    return common


def _human_size(size: int) -> str:
    return f"{size // 1024} KB" if size >= 1024 else f"{size} 字节"


def _rotate_logs(config: Config) -> None:
    """把超限的 launchd 日志截到尾部，保证无人值守时磁盘占用有上限。"""
    # stdout 重定向到文件时是块缓冲的，先刷掉再量体积，否则会低估当前大小
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError):
            pass
    try:
        rotated = schedule.rotate_logs(
            schedule.log_dir(config.database),
            max_bytes=config.logs.max_bytes,
            keep_bytes=config.logs.keep_bytes,
        )
    except schedule.ScheduleError as exc:
        LOGGER.warning("%s", exc)
        return
    if rotated:
        LOGGER.info(
            "已轮转日志，保留最近 %s：%s", _human_size(config.logs.keep_bytes), "、".join(rotated)
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # httpx 会把每个请求以 INFO 打到日志里，噪音太大，只保留告警
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = load_config(getattr(args, "config", None))
    except (ConfigError, TimeWindowError) as exc:
        # 读不到配置就没有通知渠道，只能留在日志里
        LOGGER.error("配置加载失败：%s", exc)
        return EXIT_ERROR

    # 定时任务的 stdout/stderr 落在 data/logs/launchd.log，长期运行需要防止无界增长
    _rotate_logs(config)
    try:
        return args.handler(config, args)
    except (
        DiscordError,
        FeishuError,
        SummarizeError,
        TimeWindowError,
        ConfigError,
        schedule.ScheduleError,
    ) as exc:
        LOGGER.error("%s", exc)
        _notify_failure(
            config,
            command=args.command,
            exc=exc,
            no_alert=getattr(args, "no_alert", False),
        )
        return EXIT_ERROR
    except Exception as exc:
        LOGGER.exception("未预期的错误")
        _notify_failure(
            config,
            command=args.command,
            exc=exc,
            no_alert=getattr(args, "no_alert", False),
        )
        return EXIT_ERROR
    except KeyboardInterrupt:
        LOGGER.error("已中断")
        return 130
    finally:
        # 单轮输出过大时也要立刻收口，不能等到下次启动才轮转
        _rotate_logs(config)


if __name__ == "__main__":
    sys.exit(main())
