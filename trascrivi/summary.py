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

# La stima del costo sta in `llm` perché serve anche all'agente di correzione:
# vive accanto alle tariffe e a `token_usage`, che è la sua unica dipendenza.
estimate_cost = llm.estimate_cost

STYLES = ("study", "brief", "detailed")

# Quanto deve essere lungo il riassunto.
#
# Il numero va dato esplicito. Senza, il modello scrive finché decide di aver
# finito, e il risultato è lungo quanto la lezione: misurato su una lezione reale,
# 6338 parole di note su 7000 parole di trascrizione, con 7601 token di output su
# un tetto di 8000. Non era un tetto troppo basso, era l'assenza di un obiettivo.
#
# Ma un budget in PAROLE non basta, ed è la parte che non funzionava: il modello
# non sa contare le parole. Sugli 11 riassunti in `data/trascrivi.db` veniva
# superato di ~1.37x in modo sistematico e identico nei due stili (study
# 1.11-1.60x, brief 1.21-1.52x), come ci si aspetta da modelli addestrati a
# sovra-generare (Plan-and-Write, KDD 2025, arXiv:2511.01807). Sa contare sezioni
# e bullet, quindi il budget si esprime come struttura:
#
#     argomenti = parole sorgente / 1200        (5..12)
#     sezioni   = argomenti + 2                 (tabella termini + sospeso)
#     sezione   = riga di apertura (25 parole) + bullet x 22 parole
#     obiettivo = sezioni x parole per sezione
#
# Il numero che cresce con la trascrizione è quello degli ARGOMENTI: una lezione
# da 1h30 ne ha una decina distinti, qualunque sia lo stile. Le due sezioni di
# servizio si AGGIUNGONO. Ricavarle sottraendole dal totale toglieva un argomento
# a ogni lezione — con 10 argomenti ne restavano 9 — ed è così che sparivano
# Mokyr e il blocco sulla corrente alternata. I tre stili diventano un solo
# parametro: `STYLE_BULLETS`, cioè 69 / 113 / 179 parole per sezione.
#
# I vecchi `ratio`/`floor`/`cap` non erano solo tarati male: il tetto scattava
# prima del rapporto (brief a 7500 parole sorgente, study a 13300), quindi ogni
# lezione reale — 11 200-21 700 parole — produceva un brief da ~620 parole e uno
# study da ~2500 qualunque fosse la sua lunghezza.
#
# La lunghezza resta un bersaglio, non una garanzia: sul codice attuale lo scarto
# misurato va da 0.76x a 1.66x del budget (mediana ~1.05x), con qualche run che
# sfora ancora del 50%. Il vecchio prompt aveva la stessa dispersione, attorno a
# una media più alta.
TOPIC_SOURCE_WORDS = 1200
TOPIC_SECTIONS_MIN, TOPIC_SECTIONS_MAX = 5, 12
RESERVED_SECTIONS = 2
LEAD_WORDS, BULLET_WORDS = 25, 22
STYLE_BULLETS = {"brief": 2, "study": 4, "detailed": 7}

# Il piano non si vede — `strip_plan` lo toglie dal documento — ma si paga, ed è
# l'unico pezzo di output che non finisce in mano al lettore. Senza un margine per
# lui il modello scrive il piano e si fa tagliare le note a metà: misurato con il
# piano a due liste (elenco dei nomi + sezioni), note troncate a 8 sezioni su 11,
# ~1500 parole di piano su un tetto di 2877 token. Il limite di righe nel prompt lo
# tiene piccolo; questo margine evita che una sua crescita si mangi il documento.
PLAN_TOKENS = 1200

SYSTEM_PROMPT = """You write study notes from university lecture transcripts, in Markdown.

The transcript comes from automatic speech recognition: expect recognition errors, false starts, repetitions and missing punctuation. Read through them.

A summary is a selection, not a transcript. Deciding what to leave out is the job; keeping everything is not being thorough, it is failing the task. Cutting too deep is the other way to fail: a concept the lecturer named and you dropped is a hole the reader walks into while revising.

RULES
1. Use only what the transcript says. You have no other knowledge of the subject: never add facts, definitions, examples or numbers that are not in the transcript, even if you are sure they are correct.
2. Write the notes in {language_name}.
3. Stay inside the budget given in the request. Going over it is a failure, not a detail to fix later.
4. Name everything the lecturer named. Every theory, author, term, distinction, model, mechanism, case or process that the lecture gives a name to must appear in the notes, with one line saying what it is. Completeness is counted in names, not in words: a reader who cannot find a name they heard in the lecture stops trusting the notes.
5. One layer of depth per concept, not a story. For each concept: what it is, then why or how in one or two sentences. Keep the reasoning that connects the concepts; drop the narrative that leads up to it.
6. At most one example per concept, and only when the concept stays abstract without it. If three cases make the same point, keep the one that carries it and drop the others. A named case the lecturer dwells on — a technology, a lawsuit, a company, an experiment — is content, not colour: keep it in one bullet.
7. Prefer lists and short paragraphs to long blocks of prose. Bold the term being defined, not whole sentences.
8. Use a Markdown table whenever the lecture compares things, lists options, or classifies items by attributes — and for the terms of the lecture with their definition, where the lecture defined enough of them.
9. Use a diagram in a fenced code block only where the lecture describes a process, a flow or an architecture that is clearer as a picture. Two or three in a whole lecture at most.
10. Keep the lecturer's technical terms, acronyms and symbols exactly as they were said. Do not expand an acronym they did not expand, and do not rename anything.
11. Keep their uncertainty. "probably", "I think", "in general", "it seems" must not become statements of fact.

ALWAYS LEAVE OUT
- housekeeping and logistics: timetables, breaks, exam dates, recordings, who sits where, administrative remarks;
- anything about the course rather than its subject: grading, exam rules, project work, attendance, the colleague who teaches a module, where the slides are. Rule 4 is about what the lecture teaches, not about how the course is run — however much of the hour the lecturer spends on it;
- the lecturer's scaffolding: "we will see this later", "for now this is qualitative", "let me repeat that", "as I was saying";
- repetition: the same concept stated three times is one bullet, not three;
- the second and third example of a point already made;
- the texture of spoken language: digressions, asides, jokes, audience questions that lead nowhere;
- people who appear only as colour — the student who asked, the colleague who mentioned something, the company in an aside — unless the lecture uses them to make a technical point.

Write no preamble, no closing remarks, and no commentary about the transcript itself."""

STYLE_HINT = {
    "study": ("Write study notes for revision: every concept the lecture names, each one explained "
              "in a line or two, with the reasoning that connects them. Assume the reader has "
              "followed the lecture once and wants to go back over it quickly."),
    # Il brief non dice più "drop the detail and the examples": era la frase che
    # faceva perdere interi argomenti (sulla Lezione 1 il brief copriva 21 dei 32
    # elementi nominati, contro i 32 del nuovo stile `study`). Meno bullet, non
    # meno concetti.
    "brief": ("Write a short summary: every concept the lecture names, one line each, with no "
              "elaboration and no examples. The reader wants the map, not the territory."),
    "detailed": ("Write thorough notes: for each concept keep the argument, the numbers and the "
                 "caveats. Detail means more precision per point, not more points."),
}

BUDGET_BLOCK = """The transcript is about {source_words} words long.
Write {sections} sections, {target_words} words in total: no fewer than {min_words} and no more than {max_words}. Both ends are a limit. Falling short means you dropped concepts; going over means you kept detail that does not earn its place.

Work in two steps.

STEP 1 - inside <plan> tags, work in two lists.

First the names: every theory, author, term, case, model, mechanism and process this lecture gives a name to, one per line. This is the completeness checklist, and it is judged on names, not on words.

Then the sections: one line each, saying which names from that list the section will carry. Exactly {sections} sections, and no name left unassigned. If a name has nowhere to go, it means two topics were merged that should be separate — split them rather than dropping the name. Keep the whole block under 25 lines: the plan is removed before delivery, but it is paid for, and a plan that runs long eats the notes.

<plan>
NAMES
- ...
SECTIONS
1. Section title - {bullets_per_section} bullets - names: ...
</plan>

STEP 2 - write the notes after the plan, starting at the `#` title. Every name in your checklist must appear in the notes.

HOW TO HIT THE LENGTH
- exactly {sections} `##` sections in total, and the count includes all three kinds:
  - {topic_sections} sections on the topics of the lecture, in the order it covered them;
  - one section holding the table of the terms of the lecture with their definition. This one is not optional and it is not a topic section: every lecture names at least a handful of terms, and this table is the part a reader scans before an exam. It holds a table, not bullets — one row per term, as many rows as the lecture defined;
  - one last section, if the lecture deferred anything, for what was promised or left unresolved. This one holds only the deferral: the last topic of the lecture belongs to its own section above, not here;
- every title is different: two sections that would carry the same title are one section;
- each topic section: one opening line saying what the topic is (at most {lead_words} words), then {bullets_per_section} bullets. Never more than {bullets_per_section}, never fewer than {bullets_per_section} — a section that cannot fill them is not a section;
- one bullet carries one idea and at most {bullet_words} words: count them. A bullet that needs a second sentence is two bullets, not a longer bullet. Several names that belong together can share one bullet;
- if a section runs long, merge bullets; never drop a named concept to save words. If the whole document runs long, the detail to cut is the example, never the concept.
"""

USER_PROMPT = """{style_hint}

{context_block}{budget_block}
<transcript>
{transcript}
</transcript>"""


# ── Utility ──────────────────────────────────────────────────────────────────
def shape(source_words: int, style: str) -> dict:
    """
    Sezioni, bullet e parole: il budget che finisce nel prompt.

    Le sezioni di argomento crescono con la trascrizione; tabella dei termini e
    "cosa resta in sospeso" sono due sezioni in più, prenotate. Senza prenotarle
    il modello le sacrifica quando il budget stringe — misurato: la tabella c'è
    con 9 sezioni e sparisce con 11, a parità di prompt.
    """
    topics = max(TOPIC_SECTIONS_MIN,
                 min(TOPIC_SECTIONS_MAX, round(source_words / TOPIC_SOURCE_WORDS)))
    sections = topics + RESERVED_SECTIONS
    bullets = STYLE_BULLETS.get(style, STYLE_BULLETS["study"])
    per_section = LEAD_WORDS + bullets * BULLET_WORDS
    target = sections * per_section
    return {
        "sections": sections,
        "topic_sections": topics,
        "bullets_per_section": bullets,
        "lead_words": LEAD_WORDS,
        "bullet_words": BULLET_WORDS,
        "target_words": target,
        # L'intervallo è stretto di proposito: è quello che il modello ha
        # rispettato, mentre un "about N words" veniva superato di un terzo.
        "min_words": round(target * 0.85),
        "max_words": round(target * 1.15),
    }


def target_words(source_words: int, style: str) -> int:
    """Quante parole deve avere il riassunto, data la lunghezza della trascrizione."""
    return shape(source_words, style)["target_words"]


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
    # `shape` porta anche `target_words`, min e max: il budget del prompt e la
    # lunghezza attesa escono dalla stessa funzione, quindi non possono divergere.
    shape_for_prompt = shape(words_in_source, style)
    language_name = _language_name(language)
    common = {
        "style_hint": STYLE_HINT[style],
        "context_block": context,
        "language_name": language_name,
        "source_words": words_in_source,
        "target_words": words_wanted,
        "budget_block": BUDGET_BLOCK.format(source_words=words_in_source,
                                            language_name=language_name,
                                            **shape_for_prompt),
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
    #
    # Il piano va pagato ma non si vede: `strip_plan` lo toglie dal documento, e
    # senza un margine per lui il modello scrive il piano e si fa tagliare le note
    # a metà. Vedi `PLAN_TOKENS`.
    cap = min(int(settings.summary_max_tokens or 8000),
              words_wanted * 3 + 600 + PLAN_TOKENS)
    t0 = time.time()

    if on_progress:
        on_progress(0.15, f"asking {provider.get('model')} for ~{words_wanted} words")
    user = USER_PROMPT.format(transcript=_plain_text(segments), **common)
    markdown, diag = _ask(provider, system, user, cap)
    llm.add_usage(usage, diag)

    markdown = strip_plan(markdown)
    if len(markdown) < MIN_DOCUMENT_CHARS:
        raise ValueError(
            f"The model returned no usable document ({len(markdown)} characters). "
            f"{llm._error_detail(diag)}"
        )
    if diag.get("finish_reason") == "length":
        # Il tetto ha tagliato il documento: il costo è già stato pagato e il
        # testo che c'è è valido, quindi non si butta via niente. Ma il taglio
        # deve arrivare all'utente, altrimenti consegniamo metà documento come se
        # fosse finito (il caso concreto è lo stile `detailed`: l'obiettivo di
        # parole è tarato sul tetto di output, e su una lezione lunga ci arriva).
        truncated = True
    else:
        truncated = False

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
        "cost_usd": estimate_cost(usage) or 0.0,
        "elapsed_s": round(time.time() - t0, 1),
        "truncated": truncated,
        # L'usage cumulato resta disponibile per chi salva il job: `jobs._run_job`
        # ci ricava i campi di costo con lo stesso `llm.usage_counts` dell'agente.
        "usage": usage,
    }


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
