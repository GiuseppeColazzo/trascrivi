"""Dataset finto per gli screenshot e le prove manuali offline (dev-only).

Un corso, tre lezioni, un riassunto, un deck con pagine anche complesse, due
carte. Nessuna chiamata di rete: serve a guardare la UI senza toccare il `data/`
reale, quindi prende la cartella su cui lavorare come argomento.

    .venv\\Scripts\\python.exe tools\\seed_ui_demo.py D:\\tmp\\trascrivi-demo
"""

from __future__ import annotations

import sys
from pathlib import Path


def _remap(data_dir: Path) -> None:
    """Sposta i percorsi di `config` sotto `data_dir` (stessa tecnica di conftest)."""
    from trascrivi import config

    data_dir = Path(data_dir).resolve()
    config.DATA = data_dir
    config.SOURCES = data_dir / "sources"
    config.TRANSCRIPTS = data_dir / "transcripts"
    config.PARTIAL = data_dir / "partial"
    config.LOGS = data_dir / "logs"
    config.BACKUPS = data_dir / "backup"
    config.SLIDES = data_dir / "slides"
    config.DB_PATH = data_dir / "trascrivi.db"
    config.SECRET_KEY_PATH = data_dir / "secret.key"
    config.SETTINGS_PATH = data_dir / "settings.json"
    config._LOG_CONFIGURED = True
    config.ensure_dirs()


LECTURES = [
    ("Lecture 1 — Depthwise separable convolutions",
     "Separable convolution, from standard conv to depthwise plus pointwise.\n"
     "The cost drops from FX*FY*C*K to two much cheaper stages.\n"
     "MobileNetV1 is the canonical example of this trade-off.",
     "Depthwise Separable Convolutions",
     "# Separable convolutions\n\nA standard convolution mixes channels: every one of the "
     "K output channels reads all C input channels.\n\n* **Depthwise**: one filter per input "
     "channel, $C \\times FX \\times FY$ weights\n* **Pointwise**: 1x1 convolution that mixes "
     "channels, $C \\times K$ weights\n* the two together cost roughly $1/K + 1/FX^2$ of the "
     "standard layer\n\n**MobileNetV1** stacks these blocks, and that is why it runs on a phone."),
    ("Lecture 2 — Width and resolution multipliers",
     "Two multipliers, one for the channels and one for the input resolution, plus the "
     "reduction of the KV cache.",
     "Width and Resolution Multipliers",
     "# Width multipliers\n\nThe width multiplier $\\alpha$ scales the number of channels of "
     "every layer; the resolution multiplier scales the input image.\n\n* alpha=1, L1 K=8, L2 K=4\n"
     "* alpha=0.75, L1 K=6, L2 K=3\n* alpha=0.5, L1 K=4, L2 K=2\n\n**Reducing the KV cache**: "
     "keys and values are shared between all the heads in MQA."),
    ("Lecture 3 — Cost-aware DNAS",
     "Differentiable architecture search with a cost term: the regularizer weights the size "
     "of every layer.",
     "SuperNet-based DNAS",
     "# Cost-aware DNAS\n\nA SuperNet holds alternative operators per layer and the search "
     "picks one per layer.\n\n* the regularizer is $R(\\alpha) = \\sum_l R^{(l)}(\\alpha^{(l)})$\n"
     "* $R^{(l)}$ estimates the expected number of weights of layer l\n"
     "* standard conv costs $FX*FY*C*K$, depthwise plus pointwise much less"),
]

CARDS = [
    {"kind": "cloze", "front": "Con width multiplier $\\alpha = 0.75$ i canali di L1 e L2 "
                               "diventano {{c1::6 e 3}}", "back": "", "slide_ref": 25},
    {"kind": "qa", "front": "Che cosa riduce il width multiplier di MobileNetV1?",
     "back": "Il numero di canali di output di tutti i layer, a costo di accuratezza.",
     "slide_ref": 25},
]


def _render_page(path: Path, title: str, lines: list[str], *, accent: str = "#4fd08a") -> None:
    """Un PNG che sembra una slide: serve a vedere le anteprime, non a fare OCR."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (960, 540), "#f6f6f2")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 60, 960, 68], fill=accent)
    draw.text((40, 24), title, fill="#1a1a1a")
    y = 100
    for line in lines:
        draw.text((60, y), f"— {line}", fill="#3a3a3a")
        y += 26
    for row in range(3):
        for col in range(4):
            draw.rectangle([560 + col * 70, 300 + row * 60, 610 + col * 70, 340 + row * 60],
                           fill=accent if (row + col) % 2 else "#e0b030")
    draw.text((840, 510), "ECAI", fill="#8a8a86")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


def seed(data_dir: Path, *, remap: bool = True) -> dict:
    """
    Crea il dataset e restituisce gli id utili (corso, lezioni, deck).

    `remap=False` usa il `config` già attivo: serve ai test, che hanno la loro
    cartella temporanea e non vogliono che il seed tocchi i percorsi globali.
    """
    if remap:
        _remap(data_dir)
    from trascrivi import db

    db.init_db()
    project_id = db.create_project("Efficient Computing", code="ECAI",
                                   description="Part-II: AI Computing Optimization")

    transcript_ids = []
    for index, (title, text, slide_title, markdown) in enumerate(LECTURES, start=1):
        segments = [{"start": i * 6.0, "end": i * 6.0 + 5.0, "text": line}
                    for i, line in enumerate(text.splitlines())]
        transcript_id = db.create_transcript(
            project_id, title, text, segments, language="en", model="distil-large-v3",
            duration=5400.0, description=f"Lecture {index} of the course.")
        transcript_ids.append(transcript_id)
        db.create_summary(transcript_id, style="study", language="en", provider="ollama",
                          model="qwen3.5:4b", title=slide_title, overview=markdown[:200],
                          markdown=markdown, n_words=len(markdown.split()), target_words=400,
                          source_words=len(text.split()), cost_usd=0.0)

    deck_project_id = project_id
    doc_id = db.create_slide_doc(deck_project_id, "ecai-part2.pdf", ".pdf", "demo-sha")
    slides_dir = Path(data_dir) / "slides" / str(deck_project_id) / str(doc_id) / "pages"
    pages = []
    for number in range(1, 7):
        title, lines, complex_page = {
            1: ("Depthwise Separable Convolutions", ["separate a multi-channel conv into C single-channel convs",
                                                     "alternate with a pointwise 1x1 conv", "reduce complexity"], False),
            2: ("Width and Resolution Multipliers", ["width multiplier alpha", "alpha = 1", "alpha = 0.75",
                                                     "alpha = 0.5"], True),
            3: ("Reducing KV Cache Size", ["MQA: one head of keys and values", "L, S, H, P legend",
                                          "dashed lines show the sharing"], True),
            4: ("SuperNet-based DNAS", ["search space", "one operator per layer", "combiner"], True),
            5: ("Cost-aware DNAS", ["R(alpha) = sum R(alpha)", "weights of standard conv",
                                    "DW + PW", "identity: 0"], True),
            6: ("Course logistics", ["exam dates", "office hours"], False),
        }[number]
        render = slides_dir / f"p{number:04d}.png"
        _render_page(render, title, lines, accent="#4fd08a" if number % 2 else "#7aa2f7")
        pages.append({
            "page_no": number, "text": "\n".join([title, *lines]), "complex": complex_page,
            "has_text": True, "n_images": 1 if complex_page else 0, "ocr_chars": 0,
            "render_path": str(render), "render_sha": f"demo{number}",
            "vlm_md": (f"## Testo\n{title}\n" + "\n".join(f"* {line}" for line in lines))
            if complex_page else None,
            "math_uncertain": 1 if number == 5 else 0,
        })
    db.save_slide_pages(doc_id, pages)
    db.update_slide_doc(doc_id, {"status": "ready", "n_pages": len(pages),
                                 "boilerplate": db.jdump(["ECAI",
                                                          "Part-II: AI Computing Optimization"])})
    for start in (1, 3):
        db.upsert_link(doc_id, start, start + 1, transcript_ids[1 if start == 1 else 2],
                       score=0.72 if start == 1 else 0.61, source="auto")
    db.upsert_link(doc_id, 5, 6, transcript_ids[2], score=0.58, source="manual")

    counter = 0

    def with_uid(items: list[dict]) -> list[dict]:
        nonlocal counter
        out = []
        for item in items:
            counter += 1
            out.append({**item, "uid": f"demo{counter:028d}"})
        return out

    db.create_cards(transcript_ids[1], with_uid(CARDS))
    db.create_cards(transcript_ids[0], with_uid([
        {"kind": "qa", "front": "Quanti pesi ha una convoluzione depthwise?",
         "back": "Uno per canale di input: C x FX x FY.", "slide_ref": 1},
    ]))
    db.create_card_run(transcript_ids[1], provider="ollama", model="qwen3.5:4b",
                       prompt_version="p1", inputs_hash="demo", n_new=2, status="ok")

    return {"project_id": project_id, "transcripts": transcript_ids, "doc_id": doc_id}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Uso: seed_ui_demo.py <cartella-dati>")
    ids = seed(Path(sys.argv[1]))
    print(f"corso {ids['project_id']}, lezioni {ids['transcripts']}, deck {ids['doc_id']}")


if __name__ == "__main__":
    main()
