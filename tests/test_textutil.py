"""Conversioni raw ⇄ segmenti ⇄ markdown (`trascrivi.textutil`)."""

from __future__ import annotations

from trascrivi.textutil import (
    build_markdown,
    fmt_ts,
    parse_raw,
    segments_to_text,
    text_to_segments,
)


# ── parse_raw ────────────────────────────────────────────────────────────────
def test_parse_raw_reads_both_timestamp_formats():
    raw = "[00:05] Hello there\n[01:10] Second line\n[1:00:00] Late line\n"
    segs = parse_raw(raw)

    assert [s["start"] for s in segs] == [5, 70, 3600]
    assert [s["text"] for s in segs] == ["Hello there", "Second line", "Late line"]
    # `end` di un segmento = start del successivo; l'ultimo resta aperto sul suo start.
    assert [s["end"] for s in segs] == [70, 3600, 3600]
    # offset progressivi nel testo pulito (lunghezza + "\n")
    assert [s["offset"] for s in segs] == [0, 12, 24]
    assert all(s["ts"] is True for s in segs)


def test_parse_raw_appends_lines_without_timestamp_to_previous_segment():
    raw = "[00:05] First part\nsecond part\n\nthird part\n"
    segs = parse_raw(raw)

    assert len(segs) == 1
    assert segs[0]["text"] == "First part\nsecond part\nthird part"
    assert segs[0]["start"] == 5
    assert segs[0]["end"] == 5
    assert segs[0]["offset"] == 0


def test_parse_raw_leading_text_is_merged_into_first_timestamped_line():
    segs = parse_raw("intro line\n[00:10] real line\n")

    assert len(segs) == 1
    assert segs[0]["text"] == "intro line real line"
    assert segs[0]["start"] == 10


def test_parse_raw_without_any_timestamp_keeps_text_visible():
    segs = parse_raw("just some text\nmore text\n")

    assert len(segs) == 1
    assert segs[0]["text"] == "just some text more text"
    assert segs[0]["start"] == 0.0
    assert segs[0]["ts"] is False


def test_parse_raw_empty_input():
    assert parse_raw("") == []
    assert parse_raw("\n\n   \n") == []


# ── text_to_segments / segments_to_text ──────────────────────────────────────
def test_text_to_segments_roundtrip():
    text = "prima riga\nseconda riga\nterza riga"
    segs = text_to_segments(text)

    assert [s["text"] for s in segs] == ["prima riga", "seconda riga", "terza riga"]
    assert segments_to_text(segs) == text
    assert all(s["ts"] is False for s in segs)


def test_text_to_segments_honours_inline_timestamp():
    segs = text_to_segments("[02:30] Ciao\nafter")

    assert segs[0]["start"] == 150.0
    assert segs[0]["text"] == "Ciao"
    assert segs[0]["ts"] is True
    assert segs[1]["text"] == "after"


def test_text_to_segments_keeps_previous_starts_when_line_count_matches():
    previous = [
        {"start": 0.0, "end": 5.0, "text": "a"},
        {"start": 5.0, "end": 10.0, "text": "b"},
        {"start": 10.0, "end": 15.0, "text": "c"},
    ]
    segs = text_to_segments("x\ny\nz", previous)

    assert [s["start"] for s in segs] == [0.0, 5.0, 10.0]
    assert [s["end"] for s in segs] == [5.0, 10.0, 15.0]


def test_text_to_segments_interpolates_when_lines_are_added():
    previous = [{"start": 0.0, "end": 300.0, "text": "a"}]
    segs = text_to_segments("l1\nl2\nl3\nl4", previous)

    # Le righe in eccesso si distribuiscono sulla durata nota invece di sparire.
    assert [s["start"] for s in segs] == [0.0, 75.0, 150.0, 225.0]
    assert [s["end"] for s in segs] == [75.0, 150.0, 225.0, 300.0]


def test_text_to_segments_skips_blank_lines():
    segs = text_to_segments("a\n\n   \nb")

    assert [s["text"] for s in segs] == ["a", "b"]


def test_fmt_ts_and_segments_to_text_with_timestamps():
    assert fmt_ts(0) == "00:00"
    assert fmt_ts(65) == "01:05"
    assert fmt_ts(3661) == "01:01:01"
    assert fmt_ts(-3) == "00:00"

    segs = [{"start": 0.0, "text": "a"}, {"start": 65.0, "text": "b"}, {"start": 3661.0, "text": "c"}]
    assert segments_to_text(segs, with_ts=True) == "[00:00] a\n[01:05] b\n[01:01:01] c"
    assert segments_to_text(segs) == "a\nb\nc"


# ── build_markdown ───────────────────────────────────────────────────────────
def test_build_markdown_has_title_headings_and_glossary():
    segs = [{"start": 0.0, "text": "primo"}, {"start": 310.0, "text": "secondo"}]
    md = build_markdown(
        "Lezione 1",
        {
            "course": "Analisi 1",
            "date": "2026-01-01",
            "language": "it",
            "model": "large-v3",
            "duration": 310,
        },
        segs,
        glossary=[
            {"find": "PyTorch", "replace": "PyTorch"},
            {"find": "derivata", "note": "limite del rapporto incrementale"},
        ],
    )

    assert md.startswith("# Lezione 1\n")
    assert "**Course:** Analisi 1" in md
    assert "**Date:** 2026-01-01" in md
    assert "**Language:** IT" in md
    assert "**Model:** large-v3" in md
    assert "**Duration:** 05:10" in md
    # Un heading ogni 300 secondi.
    assert "## 00:00" in md
    assert "## 05:00" in md
    assert md.index("## 00:00") < md.index("primo") < md.index("## 05:00") < md.index("secondo")
    # Sezione glossario: usa `replace`, in mancanza la `note`.
    assert "## Glossary" in md
    assert "- **PyTorch** \u2014 PyTorch" in md
    assert "- **derivata** \u2014 limite del rapporto incrementale" in md


def test_build_markdown_without_optional_metadata():
    md = build_markdown("Solo titolo", {}, [{"start": 0.0, "text": "ciao"}])

    assert md.startswith("# Solo titolo\n")
    assert "**Course:**" not in md
    assert "## 00:00" in md
    assert "Glossary" not in md
    assert md.endswith("\n")
