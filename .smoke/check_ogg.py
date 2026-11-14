"""Verifica empirica: il progetto accetta e DECODIFICA i file .ogg?

1. crea un .ogg (vorbis) e un .opus reali da un mp4 di lezione, con PyAV;
2. controlla che l'estensione sia in SUPPORTED_FORMATS (CLI + API web);
3. controlla che probe_media() (usato da CLI e backend) li veda;
4. controlla che faster_whisper.audio.decode_audio() li decodifichi davvero.
"""
import sys
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trascrivi_audio import SUPPORTED_FORMATS, probe_media  # noqa: E402
from faster_whisper.audio import decode_audio  # noqa: E402

SRC = ROOT / "lezioni" / "webcams.mp4"
TMP = ROOT / ".smoke" / "tmp"
TMP.mkdir(parents=True, exist_ok=True)
SECONDS = 20


def extract(src: Path, out: Path, codec: str) -> None:
    in_c = av.open(str(src))
    in_s = next(s for s in in_c.streams if s.type == "audio")
    out_c = av.open(str(out), "w", format="ogg")
    out_s = out_c.add_stream(codec, rate=16000)
    out_s.layout = "mono"
    # L'encoder vorbis nativo di FFmpeg e' marcato "experimental".
    out_s.codec_context.options = {"strict": "experimental"}
    written = 0
    for frame in in_c.decode(in_s):
        frame.pts = None
        for packet in out_s.encode(frame):
            out_c.mux(packet)
        written += frame.samples / float(frame.sample_rate or 16000)
        if written >= SECONDS:
            break
    for packet in out_s.encode(None):
        out_c.mux(packet)
    out_c.close()
    in_c.close()


# L'encoder Vorbis nativo di FFmpeg e' "experimental" e il sandbox blocca
# avcodec_open2: mi limito a verificarne il DECODER (che e' cio' che serve).
print("decoder vorbis disponibile:", av.codec.Codec("vorbis", "r").name)
for codec, ext in (("libopus", ".ogg"), ("libopus", ".opus")):
    target = TMP / f"prova_{codec}{ext}"
    extract(SRC, target, codec)
    size = target.stat().st_size
    has_audio, duration = probe_media(target)
    audio = decode_audio(str(target), sampling_rate=16000)
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    print(
        f"{target.name:22s} ext_ok={target.suffix.lower() in SUPPORTED_FORMATS} "
        f"size={size:>8d}B probe=(has_audio={has_audio}, dur={duration}) "
        f"decoded_samples={audio.size} peak={peak:.4f}"
    )
