"""
Generazione delle carte: contesto, cache, dedup, formule verificate, API.

Tutte le chiamate sono finte (nessuna rete): quello che si verifica è la logica
che decide *cosa* diventa una carta e cosa no, perché è lì che si sbaglia.
"""

from __future__ import annotations

import json

import pytest

from trascrivi import cards, config, db, jobs, llm


def fake_cards_llm(monkeypatch, payload, *, raise_exc: Exception | None = None,
                   raw_text: str | None = None):
    """Provider finto: risponde con `payload` (dict) come JSON e registra la chiamata."""
    calls: list[dict] = []
    content = raw_text if raw_text is not None else json.dumps(payload)

    class _Usage:
        def model_dump(self):
            return {"prompt_tokens": 900, "completion_tokens": 300,
                    "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 900}

    class _Resp:
        usage = _Usage()
        choices = [type("C", (), {
            "message": type("M", (), {"content": content})(),
            "finish_reason": "stop"})()]

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if raise_exc:
                raise raise_exc
            return _Resp()

    client = type("C", (), {
        "chat": type("Chat", (), {"completions": _Completions()})(),
    })()
    monkeypatch.setattr(llm, "_client", lambda provider: client)
    return calls


CARD_JSON = {"cards": [
    {"kind": "cloze", "front": "Con width multiplier α = {{c1::0.75}} i canali diventano "
                               "{{c2::6 e 3}}", "back": "", "slide": 25},
    {"kind": "qa", "front": "Che cosa riduce il width multiplier in MobileNetV1?",
     "back": "Il numero di canali di output di tutti i layer.", "slide": 25},
    {"kind": "qa", "front": "Un'altra domanda valida?", "back": "Sì.", "slide": None},
]}


@pytest.fixture
def lecture(project, make_transcript):
    """Una lezione con riassunto e tre pagine di slide assegnate."""
    transcript_id = make_transcript(project, "Lezione 3", segments=[
        {"start": 0, "end": 5, "text": "Today we talk about width multipliers"},
    ])
    db.create_summary(transcript_id, markdown="# Width multipliers\n\n"
                      "Il width multiplier riduce i canali di output.\n"
                      "* il fattore è $\\alpha$\n", title="Width multipliers",
                      overview="width multiplier")
    doc_id = db.create_slide_doc(project, "deck.pdf", ".pdf", "sha-cards")
    db.save_slide_pages(doc_id, [
        {"page_no": 24, "text": "Depthwise separable convolutions", "has_text": True,
         "render_path": None, "complex": False},
        {"page_no": 25, "text": "Width multiplier α = 0.75: L1 K=6, L2 K=3",
         "has_text": True, "render_path": None, "complex": True,
         "vlm_md": "## Testo\nWidth multiplier $\\alpha = 0.75$"},
        {"page_no": 40, "text": "KV cache", "has_text": True, "render_path": None,
         "complex": False},
    ])
    db.update_slide_doc(doc_id, {"status": "ready", "n_pages": 3})
    db.upsert_link(doc_id, 24, 25, transcript_id)
    return transcript_id


def _provider(name="deepseek"):
    return {"name": name, "model": "deepseek-flash", "base_url": "https://x",
            "api_key_enc": None}


def _run(monkeypatch, transcript_id, payload=CARD_JSON, **kwargs):
    calls = fake_cards_llm(monkeypatch, payload)
    provider_id = db.get_provider_by_name("deepseek")["id"]
    job_id = db.create_job("llm_cards", {"transcript_id": transcript_id},
                           transcript_id=transcript_id)
    result = jobs.run_cards(job_id, {"transcript_id": transcript_id,
                                     "provider_id": provider_id, **kwargs})
    return result, calls


# ── Contesto ─────────────────────────────────────────────────────────────────
def test_the_context_is_the_summary_plus_the_linked_pages(lecture):
    context = cards.build_context(db.get_transcript(lecture), config.load_settings())

    assert context["summary"].startswith("# Width multipliers")
    assert len(context["slides"]) == 2          # pagine 24 e 25, non la 40
    assert context["slides"][0].startswith("Slide 24")
    assert "structured rendering" in context["slides"][1]


def test_the_context_never_exceeds_its_budget(lecture, mock_settings):
    mock_settings(cards_context_chars=600)
    context = cards.build_context(db.get_transcript(lecture), config.load_settings())
    assert context["chars"] <= 600
    assert context["summary"]          # il riassunto non sparisce mai del tutto


def test_a_page_with_uncertain_math_says_so_in_the_context(lecture):
    doc = db.list_slide_docs(db.get_transcript(lecture)["project_id"])[0]
    db.update_slide_page(doc["id"], 25, {"math_uncertain": 1})

    context = cards.build_context(db.get_transcript(lecture), config.load_settings())
    assert "formulas on this page are uncertain" in "\n".join(context["slides"])


def test_uncertain_pages_cannot_contribute_verified_latex(lecture):
    doc = db.list_slide_docs(db.get_transcript(lecture)["project_id"])[0]
    context = cards.build_context(db.get_transcript(lecture), config.load_settings())
    assert any("alpha = 0.75" in span for span in cards.latex_set(context))

    db.update_slide_page(doc["id"], 25, {"math_uncertain": 1})
    context = cards.build_context(db.get_transcript(lecture), config.load_settings())
    assert not any("alpha = 0.75" in span for span in cards.latex_set(context))


# ── Generazione ──────────────────────────────────────────────────────────────
def test_cards_are_generated_saved_and_cited(monkeypatch, lecture):
    result, calls = _run(monkeypatch, lecture)

    assert result["n_new"] == 3
    saved = db.list_cards(lecture)
    assert [c["kind"] for c in saved] == ["cloze", "qa", "qa"]
    assert saved[0]["slide_ref"] == "25" or saved[0]["slide_ref"] == 25
    assert all(len(c["uid"]) == 32 for c in saved)
    assert saved[0]["origin"] == "llm"
    # Il prompt porta la lingua, il tetto e il materiale.
    system = calls[0]["messages"][0]["content"]
    user = calls[0]["messages"][1]["content"]
    assert "English" in system
    assert "Slide 25" in user and "Width multipliers" in user


def test_the_same_material_is_not_paid_twice(monkeypatch, lecture):
    """Seconda generazione con lo stesso contesto: nessuna chiamata."""
    _first, calls = _run(monkeypatch, lecture)
    result, calls_again = _run(monkeypatch, lecture)

    assert result["cached"] is True
    assert result["n_new"] == 0
    assert calls_again == []
    assert len(db.list_cards(lecture)) == 3


def test_force_regenerates_and_keeps_the_hand_edited_cards(monkeypatch, lecture):
    _first, _calls = _run(monkeypatch, lecture)
    edited = db.list_cards(lecture)[0]
    db.update_card(edited["id"], {"front": "Domanda riscritta a mano", "edited": 1})

    # Una rigenerazione vera propone carte nuove: se proponesse le stesse, il
    # dedup le scarterebbe (ed è giusto: sono già lì).
    fresh = {"cards": [
        {"kind": "qa", "front": f"Domanda nuova {i}?", "back": "Risposta.", "slide": 24}
        for i in range(3)
    ]}
    fake_cards_llm(monkeypatch, fresh)
    job_id = db.create_job("llm_cards", {"transcript_id": lecture}, transcript_id=lecture)
    result = jobs.run_cards(job_id, {
        "transcript_id": lecture, "provider_id": db.get_provider_by_name("deepseek")["id"],
        "force": True})

    assert result["cached"] is False
    assert result["n_new"] == 3
    fronts = [c["front"] for c in db.list_cards(lecture)]
    assert "Domanda riscritta a mano" in fronts
    assert len(db.list_cards(lecture)) == 4


def test_a_changed_summary_invalidates_the_cache(monkeypatch, lecture):
    _first, _calls = _run(monkeypatch, lecture)
    db.create_summary(lecture, markdown="# Nuovo riassunto\n\ncontenuto diverso",
                      title="Nuovo")

    result, calls = _run(monkeypatch, lecture)

    assert result["cached"] is False
    assert len(calls) == 1


def test_duplicates_are_dropped_and_counted(monkeypatch, lecture):
    payload = {"cards": [
        {"kind": "qa", "front": "Che cosa riduce il width multiplier?",
         "back": "Il numero di canali.", "slide": 25},
        # Stessa domanda, maiuscole e punteggiatura diverse: è un duplicato.
        {"kind": "qa", "front": "che cosa riduce il WIDTH MULTIPLIER",
         "back": "Il numero di canali.", "slide": 25},
    ]}
    result, _calls = _run(monkeypatch, lecture, payload=payload)

    assert result["n_new"] == 1
    assert result["n_skipped_dup"] == 1


def test_a_card_with_an_unverified_formula_is_rejected(monkeypatch, lecture):
    """
    Principio 7: una formula che non è nel materiale non entra in una carta,
    nemmeno se il modello la considera ovvia.
    """
    payload = {"cards": [
        {"kind": "cloze", "front": "La formula è {{c1::$R(\\alpha) = \\sum_l R^{(l)}$}}",
         "back": "", "slide": 25},
        {"kind": "qa", "front": "Domanda pulita?", "back": "Risposta pulita.", "slide": 25},
    ]}
    result, _calls = _run(monkeypatch, lecture, payload=payload)

    assert result["n_new"] == 1
    assert "unverified_formula" in result["rejected"]


def test_a_formula_that_is_in_the_material_is_accepted(monkeypatch, lecture):
    payload = {"cards": [
        {"kind": "cloze", "front": "Con width multiplier $\\alpha = 0.75$ i canali di L1 "
                                   "diventano {{c1::6}}", "back": "", "slide": 25},
    ]}
    result, _calls = _run(monkeypatch, lecture, payload=payload)
    assert result["n_new"] == 1


def test_a_bad_cloze_is_rejected(monkeypatch, lecture):
    payload = {"cards": [
        {"kind": "cloze", "front": "Senza cancellazioni", "back": "", "slide": 25},
        {"kind": "cloze", "front": "Con {{c1::}} vuota", "back": "", "slide": 25},
        {"kind": "qa", "front": "Domanda?", "back": "Risposta.", "slide": 25},
    ]}
    result, _calls = _run(monkeypatch, lecture, payload=payload)
    assert result["n_new"] == 1
    assert result["rejected"].count("bad_cloze") == 2


def test_a_slide_number_outside_the_context_is_dropped(monkeypatch, lecture):
    payload = {"cards": [
        {"kind": "qa", "front": "Domanda?", "back": "Risposta.", "slide": 999},
    ]}
    _result, _calls = _run(monkeypatch, lecture, payload=payload)
    assert db.list_cards(lecture)[0]["slide_ref"] is None


def test_the_maximum_number_of_cards_is_respected(monkeypatch, lecture, mock_settings):
    mock_settings(cards_max=2)
    payload = {"cards": [
        {"kind": "qa", "front": f"Domanda {i}?", "back": "Risposta.", "slide": 25}
        for i in range(6)
    ]}
    result, _calls = _run(monkeypatch, lecture, payload=payload)
    assert result["n_new"] == 2


def test_garbage_from_the_model_saves_nothing(monkeypatch, lecture):
    fake_cards_llm(monkeypatch, None, raw_text="I cannot write flashcards about this.")
    provider_id = db.get_provider_by_name("deepseek")["id"]
    job_id = db.create_job("llm_cards", {"transcript_id": lecture}, transcript_id=lecture)
    with pytest.raises(ValueError, match="usable JSON|did not return"):
        jobs.run_cards(job_id, {"transcript_id": lecture, "provider_id": provider_id})
    assert db.list_cards(lecture) == []


def test_a_response_with_no_cards_is_not_an_error(monkeypatch, lecture):
    """JSON valido ma vuoto: nessuna carta, e nessuna eccezione da mostrare."""
    result, _calls = _run(monkeypatch, lecture, payload={"cards": []})
    assert result["n_new"] == 0
    assert db.latest_card_run(lecture)["status"] == "empty"


def test_the_cost_of_the_run_is_recorded(monkeypatch, lecture):
    """Il ledger delle carte: token e costo finiscono sul run e sul job."""
    result, _calls = _run(monkeypatch, lecture)
    run = db.latest_card_run(lecture)
    assert run["tokens_in"] == 900 and run["tokens_out"] == 300
    assert run["cost_usd"] > 0
    assert run["inputs_hash"] == result["run_id"] or run["inputs_hash"]


# ── Stima locale ─────────────────────────────────────────────────────────────
def test_the_estimate_is_local_and_counts_the_images(lecture, tmp_path):
    """
    Con la vision mode ogni immagine vale circa 1000 token: se la stima non le
    contasse, il preventivo mostrato prima di premere "Genera" sarebbe una bugia.
    """
    doc = db.list_slide_docs(db.get_transcript(lecture)["project_id"])[0]
    render = tmp_path / "p.png"
    render.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    db.update_slide_page(doc["id"], 25, {"render_path": str(render)})

    without = cards.estimate(db.get_transcript(lecture), _provider("ollama"),
                             config.load_settings(), vision=False)
    with_images = cards.estimate(db.get_transcript(lecture), _provider("ollama"),
                                 config.load_settings(), vision=True)

    assert without["images"] == 0
    assert with_images["images"] == 1
    assert with_images["tokens_in"] - without["tokens_in"] >= 1000
    assert with_images["cached"] is False


def test_a_deepseek_estimate_never_offers_images(lecture):
    """DeepSeek non legge immagini: la stima non deve promettere una vision mode."""
    est = cards.estimate(db.get_transcript(lecture), _provider("deepseek"),
                         config.load_settings(), vision=True)
    assert est["images"] == 0
    assert est["vision"] is False
    assert est["vision_available"] is False


def test_the_estimate_reports_a_cache_hit(monkeypatch, lecture):
    _result, _calls = _run(monkeypatch, lecture)
    est = cards.estimate(db.get_transcript(lecture), _provider("deepseek"),
                         config.load_settings())
    assert est["cached"] is True


# ── API ──────────────────────────────────────────────────────────────────────
def test_the_api_queues_the_generation(monkeypatch, client, lecture):
    monkeypatch.setattr(jobs, "enqueue", lambda job_id: None)
    provider_id = db.get_provider_by_name("deepseek")["id"]
    db.update_provider(provider_id, api_key_enc=llm.encrypt_key("sk-test"), enabled=True)

    res = client.post(f"/api/transcripts/{lecture}/cards",
                      json={"provider_id": provider_id})
    assert res.status_code == 201, res.text
    assert res.json()["job"]["kind"] == "llm_cards"


def test_the_api_hides_no_cards_and_reports_counts(client, lecture):
    monkeypatch_free = db.create_cards(lecture, [
        {"uid": "u1", "kind": "qa", "front": "Domanda?", "back": "Risposta."}])
    res = client.get(f"/api/transcripts/{lecture}/cards")
    body = res.json()
    assert len(body["cards"]) == 1
    assert body["counts"]["total"] == 1
    assert body["n_slide_links"] == 1
    assert monkeypatch_free


def test_editing_a_card_marks_it_edited(client, lecture):
    card_id = db.create_cards(lecture, [
        {"uid": "u2", "kind": "qa", "front": "Domanda?", "back": "Risposta."}])[0]
    res = client.patch(f"/api/cards/{card_id}", json={"front": "Domanda migliore?"})
    assert res.status_code == 200
    assert res.json()["edited"] == 1


def test_a_card_can_be_suspended_and_deleted(client, lecture):
    card_id = db.create_cards(lecture, [
        {"uid": "u3", "kind": "qa", "front": "Domanda?", "back": "Risposta."}])[0]
    assert client.patch(f"/api/cards/{card_id}",
                        json={"status": "suspended"}).json()["status"] == "suspended"
    assert client.patch(f"/api/cards/{card_id}",
                        json={"status": "boh"}).status_code == 422
    assert client.delete(f"/api/cards/{card_id}").status_code == 200
    assert db.list_cards(lecture) == []


def test_the_estimate_endpoint_needs_no_network(client, lecture):
    res = client.get(f"/api/transcripts/{lecture}/cards/estimate")
    assert res.status_code == 200
    body = res.json()
    assert body["cards_max"] == 12
    assert "tokens_in" in body
