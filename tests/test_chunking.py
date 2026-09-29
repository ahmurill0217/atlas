from brain.chunking import chunk_text, count_tokens
from brain.ingestion import parse_text

TEXT = """# Title

First paragraph sentence one. It was approved by the U.S. Food and Drug Administration in 2021.

Second paragraph is here. It has two sentences!

Third paragraph, alone."""


def test_offsets_point_back_into_source():
    for c in chunk_text(TEXT, max_tokens=12):
        assert TEXT[c.start : c.end] == c.text


def test_respects_token_budget_without_cutting_sentences():
    chunks = chunk_text(TEXT, max_tokens=20)
    assert len(chunks) > 1
    for c in chunks:
        # A single sentence may exceed the budget, but packed chunks may not.
        assert c.token_count <= 20 or "." not in c.text.rstrip(".")
        assert c.text.rstrip().endswith((".", "!", "Title"))


def test_abbreviation_does_not_split_sentence():
    chunks = chunk_text(TEXT, max_tokens=12)
    assert any("U.S. Food and Drug Administration" in c.text for c in chunks)


def test_large_budget_gives_single_chunk_and_indices_are_sequential():
    assert len(chunk_text(TEXT, max_tokens=1000)) == 1
    assert [c.index for c in chunk_text(TEXT, 12)] == list(range(len(chunk_text(TEXT, 12))))


def test_deterministic():
    assert chunk_text(TEXT, 15) == chunk_text(TEXT, 15)


def test_overlap_repeats_trailing_sentences():
    no_overlap = chunk_text(TEXT, max_tokens=15)
    overlap = chunk_text(TEXT, max_tokens=15, overlap_sentences=1)
    assert len(overlap) >= len(no_overlap)
    assert overlap[1].start < overlap[0].end


def test_parser_normalizes_and_hashes():
    a = parse_text("# Heading\r\n\r\n\r\n\r\nBody  \n", "a.md")
    b = parse_text("# Heading\n\nBody", "a.md")
    assert a.text == b.text and a.content_hash == b.content_hash
    assert a.title == "Heading"
    assert count_tokens("hello world") >= 2


def test_pdf_text_cleanup_and_title_fallback():
    from brain.ingestion.parser import _first_line, clean_pdf_text

    assert clean_pdf_text("Trans-\nformer\tmodels use  atten-\ntion") == "Transformer models use attention"
    assert _first_line("Published as a conference paper at ICLR 2015\nNeural Machine Translation") == "Neural Machine Translation"


def test_nul_bytes_are_stripped():
    assert "\x00" not in parse_text("a\x00b", "x.txt").text


def test_special_token_strings_in_text_do_not_raise():
    text = "The prompt ends with <|endofprompt|> and <|endoftext|>. Next sentence."
    assert count_tokens(text) > 0 and chunk_text(text, 50)


def test_pdf_small_caps_are_repaired():
    from brain.ingestion.parser import clean_pdf_text

    assert clean_pdf_text("our R/e.sc /t.sc /r.sc/o.sc model and B/e.sc /r.sc/t.sc") == "our RETRO model and BERT"
