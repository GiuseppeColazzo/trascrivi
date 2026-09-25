#!/usr/bin/env python
"""
trascrivi_audio.py
==================
Trascrizione automatica di file audio tramite faster-whisper, ottimizzata per GPU.

Ottimizzazioni rispetto alla versione sequenziale
-------------------------------------------------
1. INFERENZA BATCHED (il guadagno principale):
   usa BatchedInferencePipeline, che raggruppa i chunk vocali (VAD) in batch e li
   decodifica in parallelo sulla GPU, invece di processare un segmento per volta.
2. GPU con fallback automatico: rileva CUDA/VRAM senza importare torch, sceglie
   compute_type e batch_size in base alla memoria disponibile.
3. PARALLELISMO SU PIU' FILE: `--workers N` avvia piu' processi (utile su CPU);
   su GPU il default e' 1 perche' la GPU e' gia' satura col batching.
4. THREAD CPU espliciti (`cpu_threads`) invece del default di ctranslate2.
5. DECODING mirato: niente `condition_on_previous_text`, `beam_size` adattivo,
   `--skip-existing` per riprendere lotti interrotti senza rifare il lavoro.
6. Progresso aggregato e riepilogo tempi per file.

Questo file resta il MOTORE di trascrizione ed e' usato sia dalla CLI sia dalla
piattaforma web (modulo `trascrivi`). Le funzioni pubbliche sono retrocompatibili:
la piattaforma passa solo i parametri extra keyword-only
(`progress_cb`, `should_stop`, `return_segments`, `write_segments`, `initial_prompt`).

Installazione:
    python -m venv --system-site-packages .venv
    .venv\\Scripts\\pip install faster-whisper

Uso:
    python trascrivi_audio.py lezione.mp3
    python trascrivi_audio.py lezioni\\*.mp3 --model distil-large-v3
    python trascrivi_audio.py lezioni\\*.mp3 --output-dir trascrizioni --skip-existing
    python trascrivi_audio.py audio.wav --language auto     # rileva la lingua
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# ── Compatibilità Windows / Hugging Face ────────────────────────────────────
# DEVE stare PRIMA di importare faster_whisper (che importa huggingface_hub).
#
# huggingface_hub salva i modelli nella cache creando dei SYMLINK dai file in
# "snapshots/" ai blob reali. Su Windows i symlink richiedono Developer Mode o
# privilegi di amministratore: senza, il download fallisce con
#     OSError: [WinError 1314] Il privilegio richiesto non appartiene al client
# Nelle versioni recenti l'avviso "Caching files will still work but in a
# degraded version" è fuorviante: il fallback NON scatta e il download muore.
# Queste variabili forzano la modalità copia, che funziona sempre.
#
# Il download di default passa dal backend "xet": può fallire sullo stesso
# problema. Per disattivarlo basta avviare con HF_HUB_DISABLE_XET=1.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _register_cuda_dlls() -> bool:
    """
    Rende trovabili a ctranslate2 le DLL CUDA (cuBLAS / cuDNN) fornite da torch.

    ctranslate2 usa il backend CUDA ma NON porta con se' cuBLAS/cuDNN: se non le
    trova solleva
        RuntimeError: Library cublas64_12.dll is not found or cannot be loaded
    Su Windows queste DLL non sono di sistema, stanno dentro il pacchetto torch
    (site-packages/torch/lib, oppure site-packages/nvidia/*/bin). Se l'ambiente
    non ha gia' quei percorsi nel PATH (tipico lanciando lo script da un
    PowerShell "pulito", senza conda attivo) la GPU e' inutilizzabile.
    Qui li aggiungiamo noi, senza toccare il sistema.

    Ritorna True se almeno una cartella CUDA e' stata trovata.
    """
    candidates: list[str] = []
    try:
        import torch
        candidates.append(str(Path(torch.__file__).parent / "lib"))
    except Exception:
        pass

    # Variante usata quando le librerie CUDA arrivano dai wheel "nvidia-*".
    for sp in sys.path:
        nvidia_dir = Path(sp) / "nvidia"
        if nvidia_dir.is_dir():
            candidates.extend(str(p) for p in nvidia_dir.glob("*/bin"))

    found = False
    for c in candidates:
        if os.path.isdir(c):
            found = True
            try:
                if hasattr(os, "add_dll_directory"):
                    # Necessario da Python 3.8: PATH da solo non basta per
                    # le dipendenze caricate dal DLL gia' aperto.
                    os.add_dll_directory(c)
            except OSError:
                pass
            if c not in os.environ.get("PATH", ""):
                os.environ["PATH"] = c + os.pathsep + os.environ.get("PATH", "")
    return found


_CUDA_DLLS_REGISTERED = _register_cuda_dlls()


def _make_console_utf8_safe() -> None:
    """
    Evita il crash di rich sulla console Windows in code page 1252.

    Il riepilogo finale contiene emoji (✅, 🌍…): su un `cmd` legacy la codifica
    fallisce con
        UnicodeEncodeError: 'charmap' codec can't encode character '\\u2705'
    e la trascrizione va a buon fine ma il processo termina con errore. Forziamo
    UTF-8 con `backslashreplace`: il testo resta leggibile e nessuna stampa può
    più far cadere il processo a lavoro completato.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


_make_console_utf8_safe()

# ── Dipendenze esterne ──────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table
    from rich.text import Text
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

try:
    from faster_whisper import BatchedInferencePipeline, WhisperModel
except ImportError:
    print(
        "ERRORE: faster-whisper non trovato.\n"
        "Installa le dipendenze con:\n"
        "    python -m venv --system-site-packages .venv\n"
        "    .venv\\Scripts\\pip install faster-whisper",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Formati audio supportati ─────────────────────────────────────────────────
SUPPORTED_FORMATS = {
    ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma",
    ".mp4", ".mkv", ".mov", ".webm",
}

# ── Modelli ──────────────────────────────────────────────────────────────────
# I modelli "distil-*" sono solo per l'inglese, ma sono ~5-6x piu' veloci di
# large-v3 con accuratezza quasi identica: ideali per lezioni in inglese.
MODELS = [
    "tiny", "tiny.en",
    "base", "base.en",
    "small", "small.en",
    "medium", "medium.en",
    "large-v1", "large-v2", "large-v3",
    "distil-small.en", "distil-medium.en", "distil-large-v2", "distil-large-v3",
]

# Modelli che funzionano SOLO in inglese.
ENGLISH_ONLY_PREFIXES = ("distil-",)
ENGLISH_ONLY_SUFFIX = ".en"

# Default pensati per GPU + lezioni in inglese (nessuna perdita di accuratezza
# rispetto a large-v3 grazie alla distillazione).
DEFAULT_MODEL = "distil-large-v3"
DEFAULT_LANGUAGE = "en"

# Soglia VRAM sotto la quale non si usa il modello large.
LARGE_MODEL_MIN_VRAM_GB = 4.0

# Compute type di ripiego quando `auto` non basta: e' la stessa scelta che fa
# `resolve_runtime` per quel device.
DEFAULT_COMPUTE_TYPE = {"cuda": "float16", "cpu": "int8"}

# Marcatore dei WAV temporanei prodotti dalle versioni precedenti dello script.
# Viene usato solo per ignorare se ne trovi ancora in giro.
TEMP_MARKER = ".trascrivi_tmp.wav"


def is_english_only(model_name: str) -> bool:
    """True se il modello funziona solo in inglese (distil-* o *.en)."""
    return model_name.startswith(ENGLISH_ONLY_PREFIXES) or model_name.endswith(
        ENGLISH_ONLY_SUFFIX
    )


# ── Logging ──────────────────────────────────────────────────────────────────
def setup_logging(verbose: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    if RICH_AVAILABLE:
        logging.basicConfig(
            level=level,
            format="%(message)s",
            handlers=[RichHandler(rich_tracebacks=True, show_time=True, show_path=False)],
        )
    else:
        logging.basicConfig(
            level=level,
            format="[%(asctime)s] %(levelname)s - %(message)s",
            datefmt="%H:%M:%S",
        )
    # ctranslate2 e huggingface_hub sono molto verbosi su DEBUG.
    if verbose:
        logging.getLogger("faster_whisper").setLevel(logging.INFO)
    return logging.getLogger("trascrittore")


def _md(msg: str) -> str:
    """Marcatura rich solo se rich e' disponibile (evita tag visibili in plain)."""
    return msg if RICH_AVAILABLE else _strip_markup(msg)


def _strip_markup(msg: str) -> str:
    return re.sub(r"\[/?[a-zA-Z0-9_# =. ]+\]", "", msg)


# ── Rilevamento hardware (senza importare torch) ──────────────────────────────
def cuda_device_count() -> int:
    """Numero di GPU usabili. Prova ctranslate2, poi torch, poi nvidia-smi."""
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        if n > 0:
            return n
    except Exception:
        pass
    try:
        import torch  # import pesante: solo come fallback
        if torch.cuda.is_available():
            return torch.cuda.device_count()
    except Exception:
        pass
    return 0


def cuda_free_vram_gb(device_index: int = 0) -> float | None:
    """VRAM libera in GB, o None se non determinabile."""
    try:
        import torch
        if torch.cuda.is_available():
            free, _total = torch.cuda.mem_get_info(device_index)
            return free / (1024 ** 3)
    except Exception:
        pass
    return None


def cuda_gpu_name(device_index: int = 0) -> str | None:
    """Nome della GPU, o None se non disponibile."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(device_index)
    except Exception:
        pass
    return None


def supported_compute_types(device: str) -> set[str] | None:
    """
    Tipo di calcolo accettati dal backend su quel device, o None se non si sa.

    Non e' un test hardware: ctranslate2 risponde per il backend scelto (su CPU
    ad esempio `float16` non c'e'). La usiamo per non passare a `WhisperModel`
    una precisione che lo farebbe sollevare ValueError.
    """
    try:
        import ctranslate2
        return set(ctranslate2.get_supported_compute_types(device))
    except Exception:
        return None


@dataclass
class RuntimeConfig:
    """Configurazione risolta di device / compute_type / batch."""
    device: str
    compute_type: str
    batch_size: int
    cpu_threads: int
    notes: list[str] = field(default_factory=list)


def resolve_runtime(
    requested_device: str,
    requested_compute: str,
    requested_batch: int | None,
    model_name: str,
) -> RuntimeConfig:
    """Sceglie device, precisione e batch size in base all'hardware reale."""
    notes: list[str] = []
    is_large = "large" in model_name or "medium" in model_name

    n_gpu = cuda_device_count()
    has_cuda = n_gpu > 0
    cpu_count = os.cpu_count() or 4

    # ── Device ───────────────────────────────────────────────────────────────
    if requested_device == "auto":
        device = "cuda" if has_cuda else "cpu"
    else:
        device = requested_device
        if device == "cuda" and not has_cuda:
            notes.append("CUDA richiesto ma non disponibile: uso la CPU.")
            device = "cpu"

    # ── Compute type ─────────────────────────────────────────────────────────
    # Un tipo scelto per la GPU (`float16`, `int8_float16`) su CPU non e' "solo
    # lento": ctranslate2 solleva ValueError e il job muore. Succede appena il
    # device viene declassato a CPU (Mac, macchina senza CUDA) o quando l'utente
    # sceglie la precisione dal form senza cambiare device. Si ricade sul default
    # del device invece di far fallire tutto per un parametro opzionale.
    supported = supported_compute_types(device)
    if requested_compute == "auto":
        compute_type = DEFAULT_COMPUTE_TYPE.get(device, "float32")
    else:
        compute_type = requested_compute

    if supported and compute_type not in supported:
        fallback = DEFAULT_COMPUTE_TYPE.get(device, "float32")
        if fallback not in supported:
            fallback = "float32" if "float32" in supported else sorted(supported)[0]
        notes.append(
            f"compute type '{compute_type}' non supportato su {device}: uso '{fallback}'."
        )
        compute_type = fallback

    # ── Batch size ───────────────────────────────────────────────────────────
    if requested_batch is not None:
        batch_size = requested_batch
    elif device == "cuda":
        vram = cuda_free_vram_gb()
        if vram is None:
            batch_size = 8
            notes.append("VRAM non rilevabile: batch_size prudente (8).")
        elif vram < 2.0:
            batch_size = 2 if is_large else 4
            notes.append(f"VRAM libera bassa ({vram:.1f} GB): batch ridotto.")
        elif vram < LARGE_MODEL_MIN_VRAM_GB and is_large:
            batch_size = 4
            notes.append(f"VRAM {vram:.1f} GB con modello grande: batch 4.")
        elif vram < 6.0:
            batch_size = 8
        else:
            batch_size = 16
    else:
        # Su CPU batch piu' alti non aiutano quasi mai: la memoria e' il limite.
        batch_size = 1

    # ── Thread CPU ───────────────────────────────────────────────────────────
    # ctranslate2 di default usa 4 thread: su CPU moderne e' molto sottoutilizzato.
    cpu_threads = max(1, min(cpu_count, 16)) if device == "cpu" else 4

    return RuntimeConfig(device, compute_type, batch_size, cpu_threads, notes)


# ── Trascrizione (worker) ────────────────────────────────────────────────────
def _fmt_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


_TS_RE = re.compile(r"^\[(\d+):(\d{2})(?::(\d{2}))?\]")

# Valori sentinella per il canale di stop condiviso (usato dalla piattaforma web).
STOP_ERROR = "error"
STOP_STOP = "stop"


def read_stop_flag(shared: object | None) -> str | None:
    """
    Legge il motivo di stop da un oggetto condiviso, in modo tollerante.

    La piattaforma web passa una `multiprocessing.Event` (che non sa comunicare
    il motivo); qui accettiamo anche oggetti con `read_flag()`/`value`. Tutto il
    resto viene interpretato come "nessuno stop".
    """
    if shared is None:
        return None
    value = getattr(shared, "value", None)
    for candidate in (shared, value):
        read_flag = getattr(candidate, "read_flag", None)
        if callable(read_flag):
            try:
                flag = read_flag()
            except Exception:
                continue
            return flag if flag in (STOP_STOP, STOP_ERROR) else None
    return None


def last_transcribed_seconds(output_path: Path) -> float:
    """
    Ultimo istante trascritto in un file di output parziale, o 0.0.

    Legge il timestamp [mm:ss] (o [hh:mm:ss]) dell'ultima riga utile. Serve per
    riprendere una trascrizione interrotta a metà senza rifare tutto.
    """
    if not output_path.exists():
        return 0.0
    try:
        last = 0.0
        # Legge il file al termine: l'ultima riga utile è quella che ci interessa.
        with output_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _TS_RE.match(line)
                if not m:
                    continue
                a, b, c = m.groups()
                last = (
                    int(a) * 3600 + int(b) * 60 + int(c)
                    if c is not None
                    else int(a) * 60 + int(b)
                )
        return last
    except OSError:
        return 0.0


def apply_offset(segments, offset: float):
    """Scarta i segmenti già presenti nel file di output."""
    for seg in segments:
        if seg.end <= offset:
            continue
        yield seg


def trascrivi_file(
    audio_path: Path,
    model: WhisperModel,
    language: str | None,
    logger: logging.Logger,
    *,
    batch_size: int = 16,
    beam_size: int = 5,
    vad_filter: bool = True,
    word_timestamps: bool = False,
    write_timestamps: bool = False,
    output_path: Path | None = None,
    clip_seconds: float | None = None,
    resume_from: float = 0.0,
    flush_every: float = 30.0,
    initial_prompt: str | None = None,
    audio_duration: float | None = None,
    progress_cb: Callable[[float, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    return_segments: bool = False,
    write_segments: bool = False,
) -> tuple:
    """
    Trascrive un file. Restituisce (testo, durata_audio, lingua, secondi_trascorsi).

    Con return_segments=True restituisce in coda la lista dei segmenti
    [{"start": float, "end": float, "text": str}], utile alla piattaforma web.

    clip_seconds limita l'elaborazione ai primi N secondi (utile per prove rapide).
    resume_from riparte da un istante gia' trascritto (vedi --resume).
    flush_every scrive su disco ogni N secondi di audio, cosi' un'interruzione
    non fa perdere il lavoro fatto.
    progress_cb(end_seconds, count) viene chiamata dopo ogni segmento.
    should_stop() viene interrogata a ogni segmento: se restituisce True il loop
    si interrompe in modo pulito e il file parziale resta valido.
    audio_duration è la durata nota del file (da probe_media): serve alla
    pipeline batched, che con `clip_timestamps` non accetta una fine "infinita".
    write_segments accetta anche le righe `[mm:ss] testo` in un file di input
    gia' trascritto (import di trascrizioni esistenti).
    Il testo include timestamp [mm:ss] se write_timestamps e' attivo.
    """
    logger.info(_md(f"📂  Apertura file: [bold]{audio_path.name}[/bold]"))
    t_start = time.time()

    # ── Avvio: con batch_size > 1 usiamo la pipeline batched ────────────────
    # ATTENZIONE: BatchedInferencePipeline usa il VAD per formare i batch.
    # Con vad_filter=False e senza clip_timestamps fallisce con
    #   RuntimeError: No clip timestamps found.
    # Senza VAD quindi si resta sul percorso non batched (batch_size=1): è la
    # stessa scelta che farebbe ctranslate2, ma esplicita e senza crash.
    if not vad_filter and batch_size and batch_size > 1:
        logger.warning(_md("⚠️   VAD disattivato: batching non disponibile, uso batch=1."))
        batch_size = 1
    use_batched = batch_size and batch_size > 1
    common = dict(
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        vad_parameters=dict(min_silence_duration_ms=500),
        word_timestamps=word_timestamps,
    )
    if initial_prompt:
        common["initial_prompt"] = initial_prompt

    # Con un intervallo di clip (ripresa o benchmark) NON si usa il batching:
    # BatchedInferencePipeline, quando riceve clip_timestamps, costruisce un solo
    # chunk e scarta il resto dell'audio (verificato: 30 minuti di lezione
    # producevano un unico segmento "Let's start..."). Il percorso non batched
    # (WhisperModel.transcribe) gestisce clip_timestamps in modo corretto.
    clip_start = resume_from if resume_from > 0 else 0.0
    clip_end = float(clip_seconds) if clip_seconds else None
    clip_args: dict = {}
    if clip_end is not None or clip_start > 0:
        if clip_end is not None and clip_end > clip_start:
            end_value = clip_end
        elif audio_duration and audio_duration > clip_start:
            # Fine "aperta" (ripresa fino in fondo): chiudiamo alla durata reale.
            end_value = float(audio_duration)
        else:
            end_value = clip_start + 3600.0
        clip_args = {"clip_timestamps": [clip_start, end_value]}
        if use_batched:
            logger.warning(
                _md("⚠️   Intervallo di clip richiesto: batching disattivato (batch=1).")
            )
            use_batched = False

    if use_batched:
        pipeline = BatchedInferencePipeline(model=model)
        segments, info = pipeline.transcribe(
            str(audio_path), batch_size=batch_size, **common, **clip_args
        )
    else:
        # `batch_size` è un parametro di BatchedInferencePipeline, non di
        # WhisperModel.transcribe: passarlo qui è un TypeError.
        segments, info = model.transcribe(str(audio_path), **common, **clip_args)

    detected_lang = info.language or (language or "?")
    duration_s = info.duration or 0.0
    # Con --benchmark la durata "utile" è quella davvero elaborata.
    effective_duration = min(duration_s, float(clip_seconds)) if clip_seconds else duration_s
    if resume_from > 0:
        # I timestamp restano riferiti all'audio originale, quindi non si somma
        # nulla: con clip_timestamps gli offset sono già quelli assoluti.
        segments = apply_offset(segments, resume_from)
    logger.info(
        _md(
            f"🌍  Lingua: [cyan]{detected_lang.upper()}[/cyan]  |  "
            f"durata [cyan]{_fmt_ts(duration_s)}[/cyan]"
            + (f" (elaboro {effective_duration:.0f}s)" if clip_seconds else "")
            + (f"  |  riprendo da [cyan]{_fmt_ts(resume_from)}[/cyan]" if resume_from > 0 else "")
            + f"  |  batch={batch_size if use_batched else 1}"
        )
    )

    # ── Apertura output in modalità append (per il flush incrementale) ──────
    # Scriviamo DIRETTAMENTE il file finale: nessun file temporaneo in giro,
    # così un'interruzione lascia un .txt parziale ma riutilizzabile.
    fh = None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if resume_from > 0 and output_path.exists():
            # Riprende in coda: aggiunge una riga vuota di separazione.
            with output_path.open("a", encoding="utf-8", newline="\n") as tmp_fh:
                tmp_fh.write("\n")
        fh = output_path.open("a" if resume_from > 0 else "w",
                              encoding="utf-8", newline="\n")

    # ── Consumo del generatore ──────────────────────────────────────────────
    # Nota: faster-whisper restituisce un generatore pigro, quindi il tempo di
    # inferenza viene speso QUI, non nella chiamata transcribe().
    testo_segmenti: list[str] = []
    segmenti: list[dict] = []
    n_segmenti = 0
    last_flush = 0.0
    stopped = False
    transcribe_seconds = 0.0

    def _on_segment(seg) -> str:
        """Formatta un segmento e, se serve, svuota il buffer su disco."""
        nonlocal n_segmenti, last_flush
        linea = seg.text.strip()
        # Con write_segments il file di output è pensato per essere riletto con
        # parse_raw (import): i timestamp servono anche quando l'utente non li
        # vuole nel testo finale.
        testo_segmenti.append(f"[{_fmt_ts(seg.start)}] {linea}"
                              if (write_timestamps or write_segments) else linea)
        if return_segments:
            segmenti.append({"start": float(seg.start), "end": float(seg.end), "text": linea})
        n_segmenti += 1
        # Il flush incrementale resta attivo anche raccogliendo i segmenti in
        # memoria: è ciò che rende riprendibile un job interrotto.
        if fh is not None and seg.end - last_flush >= flush_every:
            fh.write("\n".join(testo_segmenti) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            testo_segmenti.clear()
            last_flush = seg.end
        if progress_cb is not None:
            progress_cb(float(seg.end), n_segmenti)
        return linea

    if RICH_AVAILABLE:
        console = Console(stderr=True)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=32),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(
                f"[green]{audio_path.name}", total=max(effective_duration, 1.0)
            )
            for seg in segments:
                _on_segment(seg)
                progress.update(task, completed=min(seg.end, effective_duration))
                if should_stop is not None and should_stop():
                    stopped = True
                    break
            progress.update(task, completed=max(effective_duration, 1.0))
        transcribe_seconds = time.time() - t_start
    else:
        try:
            from tqdm import tqdm
            pbar = tqdm(total=max(effective_duration, 1.0), unit="s", desc=audio_path.name, ncols=70)
            prev_end = resume_from
            for seg in segments:
                _on_segment(seg)
                pbar.update(max(0.0, seg.end - prev_end))
                prev_end = seg.end
                if should_stop is not None and should_stop():
                    stopped = True
                    break
            pbar.close()
        except ImportError:
            for seg in segments:
                _on_segment(seg)
                if should_stop is not None and should_stop():
                    stopped = True
                    break
        transcribe_seconds = time.time() - t_start

    # ── Flush finale + chiusura ─────────────────────────────────────────────
    if testo_segmenti:
        blocco = "\n".join(testo_segmenti)
        if fh is not None:
            fh.write(blocco + "\n")
        testo = blocco
    else:
        testo = ""
    if fh is not None:
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()

    if stopped:
        logger.warning(_md(f"⏹   {audio_path.name}: interrotto dall'utente, output parziale salvato."))

    rt = effective_duration / transcribe_seconds if transcribe_seconds > 0 else 0.0
    logger.info(
        _md(
            f"✅  [bold green]{audio_path.name}[/bold green] in "
            f"[bold green]{transcribe_seconds:.1f}s[/bold green] "
            f"({n_segmenti} segmenti, [magenta]{rt:.1f}x realtime[/magenta])"
        )
    )
    if return_segments:
        return testo, duration_s, detected_lang, transcribe_seconds, segmenti
    return testo, duration_s, detected_lang, transcribe_seconds


# ── Worker per il multiprocessing ────────────────────────────────────────────
_WORKER_MODEL: WhisperModel | None = None
_WORKER_CFG: dict = {}


def _init_worker(model_name: str, device: str, compute_type: str, cpu_threads: int,
                 language: str | None, batch_size: int, beam_size: int,
                 write_timestamps: bool, clip_seconds: float | None,
                 resume: bool = False) -> None:
    """Carica il modello una volta per processo (evita ricariche per file)."""
    global _WORKER_MODEL, _WORKER_CFG
    _WORKER_MODEL = WhisperModel(
        model_name, device=device, compute_type=compute_type, cpu_threads=cpu_threads
    )
    _WORKER_CFG = dict(
        language=language, batch_size=batch_size, beam_size=beam_size,
        write_timestamps=write_timestamps, clip_seconds=clip_seconds,
        resume=resume,
    )


def _worker_task(job: tuple[str, str]) -> tuple[str, str, float, str, float, str | None]:
    """Trascrive un file in un processo separato. Restituisce (src, out, ...)."""
    src, out = job
    log = logging.getLogger("trascrittore")
    target = Path(out)
    start_at = resume_offset(target, _WORKER_CFG.get("resume", False) and
                             _WORKER_CFG.get("write_timestamps", False))
    try:
        testo, durata, lingua, secondi = trascrivi_file(
            Path(src), _WORKER_MODEL, _WORKER_CFG["language"], log,
            batch_size=_WORKER_CFG["batch_size"],
            beam_size=_WORKER_CFG["beam_size"],
            write_timestamps=_WORKER_CFG["write_timestamps"],
            clip_seconds=_WORKER_CFG["clip_seconds"],
            resume_from=start_at,
            output_path=target,
        )
        return src, out, durata, lingua, secondi, None
    except Exception as exc:  # noqa: BLE001 - il worker non deve mai morire
        return src, out, 0.0, "?", 0.0, f"{type(exc).__name__}: {exc}"


# ── Utility media ────────────────────────────────────────────────────────────
# NOTA IMPORTANTE: non serve estrarre l'audio in un WAV temporaneo.
# faster-whisper decodifica da solo tramite PyAV (che include FFmpeg
# compilato staticamente), quindi accetta direttamente anche .mp4/.mkv/.mov.
#
# La versione precedente creava "<nome>.trascrivi_tmp.wav" ACCANTO all'originale.
# Era un errore per due motivi:
#   1. il file temporaneo finiva nella stessa cartella dei sorgenti, quindi un
#      pattern come "lezioni\*" lo raccoglieva al lancio successivo e il file
#      veniva trascritto DUE volte;
#   2. scrivere su disco ~140 MB per ogni lezione era tempo sprecato: la
#      decodifica diretta dell'mp4 gira a oltre 1000x realtime.
# Passiamo quindi il file originale direttamente al modello.


def probe_media(path: Path) -> tuple[bool, float | None]:
    """(ha_audio, durata_secondi) via PyAV, senza dipendenze di sistema."""
    try:
        import av
        with av.open(str(path)) as container:
            has = any(s.type == "audio" for s in container.streams)
            dur = container.duration / av.time_base if container.duration else None
            return has, dur
    except Exception:
        return False, None


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trascrivi file audio in testo tramite faster-whisper (GPU + batched).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python trascrivi_audio.py lezione.mp3
  python trascrivi_audio.py lezioni\\*.mp3 --output-dir trascrizioni --skip-existing
  python trascrivi_audio.py lezione.mp4 --model distil-large-v3 --language en
  python trascrivi_audio.py audio.wav --language auto          # rileva la lingua
  python trascrivi_audio.py *.mp3 --device cpu --workers 4     # macchina senza GPU

Modelli consigliati per lezioni in inglese (veloci, accuratezza ~large-v3):
  distil-large-v3   (default)  ~5-6x piu' veloce di large-v3
  distil-medium.en             ancora piu' veloce, leggermente meno preciso
        """,
    )
    parser.add_argument("files", nargs="+", help="File audio da trascrivere")
    parser.add_argument(
        "--model", "-m", choices=MODELS, default=DEFAULT_MODEL,
        help=f"Modello Whisper (default: {DEFAULT_MODEL}, ottimo per l'inglese)",
    )
    parser.add_argument(
        "--language", "-l", default=DEFAULT_LANGUAGE,
        help=f"Lingua (default: '{DEFAULT_LANGUAGE}'). Usa 'auto' per il rilevamento automatico",
    )
    parser.add_argument("--output", "-o", default=None,
                        help="Percorso file di output (solo con un singolo file)")
    parser.add_argument("--output-dir", "-d", default=None,
                        help="Cartella di output (default: stessa cartella dell'audio)")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto",
                        help="Dispositivo di inferenza (default: auto)")
    parser.add_argument("--compute-type", choices=["auto", "int8", "int8_float16",
                                                   "float16", "float32", "bfloat16"],
                        default="auto",
                        help="Precisione calcolo (default: auto = float16 su GPU, int8 su CPU). "
                             "Un tipo non supportato dal device scelto viene sostituito")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="Segmenti decodificati in parallelo (default: automatico "
                             "in base alla VRAM). 1 = disattiva il batching")
    parser.add_argument("--beam-size", type=int, default=5,
                        help="Beam search (default: 5). 1 = piu' veloce, meno preciso")
    parser.add_argument("--workers", "-w", type=int, default=None,
                        help="Processi paralleli sui file (default: 1 su GPU, N su CPU)")
    parser.add_argument("--cpu-threads", type=int, default=None,
                        help="Thread per worker (default: automatico)")
    parser.add_argument("--timestamps", action="store_true",
                        help="Anteponi [mm:ss] a ogni segmento nel file di output")
    parser.add_argument("--word-timestamps", action="store_true",
                        help="Timestamp a livello di parola (piu' lento)")
    parser.add_argument("--no-vad", action="store_true",
                        help="Disattiva il filtro VAD (silenzio rimosso)")
    parser.add_argument("--initial-prompt", default=None, metavar="TESTO",
                        help="Vocabolario/contesto iniziale per migliorare il lessico "
                             "(nomi propri, sigle, termini tecnici del corso)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Salta i file il cui .txt esiste gia'")
    parser.add_argument("--resume", action="store_true",
                        help="Riprende una trascrizione interrotta a meta': il .txt viene "
                             "aperto in coda e l'audio riparte dall'ultimo [mm:ss] scritto. "
                             "Richiede --timestamps")
    parser.add_argument("--benchmark", type=float, default=None, metavar="SECONDI",
                        help="Elabora solo i primi N secondi di ogni file (prova rapida)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Output di debug")
    return parser.parse_args(argv)


def expand_inputs(patterns: list[str], logger: logging.Logger) -> list[Path]:
    """Espande i pattern in una lista di file unici, in ordine stabile."""
    files: list[Path] = []
    for pattern in patterns:
        path = Path(pattern)
        if path.exists() and path.is_file():
            files.append(path)
            continue
        if path.exists() and path.is_dir():
            for ext in sorted(SUPPORTED_FORMATS):
                files.extend(sorted(path.glob(f"*{ext}")))
            continue
        found = sorted(Path(".").glob(pattern))
        if found:
            files.extend(found)
        else:
            logger.warning(_md(f"⚠️   Non trovato, ignorato: {pattern}"))

    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        # Rete di sicurezza: i vecchi WAV temporanei (creati dalle versioni
        # precedenti accanto ai sorgenti) non devono mai essere trascritti.
        if f.name.endswith(TEMP_MARKER) or f.name.startswith("~"):
            logger.warning(_md(f"⚠️   File temporaneo ignorato: {f.name}"))
            continue
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def output_for(audio_path: Path, args: argparse.Namespace, multi: bool) -> Path:
    if args.output and not multi:
        return Path(args.output)
    if args.output_dir:
        return Path(args.output_dir) / (audio_path.stem + ".txt")
    return audio_path.with_suffix(".txt")


def resume_offset(target: Path, enabled: bool) -> float:
    """
    Punto di ripresa per un file gia' parzialmente trascritto.

    Richiede --timestamps: senza timestamp nel .txt non c'e' modo di sapere
    dove ci si era fermati, quindi non si riprende (meglio rifare che
    produrre un file con un buco in mezzo).
    """
    if not enabled or not target.exists():
        return 0.0
    offset = last_transcribed_seconds(target)
    if offset <= 0.0:
        return 0.0
    # Un piccolo margine evita di perdere la frase a cavallo dell'interruzione.
    return max(0.0, offset - 2.0)


# ── Esecuzione sequenziale (un solo processo, modello gia' caricato) ─────────
def run_sequential(audio_files, out_map, model, args, logger):
    logger.info(_md("▶   Modalità sequenziale (un processo, pipeline batched)"))
    results = []
    for audio_path in audio_files:
        src = str(audio_path)
        target = out_map[src] if isinstance(out_map, dict) else out_map
        target = Path(target)
        start_at = resume_offset(target, args.resume and args.timestamps)
        try:
            _has_audio, known_duration = probe_media(audio_path)
            testo, durata, lingua, secondi = trascrivi_file(
                audio_path, model, args.language_normalized, logger,
                batch_size=args.batch_size_resolved,
                beam_size=args.beam_size,
                vad_filter=not args.no_vad,
                word_timestamps=args.word_timestamps,
                write_timestamps=args.timestamps,
                clip_seconds=args.benchmark,
                resume_from=start_at,
                output_path=target,
                initial_prompt=args.initial_prompt,
                audio_duration=known_duration,
            )
            results.append((src, str(target), durata, lingua, secondi, None))
        except Exception as exc:  # noqa: BLE001
            logger.error(_md(f"❌  Errore su {audio_path.name}: {exc}"))
            results.append((src, str(target), 0.0, "?", 0.0, f"{type(exc).__name__}: {exc}"))
    return results

# ── main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logger = setup_logging(args.verbose)

    # ── Lingua ───────────────────────────────────────────────────────────────
    lang = (args.language or "en").strip()
    args.language_normalized = None if lang.lower() in {"auto", ""} else lang.lower()

    # ── Vincoli modello/lingua ───────────────────────────────────────────────
    if is_english_only(args.model) and args.language_normalized not in (None, "en"):
        logger.error(
            _md(
                f"❌  Il modello [bold]{args.model}[/bold] funziona solo in inglese, "
                f"ma hai chiesto '{args.language_normalized}'.\n"
                f"    Usa --model large-v3 (multilingua) oppure --language en."
            )
        )
        sys.exit(2)
    if is_english_only(args.model) and args.language_normalized is None:
        # Con auto-detection i modelli .en non sono affidabili: forziamo 'en'.
        logger.warning(
            _md(f"⚠️   {args.model} è solo-inglese: imposto la lingua a 'en'.")
        )
        args.language_normalized = "en"

    # ── File di input ────────────────────────────────────────────────────────
    files = expand_inputs(args.files, logger)
    audio_files = [f for f in files if f.suffix.lower() in SUPPORTED_FORMATS]
    for s in (f for f in files if f.suffix.lower() not in SUPPORTED_FORMATS):
        logger.warning(_md(f"⚠️   Formato non supportato, ignorato: {s.name}"))

    if not audio_files:
        logger.error("❌  Nessun file audio valido trovato. Esco.")
        sys.exit(1)
    if args.output and len(audio_files) > 1:
        logger.error("❌  --output si può usare solo con un singolo file in input.")
        sys.exit(1)

    multi = len(audio_files) > 1

    # ── Configurazione runtime ───────────────────────────────────────────────
    cfg = resolve_runtime(args.device, args.compute_type, args.batch_size, args.model)
    args.batch_size_resolved = cfg.batch_size
    workers = args.workers
    if workers is None:
        # Su GPU il batching satura già la scheda: più processi competono per la
        # stessa VRAM senza guadagno. Su CPU invece il parallelismo scala bene.
        workers = 1 if cfg.device == "cuda" else min(len(audio_files), max(1, (os.cpu_count() or 4) // 4))
        workers = max(1, min(workers, len(audio_files)))
    cpu_threads = args.cpu_threads or cfg.cpu_threads

    for note in cfg.notes:
        logger.warning(_md(f"⚠️   {note}"))

    logger.info(
        _md(
            f"🤖  Modello [bold]{args.model}[/bold] · device [bold]{cfg.device}[/bold] · "
            f"compute [bold]{cfg.compute_type}[/bold] · batch [bold]{cfg.batch_size}[/bold] · "
            f"workers [bold]{workers}[/bold] · lingua "
            f"[bold]{args.language_normalized or 'auto'}[/bold]"
        )
    )

    # ── Preparazione output e skip ───────────────────────────────────────────
    pending: list[Path] = []
    out_map: dict[str, Path] = {}
    for audio_path in audio_files:
        target = output_for(audio_path, args, multi)
        out_map[str(audio_path)] = target
        if args.skip_existing and target.exists():
            logger.info(_md(f"⏭️   Già presente, salto: [dim]{audio_path.name}[/dim]"))
            continue
        pending.append(audio_path)

    if not pending:
        logger.info(_md("✔   Nulla da fare: tutti i file hanno già una trascrizione."))
        return

    # ── Verifica che i file contengano audio ─────────────────────────────────
    # Nessuna estrazione: faster-whisper decodifica direttamente anche i video.
    media_files: list[Path] = []
    for audio_path in pending:
        has_audio, duration = probe_media(audio_path)
        if not has_audio:
            logger.error(_md(f"❌  Nessuna traccia audio in {audio_path.name}: salto."))
            continue
        if duration:
            logger.info(
                _md(f"🎬  [bold]{audio_path.name}[/bold]  durata {_fmt_ts(duration)}")
            )
        media_files.append(audio_path)

    if not media_files:
        logger.error("❌  Nessun file elaborabile. Esco.")
        sys.exit(1)

    if RICH_AVAILABLE:
        Console().print(
            Panel(
                Text(
                    f"File da trascrivere: {len(media_files)}\n"
                    f"{cfg.device.upper()} · {args.model} · batch {cfg.batch_size} · "
                    f"workers {workers}",
                    justify="center",
                ),
                title="[bold green]Trascrittore Audio[/bold green]",
                border_style="green",
            )
        )

    t_batch_start = time.time()
    results: list[tuple[str, str, float, str, float, str | None]] = []

    # ── Caricamento modello ──────────────────────────────────────────────────
    t_load = time.time()
    if workers == 1:
        logger.info(_md("⏳  Caricamento modello…"))
        model = WhisperModel(
            args.model, device=cfg.device, compute_type=cfg.compute_type,
            cpu_threads=cpu_threads,
        )
        logger.info(_md(f"✔   Modello caricato in {time.time() - t_load:.1f}s"))
        out_by_path = {str(p): out_map[str(p)] for p in media_files}
        results = run_sequential(media_files, out_by_path, model, args, logger)
    else:
        logger.info(_md(f"⏳  Avvio di {workers} worker paralleli…"))
        jobs = [(str(p), str(out_map[str(p)])) for p in media_files]
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(
                args.model, cfg.device, cfg.compute_type, cpu_threads,
                args.language_normalized, cfg.batch_size, args.beam_size,
                args.timestamps, args.benchmark, args.resume,
            ),
        ) as executor:
            futures = {executor.submit(_worker_task, job): job for job in jobs}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    job = futures[fut]
                    logger.error(_md(f"❌  Worker fallito su {Path(job[0]).name}: {exc}"))
                    results.append((job[0], job[1], 0.0, "?", 0.0, str(exc)))

    # ── Pulizia temporanei ───────────────────────────────────────────────────
    wall = time.time() - t_batch_start
    errori = [r for r in results if r[5] is not None]
    ok = [r for r in results if r[5] is None]
    # Con --benchmark si elabora solo un estratto: contare la durata intera del
    # file gonfia gli "x realtime" del riepilogo (72x su un file da 73 minuti di
    # cui sono stati elaborati 60 secondi), e il riepilogo e' esattamente cio' che
    # README_CPU.md dice di guardare per scegliere il modello.
    def _processed(seconds: float) -> float:
        return min(seconds, float(args.benchmark)) if args.benchmark else seconds

    audio_tot = sum(_processed(r[2]) for r in ok)
    infer_tot = sum(r[4] for r in ok)

    # ── Riepilogo ────────────────────────────────────────────────────────────
    if RICH_AVAILABLE:
        console = Console()
        table = Table(title="Riepilogo trascrizioni", border_style="green", show_lines=False)
        table.add_column("File", overflow="fold")
        table.add_column("Durata", justify="right")
        table.add_column("Tempo", justify="right")
        table.add_column("x RT", justify="right", style="magenta")
        table.add_column("Lingua", justify="center")
        table.add_column("Esito", justify="center")
        for r in sorted(results, key=lambda x: x[0]):
            rt = (r[2] / r[4]) if r[4] else 0.0
            table.add_row(
                Path(r[0]).name,
                _fmt_ts(r[2]),
                f"{r[4]:.1f}s",
                f"{rt:.1f}x",
                r[3].upper(),
                "[red]errore[/red]" if r[5] else "[green]ok[/green]",
            )
        console.print(table)

        speedup = (audio_tot / wall) if wall else 0.0
        msg = (
            f"[green]✅ {len(ok)} trascritti[/green]"
            + (f"  |  [red]❌ {len(errori)} errori[/red]" if errori else "")
            + f"\nAudio totale: [cyan]{_fmt_ts(audio_tot)}[/cyan]  ·  "
            f"tempo totale (incluso caricamento): [cyan]{wall:.1f}s[/cyan]\n"
            f"Velocità complessiva: [bold magenta]{speedup:.1f}x realtime[/bold magenta]"
        )
        console.print(Panel(msg, title="Riepilogo", border_style="yellow" if errori else "green"))
        if errori:
            for r in errori:
                console.print(f"[red]• {Path(r[0]).name}: {r[5]}[/red]")
    else:
        for r in sorted(results, key=lambda x: x[0]):
            logger.info(
                f"{Path(r[0]).name}: {_fmt_ts(r[2])} audio in {r[4]:.1f}s "
                f"({'OK' if not r[5] else 'ERRORE: ' + r[5]})"
            )
        logger.info(
            f"Completato: {len(ok)}/{len(results)} file · "
            f"audio {_fmt_ts(audio_tot)} in {wall:.1f}s "
            f"({(audio_tot / wall) if wall else 0:.1f}x realtime)"
        )

    if errori:
        sys.exit(1)


if __name__ == "__main__":
    main()
