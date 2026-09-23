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
    out = job.get("output_path")
    if not out:
        return 0.0
    from trascrivi_audio import last_transcribed_seconds
    path = Path(out)
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
        initial_prompt=payload.get("initial_prompt") or None,
        audio_duration=float(probed_duration) if probed_duration else None,
        progress_cb=on_progress,
        should_stop=should_stop,
        return_segments=True,
    )

    cancelled = stop_flag.is_set()
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


def run_agent(job_id: int, payload: dict) -> dict:
    """Interroga il provider LLM e salva le proposte di correzione."""
    transcript_id = int(payload["transcript_id"])
    transcript = db.get_transcript(transcript_id)
    if transcript is None:
        raise ValueError("Transcript not found")

    provider = db.get_provider(int(payload["provider_id"])) if payload.get("provider_id") else None
    if provider is None:
        raise ValueError("LLM provider not configured")
    settings = config.load_settings()
    budget = int(payload.get("chunk_chars") or settings.llm_chunk_chars or llm.chunk_budget())

    segments = transcript["segments"] or text_to_segments(transcript["text"])
    if not segments:
        raise ValueError("This transcript is empty")

    # Ripartire pulito: le proposte ancora in attesa di una esecuzione
    # precedente non hanno più senso.
    removed = db.clear_pending_proposals(transcript_id)
    if removed:
        log.info("job %s: rimosse %d proposte pending precedenti", job_id, removed)

    chunks = llm.chunk_segments(segments, budget=budget)
    stop_flag = threading.Event()
    with _lock:
        _stop_flags[job_id] = stop_flag

    raw_proposals: list[dict] = []
    glossary: list[dict] = []
    failures: list[str] = []
    t0 = time.time()

    for i, chunk in enumerate(chunks, start=1):
        if stop_flag.is_set():
            break
        _mapped, spans = corr.chunk_text_with_map(chunk)
        try:
            # `ask_chunk` dimezza lo spezzone se la risposta non ci sta dentro:
            # un chunk grande fa troncare il JSON delle proposte a metà.
            proposals, chunk_glossary, _diag, origin = llm.ask_chunk(
                provider, chunk, extra_instruction=payload.get("instruction") or ""
            )
            for prop in proposals:
                # La proposta va cercata nel sub-chunk che l'ha generata: dopo una
                # divisione gli indici del chunk padre non valgono più.
                sub = origin.get(id(prop)) or chunk
                sub_text, sub_spans = corr.chunk_text_with_map(sub)
                local = corr.locate_in_spans(sub_text, sub_spans,
                                             corr.normalize_for_match(prop["find"]))
                if local is None and sub is not chunk:
                    local = corr.locate_in_spans(
                        _mapped, spans, corr.normalize_for_match(prop["find"]))
                prop["segment_index"] = local[0] if local else None
            raw_proposals.extend(proposals)
            if chunk_glossary:
                glossary.extend(chunk_glossary)
        except llm.ChunkTooLarge as exc:
            log.warning("job %s: chunk %d/%d non elaborabile: %s", job_id, i, len(chunks), exc)
            failures.append(str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - un chunk rotto non uccide il job
            log.warning("job %s: chunk %d/%d fallito: %s", job_id, i, len(chunks), exc)
            failures.append(str(exc)[:200])

        elapsed = max(0.001, time.time() - t0)
        _set_progress(
            job_id,
            progress=round(i / len(chunks), 4),
            message=f"chunk {i}/{len(chunks)} . {len(raw_proposals)} proposals",
            speed=round(i / elapsed, 3),
            eta_seconds=round((len(chunks) - i) * (elapsed / i), 1),
        )

    if stop_flag.is_set() and not raw_proposals:
        return {"cancelled": True, "proposals": 0}

    items = corr.dedupe_proposals(raw_proposals)
    # Il glossario dell'agente diventa un suggerimento per il progetto, senza
    # sovrascrivere termini esistenti.
    for entry in glossary[:10]:
        if entry.get("meaning"):
            db.upsert_term(transcript["project_id"], entry["term"], entry["meaning"],
                           auto=False, note=entry["term"])
    if items:
        db.create_proposals(
            transcript_id,
            job_id,
            [{**it, "occurrences": _count_occurrences(segments, it["find"]),
              "flag": _validate_flag(segments, it), "context": _brief_context(it)}
             for it in items],
        )

    message = f"{len(items)} proposals from {len(chunks)} chunks"
    if failures:
        message += f" . {len(failures)} chunk(s) failed"
    return {
        "proposals": len(items),
        "chunks": len(chunks),
        "failures": failures,
        "cancelled": stop_flag.is_set(),
        "message": message,
    }


def _brief_context(item: dict) -> str:
    return f"{item.get('find', '')[:60]} -> {item.get('replace', '')[:60]}"


def _count_occurrences(segments: list[dict], find: str) -> int:
    """Quante volte il `find` compare nella trascrizione (per la UI)."""
    norm = corr.normalize_for_match(find or "")
    if not norm:
        return 0
    return sum(corr.normalize_for_match(seg.get("text", "")).count(norm) for seg in segments)


def _validate_flag(segments: list[dict], item: dict) -> str:
    """Quante volte il `find` compare: 0 = inutilizzabile, >1 = ambiguo."""
    if (item.get("find") or "") == (item.get("replace") or "").strip():
        return "noop"
    hits = _count_occurrences(segments, item.get("find") or "")
    if not corr.normalize_for_match(item.get("find") or ""):
        return "unmatched"
    if hits == 0:
        return "unmatched"
    if hits > 1:
        return "ambiguous"
    return "ok"


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
    Se l'utente ha spuntato "run correction agent", accoda il job agente.

    L'accodamento avviene qui e non nell'endpoint perché prima serve la
    trascrizione: così l'ordine è garantito e il fallimento dell'agente non
    tocca il lavoro di trascrizione, che è già salvato.
    """
    if not payload.get("run_agent"):
        return
    transcript_id = result.get("transcript_id")
    if not transcript_id:
        return
    provider_id = payload.get("provider_id")
    if provider_id is None:
        providers = [p for p in db.list_providers() if p["enabled"] and p["has_key"]]
        if not providers:
            db.update_job(job["id"], message=(db.get_job(job["id"]) or {}).get("message", "")
                          + " . agent skipped: no provider")
            log.warning("job %s: agente richiesto ma nessun provider configurato", job["id"])
            return
        provider_id = providers[0]["id"]
    agent_job = db.create_job(
        "llm_fix",
        {"transcript_id": transcript_id, "provider_id": provider_id,
         "instruction": payload.get("instruction") or ""},
        project_id=job.get("project_id"), transcript_id=transcript_id,
    )
    enqueue(agent_job)
    log.info("job %s: accodato job agente %s per la trascrizione %s",
             job["id"], agent_job, transcript_id)


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
        elif kind == "llm_fix":
            result = run_agent(job_id, payload)
        else:
            raise ValueError(f"Unknown job kind: {kind}")

        cancelled = bool(result.get("cancelled"))
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
        )
        if kind == "transcribe" and not cancelled:
            _finish_transcribe_cleanup(job, payload)
            _maybe_queue_agent(job, payload, result)
        log.info("job %s (%s) %s in %.1fs: %s", job_id, kind,
                 "cancelled" if cancelled else "done", time.time() - t0, result)
    except Exception as exc:  # noqa: BLE001 - un job rotto non deve uccidere il worker
        log.exception("job %s (%s) fallito", job_id, kind)
        db.update_job(job_id, status="failed", finished_at=db.now(),
                      error=f"{type(exc).__name__}: {exc}"[:500],
                      message="failed")
    finally:
        with _lock:
            _running.pop(job_id, None)
            _stop_flags.pop(job_id, None)


def _message_for(kind: str, result: dict) -> str:
    if kind == "transcribe":
        return (f"{result.get('segments', 0)} segments . "
                f"{result.get('language', '?').upper()} . {result.get('device', '?')}")
    return f"{result.get('proposals', 0)} proposals"


def _worker_loop() -> None:
    log.info("worker dei job avviato")
    while not _shutdown.is_set():
        job = db.next_queued_job()
        if job is None:
            _wake.clear()
            _wake.wait(timeout=2.0)
            continue
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
