import inspect
from pathlib import Path

import httpx
import pytest

import dc_digest.discord_api as api_module
from dc_digest.discord_api import (
    AttachmentTooLarge,
    DiscordClient,
    DiscordError,
    DiscordRateLimited,
    ms_from_snowflake,
    snowflake_from_ms,
)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _client(handler) -> DiscordClient:
    return DiscordClient(
        "fake-token", auth="user", transport=httpx.MockTransport(handler)
    )


def test_snowflake_roundtrip() -> None:
    ts = 1_700_000_000_000
    assert ms_from_snowflake(snowflake_from_ms(ts)) == ts


def test_write_methods_are_rejected() -> None:
    client = _client(lambda request: httpx.Response(200, json={}))
    for method in WRITE_METHODS:
        with pytest.raises(DiscordError, match="只读模式"):
            client._request(method, "/channels/1/messages")
    client.close()


def test_source_contains_no_write_calls() -> None:
    """源码级保险：只读工具不应出现任何写请求方法。"""
    source = Path(inspect.getsourcefile(api_module)).read_text(encoding="utf-8")
    for method in WRITE_METHODS:
        assert f'"{method}"' not in source, f"discord_api.py 不应出现 {method} 请求"
    assert "READ_ONLY_METHODS" in source


def test_status_codes_map_to_readable_errors() -> None:
    cases = {
        401: "token 无效",
        403: "无权访问",
        404: "资源不存在",
    }
    for status, fragment in cases.items():
        client = _client(lambda request, status=status: httpx.Response(status, json={}))
        with pytest.raises(DiscordError, match=fragment) as excinfo:
            client._get("/users/@me")
        assert excinfo.value.status == status
        client.close()


def test_rate_limit_carries_retry_after() -> None:
    client = _client(
        lambda request: httpx.Response(429, json={"retry_after": 12.5})
    )
    with pytest.raises(DiscordRateLimited) as excinfo:
        client._get("/users/@me")
    assert excinfo.value.retry_after == 12.5
    client.close()


def test_network_error_is_wrapped_with_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("403 Forbidden")

    client = _client(handler)
    with pytest.raises(DiscordError, match="网络请求失败") as excinfo:
        client._get("/users/@me")
    assert "代理" in str(excinfo.value)
    client.close()


def test_messages_pass_before_and_limit() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    client = _client(handler)
    client.messages(42, before_id=123456789012345678, limit=50)
    assert seen == {"limit": "50", "before": "123456789012345678"}
    client.close()


def test_download_enforces_size_cap() -> None:
    client = _client(lambda request: httpx.Response(200, content=b"x" * 200))
    with pytest.raises(AttachmentTooLarge):
        client.download("https://cdn.discordapp.com/a.png", max_bytes=100)
    client.close()


def test_download_returns_bytes_and_content_type() -> None:
    client = _client(
        lambda request: httpx.Response(
            200, content=b"PNG", headers={"content-type": "image/png"}
        )
    )
    content, content_type = client.download(
        "https://cdn.discordapp.com/a.png", max_bytes=1_000
    )
    assert content == b"PNG"
    assert content_type == "image/png"
    client.close()


def test_headers_use_bare_token_for_user_account() -> None:
    from dc_digest.discord_api import build_headers

    headers = build_headers("abc123", "user")
    assert headers["Authorization"] == "abc123"
    assert not headers["Authorization"].startswith("Bot ")


def test_headers_prefix_bot_token() -> None:
    from dc_digest.discord_api import build_headers

    assert build_headers("abc123", "bot")["Authorization"] == "Bot abc123"


def test_headers_do_not_spoof_client_fingerprint() -> None:
    from dc_digest.discord_api import build_headers

    headers = build_headers("abc123", "user")
    assert headers["User-Agent"].startswith("dc-digest/")
    assert "Mozilla" not in headers["User-Agent"]
    assert "Content-Type" not in headers  # GET 没有请求体
    for spoofed in ("X-Super-Properties", "X-Discord-Locale", "Origin", "Referer"):
        assert spoofed not in headers


def test_headers_reject_unknown_auth() -> None:
    from dc_digest.discord_api import build_headers

    with pytest.raises(DiscordError):
        build_headers("abc123", "selfbot")
    with pytest.raises(DiscordError):
        build_headers("", "user")
