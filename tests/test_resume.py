"""Resume della trascrizione interrotta (`trascrivi_audio`)."""

from __future__ import annotations

import pytest

import trascrivi_audio  # noqa: F401  (import esplicito: è il modulo sotto test)
from trascrivi_audio import _TS_RE, is_english_only, last_transcribed_seconds, resume_offset


def _write(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_ts_regex_understands_both_formats():
    assert _TS_RE.match("[00:05] testo").groups() == ("00", "05", None)
    assert _TS_RE.match("[1:02:03] testo").groups() == ("1", "02", "03")
    assert _TS_RE.match("nessun timestamp") is None


def test_last_transcribed_seconds_reads_last_timestamp(tmp_path):
    out = _write(tmp_path, "lezione.txt", "[00:05] prima\n[01:10] seconda\n[1:02:03] terza\n")

    assert last_transcribed_seconds(out) == 3723.0


def test_last_transcribed_seconds_ignores_lines_without_timestamp(tmp_path):
    out = _write(tmp_path, "misto.txt", "[00:30] con timestamp\nriga senza timestamp\n")

    assert last_transcribed_seconds(out) == 30.0


def test_last_transcribed_seconds_missing_file(tmp_path):
    assert last_transcribed_seconds(tmp_path / "non-esiste.txt") == 0.0


def test_last_transcribed_seconds_without_timestamps(tmp_path):
    out = _write(tmp_path, "plain.txt", "solo testo\nnessun timestamp\n")

    assert last_transcribed_seconds(out) == 0.0


def test_last_transcribed_seconds_empty_file(tmp_path):
    assert last_transcribed_seconds(_write(tmp_path, "vuoto.txt", "")) == 0.0


def test_resume_offset_applies_two_second_margin(tmp_path):
    out = _write(tmp_path, "part.txt", "[00:30] x\n")

    assert resume_offset(out, True) == 28.0


def test_resume_offset_disabled_returns_zero(tmp_path):
    out = _write(tmp_path, "part.txt", "[00:30] x\n")

    assert resume_offset(out, False) == 0.0


def test_resume_offset_missing_file_returns_zero(tmp_path):
    assert resume_offset(tmp_path / "non-esiste.txt", True) == 0.0


def test_resume_offset_under_margin_never_goes_negative(tmp_path):
    out = _write(tmp_path, "part.txt", "[00:01] x\n")

    assert resume_offset(out, True) == 0.0


def test_resume_offset_without_timestamps_returns_zero(tmp_path):
    out = _write(tmp_path, "part.txt", "solo testo\n")

    assert resume_offset(out, True) == 0.0


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("distil-large-v3", True),
        ("distil-small.en", True),
        ("small.en", True),
        ("large-v3", False),
        ("large-v2", False),
        ("small", False),
        ("medium", False),
    ],
)
def test_is_english_only(model, expected):
    assert is_english_only(model) is expected
