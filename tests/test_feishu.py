import json
from pathlib import Path

import pytest

from dc_digest.config import FeishuConfig
from dc_digest.feishu import (
    Credentials,
    FeishuError,
    build_card,
    load_credentials,
    split_markdown,
)


def test_split_short_text_returns_single_part() -> None:
    assert split_markdown("短文本", 100) == ["短文本"]


def test_split_keeps_parts_under_limit() -> None:
    text = "\n".join(f"## 小节 {index}\n" + "内容" * 40 for index in range(20))
    parts = split_markdown(text, 500)
    assert len(parts) > 1
    assert all(len(part) <= 500 for part in parts)
    assert "".join(parts) == text


def test_split_hard_splits_long_line() -> None:
    parts = split_markdown("x" * 250, 100)
    assert [len(part) for part in parts] == [100, 100, 50]


def test_split_rejects_invalid_limit() -> None:
    with pytest.raises(FeishuError):
        split_markdown("text", 0)


def test_load_credentials_from_pi_channel_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "feishu": {
                    "appId": "cli_example",
                    "appSecret": "secret_example",
                    "defaultReceiver": {"type": "chat_id", "value": "oc_example"},
                }
            }
        ),
        encoding="utf-8",
    )
    credentials = load_credentials(config_path)
    assert credentials == Credentials(
        app_id="cli_example", app_secret="secret_example", default_receiver="oc_example"
    )


def test_load_credentials_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FeishuError):
        load_credentials(tmp_path / "absent.json")


def test_card_structure() -> None:
    card = build_card("标题", "正文")
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "标题"
    assert card["body"]["elements"][0]["content"] == "正文"


def test_feishu_base_url_switch() -> None:
    assert FeishuConfig(domain="feishu").base_url == "https://open.feishu.cn"
    assert FeishuConfig(domain="lark").base_url == "https://open.larksuite.com"
