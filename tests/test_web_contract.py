"""
Guardie sull'interfaccia web: niente browser, solo coerenza fra file e API.

Non sostituiscono un collaudo manuale, ma bloccano le rotture che si vedono solo
a pagina aperta: un `byId("x")` senza il relativo elemento in index.html, un
endpoint chiamato dal frontend che non esiste più, o un modulo JS con un errore
di sintassi.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from trascrivi import config
from trascrivi.api import create_app


@pytest.fixture()
def html() -> str:
    return (config.WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture()
def js() -> str:
    return (config.WEB / "app.js").read_text(encoding="utf-8")


def test_index_references_the_app_assets(html):
    assert '/app.js' in html
    assert '/style.css' in html
    # Il renderer del riassunto è un file a sé: se non viene caricato prima di
    # app.js, `renderMarkdown` non esiste e la tab del riassunto resta vuota.
    assert '/markdown.js' in html
    assert html.index('/markdown.js') < html.index('/app.js')
    # Il routing è a hash: la pagina viene servita solo alla radice.
    assert 'data-theme' in html


def test_static_ids_used_by_the_app_exist_in_the_markup(html, js):
    """Ogni byId(...) deve corrispondere a un id presente nell'HTML o creato in JS."""
    used = set(re.findall(r'byId\("([A-Za-z0-9_-]+)"\)', js))
    used |= set(re.findall(r"byId\('([A-Za-z0-9_-]+)'\)", js))
    assert used, "il frontend non usa byId: il test non sta verificando nulla"

    missing = []
    for element_id in sorted(used):
        if f'id="{element_id}"' in html:
            continue
        # Gli id dinamici sono creati da el(...) con id: "nome".
        if re.search(rf'id:\s*"{re.escape(element_id)}"', js):
            continue
        missing.append(element_id)
    assert missing == []


def test_theme_toggle_persists_the_choice(html, js):
    assert "trascrivi-theme" in html   # applicato prima del CSS, niente sfarfallio
    assert "trascrivi-theme" in js     # salvato/riletto dal toggle
    assert "prefers-color-scheme" in html


def test_every_api_path_used_by_the_frontend_is_routed(js):
    """Ogni endpoint che il frontend chiama deve esistere nell'app FastAPI."""
    # Due sorgenti, perché il frontend chiama in due modi:
    #  1. href letterali verso `/api/...` (i link di download);
    #  2. il wrapper `api("/transcripts")`, che il prefisso lo aggiunge dopo.
    # Guardare solo la prima lasciava scoperte quasi tutte le chiamate.
    #
    # `${...}` fa parte della cattura: fermarsi al `$` troncherebbe
    # `/api/summaries/${id}` a `/api/summaries`, che non è una rotta, e il test
    # segnalerebbe un falso positivo invece di verificare il percorso vero.
    literal = re.findall(r"/api/([a-zA-Z0-9_/{}\-?=&.$]*?)(?=[`'\"\s)]|$)", js)
    wrapped = re.findall(r"\bapi\(\s*[`'\"](/[^`'\"]*)", js)

    paths = {f"/api/{p.rstrip('/')}" for p in literal if p.rstrip("/")}
    paths |= {f"/api{p.rstrip('/')}" for p in wrapped if p.rstrip("/")}
    assert paths, "nessuna chiamata API trovata nel frontend"
    # Il prefisso si ricompone in due modi diversi: uno sbagliato produrrebbe
    # `/api//transcripts`, che non corrisponde a niente.
    assert not [p for p in paths if "//" in p], "percorso API mal composto"

    routes = sorted(p for p in create_app().openapi()["paths"] if p.startswith("/api"))
    assert routes, "l'app non espone rotte /api: il test non sta verificando nulla"

    def segments(path: str) -> list[str]:
        return path.strip("/").split("/")

    def compatible(frontend: str, route: str) -> bool:
        """
        Vero se il percorso del frontend può corrispondere a questa rotta.

        Il confronto è segmento per segmento con `*` come jolly su **entrambi** i
        lati: un `${...}` del frontend è un valore che si conosce solo a runtime,
        e un `{id}` di FastAPI è un segnaposto. Un `accept` letterale nella rotta
        è quindi compatibile con un `*` del frontend — che è il caso reale di
        `/proposals/${status}`, dove `status` vale "accept" o "reject".
        Non si usa una regex: `*` in una regex significa tutt'altro.
        """
        lhs = segments(frontend)
        rhs = segments(re.sub(r"\{[^}]+\}", "*", route))
        if len(lhs) != len(rhs):
            return False
        return all(a == "*" or b == "*" or a == b for a, b in zip(lhs, rhs))

    def frontend_pattern(raw: str) -> str:
        """Percorso del frontend -> pattern: `${...}` (anche troncato) e cifre -> `*`."""
        path = raw.split("?")[0]
        path = re.sub(r"\$\{[^}]*\}?", "*", path)
        return re.sub(r"\d+", "*", path)

    unknown = []
    for raw in sorted(paths):
        path = raw.split("?")[0]
        if path in routes:
            continue
        pattern = frontend_pattern(raw)
        if any(compatible(pattern, route) for route in routes):
            continue
        unknown.append(raw)
    assert unknown == [], f"chiamate a endpoint inesistenti: {unknown}"


def test_view_new_lecture_wires_handlers_without_late_byId_lookups(js):
    """
    Regressione: in `viewNewLecture` gli elementi vengono costruiti da `el(...)`
    **prima** di finire nel documento, quindi un `byId(...)` eseguito in quel
    momento restituisce null. Era il bug del selettore file: il dialogo si apriva
    ma l'evento `change` non era agganciato a nessun listener e la selezione
    veniva persa senza errori visibili (l'optional chaining inghiottiva il null).
    """
    start = js.index("async function viewNewLecture")
    end = js.index("async function viewJobs")
    body = js[start:end]
    # Via le righe di solo commento (incluso quello che cita il bug): qui
    # interessa il codice eseguito. Non si tocca il resto, perché togliere i
    # commenti in linea romperebbe le stringhe che contengono "//".
    code = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("//"))

    # L'input file deve nascere con il suo handler, non con un byId successivo.
    assert 'id: "fileInput"' in code
    assert "onchange:" in code
    assert "fileInput" not in set(re.findall(r'byId\("([^"]+)"\)', code))
    assert "uploadProgress" not in set(re.findall(r'byId\("([^"]+)"\)', code))
    # Una sola sorgente per volta, selezionata dallo switch.
    assert 'updateMode("file")' in code
    assert 'updateMode("path")' in code
    assert "sourceCard" in code and "pathRow" in code
    # La barra di avanzamento dell'upload deve esistere e contenere il riempimento.
    assert "progressBox" in code and "progressFill" in code


def test_frontend_js_has_valid_syntax():
    node = shutil.which("node")
    if not node:
        pytest.skip("node non installato: controllo di sintassi saltato")
    for name in ("app.js", "markdown.js"):
        result = subprocess.run([node, "--check", str(config.WEB / name)],
                                capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_router_and_views_agree_on_argument_order(js):
    """
    Regressione: il router chiama `view(nav, params, ...id, token)`.

    Le viste avevano firme diverse (`viewDashboard(nav, token)` contro
    `viewTranscript(nav, transcriptId, params, token)`) e la rotta radice, che
    non ha id, passava `params` al posto del token: `isCurrent` falliva sempre e
    la dashboard restava sullo scheletro di caricamento, senza errori visibili.
    Qui si blocca l'ordine, non il numero di parametri: un id in più è normale.
    """
    call = re.search(r"await view\(([^;]*?)\);", js, flags=re.S)
    assert call, "il router non chiama più view(...)"
    passed = [p.strip() for p in call.group(1).split(",") if p.strip()]
    # I primi due argomenti arrivano in quest'ordine da render().
    assert passed[0] == "nav"
    assert passed[1] == "params"
    assert passed[-1] == "token", "il token di vista deve essere l'ultimo"

    for name in re.findall(r"async function (view[A-Za-z]+)\(([^)]*)\)", js):
        fn, params = name
        args = [p.strip() for p in params.split(",") if p.strip()]
        assert args[0] == "nav", f"{fn} deve accettare nav per primo"
        assert args[1] == "params", f"{fn} deve accettare params per secondo"
        assert args[-1] == "token", f"{fn} deve accettare token per ultimo"


def _strip_strings_and_comments(code: str) -> str:
    """
    Sostituisce stringhe e commenti con spazi, **conservando** il contenuto dei
    `${...}` dentro i template literal e i fine riga.

    Senza il primo accorgimento il test trova `jobs` dentro `"#/jobs?focus="` o
    `terms` dentro una frase in inglese e segnala bug che non esistono. Senza il
    secondo i numeri di riga riportati nei messaggi sarebbero sbagliati, perché
    togliere le righe di un commento a blocco sposta tutto ciò che segue.
    """
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = code.find("\n", i)
            i = n if j == -1 else j          # il \n resta e viene copiato dal ciclo
            continue
        if ch == "/" and nxt == "*":
            j = code.find("*/", i + 2)
            end = n if j == -1 else j + 2
            out.append("\n" * code.count("\n", i, end))
            i = end
            continue
        if ch in "\"'`":
            quote = ch
            start = i
            i += 1
            while i < n:
                if code[i] == "\\":
                    i += 2
                    continue
                if quote == "`" and code[i:i + 2] == "${":
                    depth, j = 1, i + 2
                    while j < n and depth:
                        if code[j] == "{":
                            depth += 1
                        elif code[j] == "}":
                            depth -= 1
                        j += 1
                    out.append(_strip_strings_and_comments(code[i + 2:j - 1]))
                    i = j
                    continue
                if code[i] == quote:
                    i += 1
                    break
                i += 1
            out.append("\n" * code.count("\n", start, i))
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def test_each_view_fetches_the_data_it_uses(js):
    """
    Regressione: `viewTranscript` usava `settings.default_provider_id`, ma
    `settings` non è una globale — ogni vista se lo deve chiedere con
    `api("/settings")`. Il codice era sintatticamente valido, quindi
    `node --check` passava, e la pagina andava in ReferenceError solo al render.

    Qui si controlla che i nomi-dato usati da una vista siano o dichiarati
    localmente (compreso il destructuring) o parametri.
    """
    watched = ["settings", "providers", "jobs", "terms", "courses", "models", "transcripts"]
    module_decls = set(re.findall(r"^(?:const|let|var|function|async function)\s+([\w$]+)", js, re.M))

    matches = list(re.finditer(r"\nasync function (view\w+)\(([^)]*)\)\s*\{", js))
    assert matches, "nessuna vista trovata: il test non sta verificando nulla"

    problems = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(js)
        tail = js[start:end]
        # Il corpo finisce alla prima dichiarazione a colonna 0.
        cut = re.search(r"\n(?:const|let|var|function|async function|class)\s", tail)
        body = _strip_strings_and_comments(tail[:cut.start()] if cut else tail)

        params = {p.strip() for p in match.group(2).split(",") if p.strip()}
        local = set(re.findall(r"(?:const|let|var)\s+([\w$]+)", body))
        # `const [t, { jobs }, ...] = await ...` e `const { a, b } = ...`
        for pattern in re.findall(r"(?:const|let|var)\s*(\[[^\]]*\]|\{[^}]*\})\s*=", body):
            local |= set(re.findall(r"[\w$]+", pattern))

        for name in watched:
            if name in module_decls or name in local or name in params:
                continue
            # `(?<!\.)` esclude gli accessi a proprietà: `s.terms` non è l'uso
            # di una variabile `terms`.
            if re.search(rf"(?<!\.)\b{name}\b", body):
                problems.append(f"{match.group(1)} usa `{name}` senza procurarselo")

    assert problems == [], "dati usati ma mai richiesti: " + "; ".join(problems)


def _top_level_arguments(code: str, open_paren: int) -> str:
    """
    Testo degli argomenti al **primo** livello di annidamento di una chiamata.

    Serve a distinguere `f(a, cond ? x : null)` — dove il `null` diventa un nodo
    di testo — da `f(g(cond ? x : null))`, dove il `null` è un figlio di `g` e
    `g` lo filtra.
    """
    depth = 0
    out: list[str] = []
    i = open_paren
    n = len(code)
    while i < n:
        ch = code[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1:
            out.append(ch)
        i += 1
    return "".join(out)


def test_replace_children_is_not_given_null_directly(js):
    """
    Regressione: `replaceChildren(a, cond ? node : null)` mette a schermo la
    scritta "null" — `replaceChildren` converte i figli non-Node in testo, e
    `null` diventa la stringa "null". `el(...)` filtra, `replaceChildren` no.

    Il bug si è visto solo a pagina aperta, nella barra dei riassunti:
    "Generate summary Refresh nullnullnull". Per i figli condizionali si usa
    `setChildren`, che ha la stessa semantica di `el`.
    """
    clean = _strip_strings_and_comments(js)
    offenders = []
    for match in re.finditer(r"\.replaceChildren\s*\(", clean):
        open_paren = clean.index("(", match.start())
        args = _top_level_arguments(clean, open_paren)
        if re.search(r"\bnull\b|\bundefined\b", args):
            line = clean[:match.start()].count("\n") + 1
            offenders.append(f"riga ~{line}")

    assert offenders == [], (
        "replaceChildren riceve un figlio nullo (usa setChildren): " + ", ".join(offenders))


def test_the_frontend_assets_are_actually_served(client):
    """
    Un file citato in index.html ma non servito è un 404 silenzioso: il browser
    non carica `renderMarkdown`, la tab del riassunto resta vuota e nella console
    c'è un errore che nessun test di sintassi avrebbe visto.
    """
    for path in ("/app.js", "/markdown.js", "/style.css"):
        res = client.get(path)
        assert res.status_code == 200, f"{path} -> {res.status_code}"

    assert "renderMarkdown" in client.get("/markdown.js").text


def _function_body(js: str, name: str) -> str:
    """Testo di `function name` (o `async function name`) fino alla dichiarazione a colonna 0."""
    match = re.search(rf"\n\s*(?:async\s+)?function {re.escape(name)}\s*\(", js)
    assert match, f"funzione {name} non trovata"
    tail = js[match.start():]
    cut = re.search(r"\n(?:const|let|var|function|async function|class)\s", tail[1:])
    return tail[:cut.start() + 1] if cut else tail


def test_the_transcript_view_polls_running_jobs_and_stops_cleanly(js):
    """
    Regressione: `viewTranscript` calcolava il lavoro in corso una volta sola al
    render, quindi dopo aver lanciato un riassunto la tab continuava a dire "No
    summary yet" finché non si ricaricava la pagina.

    Qui si bloccano le tre proprietà del polling: parte, si spegne quando non c'è
    più niente in corso e si spegne anche quando si cambia vista — senza
    quest'ultima resterebbe un intervallo vivo su ogni pagina.
    """
    view = _function_body(js, "viewTranscript")

    assert re.search(r"let summaryJob\s*=", view), "lo stato del job deve poter cambiare"
    assert "renderMarkdown(s.markdown" in view or "renderMarkdown(s.markdown" in js
    assert "startJobPoll" in view and "setInterval(jobTick" in view
    assert "onViewTeardown(stopJobPoll)" in view, "il polling deve spegnersi cambiando vista"

    tick = _function_body(js, "jobTick")
    assert "isCurrent(token)" in tick, "un tick di una vista abbandonata non deve scrivere nel DOM"
    assert "stopJobPoll()" in tick, "il polling deve spegnersi quando non c'è più niente in corso"
    assert "loadSummaries()" in tick, "il documento pronto deve comparire da solo"


def test_stylesheet_defines_both_themes():
    css = (config.WEB / "style.css").read_text(encoding="utf-8")
    assert ":root" in css
    assert ':root[data-theme="dark"]' in css
    assert "color-scheme" in css
