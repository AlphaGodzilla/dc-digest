from pathlib import Path

import pytest

from dc_digest.config import ConfigError, load_config

CONFIG_TEMPLATE = """
timezone = "Asia/Shanghai"
database = "{database}"
output_dir = "{output_dir}"

[discord]
auth = "{auth}"

[feishu]
msg_type = "{msg_type}"

[[channels]]
channel_id = "111"
name = "#a"

[[channels]]
channel_id = "222"
name = "#b"
"""


def _write_config(tmp_path: Path, **overrides: str) -> Path:
    values = {
        "database": "./data/messages.db",
        "output_dir": "./digests",
        "auth": "user",
        "msg_type": "interactive",
    }
    values.update(overrides)
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TEMPLATE.format(**values), encoding="utf-8")
    return config_path


def test_relative_paths_resolve_against_config_directory(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    assert config.database == tmp_path.resolve() / "data" / "messages.db"
    assert config.output_dir == tmp_path.resolve() / "digests"


def test_absolute_paths_are_kept(tmp_path: Path) -> None:
    config = load_config(
        _write_config(tmp_path, database="/var/tmp/dc.db", output_dir="/var/tmp/digests")
    )
    assert config.database == Path("/var/tmp/dc.db")
    assert config.output_dir == Path("/var/tmp/digests")


def test_channels_parsed_in_order(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    assert [channel.channel_id for channel in config.channels] == [111, 222]
    assert [channel.name for channel in config.channels] == ["#a", "#b"]


def test_duplicate_channel_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[[channels]]\nchannel_id = "111"\n[[channels]]\nchannel_id = "111"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[fetch]\npage_sizes = 50\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_invalid_auth_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, auth="self"))


def test_invalid_msg_type_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, msg_type="card"))


def test_missing_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.toml")


def test_media_allowed_hosts_parsed_as_tuple(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[media]\nallowed_hosts = ["discordapp.com", "example.org"]\n',
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.media.allowed_hosts == ("discordapp.com", "example.org")


def test_example_config_is_loadable(tmp_path: Path) -> None:
    """config.example.toml 必须能直接加载，避免示例写错字段。"""
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    config = load_config(example)
    assert config.channels[0].name == "#频道名"
    assert config.media.allowed_hosts == ("discordapp.com", "discordapp.net", "discord.com")


def test_logs_defaults_keep_log_bounded(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    assert config.logs.max_bytes == 2_000_000
    assert config.logs.keep_bytes == 200_000


def test_logs_rejects_keep_not_smaller_than_max(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[logs]\nmax_bytes = 1000\nkeep_bytes = 1000\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_logs_threshold_is_configurable(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n[logs]\nmax_bytes = 500000\nkeep_bytes = 50000\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert (config.logs.max_bytes, config.logs.keep_bytes) == (500000, 50000)
