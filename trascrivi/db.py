"""
trascrivi/db.py
===============
Persistenza su SQLite (stdlib): schema, query e ricerca full-text FTS5.

Scelte deliberatamente povere:
* SQL a mano, nessun ORM — lo schema è piccolo e stabile;
* una connessione per chiamata in WAL — niente pool da gestire, e il worker dei
  job (thread separato) non condivide connessioni con la threadpool di FastAPI;
* FTS5 tenuto sincronizzato da trigger nativi, mai da codice applicativo.

`ponytail: nessun layer di repository. Le query stanno qui e le rotte le
chiamano direttamente; se un giorno servisse un secondo storage, il confine da
spostare è questo file.`
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

log = logging.getLogger("trascrivi.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  code        TEXT,
  color       TEXT DEFAULT '#4f46e5',
  description TEXT DEFAULT '',
  archived    INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  original_path TEXT NOT NULL,
  stored_path   TEXT,
  name          TEXT NOT NULL,
  sha256_1m     TEXT,
  bytes         INTEGER DEFAULT 0,
  duration      REAL,
  status        TEXT NOT NULL DEFAULT 'ok',
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_project ON sources(project_id);

CREATE TABLE IF NOT EXISTS jobs (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,
  project_id    INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  transcript_id INTEGER,
  payload       TEXT NOT NULL DEFAULT '{}',
  status        TEXT NOT NULL DEFAULT 'queued',
  progress      REAL NOT NULL DEFAULT 0,
  audio_seconds REAL NOT NULL DEFAULT 0,
  total_seconds REAL,
  speed         REAL,
  eta_seconds   REAL,
  message       TEXT,
  error         TEXT,
  output_path   TEXT,
  created_at    REAL NOT NULL,
  started_at    REAL,
  finished_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS transcripts (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id   INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  title       TEXT NOT NULL,
  description TEXT DEFAULT '',
  language    TEXT,
  model       TEXT,
  device      TEXT,
  duration    REAL DEFAULT 0,
  segments    TEXT NOT NULL DEFAULT '[]',
  text        TEXT NOT NULL DEFAULT '',
  source_ref  TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_project ON transcripts(project_id);

CREATE TABLE IF NOT EXISTS proposals (
  id            INTEGER PRIMARY KEY,
  transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  job_id        INTEGER,
  find          TEXT NOT NULL,
  replace       TEXT NOT NULL,
  reason        TEXT DEFAULT '',
  kind          TEXT DEFAULT 'other',
  confidence    REAL,
  status        TEXT NOT NULL DEFAULT 'pending',
  flag          TEXT NOT NULL DEFAULT 'ok',
  occurrences   INTEGER DEFAULT 0,
  context       TEXT DEFAULT '',
  segment_index INTEGER,
  applied_at    REAL,
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_transcript ON proposals(transcript_id, status);

CREATE TABLE IF NOT EXISTS revisions (
  id            INTEGER PRIMARY KEY,
  transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,
  n_changes     INTEGER NOT NULL DEFAULT 0,
  summary       TEXT DEFAULT '',
  created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS terms (
  id         INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  find       TEXT NOT NULL,
  replace    TEXT NOT NULL,
  auto       INTEGER NOT NULL DEFAULT 0,
  note       TEXT DEFAULT '',
  created_at REAL NOT NULL,
  UNIQUE(project_id, find)
);

CREATE TABLE IF NOT EXISTS providers (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  base_url     TEXT NOT NULL,
  model        TEXT DEFAULT '',
  fallback_model TEXT DEFAULT '',
  api_key_enc  TEXT,
  enabled      INTEGER NOT NULL DEFAULT 1,
  extra        TEXT DEFAULT '{}',
  updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
  title, text, content='transcripts', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS transcripts_ai AFTER INSERT ON transcripts BEGIN
  INSERT INTO transcripts_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS transcripts_ad AFTER DELETE ON transcripts BEGIN
  INSERT INTO transcripts_fts(transcripts_fts, rowid, title, text)
  VALUES ('delete', old.id, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS transcripts_au AFTER UPDATE ON transcripts BEGIN
  INSERT INTO transcripts_fts(transcripts_fts, rowid, title, text)
  VALUES ('delete', old.id, old.title, old.text);
  INSERT INTO transcripts_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
"""

PROVIDER_DEFAULTS = [
    ("ollama", "http://localhost:11434/v1", "qwen2.5:7b", ""),
    ("deepseek", "https://api.deepseek.com", "deepseek-chat", "deepseek-chat"),
    ("openrouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-chat-v3.1", ""),
]


def now() -> float:
    return time.time()


def connect() -> sqlite3.Connection:
    """Connessione configurata (WAL, foreign key, timeout). Chiamante chiude."""
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """Crea lo schema, migra le colonne mancanti e semina i provider."""
    config.ensure_dirs()
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        ts = now()
        for name, base_url, model, fallback in PROVIDER_DEFAULTS:
            conn.execute(
                "INSERT OR IGNORE INTO providers(name, base_url, model, fallback_model, "
                "enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, base_url, model, fallback, 1 if name == "ollama" else 0, ts),
            )
        # Il modello di ripiego serve solo dove un modello "reasoning" può
        # esaurire il budget senza produrre output: se la colonna è vuota lo
        # completiamo, senza toccare una scelta esplicita dell'utente.
        conn.execute(
            "UPDATE providers SET fallback_model = 'deepseek-chat' "
            "WHERE name = 'deepseek' AND (fallback_model IS NULL OR fallback_model = '')"
        )
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Aggiunge le colonne introdotte dopo la prima release, se mancano."""
    wanted = {
        "jobs": [("output_path", "TEXT")],
        "proposals": [("segment_index", "INTEGER"), ("context", "TEXT DEFAULT ''")],
        "transcripts": [("source_ref", "TEXT")],
        "providers": [("fallback_model", "TEXT DEFAULT ''")],
    }
    for table, columns in wanted.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("migrazione: %s.%s aggiunta", table, name)


# ── Helper ───────────────────────────────────────────────────────────────────
def rows(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def row(sql: str, params: Sequence[Any] = ()) -> dict | None:
    with connect() as conn:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None


def scalar(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    with connect() as conn:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r and r[0] is not None else default


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Esegue una scrittura e restituisce l'ultimo rowid."""
    with connect() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    with connect() as conn:
        conn.executemany(sql, list(seq))
        conn.commit()


def jload(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ── Projects ─────────────────────────────────────────────────────────────────
def list_projects() -> list[dict]:
    return rows(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM transcripts t WHERE t.project_id = p.id) AS n_transcripts,
               (SELECT COUNT(*) FROM sources s WHERE s.project_id = p.id AND s.status='ok') AS n_sources
        FROM projects p
        ORDER BY p.archived ASC, p.updated_at DESC
        """
    )


def get_project(project_id: int) -> dict | None:
    return row("SELECT * FROM projects WHERE id = ?", (project_id,))


def create_project(name: str, code: str = "", color: str = "#4f46e5",
                   description: str = "") -> int:
    ts = now()
    return execute(
        "INSERT INTO projects(name, code, color, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name.strip(), code.strip(), color, description, ts, ts),
    )


PROJECT_FIELDS = {"name", "code", "color", "description", "archived"}


def update_project(project_id: int, fields: dict) -> bool:
    clean = {k: v for k, v in fields.items() if k in PROJECT_FIELDS}
    if not clean:
        return False
    sets = ", ".join(f"{k} = ?" for k in clean)
    params = list(clean.values()) + [now(), project_id]
    execute(f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?", params)
    return True


def delete_project(project_id: int) -> dict:
    """Elimina progetto e figli; restituisce i path dei file da cancellare."""
    payload = {"sources": [], "transcripts": []}
    for s in rows("SELECT stored_path FROM sources WHERE project_id = ? AND stored_path IS NOT NULL",
                  (project_id,)):
        payload["sources"].append(s["stored_path"])
    for t in rows("SELECT id FROM transcripts WHERE project_id = ?", (project_id,)):
        payload["transcripts"].append(t["id"])
    execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return payload


# ── Sources ──────────────────────────────────────────────────────────────────
def create_source(project_id: int, original_path: str, name: str,
                  stored_path: str | None, size: int, duration: float | None,
                  sha256_1m: str | None = None) -> int:
    return execute(
        """INSERT INTO sources(project_id, original_path, stored_path, name, sha256_1m,
                               bytes, duration, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', ?)""",
        (project_id, original_path, stored_path, name, sha256_1m, size, duration, now()),
    )


def get_source(source_id: int) -> dict | None:
    return row("SELECT * FROM sources WHERE id = ?", (source_id,))


def list_sources(project_id: int) -> list[dict]:
    return rows("SELECT * FROM sources WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,))


def set_source_status(source_id: int, status: str) -> None:
    execute("UPDATE sources SET status = ? WHERE id = ?", (status, source_id))


def set_source_duration(source_id: int, duration: float) -> None:
    execute("UPDATE sources SET duration = ? WHERE id = ?", (duration, source_id))


def find_duplicate_source(project_id: int, sha256_1m: str, size: int) -> dict | None:
    return row(
        "SELECT * FROM sources WHERE project_id = ? AND sha256_1m = ? AND bytes = ?",
        (project_id, sha256_1m, size),
    )


def delete_source(source_id: int) -> dict | None:
    src = get_source(source_id)
    if src:
        execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return src


def disk_usage() -> dict:
    """Spazio occupato dalle copie locali, per il pannello impostazioni."""
    total = 0
    n = 0
    if config.SOURCES.exists():
        for p in config.SOURCES.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                    n += 1
                except OSError:
                    pass
    return {
        "sources_bytes": total,
        "sources_files": n,
        "data_bytes": _dir_size(config.DATA),
        "free_bytes": config.free_space_bytes(config.DATA),
    }


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


# ── Jobs ─────────────────────────────────────────────────────────────────────
JOB_FIELDS = {"payload", "status", "progress", "audio_seconds", "total_seconds", "speed",
              "eta_seconds", "message", "error", "output_path", "transcript_id",
              "started_at", "finished_at"}


def create_job(kind: str, payload: dict, project_id: int | None = None,
               transcript_id: int | None = None) -> int:
    return execute(
        "INSERT INTO jobs(kind, project_id, transcript_id, payload, status, created_at) "
        "VALUES (?, ?, ?, ?, 'queued', ?)",
        (kind, project_id, transcript_id, jdump(payload), now()),
    )


def get_job(job_id: int) -> dict | None:
    return row("SELECT * FROM jobs WHERE id = ?", (job_id,))


def list_jobs(statuses: Sequence[str] | None = None, limit: int = 100) -> list[dict]:
    if statuses:
        marks = ",".join("?" for _ in statuses)
        return rows(f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
                    (*statuses, limit))
    return rows("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))


def update_job(job_id: int, **fields) -> None:
    clean = {k: v for k, v in fields.items() if k in JOB_FIELDS}
    if not clean:
        return
    sets = ", ".join(f"{k} = ?" for k in clean)
    execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*clean.values(), job_id))


def next_queued_job() -> dict | None:
    return row("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id ASC LIMIT 1")


def mark_interrupted_jobs() -> int:
    """
    All'avvio: i job rimasti 'running'/'queued' sono morti con il processo.

    Diventano 'interrupted' così la UI offre Resume invece di mostrare una barra
    di progresso ferma per sempre. Il lavoro parziale è in data/partial/.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='interrupted', finished_at=?, "
            "message=COALESCE(message, 'Server stopped while this job was running') "
            "WHERE status IN ('running', 'queued')",
            (now(),),
        )
        conn.commit()
        return cur.rowcount


# ── Transcripts ──────────────────────────────────────────────────────────────
def create_transcript(project_id: int, title: str, text: str, segments: list[dict],
                      source_id: int | None = None, description: str = "",
                      language: str | None = None, model: str | None = None,
                      device: str | None = None, duration: float = 0.0,
                      source_ref: str | None = None) -> int:
    if not segments and text:
        from .textutil import text_to_segments
        segments = text_to_segments(text)
    ts = now()
    return execute(
        """INSERT INTO transcripts(project_id, source_id, title, description, language,
                                   model, device, duration, segments, text, source_ref,
                                   created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, source_id, title, description, language, model, device, duration,
         jdump(segments), text, source_ref, ts, ts),
    )


def get_transcript(transcript_id: int) -> dict | None:
    t = row("SELECT * FROM transcripts WHERE id = ?", (transcript_id,))
    if t:
        t["segments"] = jload(t["segments"], [])
    return t


def list_transcripts(project_id: int | None = None, limit: int = 200) -> list[dict]:
    """Elenco leggero: senza testo né segmenti (le liste non ne hanno bisogno)."""
    if project_id is None:
        sql = ("SELECT id, project_id, title, description, language, model, duration, "
               "LENGTH(text) AS length, created_at, updated_at FROM transcripts "
               "ORDER BY updated_at DESC LIMIT ?")
        params: Sequence[Any] = (limit,)
    else:
        sql = ("SELECT id, project_id, title, description, language, model, duration, "
               "LENGTH(text) AS length, created_at, updated_at FROM transcripts "
               "WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?")
        params = (project_id, limit)
    return rows(sql, params)


TRANSCRIPT_FIELDS = {"title", "description", "language", "segments", "text", "project_id",
                     "source_id", "source_ref", "duration", "model", "device"}


def update_transcript(transcript_id: int, fields: dict) -> None:
    clean = {k: v for k, v in fields.items() if k in TRANSCRIPT_FIELDS}
    if not clean:
        return
    if "segments" in clean and not isinstance(clean["segments"], str):
        clean["segments"] = jdump(clean["segments"])
    sets = ", ".join(f"{k} = ?" for k in clean)
    execute(f"UPDATE transcripts SET {sets}, updated_at = ? WHERE id = ?",
            (*clean.values(), now(), transcript_id))


def delete_transcript(transcript_id: int) -> None:
    execute("DELETE FROM transcripts WHERE id = ?", (transcript_id,))


def search_transcripts(query: str, project_id: int | None = None, limit: int = 50) -> list[dict]:
    """
    Ricerca full-text con snippet evidenziato.

    FTS5 ha una sintassi propria: la query utente va passata come frase quotata
    per evitare errori di parsing (parentesi, `*`, `-`…).
    """
    safe = '"' + query.replace('"', '""') + '"'
    sql = """
        SELECT t.id, t.title, t.project_id, p.name AS project_name,
               snippet(transcripts_fts, 1, '[[', ']]', '…', 12) AS snippet,
               bm25(transcripts_fts) AS score
        FROM transcripts_fts
        JOIN transcripts t ON t.id = transcripts_fts.rowid
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE transcripts_fts MATCH ?
    """
    params: list[Any] = [safe]
    if project_id is not None:
        sql += " AND t.project_id = ?"
        params.append(project_id)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        return rows(sql, params)
    except sqlite3.OperationalError as exc:  # sintassi FTS non valida
        log.warning("ricerca FTS fallita (%s) per query %r", exc, query)
        return []


# ── Proposte di correzione ───────────────────────────────────────────────────
def create_proposals(transcript_id: int, job_id: int | None, items: list[dict]) -> int:
    ts = now()
    payload = [
        (transcript_id, job_id, it["find"], it["replace"], it.get("reason", ""),
         it.get("kind", "other"), it.get("confidence"), it.get("status", "pending"),
         it.get("flag", "ok"), it.get("occurrences", 0), it.get("context", ""),
         it.get("segment_index"), ts)
        for it in items
    ]
    if not payload:
        return 0
    with connect() as conn:
        conn.executemany(
            """INSERT INTO proposals(transcript_id, job_id, find, replace, reason, kind,
                                     confidence, status, flag, occurrences, context,
                                     segment_index, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            payload,
        )
        conn.commit()
    return len(payload)


def list_proposals(transcript_id: int, status: str | None = None) -> list[dict]:
    if status:
        return rows("SELECT * FROM proposals WHERE transcript_id = ? AND status = ? "
                    "ORDER BY id", (transcript_id, status))
    return rows("SELECT * FROM proposals WHERE transcript_id = ? ORDER BY id", (transcript_id,))


def get_proposals(ids: Sequence[int]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return rows(f"SELECT * FROM proposals WHERE id IN ({marks}) ORDER BY id", tuple(ids))


def set_proposal_status(ids: Sequence[int], status: str) -> int:
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE proposals SET status = ?, applied_at = ? WHERE id IN ({marks})",
            (status, now(), *ids),
        )
        conn.commit()
        return cur.rowcount


def clear_pending_proposals(transcript_id: int) -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM proposals WHERE transcript_id = ? AND status='pending'",
                           (transcript_id,))
        conn.commit()
        return cur.rowcount


def add_revision(transcript_id: int, kind: str, n_changes: int, summary: str = "") -> int:
    return execute(
        "INSERT INTO revisions(transcript_id, kind, n_changes, summary, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (transcript_id, kind, n_changes, summary, now()),
    )


def list_revisions(transcript_id: int) -> list[dict]:
    return rows("SELECT * FROM revisions WHERE transcript_id = ? ORDER BY id DESC",
                (transcript_id,))


# ── Glossario di materia ─────────────────────────────────────────────────────
def list_terms(project_id: int) -> list[dict]:
    return rows("SELECT * FROM terms WHERE project_id = ? ORDER BY find COLLATE NOCASE",
                (project_id,))


def upsert_term(project_id: int, find: str, replace: str, auto: bool = False,
                note: str = "") -> int:
    """
    Inserisce o aggiorna un termine del glossario. Restituisce sempre il suo id.

    `ON CONFLICT` non aggiorna `lastrowid` sul ramo UPDATE, quindi lo leggiamo
    esplicitamente: il chiamante (e la UI) si aspetta un id valido in entrambi i casi.
    """
    with connect() as conn:
        conn.execute(
            """INSERT INTO terms(project_id, find, replace, auto, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, find) DO UPDATE SET
                 replace = excluded.replace, auto = excluded.auto, note = excluded.note""",
            (project_id, find, replace, 1 if auto else 0, note, now()),
        )
        row_id = conn.execute("SELECT id FROM terms WHERE project_id = ? AND find = ?",
                              (project_id, find)).fetchone()
        conn.commit()
        return int(row_id[0]) if row_id else 0


def delete_term(term_id: int) -> None:
    execute("DELETE FROM terms WHERE id = ?", (term_id,))


# ── Provider LLM ─────────────────────────────────────────────────────────────
def list_providers() -> list[dict]:
    """Non espone mai la chiave: solo `has_key`."""
    out = rows("SELECT id, name, base_url, model, fallback_model, enabled, extra, updated_at, "
               "api_key_enc IS NOT NULL AS has_key FROM providers ORDER BY id")
    for p in out:
        p["has_key"] = bool(p["has_key"])
        p["extra"] = jload(p["extra"], {})
    return out


def get_provider(provider_id: int) -> dict | None:
    return row("SELECT * FROM providers WHERE id = ?", (provider_id,))


def get_provider_by_name(name: str) -> dict | None:
    return row("SELECT * FROM providers WHERE name = ?", (name,))


def update_provider(provider_id: int, base_url: str | None = None,
                    model: str | None = None, fallback_model: str | None = None,
                    enabled: bool | None = None,
                    api_key_enc: str | None = None, extra: dict | None = None) -> None:
    sets: list[str] = []
    params: list[Any] = []
    if base_url is not None:
        sets.append("base_url = ?")
        params.append(base_url)
    if model is not None:
        sets.append("model = ?")
        params.append(model)
    if fallback_model is not None:
        sets.append("fallback_model = ?")
        params.append(fallback_model)
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    # `api_key_enc=""` cancella la chiave (None invece significa "non toccarla").
    if api_key_enc is not None:
        sets.append("api_key_enc = ?")
        params.append(api_key_enc or None)
    if extra is not None:
        sets.append("extra = ?")
        params.append(jdump(extra))
    if not sets:
        return
    sets.append("updated_at = ?")
    params.extend([now(), provider_id])
    execute(f"UPDATE providers SET {', '.join(sets)} WHERE id = ?", params)


# ── Impostazioni key/value ───────────────────────────────────────────────────
def get_setting(key: str, default: Any = None) -> Any:
    return jload(scalar("SELECT value FROM settings WHERE key = ?", (key,)), default)


def set_setting(key: str, value: Any) -> None:
    execute("INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, jdump(value)))


# ── Backup ───────────────────────────────────────────────────────────────────
def backup() -> Path:
    """Copia consistente del DB (usa l'API di backup di SQLite, non l'FS)."""
    config.ensure_dirs()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = config.BACKUPS / f"trascrivi-{stamp}.db"
    with connect() as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    _prune_backups()
    return target


def _prune_backups(keep: int | None = None) -> None:
    if keep is None:
        keep = int(get_setting("backup_keep", 5) or 5)
    files = sorted(config.BACKUPS.glob("trascrivi-*.db"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
