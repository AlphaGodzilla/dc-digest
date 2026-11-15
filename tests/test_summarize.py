from pathlib import Path

import pytest

from dc_digest.config import SummarizeConfig
from dc_digest.render import Chunk
from dc_digest.summarize import (
    EMPTY_IMAGES,
    ImageInput,
    SummarizeError,
    _manifest,
    _select,
    build_argv,
    call_summarizer,
)


def _image(index: int) -> ImageInput:
    return ImageInput(
        index=index,
        path=Path(f"/tmp/media/{index}.png"),
        manifest_line=f"图片 {index}：#dev 10:0{index} alice 发的 {index}.png",
    )


def test_build_argv_appends_images_as_at_paths() -> None:
    config = SummarizeConfig()
    images = [_image(1), _image(2)]
    argv = build_argv(config, images)
    assert argv[: len(config.argv)] == config.argv
    assert argv[-2:] == ("@/tmp/media/1.png", "@/tmp/media/2.png")
    assert "--no-sandbox" in argv


def test_build_argv_without_images_equals_base() -> None:
    config = SummarizeConfig()
    assert build_argv(config) == config.argv


def test_manifest_lists_images_in_order() -> None:
    text = _manifest([_image(1), _image(2)])
    assert text.splitlines()[0].startswith("图片 1：")
    assert text.splitlines()[1].startswith("图片 2：")


def test_manifest_empty_state() -> None:
    assert _manifest([]) == EMPTY_IMAGES


def test_select_caps_images_per_call() -> None:
    images = {index: _image(index) for index in range(1, 6)}
    selected = _select(images, [1, 2, 3, 4, 5], 3)
    assert [image.index for image in selected] == [1, 2, 3]


def test_select_ignores_unknown_indices() -> None:
    images = {1: _image(1)}
    assert [image.index for image in _select(images, [1, 9], 8)] == [1]


def test_chunk_carries_image_indices() -> None:
    chunk = Chunk(label="2026-09-08 10:00 ~ 11:00", text="x", message_count=3, image_indices=(1, 4))
    assert chunk.image_indices == (1, 4)


def test_missing_command_becomes_summarize_error(monkeypatch) -> None:
    from dc_digest.config import SummarizeConfig

    config = SummarizeConfig(
        command="dc-digest-command-that-does-not-exist",
        args=("-p",),
        timeout_seconds=5.0,
    )
    with pytest.raises(SummarizeError) as excinfo:
        call_summarizer(config, "prompt")
    message = str(excinfo.value)
    assert "找不到摘要命令" in message
    assert "PATH" in message


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _config_for_retry() -> SummarizeConfig:
    return SummarizeConfig(command="spi", args=("-p",), timeout_seconds=5.0)


def test_empty_output_is_retried(monkeypatch) -> None:
    """模型偶发返回空内容（退出码 0）时重试，而不是直接失败。"""
    calls: list[int] = []
    replies = [_FakeCompleted(stdout=""), _FakeCompleted(stdout="简报正文")]

    def fake_run(*_args, **_kwargs):
        calls.append(1)
        return replies[len(calls) - 1]

    monkeypatch.setattr("dc_digest.summarize.subprocess.run", fake_run)
    monkeypatch.setattr("dc_digest.summarize.time.sleep", lambda _s: None)
    assert call_summarizer(_config_for_retry(), "prompt") == "简报正文"
    assert len(calls) == 2


def test_empty_output_eventually_fails_with_reason(monkeypatch) -> None:
    calls: list[int] = []

    def fake_run(*_args, **_kwargs):
        calls.append(1)
        return _FakeCompleted(stdout="  \n ", stderr="provider returned empty")

    monkeypatch.setattr("dc_digest.summarize.subprocess.run", fake_run)
    monkeypatch.setattr("dc_digest.summarize.time.sleep", lambda _s: None)
    with pytest.raises(SummarizeError) as excinfo:
        call_summarizer(_config_for_retry(), "prompt")
    assert len(calls) == 3
    assert "连续 3 次没有输出" in str(excinfo.value)
    assert "provider returned empty" in str(excinfo.value)


def test_nonzero_exit_is_not_retried(monkeypatch) -> None:
    calls: list[int] = []

    def fake_run(*_args, **_kwargs):
        calls.append(1)
        return _FakeCompleted(returncode=2, stderr="boom")

    monkeypatch.setattr("dc_digest.summarize.subprocess.run", fake_run)
    with pytest.raises(SummarizeError):
        call_summarizer(_config_for_retry(), "prompt")
    assert len(calls) == 1
