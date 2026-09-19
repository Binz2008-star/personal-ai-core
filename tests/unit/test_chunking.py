"""FixedSizeChunker.

The property that matters most here is not chunk size, it is that
`content[chunk.start:chunk.end] == chunk.text` for every chunk in every
language. Offsets are how a citation gets re-read and checked; an offset that
is off by one turns a verifiable citation into a plausible one.
"""
import pytest

from personal_ai_core.knowledge import FixedSizeChunker

ENGLISH = "First paragraph here.\n\nSecond paragraph here.\n\nThird."
ARABIC = "الفقرة الأولى هنا.\n\nالفقرة الثانية هنا.\n\nالثالثة."
MIXED = "English opening paragraph.\n\nفقرة عربية في المنتصف.\n\nClosing line."


def chunk(content, document, version, **kwargs):
    return FixedSizeChunker(**kwargs).chunk(
        document=document, version=version, content=content
    )


@pytest.mark.parametrize("content", [ENGLISH, ARABIC, MIXED], ids=["en", "ar", "mixed"])
def test_offsets_index_the_source_exactly(content, document, version):
    for produced in chunk(content, document, version, max_chars=30, overlap_chars=5):
        assert content[produced.start : produced.end] == produced.text


@pytest.mark.parametrize("content", [ENGLISH, ARABIC, MIXED], ids=["en", "ar", "mixed"])
def test_chunking_is_deterministic(content, document, version):
    first = chunk(content, document, version, max_chars=30, overlap_chars=5)
    second = chunk(content, document, version, max_chars=30, overlap_chars=5)
    assert [(c.start, c.end, c.text) for c in first] == [
        (c.start, c.end, c.text) for c in second
    ]


def test_ordinals_are_sequential_and_preserve_reading_order(document, version):
    chunks = chunk(ENGLISH, document, version, max_chars=25, overlap_chars=5)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert [c.start for c in chunks] == sorted(c.start for c in chunks)


def test_chunks_carry_the_document_and_version_identity(document, version):
    for produced in chunk(ENGLISH, document, version, max_chars=25, overlap_chars=5):
        assert produced.document_id == document.id
        assert produced.version_id == version.id


def test_paragraphs_are_packed_up_to_the_limit(document, version):
    # Whole content is well under 1000 chars, so it should be one chunk.
    chunks = chunk(ENGLISH, document, version, max_chars=1000)
    assert len(chunks) == 1
    assert chunks[0].text == ENGLISH


def test_a_paragraph_longer_than_the_limit_is_split_with_overlap(document, version):
    content = "x" * 250
    chunks = chunk(content, document, version, max_chars=100, overlap_chars=20)
    assert len(chunks) > 1
    # Overlap means each chunk after the first starts before the previous ended.
    for previous, following in zip(chunks, chunks[1:]):
        assert following.start < previous.end
    # And the union still covers the whole content.
    assert chunks[0].start == 0
    assert chunks[-1].end == len(content)


@pytest.mark.parametrize("content", ["", "   ", "\n\n\n"], ids=["empty", "ws", "blanks"])
def test_blank_content_produces_no_chunks(content, document, version):
    assert chunk(content, document, version) == []


def test_blank_chunks_are_never_emitted(document, version):
    content = "Real text.\n\n\n\n   \n\n\n\nMore real text."
    for produced in chunk(content, document, version, max_chars=20, overlap_chars=5):
        assert produced.text.strip()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chars": 0},
        {"overlap_chars": -1},
        {"max_chars": 10, "overlap_chars": 10},
        {"max_chars": 10, "overlap_chars": 11},
    ],
)
def test_impossible_settings_are_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        FixedSizeChunker(**kwargs)
