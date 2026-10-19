"""A/B manuale della VLM sulle slide reali (Ollama locale, costo zero).

Uso:
    .venv\\Scripts\\python.exe tools\\vlm_ab.py [modello] [--samples N]

Non è un test: è il gate di qualità del piano (Step 4). Misura, per ogni campione
in `slide_samples/`, quanto tempo prende la pagina, quanto è verbosa, e se i
simboli e le formule che contano ci sono davvero. Il modello da 4B è lento di
qualche secondo a pagina e sbaglia le figure quando il prompt è vago: questo
script dice di quanto, invece di fidarsi.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trascrivi import config, vlm  # noqa: E402

# I fatti che una pagina *deve* riportare, scritti come li scriverebbe un umano.
# Non sono "parole chiave del testo": sono i simboli che l'OCR sbaglia (α, θ, Σ,
# apici) e le relazioni che stanno nel diagramma e non nel testo.
CHECKS: dict[str, list[tuple[str, str]]] = {
    "image1.png": [                       # p.17 Depthwise Separable Convolutions
        ("titolo", r"Depthwise Separable Convolutions"),
        ("FX=FY=1", r"FX\s*=\s*FY\s*=\s*1"),
        ("depthwise", r"(?i)depthwise"),
        ("pointwise", r"(?i)pointwise"),
        ("source citata", r"(?i)Bai"),
    ],
    "image2.png": [                       # p.25 Width and Resolution Multipliers
        ("titolo", r"Width and Resolution Multipliers"),
        ("α = 1", r"α\s*=\s*1"),
        ("α = 0.75", r"0?\.75"),
        ("α = 0.5", r"α\s*=\s*0?\.5"),
        ("canali 8/4", r"8.{0,20}4"),
        ("canali 6/3", r"6.{0,20}3"),
        ("canali 4/2", r"4.{0,20}2"),
    ],
    "image3.png": [                       # p.55 SuperNet-based DNAS
        ("titolo", r"SuperNet"),
        ("shape input", r"3,\s*32,\s*32"),
        ("shape output", r"32,\s*16,\s*16"),
        ("operatori", r"Conv3x3.{0,40}Conv5x5.{0,40}(DW3x3|DW)"),
        ("combiner", r"(?i)combiner"),
        ("pool", r"(?i)maxpool|avgpool"),
        ("identity", r"(?i)identity"),
    ],
}


def sections(markdown: str) -> dict[str, str]:
    """Divide il markdown nelle sezioni `## Nome` prodotte dal prompt."""
    out: dict[str, str] = {}
    current = ""
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower()
            out[current] = ""
        elif current:
            out[current] += line + "\n"
    return out


def evaluate(name: str, markdown: str) -> tuple[int, int, list[str]]:
    found = 0
    missing: list[str] = []
    checks = CHECKS.get(name, [])
    for label, pattern in checks:
        if re.search(pattern, markdown):
            found += 1
        else:
            missing.append(label)
    return found, len(checks), missing


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "gemma3:4b"
    samples = sorted((ROOT / "slide_samples").glob("*.png"))
    if not samples:
        raise SystemExit("Nessun campione in slide_samples/")

    provider = {"name": "ollama", "base_url": "http://localhost:11434/v1", "model": model}
    rows: list[tuple[str, float, int, int, str, int, int, list[str]]] = []
    for path in samples:
        image = vlm.encode_image(str(path))
        if not image:
            continue
        t0 = time.time()
        markdown, diag = vlm.describe_page(provider, image, model)
        elapsed = time.time() - t0
        parts = sections(markdown)
        figure_lines = len([l for l in parts.get("figure", "").splitlines() if l.strip()])
        found, total, missing = evaluate(path.name, markdown)
        rows.append((path.name, elapsed, len(markdown), figure_lines,
                     ", ".join(sorted(parts)) or "—", found, total, missing))
        print(f"\n===== {path.name} — {elapsed:.1f}s =====")
        print(markdown)

    print("\n\n## A/B — modello", model)
    print("| campione | secondi | caratteri | righe figura | sezioni | fatti trovati |")
    print("|---|---|---|---|---|---|")
    for name, elapsed, chars, figure_lines, found_sections, found, total, _missing in rows:
        print(f"| {name} | {elapsed:.1f} | {chars} | {figure_lines} | "
              f"{found_sections} | {found}/{total} |")
    for name, _elapsed, _chars, _figure_lines, _found_sections, found, total, missing in rows:
        if missing:
            print(f"\n- {name}: mancano {', '.join(missing)}")
    total_found = sum(r[5] for r in rows)
    total_checks = sum(r[6] for r in rows)
    print(f"\nTotale fatti: {total_found}/{total_checks}")


if __name__ == "__main__":
    main()
