"""
trascrivi/decks.py
==================
Export dei mazzi per Anki: `.apkg` (con GUID stabili) e `.tsv`.

Due formati perché servono a due cose diverse:

* **`.apkg`** è l'import buono. Il GUID di ogni nota deriva da `cards.uid`, che
  non cambia mai: re-importare un mazzo **aggiorna** le note invece di
  duplicarle, e Anki conserva le sue schedulazioni e le sospensioni fatte lì.
  Gli ID di mazzo e di note type sono fissi per lo stesso motivo — se cambiassero
  a ogni export, Anki creerebbe un mazzo nuovo ogni volta.
* **`.tsv`** è il formato di cortesia, identico a quello dei mazzi in `decks/`:
  due colonne per il cloze (testo + tag) e tre per QA (davanti, retro, tag).

Le immagini dentro le carte non sono un obiettivo di questa versione: una carta
di origine slide cita "(slide N)" nel retro, che è il riferimento testuale.
"""

from __future__ import annotations

import hashlib
import html
import io
import logging
import re
import tempfile
import threading

log = logging.getLogger("trascrivi.decks")

# Tag che identificano la provenienza, come nei mazzi di esempio.
TAG_ROOT = "trascrivi"
# ponytail: HTML minimo e nessun CSS custom, così le note si leggono in qualunque
# tema di Anki; un foglio di stile dedicato si aggiunge quando serve davvero.
BASIC_MODEL_ID = 1_700_101_001
CLOZE_MODEL_ID = 1_700_101_002

BASIC_NAME = "Trascrivi Basic"
CLOZE_NAME = "Trascrivi Cloze"

BASIC_TEMPLATE = """<div class="trascrivi-front">{{Front}}</div>
<hr id="answer">
<div class="trascrivi-back">{{Back}}</div>"""


def deck_name(project: dict | None, transcript: dict | None = None) -> str:
    """
    `trascrivi::<corso>` o `trascrivi::<corso>::<lezione>`.

    I tag di Anki sono gerarchici: con questa forma si può studiare un corso
    intero o una singola lezione senza cambiare i filtri a mano.
    """
    course = _slug(project.get("name") if project else "") or "course"
    name = f"{TAG_ROOT}::{course}"
    if transcript:
        lesson = _slug(transcript.get("title") or "") or f"lecture-{transcript.get('id')}"
        name += f"::{lesson}"
    return name


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in " -_") else " " for c in (text or "").strip()]
    return "-".join("".join(keep).split()).lower()[:60].strip("-")


def deck_id(name: str) -> int:
    """ID stabile ricavato dal nome: stesso mazzo, stesso id, a ogni export."""
    return int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16)


def guid(card: dict) -> str:
    """Il GUID della nota: `uid` immutabile, quindi re-import = aggiornamento."""
    return f"trascrivi-{card['uid']}"


def _back_html(card: dict) -> str:
    """
    Il retro con il riferimento alla slide in coda.

    "(slide 25)" non è decorazione: durante il ripasso è il puntatore per
    ritrovare la figura nel PDF, che è il motivo per cui la carta esiste.
    """
    back = (card.get("back") or "").strip()
    ref = card.get("slide_ref")
    if ref:
        back = f"{back}\n<p class=\"trascrivi-slide\">(slide {html.escape(str(ref))})</p>".strip()
    return back


def _models():
    """I due note type, costruiti a ogni export: sono descrizioni, non stato."""
    import genanki

    basic = genanki.Model(
        BASIC_MODEL_ID, BASIC_NAME,
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[{"name": "Card 1", "qfmt": "{{Front}}", "afmt": BASIC_TEMPLATE}],
    )
    cloze = genanki.Model(
        CLOZE_MODEL_ID, CLOZE_NAME,
        fields=[{"name": "Text"}, {"name": "Back"}],
        templates=[{"name": "Cloze", "qfmt": "{{cloze:Text}}", "afmt": "{{cloze:Text}}"}],
        model_type=genanki.Model.CLOZE,
    )
    return basic, cloze


# `genanki` scrive la collezione in un file temporaneo con `mkstemp` e non lo
# cancella mai: un file da ~50 KB per ogni export, nella cartella temporanea di
# sistema. Quando quella cartella è la working directory (succede: `TEMP` la
# punta lì) il repository si riempie di `tmpXXXXXX`. Qui si dirige `tempfile` in
# una cartella nostra, che sparisce alla fine.
# ponytail: lock globale perché `tempfile.tempdir` è stato di processo; due
# export simultanei si scambierebbero la cartella. L'export dura millisecondi: se
# un giorno diventa lento, la strada è un `tmpdir` passato dal chiamante, non
# togliere il lock.
_build_lock = threading.Lock()


def build_apkg(cards: list[dict], name: str) -> bytes:
    """
    Il mazzo come bytes di un `.apkg` (uno zip con dentro SQLite).

    Non si scrive su disco: il file va servito al browser, ed è l'unico modo per
    non lasciare file temporanei in giro per ogni download.
    """
    import genanki

    basic_model, cloze_model = _models()
    deck = genanki.Deck(deck_id(name), name)
    for card in cards:
        is_cloze = (card.get("kind") or "qa") == "cloze"
        note = genanki.Note(
            model=cloze_model if is_cloze else basic_model,
            fields=[card["front"], _back_html(card)] if not is_cloze
            else [card["front"], (card.get("back") or "")],
            guid=guid(card),
            tags=[name],
        )
        deck.add_note(note)

    buffer = io.BytesIO()
    with _build_lock, tempfile.TemporaryDirectory(prefix="trascrivi-deck-") as scratch:
        previous = tempfile.tempdir
        tempfile.tempdir = scratch
        try:
            genanki.Package(deck).write_to_file(buffer)
        finally:
            tempfile.tempdir = previous
    return buffer.getvalue()


def build_tsv(cards: list[dict], name: str) -> str:
    """
    TSV per l'import manuale, nello stesso formato di `decks/*.tsv`.

    Le righe sono divise per tipo perché Anki importa un TSV con un solo note
    type: mescolare QA e cloze in un file farebbe importare metà mazzo sbagliato.
    """
    lines: list[str] = []
    for card in cards:
        front = _one_line(card["front"])
        if (card.get("kind") or "qa") == "cloze":
            lines.append(f"{front}\t{name}")
        else:
            back = _one_line(_strip_tags(_back_html(card)))
            lines.append(f"{front}\t{back}\t{name}")
    return "\n".join(lines) + "\n" if lines else ""


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "")


def _one_line(text: str) -> str:
    """Un TSV non ha righe: gli a capo diventano spazi (Anki usa `<br>`)."""
    return " ".join((text or "").split())


def cards_for(transcript_ids: list[int], *, include_suspended: bool = False) -> list[dict]:
    """
    Le carte da esportare.

    Le sospese restano fuori: la sospensione è una decisione presa qui ("questa
    carta non è pronta"), e riesportarla la riattiverebbe in Anki dove l'utente
    l'aveva magari già sospesa per un motivo suo.
    """
    from . import db

    out: list[dict] = []
    for transcript_id in transcript_ids:
        cards = db.list_cards(int(transcript_id))
        out.extend(c for c in cards
                   if include_suspended or (c.get("status") or "active") == "active")
    return out
