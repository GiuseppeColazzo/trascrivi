"""
bench_local_llm.py - Misura la fattibilita' reale della summarization locale.

Interroga Ollama con un prompt di "map phase" realistico su uno spezzone di
trascrizione vera, e riporta:
  * se il modello gira su GPU, ibrido o CPU
  * velocita' di prompt-eval e di generazione (token/s)
  * VRAM di picco osservata durante la chiamata
  * tempo totale stimato per una lezione intera

Uso:
  python .smoke\\bench_local_llm.py --model qwen3.5:9b --ctx 8192 --chars 12000
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLLAMA = "http://localhost:11434"

SYSTEM = """You are a study-notes extractor for university lecture transcripts.
Return ONLY a JSON object:
{"topics":[{"title":"...","points":[{"text":"...","quote":"verbatim <=200 chars","ts":"mm:ss"}],"terms":["..."]}]}
Rules: extract only what the speaker actually says; never add outside knowledge;
every point must carry a verbatim quote copied from the transcript; at most 6 topics."""


def nvidia_smi(field: str) -> float:
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={field}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()[0]
        return float(out)
    except Exception:
        return -1.0


class VramSampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak = 0.0
        self.peak_total = 0.0
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            used = nvidia_smi("memory.used")
            total = nvidia_smi("memory.total")
            self.peak = max(self.peak, used)
            self.peak_total = max(self.peak_total, total)
            time.sleep(0.25)

    def stop(self) -> None:
        self._stop_evt.set()
        self.join(timeout=3)


def load_chunk(chars: int) -> str:
    src = ROOT / "trascrizioni" / "2025-09-25-lec02pt1.txt"
    if not src.exists():
        sys.exit(f"missing {src}")
    lines = [l.rstrip("\n") for l in src.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    out, size = [], 0
    for line in lines:
        if size + len(line) > chars:
            break
        out.append(line)
        size += len(line)
    return "\n".join(out)


def bench(model: str, ctx: int, chunk: str, num_predict: int, think: bool) -> dict:
    body = {
        "model": model,
        "stream": False,
        # `think: false` e' obbligatorio: con il thinking attivo (default di
        # Qwen3.5) Ollama mette tutto in `message.thinking` e lascia
        # `message.content` VUOTO - lo stesso fallimento che llm.py gestisce
        # per DeepSeek con reasoning_effort="none".
        "think": think,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Transcript chunk:\n\n{chunk}"},
        ],
        "options": {"num_ctx": ctx, "num_predict": num_predict, "temperature": 0.2},
    }
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    sampler = VramSampler()
    sampler.start()
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        wall = time.time() - t0
        sampler.stop()

    pc = payload.get("prompt_eval_count") or 0
    ec = payload.get("eval_count") or 0
    pcd = (payload.get("prompt_eval_duration") or 0) / 1e9
    ecd = (payload.get("eval_duration") or 0) / 1e9
    load = (payload.get("load_duration") or 0) / 1e9
    content = (payload.get("message") or {}).get("content") or ""
    thinking = (payload.get("message") or {}).get("thinking") or ""

    return {
        "model": model,
        "ctx": ctx,
        "think": think,
        "prompt_tokens": pc,
        "output_tokens": ec,
        "load_s": round(load, 2),
        "prompt_eval_s": round(pcd, 2),
        "prompt_tok_s": round(pc / pcd, 1) if pcd else None,
        "eval_s": round(ecd, 2),
        "output_tok_s": round(ec / ecd, 1) if ecd else None,
        "wall_s": round(wall, 2),
        "vram_peak_mib": round(sampler.peak),
        "vram_total_mib": round(sampler.peak_total),
        "json_ok": content.strip().startswith("{"),
        "chars_out": len(content),
        "thinking_chars": len(thinking),
        "head": content[:180].replace("\n", " "),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--chars", type=int, default=12000)
    ap.add_argument("--num-predict", type=int, default=900)
    ap.add_argument("--think", action="store_true", help="lascia il thinking attivo (default: off)")
    ap.add_argument("--all", action="store_true", help="matrice model x ctx")
    args = ap.parse_args()

    chunk = load_chunk(args.chars)

    runs = [(args.model, args.ctx)] if not args.all else [
        ("qwen3.5:4b", 8192), ("qwen3.5:4b", 16384),
        ("qwen3.5:9b", 8192), ("qwen3.5:9b", 16384), ("qwen3.5:9b", 32768),
    ]

    results = []
    for model, ctx in runs:
        print(f"\n=== {model} num_ctx={ctx} chunk={len(chunk)} chars (~{len(chunk)//4} tok) ===")
        print(f"vram used prima: {nvidia_smi('memory.used'):.0f} / {nvidia_smi('memory.total'):.0f} MiB")
        try:
            r = bench(model, ctx, chunk, args.num_predict, args.think)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED: {type(exc).__name__}: {exc}")
            continue
        for k, v in r.items():
            if k != "head":
                print(f"  {k:>18}: {v}")
        print(f"  {'reply head':>18}: {r['head']}")
        results.append(r)

    print("\n=== RIEPILOGO ===")
    for r in results:
        print(f"{r['model']:>14} ctx={r['ctx']:<6} out_tok/s={r['output_tok_s']} "
              f"vram={r['vram_peak_mib']}MiB json={r['json_ok']} wall={r['wall_s']}s")


if __name__ == "__main__":
    main()
