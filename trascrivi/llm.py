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

# Il "thinking mode" di DeepSeek è attivo di default con effort `high`: su un
# compito di estrazione come il nostro brucia il budget in ragionamento e può
# restituire `content` vuoto. Con `reasoning_effort="none"` il modello risponde
# diretto (e `temperature` torna ad avere effetto: in thinking mode è ignorata).
# Vedi https://api-docs.deepseek.com/guides/thinking_mode/
NO_THINKING = {"reasoning_effort": "none"}
THINKING_OFF_BODY = {"thinking": {"type": "disabled"}}
# Provider a cui mandiamo `reasoning_effort` senza doverlo scoprire a errori.
REASONING_EFFORT_PROVIDERS = {"deepseek"}
# Tetto di output per chunk. Non è il budget di ragionamento: con il thinking
# disattivato serve solo a produrre il JSON delle proposte.
MAX_OUTPUT_TOKENS = 8000
REQUEST_TIMEOUT = 180.0

SYSTEM_PROMPT = """You are a transcript proofreader for university lecture transcripts produced by speech recognition (Whisper).

Return ONLY a JSON object, no prose, no markdown fences:
{"replacements": [{"find": "...", "replace": "...", "reason": "...", "kind": "term|asr_error|name", "confidence": 0.0}], "glossary": [{"term": "...", "meaning": "..."}]}

Your job is ONLY to fix words the recogniser HEARD WRONG when the intended word is unmistakable from the sentence. Nothing else.

For every candidate ask yourself: "would a human typing this transcript while listening have written something different?" If the answer is no, do not propose it.

NEVER propose:
- wording you merely prefer, rephrasing, or style changes;
- spelling variants, hyphenation, British vs American spelling;
- capitalisation of ordinary words, or of acronyms already understood;
- punctuation-only changes, unless the missing punctuation makes a sentence unreadable;
- removing or changing filler words, repetitions, hesitations, or spoken grammar;
- anything whose meaning is already clear.

DO propose, only when unambiguous:
- "term": an acronym or technical term the recogniser spelled out or mangled ("s g d" -> "SGD", "pie torch" -> "PyTorch", "COGO" -> "CO2");
- "name": a proper noun or title clearly wrong given the context ("Mecca" -> "Meta" in a lawsuit about a tech company);
- "asr_error": a string that is not a word in this context and whose replacement the sentence makes obvious ("four people" -> "poor people", "gave back" -> "give-back").

Rules for "find":
- Copy it VERBATIM from the transcript. Maximum 80 characters.
- It must occur exactly once in the text you are given. If it appears twice, skip it.
- Never put a timestamp inside "find".
Rules for "replace":
- The smallest edit that fixes the recognition error. Every other word stays identical.
- Never add or remove information, never translate, never reorder words.

Be strict. Most chunks contain only a handful of real recognition errors; returning very few items, or none at all, is the correct answer for clean audio.
Hard limits, and they are low on purpose:
- at most 5 items in "replacements". A 30-minute stretch usually contains two or three real errors, not twenty.
- every item must carry "confidence": 0.9 or higher. If you are not that sure, leave it out.
- 12 words per "reason". "glossary" is optional and at most 3 entries.

Do not try to fill the quota. Filling it with guesses is worse than returning nothing: a wrong suggestion costs the reader more than a missed correction."""


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


# Sotto questa confidenza la proposta viene scartata. Misurato: senza soglia il
# modello riempie la quota di ipotesi (0.4-0.7); con cap basso e soglia 0.9
# restano solo le correzioni difendibili. Una correzione sbagliata costa al
# lettore più di una correzione mancata.
MIN_CONFIDENCE = 0.9


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
        "error": None if text.strip() else _error_detail(diag),
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
        # Il modello a volte restituisce `replace` identico a `find` (misurato:
        # 217 su 563 su una lezione reale). Non è una correzione: va scartata
        # qui, altrimenti riempie la review di voci inutili.
        if find == replace.strip():
            continue
        conf = raw.get("confidence")
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        # Sotto la soglia la proposta non arriva nemmeno in review.
        if conf is not None and conf < MIN_CONFIDENCE:
            continue
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


# Budget caratteri per chiamata. Il JSON delle proposte è la voce che cresce:
# su una lezione reale il modello trova decine di correzioni per chunk, e con
# chunk troppo grandi il JSON supera il tetto di token e non si chiude mai
# (`finish_reason="length"`). Misurato: 40 segmenti (~12k caratteri) → 92 proposte
# in 16k caratteri, al limite; 60 segmenti → il JSON non si chiude. 4000 caratteri
# restano larghi e la qualità dello spezzone non ne soffre.
def chunk_budget() -> int:
    return int(config.load_settings().llm_chunk_chars or 4000)


def ask_for_replacements(provider: dict, transcript_text: str,
                         extra_instruction: str = "") -> tuple[list[dict], list[dict]]:
    """Un chunk -> (proposte, glossario), con ripiego sul modello non-reasoning."""
    proposals, glossary, _diag = ask_with_fallback(provider, transcript_text, extra_instruction)
    return proposals, glossary


def ask_with_fallback(provider: dict, transcript_text: str, extra_instruction: str = "",
                      max_tokens: int | None = None) -> tuple[list[dict], list[dict], dict]:
    """
    Prova il modello configurato; se spende tutto in ragionamento, riprova.

    Un modello "reasoning" può consumare l'intero budget di token senza scrivere
    nulla nel contenuto (finish_reason="length", reasoning_tokens == completion).
    In quel caso alzare il budget non serve: se ne mangia altri. Si riprova una
    sola volta con `fallback_model`, che per DeepSeek è `deepseek-chat`
    (non-reasoning): stessa qualità su questo compito, una frazione del costo.
    """
    model = (provider.get("model") or "").strip()
    if not model:
        raise RuntimeError(f"No model configured for provider '{provider.get('name')}'.")

    candidates = [model]
    fallback = (provider.get("fallback_model") or "").strip()
    if fallback and fallback != model:
        candidates.append(fallback)

    budget = int(max_tokens or MAX_OUTPUT_TOKENS)
    diagnostics: list[dict] = []
    for index, candidate in enumerate(candidates):
        try:
            content, diag = _chat_once(provider, candidate, transcript_text,
                                       extra_instruction, budget)
        except Exception as exc:  # rete, 429, modello inesistente…
            diagnostics.append({"model": candidate, "error": f"{type(exc).__name__}: {exc}"[:200]})
            if index + 1 < len(candidates):
                log.warning("modello %s fallito (%s): provo il ripiego %s",
                            candidate, type(exc).__name__, candidates[index + 1])
                continue
            raise

        diagnostics.append({"model": candidate, **diag})
        payload = extract_json(content)
        if payload is not None:
            if index:
                log.info("chunk risolto dal modello di ripiego %s", candidate)
            return _clean_proposals(payload), _clean_glossary(payload), diagnostics[-1]

        if content:
            log.warning("risposta di %s non interpretabile (%d caratteri): %s",
                        candidate, len(content), content[:200])
        if index + 1 < len(candidates):
            log.warning(
                "modello %s non ha prodotto JSON utilizzabile (finish_reason=%s, "
                "reasoning=%s caratteri, %s token di ragionamento): riprovo con %s",
                candidate, diag.get("finish_reason"), diag.get("reasoning_chars"),
                diag.get("reasoning_tokens"), candidates[index + 1],
            )

    detail = _error_detail(diagnostics[-1])
    log.warning("risposta LLM non interpretabile su tutti i modelli disponibili: %s", detail)
    raise ValueError(f"No usable JSON from the configured models. {detail}")


# Eccezione dedicata: il chunk era troppo grande per una singola risposta. Chi
# chiama può spezzarlo e riprovare invece di perdere il lavoro.
class ChunkTooLarge(ValueError):
    pass


def ask_chunk(provider: dict, chunk: list[dict], extra_instruction: str = "",
              depth: int = 0) -> tuple[list[dict], list[dict], dict, dict[int, list[dict]]]:
    """
    Chiede le correzioni per un chunk, spezzandolo se la risposta non ci sta.

    Un chunk grande produce più proposte di quante ne entrino in una risposta:
    il JSON viene tagliato a metà e non è recuperabile. Invece di perdere lo
    spezzone lo si dimezza e si ricomincia, fino a un limite di ricorsione.
    Restituisce (proposte, glossario, diagnostica, {id(proposta): sub-chunk}),
    così chi chiama mappa ogni proposta sul testo che l'ha generata davvero.
    """
    text = chunk_text(chunk)
    try:
        proposals, glossary, diag = ask_with_fallback(provider, text, extra_instruction)
        return proposals, glossary, diag, {id(p): chunk for p in proposals}
    except ValueError as exc:
        if "did not return valid JSON" not in str(exc) and "No usable JSON" not in str(exc):
            raise
        if len(chunk) <= 2 or depth >= 4:
            raise ChunkTooLarge(
                f"The transcript chunk could not be processed even after splitting "
                f"({len(chunk)} segments left). {_error_detail({'finish_reason': 'length'})}"
            ) from exc
        middle = len(chunk) // 2
        log.warning("chunk di %d segmenti troppo grande per una risposta: lo divido in due", len(chunk))
        first = ask_chunk(provider, chunk[:middle], extra_instruction, depth + 1)
        second = ask_chunk(provider, chunk[middle:], extra_instruction, depth + 1)
        merged_map = {**first[3], **second[3]}
        return (first[0] + second[0], first[1] + second[1], first[2], merged_map)


def _chat_once(provider: dict, model: str, transcript_text: str,
               extra_instruction: str, max_tokens: int) -> tuple[str, dict]:
    """Una singola chiamata: restituisce (contenuto, diagnostica)."""
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
        "max_tokens": max_tokens,
    }
    # `response_format` non è supportato allo stesso modo da tutti i provider:
    # lo usiamo dove è sicuro, perché costringe il modello a produrre JSON.
    name = provider.get("name")
    if name in ("ollama", "deepseek"):
        kwargs["response_format"] = {"type": "json_object"}
    reasoning_requested = name in REASONING_EFFORT_PROVIDERS

    if reasoning_requested:
        kwargs.update(NO_THINKING)
        if name == "deepseek":
            # Forma esplicita accettata da DeepSeek, oltre a `reasoning_effort`.
            kwargs["extra_body"] = dict(THINKING_OFF_BODY)

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        # Provider che non conoscono `reasoning_effort`: si riprova senza, invece
        # di far fallire il job per un parametro opzionale.
        if reasoning_requested and _is_unknown_parameter(exc):
            log.info("il provider %s non accetta reasoning_effort: riprovo senza", name)
            kwargs.pop("reasoning_effort", None)
            kwargs.pop("extra_body", None)
            resp = client.chat.completions.create(**kwargs)
        else:
            raise

    content, diag = extract_content(resp)
    return content, dict(diag)


def _is_unknown_parameter(exc: Exception) -> bool:
    """True se il provider ha rifiutato un parametro che non conosce."""
    text = str(exc).lower()
    return any(marker in text for marker in
               ("reasoning_effort", "thinking", "unknown", "unrecognized",
                "unsupported", "invalid_request_error", "extra_forbidden"))


def _error_detail(diag: dict) -> str:
    """Spiega in una riga cosa è andato storto, per la UI."""
    if diag.get("error"):
        return f"The model call failed: {diag['error']}."
    if diag.get("reasoning_tokens"):
        return (
            f"The model spent the whole {diag.get('completion_tokens')}-token budget on "
            f"internal reasoning and returned no text (finish_reason="
            f"{diag.get('finish_reason')!r}). Use a non-reasoning model such as "
            f"'deepseek-chat' for this task."
        )
    if diag.get("finish_reason") == "length":
        return (f"The response was cut off at the token limit "
                f"(finish_reason='length'): raise the budget or shorten the chunk.")
    if not diag.get("content_chars"):
        return "The model returned an empty response."
    return "The model did not return valid JSON."


def extract_content(resp) -> tuple[str, dict]:
    """
    Estrae il testo dalla risposta e raccoglie la diagnostica utile.

    I modelli "reasoning" (deepseek-reasoner e simili) espongono il pensiero in un
    campo separato e a volte lasciano `content` vuoto: senza guardare
    `finish_reason` e `reasoning_content` l'errore sembra un JSON malformato,
    mentre il problema è il budget di token.
    """
    if not getattr(resp, "choices", None):
        return "", {"finish_reason": None, "reasoning_chars": 0, "reasoning_tokens": None,
                    "completion_tokens": None, "content_chars": 0, "usage": None}
    choice = resp.choices[0]
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) or ""
    # DeepSeek espone `reasoning_content` come campo del messaggio; l'SDK non lo
    # tipizza, quindi lo cerchiamo sia come attributo sia in `model_extra`.
    extra = getattr(message, "model_extra", None) or {}
    reasoning = (getattr(message, "reasoning_content", None)
                 or extra.get("reasoning_content") or extra.get("reasoning") or "")
    usage = getattr(resp, "usage", None)
    dumped = usage.model_dump() if hasattr(usage, "model_dump") else usage
    details = (dumped or {}).get("completion_tokens_details") or {} if isinstance(dumped, dict) else {}
    return content, {
        "finish_reason": getattr(choice, "finish_reason", None),
        "reasoning_chars": len(reasoning),
        # `reasoning_tokens` è la prova che il budget è finito nel pensiero.
        "reasoning_tokens": details.get("reasoning_tokens"),
        "completion_tokens": (dumped or {}).get("completion_tokens") if isinstance(dumped, dict) else None,
        "content_chars": len(content),
        "usage": dumped,
    }


def estimate_tokens(text: str) -> int:
    """Stima grossolana (~4 caratteri per token) per il costo mostrato a schermo."""
    return max(1, len(text) // 4)
