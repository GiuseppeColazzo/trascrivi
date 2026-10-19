"""
trascrivi/vlm.py
================
Rendering strutturato di una pagina complessa con una VLM locale (Ollama).

L'OCR sbaglia in modo sistematico ciò che rende utili le slide di un corso di
ingegneria: α diventa `a`, θ diventa `0`, Σ diventa `I`, apici e pedici si
perdono, e i diagrammi escono come frammenti in ordine casuale. Una VLM legge la
pagina come farebbe una persona e restituisce una trascrizione ordinata in
quattro sezioni: Testo, Formule (LaTeX), Figure, Annotazioni.

Il prompt è tarato sui tre campioni reali in `slide_samples/` (vedi
`tools/vlm_ab.py` e la tabella in PIANO-slide-e-carte.md). Tre vincoli che sono
il risultato dell'A/B, non un'opinione:

* **Formule in LaTeX**, una per riga, e `UNCERTAIN:` quando la lettura non è
  sicura: una formula inventata è peggio di una formula mancante, perché
  finirebbe in una carta.
* **Figure descritte per struttura** (gruppi, etichette, frecce disegnate) e
  mai per aspetto: chiedendo "cosa mostra il diagramma" il modello da 4B
  inventava frecce e sfere inesistenti; chiedendo i fatti visibili resta
  concreto.
* **Tersezza con tetto di righe**: questa è una forma intermedia, non un
  documento per il lettore. Senza il tetto il modello scrive paragrafi che poi
  finiscono nel contesto delle carte, dove costano token senza aggiungere fatti.

Tutto locale e gratis: nessuna chiave API, il provider è `ollama`.
"""

from __future__ import annotations

import base64
import logging

from . import db, llm

log = logging.getLogger("trascrivi.vlm")

# Cambiare il prompt significa cambiare cosa finisce in `slide_pages.vlm_md`:
# la versione sta qui per poter rigenerare solo le pagine di una versione vecchia.
PROMPT_VERSION = "v1"
# Il massimo che serve: nei campioni reali l'output è di 150-260 token. Il tetto
# resta alto per una pagina di sole formule, non per lasciar divagare.
MAX_OUTPUT_TOKENS = 1200
# Un rendering con "UNCERTAIN" non deve produrre carte di matematica: la pagina
# viene marcata e `cards.py` la tratta diversamente.
UNCERTAIN_MARK = "UNCERTAIN"

SYSTEM_PROMPT = """You transcribe one lecture slide for a study assistant that will later write flashcards from it.

Answer in Markdown with these sections, and only when the slide has something for them:

## Testo
Everything written on the slide, top to bottom, in reading order, in the original wording and language: the title first, then the text, then the labels of any diagram, grouped the way the diagram groups them. Keep formulas between $...$, in LaTeX (\\alpha, \\theta, \\Sigma, superscripts and subscripts as they appear), and keep every number next to the label it belongs to.

## Figure
One bullet per diagram, and only the structure that its labels alone do not say: "A -> B" when an arrow is actually drawn between them; when the same structure is repeated at several parameter values, one bullet per value with the sizes it produces ("alpha=0.75: L1 K=6, L2 K=3"). Never describe appearance: no colours, no textures, no shading.

## Annotazioni
Footers, source lines, legends and logos, each one next to what it annotates.

If you cannot tell the symbols of an expression apart, write `UNCERTAIN: <best attempt>` on its own line instead of guessing. Never invent content that is not visible; no preamble, no closing remarks; be terse, this is an intermediate form, not a document for a reader."""


def encode_image(path: str) -> str | None:
    """PNG di pagina in base64 per il messaggio multimodale."""
    try:
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    except OSError as exc:
        log.warning("render non leggibile (%s): %s", path, exc)
        return None


def describe_page(provider: dict, image_b64: str, model: str) -> tuple[str, dict]:
    """
    Una pagina -> markdown strutturato. Passa da `llm._client`, quindi la
    guardia anti-rete dei test copre anche questa chiamata.
    """
    client = llm._client(provider)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": SYSTEM_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        ]}],
        temperature=0,
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    content, diag = llm.extract_content(resp)
    return (content or "").strip(), diag


def wants_page(page: dict, settings, index: int) -> bool:
    """
    Questa pagina va passata alla VLM?

    `on` = tutte (entro il tetto per documento), `auto` = solo le complesse,
    `off` = nessuna. Una pagina senza render non è passabile: non c'è immagine.
    """
    mode = (getattr(settings, "slides_vlm", "off") or "off").lower()
    if mode == "off" or not page.get("render_path"):
        return False
    limit = int(getattr(settings, "slides_vlm_max", 0) or 0)
    if limit and index >= limit:
        return False
    return mode == "on" or (mode == "auto" and bool(page.get("complex")))


def mark_uncertain(markdown: str, page_text: str) -> int:
    """
    La matematica di questa pagina è stata trascritta con certezza?

    Due segnali, in ordine: la VLM ha scritto `UNCERTAIN`, oppure l'OCR della
    pagina contiene simboli matematici e il rendering non contiene *nessun*
    LaTeX — cioè la matematica c'era e non è stata trascritta, che è il caso in
    cui l'OCR ha letto `a` al posto di `α`. Una pagina con la matematica in
    `$...$` è verificata: è quella forma che finisce nel contesto delle carte.
    """
    markdown = markdown or ""
    if UNCERTAIN_MARK in markdown.upper():
        return 1
    from .slideparser import MATH_CHARS, MATH_PATTERN

    text = page_text or ""
    if not any(char in MATH_CHARS for char in text) and not MATH_PATTERN.search(text):
        return 0
    return 0 if "$" in markdown else 1


def enhance_doc(doc_id: int, settings, on_progress=None) -> int:
    """
    Passa la VLM sulle pagine che lo meritano di un documento. Restituisce
    quante pagine ha reso.

    Non solleva se la VLM non c'è (modello non scaricato, Ollama spento): il
    documento resta pronto col solo OCR, che è il default economico. Un errore
    qui non deve far fallire un parsing già riuscito.
    """
    mode = (getattr(settings, "slides_vlm", "off") or "off").lower()
    doc = db.get_slide_doc(doc_id)
    if mode == "off" or not doc or not doc.get("vlm_enabled"):
        return 0

    provider = db.get_provider_by_name("ollama")
    if not provider:
        log.info("VLM saltata: provider ollama non configurato")
        return 0
    model = (getattr(settings, "slides_vlm_model", "") or provider.get("model") or "").strip()
    if not model:
        return 0

    pages = db.list_slide_pages(doc_id)
    todo = [(i, p) for i, p in enumerate(pages)
            if wants_page(p, settings, i) and not (p.get("vlm_md") or "").strip()]
    if not todo:
        return 0

    done = 0
    for index, page in todo:
        image = encode_image(page.get("render_path") or "")
        if not image:
            continue
        try:
            markdown, _diag = describe_page(provider, image, model)
        except Exception as exc:  # noqa: BLE001 - la VLM è un extra, non un requisito
            log.warning("VLM fallita sulla pagina %s del documento %s: %s",
                        page["page_no"], doc_id, exc)
            break
        if not markdown:
            continue
        db.update_slide_page(doc_id, page["page_no"], {
            "vlm_md": markdown,
            "math_uncertain": mark_uncertain(markdown, page.get("text") or ""),
        })
        done += 1
        if on_progress:
            on_progress(done, len(todo))
    log.info("VLM: %d pagine rese sul documento %s (%s)", done, doc_id, model)
    return done
