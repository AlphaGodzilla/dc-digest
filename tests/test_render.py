from dc_digest.render import (
    RenderPolicy,
    chunk_messages,
    render_message,
    render_transcript,
)
from dc_digest.store import EmbedPreview, Message

TZ = "Asia/Shanghai"
DAY = 86_400_000
START_MS = 1_757_293_200_000  # 2026-09-08 09:00 (Asia/Shanghai)


def _message(message_id: int, ts_ms: int, content: str = "内容", **kwargs: object) -> Message:
    return Message(
        id=message_id,
        channel_id=1,
        author_id=1,
        author="alice",
        ts_ms=ts_ms,
        content=content,
        **kwargs,
    )


def test_render_appends_attachments_and_reply() -> None:
    message = _message(
        1,
        1_700_000_000_000,
        "看一下这个",
        attachments=("report.pdf",),
        reply_author="bob",
    )
    line = render_message(message, TZ, RenderPolicy())
    assert "(回复 bob)" in line
    assert "[附件: report.pdf]" in line
    assert line.startswith("[")
    assert "alice:" in line


def test_render_collapses_newlines_and_truncates() -> None:
    message = _message(1, 1_700_000_000_000, "第一行\n\n第二行  第三行")
    assert render_message(message, TZ, RenderPolicy()) .count("\n") == 0
    long_message = _message(2, 1_700_000_000_000, "x" * 50)
    assert render_message(long_message, TZ, RenderPolicy(max_content_chars=10)).endswith("…")


def test_transcript_has_day_separators() -> None:
    messages = [_message(1, 0), _message(2, DAY)]
    transcript = render_transcript(messages, TZ, RenderPolicy())
    assert transcript.count("--- ") == 2


def test_chunking_respects_budget() -> None:
    messages = [_message(index, index * 60_000, "内容" * 50) for index in range(1, 41)]
    chunks = chunk_messages(messages, TZ, RenderPolicy(), max_chars=1_000)
    assert len(chunks) > 1
    assert sum(chunk.message_count for chunk in chunks) == 40
    assert all(len(chunk.text) <= 1_200 for chunk in chunks)


def test_empty_input_gives_no_chunks() -> None:
    assert chunk_messages([], TZ, RenderPolicy(), max_chars=1_000) == []


def test_external_links_rendered_as_text() -> None:
    message = _message(1, 1_700_000_000_000, "看这个 https://x.com/a/status/1")
    line = render_message(
        message, TZ, RenderPolicy(), (), ("https://evil.example.com/x.png",)
    )
    assert "[外部链接: https://evil.example.com/x.png]" in line


def test_link_suffix_dedupes_and_caps() -> None:
    message = _message(1, 1_700_000_000_000, "多链接")
    links = [f"https://e.example.com/{index}" for index in range(5)]
    links.insert(0, links[0])
    line = render_message(message, TZ, RenderPolicy(), (), links)
    assert line.count("https://e.example.com/0") == 1
    assert "另有 2 个未列出" in line


def test_chunking_carries_links_by_message() -> None:
    messages = [_message(1, 0), _message(2, DAY)]
    chunks = chunk_messages(
        messages,
        TZ,
        RenderPolicy(),
        max_chars=10_000,
        links_by_message={2: ["https://evil.example.com/a.png"]},
    )
    assert "外部链接" not in chunks[0].text.split("---")[1]
    assert "[外部链接: https://evil.example.com/a.png]" in chunks[0].text


def _x_embed_message() -> Message:
    return Message(
        id=3,
        channel_id=1,
        author_id=9,
        author="MM_ULTRA",
        ts_ms=START_MS,
        content="https://x.com/SUOHA_AI/status/2121839303605027147",
        embeds=(
            EmbedPreview(
                url="https://twitter.com/SUOHA_AI/status/2121839303605027147",
                description="这个老哥是早期 $BNB 的前几大户",
                site="X",
                author="梭哈.AI (@SUOHA_AI)",
            ),
        ),
    )


def test_render_includes_discord_preview_line() -> None:
    line = render_message(_x_embed_message(), TZ, RenderPolicy())
    assert "https://x.com/SUOHA_AI/status/2121839303605027147" in line
    assert "[链接预览 X · 梭哈.AI (@SUOHA_AI)]" in line
    assert "早期 $BNB 的前几大户" in line


def test_render_truncates_long_preview() -> None:
    long = Message(
        id=4,
        channel_id=1,
        author_id=9,
        author="a",
        ts_ms=START_MS,
        content="",
        embeds=(EmbedPreview(description="字" * 900),),
    )
    line = render_message(long, TZ, RenderPolicy(max_preview_chars=50))
    assert "…" in line
    assert line.count("字") == 50


def test_render_skips_empty_preview() -> None:
    empty = Message(
        id=5, channel_id=1, author_id=9, author="a", ts_ms=START_MS, content="hi",
        embeds=(EmbedPreview(),),
    )
    assert "链接预览" not in render_message(empty, TZ, RenderPolicy())
