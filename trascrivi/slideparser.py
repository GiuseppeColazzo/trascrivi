"""
trascrivi/slideparser.py
========================
Da un file di slide a una lista di pagine di testo (+ un render per pagina).

Pipeline volutamente leggera, una libreria per formato e niente framework:
`pypdfium2` per i PDF, `python-pptx` e `python-docx` per gli altri due, OCR
`rapidocr` solo dove il testo non c'è (pagine scansionate, immagini estratte).

Tre cose non ovvie, e sono le ragioni per cui questo file esiste:

1. **Ordine di lettura.** Il testo nativo di un PDF esce già in ordine; quello di
   un PPTX no (le shape sono in ordine di creazione) e quello di un OCR nemmeno
   (i box sono in ordine di rilevamento). Si ordinano per banda verticale e poi
   per ascissa, con una tolleranza per riga: senza, una slide a due colonne si
   legge alternando le colonne riga per riga.

2. **Boilerplate.** Footer, loghi e numeri di pagina ricorrono su ogni slide e
   finirebbero nel contesto delle carte come se fossero contenuto. Si tolgono le
   righe presenti su più della metà delle pagine, e si salvano in
   `slide_docs.boilerplate` per poter dire all'utente cosa è stato scartato.

3. **Pagine complesse.** Formule, diagrammi e frammenti isolati vanno letti da un
   VLM (vedi `vlm.py`): qui si decide solo *quali* pagine valgono il pass, perché
   OCR e VLM restano due cose separate e la seconda è opzionale.

Import lazy: senza `rapidocr`/`pypdfium2`/`python-pptx` l'app parte lo stesso e
solo il parsing fallisce, con un messaggio che dice cosa installare.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from pathlib import Path

log = logging.getLogger("trascrivi.slideparser")

# Estensioni accettate dall'upload. Il rendering delle immagini è anche il modo
# più semplice di gestire una slide esportata come PNG.
SLIDE_EXTS = {".pdf", ".pptx", ".docx", ".png", ".jpg", ".jpeg", ".webp"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# Sotto questa soglia di caratteri nativi la pagina è "senza testo": quasi
# sempre una scansione o una slide esportata come immagine, quindi si va di OCR.
MIN_NATIVE_CHARS = 40
# Long side del render di pagina. 1600 è il minimo che fa leggere apici e pedici
# all'OCR e alla VLM, ed è già più di quanto serva alla preview in UI.
RENDER_LONG_SIDE = 1600
# DPI usati solo per le immagini estratte dai documenti (non per le pagine).
IMAGE_LONG_SIDE = 1200

# Simboli che l'OCR sbaglia in modo sistematico (α -> a, θ -> 0, Σ -> I): la loro
# presenza è il segnale più affidabile che la pagina merita la VLM.
MATH_CHARS = set("αβγδεζηθικλμνξπρστυφχψωΣΠΔΩΛΘΦΨΛ≤≥≠≈±∓×÷→←↔⇒∑∏∫∮√∞∂∇²³⁰¹⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉")
MATH_PATTERN = re.compile(r"[A-Za-z0-9)\]}]\s*[\^_]\s*[A-Za-z0-9{(]|\\frac|\\sum|\\alpha|\\theta")


class MissingDependency(RuntimeError):
    """Manca una libreria opzionale: il messaggio dice quale e come installarla."""


def _require(module: str, package: str):
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as exc:  # pragma: no cover - dipende dall'ambiente
        raise MissingDependency(
            f"The '{package}' package is required to read slides. Install it with: "
            f".venv\\Scripts\\python.exe -m pip install {package}"
        ) from exc


# ── OCR ──────────────────────────────────────────────────────────────────────
_ocr_engine = None


def ocr_engine():
    """Un solo motore per processo: il caricamento dei modelli costa ~1 s."""
    global _ocr_engine
    if _ocr_engine is None:
        rapidocr = _require("rapidocr", "rapidocr")
        _ocr_engine = rapidocr.RapidOCR()
    return _ocr_engine


def ocr_lines(image_path: str | Path, engine=None) -> list[dict]:
    """
    Riconosce le righe di un'immagine: `[{"text", "x0", "y0", "x1", "y1"}]`.

    `rapidocr` ha cambiato forma di ritorno fra le versioni (oggetto con
    `.txts`/`.boxes` oppure tupla `(risultati, tempo)` con liste `[box, testo,
    confidenza]`): l'adattatore copre entrambe invece di fissare una versione.
    """
    engine = engine or ocr_engine()
    out = engine(str(image_path))
    rows: list[dict] = []

    boxes = getattr(out, "boxes", None)
    if boxes is not None:  # rapidocr >= 1.4
        for box, text in zip(boxes, getattr(out, "txts", None) or []):
            rows.append(_line(box, text))
    else:  # forma storica: (risultati, tempo)
        result = out[0] if isinstance(out, tuple) else out
        for item in result or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                rows.append(_line(item[0], item[1]))
    return [r for r in rows if r["text"].strip()]


def _line(box, text) -> dict:
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return {"text": str(text), "x0": min(xs), "y0": min(ys),
            "x1": max(xs), "y1": max(ys)}


# ── Ordine di lettura ────────────────────────────────────────────────────────
def order_lines(lines: list[dict]) -> list[dict]:
    """
    Ordina per banda verticale e poi per ascissa.

    La banda è alta quanto la riga più alta già incontrata (~metà altezza box):
    due box sulla stessa riga visiva finiscono nello stesso gruppo anche se
    sfalsati di qualche pixel, che è il caso normale di un titolo su due pezzi.
    """
    if not lines:
        return []
    heights = sorted(l["y1"] - l["y0"] for l in lines)
    band = max(4.0, heights[len(heights) // 2] * 0.6)
    return sorted(lines, key=lambda l: (round((l["y0"] + l["y1"]) / 2 / band), l["x0"]))


def clip_flag(lines: list[dict], width: float) -> bool:
    """Testo tagliato sul bordo: il box comincia o finisce a ridosso del margine."""
    if not width:
        return False
    return any(l["x0"] < width * 0.02 or l["x1"] > width * 0.98 for l in lines)


# ── Render ───────────────────────────────────────────────────────────────────
def _png_size(data: bytes) -> tuple[int, int]:
    """Dimensioni di un PNG lette dall'header IHDR (niente Pillow per questo)."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def render_pdf_page(pdf, index: int, target: Path, long_side: int = RENDER_LONG_SIDE) -> Path:
    """Renderizza una pagina PDF in PNG con il lato lungo a `long_side`."""
    page = pdf[index]
    width, height = page.get_size()
    if not width or not height:
        raise ValueError(f"PDF page {index} has no size")
    scale = long_side / max(width, height)
    return _write_png(page.render(scale=scale).to_pil(), target)


def _write_png(image, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(target), format="PNG")
    return target


def save_image_bytes(data: bytes, target: Path, long_side: int = IMAGE_LONG_SIDE) -> Path:
    """
    Salva un'immagine estratta da un documento, rimpicciolendola se serve.

    Un PPTX contiene spesso foto a piena risoluzione (un'immagine da 4000 px per
    un diagramma): tenerle intere gonfia `data/` senza migliorare l'OCR. Si
    converte sempre in PNG con Pillow, così il file ha l'estensione giusta
    indipendentemente da com'era dentro il documento (un JPEG chiamato `.png`
    inganna il browser, non l'OCR).
    """
    target = target.with_suffix(".png")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "LA", "P"):  # trasparente su bianco, non su nero
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > long_side:
            img.thumbnail((long_side, long_side), Image.LANCZOS)
        _write_png(img, target)
    except Exception:  # noqa: BLE001 - un'immagine esotica resta com'è
        log.info("immagine non convertibile in PNG: %s", target.name)
        target.write_bytes(data)
    return target


# ── PDF ──────────────────────────────────────────────────────────────────────
def parse_pdf(path: Path, out_dir: Path, *, should_stop=None,
              on_page=None) -> list[dict]:
    pdfium = _require("pypdfium2", "pypdfium2")
    pdf = pdfium.PdfDocument(str(path))
    pages: list[dict] = []
    try:
        for index in range(len(pdf)):
            if should_stop and should_stop():
                break
            page = pdf[index]
            render = render_pdf_page(pdf, index, out_dir / "pages" / f"p{index + 1:04d}.png")
            native = _pdf_text(page)
            data = {
                "page_no": index + 1,
                "text": native,
                "has_text": len(native.strip()) >= MIN_NATIVE_CHARS,
                "n_images": _pdf_image_count(page),
                "ocr_chars": 0,
                "render_path": str(render),
                "render_sha": _file_sha(render),
            }
            if not data["has_text"]:
                lines = ocr_lines(render)
                data["text"] = "\n".join(l["text"] for l in order_lines(lines))
                data["ocr_chars"] = len(data["text"])
                data["clipped"] = clip_flag(lines, _pdf_render_size(render)[0])
            pages.append(data)
            if on_page:
                on_page(len(pages), len(pdf))
    finally:
        pdf.close()
    return pages


def _pdf_text(page) -> str:
    textpage = page.get_textpage()
    try:
        return (textpage.get_text_range() or "").strip()
    finally:
        textpage.close()


def _pdf_image_count(page) -> int:
    try:
        return sum(1 for obj in page.get_objects() if obj.type == 3)  # FPDF_PAGEOBJ_IMAGE
    except Exception:  # noqa: BLE001 - il conteggio è un dettaglio, non un requisito
        return 0


def _pdf_render_size(render: Path) -> tuple[int, int]:
    try:
        return _png_size(render.read_bytes())
    except OSError:
        return (0, 0)


# ── PPTX ─────────────────────────────────────────────────────────────────────
def parse_pptx(path: Path, out_dir: Path, *, should_stop=None,
               on_page=None) -> list[dict]:
    pptx = _require("pptx", "python-pptx")
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = pptx.Presentation(str(path))
    # Tolleranza di riga: le shape della stessa riga visiva non hanno lo stesso
    # `top` al pixel (un titolo su due pezzi è già due shape).
    band = max(1.0, float(getattr(prs, "slide_height", 0) or 720) * 0.03)
    pages: list[dict] = []
    for index, slide in enumerate(prs.slides, start=1):
        if should_stop and should_stop():
            break
        blocks: list[dict] = []
        images: list[Path] = []
        for shape in _walk_shapes(slide.shapes):
            top = float(getattr(shape, "top", 0) or 0)
            left = float(getattr(shape, "left", 0) or 0)
            height = float(getattr(shape, "height", 0) or 0)
            width = float(getattr(shape, "width", 0) or 0)
            text = ""
            if shape.has_text_frame:
                text = "\n".join(p.text.strip() for p in shape.text_frame.paragraphs
                                 if p.text.strip())
            if getattr(shape, "has_table", False):
                rows = [" | ".join(cell.text.strip() for cell in row.cells)
                        for row in shape.table.rows]
                text = "\n".join(r for r in rows if r.strip(" |"))
            if text:
                blocks.append({"text": text, "y0": top, "x0": left,
                               "y1": top + height, "x1": left + width})
            if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
                target = out_dir / "img" / f"p{index:04d}-{len(images) + 1}.png"
                saved = _save_picture(shape, target)
                if saved:
                    images.append(saved)

        blocks.sort(key=lambda b: (round(b["y0"] / band), b["x0"]))
        text = "\n".join(b["text"] for b in blocks)

        notes = ""
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()

        # Le immagini estratte vengono OCR-izzate solo se la slide non ha già
        # testo: su una slide di testo l'immagine è decorazione, e l'OCR
        # aggiungerebbe rumore (o il testo del logo) al contesto delle carte.
        ocr_chars = 0
        if len(text.strip()) < MIN_NATIVE_CHARS and images:
            flat = [line for p in images for line in ocr_lines(p)]
            if flat:
                text = "\n".join(l["text"] for l in order_lines(flat))
                ocr_chars = len(text)
        if notes:
            # Le note speaker sono contesto prezioso e non compaiono sulla slide:
            # vanno tenute distinte, altrimenti sembrano un bullet della pagina.
            text = f"{text}\n\nSpeaker notes: {notes}".strip() if text else f"Speaker notes: {notes}"

        pages.append({
            "page_no": index,
            "text": text,
            "has_text": bool(text.strip()) and ocr_chars == 0,
            "n_images": len(images),
            "ocr_chars": ocr_chars,
            # `python-pptx` non sa disegnare una slide: senza PowerPoint (COM)
            # non c'è render, quindi niente anteprima né vision mode su questa
            # slide. Testo e note ci sono comunque.
            "render_path": None,
            "render_sha": None,
        })
        if on_page:
            on_page(index, len(prs.slides))
    return pages


def _walk_shapes(shapes):
    """Itera anche i gruppi: dentro un `GraphicFrame` il testo è invisibile."""
    for shape in shapes:
        yield shape
        if getattr(shape, "shape_type", None) == 6:  # MSO_SHAPE_TYPE.GROUP
            yield from _walk_shapes(shape.shapes)


def _save_picture(shape, target: Path) -> Path | None:
    try:
        return save_image_bytes(shape.image.blob, target)
    except Exception:  # noqa: BLE001 - un'immagine illeggibile non ferma il parse
        log.info("immagine non estraibile da %s", target.name)
        return None


# ── DOCX ─────────────────────────────────────────────────────────────────────
def parse_docx(path: Path, out_dir: Path, *, should_stop=None,
               on_page=None) -> list[dict]:
    docx = _require("docx", "python-docx")

    document = docx.Document(str(path))
    chunks: list[str] = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            text = " | ".join(cell.text.strip() for cell in row.cells)
            if text.strip(" |"):
                chunks.append(text)
    images: list[Path] = []
    for index, shape in enumerate(document.inline_shapes, start=1):
        try:
            blob = shape._inline.graphic.graphicData.pic.blipFill.blip.embed
            part = document.part.related_parts[blob]
            target = out_dir / "img" / f"p0001-{index}.png"
            saved = save_image_bytes(part.blob, target)
            if saved:
                images.append(saved)
        except Exception:  # noqa: BLE001
            continue

    text = "\n".join(chunks)
    ocr_chars = 0
    if len(text.strip()) < MIN_NATIVE_CHARS and images:
        flat = [line for p in images for line in ocr_lines(p)]
        text = "\n".join(l["text"] for l in order_lines(flat))
        ocr_chars = len(text)
    # Un DOCX non ha pagine: il documento è una pagina sola. È la scelta giusta
    # per il matching (una dispensa si assegna intera a una lezione).
    return [{
        "page_no": 1,
        "text": text,
        "has_text": bool(text.strip()) and ocr_chars == 0,
        "n_images": len(images),
        "ocr_chars": ocr_chars,
        "render_path": None,
        "render_sha": None,
    }]


# ── Immagini ─────────────────────────────────────────────────────────────────
def parse_image(path: Path, out_dir: Path, *, should_stop=None,
                on_page=None) -> list[dict]:
    target = save_image_bytes(path.read_bytes(), out_dir / "pages" / "p0001.png")
    lines = ocr_lines(target)
    text = "\n".join(l["text"] for l in order_lines(lines))
    return [{
        "page_no": 1,
        "text": text,
        "has_text": False,  # l'immagine è per definizione una pagina senza testo
        "n_images": 1,
        "ocr_chars": len(text),
        "render_path": str(target),
        "render_sha": _file_sha(target),
    }]


# ── Dispatch, boilerplate, complessità ───────────────────────────────────────
PARSERS = {".pdf": parse_pdf, ".pptx": parse_pptx, ".docx": parse_docx}


def parse(path: Path, out_dir: Path, *, should_stop=None,
          on_page=None) -> tuple[list[dict], list[str]]:
    """
    Legge un documento: `(pagine, righe di boilerplate rimosse)`.

    Le pagine hanno testo, render, flag di complessità — tutto quello che serve a
    `slide_pages`, nessun oggetto intermedio da convertire.
    """
    path = Path(path)
    ext = path.suffix.lower()
    out_dir = Path(out_dir)
    parser = PARSERS.get(ext)
    if parser:
        pages = parser(path, out_dir, should_stop=should_stop, on_page=on_page)
    elif ext in IMAGE_EXTS:
        pages = parse_image(path, out_dir, should_stop=should_stop, on_page=on_page)
    else:
        raise ValueError(f"Unsupported slide format '{ext}'. "
                         f"Allowed: {', '.join(sorted(SLIDE_EXTS))}")
    pages, boilerplate = strip_boilerplate(pages)
    for page in pages:
        page["complex"] = is_complex(page)
    return pages, boilerplate


def strip_boilerplate(pages: list[dict], threshold: float = 0.5) -> tuple[list[dict], list[str]]:
    """
    Toglie le righe che ricorrono su più della metà delle pagine.

    Un documento di due pagine non ha boilerplate: con così poco campione "più
    della metà" è una riga qualsiasi, e la soglia scatta a caso. Da tre pagine in
    su la statistica vale (footer, logo testuale, numero di pagina, titolo del
    corso).

    Le righe si confrontano *letteralmente* (minuscolo, spazi normalizzati). La
    tentazione era azzerare anche le cifre, così "Pagina 1" e "Pagina 2"
    cadevano nella stessa regola; il risultato era che anche "contenuto 1" e
    "contenuto 2" diventavano la stessa riga e metà del testo finiva nel cestino.
    I numeri di pagina si riconoscono da soli: una riga fatta solo di cifre è la
    stessa riga su tutte le pagine.
    """
    if len(pages) < 3:
        return pages, []
    counts: dict[str, int] = {}
    for page in pages:
        for line in set(_line_keys(page.get("text") or "")):
            counts[line] = counts.get(line, 0) + 1
    wanted = len(pages) * threshold
    # "#" è lungo un carattere ma non è una riga qualsiasi: è la classe dei numeri
    # di pagina, e va filtrata come le altre righe ricorrenti.
    recurring = {line for line, n in counts.items()
                 if n > wanted and (len(line) > 1 or line == "#")}

    if not recurring:
        return pages, []

    seen: dict[str, str] = {}
    for page in pages:
        kept = []
        for line in (page.get("text") or "").splitlines():
            key = _line_key(line)
            if key in recurring:
                seen.setdefault(key, line.strip() or "#")
                continue
            kept.append(line)
        page["text"] = "\n".join(kept).strip()
    removed = sorted(seen.values(), key=len, reverse=True)
    log.info("boilerplate rimosso: %s", "; ".join(r[:40] for r in removed[:6]))
    return pages, removed


# Una riga fatta solo di cifre e separatori è un numero di pagina, su qualunque
# pagina: si confronta come una riga sola.
_NUMERIC_LINE = re.compile(r"^[\d\s/.,|–—-]+$")


def _line_key(line: str) -> str:
    text = " ".join((line or "").split()).lower()
    if not text:
        return ""
    return "#" if _NUMERIC_LINE.match(text) else text


def _line_keys(text: str) -> list[str]:
    return [k for k in (_line_key(l) for l in text.splitlines()) if k]


def is_complex(page: dict) -> bool:
    """
    Vale la pena pagare il pass VLM su questa pagina?

    Cinque segnali, in ordine di affidabilità: simboli matematici, pedici/apici,
    testo tagliato al bordo, molti frammenti corti, immagine dominante con poco
    testo. Una pagina senza testo nativo è complessa per costruzione: l'OCR l'ha
    letta, e su un diagramma l'OCR non basta.
    """
    text = page.get("text") or ""
    if not page.get("has_text"):
        return True
    if page.get("clipped"):
        return True
    if any(char in MATH_CHARS for char in text) or MATH_PATTERN.search(text):
        return True
    lines = [l for l in text.splitlines() if l.strip()]
    if lines:
        short = sum(1 for l in lines if len(l.split()) <= 2)
        if len(lines) >= 6 and short / len(lines) > 0.5:
            return True
    if page.get("n_images") and len(text.strip()) < 300:
        return True
    return False


def _file_sha(path: Path | None) -> str | None:
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
