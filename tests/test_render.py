from dc_digest.render import (
    RenderPolicy,
    chunk_messages,
    render_message,
    render_transcript,
)
from dc_digest.store import Message

TZ = "Asia/Shanghai"
DAY = 86_400_000


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
