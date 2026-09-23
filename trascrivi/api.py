"""
trascrivi/api.py
================
API HTTP (FastAPI) della piattaforma.

Convenzioni:
* gli endpoint sono `def` sincrone quando toccano il DB: FastAPI le esegue in
  threadpool, quindi la event loop resta libera per i poll del frontend;
* tutti i path dei file audio passano da `_check_media_path`, che rifiuta
  cartelle di sistema e percorsi non assoluti;
* la chiave API di un provider non compare **mai** in una risposta.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from trascrivi_audio import MODELS

from . import config, db, jobs, llm, models
from . import corrections as corr
from .textutil import build_markdown, parse_raw, segments_to_text, text_to_segments

log = logging.getLogger("trascrivi.api")

router = APIRouter(prefix="/api")

# Cartelle di sistema che non hanno senso come sorgente audio e la cui
# cancellazione sarebbe catastrofica: le rifiutiamo prima di qualunque altra
# cosa. Volutamente NON c'è "C:\": tutti i file dell'utente stanno lì dentro.
_FORBIDDEN_ROOTS = {
    Path("C:/Windows"), Path("C:/Program Files"), Path("C:/Program Files (x86)"),
    Path("C:/ProgramData"), Path("C:/$Recycle.Bin"), Path("C:/System Volume Information"),
}


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = ""
    color: str = "#4f46e5"
    description: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    code: str | None = None
    color: str | None = None
    description: str | None = None
    archived: bool | None = None


class SourcePathIn(BaseModel):
    path: str


class TranscribeIn(BaseModel):
    project_id: int
    source_id: int
    title: str = ""
    description: str = ""
    model: str = "distil-large-v3"
    language: str = "en"
    device: str = "auto"
    compute_type: str = "auto"
    beam_size: int = 5
    word_timestamps: bool = False
    no_vad: bool = False
    initial_prompt: str = ""
    clip_seconds: float | None = None
    flush_every: float | None = None
    delete_source: bool | None = None
    run_agent: bool = False
    provider_id: int | None = None
    instruction: str = ""


class FixIn(BaseModel):
    provider_id: int | None = None
    instruction: str = ""
    chunk_chars: int | None = None


class TranscriptPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    project_id: int | None = None
    text: str | None = None
    segments: list[dict] | None = None


class ProposalsIn(BaseModel):
    ids: list[int]
    targets: dict[int, int] | None = None


class TermIn(BaseModel):
    project_id: int
    find: str = Field(min_length=1)
    replace: str = ""
    auto: bool = False
    note: str = ""


class ProviderIn(BaseModel):
    base_url: str | None = None
    model: str | None = None
    enabled: bool | None = None
    api_key: str | None = None


# ── Validazione dei percorsi ─────────────────────────────────────────────────
def _check_media_path(raw: str, *, must_exist: bool = True) -> Path:
    """
    Valida un percorso indicato dall'utente.

    Rifiuta percorsi relativi, directory di sistema e file non audio/video. Non
    è una sandbox (l'app è locale e a singolo utente) ma impedisce gli errori
    irreversibili: nessun endpoint cancella o modifica `original_path`.
    """
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise HTTPException(400, "Empty path")
    p = Path(text).expanduser()
    if not p.is_absolute():
        raise HTTPException(400, "Use an absolute path, e.g. C:\\Users\\you\\lesson.mp4")
    # Una radice di volume ("C:\\") o una cartella di sistema non sono sorgenti
    # audio: rifiutarle evita errori grossolani senza limitare i file dell'utente
    # (che stanno *dentro* C:\, non sono C:\ stesso).
    root = Path(p.anchor)
    if p == root:
        raise HTTPException(400, "That location is not allowed")
    lowered = {str(f).lower() for f in _FORBIDDEN_ROOTS}
    if any(str(parent).lower() in lowered for parent in (p, *p.parents)):
        raise HTTPException(400, "That location is not allowed")
    if must_exist:
        if not p.exists():
            raise HTTPException(404, f"File not found: {p}")
    if p.is_dir():
        raise HTTPException(400, f"That is a folder, not a file: {p}")
    if must_exist and not p.is_file():
        raise HTTPException(400, f"Not a file: {p}")
    from trascrivi_audio import SUPPORTED_FORMATS
    if p.suffix.lower() not in SUPPORTED_FORMATS:
        raise HTTPException(
            400, f"Unsupported format '{p.suffix}'. Allowed: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    return p


def _hash_head(path: Path, size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        h.update(fh.read(size))
    return h.hexdigest()


def _dir_has_space(target: Path, needed_bytes: int) -> bool:
    free = config.free_space_bytes(target)
    return free == 0 or free > needed_bytes * 1.1


# ── Health e metadati ────────────────────────────────────────────────────────
@router.get("/health")
def health() -> dict:
    hw = models.hardware_info()
    providers = db.list_providers()
    return {
        "ok": True,
        "version": _version(),
        "hardware": hw,
        "providers": providers,
        "queue": jobs.queue_size(),
        "has_llm_provider": any(p["enabled"] and (p["has_key"] or p["name"] == "ollama")
                               for p in providers),
        "defaults": {
            "model": config.load_settings().default_model,
            "language": config.load_settings().default_language,
        },
    }


def _version() -> str:
    from . import __version__
    return __version__


@router.get("/models")
def list_models_endpoint() -> dict:
    return {"models": MODELS, "cached": models.loaded_models()}


@router.post("/models/unload")
def unload_models() -> dict:
    return {"unloaded": models.unload_all()}


@router.get("/formats")
def formats() -> dict:
    return {"audio": models.supported_formats()}


@router.get("/search")
def search(q: str = Query(min_length=1), project_id: int | None = None,
           limit: int = 50) -> dict:
    return {"results": db.search_transcripts(q, project_id, limit)}


# ── Progetti ─────────────────────────────────────────────────────────────────
@router.get("/projects")
def get_projects() -> dict:
    return {"projects": db.list_projects()}


@router.post("/projects", status_code=201)
def post_project(body: ProjectIn) -> dict:
    pid = db.create_project(body.name, body.code, body.color, body.description)
    return db.get_project(pid) or {}


@router.get("/projects/{project_id}")
def get_project(project_id: int) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.patch("/projects/{project_id}")
def patch_project(project_id: int, body: ProjectPatch) -> dict:
    if not db.get_project(project_id):
        raise HTTPException(404, "Project not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    db.update_project(project_id, fields)
    return db.get_project(project_id) or {}


@router.delete("/projects/{project_id}")
def delete_project(project_id: int) -> dict:
    if not db.get_project(project_id):
        raise HTTPException(404, "Project not found")
    payload = db.delete_project(project_id)
    for t_id in payload["transcripts"]:
        _unlink(config.transcript_path(int(t_id)))
    for stored in payload["sources"]:
        _unlink(Path(stored))
    src_dir = config.SOURCES / str(project_id)
    if src_dir.exists():
        shutil.rmtree(src_dir, ignore_errors=True)
    return {"deleted_sources": len(payload["sources"]),
            "deleted_transcripts": len(payload["transcripts"])}


def _unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        log.warning("impossibile cancellare %s: %s", path, exc)


# ── Sorgenti audio ───────────────────────────────────────────────────────────
@router.get("/projects/{project_id}/sources")
def get_sources(project_id: int) -> dict:
    return {"sources": db.list_sources(project_id)}


@router.post("/projects/{project_id}/sources", status_code=201)
def post_source_path(project_id: int, body: SourcePathIn) -> dict:
    """
    Registra un file audio **già presente sul disco** (nessuna copia).

    È il percorso zero-copia: il job legge il file dove sta, quindi non consuma
    spazio in `data/`. Il file non viene mai modificato né cancellato.
    """
    if not db.get_project(project_id):
        raise HTTPException(404, "Project not found")
    path = _check_media_path(body.path)
    has_audio, duration = models.probe_media(path)
    if not has_audio:
        raise HTTPException(422, f"No audio track found in {path.name}")
    size = path.stat().st_size
    head = _hash_head(path)
    dup = db.find_duplicate_source(project_id, head, size)
    sid = db.create_source(project_id, str(path), path.name, None, size, duration, head)
    return {"source": db.get_source(sid), "duplicate_of": dup["id"] if dup else None}


@router.post("/projects/{project_id}/upload", status_code=201)
async def upload_source(project_id: int,
                        file: UploadFile = File(...),
                        declared_path: str = Form("")) -> dict:
    """
    Copia in streaming un file scelto dal browser (drag&drop o file picker).

    Il browser non espone il percorso reale del file, quindi caricarlo è
    l'unico modo di trascriverlo. Lo salviamo in `data/sources/<progetto>/` e
    segniamo `original_path` con il percorso dichiarato, quando disponibile.
    """
    if not db.get_project(project_id):
        raise HTTPException(404, "Project not found")
    name = Path(file.filename or "audio").name
    suffix = Path(name).suffix.lower()
    if suffix not in set(models.supported_formats()):
        raise HTTPException(400, f"Unsupported format '{suffix}'")

    target_dir = config.SOURCES / str(project_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_dir / f".upload-{file.filename or 'audio'}.part"
    size = 0
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File too large (limit 4 GB)")
                out.write(chunk)
    except HTTPException:
        _unlink(tmp)
        raise
    except OSError as exc:
        _unlink(tmp)
        raise HTTPException(500, f"Write failed: {exc}") from exc

    digest = hashlib.sha256()
    try:
        with tmp.open("rb") as fh:
            digest.update(fh.read(1024 * 1024))
    except OSError:
        pass
    stored = target_dir / f"{digest.hexdigest()[:12]}-{name}"
    tmp.replace(stored)

    has_audio, duration = models.probe_media(stored)
    if not has_audio:
        _unlink(stored)
        raise HTTPException(422, f"No audio track found in {name}")

    # `original_path` resta vuoto quando il browser non ha potuto dichiarare il
    # percorso: meglio nessuna provenienza che mostrare il path interno con
    # l'hash, che non dice niente all'utente.
    original = declared_path or ""
    sid = db.create_source(project_id, original, name, str(stored), size, duration,
                          digest.hexdigest())
    return {"source": db.get_source(sid)}


@router.get("/sources/{source_id}")
def get_source(source_id: int) -> dict:
    src = db.get_source(source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    return src


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, purge: bool = True) -> dict:
    src = db.get_source(source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    if purge and src.get("stored_path"):
        jobs.cleanup_source(src)
    else:
        db.delete_source(source_id)
    return {"deleted": source_id}


@router.get("/media/probe")
def probe_media_endpoint(path: str) -> dict:
    """Controllo live del percorso digitato nel form (esiste? ha audio?)."""
    p = _check_media_path(path)
    has_audio, duration = models.probe_media(p)
    return {
        "path": str(p),
        "name": p.name,
        "size": p.stat().st_size,
        "has_audio": has_audio,
        "duration": duration,
        "warn_large": p.stat().st_size > config.WARN_UPLOAD_BYTES,
    }


# ── Job ──────────────────────────────────────────────────────────────────────
@router.post("/jobs", status_code=201)
def create_transcribe_job(body: TranscribeIn) -> dict:
    """Crea il job di trascrizione (che parte subito) e l'eventuale job agente."""
    if not db.get_project(body.project_id):
        raise HTTPException(404, "Project not found")
    source = db.get_source(body.source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    if body.model not in MODELS:
        raise HTTPException(422, f"Unsupported model: {body.model}")
    try:
        models.resolve_options(body.model, body.language, body.device, body.compute_type)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.beam_size < 1 or body.beam_size > 20:
        raise HTTPException(422, "beam_size must be between 1 and 20")

    payload = body.model_dump()
    payload["source_ref"] = source["original_path"]
    job_id = db.create_job("transcribe", payload, project_id=body.project_id)
    jobs.enqueue(job_id)
    return {"job": db.get_job(job_id)}


@router.get("/jobs")
def get_jobs(status: str | None = None, limit: int = 100) -> dict:
    statuses = [s for s in (status or "").split(",") if s] or None
    return {"jobs": db.list_jobs(statuses, limit), "queue": jobs.queue_size()}


@router.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    out = dict(job)
    out["running"] = jobs.is_running(job_id)
    out["payload"] = db.jload(job["payload"], {})
    return out


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] == "queued":
        db.update_job(job_id, status="cancelled", finished_at=db.now(),
                      message="cancelled before start")
        return {"cancelled": True, "started": False}
    if job["status"] != "running":
        raise HTTPException(409, f"Job is {job['status']}, nothing to cancel")
    started = jobs.request_cancel(job_id)
    return {"cancelled": True, "started": started}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int) -> dict:
    if not jobs.reset_job_for_retry(job_id):
        raise HTTPException(409, "Job cannot be retried")
    return {"job": db.get_job(job_id)}


@router.post("/transcripts/{transcript_id}/fix", status_code=201)
def create_fix_job(transcript_id: int, body: FixIn) -> dict:
    transcript = db.get_transcript(transcript_id)
    if not transcript:
        raise HTTPException(404, "Transcript not found")
    provider_id = body.provider_id
    if provider_id is None:
        providers = [p for p in db.list_providers() if p["enabled"] and p["has_key"]]
        if not providers:
            raise HTTPException(422, "Configure an LLM provider first (Settings -> Providers)")
        provider_id = providers[0]["id"]
    job_id = db.create_job(
        "llm_fix",
        {"transcript_id": transcript_id, "provider_id": provider_id,
         "instruction": body.instruction, "chunk_chars": body.chunk_chars},
        project_id=transcript["project_id"], transcript_id=transcript_id,
    )
    jobs.enqueue(job_id)
    return {"job": db.get_job(job_id)}


# ── Trascrizioni ─────────────────────────────────────────────────────────────
@router.get("/transcripts")
def get_transcripts(project_id: int | None = None, limit: int = 200) -> dict:
    return {"transcripts": db.list_transcripts(project_id, limit)}


@router.get("/transcripts/{transcript_id}")
def get_transcript(transcript_id: int) -> dict:
    t = db.get_transcript(transcript_id)
    if not t:
        raise HTTPException(404, "Transcript not found")
    t["segments"] = t["segments"] or []
    t["n_proposals_pending"] = len(db.list_proposals(transcript_id, "pending"))
    t["revisions"] = db.list_revisions(transcript_id)
    project = db.get_project(t["project_id"])
    t["project_name"] = project["name"] if project else None
    return t


@router.patch("/transcripts/{transcript_id}")
def patch_transcript(transcript_id: int, body: TranscriptPatch) -> dict:
    transcript = db.get_transcript(transcript_id)
    if not transcript:
        raise HTTPException(404, "Transcript not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "project_id" in fields and not db.get_project(int(fields["project_id"])):
        raise HTTPException(422, "Target project does not exist")

    if "text" in fields:
        old_text = transcript["text"]
        new_text = fields["text"]
        if new_text != old_text:
            segments = text_to_segments(new_text, transcript["segments"])
            fields["segments"] = segments
            n = len(new_text.splitlines()) - len(old_text.splitlines())
            db.add_revision(transcript_id, "manual_edit", max(0, abs(n)),
                            "Edited in the web editor")
    elif "segments" in fields:
        fields["text"] = segments_to_text(fields["segments"])

    db.update_transcript(transcript_id, fields)
    return db.get_transcript(transcript_id) or {}


@router.delete("/transcripts/{transcript_id}")
def delete_transcript(transcript_id: int) -> dict:
    if not db.get_transcript(transcript_id):
        raise HTTPException(404, "Transcript not found")
    db.delete_transcript(transcript_id)
    _unlink(config.transcript_path(transcript_id))
    return {"deleted": transcript_id}


@router.get("/transcripts/{transcript_id}/export")
def export_transcript(transcript_id: int, format: str = "txt") -> Any:
    t = db.get_transcript(transcript_id)
    if not t:
        raise HTTPException(404, "Transcript not found")
    fmt = format.lower()
    payload = t["text"] + "\n"
    media = "text/plain"
    ext = "txt"
    if fmt == "md":
        project = db.get_project(t["project_id"])
        glossary = db.list_terms(t["project_id"])
        payload = build_markdown(t["title"], {
            "course": project["name"] if project else "",
            "language": t["language"],
            "model": t["model"],
            "duration": t["duration"],
        }, t["segments"], glossary)
        media = "text/markdown"
        ext = "md"
    elif fmt == "json":
        payload = json.dumps({
            "title": t["title"],
            "description": t["description"],
            "language": t["language"],
            "model": t["model"],
            "duration": t["duration"],
            "segments": t["segments"],
            "text": t["text"],
        }, ensure_ascii=False, indent=2)
        media = "application/json"
        ext = "json"
    elif fmt == "segments":
        payload = segments_to_text(t["segments"], with_ts=True) + "\n"
        ext = "txt"
    elif fmt != "txt":
        raise HTTPException(400, "format must be txt, md, json or segments")

    safe = _slug(t["title"]) or f"transcript-{transcript_id}"
    return PlainTextResponse(
        payload, media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.{ext}"'},
    )


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in " -_") else "" for c in text.strip()]
    return "-".join("".join(keep).split())[:60].strip("-")


# ── Proposte di correzione ───────────────────────────────────────────────────
@router.get("/transcripts/{transcript_id}/proposals")
def get_proposals(transcript_id: int, status: str | None = None) -> dict:
    if not db.get_transcript(transcript_id):
        raise HTTPException(404, "Transcript not found")
    return {"proposals": db.list_proposals(transcript_id, status)}


@router.post("/transcripts/{transcript_id}/proposals/accept")
def accept_proposals(transcript_id: int, body: ProposalsIn) -> dict:
    """
    Applica le proposte accettate e aggiorna il testo in modo deterministico.

    Solo le proposte `accepted` finiscono nel piano; quelle `ambiguous` richiedono
    un `targets` esplicito (id -> indice del segmento) e altrimenti sono saltate.
    """
    transcript = db.get_transcript(transcript_id)
    if not transcript:
        raise HTTPException(404, "Transcript not found")
    rows = db.get_proposals(body.ids)
    if not rows:
        raise HTTPException(404, "No matching proposals")

    segments = transcript["segments"] or text_to_segments(transcript["text"])
    for prop in rows:
        prop["status"] = "accepted"
    plan = corr.build_plan(segments, rows)
    result = corr.apply_plan(plan, targets={int(k): int(v) for k, v in (body.targets or {}).items()})

    if result["applied"]:
        db.update_transcript(transcript_id, {
            "text": result["text"],
            "segments": result["segments"],
        })
        db.add_revision(transcript_id, "agent_fix", result["applied"],
                        f"{result['applied']} replacements applied")
    db.set_proposal_status(body.ids, "accepted")

    return {
        "applied": result["applied"],
        "skipped": result["details"],
        "changed_segments": result["changed_segments"],
        "text": result["text"],
        "segments": result["segments"],
    }


@router.post("/transcripts/{transcript_id}/proposals/reject")
def reject_proposals(transcript_id: int, body: ProposalsIn) -> dict:
    if not db.get_transcript(transcript_id):
        raise HTTPException(404, "Transcript not found")
    n = db.set_proposal_status(body.ids, "rejected")
    return {"rejected": n}


@router.post("/transcripts/{transcript_id}/proposals/undo")
def undo_pending(transcript_id: int) -> dict:
    """Elimina le proposte ancora in attesa (es. dopo un fix sbagliato)."""
    if not db.get_transcript(transcript_id):
        raise HTTPException(404, "Transcript not found")
    return {"removed": db.clear_pending_proposals(transcript_id)}


@router.post("/transcripts/{transcript_id}/proposals/preview")
def preview_proposals(transcript_id: int, body: ProposalsIn) -> dict:
    """Anteprima del risultato (diff riga per riga) senza scrivere nulla."""
    transcript = db.get_transcript(transcript_id)
    if not transcript:
        raise HTTPException(404, "Transcript not found")
    rows = db.get_proposals(body.ids)
    for prop in rows:
        prop["status"] = "accepted"
    segments = transcript["segments"] or text_to_segments(transcript["text"])
    plan = corr.build_plan(segments, rows)
    result = corr.apply_plan(plan, targets={int(k): int(v) for k, v in (body.targets or {}).items()})
    return {
        "applied": result["applied"],
        "skipped": result["details"],
        "diff": corr.diff_preview(transcript["text"], result["text"]),
    }


# ── Glossario di materia ─────────────────────────────────────────────────────
@router.get("/terms")
def get_terms(project_id: int) -> dict:
    return {"terms": db.list_terms(project_id)}


@router.post("/terms", status_code=201)
def post_term(body: TermIn) -> dict:
    if not db.get_project(body.project_id):
        raise HTTPException(404, "Project not found")
    tid = db.upsert_term(body.project_id, body.find.strip(), body.replace,
                         body.auto, body.note)
    return {"id": tid, "terms": db.list_terms(body.project_id)}


@router.delete("/terms/{term_id}")
def delete_term(term_id: int) -> dict:
    db.delete_term(term_id)
    return {"deleted": term_id}


# ── Provider LLM ─────────────────────────────────────────────────────────────
@router.get("/providers")
def get_providers() -> dict:
    return {"providers": db.list_providers()}


@router.patch("/providers/{provider_id}")
def patch_provider(provider_id: int, body: ProviderIn) -> dict:
    provider = db.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, "Provider not found")
    api_key_enc = None
    if body.api_key is not None:
        # Stringa vuota = rimuovi la chiave salvata.
        api_key_enc = llm.encrypt_key(body.api_key) if body.api_key else ""
    db.update_provider(provider_id, base_url=body.base_url, model=body.model,
                       enabled=body.enabled, api_key_enc=api_key_enc)
    return {"providers": db.list_providers()}


@router.post("/providers/{provider_id}/test")
def test_provider(provider_id: int, body: dict = Body(default={})) -> dict:
    provider = db.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, "Provider not found")
    if body.get("model"):
        db.update_provider(provider_id, model=str(body["model"]))
        provider = db.get_provider(provider_id) or provider
    try:
        return llm.test_provider(provider)
    except Exception as exc:  # noqa: BLE001 - l'errore va mostrato all'utente
        return {"ok": False, "error": str(exc)[:300]}


@router.get("/providers/{provider_id}/models")
def provider_models(provider_id: int) -> dict:
    provider = db.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, "Provider not found")
    try:
        return {"models": llm.list_models(provider)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc


# ── Impostazioni e manutenzione ──────────────────────────────────────────────
_SETTINGS_FIELDS = {"default_project_id", "default_model", "default_language",
                    "default_device", "default_compute_type", "default_provider_id",
                    "flush_every", "llm_chunk_chars", "delete_sources_after_job",
                    "backup_keep"}


@router.get("/settings")
def get_settings() -> dict:
    s = config.load_settings()
    return {"settings": s.to_json(), "disk": db.disk_usage()}


@router.put("/settings")
def put_settings(body: dict = Body(...)) -> dict:
    s = config.load_settings()
    for key, value in body.items():
        if key in _SETTINGS_FIELDS:
            setattr(s, key, value)
        else:
            s.extra[key] = value
    config.save_settings(s)
    return {"settings": s.to_json()}


@router.post("/maintenance/backup")
def do_backup() -> dict:
    path = db.backup()
    return {"path": str(path), "name": path.name,
            "size": path.stat().st_size if path.exists() else 0}


@router.get("/maintenance/backups")
def list_backups() -> dict:
    files = sorted(config.BACKUPS.glob("trascrivi-*.db"), reverse=True)
    return {"backups": [{"name": p.name, "size": p.stat().st_size,
                         "modified": p.stat().st_mtime} for p in files]}


@router.post("/maintenance/purge-sources")
def do_purge() -> dict:
    return {"purged": jobs.purge_all_sources()}


# ── App ──────────────────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(title="Trascrivi", version=_version(),
                  description="Local lecture transcription and study registry.")
    app.include_router(router)

    @app.on_event("startup")
    def _startup() -> None:
        config.load_env_file()
        config.ensure_dirs()
        config.setup_file_logging()
        db.init_db()
        jobs.recover_stale_jobs()
        jobs.start_worker()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        jobs.stop_worker(timeout=20.0)

    @app.exception_handler(ValueError)
    def _value_error(_request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    if config.WEB.exists():
        app.mount("/", StaticFiles(directory=str(config.WEB), html=True), name="web")
    return app
