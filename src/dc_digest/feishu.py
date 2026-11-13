"""飞书推送。

复用 pi-channel 扩展的应用凭证（appId / appSecret），用 tenant_access_token 调用
im/v1/messages 把简报作为卡片消息发出；长文按标题边界拆成多条。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import httpx

from .config import FeishuConfig

LOGGER = logging.getLogger("dc_digest")


class FeishuError(RuntimeError):
    """飞书接口或凭证异常。"""


@dataclass(frozen=True)
class Credentials:
    app_id: str
    app_secret: str
    default_receiver: str | None


def load_credentials(path: Path) -> Credentials:
    if not path.exists():
        raise FeishuError(f"飞书凭证文件不存在: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    section = payload.get("feishu") or {}
    app_id = section.get("appId")
    app_secret = section.get("appSecret")
    if not app_id or not app_secret:
        raise FeishuError(f"{path} 缺少 feishu.appId 或 feishu.appSecret")
    receiver = (section.get("defaultReceiver") or {}).get("value")
    return Credentials(app_id=app_id, app_secret=app_secret, default_receiver=receiver)


def split_markdown(text: str, limit: int) -> list[str]:
    """按行累积切分，优先在标题行之前断开，超长单行按 limit 硬切。"""
    if limit <= 0:
        raise FeishuError("feishu.max_chars_per_message 必须为正数")
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    buffer: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        if buffer:
            parts.append("".join(buffer))
            buffer = []
            size = 0

    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            flush()
            for start in range(0, len(line), limit):
                parts.append(line[start : start + limit])
            continue
        if size + len(line) > limit:
            flush()
        buffer.append(line)
        size += len(line)
    flush()
    return parts


def build_card(title: str, content: str) -> dict:
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }


class FeishuSender:
    def __init__(
        self,
        config: FeishuConfig,
        *,
        client: httpx.Client | None = None,
        proxy: str | None = None,
        dry_run: bool = False,
        sleeper=time.sleep,
    ) -> None:
        self._config = config
        self._dry_run = dry_run
        self._sleeper = sleeper
        self._client = client or httpx.Client(
            base_url=config.base_url, timeout=30.0, proxy=proxy or None
        )
        self._owns_client = client is None
        self._token: str | None = None
        self._receiver: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _resolve_receiver(self) -> str:
        if self._receiver:
            return self._receiver
        configured = self._config.receiver_value
        if configured:
            self._receiver = configured
            return configured
        credentials = load_credentials(self._config.credentials_file)
        if not credentials.default_receiver:
            raise FeishuError(
                "没有可用的飞书接收方：请在 config.toml 的 [feishu] 填 receiver_value，"
                f"或在 {self._config.credentials_file} 里配置 feishu.defaultReceiver.value"
            )
        self._receiver = credentials.default_receiver
        return self._receiver

    def _tenant_token(self) -> str:
        if self._token:
            return self._token
        credentials = load_credentials(self._config.credentials_file)
        response = self._client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": credentials.app_id, "app_secret": credentials.app_secret},
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise FeishuError(
                f"获取 tenant_access_token 失败: code={payload.get('code')} msg={payload.get('msg')}"
            )
        self._token = payload["tenant_access_token"]
        return self._token

    def _post_part(self, title: str, content: str) -> None:
        if self._config.msg_type == "text":
            body: dict = {"text": content}
            msg_type = "text"
        else:
            body = build_card(title, content)
            msg_type = "interactive"
        response = self._client.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": self._config.receiver_type},
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            json={
                "receive_id": self._resolve_receiver(),
                "msg_type": msg_type,
                "content": json.dumps(body, ensure_ascii=False),
            },
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise FeishuError(
                f"发送飞书消息失败: code={payload.get('code')} msg={payload.get('msg')}"
            )

    def send(self, *, title: str, markdown: str) -> list[str]:
        parts = split_markdown(markdown, self._config.max_chars_per_message)
        if self._dry_run:
            LOGGER.info("dry-run：将发送 %d 条飞书卡片，共 %d 字符", len(parts), len(markdown))
            return parts
        for index, part in enumerate(parts, start=1):
            part_title = title if len(parts) == 1 else f"{title}（{index}/{len(parts)}）"
            self._post_part(part_title, part)
            LOGGER.info("已发送飞书消息 %d/%d", index, len(parts))
            if index < len(parts):
                self._sleeper(self._config.send_interval_seconds)
        return parts
