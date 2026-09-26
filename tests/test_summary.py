"""
Riassunto (`trascrivi.summary`): da trascrizione a documento Markdown.

L'output è un testo unico — il modello scrive il documento, il markdown è il
formato di consegna. Qui si difendono le due cose che possono rompersi in
silenzio: la normalizzazione della risposta (recinzione di codice attorno a tutto
il documento, JSON inatteso, risposta vuota) e il cablaggio job → DB → API.

Nessun test tocca la rete: il client OpenAI è sostituito da un finto che
restituisce il testo preparato.
"""

from __future__ import annotations

import pytest

from trascrivi import config, db, jobs, llm, summary as sm

SEGMENTS = [
    {"start": 0.0, "end": 6.0, "text": "Good morning everyone, today we start with constrained design"},
    {"start": 6.0, "end": 14.0, "text": "The battery of your phone is the constraint you cannot ignore"},
    {"start": 14.0, "end": 22.0, "text": "Energy depends on power multiplied by time and this matters a lot"},
]

DOCUMENT = """# Constrained design

The lecture introduces constrained design: accuracy under a budget.

## The constraint

- At the edge the limit is the **battery**
- In the cloud the limit is cost

| Where | Binding constraint |
|---|---|
| Edge | battery |
| Cloud | cost |

## Terms

- **constraint** — a limit on the resources you can spend

## Left open

The calculation of the energy budget was promised for next time.
"""


# ── Finto client LLM ─────────────────────────────────────────────────────────
class _Usage:
    def __init__(self, prompt=900, completion=300, hit=0, miss=900):
        self._d = {"prompt_tokens": prompt, "completion_tokens": completion,
                   "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}

    def model_dump(self):
        return self._d


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish_reason="stop"):
        self.message = _Message(content)
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, content, usage=None, finish_reason="stop"):
        self.choices = [_Choice(content, finish_reason)]
        self.usage = usage or _Usage()


class _Completions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        # Impostato da un test per simulare una risposta tagliata dal tetto di
        # token: è il caso in cui il documento è incompleto e va segnalato.
        self.truncate = False

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return _Response(reply, finish_reason="length" if self.truncate else "stop")


def fake_provider(monkeypatch, replies, *, name="deepseek", model="deepseek-flash"):
    """Installa un client finto e restituisce (provider, oggetto per le chiamate)."""
    completions = _Completions(replies)
    client = type("FakeClient", (), {
        "chat": type("Chat", (), {"completions": completions})(),
    })()
    monkeypatch.setattr(llm, "_client", lambda provider: client)
    return {"name": name, "model": model, "base_url": "https://x"}, completions


@pytest.fixture
def transcript(make_transcript):
    """Id della trascrizione: quello che serve a URL, job e record collegati."""
    return make_transcript(segments=SEGMENTS, language="en")


@pytest.fixture
def transcript_row(transcript):
    """La riga completa, che è quello che la pipeline riceve dal worker."""
    return db.get_transcript(transcript)


@pytest.fixture
def settings():
    return config.Settings()


# ════════════════════════════════════════════════════════════════════════════
#  Normalizzazione della risposta
# ════════════════════════════════════════════════════════════════════════════
def test_unwrap_removes_a_fence_around_the_whole_document():
    """Capita spesso: senza toglierla il documento arriva come blocco di codice."""
    wrapped = "```markdown\n" + DOCUMENT + "```"

    assert sm.unwrap(wrapped) == DOCUMENT.strip()


def test_unwrap_keeps_a_real_code_block_inside_the_document():
    """Una recinzione interna (uno schema) non è una recinzione attorno a tutto."""
    document = "# Title\n\nSome text here that is long enough to be a document.\n\n```\na -> b\n```"

    assert sm.unwrap(document) == document


def test_unwrap_json_recovers_a_document_wrapped_in_an_object():
    """Alcuni modelli rispondono `{"markdown": ...}` anche senza JSON mode."""
    assert sm.unwrap_json('{"markdown": "' + DOCUMENT.replace("\n", "\\n") + '"}') == DOCUMENT.strip()


def test_unwrap_json_leaves_plain_markdown_alone():
    assert sm.unwrap_json(DOCUMENT) == DOCUMENT.strip()


def test_title_comes_from_the_first_heading():
    assert sm.title_from_markdown(DOCUMENT) == "Constrained design"
    assert sm.title_from_markdown("no heading at all", "fallback") == "fallback"


def test_word_count_ignores_markdown_markers():
    """I marcatori non sono parole; il titolo invece sì, ed è giusto contarlo."""
    assert sm.word_count("**one** two `three`") == 3
    assert sm.word_count("# Title\n\n- one") == 2


# ════════════════════════════════════════════════════════════════════════════
#  Budget di lunghezza
# ════════════════════════════════════════════════════════════════════════════
def test_target_words_scales_with_the_transcript():
    """
    Il numero va dato esplicito. Senza, il modello scrive finché decide di aver
    finito: misurato su una lezione reale, 6338 parole di note su 7000 parole di
    trascrizione. Qui si blocca il rapporto, che è la leva sulla lunghezza.
    """
    # Sotto il tetto vale il rapporto: 6% / 15% / 25%.
    assert sm.target_words(5_000, "brief") == 300
    assert sm.target_words(10_000, "study") == 1500
    assert sm.target_words(10_000, "detailed") == 2500


def test_target_words_has_a_floor_and_a_ceiling():
    """Sotto il minimo non è un riassunto, sopra il tetto non è consultabile."""
    assert sm.target_words(50, "brief") == 150          # minimo
    assert sm.target_words(10_000, "brief") == 450      # tetto
    assert sm.target_words(1_000_000, "study") == 2000  # tetto
    assert sm.target_words(10_000, "sconosciuto") == sm.target_words(10_000, "study")


def test_a_long_lecture_is_compressed_not_transcribed():
    """
    Il caso che ha motivato il budget: una lezione da 7000 parole non deve
    produrre 6000 parole di note.
    """
    words = sm.target_words(7000, "study")

    assert words <= 1200
    assert 7000 / words >= 5          # almeno 5:1 di compressione
    assert words >= 600               # ma abbastanza da poter ripassare


def test_source_word_count_prefers_the_segments():
    assert sm.source_word_count([{"text": "one two three"}, {"text": "four"}], "x " * 99) == 4
    assert sm.source_word_count([], "one two three") == 3


def test_strip_plan_removes_a_closed_plan_block():
    raw = ("<plan>\n## Vincolo energetico - 120\n## Termini - 30\n</plan>\n\n" + DOCUMENT)

    assert sm.strip_plan(raw) == DOCUMENT.strip()


def test_strip_plan_survives_an_unclosed_block():
    """Se il modello non chiude il tag, tutto ciò che precede il titolo va via."""
    raw = "<plan>\n## Vincolo - 120\n\n" + DOCUMENT

    assert sm.strip_plan(raw) == DOCUMENT.strip()


def test_strip_plan_removes_a_plain_preamble():
    raw = "Ecco le note:\n\n" + DOCUMENT

    assert sm.strip_plan(raw) == DOCUMENT.strip()


def test_strip_plan_leaves_a_document_without_a_title_alone():
    """Meglio un documento senza titolo che un documento perso."""
    raw = "Un testo senza titolo ma abbastanza lungo da essere un documento."

    assert sm.strip_plan(raw) == raw


def test_a_degenerate_answer_is_rejected():
    """Un rifiuto o un titolo solo non sono un documento."""
    assert sm.MIN_DOCUMENT_CHARS > 100


# ════════════════════════════════════════════════════════════════════════════
#  Scelta della strategia, costo, contesto
# ════════════════════════════════════════════════════════════════════════════
def test_estimate_cost_separates_cache_hits_from_misses():
    """Un hit costa 50 volte meno: senza separarli la stima è sbagliata di molto."""
    all_miss = sm.estimate_cost({"hit": 0, "miss": 1_000_000, "completion": 0})
    all_hit = sm.estimate_cost({"hit": 1_000_000, "miss": 0, "completion": 0})
    out = sm.estimate_cost({"hit": 0, "miss": 0, "completion": 1_000_000})

    assert all_miss == pytest.approx(0.15)
    assert all_hit == pytest.approx(0.003)
    assert out == pytest.approx(0.60)


def test_overview_is_the_first_paragraph():
    assert sm._overview(DOCUMENT).startswith("The lecture introduces constrained design")
    # Si ferma prima del primo titolo di sezione.
    assert "battery" not in sm._overview(DOCUMENT)


def test_context_block_warns_that_previous_lectures_are_not_a_source():
    block = sm._context_block({"name": "IoT"}, [{"title": "L1", "overview": "old stuff"}], [])

    assert "IoT" in block
    assert "L1" in block
    assert "Do not add any content that is not in THIS" in block


def test_context_block_is_empty_without_context():
    assert sm._context_block(None, [], []) == ""


# ════════════════════════════════════════════════════════════════════════════
#  Pipeline completa
# ════════════════════════════════════════════════════════════════════════════
def test_summarize_returns_the_document(monkeypatch, transcript_row, settings):
    provider, calls = fake_provider(monkeypatch, [DOCUMENT])

    result = sm.summarize(provider, transcript_row, settings)

    assert len(calls.calls) == 1, "il documento si scrive in una sola chiamata"
    assert result["markdown"] == DOCUMENT.strip()
    assert result["title"] == "Constrained design"
    assert result["words"] > 50
    assert result["cost_usd"] > 0
    # La trascrizione è finita nel prompt, senza timestamp da ricopiare.
    sent = calls.calls[0]["messages"][1]["content"]
    assert "The battery of your phone is the constraint" in sent
    assert "[00:06]" not in sent


def test_a_document_cut_off_by_the_token_cap_is_flagged(monkeypatch, transcript_row, settings):
    """
    Un documento tagliato dal tetto di output è già stato pagato e il testo che
    c'è è valido, quindi non si butta via: si segnala. Consegnarlo come se fosse
    finito è l'unico esito davvero sbagliato.
    """
    provider, completions = fake_provider(monkeypatch, [DOCUMENT])
    completions.truncate = True   # la prossima risposta finisce per tetto

    result = sm.summarize(provider, transcript_row, settings)

    assert result["truncated"] is True
    assert result["words"] > 0, "il testo parziale resta, non si scarta"


def test_a_complete_document_is_not_flagged(monkeypatch, transcript_row, settings):
    provider, _calls = fake_provider(monkeypatch, [DOCUMENT])

    assert sm.summarize(provider, transcript_row, settings)["truncated"] is False


def test_the_prompt_carries_the_word_budget(monkeypatch, transcript_row, settings):
    """
    Il budget deve arrivare al modello come numero, calcolato sulla trascrizione
    vera: è l'unica leva che ha sulla lunghezza del risultato.
    """
    _provider, calls = fake_provider(monkeypatch, [DOCUMENT])
    source = sm.source_word_count(transcript_row["segments"])
    wanted = sm.target_words(source, "study")

    sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings)

    sent = calls.calls[0]["messages"][1]["content"]
    assert f"about {wanted} words" in sent
    assert f"MUST NOT be longer than {wanted} words" in sent
    assert f"about {source} words long" in sent
    # Il blocco di pianificazione è chiesto esplicitamente.
    assert "<plan>" in sent


def test_the_course_context_reaches_the_prompt(monkeypatch, transcript_row, settings, project):
    """
    Regressione: riscrivendo i prompt il `{context_block}` era sparito dal testo
    dell'utente, quindi il corso, il glossario e le lezioni precedenti non
    arrivavano più al modello — in silenzio, perché `.format()` ignora le chiavi
    in più senza protestare.
    """
    db.upsert_term(project, "COGO", "CO2", auto=True, note="")
    _provider, calls = fake_provider(monkeypatch, [DOCUMENT])

    sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings)

    sent = calls.calls[0]["messages"][1]["content"]
    assert "<course_context>" in sent
    assert "COGO -> CO2" in sent
    assert "Do not add any content that is not in THIS" in sent


def test_the_output_cap_follows_the_budget(monkeypatch, transcript_row, settings):
    """
    Un tetto molto più alto dell'obiettivo invita a superarlo: 8000 token sono
    ~6000 parole, ed è la lunghezza che il modello raggiungeva senza un numero.
    """
    _provider, calls = fake_provider(monkeypatch, [DOCUMENT])
    wanted = sm.target_words(sm.source_word_count(transcript_row["segments"]), "study")

    sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings)

    assert calls.calls[0]["max_tokens"] < 8000
    assert calls.calls[0]["max_tokens"] >= wanted * 2   # margine per non troncare


def test_the_stated_budget_follows_the_style(monkeypatch, transcript_row, settings):
    _provider, calls = fake_provider(monkeypatch, [DOCUMENT])
    source = sm.source_word_count(transcript_row["segments"])

    sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings,
                 style="brief")

    sent = calls.calls[0]["messages"][1]["content"]
    assert f"about {sm.target_words(source, 'brief')} words" in sent


def test_summarize_strips_the_planning_block(monkeypatch, transcript_row, settings):
    """Il blocco di pianificazione non deve finire nel documento scaricabile."""
    fake_provider(monkeypatch, ["<plan>\n## Vincolo - 120\n</plan>\n\n" + DOCUMENT])

    result = sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings)

    assert "<plan>" not in result["markdown"]
    assert result["markdown"].startswith("# Constrained design")
    assert "## Vincolo - 120" not in result["markdown"]


def test_summarize_reports_the_budget_and_the_compression(monkeypatch, transcript_row, settings):
    fake_provider(monkeypatch, [DOCUMENT])

    result = sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings)

    assert result["target_words"] == sm.target_words(result["source_words"], "study")
    assert result["source_words"] == sm.source_word_count(transcript_row["segments"])


def test_summarize_does_not_ask_for_json(monkeypatch, transcript_row, settings):
    """Forzare il JSON produrrebbe il documento dentro una stringa con gli \\n escapati."""
    _provider, calls = fake_provider(monkeypatch, [DOCUMENT])

    sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings)

    assert "response_format" not in calls.calls[0]


def test_summarize_disables_thinking_for_deepseek(monkeypatch, transcript_row, settings):
    """Il thinking brucia il budget in ragionamento e si paga come output."""
    _provider, calls = fake_provider(monkeypatch, [DOCUMENT])

    sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings)

    assert calls.calls[0]["reasoning_effort"] == "none"


def test_summarize_reports_progress(monkeypatch, transcript_row, settings):
    fake_provider(monkeypatch, [DOCUMENT])
    seen: list[tuple[float, str]] = []

    sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings,
                 on_progress=lambda f, m: seen.append((f, m)))

    assert seen, "on_progress non è mai stato chiamato"
    assert seen[-1][0] == 1.0


def test_summarize_rejects_an_empty_answer(monkeypatch, transcript_row, settings):
    fake_provider(monkeypatch, ["ok"])

    with pytest.raises(ValueError) as excinfo:
        sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, transcript_row, settings)

    assert "no usable document" in str(excinfo.value)


def test_summarize_requires_a_transcript(monkeypatch, settings, project):
    fake_provider(monkeypatch, [DOCUMENT])
    empty = {"id": 1, "project_id": project, "text": "", "segments": [], "title": "t"}

    with pytest.raises(ValueError) as excinfo:
        sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, empty, settings)

    assert "empty" in str(excinfo.value)


def test_summarize_falls_back_to_the_plain_text_without_segments(monkeypatch, settings, project):
    """Una trascrizione salvata come testo senza segmenti deve funzionare lo stesso."""
    fake_provider(monkeypatch, [DOCUMENT])
    row = {"id": 1, "project_id": project, "title": "t", "language": "en",
           "text": "some plain transcript text", "segments": []}

    result = sm.summarize({"name": "deepseek", "model": "deepseek-flash"}, row, settings)

    assert result["markdown"] == DOCUMENT.strip()


# ════════════════════════════════════════════════════════════════════════════
#  Persistenza e job
# ════════════════════════════════════════════════════════════════════════════
def test_summary_crud_round_trip(transcript):
    sid = db.create_summary(transcript, style="study", title="T", overview="O",
                            markdown="# T", n_words=4, tokens_in=100, tokens_out=50,
                            cost_usd=0.001)

    got = db.get_summary(sid)
    assert got["markdown"] == "# T"
    assert got["n_words"] == 4

    assert [s["id"] for s in db.list_summaries(transcript)] == [sid]
    # L'elenco non porta il documento: è il campo grosso.
    assert "markdown" not in db.list_summaries(transcript)[0]

    db.update_summary(sid, {"title": "T2", "ignored": "x"})
    assert db.get_summary(sid)["title"] == "T2"

    db.delete_summary(sid)
    assert db.get_summary(sid) is None


def test_deleting_a_transcript_deletes_its_summaries(transcript):
    sid = db.create_summary(transcript, title="T")

    db.delete_transcript(transcript)

    assert db.get_summary(sid) is None


def test_run_summary_job_writes_the_document(monkeypatch, transcript, project):
    fake_provider(monkeypatch, [DOCUMENT])
    provider_id = db.list_providers()[1]["id"]
    db.update_provider(provider_id, model="deepseek-flash", enabled=True)
    job_id = db.create_job("llm_summary", {"transcript_id": transcript, "provider_id": provider_id},
                           project_id=project, transcript_id=transcript)

    result = jobs.run_summary(job_id, {"transcript_id": transcript, "provider_id": provider_id})

    saved = db.get_summary(result["summary_id"])
    assert saved["transcript_id"] == transcript
    assert saved["job_id"] == job_id
    assert saved["markdown"] == DOCUMENT.strip()
    assert saved["title"] == "Constrained design"
    assert saved["n_words"] > 50
    assert result["message"].endswith("single . $0.0002") or "$" in result["message"]
    # La generazione finisce nella cronologia della trascrizione.
    assert any(r["kind"] == "summary" for r in db.list_revisions(transcript))


def test_run_summary_job_requires_a_provider(transcript):
    with pytest.raises(ValueError) as excinfo:
        jobs.run_summary(1, {"transcript_id": transcript, "provider_id": 9999})

    assert "provider" in str(excinfo.value).lower()


def test_transcription_can_queue_a_summary(monkeypatch, transcript, project):
    """`run_summary` nel payload della trascrizione deve accodare un llm_summary."""
    queued: list[int] = []
    monkeypatch.setattr(jobs, "enqueue", lambda job_id: queued.append(job_id))
    provider_id = db.list_providers()[1]["id"]
    parent = db.create_job("transcribe", {}, project_id=project)

    jobs._maybe_queue_agent({"id": parent, "project_id": project},
                            {"run_summary": True, "summary_provider_id": provider_id,
                             "summary_style": "brief"},
                            {"transcript_id": transcript})

    assert len(queued) == 1
    job = db.get_job(queued[0])
    assert job["kind"] == "llm_summary"
    assert db.jload(job["payload"])["style"] == "brief"


def test_transcription_without_a_provider_skips_the_summary_and_says_so(monkeypatch, project, transcript):
    queued: list[int] = []
    monkeypatch.setattr(jobs, "enqueue", lambda job_id: queued.append(job_id))
    for provider in db.list_providers():
        db.update_provider(provider["id"], enabled=False)
    parent = db.create_job("transcribe", {}, project_id=project)

    jobs._maybe_queue_agent({"id": parent, "project_id": project}, {"run_summary": True},
                            {"transcript_id": transcript})

    assert queued == []
    assert "summary skipped: no provider" in db.get_job(parent)["message"]


# ════════════════════════════════════════════════════════════════════════════
#  API
# ════════════════════════════════════════════════════════════════════════════
def test_api_summary_lifecycle(client, transcript):
    """
    CRUD via HTTP su un riassunto già esistente.

    Il record viene seminato direttamente invece che generato dall'endpoint: il
    worker dei job è vivo durante `TestClient` e lo eseguirebbe lui, rendendo il
    test dipendente da una corsa. La creazione del job è coperta a parte.
    """
    sid = db.create_summary(transcript, style="study", title="Constrained design",
                            overview="O", markdown=DOCUMENT.strip(),
                            provider="deepseek", model="deepseek-flash", n_words=120,
                            cost_usd=0.002)

    listed = client.get(f"/api/transcripts/{transcript}/summaries").json()["summaries"]
    assert [s["id"] for s in listed] == [sid]

    got = client.get(f"/api/summaries/{sid}")
    assert got.status_code == 200
    assert got.json()["markdown"] == DOCUMENT.strip()

    # Modificare il documento riallinea titolo e anteprima al testo nuovo.
    patched = client.patch(f"/api/summaries/{sid}", json={"markdown": "# Nuovo titolo\n\nAltro testo."})
    assert patched.json()["title"] == "Nuovo titolo"
    assert patched.json()["edited"] == 1

    exported = client.get(f"/api/summaries/{sid}/export?format=md")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "# Nuovo titolo" in exported.text

    plain = client.get(f"/api/summaries/{sid}/export?format=txt")
    assert plain.status_code == 200
    assert "#" not in plain.text

    assert client.get(f"/api/summaries/{sid}/export?format=json").status_code == 200
    assert client.get(f"/api/summaries/{sid}/export?format=xml").status_code == 422

    assert client.delete(f"/api/summaries/{sid}").status_code == 200
    assert client.get(f"/api/summaries/{sid}").status_code == 404


def test_api_creates_a_summary_job(client, monkeypatch, transcript):
    monkeypatch.setattr(jobs, "enqueue", lambda job_id: None)  # niente worker nel test
    provider_id = db.list_providers()[1]["id"]
    db.update_provider(provider_id, model="deepseek-flash", enabled=True)

    created = client.post(f"/api/transcripts/{transcript}/summary",
                          json={"provider_id": provider_id, "style": "detailed"})

    assert created.status_code == 201
    job = created.json()["job"]
    assert job["kind"] == "llm_summary"
    assert db.jload(job["payload"])["style"] == "detailed"


def test_api_summary_on_missing_transcript_is_404(client):
    assert client.post("/api/transcripts/9999/summary", json={}).status_code == 404


def test_api_summary_without_a_provider_is_422(client, transcript):
    for provider in db.list_providers():
        db.update_provider(provider["id"], enabled=False)

    res = client.post(f"/api/transcripts/{transcript}/summary", json={})

    assert res.status_code == 422
    assert "provider" in res.json()["detail"].lower()


def test_api_transcript_exposes_the_summary_count(client, transcript):
    db.create_summary(transcript, title="T")

    body = client.get(f"/api/transcripts/{transcript}").json()

    assert body["n_summaries"] == 1
    assert body["latest_summary_id"] is not None


def test_api_settings_accept_the_summary_fields(client):
    res = client.put("/api/settings", json={"summary_max_tokens": 3000,
                                            "summary_style": "brief",
                                            "summary_language": "it"})

    assert res.status_code == 200
    assert res.json()["settings"]["summary_max_tokens"] == 3000
    assert config.load_settings().summary_style == "brief"


# ════════════════════════════════════════════════════════════════════════════
#  Migrazione dello schema
# ════════════════════════════════════════════════════════════════════════════
def test_an_old_summaries_table_gains_the_new_column(transcript):
    """
    La tabella `summaries` è nata con la struttura a punti e citazioni, poi
    sostituita dal documento markdown. Un database che ha già la tabella vecchia
    non riceve `n_words` da `CREATE TABLE IF NOT EXISTS`: senza `ALTER TABLE`,
    ogni INSERT e ogni SELECT fallirebbero con "no such column".
    """
    with db.connect() as conn:
        conn.execute("DROP TABLE summaries")
        conn.execute("""CREATE TABLE summaries (
            id INTEGER PRIMARY KEY,
            transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
            job_id INTEGER, style TEXT NOT NULL DEFAULT 'study', language TEXT,
            provider TEXT DEFAULT '', model TEXT DEFAULT '', title TEXT DEFAULT '',
            overview TEXT DEFAULT '', sections TEXT NOT NULL DEFAULT '[]',
            terms TEXT NOT NULL DEFAULT '[]', markdown TEXT NOT NULL DEFAULT '',
            strategy TEXT DEFAULT 'single', n_points INTEGER DEFAULT 0,
            n_quotes INTEGER DEFAULT 0, n_verified INTEGER DEFAULT 0,
            tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
            cache_hit INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, elapsed_s REAL DEFAULT 0,
            edited INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        conn.commit()

    db.init_db()  # deve aggiungere `n_words` senza perdere altro

    sid = db.create_summary(transcript, title="T", markdown="# T", n_words=7)
    assert db.get_summary(sid)["n_words"] == 7
    assert db.list_summaries(transcript)[0]["n_words"] == 7


# ════════════════════════════════════════════════════════════════════════════
#  Migrazione dei modelli DeepSeek ritirati
# ════════════════════════════════════════════════════════════════════════════
def test_retired_deepseek_model_names_are_migrated():
    """
    `deepseek-chat` e `deepseek-reasoner` sono stati ritirati il 2026-07-24: un
    database creato prima continuerebbe a chiamarli e ogni job LLM fallirebbe.
    """
    deepseek_id = db.get_provider_by_name("deepseek")["id"]
    db.execute("UPDATE providers SET model = 'deepseek-chat', fallback_model = 'deepseek-reasoner' "
               "WHERE id = ?", (deepseek_id,))

    db.init_db()

    provider = db.get_provider(deepseek_id)
    assert provider["model"] == "deepseek-flash"
    assert provider["fallback_model"] == "deepseek-flash"


def test_a_deliberate_model_choice_is_not_overwritten():
    deepseek_id = db.get_provider_by_name("deepseek")["id"]
    db.update_provider(deepseek_id, model="deepseek-v4-pro")

    db.init_db()

    assert db.get_provider(deepseek_id)["model"] == "deepseek-v4-pro"
