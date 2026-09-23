"""
trascrivi/llm.py
================
Adapter LLM: OpenRouter, DeepSeek e Ollama (locale).

Tutti e tre parlano il protocollo OpenAI, quindi il codice è uno solo: cambia
solo `base_url`. Il client `openai` è già una dipendenza installata; viene
importato dentro la funzione per non rallentare l'avvio del server.

Le chiavi API sono cifrate (Fernet, chiave in data/secret.key) e non escono mai
dal processo: l'API HTTP risponde solo `has_key: true`.
"""

from __future__ import annotations

import json
import logging
import re

from . import config

log = logging.getLogger("trascrivi.llm")

# Tetto di output per chunk. Deve stare alto: i modelli con "reasoning" consumano
# token di pensiero che non compaiono nel contenuto, e un tetto troppo basso
# restituisce HTTP 200 con `content` VUOTO (successo apparente, zero risultato).
MAX_OUTPUT_TOKENS = 8000
REQUEST_TIMEOUT = 180.0

SYSTEM_PROMPT = """You are a transcript proofreader for university lecture transcripts \
produced by automatic speech recognition (Whisper).

Return ONLY a JSON object, no prose, no markdown fences:
{"replacements": [{"find": "...", "replace": "...", "reason": "...", "kind": "term|asr_error|punctuation|grammar|name|formatting", "confidence": 0.0}], "glossary": [{"term": "...", "meaning": "..."}]}

Rules for "find":
- Copy it VERBATIM from the transcript, including punctuation. Maximum 80 characters.
- It must occur exactly once in the text you are given. If a phrase appears twice, skip it.
- Never rewrite or rephrase whole sentences: only fix what is objectively wrong.
Rules for "replace":
- Minimum edit that fixes the problem. Keep the speaker's meaning, style and wording.
- Never translate, never summarise, never add content, keep the same spelling variant.
What to fix, in order of importance:
1. "term": ASR mishearings of technical terms, acronyms, proper nouns, formulas (e.g. "pie torch" -> "PyTorch").
2. "asr_error": obvious mis-recognitions that change meaning.
3. "name": names of people, tools, papers, places.
4. "punctuation": missing sentence boundaries, run-on sentences, wrong capitalisation of proper nouns.
5. "grammar": agreement and typos that came from the recogniser.
Do NOT change: filler words, spoken repetition, discourse markers, informal register.
If there is nothing worth fixing, return {"replacements": []}.
"glossary" is optional: at most 5 entries, only for terms a student would look up."""


# ── Chunking ─────────────────────────────────────────────────────────────────
def chunk_segments(segments: list[dict], budget: int = 8000) -> list[list[dict]]:
    """
    Raggruppa i segmenti in blocchi sotto il budget di caratteri del prompt.

    L'ultimo segmento del blocco precedente viene ripetuto in coda come
    contesto: senza, una correzione a cavallo del taglio non è verificabile
    (il modello non vede la frase intera). Le proposte duplicate che ne derivano
    vengono scartate da `build_plan`, quindi il costo è solo qualche token.
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for seg in segments:
        text = seg.get("text", "")
        if current and size + len(text) > budget:
            chunks.append(current)
            current = []
            size = 0
        current.append(seg)
        size += len(text)
    if current:
        chunks.append(current)
    for i in range(1, len(chunks)):
        if chunks[i - 1]:
            chunks[i] = [chunks[i - 1][-1]] + chunks[i]
    return chunks


def chunk_text(chunk: list[dict]) -> str:
    from .textutil import fmt_ts
    return "\n".join(f"[{fmt_ts(s.get('start', 0.0))}] {s.get('text', '')}" for s in chunk)


# ── Chiavi ───────────────────────────────────────────────────────────────────
def encrypt_key(plaintext: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(config.load_or_create_secret()).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_key(token: str | None) -> str | None:
    if not token:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(config.load_or_create_secret()).decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as exc:
        # Causa tipica: data/secret.key è stato rigenerato (o il DB è stato
        # spostato senza la chiave), quindi il valore cifrato non è più
        # decifrabile. Non c'è modo di recuperarlo: va reinserita la chiave.
        log.error(
            "API key non decifrabile (%s): la chiave salvata in data/secret.key non "
            "corrisponde a quella usata per cifrarla. Reinserisci l'API key del provider.",
            type(exc).__name__,
        )
        return None


# ── Client ───────────────────────────────────────────────────────────────────
def _client(provider: dict):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - il pacchetto è in requirements
        raise RuntimeError(
            "The 'openai' package is required for the correction agent. "
            "Install it with: .venv\\Scripts\\python.exe -m pip install openai"
        ) from exc

    key = decrypt_key(provider.get("api_key_enc"))
    name = provider.get("name", "")
    if provider.get("api_key_enc") and not key:
        raise RuntimeError(
            f"The stored API key for '{name}' cannot be decrypted: data/secret.key no longer "
            f"matches the key used to encrypt it. Re-enter the API key in Settings and save."
        )
    if not key and name != "ollama":
        raise RuntimeError(f"No API key configured for provider '{name}'.")
    base_url = provider.get("base_url") or ""
    headers = {}
    if name == "openrouter":
        # Identifica l'app: OpenRouter lo consiglia per il rate limiting.
        headers = {"HTTP-Referer": "http://localhost", "X-Title": "Trascrivi"}
    return OpenAI(api_key=key or "not-needed", base_url=base_url,
                  default_headers=headers, timeout=REQUEST_TIMEOUT, max_retries=2)


def list_models(provider: dict) -> list[str]:
    """Modelli disponibili secondo il provider (Ollama è utile, gli altri no)."""
    client = _client(provider)
    models = client.models.list()
    return sorted(m.id for m in getattr(models, "data", []) or [])


def test_provider(provider: dict) -> dict:
    """
    Chiamata minima per validare chiave, modello e formato della risposta.

    Riporta anche come il modello ha risposto (finish_reason, token usati): è la
    differenza fra "chiave sbagliata" e "modello che risponde a vuoto".
    """
    model = provider.get("model") or ""
    if not model:
        return {"ok": False, "error": "No model configured"}
    client = _client(provider)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        max_tokens=64,
        temperature=0,
    )
    text, diag = extract_content(resp)
    return {
        "ok": bool(text.strip()),
        "model": model,
        "reply": text.strip()[:60],
        "finish_reason": diag.get("finish_reason"),
        "usage": diag.get("usage"),
        "error": None if text.strip() else (
            "The model replied with an empty message"
            + (f" (finish_reason={diag.get('finish_reason')})" if diag.get("finish_reason") else "")
        ),
    }


# ── Estrazione dei replacement ───────────────────────────────────────────────
def extract_json(raw: str) -> dict | None:
    """
    Estrae il primo oggetto JSON bilanciato dal testo restituito dal modello.

    I modelli locali aggiungono spesso ```json …``` o una frase di preambolo:
    tagliare sul primo `{` bilanciato è più robusto di `json.loads` diretto.
    """
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S | re.I)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                    except ValueError:
                        break
                    return parsed if isinstance(parsed, dict) else None
        start = text.find("{", start + 1)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def _clean_proposals(payload: dict) -> list[dict]:
    """Normalizza l'output del modello in proposte usabili dall'engine."""
    out: list[dict] = []
    for raw in payload.get("replacements") or []:
        if not isinstance(raw, dict):
            continue
        find = str(raw.get("find") or "").strip()
        replace = str(raw.get("replace") or "")
        if not find:
            continue
        conf = raw.get("confidence")
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        out.append({
            "find": find,
            "replace": replace,
            "reason": str(raw.get("reason") or "")[:300],
            "kind": str(raw.get("kind") or "other").lower(),
            "confidence": conf,
        })
    return out


def _clean_glossary(payload: dict) -> list[dict]:
    out: list[dict] = []
    for raw in payload.get("glossary") or []:
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("term") or "").strip()
        if not term:
            continue
        out.append({"term": term[:120], "meaning": str(raw.get("meaning") or "")[:300]})
    return out[:10]


def ask_for_replacements(provider: dict, transcript_text: str,
                         extra_instruction: str = "") -> tuple[list[dict], list[dict]]:
    """
    Una chiamata al modello su un chunk. Restituisce (proposte, glossario).

    `temperature=0` perché non vogliamo creatività: vogliamo sostituzioni
    verificabili. Il testo arriva con i timestamp per non perdere il contesto
    del parlato.
    """
    model = provider.get("model") or ""
    if not model:
        raise RuntimeError(f"No model configured for provider '{provider.get('name')}'.")
    client = _client(provider)
    user = (
        "Transcript chunk (each line starts with [mm:ss]; the timestamps are NOT part "
        "of the text and must never appear inside \"find\"):\n\n"
        f"{transcript_text}"
    )
    if extra_instruction:
        user += f"\n\nAdditional course-specific instructions:\n{extra_instruction}"

    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    # Ollama e i provider OpenAI-compatibili accettano `response_format`, ma con
    # sfumature diverse: lo usiamo solo dove è sicuro.
    if provider.get("name") == "ollama":
        kwargs["response_format"] = {"type": "json_object"}

    resp = client.chat.completions.create(**kwargs)
    raw, diag = extract_content(resp)
    payload = extract_json(raw)
    if payload is None:
        # Qui c'è il caso peggiore da distinguere: `content` vuoto con HTTP 200.
        # Succede con i modelli "reasoning" quando il budget di token viene
        # consumato dal pensiero, o quando il modello risponde solo in un campo
        # separato (reasoning_content).
        hint = ""
        if not raw:
            hint = (" The model returned an empty response"
                    f" (finish_reason={diag.get('finish_reason')!r},"
                    f" reasoning={diag.get('reasoning_chars', 0)} chars,"
                    f" usage={diag.get('usage')})."
                    " If it is a reasoning model, raise the token budget or use a"
                    " non-reasoning model for this task.")
        log.warning("risposta LLM non interpretabile (%d caratteri)%s: %s",
                    len(raw), hint, raw[:300])
        raise ValueError("The model did not return valid JSON." + hint)
    return _clean_proposals(payload), _clean_glossary(payload)


def extract_content(resp) -> tuple[str, dict]:
    """
    Estrae il testo dalla risposta e raccoglie la diagnostica utile.

    I modelli "reasoning" (deepseek-reasoner e simili) espongono il pensiero in un
    campo separato e a volte lasciano `content` vuoto: senza guardare
    `finish_reason` e `reasoning_content` l'errore sembra un JSON malformato,
    mentre il problema è il budget di token.
    """
    if not getattr(resp, "choices", None):
        return "", {"finish_reason": None, "reasoning_chars": 0, "usage": None}
    choice = resp.choices[0]
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) or ""
    extra = getattr(message, "model_extra", None) or {}
    reasoning = extra.get("reasoning_content") or extra.get("reasoning") or ""
    usage = getattr(resp, "usage", None)
    return content, {
        "finish_reason": getattr(choice, "finish_reason", None),
        "reasoning_chars": len(reasoning),
        "usage": (usage.model_dump() if hasattr(usage, "model_dump") else usage),
    }


def estimate_tokens(text: str) -> int:
    """Stima grossolana (~4 caratteri per token) per il costo mostrato a schermo."""
    return max(1, len(text) // 4)
