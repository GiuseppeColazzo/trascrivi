"""
Engine deterministico delle correzioni (`trascrivi.corrections`).

Qui sta il contratto più delicato del progetto: `find` deve esistere nel testo,
gli indici si calcolano sul testo normalizzato e l'applicazione non deve mai
"indovinare" quando una proposta è ambigua.
"""

from __future__ import annotations

from trascrivi.corrections import (
    Plan,
    PlanItem,
    apply_plan,
    apply_terms,
    build_plan,
    chunk_text_with_map,
    dedupe_proposals,
    diff_preview,
    locate_in_spans,
    normalize_for_match,
)


def seg(text: str, start: float = 0.0) -> dict:
    return {"start": start, "end": start + 5.0, "text": text}


def plan_for(texts, proposals, **kwargs):
    segments = [seg(t, i * 5.0) for i, t in enumerate(texts)]
    return build_plan(segments, proposals, **kwargs)


# ── normalizzazione ──────────────────────────────────────────────────────────
def test_normalize_for_match_maps_typographic_chars_one_to_one():
    text = "don\u2019t \u2014 \u201cok\u201d"
    normalized = normalize_for_match(text)

    assert normalized == "don't - \"ok\""
    # apostrofi/virgolette/dash occupano un carattere anche dopo la mappatura
    assert len(normalized) == len(text)
    assert normalize_for_match("a  b") == "a b"


# ── caso base ────────────────────────────────────────────────────────────────
def test_exact_single_replacement():
    plan = plan_for(["we use pie torch today"], [{"id": 1, "find": "pie torch", "replace": "PyTorch"}])

    item = plan.items[0]
    assert item.flag == "ok"
    assert item.count == 1
    assert item.occurrences == [(0, 7, 16)]

    result = apply_plan(plan)
    assert result["applied"] == 1
    assert result["skipped"] == 0
    assert result["changed_segments"] == 1
    assert result["text"] == "we use PyTorch today"
    assert result["segments"][0]["text"] == "we use PyTorch today"
    assert result["details"] == []
    assert result["applied_items"][0]["segments"] == [0]


def test_replacement_only_touches_the_matching_segment():
    plan = plan_for(
        ["we use pie torch", "the lecture starts", "open your notebooks"],
        [{"id": 3, "find": "lecture", "replace": "lesson"}],
    )

    result = apply_plan(plan)

    assert result["applied"] == 1
    assert result["changed_segments"] == 1
    assert [s["text"] for s in result["segments"]] == [
        "we use pie torch",
        "the lesson starts",
        "open your notebooks",
    ]
    # i timestamp dei segmenti non toccati restano quelli di partenza
    assert [s["start"] for s in result["segments"]] == [0.0, 5.0, 10.0]


def test_to_dict_and_summary_expose_ui_fields():
    plan = plan_for(
        ["we use pie torch"],
        [{"id": 7, "find": "pie torch", "replace": "PyTorch", "kind": "term",
          "reason": "nome di un framework", "confidence": 0.8}],
    )

    item = plan.items[0]
    as_dict = item.to_dict()

    assert as_dict["proposal_id"] == 7
    assert as_dict["find"] == "pie torch"
    assert as_dict["replace"] == "PyTorch"
    assert as_dict["kind"] == "term"
    assert as_dict["flag"] == "ok"
    assert as_dict["occurrences"] == 1
    assert as_dict["segment_index"] == 0
    assert as_dict["confidence"] == 0.8
    assert "pie torch" in as_dict["context"] and "PyTorch" in as_dict["context"]
    assert plan.by_id(7) is item
    assert plan.by_id(99) is None
    assert plan.summary() == {"ok": 1}


def test_build_plan_normalizes_unknown_kind():
    plan = plan_for(["testo di prova"], [{"id": 1, "find": "testo", "replace": "testo 2",
                                          "kind": "NONSENSE"}])

    assert plan.items[0].kind == "other"


# ── whitespace e caratteri tipografici ───────────────────────────────────────
def test_extra_whitespace_after_the_match_still_applies():
    plan = plan_for(["we use pie torch   a lot"], [{"id": 1, "find": "pie torch",
                                                    "replace": "PyTorch"}])
    item = plan.items[0]

    assert item.flag == "ok"
    # Gli indici sono sul testo ORIGINALE, non su quello normalizzato: la
    # spaziatura successiva al match non entra nella sostituzione.
    assert item.occurrences == [(0, 7, 16)]

    result = apply_plan(plan)
    assert result["applied"] == 1
    assert result["segments"][0]["text"] == "we use PyTorch   a lot"


def test_find_with_trailing_spaces_applies():
    """Uno spazio in coda al `find` non impedisce la sostituzione."""
    plan = plan_for(["we use pie torch a lot"], [{"id": 1, "find": "pie torch  ",
                                                  "replace": "PyTorch"}])

    assert plan.items[0].norm_find == "pie torch"
    assert plan.items[0].flag == "ok"
    result = apply_plan(plan)
    assert result["applied"] == 1
    assert result["segments"][0]["text"] == "we use PyTorch a lot"


def test_double_space_before_match_is_found():
    """
    Whitespace multiplo prima del match non impedisce di trovarlo, e la
    sostituzione non tocca la spaziatura originale (era il bug: indici del testo
    normalizzato applicati al testo originale).
    """
    plan = plan_for(["We  use  pie torch  here"], [{"id": 1, "find": "pie torch",
                                                    "replace": "PyTorch"}])
    assert plan.items[0].flag == "ok"
    assert plan.items[0].occurrences == [(0, 9, 18)]

    result = apply_plan(plan)

    assert result["applied"] == 1
    assert result["segments"][0]["text"] == "We  use  PyTorch  here"


def test_curly_apostrophe_in_transcript_matched_by_straight_apostrophe():
    plan = plan_for(["we don\u2019t use tensor flow"], [{"id": 1, "find": "don't use",
                                                        "replace": "do not use"}])
    assert plan.items[0].flag == "ok"

    result = apply_plan(plan)
    assert result["applied"] == 1
    assert result["segments"][0]["text"] == "we do not use tensor flow"


# ── flag di rifiuto ──────────────────────────────────────────────────────────
def test_unmatched_when_text_is_absent():
    plan = plan_for(["we use pie torch"], [{"id": 1, "find": "Tensorflow",
                                            "replace": "TensorFlow"}])
    assert plan.items[0].flag == "unmatched"
    assert plan.items[0].count == 0

    result = apply_plan(plan)
    assert result["applied"] == 0
    assert result["skipped"] == 1 == len(result["details"])
    assert result["details"][0]["skip_reason"] == "unmatched"
    assert result["applied_items"] == []

    # `accepted=[]` non salta niente: semplicemente non c'è nulla da fare.
    empty = apply_plan(plan, accepted=[])
    assert empty["applied"] == 0 and empty["skipped"] == 0
    assert empty["text"] == "we use pie torch"


def test_word_boundary_half_word_is_unmatched():
    plan = plan_for(["Thistle"], [{"id": 1, "find": "is", "replace": "was"}])
    assert plan.items[0].flag == "unmatched"

    # lo stesso `find` come parola intera viene invece trovato
    ok_plan = plan_for(["This is it"], [{"id": 1, "find": "is", "replace": "was"}])
    assert ok_plan.items[0].flag == "ok"
    assert ok_plan.items[0].count == 1
    assert apply_plan(ok_plan)["text"] == "This was it"


def test_noop_when_find_equals_replace():
    plan = plan_for(["we use pie torch"], [{"id": 1, "find": "pie torch",
                                            "replace": "pie torch"}])
    assert plan.items[0].flag == "noop"

    result = apply_plan(plan)
    assert result["applied"] == 0
    assert result["details"][0]["skip_reason"] == "noop"
    assert result["text"] == "we use pie torch"


def test_too_large_when_replacement_grows_more_than_160_chars():
    plan = plan_for(["we use pie torch"], [{"id": 1, "find": "pie torch",
                                            "replace": "pie torch" + "x" * 161}])
    assert plan.items[0].flag == "too_large"

    result = apply_plan(plan)
    assert result["applied"] == 0
    assert result["details"][0]["skip_reason"] == "too_large"


def test_delta_of_exactly_160_chars_is_still_allowed():
    plan = plan_for(["we use pie torch"], [{"id": 1, "find": "pie torch",
                                            "replace": "pie torch" + "x" * 160}])
    assert plan.items[0].flag == "ok"
    assert apply_plan(plan)["applied"] == 1


def test_identical_proposals_are_flagged_duplicate():
    plan = plan_for(
        ["we use pie torch"],
        [
            {"id": 1, "find": "pie torch", "replace": "PyTorch"},
            {"id": 2, "find": "pie torch", "replace": "PyTorch"},
        ],
    )

    assert [it.flag for it in plan.items] == ["ok", "duplicate"]

    result = apply_plan(plan)
    assert result["applied"] == 1
    assert [d["skip_reason"] for d in result["details"]] == ["duplicate"]
    assert result["text"] == "we use PyTorch"


# ── ambiguità e targets ──────────────────────────────────────────────────────
def test_ambiguous_proposal_requires_an_explicit_target():
    plan = plan_for(
        ["pie torch is fast", "pie torch is also slow"],
        [{"id": 1, "find": "pie torch", "replace": "PyTorch"}],
    )
    item = plan.items[0]
    assert item.flag == "ambiguous"
    assert item.count == 2

    skipped = apply_plan(plan)
    assert skipped["applied"] == 0
    assert skipped["details"][0]["skip_reason"] == "ambiguous"
    assert skipped["text"] == "pie torch is fast\npie torch is also slow"

    chosen = apply_plan(plan, targets={1: 1})
    assert chosen["applied"] == 1
    assert chosen["changed_segments"] == 1
    assert [s["text"] for s in chosen["segments"]] == ["pie torch is fast",
                                                       "PyTorch is also slow"]


def test_ambiguous_target_outside_occurrences_is_skipped():
    plan = plan_for(
        ["pie torch is fast", "pie torch is also slow"],
        [{"id": 1, "find": "pie torch", "replace": "PyTorch"}],
    )

    result = apply_plan(plan, targets={1: 5})

    assert result["applied"] == 0
    assert result["details"][0]["skip_reason"] == "target_not_found"


# ── conflitti ────────────────────────────────────────────────────────────────
def test_overlapping_accepted_proposals_conflict():
    plan = plan_for(
        ["we use pie torch today"],
        [
            {"id": 1, "find": "pie torch", "replace": "PyTorch"},
            {"id": 2, "find": "torch today", "replace": "PyTorch class"},
        ],
    )
    assert [it.flag for it in plan.items] == ["ok", "ok"]

    result = apply_plan(plan)

    assert result["applied"] == 1
    assert [d["skip_reason"] for d in result["details"]] == ["conflict"]
    assert result["details"][0]["proposal_id"] == 2
    assert result["text"] == "we use PyTorch today"


def test_same_span_accepted_twice_is_duplicate_span():
    plan = plan_for(["we use pie torch"], [{"id": 1, "find": "pie torch",
                                            "replace": "PyTorch"}], allow_multiple=True)
    item = plan.items[0]

    result = apply_plan(plan, accepted=[item, item])

    assert result["applied"] == 1
    assert [d["skip_reason"] for d in result["details"]] == ["duplicate_span"]
    assert result["text"] == "we use PyTorch"


def test_two_replacements_in_one_segment_work_in_any_order():
    texts = ["we use pie torch and tensor flow"]
    proposals = [
        {"id": 1, "find": "pie torch", "replace": "PyTorch"},
        {"id": 2, "find": "tensor flow", "replace": "TensorFlow"},
    ]

    forward = apply_plan(plan_for(texts, proposals))
    backward = apply_plan(plan_for(texts, list(reversed(proposals))))

    expected = "we use PyTorch and TensorFlow"
    assert forward["applied"] == 2 and forward["changed_segments"] == 1
    assert forward["text"] == expected
    assert backward["text"] == expected


# ── idempotenza, glossario, mappe, diff ──────────────────────────────────────
def test_reapplying_the_same_plan_is_a_noop():
    proposals = [{"id": 1, "find": "pie torch", "replace": "PyTorch"}]
    first = apply_plan(plan_for(["we use pie torch"], proposals))
    assert first["applied"] == 1

    second_plan = build_plan(first["segments"], proposals)
    assert second_plan.items[0].flag == "unmatched"

    second = apply_plan(second_plan)
    assert second["applied"] == 0
    assert second["text"] == "we use PyTorch"


def test_allow_multiple_replaces_every_occurrence():
    plan = plan_for(["pie torch is pie torch"], [{"id": 1, "find": "pie torch",
                                                  "replace": "PyTorch"}],
                    allow_multiple=True)
    item = plan.items[0]

    assert item.flag == "ok"
    assert item.count == 2

    # la modalità glossario va chiesta anche ad `apply_plan` (`apply_all=True`)
    result = apply_plan(plan, apply_all=True)
    assert result["applied"] == 2
    assert result["changed_segments"] == 1
    assert result["text"] == "PyTorch is PyTorch"
    assert result["details"] == []

    # senza `apply_all` una voce con più occorrenze resta ambigua
    ambiguous = apply_plan(plan)
    assert ambiguous["applied"] == 0
    assert ambiguous["details"][0]["skip_reason"] == "ambiguous"


def test_apply_terms_glossary_mode():
    text = "pie torch and pie torch again"
    new_text, n_applied = apply_terms(text, [{"find": "pie torch", "replace": "PyTorch"}])

    assert new_text == "PyTorch and PyTorch again"
    assert n_applied == 2


def test_apply_terms_without_matches_or_without_terms():
    assert apply_terms("testo", []) == ("testo", 0)
    assert apply_terms("testo", [{"find": "assente", "replace": "x"}]) == ("testo", 0)
    assert apply_terms("testo", [{"find": "   ", "replace": "x"}]) == ("testo", 0)


def test_chunk_text_with_map_and_locate_in_spans():
    segments = [seg("Il modello pie torch"), seg("e' molto veloce", 5.0)]
    joined, spans = chunk_text_with_map(segments)

    assert joined == "Il modello pie torch e' molto veloce"
    assert spans == [(0, 20, 0), (21, 36, 1)]

    assert locate_in_spans(joined, spans, "veloce") == (1, 9)
    # una frase a cavallo di due segmenti viene attribuita al segmento d'inizio
    assert locate_in_spans(joined, spans, "torch e'") == (0, 15)
    # match a metà parola e find vuoto non sono accettati
    assert locate_in_spans(joined, spans, "torc") is None
    assert locate_in_spans(joined, spans, "") is None


def test_dedupe_proposals():
    items = [
        {"find": "pie torch", "replace": "PyTorch"},
        {"find": "pie  torch", "replace": "PyTorch"},   # stessa chiave normalizzata
        {"find": "", "replace": "senza find"},
        {"find": "lecture", "replace": "lesson"},
    ]

    out = dedupe_proposals(items)

    assert [it["find"] for it in out] == ["pie torch", "lecture"]
    assert dedupe_proposals([]) == []


def test_diff_preview_marks_removed_and_added_lines():
    diff = diff_preview("a\nb\nc", "a\nB\nc")

    assert diff == [{"kind": "-", "text": "b"}, {"kind": "+", "text": "B"}]
    assert diff_preview("uguale", "uguale") == []


def test_apply_plan_on_empty_plan():
    plan = Plan(segments=[seg("nulla da fare")], items=[])

    result = apply_plan(plan)

    assert result["applied"] == 0
    assert result["skipped"] == 0
    assert result["text"] == "nulla da fare"
    assert result["details"] == []


def test_manual_plan_item_flags_are_honoured():
    """Un piano costruito a mano (senza `build_plan`) resta applicabile."""
    segments = [seg("we use pie torch")]
    item = PlanItem(find="pie torch", replace="PyTorch", proposal_id=1,
                    norm_find="pie torch", occurrences=[(0, 7, 16)])
    plan = Plan(segments=segments, items=[item])

    result = apply_plan(plan)

    assert result["applied"] == 1
    assert result["text"] == "we use PyTorch"
