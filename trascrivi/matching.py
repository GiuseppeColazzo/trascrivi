"""
trascrivi/matching.py
=====================
Assegnazione automatica delle pagine di slide alle lezioni, con embeddings locali.

L'idea è che l'utente non debba dire "queste 20 pagine sono la lezione 3": lo
dice la similarità fra il testo della pagina e il riassunto della lezione. Ma
l'idea sbagliata che questo file evita è fidarsi della similarità pagina per
pagina: su un corso di dieci lezioni una singola pagina di formule è vicina a
molte lezioni, e una proposta sbagliata sporca il contesto di una carta per
sempre. Quindi:

* soglia alta (`match_threshold`), perché meglio nessuna assegnazione che una
  sbagliata;
* **smoothing dei run corti**: una pagina isolata che finisce in mezzo a un
  blocco di un'altra lezione è quasi sempre rumore — viene riassorbita dal
  vicino, o scartata quando i due vicini litigano;
* le proposte restano `source='auto'` finché l'utente non conferma, e la UI le
  mostra con lo score: un numero spiega una scelta, un'assegnazione muta no.

Il vettore è normalizzato in scrittura, quindi la similarità del coseno è un
prodotto scalare. I vettori stanno in `vectors` con `kind='page:<doc>'` e
`ref_id=page_no`: il documento fa parte della chiave, quindi due deck diversi non
si mescolano.
"""

from __future__ import annotations

import hashlib
import logging
import math

from . import db, llm

log = logging.getLogger("trascrivi.matching")

# Sotto questa lunghezza il testo di una pagina non dice niente (una slide di
# soli titoli): il vettore sarebbe dominato dal rumore.
MIN_PAGE_CHARS = 40
# Il riassunto può essere lungo: la parte utile per riconoscere l'argomento è
# l'inizio (titolo + overview). Il resto sono dettagli che diluiscono.
SUMMARY_CHARS = 1200
# Un run più corto di così viene riassorbito dal vicino (o scartato).
MIN_RUN = 3


def page_kind(doc_id: int) -> str:
    return f"page:{doc_id}"


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def _sha(text: str) -> str:
    """Impronta del testo da cui nasce un vettore (vedi `db.load_vectors_with_source`)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _dot(a: list[float], b: list[float]) -> float:
    # ponytail: prodotto scalare in Python puro; con centinaia di migliaia di
    # confronti passare a numpy (già nel venv) è una riga.
    return sum(x * y for x, y in zip(a, b))


def page_text(page: dict) -> str:
    """Testo della pagina per l'embedding: OCR più rendering VLM, se c'è."""
    parts = [(page.get("text") or "").strip()]
    if (page.get("vlm_md") or "").strip():
        parts.append(page["vlm_md"].strip())
    return "\n\n".join(p for p in parts if p)


def summary_text(transcript: dict, summary: dict | None) -> str:
    """Il testo che rappresenta una lezione: riassunto se c'è, altrimenti la coda."""
    if summary:
        parts = [summary.get("title") or "", summary.get("overview") or "",
                 (summary.get("markdown") or "")[:SUMMARY_CHARS]]
        text = "\n".join(p for p in parts if p)
        if text.strip():
            return text[:SUMMARY_CHARS]
    return f"{transcript.get('title') or ''}\n{(transcript.get('text') or '')[:SUMMARY_CHARS]}"


def embed_provider() -> dict | None:
    """
    Gli embeddings sono locali: provider `ollama`, nessuna chiave.

    Se Ollama non c'è, l'assegnazione automatica non è disponibile e la UI lo
    dice — non si ripiega su un provider a pagamento, che è la cosa che il piano
    esclude esplicitamente.
    """
    return db.get_provider_by_name("ollama")


def ensure_page_vectors(doc_id: int, model: str, provider: dict | None = None) -> int:
    """
    Calcola i vettori delle pagine che non ne hanno uno aggiornato.

    Il confronto è sull'impronta del testo: dopo un pass VLM la pagina ha più
    contenuto, e un vettore calcolato prima risponderebbe su metà del testo.
    """
    provider = provider or embed_provider()
    if provider is None:
        return 0
    pages = db.list_slide_pages(doc_id)
    existing = db.load_vectors_with_source(page_kind(doc_id), model)
    todo = []
    for page in pages:
        text = page_text(page)
        if len(text) < MIN_PAGE_CHARS:
            continue
        known = existing.get(int(page["page_no"]))
        if known is None or known[1] != _sha(text):
            todo.append((int(page["page_no"]), text))
    if not todo:
        return 0
    vectors = llm.embed_texts(provider, [text for _no, text in todo], model=model)
    for (page_no, text), vec in zip(todo, vectors):
        db.put_vector(page_kind(doc_id), page_no, model, _unit(vec), source_sha=_sha(text))
    return len(todo)


def ensure_summary_vectors(project_id: int, model: str, provider: dict | None = None) -> int:
    """
    Calcola i vettori delle lezioni che non ne hanno uno aggiornato.

    Riscrivere un riassunto cambia il testo: senza il confronto sull'impronta il
    matching continuerebbe a usare il riassunto precedente, e l'unico modo di
    accorgersene sarebbe un'assegnazione che non torna.
    """
    provider = provider or embed_provider()
    if provider is None:
        return 0
    existing = db.load_vectors_with_source("summary", model)
    todo = []
    for transcript in db.list_transcripts(project_id):
        summary = db.latest_summary(int(transcript["id"]))
        text = summary_text(db.get_transcript(int(transcript["id"])) or transcript, summary)
        if len(text.strip()) < MIN_PAGE_CHARS:
            continue
        known = existing.get(int(transcript["id"]))
        if known is None or known[1] != _sha(text):
            todo.append((int(transcript["id"]), text))
    if not todo:
        return 0
    vectors = llm.embed_texts(provider, [text for _id, text in todo], model=model)
    for (transcript_id, text), vec in zip(todo, vectors):
        db.put_vector("summary", transcript_id, model, _unit(vec), source_sha=_sha(text))
    return len(todo)


def score_pages(doc_id: int, summaries: dict[int, list[float]], model: str,
                threshold: float = 0.55) -> list[tuple[int | None, float]]:
    """
    Per ogni pagina: la lezione più simile e il suo score (o `None` sotto soglia).
    """
    pages = db.list_slide_pages(doc_id)
    vectors = db.load_vectors(page_kind(doc_id), model)
    out: list[tuple[int | None, float]] = []
    for page in pages:
        vec = vectors.get(int(page["page_no"]))
        if not vec or not summaries:
            out.append((None, 0.0))
            continue
        best_id, best_score = None, 0.0
        for transcript_id, summary_vec in summaries.items():
            score = _dot(vec, summary_vec)
            if score > best_score:
                best_id, best_score = transcript_id, score
        if best_score < threshold:
            best_id = None
        out.append((best_id, round(best_score, 4)))
    return out


def _settings_threshold() -> float:
    from . import config

    return float(config.load_settings().match_threshold or 0.55)


def smooth(assignments: list[tuple[int | None, float]], min_run: int = MIN_RUN
           ) -> list[tuple[int | None, float]]:
    """
    Riassorbe i run corti.

    Un run corto non è sempre rumore, e distinguerlo è tutto il punto:

    * run corto **dentro un blocco** (stessa lezione a destra e a sinistra) ->
      assorbito, anche se la pagina non somigliava a niente: una slide di soli
      titoli in mezzo alle slide di una lezione è di quella lezione;
    * buco corto **al bordo** del documento -> chiuso verso l'unico vicino solo
      se la pagina somigliava a qualcosa (`score > 0`); una pagina che non
      somiglia a nessuna lezione resta senza assegnazione;
    * run corto **conteso** fra due lezioni diverse -> nessuna assegnazione (i
      vicini litigano: meglio niente che una scelta a caso);
    * run corto **assegnato** al bordo (l'inizio di un deck) -> resta com'è: un
      blocco di due pagine all'inizio è l'inizio di una lezione, non rumore.
    """
    current = list(assignments)
    for _ in range(200):
        runs = _runs(current)
        changed = False
        for index, (start, end, transcript_id, score) in enumerate(runs):
            if end - start + 1 >= min_run:
                continue
            has_left, has_right = index > 0, index + 1 < len(runs)
            left = runs[index - 1][2] if has_left else None
            right = runs[index + 1][2] if has_right else None
            length = end - start + 1

            if left is not None and left == right:
                # Buco o pagina isolata dentro un blocco: è di quel blocco,
                # anche se la pagina non somigliava a niente (una slide di soli
                # titoli in mezzo a una lezione è di quella lezione).
                target = left
            elif transcript_id is None and not (has_left and has_right):
                # Buco al bordo del documento: si chiude solo se la pagina
                # somigliava a qualcosa. Una pagina che non somiglia a nessuna
                # lezione resta senza assegnazione (meglio niente che sbagliata).
                neighbour = left if left is not None else right
                target = neighbour if (score > 0 and neighbour is not None) else None
            elif transcript_id is not None and left is not None and right is not None:
                # Conteso fra due lezioni diverse: i vicini litigano.
                target = None
            else:
                # Blocco corto al bordo (l'inizio di un deck) o in mezzo a un
                # buco: non è rumore, resta com'è.
                target = transcript_id
            if target == transcript_id:
                continue
            for i in range(start, end + 1):
                current[i] = (target, score)
            changed = True
            break  # i run sono cambiati: si ricalcolano
        if not changed:
            break
    return current


def _runs(assignments: list[tuple[int | None, float]]) -> list[tuple[int, int, int | None, float]]:
    """I tratti consecutivi con la stessa assegnazione: (inizio, fine, id, score)."""
    runs: list[tuple[int, int, int | None, float]] = []
    start = 0
    for index in range(1, len(assignments) + 1):
        if index == len(assignments) or assignments[index][0] != assignments[start][0]:
            scores = [s for _t, s in assignments[start:index]]
            runs.append((start, index - 1, assignments[start][0],
                         round(sum(scores) / len(scores), 4) if scores else 0.0))
            start = index
    return runs


def to_ranges(assignments: list[tuple[int | None, float]],
              page_numbers: list[int]) -> list[dict]:
    """Dagli assegnamenti per pagina agli intervalli da salvare in `slide_links`."""
    out = []
    for start, end, transcript_id, score in _runs(assignments):
        if transcript_id is None:
            continue
        out.append({"page_from": page_numbers[start], "page_to": page_numbers[end],
                    "transcript_id": transcript_id, "score": score})
    return out


def match_project(project_id: int, on_progress=None) -> dict:
    """
    Ricalcola le proposte di tutto un corso. Restituisce un riassunto per la UI.

    È tutto il corso e non il singolo documento: aggiungere la lezione 5 cambia
    la similarità delle pagine della lezione 4, e un ri-match parziale
    lascerebbe proposte vecchie accanto a quelle nuove.
    """
    from . import config

    settings = config.load_settings()
    model = (settings.embed_model or "nomic-embed-text:v1.5").strip()
    provider = embed_provider()
    if provider is None:
        raise ValueError("Ollama is not configured: slide matching needs local embeddings")

    docs = db.list_slide_docs(project_id)
    ensure_summary_vectors(project_id, model, provider)
    summaries = db.load_vectors("summary", model)
    # Solo le lezioni di questo corso possono essere proposte: il vettore di un
    # altro corso risponderebbe su una pagina che non gli appartiene.
    course_ids = {int(t["id"]) for t in db.list_transcripts(project_id)}
    summaries = {k: v for k, v in summaries.items() if k in course_ids}
    threshold = float(settings.match_threshold or 0.55)

    total_links = 0
    for index, doc in enumerate(docs, start=1):
        ensure_page_vectors(int(doc["id"]), model, provider)
        assignments = score_pages(int(doc["id"]), summaries, model, threshold)
        assignments = smooth(assignments)
        page_numbers = [int(p["page_no"]) for p in db.list_slide_pages(int(doc["id"]))]
        ranges = to_ranges(assignments, page_numbers)
        db.replace_auto_links(int(doc["id"]), ranges)
        total_links += len(ranges)
        if on_progress:
            on_progress(index, len(docs))
    log.info("matching corso %s: %d intervalli su %d documenti",
             project_id, total_links, len(docs))
    return {"project_id": project_id, "docs": len(docs), "links": total_links,
            "model": model}
