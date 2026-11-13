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
