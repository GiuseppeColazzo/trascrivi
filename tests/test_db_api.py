"""
Persistenza (`trascrivi.db`) e API HTTP (`trascrivi.api`).

Gli endpoint sono esercitati con `TestClient` come context manager (l'evento
`startup` avvia il worker dei job) e solo su percorsi che **non** eseguono una
inferenza: nessun test carica un modello Whisper, tocca la GPU o la rete.
"""

from __future__ import annotations

import json

from trascrivi import config, db
from trascrivi.textutil import segments_to_text

TWO_SEGMENTS = [
    {"start": 0.0, "end": 5.0, "text": "we use pie torch"},
    {"start": 5.0, "end": 10.0, "text": "the lecture starts"},
]


def make_source(project_id, tmp_path, name="lesson.mp3"):
    return db.create_source(project_id, str(tmp_path / name), name, None, 1234, 42.0)


# ════════════════════════════════════════════════════════════════════════════
#  DB
# ════════════════════════════════════════════════════════════════════════════
def test_project_crud():
    pid = db.create_project("  Analisi 1  ", code=" AN1 ")

    project = db.get_project(pid)
    assert project["name"] == "Analisi 1"
    assert project["code"] == "AN1"
    assert project["archived"] == 0

    assert db.update_project(pid, {"name": "Analisi 2", "ignored": "x"}) is True
    assert db.get_project(pid)["name"] == "Analisi 2"
    assert db.update_project(pid, {"ignored": "x"}) is False

    assert [p["id"] for p in db.list_projects()] == [pid]
    assert db.get_project(9999) is None
    assert db.delete_project(pid) == {"sources": [], "transcripts": []}
    assert db.get_project(pid) is None


def test_list_projects_counts_children(project, tmp_path, make_transcript):
    make_source(project, tmp_path)
    make_transcript()

    row = db.list_projects()[0]

    assert row["n_transcripts"] == 1
    assert row["n_sources"] == 1


def test_delete_project_returns_children_to_clean(project, tmp_path, make_transcript):
    stored = tmp_path / "stored.mp3"
    stored.write_bytes(b"audio")
    sid = db.create_source(project, str(tmp_path / "orig.mp3"), "orig.mp3", str(stored), 5, 1.0)
    tid = make_transcript()

    payload = db.delete_project(project)

    assert payload["transcripts"] == [tid]
    assert payload["sources"] == [str(stored)]
    assert db.get_source(sid) is None       # ON DELETE CASCADE
    assert db.get_transcript(tid) is None
    assert db.get_project(project) is None


def test_transcript_crud_and_derived_segments(project, make_transcript):
    tid = make_transcript()
    transcript = db.get_transcript(tid)

    assert [s["text"] for s in transcript["segments"]] == [
        "Good morning everyone",
        "Today we talk about PyTorch",
        "Please open your notebooks",
    ]
    assert transcript["text"].startswith("Good morning everyone")
    listed = db.list_transcripts(project)
    assert [t["id"] for t in listed] == [tid]
    assert listed[0]["length"] == len(transcript["text"])

    # `create_transcript` deriva i segmenti dal testo quando non gliene passiamo
    derived = db.create_transcript(project, "Derivata", "riga uno\nriga due", [])
    assert [s["text"] for s in db.get_transcript(derived)["segments"]] == ["riga uno", "riga due"]

    db.update_transcript(derived, {"text": "solo una riga",
                                   "segments": [{"start": 0.0, "end": 3.0, "text": "solo una riga"}]})
    assert db.get_transcript(derived)["text"] == "solo una riga"
    assert db.get_transcript(derived)["segments"][0]["text"] == "solo una riga"

    db.delete_transcript(derived)
    assert db.get_transcript(derived) is None


def test_fts_stays_in_sync_on_insert_update_delete(project):
    tid = db.create_transcript(project, "Lezione FTS", "parliamo di PyTorch oggi", [])

    hits = db.search_transcripts("PyTorch")
    assert [h["id"] for h in hits] == [tid]
    assert "[[PyTorch]]" in hits[0]["snippet"]
    assert "]]" in hits[0]["snippet"]
    assert hits[0]["project_name"] == db.get_project(project)["name"]
    assert db.search_transcripts("Tensorflow") == []

    db.update_transcript(tid, {"text": "parliamo di Tensorflow oggi"})
    assert db.search_transcripts("PyTorch") == []
    assert [h["id"] for h in db.search_transcripts("Tensorflow")] == [tid]

    db.delete_transcript(tid)
    assert db.search_transcripts("Tensorflow") == []


def test_search_transcripts_filters_by_project(project):
    other = db.create_project("Altro corso")
    db.create_transcript(project, "A", "argomento condiviso", [])
    db.create_transcript(other, "B", "argomento condiviso", [])

    assert len(db.search_transcripts("condiviso")) == 2
    filtered = db.search_transcripts("condiviso", project_id=project)
    assert [h["project_id"] for h in filtered] == [project]


def test_proposals_lifecycle(make_transcript):
    tid = make_transcript()

    assert db.create_proposals(tid, None, [
        {"find": "pie torch", "replace": "PyTorch", "kind": "term", "confidence": 0.9}
    ]) == 1
    assert db.create_proposals(tid, None, []) == 0

    props = db.list_proposals(tid)
    assert len(props) == 1
    assert props[0]["status"] == "pending"
    assert props[0]["flag"] == "ok"
    assert props[0]["confidence"] == 0.9
    assert db.list_proposals(tid, "accepted") == []

    prop_id = props[0]["id"]
    assert [p["id"] for p in db.get_proposals([prop_id])] == [prop_id]
    assert db.get_proposals([]) == []

    assert db.set_proposal_status([prop_id], "accepted") == 1
    accepted = db.list_proposals(tid, "accepted")
    assert accepted[0]["applied_at"] is not None
    assert db.set_proposal_status([], "rejected") == 0

    assert db.clear_pending_proposals(tid) == 0
    assert db.create_proposals(tid, None, [{"find": "lecture", "replace": "lesson"}]) == 1
    assert db.clear_pending_proposals(tid) == 1
    assert db.list_proposals(tid, "pending") == []

    db.add_revision(tid, "agent_fix", 3, "three replacements")
    revisions = db.list_revisions(tid)
    assert revisions[0]["kind"] == "agent_fix"
    assert revisions[0]["n_changes"] == 3
    assert revisions[0]["summary"] == "three replacements"


def test_upsert_term_updates_instead_of_duplicating(project):
    db.upsert_term(project, "pie torch", "PyTorch")
    db.upsert_term(project, "pie torch", "PyTorch 2", auto=True, note="nota aggiornata")

    terms = db.list_terms(project)
    assert len(terms) == 1
    assert terms[0]["replace"] == "PyTorch 2"
    assert terms[0]["auto"] == 1
    assert terms[0]["note"] == "nota aggiornata"

    db.delete_term(terms[0]["id"])
    assert db.list_terms(project) == []


def test_job_lifecycle_and_mark_interrupted(project):
    first = db.create_job("transcribe", {"a": 1}, project_id=project)
    second = db.create_job("llm_fix", {"b": 2}, project_id=project)

    assert db.get_job(first)["status"] == "queued"
    assert db.get_job(first)["payload"] == '{"a": 1}'
    assert db.next_queued_job()["id"] == first

    db.update_job(second, status="running")
    assert db.next_queued_job()["id"] == first
    assert [j["id"] for j in db.list_jobs(statuses=["running"])] == [second]

    db.update_job(first, status="done", progress=1.0)
    assert db.next_queued_job() is None
    assert len(db.list_jobs()) == 2

    assert db.mark_interrupted_jobs() == 1
    interrupted = db.get_job(second)
    assert interrupted["status"] == "interrupted"
    assert interrupted["message"] == "Server stopped while this job was running"
    assert interrupted["finished_at"] is not None
    assert db.mark_interrupted_jobs() == 0


def test_backup_creates_a_file_in_the_backup_dir(project):
    db.create_project("Da salvare")
    target = db.backup()

    assert target.exists()
    assert target.parent == config.BACKUPS
    assert target.name.startswith("trascrivi-")
    assert target.suffix == ".db"
    assert [p.name for p in config.BACKUPS.glob("trascrivi-*.db")] == [target.name]


def test_list_providers_never_exposes_key_material():
    providers = db.list_providers()

    assert [p["name"] for p in providers] == ["ollama", "deepseek", "openrouter"]
    assert all(p["has_key"] is False for p in providers)
    assert all("api_key_enc" not in p for p in providers)
    assert all(p["extra"] == {} for p in providers)

    deepseek = providers[1]
    db.update_provider(deepseek["id"], api_key_enc="encrypted-blob", enabled=True)

    updated = {p["name"]: p for p in db.list_providers()}["deepseek"]
    assert updated["has_key"] is True
    assert updated["enabled"] == 1
    assert "api_key_enc" not in updated


def test_update_provider_can_clear_the_key_at_db_level():
    provider = db.list_providers()[0]
    db.update_provider(provider["id"], api_key_enc="encrypted-blob")
    assert db.list_providers()[0]["has_key"] is True

    db.update_provider(provider["id"], api_key_enc="")

    assert db.list_providers()[0]["has_key"] is False


def test_settings_roundtrip():
    assert db.get_setting("assente") is None
    assert db.get_setting("assente", 7) == 7

    db.set_setting("last_project", {"id": 3})
    assert db.get_setting("last_project") == {"id": 3}

    db.set_setting("last_project", 4)
    assert db.get_setting("last_project") == 4


# ════════════════════════════════════════════════════════════════════════════
#  API
# ════════════════════════════════════════════════════════════════════════════
def test_health_endpoint(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["version"] == "1.0.0"
    assert isinstance(body["hardware"]["cpu_count"], int)
    assert body["hardware"]["cached_models"] == []          # nessun modello caricato
    assert [p["name"] for p in body["providers"]] == ["ollama", "deepseek", "openrouter"]
    assert body["has_llm_provider"] is True                 # ollama è abilitato per default
    assert body["queue"] == 0
    assert body["defaults"] == {"model": "distil-large-v3", "language": "en"}


def test_api_project_crud(client):
    created = client.post("/api/projects", json={"name": "Analisi 1", "code": "AN1"})
    assert created.status_code == 201
    pid = created.json()["id"]
    assert created.json()["name"] == "Analisi 1"

    assert client.post("/api/projects", json={"name": ""}).status_code == 422

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()["projects"]] == [pid]

    assert client.get(f"/api/projects/{pid}").status_code == 200
    assert client.get("/api/projects/9999").status_code == 404

    patched = client.patch(f"/api/projects/{pid}", json={"name": "Analisi 2", "archived": True})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Analisi 2"
    assert patched.json()["archived"] == 1
    assert client.patch("/api/projects/9999", json={"name": "x"}).status_code == 404

    deleted = client.delete(f"/api/projects/{pid}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted_sources": 0, "deleted_transcripts": 0}
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_api_delete_project_removes_its_transcripts(client, project, make_transcript):
    tid = make_transcript()

    deleted = client.delete(f"/api/projects/{project}")

    assert deleted.json() == {"deleted_sources": 0, "deleted_transcripts": 1}
    assert db.get_transcript(tid) is None
    assert client.get("/api/transcripts").json()["transcripts"] == []


def test_source_endpoint_rejects_invalid_paths(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "Analisi 1"}).json()["id"]
    url = f"/api/projects/{pid}/sources"

    assert client.post(url, json={"path": ""}).status_code == 400
    assert client.post(url, json={"path": "relative/lesson.mp3"}).status_code == 400
    # radice di volume e cartelle di sistema non sono sorgenti audio valide
    assert client.post(url, json={"path": "C:/"}).status_code == 400
    assert client.post(url, json={"path": "C:/Windows/notepad.exe"}).status_code == 400
    # una cartella non è un file audio
    assert client.post(url, json={"path": str(tmp_path)}).status_code == 400
    # path assoluto inesistente → 404
    assert client.post(url, json={"path": str(tmp_path / "assente.mp3")}).status_code == 404
    assert client.post(url, json={"path": "D:/non-esiste/lesson.mp3"}).status_code == 404
    # progetto inesistente → 404
    assert client.post("/api/projects/9999/sources",
                       json={"path": "D:/non-esiste/lesson.mp3"}).status_code == 404
    assert db.list_sources(pid) == []


def test_source_endpoint_rejects_a_file_without_audio_track(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "Analisi 1"}).json()["id"]
    fake = tmp_path / "finto.mp3"
    fake.write_bytes(b"non e' audio, solo un file con l'estensione giusta")

    response = client.post(f"/api/projects/{pid}/sources", json={"path": str(fake)})

    assert response.status_code == 422
    assert "No audio track" in response.json()["detail"]
    assert db.list_sources(pid) == []


def test_create_job_validates_model_and_language(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "Analisi 1"}).json()["id"]
    sid = make_source(pid, tmp_path)
    url = "/api/jobs"

    unknown = client.post(url, json={"project_id": pid, "source_id": sid,
                                     "model": "modello-inventato", "language": "en"})
    assert unknown.status_code == 422
    assert "Unsupported model" in unknown.json()["detail"]
    assert db.list_jobs() == []

    english_only = client.post(url, json={"project_id": pid, "source_id": sid,
                                          "model": "distil-large-v3", "language": "it"})
    assert english_only.status_code == 422
    assert "only works in English" in english_only.json()["detail"]
    assert db.list_jobs() == []

    assert client.post(url, json={"project_id": 9999, "source_id": sid,
                                  "model": "large-v3"}).status_code == 404
    assert client.post(url, json={"project_id": pid, "source_id": 9999,
                                  "model": "large-v3"}).status_code == 404
    # nessun job è stato accodato: il worker non eseguirà mai una inferenza
    assert db.list_jobs() == []


def test_api_transcript_list_and_get(client, make_transcript):
    tid = make_transcript()

    listed = client.get("/api/transcripts", params={"project_id": db.list_projects()[0]["id"]})
    assert listed.status_code == 200
    assert [t["id"] for t in listed.json()["transcripts"]] == [tid]

    response = client.get(f"/api/transcripts/{tid}")
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Lezione 1"
    assert [s["text"] for s in body["segments"]] == [
        "Good morning everyone",
        "Today we talk about PyTorch",
        "Please open your notebooks",
    ]
    assert body["n_proposals_pending"] == 0
    assert body["revisions"] == []
    assert body["project_name"] == "Analisi 1"

    assert client.get("/api/transcripts/9999").status_code == 404


def test_api_patch_transcript_rebuilds_segments(client, make_transcript):
    tid = make_transcript()

    response = client.patch(f"/api/transcripts/{tid}", json={"text": "una riga\nun'altra riga"})

    assert response.status_code == 200
    body = response.json()
    assert [s["text"] for s in body["segments"]] == ["una riga", "un'altra riga"]
    # i timestamp dei segmenti precedenti vengono riusati quando il conteggio combacia
    assert [s["start"] for s in body["segments"]] == [0.0, 5.0]
    assert all("end" in s and "offset" in s for s in body["segments"])

    revisions = client.get(f"/api/transcripts/{tid}").json()["revisions"]
    assert revisions and revisions[0]["kind"] == "manual_edit"


def test_api_patch_transcript_updates_the_text_column(client, make_transcript):
    """
    BUG (non corretto da questa suite): `patch_transcript` fa
    `new_text = fields.pop("text")` (api.py:504) e poi aggiorna solo i segmenti,
    quindi la colonna `text` resta vecchia: GET restituisce il testo vecchio con
    i segmenti nuovi e l'indice FTS continua a indicizzare il testo vecchio.
    """
    tid = make_transcript()

    response = client.patch(f"/api/transcripts/{tid}", json={"text": "una riga\nun'altra riga"})

    assert response.status_code == 200
    assert response.json()["text"] == "una riga\nun'altra riga"
    assert db.get_transcript(tid)["text"] == "una riga\nun'altra riga"
    assert [h["id"] for h in db.search_transcripts("riga")] == [tid]


def test_api_patch_transcript_with_segments_rebuilds_text(client, make_transcript):
    tid = make_transcript()
    segments = [{"start": 0.0, "end": 2.0, "text": "alpha"},
                {"start": 2.0, "end": 4.0, "text": "beta"}]

    response = client.patch(f"/api/transcripts/{tid}", json={"segments": segments})

    assert response.status_code == 200
    assert response.json()["text"] == "alpha\nbeta"
    assert [s["text"] for s in response.json()["segments"]] == ["alpha", "beta"]


def test_api_patch_transcript_validates_target_project(client, make_transcript):
    tid = make_transcript()

    assert client.patch(f"/api/transcripts/{tid}",
                        json={"project_id": 9999}).status_code == 422
    assert client.patch("/api/transcripts/9999", json={"title": "x"}).status_code == 404


def test_api_delete_transcript(client, make_transcript):
    tid = make_transcript()

    deleted = client.delete(f"/api/transcripts/{tid}")

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": tid}
    assert client.get(f"/api/transcripts/{tid}").status_code == 404
    assert client.delete("/api/transcripts/9999").status_code == 404


def test_api_export_transcript_formats(client, make_transcript):
    tid = make_transcript()
    text = client.get(f"/api/transcripts/{tid}").json()["text"]

    txt = client.get(f"/api/transcripts/{tid}/export", params={"format": "txt"})
    assert txt.status_code == 200
    assert txt.headers["content-type"] == "text/plain; charset=utf-8"
    assert txt.text == text + "\n"
    assert 'filename="Lezione-1.txt"' in txt.headers["content-disposition"]

    md = client.get(f"/api/transcripts/{tid}/export", params={"format": "md"})
    assert md.status_code == 200
    assert md.headers["content-type"] == "text/markdown; charset=utf-8"
    assert md.text.startswith("# Lezione 1")
    assert "## 00:00" in md.text
    assert "Good morning everyone" in md.text
    assert 'filename="Lezione-1.md"' in md.headers["content-disposition"]

    as_json = client.get(f"/api/transcripts/{tid}/export", params={"format": "json"})
    assert as_json.status_code == 200
    assert as_json.headers["content-type"] == "application/json; charset=utf-8"
    payload = json.loads(as_json.text)
    assert payload["title"] == "Lezione 1"
    assert payload["text"] == text
    assert [s["text"] for s in payload["segments"]] == text.splitlines()

    segments = client.get(f"/api/transcripts/{tid}/export", params={"format": "segments"})
    assert segments.status_code == 200
    assert segments.headers["content-type"] == "text/plain; charset=utf-8"
    assert segments.text == (
        "[00:00] Good morning everyone\n"
        "[00:05] Today we talk about PyTorch\n"
        "[00:10] Please open your notebooks\n"
    )

    assert client.get(f"/api/transcripts/{tid}/export", params={"format": "xml"}).status_code == 400
    assert client.get("/api/transcripts/9999/export").status_code == 404


def test_api_search_endpoint(client):
    pid = client.post("/api/projects", json={"name": "Analisi 1"}).json()["id"]
    tid = db.create_transcript(pid, "Lezione Gauss", "la formula di Gauss e la Gaussiana", [])

    response = client.get("/api/search", params={"q": "Gaussiana"})

    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["id"] for r in results] == [tid]
    assert "[[Gaussiana]]" in results[0]["snippet"]
    assert results[0]["project_name"] == "Analisi 1"
    assert client.get("/api/search", params={"q": "inesistente"}).json()["results"] == []
    assert client.get("/api/search").status_code == 422          # q obbligatoria


def test_api_proposals_preview_then_accept(client, make_transcript):
    tid = make_transcript(segments=TWO_SEGMENTS)
    db.create_proposals(tid, None, [
        {"find": "pie torch", "replace": "PyTorch", "kind": "term", "confidence": 0.9},
        {"find": "lecture", "replace": "lesson", "kind": "asr_error"},
    ])
    ids = [p["id"] for p in client.get(f"/api/transcripts/{tid}/proposals").json()["proposals"]]
    assert len(ids) == 2

    preview = client.post(f"/api/transcripts/{tid}/proposals/preview", json={"ids": ids})
    assert preview.status_code == 200
    preview_body = preview.json()
    assert preview_body["applied"] == 2
    kinds = {d["kind"] for d in preview_body["diff"]}
    assert "-" in kinds and "+" in kinds
    # l'anteprima non scrive nulla
    assert client.get(f"/api/transcripts/{tid}").json()["text"] == segments_to_text(TWO_SEGMENTS)

    accepted = client.post(f"/api/transcripts/{tid}/proposals/accept", json={"ids": ids})
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["applied"] == 2
    assert body["changed_segments"] == 2
    assert body["text"] == "we use PyTorch\nthe lesson starts"

    stored = client.get(f"/api/transcripts/{tid}").json()
    assert [s["text"] for s in stored["segments"]] == ["we use PyTorch", "the lesson starts"]
    assert stored["text"] == "we use PyTorch\nthe lesson starts"
    assert stored["revisions"][0]["kind"] == "agent_fix"
    assert stored["revisions"][0]["n_changes"] == 2
    assert all(p["status"] == "accepted"
               for p in client.get(f"/api/transcripts/{tid}/proposals").json()["proposals"])

    assert client.post(f"/api/transcripts/{tid}/proposals/accept",
                       json={"ids": []}).status_code == 404
    assert client.post("/api/transcripts/9999/proposals/accept",
                       json={"ids": ids}).status_code == 404


def test_api_proposals_ambiguous_needs_explicit_target(client, make_transcript):
    tid = make_transcript(segments=[
        {"start": 0.0, "end": 5.0, "text": "pie torch is fast"},
        {"start": 5.0, "end": 10.0, "text": "pie torch is also slow"},
    ])
    db.create_proposals(tid, None, [{"find": "pie torch", "replace": "PyTorch"}])
    prop = client.get(f"/api/transcripts/{tid}/proposals").json()["proposals"][0]

    skipped = client.post(f"/api/transcripts/{tid}/proposals/accept",
                          json={"ids": [prop["id"]]})
    assert skipped.status_code == 200
    assert skipped.json()["applied"] == 0
    assert skipped.json()["skipped"][0]["skip_reason"] == "ambiguous"
    assert client.get(f"/api/transcripts/{tid}").json()["text"] == (
        "pie torch is fast\npie torch is also slow"
    )

    chosen = client.post(f"/api/transcripts/{tid}/proposals/accept",
                         json={"ids": [prop["id"]], "targets": {str(prop["id"]): 1}})
    assert chosen.status_code == 200
    body = chosen.json()
    assert body["applied"] == 1
    assert [s["text"] for s in body["segments"]] == ["pie torch is fast",
                                                     "PyTorch is also slow"]


def test_api_proposals_reject_leaves_text_untouched(client, make_transcript):
    tid = make_transcript(segments=TWO_SEGMENTS)
    db.create_proposals(tid, None, [{"find": "pie torch", "replace": "PyTorch"}])
    prop = client.get(f"/api/transcripts/{tid}/proposals").json()["proposals"][0]
    before = client.get(f"/api/transcripts/{tid}").json()["text"]

    rejected = client.post(f"/api/transcripts/{tid}/proposals/reject", json={"ids": [prop["id"]]})

    assert rejected.status_code == 200
    assert rejected.json() == {"rejected": 1}
    assert [p["id"] for p in client.get(f"/api/transcripts/{tid}/proposals",
                                        params={"status": "rejected"}).json()["proposals"]] == [prop["id"]]
    assert client.get(f"/api/transcripts/{tid}/proposals",
                      params={"status": "pending"}).json()["proposals"] == []
    assert client.get(f"/api/transcripts/{tid}").json()["text"] == before
    assert client.post("/api/transcripts/9999/proposals/reject",
                       json={"ids": [prop["id"]]}).status_code == 404


def test_api_providers_never_leak_key_material(client):
    response = client.get("/api/providers")

    assert response.status_code == 200
    providers = response.json()["providers"]
    assert [p["name"] for p in providers] == ["ollama", "deepseek", "openrouter"]
    assert all(p["has_key"] is False for p in providers)
    assert all("api_key_enc" not in p for p in providers)
    assert "api_key_enc" not in response.text

    deepseek_id = providers[1]["id"]
    patched = client.patch(f"/api/providers/{deepseek_id}", json={"api_key": "sk-super-secret"})
    assert patched.status_code == 200
    updated = {p["name"]: p for p in patched.json()["providers"]}["deepseek"]
    assert updated["has_key"] is True
    assert "sk-super-secret" not in patched.text
    assert "api_key_enc" not in patched.text
    # nel DB la chiave c'è, ma cifrata
    stored = db.get_provider(deepseek_id)["api_key_enc"]
    assert stored and "sk-super-secret" not in stored

    assert client.patch("/api/providers/9999", json={"api_key": "x"}).status_code == 404
