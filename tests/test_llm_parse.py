"""Parsing delle risposte LLM e cifratura delle API key (`trascrivi.llm`)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from trascrivi import config, llm
from trascrivi.llm import (
    decrypt_key,
    encrypt_key,
    extract_json,
)


# ── forma della risposta e diagnostica ───────────────────────────────────────
class _Message:
    def __init__(self, content: str = "", extra: dict | None = None) -> None:
        self.content = content
        self.model_extra = extra or {}


class _Choice:
    def __init__(self, content: str = "", finish: str | None = None,
                 extra: dict | None = None) -> None:
        self.message = _Message(content, extra)
        self.finish_reason = finish


class _Usage:
    def model_dump(self) -> dict:
        return {"completion_tokens": 4000}


class _Response:
    def __init__(self, choices=(), usage=None) -> None:
        self.choices = list(choices)
        self.usage = usage


def test_extract_content_reports_finish_reason_and_reasoning():
    """
    Regressione: un modello "reasoning" può rispondere HTTP 200 con `content`
    vuoto e il pensiero in `reasoning_content`. Senza questa diagnostica l'errore
    appare come "JSON non valido" e nasconde la causa vera (budget di token).
    """
    response = _Response([_Choice("", "length", {"reasoning_content": "x" * 120})], _Usage())

    text, diag = llm.extract_content(response)

    assert text == ""
    assert diag["finish_reason"] == "length"
    assert diag["reasoning_chars"] == 120
    assert diag["usage"] == {"completion_tokens": 4000}


def test_extract_content_handles_empty_choices():
    text, diag = llm.extract_content(_Response())

    assert text == ""
    assert diag["finish_reason"] is None


def test_unreadable_key_is_reported_not_silently_ignored(monkeypatch, tmp_path):
    """Una chiave non decifrabile deve dare un errore che dice cosa fare."""
    monkeypatch.setattr(config, "SECRET_KEY_PATH", tmp_path / "secret.key")
    token = Fernet(Fernet.generate_key()).encrypt(b"sk-test").decode("ascii")

    with pytest.raises(RuntimeError) as excinfo:
        llm._client({"name": "deepseek", "api_key_enc": token, "base_url": "https://x"})

    assert "cannot be decrypted" in str(excinfo.value)


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
# ── _clean_proposals ─────────────────────────────────────────────────────────
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
