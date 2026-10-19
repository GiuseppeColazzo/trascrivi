"""
Fixture condivise della suite.

Regola numero uno: nessun test deve scrivere nel `data/` reale del repository.
`trascrivi.config` risolve i percorsi da costanti di modulo, quindi la fixture
`isolated_config` le rimappa tutte sotto `tmp_path` *prima* di `db.init_db()`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # normalmete lo fa pytest (pythonpath in pytest.ini)
    sys.path.insert(0, str(ROOT))

from trascrivi import config, db, llm  # noqa: E402
from trascrivi.textutil import segments_to_text  # noqa: E402

# Segmenti noti: i test che hanno bisogno di una trascrizione "vera" partono da qui.
SAMPLE_SEGMENTS = [
    {"start": 0.0, "end": 5.0, "text": "Good morning everyone"},
    {"start": 5.0, "end": 10.0, "text": "Today we talk about PyTorch"},
    {"start": 10.0, "end": 15.0, "text": "Please open your notebooks"},
]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Sposta ogni percorso di `config` sotto `tmp_path` e prepara lo schema."""
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA", data)
    monkeypatch.setattr(config, "SOURCES", data / "sources")
    monkeypatch.setattr(config, "TRANSCRIPTS", data / "transcripts")
    monkeypatch.setattr(config, "PARTIAL", data / "partial")
    monkeypatch.setattr(config, "LOGS", data / "logs")
    monkeypatch.setattr(config, "BACKUPS", data / "backup")
    monkeypatch.setattr(config, "SLIDES", data / "slides")
    monkeypatch.setattr(config, "DB_PATH", data / "trascrivi.db")
    monkeypatch.setattr(config, "SECRET_KEY_PATH", data / "secret.key")
    monkeypatch.setattr(config, "SETTINGS_PATH", data / "settings.json")
    # Il log su file non serve e terrebbe un handle aperto dentro tmp_path.
    monkeypatch.setattr(config, "_LOG_CONFIGURED", True)
    # La chiave Fernet dei test non deve arrivare dall'ambiente esterno.
    monkeypatch.delenv("TRASCRIVI_SECRET", raising=False)
    assert config.DATA != ROOT / "data"
    config.ensure_dirs()
    db.init_db()
    return data


@pytest.fixture(autouse=True)
def no_provider_calls(monkeypatch):
    """
    Nessun test automatico apre una connessione verso un provider LLM.

    È la regola numero uno del piano: la chiave dell'utente non deve mai essere
    usata da pytest (e nemmeno da un Ollama locale, che renderebbe i test lenti e
    dipendenti da cosa è scaricato).

    Il blocco sta su `openai.OpenAI`, cioè dove nasce il client HTTP: tutto passa
    di lì — chat, embeddings, VLM. Prima era su `llm._client`, che però valida
    anche le chiavi *senza* toccare la rete: bloccarlo rendeva non testabile il
    percorso "chiave non decifrabile". Un test che vuole un finto sostituisce
    `llm._client` e vince lui, perché la sua monkeypatch è più interna.
    """

    class _Blocked:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "il test ha provato a creare un client LLM reale: "
                "sostituisci llm._client con un fake (vedi tests/test_costs.py)"
            )

    import openai

    monkeypatch.setattr(openai, "OpenAI", _Blocked)


@pytest.fixture
def project():
    """Progetto di appoggio per i test che non stanno testando i progetti."""
    return db.create_project("Analisi 1", code="AN1")


@pytest.fixture
def mock_settings(monkeypatch):
    """
    Cambia le impostazioni per un test: `mock_settings(cards_max=3)`.

    Si sovrascrive `config.load_settings` invece di scrivere `settings.json`:
    i moduli leggono le impostazioni dalla funzione, quindi la patch copre anche
    i job, che non ricevono le impostazioni come argomento.
    """

    def _apply(**fields):
        settings = config.load_settings()
        for key, value in fields.items():
            setattr(settings, key, value)
        monkeypatch.setattr(config, "load_settings", lambda: settings)
        return settings

    return _apply


@pytest.fixture
def make_transcript(project):
    """Restituisce `make_transcript(...)` che crea una trascrizione nota."""

    def _make(project_id=None, title="Lezione 1", segments=None, text=None, **fields):
        segs = [dict(s) for s in (SAMPLE_SEGMENTS if segments is None else segments)]
        if text is None:
            text = segments_to_text(segs)
        return db.create_transcript(
            project if project_id is None else project_id, title, text, segs, **fields
        )

    return _make


@pytest.fixture
def client(isolated_config):
    """
    `TestClient` usato come context manager: l'evento `startup` avvia il worker
    dei job, quindi l'istanza va chiusa (shutdown) a fine test.
    """
    from fastapi.testclient import TestClient

    from trascrivi.api import create_app

    with TestClient(create_app()) as c:
        yield c
