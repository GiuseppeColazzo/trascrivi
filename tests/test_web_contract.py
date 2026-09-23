"""
Guardie sull'interfaccia web: niente browser, solo coerenza fra file e API.

Non sostituiscono un collaudo manuale, ma bloccano le rotture che si vedono solo
a pagina aperta: un `byId("x")` senza il relativo elemento in index.html, un
endpoint chiamato dal frontend che non esiste più, o un modulo JS con un errore
di sintassi.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from trascrivi import config
from trascrivi.api import create_app


@pytest.fixture()
def html() -> str:
    return (config.WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture()
def js() -> str:
    return (config.WEB / "app.js").read_text(encoding="utf-8")


def test_index_references_the_app_assets(html):
    assert '/app.js' in html
    assert '/style.css' in html
    # Il routing è a hash: la pagina viene servita solo alla radice.
    assert 'data-theme' in html


def test_static_ids_used_by_the_app_exist_in_the_markup(html, js):
    """Ogni byId(...) deve corrispondere a un id presente nell'HTML o creato in JS."""
    used = set(re.findall(r'byId\("([A-Za-z0-9_-]+)"\)', js))
    used |= set(re.findall(r"byId\('([A-Za-z0-9_-]+)'\)", js))
    assert used, "il frontend non usa byId: il test non sta verificando nulla"

    missing = []
    for element_id in sorted(used):
        if f'id="{element_id}"' in html:
            continue
        # Gli id dinamici sono creati da el(...) con id: "nome".
        if re.search(rf'id:\s*"{re.escape(element_id)}"', js):
            continue
        missing.append(element_id)
    assert missing == []


def test_theme_toggle_persists_the_choice(html, js):
    assert "trascrivi-theme" in html   # applicato prima del CSS, niente sfarfallio
    assert "trascrivi-theme" in js     # salvato/riletto dal toggle
    assert "prefers-color-scheme" in html


def test_every_api_path_used_by_the_frontend_is_routed(js):
    """Le stringhe `/api/...` del frontend devono esistere nell'app FastAPI."""
    # Il gruppo cattura solo la parte dopo /api/: si ricompone il path completo.
    paths = {f"/api/{p.rstrip('/')}" for p in re.findall(
        r"/api/([a-zA-Z0-9_/{}\-?=&.]*?)(?=[`'\"$\s)]|$)", js) if p.rstrip("/")}
    assert paths, "nessuna chiamata API trovata nel frontend"

    # Lo schema OpenAPI è la fonte affidabile: FastAPI recente non appiattisce
    # le rotte incluse in `app.routes`.
    routes = sorted(p for p in create_app().openapi()["paths"] if p.startswith("/api"))
    assert routes, "l'app non espone rotte /api: il test non sta verificando nulla"

    def normalize(route: str) -> str:
        return re.sub(r"\{[^}]+\}", "*", route)

    normalized = {normalize(r) for r in routes}
    unknown = []
    for raw in sorted(paths):
        candidate = raw.split("?")[0]
        concrete = re.sub(r"\d+", "*", re.sub(r"\$\{[^}]+\}", "*", candidate))
        if candidate in routes or candidate in normalized or concrete in normalized:
            continue
        unknown.append(raw)
    assert unknown == [], f"chiamate a endpoint inesistenti: {unknown}"


def test_view_new_lecture_wires_handlers_without_late_byId_lookups(js):
    """
    Regressione: in `viewNewLecture` gli elementi vengono costruiti da `el(...)`
    **prima** di finire nel documento, quindi un `byId(...)` eseguito in quel
    momento restituisce null. Era il bug del selettore file: il dialogo si apriva
    ma l'evento `change` non era agganciato a nessun listener e la selezione
    veniva persa senza errori visibili (l'optional chaining inghiottiva il null).
    """
    start = js.index("async function viewNewLecture")
    end = js.index("async function viewJobs")
    body = js[start:end]
    # Via le righe di solo commento (incluso quello che cita il bug): qui
    # interessa il codice eseguito. Non si tocca il resto, perché togliere i
    # commenti in linea romperebbe le stringhe che contengono "//".
    code = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("//"))

    # L'input file deve nascere con il suo handler, non con un byId successivo.
    assert 'id: "fileInput"' in code
    assert "onchange:" in code
    assert "fileInput" not in set(re.findall(r'byId\("([^"]+)"\)', code))
    assert "uploadProgress" not in set(re.findall(r'byId\("([^"]+)"\)', code))
    # La barra di avanzamento dell'upload deve esistere e contenere il riempimento.
    assert "progressBox" in code and "progressFill" in code


def test_frontend_js_has_valid_syntax():
    node = shutil.which("node")
    if not node:
        pytest.skip("node non installato: controllo di sintassi saltato")
    result = subprocess.run([node, "--check", str(config.WEB / "app.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_stylesheet_defines_both_themes():
    css = (config.WEB / "style.css").read_text(encoding="utf-8")
    assert ":root" in css
    assert ':root[data-theme="dark"]' in css
    assert "color-scheme" in css
