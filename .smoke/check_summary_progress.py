"""
check_summary_progress.py - Cosa vede l'utente mentre il riassunto si scrive.

Esegue un job di riassunto con un client LLM finto e lento, campiona la riga del
job come farebbe il polling del frontend, e stampa la sequenza di
(progresso, messaggio) che finisce a schermo.

Serve a rispondere a una domanda sola: "si vede subito che e' in corso?".
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = Path(tempfile.mkdtemp(prefix="trascrivi-progress-"))

from trascrivi import config  # noqa: E402

config.DATA = DATA
config.SOURCES = DATA / "sources"
config.TRANSCRIPTS = DATA / "transcripts"
config.PARTIAL = DATA / "partial"
config.LOGS = DATA / "logs"
config.BACKUPS = DATA / "backup"
config.DB_PATH = DATA / "trascrivi.db"
config.SECRET_KEY_PATH = DATA / "secret.key"
config.SETTINGS_PATH = DATA / "settings.json"
config._LOG_CONFIGURED = True

from trascrivi import db, jobs, llm  # noqa: E402

DOCUMENT = """# Reti di sensori

La lezione introduce le reti di sensori e i vincoli energetici.

## Il vincolo energetico

- Al bordo il limite e' la **batteria**
- Nel cloud il limite e' il costo

| Dove | Vincolo |
|---|---|
| Edge | batteria |
| Cloud | costo |

## Lasciato in sospeso

Il calcolo del budget energetico e' rimandato alla prossima lezione.
""" + "\n\n" + ("Testo di riempimento per superare la soglia minima. " * 6)


class _Usage:
    def model_dump(self):
        return {"prompt_tokens": 15000, "completion_tokens": 1200,
                "prompt_cache_hit_tokens": 8000, "prompt_cache_miss_tokens": 7000}


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)
        self.finish_reason = "stop"


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class SlowCompletions:
    """Finge una chiamata LLM lenta: e' il caso in cui serve il segnale di vita."""

    def __init__(self, delay: float, parts: int):
        self.delay = delay
        self.parts = parts
        self.n = 0

    def create(self, **kwargs):
        self.n += 1
        total = self.delay * (self.parts if kwargs["messages"][1]["content"].count("<transcript_part>") else 1)
        step = total / 10
        for _ in range(10):
            time.sleep(step)
        content = "### Parte\n\n- appunto" if "<transcript_part>" in kwargs["messages"][1]["content"] else DOCUMENT
        return _Resp(content)


def main() -> None:
    config.ensure_dirs()
    db.init_db()

    project = db.create_project("IoT", code="IOT")
    segments = [{"start": i * 5.0, "end": i * 5.0 + 5.0,
                 "text": f"Frase numero {i} della lezione sulle reti di sensori"}
                for i in range(60)]
    from trascrivi.textutil import segments_to_text
    tid = db.create_transcript(project, "Lezione 1", segments_to_text(segments), segments,
                               language="it")

    provider_id = db.get_provider_by_name("deepseek")["id"]
    db.update_provider(provider_id, model="deepseek-flash", enabled=True)

    big = len(segments_to_text(segments))
    settings = config.load_settings()
    if "--chunk" in sys.argv:
        settings.summary_chunk_chars = int(sys.argv[sys.argv.index("--chunk") + 1])
        # Si passa dalla stessa via dell'app: l'impostazione salvata, non un
        # parametro speciale che esisterebbe solo per questo script.
        config.save_settings(settings)
    from trascrivi import summary as sm
    strategy = sm.choose_strategy(big, settings.summary_chunk_chars)

    calls = SlowCompletions(delay=1.2, parts=4)
    llm._client = lambda provider: type("C", (), {
        "chat": type("Ch", (), {"completions": calls})(),
    })()

    job_id = db.create_job("llm_summary", {"transcript_id": tid, "provider_id": provider_id},
                           project_id=project, transcript_id=tid)

    seen: list[tuple[int, float, str]] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            row = db.get_job(job_id) or {}
            snap = (round(float(row.get("progress") or 0) * 100), row.get("message") or "")
            if not seen or seen[-1][1:] != snap:
                seen.append((round(time.time() - t0, 1), *snap))
            time.sleep(0.2)

    print(f"trascrizione: {big} caratteri -> strategia '{strategy}'\n")
    t0 = time.time()
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        result = jobs.run_summary(job_id, {"transcript_id": tid, "provider_id": provider_id})
    finally:
        stop.set()
        sampler.join(timeout=2)

    print(f"{'t(s)':>6} {'%':>4}  messaggio")
    for when, pct, msg in seen:
        print(f"{when:>6} {pct:>4}  {msg}")
    print()
    print("risultato:", json.dumps(result, indent=2)[:400])


if __name__ == "__main__":
    main()
