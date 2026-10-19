"""
Il pass VLM sulle pagine complesse.

Il client è finto (la rete non si tocca) ma il resto è vero: la scelta delle
pagine, la cache, la marcatura `math_uncertain` e il fatto che una VLM assente
non debba rompere un parsing già riuscito.
"""

from __future__ import annotations

import pytest

from trascrivi import config, db, vlm

PAGE_MD = """## Testo
Width and Resolution Multipliers
• Width multiplier ($\\alpha$): uniform reduction of the num. of output channels
α = 0.75

## Figure
- three variants: α=1 (L1 K=8, L2 K=4), α=0.75 (L1 K=6, L2 K=3)

## Annotazioni
Part-II: AI Computing Optimization
"""


def fake_vlm(monkeypatch, content: str = PAGE_MD, *, raises: Exception | None = None):
    """Client finto: registra le chiamate e restituisce `content`."""
    calls: list[dict] = []

    class _Message:
        def __init__(self, text):
            self.content = text

    class _Choice:
        def __init__(self, text):
            self.message = _Message(text)
            self.finish_reason = "stop"

    class _Resp:
        def __init__(self, text):
            self.choices = [_Choice(text)]
            self.usage = None

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if raises:
                raise raises
            return _Resp(content)

    client = type("C", (), {
        "chat": type("Chat", (), {"completions": _Completions()})(),
    })()
    monkeypatch.setattr("trascrivi.llm._client", lambda provider: client)
    return calls


@pytest.fixture
def doc_with_pages(project, tmp_path):
    """Un documento con una pagina complessa (con render) e una semplice."""

    def _make(pages=None):
        doc_id = db.create_slide_doc(project, "deck.pdf", ".pdf", "sha-vlm")
        render = tmp_path / "p1.png"
        render.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        rows = pages or [
            {"page_no": 1, "text": "Width multiplier α riduce i canali", "complex": True,
             "has_text": True, "n_images": 0, "render_path": str(render),
             "render_sha": "abc"},
            {"page_no": 2, "text": "Depthwise separable convolutions reduce complexity "
                                   "and this is a plain page with no figures at all",
             "complex": False, "has_text": True, "n_images": 0, "render_path": str(render),
             "render_sha": "abc"},
        ]
        db.save_slide_pages(doc_id, rows)
        db.update_slide_doc(doc_id, {"status": "ready", "n_pages": len(rows)})
        return doc_id

    return _make


def test_auto_mode_takes_only_the_complex_pages(doc_with_pages, monkeypatch):
    calls = fake_vlm(monkeypatch)
    doc_id = doc_with_pages()

    rendered = vlm.enhance_doc(doc_id, config.load_settings())

    assert rendered == 1
    assert len(calls) == 1
    assert db.get_slide_page(doc_id, 1)["vlm_md"].startswith("## Testo")
    assert db.get_slide_page(doc_id, 2)["vlm_md"] is None


def test_on_mode_takes_every_page_that_has_a_render(doc_with_pages, monkeypatch, mock_settings):
    mock_settings(slides_vlm="on")
    calls = fake_vlm(monkeypatch)
    doc_id = doc_with_pages()

    assert vlm.enhance_doc(doc_id, config.load_settings()) == 2
    assert len(calls) == 2


def test_the_cap_limits_the_pages_per_document(doc_with_pages, monkeypatch, mock_settings):
    mock_settings(slides_vlm="on", slides_vlm_max=1)
    fake_vlm(monkeypatch)
    assert vlm.enhance_doc(doc_with_pages(), config.load_settings()) == 1


def test_off_mode_and_disabled_documents_are_skipped(doc_with_pages, monkeypatch, mock_settings):
    calls = fake_vlm(monkeypatch)
    mock_settings(slides_vlm="off")
    assert vlm.enhance_doc(doc_with_pages(), config.load_settings()) == 0
    assert calls == []

    mock_settings(slides_vlm="auto")
    doc_id = doc_with_pages()
    db.update_slide_doc(doc_id, {"vlm_enabled": 0})
    assert vlm.enhance_doc(doc_id, config.load_settings()) == 0
    assert calls == []


def test_a_page_without_a_render_is_not_sent_anywhere(doc_with_pages, monkeypatch, mock_settings):
    mock_settings(slides_vlm="on")
    calls = fake_vlm(monkeypatch)
    doc_id = doc_with_pages([
        {"page_no": 1, "text": "una slide qualunque di parole", "complex": True,
         "has_text": True, "n_images": 0, "render_path": None, "render_sha": None},
    ])

    assert vlm.enhance_doc(doc_id, config.load_settings()) == 0
    assert calls == []


def test_a_rendered_page_is_not_paid_twice(doc_with_pages, monkeypatch, mock_settings):
    """La cache è la regola della generazione: stessi pixel, nessuna seconda chiamata."""
    mock_settings(slides_vlm="on")
    calls = fake_vlm(monkeypatch)
    doc_id = doc_with_pages()

    vlm.enhance_doc(doc_id, config.load_settings())
    first = len(calls)
    vlm.enhance_doc(doc_id, config.load_settings())

    assert len(calls) == first


def test_uncertain_formulas_mark_the_page(doc_with_pages, monkeypatch):
    fake_vlm(monkeypatch, PAGE_MD.replace("α = 0.75", "UNCERTAIN: R(α) = Σ R(α)"))
    doc_id = doc_with_pages()

    vlm.enhance_doc(doc_id, config.load_settings())

    assert db.get_slide_page(doc_id, 1)["math_uncertain"] == 1


def test_a_page_with_math_that_never_got_latex_is_uncertain(doc_with_pages, monkeypatch):
    """
    L'OCR della pagina contiene α e il rendering non ha nessun LaTeX: quella
    formula non esiste in una forma verificabile, quindi non ci si costruiscono
    carte.
    """
    fake_vlm(monkeypatch, "## Testo\nWidth multiplier\n")
    doc_id = doc_with_pages()

    vlm.enhance_doc(doc_id, config.load_settings())

    assert db.get_slide_page(doc_id, 1)["math_uncertain"] == 1


def test_math_transcribed_in_latex_is_verified(doc_with_pages, monkeypatch):
    """Se il rendering ha `$...$` la matematica è stata trascritta: non è incerta."""
    fake_vlm(monkeypatch, "## Testo\nWidth multiplier $\\alpha$ riduce i canali\n")
    doc_id = doc_with_pages()

    vlm.enhance_doc(doc_id, config.load_settings())

    assert db.get_slide_page(doc_id, 1)["math_uncertain"] == 0


def test_a_clean_page_is_not_marked_uncertain(doc_with_pages, monkeypatch):
    fake_vlm(monkeypatch)
    doc_id = doc_with_pages()

    vlm.enhance_doc(doc_id, config.load_settings())

    assert db.get_slide_page(doc_id, 1)["math_uncertain"] == 0


def test_a_broken_vlm_does_not_break_the_document(doc_with_pages, monkeypatch):
    """Ollama spento o modello non scaricato non devono far fallire il parsing."""
    fake_vlm(monkeypatch, raises=ConnectionError("ollama non raggiungibile"))
    doc_id = doc_with_pages()

    assert vlm.enhance_doc(doc_id, config.load_settings()) == 0
    assert db.get_slide_doc(doc_id)["status"] == "ready"


def test_no_ollama_means_no_vlm(doc_with_pages, monkeypatch):
    calls = fake_vlm(monkeypatch)
    monkeypatch.setattr(db, "get_provider_by_name", lambda name: None)
    doc_id = doc_with_pages()

    assert vlm.enhance_doc(doc_id, config.load_settings()) == 0
    assert calls == []


def test_mark_uncertain_reads_both_signals():
    assert vlm.mark_uncertain("## Testo\nUNCERTAIN: R = Σ", "") == 1
    assert vlm.mark_uncertain("## Testo\n$\\alpha = 0.75$", "α = 0.75") == 0
    assert vlm.mark_uncertain("## Testo\nsolo parole", "nessun simbolo") == 0
    # Matematica nell'OCR, nessun LaTeX nel rendering: non verificata.
    assert vlm.mark_uncertain("## Testo\nsolo parole", "α e θ") == 1


def test_the_prompt_asks_for_sections_and_forbids_appearance():
    """
    Il prompt è il contratto con il modello, e l'A/B sui campioni reali ha
    mostrato cosa succede quando è vago: figure descritte per colori e sfere
    inventate. Questi vincoli non devono sparire per sbaglio.
    """
    prompt = vlm.SYSTEM_PROMPT
    for section in ("## Testo", "## Figure", "## Annotazioni"):
        assert section in prompt, section
    assert "UNCERTAIN" in prompt
    assert "LaTeX" in prompt
    assert "no colours" in prompt
    assert "Never invent" in prompt
    assert "reading order" in prompt
