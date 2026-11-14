"""Verifica rapida: quali modelli/lingue accetta il motore, e quanto costa in
tempo un modello multilingue già in cache sul file .ogg dell'utente.
Stampa solo lingua rilevata e velocità: nessun contenuto della registrazione."""
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from trascrivi.models import resolve_options  # noqa: E402

print("── regole lingua/modello ──")
for model, lang in [("large-v3", "it"), ("large-v3", "auto"), ("medium", "it"),
                    ("medium", "auto"), ("distil-large-v3", "it"),
                    ("distil-large-v3", "auto"), ("small.en", "it")]:
    try:
        normalized, notes = resolve_options(model, lang, "auto", "auto")
        print(f"  {model:16s} lang={lang:5s} -> lingua={normalized or 'auto-detect'} {notes}")
    except ValueError as exc:
        print(f"  {model:16s} lang={lang:5s} -> RIFIUTATO: {exc}")

if "--speed" not in sys.argv:
    raise SystemExit(0)

import numpy as np  # noqa: E402
from faster_whisper import BatchedInferencePipeline, WhisperModel  # noqa: E402
from faster_whisper.audio import decode_audio  # noqa: E402

from trascrivi_audio import resolve_runtime  # noqa: E402

ogg = next(Path("data/sources/5").glob("*audio_2026-09-28*.ogg"))
CLIP = 120.0
model_name = sys.argv[sys.argv.index("--speed") + 1] if len(sys.argv) > sys.argv.index("--speed") + 1 else "medium"
cfg = resolve_runtime("auto", "auto", None, model_name)
print(f"\n── prova su {ogg.name}, primi {CLIP:.0f}s, modello '{model_name}' ──")
print(f"  runtime scelto dall'app: device={cfg.device} compute={cfg.compute_type} batch={cfg.batch_size}")
t0 = time.time()
audio = decode_audio(str(ogg), sampling_rate=16000)[: int(CLIP * 16000)]
print(f"  decodifica: {time.time() - t0:.1f}s")

t0 = time.time()
model = WhisperModel(model_name, device=cfg.device, compute_type=cfg.compute_type)
load = time.time() - t0
pipeline = BatchedInferencePipeline(model=model)
t0 = time.time()
segments, info = pipeline.transcribe(audio, language=None, beam_size=5,
                                     batch_size=cfg.batch_size, vad_filter=True)
text = " ".join(s.text.strip() for s in segments)
elapsed = time.time() - t0
print(f"  caricamento modello: {load:.1f}s")
print(f"  lingua rilevata: {info.language} (probabilità {info.language_probability:.2f})")
print(f"  {CLIP / elapsed:.1f}x realtime ({elapsed:.1f}s per {CLIP:.0f}s di audio, batched)")
print(f"  1h30m20s -> circa {(5420 / (CLIP / elapsed)) / 60:.1f} minuti")
print(f"  caratteri trascritti: {len(text)} (nessun testo mostrato qui)")

