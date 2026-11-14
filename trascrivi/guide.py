"""
trascrivi/guide.py
==================
La guida d'esame di un corso: un documento che mette insieme tutte le lezioni.

Il riassunto risponde a "cosa ha detto in questa lezione"; la guida risponde a
"cosa devo sapere per l'esame". Gli ingressi sono già condensati — i riassunti
di ogni lezione (~1200 parole l'uno) e, se ci sono, il testo degli esami degli
anni passati — quindi una sola chiamata basta anche per un corso intero: 15
lezioni sono ~25k token d'ingresso, meno di un centesimo.

Con gli esami la guida fa la cosa che uno studente fa a mano la sera prima:
per ogni domanda uscita negli anni passati, dove se ne parla nelle lezioni e
una traccia di risposta. Le risposte vengono dai riassunti, non dalla
conoscenza del modello: se un argomento d'esame non è nelle lezioni registrate
la guida lo dice, invece di inventare.
"""

from __future__ import annotations

import logging
import time

from . import db, llm
from .summary import _language_name, title_from_markdown, unwrap, word_count

log = logging.getLogger("trascrivi.guide")

# Tetti d'ingresso: un corso lungo resta comunque dentro una chiamata.
LECTURE_CHARS = 9000       # per riassunto: un riassunto "study" ne usa ~7000
EXAM_CHARS = 40000         # tutti gli esami insieme
MAX_TOKENS = 9000

SYSTEM_PROMPT = """You prepare students for a university exam. You receive the study notes of every recorded lecture of a course, labelled L1, L2, ..., and possibly the text of exams from previous years (OCR from scans: expect broken lines and misread symbols).

Write ONE exam-preparation guide in Markdown, in {language}.

RULES
1. Content comes from the lecture notes. Never add facts that are not in them. If an exam question is about something the notes do not cover, say so plainly ("not covered in the recorded lectures") instead of answering from general knowledge.
2. Every concept and every answer carries the lectures it comes from, as (L3) or (L3, L7). The student uses these to go back to the right notes.
3. Be dense: bullets, short definitions, tables where things are compared. No preamble, no closing remarks.

STRUCTURE
# {title}
## Course map
A table: lecture | main topics (one line each).
## Core concepts
The ideas the whole course rests on, grouped by theme rather than by lecture, each with a one- or two-line explanation and its lecture references. Show how concepts from different lectures connect.
{exam_sections}
## Checklist
A list of 10-20 things the student must be able to do or explain at the exam, each with lecture references."""

EXAM_SECTIONS = """## Past exam questions
For EVERY question found in the past exams, in order: the question (shortened if long), the lectures where it is covered, and an answer outline of 2-5 bullets built from the notes. Group identical or near-identical questions that recur across years and say how often they appear.
## What the exams ask most
A table: topic | how many times it appears in the past exams | lectures. Then two lines on the pattern (e.g. definitions vs exercises, which parts of the course weigh most)."""

NO_EXAM_SECTIONS = """## Likely exam questions
10-15 questions a lecturer of this course would plausibly ask, from the topics the lectures stress most. For each: the lectures it draws on and an answer outline of 2-4 bullets built from the notes."""


def lectures(project_id: int) -> list[dict]:
    """
    Le lezioni del corso con il loro ultimo riassunto, nell'ordine in cui sono
    state fatte. `label` (L1, L2, …) è il numero che la lezione ha nella tabella
    del corso, anche se qualche lezione non ha ancora il riassunto.
    """
    numbers = db.lecture_numbers(project_id)
    out = db.rows(
        """SELECT t.id, t.title, t.language, s.title AS summary_title, s.markdown
           FROM transcripts t
           JOIN summaries s ON s.id = (SELECT MAX(id) FROM summaries WHERE transcript_id = t.id)
           WHERE t.project_id = ?
           ORDER BY t.created_at, t.id""",
        (int(project_id),),
    )
    for row in out:
        row["label"] = f"L{numbers.get(int(row['id']), 0)}"
    return out


def exam_pages(project_id: int) -> list[dict]:
    return db.rows(
        """SELECT d.name AS doc_name, p.page_no, p.text, p.vlm_md
           FROM slide_pages p JOIN slide_docs d ON d.id = p.doc_id
           WHERE d.project_id = ? AND d.kind = 'exam' AND d.status = 'ready'
           ORDER BY d.name, p.page_no""",
        (int(project_id),),
    )


def _language(project_id: int, rows: list[dict], settings) -> str:
    if getattr(settings, "summary_language", ""):
        return settings.summary_language
    langs = [r.get("language") for r in rows if r.get("language")]
    return max(set(langs), key=langs.count) if langs else "en"


def build_prompt(project: dict, rows: list[dict], exams: list[dict], language: str) -> tuple[str, str]:
    title = f"Exam guide — {project.get('name') or 'Course'}"
    system = SYSTEM_PROMPT.format(language=_language_name(language), title=title,
                                  exam_sections=EXAM_SECTIONS if exams else NO_EXAM_SECTIONS)
    parts = []
    for lec in rows:
        name = lec.get("summary_title") or lec.get("title") or lec["label"]
        parts.append(f'<lecture id="{lec["label"]}" title="{name}">\n'
                     f"{(lec.get('markdown') or '')[:LECTURE_CHARS]}\n</lecture>")
    user = "\n\n".join(parts)
    if exams:
        text, used = [], 0
        for page in exams:
            body = ((page.get("vlm_md") or "").strip() or (page.get("text") or "").strip())
            chunk = f"[{page['doc_name']} p.{page['page_no']}]\n{body}"
            if used + len(chunk) > EXAM_CHARS:
                break
            text.append(chunk)
            used += len(chunk)
        user += "\n\n<past_exams>\n" + "\n\n".join(text) + "\n</past_exams>"
    return system, user


def generate(provider: dict, project_id: int, settings, *, instructions: str = "",
             on_progress=None) -> dict:
    project = db.get_project(int(project_id))
    if not project:
        raise ValueError("Course not found")
    rows = lectures(project_id)
    if not rows:
        raise ValueError("No lecture of this course has a summary yet: the guide is built from them")
    exams = exam_pages(project_id)
    language = _language(project_id, rows, settings)
    system, user = build_prompt(project, rows, exams, language)
    if instructions:
        user += f"\n\nAdditional instructions from the student:\n{instructions}"

    if on_progress:
        on_progress(0.15, f"{len(rows)} lectures, {len(exams)} exam pages → {provider.get('model')}")
    t0 = time.time()
    content, diag = llm.chat_completion(provider, provider.get("model") or "", system, user,
                                        max_tokens=MAX_TOKENS, temperature=0.2, json_mode=False)
    markdown = unwrap(content or "")
    usage = {"prompt": 0, "completion": 0, "hit": 0, "miss": 0}
    llm.add_usage(usage, diag)
    if len(markdown) < 200:
        # La chiamata è stata pagata anche se il risultato non serve: il costo
        # viaggia con l'eccezione e `_run_job` lo salva sul job fallito.
        exc = ValueError(f"The model returned no usable guide. {llm._error_detail(diag)}")
        exc.usage = usage
        raise exc
    counts = llm.usage_counts(usage)
    guide_id = db.execute(
        """INSERT INTO guides(project_id, title, markdown, provider, model, n_lectures,
                              n_exam_pages, tokens_in, tokens_out, cache_hit, cost_usd, created_at,
                              labels)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (int(project_id), title_from_markdown(markdown, f"Exam guide — {project['name']}"),
         markdown, provider.get("name") or "", provider.get("model") or "", len(rows),
         len(exams), counts["tokens_in"], counts["tokens_out"], counts["cache_hit"],
         counts["cost_usd"], db.now(),
         db.jdump({r["label"]: {"transcript_id": int(r["id"]),
                                "title": r.get("summary_title") or r["title"]} for r in rows})),
    )
    truncated = diag.get("finish_reason") == "length"
    message = (f"{word_count(markdown)} words from {len(rows)} lectures"
               f"{f' and {len(exams)} exam pages' if exams else ''} . ${counts['cost_usd']:.4f}"
               f"{' . TRUNCATED at the token limit' if truncated else ''}")
    log.info("guida corso %s: %s in %.1fs", project_id, message, time.time() - t0)
    return {"guide_id": guide_id, "message": message, "usage": usage}
