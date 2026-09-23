"""
Renderer Markdown del riassunto (`web/markdown.js`).

Il riassunto è un documento Markdown e questo è il codice che lo trasforma in
qualcosa di leggibile: titoli, liste, tabelle, schemi. È la parte che l'utente
guarda davvero, quindi si esegue per davvero — con node, sul file vero — invece
di controllarne solo la sintassi.

Il file è una funzione pura e non tocca il DOM: è esattamente per questo che sta
in `markdown.js` e non dentro `app.js`.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from trascrivi import config

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node non installato")

MARKDOWN_JS = config.WEB / "markdown.js"


def render(markdown: str) -> str:
    """Esegue il renderer vero, in node, e restituisce l'HTML."""
    script = (
        f"const {{ renderMarkdown }} = require({json.dumps(str(MARKDOWN_JS))});"
        f"process.stdout.write(renderMarkdown({json.dumps(markdown)}));"
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_headings_get_their_level():
    html = render("# Title\n\n## Section\n\n### Detail")

    assert "<h1>Title</h1>" in html
    assert "<h2>Section</h2>" in html
    assert "<h3>Detail</h3>" in html


def test_bullets_become_a_list():
    html = render("- one\n- two\n- three")

    assert html.count("<li>") == 3
    assert html.startswith("<ul class=\"md-list\">")
    assert html.endswith("</ul>")


def test_a_blank_line_and_a_heading_close_the_list():
    """Senza chiudere la lista, il titolo successivo finirebbe dentro il `<ul>`."""
    html = render("- one\n\n## Section")

    assert html.index("</ul>") < html.index("<h2>")


def test_numbered_items_become_an_ordered_list():
    html = render("1. first\n2. second")

    assert html.startswith("<ol class=\"md-list\">")
    assert html.count("<li>") == 2


def test_nested_bullets_are_indented_not_nested_lists():
    """`<ul>` dentro `<ul>` è HTML non valido: si usa un rientro."""
    html = render("- outer\n  - inner")

    assert html.count("<ul") == 1
    assert "margin-left:18px" in html


def test_a_table_needs_the_separator_row():
    with_table = render("| A | B |\n|---|---|\n| 1 | 2 |")
    without = render("| A | B |\n| 1 | 2 |")

    assert "<table>" in with_table
    assert "<th>A</th>" in with_table and "<td>1</td>" in with_table
    assert "<table>" not in without


def test_a_fenced_block_is_preserved_verbatim():
    """È dove finiscono gli schemi: gli spazi devono restare quelli scritti."""
    diagram = "```\n  client\n     |\n  server\n```"

    html = render(diagram)

    assert "<pre><code>" in html
    assert "  client\n     |\n  server" in html


def test_inline_formatting():
    html = render("some **bold** and *italic* and `code` here")

    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>code</code>" in html


def test_html_from_the_model_is_escaped_not_executed():
    """
    Il documento lo scrive un modello: se ci finisce dell'HTML deve vedersi come
    testo. Il renderer escapa prima di formattare, quindi non può sfuggire.
    """
    html = render("A paragraph with <script>alert(1)</script> inside")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_link_stays_text():
    """Non si generano link: sono la via più facile per un `javascript:`."""
    html = render("see [here](javascript:alert(1)) for more")

    assert "<a " not in html
    assert "javascript:alert(1)" in html


def test_consecutive_paragraph_lines_are_joined():
    html = render("first line\nsecond line\n\nnew paragraph")

    assert html.count("<p>") == 2
    assert "<p>first line second line</p>" in html


def test_the_rendered_document_has_the_expected_skeleton():
    """Il caso reale: un documento con titolo, sezioni, lista, tabella e chiusura."""
    document = (
        "# Constrained design\n\n"
        "The lecture introduces constrained design.\n\n"
        "## The constraint\n\n"
        "- At the edge the limit is the **battery**\n"
        "- In the cloud it is cost\n\n"
        "| Where | Constraint |\n|---|---|\n| Edge | battery |\n| Cloud | cost |\n\n"
        "## Left open\n\n"
        "The energy budget was promised for next time.\n"
    )

    html = render(document)

    assert html.count("<h2>") == 2
    assert "<ul class=\"md-list\">" in html
    assert "<table>" in html
    assert "<strong>battery</strong>" in html
    # Due paragrafi: l'apertura e la chiusura. Il resto sono titoli, lista e tabella.
    assert html.count("<p>") == 2
