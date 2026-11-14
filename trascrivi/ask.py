"""
trascrivi/ask.py
================
"Chiedi al corso": una domanda, una risposta con le fonti (lezione e minuto).

Due livelli di contesto, scelti per costo e cache:

* **tutti i riassunti del corso**, sempre, nel messaggio di sistema. Sono già
  condensati (~25k token per 15 lezioni) e restano identici da una domanda
  all'altra: DeepSeek serve un prefisso ripetuto dalla cache a 1/50 del prezzo,
  quindi dalla seconda domanda in poi pagano quasi solo la risposta.
* **i passaggi di trascrizione più vicini alla domanda** (TF-IDF locale su
  finestre di ~1000 caratteri), con il minutaggio: servono a citare cosa ha
  detto il docente e dove riascoltarlo.

Nessun embedding, nessun Ollama: il recupero è lessicale e gira ovunque.
"""

from __future__ import annotations

import json
import logging

from . import db, llm
from .guide import LECTURE_CHARS, lectures
from .matching import rank_lexical
from .textutil import fmt_ts

log = logging.getLogger("trascrivi.ask")

WINDOW_CHARS = 1000
PASSAGES = 8
MAX_TOKENS = 1500

SYSTEM_PROMPT = """You answer a student's questions about a university course, using only the course material below: the study notes of each lecture (labelled L1, L2, ...) and, in the question message, passages of the lecture transcripts with their timestamps.

RULES
- Answer in the language of the question. Be direct and complete, like a good teaching assistant: definitions, the reasoning, and the formula or example the lecture gives, in Markdown.
- Cite where each point comes from: [L3] for a lecture, [L3 12:40] when it comes from a transcript passage. The student uses the citations to go back to the recording.
- Use only the material. If the course does not cover the question, say so in one sentence and point to the closest topic that is covered. Never fill a gap with general knowledge.
- The transcripts come from speech recognition: read through misheard words.

<course_notes>
{notes}
</course_notes>"""


def _windows(transcript: dict, label: str) -> list[dict]:
    """Finestre consecutive di segmenti, con l'istante di inizio."""
    segments = db.jload(transcript.get("segments"), []) or []
    out, text, start = [], "", None
    for seg in segments:
        if start is None:
            start = float(seg.get("start") or 0.0)
        text += " " + (seg.get("text") or "").strip()
        if len(text) >= WINDOW_CHARS:
            out.append({"transcript_id": transcript["id"], "label": label,
                        "title": transcript["title"], "start": start, "text": text.strip()})
            text, start = "", None
    if text.strip():
        out.append({"transcript_id": transcript["id"], "label": label,
                    "title": transcript["title"], "start": start or 0.0, "text": text.strip()})
    return out


def answer(provider: dict, project_id: int, question: str) -> dict:
    question = (question or "").strip()
    if not question:
        raise ValueError("Empty question")
    rows = lectures(project_id)
    if not rows:
        raise ValueError("No lecture of this course has a summary yet")
    labels = {int(r["id"]): r["label"] for r in rows}
    notes = "\n\n".join(
        f'<lecture id="{labels[int(r["id"])]}" title="{r.get("summary_title") or r["title"]}">\n'
        f"{(r.get('markdown') or '')[:LECTURE_CHARS]}\n</lecture>" for r in rows)

    windows = []
    for t in db.rows("SELECT id, title, segments FROM transcripts WHERE project_id = ?",
                     (int(project_id),)):
        if int(t["id"]) in labels:
            windows.extend(_windows(t, labels[int(t["id"])]))
    passages = [w for score, w in rank_lexical(question, windows)[:PASSAGES] if score > 0.02]
    passages.sort(key=lambda w: (int(w["label"][1:]), w["start"]))

    user = ""
    if passages:
        user += "<transcript_passages>\n" + "\n\n".join(
            f"[{w['label']} {fmt_ts(w['start'])}] {w['text']}" for w in passages
        ) + "\n</transcript_passages>\n\n"
    user += f"Question: {question}"

    content, diag = llm.chat_completion(provider, provider.get("model") or "",
                                        SYSTEM_PROMPT.format(notes=notes), user,
                                        max_tokens=MAX_TOKENS, temperature=0.2, json_mode=False)
    if not (content or "").strip():
        raise ValueError(f"The model returned no answer. {llm._error_detail(diag)}")
    usage = {"prompt": 0, "completion": 0, "hit": 0, "miss": 0}
    llm.add_usage(usage, diag)
    counts = llm.usage_counts(usage)
    sources = [{"label": labels[int(r["id"])], "transcript_id": int(r["id"]),
                "title": r.get("summary_title") or r["title"]} for r in rows]
    cited = [{"label": w["label"], "transcript_id": w["transcript_id"], "start": w["start"]}
             for w in passages]
    qid = db.execute(
        """INSERT INTO questions(project_id, question, answer, sources, model, tokens_in,
                                 tokens_out, cache_hit, cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (int(project_id), question, content.strip(),
         json.dumps({"lectures": sources, "passages": cited}),
         provider.get("model") or "", counts["tokens_in"], counts["tokens_out"],
         counts["cache_hit"], counts["cost_usd"], db.now()),
    )
    return get(qid)


def get(question_id: int) -> dict | None:
    row = db.row("SELECT * FROM questions WHERE id = ?", (int(question_id),))
    if row:
        row["sources"] = db.jload(row.get("sources"), {}) or {}
    return row


def history(project_id: int, limit: int = 30) -> list[dict]:
    out = db.rows("SELECT * FROM questions WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                  (int(project_id), int(limit)))
    for row in out:
        row["sources"] = db.jload(row.get("sources"), {}) or {}
    return out
