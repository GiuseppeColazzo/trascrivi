"""
trascrivi/jobs.py
=================
Coda dei job in un thread, con progresso, cancel, resume e recovery.

Perché un solo worker e un thread: l'inferenza satura la GPU, quindi eseguire
due trascrizioni in parallelo peggiora soltanto. Il thread permette a FastAPI di
restare reattivo (ctranslate2 non tiene il GIL), e il problema classico del
"figlio ucciso a metà" qui non esiste: il lavoro parziale è su disco e
`should_stop` fa uscire il loop in modo pulito fra un segmento e il successivo.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

from . import config, db
from . import corrections as corr
from . import llm, models
from .textutil import fmt_ts, parse_raw, segments_to_text, text_to_segments

log = logging.getLogger("trascrivi.jobs")

# Aggiorniamo il DB al massimo una volta al secondo: scrivere a ogni segmento
# (uno ogni 2-5 s di audio) è lavoro inutile su una barra di progresso.
PROGRESS_MIN_INTERVAL = 1.0

_stop_flags: dict[int, threading.Event] = {}
_running: dict[int, str] = {}
_lock = threading.Lock()
_wake = threading.Event()
_thread: threading.Thread | None = None
_shutdown = threading.Event()


# ── Registro degli stop ──────────────────────────────────────────────────────
def request_cancel(job_id: int) -> bool:
    with _lock:
        flag = _stop_flags.get(job_id)
    if flag is None:
        return False
    flag.set()
    log.info("richiesta di stop per il job %s", job_id)
    return True


def is_running(job_id: int) -> bool:
    with _lock:
        return job_id in _running


def cancel_flags() -> list[int]:
    with _lock:
        return list(_stop_flags)


# ── Accelerazione del path di stop ───────────────────────────────────────────
_RESUME_MARGIN = 2.0


def resume_start(job: dict) -> float:
    """
    Secondo da cui riprendere, leggendo l'ultimo timestamp dell'output parziale.

    Riusa `last_transcribed_seconds` dello script: entrambi i formati
    `[mm:ss]` e `[hh:mm:ss]` sono gestiti. Senza timestamp si riparte da zero
    (meglio rifare che lasciare un buco nel mezzo).
    """
    if not job.get("id"):
        return 0.0
    from trascrivi_audio import last_transcribed_seconds
    # Il parziale sta sempre in `partial_path(job_id)`. Prima si leggeva
    # `job["output_path"]`, che nessuno scriveva mai: ogni "Resume" ripartiva
    # da 0:00 e sovrascriveva il parziale.
    path = config.partial_path(int(job["id"]))
    if not path.exists():
        return 0.0
    offset = last_transcribed_seconds(path)
    if offset <= 0:
        return 0.0
    return max(0.0, offset - _RESUME_MARGIN)


# ── Worker ───────────────────────────────────────────────────────────────────
def _set_progress(job_id: int, **fields) -> None:
    db.update_job(job_id, **fields)


def run_transcribe(job_id: int, payload: dict) -> dict:
    """Trascrive un file e materializza la trascrizione."""
    from trascrivi_audio import probe_media, trascrivi_file

    source = db.get_source(int(payload["source_id"])) if payload.get("source_id") else None
    if source is None:
        raise ValueError("Source not found (was it deleted?)")

    audio_path = Path(source["stored_path"] or source["original_path"])
    if not audio_path.exists():
        db.set_source_status(source["id"], "missing")
        raise FileNotFoundError(f"Audio file is gone: {audio_path}")

    has_audio, probed_duration = probe_media(audio_path)
    if not has_audio:
        raise ValueError(f"No audio track found in {audio_path.name}")
    if probed_duration:
        db.set_source_duration(source["id"], float(probed_duration))

    language, notes = models.resolve_options(
        payload.get("model") or "distil-large-v3",
        payload.get("language") or "en",
        payload.get("device") or "auto",
        payload.get("compute_type") or "auto",
    )
    for note in notes:
        log.info("job %s: %s", job_id, note)

    model, cfg = models.get_model(
        payload.get("model") or "distil-large-v3",
        payload.get("device") or "auto",
        payload.get("compute_type") or "auto",
    )

    initial_prompt = payload.get("initial_prompt") or ""
    if not initial_prompt and config.load_settings().auto_vocabulary:
        from . import matching
        try:
            initial_prompt = matching.course_vocabulary(int(payload["project_id"]))
        except Exception:  # noqa: BLE001 - il vocabolario aiuta, non deve fermare il job
            log.warning("vocabolario del corso non disponibile", exc_info=True)
        if initial_prompt:
            log.info("job %s: vocabolario del corso per Whisper: %s", job_id, initial_prompt[:200])

    out_path = config.partial_path(job_id)
    start_at = resume_start(db.get_job(job_id) or {})
    stop_flag = threading.Event()
    with _lock:
        _stop_flags[job_id] = stop_flag

    state = {"last_db": 0.0, "segments": 0, "speed": None}
    t0 = time.time()
    logo = logging.getLogger("trascrivi.jobs.transcribe")

    def on_progress(end_seconds: float, count: int) -> None:
        state["segments"] = count
        now = time.time()
        if now - state["last_db"] < PROGRESS_MIN_INTERVAL:
            return
        state["last_db"] = now
        audio_seconds = end_seconds - start_at
        elapsed = max(0.001, now - t0)
        speed = audio_seconds / elapsed if audio_seconds > 0 else None
        total = probed_duration or 0.0
        progress = min(1.0, (end_seconds / total)) if total else 0.0
        eta = None
        if speed and total and end_seconds < total:
            eta = (total - end_seconds) / speed
        _set_progress(
            job_id,
            audio_seconds=round(end_seconds, 1),
            total_seconds=round(total, 1) if total else None,
            progress=round(progress, 4),
            speed=round(speed, 2) if speed else None,
            eta_seconds=round(eta, 1) if eta is not None else None,
            message=f"{count} segments . {fmt_ts(end_seconds)} of {fmt_ts(total)}",
        )

    def should_stop() -> bool:
        return stop_flag.is_set()

    _text, duration, detected_lang, elapsed, segments = trascrivi_file(
        audio_path,
        model,
        language,
        logo,
        batch_size=cfg.batch_size,
        beam_size=int(payload.get("beam_size") or 5),
        vad_filter=not payload.get("no_vad", False),
        word_timestamps=bool(payload.get("word_timestamps", False)),
        write_timestamps=True,
        # Il file parziale con i timestamp è la memoria del job: permette la
        # ripresa sia dal DB (segmenti) sia dal disco (mtime/crash).
        write_segments=True,
        output_path=out_path,
        clip_seconds=payload.get("clip_seconds"),
        resume_from=start_at,
        flush_every=float(payload.get("flush_every") or config.load_settings().flush_every),
        initial_prompt=initial_prompt or None,
        audio_duration=float(probed_duration) if probed_duration else None,
        progress_cb=on_progress,
        should_stop=should_stop,
        return_segments=True,
    )

    if _shutdown.is_set():
        # Il server si sta chiudendo: non è un annullamento dell'utente. Niente
        # trascrizione troncata; il parziale resta e il job diventa `interrupted`,
        # così Retry riprende da dove era arrivato.
        return {"interrupted": True, "transcript_id": None}
    cancelled = stop_flag.is_set()
    if not segments and not cancelled:
        # Era il caso della lezione 65: 75 minuti di audio, 0 segmenti, job
        # "done", riassunto fallito e copia dell'audio cancellata. Un risultato
        # vuoto è un errore: l'audio resta, e Retry è possibile.
        raise ValueError("The transcription produced no text (silence, wrong language or "
                         "unreadable audio). The audio was kept: check the language and retry.")
    if cancelled and segments:
        # Completa quel che c'è: l'utente ha fermato il job, non perso il lavoro.
        log.info("job %s interrotto dall'utente: salvo %d segmenti", job_id, len(segments))
    if not segments and cancelled:
        return {"cancelled": True, "transcript_id": None}

    title = payload.get("title") or audio_path.stem
    project_id = int(payload["project_id"])
    source_ref = source["original_path"]

    # L'output parziale è la fonte autorevole: se un job precedente aveva già
    # scritto dei segmenti, il file contiene l'inizio della lezione.
    if start_at > 0 and out_path.exists():
        raw = out_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_raw(raw)
        if len(parsed) >= len(segments):
            segments = parsed

    text = segments_to_text(segments)
    terms = [t for t in db.list_terms(project_id) if t.get("auto")]
    applied_terms = 0
    if terms:
        text, applied_terms = corr.apply_terms(text, terms)
        segments = text_to_segments(text, segments)

    previous = (db.get_job(job_id) or {}).get("transcript_id")
    if previous and db.get_transcript(int(previous)):
        # Un job annullato aveva già salvato la parte trascritta: il Retry la
        # completa invece di creare una seconda lezione.
        transcript_id = int(previous)
        db.update_transcript(transcript_id, {"text": text, "segments": segments,
                                             "duration": float(duration or 0.0)})
        _cleanup_partial(out_path, keep=cancelled)
        return {"transcript_id": transcript_id, "cancelled": cancelled,
                "segments": len(segments), "duration": float(duration or 0.0),
                "language": detected_lang, "device": cfg.device, "speed": None,
                "glossary_applied": applied_terms}
    transcript_id = db.create_transcript(
        project_id=project_id,
        title=title,
        text=text,
        segments=segments,
        source_id=source["id"],
        description=payload.get("description") or "",
        language=detected_lang,
        model=payload.get("model") or "distil-large-v3",
        device=cfg.device,
        duration=float(duration or 0.0),
        source_ref=source_ref,
    )
    if applied_terms:
        db.add_revision(transcript_id, "glossary", applied_terms,
                        "Automatic term replacements on import")

    _cleanup_partial(out_path, keep=cancelled)
    return {
        "transcript_id": transcript_id,
        "cancelled": cancelled,
        "segments": len(segments),
        "duration": float(duration or 0.0),
        "language": detected_lang,
        "device": cfg.device,
        "speed": round((float(duration or 0.0) / elapsed), 2) if elapsed > 0 else None,
        "glossary_applied": applied_terms,
    }


def _cleanup_partial(path: Path, keep: bool = False) -> None:
    """
    Rimuove l'output parziale a job completato.

    Su un job annullato o fallito il file resta: è la memoria su cui si appoggia
    la ripresa, e senza di esso si perderebbe l'ultimo tratto già trascritto.
    """
    if keep:
        log.info("output parziale conservato per la ripresa: %s", path)
        return
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        log.warning("impossibile rimuovere l'output parziale %s: %s", path, exc)


def cleanup_source(source: dict) -> None:
    """
    Cancella la copia locale del file audio e conserva la riga `sources`.

    Il file originale dell'utente non viene mai toccato: cancelliamo solo le
    copie che ha caricato il browser (`stored_path`).
    """
    stored = source.get("stored_path")
    if stored:
        try:
            p = Path(stored)
            if p.exists():
                p.unlink()
        except OSError as exc:
            log.warning("impossibile cancellare %s: %s", stored, exc)
    db.execute("UPDATE transcripts SET source_ref = NULL WHERE source_id = ?", (source["id"],))
    db.execute("UPDATE sources SET stored_path = NULL, status = 'purged' WHERE id = ?",
               (source["id"],))


def _maybe_queue_agent(job: dict, payload: dict, result: dict) -> None:
    """
    Accoda i job LLM che l'utente ha chiesto al momento della trascrizione.

    L'accodamento avviene qui e non nell'endpoint perché prima serve la
    trascrizione: così l'ordine è garantito e il fallimento di un job LLM non
    tocca il lavoro di trascrizione, che è già salvato.

    L'unico follow-up è `run_summary`: il riassunto della lezione appena
    trascritta. (L'agente di correzione è stato tolto: la correzione dei nomi
    riconosciuti male ora la fa il riassunto, con slide e glossario come
    riferimento.)
    """
    transcript_id = result.get("transcript_id")
    if not transcript_id:
        return
    if payload.get("run_summary"):
        _queue_llm_job(job, "llm_summary",
                       {"transcript_id": transcript_id,
                        "provider_id": payload.get("summary_provider_id")
                        or payload.get("provider_id"),
                        "style": payload.get("summary_style") or "",
                        "instruction": payload.get("summary_instruction") or ""},
                       transcript_id, "summary")


def _queue_llm_job(job: dict, kind: str, payload: dict, transcript_id: int, label: str) -> None:
    """Crea il job LLM, risolvendo il provider e segnalando in `message` se manca."""
    if payload.get("provider_id") is None:
        providers = db.usable_providers()
        if not providers:
            current = (db.get_job(job["id"]) or {}).get("message") or ""
            db.update_job(job["id"], message=f"{current} . {label} skipped: no provider")
            log.warning("job %s: %s richiesto ma nessun provider configurato", job["id"], label)
            return
        payload["provider_id"] = providers[0]["id"]
    new_job = db.create_job(kind, payload, project_id=job.get("project_id"),
                            transcript_id=transcript_id)
    enqueue(new_job)
    log.info("job %s: accodato job %s %s per la trascrizione %s",
             job["id"], kind, new_job, transcript_id)


def run_summary(job_id: int, payload: dict) -> dict:
    """Genera e salva il riassunto di una trascrizione."""
    from . import summary as summary_mod

    transcript_id = int(payload["transcript_id"])
    transcript = db.get_transcript(transcript_id)
    if transcript is None:
        raise ValueError("Transcript not found")

    provider = db.get_provider(int(payload["provider_id"])) if payload.get("provider_id") else None
    if provider is None:
        raise ValueError("LLM provider not configured")

    settings = config.load_settings()
    style = payload.get("style") or settings.summary_style or "study"

    def on_progress(fraction: float, message: str) -> None:
        _set_progress(job_id, progress=round(min(0.99, fraction), 3), message=message)

    on_progress(0.05, "preparing the transcript")
    result = summary_mod.summarize(
        provider, transcript, settings,
        style=style, instructions=payload.get("instruction") or "",
        on_progress=on_progress,
    )

    summary_id = db.create_summary(
        transcript_id, job_id=job_id, style=result["style"], language=result["language"],
        provider=result["provider"], model=result["model"], title=result["title"],
        overview=result["overview"], markdown=result["markdown"],
        n_words=result["words"], target_words=result["target_words"],
        source_words=result["source_words"], tokens_in=result["tokens_in"],
        tokens_out=result["tokens_out"], cache_hit=result["cache_hit"],
        cost_usd=result["cost_usd"], elapsed_s=result["elapsed_s"],
        truncated=bool(result.get("truncated")),
    )
    db.add_revision(transcript_id, "summary", 0,
                    f"{result['style']} summary: {result['words']} words "
                    f"(target {result['target_words']}) from "
                    f"{result['provider']}/{result['model']}")

    # Il riassunto è la controparte del matching pagina -> lezione: appena ce n'è
    # uno nuovo, le assegnazioni di quel corso vanno ricalcolate (i vettori sono
    # locali, costo zero). Senza slide caricate non c'è niente da riassegnare.
    matching_job = _queue_matching(transcript.get("project_id"))

    message = (f"{result['words']} words of {result['target_words']} . "
               f"${result['cost_usd']:.4f}")
    if matching_job:
        message += " . slide matching queued"

    return {
        "summary_id": summary_id,
        "transcript_id": transcript_id,
        "words": result["words"],
        "target_words": result["target_words"],
        "cost_usd": result["cost_usd"],
        "message": message,
        # Senza, il job del riassunto restava a costo 0 (la vista costi lo
        # recuperava dalla tabella summaries, la lista job no).
        "usage": result.get("usage") or {},
    }


# ── Slide ────────────────────────────────────────────────────────────────────
def run_parse_slides(job_id: int, payload: dict) -> dict:
    """
    Legge un documento di slide: testo, render, boilerplate, pagine complesse.

    Tutto locale (pypdfium2/python-pptx/rapidocr). Il pass VLM opzionale gira
    nello stesso job perché è la seconda metà della stessa lettura: senza, il
    documento è comunque pronto e le pagine complesse restano col solo OCR.
    """
    from . import slideparser

    doc_id = int(payload["doc_id"])
    doc = db.get_slide_doc(doc_id)
    if doc is None:
        raise ValueError("Slide document not found (was it deleted?)")
    path = config.slide_source_path(doc["project_id"], doc_id, doc["ext"])
    if not path.exists():
        db.update_slide_doc(doc_id, {"status": "error", "error": "Source file is gone"})
        raise FileNotFoundError(f"Slide file is gone: {path}")

    stop_flag = threading.Event()
    with _lock:
        _stop_flags[job_id] = stop_flag

    def on_page(done: int, total: int) -> None:
        _set_progress(job_id, progress=round(done / max(1, total) * 0.9, 3),
                      message=f"page {done}/{total}")

    db.update_slide_doc(doc_id, {"status": "parsing", "error": None})
    try:
        pages, boilerplate = slideparser.parse(path, path.parent,
                                               should_stop=stop_flag.is_set,
                                               on_page=on_page)
    except Exception as exc:  # noqa: BLE001 - qualunque errore, non solo le dipendenze
        # Prima solo MissingDependency segnava l'errore: un PDF rotto lasciava
        # il documento "parsing" per sempre.
        db.update_slide_doc(doc_id, {"status": "error", "error": str(exc)[:500]})
        raise

    db.save_slide_pages(doc_id, pages)
    db.update_slide_doc(doc_id, {"n_pages": len(pages), "status": "ready",
                                 "boilerplate": db.jdump(boilerplate)})

    n_complex = sum(1 for p in pages if p["complex"])
    message = f"{len(pages)} pages . {n_complex} complex"
    if boilerplate:
        message += f" . {len(boilerplate)} boilerplate lines removed"

    vlm_pages = 0
    if not stop_flag.is_set():
        from . import vlm

        settings = config.load_settings()
        vlm_pages = vlm.enhance_doc(doc_id, settings, on_progress=lambda done, total:
                                    _set_progress(job_id, progress=round(0.9 + 0.1 * done / max(1, total), 3),
                                                  message=f"VLM page {done}/{total}"))
        if vlm_pages:
            message += f" . {vlm_pages} pages rendered by VLM"

    # Il matching è un altro giro (embeddings): meglio un job suo, così l'utente
    # vede due passi distinti e può rifare la sola assegnazione.
    if (doc.get("kind") or "slides") != "slides":
        # Un esame passato non va assegnato alle lezioni: serve alla guida.
        return {"doc_id": doc_id, "pages": len(pages), "complex": n_complex,
                "vlm_pages": vlm_pages, "boilerplate": boilerplate,
                "cancelled": stop_flag.is_set(), "message": message}
    embed_job = db.create_job("embed_match", {"project_id": doc["project_id"],
                                              "doc_id": doc_id, "auto": True},
                              project_id=doc["project_id"])
    enqueue(embed_job)

    return {"doc_id": doc_id, "pages": len(pages), "complex": n_complex,
            "vlm_pages": vlm_pages, "boilerplate": boilerplate,
            "cancelled": stop_flag.is_set(), "message": message}


def run_embed_match(job_id: int, payload: dict) -> dict:
    """Assegna le pagine di slide alle lezioni (embeddings locali, nessun costo)."""
    from . import matching

    project_id = int(payload["project_id"])
    if not db.get_project(project_id):
        raise ValueError("Project not found")

    def on_progress(done: int, total: int) -> None:
        _set_progress(job_id, progress=round(done / max(1, total), 3),
                      message=f"document {done}/{total}")

    try:
        result = matching.match_project(project_id, on_progress=on_progress)
    except (RuntimeError, ValueError) as exc:
        # Accodato in automatico (dopo trascrizione/riassunto/parsing) con Ollama
        # spento: non è un errore dell'utente. Prima riempiva la coda di job
        # falliti (25 su 56 nel DB reale). Dal bottone "Associa" invece fallisce.
        if payload.get("auto") and ("Embeddings" in str(exc) or "Ollama" in str(exc)):
            return {"links": 0, "message": "skipped: Ollama is not running"}
        raise
    return {**result, "message": f"{result['links']} page ranges . {result['model']}"}


def run_cards(job_id: int, payload: dict) -> dict:
    """Genera e salva le flashcard di una lezione (unica chiamata LLM pagata)."""
    from . import cards as cards_mod

    transcript_id = int(payload["transcript_id"])
    transcript = db.get_transcript(transcript_id)
    if transcript is None:
        raise ValueError("Transcript not found")
    provider = db.get_provider(int(payload["provider_id"])) if payload.get("provider_id") else None
    if provider is None:
        raise ValueError("LLM provider not configured")

    settings = config.load_settings()

    def on_progress(fraction: float, message: str) -> None:
        _set_progress(job_id, progress=round(min(0.95, fraction), 3), message=message)

    result = cards_mod.generate(provider, transcript, settings,
                                force=bool(payload.get("force")),
                                vision=payload.get("vision"),
                                on_progress=on_progress)
    if result.get("n_new"):
        db.add_revision(transcript_id, "cards", result["n_new"],
                        f"{result['n_new']} cards from {provider.get('name')}/"
                        f"{provider.get('model')}")
    return result


def run_guide(job_id: int, payload: dict) -> dict:
    """Guida d'esame del corso: tutti i riassunti (+ esami passati) in una chiamata."""
    from . import guide

    provider = db.get_provider(int(payload["provider_id"])) if payload.get("provider_id") else None
    if provider is None:
        raise ValueError("LLM provider not configured")

    def on_progress(fraction: float, message: str) -> None:
        _set_progress(job_id, progress=round(min(0.95, fraction), 3), message=message)

    result = guide.generate(provider, int(payload["project_id"]), config.load_settings(),
                            instructions=payload.get("instruction") or "",
                            on_progress=on_progress)
    db.execute("UPDATE guides SET job_id = ? WHERE id = ?", (job_id, result["guide_id"]))
    return result


def _queue_matching(project_id: int | None) -> int | None:
    """
    Accoda un `embed_match` per il corso, se serve e se non ce n'è già uno.

    Tre chiamanti (fine di un parsing, fine di una trascrizione, riassunto nuovo)
    possono trovarsi nello stesso momento — una lezione trascritta con riassunto
    ne genera due in un colpo. Il matching è idempotente, ma accodarne due rifà
    gli stessi prodotti scalari: la guardia sul job già in coda evita il lavoro
    doppio senza perdere l'aggiornamento (chi lo accoda dopo legge il materiale
    più nuovo comunque, perché gira dopo).
    """
    if not project_id:
        return None
    project_id = int(project_id)
    if not any((d.get("kind") or "slides") == "slides" for d in db.list_slide_docs(project_id)):
        return None
    if db.row("SELECT id FROM jobs WHERE kind = 'embed_match' AND project_id = ? "
              "AND status IN ('queued', 'running')", (project_id,)):
        return None
    job_id = db.create_job("embed_match", {"project_id": project_id, "auto": True},
                           project_id=project_id)
    enqueue(job_id)
    log.info("accodato embed_match %s per il corso %s", job_id, project_id)
    return job_id


def _finish_transcribe_cleanup(job: dict, payload: dict) -> None:
    """Applica la politica sull'audio: di default la copia locale viene rimossa."""
    settings = config.load_settings()
    delete = payload.get("delete_source")
    if delete is None:
        delete = settings.delete_sources_after_job
    if not delete:
        return
    source = db.get_source(int(payload["source_id"])) if payload.get("source_id") else None
    if source and source.get("stored_path"):
        cleanup_source(source)


# ── Dispatch e ciclo del worker ──────────────────────────────────────────────
def _run_job(job: dict) -> None:
    job_id = int(job["id"])
    payload = db.jload(job["payload"], {}) or {}
    kind = job["kind"]

    db.update_job(job_id, status="running", started_at=db.now(), message="starting",
                  error=None, progress=0.0)
    with _lock:
        _running[job_id] = kind
    t0 = time.time()
    try:
        if kind == "transcribe":
            result = run_transcribe(job_id, payload)
        elif kind == "llm_summary":
            result = run_summary(job_id, payload)
        elif kind == "parse_slides":
            result = run_parse_slides(job_id, payload)
        elif kind == "embed_match":
            result = run_embed_match(job_id, payload)
        elif kind == "llm_cards":
            result = run_cards(job_id, payload)
        elif kind == "llm_guide":
            result = run_guide(job_id, payload)
        else:
            raise ValueError(f"Unknown job kind: {kind}")

        cancelled = bool(result.get("cancelled"))
        if result.get("interrupted"):
            db.update_job(job_id, status="interrupted", finished_at=db.now(),
                          message="server stopped: retry to resume", eta_seconds=None)
            return
        # `progress` è NOT NULL: su un job annullato si conserva l'ultimo valore
        # raggiunto invece di azzerarlo (una barra che torna a 0 sembra un errore).
        current_progress = float((db.get_job(job_id) or {}).get("progress") or 0.0)
        db.update_job(
            job_id,
            status="cancelled" if cancelled else "done",
            finished_at=db.now(),
            progress=current_progress if cancelled else 1.0,
            transcript_id=result.get("transcript_id") or job.get("transcript_id"),
            message=result.get("message") or _message_for(kind, result),
            speed=result.get("speed"),
            eta_seconds=None,
            # Il costo delle chiamate LLM vive sul job che le ha fatte: è l'unico
            # punto in cui si sa quanto è costata *quella* esecuzione, anche se il
            # job muore prima di salvare il riassunto. `usage_counts` restituisce
            # chiavi già pronte (tokens_in/tokens_out/cache_hit/cost_usd).
            **llm.usage_counts(result.get("usage") or {}),
        )
        if kind == "transcribe" and not cancelled:
            _finish_transcribe_cleanup(job, payload)
            _maybe_queue_agent(job, payload, result)
            # Slides caricate prima della lezione: il matching va rifatto ora che
            # c'è la controparte. Senza, le pagine resterebbero senza proposta
            # finché non si genera un riassunto o non si preme "Associa".
            _queue_matching(job.get("project_id"))
        log.info("job %s (%s) %s in %.1fs: %s", job_id, kind,
                 "cancelled" if cancelled else "done", time.time() - t0, result)
    except Exception as exc:  # noqa: BLE001 - un job rotto non deve uccidere il worker
        log.exception("job %s (%s) fallito", job_id, kind)
        db.update_job(job_id, status="failed", finished_at=db.now(),
                      error=f"{type(exc).__name__}: {exc}"[:500],
                      message="failed",
                      # Una chiamata pagata con risultato inutilizzabile costa lo stesso.
                      **(llm.usage_counts(exc.usage) if getattr(exc, "usage", None) else {}))
    finally:
        with _lock:
            _running.pop(job_id, None)
            _stop_flags.pop(job_id, None)


def _message_for(kind: str, result: dict) -> str:
    if kind == "transcribe":
        return (f"{result.get('segments', 0)} segments . "
                f"{result.get('language', '?').upper()} . {result.get('device', '?')}")
    if kind == "llm_summary":
        return f"{result.get('words', 0)} of {result.get('target_words', 0)} words"
    if kind == "parse_slides":
        return f"{result.get('pages', 0)} pages . {result.get('complex', 0)} complex"
    if kind == "embed_match":
        return f"{result.get('links', 0)} page ranges"
    if kind == "llm_cards":
        return f"{result.get('n_new', 0)} cards"
    return "done"


def _worker_loop() -> None:
    log.info("worker dei job avviato")
    while not _shutdown.is_set():
        job = db.next_queued_job()
        if job is None:
            _wake.clear()
            _wake.wait(timeout=2.0)
            continue
        if db.claim_job(int(job["id"])):
            _run_job(job)
    log.info("worker dei job fermato")


def start_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _shutdown.clear()
    _thread = threading.Thread(target=_worker_loop, name="trascrivi-worker", daemon=True)
    _thread.start()


def stop_worker(timeout: float = 20.0) -> None:
    """
    Ferma il thread del worker.

    Non interrompe un segmento in corso (farlo lascerebbe l'audio senza flush):
    chiede lo stop, che il loop rispetta fra un segmento e il successivo, e
    attende il tempo massimo indicato.
    """
    _shutdown.set()
    _wake.set()
    for job_id in cancel_flags():
        request_cancel(job_id)
    if _thread and _thread.is_alive():
        _thread.join(timeout=timeout)
        if _thread.is_alive():
            log.warning("il worker non si è fermato entro %.0fs", timeout)


def enqueue(job_id: int) -> None:
    """Segnala al worker che c'è un job pronto, senza attendere il polling."""
    _wake.set()


def recover_stale_jobs() -> int:
    """All'avvio: i job 'running' rimasti da un processo morto diventano interrotti."""
    n = db.mark_interrupted_jobs()
    # Un documento rimasto "parsing" è morto con il processo: lo si dice.
    db.execute("UPDATE slide_docs SET status = 'error', error = 'Server stopped while parsing: "
               "use Reparse' WHERE status = 'parsing'")
    if n:
        log.warning("%d job interrotti recuperati all'avvio", n)
    return n


def load_and_resume_transcript(job: dict) -> dict | None:
    """
    Rilegge l'artefatto JSON di una trascrizione, se la riga nel DB è andata persa.

    Non è un percorso normale: serve solo se il DB viene ripristinato da un
    backup vecchio mentre i file su disco sono più recenti.
    """
    path = config.transcript_path(int(job["transcript_id"])) if job.get("transcript_id") else None
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def reset_job_for_retry(job_id: int) -> bool:
    """Rimette un job fallito/interrotto in coda, azzerando i campi di stato."""
    job = db.get_job(job_id)
    if job is None or job["status"] in ("queued", "running"):
        return False
    db.update_job(job_id, status="queued", progress=0.0, audio_seconds=0.0,
                  total_seconds=None, speed=None, eta_seconds=None,
                  message="requeued", error=None, started_at=None, finished_at=None)
    enqueue(job_id)
    return True


def queue_eta() -> float | None:
    """
    Secondi stimati per svuotare la coda: l'audio che resta da trascrivere
    diviso la velocità media delle ultime trascrizioni, più ~40 s per ogni
    passo LLM (riassunto, guida…) in coda. Una stima, non una promessa.
    """
    queued = db.rows("SELECT kind, payload FROM jobs WHERE status = 'queued'")
    running = db.rows("SELECT kind, eta_seconds FROM jobs WHERE status = 'running'")
    if not queued and not running:
        return None
    speeds = [r["speed"] for r in db.rows(
        "SELECT speed FROM jobs WHERE kind = 'transcribe' AND status = 'done' AND speed > 0 "
        "ORDER BY id DESC LIMIT 10")]
    speed = sum(speeds) / len(speeds) if speeds else 10.0
    total = sum(float(r["eta_seconds"] or 40) for r in running)
    for job in queued:
        if job["kind"] == "transcribe":
            payload = db.jload(job["payload"], {}) or {}
            duration = db.scalar("SELECT duration FROM sources WHERE id = ?",
                                 (payload.get("source_id"),), default=0) or 0
            total += float(duration) / speed + 20   # + caricamento del modello
        elif job["kind"] in ("llm_summary", "llm_guide", "llm_cards"):
            total += 40
        else:
            total += 15
    return round(total)


def queue_size() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'queued'", default=0) or 0)


def purge_all_sources() -> int:
    """Cancella tutte le copie audio locali (i riferimenti a file esterni restano)."""
    n = 0
    for src in db.rows("SELECT * FROM sources WHERE stored_path IS NOT NULL"):
        cleanup_source(src)
        n += 1
    if config.SOURCES.exists():
        for child in config.SOURCES.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    return n
