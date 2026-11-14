"""Verifica manuale: slide caricate in un corso che non ha ancora lezioni.

Embeddings finti e deterministici (Ollama può essere spento): quello che si
verifica è il *flusso* — parsing, zero proposte, poi le proposte che compaiono
quando la lezione esiste — non la qualità della similarità.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

scratch = ROOT / ".tmp"
scratch.mkdir(exist_ok=True)
data_dir = Path(tempfile.mkdtemp(prefix="check-nolec-", dir=scratch))

from trascrivi import config  # noqa: E402

for name, sub in (("DATA", ""), ("SOURCES", "sources"), ("TRANSCRIPTS", "transcripts"),
                  ("PARTIAL", "partial"), ("LOGS", "logs"), ("BACKUPS", "backup"),
                  ("SLIDES", "slides")):
    setattr(config, name, data_dir / sub if sub else data_dir)
config.DB_PATH = data_dir / "trascrivi.db"
config.SECRET_KEY_PATH = data_dir / "secret.key"
config.SETTINGS_PATH = data_dir / "settings.json"
config._LOG_CONFIGURED = True
config.ensure_dirs()

from trascrivi import db, llm  # noqa: E402


def _fake_embed(provider, texts, model):
    """Due dimensioni, una per argomento: zero rete, risultato prevedibile."""
    out = []
    for text in texts:
        low = text.lower()
        out.append([1.0, 0.0] if ("convolution" in low or "multiplier" in low)
                   else [0.0, 1.0])
    return out


llm.embed_texts = _fake_embed


def wait(client, job_id: int, label: str, timeout: float = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            print(f"  {label}: {job['status']} — {job.get('message')}")
            return job
        time.sleep(0.5)
    raise SystemExit(f"{label}: timeout")


from fastapi.testclient import TestClient  # noqa: E402

from trascrivi import jobs as jobs_mod  # noqa: E402
from trascrivi.api import create_app  # noqa: E402

with TestClient(create_app()) as client:
    project = db.create_project("Corso senza lezioni", code="VUOTO")
    print("1. corso", project, "— lezioni:", len(db.list_transcripts(project)))

    for name in ("image1.png", "image2.png", "image3.png"):
        with (ROOT / "slide_samples" / name).open("rb") as fh:
            res = client.post(f"/api/projects/{project}/slides",
                              files={"file": (name, fh, "image/png")})
        wait(client, res.json()["job_id"], f"2. parse {name}")

    docs = client.get(f"/api/projects/{project}/slides").json()["slides"]
    print("   deck:", [(d["name"], d["status"], d["n_parsed"], d["n_vlm"]) for d in docs])

    embed = [j for j in client.get("/api/jobs").json()["jobs"] if j["kind"] == "embed_match"]
    print("3. job embed_match partiti da soli:",
          [(j["status"], j.get("message")) for j in embed])
    print("   intervalli proposti:",
          client.get(f"/api/projects/{project}/slide-links").json()["links"])

    detail = client.get(f"/api/slides/{docs[0]['id']}").json()
    print("4. testo leggibile anche senza lezioni:",
          len(detail["pages"][0]["text"]), "char OCR,",
          len(detail["pages"][0]["vlm_md"] or ""), "char VLM")

    # ...ora la lezione esiste, come se fosse appena stata trascritta.
    transcript_id = db.create_transcript(
        project, "Lezione 1",
        "width multiplier alpha and depthwise separable convolution layers",
        [{"start": 0.0, "end": 10.0,
          "text": "width multiplier alpha and depthwise separable convolution layers"}],
        language="en")
    new_job = jobs_mod._queue_matching(project)
    wait(client, new_job, "5. embed_match dopo la lezione")
    links = client.get(f"/api/projects/{project}/slide-links").json()["links"]
    print("   intervalli:", [(l["doc_name"], l["page_from"], l["page_to"],
                              l["transcript_title"], round(l["score"], 2)) for l in links])

    est = client.get(f"/api/transcripts/{transcript_id}/cards/estimate").json()
    print("6. stima carte:", est["slides"], "slide ·", est["tokens_in"], "token · cached:",
          est["cached"])
