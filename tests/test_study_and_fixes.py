"""
Regressioni dei bug corretti a novembre 2026 e le funzioni di studio sul corso
(guida d'esame, "Chiedi al corso", esami passati, slide nel riassunto).

Nessuna chiamata di rete: il client LLM è finto (`fake_provider`).
"""

from __future__ import annotations

from trascrivi import ask, config, db, guide, jobs, matching, summary
from trascrivi.api import _download_headers

from tests.test_summary import DOCUMENT, fake_provider

SEGS = [
    {"start": 0.0, "end": 5.0, "text": "Today we talk about loop tiling and the roofline model."},
    {"start": 5.0, "end": 10.0, "text": "Tiling keeps the working set in the cache."},
    {"start": 70.0, "end": 75.0, "text": "Layer fusion avoids storing intermediate results."},
]


def _provider_id():
    pid = db.list_providers()[1]["id"]
    db.update_provider(pid, model="deepseek-flash", enabled=True)
    return pid


def _doc(project, kind, pages):
    doc = db.create_slide_doc(project, f"{kind}.pdf", ".pdf", f"sha-{kind}", kind=kind)
    db.save_slide_pages(doc, [{"page_no": i, "text": t, "complex": 0, "has_text": 1,
                               "n_images": 0, "ocr_chars": 0, "render_path": None,
                               "render_sha": None} for i, t in enumerate(pages, start=1)])
    db.update_slide_doc(doc, {"status": "ready", "n_pages": len(pages)})
    return doc


# ── Bug fix ──────────────────────────────────────────────────────────────────
def test_resume_reads_the_partial_file_of_the_job(project):
    job = db.create_job("transcribe", {}, project_id=project)
    path = config.partial_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[00:00] a\n[05:00] b\n", encoding="utf-8")
    assert jobs.resume_start(db.get_job(job)) == 298.0


def test_a_cancelled_queued_job_is_not_claimed(project):
    job = db.create_job("transcribe", {}, project_id=project)
    db.update_job(job, status="cancelled")
    assert db.claim_job(job) is False
    other = db.create_job("transcribe", {}, project_id=project)
    assert db.claim_job(other) is True


def test_download_headers_survive_non_latin_titles():
    header = _download_headers("Lezione-λ-calcolo.md")["Content-Disposition"]
    header.encode("latin-1")  # non deve sollevare
    assert "filename*=UTF-8''Lezione-%CE%BB-calcolo.md" in header


def test_list_jobs_decodes_the_payload_and_names_the_lecture(project, make_transcript):
    tid = make_transcript(title="Lecture 7")
    db.create_job("llm_summary", {"transcript_id": tid}, project_id=project, transcript_id=tid)
    job = db.list_jobs()[0]
    assert job["payload"] == {"transcript_id": tid}
    assert job["transcript_title"] == "Lecture 7"


def test_summary_job_records_its_cost(monkeypatch, project, make_transcript):
    fake_provider(monkeypatch, [DOCUMENT])
    tid = make_transcript(segments=SEGS, language="en")
    result = jobs.run_summary(1, {"transcript_id": tid, "provider_id": _provider_id()})
    assert "usage" in result and result["usage"]["prompt"] > 0


def test_auto_matching_without_ollama_is_skipped_not_failed(monkeypatch, project):
    def boom(*a, **k):
        raise RuntimeError("Embeddings from 'ollama' failed: Connection error")
    monkeypatch.setattr(matching, "match_project", boom)
    assert "skipped" in jobs.run_embed_match(1, {"project_id": project, "auto": True})["message"]


# ── Slide nel riassunto ──────────────────────────────────────────────────────
def test_lexical_pages_pick_the_slides_of_the_lecture_and_skip_exams(project):
    _doc(project, "slides", ["Loop tiling and blocking: keep the working set in cache, roofline",
                             "Course logistics, exam dates and office hours for the students",
                             "Layer fusion: fuse linear, batch norm and ReLU in one kernel"])
    _doc(project, "exam", ["Explain loop tiling and the roofline model in detail please"])
    pages = matching.lexical_pages(project, " ".join(s["text"] for s in SEGS))
    assert pages[0]["page_no"] == 1 and 2 not in {p["page_no"] for p in pages}
    assert all(p["doc_name"] == "slides.pdf" for p in pages)


def test_the_summary_prompt_carries_the_slides(monkeypatch, project, make_transcript):
    _doc(project, "slides", ["Loop tiling: blocking keeps the working set in the cache (roofline)"])
    _provider, calls = fake_provider(monkeypatch, [DOCUMENT])
    tid = make_transcript(segments=SEGS, language="en")
    result = summary.summarize(_provider, db.get_transcript(tid), config.Settings())
    user = calls.calls[0]["messages"][1]["content"]
    assert "<slides>" in user and "Loop tiling: blocking" in user
    assert result["slide_pages"] == 1


# ── Guida d'esame ────────────────────────────────────────────────────────────
GUIDE = "# Exam guide — Analisi 1\n\n## Course map\n\n| L | Topics |\n|---|---|\n| L1 | tiling |\n\n" + \
        "## Core concepts\n\n- **Tiling** keeps the working set in cache (L1).\n" * 3


def test_guide_uses_summaries_and_past_exams(monkeypatch, project, make_transcript):
    tid = make_transcript(segments=SEGS, language="en")
    db.create_summary(tid, markdown="# Tiling\n\nNotes about tiling.", title="Tiling")
    _doc(project, "exam", ["1) Explain loop tiling. 2) What is layer fusion?"])
    _p, calls = fake_provider(monkeypatch, [GUIDE])
    job = db.create_job("llm_guide", {}, project_id=project)
    result = jobs.run_guide(job, {"project_id": project, "provider_id": _provider_id()})
    msg = calls.calls[0]["messages"]
    assert "Past exam questions" in msg[0]["content"]
    assert '<lecture id="L1"' in msg[1]["content"] and "<past_exams>" in msg[1]["content"]
    saved = db.row("SELECT * FROM guides WHERE id = ?", (result["guide_id"],))
    assert saved["job_id"] == job and saved["n_exam_pages"] == 1
    assert saved["title"] == "Exam guide — Analisi 1"


def test_guide_without_summaries_explains_why(monkeypatch, project, make_transcript):
    make_transcript(segments=SEGS)
    fake_provider(monkeypatch, [GUIDE])
    try:
        guide.generate({"name": "deepseek", "model": "m"}, project, config.Settings())
    except ValueError as exc:
        assert "summary" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# ── Chiedi al corso ──────────────────────────────────────────────────────────
def test_ask_sends_notes_and_timestamped_passages(monkeypatch, project, make_transcript):
    tid = make_transcript(segments=SEGS, language="en")
    db.create_summary(tid, markdown="# Tiling\n\nNotes.", title="Tiling")
    _p, calls = fake_provider(monkeypatch, ["Tiling keeps data in cache [L1 00:05]."])
    out = ask.answer({"name": "deepseek", "model": "m"}, project, "What does layer fusion avoid?")
    system, user = (m["content"] for m in calls.calls[0]["messages"])
    assert "<course_notes>" in system and "Notes." in system
    assert "[L1 00:00]" in user and "Layer fusion" in user  # passaggio con il minuto
    assert out["sources"]["lectures"][0] == {"label": "L1", "transcript_id": tid, "title": "Tiling"}
    assert ask.history(project)[0]["id"] == out["id"]


def test_exam_upload_kind_is_validated(client, project):
    res = client.post(f"/api/projects/{project}/slides", data={"kind": "nope"},
                      files={"file": ("e.txt", b"Q1. Explain tiling.", "text/plain")})
    assert res.status_code == 422
    res = client.post(f"/api/projects/{project}/slides", data={"kind": "exam"},
                      files={"file": ("e.txt", b"Q1. Explain tiling.", "text/plain")})
    assert res.status_code == 201
    assert db.get_slide_doc(res.json()["slide"]["id"])["kind"] == "exam"


# ── Migliorie UX: numerazione, stato di studio, ricerca, coda, budget ────────
def test_lectures_are_numbered_in_course_order_and_share_labels(project, make_transcript):
    first = make_transcript(title="a", segments=SEGS)
    second = make_transcript(title="b", segments=SEGS)
    db.create_summary(second, markdown="# B\n\nNotes.", title="B")
    rows = {r["id"]: r for r in db.list_transcripts(project)}
    assert (rows[first]["lecture_no"], rows[second]["lecture_no"]) == (1, 2)
    # La guida e Ask usano lo stesso numero anche se L1 non ha riassunto.
    assert [r["label"] for r in guide.lectures(project)] == ["L2"]


def test_study_status_is_validated_and_saved(client, project, make_transcript):
    tid = make_transcript(segments=SEGS)
    assert client.patch(f"/api/transcripts/{tid}", json={"study_status": "nope"}).status_code == 422
    assert client.patch(f"/api/transcripts/{tid}", json={"study_status": "reviewed"}).status_code == 200
    assert db.list_transcripts(project)[0]["study_status"] == "reviewed"
    body = client.get(f"/api/transcripts/{tid}").json()
    assert body["lecture_no"] == 1 and body["display_title"]


def test_search_points_at_the_segment_and_covers_summaries(client, project, make_transcript):
    tid = make_transcript(segments=SEGS)
    db.create_summary(tid, markdown="# T\n\nLayer fusion explained.", title="T")
    body = client.get("/api/search", params={"q": "fusion"}).json()
    assert body["results"][0]["at"] == 70.0
    assert body["summaries"][0]["transcript_id"] == tid
    assert "[[fusion]]" in body["summaries"][0]["snippet"].lower()


def test_queue_eta_counts_queued_audio(project):
    src = db.create_source(project, "x.mp3", "x.mp3", None, 1, 600.0, "h")
    assert jobs.queue_eta() is None
    db.create_job("transcribe", {"source_id": src}, project_id=project)
    db.create_job("llm_summary", {}, project_id=project)
    assert jobs.queue_eta() == round(600 / 10.0 + 20 + 40)


def test_costs_report_the_budget(client, mock_settings):
    mock_settings(monthly_budget_usd=2.5)
    body = client.get("/api/costs").json()
    assert body["budget_usd"] == 2.5 and body["month_total_usd"] == 0


def test_course_vocabulary_keeps_terms_and_drops_shouted_words(project):
    _doc(project, "slides", ["WHAT is GEMM? the Abernathy-Utterback model, PyTorch", "GEMM tiling, what PyTorch does",
                             "Abernathy-Utterback again. WHAT else? II", "II II"])
    db.upsert_term(project, "pie torch", "PyTorch", auto=True, note="")
    db.upsert_term(project, "Meta", "technology company involved in lawsuits", auto=False, note="Meta")
    vocab = matching.course_vocabulary(project).split(", ")
    assert "GEMM" in vocab and "Abernathy-Utterback" in vocab and "PyTorch" in vocab
    assert "WHAT" not in vocab and "II" not in vocab
    assert not any("lawsuits" in v for v in vocab), "una nota del glossario non è vocabolario"


def test_a_lecture_with_too_little_text_is_flagged(project, make_transcript):
    full = [{"start": i * 10.0, "end": i * 10.0 + 9, "text": "word " * 40} for i in range(120)]
    for title in ("a", "b", "c"):
        make_transcript(title=title, segments=full, duration=1200.0)
    thin = make_transcript(title="thin", segments=full[:30], duration=1200.0)
    flags = {t["id"]: t["low_coverage"] for t in db.list_transcripts(project)}
    assert flags[thin] is True and sum(flags.values()) == 1


def test_only_confirmed_glossary_terms_reach_the_summary_prompt(monkeypatch, project, make_transcript):
    db.upsert_term(project, "Trez", "Threads", auto=True, note="")
    db.upsert_term(project, "AI", "artificial intelligence", auto=False, note="AI")
    _p, calls = fake_provider(monkeypatch, [DOCUMENT])
    tid = make_transcript(segments=SEGS, language="en")
    summary.summarize(_p, db.get_transcript(tid), config.Settings())
    user = calls.calls[0]["messages"][1]["content"]
    assert "Trez -> Threads" in user and "artificial intelligence" not in user


# ── Review: regressioni ──────────────────────────────────────────────────────
def test_a_new_job_never_inherits_a_stale_partial(project):
    first = db.create_job("transcribe", {}, project_id=project)
    path = config.partial_path(first)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[20:00] old lecture\n", encoding="utf-8")
    db.execute("DELETE FROM jobs WHERE id = ?", (first,))
    again = db.create_job("transcribe", {}, project_id=project)
    assert again == first and not path.exists()
    assert jobs.resume_start(db.get_job(again)) == 0.0


def test_regenerating_a_summary_keeps_the_old_cost(project, make_transcript):
    tid = make_transcript(segments=SEGS)
    db.create_summary(tid, markdown="# A\n\nold", title="A", cost_usd=0.05)       # job senza token
    job = db.create_job("llm_summary", {}, project_id=project, transcript_id=tid)
    db.update_job(job, status="done", finished_at=db.now(), tokens_in=100, cost_usd=0.03)
    db.create_summary(tid, job_id=job, markdown="# B\n\nnew", title="B", cost_usd=0.03)
    total = sum(c["cost_usd"] for c in db.llm_cost_cells()["cells"] if c["project_id"] == project)
    assert round(total, 4) == 0.08


def test_study_status_does_not_touch_updated_at(project, make_transcript):
    tid = make_transcript(segments=SEGS)
    before = db.get_transcript(tid)["updated_at"]
    db.update_transcript(tid, {"study_status": "reviewed"})
    assert db.get_transcript(tid)["updated_at"] == before


def test_summary_search_folds_accented_capitals(project, make_transcript):
    tid = make_transcript(segments=SEGS)
    db.create_summary(tid, markdown="# T\n\nLa città e l'èlite.", title="T")
    assert db.search_summaries("CITTÀ")[0]["transcript_id"] == tid


def test_the_same_pdf_can_be_slides_and_exam(project):
    db.create_slide_doc(project, "x.pdf", ".pdf", "same-sha", kind="slides")
    assert db.find_duplicate_slide(project, "same-sha", "exam") is None
    assert db.find_duplicate_slide(project, "same-sha", "slides") is not None


def test_queued_jobs_survive_a_restart(project):
    queued = db.create_job("transcribe", {}, project_id=project)
    running = db.create_job("transcribe", {}, project_id=project)
    db.update_job(running, status="running")
    db.mark_interrupted_jobs()
    assert db.get_job(queued)["status"] == "queued"
    assert db.get_job(running)["status"] == "interrupted"
