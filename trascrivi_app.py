#!/usr/bin/env python
"""
trascrivi_app.py
================
Avvia la piattaforma web: server, coda dei job e browser.

Uso:
    .venv\\Scripts\\python.exe trascrivi_app.py
    .venv\\Scripts\\python.exe trascrivi_app.py --port 8010 --no-browser

Modifica il codice del server? `--dev` attiva il reload di uvicorn (il server
si riavvia da solo, i job in corso vengono marcati come interrotti).
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser

from trascrivi import config

log = logging.getLogger("trascrivi")


def _make_console_utf8_safe() -> None:
    """
    Evita i crash da encoding sulla console Windows.

    Su un `cmd` con code page 1252 stampare `→` o un'emoji solleva
    UnicodeEncodeError e uccide il server appena avviato (bug visto lanciando
    run.bat). Qui forziamo UTF-8 con `backslashreplace`: il testo resta leggibile
    e nessuna stampa può più far cadere il processo.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(preferred: int, host: str = "127.0.0.1", tries: int = 10) -> int:
    """Prima porta libera a partire da quella preferita (8000, 8001, …)."""
    for port in range(preferred, preferred + tries):
        if _port_free(port, host):
            return port
    raise SystemExit(f"ERRORE: nessuna porta libera fra {preferred} e {preferred + tries}.")


def _banner(url: str) -> None:
    """
    Riga di avvio leggibile su qualunque console.

    L'emoji è di ripiego ma l'informazione che conta è l'URL: se il code page
    della console non la supporta, si stampa comunque tutto il resto.
    """
    try:
        print(f"\n  Trascrivi  ->  {url}\n  (Ctrl+C per fermare il server)\n")
    except UnicodeEncodeError:
        print(f"\n  Trascrivi  ->  {url}\n  (Ctrl+C per fermare il server)\n"
              .encode("ascii", "replace").decode("ascii"))


def open_browser(url: str, delay: float = 1.2) -> None:
    """Apre il browser senza bloccare l'avvio del server."""
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - l'apertura del browser è un extra
            log.warning("impossibile aprire il browser: %s", exc)
    threading.Thread(target=_open, daemon=True).start()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Avvia la piattaforma Trascrivi.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="interfaccia di ascolto (default: 127.0.0.1, solo locale)")
    ap.add_argument("--port", type=int, default=8000,
                    help="porta preferita (default: 8000; se occupata prova la successiva)")
    ap.add_argument("--no-browser", action="store_true", help="non aprire il browser")
    ap.add_argument("--dev", action="store_true", help="reload automatico (sviluppo)")
    args = ap.parse_args(argv)

    _make_console_utf8_safe()
    config.load_env_file()
    config.ensure_dirs()
    config.setup_file_logging()

    import uvicorn

    port = pick_port(args.port, args.host)
    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{port}/"

    log.info("Trascrivi in ascolto su %s", url)
    _banner(url)
    if not args.no_browser:
        open_browser(url)

    # La UI fa polling ogni 1,5 s: i log di accesso di uvicorn diventerebbero
    # centinaia di righe al minuto e nasconderebbero gli errori veri. Restano i
    # log dell'applicazione (data/logs/app.log e stderr).
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"format": "%(levelname)s: %(message)s"}},
        "handlers": {"default": {"class": "logging.StreamHandler", "formatter": "default"}},
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
            "httpx": {"handlers": [], "level": "WARNING", "propagate": False},
            "httpx2": {"handlers": [], "level": "WARNING", "propagate": False},
        },
    }

    if args.dev:
        uvicorn.run("trascrivi.api:create_app", factory=True, host=args.host, port=port,
                    reload=True, reload_dirs=[str(config.ROOT / "trascrivi")],
                    log_level="info", log_config=log_config)
    else:
        uvicorn.run("trascrivi.api:create_app", factory=True, host=args.host, port=port,
                    log_level="info", log_config=log_config)


if __name__ == "__main__":
    sys.exit(main())
