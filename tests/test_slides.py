"""
Slide: upload, storage su disco, dedup per hash e job di parsing.

Tutto offline. Il parsing vero ha i suoi test in `test_slideparser.py`; qui si
verifica il confine HTTP — cosa entra, dove finisce su disco, cosa succede a un
nome file ostile — e che il job di parsing scriva le pagine, senza dipendere
dalle librerie di parsing (che sono opzionali e possono mancare).
"""

from __future__ import annotations

import pytest

from trascrivi import config, db, jobs, slideparser

PDF_BYTES = b"%PDF-1.4\n% test\n"


@pytest.fixture
def no_worker(monkeypatch):
    """L'upload accoda un job: qui non deve partire, i test lo eseguono a mano."""
    monkeypatch.setattr(jobs, "enqueue", lambda job_id: None)


def _upload(client, project_id: int, name: str, data: bytes = PDF_BYTES):
    return client.post(f"/api/projects/{project_id}/slides",
                       files={"file": (name, data, "application/pdf")})


def test_upload_stores_the_file_and_queues_the_parser(client, project, no_worker):
    res = _upload(client, project, "lezione-3.pdf")
    assert res.status_code == 201, res.text
    slide = res.json()["slide"]

    assert slide["status"] == "parsing"
    assert slide["name"] == "lezione-3.pdf"
    assert slide["ext"] == ".pdf"

    stored = config.slide_source_path(project, slide["id"], ".pdf")
    assert stored.read_bytes() == PDF_BYTES
    assert stored.parent == config.slide_dir(project, slide["id"])

    job = db.get_job(res.json()["job_id"])
    assert job and job["kind"] == "parse_slides"
    assert db.jload(job["payload"])["doc_id"] == slide["id"]


def test_the_same_deck_uploaded_twice_is_not_parsed_twice(client, project, no_worker):
    first = _upload(client, project, "deck.pdf").json()
    second = _upload(client, project, "copia.pdf").json()

    assert second["duplicate_of"] == first["slide"]["id"]
    assert second["job_id"] is None
    assert len(db.list_slide_docs(project)) == 1


def test_a_hostile_filename_cannot_escape_the_slides_folder(client, project, no_worker):
    """Il nome sta nel database, il file su disco ha un nome fisso."""
    res = _upload(client, project, "../../evil.pdf")
    slide = res.json()["slide"]

    assert "/" not in slide["name"] and "\\" not in slide["name"]
    stored = config.slide_source_path(project, slide["id"], ".pdf")
    assert stored.resolve().is_relative_to(config.SLIDES.resolve())


def test_unsupported_formats_are_rejected(client, project, no_worker):
    res = _upload(client, project, "dispensa.exe")
    assert res.status_code == 400
    assert "Unsupported format" in res.json()["detail"]


def test_listing_and_deleting_a_document(client, project, no_worker):
    uploaded = _upload(client, project, "deck.pdf").json()["slide"]
    listing = client.get(f"/api/projects/{project}/slides").json()["slides"]
    assert [s["id"] for s in listing] == [uploaded["id"]]

    assert client.delete(f"/api/slides/{uploaded['id']}").status_code == 200
    assert db.get_slide_doc(uploaded["id"]) is None
    assert not config.slide_dir(project, uploaded["id"]).exists()


def test_the_parser_job_writes_pages_and_schedules_the_matching(monkeypatch, project):
    """
    Il job non fa il matching: accoda `embed_match`. Diagramma del piano §4.
    """
    doc_id = db.create_slide_doc(project, "deck.pdf", ".pdf", "sha1")
    stored = config.slide_source_path(project, doc_id, ".pdf")
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(PDF_BYTES)

    monkeypatch.setattr(slideparser, "parse", lambda path, out_dir, **kw: (
        [
            {"page_no": 1, "text": "Width multiplier alpha = 0.75", "complex": True,
             "has_text": True, "n_images": 0, "ocr_chars": 0, "render_path": None,
             "render_sha": None},
            {"page_no": 2, "text": "Depthwise separable convolutions", "complex": False,
             "has_text": True, "n_images": 0, "ocr_chars": 0, "render_path": None,
             "render_sha": None},
        ],
        ["Part-II: AI Computing Optimization"],
    ))
    monkeypatch.setattr(jobs, "enqueue", lambda job_id: None)

    job_id = db.create_job("parse_slides", {"doc_id": doc_id}, project_id=project)
    queued: list[tuple[str, dict]] = []
    monkeypatch.setattr(db, "create_job",
                        lambda kind, payload, **kw: queued.append((kind, payload)) or 99)

    result = jobs.run_parse_slides(job_id, {"doc_id": doc_id})

    doc = db.get_slide_doc(doc_id)
    assert doc["status"] == "ready"
    assert doc["n_pages"] == 2
    assert db.jload(doc["boilerplate"]) == ["Part-II: AI Computing Optimization"]

    pages = db.list_slide_pages(doc_id)
    assert [p["page_no"] for p in pages] == [1, 2]
    assert pages[0]["complex"] == 1 and pages[1]["complex"] == 0
    assert result["complex"] == 1

    assert ("embed_match", {"project_id": project, "doc_id": doc_id}) in queued


def test_a_missing_file_marks_the_document_as_failed(project):
    """Il documento esiste nel DB ma il file è stato cancellato a mano."""
    doc_id = db.create_slide_doc(project, "perso.pdf", ".pdf", "sha2")
    job_id = db.create_job("parse_slides", {"doc_id": doc_id}, project_id=project)

    with pytest.raises(FileNotFoundError):
        jobs.run_parse_slides(job_id, {"doc_id": doc_id})

    assert db.get_slide_doc(doc_id)["status"] == "error"
