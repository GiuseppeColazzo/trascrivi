"""E2E locale con Ollama: slide vere -> parse/VLM -> matching -> carte -> .apkg.

Costo zero e nessuna chiave API: tutto quello che chiama un modello passa da
Ollama sulla macchina. Non è un test automatico (la suite resta offline): è lo
smoke manuale che il piano chiede allo Step 8, e si lancia a mano.

    .venv\\Scripts\\python.exe .smoke\\e2e_slides.py

Stampa un riepilogo per ogni passo e, se qualcosa non torna, alza.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODEL = "qwen3.5:4b"          # testo: il più piccolo già scaricato
VLM_MODEL = "gemma3:4b"       # vision
EMBED_MODEL = "nomic-embed-text:v1.5"


def _remap(data_dir: Path) -> None:
    from trascrivi import config

    for name, sub in (("DATA", ""), ("SOURCES", "sources"), ("TRANSCRIPTS", "transcripts"),
                      ("PARTIAL", "partial"), ("LOGS", "logs"), ("BACKUPS", "backup"),
                      ("SLIDES", "slides")):
        setattr(config, name, data_dir / sub if sub else data_dir)
    config.DB_PATH = data_dir / "trascrivi.db"
    config.SECRET_KEY_PATH = data_dir / "secret.key"
    config.SETTINGS_PATH = data_dir / "settings.json"
    # Qui il log su file serve: è uno smoke manuale, e le righe INFO (una cloze
    # scartata, un vettore ricalcolato) sono l'unico modo di capire cosa è andato.
    config._LOG_CONFIGURED = False
    config.ensure_dirs()


def wait_job(client, job_id: int, label: str, timeout: float = 600.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        line = f"{label}: {job['status']} {round((job.get('progress') or 0) * 100)}% {job.get('message') or ''}"
        if line != last:
            print("  ", line)
            last = line
        if job["status"] not in ("queued", "running"):
            assert job["status"] == "done", f"{label} è finito in stato {job['status']}: {job.get('error')}"
            return job
        time.sleep(1.0)
    raise AssertionError(f"{label}: timeout dopo {timeout}s")


def main() -> None:
    # La cartella temporanea sta dentro `.tmp/` (gitignored): su questa macchina
    # `tempfile` la crea nel repository, e i dati di prova non devono restare lì.
    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="trascrivi-e2e-", dir=scratch))
    _remap(data_dir)

    from fastapi.testclient import TestClient

    from trascrivi import config, db
    from trascrivi.api import create_app

    config.setup_file_logging()

    with TestClient(create_app()) as client:
        # 1. Provider locale: nessuna chiave, modello già scaricato.
        provider_id = db.get_provider_by_name("ollama")["id"]
        client.patch(f"/api/providers/{provider_id}", json={"model": MODEL, "enabled": True})
        client.put("/api/settings", json={"slides_vlm": "auto", "slides_vlm_model": VLM_MODEL,
                                          "embed_model": EMBED_MODEL, "cards_max": 6,
                                          "summary_max_tokens": 20000})
        print("1. provider ollama:", MODEL, "· vlm:", VLM_MODEL)

        # 2. Corso, lezione e "trascrizione" (il testo viene dalle slide, serve
        #    solo a dare al riassunto qualcosa su cui lavorare).
        project_id = db.create_project("ECAI — E2E", code="ECAI")
        transcript_id = db.create_transcript(
            project_id, "Lezione E2E",
            "Depthwise separable convolutions split a standard convolution into a depthwise "
            "stage and a pointwise stage. The width multiplier alpha scales the number of "
            "channels of every layer of MobileNetV1. The cost-aware DNAS regularizer sums the "
            "expected number of weights of every layer.",
            [{"start": 0.0, "end": 30.0,
              "text": "Depthwise separable convolutions split a standard convolution into a "
                      "depthwise stage and a pointwise stage. The width multiplier alpha scales "
                      "the number of channels of every layer of MobileNetV1. The cost-aware "
                      "DNAS regularizer sums the expected number of weights of every layer."}],
            language="en")
        print("2. lezione", transcript_id)

        # 3. Upload delle tre slide reali.
        for name in ("image1.png", "image2.png", "image3.png"):
            path = ROOT / "slide_samples" / name
            with path.open("rb") as fh:
                res = client.post(f"/api/projects/{project_id}/slides",
                                  files={"file": (name, fh, "image/png")})
            assert res.status_code == 201, res.text
            job_id = res.json()["job_id"]
            pages_job = wait_job(client, job_id, f"3. parse {name}")
            print("   ", pages_job.get("message"))

        docs = client.get(f"/api/projects/{project_id}/slides").json()["slides"]
        assert len(docs) == 3, docs
        for doc in docs:
            detail = client.get(f"/api/slides/{doc['id']}").json()
            page = detail["pages"][0]
            assert page["text"], f"{doc['name']}: nessun testo"
            assert page["vlm_md"], f"{doc['name']}: la VLM non ha reso la pagina"
            print(f"    {doc['name']}: {len(page['text'])} char OCR, "
                  f"{len(page['vlm_md'])} char VLM, math_uncertain={page['math_uncertain']}")

        # 4. Riassunto (serve come controparte per il matching).
        res = client.post(f"/api/transcripts/{transcript_id}/summary",
                          json={"provider_id": provider_id})
        wait_job(client, res.json()["job"]["id"], "4. riassunto")
        summary = client.get(f"/api/transcripts/{transcript_id}/summaries").json()["summaries"][0]
        print("    riassunto:", summary["n_words"], "parole")

        # 5. Matching pagine -> lezione, e conferma di tutte le proposte.
        res = client.post(f"/api/projects/{project_id}/slides/match")
        wait_job(client, res.json()["job"]["id"], "5. matching")
        links = client.get(f"/api/projects/{project_id}/slide-links").json()["links"]
        print("    intervalli proposti:", [(l["page_from"], l["page_to"], round(l["score"], 2))
                                           for l in links])
        assert links, "il matching non ha proposto niente: l'endpoint è verificato a mano"
        for doc_id in {l["doc_id"] for l in links}:
            client.post(f"/api/slides/{doc_id}/links/accept", json={})

        # 6. Preventivo e generazione delle carte (l'unica chiamata "di contenuto").
        est = client.get(f"/api/transcripts/{transcript_id}/cards/estimate").json()
        print("6. stima:", est["tokens_in"], "token,", est["slides"], "slide, cached:",
              est["cached"])
        res = client.post(f"/api/transcripts/{transcript_id}/cards",
                          json={"provider_id": provider_id})
        wait_job(client, res.json()["job"]["id"], "   carte")
        cards = client.get(f"/api/transcripts/{transcript_id}/cards").json()
        print("    carte:", len(cards["cards"]), "· run:", cards["run"]["n_new"],
              "· costo:", cards["run"]["cost_usd"])
        for card in cards["cards"]:
            print(f"    [{card['kind']}] {card['front'][:90]}")
        assert cards["cards"], "nessuna carta generata"

        # 7. Export: due volte, stessi GUID (re-import = aggiornamento). I byte
        #    differiscono per il timestamp della collezione, che è normale: Anki
        #    riconosce le note dal GUID.
        first = client.get(f"/api/transcripts/{transcript_id}/deck.apkg")
        second = client.get(f"/api/transcripts/{transcript_id}/deck.apkg")
        assert first.status_code == 200 and first.content[:2] == b"PK"
        import io
        import sqlite3
        import zipfile

        def guids_of(data: bytes) -> list[str]:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                collection = next(n for n in zf.namelist() if n.startswith("collection"))
                blob = zf.read(collection)
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
                fh.write(blob)
                path = Path(fh.name)
            conn = sqlite3.connect(str(path))
            try:
                return [row[0] for row in conn.execute("SELECT guid FROM notes")]
            finally:
                conn.close()
                path.unlink(missing_ok=True)

        guids = guids_of(first.content)
        assert guids == guids_of(second.content), "i GUID cambiano fra due export"
        print("7. apkg:", len(first.content), "byte,", len(guids), "note, GUID stabili")
        assert len(guids) == len(cards["cards"])

        tsv = client.get(f"/api/transcripts/{transcript_id}/deck.tsv")
        print("   tsv:", len(tsv.text.splitlines()), "righe")
        print("\nE2E completato. Dati in", data_dir)


if __name__ == "__main__":
    main()
