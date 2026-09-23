"""
trascrivi/config.py
===================
Percorsi, impostazioni e logging della piattaforma.

Tutto lo stato dell'applicazione vive sotto `data/` (gitignored):
    data/trascrivi.db      database SQLite
    data/sources/<proj>/   copie dei file audio caricate dal browser
    data/transcripts/      trascrizioni strutturate (JSON)
    data/partial/          output parziali dei job interrotti (riprendibili)
    data/secret.key        chiave Fernet per cifrare le API key
    data/settings.json     preferenze modificabili da UI
    data/logs/app.log      log applicativo
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SOURCES = DATA / "sources"
TRANSCRIPTS = DATA / "transcripts"
PARTIAL = DATA / "partial"
LOGS = DATA / "logs"
BACKUPS = DATA / "backup"
WEB = ROOT / "web"
DB_PATH = DATA / "trascrivi.db"
SECRET_KEY_PATH = DATA / "secret.key"
SETTINGS_PATH = DATA / "settings.json"

# Oltre questa dimensione l'upload viene rifiutato (protezione da errori di
# selezione, non un limite tecnico): una lezione da 3 ore sta sotto 1 GB.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
# Sopra questa soglia la UI avvisa che la copia richiederà tempo/spazio.
WARN_UPLOAD_BYTES = 1024 * 1024 * 1024


@dataclass
class Settings:
    """Impostazioni applicative, serializzate in data/settings.json."""

    default_project_id: int | None = None
    default_model: str = "distil-large-v3"
    default_language: str = "en"
    default_device: str = "auto"
    default_compute_type: str = "auto"
    default_provider_id: int | None = None
    flush_every: float = 10.0          # secondi di audio tra due flush su disco
    llm_chunk_chars: int = 4000       # budget caratteri per chunk dell'agente
    delete_sources_after_job: bool = True
    backup_keep: int = 5               # numero di backup da conservare
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        data = self.__dict__.copy()
        return data

    @classmethod
    def from_json(cls, raw: dict) -> "Settings":
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {k: v for k, v in raw.items() if k in known}
        extra = {k: v for k, v in raw.items() if k not in known}
        obj = cls(**kwargs)
        obj.extra = extra
        return obj


def ensure_dirs() -> None:
    for d in (DATA, SOURCES, TRANSCRIPTS, PARTIAL, LOGS, BACKUPS):
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    ensure_dirs()
    if not SETTINGS_PATH.exists():
        s = Settings()
        save_settings(s)
        return s
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return Settings.from_json(raw) if isinstance(raw, dict) else Settings()
    except (OSError, ValueError):
        # File corrotto: meglio ripartire dai default che impedire l'avvio.
        return Settings()


def save_settings(settings: Settings) -> None:
    ensure_dirs()
    payload = settings.to_json()
    payload.update(settings.extra)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


# ── Segreto per la cifratura delle API key ───────────────────────────────────
def load_or_create_secret() -> bytes:
    """
    Chiave Fernet, generata al primo avvio.

    `TRASCRIVI_SECRET` (stringa, ad esempio in .env) ha la precedenza: serve per
    spostare il DB su un'altra macchina senza perdere le chiavi cifrate.
    Altrimenti si usa data/secret.key, che sta fuori dal versionamento.
    """
    from cryptography.fernet import Fernet

    env = os.environ.get("TRASCRIVI_SECRET", "").strip()
    if env:
        try:
            return env.encode("ascii")
        except UnicodeEncodeError:
            return env.encode("utf-8")
    if SECRET_KEY_PATH.exists():
        key = SECRET_KEY_PATH.read_bytes().strip()
        if key:
            return key
    key = Fernet.generate_key()
    SECRET_KEY_PATH.write_bytes(key)
    try:  # permessi ristretti dove il filesystem li supporta (su Windows no-op).
        SECRET_KEY_PATH.chmod(0o600)
    except OSError:
        pass
    return key


def load_env_file(path: Path | None = None) -> None:
    """
    Carica `.env` minimale (KEY=VALUE) nell'ambiente, senza sovrascrivere.

    Zero dipendenze: il formato che serve è una riga per variabile.
    """
    p = path or (ROOT / ".env")
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


# ── Logging ──────────────────────────────────────────────────────────────────
_LOG_CONFIGURED = False


def setup_file_logging(level: int = logging.INFO) -> None:
    """
    Log applicativo su data/logs/app.log (rotazione a 1 MB) + stderr.

    La CLI usa rich; qui non serve, ma il file è indispensabile per capire un
    job fallito dopo che la pagina è stata chiusa.
    """
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    ensure_dirs()
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(LOGS / "app.log", maxBytes=1_000_000, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    # In finestra serve sapere subito che il server è partito.
    if sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    _LOG_CONFIGURED = True


def free_space_bytes(path: Path = DATA) -> int:
    """Spazio libero sul volume che ospita `path` (0 se non determinabile)."""
    try:
        import shutil
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def source_path(project_id: int, name: str) -> Path:
    """Percorso della copia locale di un file audio caricato dal browser."""
    return SOURCES / str(project_id) / name


def transcript_path(transcript_id: int) -> Path:
    return TRANSCRIPTS / f"{transcript_id}.json"


def partial_path(job_id: int) -> Path:
    return PARTIAL / f"{job_id}.txt"
