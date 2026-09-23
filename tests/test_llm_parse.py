"""Parsing delle risposte LLM e cifratura delle API key (`trascrivi.llm`)."""

from __future__ import annotations

from cryptography.fernet import Fernet

from trascrivi import config
from trascrivi.llm import (
    _clean_proposals,
    chunk_segments,
    decrypt_key,
    encrypt_key,
    extract_json,
)


# ── extract_json ─────────────────────────────────────────────────────────────
def test_extract_json_from_fenced_block():
    raw = 'Ecco le correzioni:\n```json\n{"replacements": [{"find": "a", "replace": "b"}]}\n```\n'

    assert extract_json(raw) == {"replacements": [{"find": "a", "replace": "b"}]}


def test_extract_json_from_plain_fence():
    raw = "```\n{\"replacements\": []}\n```"

    assert extract_json(raw) == {"replacements": []}


def test_extract_json_with_preamble_and_trailing_prose():
    raw = 'Sure! The JSON is: {"replacements": [], "glossary": [{"term": "x"}]} Hope it helps.'

    assert extract_json(raw) == {"replacements": [], "glossary": [{"term": "x"}]}


def test_extract_json_handles_nested_braces_and_escaped_quotes_in_strings():
    raw = '{"find": "a } b { c", "note": "he said \\"stop\\" now", "nested": {"k": [1, 2]}}'

    parsed = extract_json(raw)

    assert parsed["find"] == "a } b { c"
    assert parsed["note"] == 'he said "stop" now'
    assert parsed["nested"] == {"k": [1, 2]}


def test_extract_json_garbage_returns_none():
    assert extract_json("no json here at all") is None
    assert extract_json("") is None
    assert extract_json("   ") is None
    assert extract_json("[1, 2, 3]") is None          # non è un oggetto
    assert extract_json('{"broken": ') is None


# ── chunk_segments ───────────────────────────────────────────────────────────
def test_chunk_segments_respects_budget_and_repeats_last_segment_as_context():
    segments = [{"start": float(i), "end": i + 1.0, "text": c * 10}
                for i, c in enumerate("abcd")]

    chunks = chunk_segments(segments, budget=25)

    # a+b stanno nel budget, c lo sfora
    assert [[s["text"][0] for s in c] for c in chunks] == [["a", "b"], ["b", "c", "d"]]
    # l'ultimo segmento del blocco precedente è ripetuto in testa come contesto
    assert chunks[1][0] is chunks[0][-1]
    # i segmenti "propri" di ogni blocco rispettano il budget
    for i, chunk in enumerate(chunks):
        own = chunk[1:] if i else chunk
        assert sum(len(s["text"]) for s in own) <= 25


def test_chunk_segments_keeps_an_oversized_segment_alone():
    big = {"start": 0.0, "end": 1.0, "text": "x" * 100}

    chunks = chunk_segments([big], budget=25)

    assert chunks == [[big]]


def test_chunk_segments_covers_all_segments_in_order():
    segments = [{"start": float(i), "end": i + 1.0, "text": f"riga {i} " * 5} for i in range(7)]

    chunks = chunk_segments(segments, budget=60)

    assert len(chunks) > 1
    flattened = [s for i, chunk in enumerate(chunks) for s in (chunk if i == 0 else chunk[1:])]
    assert flattened == segments
    assert chunks[0] == segments[: len(chunks[0])]


# ── _clean_proposals ─────────────────────────────────────────────────────────
def test_clean_proposals_drops_invalid_entries_and_normalizes_fields():
    payload = {
        "replacements": [
            {"find": "  pie torch  ", "replace": "PyTorch", "kind": "TERM",
             "confidence": "0.5", "reason": "termine tecnico"},
            {"replace": "senza find"},
            {"find": "x", "confidence": "non-un-numero"},
            {"find": "y", "confidence": 0},
            "non sono un dict",
        ]
    }

    out = _clean_proposals(payload)

    assert [p["find"] for p in out] == ["pie torch", "x", "y"]
    assert out[0] == {"find": "pie torch", "replace": "PyTorch", "reason": "termine tecnico",
                      "kind": "term", "confidence": 0.5}
    assert out[1]["replace"] == ""
    assert out[1]["kind"] == "other"
    assert out[1]["confidence"] is None
    assert out[2]["confidence"] == 0.0


def test_clean_proposals_without_replacements_key():
    assert _clean_proposals({}) == []
    assert _clean_proposals({"replacements": None}) == []


# ── chiavi cifrate ───────────────────────────────────────────────────────────
def test_encrypt_decrypt_roundtrip_with_env_secret(monkeypatch):
    monkeypatch.setenv("TRASCRIVI_SECRET", Fernet.generate_key().decode("ascii"))

    token = encrypt_key("sk-test-123")

    assert token != "sk-test-123" and "sk-test-123" not in token
    assert decrypt_key(token) == "sk-test-123"
    # il segreto arriva dall'ambiente: nessun file creato nel repo
    assert not config.SECRET_KEY_PATH.exists()


def test_decrypt_key_none_or_empty():
    assert decrypt_key(None) is None
    assert decrypt_key("") is None


def test_decrypt_key_with_rotated_secret_returns_none(monkeypatch):
    monkeypatch.setenv("TRASCRIVI_SECRET", Fernet.generate_key().decode("ascii"))
    token = encrypt_key("sk-test")

    monkeypatch.setenv("TRASCRIVI_SECRET", Fernet.generate_key().decode("ascii"))

    assert decrypt_key(token) is None
    assert decrypt_key("not-a-token") is None


def test_encrypt_key_falls_back_to_the_secret_file(monkeypatch):
    monkeypatch.delenv("TRASCRIVI_SECRET", raising=False)

    token = encrypt_key("sk-file")

    assert config.SECRET_KEY_PATH.exists()
    assert decrypt_key(token) == "sk-file"
