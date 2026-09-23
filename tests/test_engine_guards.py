"""
Regressioni sul motore di trascrizione.

Tre bug trovati collaudando la piattaforma con audio reale e faster-whisper
1.2.1: qui restano bloccati con un modello finto, così i test girano in
millisecondi e senza GPU.

1. `WhisperModel.transcribe(batch_size=…)` non esiste → TypeError.
2. `BatchedInferencePipeline` + `clip_timestamps` produce UN solo segmento e
   scarta il resto dell'audio (verificato su una lezione di 73 minuti).
3. Con `return_segments=True` il flush incrementale non scriveva il file
   parziale, quindi un job annullato non era riprendibile.
"""

from __future__ import annotations

import logging

import pytest

import trascrivi_audio as engine


class FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class FakeInfo:
    language = "en"
    duration = 120.0


class FakeModel:
    """Registra la chiamata e restituisce segmenti finti."""

    def __init__(self, segments=None):
        self.calls: list[dict] = []
        self.segments = segments or [FakeSegment(0.0, 4.0, "hello"), FakeSegment(4.0, 8.0, "world")]

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter(self.segments), FakeInfo()


class FakePipeline:
    batches: list[dict] = []

    def __init__(self, model=None):
        self.model = model

    def transcribe(self, audio, **kwargs):
        FakePipeline.batches.append(kwargs)
        return iter([FakeSegment(0.0, 4.0, "batched")]), FakeInfo()


@pytest.fixture()
def fake_pipeline(monkeypatch):
    FakePipeline.batches = []
    monkeypatch.setattr(engine, "BatchedInferencePipeline", FakePipeline)
    return FakePipeline


def _log() -> logging.Logger:
    return logging.getLogger("test-engine")


def test_batch_size_is_not_forwarded_to_plain_transcribe(tmp_path, monkeypatch):
    """`batch_size` è solo di BatchedInferencePipeline: non deve finire qui."""
    logger = _log()
    monkeypatch.setattr(logger, "warning", lambda *a, **k: None)
    model = FakeModel()

    engine.trascrivi_file(tmp_path / "x.wav", model, "en", logger, batch_size=1)

    assert len(model.calls) == 1
    assert "batch_size" not in model.calls[0]


def test_no_vad_falls_back_to_single_decoding_and_does_not_crash(tmp_path, fake_pipeline):
    """Senza VAD il batching non è utilizzabile: si resta su batch=1."""
    model = FakeModel()

    text, _dur, _lang, _secs, segments = engine.trascrivi_file(
        tmp_path / "x.wav", model, "en", _log(), batch_size=16, vad_filter=False,
        return_segments=True,
    )

    assert fake_pipeline.batches == []          # nessun tentativo di batching
    assert model.calls[0]["vad_filter"] is False
    assert len(segments) == 2 and "hello" in text


def test_clip_range_disables_batching(tmp_path, fake_pipeline):
    """
    Con un intervallo di clip il batching restituirebbe un solo segmento: il
    motore deve passare al percorso non batched, con clip_timestamps [start, end].
    """
    model = FakeModel()

    engine.trascrivi_file(tmp_path / "x.wav", model, "en", _log(), batch_size=16,
                          clip_seconds=30.0)

    assert fake_pipeline.batches == []
    assert model.calls[0]["clip_timestamps"] == [0.0, 30.0]


def test_batching_is_used_when_no_clip_is_requested(tmp_path, fake_pipeline):
    model = FakeModel()

    engine.trascrivi_file(tmp_path / "x.wav", model, "en", _log(), batch_size=16)

    assert len(fake_pipeline.batches) == 1
    assert fake_pipeline.batches[0]["batch_size"] == 16
    assert "clip_timestamps" not in fake_pipeline.batches[0]
    assert model.calls == []


def test_resume_writes_clip_timestamps_with_real_duration(tmp_path):
    model = FakeModel()

    engine.trascrivi_file(tmp_path / "x.wav", model, "en", _log(), batch_size=16,
                          resume_from=60.0, audio_duration=120.0)

    assert model.calls[0]["clip_timestamps"] == [60.0, 120.0]


def test_partial_file_is_flushed_even_with_return_segments(tmp_path):
    """
    Il file parziale è la memoria della ripresa: deve essere scritto anche
    quando i segmenti vengono restituiti in memoria alla piattaforma.
    """
    model = FakeModel([
        FakeSegment(0.0, 8.0, "primo"),
        FakeSegment(8.0, 16.0, "secondo"),
        FakeSegment(16.0, 24.0, "terzo"),
    ])
    out = tmp_path / "partial.txt"

    engine.trascrivi_file(tmp_path / "x.wav", model, "en", _log(), batch_size=1,
                          write_segments=True, output_path=out, flush_every=10.0,
                          return_segments=True)

    written = out.read_text(encoding="utf-8")
    assert "[00:00] primo" in written
    assert "[00:16] terzo" in written

    # Il file scritto è rileggibile: è ciò che permette il resume.
    assert engine.last_transcribed_seconds(out) == 16.0


def test_cancel_keeps_partial_file_and_resume_offset(tmp_path):
    """`should_stop` interrompe il loop e lascia un parziale con timestamp."""
    model = FakeModel([FakeSegment(0.0, 8.0, "a"), FakeSegment(8.0, 20.0, "b")])
    out = tmp_path / "partial.txt"
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # fermati dopo il secondo segmento

    text, _dur, _lang, _secs, segments = engine.trascrivi_file(
        tmp_path / "x.wav", model, "en", _log(), batch_size=1, write_segments=True,
        output_path=out, return_segments=True, should_stop=should_stop, flush_every=5.0,
    )

    assert len(segments) == 2
    assert engine.last_transcribed_seconds(out) == 8.0
    # Il punto di ripresa è l'ultimo timestamp utile meno il margine di 2 s.
    assert engine.resume_offset(out, True) == pytest.approx(6.0, abs=0.1)


def test_resume_falls_back_to_zero_without_timestamps(tmp_path):
    """Senza timestamp nel parziale non si riprende: meglio rifare che bucare."""
    out = tmp_path / "no_ts.txt"
    out.write_text("riga senza timestamp\n", encoding="utf-8")

    assert engine.last_transcribed_seconds(out) == 0.0
    assert engine.resume_offset(out, True) == 0.0
