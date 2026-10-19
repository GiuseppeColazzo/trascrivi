"""
Il parser delle slide, provato su file veri.

Nessuna rete e nessun campione esterno: i PDF, i PPTX e i DOCX si costruiscono
qui dentro. I tre casi complessi del piano (formule con simboli, bullet con
etichette, diagramma) sono fixture sintetiche minime: i campioni reali servono
all'A/B della VLM (`tools/vlm_ab.py`), non alla suite, che deve restare
riproducibile su qualunque macchina.
"""

from __future__ import annotations

import io

import pytest

from trascrivi import slideparser


# ── Fixture: PDF minimo scritto a mano ───────────────────────────────────────
def make_pdf(pages: list[list[str]]) -> bytes:
    """
    Un PDF valido con un font base14, una pagina per lista di righe.

    Scritto a mano invece che con una libreria: il test deve verificare che il
    parser legga un PDF *prodotto altrove*, e con una libreria la pagina sarebbe
    costruita dalla stessa famiglia di codice che la legge.
    """
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(pages))]      # 4, 6, 8...
    content_ids = [5 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, lines in enumerate(pages):
        stream = ["BT", "/F1 20 Tf", "60 340 Td", "26 TL"]
        for line in lines:
            safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            stream.append(f"({safe}) Tj T*")
        stream.append("ET")
        body = "\n".join(stream).encode("latin-1", "replace")
        objects.append(
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 720 405] "
             f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_ids[index]} 0 R >>"
             ).encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref))
    return bytes(out)


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "deck.pdf"
    path.write_bytes(make_pdf([
        ["Width and Resolution Multipliers", "Width multiplier alpha reduces channels"],
        ["Depthwise Separable Convolutions", "Separate a multi-channel Conv into C convolutions"],
    ]))
    return path


# ── PDF ──────────────────────────────────────────────────────────────────────
def test_a_pdf_page_with_native_text_needs_no_ocr(pdf_path, tmp_path, monkeypatch):
    """Con il testo dentro il PDF l'OCR non deve nemmeno partire."""
    def _boom(*args, **kwargs):
        raise AssertionError("l'OCR non deve essere chiamato su una pagina con testo")

    monkeypatch.setattr(slideparser, "ocr_lines", _boom)
    pages = slideparser.parse_pdf(pdf_path, tmp_path / "doc")

    assert len(pages) == 2
    assert "Width and Resolution Multipliers" in pages[0]["text"]
    assert pages[0]["has_text"] is True
    assert pages[0]["ocr_chars"] == 0
    render = pages[0]["render_path"]
    assert render and render.endswith(".png")


def test_a_pdf_page_without_text_goes_through_the_ocr(pdf_path, tmp_path, monkeypatch):
    """
    Una pagina scansionata non ha testo nativo: quello che si legge è il risultato
    dell'OCR, e la pagina è marcata complessa (una figura senza testo è
    esattamente il caso che la VLM deve vedere).
    """
    monkeypatch.setattr(slideparser, "ocr_lines", lambda path, engine=None: [
        {"text": "Right column", "x0": 400, "y0": 100, "x1": 500, "y1": 120},
        {"text": "Left column", "x0": 50, "y0": 100, "x1": 150, "y1": 120},
    ])
    path = tmp_path / "scansione.pdf"
    path.write_bytes(make_pdf([[]]))   # una pagina senza contenuto

    pages = slideparser.parse_pdf(path, tmp_path / "doc")

    assert pages[0]["has_text"] is False
    assert pages[0]["ocr_chars"] > 0
    # La riga più a sinistra viene prima, anche se l'OCR l'ha restituita dopo.
    assert pages[0]["text"].splitlines() == ["Left column", "Right column"]
    assert slideparser.is_complex(pages[0]) is True


def test_the_ocr_really_reads_the_rendered_page(pdf_path, tmp_path):
    """Il pezzo che non si può fingere: RapidOCR legge il PNG che abbiamo reso."""
    pages = slideparser.parse_pdf(pdf_path, tmp_path / "doc")
    lines = slideparser.ocr_lines(pages[0]["render_path"])
    text = " ".join(line["text"] for line in lines)

    assert "Width" in text and "Multipliers" in text, text


# ── Ordine di lettura ────────────────────────────────────────────────────────
def test_two_columns_are_read_column_by_column():
    """
    L'ordine geometrico è per banda verticale e poi per ascissa: due box sulla
    stessa riga ma in colonne diverse restano nell'ordine giusto, e una riga più
    in basso non passa davanti a una più in alto.
    """
    lines = [
        {"text": "right-1", "x0": 400, "y0": 100, "x1": 500, "y1": 120},
        {"text": "left-2", "x0": 50, "y0": 140, "x1": 150, "y1": 160},
        {"text": "left-1", "x0": 50, "y0": 100, "x1": 150, "y1": 120},
    ]
    assert [l["text"] for l in slideparser.order_lines(lines)] == ["left-1", "right-1", "left-2"]


def test_text_cut_at_the_border_is_flagged():
    lines = [{"text": "ember that l = f(l)", "x0": 0, "y0": 10, "x1": 120, "y1": 30}]
    assert slideparser.clip_flag(lines, 1400) is True
    centred = [{"text": "centrato", "x0": 400, "y0": 10, "x1": 900, "y1": 30}]
    assert slideparser.clip_flag(centred, 1400) is False


# ── Boilerplate ──────────────────────────────────────────────────────────────
def _page(number: int, body: str, footer: str = "Part-II: AI Computing Optimization") -> dict:
    return {"page_no": number, "text": f"{body}\n{footer}\n{number}", "has_text": True,
            "n_images": 0}


def test_recurring_lines_are_removed_and_reported():
    pages = [_page(i, f"contenuto {i}") for i in range(1, 5)]

    cleaned, removed = slideparser.strip_boilerplate(pages)

    assert "Part-II" in " ".join(removed)
    assert all("Part-II" not in p["text"] for p in cleaned)
    assert cleaned[0]["text"] == "contenuto 1"
    # I numeri di pagina si normalizzano a "#": una riga sola li copre tutti.
    assert all(not p["text"].strip().isdigit() for p in cleaned)


def test_two_pages_are_not_enough_to_call_something_boilerplate():
    """Con due pagine "più della metà" è una riga qualsiasi: meglio non toccare."""
    pages = [_page(1, "slide uno"), _page(2, "slide due")]
    cleaned, removed = slideparser.strip_boilerplate(pages)
    assert removed == []
    assert cleaned == pages


def test_boilerplate_does_not_touch_a_line_that_appears_half_the_time():
    pages = [_page(i, "uguale" if i <= 2 else f"diverso {i}") for i in range(1, 5)]
    _cleaned, removed = slideparser.strip_boilerplate(pages)
    assert "Part-II: AI Computing Optimization" in removed


# ── Complessità ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("page, expected", [
    ({"text": "R(α) = Σ R(α)", "has_text": True, "n_images": 0}, True),
    ({"text": "theta_0 and alpha^(1)", "has_text": True, "n_images": 0}, True),
    ({"text": "Depthwise separable convolutions reduce complexity", "has_text": True,
      "n_images": 0}, False),
    ({"text": "", "has_text": False, "n_images": 1}, True),
    ({"text": "L1\nK=8\nL2\nK=4\nalpha\n0.75\nbeta", "has_text": True, "n_images": 0}, True),
    # Un'immagine su una pagina di testo lungo è un logo o una decorazione: non
    # vale il pass VLM. Su una pagina quasi vuota invece è la pagina.
    ({"text": "una slide di testo normale con una frase completa e leggibile, che "
              "continua anche nella riga successiva e poi in una terza riga ancora, "
              "così il conteggio dei caratteri supera abbondantemente la soglia dei "
              "trecento caratteri usata dall'euristica; qui aggiungo altre parole per "
              "essere sicuro di stare sopra la soglia senza doverci pensare", "has_text": True,
      "n_images": 1}, False),
])
def test_complexity_is_flagged_from_real_signals(page, expected):
    assert slideparser.is_complex(page) is expected


# ── PPTX ─────────────────────────────────────────────────────────────────────
def _png(width: int = 80, height: int = 40) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), "white")
    ImageDraw.Draw(img).text((4, 12), "FIG", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pptx(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Width and Resolution Multipliers"
    # Due shape sulla stessa riga visiva: vince l'ascissa.
    left = slide.shapes.add_textbox(Inches(0.5), Inches(2), Inches(3), Inches(0.6))
    left.text_frame.text = "left block"
    right = slide.shapes.add_textbox(Inches(5), Inches(2.02), Inches(3), Inches(0.6))
    right.text_frame.text = "right block"
    lower = slide.shapes.add_textbox(Inches(0.5), Inches(3.2), Inches(3), Inches(0.6))
    lower.text_frame.text = "alpha = 0.75"
    slide.notes_slide.notes_text_frame.text = "Ricorda che alpha scala tutti i layer"
    slide.shapes.add_picture(io.BytesIO(_png()), Inches(7), Inches(4), Inches(1.5))

    second = prs.slides.add_slide(prs.slide_layouts[5])
    second.shapes.title.text = "Cost-aware DNAS"
    path = tmp_path / "deck.pptx"
    prs.save(str(path))
    return path


def test_a_pptx_page_keeps_text_notes_and_pictures_apart(tmp_path):
    path = _pptx(tmp_path)
    pages = slideparser.parse_pptx(path, tmp_path / "doc")

    assert len(pages) == 2
    text = pages[0]["text"]
    assert text.index("Width and Resolution Multipliers") < text.index("left block")
    assert text.index("left block") < text.index("right block")
    assert text.index("right block") < text.index("alpha = 0.75")
    # Le note speaker non sono un bullet della pagina: restano riconoscibili.
    assert "Speaker notes: Ricorda che alpha scala tutti i layer" in text
    assert pages[0]["n_images"] == 1
    # Senza PowerPoint non c'è render: il documento resta leggibile lo stesso.
    assert pages[0]["render_path"] is None


def test_pptx_paragraphs_are_not_glued_together(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(3))
    frame = box.text_frame
    frame.text = "First bullet"
    frame.add_paragraph().text = "Second bullet"
    path = tmp_path / "bullets.pptx"
    prs.save(str(path))

    pages = slideparser.parse_pptx(path, tmp_path / "doc")
    assert pages[0]["text"].splitlines()[:2] == ["First bullet", "Second bullet"]


# ── DOCX ─────────────────────────────────────────────────────────────────────
def test_a_docx_is_one_page_with_paragraphs_and_tables(tmp_path):
    import docx

    document = docx.Document()
    document.add_paragraph("Reti neurali e convoluzioni")
    document.add_paragraph("Il capitolo spiega la convoluzione depthwise.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "layer"
    table.cell(0, 1).text = "canali"
    table.cell(1, 0).text = "Conv3x3"
    table.cell(1, 1).text = "32"
    path = tmp_path / "dispensa.docx"
    document.save(str(path))

    pages = slideparser.parse_docx(path, tmp_path / "doc")

    assert len(pages) == 1
    assert "convoluzione depthwise" in pages[0]["text"]
    assert "Conv3x3 | 32" in pages[0]["text"]


# ── Immagini e dispatch ──────────────────────────────────────────────────────
def test_an_uploaded_image_is_ocr_ed(tmp_path, monkeypatch):
    monkeypatch.setattr(slideparser, "ocr_lines", lambda path, engine=None: [
        {"text": "SuperNet-based DNAS", "x0": 10, "y0": 10, "x1": 300, "y1": 40}])
    path = tmp_path / "slide.png"
    path.write_bytes(_png(200, 100))

    pages = slideparser.parse_image(path, tmp_path / "doc")

    assert pages[0]["text"] == "SuperNet-based DNAS"
    assert pages[0]["has_text"] is False
    assert pages[0]["render_path"] and pages[0]["render_path"].endswith(".png")


def test_an_unknown_format_is_rejected_with_the_allowed_list(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("niente")
    with pytest.raises(ValueError, match="Unsupported slide format"):
        slideparser.parse(path, tmp_path / "doc")


def test_a_missing_library_says_how_to_install_it():
    with pytest.raises(slideparser.MissingDependency, match="pip install"):
        slideparser._require("modulo_inesistente", "pacchetto-inesistente")
