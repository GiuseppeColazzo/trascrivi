"""Screenshot delle viste chiave, senza toccare il `data/` reale (dev-only).

Avvia l'app in-process su una cartella temporanea con il dataset finto di
`seed_ui_demo.py`, apre le viste con Playwright e salva le PNG in
`tools/shots/out/`. Nessuna chiamata LLM: le viste che le farebbero restano
sulla loro schermata vuota, che è quello che serve per una review del layout.

    .venv\\Scripts\\python.exe tools\\shots.py [--keep] [--only course,cards]

`--keep` lascia la cartella dati temporanea (per guardare le pagine a mano).
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

# Chromium crea i suoi file temporanei dentro la cartella di sistema: qui quella
# cartella può essere vietata (succede con le sandbox), quindi si punta a una
# cartella del repository. Anche i browser stanno nel repo, così l'installazione
# non tocca il profilo dell'utente.
_TMP = ROOT / ".tmp"
_TMP.mkdir(exist_ok=True)
for _var in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_var] = str(_TMP)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".pwbrowsers"))

OUT = Path(__file__).resolve().parent / "shots" / "out"

# Le viste: rotta, nome del file, e cosa aspettarsi di vedere (il selettore su
# cui aspettare evita di fotografare lo scheletro di caricamento).
VIEWS = [
    ("/", "01-library", "main"),
    ("/p/1", "02-course", ".slide-doc"),
    ("/t/1", "03-lecture-content", "[role=tablist]"),
    ("/jobs", "05-jobs", "main"),
    ("/settings", "06-settings", "main"),
    ("/costs", "07-costs", "main"),
]

TAB_VIEWS = [
    # (rotta, selettore della tab, nome file, selettore di attesa)
    ("/t/1", "#tab-cards", "04-lecture-cards", ".card-row, .empty"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as res:
                if res.status == 200:
                    return
        except Exception:  # noqa: BLE001 - si riprova finché il server c'è
            time.sleep(0.2)
    raise SystemExit(f"il server non è partito: {url}")


def main() -> None:
    keep = "--keep" in sys.argv
    only = ""
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    # Anche i dati dello smoke stanno in `.tmp/`: su questa macchina `tempfile`
    # senza `dir` li crea nella cartella corrente, cioè nel repository.
    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="trascrivi-shots-", dir=scratch))
    import seed_ui_demo

    ids = seed_ui_demo.seed(data_dir)

    import uvicorn

    from trascrivi.api import create_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    _wait_for(f"{base}/api/health")

    try:
        from playwright.sync_api import sync_playwright

        OUT.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900},
                                    device_scale_factor=2)
            # Le rotte sono a hash: si attende la vista, non il `load`.
            for route, name, selector in VIEWS:
                if only and not any(part in name for part in only.split(",")):
                    continue
                page.goto(f"{base}/#{route}")
                page.wait_for_selector(selector, timeout=15000)
                page.wait_for_timeout(400)   # icone e transizioni
                page.screenshot(path=str(OUT / f"{name}.png"))
                print("ok", name)

            for route, tab, name, selector in TAB_VIEWS:
                if only and not any(part in name for part in only.split(",")):
                    continue
                page.goto(f"{base}/#{route}")
                page.wait_for_selector(tab, timeout=15000)
                page.click(tab)
                page.wait_for_selector(selector, timeout=15000)
                page.wait_for_timeout(400)
                page.screenshot(path=str(OUT / f"{name}.png"))
                print("ok", name)

            # La sezione slide sta in fondo alla pagina del corso: si fotografa a
            # parte, altrimenti resta fuori dallo schermo.
            if not only or "slides" in only:
                page.goto(f"{base}/#/p/{ids['project_id']}")
                page.wait_for_selector(".slide-doc", timeout=15000)
                page.locator(".slide-doc").first.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                page.screenshot(path=str(OUT / "02b-course-slides.png"))
                print("ok 02b-course-slides")
                # I badge per pagina: leggibili solo aprendo il `<details>`.
                page.locator(".slide-doc details summary").first.click()
                page.wait_for_selector(".page-chip", timeout=15000)
                page.wait_for_timeout(300)
                page.locator(".slide-doc").first.scroll_into_view_if_needed()
                page.screenshot(path=str(OUT / "02c-course-pages.png"))
                print("ok 02c-course-pages")

            # Le impostazioni nuove stanno a metà pagina: si fotografano a parte.
            if not only or "settings" in only:
                page.goto(f"{base}/#/settings")
                page.wait_for_selector("text=Slide e carte", timeout=15000)
                page.get_by_text("Slide e carte").first.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                page.screenshot(path=str(OUT / "06b-settings-slides.png"))
                print("ok 06b-settings-slides")

            # La conferma delle assegnazioni: si apre dal primo deck della pagina.
            if not only or "links" in only:
                page.goto(f"{base}/#/p/{ids['project_id']}")
                page.wait_for_selector(".slide-doc", timeout=15000)
                # `exact=True`: la sezione ha anche "Associa alle lezioni", che
                # lancia il matching e non apre la finestra.
                page.get_by_role("button", name="Associa", exact=True).first.click()
                page.wait_for_selector("dialog[open]", timeout=15000)
                page.wait_for_timeout(600)
                page.screenshot(path=str(OUT / "08-slide-links.png"))
                print("ok 08-slide-links")

            # Il testo letto dalle slide, con il rendering VLM.
            if not only or "texts" in only:
                page.goto(f"{base}/#/p/{ids['project_id']}")
                page.wait_for_selector(".slide-doc", timeout=15000)
                page.get_by_role("button", name="Testi", exact=True).first.click()
                page.wait_for_selector("dialog[open] .page-doc", timeout=15000)
                # La quinta pagina del dataset è complessa, ha il rendering VLM e
                # la formula incerta: è il caso che vale la pena fotografare.
                page.locator("dialog[open] .page-doc summary").nth(4).click()
                page.wait_for_timeout(400)
                page.screenshot(path=str(OUT / "09-slide-texts.png"))
                print("ok 09-slide-texts")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if not keep:
            shutil.rmtree(data_dir, ignore_errors=True)
        else:
            print("cartella dati:", data_dir)
    print("screenshot in", OUT)


if __name__ == "__main__":
    main()
