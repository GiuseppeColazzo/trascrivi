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
# Ollama è nell'elenco per i modelli locali "reasoning" (qwen3.5 e simili): senza,
# spendono tutto il budget in ragionamento e restituiscono `content` vuoto con
# `finish_reason="length"` — misurato qui: 2392 caratteri di pensiero e zero note.
# Il suo endpoint OpenAI-compatibile accetta il parametro, e se un modello non lo
# conosce `_is_unknown_parameter` fa riprovare senza.
REASONING_EFFORT_PROVIDERS = {"deepseek", "ollama"}
REQUEST_TIMEOUT = 180.0

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


# Gli embeddings servono al matching slide -> lezioni e sono locali (Ollama):
# costano zero e non toccano la chiave dell'utente. Passano però dallo stesso
# adapter, così c'è un solo punto che parla con i provider.
EMBED_BATCH = 32


def embed_texts(provider: dict, texts: list[str], model: str) -> list[list[float]]:
    """
    Embedding di una lista di testi, a blocchi: la risposta è nello stesso ordine.

    Gli errori vengono riscritti con il provider e l'indirizzo: il caso normale è
    Ollama spento, e un job fallito con "Connection error." non dice a nessuno
    che cosa avviare. Il messaggio originale resta nella catena (`from exc`).
    """
    if not texts:
        return []
    client = _client(provider)
    out: list[list[float]] = []
    try:
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start:start + EMBED_BATCH]
            resp = client.embeddings.create(model=model, input=batch)
            out.extend(list(item.embedding) for item in resp.data)
    except Exception as exc:  # noqa: BLE001 - il tipo dipende dall'SDK e dal provider
        raise RuntimeError(
            f"Embeddings from '{provider.get('name')}' ({provider.get('base_url')}) failed: "
            f"{type(exc).__name__}: {exc}. If it is Ollama, start it and pull the model "
            f"(`ollama pull {model}`)."
        ) from exc
    if len(out) != len(texts):
        raise RuntimeError(f"Embedding truncated: {len(out)} vectors for {len(texts)} texts")
    return out


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


# ── JSON dalle risposte ───────────────────────────────────────────────
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


def chat_completion(provider: dict, model: str, system: str, user: str, *,
                    max_tokens: int, temperature: float = 0.0,
                    json_mode: bool = True,
                    images: list[str] | None = None) -> tuple[str, dict]:
    """
    Una chiamata chat generica sullo stesso adattatore dei provider.

    È il punto unico in cui si decide come parlare ai provider: il riassunto e le
    carte lo usano invece di reimplementare JSON mode e disattivazione del
    thinking, così una correzione vale per tutte le funzioni.

    `images` sono PNG in base64: quando ci sono, il messaggio utente diventa
    multimodale. Sapere *quali* provider accettano immagini è compito di chi
    chiama (`cards.VISION_PROVIDERS`): qui si manda quello che ci viene dato.
    """
    client = _client(provider)

    user_content: object = user
    if images:
        user_content = [{"type": "text", "text": user}, *[
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}
            for data in images
        ]]

    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # `response_format` non è supportato allo stesso modo da tutti i provider:
    # lo usiamo dove è sicuro, perché costringe il modello a produrre JSON.
    name = provider.get("name")
    if json_mode and name in ("ollama", "deepseek"):
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
    # `finish_reason="length"` su un output JSON significa JSON tagliato a metà:
    # chi chiama deve saperlo, altrimenti interpreta un troncamento come un
    # modello che "non collabora".
    diag["model"] = model
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
            f"'deepseek-flash' for this task."
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


def token_usage(diag: dict) -> dict:
    """
    Conteggi token normalizzati, inclusi i token serviti dalla cache.

    DeepSeek riporta `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` e la
    differenza di prezzo fra i due è 50×: senza separarli il costo stimato è
    sbagliato di un ordine di grandezza. Gli altri provider non li espongono e
    ricadiamo su "tutto miss".
    """
    usage = diag.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = usage.get("prompt_cache_miss_tokens")
    miss = int(miss) if miss is not None else max(0, prompt - hit)
    return {"prompt": prompt, "completion": completion, "hit": hit, "miss": miss}


# Tariffe DeepSeek `deepseek-flash` in USD per 1M token, listino letto il
# 2026-09-23. Fuori picco = 50% esatto del picco; qui si usa la fascia off-peak
# perché è quella in cui cade quasi tutto l'orario europeo. È una STIMA: il
# costo vero è quello che fattura il provider, e per modelli diversi da
# `deepseek-flash` questo listino è quello sbagliato.
DEEPSEEK_RATES = {
    "cache_hit": 0.003,
    "cache_miss": 0.15,
    "output": 0.60,
}


def estimate_cost(usage: dict) -> float | None:
    """
    Costo stimato in USD alle tariffe DeepSeek off-peak.

    `usage` è la forma di `token_usage`. Restituisce `None` quando il provider non
    ha riportato alcun token: uno zero direbbe "è stato gratis", che è
    un'affermazione diversa da "non lo so" — e su una vista costi la differenza
    conta.
    """
    if not any(usage.get(k) for k in ("prompt", "completion", "hit", "miss")):
        return None
    return round(
        usage.get("hit", 0) * DEEPSEEK_RATES["cache_hit"] / 1e6
        + usage.get("miss", 0) * DEEPSEEK_RATES["cache_miss"] / 1e6
        + usage.get("completion", 0) * DEEPSEEK_RATES["output"] / 1e6,
        6,
    )


def add_usage(total: dict, diag: dict) -> None:
    """
    Somma i token di una chiamata al totale di un job.

    Un job fa più chiamate (una per chunk di trascrizione, più i riassunti degli
    spezzoni): il totale si costruisce accumulando, non leggendo l'ultima.
    """
    counts = token_usage(diag or {})
    for key in ("prompt", "completion", "hit", "miss"):
        total[key] += counts[key]


def usage_counts(usage: dict) -> dict:
    """
    I campi di costo da salvare, a partire dall'usage cumulato di un job.

    Le chiavi sono già quelle delle colonne di `jobs`/`summaries`, così chi
    salva non deve rimappare niente. `cost_usd` è 0 quando il provider non
    riporta i token: chi legge distingue "non lo so" da "zero" guardando se i
    token ci sono (`tokens_in + tokens_out + cache_hit == 0`).
    """
    return {
        "tokens_in": usage.get("prompt", 0),
        "tokens_out": usage.get("completion", 0),
        "cache_hit": usage.get("hit", 0),
        "cost_usd": estimate_cost(usage) or 0.0,
    }
