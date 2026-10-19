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
import re
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
  color       TEXT DEFAULT '#ff4a17',
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
  -- Costo stimato delle chiamate LLM di questo job (0 = nessuna chiamata a
  -- pagamento, oppure job vecchio, precedente alla colonna: vedi `_migrate`).
  tokens_in     INTEGER DEFAULT 0,
  tokens_out    INTEGER DEFAULT 0,
  cache_hit     INTEGER DEFAULT 0,
  cost_usd      REAL DEFAULT 0,
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

-- Un riassunto per riga, non una colonna su transcripts: rigenerare o cambiare
-- stile non deve distruggere il precedente. `markdown` e' il documento, ed e'
-- l'unico contenuto: la tab lo mostra e lo esporta cosi' com'e'.
CREATE TABLE IF NOT EXISTS summaries (
  id            INTEGER PRIMARY KEY,
  transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  job_id        INTEGER,
  style         TEXT NOT NULL DEFAULT 'study',
  language      TEXT,
  provider      TEXT DEFAULT '',
  model         TEXT DEFAULT '',
  title         TEXT DEFAULT '',
  overview      TEXT DEFAULT '',
  markdown      TEXT NOT NULL DEFAULT '',
  n_words       INTEGER DEFAULT 0,
  target_words  INTEGER DEFAULT 0,
  source_words  INTEGER DEFAULT 0,
  tokens_in     INTEGER DEFAULT 0,
  tokens_out    INTEGER DEFAULT 0,
  cache_hit     INTEGER DEFAULT 0,
  cost_usd      REAL DEFAULT 0,
  elapsed_s     REAL DEFAULT 0,
  edited        INTEGER NOT NULL DEFAULT 0,
  -- Il modello ha esaurito il tetto di output: il documento c'è ma è tagliato,
  -- e va detto a schermo invece di consegnarlo come se fosse completo.
  truncated     INTEGER NOT NULL DEFAULT 0,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_transcript
  ON summaries(transcript_id, created_at DESC);

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

-- ── Slide (PIANO-slide-e-carte) ─────────────────────────────────────────────
-- Un documento caricato dal browser, già copiato in data/slides/<corso>/<doc>/.
-- `boilerplate` è l'elenco JSON delle righe filtrate perché ricorrono su più
-- della metà delle pagine: serve alla UI per dire cosa è stato buttato via.
CREATE TABLE IF NOT EXISTS slide_docs (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  ext         TEXT NOT NULL DEFAULT '',
  sha256      TEXT NOT NULL DEFAULT '',
  n_pages     INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'parsing',
  error       TEXT,
  boilerplate TEXT NOT NULL DEFAULT '[]',
  vlm_enabled INTEGER NOT NULL DEFAULT 1,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slide_docs_project ON slide_docs(project_id);

-- Una riga per pagina. `text` è il testo nativo + OCR, `vlm_md` il rendering
-- strutturato della pagina complessa. `render_path` è il PNG su cui girano OCR e
-- VLM (e che la vision mode allega al generatore di carte); `render_sha` dice se
-- il pass VLM va rifatto.
CREATE TABLE IF NOT EXISTS slide_pages (
  doc_id         INTEGER NOT NULL REFERENCES slide_docs(id) ON DELETE CASCADE,
  page_no        INTEGER NOT NULL,
  text           TEXT NOT NULL DEFAULT '',
  complex        INTEGER NOT NULL DEFAULT 0,
  vlm_md         TEXT,
  math_uncertain INTEGER NOT NULL DEFAULT 0,
  n_images       INTEGER NOT NULL DEFAULT 0,
  ocr_chars      INTEGER NOT NULL DEFAULT 0,
  has_text       INTEGER NOT NULL DEFAULT 0,
  render_path    TEXT,
  render_sha     TEXT,
  PRIMARY KEY (doc_id, page_no)
);

-- Un intervallo di pagine assegnato a una lezione. `source='auto'` viene dal
-- matching per embeddings, `manual` da una conferma o correzione in UI.
CREATE TABLE IF NOT EXISTS slide_links (
  id            INTEGER PRIMARY KEY,
  doc_id        INTEGER NOT NULL REFERENCES slide_docs(id) ON DELETE CASCADE,
  page_from     INTEGER NOT NULL,
  page_to       INTEGER NOT NULL,
  transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  score         REAL NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT 'auto',
  updated_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_slide_links_unique
  ON slide_links(doc_id, page_from, page_to);
CREATE INDEX IF NOT EXISTS idx_slide_links_transcript ON slide_links(transcript_id);

-- ── Carte ───────────────────────────────────────────────────────────────────
-- `uid` è immutabile e diventa il GUID della nota in Anki: re-importare un mazzo
-- aggiorna la carta, non ne crea una seconda.
CREATE TABLE IF NOT EXISTS cards (
  id            INTEGER PRIMARY KEY,
  uid           TEXT NOT NULL UNIQUE,
  transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL DEFAULT 'qa',
  front         TEXT NOT NULL,
  back          TEXT NOT NULL DEFAULT '',
  origin        TEXT NOT NULL DEFAULT 'llm',
  edited        INTEGER NOT NULL DEFAULT 0,
  slide_ref     TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  model         TEXT DEFAULT '',
  run_id        INTEGER,
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_transcript ON cards(transcript_id, status);

-- Un run di generazione: `inputs_hash` è la cache (stessi input = non si paga
-- due volte) e insieme il registro del costo di quella chiamata.
CREATE TABLE IF NOT EXISTS card_runs (
  id            INTEGER PRIMARY KEY,
  transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  job_id        INTEGER,
  inputs_hash   TEXT NOT NULL DEFAULT '',
  provider      TEXT DEFAULT '',
  model         TEXT DEFAULT '',
  prompt_version TEXT DEFAULT '',
  n_new         INTEGER DEFAULT 0,
  n_skipped_dup INTEGER DEFAULT 0,
  tokens_in     INTEGER DEFAULT 0,
  tokens_out    INTEGER DEFAULT 0,
  cache_hit     INTEGER DEFAULT 0,
  cost_usd      REAL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'ok',
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_card_runs_transcript
  ON card_runs(transcript_id, created_at DESC);

-- Embeddings dei riassunti e delle pagine, in float32: il matching pagina ->
-- lezione è un prodotto scalare, non serve SQLite vettoriale. `source_sha` è
-- l'impronta del testo da cui il vettore è stato calcolato: senza, un riassunto
-- riscritto (o una pagina riletta con la VLM) continuerebbe a usare il vettore
-- vecchio e il matching risponderebbe su un testo che non esiste più.
CREATE TABLE IF NOT EXISTS vectors (
  kind       TEXT NOT NULL,
  ref_id     INTEGER NOT NULL,
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL DEFAULT 0,
  source_sha TEXT NOT NULL DEFAULT '',
  vec        BLOB NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (kind, ref_id, model)
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
    ("deepseek", "https://api.deepseek.com", "deepseek-flash", ""),
    ("openrouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-chat-v3.1", ""),
]

# Modelli DeepSeek ritirati il 2026-07-24: `deepseek-chat` e `deepseek-reasoner`
# non esistono piu' nel listino. Un database creato prima di quella data li ha
# ancora salvati e ogni chiamata fallirebbe; li rimappiamo sul modello corrente
# SOLO se il valore e' rimasto quello seminato da noi, per non sovrascrivere una
# scelta esplicita dell'utente.
RETIRED_DEEPSEEK = ("deepseek-chat", "deepseek-reasoner")


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
        # Migrazione dei nomi di modello DeepSeek ritirati: senza, un database
        # creato prima del 2026-07-24 continua a chiamare `deepseek-chat` e ogni
        # richiesta dell'agente o del riassunto fallisce con "model not found".
        marks = ",".join("?" for _ in RETIRED_DEEPSEEK)
        for column in ("model", "fallback_model"):
            cur = conn.execute(
                f"UPDATE providers SET {column} = 'deepseek-flash' "
                f"WHERE name = 'deepseek' AND {column} IN ({marks})",
                RETIRED_DEEPSEEK,
            )
            if cur.rowcount:
                log.info("migrazione: providers.deepseek.%s aggiornato a deepseek-flash (%d righe)",
                         column, cur.rowcount)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Aggiunge le colonne introdotte dopo la prima release, se mancano."""
    wanted = {
        # Il costo delle chiamate LLM si registra sul job che le ha fatte: prima
        # esisteva solo su `summaries`, quindi le correzioni non erano
        # contabilizzabili. I job già passati restano a zero — la diagnostica non
        # è stata salvata — e la vista costi lo dice invece di fingere uno zero.
        "jobs": [("output_path", "TEXT"),
                 ("tokens_in", "INTEGER DEFAULT 0"),
                 ("tokens_out", "INTEGER DEFAULT 0"),
                 ("cache_hit", "INTEGER DEFAULT 0"),
                 ("cost_usd", "REAL DEFAULT 0")],
        "proposals": [("segment_index", "INTEGER"), ("context", "TEXT DEFAULT ''")],
        "transcripts": [("source_ref", "TEXT")],
        "providers": [("fallback_model", "TEXT DEFAULT ''")],
        # `summaries` è nata con una struttura a punti e citazioni, poi sostituita
        # dal documento markdown: `n_words` è arrivata dopo, e `target_words` /
        # `source_words` con il budget di lunghezza. Senza queste righe un
        # database che ha già la tabella vecchia fallisce su ogni INSERT e SELECT,
        # perché `CREATE TABLE IF NOT EXISTS` non tocca una tabella esistente.
        "summaries": [("n_words", "INTEGER DEFAULT 0"),
                      ("target_words", "INTEGER DEFAULT 0"),
                      ("source_words", "INTEGER DEFAULT 0"),
                      ("truncated", "INTEGER NOT NULL DEFAULT 0")],
        # `vectors` è nata senza l'impronta del testo: aggiunta quando si è
        # visto che un riassunto riscritto lasciava il vettore vecchio.
        "vectors": [("source_sha", "TEXT NOT NULL DEFAULT ''")],
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


def create_project(name: str, code: str = "", color: str = "#ff4a17",
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
              "started_at", "finished_at", "tokens_in", "tokens_out", "cache_hit", "cost_usd"}


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


# ── Costi delle chiamate LLM ─────────────────────────────────────────────────
# Due sorgenti, una per tipo di lavoro, e non è una duplicazione: i riassunti
# hanno una riga propria in `summaries` (con provider e modello, perché più
# versioni convivono), le correzioni esistono solo come job. Si restituiscono
# celle già raggruppate — corso, mese, tipo — perché è il minimo che serve a
# filtrare per mese e per tipo senza tornare sul database a ogni cambio di menu.
COST_CELLS_SQL = """
SELECT project_id, month, kind,
       SUM(cost_usd) AS cost_usd, SUM(tokens) AS tokens, SUM(n_jobs) AS n_jobs
FROM (
    SELECT j.project_id AS project_id,
           strftime('%Y-%m', j.finished_at, 'unixepoch', 'localtime') AS month,
           j.kind AS kind,
           SUM(j.cost_usd) AS cost_usd,
           SUM(j.tokens_in + j.tokens_out + j.cache_hit) AS tokens,
           COUNT(*) AS n_jobs
    FROM jobs j
    WHERE j.kind IN ('llm_summary', 'llm_fix')
      AND j.finished_at IS NOT NULL
    GROUP BY j.project_id, month, j.kind

    UNION ALL

    -- I riassunti che nessun job con costo copre: quelli scritti prima che il
    -- costo si registrasse anche sui job, e quelli il cui job è stato registrato
    -- senza costo. Senza questo ramo la vista mostra zero su un'istanza che ha
    -- già speso. Il confronto è riga per riga, non per cella: un riassunto
    -- coperto non deve far scartare il suo vicino non coperto.
    SELECT t.project_id AS project_id,
           strftime('%Y-%m', s.created_at, 'unixepoch', 'localtime') AS month,
           'llm_summary' AS kind,
           SUM(s.cost_usd) AS cost_usd,
           SUM(s.tokens_in + s.tokens_out + s.cache_hit) AS tokens,
           COUNT(*) AS n_jobs
    FROM summaries s
    JOIN transcripts t ON t.id = s.transcript_id
    WHERE NOT EXISTS (
          SELECT 1 FROM jobs j
          WHERE j.kind = 'llm_summary' AND j.transcript_id = s.transcript_id
            AND j.tokens_in + j.tokens_out + j.cache_hit > 0
            AND strftime('%Y-%m', j.finished_at, 'unixepoch', 'localtime')
                = strftime('%Y-%m', s.created_at, 'unixepoch', 'localtime')
      )
    GROUP BY t.project_id, month
)
GROUP BY project_id, month, kind
"""


def llm_cost_cells() -> list[dict]:
    """
    Costo per (corso, mese, tipo di lavoro) e i nomi dei corsi.

    Una cella per combinazione: il filtro per mese e per tipo è una somma su
    queste righe, quindi la UI lo fa senza rifare la query.
    """
    cells = rows(COST_CELLS_SQL)
    names = {p["id"]: p for p in rows("SELECT id, name, code FROM projects")}
    return {
        "cells": cells,
        "projects": sorted(names.values(), key=lambda p: (p["name"] or "").lower()),
    }


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


# I riassunti, per come è fatto il prompt, si intitolano "<materia> — <titolo>".
# In tabella la materia è già l'intestazione della pagina, quindi il prefisso si
# butta e resta il titolo. Il taglio scatta solo con la lineetta circondata da
# spazi: un titolo fuori formato ("Appunti di strategia digitale", oppure
# "Encoder-Decoder" con i trattini dentro le parole) resta intero — meglio un
# nome lungo che un nome tagliato a metà.
_COURSE_PREFIX_RE = re.compile(r"^.+?\s+[\u2013\u2014]\s+(.+)$")


def _display_title(summary_title: str | None, fallback: str) -> str:
    """
    Il nome della lezione da mostrare in tabella: l'ultimo titolo di riassunto
    senza il prefisso della materia, o il titolo della trascrizione (il nome
    del file) quando il riassunto non c'è.
    """
    title = (summary_title or "").strip()
    if not title:
        return fallback
    match = _COURSE_PREFIX_RE.match(title)
    return match.group(1).strip() if match else title


def list_transcripts(project_id: int | None = None, limit: int = 200) -> list[dict]:
    """
    Elenco leggero: senza testo né segmenti (le liste non ne hanno bisogno).

    `n_summaries` serve al dump: una lezione senza riassunto non deve offrire una
    casella che non produrrebbe nessun file. `latest_summary_id` serve alla
    tabella del corso: il bottone scarica l'*ultimo* riassunto quando c'è, e
    NULL dice alla UI che non c'è niente da scaricare e va offerta la generazione.

    Il nome da mostrare arriva già pronto in `display_title`: il titolo
    dell'ultimo riassunto senza il prefisso della materia, o `title` quando il
    riassunto non c'è. Il taglio e il fallback stanno qui perché sono
    verificabili con un test vero, non con una regex in JavaScript.
    """
    columns = ("id, project_id, title, description, language, model, duration, "
               "LENGTH(text) AS length, "
               "(SELECT COUNT(*) FROM summaries s WHERE s.transcript_id = transcripts.id) "
               "AS n_summaries, "
               # `created_at DESC, id DESC`: due riassunti scritti nello stesso
               # secondo devono avere un ordine, e il più recente è l'ultimo inserito.
               "(SELECT s.id FROM summaries s WHERE s.transcript_id = transcripts.id "
               "ORDER BY s.created_at DESC, s.id DESC LIMIT 1) AS latest_summary_id, "
               # Il titolo dell'ultimo riassunto: è il materiale grezzo da cui
               # `_display_title` ricava `display_title`, e viene tolto dal
               # risultato dopo il calcolo.
               "(SELECT s.title FROM summaries s WHERE s.transcript_id = transcripts.id "
               "ORDER BY s.created_at DESC, s.id DESC LIMIT 1) AS summary_title, "
               # Carte e stato dell'ultimo run: la tabella del corso mostra il
               # conteggio e se la generazione è in corso senza una seconda query.
               "(SELECT COUNT(*) FROM cards c WHERE c.transcript_id = transcripts.id) "
               "AS n_cards, "
               "(SELECT COUNT(*) FROM slide_links l "
               "WHERE l.transcript_id = transcripts.id) AS n_slide_links, "
               "(SELECT r.status FROM card_runs r WHERE r.transcript_id = transcripts.id "
               "ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS last_card_run, "
               "created_at, updated_at")
    if project_id is None:
        sql = (f"SELECT {columns} FROM transcripts "
               "ORDER BY updated_at DESC LIMIT ?")
        params: Sequence[Any] = (limit,)
    else:
        sql = (f"SELECT {columns} FROM transcripts "
               "WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?")
        params = (project_id, limit)
    out = rows(sql, params)
    # `summary_title` è solo il valore di passaggio: la lista è "leggera" e
    # porta quello che serve alla UI, cioè il nome già pronto da mostrare.
    for t in out:
        t["display_title"] = _display_title(t.pop("summary_title", None), t["title"])
    return out


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


# ── Riassunti ────────────────────────────────────────────────────────────────
SUMMARY_FIELDS = {"title", "overview", "markdown", "edited"}


def create_summary(transcript_id: int, *, job_id: int | None = None, style: str = "study",
                   language: str | None = None, provider: str = "", model: str = "",
                   title: str = "", overview: str = "", markdown: str = "",
                   n_words: int = 0, target_words: int = 0,
                   source_words: int = 0, tokens_in: int = 0,
                   tokens_out: int = 0, cache_hit: int = 0, cost_usd: float = 0.0,
                   elapsed_s: float = 0.0, truncated: bool = False) -> int:
    ts = now()
    return execute(
        """INSERT INTO summaries(transcript_id, job_id, style, language, provider, model,
                                 title, overview, markdown, n_words, target_words,
                                 source_words, tokens_in, tokens_out, cache_hit, cost_usd,
                                 elapsed_s, edited, truncated, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
        (transcript_id, job_id, style, language, provider, model, title, overview,
         markdown, n_words, target_words, source_words, tokens_in, tokens_out,
         cache_hit, cost_usd, elapsed_s, 1 if truncated else 0, ts, ts),
    )


def get_summary(summary_id: int) -> dict | None:
    return row("SELECT * FROM summaries WHERE id = ?", (summary_id,))


def list_summaries(transcript_id: int, limit: int = 50) -> list[dict]:
    """Elenco per la UI: senza `markdown`, che è il campo grosso."""
    return rows(
        "SELECT id, transcript_id, job_id, style, language, provider, model, title, overview, "
        "n_words, target_words, source_words, tokens_in, tokens_out, cost_usd, "
        "elapsed_s, edited, truncated, created_at, updated_at FROM summaries "
        "WHERE transcript_id = ? ORDER BY created_at DESC LIMIT ?",
        (transcript_id, limit),
    )


def latest_summary(transcript_id: int) -> dict | None:
    return row("SELECT * FROM summaries WHERE transcript_id = ? ORDER BY created_at DESC LIMIT 1",
               (transcript_id,))


def update_summary(summary_id: int, fields: dict) -> None:
    clean = {k: v for k, v in fields.items() if k in SUMMARY_FIELDS}
    if not clean:
        return
    sets = ", ".join(f"{k} = ?" for k in clean)
    execute(f"UPDATE summaries SET {sets}, updated_at = ? WHERE id = ?",
            (*clean.values(), now(), summary_id))


def delete_summary(summary_id: int) -> None:
    execute("DELETE FROM summaries WHERE id = ?", (summary_id,))


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


def usable_providers() -> list[dict]:
    """
    I provider che possono fare una chiamata: abilitati e con una chiave.

    Ollama è l'eccezione: è locale e non ha chiavi. Senza questa eccezione un
    utente che lavora solo in locale non troverebbe mai un provider e la UI gli
    direbbe di configurare una chiave che non gli serve.
    """
    return [p for p in list_providers()
            if p["enabled"] and (p["has_key"] or p["name"] == "ollama")]


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


# ── Slide: documenti e pagine ────────────────────────────────────────────────
SLIDE_DOC_FIELDS = {"n_pages", "status", "error", "boilerplate", "vlm_enabled", "sha256"}
SLIDE_PAGE_FIELDS = {"text", "complex", "vlm_md", "math_uncertain", "n_images",
                     "ocr_chars", "has_text", "render_path", "render_sha"}


def create_slide_doc(project_id: int, name: str, ext: str, sha256: str) -> int:
    return execute(
        "INSERT INTO slide_docs(project_id, name, ext, sha256, status, created_at) "
        "VALUES (?, ?, ?, ?, 'parsing', ?)",
        (project_id, name, ext, sha256, now()),
    )


def get_slide_doc(doc_id: int) -> dict | None:
    return row("SELECT * FROM slide_docs WHERE id = ?", (doc_id,))


def list_slide_docs(project_id: int) -> list[dict]:
    """Elenco per la UI: numero di pagine, stato e quante ne sono assegnate."""
    return rows(
        """SELECT d.*,
                  (SELECT COUNT(*) FROM slide_pages p WHERE p.doc_id = d.id) AS n_parsed,
                  (SELECT COUNT(*) FROM slide_pages p WHERE p.doc_id = d.id AND p.complex = 1)
                    AS n_complex,
                  (SELECT COUNT(*) FROM slide_pages p WHERE p.doc_id = d.id
                     AND p.vlm_md IS NOT NULL) AS n_vlm,
                  (SELECT COUNT(*) FROM slide_links l WHERE l.doc_id = d.id) AS n_links
           FROM slide_docs d WHERE d.project_id = ? ORDER BY d.created_at DESC""",
        (project_id,),
    )


def find_duplicate_slide(project_id: int, sha256: str) -> dict | None:
    return row("SELECT * FROM slide_docs WHERE project_id = ? AND sha256 = ?",
               (project_id, sha256))


def update_slide_doc(doc_id: int, fields: dict) -> None:
    clean = {k: v for k, v in fields.items() if k in SLIDE_DOC_FIELDS}
    if not clean:
        return
    sets = ", ".join(f"{k} = ?" for k in clean)
    execute(f"UPDATE slide_docs SET {sets} WHERE id = ?", (*clean.values(), doc_id))


def delete_slide_doc(doc_id: int) -> dict | None:
    doc = get_slide_doc(doc_id)
    if doc:
        execute("DELETE FROM slide_docs WHERE id = ?", (doc_id,))
    return doc


def save_slide_pages(doc_id: int, pages: list[dict]) -> None:
    """Scrive le pagine di un documento (insert-or-replace: un re-parse le aggiorna)."""
    payload = [
        (doc_id, int(p["page_no"]), p.get("text") or "", 1 if p.get("complex") else 0,
         p.get("vlm_md"), 1 if p.get("math_uncertain") else 0, int(p.get("n_images") or 0),
         int(p.get("ocr_chars") or 0), 1 if p.get("has_text") else 0,
         p.get("render_path"), p.get("render_sha"))
        for p in pages
    ]
    if not payload:
        return
    with connect() as conn:
        conn.executemany(
            """INSERT INTO slide_pages(doc_id, page_no, text, complex, vlm_md,
                                       math_uncertain, n_images, ocr_chars, has_text,
                                       render_path, render_sha)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(doc_id, page_no) DO UPDATE SET
                 text = excluded.text, complex = excluded.complex,
                 math_uncertain = excluded.math_uncertain, n_images = excluded.n_images,
                 ocr_chars = excluded.ocr_chars, has_text = excluded.has_text,
                 render_path = excluded.render_path, render_sha = excluded.render_sha""",
            payload,
        )
        conn.commit()


def update_slide_page(doc_id: int, page_no: int, fields: dict) -> None:
    clean = {k: v for k, v in fields.items() if k in SLIDE_PAGE_FIELDS}
    if not clean:
        return
    sets = ", ".join(f"{k} = ?" for k in clean)
    execute(f"UPDATE slide_pages SET {sets} WHERE doc_id = ? AND page_no = ?",
            (*clean.values(), doc_id, page_no))


def list_slide_pages(doc_id: int) -> list[dict]:
    return rows("SELECT * FROM slide_pages WHERE doc_id = ? ORDER BY page_no", (doc_id,))


def get_slide_page(doc_id: int, page_no: int) -> dict | None:
    return row("SELECT * FROM slide_pages WHERE doc_id = ? AND page_no = ?", (doc_id, page_no))


# ── Slide: assegnazione alle lezioni ─────────────────────────────────────────
def replace_auto_links(doc_id: int, ranges: list[dict]) -> int:
    """
    Sostituisce le proposte automatiche di un documento.

    Le righe `manual` non si toccano: una correzione fatta in UI non deve essere
    cancellata dal run successivo del matching. Per lo stesso motivo il
    `ON CONFLICT` aggiorna solo se la riga esistente è ancora `auto`: senza il
    `WHERE`, un intervallo corretto a mano verrebbe sovrascritto dalla proposta.
    """
    with connect() as conn:
        conn.execute("DELETE FROM slide_links WHERE doc_id = ? AND source = 'auto'", (doc_id,))
        conn.executemany(
            """INSERT INTO slide_links(doc_id, page_from, page_to, transcript_id, score,
                                       source, updated_at)
               VALUES (?, ?, ?, ?, ?, 'auto', ?)
               ON CONFLICT(doc_id, page_from, page_to) DO UPDATE SET
                 transcript_id = excluded.transcript_id, score = excluded.score,
                 updated_at = excluded.updated_at
               WHERE slide_links.source = 'auto'""",
            [(doc_id, int(r["page_from"]), int(r["page_to"]), int(r["transcript_id"]),
              float(r.get("score") or 0.0), now()) for r in ranges],
        )
        conn.commit()
    return len(ranges)


def upsert_link(doc_id: int, page_from: int, page_to: int, transcript_id: int,
                score: float = 0.0, source: str = "manual") -> None:
    execute(
        """INSERT INTO slide_links(doc_id, page_from, page_to, transcript_id, score,
                                   source, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(doc_id, page_from, page_to) DO UPDATE SET
             transcript_id = excluded.transcript_id, score = excluded.score,
             source = excluded.source, updated_at = excluded.updated_at""",
        (doc_id, page_from, page_to, transcript_id, score, source, now()),
    )


def delete_link(link_id: int) -> None:
    execute("DELETE FROM slide_links WHERE id = ?", (link_id,))


def confirm_links(ids: Sequence[int]) -> int:
    """Una proposta confermata diventa `manual`: il matching non la cancella più."""
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with connect() as conn:
        cur = conn.execute(f"UPDATE slide_links SET source = 'manual', updated_at = ? "
                           f"WHERE id IN ({marks})", (now(), *ids))
        conn.commit()
        return cur.rowcount


def list_slide_links(project_id: int | None = None, doc_id: int | None = None,
                     transcript_id: int | None = None) -> list[dict]:
    """Righe pronte per la schermata di conferma e per il contesto delle carte."""
    sql = """SELECT l.*, d.name AS doc_name, d.n_pages, t.title AS transcript_title,
                    (SELECT p.text FROM slide_pages p
                     WHERE p.doc_id = l.doc_id AND p.page_no = l.page_from) AS first_text
             FROM slide_links l
             JOIN slide_docs d ON d.id = l.doc_id
             JOIN transcripts t ON t.id = l.transcript_id"""
    where, params = [], []
    if project_id is not None:
        where.append("d.project_id = ?")
        params.append(project_id)
    if doc_id is not None:
        where.append("l.doc_id = ?")
        params.append(doc_id)
    if transcript_id is not None:
        where.append("l.transcript_id = ?")
        params.append(transcript_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.name, l.page_from"
    return rows(sql, tuple(params))


def linked_pages(transcript_id: int) -> list[dict]:
    """Le pagine assegnate a una lezione, in ordine: è il contesto delle carte."""
    return rows(
        """SELECT d.id AS doc_id, d.name AS doc_name, p.page_no, p.text, p.vlm_md,
                  p.math_uncertain, p.complex, p.render_path, l.score
           FROM slide_links l
           JOIN slide_docs d ON d.id = l.doc_id
           JOIN slide_pages p ON p.doc_id = l.doc_id
                             AND p.page_no BETWEEN l.page_from AND l.page_to
           WHERE l.transcript_id = ?
           ORDER BY d.name, p.page_no""",
        (transcript_id,),
    )


def count_slide_links(transcript_id: int) -> int:
    return int(scalar("SELECT COUNT(*) FROM slide_links WHERE transcript_id = ?",
                      (transcript_id,), default=0) or 0)


# ── Carte ────────────────────────────────────────────────────────────────────
CARD_FIELDS = {"kind", "front", "back", "origin", "edited", "slide_ref", "status", "model"}


def create_cards(transcript_id: int, items: list[dict], run_id: int | None = None) -> list[int]:
    """Inserisce le carte di un run e restituisce gli id."""
    if not items:
        return []
    ts = now()
    ids: list[int] = []
    with connect() as conn:
        for it in items:
            cur = conn.execute(
                """INSERT INTO cards(uid, transcript_id, kind, front, back, origin, edited,
                                     slide_ref, status, model, run_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (it["uid"], transcript_id, it.get("kind") or "qa", it["front"],
                 it.get("back") or "", it.get("origin") or "llm", 1 if it.get("edited") else 0,
                 it.get("slide_ref"), it.get("status") or "active", it.get("model") or "",
                 run_id, ts),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
    return ids


def get_card(card_id: int) -> dict | None:
    return row("SELECT * FROM cards WHERE id = ?", (card_id,))


def list_cards(transcript_id: int, status: str | None = None) -> list[dict]:
    if status:
        return rows("SELECT * FROM cards WHERE transcript_id = ? AND status = ? ORDER BY id",
                    (transcript_id, status))
    return rows("SELECT * FROM cards WHERE transcript_id = ? ORDER BY id", (transcript_id,))


def project_cards(project_id: int, status: str | None = None) -> list[dict]:
    """Tutte le carte di un corso, per l'export di un mazzo unico."""
    sql = """SELECT c.*, t.title AS transcript_title, t.project_id
             FROM cards c JOIN transcripts t ON t.id = c.transcript_id
             WHERE t.project_id = ?"""
    params: list[Any] = [project_id]
    if status:
        sql += " AND c.status = ?"
        params.append(status)
    sql += " ORDER BY t.title, c.id"
    return rows(sql, tuple(params))


def update_card(card_id: int, fields: dict) -> None:
    clean = {k: v for k, v in fields.items() if k in CARD_FIELDS}
    if not clean:
        return
    sets = ", ".join(f"{k} = ?" for k in clean)
    execute(f"UPDATE cards SET {sets} WHERE id = ?", (*clean.values(), card_id))


def delete_card(card_id: int) -> None:
    execute("DELETE FROM cards WHERE id = ?", (card_id,))


def delete_llm_cards(transcript_id: int, keep_edited: bool = True) -> int:
    """Rigenerazione: le carte scritte a mano (`edited=1`) restano."""
    sql = ("DELETE FROM cards WHERE transcript_id = ? AND origin = 'llm'"
           + (" AND edited = 0" if keep_edited else ""))
    with connect() as conn:
        cur = conn.execute(sql, (transcript_id,))
        conn.commit()
        return cur.rowcount


def card_fronts(transcript_id: int) -> list[dict]:
    """
    I `front` già esistenti della lezione: servono al prompt anti-duplicato e al
    dedup testuale. `edited` dice quali sono stati scritti a mano.
    """
    return rows("SELECT id, front, kind, edited FROM cards WHERE transcript_id = ? "
                "ORDER BY id", (transcript_id,))


def count_cards(transcript_id: int) -> dict:
    out = {"total": 0, "active": 0, "suspended": 0}
    for r in rows("SELECT status, COUNT(*) AS n FROM cards WHERE transcript_id = ? "
                  "GROUP BY status", (transcript_id,)):
        out["total"] += int(r["n"])
        if r["status"] in out:
            out[r["status"]] = int(r["n"])
    return out


def create_card_run(transcript_id: int, job_id: int | None = None, **fields) -> int:
    return execute(
        """INSERT INTO card_runs(transcript_id, job_id, inputs_hash, provider, model,
                                 prompt_version, n_new, n_skipped_dup, tokens_in,
                                 tokens_out, cache_hit, cost_usd, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (transcript_id, job_id, fields.get("inputs_hash") or "",
         fields.get("provider") or "", fields.get("model") or "",
         fields.get("prompt_version") or "", int(fields.get("n_new") or 0),
         int(fields.get("n_skipped_dup") or 0), int(fields.get("tokens_in") or 0),
         int(fields.get("tokens_out") or 0), int(fields.get("cache_hit") or 0),
         float(fields.get("cost_usd") or 0.0), fields.get("status") or "ok", now()),
    )


def latest_card_run(transcript_id: int) -> dict | None:
    return row("SELECT * FROM card_runs WHERE transcript_id = ? "
               "ORDER BY created_at DESC, id DESC LIMIT 1", (transcript_id,))


def update_card_run(run_id: int, fields: dict) -> None:
    wanted = {"inputs_hash", "provider", "model", "prompt_version", "n_new",
              "n_skipped_dup", "tokens_in", "tokens_out", "cache_hit", "cost_usd", "status"}
    clean = {k: v for k, v in fields.items() if k in wanted}
    if not clean:
        return
    sets = ", ".join(f"{k} = ?" for k in clean)
    execute(f"UPDATE card_runs SET {sets} WHERE id = ?", (*clean.values(), run_id))


# ── Vettori (embeddings locali) ──────────────────────────────────────────────
def put_vector(kind: str, ref_id: int, model: str, vec: Sequence[float],
               source_sha: str = "") -> None:
    import array

    blob = array.array("f", [float(x) for x in vec]).tobytes()
    execute(
        """INSERT INTO vectors(kind, ref_id, model, dim, source_sha, vec, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(kind, ref_id, model) DO UPDATE SET
             dim = excluded.dim, source_sha = excluded.source_sha, vec = excluded.vec,
             created_at = excluded.created_at""",
        (kind, ref_id, model, len(vec), source_sha, blob, now()),
    )


def get_vector(kind: str, ref_id: int, model: str) -> list[float] | None:
    import array

    found = row("SELECT vec FROM vectors WHERE kind = ? AND ref_id = ? AND model = ?",
                (kind, ref_id, model))
    if not found:
        return None
    arr = array.array("f")
    arr.frombytes(found["vec"])
    return list(arr)


def load_vectors(kind: str, model: str) -> dict[int, list[float]]:
    """Tutti i vettori di un tipo, già decodificati: il matching ne fa un dict."""
    return {ref_id: vec for ref_id, (vec, _sha) in load_vectors_with_source(kind, model).items()}


def load_vectors_with_source(kind: str, model: str) -> dict[int, tuple[list[float], str]]:
    """
    I vettori **con l'impronta del testo** da cui sono nati.

    Serve a capire quando un vettore è vecchio: il matching confronta l'impronta
    del testo attuale con quella salvata e ricalcola solo i vettori cambiati.
    """
    import array

    out: dict[int, tuple[list[float], str]] = {}
    for found in rows("SELECT ref_id, vec, source_sha FROM vectors "
                      "WHERE kind = ? AND model = ?", (kind, model)):
        arr = array.array("f")
        arr.frombytes(found["vec"])
        out[int(found["ref_id"])] = (list(arr), found["source_sha"] or "")
    return out
