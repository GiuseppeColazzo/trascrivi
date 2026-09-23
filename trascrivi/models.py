"""
trascrivi/models.py
===================
Cache dei modelli Whisper e informazioni sull'hardware.

Un modello `large` pesa ~1.5 GB su disco e qualche GB di VRAM: ricaricarlo a
ogni job costerebbe decine di secondi. La cache tiene in memoria fino a
`MAX_CACHED` modelli (LRU). Su una GPU da 8 GB due modelli large non stanno
insieme, quindi il limite è basso e c'è un endpoint per svuotarla.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from trascrivi_audio import (
    MODELS,
    SUPPORTED_FORMATS,
    cuda_device_count,
    cuda_free_vram_gb,
    cuda_gpu_name,
    is_english_only,
    probe_media,
    resolve_runtime,
)

log = logging.getLogger("trascrivi.models")

# ponytail: cache in-process, non condivisa fra processi. Il collo di bottiglia è
# la GPU, quindi il server gira a worker singolo: se un giorno servisse scalare,
# l'unico punto da cambiare è `get_model`/`unload_all`.
MAX_CACHED = 2

_lock = threading.Lock()
_cache: "OrderedDict[tuple, object]" = OrderedDict()


def get_model(model_name: str, device: str = "auto", compute_type: str = "auto",
              cpu_threads: int | None = None):
    """
    Restituisce (modello, RuntimeConfig) riusando un'istanza già caricata.

    Il modello è thread-safe per l'inferenza su GPU (ctranslate2 serializza) ma
    un solo worker per volta lo usa comunque, quindi il lock serve solo a
    proteggere la cache.
    """
    cfg = resolve_runtime(device, compute_type, None, model_name)
    key = (model_name, cfg.device, cfg.compute_type, cpu_threads or cfg.cpu_threads)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            log.info("modello riusato dalla cache: %s", key)
            return _cache[key], cfg

    # Caricamento fuori dal lock: è lento, non deve bloccare un unload.
    from faster_whisper import WhisperModel

    log.info("carico modello %s (device=%s compute=%s)", model_name, cfg.device, cfg.compute_type)
    model = WhisperModel(
        model_name,
        device=cfg.device,
        compute_type=cfg.compute_type,
        cpu_threads=cpu_threads or cfg.cpu_threads,
    )
    with _lock:
        _cache[key] = model
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHED:
            old_key, _old = _cache.popitem(last=False)
            log.info("scarico modello dalla cache: %s", old_key)
    return model, cfg


def loaded_models() -> list[dict]:
    with _lock:
        return [
            {"model": k[0], "device": k[1], "compute_type": k[2], "cpu_threads": k[3]}
            for k in _cache
        ]


def unload_all() -> int:
    with _lock:
        n = len(_cache)
        _cache.clear()
    if n:
        _free_cuda()
    return n


def _free_cuda() -> None:
    """Rilascia la memoria CUDA se torch è disponibile; altrimenti no-op."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def hardware_info() -> dict:
    """Riepilogo hardware per /api/health e per i default del form."""
    n_gpu = cuda_device_count()
    free = cuda_free_vram_gb() if n_gpu else None
    return {
        "cuda_devices": n_gpu,
        "gpu_name": cuda_gpu_name() if n_gpu else None,
        "vram_free_gb": round(free, 2) if free is not None else None,
        "cpu_count": _cpu_count(),
        "cached_models": loaded_models(),
    }


def _cpu_count() -> int:
    import os
    return os.cpu_count() or 1


def supported_formats() -> list[str]:
    return sorted(SUPPORTED_FORMATS)


def resolve_options(model: str, language: str, device: str,
                    compute_type: str) -> tuple[str, list[str]]:
    """
    Valida modello+lingua e normalizza la lingua (None = auto-detect).

    Riusa la stessa regola della CLI in modo che il messaggio d'errore sia
    identico: un modello .en/distil-* con lingua diversa dall'inglese non è
    utilizzabile.
    """
    lang = (language or "en").strip().lower()
    normalized = None if lang in {"auto", ""} else lang
    notes: list[str] = []
    if model not in MODELS:
        raise ValueError(f"Unsupported model: {model}")
    if is_english_only(model):
        if normalized is None:
            normalized = "en"
            notes.append(f"{model} is English-only: language forced to 'en'.")
        elif normalized != "en":
            raise ValueError(
                f"Model {model} only works in English, but language is '{normalized}'. "
                "Use large-v3 (multilingual) or language 'en'."
            )
    return normalized, notes


# `probe_media` è ri-esportata: l'API la usa per validare un path prima del job.
__all__ = [
    "get_model", "loaded_models", "unload_all", "hardware_info",
    "supported_formats", "resolve_options", "probe_media", "MODELS", "MAX_CACHED",
]
