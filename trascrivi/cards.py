"""
trascrivi/cards.py
==================
Dalle slide e dal riassunto a un mazzo di flashcard.

Tre cose rendono questo file più lungo di "chiedi al modello un JSON":

1. **Il contesto è assemblato e tagliato qui**, non lasciato al modello. Il
   riassunto della lezione più le pagine assegnate possono superare qualunque
   finestra comoda: si entra nel budget `cards_context_chars` a blocchi
   etichettati ("Slide 25 — …"), perché un blocco senza etichetta non si può
   citare, e la citazione è metà del valore di una carta presa da una slide.

2. **Nessuna formula inventata** (principio 7 del piano). Il contesto dichiara
   quali espressioni LaTeX sono state verificate; una carta che ne contiene una
   diversa viene scartata. Se una pagina è `math_uncertain`, le sue formule non
   entrano nell'insieme verificato: si possono leggere, non citare.

3. **La rigenerazione è per hash.** Lo stesso contesto non si paga due volte, e
   quando il contesto cambia si sostituiscono solo le carte generate che nessuno
   ha toccato: quelle modificate a mano restano e vengono passate al modello
   nell'elenco degli esistenti, così non le duplica.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
import uuid

from . import config, db, llm

log = logging.getLogger("trascrivi.cards")

# Come per il riassunto: il prompt è versionato perché il contenuto delle carte
# dipende da lui, e la cache deve invalidarsi quando cambia. p2: regola esplicita
# sulla cancellazione nei cloze (i modelli locali producevano cloze senza buchi,
# che Anki mostrerebbe come una carta con niente da ricordare).
PROMPT_VERSION = "p2"
# Le immagini costano circa un migliaio di token l'una: nel preventivo vanno
# contate, altrimenti la stima prima della generazione è una bugia.
VISION_TOKENS_PER_IMAGE = 1000
VISION_MAX_PAGES = 3
VISION_IMAGE_LONG_SIDE = 1024
# Quanta parte del contesto può prendere il riassunto: il resto è slide, che è
# la ragione per cui questa funzione esiste.
SUMMARY_SHARE = 0.5
# Una pagina di slide lunga quanto un libro non serve intera per fare una carta.
PAGE_CHARS = 1800

# Provider che sanno leggere un'immagine. DeepSeek no: è l'unico text-only dei
# tre, e la mappa è esplicita perché la vision mode non deve fallire a runtime
# su un modello che non la supporta (finirebbe in una carta vuota o in un 400).
VISION_PROVIDERS = {"ollama": True, "openrouter": True, "deepseek": False}

SYSTEM_PROMPT = """You write flashcards from a university lecture for spaced repetition in Anki.

Return ONLY a JSON object, no prose, no markdown fences:
{"cards": [{"kind": "qa", "front": "...", "back": "...", "slide": 25}]}

A card is good when it tests exactly one thing.
- "qa": a question whose answer is short and unambiguous. The question says what it is about; "what is X?" is fine, "explain the slide" is not.
- "cloze": one sentence from the material with one or two deletions, written as {{c1::...}} and {{c2::...}}. At most two deletions, and they must be the words that carry the fact. A cloze card without a deletion is not a card: if you cannot choose which words to delete, write a "qa" card instead.
- Mix the two kinds: cloze when the material states a fact in a sentence worth finishing, qa when the reader has to give an answer of their own. At least one card in three should be cloze when the material allows it.
- Atomic: one fact per card. If a sentence carries two facts, that is two cards.
- Answerable from the material alone: never use outside knowledge, never add examples, numbers or opinions that are not in it.
- The answer must not be guessable from the question alone, and must not be longer than two lines.

Formulas and notation:
- Write mathematics as LaTeX between $...$, copied exactly as it appears in the material.
- A card about a formula is allowed ONLY when that formula is present in the material as LaTeX. If a block says its formulas are uncertain, do not write cards about them.
- Never write a formula by memory and never "fix" one because it looks wrong.

Rules:
- Write the cards in {language}.
- Do not repeat a front that already exists for this lecture: the list is in the request.
- Maximum {max_cards} cards. Fewer, better cards always beat a full quota.
- "slide" is the page number the card comes from, taken from the "Slide N" label of the block you used; use null when the card comes from the summary alone.
- No preamble, no commentary, no card about the slides themselves ("this slide shows...")."""

# I segnaposto si sostituiscono con `replace`, non con `format`: il prompt
# contiene `{{c1::...}}` e un oggetto JSON, e `format` se li mangerebbe (KeyError
# su "cards" o cancellazioni sparite). È un errore già costato un giro.
USER_PROMPT = """<lecture>
{summary}
</lecture>

{slides}

{existing}Write at most {max_cards} cards from this material."""


# ── Contesto ─────────────────────────────────────────────────────────────────
def _language_name(code: str | None) -> str:
    from .summary import _language_name as name

    return name(code)


def _slide_block(page: dict) -> str:
    """
    Una pagina come blocco citabile: etichetta + testo + rendering VLM.

    `math_uncertain` non toglie la pagina dal contesto (l'OCR può comunque
    contenere l'idea), ma dichiara che le sue formule non sono verificabili: è
    l'informazione che serve al modello per non scriverci sopra una carta.
    """
    parts = [f"Slide {page['page_no']} — {page.get('doc_name') or 'slides'}"]
    text = (page.get("text") or "").strip()
    if text:
        parts.append(text[:PAGE_CHARS])
    vlm_md = (page.get("vlm_md") or "").strip()
    if vlm_md:
        parts.append(f"[structured rendering]\n{vlm_md[:PAGE_CHARS]}")
    if page.get("math_uncertain"):
        parts.append("[formulas on this page are uncertain: do not write cards about them]")
    return "\n".join(parts)


def _summary_block(transcript: dict, summary: dict | None) -> str:
    if summary and (summary.get("markdown") or "").strip():
        return summary["markdown"].strip()
    return (transcript.get("text") or "").strip()


def build_context(transcript: dict, settings, *, limit_pages: int | None = None) -> dict:
    """
    Il materiale che finisce nel prompt, dentro il budget di caratteri.

    Ordine: il riassunto (metà del budget al massimo), poi le pagine assegnate,
    poi — se avanza spazio — la trascrizione grezza. Le pagine senza link non
    entrano: il piano è esplicito sul fatto che il contesto non si sporca da
    solo (`slide_links` è la scelta dell'utente o del matching confermato).
    """
    budget = int(getattr(settings, "cards_context_chars", 12000) or 12000)
    summary = db.latest_summary(int(transcript["id"]))
    pages = db.linked_pages(int(transcript["id"]))
    if limit_pages is not None:
        pages = pages[:limit_pages]

    summary_text = _summary_block(transcript, summary)
    summary_budget = max(1000, int(budget * SUMMARY_SHARE))
    summary_text = summary_text[:summary_budget]

    blocks: list[str] = []
    used = len(summary_text)
    for page in pages:
        block = _slide_block(page)
        if used + len(block) > budget:
            break
        blocks.append(block)
        used += len(block)

    return {
        "summary": summary_text,
        "slides": blocks,
        "pages": pages[:len(blocks)],
        "chars": used,
        "budget": budget,
        "has_summary": bool(summary),
    }


_LATEX = re.compile(r"\$([^$]{1,200})\$")


def latex_set(context: dict) -> set[str]:
    """
    Le formule che una carta può citare: quelle presenti nel materiale e non
    marcate incerte.

    Il confronto è sul testo *spazi normalizzati*: il modello ricopia quasi
    sempre uguale, ma uno spazio di troppo non deve far scartare una carta buona.
    """
    allowed: set[str] = set()
    for chunk in [context["summary"], *context["slides"]]:
        for match in _LATEX.finditer(chunk):
            allowed.add(_normalize_math(match.group(1)))
    for page in context["pages"]:
        if page.get("math_uncertain"):
            for match in _LATEX.finditer(page.get("vlm_md") or ""):
                allowed.discard(_normalize_math(match.group(1)))
    return allowed


def _normalize_math(text: str) -> str:
    return " ".join((text or "").split())


def existing_fronts(project_id: int, transcript_id: int, *, for_hash: bool = False) -> list[str]:
    """
    I front già presenti, per la regola anti-duplicato.

    Due usi, due elenchi: il **prompt** vuole tutto (così il modello non riscrive
    una domanda che esiste già), mentre la **cache** non può includere le carte
    generate da questa lezione — sono proprio quelle che il run sostituisce, e
    metterle nell'impronta la farebbe cambiare a ogni run, disattivando la cache
    per sempre. Restano nell'impronta le carte scritte a mano: cambiarle è una
    ragione vera per rigenerare.
    """
    fronts: list[str] = []
    for card in db.project_cards(project_id):
        if int(card["transcript_id"]) != int(transcript_id):
            fronts.append(card["front"])
    for card in db.card_fronts(transcript_id):
        if for_hash and not card.get("edited"):
            continue
        fronts.append(card["front"])
    return fronts


def inputs_hash(context: dict, settings, provider: dict, *, vision: bool,
                existing: list[str]) -> str:
    """
    L'impronta di tutto ciò che determina il risultato.

    Se non cambia, la generazione non serve: è la cache che evita di pagare due
    volte lo stesso mazzo. Il modello e la versione del prompt sono dentro
    perché cambiare l'uno o l'altro cambia le carte.
    """
    payload = {
        "prompt": PROMPT_VERSION,
        "provider": provider.get("name") or "",
        "model": provider.get("model") or "",
        "vision": bool(vision),
        "cards_max": int(getattr(settings, "cards_max", 12) or 12),
        "language": getattr(settings, "cards_language", "") or "",
        "summary": context["summary"],
        "slides": context["slides"],
        "existing": existing,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ── Stima locale (nessuna chiamata) ──────────────────────────────────────────
def vision_pages(context: dict, pages_limit: int = VISION_MAX_PAGES) -> list[dict]:
    """Le pagine da allegare come immagine: quelle linkate che hanno un render."""
    out = [p for p in context["pages"] if p.get("render_path")]
    return out[:pages_limit]


def supports_vision(provider: dict, settings) -> bool:
    return bool(VISION_PROVIDERS.get(provider.get("name") or "", False))


def estimate(transcript: dict, provider: dict, settings, *, vision: bool | None = None,
             limit_pages: int | None = None) -> dict:
    """
    Il preventivo mostrato in UI prima di premere "Genera": tutto locale.

    Non è una chiamata all'API di fatturazione del provider (che non esiste):
    sono caratteri contati qui e token stimati con la stessa regola del resto
    dell'app (~4 caratteri per token).
    """
    context = build_context(transcript, settings, limit_pages=limit_pages)
    if vision is None:
        vision = wants_vision(provider, settings, context)
    images = vision_pages(context) if vision else []
    existing = existing_fronts(int(transcript["project_id"]), int(transcript["id"]))
    text_tokens = llm.estimate_tokens(context["summary"] + "\n".join(context["slides"])
                                     + "\n".join(existing) + SYSTEM_PROMPT)
    latest = db.latest_card_run(int(transcript["id"]))
    fingerprint = inputs_hash(context, settings, provider, vision=bool(images),
                              existing=existing_fronts(int(transcript["project_id"]),
                                                       int(transcript["id"]),
                                                       for_hash=True))
    cached = bool(latest and latest["inputs_hash"] == fingerprint
                  and latest.get("status") == "ok")
    return {
        "chars": context["chars"],
        "budget": context["budget"],
        "slides": len(context["slides"]),
        "summary": context["has_summary"],
        "tokens_in": text_tokens + VISION_TOKENS_PER_IMAGE * len(images),
        "images": len(images),
        "vision_available": supports_vision(provider, settings),
        "vision": bool(images),
        "cards_max": int(getattr(settings, "cards_max", 12) or 12),
        "cached": cached,
        "inputs_hash": fingerprint,
        "prompt_version": PROMPT_VERSION,
    }


def wants_vision(provider: dict, settings, context: dict) -> bool:
    """`cards_vision=auto` allega le immagini solo se il modello le sa leggere."""
    mode = (getattr(settings, "cards_vision", "off") or "off").lower()
    if mode not in ("auto", "on"):
        return False
    if not supports_vision(provider, settings):
        return False
    return bool(vision_pages(context))


# ── Validazione delle carte ──────────────────────────────────────────────────
def _normalize_for_dedup(text: str) -> str:
    """Minuscolo, senza accenti e senza punteggiatura: la forma del confronto."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _cloze_is_valid(text: str) -> bool:
    """
    `{{c1::…}}`: almeno una cancellazione, non vuota, non più di due.

    Una cancellazione di un carattere è legittima — "i canali diventano
    {{c1::6}}" è una carta buona — mentre una cancellazione vuota è un errore di
    formato che non si può correggere indovinando.
    """
    found = re.findall(r"\{\{c(\d)::(.+?)\}\}", text or "", re.S)
    if not found or len(found) > 2:
        return False
    return all(value.strip() for _num, value in found)


def _latex_spans(text: str) -> list[str]:
    return [_normalize_math(m.group(1)) for m in _LATEX.finditer(text or "")]


def cards_from_response(content: str, parsed) -> list:
    """
    Le carte dalla risposta del modello, in qualunque forma sia arrivata.

    Le forme viste in pratica sono tre, e tutte vanno accettate: l'oggetto
    `{"cards": [...]}` che il prompt chiede, l'**array nudo**, e una carta sola.
    Il parser JSON condiviso (`llm.extract_json`) restituisce solo oggetti — il
    contratto dell'agente di correzione — e su un array nudo restituiva la prima
    carta dell'array, facendo passare un mazzo intero per una carta e salvando
    zero: trovato nell'E2E, non in teoria.
    """
    if isinstance(parsed, dict) and isinstance(parsed.get("cards"), list):
        return parsed["cards"]
    if isinstance(parsed, dict) and parsed.get("front"):
        return [parsed]
    start = (content or "").find("[")
    if start >= 0:
        try:
            value = json.JSONDecoder().raw_decode(content[start:])[0]
        except ValueError:
            value = None
        if isinstance(value, list):
            return value
    return []


def clean_cards(items: list, *, allowed_latex: set[str], existing_hashes: set[str],
                page_numbers: set[int], cards_max: int) -> tuple[list[dict], list[str]]:
    """
    Da quello che ha risposto il modello alle carte che si possono salvare.

    Scarta (e dice perché) invece di aggiustare: una carta con LaTeX non
    verificato o con un cloze malformato non si corregge indovinando.
    """
    out: list[dict] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "qa").strip().lower()
        front = str(item.get("front") or "").strip()
        back = str(item.get("back") or "").strip()
        if kind not in ("qa", "cloze") or not front:
            rejected.append("malformed")
            continue
        if kind == "qa" and not back:
            rejected.append("empty_back")
            continue
        if kind == "cloze" and not _cloze_is_valid(front):
            # Il testo scartato resta nel log: senza, "3 scartate" non dice se il
            # problema è il modello o la validazione.
            log.info("cloze scartata (formato): %.160s", front)
            rejected.append("bad_cloze")
            continue
        unverified = [span for span in _latex_spans(f"{front} {back}")
                      if span and span not in allowed_latex]
        if unverified:
            # Principio 7: la formula o è nel materiale, o la carta non nasce.
            rejected.append("unverified_formula")
            log.info("carta scartata per formula non verificata: %s", unverified[0][:80])
            continue
        digest = _normalize_for_dedup(front)
        if not digest or digest in existing_hashes or digest in seen:
            rejected.append("duplicate")
            continue
        slide = item.get("slide")
        try:
            slide_ref = int(slide) if slide is not None else None
        except (TypeError, ValueError):
            slide_ref = None
        if slide_ref is not None and slide_ref not in page_numbers:
            # Un numero di pagina che non esiste nel contesto è un'invenzione.
            slide_ref = None
        seen.add(digest)
        out.append({"uid": uuid.uuid4().hex, "kind": kind, "front": front,
                    "back": back, "slide_ref": slide_ref, "origin": "llm"})
        if len(out) >= cards_max:
            break
    return out, rejected


# ── Generazione ──────────────────────────────────────────────────────────────
def generate(provider: dict, transcript: dict, settings, *, force: bool = False,
             vision: bool | None = None, on_progress=None) -> dict:
    """
    Genera e salva le carte di una lezione. Ritorna un riassunto per il job.

    Nessun salvataggio prima che la risposta sia stata validata: se il modello
    restituisce spazzatura, le carte che c'erano restano dove sono.
    """
    context = build_context(transcript, settings)
    if not context["summary"].strip():
        raise ValueError("This lecture has no summary and no text to make cards from")

    use_vision = wants_vision(provider, settings, context) if vision is None else (
        vision and supports_vision(provider, settings))
    images = vision_pages(context) if use_vision else []
    existing = existing_fronts(int(transcript["project_id"]), int(transcript["id"]))
    fingerprint = inputs_hash(context, settings, provider, vision=bool(images),
                              existing=existing_fronts(int(transcript["project_id"]),
                                                       int(transcript["id"]),
                                                       for_hash=True))
    latest = db.latest_card_run(int(transcript["id"]))
    if not force and latest and latest["inputs_hash"] == fingerprint \
            and latest.get("status") == "ok":
        return {"cached": True, "n_new": 0, "n_skipped_dup": 0,
                "message": "already up to date: same material, no call made",
                "run_id": int(latest["id"])}

    cards_max = int(getattr(settings, "cards_max", 12) or 12)
    language_name = _language_name(getattr(settings, "cards_language", "")
                                   or transcript.get("language") or "en")
    summary_block = f"[summary of the lecture]\n{context['summary']}"
    slide_blocks = "\n\n".join(context["slides"]) if context["slides"] else \
        "(no slides assigned to this lecture)"
    existing_block = ""
    if existing:
        listed = "\n".join(f"- {front}" for front in existing[:60])
        existing_block = ("<already_existing_fronts>\n"
                          "Do not write a card with one of these questions:\n"
                          f"{listed}\n</already_existing_fronts>\n\n")

    user = (USER_PROMPT.replace("{summary}", summary_block)
            .replace("{slides}", slide_blocks)
            .replace("{existing}", existing_block)
            .replace("{max_cards}", str(cards_max)))
    system = (SYSTEM_PROMPT.replace("{language}", language_name)
              .replace("{max_cards}", str(cards_max)))

    if on_progress:
        on_progress(0.2, f"asking {provider.get('model')} for up to {cards_max} cards")

    started = time.time()
    content, diag = llm.chat_completion(
        provider, provider.get("model") or "", system, user,
        max_tokens=max(800, cards_max * 220),
        temperature=0.2,
        images=[_image_b64(page) for page in images] if images else None,
    )
    usage = {"prompt": 0, "completion": 0, "hit": 0, "miss": 0}
    llm.add_usage(usage, diag)

    payload = llm.extract_json(content)
    if payload is None:
        raise ValueError(f"The model did not return usable JSON. {llm._error_detail(diag)}")

    page_numbers = {int(p["page_no"]) for p in context["pages"]}
    existing_hashes = {_normalize_for_dedup(f) for f in existing}
    cards, rejected = clean_cards(cards_from_response(content, payload),
                                  allowed_latex=latex_set(context),
                                  existing_hashes=existing_hashes,
                                  page_numbers=page_numbers, cards_max=cards_max)
    if not cards:
        # Un run a vuoto ha sempre una causa, e senza questa riga il log dice solo
        # "0 carte": qui resta cosa è stato interpretato e cosa è stato scartato.
        log.warning("nessuna carta salvata (scartate: %s, finish=%s) — interpretato: %.500s "
                    "— risposta: %.500s", rejected or "nessuna", diag.get("finish_reason"),
                    json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload,
                    content or "(vuota)")
    for card in cards:
        card["model"] = provider.get("model") or ""

    run_id = db.create_card_run(
        int(transcript["id"]), provider=provider.get("name") or "",
        model=provider.get("model") or "", prompt_version=PROMPT_VERSION,
        inputs_hash=fingerprint, n_new=len(cards),
        n_skipped_dup=sum(1 for r in rejected if r == "duplicate"),
        tokens_in=usage["prompt"], tokens_out=usage["completion"],
        cache_hit=usage["hit"], cost_usd=llm.estimate_cost(usage) or 0.0,
        status="ok",
    )

    # Sostituzione: le carte generate non modificate se ne vanno, quelle
    # modificate a mano restano (e sono già nell'elenco degli esistenti).
    removed = db.delete_llm_cards(int(transcript["id"]), keep_edited=True) if (
        force or latest) else 0
    db.create_cards(int(transcript["id"]), cards, run_id=run_id)
    if not cards:
        db.update_card_run(run_id, {"status": "empty"})

    message = f"{len(cards)} cards"
    if rejected:
        message += f" . {len(rejected)} rejected ({rejected[0]})"
    if removed:
        message += f" . {removed} replaced"
    return {
        "cached": False,
        "run_id": run_id,
        "n_new": len(cards),
        "n_skipped_dup": sum(1 for r in rejected if r == "duplicate"),
        "rejected": rejected,
        "replaced": removed,
        "usage": usage,
        "elapsed_s": round(time.time() - started, 1),
        "message": message,
    }


def _image_b64(page: dict, long_side: int = VISION_IMAGE_LONG_SIDE) -> str:
    """
    Il render di una pagina come immagine per il modello, ridotto a 1024 px.

    Il tetto non è una preferenza: ogni immagine costa token, e una pagina di
    slide a 1600 px non aggiunge leggibilità che valga il 60% di token in più.
    """
    import base64
    import io

    from PIL import Image

    img = Image.open(page["render_path"])
    if max(img.size) > long_side:
        img.thumbnail((long_side, long_side), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")
