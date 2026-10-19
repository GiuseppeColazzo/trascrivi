"""
La guardia anti-rete, e il fatto che la usi davvero.

I test non devono mai raggiungere un provider LLM: la chiave dell'utente non è
un dato di test, e un test che dipende da Ollama locale passa sulla macchina di
chi l'ha scritto e fallisce su ogni altra. La guardia vive in `conftest.py` e qui
si verifica che *esista* e che blocchi davvero — un fake che non scatta mai è
peggio di nessuna guardia.
"""

from __future__ import annotations

import pytest

from trascrivi import db, llm

BLOCKED = "ha provato a creare un client LLM reale"


def test_a_chat_call_is_blocked():
    """Un provider senza chiave richiesta arriva fino alla creazione del client."""
    with pytest.raises(RuntimeError, match=BLOCKED):
        llm.test_provider({"name": "ollama", "base_url": "http://localhost:11434/v1",
                           "model": "qwen3.5:4b"})


def test_a_summary_without_a_fake_cannot_reach_the_network():
    """Il percorso vero dei job: se qualcuno dimentica il fake, il test lo dice."""
    with pytest.raises(RuntimeError, match=BLOCKED):
        llm.chat_completion({"name": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                             "model": "x", "api_key_enc": llm.encrypt_key("sk-test")},
                            "x", "system", "user", max_tokens=10)


def test_a_fake_set_by_the_test_still_wins(monkeypatch):
    """Chi ha bisogno di un finto lo mette dopo: l'ordine delle fixture lo garantisce."""
    monkeypatch.setattr(llm, "_client", lambda provider: "finto")
    assert llm._client({"name": "ollama"}) == "finto"


def test_the_guard_does_not_hide_key_validation():
    """
    Il percorso "chiave non decifrabile" non tocca la rete e deve restare
    testabile: è per questo che la guardia sta sul client e non su `_client`.
    """
    provider = db.get_provider_by_name("deepseek") or {}
    assert provider, "il provider deepseek è seminato all'avvio"
    with pytest.raises(RuntimeError, match="No API key configured"):
        llm._client({**provider, "api_key_enc": None})
