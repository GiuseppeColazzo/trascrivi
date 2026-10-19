"""
Il dataset finto degli screenshot, provato come fixture.

`tools/seed_ui_demo.py` serve a guardare la UI senza il `data/` reale: se si
rompe, `tools/shots.py` fallisce con un traceback di Playwright che non dice
niente. Qui si verifica che produca davvero quello che le viste si aspettano.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from trascrivi import config, db

TOOLS = Path(__file__).resolve().parent.parent / "tools"


@pytest.fixture(scope="module")
def seed_module():
    path = TOOLS / "seed_ui_demo.py"
    spec = importlib.util.spec_from_file_location("seed_ui_demo", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["seed_ui_demo"] = module
    spec.loader.exec_module(module)
    return module


def test_the_demo_dataset_fills_every_view(seed_module):
    """Un corso, tre lezioni con riassunto, un deck con pagine, carte e link."""
    ids = seed_module.seed(config.DATA, remap=False)

    project = db.get_project(ids["project_id"])
    assert project and project["name"] == "Efficient Computing"

    transcripts = db.list_transcripts(ids["project_id"])
    assert len(transcripts) == 3
    assert all(t["n_summaries"] == 1 for t in transcripts)

    doc = db.get_slide_doc(ids["doc_id"])
    assert doc["status"] == "ready" and doc["n_pages"] == 6
    pages = db.list_slide_pages(ids["doc_id"])
    assert sum(1 for p in pages if p["complex"]) == 4
    assert any(p["vlm_md"] for p in pages)
    assert any(p["math_uncertain"] for p in pages)
    # Le pagine complesse hanno un render: senza, la UI non ha anteprime.
    assert all(Path(p["render_path"]).exists() for p in pages if p["complex"])

    links = db.list_slide_links(doc_id=ids["doc_id"])
    assert len(links) == 3
    assert {l["source"] for l in links} == {"auto", "manual"}

    assert sum(t["n_cards"] for t in transcripts) == 3


def test_the_tools_do_not_touch_the_real_data_folder(seed_module):
    """
    Il seed scrive solo dove gli si dice: è la ragione per cui esiste
    (`tools/shots.py` punta a una cartella temporanea).
    """
    before = (TOOLS.parent / "data" / "trascrivi.db")
    stamp = before.stat().st_mtime if before.exists() else None
    seed_module.seed(config.DATA, remap=False)
    after = before.stat().st_mtime if before.exists() else None
    assert stamp == after
