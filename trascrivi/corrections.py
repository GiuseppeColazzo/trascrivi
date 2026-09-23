"""
trascrivi/corrections.py
========================
Edit-plan deterministico: l'agente propone sostituzioni, il codice le verifica.

Perché un edit-plan e non una riscrittura: sostituire *find -> replace* costa
token proporzionali alle correzioni (non al testo), è verificabile (match esatto,
conteggio delle occorrenze, conflitti) e resta accettabile/rifiutabile una
proposta per volta. Il prezzo è che `find` deve esistere **verbatim** nel testo:
qui lo verifichiamo invece di fidarci del modello.

Un `Plan` viene costruito una volta sola a partire dal testo; ogni voce porta le
occorrenze trovate come (indice segmento, inizio, fine) sul **testo normalizzato**
che è costruito con lo stesso numero di caratteri del testo reale.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .textutil import normalize_ws, segments_to_text, text_to_segments

log = logging.getLogger("trascrivi.corrections")

# Oltre questa differenza di lunghezza una sostituzione è troppo aggressiva:
# significa che il modello sta riscrivendo una frase, non correggendo un token.
MAX_DELTA_CHARS = 160

_DASHES = {"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
           "\u2212": "-"}
_QUOTES = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00ab": '"',
           "\u00bb": '"', "\u2032": "'", "\u00b4": "'", "\u0060": "'"}
_WS_RE = re.compile(r"\s+")

_TRANSLATE = {ord(k): v for k, v in {**_QUOTES, **_DASHES}.items()}

ALLOWED_STATUS = {"pending", "accepted", "rejected"}
ALLOWED_KINDS = {"term", "asr_error", "punctuation", "grammar", "name", "formatting",
                 "other"}


def normalize_for_match(text: str) -> str:
    """
    Normalizzazione **tollerante** per confrontare testi: run di whitespace
    collassati in un solo spazio, apostrofi/virgolette/dash tipografici in ASCII.

    Questa forma si usa per decidere *se* un `find` esiste (e per confrontare due
    proposte), non per localizzarlo: la lunghezza cambia. Per gli indici serve
    `normalize_with_map`.
    """
    return " ".join(text.split()).translate(_TRANSLATE)


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """
    Normalizza e restituisce la mappa `normalized_index -> original_index`.

    La mappa permette di cercare su un testo tollerante agli spazi e di applicare
    la sostituzione sul testo **originale**, senza rischiare di tagliare nel punto
    sbagliato (era il bug: indici calcolati sul testo collassato, taglio
    sull'originale).
    """
    out: list[str] = []
    mapping: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            out.append(" ")
            mapping.append(i)
            i = j
            continue
        out.append(_DASHES.get(ch) or _QUOTES.get(ch) or ch)
        mapping.append(i)
        i += 1
    return "".join(out), mapping


def _find_matches(segment_text: str, norm_find: str) -> list[tuple[int, int]]:
    """
    Occorrenze di `norm_find` in un segmento, come (inizio, fine) sul testo
    **originale**.

    Il confronto è tollerante su spazi e caratteri tipografici: un run di spazi
    diversi o un apostrofo dritto al posto di uno curvo non impediscono il match,
    perché quel testo arriva da un riconoscitore e da un modello, non da un
    editor. I match a metà parola sono scartati: `is` dentro `this` non è una
    correzione.
    """
    if not norm_find:
        return []
    line, mapping = normalize_with_map(segment_text)
    pattern = "".join(
        r"[ \t\u00a0\u2018\u2019\u0060\u00b4]+" if ch == " " else re.escape(ch)
        for ch in norm_find
    )
    out: list[tuple[int, int]] = []
    for m in re.finditer(pattern, line):
        n_start, n_end = m.span()
        before_ok = n_start == 0 or not line[n_start - 1].isalnum()
        after_ok = n_end >= len(line) or not line[n_end].isalnum()
        if not (before_ok and after_ok):
            continue
        # Il run di spazi finale del match non deve portarsi dietro la spaziatura
        # originale: la sostituzione riguarda il testo, non i separatori.
        while n_end > n_start and line[n_end - 1] == " " and norm_find[-1] != " ":
            n_end -= 1
        if n_end <= n_start:
            continue
        orig_start = mapping[n_start]
        orig_end = mapping[n_end - 1] + 1
        out.append((orig_start, orig_end))
    return out


def chunk_text_with_map(segments: list[dict]) -> tuple[str, list[tuple[int, int, int]]]:
    """
    Testo del chunk nello stesso formato inviato al modello, con mappa dei range.

    Restituisce `(testo, [(inizio, fine, indice_segmento), …])` sul testo
    normalizzato: così un `find` trovato dal modello si traduce subito in
    (segmento, posizione) senza secondo passaggio di ricerca.
    """
    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    pos = 0
    for idx, seg in enumerate(segments):
        norm = normalize_for_match(seg.get("text", ""))
        if pos:
            parts.append(" ")
            pos += 1
        parts.append(norm)
        spans.append((pos, pos + len(norm), idx))
        pos += len(norm)
    return "".join(parts), spans


def locate_in_spans(joined: str, spans: list[tuple[int, int, int]],
                    norm_find: str) -> tuple[int, int] | None:
    """
    Trova `norm_find` nel testo del chunk e restituisce (segmento, offset locale).

    Cerca su tutta la stringa unita (non riga per riga), così una sostituzione a
    cavallo di due segmenti resta individuabile; poi riporta la posizione nella
    riga.
    """
    if not norm_find:
        return None
    for start, end in _find_matches(joined, norm_find):
        for s, e, idx in spans:
            if s <= start < e:
                return idx, start - s
    return None


@dataclass
class PlanItem:
    """Una sostituzione proposta, con le occorrenze verificate nel testo."""

    find: str
    replace: str
    kind: str = "other"
    reason: str = ""
    confidence: float | None = None
    proposal_id: int | None = None
    # (segment_index, start, end) sul testo ORIGINALE del segmento
    occurrences: list[tuple[int, int, int]] = field(default_factory=list)
    flag: str = "ok"
    norm_find: str = ""

    @property
    def count(self) -> int:
        return len(self.occurrences)

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "find": self.find,
            "replace": self.replace,
            "kind": self.kind,
            "reason": self.reason,
            "confidence": self.confidence,
            "flag": self.flag,
            "occurrences": len(self.occurrences),
            "segment_index": self.occurrences[0][0] if self.occurrences else None,
            "context": context(self.find, self.replace),
        }


@dataclass
class Plan:
    segments: list[dict]
    items: list[PlanItem]

    def by_id(self, proposal_id: int) -> PlanItem | None:
        for it in self.items:
            if it.proposal_id == proposal_id:
                return it
        return None

    def summary(self) -> dict:
        from collections import Counter
        return dict(Counter(it.flag for it in self.items))


def context(find: str, replace: str, width: int = 60) -> str:
    """Anteprima breve per la UI: `…prima [find] dopo…` → replacement."""
    return f"{find[:width]} -> {replace[:width]}"


# ── Costruzione del piano ────────────────────────────────────────────────────
def build_plan(segments: list[dict], proposals: list[dict],
               allow_multiple: bool = False) -> Plan:
    """
    Verifica ogni proposta contro i segmenti.

    `allow_multiple=True` è il modo "glossario": sostituisci tutte le occorrenze.
    Con `False` (proposte LLM) più occorrenze rendono la voce `ambiguous` e
    l'applicazione richiede che l'utente scelga quale segmento correggere.

    Le occorrenze non vengono memorizzate qui: `apply_plan` le ricalcola sul testo
    corrente, così l'ordine di applicazione è indifferente e non restano indici
    stantii.
    """
    items: list[PlanItem] = []
    seen: set[tuple[str, str]] = set()

    for prop in proposals:
        find = (prop.get("find") or "")
        replace = prop.get("replace") or ""
        item = PlanItem(
            find=find,
            replace=replace,
            kind=(prop.get("kind") or "other").lower(),
            reason=prop.get("reason") or "",
            confidence=prop.get("confidence"),
            proposal_id=prop.get("id"),
            norm_find=normalize_for_match(find),
        )
        if item.kind not in ALLOWED_KINDS:
            item.kind = "other"

        key = (item.norm_find, normalize_for_match(replace))
        if find.strip() and item.norm_find and key in seen:
            item.flag = "duplicate"
            items.append(item)
            continue
        seen.add(key)

        if not find.strip():
            item.flag = "unmatched"
        elif find == replace:
            item.flag = "noop"
        elif abs(len(replace) - len(find)) > MAX_DELTA_CHARS:
            item.flag = "too_large"
        else:
            item.occurrences = _scan(segments, item.norm_find)
            if not item.occurrences:
                item.flag = "unmatched"
            elif len(item.occurrences) > 1 and not allow_multiple:
                item.flag = "ambiguous"
        items.append(item)

    return Plan(segments=segments, items=items)


def _scan(segments: list[dict], norm_find: str) -> list[tuple[int, int, int]]:
    """Occorrenze di `norm_find` su tutti i segmenti, come nel piano."""
    out: list[tuple[int, int, int]] = []
    if not norm_find:
        return out
    for idx, seg in enumerate(segments):
        for start, end in _find_matches(seg.get("text", ""), norm_find):
            out.append((idx, start, end))
    return out


# ── Applicazione ─────────────────────────────────────────────────────────────
def apply_plan(plan: Plan, accepted: list[PlanItem] | None = None,
               targets: dict[int, int] | None = None,
               apply_all: bool = False) -> dict:
    """
    Applica le voci accettate e restituisce testo, segmenti e statistiche.

    `targets` sceglie il segmento quando una voce è ambigua:
    `{proposal_id: segment_index}`. Una voce ambigua senza target viene saltata
    con flag `ambiguous`, mai applicata "a caso".
    `apply_all=True` è la modalità glossario: applica tutte le occorrenze invece
    di pretendere che siano uniche.
    """
    items = accepted if accepted is not None else list(plan.items)
    # Difesa: se un segmento contenesse un a capo (testo importato a mano) gli
    # indici riga/segmento non coinciderebbero più. Normalizziamo qui.
    flat = text_to_segments(segments_to_text(plan.segments), plan.segments)
    lines = [s.get("text", "").replace("\n", " ") for s in flat]
    norm_lines = [normalize_for_match(t) for t in lines]
    targets = targets or {}

    # Le occorrenze si ricalcolano qui, sul testo corrente: così l'ordine delle
    # proposte è indifferente e non esistono indici stantii se il piano è stato
    # costruito su una versione precedente del testo.
    counts = {id(item): _scan(flat, item.norm_find) for item in items}

    applied: list[dict] = []
    skipped: list[dict] = []
    # Spostamenti di lunghezza accumulati per riga: (posizione_originale, delta).
    shifts: dict[int, list[tuple[int, int]]] = {}

    todo: list[tuple[int, int, int, PlanItem]] = []
    for item in items:
        if item.flag in {"unmatched", "noop", "too_large", "duplicate"}:
            skipped.append({**item.to_dict(), "skip_reason": item.flag})
            continue
        occ = counts[id(item)]
        if not occ:
            skipped.append({**item.to_dict(), "skip_reason": "unmatched"})
            continue
        if len(occ) > 1:
            if apply_all:
                pass
            else:
                chosen = targets.get(item.proposal_id) if item.proposal_id is not None else None
                if chosen is None:
                    skipped.append({**item.to_dict(), "skip_reason": "ambiguous"})
                    continue
                # `chosen` è l'indice del segmento: la prima occorrenza in quel segmento.
                occ = [o for o in occ if o[0] == chosen]
                if not occ:
                    skipped.append({**item.to_dict(), "skip_reason": "target_not_found"})
                    continue
                occ = occ[:1]
        for seg_idx, start, end in occ:
            todo.append((seg_idx, start, end, item))

    # Conflitti: due sostituzioni accettate che si sovrappongono nella stessa
    # riga. Passa la prima in ordine di posizione (la più a sinistra); la
    # seconda viene saltata e segnalata, mai applicata sopra la prima.
    todo.sort(key=lambda t: (t[0], t[1], -(t[2] - t[1])))
    accepted_spans: dict[int, list[tuple[int, int, int]]] = {}
    final: list[tuple[int, int, int, PlanItem]] = []
    seen_spans: set[tuple[int, int, int]] = set()
    for seg_idx, start, end, item in todo:
        span_key = (seg_idx, start, end)
        if span_key in seen_spans:
            skipped.append({**item.to_dict(), "skip_reason": "duplicate_span"})
            continue
        clash = next(
            (o for o in accepted_spans.get(seg_idx, []) if start < o[1] and o[0] < end),
            None,
        )
        if clash is not None:
            skipped.append({**item.to_dict(), "skip_reason": "conflict"})
            continue
        seen_spans.add(span_key)
        accepted_spans.setdefault(seg_idx, []).append((start, end, 0))
        final.append((seg_idx, start, end, item))

    # Applica: per riga, dalla sostituzione più a destra alla più a sinistra.
    for seg_idx in sorted({t[0] for t in final}):
        for _s, start, end, item in sorted(
            (t for t in final if t[0] == seg_idx), key=lambda t: t[1], reverse=True
        ):
            line = lines[seg_idx]
            norm = norm_lines[seg_idx]
            hit = normalize_for_match(line[start:end]) == item.norm_find
            if not hit:
                # Non dovrebbe accadere (gli indici derivano dallo stesso testo),
                # ma meglio saltare che corrompere il testo.
                skipped.append({**item.to_dict(), "skip_reason": "stale_index"})
                continue
            lines[seg_idx] = line[:start] + item.replace + line[end:]
            delta = len(item.replace) - len(item.norm_find)
            shifts.setdefault(seg_idx, []).append((start, delta))
            applied.append({
                **item.to_dict(),
                "skip_reason": None,
                "segments": [seg_idx],
                "replacements": 1,
            })

    if not applied:
        return {
            "applied": 0, "skipped": len(skipped), "changed_segments": 0,
            "text": segments_to_text(flat), "segments": flat,
            "details": skipped, "applied_items": [],
        }

    # Ricostruisce i segmenti: offset ricalcolati, timestamp del primo segmento
    # toccato riusato per le eventuali righe aggiunte.
    new_segments = [{**seg, "text": text} for seg, text in zip(flat, lines)]
    new_segments = text_to_segments(segments_to_text(new_segments), flat)
    # text_to_segments preserva i timestamp passati: ricompattiamo il caso in cui
    # il numero di righe non cambia (percorso normale, nessuna interpolazione).
    if len(new_segments) == len(flat):
        for old, new in zip(flat, new_segments):
            new["start"] = old.get("start", 0.0)
            new["end"] = old.get("end", 0.0)

    changed = sorted({t[0] for t in final})
    return {
        "applied": len(applied),
        "skipped": len(skipped),
        "changed_segments": len(changed),
        "changed_indices": changed,
        "text": segments_to_text(new_segments),
        "segments": new_segments,
        "details": skipped,
        "applied_items": applied,
        "shifts": {str(k): v for k, v in shifts.items()},
    }


def apply_terms(text: str, terms: list[dict]) -> tuple[str, int]:
    """
    Applica il glossario di materia a un testo (tutte le occorrenze).

    Usato all'import di una trascrizione: zero token, zero latenza, e resta la
    strada per cui le correzioni accettate su una lezione migliorano le
    successive. Il testo viene ricostruito dai segmenti per non perdere la
    mappatura con i timestamp.
    """
    if not terms:
        return text, 0
    segments = text_to_segments(text)
    n_applied = 0
    for term in terms:
        find = (term.get("find") or "").strip()
        if not find:
            continue
        plan = build_plan(segments, [{"find": find, "replace": term.get("replace") or "",
                                      "kind": "term"}], allow_multiple=True)
        result = apply_plan(plan, apply_all=True)
        if result["applied"]:
            segments = result["segments"]
            text = result["text"]
            n_applied += result["applied"]
    return text, n_applied


def dedupe_proposals(items: list[dict]) -> list[dict]:
    """
    Scarta le proposte identiche (il chunking ripete un segmento di contesto).

    Tiene la prima occorrenza: le successive non aggiungono informazione e
    renderebbero la review inutilmente lunga.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for it in items:
        key = (normalize_for_match(it.get("find") or ""), normalize_for_match(it.get("replace") or ""))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def diff_preview(old_text: str, new_text: str, limit: int = 200) -> list[dict]:
    """
    Righe cambiate fra due testi, per l'anteprima prima di applicare.

    Confronto riga per riga con `difflib`: zero dipendenze e sufficiente per un
    editor di appunti (non serve un diff a livello di carattere).
    """
    import difflib
    out: list[dict] = []
    for line in difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                     lineterm="", n=0):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith(("-", "+")):
            out.append({"kind": line[0], "text": line[1:]})
        if len(out) >= limit:
            break
    return out
