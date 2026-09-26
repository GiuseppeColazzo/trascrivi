"""
Vista costi: registrazione sul job e aggregazione per corso e mese.

Il pezzo delicato è la provenienza del dato. Prima di questa funzione il costo
esisteva solo su `summaries`, quindi le correzioni non erano contabilizzabili; e
un job che non riporta token non ha costo zero, ha costo *ignoto*. I test qui
sotto bloccano entrambe le cose: da dove arriva il numero, e cosa si mostra
quando il numero non c'è.
"""

from __future__ import annotations

from trascrivi import db, jobs, llm


def _job(project_id: int, kind: str, *, cost: float, tokens: int = 0,
         status: str = "done", finished_at: float | None = None,
         transcript_id: int | None = None) -> int:
    """Un job concluso con il suo costo, come lo lascia `_run_job`."""
    job_id = db.create_job(kind, {}, project_id=project_id, transcript_id=transcript_id)
    db.update_job(job_id, status=status, cost_usd=cost, tokens_in=tokens,
                  finished_at=finished_at if finished_at is not None else db.now())
    return job_id


# ── La stima ─────────────────────────────────────────────────────────────────
def test_a_call_without_tokens_has_no_price():
    """
    Zero e "non lo so" sono cose diverse: un provider che non espone i token non
    ha un costo di zero dollari, e la vista costi deve poterlo dire.
    """
    assert llm.estimate_cost({"hit": 0, "miss": 0, "completion": 0}) is None
    assert llm.estimate_cost({"hit": 0, "miss": 1_000_000, "completion": 0}) == 0.15
    assert llm.usage_counts({}) == {"tokens_in": 0, "tokens_out": 0,
                                    "cache_hit": 0, "cost_usd": 0.0}


# ── Aggregazione ─────────────────────────────────────────────────────────────
def _cells(match=None) -> list[dict]:
    """Le celle (corso, mese, tipo) così come le legge la vista costi."""
    out = db.llm_cost_cells()["cells"]
    return [c for c in out if match is None or match(c)]


def _course_total(project_id: int) -> float:
    return round(sum(c["cost_usd"] for c in _cells(lambda c: c["project_id"] == project_id)), 6)


def test_costs_are_grouped_by_course_month_and_kind(project, make_transcript):
    """La correzione è un job, il riassunto è un job *e* una riga: si contano una volta."""
    other = db.create_project("Fisica 2", code="FIS2")
    t1 = make_transcript(project, "Lezione 1")
    t2 = make_transcript(other, "Lezione 1")

    _job(project, "llm_fix", cost=0.25, tokens=3000, transcript_id=t1)
    _job(project, "llm_summary", cost=0.50, tokens=4000, transcript_id=t1)
    _job(other, "llm_summary", cost=1.00, tokens=9000, transcript_id=t2)
    # Ignorato: non è una chiamata a pagamento.
    _job(project, "transcribe", cost=0.0)

    fix = next(c for c in _cells() if c["project_id"] == project and c["kind"] == "llm_fix")
    summary = next(c for c in _cells() if c["project_id"] == project and c["kind"] == "llm_summary")
    assert (fix["cost_usd"], summary["cost_usd"]) == (0.25, 0.50)
    assert (fix["n_jobs"], fix["tokens"]) == (1, 3000)
    assert _course_total(project) == 0.75
    assert _course_total(other) == 1.00
    assert not _cells(lambda c: c["kind"] == "transcribe"), "una trascrizione locale non costa"

    # Due mesi distinti, in due celle diverse.
    jan = 1_767_225_600.0   # 2026-01-01 UTC
    feb = 1_769_904_000.0   # 2026-02-01 UTC
    old = db.create_project("Vecchio corso")
    _job(old, "llm_summary", cost=0.10, tokens=1000, finished_at=jan)
    _job(old, "llm_summary", cost=0.20, tokens=2000, finished_at=feb)

    months = sorted(c["month"] for c in _cells(lambda c: c["project_id"] == old))
    assert months == ["2026-01", "2026-02"], months


def test_a_summary_written_before_jobs_recorded_costs_is_still_counted(project, make_transcript):
    """
    Un'istanza che ha già speso non deve mostrare zero solo perché il costo vive
    nella tabella dei riassunti e non in quella dei job: è il caso reale di ogni
    installazione precedente a questa funzione.
    """
    t = make_transcript(project, "Lezione 1")
    db.create_summary(t, markdown="# L", n_words=10, tokens_in=800, tokens_out=200,
                      cost_usd=0.02)

    cells = _cells(lambda c: c["project_id"] == project)
    assert len(cells) == 1, cells
    assert cells[0]["cost_usd"] == 0.02
    assert cells[0]["kind"] == "llm_summary"


def test_a_summary_covered_by_a_costing_job_is_counted_once(project, make_transcript):
    """Il riassunto di oggi esiste in due tabelle: si contabilizza una volta sola.

    Il caso che ha rotto la prima versione è il vicino: un job di riassunto
    registrato *senza* costo (colonna appena introdotta), che non deve far
    scartare il riassunto che il costo ce l'ha.
    """
    t = make_transcript(project, "Lezione 1")
    _job(project, "llm_summary", cost=0.30, tokens=5000, transcript_id=t)
    db.create_summary(t, markdown="# L", n_words=10, tokens_in=4000, tokens_out=1000,
                      cost_usd=0.30)

    assert _course_total(project) == 0.30

    # Una seconda lezione: job senza costo, riassunto con il costo. Vale il
    # riassunto, una volta sola.
    other = make_transcript(project, "Lezione 2")
    _job(project, "llm_summary", cost=0.0, tokens=0, transcript_id=other)
    db.create_summary(other, markdown="# L2", n_words=10, tokens_in=900, tokens_out=200,
                      cost_usd=0.05)

    assert _course_total(project) == 0.35


def test_a_job_that_reported_no_tokens_costs_nothing_to_report(project, make_transcript):
    """
    Un provider che non espone i token non ha un costo: zero, non "ignoto". La
    vista non lo distingue più, quindi il test blocca solo che non inventi un
    numero e che il job contato davvero porti il suo costo.
    """
    t = make_transcript(project, "Lezione 1")
    _job(project, "llm_fix", cost=0.0, tokens=0, transcript_id=t)
    _job(project, "llm_fix", cost=0.30, tokens=5000, transcript_id=t)

    cells = _cells(lambda c: c["project_id"] == project)
    assert sum(c["n_jobs"] for c in cells) == 2
    assert _course_total(project) == 0.30


def test_the_cost_of_a_failed_job_still_counts(project, make_transcript):
    """Un agente morto a metà ha già speso i token dei chunk che aveva finito."""
    t = make_transcript(project, "Lezione 1")
    _job(project, "llm_fix", cost=0.40, tokens=4000, status="failed", transcript_id=t)

    assert _course_total(project) == 0.40


# ── L'agente di correzione ───────────────────────────────────────────────────
def _fake_agent_llm(monkeypatch, *, prompt=1200, completion=80, hit=0, miss=1200):
    class _Usage:
        def model_dump(self):
            return {"prompt_tokens": prompt, "completion_tokens": completion,
                    "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}

    class _Resp:
        usage = _Usage()
        choices = [type("C", (), {
            "message": type("M", (), {"content": '{"replacements": [{"find": "pie torch", '
                                                 '"replace": "PyTorch"}]}'})(),
            "finish_reason": "stop"})()]

    client = type("FakeClient", (), {
        "chat": type("Chat", (), {
            "completions": type("Comp", (), {"create": lambda self, **kw: _Resp()})(),
        })(),
    })()
    monkeypatch.setattr(llm, "_client", lambda provider: client)


def test_the_correction_agent_records_its_own_cost(monkeypatch, project, make_transcript):
    """
    Regressione: `run_agent` riceveva la diagnostica di ogni chunk e la
    scartava, quindi le correzioni non comparivano in nessun conto.
    """
    _fake_agent_llm(monkeypatch)
    provider_id = db.get_provider_by_name("deepseek")["id"]
    transcript = make_transcript(project, "Lezione 1", segments=[
        {"start": 0.0, "end": 5.0, "text": "Today we talk about pie torch"},
    ])
    job_id = db.create_job("llm_fix", {"transcript_id": transcript, "provider_id": provider_id},
                           project_id=project, transcript_id=transcript)

    result = jobs.run_agent(job_id, {"transcript_id": transcript, "provider_id": provider_id})

    assert result["usage"]["prompt"] == 1200
    assert result["usage"]["completion"] == 80
    stored = llm.usage_counts(result["usage"])
    assert stored["tokens_in"] == 1200
    assert stored["cost_usd"] > 0
