"""
Matching pagine -> lezioni con embeddings locali.

I vettori sono finti e deterministici: la rete non si tocca, e i test misurano
quello che conta davvero, cioè le regole di assegnazione — soglia, smoothing dei
run corti, e il fatto che una proposta confermata a mano non venga cancellata dal
run successivo.
"""

from __future__ import annotations

import pytest

from trascrivi import config, db, llm, matching


def _vector(text: str) -> list[float]:
    """Un one-hot per parola chiave: la similarità del coseno diventa ovvia."""
    lowered = text.lower()
    if "convolution" in lowered:
        return [1.0, 0.0, 0.0]
    if "pruning" in lowered:
        return [0.0, 1.0, 0.0]
    if "attention" in lowered:
        return [0.0, 0.0, 1.0]
    return [0.0, 0.0, 0.0]


@pytest.fixture
def fake_embeddings(monkeypatch):
    calls: list[int] = []

    def _embed(provider, texts, model):
        calls.append(len(texts))
        return [_vector(t) for t in texts]

    monkeypatch.setattr(llm, "embed_texts", _embed)
    return calls


@pytest.fixture
def course(project, make_transcript):
    """Un corso con due lezioni che parlano di cose diverse."""
    a = make_transcript(project, "Convolutions", segments=[
        {"start": 0, "end": 4, "text": "Today we talk about convolution layers"},
    ])
    b = make_transcript(project, "Pruning", segments=[
        {"start": 0, "end": 4, "text": "Today we talk about pruning masks"},
    ])
    db.create_summary(a, markdown="# Convolutions\n\nconvolution everywhere " * 20,
                      title="Convolutions", overview="convolution layers")
    db.create_summary(b, markdown="# Pruning\n\npruning everywhere " * 20,
                      title="Pruning", overview="pruning masks")
    return {"a": a, "b": b}


def _doc(project_id: int, texts: list[str], *, status: str = "ready") -> int:
    doc_id = db.create_slide_doc(project_id, "deck.pdf", ".pdf", f"sha{len(texts)}-{project_id}")
    db.save_slide_pages(doc_id, [
        {"page_no": i, "text": text, "has_text": True, "n_images": 0,
         "render_path": None, "complex": False}
        for i, text in enumerate(texts, start=1)
    ])
    db.update_slide_doc(doc_id, {"status": status, "n_pages": len(texts)})
    return doc_id


def test_the_pages_of_a_block_are_assigned_to_the_nearest_lecture(
        project, course, fake_embeddings):
    doc_id = _doc(project, [
        "convolution layers in a deep network, page one",
        "convolution layers in a deep network, page two",
        "convolution layers in a deep network, page three",
        "pruning masks and sparse weights, page four",
        "pruning masks and sparse weights, page five",
        "pruning masks and sparse weights, page six",
    ])

    result = matching.match_project(project)

    links = db.list_slide_links(doc_id=doc_id)
    assert [(l["page_from"], l["page_to"], l["transcript_id"]) for l in links] == [
        (1, 3, course["a"]), (4, 6, course["b"])]
    assert all(l["source"] == "auto" for l in links)
    assert all(l["score"] > 0.9 for l in links), links
    assert result["links"] == 2


def test_a_page_below_the_threshold_gets_no_lecture(project, course, fake_embeddings):
    """Il vettore nullo non somiglia a niente: meglio nessuna assegnazione."""
    doc_id = _doc(project, [
        "convolution layers in a deep network, page one",
        "convolution layers in a deep network, page two",
        "convolution layers in a deep network, page three",
        "una pagina che non parla di nessuno dei due argomenti del corso",
    ])

    matching.match_project(project)

    links = db.list_slide_links(doc_id=doc_id)
    assert [l["page_to"] for l in links] == [3], links


def test_a_single_page_run_is_absorbed_by_its_neighbours(project, course, fake_embeddings):
    """
    Cinque pagine di convoluzioni con una di pruning in mezzo sono un blocco di
    convoluzioni: la pagina isolata è rumore del matching, non una lezione.
    """
    doc_id = _doc(project, [
        "convolution layers in a deep network, page one",
        "convolution layers in a deep network, page two",
        "pruning masks and sparse weights, pagina isolata",
        "convolution layers in a deep network, page three",
        "convolution layers in a deep network, page four",
    ])

    matching.match_project(project)

    links = db.list_slide_links(doc_id=doc_id)
    assert [(l["page_from"], l["page_to"], l["transcript_id"]) for l in links] == [
        (1, 5, course["a"])]


def test_a_short_run_between_two_different_lectures_is_dropped(project, course,
                                                               fake_embeddings):
    """
    Una pagina di `attention` fra due blocchi: i vicini litigano, e una scelta a
    caso sporcherebbe il contesto di una carta. Resta senza assegnazione.
    """
    doc_id = _doc(project, [
        "convolution layers in a deep network, page one",
        "convolution layers in a deep network, page two",
        "convolution layers in a deep network, page three",
        "attention is all you need, pagina isolata",
        "pruning masks and sparse weights, page four",
        "pruning masks and sparse weights, page five",
        "pruning masks and sparse weights, page six",
    ])

    matching.match_project(project)

    links = db.list_slide_links(doc_id=doc_id)
    covered = {p for l in links for p in range(l["page_from"], l["page_to"] + 1)}
    assert 4 not in covered, links


def test_a_confirmed_link_survives_the_next_matching_run(project, course, fake_embeddings):
    """
    Il matching cancella solo le proprie proposte: quello che l'utente ha
    confermato (o corretto) a mano resta.
    """
    doc_id = _doc(project, [
        "convolution layers in a deep network, page one",
        "convolution layers in a deep network, page two",
        "pruning masks and sparse weights, page three",
    ])
    matching.match_project(project)

    auto = db.list_slide_links(doc_id=doc_id)[0]
    db.confirm_links([auto["id"]])
    db.upsert_link(doc_id, 3, 3, course["a"])   # correzione manuale

    matching.match_project(project)

    links = db.list_slide_links(doc_id=doc_id)
    manual = {(l["page_from"], l["page_to"], l["transcript_id"]) for l in links
              if l["source"] == "manual"}
    assert (auto["page_from"], auto["page_to"], auto["transcript_id"]) in manual
    assert (3, 3, course["a"]) in manual


def test_vectors_are_cached_and_not_recomputed(project, course, fake_embeddings):
    """Il secondo run non richiama gli embeddings già calcolati."""
    _doc(project, ["convolution layers in a deep network, page one",
                   "convolution layers in a deep network, page two"])
    matching.match_project(project)
    first = sum(fake_embeddings)
    matching.match_project(project)
    second = sum(fake_embeddings) - first

    # Restano da calcolare solo i vettori nuovi (nessuno, in questo caso).
    assert second == 0, (first, second)


# ── I vettori invecchiano: il testo cambia, il matching deve seguirlo ────────
def test_rewriting_a_summary_refreshes_its_vector(project, course, fake_embeddings):
    """
    Un riassunto riscritto è materiale nuovo: senza ricalcolare il vettore, il
    matching continuerebbe a confrontare le pagine con il riassunto vecchio e
    l'assegnazione sembrerebbe sbagliata senza motivo.
    """
    doc_id = _doc(project, ["convolution layers in a deep network, page one"])
    matching.match_project(project)
    assert db.list_slide_links(doc_id=doc_id)[0]["transcript_id"] == course["a"]

    # La lezione A ora parla di pruning, la B di convoluzioni: si invertono.
    db.create_summary(course["a"], markdown="# Pruning\n\n" + "pruning masks " * 60,
                      title="Pruning", overview="pruning")
    db.create_summary(course["b"], markdown="# Convolutions\n\n" + "convolution layers " * 60,
                      title="Convolutions", overview="convolution")

    matching.match_project(project)

    links = db.list_slide_links(doc_id=doc_id)
    assert [l["transcript_id"] for l in links] == [course["b"]], links


def test_a_vlm_rendering_changes_the_page_vector(project, course, fake_embeddings):
    """Dopo il pass VLM la pagina ha più testo: il vettore va ricalcolato."""
    doc_id = _doc(project, ["una pagina povera di testo, page one"])
    doc = db.list_slide_docs(project)[0]
    db.update_slide_page(doc_id, 1, {"vlm_md": "## Testo\npruning masks and sparse weights"})

    matching.match_project(project)

    links = db.list_slide_links(doc_id=doc_id)
    assert [l["transcript_id"] for l in links] == [course["b"]], (links, db.list_slide_pages(doc_id))


# ── Il riassunto nuovo fa ripartire il matching da solo ─────────────────────
def test_a_new_summary_queues_a_new_matching_run(monkeypatch, project, course):
    """
    Il riassunto è la controparte del matching: quando ne arriva uno nuovo gli
    intervalli del corso vanno rifatti. Senza questo, dopo aver scritto il
    riassunto bisognava ricordarsi di premere "Associa alle lezioni".
    """
    from trascrivi import jobs, llm

    class _Resp:
        class usage:
            @staticmethod
            def model_dump():
                return {"prompt_tokens": 10, "completion_tokens": 10}

        choices = [type("C", (), {
            "message": type("M", (), {"content": "# Convolutions\n\n" + "convolution " * 60})(),
            "finish_reason": "stop"})()]

    client = type("C", (), {"chat": type("Chat", (), {"completions": type(
        "Comp", (), {"create": lambda self, **kw: _Resp()})()})()})()
    monkeypatch.setattr(llm, "_client", lambda provider: client)
    monkeypatch.setattr(jobs, "enqueue", lambda job_id: None)

    _doc(project, ["convolution layers in a deep network, page one"])
    job_id = db.create_job("llm_summary", {"transcript_id": course["a"]},
                           project_id=project, transcript_id=course["a"])
    jobs.run_summary(job_id, {"transcript_id": course["a"],
                             "provider_id": db.get_provider_by_name("deepseek")["id"]})

    queued = db.rows("SELECT * FROM jobs WHERE kind = 'embed_match'")
    assert len(queued) == 1 and queued[0]["project_id"] == project


def test_a_course_without_slides_does_not_queue_matching(monkeypatch, project, course):
    from trascrivi import jobs, llm

    class _Resp:
        class usage:
            @staticmethod
            def model_dump():
                return {"prompt_tokens": 10, "completion_tokens": 10}

        choices = [type("C", (), {
            "message": type("M", (), {"content": "# Note\n\n" + "contenuto " * 60})(),
            "finish_reason": "stop"})()]

    client = type("C", (), {"chat": type("Chat", (), {"completions": type(
        "Comp", (), {"create": lambda self, **kw: _Resp()})()})()})()
    monkeypatch.setattr(llm, "_client", lambda provider: client)
    monkeypatch.setattr(jobs, "enqueue", lambda job_id: None)

    job_id = db.create_job("llm_summary", {"transcript_id": course["a"]},
                           project_id=project, transcript_id=course["a"])
    jobs.run_summary(job_id, {"transcript_id": course["a"],
                             "provider_id": db.get_provider_by_name("deepseek")["id"]})

    assert db.rows("SELECT * FROM jobs WHERE kind = 'embed_match'") == []


# ── Il caso "slide caricate prima che la lezione esista" ────────────────────
def test_matching_without_lectures_leaves_every_page_unassigned(project, fake_embeddings):
    """
    Un corso di sole slide: il matching non ha candidati, non inventa niente e
    non fallisce. Le pagine restano leggibili, l'assegnazione arriva quando ci
    sarà una lezione.
    """
    doc_id = _doc(project, ["convolution layers in a deep network, page one"])

    result = matching.match_project(project)

    assert result["links"] == 0
    assert db.list_slide_links(doc_id=doc_id) == []
    assert db.list_slide_pages(doc_id)  # il parsing è rimasto intatto


def test_a_finished_lecture_queues_the_matching_when_slides_exist(monkeypatch, project,
                                                                 course):
    """
    Slide caricate quando le lezioni non c'erano: le pagine restano senza
    proposta. Appena una trascrizione finisce, il matching parte da solo —
    altrimenti l'unico modo di accorgersene sarebbe premere un bottone e
    chiedersi perché non succede niente.
    """
    from trascrivi import jobs

    _doc(project, ["convolution layers in a deep network, page one"])
    monkeypatch.setattr(jobs, "run_transcribe", lambda job_id, payload: {
        "transcript_id": course["a"], "segments": 2})
    monkeypatch.setattr(jobs, "_finish_transcribe_cleanup", lambda job, payload: None)
    monkeypatch.setattr(jobs, "_maybe_queue_agent", lambda *args: None)
    monkeypatch.setattr(jobs, "enqueue", lambda job_id: None)

    job_id = db.create_job("transcribe", {"source_id": None}, project_id=project)
    jobs._run_job(db.get_job(job_id))

    queued = db.rows("SELECT * FROM jobs WHERE kind = 'embed_match'")
    assert len(queued) == 1 and queued[0]["project_id"] == project


def test_the_matching_job_is_not_queued_twice(monkeypatch, project):
    """Trascrizione e riassunto nello stesso giro accodano un solo matching."""
    from trascrivi import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda job_id: None)
    _doc(project, ["convolution layers in a deep network, page one"])

    first = jobs._queue_matching(project)
    second = jobs._queue_matching(project)

    assert first and second is None
    assert len(db.rows("SELECT * FROM jobs WHERE kind = 'embed_match'")) == 1


def test_matching_without_ollama_says_so(monkeypatch, project, course, fake_embeddings):
    """Nessun ripiego su un provider a pagamento: il piano lo esclude."""
    monkeypatch.setattr(matching, "embed_provider", lambda: None)
    with pytest.raises(ValueError, match="Ollama"):
        matching.match_project(project)


def test_an_embedding_failure_names_the_provider_and_the_fix(monkeypatch):
    """
    "Connection error." da solo non dice a nessuno che cosa avviare: il messaggio
    deve portarsi dietro provider, indirizzo e modello da scaricare.
    """
    class _Embeddings:
        def create(self, **kwargs):
            raise ConnectionError("Connection error.")

    client = type("C", (), {"embeddings": _Embeddings()})()
    monkeypatch.setattr(llm, "_client", lambda provider: client)
    provider = {"name": "ollama", "base_url": "http://localhost:11434/v1"}

    with pytest.raises(RuntimeError) as excinfo:
        llm.embed_texts(provider, ["hello"], model="nomic-embed-text:v1.5")

    message = str(excinfo.value)
    assert "localhost:11434" in message
    assert "ollama pull nomic-embed-text:v1.5" in message


def test_the_threshold_comes_from_settings(project, course, mock_settings, fake_embeddings):
    mock_settings(match_threshold=0.99)
    doc_id = _doc(project, [
        "convolution layers in a deep network, page one",
        "convolution layers in a deep network, page two",
        "convolution layers in a deep network, page three",
    ])
    matching.match_project(project)
    # [1,0,0] contro sé stesso fa 1.0: passa. Un mezzo match non passerebbe.
    assert db.list_slide_links(doc_id=doc_id)


# ── API ──────────────────────────────────────────────────────────────────────
def test_the_api_lists_proposals_and_accepts_them(client, project, course,
                                                 fake_embeddings):
    doc_id = _doc(project, [
        "convolution layers in a deep network, page one",
        "convolution layers in a deep network, page two",
        "pruning masks and sparse weights, page three",
    ])
    matching.match_project(project)

    listing = client.get(f"/api/projects/{project}/slide-links").json()["links"]
    assert len(listing) == 2
    assert listing[0]["doc_name"] == "deck.pdf"
    assert listing[0]["transcript_title"] in ("Convolutions", "Pruning")

    res = client.post(f"/api/slides/{doc_id}/links/accept", json={})
    assert res.status_code == 200
    assert all(l["source"] == "manual" for l in res.json()["links"])


def test_the_api_adds_and_removes_a_manual_range(client, project, course):
    doc_id = _doc(project, ["convolution layers in a deep network, page one"])
    res = client.post(f"/api/slides/{doc_id}/links",
                      json={"page_from": 1, "page_to": 1, "transcript_id": course["b"]})
    assert res.status_code == 201, res.text
    link = res.json()["links"][0]
    assert (link["transcript_id"], link["source"]) == (course["b"], "manual")

    assert client.delete(f"/api/slide-links/{link['id']}").status_code == 200
    assert client.get(f"/api/projects/{project}/slide-links").json()["links"] == []


def test_a_range_beyond_the_document_is_rejected(client, project, course):
    doc_id = _doc(project, ["convolution layers in a deep network, page one"])
    res = client.post(f"/api/slides/{doc_id}/links",
                      json={"page_from": 1, "page_to": 9, "transcript_id": course["a"]})
    assert res.status_code == 422
    assert "1 pages" in res.json()["detail"]


def test_the_match_endpoint_queues_a_job(monkeypatch, client, project):
    from trascrivi import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda job_id: None)
    _doc(project, ["convolution layers in a deep network, page one"])
    res = client.post(f"/api/projects/{project}/slides/match")
    assert res.status_code == 201
    assert res.json()["job"]["kind"] == "embed_match"
