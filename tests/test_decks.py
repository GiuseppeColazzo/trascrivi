"""
Export per Anki: `.apkg` con GUID stabili e `.tsv` di cortesia.

Il test che conta è il re-import: due export dello stesso mazzo devono produrre
le stesse note (stessi GUID), altrimenti Anki duplica invece di aggiornare. Per
verificarlo si apre l'`.apkg` — è uno zip con dentro un database SQLite — e si
leggono i GUID veri.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile

import pytest

from trascrivi import db, decks


@pytest.fixture
def lecture_with_cards(project, make_transcript):
    transcript_id = make_transcript(project, "Lezione 3")
    db.create_cards(transcript_id, [
        {"uid": "a" * 32, "kind": "qa", "front": "Che cosa riduce α?",
         "back": "Il numero di canali.", "slide_ref": 25},
        {"uid": "b" * 32, "kind": "cloze",
         "front": "In MQA chiavi e valori sono {{c1::condivisi}}", "back": ""},
        {"uid": "c" * 32, "kind": "qa", "front": "Carta sospesa?", "back": "Sì.",
         "status": "suspended"},
    ])
    return transcript_id


def _read_notes_from(data: bytes) -> list[dict]:
    """Le note dentro un `.apkg`: è uno zip la cui collezione è un SQLite."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        collection = next(n for n in zf.namelist() if n.startswith("collection"))
        blob = zf.read(collection)
    return _read_notes(blob)


def _read_notes(blob: bytes) -> list[dict]:
    """Apre la collezione da bytes (SQLite in memoria su file temporaneo)."""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
        fh.write(blob)
        path = Path(fh.name)
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT guid, flds, tags FROM notes")]
        conn.close()
        return rows
    finally:
        path.unlink(missing_ok=True)


def test_the_deck_name_is_hierarchical_and_stable(project, lecture_with_cards):
    transcript = db.get_transcript(lecture_with_cards)
    course = db.get_project(project)

    name = decks.deck_name(course, transcript)
    assert name == "trascrivi::analisi-1::lezione-3"
    assert decks.deck_name(course) == "trascrivi::analisi-1"


def test_the_same_cards_produce_the_same_deck_file(lecture_with_cards):
    transcript = db.get_transcript(lecture_with_cards)
    cards = decks.cards_for([lecture_with_cards])
    name = decks.deck_name(db.get_project(project_id=transcript["project_id"]), transcript)

    first = decks.build_apkg(cards, name)
    second = decks.build_apkg(cards, name)

    notes_a = {(n["guid"], n["flds"]) for n in _read_notes_from(first)}
    notes_b = {(n["guid"], n["flds"]) for n in _read_notes_from(second)}
    assert notes_a == notes_b
    assert len(notes_a) == 2


def _read_notes_from(data: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        collection = next(n for n in zf.namelist() if n.startswith("collection"))
        return _read_notes(zf.read(collection))


def test_the_guid_comes_from_the_card_uid(lecture_with_cards):
    cards = decks.cards_for([lecture_with_cards])
    guids = [n["guid"] for n in _read_notes_from(decks.build_apkg(
        cards, decks.deck_name(None, None)))]
    assert len(guids) == 2
    assert all(len(g) > 10 for g in guids)
    # Il GUID è calcolato dall'uid: cambiare l'uid cambia il GUID, cambiare il
    # testo della carta no.
    assert guids == sorted(guids)


def test_a_suspended_card_is_not_exported(lecture_with_cards):
    cards = decks.cards_for([lecture_with_cards])
    assert len(cards) == 2
    assert not any(c["front"] == "Carta sospesa?" for c in cards)

    with_suspended = decks.cards_for([lecture_with_cards], include_suspended=True)
    assert len(with_suspended) == 3


def test_the_slide_reference_ends_up_on_the_back(lecture_with_cards):
    cards = decks.cards_for([lecture_with_cards])
    name = decks.deck_name(None, None)
    notes = _read_notes_from(decks.build_apkg(cards, name))
    qa = next(n for n in notes if "Che cosa riduce" in n["flds"])
    assert "(slide 25)" in qa["flds"]
    assert "trascrivi" in qa["tags"]


def test_the_tsv_matches_the_format_of_the_demo_decks(lecture_with_cards):
    """Due colonne per il cloze, tre per QA: come in `decks/*.tsv`."""
    cards = decks.cards_for([lecture_with_cards])
    text = decks.build_tsv(cards, "trascrivi::corso::lezione")

    lines = [l for l in text.strip().splitlines() if l]
    assert len(lines) == 2
    qa = next(l for l in lines if "Che cosa riduce" in l)
    cloze = next(l for l in lines if "In MQA" in l)
    assert len(qa.split("\t")) == 3
    assert len(cloze.split("\t")) == 2
    assert qa.endswith("trascrivi::corso::lezione")
    assert "{{c1::condivisi}}" in cloze


def test_the_export_does_not_leave_temporary_files(lecture_with_cards, tmp_path):
    """
    `genanki` scrive la collezione in un file temporaneo e non lo cancella: se
    `TEMP` punta alla cartella di lavoro, ogni export lascia un `tmpXXXXXX` da
    50 KB nel repository. Il builder dirige `tempfile` in una cartella propria.
    """
    import tempfile
    from pathlib import Path

    scratch = Path(tmp_path) / "ambient-temp"
    scratch.mkdir()
    previous = tempfile.tempdir
    tempfile.tempdir = str(scratch)
    try:
        cards = decks.cards_for([lecture_with_cards])
        decks.build_apkg(cards, decks.deck_name(None, None))
        decks.build_apkg(cards, decks.deck_name(None, None))
    finally:
        tempfile.tempdir = previous

    assert list(scratch.iterdir()) == [], "l'export ha scritto nella cartella temporanea"


# ── API ──────────────────────────────────────────────────────────────────────
def test_the_lecture_deck_is_downloadable(client, lecture_with_cards):
    res = client.get(f"/api/transcripts/{lecture_with_cards}/deck.apkg")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/octet-stream"
    assert ".apkg" in res.headers["content-disposition"]
    assert res.content[:2] == b"PK"


def test_the_lecture_tsv_is_downloadable(client, lecture_with_cards):
    res = client.get(f"/api/transcripts/{lecture_with_cards}/deck.tsv")
    assert res.status_code == 200
    assert "text/tab-separated-values" in res.headers["content-type"]
    assert "{{c1::condivisi}}" in res.text


def test_the_course_deck_contains_the_lectures(client, lecture_with_cards):
    project_id = db.get_transcript(lecture_with_cards)["project_id"]

    res = client.get(f"/api/projects/{project_id}/deck.apkg")
    assert res.status_code == 200
    notes = _read_notes_from(res.content)
    assert len(notes) == 2

    # Senza quella lezione non c'è niente da esportare.
    res = client.get(f"/api/projects/{project_id}/deck.apkg?transcripts=999")
    assert res.status_code == 404


def test_a_lecture_without_cards_is_a_clear_404(client, make_transcript):
    transcript_id = make_transcript(title="Vuota")
    res = client.get(f"/api/transcripts/{transcript_id}/deck.apkg")
    assert res.status_code == 404
    assert "no active cards" in res.json()["detail"]
