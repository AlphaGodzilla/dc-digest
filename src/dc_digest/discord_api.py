"""Discord REST 客户端，只读历史消息，不建 Gateway 连接。

用户 token 发送裸 `Authorization: <token>`，机器人 token 发送 `Authorization: Bot <token>`。
机器人账号读取消息正文需要 Dev Portal 开启 Message Content privileged intent。
"""

from __future__ import annotations

import json
from typing import Self

import httpx

API_BASE = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1_420_070_400_000
TEXT_CHANNEL_TYPES = {0: "文字", 5: "公告"}

# 只读工具：除 GET 外的任何方法都直接拒绝
READ_ONLY_METHODS = frozenset({"GET"})
USER_AGENT = "dc-digest/0.1.0"


class DiscordError(RuntimeError):
    """Discord 返回不可恢复的错误。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DiscordRateLimited(DiscordError):
    """收到 429，需要等待后重试或结束本轮运行。"""

    def __init__(self, path: str, retry_after: float) -> None:
        super().__init__(f"接口限流 {path}，retry_after={retry_after:.1f}s", 429)
        self.retry_after = retry_after


class AttachmentTooLarge(DiscordError):
    """附件体积超过上限，跳过而不是截断。"""

    def __init__(self, limit: int) -> None:
        super().__init__(f"附件超过 {limit} 字节上限")
        self.limit = limit


def _network_hint(target: str, exc: Exception) -> str:
    return (
        f"网络请求失败: {target}（{type(exc).__name__}: {exc}）\n"
        "排查顺序：代理是否放行 discord.com 与 cdn.discordapp.com、"
        "NO_PROXY 是否误包含 discord 域名、本机是否能直连 443"
    )


def snowflake_from_ms(ms: int) -> int:
    """把毫秒时间戳换算成同刻的 snowflake 上界。"""
    return (ms - DISCORD_EPOCH_MS) << 22


def ms_from_snowflake(value: int) -> int:
    """把 snowflake 换算回毫秒时间戳。"""
    return (value >> 22) + DISCORD_EPOCH_MS


def build_headers(token: str, auth: str) -> dict[str, str]:
    """构造 REST 请求头。

    用户 token 用裸 `Authorization`，机器人 token 加 `Bot ` 前缀——这是 Discord 区分的唯一方式，
    加错前缀会直接 401。GET 请求不带 `Content-Type`（没有请求体）。
    `User-Agent` 如实标注为本工具，不伪装浏览器；也不伪造 `X-Super-Properties` 等客户端指纹，
    因为伪造一套与真实客户端版本不一致的指纹比不发送更容易被识别。
    """
    if auth not in {"user", "bot"}:
        raise DiscordError(f"auth 只能是 user 或 bot，当前为 {auth!r}")
    if not token:
        raise DiscordError("Discord token 为空")
    return {
        "Authorization": f"Bot {token}" if auth == "bot" else token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


class DiscordClient:
    def __init__(
        self,
        token: str,
        *,
        auth: str = "user",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        proxy: str | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=API_BASE,
            timeout=timeout,
            transport=transport,
            proxy=proxy or None,
            headers=build_headers(token, auth),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, object] | None = None) -> object:
        """只读入口：所有 API 调用都经过这里，非 GET 一律拒绝。"""
        return self._request("GET", path, params)

    def _request(
        self, method: str, path: str, params: dict[str, object] | None = None
    ) -> object:
        if method.upper() not in READ_ONLY_METHODS:
            raise DiscordError(
                f"只读模式：拒绝 {method.upper()} 请求（本工具只在群组内读取，不发言）"
            )
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise DiscordError(_network_hint(path, exc)) from exc
        if response.status_code == 429:
            payload = response.json()
            retry_after = float(payload.get("retry_after", 60.0))
            raise DiscordRateLimited(path, retry_after)
        if response.status_code == 401:
            raise DiscordError("token 无效或已过期（401）", 401)
        if response.status_code == 403:
            raise DiscordError(
                "无权访问该资源（403）：确认账号可见该频道并具备「查看频道」「读取历史消息」权限",
                403,
            )
        if response.status_code == 404:
            raise DiscordError(f"资源不存在（404）: {path}", 404)
        if response.status_code >= 400:
            raise DiscordError(
                f"请求失败（HTTP {response.status_code}）: {path} {response.text[:200]}",
                response.status_code,
            )
        return response.json()

    def me(self) -> dict:
        payload = self._get("/users/@me")
        assert isinstance(payload, dict)
        return payload

    def guilds(self) -> list[dict]:
        payload = self._get("/users/@me/guilds", {"limit": 200})
        if not isinstance(payload, list):
            raise DiscordError("/users/@me/guilds 返回结构异常")
        return payload

    def guild_channels(self, guild_id: int) -> list[dict]:
        payload = self._get(f"/guilds/{guild_id}/channels")
        if not isinstance(payload, list):
            raise DiscordError(f"/guilds/{guild_id}/channels 返回结构异常")
        return payload

    def messages(
        self,
        channel_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """倒序取一页消息：返回结果按时间从新到旧。"""
        params: dict[str, object] = {"limit": limit}
        if before_id is not None:
            params["before"] = str(before_id)
        payload = self._get(f"/channels/{channel_id}/messages", params)
        if not isinstance(payload, list):
            raise DiscordError(f"/channels/{channel_id}/messages 返回结构异常")
        return payload

    def download(self, url: str, *, max_bytes: int) -> tuple[bytes, str]:
        """下载 CDN 附件，返回 (内容, content-type)；超过上限即抛 AttachmentTooLarge。"""
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code == 429:
                    retry_after = 60.0
                    try:
                        retry_after = float(
                            json.loads(response.read()).get("retry_after", 60.0)
                        )
                    except (ValueError, TypeError):
                        pass
                    raise DiscordRateLimited(url, retry_after)
                if response.status_code in {401, 403}:
                    raise DiscordError(
                        f"无权下载附件（{response.status_code}）: {url}", response.status_code
                    )
                if response.status_code == 404:
                    raise DiscordError(f"附件已失效（404）: {url}", 404)
                if response.status_code >= 400:
                    raise DiscordError(
                        f"下载失败（HTTP {response.status_code}）: {url}", response.status_code
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise AttachmentTooLarge(max_bytes)
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            raise DiscordError(_network_hint(url, exc)) from exc
