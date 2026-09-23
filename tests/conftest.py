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

from trascrivi import config, db  # noqa: E402
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


@pytest.fixture
def project():
    """Progetto di appoggio per i test che non stanno testando i progetti."""
    return db.create_project("Analisi 1", code="AN1")


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
