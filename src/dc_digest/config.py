"""配置与凭证加载。

config.toml 只放非敏感配置。Discord token 从环境变量或 macOS Keychain 读取，
飞书应用凭证复用 pi-channel 扩展已配置好的 appId/appSecret。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .schedule import LOG_KEEP_BYTES, LOG_MAX_BYTES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"

TEMP_MEDIA_DIR = Path(tempfile.gettempdir()) / "dc-digest-media"

FEISHU_BASE_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}


class ConfigError(RuntimeError):
    """配置缺失或非法，就地失败。"""


def keychain_secret(service: str) -> str | None:
    """读取 Keychain 中的通用密码，不存在时返回 None。"""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _expand(raw: str, base: Path) -> Path:
    """展开路径：绝对路径原样返回，相对路径按配置文件所在目录解析。"""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _check_keys(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"[{where}] 存在未知配置项: {', '.join(unknown)}")


@dataclass(frozen=True)
class DiscordConfig:
    auth: str = "user"
    token_keychain_service: str = "dc-digest-discord-token"
    token_env: str = "DC_DIGEST_DISCORD_TOKEN"

    def __post_init__(self) -> None:
        if self.auth not in {"user", "bot"}:
            raise ConfigError(f"discord.auth 只能是 user 或 bot，当前为 {self.auth!r}")

    def token(self) -> str:
        env_value = os.environ.get(self.token_env)
        if env_value and env_value.strip():
            return env_value.strip()
        secret = keychain_secret(self.token_keychain_service)
        if secret:
            return secret
        raise ConfigError(
            "未找到 Discord token，二选一：\n"
            f"  export {self.token_env}='<token>'\n"
            f"  security add-generic-password -s {self.token_keychain_service} -a \"$USER\" -w '<token>'"
        )


@dataclass(frozen=True)
class FetchPolicy:
    """拟人化取数策略：页大小、间隔、停顿、单次预算与限流退避。"""

    page_size: int = 50
    max_requests_per_run: int = 40
    max_messages_per_run: int = 2000
    max_minutes_per_run: float = 15.0
    min_page_delay: float = 2.5
    max_page_delay: float = 6.0
    pause_every_pages: int = 5
    pause_min: float = 10.0
    pause_max: float = 25.0
    channel_cooldown_min: float = 45.0
    channel_cooldown_max: float = 90.0
    rate_limit_stop_after: int = 2
    rate_limit_backoff_minutes: float = 45.0

    def __post_init__(self) -> None:
        if not 1 <= self.page_size <= 100:
            raise ConfigError("fetch.page_size 必须在 1..100 之间")
        if self.min_page_delay > self.max_page_delay:
            raise ConfigError("fetch.min_page_delay 不能大于 max_page_delay")
        if self.pause_every_pages < 1:
            raise ConfigError("fetch.pause_every_pages 必须 >= 1")


@dataclass(frozen=True)
class HttpConfig:
    """HTTP 出口设置。留空则沿用环境变量里的代理。"""

    proxy: str = ""


@dataclass(frozen=True)
class AlertsConfig:
    """失败与降级通知：无法获取聊天信息时通过飞书告知，而不是静默失败。"""

    enabled: bool = True


@dataclass(frozen=True)
class StoreConfig:
    """库留存策略：超过留存期的消息与附件记录会被 prune 清理。"""

    retention_days: int = 180

    def __post_init__(self) -> None:
        if self.retention_days < 1:
            raise ConfigError("store.retention_days 必须 >= 1")



@dataclass(frozen=True)
class LogsConfig:
    """launchd 日志的体量上限：超过 max_bytes 就截到尾部 keep_bytes。"""

    max_bytes: int = LOG_MAX_BYTES
    keep_bytes: int = LOG_KEEP_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes < 1024:
            raise ConfigError("logs.max_bytes 至少 1024")
        if self.keep_bytes < 0 or self.keep_bytes >= self.max_bytes:
            raise ConfigError("logs.keep_bytes 必须 >= 0 且小于 logs.max_bytes")

@dataclass(frozen=True)
class MediaConfig:
    """图片附件下载与多模态摘要策略。"""

    enabled: bool = True
    max_files_per_run: int = 40
    max_bytes_per_file: int = 8_000_000
    max_bytes_per_run: int = 40_000_000
    min_download_delay: float = 1.0
    max_download_delay: float = 3.0
    max_images_per_call: int = 8
    max_images_per_digest: int = 30
    ensure_on_digest: bool = True
    # 只下载这些域名下的附件；其余域名记为外部链接，只以文本进入 transcript
    allowed_hosts: tuple[str, ...] = (
        "discordapp.com",
        "discordapp.net",
        "discord.com",
    )

    def __post_init__(self) -> None:
        if self.min_download_delay > self.max_download_delay:
            raise ConfigError("media.min_download_delay 不能大于 max_download_delay")
        if self.max_images_per_call < 1:
            raise ConfigError("media.max_images_per_call 必须 >= 1")


@dataclass(frozen=True)
class SummarizeConfig:
    command: str = "spi"
    args: tuple[str, ...] = (
        "-p",
        "--no-tools",
        "--no-session",
        "--no-sandbox",
        "--mode",
        "text",
        "--model",
        "opencode-go/deepseek-v4.1-flash",
    )
    digest_prompt: Path = PROJECT_ROOT / "prompts" / "digest.md"
    chunk_prompt: Path = PROJECT_ROOT / "prompts" / "chunk.md"
    reduce_prompt: Path = PROJECT_ROOT / "prompts" / "reduce.md"
    max_chars_per_call: int = 150_000
    timeout_seconds: float = 900.0

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.command, *self.args)


@dataclass(frozen=True)
class FeishuConfig:
    enabled: bool = True
    credentials_file: Path = Path.home() / ".pi/agent/extensions/pi-channel/config.json"
    domain: str = "feishu"
    msg_type: str = "interactive"
    receiver_type: str = "chat_id"
    receiver_value: str | None = None
    max_chars_per_message: int = 6000
    send_interval_seconds: float = 1.5

    @property
    def base_url(self) -> str:
        try:
            return FEISHU_BASE_URLS[self.domain]
        except KeyError:
            raise ConfigError(
                f"feishu.domain 只能是 {'/'.join(FEISHU_BASE_URLS)}，当前为 {self.domain!r}"
            ) from None


@dataclass(frozen=True)
class Channel:
    channel_id: int
    name: str
    guild_id: int | None = None


@dataclass(frozen=True)
class Config:
    path: Path
    timezone: str
    database: Path
    output_dir: Path
    media_dir: Path
    http: HttpConfig
    store: StoreConfig
    alerts: AlertsConfig
    discord: DiscordConfig
    fetch: FetchPolicy
    media: MediaConfig
    summarize: SummarizeConfig
    feishu: FeishuConfig
    channels: tuple[Channel, ...]
    logs: LogsConfig = field(default_factory=LogsConfig)


def _build_alerts(raw: dict[str, Any]) -> AlertsConfig:
    _check_keys(raw, {f.name for f in fields(AlertsConfig)}, "alerts")
    return AlertsConfig(**raw)


def _build_logs(raw: dict[str, Any]) -> LogsConfig:
    _check_keys(raw, {f.name for f in fields(LogsConfig)}, "logs")
    return LogsConfig(**raw)


def _build_store(raw: dict[str, Any]) -> StoreConfig:
    _check_keys(raw, {f.name for f in fields(StoreConfig)}, "store")
    return StoreConfig(**raw)


def _build_http(raw: dict[str, Any]) -> HttpConfig:
    _check_keys(raw, {f.name for f in fields(HttpConfig)}, "http")
    return HttpConfig(**raw)


def _build_discord(raw: dict[str, Any]) -> DiscordConfig:
    _check_keys(raw, {f.name for f in fields(DiscordConfig)}, "discord")
    return DiscordConfig(**raw)


def _build_fetch(raw: dict[str, Any]) -> FetchPolicy:
    _check_keys(raw, {f.name for f in fields(FetchPolicy)}, "fetch")
    return FetchPolicy(**raw)


def _build_media(raw: dict[str, Any]) -> MediaConfig:
    _check_keys(raw, {f.name for f in fields(MediaConfig)}, "media")
    kwargs = dict(raw)
    if "allowed_hosts" in kwargs:
        kwargs["allowed_hosts"] = tuple(kwargs["allowed_hosts"])
    return MediaConfig(**kwargs)


def _build_summarize(raw: dict[str, Any], base: Path) -> SummarizeConfig:
    allowed = {f.name for f in fields(SummarizeConfig)}
    _check_keys(raw, allowed, "summarize")
    kwargs = dict(raw)
    if "args" in kwargs:
        kwargs["args"] = tuple(kwargs["args"])
    for key in ("digest_prompt", "chunk_prompt", "reduce_prompt"):
        if key in kwargs:
            kwargs[key] = _expand(kwargs[key], base)
    return SummarizeConfig(**kwargs)


def _build_feishu(raw: dict[str, Any], base: Path) -> FeishuConfig:
    allowed = {f.name for f in fields(FeishuConfig)}
    _check_keys(raw, allowed, "feishu")
    kwargs = dict(raw)
    if "credentials_file" in kwargs:
        kwargs["credentials_file"] = _expand(kwargs["credentials_file"], base)
    if kwargs.get("msg_type") not in {None, "interactive", "text"}:
        raise ConfigError("feishu.msg_type 只能是 interactive 或 text")
    return FeishuConfig(**kwargs)


def _build_channels(raw: list[dict[str, Any]]) -> tuple[Channel, ...]:
    channels: list[Channel] = []
    seen: set[int] = set()
    for index, item in enumerate(raw):
        _check_keys(item, {"channel_id", "name", "guild_id"}, f"channels[{index}]")
        if "channel_id" not in item:
            raise ConfigError(f"[[channels]] 第 {index + 1} 项缺少 channel_id")
        channel_id = int(item["channel_id"])
        if channel_id in seen:
            raise ConfigError(f"[[channels]] 中 channel_id {channel_id} 重复")
        seen.add(channel_id)
        guild_id = item.get("guild_id")
        channels.append(
            Channel(
                channel_id=channel_id,
                name=str(item.get("name") or f"channel-{channel_id}"),
                guild_id=int(guild_id) if guild_id is not None else None,
            )
        )
    return tuple(channels)


def load_config(path: Path | None = None) -> Config:
    config_path = (path or DEFAULT_CONFIG_PATH).resolve()
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在: {config_path}")
    base = config_path.parent
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    channels = _build_channels(raw.get("channels", []))
    return Config(
        path=config_path,
        timezone=str(raw.get("timezone", "Asia/Shanghai")),
        database=_expand(raw.get("database", "./data/messages.db"), base),
        output_dir=_expand(raw.get("output_dir", "./digests"), base),
        media_dir=_expand(raw.get("media_dir", str(TEMP_MEDIA_DIR)), base),
        http=_build_http(raw.get("http", {})),
        store=_build_store(raw.get("store", {})),
        logs=_build_logs(raw.get("logs", {})),
        alerts=_build_alerts(raw.get("alerts", {})),
        discord=_build_discord(raw.get("discord", {})),
        fetch=_build_fetch(raw.get("fetch", {})),
        media=_build_media(raw.get("media", {})),
        summarize=_build_summarize(raw.get("summarize", {}), base),
        feishu=_build_feishu(raw.get("feishu", {}), base),
        channels=channels,
    )
