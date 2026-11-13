from pathlib import Path

from dc_digest import cli
from dc_digest.alerts import alert_hint, format_degraded, format_failure
from dc_digest.discord_api import DiscordError
from dc_digest.feishu import FeishuError
from dc_digest.summarize import SummarizeError
from dc_digest.timewin import TimeWindowError

CONFIG_TEMPLATE = """
timezone = "Asia/Shanghai"
database = "{database}"
output_dir = "{output_dir}"

[discord]
auth = "user"
token_keychain_service = "dc-digest-test-definitely-missing"
token_env = "DC_DIGEST_TEST_MISSING_TOKEN"

[feishu]
enabled = true
credentials_file = "{credentials}"

[[channels]]
channel_id = "1"
name = "#dev"
"""


def _config_file(tmp_path: Path) -> Path:
    (tmp_path / "creds.json").write_text(
        '{"feishu": {"appId": "a", "appSecret": "b", "defaultReceiver": {"value": "c"}}}',
        encoding="utf-8",
    )
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG_TEMPLATE.format(
            database=str(tmp_path / "messages.db"),
            output_dir=str(tmp_path / "digests"),
            credentials=str(tmp_path / "creds.json"),
        ),
        encoding="utf-8",
    )
    return path


def test_hint_covers_ban_and_credentials() -> None:
    assert "封禁" in alert_hint(DiscordError("token 无效或已过期（401）", 401))
    assert "权限" in alert_hint(DiscordError("无权访问该资源（403）", 403))
    assert "channel_id" in alert_hint(DiscordError("资源不存在（404）", 404))
    assert "代理" in alert_hint(DiscordError("网络请求失败: https://discord.com（ProxyError）"))
    assert "飞书" in alert_hint(FeishuError("发送飞书消息失败"))
    assert "summarize" in alert_hint(SummarizeError("spi 没有输出"))
    assert "时间参数" in alert_hint(TimeWindowError("无法解析时间"))
    assert "未归类的异常" in alert_hint(ValueError("boom"))


def test_format_failure_lists_root_cause_and_hint() -> None:
    body = format_failure(
        command="fetch",
        window_text="2026-11-13 09:00 ~ 16:00",
        exc=DiscordError("token 无效或已过期（401）", 401),
        now_text="2026-11-13 17:00:00",
    )
    assert "**命令**：fetch" in body
    assert "**窗口**：2026-11-13 09:00 ~ 16:00" in body
    assert "DiscordError" in body
    assert "封禁" in body


def test_format_degraded_mentions_resume() -> None:
    body = format_degraded(
        command="run",
        window_text="2026-11-13 09:00 ~ 16:00",
        reason="触发限流，退避 45 分钟后再续传",
        incomplete=["#dev"],
        now_text="2026-11-13 17:00:00",
    )
    assert "触发限流" in body
    assert "#dev" in body
    assert "续传" in body


def test_main_sends_alert_on_credential_failure(tmp_path: Path, monkeypatch) -> None:
    config_path = _config_file(tmp_path)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli, "_send_alert", lambda config, *, title, body: sent.append((title, body))
    )
    assert cli.main(["fetch", "--config", str(config_path)]) == 2
    assert sent, "失败时必须发出提醒"
    title, body = sent[0]
    assert "失败" in title and "fetch" in title
    assert "token" in body


def test_main_skips_alert_with_no_alert_flag(tmp_path: Path, monkeypatch) -> None:
    config_path = _config_file(tmp_path)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli, "_send_alert", lambda config, *, title, body: sent.append((title, body))
    )
    assert cli.main(["--no-alert", "fetch", "--config", str(config_path)]) == 2
    assert sent == []


def test_alert_disabled_in_config(tmp_path: Path) -> None:
    config_path = _config_file(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace(
        "[feishu]", "[alerts]\nenabled = false\n\n[feishu]"
    )
    config_path.write_text(text, encoding="utf-8")

    sent: list[tuple[str, str]] = []
    original = cli.FeishuSender

    class Recorder:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("alerts.enabled=false 时不应构造发送器")

    cli.FeishuSender = Recorder  # type: ignore[assignment]
    try:
        cli._send_alert(cli.load_config(config_path), title="t", body="b")
    finally:
        cli.FeishuSender = original  # type: ignore[assignment]
    assert sent == []
