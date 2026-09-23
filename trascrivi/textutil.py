"""
trascrivi/textutil.py
=====================
Conversione fra le due rappresentazioni della trascrizione:

* **raw / testo con timestamp** - il formato prodotto da `trascrivi_audio.py`
  (`[mm:ss] testo` a inizio riga). È l'unico su cui `--resume` sa ripartire,
  quindi resta su disco durante il job.
* **testo pulito + segmenti** - quello che l'utente legge, cerca ed edita.

Qui vivono anche il render Markdown e la normalizzazione del testo usata
dall'engine dei replacement.
"""

from __future__ import annotations

import re

TS_LINE_RE = re.compile(r"^\[(\d+):(\d{2})(?::(\d{2}))?\]\s?(.*)$")


def fmt_ts(seconds: float) -> str:
    """Secondi → `mm:ss` oppure `hh:mm:ss`."""
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_ts(a: str, b: str, c: str | None) -> float:
    if c is None:
        return int(a) * 60 + int(b)
    return int(a) * 3600 + int(b) * 60 + int(c)


def parse_raw(raw: str) -> list[dict]:
    """
    Righe `[mm:ss] testo` → segmenti con offset nel testo pulito.

    Le righe senza timestamp vengono accodate all'ultimo segmento (capita con
    output manualmente editati); quelle iniziali senza timestamp diventano un
    segmento con `start=0` e `ts=False`, così restano visibili.
    """
    segments: list[dict] = []
    offset = 0
    pending_prefix: list[str] = []

    for line in raw.splitlines():
        m = TS_LINE_RE.match(line)
        if not m:
            text = line.strip()
            if not text:
                continue
            if segments:
                add = ("\n" if segments[-1]["text"] else "") + text
                segments[-1]["text"] += add
                segments[-1]["end"] = segments[-1].get("end") or segments[-1]["start"]
            else:
                pending_prefix.append(text)
            continue
        a, b, c, text = m.groups()
        text = text.strip()
        start = _parse_ts(a, b, c)
        if pending_prefix:
            text = " ".join(pending_prefix + [text]).strip()
            pending_prefix = []
        # I timestamp possono essere spaziati in modo non lineare: manteniamo il
        # valore precedente come fine del segmento precedente.
        if segments:
            segments[-1]["end"] = start
        segments.append({"start": start, "end": start, "text": text, "offset": offset})
        offset += len(text) + 1

    if pending_prefix:
        segments.insert(0, {"start": 0.0, "end": 0.0, "text": " ".join(pending_prefix),
                            "offset": 0, "ts": False})
    return _reindex(segments)


def _reindex(segments: list[dict]) -> list[dict]:
    """Ricalcola gli offset a partire dai testi, in ordine."""
    offset = 0
    for seg in segments:
        seg["offset"] = offset
        offset += len(seg["text"]) + 1
        seg.setdefault("ts", True)
    return segments


def segments_to_text(segments: list[dict], with_ts: bool = False) -> str:
    """Segmenti → testo (opzionalmente con `[mm:ss] ` a inizio riga)."""
    if with_ts:
        return "\n".join(f"[{fmt_ts(s.get('start', 0.0))}] {s['text']}" for s in segments)
    return "\n".join(s["text"] for s in segments)


def text_to_segments(text: str, previous: list[dict] | None = None) -> list[dict]:
    """
    Testo editato a mano → segmenti.

    Strategia: una riga = un segmento. Se il numero di righe combacia con i
    segmenti precedenti i timestamp restano quelli; altrimenti si interpolano
    linearmente sulla durata nota, in modo che i timestamp restino monotoni e
    plausibili invece di sparire. `[mm:ss]` a inizio riga è accettato e ha la
    precedenza sul timestamp interpolato.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]

    starts: list[float | None] = []
    texts: list[str] = []
    for ln in lines:
        m = TS_LINE_RE.match(ln)
        if m:
            a, b, c, body = m.groups()
            starts.append(_parse_ts(a, b, c))
            texts.append(body.strip())
        else:
            starts.append(None)
            texts.append(ln.strip())

    known = [s["start"] for s in (previous or [])]
    duration = 0.0
    if previous:
        duration = max(float(s.get("end") or s.get("start") or 0.0) for s in previous)

    n = len(texts)
    # 1) timestamp riusati o espliciti (gli espliciti vincono)
    provisional: list[float | None] = []
    for i in range(n):
        if starts[i] is not None:
            provisional.append(float(starts[i]))
        elif i < len(known):
            provisional.append(float(known[i]))
        else:
            provisional.append(None)

    # 2) le righe senza ancora vengono distribuite fra le ancore note, così la
    #    sequenza resta monotona anche se righe nuove si infilano fra quelle
    #    riusate (una interpolazione uniforme sul totale le farebbe tornare indietro).
    out_starts: list[float] = [0.0] * n
    anchors = [i for i, v in enumerate(provisional) if v is not None]
    last = 0.0
    for pos, i in enumerate(anchors):
        value = max(float(provisional[i]), last)
        out_starts[i] = value
        last = value
        gap_start = i + 1
        gap_end = anchors[pos + 1] if pos + 1 < len(anchors) else n
        missing = gap_end - gap_start
        if missing > 0:
            nxt = float(provisional[anchors[pos + 1]]) if pos + 1 < len(anchors) else duration
            step = (nxt - value) / (missing + 1)
            for k in range(missing):
                out_starts[gap_start + k] = value + step * (k + 1)
    # Coda senza ancore (nessun timestamp noto a monte): distribuzione uniforme.
    if not anchors and n:
        for i in range(n):
            out_starts[i] = duration * i / n if duration else float(i)
    # Rete di sicurezza: monotonia stretta, qualunque cosa sia successo sopra.
    for i in range(1, n):
        if out_starts[i] < out_starts[i - 1]:
            out_starts[i] = out_starts[i - 1]

    out: list[dict] = [
        {"start": out_starts[i], "end": out_starts[i], "text": body,
         "ts": starts[i] is not None}
        for i, body in enumerate(texts)
    ]

    # `end` di ciascun segmento = start del successivo, l'ultimo chiude alla durata.
    for i, seg in enumerate(out):
        if i + 1 < len(out):
            seg["end"] = max(out[i + 1]["start"], seg["start"])
        else:
            seg["end"] = max(duration, seg["start"])
    return _reindex(out)


def normalize_ws(text: str) -> str:
    """Collassa ogni sequenza di whitespace in un singolo spazio."""
    return " ".join(text.split())


def line_offsets(lines: list[str]) -> list[int]:
    """Offset di inizio di ogni riga in un testo ottenuto unendole con `\\n`."""
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    return offsets


# ── Export ───────────────────────────────────────────────────────────────────
def build_markdown(title: str, meta: dict, segments: list[dict],
                   glossary: list[dict] | None = None) -> str:
    """
    Markdown pronto per gli appunti.

    Ogni ~5 minuti inserisce un heading temporale, così il documento è navigabile
    anche su una lezione di due ore.
    """
    parts: list[str] = [f"# {title}", ""]
    lines: list[str] = []
    if meta.get("course"):
        lines.append(f"**Course:** {meta['course']}")
    if meta.get("date"):
        lines.append(f"**Date:** {meta['date']}")
    if meta.get("language"):
        lines.append(f"**Language:** {str(meta['language']).upper()}")
    if meta.get("model"):
        lines.append(f"**Model:** {meta['model']}")
    if meta.get("duration"):
        lines.append(f"**Duration:** {fmt_ts(meta['duration'])}")
    if lines:
        parts.extend(lines)
        parts.append("")

    bucket = -1
    for seg in segments:
        b = int(float(seg.get("start", 0.0)) // 300)
        if b != bucket:
            bucket = b
            parts.append(f"## {fmt_ts(bucket * 300)}")
            parts.append("")
        parts.append(seg.get("text", "").strip())

    if glossary:
        parts.append("")
        parts.append("## Glossary")
        parts.append("")
        for term in glossary:
            meaning = term.get("replace") or term.get("note") or ""
            # Em dash: l'export Markdown è un file, non output di console, e la
            # risposta HTTP dichiara charset=utf-8.
            parts.append(f"- **{term.get('find', '')}** \u2014 {meaning}")

    return "\n".join(parts).strip() + "\n"
