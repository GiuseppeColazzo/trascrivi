"""
gen_testdata.py
===============
Genera audio sintetico "speech-like" per benchmark e test del trascrittore.

Non serve a valutare l'accuratezza del testo (non e' parlato reale), ma ha la
stessa struttura spettrale/temporale del parlato: formanti, sillabe, pause.
Questo basta per misurare il THROUGHPUT reale della pipeline (VAD + batching +
decoding) in modo riproducibile e offline.

Uso:
    python gen_testdata.py                    # 4 file da ~2 min in testdata/
    python gen_testdata.py --files 8 --minutes 5 --out testdata
"""

from __future__ import annotations

import argparse
import math
import wave
from pathlib import Path

import numpy as np

SR = 16000


def _formant_burst(rng: np.random.Generator, duration: float, f0: float) -> np.ndarray:
    """Un burst 'sillabico': armoniche con inviluppo formantico e attacco/decadimento."""
    n = int(SR * duration)
    if n < 8:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n) / SR

    # Frequenza fondamentale con leggera modulazione (intonazione naturale).
    f0_curve = f0 * (1.0 + 0.06 * np.sin(2 * np.pi * 1.7 * t + rng.random() * 6))
    phase = 2 * np.pi * np.cumsum(f0_curve) / SR

    sig = np.zeros(n, dtype=np.float64)
    # Formanti tipiche delle vocali: F1 ~500 Hz, F2 ~1500 Hz, F3 ~2500 Hz.
    for k, (fc, bw, amp) in enumerate(
        [(500, 120, 1.0), (1500, 180, 0.6), (2500, 250, 0.3)]
    ):
        # Somma delle armoniche che cadono vicino alla formante.
        for h in range(1, 25):
            fh = f0 * h
            gain = amp * math.exp(-((fh - fc) ** 2) / (2 * bw ** 2))
            if gain < 1e-3:
                continue
            sig += gain * np.sin(h * phase + rng.random() * 6)

    # Inviluppo: attacco rapido, corpo, rilascio (come una sillaba).
    env = np.ones(n)
    a = max(1, int(0.02 * SR))
    r = max(1, int(0.05 * SR))
    env[:a] = np.linspace(0, 1, a)
    env[-r:] = np.linspace(1, 0, r)
    return (sig * env).astype(np.float32)


def synth_speech(duration_s: float, seed: int = 0) -> np.ndarray:
    """Audio sintetico con sillabe, pause e 'frasi' di lunghezza variabile."""
    rng = np.random.default_rng(seed)
    out: list[np.ndarray] = []
    total = 0.0

    while total < duration_s:
        # Una "frase" = 4-12 sillabe, poi una pausa.
        n_syl = int(rng.integers(4, 13))
        for _ in range(n_syl):
            dur = float(rng.uniform(0.09, 0.22))
            f0 = float(rng.uniform(95, 210))
            out.append(_formant_burst(rng, dur, f0))
            out.append(np.zeros(int(SR * rng.uniform(0.01, 0.05)), dtype=np.float32))
            total += dur
        # Pausa tra le frasi (a volte abbastanza lunga da attivare il VAD).
        pause = float(rng.uniform(0.25, 0.9)) if rng.random() < 0.85 else float(
            rng.uniform(1.0, 1.6)
        )
        out.append(np.zeros(int(SR * pause), dtype=np.float32))
        total += pause

    audio = np.concatenate(out) if out else np.zeros(1, dtype=np.float32)
    audio = audio[: int(SR * duration_s)]
    peak = float(np.max(np.abs(audio))) or 1.0
    return (audio / peak * 0.85).astype(np.float32)


def write_wav(path: Path, audio: np.ndarray, sr: int = SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(audio * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def main() -> None:
    ap = argparse.ArgumentParser(description="Genera audio sintetico per benchmark.")
    ap.add_argument("--files", type=int, default=4, help="numero di file (default 4)")
    ap.add_argument("--minutes", type=float, default=2.0, help="durata in minuti per file")
    ap.add_argument("--out", default="testdata", help="cartella di output")
    ap.add_argument("--sr", type=int, default=SR)
    args = ap.parse_args()

    outdir = Path(args.out)
    dur = args.minutes * 60.0
    for i in range(1, args.files + 1):
        audio = synth_speech(dur, seed=1000 + i)
        path = outdir / f"lezione_{i:02d}.wav"
        write_wav(path, audio, args.sr)
        mb = path.stat().st_size / (1024 * 1024)
        print(f"scritto {path}  ({dur:.0f}s, {mb:.1f} MB)")


if __name__ == "__main__":
    main()
