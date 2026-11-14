"""Le traduzioni: stesse chiavi nelle due lingue, e ogni chiave usata dal frontend esiste."""

from __future__ import annotations

import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def _load(code: str) -> dict:
    return json.loads((WEB / "i18n" / f"{code}.json").read_text(encoding="utf-8"))


def test_italian_has_exactly_the_english_keys():
    en, it = _load("en"), _load("it")
    assert sorted(set(en) - set(it)) == []
    assert sorted(set(it) - set(en)) == []


def test_plural_entries_have_the_same_shape():
    en, it = _load("en"), _load("it")
    for key, value in en.items():
        if isinstance(value, dict):
            assert isinstance(it[key], dict) and "other" in value and "other" in it[key], key


def test_every_key_used_by_the_frontend_exists():
    en = _load("en")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r"""\bt\(\s*["']([a-zA-Z0-9_.-]+)["']""", js))
    used |= set(re.findall(r'data-i18n="([^"]+)"', html))
    used |= {k.split(":")[1].strip() for attr in re.findall(r'data-i18n-attr="([^"]+)"', html)
             for k in attr.split(";")}
    assert sorted(used - set(en)) == []


def test_placeholders_match_between_languages():
    en, it = _load("en"), _load("it")
    names = lambda v: sorted(re.findall(r"\{(\w+)\}", json.dumps(v)))  # noqa: E731
    for key in en:
        assert names(en[key]) == names(it[key]), key


def test_changelog_matches_the_app_version_and_has_both_languages():
    from trascrivi import __version__
    entries = json.loads((WEB / "changelog.json").read_text(encoding="utf-8"))
    assert entries[0]["version"] == __version__, "aggiorna web/changelog.json insieme alla versione"
    for entry in entries:
        assert entry["en"] and len(entry["en"]) == len(entry["it"]), entry["version"]
