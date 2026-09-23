"""
trascrivi/summary.py
====================
Da una trascrizione a un documento di studio in **Markdown**.

L'output è un testo unico, ben formattato: il modello scrive direttamente il
documento, e il markdown è il formato di consegna (si legge nella tab, si scarica
come `.md`). Niente struttura JSON intermedia, niente citazioni da verificare:
il documento *è* il risultato.

La prima versione chiedeva JSON con una citazione letterale per ogni punto e
verificava ogni citazione contro la trascrizione. Era difendibile in teoria e
illeggibile in pratica: il testo si riempiva di citazioni e il lettore perdeva il
filo. Qui resta la parte che serviva davvero — il modello non deve inventare — ma
la si ottiene con le istruzioni, non con un'impalcatura di validazione.

Strategia: **una sola passata.** La trascrizione intera entra in una chiamata,
perché `deepseek-flash` ha 1M token di contesto e una lezione da due ore ne occupa
~20 000. Il percorso a blocchi (appunti per parte + stesura finale) è stato tolto:
costava 2,5 volte tanto, spediva la trascrizione due volte e — soprattutto — la
stesura finale riceveva appunti già lunghi che finivano per fare da ancora, facendo
uscire il documento circa il 60% sopra il budget.

Il limite pratico ora è la finestra del modello: un modello locale da 32k token non
contiene una lezione intera, quindi va scelta una finestra più ampia.
"""

from __future__ import annotations

import json
import logging
import re
import time

from . import config, db, llm

log = logging.getLogger("trascrivi.summary")

# Tariffe DeepSeek `deepseek-flash` in USD per 1M token, listino letto il
# 2026-09-23. Fuori picco = 50% esatto del picco; qui si usa la fascia off-peak
# perché è quella in cui cade quasi tutto l'orario europeo. È una STIMA: il
# costo vero è quello che fattura il provider.
DEEPSEEK_RATES = {
    "cache_hit": 0.003,
    "cache_miss": 0.15,
    "output": 0.60,
}

STYLES = ("study", "brief", "detailed")

# Quanto deve essere lungo il riassunto, in proporzione alla trascrizione.
#
# Il numero va dato esplicito. Senza, il modello scrive finché decide di aver
# finito, e il risultato è lungo quanto la lezione: misurato su una lezione reale,
# 6338 parole di note su 7000 parole di trascrizione, con 7601 token di output su
# un tetto di 8000. Non era un tetto troppo basso, era l'assenza di un obiettivo.
#
# I modelli sovra-generano in modo sistematico (Plan-and-Write, KDD 2025,
# arXiv:2511.01807: "systematic bias toward over-generation"), quindi il rapporto
# va tenuto basso e il limite va ripetuto come vincolo, non come suggerimento.
STYLE_SHAPE = {
    "brief":    {"ratio": 0.06, "floor": 150, "cap": 450},
    "study":    {"ratio": 0.15, "floor": 300, "cap": 2000},
    "detailed": {"ratio": 0.25, "floor": 600, "cap": 3500},
}

SYSTEM_PROMPT = """You write study notes from university lecture transcripts, in Markdown.

The transcript comes from automatic speech recognition: expect recognition errors, false starts, repetitions and missing punctuation. Read through them.

A summary is a selection, not a transcript. Deciding what to leave out is the job; keeping everything is not being thorough, it is failing the task.

RULES
1. Use only what the transcript says. You have no other knowledge of the subject: never add facts, definitions, examples or numbers that are not in the transcript, even if you are sure they are correct.
2. Write the notes in {language_name}.
3. Stay inside the word budget given in the request. Going over it is a failure, not a detail to fix later.
4. Cover the topics that carry the lecture, in the order they were given. Every section must earn its place: if one would only repeat another, merge it or drop it.
5. Prefer lists and short paragraphs to long blocks of prose. Bold the term being defined, not whole sentences.
6. Use a Markdown table whenever the lecture compares things, lists options, or classifies items by attributes.
7. Use a diagram in a fenced code block only where the lecture describes a process, a flow or an architecture that is clearer as a picture. Two or three in a whole lecture at most.
8. Keep the lecturer's technical terms, acronyms and symbols exactly as they were said. Do not expand an acronym they did not expand, and do not rename anything.
9. Keep their uncertainty. "probably", "I think", "in general", "it seems" must not become statements of fact.

ALWAYS LEAVE OUT
- housekeeping and logistics: timetables, breaks, exam dates, recordings, who sits where, administrative remarks;
- the lecturer's scaffolding: "we will see this later", "for now this is qualitative", "let me repeat that", "as I was saying";
- repetition: the same concept stated three times is one bullet, not three;
- examples that only illustrate a definition already given. Keep an example only when the concept does not survive without it;
- the texture of spoken language: digressions, asides, jokes, audience questions that lead nowhere.

Write no preamble, no closing remarks, and no commentary about the transcript itself."""

STYLE_HINT = {
    "study": ("Write study notes: the concepts, the definitions, the results and the reasoning that "
              "connects them, at a level of detail that lets someone revise the lecture."),
    "brief": ("Write a short summary: the essential ideas only, one section per main topic, a few "
              "bullets each. Keep the definitions, drop the detail and the examples."),
    "detailed": ("Write thorough notes: for each concept keep the argument, the numbers and the "
                 "caveats. Detail means more precision per point, not more points."),
}

BUDGET_BLOCK = """The transcript is about {source_words} words long.
Your notes must be about {target_words} words, and MUST NOT be longer than {target_words} words. The budget is a hard limit.

Work in two steps.

STEP 1 - inside <plan> tags, choose your sections and give each one a word budget. One line per section, the numbers adding up to about {target_words}. Keep it under 10 lines. This block is not part of the notes and will be removed.

STEP 2 - write the notes after the plan, starting at the `#` title, staying inside your budget.

<plan>
...
</plan>

SHAPE OF THE DOCUMENT
- one `##` section per topic, in the order the lecture covered them;
- a table of the technical terms with their definition, where the lecture defined enough of them;
- a final `##` section, titled in {language_name}, for what the lecturer left unfinished or promised for later. Drop it entirely if there is nothing to put in it.
"""

USER_PROMPT = """{style_hint}

{context_block}{budget_block}
<transcript>
{transcript}
</transcript>"""


# ── Utility ──────────────────────────────────────────────────────────────────
def estimate_cost(usage: dict) -> float:
    """Costo stimato in USD alle tariffe DeepSeek off-peak."""
    return round(
        usage.get("hit", 0) * DEEPSEEK_RATES["cache_hit"] / 1e6
        + usage.get("miss", 0) * DEEPSEEK_RATES["cache_miss"] / 1e6
        + usage.get("completion", 0) * DEEPSEEK_RATES["output"] / 1e6,
        6,
    )


def target_words(source_words: int, style: str) -> int:
    """
    Quante parole deve avere il riassunto, dato quanto è lunga la trascrizione.

    Il rapporto dipende dallo stile e il risultato è limitato da una soglia
    minima e da un tetto: sotto il minimo non è un riassunto, sopra il tetto non
    è più consultabile durante lo studio.
    """
    shape = STYLE_SHAPE.get(style, STYLE_SHAPE["study"])
    return max(shape["floor"], min(shape["cap"], round(source_words * shape["ratio"])))


def source_word_count(segments: list[dict], text: str = "") -> int:
    """Parole della trascrizione: la base su cui si calcola il budget."""
    words = sum(len((s.get("text") or "").split()) for s in segments)
    return words or len((text or "").split())


_PLAN_RE = re.compile(r"<plan>.*?</plan>\s*", re.S | re.I)


def strip_plan(text: str) -> str:
    """
    Toglie il blocco di pianificazione: il documento comincia al primo titolo.

    Il modello pianifica i budget per sezione prima di scrivere — è il meccanismo
    che tiene il documento dentro la lunghezza — ma quel blocco non fa parte
    delle note. Si toglie il `<plan>...</plan>` chiuso; se non è chiuso si taglia
    comunque tutto ciò che precede il titolo, insieme a qualsiasi preambolo
    ("Ecco le note:"), che non vogliamo comunque.
    """
    text = _PLAN_RE.sub("", text or "")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("# "):
            return "\n".join(lines[index:]).strip()
    return text.strip()


def _language_name(code: str | None) -> str:
    """Nome leggibile della lingua: un codice ISO in un'istruzione è ambiguo."""
    table = {
        "it": "Italian", "en": "English", "fr": "French", "de": "German",
        "es": "Spanish", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
        "zh": "Chinese", "ja": "Japanese", "ar": "Arabic",
    }
    base = (code or "").split("-")[0].lower()
    if base in table:
        return table[base]
    return f"the same language as the transcript ({code})" if code else "the same language as the transcript"


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?\s*```\s*$", re.S)


def unwrap(text: str) -> str:
    """
    Toglie la recinzione con cui il modello a volte avvolge l'intero documento.

    Capita spesso con ` ```markdown ... ``` `: è una recinzione attorno a tutto,
    non un blocco di codice del documento, e senza toglierla il markdown
    arriverebbe all'utente come un unico blocco di codice.
    """
    text = (text or "").strip()
    match = _FENCE_RE.match(text)
    if match and "\n## " in "\n" + match.group(1):
        return match.group(1).strip()
    return text


def unwrap_json(text: str) -> str:
    """
    Ripiego: alcuni modelli rispondono `{"markdown": "..."}` anche senza JSON mode.

    Se il testo è un oggetto JSON con un campo di testo lo si usa; altrimenti il
    testo resta com'è. Non è una convalida, è solo non perdere il lavoro.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return stripped
    try:
        payload = json.loads(stripped)
    except ValueError:
        return stripped
    if not isinstance(payload, dict):
        return stripped
    for key in ("markdown", "documento", "document", "notes", "appunti", "text", "testo"):
        value = payload.get(key)
        if isinstance(value, str) and len(value.strip()) > 40:
            return value.strip()
    return stripped


def title_from_markdown(markdown: str, fallback: str = "") -> str:
    """Il titolo è il primo `#` del documento: una sola fonte di verità, non due."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()[:200]
    return fallback or "Summary"


def word_count(markdown: str) -> int:
    return len(re.sub(r"[#*`>|\-]", " ", markdown).split())


# ── Contesto del corso ───────────────────────────────────────────────────────
def _context_block(project: dict | None, previous: list[dict], glossary: list[dict]) -> str:
    """
    Contesto del corso: aiuta a interpretare i riferimenti, non ad aggiungere contenuto.

    L'istruzione esplicita è necessaria: senza, il modello usa il riassunto della
    lezione precedente come se fosse materiale di questa.
    """
    if not project and not previous and not glossary:
        return ""
    parts = ["<course_context>"]
    if project:
        if project.get("name"):
            parts.append(f"Course: {project['name']}")
        if project.get("code"):
            parts.append(f"Code: {project['code']}")
        if project.get("description"):
            parts.append(f"Course description: {str(project['description'])[:600]}")
    if glossary:
        terms = "; ".join(f"{t['find']} -> {t['replace']}" for t in glossary[:60])
        parts.append(f"Course glossary (use these exact spellings): {terms}")
    if previous:
        parts.append("Previous lectures of this course (for interpreting references only):")
        for prev in previous[:5]:
            snippet = re.sub(r"\s+", " ", str(prev.get("overview") or ""))[:300]
            parts.append(f"- {prev.get('title') or 'lecture'}: {snippet}")
    parts.append(
        "The course context is ONLY there to help you interpret what the lecturer refers to "
        "(for example \"as we saw last time\"). Do not add any content that is not in THIS "
        "lecture's transcript."
    )
    parts.append("</course_context>\n")
    return "\n".join(parts) + "\n"


def _previous_overviews(transcript: dict) -> list[dict]:
    """Gli ultimi riassunti delle altre lezioni dello stesso corso."""
    project_id = transcript.get("project_id")
    if not project_id:
        return []
    try:
        return db.rows(
            "SELECT s.title, s.overview, s.created_at FROM summaries s "
            "JOIN transcripts t ON t.id = s.transcript_id "
            "WHERE t.project_id = ? AND s.transcript_id != ? "
            "ORDER BY s.created_at DESC LIMIT 5",
            (int(project_id), int(transcript.get("id") or 0)),
        )
    except Exception:  # noqa: BLE001 - il contesto è opzionale, non deve rompere il job
        log.warning("contesto lezioni precedenti non disponibile", exc_info=True)
        return []


# ── Pipeline ─────────────────────────────────────────────────────────────────
def _ask(provider: dict, system: str, user: str, max_tokens: int) -> tuple[str, dict]:
    """
    Una chiamata in modalità testo.

    `json_mode=False`: qui si vuole markdown, e forzare il JSON produrrebbe il
    documento con gli a capo escapati dentro una stringa.

    Qui si controlla solo che il modello abbia scritto *qualcosa*: quanto debba
    essere lungo dipende dalla fase (gli appunti di un blocco sono corti per
    natura), quindi la verifica di consistenza del documento sta in `summarize`.
    """
    model = (provider.get("model") or "").strip()
    if not model:
        raise RuntimeError(f"No model configured for provider '{provider.get('name')}'.")
    content, diag = llm.chat_completion(provider, model, system, user,
                                        max_tokens=max_tokens, temperature=0.2,
                                        json_mode=False)
    text = unwrap_json(unwrap(content))
    if not text.strip():
        raise ValueError(f"The model returned nothing. {llm._error_detail(diag)}")
    if diag.get("finish_reason") == "length":
        # Il documento è tagliato: meglio dirlo che consegnarne metà in silenzio.
        log.warning("riassunto: risposta tagliata al tetto di %d token", max_tokens)
    return text, diag


# Sotto questa lunghezza non è un documento: è una risposta degenere (un rifiuto,
# un "non posso", un titolo e basta). Una lezione, anche riassunta breve, sta
# abbondantemente sopra.
MIN_DOCUMENT_CHARS = 200


def summarize(provider: dict, transcript: dict, settings: "config.Settings", *,
              style: str = "study", instructions: str = "",
              on_progress=None) -> dict:
    """
    Genera il documento di studio di una trascrizione.

    `on_progress(fraction, message)` è opzionale e serve solo alla barra della UI.
    """
    segments = transcript.get("segments") or []
    if not segments:
        from .textutil import text_to_segments
        segments = text_to_segments(transcript.get("text") or "")
    if not segments:
        raise ValueError("This transcript is empty")

    language = transcript.get("language") or settings.summary_language or "auto"
    style = style if style in STYLES else "study"
    context = _context_block(
        db.get_project(int(transcript["project_id"])) if transcript.get("project_id") else None,
        _previous_overviews(transcript),
        db.list_terms(int(transcript["project_id"])) if transcript.get("project_id") else [],
    )
    text = transcript.get("text") or ""

    # Il budget di parole è la leva principale sulla lunghezza del risultato:
    # si calcola dalla trascrizione vera, non si lascia decidere al modello.
    words_in_source = source_word_count(segments, text)
    words_wanted = target_words(words_in_source, style)
    common = {
        "style_hint": STYLE_HINT[style],
        "context_block": context,
        "language_name": _language_name(language),
        "source_words": words_in_source,
        "target_words": words_wanted,
        "budget_block": BUDGET_BLOCK.format(source_words=words_in_source,
                                            target_words=words_wanted,
                                            language_name=_language_name(language)),
    }
    if instructions:
        common["style_hint"] += f"\n\nAdditional instructions from the student:\n{instructions}"

    system = SYSTEM_PROMPT.format(language_name=common["language_name"])
    usage = {"prompt": 0, "completion": 0, "hit": 0, "miss": 0}
    # Tetto di output proporzionato all'obiettivo. Il valore assoluto delle
    # impostazioni resta come massimo, ma un tetto molto più alto dell'obiettivo
    # invita a superarlo: 8000 token sono ~6000 parole, ed è esattamente la
    # lunghezza che il modello raggiungeva quando non aveva un numero.
    # Si tiene comunque un margine ampio per non troncare a metà frase.
    cap = min(int(settings.summary_max_tokens or 8000), words_wanted * 3 + 600)
    t0 = time.time()

    if on_progress:
        on_progress(0.15, f"asking {provider.get('model')} for ~{words_wanted} words")
    user = USER_PROMPT.format(transcript=_plain_text(segments), **common)
    markdown, diag = _ask(provider, system, user, cap)
    _add_usage(usage, diag)

    markdown = strip_plan(markdown)
    if len(markdown) < MIN_DOCUMENT_CHARS:
        raise ValueError(
            f"The model returned no usable document ({len(markdown)} characters). "
            f"{llm._error_detail(diag)}"
        )

    written = word_count(markdown)
    # Un superamento netto non si corregge troncando (si perderebbe la coda del
    # documento): si segnala, così è visibile che il prompt non ha tenuto.
    if written > words_wanted * 1.5:
        log.warning("riassunto: %d parole scritte contro un obiettivo di %d",
                    written, words_wanted)

    if on_progress:
        on_progress(1.0, f"{written} words")

    return {
        "markdown": markdown,
        "title": title_from_markdown(markdown, transcript.get("title") or ""),
        "overview": _overview(markdown),
        "style": style,
        "language": language,
        "provider": provider.get("name") or "",
        "model": provider.get("model") or "",
        "words": written,
        "target_words": words_wanted,
        "source_words": words_in_source,
        "tokens_in": usage["prompt"],
        "tokens_out": usage["completion"],
        "cache_hit": usage["hit"],
        "cost_usd": estimate_cost(usage),
        "elapsed_s": round(time.time() - t0, 1),
    }


def _add_usage(total: dict, diag: dict) -> None:
    counts = llm.token_usage(diag)
    for key in ("prompt", "completion", "hit", "miss"):
        total[key] += counts[key]


def _overview(markdown: str, limit: int = 400) -> str:
    """
    Il primo paragrafo dopo il titolo, per l'anteprima nella lista e per dare
    contesto alla lezione successiva dello stesso corso.
    """
    lines = markdown.splitlines()
    body: list[str] = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not started:
            if stripped.startswith("# "):
                started = True
            continue
        if stripped.startswith("#"):
            break
        if stripped:
            body.append(stripped)
        elif body:
            break
    return re.sub(r"[*`]", "", " ".join(body))[:limit]


def _plain_text(segments: list[dict]) -> str:
    """
    La trascrizione senza timestamp.

    Per scrivere note leggibili i `[mm:ss]` non servono, e lasciarli nel prompt
    porta il modello a ricopiarli nel documento.
    """
    return "\n".join((s.get("text") or "").strip() for s in segments if (s.get("text") or "").strip())
