/* ============================================================================
   Trascrivi — SPA arancione-free: solo DOM, fetch e hash routing.
   Nessuna dipendenza, nessun build step: il server serve questi file così come
   sono. Il routing è a hash perché un `history.pushState` avrebbe richiesto un
   fallback lato server per ogni rotta.
   ========================================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* I modelli sono definiti dal backend (trascrivi_audio.MODELS): la UI non li
   duplica a mano, li carica da `/api/models`. La promessa resta in memoria: se
   la prima chiamata fallisce — succede quando la pagina si carica mentre il
   server sta ancora partendo — la vista che ne ha bisogno la ritenta, invece di
   disegnare un menu vuoto che manda il job con `model=""`. */
const MODELS = [];
/* Cosa sanno fare i modelli, sempre dal backend: `ENGLISH_ONLY` sono quelli che
   il motore rifiuta con una lingua diversa dall'inglese, `LANGUAGES` i codici
   ISO che accetta. La UI non li elenca a mano. */
const ENGLISH_ONLY = new Set();
const LANGUAGES = [];
let modelsPromise = null;

function loadModels() {
  if (!modelsPromise) {
    modelsPromise = api("/models").then((res) => {
      MODELS.splice(0, MODELS.length, ...(Array.isArray(res?.models) ? res.models : []));
      ENGLISH_ONLY.clear();
      (Array.isArray(res?.english_only) ? res.english_only : []).forEach((m) => ENGLISH_ONLY.add(m));
      LANGUAGES.splice(0, LANGUAGES.length,
        ...(Array.isArray(res?.languages) && res.languages.length ? res.languages : ["en", "it"]));
      return MODELS;
    });
    // Il rifiuto va marcato come gestito (altrimenti è un unhandled rejection) e
    // poi dimenticato: il prossimo chiamante deve ritentare, non ricevere di
    // nuovo lo stesso errore.
    modelsPromise.catch(() => { modelsPromise = null; });
  }
  return modelsPromise;
}

/* ── API ─────────────────────────────────────────────────────────────────── */
async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body !== undefined && !(opts.body instanceof FormData)) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(`/api${path}`, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
  }
  return data;
}

/* ── Errori leggibili ─────────────────────────────────────────────────────────
   Il messaggio tecnico resta (tra parentesi, per chi deve capire cosa è
   successo), ma prima viene la frase che dice cosa fare. */
const ERROR_HINTS = [
  [/ollama|11434/i, "error.ollama"],
  [/cannot be decrypted|secret\.key/i, "error.key_unreadable"],
  [/api key|401|unauthori[sz]ed|authentication/i, "error.key_invalid"],
  [/402|insufficient (balance|credit)|quota/i, "error.no_credit"],
  [/connection error|timed? ?out|ECONNREFUSED|network/i, "error.network"],
  [/no provider|provider not configured|configure an llm provider/i, "error.no_provider"],
  [/produced no text|transcript is empty/i, "error.no_text"],
  [/no audio track/i, "error.no_audio"],
  [/out of memory|CUDA error|cublas/i, "error.gpu"],
  [/no lecture of this course has a summary|has no summary/i, "error.need_summary"],
];

function humanError(message) {
  const text = String(message || "");
  const hit = ERROR_HINTS.find(([re]) => re.test(text));
  return hit ? `${t(hit[1])} (${text.slice(0, 160)})` : text;
}

/* ── Fine coda: notifica del sistema ─────────────────────────────────────────
   Con il caricamento in blocco nessuno guarda la pagina Jobs per un'ora: quando
   la coda si svuota arriva una notifica (se il permesso c'è) e un toast. */
let lastActiveJobs = null;
let lastNotifyAt = Date.now() / 1000;
function askNotifyPermission() {
  try {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  } catch { /* browser senza notifiche */ }
}
function notifyQueueDone(failed) {
  const body = failed ? t("notify.done_failed", { count: failed }) : t("notify.done_body");
  toast(`${t("notify.done_title")} — ${body}`, failed ? "err" : "ok");
  try {
    if ("Notification" in window && Notification.permission === "granted") {
      new Notification(t("notify.done_title"), { body });
    }
  } catch { /* ignora */ }
}

/* ── Toast ───────────────────────────────────────────────────────────────── */
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = kind === "err" ? humanError(message) : message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 8000 : 4000);
}

/* ── Dialoghi ────────────────────────────────────────────────────────────── */
function confirmDialog(title, text, okLabel = t("common.delete")) {
  return new Promise((resolve) => {
    const dlg = $("#confirmDialog");
    $("#confirmTitle").textContent = title;
    $("#confirmText").textContent = text;
    $("#confirmOk").textContent = okLabel;
    dlg.addEventListener("close", function once() {
      dlg.removeEventListener("close", once);
      resolve(dlg.returnValue === "ok");
    });
    dlg.showModal();
  });
}

/* Rinomina senza `window.prompt`: il prompt di sistema non ha stile, non si
   può annullare con un pulsante vero e su alcuni browser viene bloccato del
   tutto. Restituisce il nuovo valore, oppure null se si annulla. */
function promptDialog({ title, label, value = "", okLabel = t("common.save"), placeholder = "" }) {
  return new Promise((resolve) => {
    const dlg = byId("promptDialog");
    const input = byId("promptInput");
    const error = byId("promptError");
    byId("promptTitle").textContent = title;
    byId("promptLabel").textContent = label;
    byId("promptOk").textContent = okLabel;
    input.value = value;
    input.placeholder = placeholder;
    error.classList.add("hidden");
    error.textContent = "";
    input.removeAttribute("aria-invalid");

    input.addEventListener("input", function () {
      error.classList.add("hidden");
      input.removeAttribute("aria-invalid");
    }, { once: true });

    dlg.addEventListener("close", function once() {
      dlg.removeEventListener("close", once);
      if (dlg.returnValue !== "ok") return resolve(null);
      const next = input.value.trim();
      if (!next) {
        // Il dialogo si è chiuso ma il valore non è utilizzabile: chi ha
        // chiamato deve poterlo dire invece di salvare una stringa vuota.
        return resolve("");
      }
      resolve(next);
    });

    dlg.showModal();
    input.focus();
    input.select();
  });
}

/* ── Formattazione ───────────────────────────────────────────────────────── */
const fmtTs = (secs) => {
  secs = Math.max(0, Math.floor(secs || 0));
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
};

const fmtBytes = (n) => {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
};

const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString(I18N.lang, {
  day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
}) : "—";

/* Costi in dollari: sotto il dollaro servono quattro decimali, altrimenti una
   correzione da $0.003 si legge "$0.00" — cioè zero, che è un'altra cosa.
   `null` non è zero: è un costo che nessuno ha registrato. */
const fmtUsd = (n) => (n === null || n === undefined) ? "—"
  : `$${Number(n).toFixed(Math.abs(n) < 1 ? 4 : 2)}`;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

/* Il colore arriva dal database e finisce in uno stile inline: se un giorno
   contenesse `;` o `}` uscirebbe dalla proprietà che stiamo impostando. Accet-
   tiamo solo le forme che scriviamo noi (#rgb, #rrggbb, #rrggbbaa) e per tutto
   il resto ricadiamo sull'accento. */
const ACCENT_HEX = "#ff4a17";
const safeColor = (value) => {
  const v = String(value ?? "").trim();
  return /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(v) ? v : ACCENT_HEX;
};

const byId = (id) => document.getElementById(id);
const appendChild = (parent, child) => {
  if (child === null || child === undefined || child === false) return;
  parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
};

const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== false && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children.flat()) appendChild(node, child);
  return node;
};

/* Sostituisce i figli di `parent` con la stessa semantica di `el(...)`: i figli
   nulli si saltano. `replaceChildren` invece NON filtra — un `cond ? node : null`
   diventa un nodo di testo "null" a schermo, e l'errore si vede solo a pagina
   aperta (era la barra dei riassunti: "Generate summary Refresh nullnullnull"). */
const setChildren = (parent, ...children) => {
  parent.replaceChildren();
  for (const child of children.flat()) appendChild(parent, child);
};

/* ── Tema ────────────────────────────────────────────────────────────────── */
/* Icone disegnate a mano (stroke da 1.4px): l'emoji non ha né peso né colore,
   e in un header hairline si vede subito che è un innesto posticcio. */
const svgWrap = (body) => `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor"
  stroke-width="1.4" stroke-linecap="round" aria-hidden="true">${body}</svg>`;
const ICON_SUN = svgWrap(`<circle cx="10" cy="10" r="3.4"></circle>
  <path d="M10 2.2v2.1M10 15.7v2.1M2.2 10h2.1M15.7 10h2.1M4.4 4.4l1.5 1.5M14.1 14.1l1.5 1.5M15.6 4.4l-1.5 1.5M5.9 14.1l-1.5 1.5"></path>`);
const ICON_MOON = svgWrap(`<path d="M15.6 12.3A6.2 6.2 0 0 1 7.7 4.4a6.6 6.6 0 1 0 7.9 7.9Z"></path>`);
const ICON_AUTO = svgWrap(`<circle cx="10" cy="10" r="6.6"></circle>
  <path d="M10 3.4v13.2"></path><path d="M10 3.4a6.6 6.6 0 0 1 0 13.2Z" fill="currentColor" stroke="none"></path>`);

const THEME_CHOICES = [
  // Etichette risolte al paint (t() non è pronto al caricamento del modulo):
  // theme.system, theme.light, theme.dark.
  { id: "auto", labelKey: "theme.system", icon: ICON_AUTO },
  { id: "light", labelKey: "theme.light", icon: ICON_SUN },
  { id: "dark", labelKey: "theme.dark", icon: ICON_MOON },
];

const systemDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;

function initTheme() {
  const menu = byId("themeMenu");
  const trigger = byId("themeToggle");
  const list = byId("themeList");
  const iconBox = byId("themeIcon");
  const nameBox = byId("themeName");

  // La preferenza salvata è una fra auto/light/dark. Un valore vecchio o
  // corrotto non deve rompere il tema: ricadiamo su "auto".
  const saved = localStorage.getItem("trascrivi-theme");
  let choice = THEME_CHOICES.some((c) => c.id === saved) ? saved : "auto";

  const media = window.matchMedia("(prefers-color-scheme: dark)");

  const paint = () => {
    const resolved = choice === "auto" ? (systemDark() ? "dark" : "light") : choice;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themeChoice = choice;

    // Il colore della barra del browser segue il tema, altrimenti su mobile
    // resta una striscia scura sopra una pagina chiara.
    const meta = byId("metaThemeColor");
    if (meta) meta.setAttribute("content", resolved === "dark" ? "#0a0b0d" : "#f0ece4");

    // Icona e nome seguono la scelta, non il tema risolto: con "sistema" devo
    // vedere che sto seguendo il sistema anche quando il risultato è scuro.
    const active = THEME_CHOICES.find((c) => c.id === choice);
    const activeLabel = t(active.labelKey);
    iconBox.innerHTML = active.icon;
    nameBox.textContent = activeLabel;

    THEME_CHOICES.forEach((c) => {
      const btn = $(`[data-theme-choice="${c.id}"]`, menu);
      if (!btn) return;
      const on = c.id === choice;
      btn.setAttribute("aria-current", on ? "true" : "false");
      const ico = $(".theme-ico", btn);
      if (ico) ico.innerHTML = c.icon;
      const hint = $(".theme-hint", btn);
      if (hint) hint.textContent = on ? t("theme.on") : "";
    });

    trigger.setAttribute("aria-label", t("theme.aria", { name: activeLabel }));
    trigger.title = t("theme.title", { name: activeLabel });
  };

  const close = () => {
    list.hidden = true;
    menu.dataset.open = "false";
    trigger.setAttribute("aria-expanded", "false");
  };
  const open = () => {
    list.hidden = false;
    menu.dataset.open = "true";
    trigger.setAttribute("aria-expanded", "true");
    const first = $('[aria-current="true"]', list) || $(".theme-option", list);
    first?.focus();
  };

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    list.hidden ? open() : close();
  });

  THEME_CHOICES.forEach((c) => {
    const btn = $(`[data-theme-choice="${c.id}"]`, menu);
    if (!btn) return;
    btn.addEventListener("click", () => {
      choice = c.id;
      localStorage.setItem("trascrivi-theme", choice);
      paint();
      close();
      trigger.focus();
    });
  });

  // Click fuori o Esc: il menu non deve restare aperto sopra la pagina.
  document.addEventListener("click", (e) => {
    if (!list.hidden && !menu.contains(e.target)) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !list.hidden) { close(); trigger.focus(); }
  });

  // Se la scelta è "sistema", un cambio di preferenza del sistema si applica
  // subito, senza ricaricare la pagina.
  const onSystemChange = () => { if (choice === "auto") paint(); };
  if (media.addEventListener) media.addEventListener("change", onSystemChange);
  else if (media.addListener) media.addListener(onSystemChange);

  paint();
}

/* ── Scheletro di caricamento ────────────────────────────────────────────── */
/* Ogni vista nasce da una fetch: mentre la risposta viaggia il contenitore
   mostra la forma del contenuto che sta arrivando, non un "Loading…" nudo. */
function viewSkeleton(rows = 3) {
  const bar = (w) => el("span", { class: "sk", style: `--w:${w}` });
  return el("div", { class: "stack", role: "status", "aria-label": t("common.loading_content") },
    el("div", { class: "sk-head" }, bar("38%"), bar("12%")),
    el("div", { class: "grid" },
      Array.from({ length: rows }, () =>
        el("div", { class: "sk-card" }, bar("46%"), bar("100%"), bar("68%")))));
}

/* ── Router ──────────────────────────────────────────────────────────────── */
const routes = [
  [/^\/$/, viewDashboard],
  [/^\/p\/(\d+)\/?$/, viewProject],
  [/^\/p\/(\d+)\/new\/?$/, viewNewLecture],
  [/^\/jobs\/?$/, viewJobs],
  [/^\/t\/(\d+)\/?$/, viewTranscript],
  [/^\/costs\/?$/, viewCosts],
  [/^\/settings\/?$/, viewSettings],
  [/^\/about\/?$/, viewAbout],
];

/* Titolo del documento per rotta: una scheda fra dieci identiche non dice
   niente. `setDocTitle` accetta anche il contenuto vero (nome del corso,
   titolo della trascrizione) e lo accoda dopo il caricamento.
   Sono chiavi di traduzione, risolte in setRouteTitle. */
const ROUTE_TITLES = [
  [/^\/$/, "nav.library"],
  [/^\/p\/\d+\/new\/?$/, "route.new_lecture"],
  [/^\/p\/\d+\/?$/, "route.course"],
  [/^\/jobs\/?$/, "nav.jobs"],
  [/^\/t\/\d+\/?$/, "route.transcript"],
  [/^\/costs\/?$/, "nav.costs"],
  [/^\/settings\/?$/, "nav.settings"],
  [/^\/about\/?$/, "foot.about"],
];

const APP_TITLE = "Trascrivi";

function setDocTitle(part) {
  document.title = part ? `${part} · ${APP_TITLE}` : t("route.home", { app: APP_TITLE });
}

function setRouteTitle(path, part) {
  const hit = ROUTE_TITLES.find(([re]) => re.test(path));
  setDocTitle(part || t(hit ? hit[1] : "route.not_found"));
}

let currentPoll = null;
let detachView = null;
/* Generazione della vista corrente. Ogni render la incrementa: una vista che
   finisce di caricare dopo che si è già navigato altrove non deve più scrivere
   nel DOM. Senza questo, il polling di Jobs ridisegnava la coda sopra la pagina
   appena aperta (e lo stesso vale per ogni fetch lenta). */
let viewToken = 0;

function stopPoll() {
  if (currentPoll) { clearInterval(currentPoll); currentPoll = null; }
}

/* Ogni vista può registrare qui i listener che vive fuori dal proprio DOM
   (document, window). Senza questo, ogni navigazione aggiungeva un listener
   `keydown` in più su document: dopo dieci trascrizioni il tasto "s" con Ctrl
   faceva dieci salvataggi. */
function onViewTeardown(fn) {
  const previous = detachView;
  detachView = () => { previous?.(); fn(); };
}

/* Una richiesta è ancora quella della vista a schermo? */
function isCurrent(token) {
  return token === viewToken;
}

async function render() {
  stopPoll();
  detachView?.();
  detachView = null;
  const token = ++viewToken;
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  const nav = byId("view");
  for (const [re, view] of routes) {
    const m = path.match(re);
    if (m) {
      setRouteTitle(path);
      nav.setAttribute("aria-busy", "true");
      nav.replaceChildren(viewSkeleton());
      try {
        // Ordine degli argomenti uniforme: (nav, params, ...id, token).
        // `params` viene prima degli id perché la rotta radice non ha id.
        await view(nav, params, ...m.slice(1).map(Number), token);
      } catch (err) {
        console.error(err);
        // Un errore arrivato da una vista che non è più a schermo non va
        // mostrato: sostituirebbe la pagina su cui l'utente sta lavorando.
        if (isCurrent(token)) nav.replaceChildren(errorPanel(err));
      } finally {
        if (isCurrent(token)) nav.removeAttribute("aria-busy");
      }
      return;
    }
  }
  setDocTitle(t("route.not_found"));
  nav.replaceChildren(notFoundView(path));
}

/* ── Badge dei job in corso ──────────────────────────────────────────────── */
async function refreshJobBadge() {
  try {
    const { jobs } = await api("/jobs?status=running,queued&limit=20");
    const wasActive = lastActiveJobs;
    // Si aggiorna PRIMA del secondo await: due giri sovrapposti (pagina Job e
    // timer globale) non devono notificare due volte.
    lastActiveJobs = jobs.length;
    if (wasActive && !jobs.length) {
      // Contano solo i fallimenti dopo l'ultima notifica, non quelli già detti.
      const since = lastNotifyAt;
      lastNotifyAt = Date.now() / 1000;
      const failed = (await api("/jobs?limit=40")).jobs
        .filter((j) => j.status === "failed" && j.finished_at && j.finished_at > since).length;
      notifyQueueDone(failed);
    }
    const badge = byId("navJobs");
    if (jobs.length) {
      badge.textContent = String(jobs.length);
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  } catch { /* il badge non è critico */ }
}

/* ── Icone ───────────────────────────────────────────────────────────────── */
/* Un solo set, tratti da 1.4px come i glifi dell'header: mescolare spessori
   diversi è il modo più veloce per far sembrare sciatta un'interfaccia. */
const ICONS = {
  back: `<path d="M9.5 4 5 8.5l4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"></path>`,
  chevron: `<path d="M3.5 6 7.5 10l4-4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"></path>`,
  alert: `<path d="M8 2.6 14.4 13H1.6L8 2.6Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"></path><path d="M8 6.6v3.1" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"></path><circle cx="8" cy="11.4" r="0.85" fill="currentColor"></circle>`,
  check: `<path d="M3.2 8.4 6.4 11.6 12.8 4.8" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>`,
  warn: `<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.4"></circle><path d="M8 4.9v3.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"></path><circle cx="8" cy="10.9" r="0.85" fill="currentColor"></circle>`,
  info: `<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.4"></circle><path d="M8 7.4v3.9" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"></path><circle cx="8" cy="4.9" r="0.85" fill="currentColor"></circle>`,
  copy: `<rect x="5.6" y="5.6" width="7.4" height="7.4" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.4"></rect><path d="M10.4 3H4.2A1.2 1.2 0 0 0 3 4.2v6.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"></path>`,
  download: `<path d="M8 2.6v7.1" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"></path><path d="M4.9 6.8 8 9.9l3.1-3.1" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"></path><path d="M3.1 12.6h9.8" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"></path>`,
};

const icon = (name, cls = "") => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  /* Dimensioni proprie, non solo il viewBox: un SVG senza `width`/`height` cade
     sul default 300x150 e dove nessuna regola CSS lo ridimensiona diventa una
     scatola enorme con l'icona invisibile. Le regole per posizione (`.fs-caret`,
     `.notice svg`, …) vincono comunque su questi attributi: qui c'è solo il
     default per chi non ne ha una. */
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  svg.setAttribute("aria-hidden", "true");
  if (cls) svg.setAttribute("class", cls);
  svg.innerHTML = ICONS[name] || "";
  return svg;
};

/* Avviso in linea: sostituisce window.alert e i messaggi rossi buttati lì. */
const notice = (kind, text, node = null) => el("div", { class: `notice ${kind}` },
  icon(kind === "ok" ? "check" : kind === "err" ? "alert" : kind === "warn" ? "warn" : "info"),
  el("div", {}, text, node));

/* ── Pagine di cortesia ──────────────────────────────────────────────────── */
function notFoundView(path) {
  return el("div", { class: "stack" },
    el("div", { class: "card nf" },
      el("p", { class: "empty-kicker" }, t("notfound.kicker")),
      el("h1", {}, t("notfound.title")),
      el("p", { class: "muted prose" }, t("notfound.body")),
      el("p", {}, el("span", { class: "nf-path" }, `#${path}`)),
      el("div", { class: "nf-links" },
        el("a", { href: "#/" }, el("span", { class: "nf-k" }, t("nav.library")), t("notfound.library_hint")),
        el("a", { href: "#/jobs" }, el("span", { class: "nf-k" }, t("nav.jobs")), t("notfound.jobs_hint")),
        el("a", { href: "#/settings" }, el("span", { class: "nf-k" }, t("nav.settings")), t("notfound.settings_hint")))));
}

function errorPanel(err) {
  return el("div", { class: "card" },
    el("h2", {}, t("errpanel.title")),
    el("p", { class: "muted prose" }, err.message),
    el("p", { class: "hint" }, t("errpanel.hint")),
    el("div", { class: "split", style: "margin-top:12px" },
      el("button", { class: "primary", onclick: render }, t("errpanel.retry")),
      el("a", { class: "btn", href: "#/jobs" }, t("errpanel.open_jobs"))));
}


/* Menù a comparsa riutilizzabile. Tre bottoni di download affiancati sono
   rumore: un solo comando che apre la lista tiene pulita la riga del titolo. */
function popupMenu(label, items, { align = "right" } = {}) {
  const list = el("div", { class: "menu-list", role: "menu", hidden: "hidden" });
  const wrap = el("div", { class: "menu", "data-open": "false" });

  const close = () => {
    list.hidden = true;
    wrap.dataset.open = "false";
    trigger.setAttribute("aria-expanded", "false");
  };

  const trigger = el("button", {
    type: "button", class: "menu-trigger", "aria-haspopup": "true", "aria-expanded": "false",
    onclick: (e) => {
      e.stopPropagation();
      const opening = list.hidden;
      // Un solo menù aperto per volta: niente liste sovrapposte.
      $$(".menu[data-open='true'], .theme-menu[data-open='true']").forEach((m) => {
        if (m !== wrap) $("button", m)?.click();
      });
      list.hidden = !opening;
      wrap.dataset.open = String(opening);
      trigger.setAttribute("aria-expanded", String(opening));
      if (opening) $("[role=menuitem]", list)?.focus();
    },
  }, label, icon("chevron"));

  items.forEach((item) => {
    if (item.sep) return list.append(el("div", { class: "menu-sep" }));
    if (item.href) {
      list.append(el("a", { href: item.href, role: "menuitem", onclick: close },
        item.label, item.meta ? el("span", { class: "menu-k" }, item.meta) : null));
    } else {
      list.append(el("button", {
        type: "button", role: "menuitem", disabled: item.disabled ? "disabled" : null,
        onclick: () => { close(); item.onclick?.(); },
      }, item.label, item.meta ? el("span", { class: "menu-k" }, item.meta) : null));
    }
  });

  wrap.append(trigger, list);
  if (align === "left") { list.style.left = "0"; list.style.right = "auto"; }

  const onDocClick = (e) => {
    if (!list.hidden && !wrap.contains(e.target)) close();
  };
  document.addEventListener("click", onDocClick);
  // Un menù per ogni trascrizione aperta: se non ci si registra qui, ogni
  // navigazione lascia un listener su document.
  onViewTeardown(() => document.removeEventListener("click", onDocClick));
  wrap.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { close(); trigger.focus(); }
  });
  return wrap;
}

/* ── Menu con bandiere (modello e lingua) ────────────────────────────────── */
/* `<select>` non può contenere immagini — le sue voci sono solo testo — e su
   Windows le emoji-bandiera non esistono (🇮🇹 si legge "IT"): per mostrare
   davvero le bandiere il menu è fatto in casa. Stesso schema di `popupMenu`:
   trigger + lista, un solo menu aperto per volta, chiusura al click fuori, Esc
   e frecce. Il valore si legge con `.value`, come da un `<select>`. */
const FLAGS = {
  it: `<rect width="21" height="15" fill="#fff"></rect>
       <rect width="7" height="15" fill="#009246"></rect>
       <rect x="14" width="7" height="15" fill="#ce2b37"></rect>`,
  /* Metà bandiera britannica e metà americana, ognuna compressa in mezza
     larghezza: è l'inglese dei due paesi che lo parlano. */
  en: `<rect width="10.5" height="15" fill="#012169"></rect>
       <path d="M0 0 10.5 15M10.5 0 0 15" stroke="#fff" stroke-width="3"></path>
       <path d="M0 0 10.5 15M10.5 0 0 15" stroke="#c8102e" stroke-width="1.5"></path>
       <path d="M5.25 0v15M0 7.5h10.5" stroke="#fff" stroke-width="4.6"></path>
       <path d="M5.25 0v15M0 7.5h10.5" stroke="#c8102e" stroke-width="2.4"></path>
       <rect x="10.5" width="10.5" height="15" fill="#fff"></rect>
       <g fill="#b22234">
         <rect x="10.5" y="0" width="10.5" height="1.16"></rect>
         <rect x="10.5" y="2.31" width="10.5" height="1.16"></rect>
         <rect x="10.5" y="4.62" width="10.5" height="1.16"></rect>
         <rect x="10.5" y="6.92" width="10.5" height="1.16"></rect>
         <rect x="10.5" y="9.23" width="10.5" height="1.16"></rect>
         <rect x="10.5" y="11.54" width="10.5" height="1.16"></rect>
         <rect x="10.5" y="13.85" width="10.5" height="1.16"></rect>
       </g>
       <rect x="10.5" width="5.2" height="8.1" fill="#3c3b6e"></rect>
       <rect x="10.2" width="0.6" height="15" fill="#fff" opacity="0.8"></rect>`,
  /* Per "un centinaio di lingue" non esiste una bandiera: il mappamondo. */
  globe: `<circle cx="10.5" cy="7.5" r="6.1" fill="none" stroke="currentColor" stroke-width="1.3"></circle>
          <ellipse cx="10.5" cy="7.5" rx="2.7" ry="6.1" fill="none" stroke="currentColor" stroke-width="1.05"></ellipse>
          <path d="M4.6 5.5h11.8M4.6 9.5h11.8" fill="none" stroke="currentColor" stroke-width="1.05"></path>`,
};

const flagIcon = (kind) => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 21 15");
  svg.setAttribute("class", "fs-flag");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = FLAGS[kind] || FLAGS.globe;
  return svg;
};

function flagSelect({ id, options, value, invalid = false }) {
  let current = value || (options[0] ? options[0].value : "");
  // Un valore fuori lista (una lingua scritta a mano in passato) non deve
  // sparire: si aggiunge in cima con il suo codice.
  if (current && !options.some((o) => o.value === current)) {
    options = [{ value: current, label: String(current).toUpperCase() }, ...options];
  }

  const labelOf = (opt) => el("span", { class: "fs-label" }, opt.label);
  const iconsOf = (opt) => el("span", { class: "fs-icons" },
    (opt.flags || []).map(flagIcon),
    opt.chip ? el("span", { class: "fs-chip" }, opt.chip) : null);

  const trigger = el("button", {
    type: "button", id, class: "fs-trigger", "aria-haspopup": "true",
    "aria-expanded": "false", "aria-invalid": invalid ? "true" : null,
  });
  const list = el("div", { class: "fs-menu", role: "menu", hidden: "hidden" });
  const wrap = el("div", { class: "fs", "data-open": "false" }, trigger, list);
  const rows = new Map();

  const paint = () => {
    const opt = options.find((o) => o.value === current) || options[0];
    setChildren(trigger, labelOf(opt), iconsOf(opt), icon("chevron", "fs-caret"));
    if (opt.hint) trigger.setAttribute("title", opt.hint);
    else trigger.removeAttribute("title");
  };

  const close = () => {
    list.hidden = true;
    wrap.dataset.open = "false";
    trigger.setAttribute("aria-expanded", "false");
  };

  const choose = (next) => {
    current = next;
    rows.forEach((btn, key) => btn.setAttribute("aria-checked", String(key === current)));
    paint();
    close();
    trigger.focus();
  };

  options.forEach((opt) => {
    if (opt.group) { list.append(el("div", { class: "fs-group" }, opt.group)); return; }
    const btn = el("button", {
      type: "button", role: "menuitemradio", class: "fs-item", title: opt.hint || null,
      "aria-checked": String(opt.value === current),
      onclick: () => choose(opt.value),
    }, labelOf(opt), iconsOf(opt));
    rows.set(opt.value, btn);
    list.append(btn);
  });

  const open = (step) => {
    // Un solo menu aperto per volta, come per `popupMenu`.
    $$(".fs[data-open='true'], .menu[data-open='true'], .theme-menu[data-open='true']")
      .forEach((other) => { if (other !== wrap) $("button", other)?.click(); });
    list.hidden = false;
    wrap.dataset.open = "true";
    trigger.setAttribute("aria-expanded", "true");
    const buttons = [...rows.values()];
    if (!buttons.length) return;
    const checked = rows.get(current);
    (step < 0 && !checked ? buttons[buttons.length - 1] : checked || buttons[0]).focus();
  };

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    if (list.hidden) open(1); else close();
  });

  wrap.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { close(); trigger.focus(); return; }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const buttons = [...rows.values()];
    if (!buttons.length) return;
    if (list.hidden) { open(e.key === "ArrowDown" ? 1 : -1); return; }
    const step = e.key === "ArrowDown" ? 1 : -1;
    const at = buttons.indexOf(document.activeElement);
    buttons[(at + step + buttons.length) % buttons.length].focus();
  });

  const onDocClick = (e) => { if (!list.hidden && !wrap.contains(e.target)) close(); };
  document.addEventListener("click", onDocClick);
  // Un listener su document per ogni menu: senza registrarsi qui, ogni
  // navigazione ne lascia uno dietro.
  onViewTeardown(() => document.removeEventListener("click", onDocClick));

  paint();
  return {
    el: wrap,
    get value() { return current; },
    focus: () => trigger.focus(),
  };
}

/* Le lingue di un modello in una riga: solo-inglese → mezza bandiera
   britannica/americana; multilingue → mappamondo, Italia e Inghilterra (tutti i
   modelli conoscono l'inglese). I fatti arrivano da `/api/models`. */
function modelOptions(names, englishOnly) {
  const onlyEn = new Set(englishOnly || []);
  return names.map((name) => (onlyEn.has(name)
    ? { value: name, label: name, flags: ["en"], hint: t("model.hint_en", { name }) }
    : { value: name, label: name, flags: ["globe", "it", "en"],
        hint: t("model.hint_multi", { name }) }));
}

/* Il menu della lingua: i tre casi di tutti i giorni con la bandiera, poi tutti
   i codici che il motore accetta — una lingua non deve sparire solo perché non
   ha una bandiera disegnata. */
function languageOptions(codes) {
  const rest = (codes && codes.length ? codes : ["en", "it"])
    .filter((code) => code !== "en" && code !== "it");
  return [
    { value: "auto", label: t("lang.auto"), flags: ["globe"], hint: t("lang.auto_hint") },
    { value: "en", label: "English", flags: ["en"], hint: "English" },
    { value: "it", label: "Italiano", flags: ["it"], hint: "Italiano" },
    { group: t("lang.all_group") },
    ...rest.map((code) => ({ value: code, label: code.toUpperCase() })),
  ];
}

/* Etichette come chiavi, risolte quando il menu si costruisce:
   export.txt, export.md, export.json, export.segments. */
const EXPORTS = [
  { format: "txt", labelKey: "export.txt", meta: ".txt" },
  { format: "md", labelKey: "export.md", meta: ".md" },
  { format: "json", labelKey: "export.json", meta: ".json" },
  { format: "segments", labelKey: "export.segments", meta: ".txt" },
];

const exportMenu = (tr) => popupMenu(t("export.menu"), [
  ...EXPORTS.map((e) => ({
    label: t(e.labelKey), meta: e.meta,
    href: `/api/transcripts/${tr.id}/export?format=${e.format}`,
  })),
  { sep: true },
  {
    label: t("export.copy"), meta: t("export.clipboard"),
    onclick: async () => {
      try { await navigator.clipboard.writeText(tr.text || ""); toast(t("export.copied"), "ok"); }
      catch { toast(t("common.clipboard_unavailable"), "err"); }
    },
  },
]);


/* ============================================================================
   Dump
   ========================================================================== */
/* Scegli corso e lezioni, poi uno zip: `<Corso>/<Lezione>/transcript.md` e
   `summary.md`. Il file lo compone il server — è l'unico posto che sa come si
   chiama un corso e come si evita che due lezioni con lo stesso titolo si
   sovrascrivano — e la risposta si scarica come blob, perché un POST non passa
   da un `<a href>`. */
async function openDumpDialog() {
  let projects, transcripts;
  try {
    [{ projects }, { transcripts }] = await Promise.all([api("/projects"), api("/transcripts")]);
  } catch (err) { return toast(err.message, "err"); }
  if (!transcripts.length) return toast(t("dump.empty"), "err");

  const items = transcripts.map((t) => {
    const hasSummary = (t.n_summaries || 0) > 0;
    // Lo stesso nome della tabella del corso, con il numero: il nome del file non dice niente.
    return { id: t.id, project_id: t.project_id, hasSummary,
             title: `${t.lecture_no ? `L${t.lecture_no} · ` : ""}${t.display_title || t.title}`,
             no: t.lecture_no || 0, text: true, summary: hasSummary, textEl: null, sumEl: null };
  }).sort((a, b) => a.no - b.no);
  const byCourse = new Map();
  items.forEach((it) => {
    if (!byCourse.has(it.project_id)) byCourse.set(it.project_id, []);
    byCourse.get(it.project_id).push(it);
  });

  const lessonRow = (it) => el("label", { class: "dump-lesson" },
    el("span", { class: "dump-title" }, it.title),
    el("span", { class: "dump-pick" },
      it.textEl = el("input", { type: "checkbox",
        onchange: (e) => { it.text = e.target.checked; sync(); } }),
      t("dump.transcript")),
    el("span", { class: `dump-pick${it.hasSummary ? "" : " off"}` },
      it.sumEl = el("input", { type: "checkbox", disabled: it.hasSummary ? null : "disabled",
        onchange: (e) => { it.summary = e.target.checked; sync(); } }),
      t("dump.summary")));

  const allBox = el("input", { type: "checkbox", checked: "checked" });
  const count = el("span", { class: "hint" });
  const okBtn = el("button", { class: "primary", type: "button", onclick: download },
    icon("download"), t("dump.download"));
  const courseBoxes = [];

  const tree = el("div", { class: "dump-list" },
    [...byCourse.entries()].map(([pid, mine]) => {
      const project = projects.find((p) => p.id === pid);
      const box = el("input", { type: "checkbox", checked: "checked" });
      box.addEventListener("change", () => {
        mine.forEach((it) => { it.text = box.checked; if (it.hasSummary) it.summary = box.checked; });
        sync();
      });
      courseBoxes.push({ box, mine });
      return el("div", { class: "dump-course" },
        el("label", { class: "dump-course-head" }, box,
          el("span", { class: "dump-title" }, project ? project.name : t("dump.course_fallback", { id: pid })),
          el("span", { class: "dump-count" },
            t("common.n_lectures", { count: mine.length }))),
        el("div", { class: "dump-lessons" }, mine.map(lessonRow)));
    }));

  allBox.addEventListener("change", () => {
    items.forEach((it) => { it.text = allBox.checked; if (it.hasSummary) it.summary = allBox.checked; });
    sync();
  });

  const dlg = el("dialog", { class: "dump", "aria-label": t("dump.aria") },
    el("h2", {}, t("dump.title")),
    el("p", { class: "hint" }, t("dump.hint")),
    el("div", { class: "split dump-all" },
      el("label", { class: "check" }, allBox, el("span", {}, t("dump.all"))),
      el("span", { class: "spacer" }), count),
    tree,
    el("div", { class: "split" },
      el("button", { type: "button", onclick: () => dlg.close() }, t("common.cancel")),
      okBtn));

  async function download() {
    const chosen = items.filter((it) => it.text || it.summary)
      .map((it) => ({ id: it.id, transcript: it.text, summary: it.summary }));
    if (!chosen.length) return;
    okBtn.disabled = true;
    okBtn.textContent = t("dump.building");
    try {
      const res = await fetch("/api/dump", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: chosen }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Il nome lo decide il server: una sola formattazione della data in giro.
      const name = (res.headers.get("Content-Disposition") || "")
        .match(/filename="([^"]+)"/)?.[1] || "trascrivi-dump.zip";
      const url = URL.createObjectURL(await res.blob());
      const link = el("a", { href: url, download: name });
      document.body.append(link);
      link.click();
      link.remove();
      // Il blob va lasciato vivo finché il browser non ha finito di scriverlo.
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      dlg.close();
      toast(t("dump.done", { count: chosen.length, name }), "ok");
    } catch (err) {
      toast(t("dump.failed", { error: err.message }), "err");
    } finally {
      okBtn.textContent = t("dump.download");
      sync();
    }
  }

  function sync() {
    items.forEach((it) => { it.textEl.checked = it.text; it.sumEl.checked = it.summary; });
    courseBoxes.forEach(({ box, mine }) => {
      const on = mine.filter((it) => it.text || it.summary).length;
      box.checked = on === mine.length;
      box.indeterminate = on > 0 && on < mine.length;
    });
    const lectures = items.filter((it) => it.text || it.summary).length;
    const files = items.reduce((n, it) => n + (it.text ? 1 : 0) + (it.summary ? 1 : 0), 0);
    allBox.checked = lectures === items.length;
    allBox.indeterminate = lectures > 0 && lectures < items.length;
    count.textContent = lectures
      ? `${t("common.n_lectures", { count: lectures })} · ${t("common.n_files", { count: files })}`
      : t("dump.nothing");
    okBtn.disabled = !lectures;
  }

  sync();
  document.body.append(dlg);
  // Navigare altrove con il dialogo aperto non deve lasciarlo a schermo: sta
  // fuori da `nav`, quindi il render della vista nuova non lo tocca.
  onViewTeardown(() => dlg.remove());
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
}


/* ============================================================================
   Dashboard
   ========================================================================== */
/* ── Primo avvio ─────────────────────────────────────────────────────────────
   Quattro passi con il loro stato vero (GPU, provider LLM, corso, lezione).
   Sparisce da solo quando sono tutti fatti, o se lo chiudi. */
function gettingStartedCard({ health, projects, totalTranscripts }) {
  let dismissed = false;
  try { dismissed = localStorage.getItem("trascrivi-onboarding") === "done"; } catch { /* ignora */ }
  const hw = health.hardware;
  const steps = [
    // La GPU è un'informazione, non un compito: su un PC senza NVIDIA non si completa.
    [true, hw.cuda_devices ? t("onb.gpu_ok", { gpu: hw.gpu_name || "CUDA" })
      : t("onb.gpu_cpu"), null],
    // Ollama è abilitato di default ma spesso spento: conta una chiave salvata.
    [health.providers.some((p) => p.enabled && p.has_key), t("onb.provider"), el("a", { class: "btn", href: "#/settings" }, t("onb.provider_cta"))],
    [projects.length > 0, t("onb.course"), null],
    [totalTranscripts > 0, t("onb.lecture"), projects.length
      ? el("a", { class: "btn", href: `#/p/${projects[0].id}/new` }, t("onb.lecture_cta")) : null],
  ];
  if (dismissed || steps.slice(1).every(([done]) => done)) return null;
  const card = el("div", { class: "card onboarding" },
    el("div", { class: "split" },
      el("h2", { style: "margin:0" }, t("onb.title")),
      el("span", { class: "spacer" }),
      el("button", { class: "ghost", type: "button", onclick: () => {
        try { localStorage.setItem("trascrivi-onboarding", "done"); } catch { /* ignora */ }
        card.remove();
      } }, t("onb.dismiss"))),
    el("p", { class: "muted small" }, t("onb.intro")),
    el("ol", { class: "onb-steps" }, steps.map(([done, text, action]) => el("li", { class: done ? "done" : "" },
      el("span", { class: `badge ${done ? "ok" : "warn"}` }, done ? "✓" : "·"),
      el("span", { class: "grow" }, text), done ? null : action))));
  return card;
}

async function viewDashboard(nav, params, token) {
  const [{ projects }, health, costs] = await Promise.all([api("/projects"), api("/health"),
    api("/costs").catch(() => null)]);
  const hw = health.hardware;

  const cards = el("div", { class: "grid" });
  projects.forEach((p) => {
    const id = p.id;
    const open = () => { location.hash = `#/p/${id}`; };
    const card = el("div", {
      class: "card link card-accent",
      style: `--color:${safeColor(p.color)}`,
      tabindex: "0",
      role: "link",
      "aria-label": t("dash.card_aria", { name: `${p.name}${p.code ? ` (${p.code})` : ""}`, count: p.n_transcripts }),
      onclick: open,
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      },
    },
      el("h2", {}, p.name),
      p.code ? el("span", { class: "badge" }, p.code) : null,
      // La descrizione resta sempre presente, anche vuota: serve a tenere
      // allineate le tre fasce (descrizione, metadati, numero) in ogni card.
      el("p", { class: "muted small" }, p.description || t("dash.no_description")),
      el("div", { class: "split small muted" },
        el("span", {}, t("common.n_transcripts", { count: p.n_transcripts })),
        el("span", { "aria-hidden": "true" }, "·"),
        el("span", {}, t("dash.updated", { date: fmtDate(p.updated_at) }))),
    );
    cards.append(card);
  });

  const newProject = el("form", { class: "card", onsubmit: submit },
    el("h2", {}, t("dash.new_course")),
    el("p", { class: "hint", style: "margin:-6px 0 14px" }, t("dash.new_course_hint")),
    el("div", { class: "row" },
      el("div", { class: "grow field" }, el("label", { for: "npName" }, t("dash.course_name")),
        el("input", { id: "npName", required: "required", placeholder: t("dash.course_name_ph") })),
      el("div", { class: "grow field" }, el("label", { for: "npCode" }, t("dash.code")),
        el("input", { id: "npCode", placeholder: "DS-2025" }))),
    el("div", { class: "field" }, el("label", { for: "npDesc" }, t("dash.description")),
      el("input", { id: "npDesc", placeholder: t("dash.description_ph") })),
    el("button", { class: "primary", type: "submit" }, t("dash.create")));

  async function submit(event) {
    event.preventDefault();
    const name = byId("npName");
    if (!name.value.trim()) {
      name.setAttribute("aria-invalid", "true");
      name.focus();
      return;
    }
    name.removeAttribute("aria-invalid");
    try {
      const p = await api("/projects", {
        method: "POST",
        body: { name: name.value.trim(), code: byId("npCode").value, description: byId("npDesc").value },
      });
      toast(t("dash.created", { name: p.name }), "ok");
      location.hash = `#/p/${p.id}`;
    } catch (err) { toast(err.message, "err"); }
  }

  const searchResults = el("div");
  const term = (new URLSearchParams(location.hash.split("?")[1] || "")).get("q");
  if (term) {
    const { results, summaries: summaryHits = [] } = await api(`/search?q=${encodeURIComponent(term)}`);
    setDocTitle(t("search.doc_title", { term }));
    searchResults.append(el("div", { class: "card" },
      el("div", { class: "split" },
        el("h2", { style: "margin:0;padding:0;border:0" }, t("search.title")),
        el("span", { class: "badge accent" }, t("search.hits", { count: results.length + summaryHits.length })),
        el("span", { class: "spacer" }),
        el("a", { class: "btn", href: "#/" }, t("search.clear"))),
      el("p", { class: "hint", style: "margin-bottom:12px" }, t("search.matches", { term })),
      summaryHits.length ? el("div", { class: "stack", style: "margin-bottom:14px" },
        el("p", { class: "eyebrow" }, t("search.in_summaries"), el("span", { class: "count" }, String(summaryHits.length))),
        summaryHits.map((r) => el("div", { class: "split" },
          el("a", { href: `#/t/${r.transcript_id}?tab=summary` }, r.title || "—"),
          el("span", { class: "badge" }, r.project_name || "—"),
          el("p", { class: "small muted", style: "flex-basis:100%;margin:0",
            html: esc(r.snippet).replace(/\[\[/g, "<mark>").replace(/\]\]/g, "</mark>") })))) : null,
      results.length ? el("p", { class: "eyebrow" }, t("search.in_transcripts"), el("span", { class: "count" }, String(results.length))) : null,
      results.length ? el("div", { class: "stack" }, results.map((r) =>
        el("div", { class: "split" },
          el("a", { href: `#/t/${r.id}?q=${encodeURIComponent(term)}${r.at != null ? `&at=${Math.floor(r.at)}` : ""}` }, r.title),
          r.at != null ? el("span", { class: "badge" }, fmtTs(r.at)) : null,
          el("span", { class: "badge" }, r.project_name || "—"),
          el("span", { class: "spacer" }),
          el("span", { class: "small muted" }, t("search.open")),
          el("p", { class: "small muted", style: "flex-basis:100%;margin:0",
            html: esc(r.snippet).replace(/\[\[/g, "<mark>").replace(/\]\]/g, "</mark>") }),
        ))) : summaryHits.length ? null : el("p", { class: "muted" }, t("search.none"))));
  }

  // Riga di stato dell'hardware: badge separati quando i valori ci sono,
  // altrimenti il solo fatto che conta (CPU o GPU).
  const hwBadges = el("div", { class: "toolbar trailing" },
    hw.cuda_devices
      ? el("span", { class: "badge ok" }, `GPU · ${hw.gpu_name || "CUDA"}`)
      : el("span", { class: "badge" }, t("hw.cpu_only")),
    hw.vram_free_gb != null ? el("span", { class: "badge" }, t("hw.vram_free", { gb: hw.vram_free_gb })) : null,
    el("span", { class: "badge" }, t("hw.threads", { count: hw.cpu_count })),
    hw.cached_models.length ? el("span", { class: "badge" }, t("hw.models_in_ram", { count: hw.cached_models.length })) : null);

  const totalTranscripts = projects.reduce((sum, p) => sum + (p.n_transcripts || 0), 0);

  if (!isCurrent(token)) return;
  nav.replaceChildren(
    el("div", { class: "stack" },
      el("div", { class: "card" },
        el("div", { class: "split hero" },
          el("div", { class: "grow" },
            el("p", { class: "empty-kicker" }, t("shell.kicker")),
            el("h1", {}, t("nav.library")),
            el("p", { class: "muted small hero-note" },
              projects.length
                ? t("dash.summary", { courses: t("common.n_courses", { count: projects.length }),
                  transcripts: t("common.n_transcripts", { count: totalTranscripts }) })
                : t("dash.intro"))),
          el("div", { class: "hero-side" },
            hwBadges,
            el("div", { class: "actions" },
              el("button", { type: "button",
                title: t("dash.dump_hint"),
                disabled: totalTranscripts ? null : "disabled",
                onclick: openDumpDialog }, icon("download"), t("dump.title")))))),
      costs && costs.budget_usd > 0 && costs.month_total_usd > costs.budget_usd
        ? notice("warn", t("budget.over", { spent: costs.month_total_usd.toFixed(2), budget: costs.budget_usd.toFixed(2) }))
        : null,
      gettingStartedCard({ health, projects, totalTranscripts }),
      searchResults,
      projects.length
        ? el("p", { class: "eyebrow" }, t("dash.courses"), el("span", { class: "count" }, String(projects.length)))
        : null,
      projects.length ? cards : el("div", { class: "empty" },
        el("p", { class: "empty-kicker" }, t("dash.empty_kicker")),
        el("p", {}, t("dash.empty")),
        el("div", { class: "empty-actions" },
          el("a", { class: "btn", href: "#/settings" }, t("dash.check_defaults")))),
      newProject,
    ));
}

/* ============================================================================
   Vista progetto
   ========================================================================== */
/* ── Slide del corso ──────────────────────────────────────────────────────────
   Upload (drag&drop o file picker), stato del parsing per documento, badge per
   pagina e assegnazione alle lezioni. Un deck ha centinaia di pagine: i badge
   stanno dentro un `<details>` chiuso, altrimenti la scheda del corso diventa
   lunga quanto il corso. */
/* Due usi dello stesso componente: le slide (contesto di riassunti e carte,
   assegnate alle lezioni) e gli esami degli anni passati (`kind = "exam"`:
   stesso parser con OCR, niente assegnazione, servono alla guida d'esame). */
function slideDocsCard({ projectId, docs, links, transcripts, jobs, settings, kind = "slides" }) {
  const isExam = kind === "exam";
  const host = el("div", { class: "stack" });
  const picker = el("input", {
    type: "file", id: isExam ? "examUpload" : "slideUpload", multiple: "multiple", class: "hidden",
    accept: ".pdf,.pptx,.docx,.png,.jpg,.jpeg,.webp,.txt,.md",
    onchange: (e) => { upload([...e.currentTarget.files]); e.currentTarget.value = ""; },
  });

  const drop = el("div", {
    class: "drop", tabindex: "0", role: "button",
    "aria-label": isExam ? t("slides.drop_aria_exam") : t("slides.drop_aria"),
    onclick: () => picker.click(),
    onkeydown: (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); }
    },
    ondragover: (e) => { e.preventDefault(); drop.classList.add("is-over"); },
    ondragleave: () => drop.classList.remove("is-over"),
    ondrop: (e) => {
      e.preventDefault();
      drop.classList.remove("is-over");
      upload([...e.dataTransfer.files]);
    },
  },
    el("span", { class: "drop-mark", "aria-hidden": "true" }, "⇪"),
    el("span", {}, isExam ? t("slides.drop_exam") : t("slides.drop")));

  let busy = false;

  async function upload(files) {
    if (busy || !files.length) return;
    busy = true;
    drop.classList.add("is-busy");
    try {
      for (const file of files) {
        const form = new FormData();
        form.append("file", file);
        form.append("kind", kind);
        const res = await api(`/projects/${projectId}/slides`, { method: "POST", body: form });
        if (res.duplicate_of) toast(t("slides.duplicate", { name: file.name }), "");
        else toast(t("slides.queued", { name: file.name }), "ok");
      }
      render();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy = false;
      drop.classList.remove("is-busy");
    }
  }

  async function toggleVlm(doc, enabled) {
    try {
      await api(`/slides/${doc.id}`, { method: "PATCH", body: { vlm_enabled: enabled } });
      toast(enabled ? t("slides.vlm_on") : t("slides.vlm_off"), "ok");
    } catch (err) { toast(err.message, "err"); }
  }

  async function reparse(doc) {
    try {
      await api(`/slides/${doc.id}/reparse`, { method: "POST" });
      toast(t("slides.reparse_queued"), "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  }

  async function removeDoc(doc) {
    if (!(await confirmDialog(t("slides.delete_title"),
      t("slides.delete_text", { name: doc.name })))) return;
    try {
      await api(`/slides/${doc.id}`, { method: "DELETE" });
      toast(t("slides.deleted"), "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  }

  async function findMatches() {
    try {
      await api(`/projects/${projectId}/slides/match`, { method: "POST" });
      toast(t("slides.match_queued"), "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  }

  function pageChip(page) {
    const flags = [page.complex ? "complex" : "", page.vlm_md ? "vlm" : "",
      page.math_uncertain ? "uncertain" : ""].filter(Boolean);
    const title = [t("slides.page", { n: page.page_no }), page.complex ? t("slides.flag_complex") : "",
      page.vlm_md ? t("slides.flag_vlm") : "", page.math_uncertain ? t("slides.flag_uncertain") : ""]
      .filter(Boolean).join(" · ");
    return el("a", {
      class: `page-chip ${flags.join(" ")}`, title,
      href: page.render_path ? `/api/slides/${page.doc_id}/pages/${page.page_no}.png` : null,
      target: page.render_path ? "_blank" : null, rel: "noopener",
    }, String(page.page_no));
  }

  /* Il testo che il parser ha letto: OCR/testo nativo e rendering VLM, pagina
     per pagina. È la forma intermedia da cui nascono le carte, e senza vederla
     non c'è modo di capire perché una carta manca o perché una pagina è
     "incerta": si apre una volta e si legge tutto. */
  function openTexts(doc) {
    const body = el("div", { class: "stack", style: "max-height:62vh; overflow:auto" },
      el("p", { class: "muted small" }, t("slides.texts_loading")));
    const dlg = el("dialog", { "aria-label": t("slides.texts_aria", { name: doc.name }) },
      el("h2", {}, t("slides.texts_title")),
      el("p", { class: "hint" }, t("slides.texts_hint", { name: doc.name })),
      body,
      el("div", { class: "split" },
        el("button", { type: "button", onclick: () => dlg.close() }, t("common.close"))));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();

    api(`/slides/${doc.id}`).then(({ pages }) => {
      if (!pages || !pages.length) {
        body.replaceChildren(el("p", { class: "muted small" }, t("slides.texts_none")));
        return;
      }
      body.replaceChildren(...pages.map((page) => {
        const flags = [page.complex ? t("slides.flag_complex") : "", page.vlm_md ? "VLM" : "",
          page.math_uncertain ? t("slides.flag_uncertain") : ""].filter(Boolean).join(" · ");
        const ocr = el("pre", { class: "page-text" }, page.text || t("slides.no_text"));
        const block = [el("div", { class: "small muted" }, t("slides.text_ocr")), ocr];
        if (page.vlm_md) {
          block.push(el("div", { class: "small muted", style: "margin-top:8px" }, t("slides.vlm_heading")),
            el("pre", { class: "page-text" }, page.vlm_md));
        }
        return el("details", { class: "page-doc" },
          el("summary", {},
            el("strong", {}, t("slides.page", { n: page.page_no })),
            flags ? el("span", { class: "small muted" }, ` · ${flags}`) : null),
          el("div", { class: "stack", style: "margin-top:8px" }, ...block,
            page.render_path
              ? el("a", { class: "small", href: `/api/slides/${doc.id}/pages/${page.page_no}.png`,
                  target: "_blank", rel: "noopener" }, t("slides.open_render"))
              : null));
      }));
    }).catch((err) => {
      body.replaceChildren(el("p", { class: "field-error" }, err.message));
    });
  }

  function docRow(doc) {
    const parsed = doc.n_parsed || 0;
    const details = el("details", {
      ontoggle: async (e) => {
        if (!e.currentTarget.open || e.currentTarget.dataset.loaded) return;
        e.currentTarget.dataset.loaded = "1";
        const grid = e.currentTarget.querySelector(".page-grid");
        try {
          const { pages } = await api(`/slides/${doc.id}`);
          grid.replaceChildren(...pages.map(pageChip));
        } catch (err) { grid.replaceChildren(el("span", { class: "small muted" }, err.message)); }
      },
    }, el("summary", { class: "small muted" }, t("slides.pages_toggle")),
      el("div", { class: "small muted", style: "margin-top:6px" }, t("slides.legend")),
      el("div", { class: "page-grid" }));

    return el("div", { class: "slide-doc" },
      el("div", { class: "split" },
        el("div", { class: "grow" },
          el("strong", {}, doc.name),
          el("div", { class: "small muted" },
            t("slides.doc_stats", { count: parsed, complex: doc.n_complex || 0, vlm: doc.n_vlm || 0 }),
            isExam ? "" : ` · ${t("slides.doc_links", { count: doc.n_links || 0 })}`),
          doc.error ? el("div", { class: "small field-error" }, doc.error) : null),
        // slides.status_parsing, slides.status_queued, slides.status_ready, slides.status_error
        el("span", { class: `badge ${doc.status === "ready" ? "ok" : "warn"}` },
          ["parsing", "queued", "ready", "error"].includes(doc.status) ? t(`slides.status_${doc.status}`) : doc.status),
        el("label", { class: "check", title: t("slides.vlm_title") },
          el("input", {
            type: "checkbox", checked: doc.vlm_enabled ? "checked" : null,
            onchange: (e) => toggleVlm(doc, e.currentTarget.checked),
          }), el("span", {}, "VLM")),
        isExam ? null : el("button", {
          type: "button", class: "btn", disabled: parsed ? null : "disabled",
          title: t("slides.assign_title"), onclick: () => openLinks(doc),
        }, t("slides.assign")),
        el("button", {
          type: "button", class: "btn", disabled: parsed ? null : "disabled",
          title: t("slides.texts_btn_title"),
          onclick: () => openTexts(doc),
        }, t("slides.texts_btn")),
        el("button", { type: "button", class: "btn", onclick: () => reparse(doc),
          title: t("slides.reparse_title") }, t("slides.reparse")),
        el("button", { type: "button", class: "danger", onclick: () => removeDoc(doc) },
          t("common.delete"))),
      parsed ? details : null);
  }

  /* La conferma delle assegnazioni: un intervallo per riga, con l'anteprima
     della prima pagina — un numero di pagina da solo non dice se è giusto. */
  function openLinks(doc) {
    const mine = links.filter((l) => l.doc_id === doc.id);
    // Un deck grande produce decine di intervalli: la finestra scorre, non cresce
    // oltre lo schermo.
    const rowsHost = el("div", { class: "stack", style: "max-height:52vh; overflow:auto" });
    const from = el("input", { id: "linkFrom", type: "number", min: "1", max: String(doc.n_pages || 1),
      value: "1", "aria-label": t("slides.from_page"), style: "width:5em" });
    const to = el("input", { id: "linkTo", type: "number", min: "1", max: String(doc.n_pages || 1),
      value: String(doc.n_pages || 1), "aria-label": t("slides.to_page"), style: "width:5em" });
    const target = el("select", { id: "linkTarget", "aria-label": t("slides.lecture") },
      transcripts.map((tr) => el("option", { value: tr.id }, tr.title)));

    function paint() {
      rowsHost.replaceChildren(...(mine.length ? mine.map((link) => el("div", { class: "slide-link" },
        el("img", {
          class: "thumb", loading: "lazy", alt: "",
          src: `/api/slides/${doc.id}/pages/${link.page_from}.png`,
          onerror: (e) => { e.currentTarget.classList.add("hidden"); },
        }),
        el("div", { class: "grow" },
          el("div", { class: "small" },
            t("slides.link_pages", { from: link.page_from, to: link.page_to }), el("strong", {}, link.transcript_title)),
          el("div", { class: "small muted nowrap" },
            // slides.source_auto, slides.source_manual
            el("span", { class: "badge" },
              ["auto", "manual"].includes(link.source) ? t(`slides.source_${link.source}`) : link.source),
            ` ${t("slides.score", { score: (link.score || 0).toFixed(2) })}`),
          el("div", { class: "small muted" }, (link.first_text || "").slice(0, 140))),
        el("button", { type: "button", class: "btn", title: t("slides.remove_range_title"),
          onclick: async () => {
            try {
              await api(`/slide-links/${link.id}`, { method: "DELETE" });
              mine.splice(mine.indexOf(link), 1);
              paint();
            } catch (err) { toast(err.message, "err"); }
          } }, t("common.remove")))) : [
        el("p", { class: "muted small" }, t("slides.no_ranges")),
      ]));
    }
    paint();

    const dlg = el("dialog", { "aria-label": t("slides.links_aria", { name: doc.name }) },
      el("h2", {}, t("slides.links_title")),
      el("p", { class: "hint" }, t("slides.links_hint", { name: doc.name, count: doc.n_pages || 0 })),
      rowsHost,
      el("hr"),
      el("div", { class: "split" },
        el("span", { class: "small muted" }, t("slides.pages_toggle")), from, el("span", { class: "muted" }, "→"), to,
        target,
        el("button", { type: "button", onclick: async () => {
          try {
            await api(`/slides/${doc.id}/links`, {
              method: "POST",
              body: {
                page_from: Number(from.value), page_to: Number(to.value),
                transcript_id: Number(target.value),
              },
            });
            toast(t("slides.range_assigned"), "ok");
            dlg.close();
            render();
          } catch (err) { toast(err.message, "err"); }
        } }, t("slides.add_range"))),
      el("div", { class: "split" },
        el("button", { type: "button", onclick: () => dlg.close() }, t("common.close")),
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "primary", onclick: async () => {
          try {
            await api(`/slides/${doc.id}/links/accept`, { method: "POST", body: {} });
            toast(t("slides.links_accepted"), "ok");
            dlg.close();
            render();
          } catch (err) { toast(err.message, "err"); }
        } }, t("slides.accept_all"))));

    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
  }

  const ready = docs.filter((d) => d.status === "ready").length;
  // Senza lezioni non c'è niente a cui associare: il bottone lo dice invece di
  // accodare un job che trova zero candidati e non spiega perché.
  const hasLectures = transcripts.length > 0;
  setChildren(host,
    el("div", { class: "split" },
      el("div", { class: "grow" },
        el("h2", {}, isExam ? t("slides.exams_title") : t("slides.title")),
        el("p", { class: "muted small" }, isExam ? t("slides.exams_intro") : t("slides.intro"))),
      isExam ? null : el("button", {
        type: "button", class: "btn", disabled: ready && hasLectures ? null : "disabled",
        title: hasLectures
          ? t("slides.match_title")
          : t("slides.match_needs_lecture"),
        onclick: findMatches,
      }, t("slides.match"))),
    picker,
    drop,
    docs.length
      ? el("div", { class: "stack" }, docs.map(docRow))
      : el("p", { class: "muted small" }, isExam ? t("slides.no_exams") : t("slides.no_decks")),
    !isExam && !hasLectures && ready
      ? notice("warn", t("slides.no_lectures_notice"))
      : null,
    /* La VLM è quella che può mancare senza far fallire niente: se una pagina
       complessa non ha il rendering, quasi sempre Ollama è spento o il modello
       non è scaricato. Dirlo qui evita di cercare il problema nel parser. */
    (settings.slides_vlm || "auto") !== "off"
      && docs.some((d) => d.status === "ready" && (d.n_complex || 0) > 0 && !d.n_vlm)
      ? notice("warn",
          t("slides.vlm_missing", { model: settings.slides_vlm_model || "gemma3:4b" }),
          el("strong", {}, t("slides.reparse")))
      : null,
    docs.some((d) => ["parsing", "queued"].includes(d.status))
      ? notice("warn", t("slides.parsing"))
      : null);
  return host;
}

/* ============================================================================
   Studio sul corso intero: "Ask the course" e la guida d'esame
   ========================================================================== */
/* Le citazioni del modello ([L3] o [L3 12:40]) diventano link alla lezione. */
function linkCitations(html, lectures) {
  const byLabel = new Map((lectures || []).map((l) => [l.label, l]));
  return html.replace(/\[(L\d+)((?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\]/g, (match, label, stamp) => {
    const lec = byLabel.get(label);
    if (!lec) return match;
    const at = stamp.trim();
    const secs = at ? at.split(":").reduce((acc, part) => acc * 60 + Number(part), 0) : null;
    return `<a class="cite" href="#/t/${lec.transcript_id}${secs !== null ? `?at=${secs}` : ""}" title="${esc(lec.title)}${at ? ` · ${at}` : ""}">${label}${at ? ` ${at}` : ""}</a>`;
  }).replace(/\((L\d+(?:,\s*L\d+)*)\)/g, (match, group) => {
    // La guida cita tra parentesi tonde: (L3) o (L3, L7).
    if (!group.split(/,\s*/).every((label) => byLabel.has(label))) return match;
    return `(${group.split(/,\s*/).map((label) => {
      const lec = byLabel.get(label);
      return `<a class="cite" href="#/t/${lec.transcript_id}" title="${esc(lec.title)}">${label}</a>`;
    }).join(", ")})`;
  });
}

function providerPicker(id, providers, settings) {
  const enabled = providers.filter((p) => p.enabled);
  return enabled.length
    ? el("select", { id, "aria-label": t("common.provider") }, enabled.map((p) => el("option", {
      value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null,
    }, `${p.name} · ${p.model || t("common.no_model")}`)))
    : null;
}

function askCourseCard({ projectId, providers, settings, nSummaries, token }) {
  const host = el("div", { class: "stack" });
  const list = el("div", { class: "stack" });
  const provider = providerPicker("askProvider", providers, settings);
  const input = el("textarea", { id: "askInput", rows: "2", class: "ask-input",
    placeholder: t("ask.placeholder") });
  const button = el("button", { class: "primary", type: "submit" }, t("ask.button"));
  const status = el("span", { class: "muted small" });

  const qa = (q, i = 0) => el("details", { class: "qa", open: i === 0 ? "open" : null },
    el("summary", {}, el("span", { class: "qa-q" }, q.question),
      el("span", { class: "spacer" }),
      el("span", { class: "muted small tabnum" },
        `${fmtDate(q.created_at)}${q.cost_usd ? ` · $${Number(q.cost_usd).toFixed(4)}` : ""}`)),
    el("div", { class: "md", html: linkCitations(renderMarkdown(q.answer || ""), q.sources?.lectures) }),
    el("div", { class: "split" }, el("span", { class: "spacer" }),
      el("button", { class: "ghost", type: "button", onclick: async (e) => {
        // currentTarget vale null dopo un await: il nodo si prende prima.
        const box = e.currentTarget.closest("details");
        await api(`/questions/${q.id}`, { method: "DELETE" });
        box.remove();
      } }, t("common.remove"))));

  async function submit(event) {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) { input.focus(); return; }
    button.disabled = true;
    button.textContent = t("ask.thinking");
    status.replaceChildren(el("span", { class: "spin" }), ` ${t("ask.reading")}`);
    try {
      const res = await api(`/projects/${projectId}/ask`, { method: "POST",
        body: { question, provider_id: provider ? Number(provider.value) : null } });
      if (!isCurrent(token)) return;
      list.prepend(qa(res.question));
      input.value = "";
      status.textContent = "";
    } catch (err) {
      status.replaceChildren(notice("err", humanError(err.message)));
    } finally {
      button.disabled = false;
      button.textContent = t("ask.button");
    }
  }
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit(); }
  });

  const form = el("form", { class: "stack", onsubmit: submit },
    input,
    el("div", { class: "split" }, provider, button, status, el("span", { class: "spacer" }),
      el("span", { class: "muted small" }, t("ask.shortcut"))));

  setChildren(host,
    el("div", {},
      el("h2", {}, t("ask.title")),
      el("p", { class: "muted small" }, t("ask.intro"))),
    nSummaries
      ? (provider ? form : notice("warn", t("ask.need_provider")))
      : notice("warn", t("ask.need_summary")),
    list);
  api(`/projects/${projectId}/questions`).then(({ questions }) => {
    if (isCurrent(token)) setChildren(list, questions.map(qa));
  }).catch(() => {});
  return host;
}

function examGuideCard({ projectId, providers, settings, nSummaries, nExams, jobs, token, lectures = [] }) {
  // Il polling ridisegna la card ogni 2 s mentre un job gira: senza questo la
  // guida aperta per leggerla si richiudeva da sola.
  let guideOpen = false;
  /* Le etichette L# con cui la guida cita le lezioni sono quelle del momento in
     cui è stata scritta (salvate con la guida): cancellare una lezione rinumera
     le altre, e i link punterebbero alla lezione sbagliata. */
  const guideLectures = (guide) => {
    let map = {};
    try { map = JSON.parse(guide.labels || "{}"); } catch { /* guida vecchia */ }
    const saved = Object.entries(map).map(([label, v]) => ({ label, transcript_id: v.transcript_id, title: v.title }));
    return saved.length ? saved : lectures;
  };
  const host = el("div", { class: "stack" });
  const body = el("div", { class: "stack" });
  const provider = providerPicker("guideProvider", providers, settings);
  let job = jobs.find((j) => j.kind === "llm_guide" && j.project_id === projectId
    && ["queued", "running"].includes(j.status)) || null;
  let guides = [];
  let poll = null;

  function paint() {
    const latest = guides[0];
    const button = el("button", {
      class: "primary", type: "button",
      disabled: !job && provider && nSummaries ? null : "disabled",
      onclick: generate,
    }, job ? t("guide.writing") : latest ? t("guide.regenerate") : t("guide.generate"));
    setChildren(host,
      el("div", { class: "split" },
        el("div", { class: "grow" },
          el("h2", {}, t("guide.title")),
          el("p", { class: "muted small" }, nExams
            ? t("guide.intro_exams", { summaries: t("common.n_lecture_summaries", { count: nSummaries }),
              exams: t("guide.n_exams", { count: nExams }) })
            : t("guide.intro", { summaries: t("common.n_lecture_summaries", { count: nSummaries }) }))),
        provider, button,
        latest ? el("a", { class: "btn", href: `/api/guides/${latest.id}/export` }, t("common.download_md")) : null),
      job ? el("div", { class: "split small muted" }, el("span", { class: "spin" }),
        t("common.follow_in", { status: jobMessage(job.message) || jobStatusLabel(job.status) }), el("a", { href: `#/jobs?focus=${job.id}` }, t("nav.jobs"))) : null,
      latest ? el("details", { class: "guide-doc", open: guideOpen ? "open" : null,
        ontoggle: (e) => { guideOpen = e.currentTarget.open; } },
        el("summary", {}, `${latest.title} · ${fmtDate(latest.created_at)} · ${t("common.n_lectures", { count: latest.n_lectures })}`
          + `${latest.n_exam_pages ? ` · ${t("guide.n_exam_pages", { count: latest.n_exam_pages })}` : ""}`
          + `${latest.cost_usd ? ` · $${Number(latest.cost_usd).toFixed(4)}` : ""}`),
        el("div", { class: "md", html: linkCitations(renderMarkdown(latest.markdown || ""), guideLectures(latest)) })) : null,
      !nSummaries ? notice("warn", t("guide.need_summaries")) : null);
  }

  async function load() {
    guides = (await api(`/projects/${projectId}/guides`)).guides || [];
    if (isCurrent(token)) paint();
  }

  async function generate() {
    askNotifyPermission();
    try {
      const res = await api(`/projects/${projectId}/guide`, { method: "POST",
        body: { provider_id: provider ? Number(provider.value) : null } });
      job = res.job;
      paint();
      startPoll();
    } catch (err) { toast(err.message, "err"); }
  }

  function startPoll() {
    if (poll) return;
    poll = setInterval(async () => {
      if (!isCurrent(token)) { clearInterval(poll); poll = null; return; }
      try {
        const fresh = await api(`/jobs/${job.id}`);
        if (["queued", "running"].includes(fresh.status)) { job = fresh; paint(); return; }
        clearInterval(poll); poll = null; job = null;
        if (fresh.status === "done") { toast(t("guide.ready"), "ok"); await load(); }
        else { toast(fresh.error || t("guide.ended", { status: jobStatusLabel(fresh.status) }), "err"); paint(); }
      } catch { /* rete: si riprova al giro dopo */ }
    }, 2000);
    onViewTeardown(() => { if (poll) clearInterval(poll); });
  }

  paint();
  load().catch(() => {});
  if (job) startPoll();
  return host;
}

/* Stato di studio di una lezione: un menu nella riga, salvato subito.
   Etichette: study.none, study.todo, study.reviewed, study.confident. */
const STUDY = ["", "todo", "reviewed", "confident"];
function studySelect(lecture, onChange) {
  const select = el("select", { class: `study-select study-${lecture.study_status || "none"}`,
    "aria-label": t("study.column"),
    onchange: async (e) => {
      const value = e.currentTarget.value;
      try {
        await api(`/transcripts/${lecture.id}`, { method: "PATCH", body: { study_status: value } });
        lecture.study_status = value;
        select.className = `study-select study-${value || "none"}`;
        select.closest("tr")?.setAttribute("class", value ? `study-${value}` : "");
        onChange?.();
      } catch (err) { toast(err.message, "err"); }
    } },
  STUDY.map((v) => el("option", { value: v, selected: (lecture.study_status || "") === v ? "selected" : null },
    t(`study.${v || "none"}`))));
  return select;
}

/* Corso senza lezioni: i tre passi, nell'ordine in cui servono. */
function emptyCourseSteps(projectId) {
  return el("div", { class: "card" },
    el("h2", {}, t("course.start_title")),
    el("ol", { class: "onb-steps" },
      el("li", {}, el("span", { class: "badge warn" }, "1"), el("span", { class: "grow" }, t("course.step_lectures")),
        el("a", { class: "btn primary", href: `#/p/${projectId}/new` }, t("course.step_lectures_cta"))),
      el("li", {}, el("span", { class: "badge" }, "2"), el("span", { class: "grow" }, t("course.step_slides"))),
      el("li", {}, el("span", { class: "badge" }, "3"), el("span", { class: "grow" }, t("course.step_exams")))));
}

async function viewProject(nav, params, projectId, token) {
  // `providers` e `settings` servono alla modale del riassunto, `jobs` a sapere
  // se uno è già in corso: come la vista trascrizione, la pagina se li procura
  // da sé invece di darli per globali.
  const [project, { transcripts }, { terms }, { jobs }, providersRes, settingsRes,
    slidesRes, linksRes] =
    await Promise.all([
      api(`/projects/${projectId}`), api(`/transcripts?project_id=${projectId}`),
      api(`/terms?project_id=${projectId}`), api("/jobs?limit=20"),
      api("/providers"), api("/settings"),
      api(`/projects/${projectId}/slides`), api(`/projects/${projectId}/slide-links`),
    ]);
  const providers = providersRes.providers;
  const settings = settingsRes.settings;
  const slides = slidesRes.slides || [];
  const slideLinks = linksRes.links || [];
  const deckDocs = slides.filter((d) => (d.kind || "slides") === "slides");
  const exams = slides.filter((d) => d.kind === "exam");
  const nSummaries = transcripts.filter((t) => t.n_summaries > 0).length;
  setDocTitle(`${project.name} · ${t("route.course")}`);

  const totalSeconds = transcripts.reduce((sum, t) => sum + (t.duration || 0), 0);

  /* ── Il bottone del riassunto in tabella ───────────────────────────────────
     Un solo comando che cambia con lo stato: con un riassunto scarica l'ultimo
     (.md), senza riassunto apre la modale che lo genera, e mentre il job scrive
     è spento. Lo stato cambia da solo (polling), quindi ogni cella è un
     contenitore stabile che `paintSummaryCell` ridisegna quando serve. */
  const summaryCells = new Map();      // transcript id -> { t, host }
  const summaryPending = new Map();    // transcript id -> job in corso
  /* Il chip delle carte, accanto a quello del riassunto: conteggio delle carte
     della lezione e, mentre il job gira, lo stato. Le carte si generano dentro
     la lezione (tab Carte), quindi qui il chip è un puntatore con il numero. */
  const cardsCells = new Map();        // transcript id -> { t, host }
  const cardsPending = new Map();      // transcript id -> job in corso

  function paintCardsCell(id) {
    const cell = cardsCells.get(id);
    if (!cell) return;
    const { t: tr, host } = cell;
    const job = cardsPending.get(id);
    const counts = tr.card_counts || {};
    const total = counts.total || tr.n_cards || 0;
    setChildren(host,
      job
        ? el("button", { class: "btn", type: "button", disabled: "disabled",
            title: t("cards.generating_title") }, t("cards.generating"))
        : el("a", {
            class: "btn", href: `#/t/${tr.id}`,
            title: total ? t("cards.cell_title", { count: total })
              : t("cards.cell_empty_title"),
          }, total ? t("cards.n", { count: total }) : t("cards.add")));
  }

  function paintSummaryCell(id) {
    const cell = summaryCells.get(id);
    if (!cell) return;
    const { t: tr, host } = cell;
    const job = summaryPending.get(id);
    setChildren(host,
      job
        ? el("button", { class: "btn", type: "button", disabled: "disabled",
            title: t("sumcell.writing_title") }, t("sumcell.writing"))
        : tr.latest_summary_id
          ? el("a", { class: "btn", title: t("sumcell.download_title"),
              href: `/api/summaries/${tr.latest_summary_id}/export?format=md` }, t("sumcell.download"))
          : el("button", { class: "btn", type: "button",
              title: t("sumcell.generate_title"),
              onclick: () => openSummaryDialog(tr) }, t("sumcell.add")));
  }

  // Polling mirato: parte solo quando c'è un riassunto in coda e si spegne da
  // sé quando l'ultimo è finito — o quando si lascia la vista. Lo stesso ciclo
  // segue le slide (parsing e matching) e le carte: sono tutti job dello stesso
  // corso, e un intervallo per tipo sarebbe tre timer per la stessa pagina.
  let coursePoll = null;
  let courseTickBusy = false;
  const SLIDE_JOBS = ["parse_slides", "embed_match"];
  let slidesPending = jobs.some((j) => SLIDE_JOBS.includes(j.kind) && !j.payload?.auto && j.project_id === projectId
    && ["queued", "running"].includes(j.status));

  function startCoursePoll() {
    if (!coursePoll) coursePoll = setInterval(courseTick, 2000);
  }

  function stopCoursePoll() {
    if (coursePoll) { clearInterval(coursePoll); coursePoll = null; }
  }
  onViewTeardown(stopCoursePoll);

  async function courseTick() {
    if (!isCurrent(token)) { stopCoursePoll(); return; }
    // Una risposta più lenta dell'intervallo non deve accavallarsi alla
    // successiva: due risposte fuori ordine farebbero lampeggiare lo stato.
    if (courseTickBusy) return;
    courseTickBusy = true;
    let latest;
    try {
      latest = (await api("/jobs?limit=20")).jobs || [];
    } catch {
      return;   // rete: si riprova al giro successivo, senza spaventare l'utente
    } finally {
      courseTickBusy = false;
    }
    if (!isCurrent(token)) { stopCoursePoll(); return; }

    const active = new Map();
    transcripts.forEach((t) => {
      const job = latest.find((j) => j.kind === "llm_summary" && j.transcript_id === t.id
        && ["queued", "running"].includes(j.status));
      if (job) active.set(t.id, job);
    });
    active.forEach((job, id) => {
      if (!summaryPending.has(id)) paintSummaryCell(id);
      summaryPending.set(id, job);
    });

    // Carte in corso: stesso schema del riassunto, cella diversa.
    const cardsActive = new Map();
    transcripts.forEach((t) => {
      const job = latest.find((j) => j.kind === "llm_cards" && j.transcript_id === t.id
        && ["queued", "running"].includes(j.status));
      if (job) cardsActive.set(t.id, job);
    });
    cardsActive.forEach((job, id) => {
      if (!cardsPending.has(id)) paintCardsCell(id);
      cardsPending.set(id, job);
    });
    for (const id of [...cardsPending.keys()]) {
      if (cardsActive.has(id)) continue;
      cardsPending.delete(id);
      try {
        const fresh = await api(`/transcripts/${id}`);
        const cell = cardsCells.get(id);
        if (cell) cell.t.card_counts = fresh.card_counts;
        if (isCurrent(token)) paintCardsCell(id);
        if (fresh.card_counts && fresh.card_counts.total) toast(t("cards.ready"), "ok");
      } catch { /* si riprova al prossimo giro */ }
    }

    // Slide: parsing o matching finiti -> la pagina si ridisegna una volta sola,
    // con i nuovi conteggi e i badge delle pagine.
    // Il matching accodato in automatico (dopo ogni trascrizione e riassunto)
    // non ridisegna la pagina: cancellava la domanda che stavi scrivendo e
    // richiudeva gli accordion. Solo slide ed esami caricati da te.
    const stillSlides = latest.some((j) => SLIDE_JOBS.includes(j.kind) && !j.payload?.auto
      && j.project_id === projectId && ["queued", "running"].includes(j.status));
    if (slidesPending && !stillSlides && isCurrent(token) && !byId("askInput")?.value.trim()) {
      slidesPending = false;
      render();
      return;
    }
    slidesPending = stillSlides;

    // Usciti dalla lista: o completati o falliti. L'esito non si indovina dallo
    // stato del job (che può non essere più tra gli ultimi 20): si rilegge la
    // trascrizione e si guarda se il riassunto c'è.
    for (const id of [...summaryPending.keys()]) {
      if (active.has(id)) continue;
      const job = summaryPending.get(id);
      summaryPending.delete(id);
      let fresh = null;
      try { fresh = await api(`/transcripts/${id}`); } catch { /* si riprova al prossimo giro */ }
      if (!isCurrent(token)) { stopCoursePoll(); return; }
      if (!fresh) { summaryPending.set(id, job); continue; }
      const cell = summaryCells.get(id);
      if (cell) cell.t.latest_summary_id = fresh.latest_summary_id || null;
      paintSummaryCell(id);
      // Lo stato finale, se il job è ancora tra gli ultimi, sta nella lista:
      // è lì che si trova il messaggio d'errore vero.
      const ended = job && job.id != null ? latest.find((j) => j.id === job.id) : null;
      if (fresh.latest_summary_id) toast(t("summary.ready"), "ok");
      else if (ended && ended.error) toast(t("summary.failed", { error: ended.error }), "err");
      else toast(t("summary.no_result"), "err");
    }
    if (!summaryPending.size && !cardsPending.size && !slidesPending) stopCoursePoll();
  }

  /* La modale di generazione: le stesse scelte della tab Summary (provider,
     stile, istruzioni) senza aprire la trascrizione. */
  function openSummaryDialog(tr) {
    const enabled = providers.filter((p) => p.enabled);
    const providerSel = el("select", { id: "courseSumProvider", "aria-label": t("sumdlg.provider_aria") },
      enabled.map((p) => el("option", {
        value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null,
      }, `${p.name} · ${p.model || t("common.no_model")}`)));
    // summary.style_study, summary.style_brief, summary.style_detailed
    const styleSel = el("select", { id: "courseSumStyle", "aria-label": t("sumdlg.style_aria") },
      ["study", "brief", "detailed"]
        .map((v) => el("option", {
          value: v, selected: v === (settings.summary_style || "study") ? "selected" : null,
        }, t(`summary.style_${v}`))));
    const instruction = el("input", {
      id: "courseSumInstruction", placeholder: t("sumdlg.instruction_ph"),
    });
    const error = el("p", { class: "field-error hidden" });
    const okBtn = el("button", { class: "primary", type: "button", onclick: queue }, t("sumdlg.generate"));
    okBtn.disabled = enabled.length ? null : "disabled";

    const dlg = el("dialog", { "aria-label": t("sumdlg.aria", { title: tr.title }) },
      el("h2", {}, t("sumdlg.generate")),
      el("p", { class: "hint" }, t("sumdlg.hint", { title: tr.title })),
      enabled.length
        ? el("div", { class: "stack" },
            el("div", { class: "field" },
              el("label", { for: "courseSumProvider" }, t("common.provider")), providerSel),
            el("div", { class: "field" },
              el("label", { for: "courseSumStyle" }, t("sumdlg.style")), styleSel),
            el("div", { class: "field" },
              el("label", { for: "courseSumInstruction" }, t("sumdlg.instruction")), instruction))
        : notice("warn", `${t("sumdlg.no_provider")} `,
            el("a", { href: "#/settings" }, t("onb.provider_cta"))),
      error,
      el("div", { class: "split" },
        el("button", { type: "button", onclick: () => dlg.close() }, t("common.cancel")),
        okBtn));

    async function queue() {
      okBtn.disabled = "disabled";
      okBtn.textContent = t("common.queueing");
      try {
        const res = await api(`/transcripts/${tr.id}/summary`, {
          method: "POST",
          body: {
            provider_id: providerSel.value ? Number(providerSel.value) : null,
            style: styleSel.value,
            instruction: instruction.value,
          },
        });
        dlg.close();
        toast(t("summary.queued", { title: tr.title }), "ok");
        // Il job c'è già nella risposta: lo stato in tabella cambia subito e il
        // polling parte, invece di aspettare un ricaricamento della pagina.
        summaryPending.set(tr.id, res.job || { id: null, transcript_id: tr.id, status: "queued" });
        paintSummaryCell(tr.id);
        startCoursePoll();
      } catch (err) {
        error.textContent = err.message;
        error.classList.remove("hidden");
        okBtn.disabled = null;
        okBtn.textContent = t("sumdlg.generate");
      }
    }

    document.body.append(dlg);
    // Navigare altrove con il dialogo aperto non deve lasciarlo a schermo: sta
    // fuori da `nav`, quindi il render della vista nuova non lo tocca.
    onViewTeardown(() => dlg.remove());
    dlg.addEventListener("close", () => dlg.remove());
    dlg.showModal();
  }

  /* Il nome della lezione che si legge in tabella arriva dal server come
     `display_title`: il titolo dell'ultimo riassunto senza il prefisso della
     materia ("lecture1410" non dice niente mentre si cerca una lezione), o
     il titolo della trascrizione quando il riassunto non c'è. Il nome del
     file resta nel tooltip del link, così non si perde l'aggancio con l'audio. */
  const lessonName = (t) => t.display_title || t.title;

  /* Progresso di studio: quante lezioni sono almeno ripassate. Si aggiorna
     quando cambi lo stato di una riga, senza ridisegnare la pagina. */
  const progressN = el("span", { class: "stat-n" });
  const progressStat = el("div", { class: "stat" },
    el("span", { class: "stat-k" }, t("study.progress")), progressN);
  function paintProgress() {
    const done = transcripts.filter((x) => ["reviewed", "confident"].includes(x.study_status)).length;
    progressN.textContent = `${done}/${transcripts.length}`;
    progressStat.classList.toggle("is-accent", done > 0 && done === transcripts.length);
  }
  paintProgress();

  const rows = transcripts.map((tr) => {
    const summaryHost = el("span", { class: "summary-cell" });
    summaryCells.set(tr.id, { t: tr, host: summaryHost });
    const cardsHost = el("span", { class: "summary-cell" });
    cardsCells.set(tr.id, { t: tr, host: cardsHost });
    return el("tr", { class: tr.study_status ? `study-${tr.study_status}` : "" },
      el("td", { class: "nowrap mono small muted col-num" }, tr.lecture_no ? `L${tr.lecture_no}` : ""),
      el("td", {}, el("a", { href: `#/t/${tr.id}`, title: tr.title }, lessonName(tr)),
        tr.low_coverage ? el("span", { class: "badge warn", style: "margin-left:8px",
          title: t("course.low_coverage_hint", { rate: tr.chars_per_min }) }, t("course.low_coverage")) : null,
        tr.description ? el("div", { class: "small muted" }, tr.description) : null),
      el("td", {}, studySelect(tr, () => paintProgress())),
      el("td", { class: "nowrap small" }, fmtTs(tr.duration)),
      el("td", { class: "nowrap small muted" }, tr.language ? tr.language.toUpperCase() : "—"),
      el("td", { class: "actions" },
        el("a", { class: "btn", href: `/api/transcripts/${tr.id}/export?format=txt` }, "txt"),
        summaryHost,
        settings.cards_enabled ? cardsHost : null,
        el("button", { class: "danger", onclick: () => removeTranscript(tr) }, t("common.delete"))));
  });

  // Stato iniziale delle celle: una lezione con un riassunto (o delle carte) già
  // in coda non deve mostrare "+ summary" e accodarne un secondo.
  transcripts.forEach((t) => {
    const job = jobs.find((j) => j.kind === "llm_summary" && j.transcript_id === t.id
      && ["queued", "running"].includes(j.status));
    if (job) summaryPending.set(t.id, job);
    paintSummaryCell(t.id);
    const cardJob = jobs.find((j) => j.kind === "llm_cards" && j.transcript_id === t.id
      && ["queued", "running"].includes(j.status));
    if (cardJob) cardsPending.set(t.id, cardJob);
    paintCardsCell(t.id);
  });
  if (summaryPending.size || cardsPending.size || slidesPending) startCoursePoll();

  async function removeTranscript(tr) {
    if (!await confirmDialog(t("transcript.delete_title"), t("transcript.delete_text", { name: lessonName(tr) }))) return;
    await api(`/transcripts/${tr.id}`, { method: "DELETE" });
    toast(t("transcript.deleted"), "ok");
    render();
  }

  const termForm = el("form", { class: "split", onsubmit: addTerm },
    el("input", { id: "termFind", placeholder: t("terms.find_ph"), required: "required", style: "flex:1 1 140px" }),
    el("span", { class: "muted" }, "→"),
    el("input", { id: "termReplace", placeholder: t("terms.replace_ph"), style: "flex:1 1 140px" }),
    el("label", { class: "check", style: "margin:0" },
      el("input", { id: "termAuto", type: "checkbox" }), el("span", {}, t("terms.auto"))),
    el("button", { type: "submit" }, t("common.add")));

  async function addTerm(event) {
    event.preventDefault();
    try {
      await api("/terms", {
        method: "POST",
        body: {
          project_id: projectId,
          find: byId("termFind").value,
          replace: byId("termReplace").value,
          auto: byId("termAuto").checked,
        },
      });
      toast(t("terms.saved"), "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  }

  /* Due tipi di voce: le correzioni "auto" (applicate a ogni nuova lezione e
     passate a riassunto e Whisper) e le note, che restano solo qui. Una lista
     lunga si chiude in un accordion: è consultazione, non lavoro quotidiano. */
  const termRow = (term) => el("div", { class: "split" },
    el("span", { class: "mono" }, term.find),
    el("span", { class: "muted", "aria-hidden": "true" }, "→"),
    el("span", { class: term.auto ? "mono" : "small muted" }, term.replace || "—"),
    term.auto ? el("span", { class: "badge ok" }, t("terms.auto")) : el("span", { class: "badge" }, t("terms.note")),
    el("span", { class: "spacer" }),
    el("button", { class: "ghost", title: t("terms.remove_title", { term: term.find }),
      onclick: async () => { await api(`/terms/${term.id}`, { method: "DELETE" }); render(); } }, "✕"));
  const sortedTerms = [...terms].sort((x, y) => Number(Boolean(y.auto)) - Number(Boolean(x.auto)));
  const termRows = el("div", { class: "stack" }, sortedTerms.map(termRow));
  const termList = !terms.length ? el("p", { class: "muted small" }, t("terms.empty"))
    : terms.length <= 8 ? termRows
      : el("details", { class: "adv" }, el("summary", {}, t("terms.show_all", {
        count: terms.length, auto: terms.filter((x) => x.auto).length })), termRows);

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" },
      el("a", { class: "crumb-back", href: "#/" }, icon("back"), t("nav.library")), " / ",
      el("span", { class: "here" }, project.name)),
    el("div", { class: "card" },
      el("div", { class: "split hero" },
        el("div", { class: "grow" },
          el("p", { class: "empty-kicker" }, t("route.course")),
          el("h1", {}, project.name),
          project.code ? el("span", { class: "badge" }, project.code) : null),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "primary",
            onclick: () => { location.hash = `#/p/${projectId}/new`; } }, t("course.new_lecture")),
          el("button", { type: "button", onclick: editProject }, t("common.rename")),
          el("button", { type: "button", class: "danger", onclick: removeProject }, t("course.delete")))),
      project.description ? el("p", { class: "muted" }, project.description) : null),
    transcripts.length ? el("div", { class: "stat-bar" },
      el("div", { class: "stat" },
        el("span", { class: "stat-k" }, t("course.transcripts")),
        el("span", { class: "stat-n" }, String(transcripts.length))),
      el("div", { class: "stat" },
        el("span", { class: "stat-k" }, t("course.audio_transcribed")),
        el("span", { class: "stat-n" }, totalSeconds ? fmtTs(totalSeconds) : "—")),
      el("div", { class: "stat" },
        el("span", { class: "stat-k" }, t("course.glossary_terms")),
        el("span", { class: "stat-n" }, String(terms.length))),
      progressStat) : null,
    transcripts.length ? null : emptyCourseSteps(projectId),
    el("div", { class: "card" }, askCourseCard({ projectId, providers, settings, nSummaries, token })),
    el("div", { class: "card" }, examGuideCard({ projectId, providers, settings, nSummaries,
      lectures: transcripts.map((tr) => ({ label: `L${tr.lecture_no}`, transcript_id: tr.id,
        title: tr.display_title || tr.title })),
      nExams: exams.length, jobs, token })),
    el("div", { class: "card" },
      el("h2", {}, t("course.transcripts")),
      transcripts.length ? el("table", {},
        el("thead", {}, el("tr", {},
          el("th", { class: "col-num" }, "#"), el("th", { class: "col-title" }, t("course.col_title")), el("th", {}, t("study.column")),
          el("th", {}, t("course.col_duration")), el("th", {}, t("course.col_lang")),
          el("th", {}, ""))),
        el("tbody", {}, rows)) : el("div", { class: "empty" },
        el("p", { class: "empty-kicker" }, t("course.no_transcripts")),
        el("p", {}, t("course.no_transcripts_text")),
        el("div", { class: "empty-actions" },
          el("button", { class: "primary",
            onclick: () => { location.hash = `#/p/${projectId}/new`; } }, t("course.add_first"))))),
    el("div", { class: "card" },
      slideDocsCard({ projectId, docs: deckDocs, links: slideLinks, transcripts, jobs,
        settings })),
    el("div", { class: "card" },
      slideDocsCard({ projectId, docs: exams, links: [], transcripts, jobs, settings, kind: "exam" })),
    el("div", { class: "card" },
      el("h2", {}, t("terms.title")),
      el("p", { class: "muted small" }, t("terms.intro")),
      termList, el("hr"), termForm),
  ));

  async function editProject() {
    const name = await promptDialog({
      title: t("course.rename_title"), label: t("dash.course_name"), value: project.name, okLabel: t("common.rename"),
    });
    if (name === null) return;
    if (!name) return toast(t("course.needs_name"), "err");
    try {
      await api(`/projects/${projectId}`, { method: "PATCH", body: { name } });
      toast(t("course.renamed"), "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  }

  async function removeProject() {
    const n = transcripts.length;
    const detail = n
      ? t("course.delete_text_n", { name: project.name, count: n })
      : t("course.delete_text", { name: project.name });
    if (!await confirmDialog(t("course.delete"), detail, t("course.delete"))) return;
    try {
      const res = await api(`/projects/${projectId}`, { method: "DELETE" });
      toast(t("course.deleted", { transcripts: t("common.n_transcripts", { count: res.deleted_transcripts }),
        audio: t("course.n_audio_copies", { count: res.deleted_sources }) }), "ok");
      location.hash = "#/";
    } catch (err) { toast(err.message, "err"); }
  }
}

/* ============================================================================
   Nuova lezione
   ========================================================================== */
async function viewNewLecture(nav, params, projectId, token) {
  const [project, health, { terms }] = await Promise.all([
    api(`/projects/${projectId}`), api("/health"), api(`/terms?project_id=${projectId}`),
  ]);
  const settings = (await api("/settings")).settings;
  const providers = health.providers;
  setDocTitle(`${t("route.new_lecture")} · ${project.name}`);

  /* Il menu dei modelli deve avere delle voci: un <select> vuoto invia il job con
     `model=""` e il server risponde "Unsupported model: ". Se l'avvio non le ha
     caricate (o è fallito), qui si ritenta. */
  const modelNames = await loadModels().catch(() => MODELS);
  const noModels = modelNames.length === 0;

  /* Una o più lezioni: ogni voce diventa un job di trascrizione con il proprio
     titolo, e tutte condividono le opzioni del form. Il caricamento è in serie
     (upload 1 → job 1 → upload 2 …) così la GPU parte mentre il resto sale. */
  const items = [];   // {key, name, size, file?, source?, title: <input>}
  const itemList = el("div", { class: "stack upload-list" });

  // L'input file va costruito con il suo `onchange` già agganciato: finché non è
  // nel documento `byId("fileInput")` restituisce null e l'evento di selezione
  // andrebbe perso (era il bug: il dialogo si apriva e non succedeva niente).
  const fileInput = el("input", {
    id: "fileInput", type: "file", class: "hidden", accept: "audio/*,video/*", multiple: "multiple",
    onchange: (e) => { addFiles([...e.target.files]); e.target.value = ""; },
  });
  const drop = el("div", { class: "drop" },
    el("p", {}, t("newlec.drop")),
    el("p", { class: "small" }, t("newlec.drop_hint")),
    fileInput);

  drop.addEventListener("click", (e) => {
    // Un click sull'input non deve riaprire il dialogo due volte.
    if (e.target === fileInput) return;
    fileInput.click();
  });
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    addFiles([...e.dataTransfer.files]);
  });

  const fileStatus = el("div", { class: "muted small" });
  const descInput = el("input", { id: "srcDesc", placeholder: t("newlec.desc_ph") });
  const pathInput = el("textarea", { id: "srcPath", rows: "3",
    placeholder: t("newlec.path_ph") });
  const pathStatus = el("div", { class: "muted small" }, t("newlec.path_status"));
  const progressFill = el("span");
  const progressBox = el("div", { class: "progress hidden", role: "progressbar",
    "aria-label": t("newlec.progress_aria"), "aria-valuemin": "0", "aria-valuemax": "100" }, progressFill);

  /* Uno stato può essere testo semplice (attesa) o un avviso con icona (esito).
     `kind` vuoto = testo nudo. */
  const setStatus = (node, kind, text) => {
    node.replaceChildren(...(kind ? [notice(kind, text)] : [text]));
  };

  const useFileBtn = el("button", { type: "button", class: "tab", "aria-selected": "true", onclick: () => updateMode("file") }, t("newlec.mode_file"));
  const usePathBtn = el("button", { type: "button", class: "tab", "aria-selected": "false", onclick: () => updateMode("path") }, t("newlec.mode_path"));
  const modeBar = el("div", { class: "toolbar", role: "tablist" }, useFileBtn, usePathBtn);
  const sourceCard = el("div", { class: "card" }, el("h2", {}, t("newlec.step1")), modeBar);

  const pathRow = el("div", { class: "row" },
    el("div", { class: "grow" }, pathInput),
    el("button", { type: "button", onclick: probePaths, style: "align-self:flex-start" }, t("newlec.check_add")));

  function updateMode(mode) {
    const isFile = mode === "file";
    useFileBtn.setAttribute("aria-selected", String(isFile));
    usePathBtn.setAttribute("aria-selected", String(!isFile));
    sourceCard.replaceChildren(
      el("h2", {}, t("newlec.step1")),
      modeBar,
      isFile ? el("div", {}, drop, fileStatus) : el("div", {}, pathRow, pathStatus),
      itemList, progressBox,
      el("p", { class: "hint" },
        isFile
          ? t("newlec.hint_file")
          : t("newlec.hint_path")),
    );
  }

  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });

  function addItem(entry) {
    const key = `${entry.name}|${entry.size}`;
    if (items.some((it) => it.key === key)) return false;
    items.push({ ...entry, key,
      title: el("input", { value: entry.name.replace(/\.[^.]+$/, ""), "aria-label": t("newlec.title_aria", { name: entry.name }) }) });
    return true;
  }

  let submitting = false;
  function renderItems() {
    setChildren(itemList, items.map((it, i) => el("div", { class: "upload-item" },
      el("span", { class: "muted small tabnum" }, `${i + 1}.`),
      el("div", { class: "grow" }, it.title),
      el("span", { class: "muted small" },
        `${it.name} · ${fmtBytes(it.size)}${it.source && !it.file ? ` · ${t("newlec.in_place")}` : ""}`),
      it.dup ? el("span", { class: "badge warn", title: t("newlec.dup_title", { id: it.dup }) }, t("newlec.dup")) : null,
      el("button", { type: "button", class: "ghost", "aria-label": t("newlec.remove_aria", { name: it.name }),
        disabled: submitting ? "disabled" : null,
        onclick: () => { items.splice(items.indexOf(it), 1); renderItems(); } }, t("common.remove")))));
    syncNote();
  }

  function addFiles(files) {
    const media = files.filter((f) => /^(audio|video)\//.test(f.type) || /\.(mp3|wav|m4a|ogg|flac|aac|opus|wma|mp4|mkv|mov|webm)$/i.test(f.name));
    const skipped = files.length - media.length;
    const added = media.sort(byName).filter((f) => addItem({ name: f.name, size: f.size, file: f })).length;
    setStatus(fileStatus, added ? "ok" : "warn",
      `${t("newlec.files_added", { count: added })}${skipped ? ` · ${t("newlec.skipped", { count: skipped })}` : ""}.`);
    renderItems();
  }

  async function addPath(path) {
    const created = await api(`/projects/${projectId}/sources`, { method: "POST", body: { path } });
    const src = created.source;
    return addItem({ name: src.name, size: src.bytes || 0, source: src, dup: created.duplicate_of });
  }

  async function probePaths() {
    const lines = pathInput.value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    if (!lines.length) {
      pathInput.setAttribute("aria-invalid", "true");
      pathInput.focus();
      return;
    }
    pathInput.removeAttribute("aria-invalid");
    setStatus(pathStatus, null, t("newlec.checking"));
    let added = 0;
    const errors = [];
    for (const line of lines) {
      try {
        // Una cartella si espande nei suoi file; se non lo è, è un file.
        const folder = await api(`/media/folder?path=${encodeURIComponent(line)}`).catch(() => null);
        const paths = folder ? folder.files.map((f) => f.path) : [line];
        for (const p of paths) {
          try { if (await addPath(p)) added += 1; }
          catch (err) { errors.push(`${p}: ${err.message}`); }
        }
      } catch (err) { errors.push(`${line}: ${err.message}`); }
    }
    items.sort(byName);
    renderItems();
    setStatus(pathStatus, errors.length ? "warn" : "ok",
      `${t("newlec.recordings_added", { count: added })}${errors.length ? ` · ${t("newlec.rejected", { list: errors.join(" · ") })}` : ""}`);
    if (!errors.length) pathInput.value = "";
  }

  async function uploadFile(file) {
    const form = new FormData();
    form.append("file", file, file.name);
    progressFill.style.width = "0%";
    const res = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/projects/${projectId}/upload`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const pct = (e.loaded / e.total) * 100;
          progressFill.style.width = `${pct}%`;
          progressBox.setAttribute("aria-valuenow", String(Math.round(pct)));
        }
      };
      xhr.onload = () => {
        try {
          const data = JSON.parse(xhr.responseText || "{}");
          if (xhr.status >= 400) reject(new Error(data.detail || `HTTP ${xhr.status}`));
          else resolve(data);
        } catch (err) { reject(err); }
      };
      xhr.onerror = () => reject(new Error(t("newlec.upload_failed")));
      xhr.send(form);
    });
    return res.source;
  }

  const opts = {
    // Un menu senza voci non è un menu: `noModels` lo dice all'utente e
    // `submit()` si ferma prima di mandare un modello vuoto al server.
    model: flagSelect({ id: "optModel", invalid: noModels,
      options: modelOptions(modelNames, ENGLISH_ONLY), value: settings.default_model }),
    language: flagSelect({ id: "optLang",
      options: languageOptions(LANGUAGES), value: settings.default_language || "en" }),
    device: el("select", { id: "optDevice" },
      ["auto", "cuda", "cpu"].map((d) => el("option", { value: d, selected: d === (settings.default_device || "auto") ? "selected" : null }, d))),
    compute: el("select", { id: "optCompute" },
      ["auto", "float16", "int8", "int8_float16", "float32", "bfloat16"].map((c) => el("option", { value: c, selected: c === (settings.default_compute_type || "auto") ? "selected" : null }, c))),
    beam: el("input", { id: "optBeam", type: "number", min: "1", max: "20", value: "5" }),
    prompt: el("input", { id: "optPrompt", placeholder: t("newlec.prompt_ph") }),
    wordTs: el("input", { id: "optWordTs", type: "checkbox" }),
    noVad: el("input", { id: "optNoVad", type: "checkbox" }),
  };

  // Il riassunto è il motivo per cui si trascrive: acceso di default.
  const summaryToggle = el("input", { id: "optSummary", type: "checkbox",
    checked: providers.some((p) => p.enabled) ? "checked" : null });
  const summaryProvider = el("select", { id: "optSummaryProvider" },
    providers.filter((p) => p.enabled).map((p) => el("option", { value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null }, `${p.name} · ${p.model || t("common.no_model")}`)));
  // summary.style_study_default, summary.style_brief, summary.style_detailed
  const summaryStyle = el("select", { id: "optSummaryStyle" },
    [["study", "summary.style_study_default"], ["brief", "summary.style_brief"], ["detailed", "summary.style_detailed"]]
      .map(([v, key]) => el("option", { value: v, selected: v === (settings.summary_style || "study") ? "selected" : null }, t(key))));
  const summaryInstruction = el("input", { id: "optSummaryInstruction", placeholder: t("newlec.instruction_ph") });
  const autoTerms = terms.filter((t) => t.auto);
  const submitNote = el("span", { class: "muted small" });
  const submitBtn = el("button", { class: "primary", type: "submit" }, t("newlec.start"));
  /* Il perché di un menu vuoto va detto dove serve — accanto al menu — e non
     solo nel toast al momento dell'invio. */
  const modelNotice = noModels
    ? el("p", { class: "notice err" }, t("newlec.no_models"))
    : null;

  const form = el("form", { class: "stack", onsubmit: submit },
    sourceCard,
    el("div", { class: "card" }, el("h2", {}, t("newlec.step2")),
      el("div", { class: "field" }, el("label", { for: "srcDesc" }, t("dash.description")), descInput),
      el("p", { class: "hint" }, t("newlec.title_hint"))),
    el("div", { class: "card" }, el("h2", {}, t("newlec.step3")),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "optModel" }, t("opt.model")), opts.model.el),
        el("div", { class: "grow field" }, el("label", { for: "optLang" }, t("opt.language")), opts.language.el)),
      modelNotice,
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "optDevice" }, t("opt.device")), opts.device),
        el("div", { class: "grow field" }, el("label", { for: "optCompute" }, t("opt.compute")), opts.compute),
        el("div", { class: "grow field" }, el("label", { for: "optBeam" }, t("opt.beam")),
          opts.beam,
          el("p", { class: "hint" }, t("opt.beam_hint")))),
      el("details", { class: "adv" }, el("summary", {}, t("settings.advanced")),
        el("div", { class: "field", style: "margin-top:10px" },
          el("label", { for: "optPrompt" }, t("opt.prompt")),
          opts.prompt,
          el("p", { class: "hint" }, t("opt.prompt_hint"))),
        el("label", { class: "check" }, opts.wordTs, el("span", {}, t("opt.word_ts"))),
        el("label", { class: "check" }, opts.noVad, el("span", {}, t("opt.no_vad")))),
      el("p", { class: "hint" }, t("opt.ts_hint"))),
    el("div", { class: "card" }, el("h2", {}, t("newlec.step4")),
      providers.length ? el("div", { class: "stack" },
        el("label", { class: "check" }, summaryToggle, el("span", {}, t("newlec.auto_summary"))),
        el("div", { class: "row" },
          el("div", { class: "grow field" }, el("label", { for: "optSummaryProvider" }, t("common.provider")), summaryProvider),
          el("div", { class: "grow field" }, el("label", { for: "optSummaryStyle" }, t("sumdlg.style")), summaryStyle)),
        el("div", { class: "field" },
          el("label", { for: "optSummaryInstruction" }, t("newlec.extra")),
          summaryInstruction),
        el("p", { class: "hint" }, t("newlec.summary_hint")),
      ) : el("p", { class: "muted" }, t("newlec.no_provider"))),
    el("div", { class: "card" },
      el("div", { class: "split" }, submitBtn, submitNote),
      el("p", { class: "hint" }, t("newlec.queue_hint"))),
    );

  // Stato iniziale: una sola sorgente visibile (file), l'altra raggiungibile
  // dallo switch in cima alla card.
  updateMode("file");

  /* La riga accanto al pulsante dice cosa manca per partire, invece di lasciare
     il pulsante muto e l'utente a indovinare. */
  function syncNote() {
    const bits = [];
    if (!items.length) bits.push(t("newlec.note_choose"));
    else bits.push(t("newlec.note_ready", { count: items.length }));
    if (items.some((it) => it.file)) bits.push(t("newlec.note_upload"));
    if (noModels) bits.push(t("newlec.note_no_models"));
    if (autoTerms.length) bits.push(t("newlec.note_terms", { count: autoTerms.length }));
    submitNote.textContent = bits.join(" · ");
    submitBtn.textContent = items.length > 1 ? t("newlec.start_n", { count: items.length }) : t("newlec.start");
  }
  syncNote();

  async function submit(event) {
    event.preventDefault();
    askNotifyPermission();
    if (!items.length) {
      setStatus(fileStatus, "err", t("newlec.choose_first"));
      drop.classList.add("over");
      setTimeout(() => drop.classList.remove("over"), 900);
      return;
    }
    const untitled = items.find((it) => !it.title.value.trim());
    if (untitled) {
      untitled.title.setAttribute("aria-invalid", "true");
      untitled.title.focus();
      return;
    }
    /* Senza modello il server risponde 422 "Unsupported model: " e l'utente non
       capisce cosa manca: meglio fermarsi qui, dicendolo. */
    if (!opts.model.value) {
      setStatus(fileStatus, "err", t("newlec.no_model_submit"));
      opts.model.focus();
      return;
    }

    submitBtn.disabled = true;
    const queued = [];
    // Durante l'invio la lista non si tocca: togliere una voce mentre sale
    // faceva saltare quella dopo.
    submitting = true;
    items.forEach((it) => { it.title.disabled = true; });
    renderItems();
    // Si consumano le voci una alla volta: se una fallisce, quelle già in coda
    // restano in coda e la lista mostra solo ciò che manca.
    while (items.length) {
      const it = items[0];
      submitBtn.textContent = t("newlec.queuing", { n: queued.length + 1, total: queued.length + items.length });
      try {
        if (!it.source) {
          progressBox.classList.remove("hidden");
          setStatus(fileStatus, null, t("newlec.uploading", { name: it.name }));
          it.source = await uploadFile(it.file);
        }
        const job = await api("/jobs", {
          method: "POST",
          body: {
            project_id: projectId,
            source_id: it.source.id,
            title: it.title.value.trim(),
            description: descInput.value,
            model: opts.model.value,
            language: opts.language.value,
            device: opts.device.value,
            compute_type: opts.compute.value,
            beam_size: Number(opts.beam.value) || 5,
            word_timestamps: opts.wordTs.checked,
            no_vad: opts.noVad.checked,
            initial_prompt: opts.prompt.value,
            // Il job del riassunto nasce quando la trascrizione esiste già,
            // così trova il testo pronto.
            run_summary: summaryToggle.checked,
            summary_provider_id: summaryProvider.value ? Number(summaryProvider.value) : null,
            summary_style: summaryStyle.value,
            summary_instruction: summaryInstruction.value,
          },
        });
        queued.push(job.job.id);
        items.shift();
        renderItems();
      } catch (err) {
        progressBox.classList.add("hidden");
        setStatus(fileStatus, "err", `${it.name}: ${err.message}`);
        toast(`${it.name}: ${err.message}`, "err");
        submitBtn.disabled = false;
        submitting = false;
        items.forEach((x) => { x.title.disabled = false; });
        renderItems();
        syncNote();
        if (queued.length) toast(t("newlec.already_queued", { count: queued.length }), "ok");
        return;
      }
    }
    progressBox.classList.add("hidden");
    toast(t("newlec.queued", { count: queued.length }), "ok");
    location.hash = `#/jobs?focus=${queued[0]}`;
  }

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" },
      el("a", { class: "crumb-back", href: "#/" }, icon("back"), t("nav.library")), " / ",
      el("a", { href: `#/p/${projectId}` }, project.name), " / ",
      el("span", { class: "here" }, t("route.new_lecture"))),
    el("div", { class: "card" },
      el("p", { class: "empty-kicker" }, `${t("route.course")} · ${project.name}`),
      el("h1", {}, t("newlec.title")),
      el("p", { class: "muted small hero-note" }, t("newlec.intro"))),
    form));
}

/* ============================================================================
   Coda dei job
   ========================================================================== */
/* Le etichette si risolvono quando servono (t() non è pronto al caricamento):
   jobkind.transcribe, jobkind.llm_summary, jobkind.llm_cards, jobkind.parse_slides,
   jobkind.embed_match, jobkind.llm_guide, jobkind.llm_fix. */
const JOB_KINDS = ["transcribe", "llm_summary", "llm_cards", "parse_slides", "embed_match", "llm_guide", "llm_fix"];
const jobKindLabel = (kind) => (JOB_KINDS.includes(kind) ? t(`jobkind.${kind}`) : kind);
/* Stato di un job a schermo (il valore resta quello del server):
   jobstatus.queued, jobstatus.pending, jobstatus.running, jobstatus.done,
   jobstatus.failed, jobstatus.interrupted, jobstatus.cancelled. */
const JOB_STATUSES = ["queued", "pending", "running", "done", "failed", "interrupted", "cancelled"];
const jobStatusLabel = (status) => (JOB_STATUSES.includes(status) ? t(`jobstatus.${status}`) : status);

/* I messaggi dei job li scrive il server, in inglese (servono anche ai log).
   A schermo si traducono pezzo per pezzo: sono frasi fisse con numeri dentro,
   separate da " . ". Un pezzo che non si riconosce resta com'è. */
const JOB_MESSAGES = [
  [/^(?<count>\d+) segments$/, "jobmsg.segments"],
  [/^(?<count>\d+) segments (?<at>[\d:]+) of (?<total>[\d:]+)$/, "jobmsg.segments_at"],
  [/^(?<at>[\d:]+) of (?<total>[\d:]+)$/, "jobmsg.at_of"],
  [/^starting$/, "jobmsg.starting"],
  [/^requeued$/, "jobmsg.requeued"],
  [/^failed$/, "jobmsg.failed"],
  [/^cancelled before start$/, "jobmsg.cancelled_before_start"],
  [/^server stopped: retry to resume$/i, "jobmsg.server_stopped"],
  [/^Server stopped while this job was running$/, "jobmsg.server_stopped"],
  [/^preparing the transcript$/, "jobmsg.preparing"],
  [/^asking (?<model>.+) for ~(?<count>\d+) words$/, "jobmsg.asking_words"],
  [/^asking (?<model>.+) for up to (?<count>\d+) cards$/, "jobmsg.asking_cards"],
  [/^(?<count>\d+) words$/, "jobmsg.words"],
  [/^(?<count>\d+) words of (?<target>\d+)$/, "jobmsg.words_of"],
  [/^(?<count>\d+) of (?<target>\d+) words$/, "jobmsg.words_of"],
  [/^slide matching queued$/, "jobmsg.matching_queued"],
  [/^page (?<i>\d+)\/(?<n>\d+)$/, "jobmsg.page"],
  [/^VLM page (?<i>\d+)\/(?<n>\d+)$/, "jobmsg.vlm_page"],
  [/^document (?<i>\d+)\/(?<n>\d+)$/, "jobmsg.document"],
  [/^(?<count>\d+) pages$/, "jobmsg.pages"],
  [/^(?<count>\d+) complex$/, "jobmsg.complex"],
  [/^(?<count>\d+) boilerplate lines removed$/, "jobmsg.boilerplate"],
  [/^(?<count>\d+) pages rendered by VLM$/, "jobmsg.vlm_pages"],
  [/^(?<count>\d+) page ranges$/, "jobmsg.page_ranges"],
  [/^skipped: Ollama is not running$/, "jobmsg.ollama_skipped"],
  [/^(?<count>\d+) cards$/, "jobmsg.cards"],
  [/^(?<count>\d+) replaced$/, "jobmsg.replaced"],
  [/^already up to date: same material, no call made$/, "jobmsg.cards_cached"],
  [/^(?<lectures>\d+) lectures, (?<exams>\d+) exam pages → (?<model>.+)$/, "jobmsg.guide_asking"],
  [/^(?<count>\d+) words from (?<lectures>\d+) lectures and (?<exams>\d+) exam pages$/, "jobmsg.guide_done_exams"],
  [/^(?<count>\d+) words from (?<lectures>\d+) lectures$/, "jobmsg.guide_done"],
  [/^TRUNCATED at the token limit$/, "jobmsg.truncated"],
  [/^summary skipped: no provider$/, "jobmsg.summary_skipped"],
];

/* Le note della cronologia le scrive il server in inglese: stesse regole dei
   messaggi dei job, frasi fisse con numeri dentro. */
const REVISION_TEXTS = [
  [/^(?<style>\w+) summary: (?<count>\d+) words \(target (?<target>\d+)\) from (?<model>.+)$/, "rev.summary"],
  [/^(?<count>\d+) cards from (?<model>.+)$/, "rev.cards"],
  [/^Edited in the web editor$/, "rev.edited"],
  [/^Automatic term replacements on import$/, "rev.glossary"],
  [/^(?<count>\d+) replacements applied$/, "rev.replacements"],
];
function revisionText(text) {
  for (const [re, key] of REVISION_TEXTS) {
    const m = String(text).match(re);
    if (m) {
      const g = m.groups || {};
      return t(key, { ...g, count: Number(g.count || 0), style: g.style ? summaryStyleLabel(g.style) : "" });
    }
  }
  return text;
}

function jobMessage(message) {
  if (!message) return "";
  return String(message).split(" . ").map((part) => {
    const text = part.trim();
    for (const [re, key] of JOB_MESSAGES) {
      const m = text.match(re);
      if (m) return t(key, { ...(m.groups || {}), count: Number(m.groups?.count ?? 0) });
    }
    return text;
  }).filter(Boolean).join(" · ");
}

async function viewJobs(nav, params, token) {
  const focus = Number(params.get("focus") || 0);
  const container = el("div", { class: "stack" });
  // Filtro: "recent" (default) nasconde completati e falliti più vecchi di 7
  // giorni, che altrimenti riempivano la pagina di matching falliti di settimane fa.
  let filter = "recent";
  // Il polling ridisegna tutto ogni 1.5 s: quali accordion sono aperti va
  // ricordato fuori dal DOM, altrimenti si richiuderebbero da soli.
  const openGroups = new Set();
  let focusSeen = false;

  const statusClass = (s) => (
    s === "done" ? "ok"
      : s === "failed" ? "danger"
        : s === "running" ? "accent"
          : ""
  );

  const renderJobs = async () => {
    const { jobs, queue, queue_eta_seconds: queueEta } = await api("/jobs?limit=100");
    const weekAgo = Date.now() / 1000 - 7 * 86400;

    const counts = {
      running: jobs.filter((j) => j.status === "running").length,
      queued: jobs.filter((j) => ["queued", "pending"].includes(j.status)).length,
      done: jobs.filter((j) => j.status === "done").length,
      failed: jobs.filter((j) => ["failed", "interrupted", "cancelled"].includes(j.status)).length,
    };

    /* Una entry per lezione: la trascrizione e i passi che ne seguono (riassunto,
       carte…) condividono `transcript_id`; parsing e matching di un deck
       condividono `doc_id`. Il resto resta da solo. */
    const groupKey = (j) => (j.transcript_id ? `t${j.transcript_id}`
      : j.payload?.doc_id ? `d${j.payload.doc_id}` : `j${j.id}`);
    const groups = new Map();
    // `jobs` arriva dal più recente: si ribalta così i passi stanno in ordine.
    [...jobs].reverse().forEach((j) => {
      const key = groupKey(j);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(j);
    });
    const isActive = (s) => ["running", "queued", "pending"].includes(s);
    const isFailed = (s) => ["failed", "interrupted", "cancelled"].includes(s);
    const list = [...groups.entries()]
      .map(([key, steps]) => ({ key, steps, last: Math.max(...steps.map((s) => s.id)) }))
      .filter((g) => (filter === "active" ? g.steps.some((s) => isActive(s.status)) : true))
      .filter((g) => filter === "all"
        || g.steps.some((s) => isActive(s.status))
        || (filter === "recent" && g.steps.some((s) => (s.finished_at || s.created_at) > weekAgo)))
      .sort((a, b) => b.last - a.last);

    const bar = (j, label) => {
      const running = j.status === "running";
      const pct = Math.round((j.progress || 0) * 100);
      const cls = `progress${j.status === "done" ? " done" : isFailed(j.status) ? " failed" : ""}`;
      return el("div", {
        class: running && pct === 0 ? `${cls} indeterminate` : cls,
        role: "progressbar", "aria-label": label,
        "aria-valuemin": "0", "aria-valuemax": "100",
        "aria-valuenow": running && pct === 0 ? null : String(pct),
      }, el("span", { style: `width:${pct}%` }));
    };

    const stepRow = (j) => {
      const running = j.status === "running";
      const pct = Math.round((j.progress || 0) * 100);
      return el("div", { class: "job-step", id: `job-${j.id}` },
        el("div", { class: "split" },
          el("strong", {}, jobKindLabel(j.kind)),
          el("span", { class: `badge ${statusClass(j.status)}` }, jobStatusLabel(j.status)),
          j.speed && running && j.kind === "transcribe" ? el("span", { class: "badge" }, t("jobs.realtime", { speed: j.speed })) : null,
          j.cost_usd ? el("span", { class: "badge" }, `$${Number(j.cost_usd).toFixed(4)}`) : null,
          el("span", { class: "spacer" }),
          (running && ["transcribe", "parse_slides"].includes(j.kind)) || ["queued", "pending"].includes(j.status)
            ? el("button", { class: "danger", onclick: () => cancel(j.id) }, t("common.cancel")) : null,
          isFailed(j.status) ? el("button", { onclick: () => retry(j.id) }, t("jobs.retry")) : null),
        bar(j, t("jobs.progress_aria", { id: j.id })),
        el("div", { class: "split small muted" },
          el("span", { class: "tabnum" }, running && pct === 0 ? t("jobs.starting") : `${pct}%`),
          j.message ? el("span", {}, jobMessage(j.message)) : null,
          j.eta_seconds && running ? el("span", {}, t("jobs.step_eta", { time: fmtTs(j.eta_seconds) })) : null,
          ["queued", "pending"].includes(j.status) ? el("span", {}, t("jobs.waiting")) : null,
          el("span", { class: "spacer" }),
          el("span", {}, `#${j.id} · ${fmtDate(j.created_at)}`)),
        j.error ? notice("err", humanError(j.error)) : null);
    };

    const cards = list.map(({ key, steps }) => {
      const root = steps.find((s) => s.kind === "transcribe") || steps[0];
      const current = steps.find((s) => s.status === "running")
        || steps.find((s) => isActive(s.status)) || steps[steps.length - 1];
      // Conta l'ultimo passo di ogni tipo: un riassunto fallito e poi rifatto
      // con successo non deve lasciare la lezione rossa per sempre.
      const lastOfKind = [...new Map(steps.map((s) => [s.kind, s])).values()];
      const status = lastOfKind.some((s) => s.status === "running") ? "running"
        : lastOfKind.some((s) => isActive(s.status)) ? "queued"
          : lastOfKind.some((s) => s.status === "failed" || s.status === "interrupted") ? "failed"
            : current.status;
      const title = root.transcript_title || root.payload?.title
        || (root.payload?.doc_id ? t("jobs.slides_doc", { id: root.payload.doc_id }) : jobKindLabel(root.kind));
      const transcriptId = steps.find((s) => s.transcript_id)?.transcript_id;
      if (!focusSeen && steps.some((s) => s.id === focus)) { focusSeen = true; openGroups.add(key); }
      const box = el("details", {
        class: `card job-group${isFailed(status) ? " has-error" : ""}`,
        open: openGroups.has(key) ? "open" : null,
        ontoggle: (e) => { if (e.currentTarget.open) openGroups.add(key); else openGroups.delete(key); },
      },
      el("summary", {},
        el("div", { class: "job-head" },
          el("div", { class: "split" },
            el("h3", { style: "margin:0" }, title),
            root.project_name ? el("span", { class: "muted small" }, root.project_name) : null,
            el("span", { class: `badge ${statusClass(status)}` }, jobStatusLabel(status)),
            el("span", { class: "spacer" }),
            el("span", { class: "muted small" },
              steps.map((s) => jobKindLabel(s.kind)).join(" → ")),
            transcriptId ? el("a", { class: "btn", href: `#/t/${transcriptId}`,
              onclick: (e) => e.stopPropagation() }, t("jobs.open")) : null),
          bar(current, t("jobs.group_progress", { title })),
          el("div", { class: "split small muted" },
            el("span", {}, `${jobKindLabel(current.kind)}: ${jobMessage(current.message) || jobStatusLabel(current.status)}`),
            el("span", { class: "spacer" }),
            el("span", {}, fmtDate(root.created_at))))),
      el("div", { class: "stack job-steps" }, steps.map(stepRow)));
      return box;
    });

    if (!isCurrent(token)) return;
    container.replaceChildren(
      el("div", { class: "card" },
        el("div", { class: "split hero" },
          el("div", { class: "grow" },
            el("p", { class: "empty-kicker" }, t("jobs.kicker")),
            el("h1", {}, t("nav.jobs")),
            el("p", { class: "muted small hero-note" }, t("jobs.intro"))),
          el("div", { class: "actions" },
            el("button", { onclick: renderJobs }, t("jobs.refresh"))))),
      el("div", { class: "stat-bar" },
        el("div", { class: `stat ${counts.running ? "is-accent" : "is-idle"}` },
          el("span", { class: "stat-k" }, t("jobs.running")),
          el("span", { class: "stat-n" }, String(counts.running))),
        el("div", { class: `stat ${queue ? "is-accent" : ""}` },
          el("span", { class: "stat-k" }, t("jobs.queued")),
          el("span", { class: "stat-n" }, String(queue)),
          queueEta ? el("span", { class: "stat-k", title: t("jobs.eta_hint") },
            t("jobs.eta", { time: fmtTs(queueEta) })) : null),
        el("div", { class: "stat" },
          el("span", { class: "stat-k" }, t("jobs.done")),
          el("span", { class: "stat-n" }, String(counts.done))),
        el("div", { class: `stat ${counts.failed ? "is-danger" : "is-idle"}` },
          el("span", { class: "stat-k" }, t("jobs.failed")),
          el("span", { class: "stat-n" }, String(counts.failed)))),
      el("div", { class: "split" },
        el("div", { class: "toolbar", role: "tablist", "aria-label": t("jobs.filter_aria") },
          [["recent", t("jobs.filter_recent")], ["active", `${t("jobs.filter_active")} · ${counts.running + counts.queued}`],
            ["all", `${t("jobs.filter_all")} · ${groups.size}`]].map(([key, label]) => el("button", {
            class: "tab", type: "button",
            "aria-selected": filter === key ? "true" : "false",
            onclick: (e) => { filter = key; setFilter(e.currentTarget); },
          }, label))),
        el("span", { class: "spacer" }),
        el("span", { class: "muted small" }, t("jobs.auto_refresh"))),
      list.length ? el("div", { class: "stack" }, cards)
        : el("div", { class: "empty" },
          el("p", { class: "empty-kicker" }, filter === "active" ? t("jobs.nothing_running") : t("jobs.queue_empty")),
          el("p", {}, filter === "active"
            ? t("jobs.nothing_running_text")
            : t("jobs.empty_text")),
          el("div", { class: "empty-actions" },
            el("a", { class: "btn", href: "#/" }, t("jobs.go_library")))));
  };

  // Il filtro è locale: non rifà la richiesta, ridisegna solo la lista.
  function setFilter(btn) {
    $$(".tab", btn.parentElement).forEach((b) => b.setAttribute("aria-selected", "false"));
    btn.setAttribute("aria-selected", "true");
    renderJobs().catch((err) => toast(err.message, "err"));
  }

  async function cancel(id) {
    try { await api(`/jobs/${id}/cancel`, { method: "POST" }); toast(t("jobs.cancelling")); renderJobs(); }
    catch (err) { toast(err.message, "err"); }
  }
  async function retry(id) {
    try { await api(`/jobs/${id}/retry`, { method: "POST" }); toast(t("jobs.requeued"), "ok"); renderJobs(); }
    catch (err) { toast(err.message, "err"); }
  }

  if (!isCurrent(token)) return;
  nav.replaceChildren(container);
  await renderJobs();
  if (!isCurrent(token)) return;
  if (focus) {
    const target = byId(`job-${focus}`);
    target?.scrollIntoView({ block: "center", behavior: "smooth" });
    // Il job appena accodato si riconosce: bordo acceso per un paio di secondi.
    target?.animate?.(
      [{ boxShadow: "0 0 0 0 var(--accent-line)" }, { boxShadow: "0 0 0 3px transparent" }],
      { duration: 1600, easing: "ease-out" },
    );
  }
  // Il polling si spegne da solo se la vista non è più quella a schermo: il
  // token viene incrementato da render() a ogni navigazione.
  currentPoll = setInterval(() => {
    if (!isCurrent(token)) { stopPoll(); return; }
    renderJobs().catch(() => {});
    refreshJobBadge();
  }, 1500);
}

/* ============================================================================
   Trascrizione
   ========================================================================== */
/* Modalità lettura: il riassunto a tutto schermo, con l'indice delle sezioni
   a lato (in colonna sotto i 900px). Esc chiude. */
function readingMode(summary) {
  const doc = el("article", { class: "md reading-doc", html: renderMarkdown(summary.markdown || "") });
  const heads = [...doc.querySelectorAll("h2")];
  heads.forEach((h, i) => { h.id = `sec-${i}`; });
  const toc = el("nav", { class: "reading-toc", "aria-label": t("summary.toc") },
    el("p", { class: "eyebrow" }, t("summary.toc")),
    heads.map((h) => el("a", { href: `#${h.id}`, onclick: (e) => {
      e.preventDefault(); h.scrollIntoView({ behavior: "smooth", block: "start" });
    } }, h.textContent)));
  const dlg = el("dialog", { class: "reading", "aria-label": summary.title || t("summary.default_title") },
    el("div", { class: "reading-bar" },
      el("strong", {}, summary.title || ""), el("span", { class: "spacer" }),
      el("button", { type: "button", onclick: () => window.print() }, t("summary.print")),
      el("button", { type: "button", onclick: () => dlg.close() }, t("common.close"))),
    el("div", { class: "reading-body" }, heads.length > 2 ? toc : null, doc));
  document.body.append(dlg);
  dlg.showModal();
  // L'indice segue la lettura: è attiva l'ultima sezione il cui titolo è già
  // passato sotto la barra. Su scroll, un calcolo per frame al massimo.
  const body = dlg.querySelector(".reading-body");
  const links = [...toc.querySelectorAll("a")];
  let pending = false;
  const spy = () => {
    pending = false;
    const top = body.getBoundingClientRect().top + 80;
    let current = heads[0];
    for (const h of heads) { if (h.getBoundingClientRect().top <= top) current = h; else break; }
    links.forEach((a) => a.classList.toggle("active", current && a.getAttribute("href") === `#${current.id}`));
  };
  body.addEventListener("scroll", () => { if (!pending) { pending = true; requestAnimationFrame(spy); } }, { passive: true });
  spy();
  dlg.addEventListener("close", () => dlg.remove());
}

/* Stile di un riassunto a schermo: summary.style_study, summary.style_brief,
   summary.style_detailed (il valore resta quello del server). */
const summaryStyleLabel = (style) => (["study", "brief", "detailed"].includes(style) ? t(`summary.style_${style}`) : style);
/* Tipo di revisione a schermo: revkind.manual_edit, revkind.glossary,
   revkind.summary, revkind.cards. */
const revisionKindLabel = (kind) => (["manual_edit", "glossary", "summary", "cards"].includes(kind) ? t(`revkind.${kind}`) : kind);

async function viewTranscript(nav, params, transcriptId, token) {
  const query = params.get("q") || "";
  // `settings` non è globale: ogni vista che lo usa deve chiederlo. Senza,
  // `settings.default_provider_id` esplode già al render e la pagina non si apre.
  const [lecture, { jobs }, providersRes, settingsRes] = await Promise.all([
    api(`/transcripts/${transcriptId}`), api(`/jobs?limit=20`),
    api("/providers"), api("/settings"),
  ]);
  const providers = providersRes.providers;
  const settings = settingsRes.settings;

  const textArea = el("textarea", { class: "editor", spellcheck: "false", "aria-label": t("tr.text_aria") });
  textArea.value = lecture.text;
  const saveState = el("span", { class: "small muted tabnum" }, t("tr.saved"));
  let dirty = false;
  let timer = null;

  const words = () => textArea.value.split(/\s+/).filter(Boolean).length;
  const wordCount = el("span", { class: "muted small tabnum" }, t("tr.words", { count: words() }));

  const save = async () => {
    if (!dirty) return;
    saveState.textContent = t("tr.saving");
    try {
      const updated = await api(`/transcripts/${transcriptId}`, { method: "PATCH", body: { text: textArea.value } });
      lecture.text = updated.text;
      lecture.segments = updated.segments;
      dirty = false;
      saveState.textContent = t("tr.saved_at", { time: new Date().toLocaleTimeString(I18N.lang) });
    } catch (err) { saveState.textContent = t("tr.save_failed"); toast(err.message, "err"); }
  };

  textArea.addEventListener("input", () => {
    dirty = true;
    saveState.textContent = t("tr.unsaved");
    wordCount.textContent = t("tr.words", { count: words() });
    clearTimeout(timer);
    timer = setTimeout(save, 1200);
  });
  textArea.addEventListener("blur", save);

  /* Ctrl/Cmd+S salva. Il listener sta su document, quindi va tolto quando si
     lascia la vista: prima restava agganciato per sempre e dopo dieci
     trascrizioni un solo Ctrl+S lanciava dieci PATCH identiche. */
  const onKey = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
  };
  document.addEventListener("keydown", onKey);
  onViewTeardown(() => {
    document.removeEventListener("keydown", onKey);
    clearTimeout(timer);
  });

  const tabContent = el("div", { class: "stack" },
    el("div", { class: "split" },
      el("span", { class: "muted small" }, t("tr.editor_hint")),
      el("span", { class: "spacer" }),
      saveState),
    textArea,
    el("div", { class: "split" },
      el("button", { class: "primary", onclick: save }, t("tr.save_now")),
      el("button", { onclick: copyAll }, t("tr.copy_all")),
      el("span", { class: "spacer" }),
      wordCount,
      el("span", { class: "muted small" }, t("tr.segments", { count: (lecture.segments || []).length }))));

  async function copyAll() {
    try { await navigator.clipboard.writeText(textArea.value); toast(t("common.copied"), "ok"); }
    catch { toast(t("common.clipboard_unavailable"), "err"); }
  }

  const segRows = (lecture.segments || []).map((s, i) => {
    const norm = (s.text || "").toLowerCase();
    const hit = query && norm.includes(query.toLowerCase());
    return el("div", { class: `seg ${hit ? "hit" : ""}`, "data-i": String(i) },
      // Il timestamp è un comando: copia il minutaggio da citare altrove.
      el("button", {
        type: "button", class: "ts-btn", title: t("tr.copy_stamp", { stamp: fmtTs(s.start) }),
        onclick: () => copyStamp(fmtTs(s.start)),
      }, fmtTs(s.start)),
      el("p", { html: query ? highlight(s.text, query) : esc(s.text) }));
  });

  async function copyStamp(stamp) {
    try { await navigator.clipboard.writeText(stamp); toast(t("tr.stamp_copied", { stamp }), "ok"); }
    catch { toast(t("common.clipboard_unavailable"), "err"); }
  }

  const segmentCount = (lecture.segments || []).length;
  const tabSegments = el("div", {},
    el("div", { class: "split", style: "margin-bottom:10px" },
      el("span", { class: "muted small tabnum" }, t("tr.segments", { count: segmentCount })),
      el("span", { class: "muted small" }, t("tr.click_stamp")),
      el("span", { class: "spacer" }),
      el("a", { class: "btn", href: `/api/transcripts/${transcriptId}/export?format=segments` }, t("tr.download_ts"))),
    el("div", { class: "segments" }, segRows.length ? segRows
      : el("div", { class: "empty", style: "border:0;background:none" },
        el("p", { class: "empty-kicker" }, t("tr.no_segments")),
        el("p", {}, t("tr.no_segments_text")))));

  const detailsCard = el("div", { class: "card" },
    el("h2", {}, t("tr.details")),
    el("table", { class: "details-table" }, el("tbody", {},
      row(t("route.course"), lecture.project_name || "—"),
      row(t("opt.language"), (lecture.language || "—").toUpperCase()),
      row(t("course.col_model"), lecture.model || "—"),
      row(t("opt.device"), lecture.device || "—"),
      row(t("course.col_duration"), fmtTs(lecture.duration)),
      row(t("tr.created"), fmtDate(lecture.created_at)),
      row(t("course.col_updated"), fmtDate(lecture.updated_at)),
      row(t("tr.source"), lecture.source_ref || (lecture.source_id ? t("tr.source_n", { id: lecture.source_id }) : "—")))));

  const historyCard = el("div", { class: "card" },
    el("h2", {}, t("tr.history")),
    (lecture.revisions || []).length
      ? el("div", { class: "stack" }, lecture.revisions.map((r) => el("div", { class: "split" },
        el("span", { class: "badge" }, revisionKindLabel(r.kind)),
        r.n_changes ? el("span", { class: "tabnum" }, t("tr.changes", { count: r.n_changes })) : null,
        r.summary ? el("span", { class: "muted" }, revisionText(r.summary)) : null,
        el("span", { class: "spacer" }),
        el("span", { class: "muted small tabnum" }, fmtDate(r.created_at)))))
      : el("div", { class: "empty", style: "border:0;background:none;padding:24px 12px" },
        el("p", { class: "empty-kicker" }, t("tr.no_edits")),
        el("p", {}, t("tr.no_edits_text"))));

  // ── Tab Summary ────────────────────────────────────────────────────────────
  const summaryHost = el("div", { class: "stack" });
  const summaryBar = el("div", { class: "stack" });
  const tabSummary = el("div", { class: "stack" }, summaryBar, summaryHost);

  let summaryList = [];
  let summaryFull = null;
  let activeSummaryId = lecture.latest_summary_id || null;

  const sumProvider = el("select", { id: "sumProvider", "aria-label": t("sumdlg.provider_aria") },
    providers.filter((p) => p.enabled).map((p) => el("option", {
      value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null,
    }, `${p.name} · ${p.model || t("common.no_model")}`)));
  const sumStyle = el("select", { id: "sumStyle", "aria-label": t("sumdlg.style_aria") },
    ["study", "brief", "detailed"]
      .map((v) => el("option", {
        value: v, selected: v === (settings.summary_style || "study") ? "selected" : null,
      }, summaryStyleLabel(v))));
  const sumInstruction = el("input", {
    id: "sumInstruction", placeholder: t("sumdlg.instruction_ph"),
    style: "min-width:220px",
  });

  // Stato del job, non una costante: il polling lo aggiorna mentre è in corso
  // (vedi `jobTick` più sotto), altrimenti dopo il click la tab continuerebbe a
  // dire "No summary yet" fino a un ricaricamento della pagina.
  let summaryJob = jobs.find((j) => j.kind === "llm_summary" && j.transcript_id === lecture.id
    && ["queued", "running"].includes(j.status)) || null;

  /* Con un riassunto già scritto i comandi di generazione stanno chiusi in
     "Rigenera…": il documento è la cosa da leggere, non il modulo. */
  let regenOpen = false;
  function renderSummaryBar() {
    const hasProvider = providers.some((p) => p.enabled);
    const generate = el("div", { class: "split" },
      ...(hasProvider ? [sumProvider, sumStyle, sumInstruction] : []),
      el("button", {
        class: "primary",
        disabled: hasProvider && !summaryJob ? null : "disabled",
        onclick: runSummary,
      }, summaryJob ? t("guide.writing") : (summaryFull ? t("guide.regenerate") : t("sumdlg.generate"))));
    const versions = summaryList.length > 1
      ? el("select", { id: "sumHistory", "aria-label": t("summary.versions_aria"),
          onchange: (e) => selectSummary(Number(e.currentTarget.value)) },
          summaryList.map((s) => el("option", {
            value: s.id, selected: s.id === activeSummaryId ? "selected" : null,
          }, t("summary.version_label", { date: fmtDate(s.created_at), style: summaryStyleLabel(s.style),
            words: `${s.n_words || 0}${s.target_words ? `/${s.target_words}` : ""}` }))))
      : null;
    if (!summaryFull || summaryJob) {
      setChildren(summaryBar, generate);
      return;
    }
    setChildren(summaryBar,
      el("div", { class: "split" },
        el("a", { class: "btn primary", href: `/api/summaries/${summaryFull.id}/export?format=md` },
          t("common.download_md")),
        el("button", { onclick: () => readingMode(summaryFull) }, t("summary.read")),
        summaryList.length > 1
          ? el("button", { onclick: compareSummary, title: t("summary.compare_hint") }, t("summary.compare")) : null,
        versions,
        el("span", { class: "spacer" }),
        el("button", { class: "ghost", onclick: removeSummary }, t("common.delete"))),
      el("details", { class: "adv", open: regenOpen ? "open" : null,
        ontoggle: (e) => { regenOpen = e.currentTarget.open; } },
        el("summary", {}, t("summary.regenerate_more")), generate));
  }

  /* Confronto con la versione precedente: un diff per riga, semplice (LCS),
     per giudicare se la nuova generazione è migliore. */
  async function compareSummary() {
    const index = summaryList.findIndex((x) => x.id === summaryFull.id);
    const other = summaryList[index + 1] || summaryList[index - 1];
    if (!other) return;
    const older = await api(`/summaries/${other.id}`);
    // Affiancati e formattati: due generazioni cambiano quasi ogni riga, quindi
    // un diff testuale è tutto barrato. Si giudica leggendo, non contando righe.
    const column = (sum, label) => el("div", { class: "compare-col" },
      el("p", { class: "eyebrow" }, label),
      el("p", { class: "small muted" }, `${fmtDate(sum.created_at)} · ${summaryStyleLabel(sum.style)} · `
        + `${sum.model || ""} · ${t("jobmsg.words", { count: sum.n_words || 0 })}`),
      el("div", { class: "md", html: renderMarkdown(sum.markdown || "") }));
    const body = el("div", { class: "compare" },
      ...(older.created_at <= summaryFull.created_at
        ? [column(older, t("summary.compare_old")), column(summaryFull, t("summary.compare_new"))]
        : [column(summaryFull, t("summary.compare_old")), column(older, t("summary.compare_new"))]));
    const dlg = el("dialog", { class: "wide", "aria-label": t("summary.compare") },
      el("h2", {}, t("summary.compare")),
      body,
      el("div", { class: "split" }, el("button", { type: "button", onclick: () => dlg.close() }, t("common.close"))));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
  }

  async function runSummary() {
    if (!providers.some((p) => p.enabled)) {
      toast(t("summary.need_provider"), "err");
      return;
    }
    try {
      const res = await api(`/transcripts/${transcriptId}/summary`, {
        method: "POST",
        body: {
          provider_id: sumProvider.value ? Number(sumProvider.value) : null,
          style: sumStyle.value,
          instruction: sumInstruction.value,
        },
      });
      // Il job esiste già nella risposta: lo si mostra subito e si accende il
      // polling, invece di aspettare che qualcuno ricarichi la pagina.
      summaryJob = res.job || { id: null, status: "queued", progress: 0, message: jobStatusLabel("queued") };
      toast(t("summary.queued_toast"), "ok");
      renderJobBanners();
      renderSummaryBar();
      renderSummaryBody();
      startJobPoll();
    } catch (err) { toast(err.message, "err"); }
  }

  async function removeSummary() {
    if (!summaryFull) return;
    if (!(await confirmDialog(t("summary.delete_title"), t("summary.delete_text")))) return;
    try {
      await api(`/summaries/${summaryFull.id}`, { method: "DELETE" });
      toast(t("summary.deleted"), "ok");
      summaryFull = null;
      activeSummaryId = null;
      await loadSummaries();
    } catch (err) { toast(err.message, "err"); }
  }

  async function selectSummary(id) {
    activeSummaryId = id;
    await loadSummaries();
  }

  // Come per i banner: si ricostruisce il corpo solo quando cambia lo *stato*
  // (in corso / documento / vuoto). Ridisegnarlo ogni due secondi rimetterebbe
  // da zero la transizione della barra di avanzamento, che si vedrebbe scattare.
  let summaryBodyKey = "";
  let summaryBodyRefs = null;

  function renderSummaryBody() {
    // `updated_at` nel tasto: senza, ricaricare la stessa versione dopo una
    // modifica non ridisegnerebbe il documento.
    const key = summaryJob && !summaryFull ? `job:${summaryJob.id}`
      : (summaryFull ? `doc:${summaryFull.id}:${summaryFull.updated_at}` : "empty");
    if (key === summaryBodyKey) {
      if (summaryJob && summaryBodyRefs) {
        const pct = Math.round((summaryJob.progress || 0) * 100);
        summaryBodyRefs.msg.textContent = jobMessage(summaryJob.message) || t("summary.writing_msg");
        summaryBodyRefs.meta.textContent = `job #${summaryJob.id} · ${pct}%${elapsed(summaryJob)}`;
        summaryBodyRefs.fill.style.width = `${pct}%`;
      }
      return;
    }
    summaryBodyKey = key;
    summaryBodyRefs = null;

    if (summaryJob && !summaryFull) {
      const pct = Math.round((summaryJob.progress || 0) * 100);
      const msg = el("p", {}, jobMessage(summaryJob.message) || t("summary.writing_msg"));
      const meta = el("p", { class: "muted small tabnum" },
        `job #${summaryJob.id} · ${pct}%${elapsed(summaryJob)}`);
      const fill = el("span", { style: `width:${pct}%` });
      summaryBodyRefs = { msg, meta, fill };
      setChildren(summaryHost, el("div", { class: "card" },
        el("div", { class: "split" },
          el("span", { class: "spin" }),
          el("div", { class: "grow" }, msg, meta),
          el("a", { href: `#/jobs?focus=${summaryJob.id}` }, t("common.follow_jobs"))),
        el("div", { class: "progress", role: "progressbar", style: "margin-top:10px" }, fill)));
      return;
    }
    if (!summaryFull) {
      setChildren(summaryHost, el("div", { class: "empty" },
        el("p", { class: "empty-kicker" }, t("summary.empty_kicker")),
        el("p", {}, t("summary.empty"))));
      return;
    }
    const s = summaryFull;
    const meta = [
      summaryStyleLabel(s.style),
      s.model || s.provider,
      // Obiettivo e risultato insieme: è il numero che dice se il budget di
      // lunghezza ha retto, e quanto la lezione è stata compressa.
      s.n_words ? (s.target_words ? t("summary.words_of", { count: s.n_words, target: s.target_words })
        : t("tr.words", { count: s.n_words })) : null,
      s.source_words && s.n_words ? `1:${(s.source_words / s.n_words).toFixed(1)}` : null,
      s.cost_usd ? `~$${s.cost_usd.toFixed(4)}` : null,
      s.elapsed_s ? `${Math.round(s.elapsed_s)}s` : null,
      s.edited ? t("summary.edited") : null,
    ].filter(Boolean).join(" · ");

    // Niente "Download .md" qui: il documento è la card sotto il titolo e il
    // comando è già nella testata della card. La prosa usa tutta la larghezza.
    setChildren(summaryHost,
      el("div", { class: "card" },
        el("h2", {}, s.title || t("summary.default_title")),
        el("p", { class: "muted small tabnum" }, meta),
        // Il modello ha finito i token a metà frase: il documento c'è, ma non è
        // completo, e dirlo è l'unico modo perché non venga preso per finito.
        s.truncated ? notice("warn", t("summary.truncated")) : null,
        // Il titolo è già l'intestazione della card: il primo "# …" del
        // documento non si ripete sotto.
        el("div", { class: "md", html: renderMarkdown((s.markdown || "").replace(/^\s*# [^\n]*\n+/, "")) })));
  }

  async function loadSummaries() {
    try {
      const res = await api(`/transcripts/${transcriptId}/summaries`);
      if (!isCurrent(token)) return;
      summaryList = res.summaries || [];
      if (activeSummaryId === null && summaryList.length) activeSummaryId = summaryList[0].id;
      summaryFull = activeSummaryId
        ? await api(`/summaries/${activeSummaryId}`).catch(() => null)
        : null;
      if (!isCurrent(token)) return;
      renderSummaryBar();
      renderSummaryBody();
    } catch (err) {
      setChildren(summaryHost, notice("warn", err.message));
    }
  }

  // ── Tab Cards ──────────────────────────────────────────────────────────────
  /* Le carte: generazione su click (con preventivo locale), revisione, modifica,
     sospensione ed export. Il preventivo è la parte importante: nessuna chiamata
     parte senza che l'utente abbia visto cosa costa e se è già in cache. */
  const cardsHost = el("div", { class: "stack" });
  const cardsBar = el("div", { class: "split" });
  const tabCards = el("div", { class: "stack" }, cardsBar, cardsHost);

  let cardList = [];
  let cardCounts = { total: 0, active: 0, suspended: 0 };
  let cardsEstimate = null;
  let cardsJob = jobs.find((j) => j.kind === "llm_cards" && j.transcript_id === lecture.id
    && ["queued", "running"].includes(j.status)) || null;

  const cardsProvider = el("select", {
    id: "cardsProvider", "aria-label": t("cards.provider_aria"),
    // `select` è a larghezza piena per i form: in una barra di comandi va limitato,
    // altrimenti occupa una riga intera e manda i bottoni a capo.
    style: "max-width:280px",
  },
    providers.filter((p) => p.enabled).map((p) => el("option", {
      value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null,
    }, `${p.name} · ${p.model || t("common.no_model")}`)));
  const cardsForce = el("input", { type: "checkbox", id: "cardsForce" });

  function renderCardsBar() {
    const hasProvider = providers.some((p) => p.enabled);
    const cached = Boolean(cardsEstimate && cardsEstimate.cached);
    setChildren(cardsBar,
      ...(hasProvider ? [cardsProvider] : []),
      el("label", { class: "check", title: t("cards.force_title") },
        cardsForce, el("span", {}, t("cards.force"))),
      el("button", {
        class: "primary", type: "button",
        disabled: hasProvider && !cardsJob ? null : "disabled",
        onclick: () => runCards(cached || cardsForce.checked),
      }, cardsJob ? t("cards.generating_btn") : cached ? t("cards.refresh_cached") : (cardList.length ? t("common.regenerate") : t("cards.generate"))),
      el("span", { class: "spacer" }),
      cardList.length
        ? el("a", { class: "btn", href: `/api/transcripts/${lecture.id}/deck.apkg`,
            title: t("cards.anki_title") }, "deck .apkg")
        : null,
      cardList.length
        ? el("a", { class: "btn", href: `/api/transcripts/${lecture.id}/deck.tsv` }, ".tsv")
        : null);
  }

  function cardText(text) {
    /* Le cancellazioni di un cloze si vedono: `{{c1::x}}` diventa una parola
       evidenziata, senza passare dall'HTML (il testo arriva dal modello). */
    const parts = [];
    const re = /\{\{c(\d)::(.+?)\}\}/g;
    let last = 0;
    let match;
    while ((match = re.exec(text || "")) !== null) {
      if (match.index > last) parts.push(text.slice(last, match.index));
      parts.push(el("mark", {}, match[2]));
      last = match.index + match[0].length;
    }
    if (last < (text || "").length) parts.push(text.slice(last));
    return parts.length ? parts : [text || ""];
  }

  function cardRow(card) {
    const row = el("div", { class: "card-row" });
    const actions = el("div", { class: "actions" });

    function view() {
      setChildren(row,
        el("div", { class: "grow" },
          el("div", { class: "small" },
            // cards.kind_qa, cards.kind_cloze
            el("span", { class: `badge ${card.kind === "cloze" ? "warn" : "ok"}` },
              ["qa", "cloze"].includes(card.kind) ? t(`cards.kind_${card.kind}`) : card.kind),
            card.slide_ref ? el("span", { class: "small muted" }, ` ${t("cards.slide_ref", { ref: card.slide_ref })}`) : null,
            card.edited ? el("span", { class: "badge" }, t("cards.edited")) : null,
            card.status === "suspended" ? el("span", { class: "badge" }, t("cards.suspended")) : null),
          el("div", { class: "card-front" }, cardText(card.front)),
          card.back ? el("div", { class: "small muted" }, cardText(card.back)) : null),
        actions);
      setChildren(actions,
        el("button", { class: "btn", type: "button", onclick: edit }, t("common.edit")),
        el("button", {
          class: "btn", type: "button",
          onclick: () => patch({
            status: card.status === "suspended" ? "active" : "suspended",
          }),
        }, card.status === "suspended" ? t("cards.resume") : t("cards.suspend")),
        el("button", {
          class: "danger", type: "button",
          onclick: async () => {
            if (!(await confirmDialog(t("cards.delete_title"), t("cards.delete_text")))) return;
            try {
              await api(`/cards/${card.id}`, { method: "DELETE" });
              cardList = cardList.filter((c) => c.id !== card.id);
              paint();
            } catch (err) { toast(err.message, "err"); }
          },
        }, t("common.delete")));
    }

    function edit() {
      const front = el("textarea", { rows: "2", "aria-label": t("cards.front") }, card.front);
      const back = el("textarea", { rows: "2", "aria-label": t("cards.back") }, card.back || "");
      setChildren(row,
        el("div", { class: "stack grow" },
          front,
          card.kind === "cloze" ? null : back,
          el("div", { class: "actions" },
            el("button", { class: "primary", type: "button", onclick: async () => {
              await patch({ front: front.value, back: back.value });
            } }, t("common.save")),
            el("button", { type: "button", onclick: view }, t("common.cancel")))));
      front.focus();
    }

    async function patch(fields) {
      try {
        const updated = await api(`/cards/${card.id}`, { method: "PATCH", body: fields });
        Object.assign(card, updated);
        view();
        if ("status" in fields) await loadCards();
      } catch (err) { toast(err.message, "err"); }
    }

    view();
    return row;
  }

  function paint() {
    cardCounts = {
      total: cardList.length,
      active: cardList.filter((c) => c.status !== "suspended").length,
      suspended: cardList.filter((c) => c.status === "suspended").length,
    };
    renderCardsBar();
    setChildren(cardsHost,
      cardsEstimate
        ? el("p", { class: "muted small" },
            cardsEstimate.slides
              ? t("cards.est_slides", { count: cardsEstimate.slides })
                + (cardsEstimate.linked > cardsEstimate.slides
                  ? ` (${t("cards.est_linked", { count: cardsEstimate.linked })})` : "")
                + " · "
              : cardsEstimate.linked
                ? `${t("cards.est_no_slides_linked", { count: cardsEstimate.linked })} · `
                : `${t("cards.est_no_slides")} · `,
            t("cards.est_context", { chars: cardsEstimate.chars, tokens: cardsEstimate.tokens_in }),
            cardsEstimate.images ? ` ${t("cards.est_images", { count: cardsEstimate.images })}` : "",
            ` · ${t("cards.est_max", { count: cardsEstimate.cards_max })}`,
            cardsEstimate.unconfirmed
              ? el("span", { class: "badge warn", style: "margin-left:8px",
                  title: t("cards.est_unconfirmed_title") },
                  t("cards.est_unconfirmed", { count: cardsEstimate.unconfirmed }))
              : null,
            cardsEstimate.auto_ignored
              ? el("span", { class: "badge", style: "margin-left:8px",
                  title: t("cards.est_auto_ignored_title") },
                  t("cards.est_auto_ignored", { count: cardsEstimate.auto_ignored }))
              : null,
            cardsEstimate.cached ? ` · ${t("cards.est_cached")}` : "")
        : null,
      cardsJob
        ? el("div", { class: "card" }, el("div", { class: "split" },
            el("span", { class: "spin" }),
            el("div", { class: "grow" },
              el("p", {}, jobMessage(cardsJob.message) || t("cards.generating_msg")),
              el("p", { class: "muted small tabnum" }, `job #${cardsJob.id} · ${Math.round((cardsJob.progress || 0) * 100)}%`)),
            el("a", { href: `#/jobs?focus=${cardsJob.id}` }, t("common.follow_jobs"))))
        : null,
      cardList.length
        ? el("div", { class: "stack" }, cardList.map(cardRow))
        : el("div", { class: "empty" },
            el("p", { class: "empty-kicker" }, t("cards.empty_kicker")),
            el("p", {}, t("cards.empty"))));
  }

  async function loadCards() {
    try {
      const res = await api(`/transcripts/${transcriptId}/cards`);
      cardList = res.cards || [];
      cardsEstimate = await api(`/transcripts/${transcriptId}/cards/estimate`);
      if (isCurrent(token)) paint();
    } catch (err) { toast(err.message, "err"); }
  }

  async function runCards(force) {
    if (!providers.some((p) => p.enabled)) {
      toast(t("summary.need_provider"), "err");
      return;
    }
    if (cardsEstimate && cardsEstimate.cached && !force) {
      const ok = await confirmDialog(t("cards.unchanged_title"), t("cards.unchanged_text"), t("common.regenerate"));
      if (!ok) return;
      force = true;
    }
    try {
      const res = await api(`/transcripts/${transcriptId}/cards`, {
        method: "POST",
        body: {
          provider_id: cardsProvider.value ? Number(cardsProvider.value) : null,
          force: Boolean(force),
        },
      });
      cardsJob = res.job || { id: null, status: "queued", progress: 0, message: jobStatusLabel("queued") };
      toast(t("cards.queued"), "ok");
      renderJobBanners();
      paint();
      startJobPoll();
    } catch (err) { toast(err.message, "err"); }
  }

  const tabInfo = el("div", { class: "stack" }, detailsCard, historyCard);

  function row(k, v) { return el("tr", {}, el("th", {}, k), el("td", {}, String(v))); }

  const tabs = {
    content: tabContent,
    segments: tabSegments,
    summary: tabSummary,
    // Le carte sono opzionali: la tab c'è solo se le hai accese in Settings.
    ...(settings.cards_enabled ? { cards: tabCards } : {}),
    info: tabInfo,
  };
  // Il riassunto è il motivo per cui si apre una lezione: se c'è, si parte da lì.
  let startTab = params.get("tab") || (lecture.latest_summary_id ? "summary" : "content");
  if (!tabs[startTab]) startTab = "content";
  const panel = el("div", { role: "tabpanel", "aria-label": t("tr.view_aria") }, tabs[startTab]);

  const tabBar = el("div", { class: "toolbar", role: "tablist", "aria-label": t("tr.sections_aria") },
    Object.entries(tabs).map(([key, node], i) => el("button", {
      class: "tab", role: "tab", id: `tab-${key}`,
      "aria-selected": key === startTab ? "true" : "false",
      tabindex: key === startTab ? "0" : "-1",
      onclick: (e) => {
        $$(".tab", tabBar).forEach((b) => {
          b.setAttribute("aria-selected", "false");
          b.setAttribute("tabindex", "-1");
        });
        e.currentTarget.setAttribute("aria-selected", "true");
        e.currentTarget.setAttribute("tabindex", "0");
        panel.replaceChildren(node);
        // Le tab sono bottoni: muoversi con le frecce è quello che ci si
        // aspetta da un tablist, non serve il Tab per ognuna.
        e.currentTarget.focus();
      },
      onkeydown: (e) => {
        const keys = Object.keys(tabs);
        const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (!step) return;
        e.preventDefault();
        const next = $(`#tab-${keys[(i + step + keys.length) % keys.length]}`, tabBar);
        next?.click();
      },
    // tr.tab_content, tr.tab_segments, tr.tab_summary, tr.tab_cards, tr.tab_info
    }, t(`tr.tab_${key}`))));

  /* ── Lavori in corso su questa trascrizione ─────────────────────────────────
     Un elemento stabile che il polling riscrive. Prima il banner dell'agente era
     un figlio condizionale costruito una volta sola, quindi compariva solo
     ricaricando la pagina: lo stesso problema del riassunto. */
  const jobsHost = el("div", { class: "stack" });
  let bannerKey = "";
  let bannerEntries = [];

  function renderJobBanners() {
    const active = [[t("tr.tab_summary"), summaryJob],
      [t("tr.tab_cards"), cardsJob]]
      .filter(([, job]) => Boolean(job));
    const key = active.map(([label, job]) => `${label}:${job.id}`).join("|");
    // Si ricostruisce il DOM solo quando cambia *quali* lavori sono in corso.
    // Ridisegnare ogni due secondi rimetterebbe lo spinner da zero e si vedrebbe
    // uno scatto a ogni giro; il resto si aggiorna scrivendo nei nodi esistenti.
    if (key !== bannerKey) {
      bannerKey = key;
      bannerEntries = active.map(([label, job]) => ({ label, job, ...buildBanner(label, job) }));
      setChildren(jobsHost, ...bannerEntries.map((b) => b.node));
    }
    for (let i = 0; i < bannerEntries.length; i++) {
      updateBanner(bannerEntries[i], active[i][0], active[i][1]);
    }
  }

  function buildBanner(label, job) {
    const msg = el("p", {});
    const meta = el("p", { class: "muted small tabnum" });
    const node = el("div", { class: "card" }, el("div", { class: "split" },
      el("span", { class: "spin" }),
      el("div", { class: "grow" }, msg, meta),
      el("a", { href: `#/jobs?focus=${job.id}` }, t("common.follow_jobs"))));
    return { node, msg, meta };
  }

  function updateBanner(entry, label, job) {
    entry.job = job;
    entry.msg.textContent = `${label}: ${jobMessage(job.message) || jobStatusLabel("running")}`;
    entry.meta.textContent =
      `job #${job.id} · ${Math.round((job.progress || 0) * 100)}%${elapsed(job)}`;
  }

  /* In single-pass la percentuale resta ferma durante la chiamata: il tempo
     trascorso è l'unico segno onesto che sta ancora lavorando. Si aggiorna da
     solo perché il polling ridisegna ogni due secondi. */
  function elapsed(job) {
    if (!job.started_at) return "";
    const seconds = Math.max(0, Math.round(Date.now() / 1000 - job.started_at));
    if (seconds < 60) return ` · ${seconds}s`;
    return ` · ${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }

  // Polling mirato: gira solo mentre c'è un lavoro in corso, e si spegne da solo.
  // Un intervallo fisso sempre attivo terrebbe occupata la pagina per niente.
  let jobPoll = null;
  let jobTickBusy = false;

  function startJobPoll() {
    if (jobPoll || !(summaryJob || cardsJob)) return;
    jobPoll = setInterval(jobTick, 2000);
  }

  function stopJobPoll() {
    if (jobPoll) { clearInterval(jobPoll); jobPoll = null; }
  }

  async function jobTick() {
    if (!isCurrent(token)) { stopJobPoll(); return; }
    // Una richiesta più lenta dell'intervallo non deve accavallarsi alla
    // successiva: due risposte fuori ordine farebbero lampeggiare lo stato.
    if (jobTickBusy) return;
    jobTickBusy = true;
    let latest;
    try {
      latest = (await api("/jobs?limit=20")).jobs || [];
    } catch {
      return;   // rete: si riprova al giro successivo, senza spaventare l'utente
    } finally {
      jobTickBusy = false;
    }
    if (!isCurrent(token)) return;

    const mine = (kind) => latest.find((j) => j.kind === kind && j.transcript_id === lecture.id
      && ["queued", "running"].includes(j.status)) || null;
    const finishedSummary = summaryJob && !mine("llm_summary");
    const finishedCards = cardsJob && !mine("llm_cards");
    summaryJob = mine("llm_summary");
    cardsJob = mine("llm_cards");

    renderJobBanners();
    if (finishedSummary) {
      // Il documento è pronto: si carica da solo invece di chiedere un refresh.
      await loadSummaries();
      if (isCurrent(token)) toast(t("summary.ready"), "ok");
    } else {
      renderSummaryBar();
      renderSummaryBody();
    }
    if (finishedCards) {
      await loadCards();
      if (isCurrent(token)) toast(t("cards.ready"), "ok");
    } else {
      paint();
    }
    if (!summaryJob && !cardsJob) stopJobPoll();
  }

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" },
      el("a", { class: "crumb-back", href: "#/" }, icon("back"), t("nav.library")), " / ",
      el("a", { href: `#/p/${lecture.project_id}` }, lecture.project_name || t("route.course")), " / ",
      el("span", { class: "here" }, lecture.display_title || lecture.title)),
    el("div", { class: "card" },
      el("div", { class: "split hero" },
        el("div", { class: "grow" },
          el("p", { class: "empty-kicker" },
            `${lecture.lecture_no ? `L${lecture.lecture_no} · ` : ""}${lecture.project_name || t("route.course")} · ${fmtDate(lecture.created_at)}`),
          // Lo stesso nome della tabella del corso; il nome del file resta sotto.
          el("h1", {}, lecture.display_title || lecture.title),
          el("p", { class: "muted small hero-note" },
            lecture.display_title && lecture.display_title !== lecture.title ? `${lecture.title} · ` : "",
            `${t("tr.hero_audio", { time: fmtTs(lecture.duration) })} · ${lecture.language ? lecture.language.toUpperCase() : t("tr.unknown_lang")} · `,
            `${t("tr.segments", { count: (lecture.segments || []).length })} · ${t("tr.words", { count: words() })}`)),
        el("div", { class: "actions" },
          studySelect(lecture),
          exportMenu(lecture),
          el("button", { onclick: rename }, t("common.rename")),
          el("button", { class: "danger", onclick: remove }, t("common.delete")))),
      lecture.description ? el("p", { class: "muted" }, lecture.description) : null),
    jobsHost,
    tabBar, panel));

  await loadSummaries();
  renderJobBanners();
  paint();
  loadCards();
  startJobPoll();
  onViewTeardown(stopJobPoll);

  // `?tab=summary` (dai risultati di ricerca nei riassunti) apre quella tab.
  if (params.get("tab")) $(`#tab-${params.get("tab")}`, tabBar)?.click();

  /* `?at=<secondi>` (dalle citazioni di "Ask the course"): apre i segmenti e
     porta al punto della lezione, evidenziato. */
  const at = Number(params.get("at"));
  if (params.has("at") && Number.isFinite(at)) {
    $("#tab-segments", tabBar)?.click();
    const segs = lecture.segments || [];
    // `at` arriva arrotondato al secondo: +1 evita di cadere sul segmento prima.
    let idx = segs.findIndex((s) => (s.start || 0) > at + 1) - 1;
    if (idx < 0) idx = segs.length && (segs[segs.length - 1].start || 0) <= at ? segs.length - 1 : 0;
    const node = $(`.seg[data-i="${idx}"]`, tabSegments);
    node?.classList.add("hit");
    node?.scrollIntoView({ block: "center" });
  }

  async function rename() {
    const title = await promptDialog({
      title: t("tr.rename_title"), label: t("course.col_title"), value: lecture.title, okLabel: t("common.rename"),
    });
    if (title === null) return;
    if (!title) return toast(t("tr.needs_title"), "err");
    try {
      await api(`/transcripts/${transcriptId}`, { method: "PATCH", body: { title } });
      toast(t("tr.renamed"), "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  }
  async function remove() {
    if (!await confirmDialog(t("transcript.delete_title"), t("tr.delete_text_short", { name: lecture.title }))) return;
    await api(`/transcripts/${transcriptId}`, { method: "DELETE" });
    toast(t("common.deleted"), "ok");
    location.hash = `#/p/${lecture.project_id}`;
  }

  if (query) {
    const first = $(".seg.hit");
    first?.scrollIntoView({ block: "center" });
  }
}

function highlight(text, query) {
  const safe = esc(text);
  if (!query) return safe;
  try {
    const re = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
    return safe.replace(re, "<mark>$1</mark>");
  } catch { return safe; }
}

/* ============================================================================
   Impostazioni
   ========================================================================== */
async function viewSettings(nav, params, token) {
  const [{ settings, disk }, { providers }, modelsRes] = await Promise.all([
    api("/settings"), api("/providers"), api("/models"),
  ]);
  const modelNames = modelsRes.models || [];
  const cached = modelsRes.cached || [];

  /* Stessi menu della pagina "New lecture": le bandiere dicono cosa sa fare
     ogni modello, e il valore si legge dal componente (non da byId: il nodo è
     un bottone, non un <select>). */
  const modelPick = flagSelect({
    id: "sModel", options: modelOptions(modelNames, modelsRes.english_only),
    value: settings.default_model,
  });
  const langPick = flagSelect({
    id: "sLang", options: languageOptions(modelsRes.languages),
    value: settings.default_language,
  });

  const field = (label, id, value, type = "text") => el("div", { class: "field" },
    el("label", { for: id }, label), el("input", { id, type, value: value ?? "" }));

  const form = el("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    try {
      await api("/settings", { method: "PUT", body: {
        default_model: modelPick.value,
        default_language: langPick.value,
        default_device: byId("sDevice").value,
        default_compute_type: byId("sCompute").value,
        default_provider_id: Number(byId("sProvider").value) || null,
        flush_every: Number(byId("sFlush").value) || 10,
        cards_enabled: byId("sCardsOn").checked,
        monthly_budget_usd: Number(byId("sBudget").value) || 0,
        auto_vocabulary: byId("sAutoVocab").checked,
        summary_max_tokens: Number(byId("sSumTokens").value) || 8000,
        summary_style: byId("sSumStyle").value,
        summary_language: byId("sSumLang").value.trim(),
        backup_keep: Number(byId("sKeep").value) || 5,
        cards_max: Number(byId("sCardsMax").value) || 12,
        cards_language: byId("sCardsLang").value.trim(),
        cards_context_chars: Number(byId("sCardsCtx").value) || 12000,
        cards_vision: byId("sCardsVision").value,
        slides_vlm: byId("sVlmMode").value,
        slides_vlm_model: byId("sVlmModel").value.trim(),
        match_threshold: Number(byId("sMatch").value) || 0.60,
        match_margin: Number.isFinite(parseFloat(byId("sMatchMargin").value))
          ? parseFloat(byId("sMatchMargin").value) : 0.02,
        embed_model: byId("sEmbed").value.trim(),
      } });
      toast(t("settings.saved"), "ok");
    } catch (err) { toast(err.message, "err"); }
  } },
    el("div", { class: "card" }, el("h2", {}, t("settings.transcription")),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sModel" }, t("course.col_model")), modelPick.el),
        el("div", { class: "grow field" }, el("label", { for: "sLang" }, t("opt.language")), langPick.el)),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sProvider" }, t("settings.default_provider")),
          el("select", { id: "sProvider" }, [el("option", { value: "" }, t("settings.none"))].concat(
            providers.map((p) => el("option", { value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null }, `${p.name} · ${p.model || "?"}`)))))),
      el("label", { class: "check" },
        el("input", { id: "sAutoVocab", type: "checkbox", checked: settings.auto_vocabulary !== false ? "checked" : null }),
        el("span", {}, t("settings.auto_vocab"))),
      el("p", { class: "hint" }, t("settings.auto_vocab_hint")),
      el("details", { class: "adv" }, el("summary", {}, t("settings.advanced")),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sDevice" }, t("opt.device")),
          el("select", { id: "sDevice" }, ["auto", "cuda", "cpu"].map((d) => el("option", { value: d, selected: d === settings.default_device ? "selected" : null }, d)))),
        el("div", { class: "grow field" }, el("label", { for: "sCompute" }, t("opt.compute")),
          el("select", { id: "sCompute" }, ["auto", "float16", "int8", "int8_float16", "float32", "bfloat16"].map((c) => el("option", { value: c, selected: c === settings.default_compute_type ? "selected" : null }, c))))),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sFlush" }, t("settings.flush")),
          el("input", { id: "sFlush", type: "number", min: "5", max: "120", value: settings.flush_every })),
        el("div", { class: "grow field" }, el("label", { for: "sKeep" }, t("settings.backups")),
          el("input", { id: "sKeep", type: "number", min: "1", max: "50", value: settings.backup_keep })))),
      el("button", { class: "primary", type: "submit" }, t("settings.save"))),

    el("div", { class: "card" }, el("h2", {}, t("settings.summaries")),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sSumStyle" }, t("settings.default_style")),
          el("select", { id: "sSumStyle" },
            ["study", "brief", "detailed"]
              .map((v) => el("option", { value: v, selected: v === (settings.summary_style || "study") ? "selected" : null }, summaryStyleLabel(v))))),
        el("div", { class: "grow field" }, el("label", { for: "sSumLang" }, t("settings.output_lang")),
          el("input", { id: "sSumLang", value: settings.summary_language || "", placeholder: t("settings.output_lang_ph") })),
        el("div", { class: "grow field" }, el("label", { for: "sBudget" }, t("settings.budget")),
          el("input", { id: "sBudget", type: "number", min: "0", step: "0.5", value: settings.monthly_budget_usd || 0 }))),
      el("p", { class: "hint" }, t("settings.budget_hint")),
      el("details", { class: "adv" }, el("summary", {}, t("settings.advanced")),
        el("div", { class: "field" }, el("label", { for: "sSumTokens" }, t("settings.max_tokens")),
          el("input", { id: "sSumTokens", type: "number", min: "1000", max: "32000", step: "500", value: settings.summary_max_tokens })),
      el("p", { class: "hint" }, t("settings.max_tokens_hint"))),
      el("button", { class: "primary", type: "submit" }, t("settings.save"))),

    el("div", { class: "card" }, el("h2", {}, t("settings.slides_cards")),
      el("label", { class: "check" },
        el("input", { id: "sCardsOn", type: "checkbox", checked: settings.cards_enabled ? "checked" : null }),
        el("span", {}, t("settings.cards_on"))),
      el("p", { class: "hint" }, t("settings.slides_hint")),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sVlmMode" }, t("slides.vlm_heading")),
          // settings.vlm_off, settings.vlm_auto, settings.vlm_on
          el("select", { id: "sVlmMode" },
            ["off", "auto", "on"]
              .map((v) => el("option", {
                value: v, selected: v === (settings.slides_vlm || "auto") ? "selected" : null,
              }, t(`settings.vlm_${v}`))))),
      ),
      el("details", { class: "adv" }, el("summary", {}, t("settings.advanced")),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sVlmModel" }, t("settings.vlm_model")),
          el("input", { id: "sVlmModel", value: settings.slides_vlm_model || "gemma3:4b",
            placeholder: "gemma3:4b" })),
        el("div", { class: "grow field" }, el("label", { for: "sEmbed" }, t("settings.embed_model")),
          el("input", { id: "sEmbed", value: settings.embed_model || "nomic-embed-text:v1.5" }))),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sMatch" }, t("settings.match_threshold")),
          el("input", { id: "sMatch", type: "number", min: "0.3", max: "0.9", step: "0.05",
            value: settings.match_threshold ?? 0.60 })),
        el("div", { class: "grow field" }, el("label", { for: "sMatchMargin" }, t("settings.match_margin")),
          el("input", { id: "sMatchMargin", type: "number", min: "0", max: "0.2", step: "0.01",
            value: settings.match_margin ?? 0.02 })),
        el("div", { class: "grow field" }, el("label", { for: "sCardsMax" }, t("settings.cards_max")),
          el("input", { id: "sCardsMax", type: "number", min: "3", max: "60",
            value: settings.cards_max ?? 12 }))),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sCardsCtx" }, t("settings.cards_ctx")),
          el("input", { id: "sCardsCtx", type: "number", min: "2000", max: "60000", step: "1000",
            value: settings.cards_context_chars ?? 12000 })),
        el("div", { class: "grow field" }, el("label", { for: "sCardsLang" }, t("settings.cards_lang")),
          el("input", { id: "sCardsLang", value: settings.cards_language || "",
            placeholder: t("settings.cards_lang_ph") })),
        el("div", { class: "grow field" }, el("label", { for: "sCardsVision" }, t("settings.cards_vision")),
          // settings.vision_off, settings.vision_auto
          el("select", { id: "sCardsVision" },
            ["off", "auto"]
              .map((v) => el("option", {
                value: v, selected: v === (settings.cards_vision || "off") ? "selected" : null,
              }, t(`settings.vision_${v}`)))))),
      el("p", { class: "hint" }, t("settings.match_hint"))),
      el("button", { class: "primary", type: "submit" }, t("settings.save"))),

    el("div", { class: "card" }, el("h2", {}, t("settings.providers")),
      el("p", { class: "muted small" }, t("settings.providers_hint")),
      el("div", { class: "stack" }, providers.map((p) => providerRow(p)))),

    el("div", { class: "card" }, el("h2", {}, t("settings.storage")),
      el("div", { class: "stat-bar", style: "margin-bottom:14px" },
        el("div", { class: "stat" },
          el("span", { class: "stat-k" }, t("settings.audio_copies")),
          el("span", { class: "stat-n" }, fmtBytes(disk.sources_bytes)),
          el("span", { class: "stat-k" }, t("common.n_files", { count: disk.sources_files }))),
        el("div", { class: "stat" },
          el("span", { class: "stat-k" }, t("settings.data_dir")),
          el("span", { class: "stat-n" }, fmtBytes(disk.data_bytes))),
        el("div", { class: "stat" },
          el("span", { class: "stat-k" }, t("settings.free")),
          el("span", { class: "stat-n" }, fmtBytes(disk.free_bytes)))),
      el("p", { class: "hint" }, t("settings.storage_hint")),
      el("div", { class: "split", style: "margin-top:12px" },
        el("button", { type: "button", onclick: async () => {
          try {
            const r = await api("/maintenance/backup");
            toast(t("settings.backed_up", { path: r.path || "data/backups" }), "ok");
          } catch (err) { toast(err.message, "err"); }
        } }, t("settings.backup")),
        el("button", { type: "button", onclick: async () => {
          try {
            const r = await api("/maintenance/purge-sources", { method: "POST" });
            toast(t("settings.purged", { count: r.purged }), "ok");
            render();
          } catch (err) { toast(err.message, "err"); }
        } }, t("settings.purge")),
        el("button", { type: "button", onclick: async () => {
          try {
            const r = await api("/models/unload", { method: "POST" });
            toast(t("settings.unloaded", { count: r.unloaded }));
          } catch (err) { toast(err.message, "err"); }
        } }, t("settings.unload")))),

    el("div", { class: "card" }, el("h2", {}, t("settings.runtime")),
      el("div", { class: "split small" },
        cached.length
          ? cached.map((m) => el("span", { class: "badge ok" }, `${m.model} · ${m.device}`))
          : el("span", { class: "badge" }, t("settings.no_model_loaded"))),
      el("p", { class: "hint" }, t("settings.runtime_hint"))),
  );

  function providerRow(p) {
    const keyInput = el("input", { type: "password", placeholder: p.has_key ? t("prov.key_configured") : t("prov.api_key"), autocomplete: "off" });
    const modelInput = el("input", { value: p.model || "", placeholder: t("prov.model_ph") });
    const urlInput = el("input", { value: p.base_url, placeholder: t("prov.base_url_ph") });
    const enabled = el("input", { type: "checkbox" });
    enabled.checked = !!p.enabled;
    const status = el("span", { class: "small muted" }, "");
    const keyBadge = p.key_state === "unreadable"
      ? el("span", { class: "badge danger" }, t("prov.key_unreadable"))
      : p.has_key
        ? el("span", { class: "badge ok" }, t("prov.key_stored"))
        : el("span", { class: "badge" }, t("prov.no_key"));
    const isReasoning = /reason|think|r1|o[1-4]\b/i.test(p.model || "");
    return el("div", { class: "card" },
      el("div", { class: "split" },
        el("h3", { style: "margin:0" }, p.name),
        keyBadge,
        el("label", { class: "check", style: "margin:0" }, enabled, el("span", {}, t("prov.enabled"))),
        el("span", { class: "spacer" }),
        el("button", { type: "button", onclick: async () => {
          status.textContent = t("prov.testing");
          try {
            const r = await api(`/providers/${p.id}/test`, { method: "POST", body: { model: modelInput.value } });
            setChildren(status, r.ok ? notice("ok", t("prov.test_ok", { model: r.model, reply: r.reply }))
              : notice("err", humanError(r.error || "")));
          } catch (err) { setChildren(status, notice("err", humanError(err.message))); }
        } }, t("prov.test"))),
      p.key_state === "unreadable"
        ? notice("err", t("prov.unreadable_notice"))
        : null,
      isReasoning
        ? notice("warn", t("prov.reasoning"))
        : null,
      el("div", { class: "row", style: "margin-top:8px" },
        el("div", { class: "grow field" }, el("label", {}, t("prov.base_url")), urlInput),
        el("div", { class: "grow field" }, el("label", {}, t("course.col_model")), modelInput),
        el("div", { class: "grow field" }, el("label", {}, t("prov.api_key")), keyInput)),
      el("div", { class: "split" },
        el("button", { type: "button", onclick: async () => {
          const body = { base_url: urlInput.value, model: modelInput.value, enabled: enabled.checked };
          if (keyInput.value) body.api_key = keyInput.value;
          await api(`/providers/${p.id}`, { method: "PATCH", body });
          toast(t("prov.saved"), "ok");
          render();
        } }, t("common.save")),
        el("button", { type: "button", onclick: async () => {
          const r = await api(`/providers/${p.id}/models`).catch((e) => { toast(e.message, "err"); return null; });
          if (r) status.textContent = r.models.slice(0, 12).join(", ");
        } }, t("prov.list_models")),
        status));
  }

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "card" },
      el("p", { class: "empty-kicker" }, t("settings.kicker")),
      el("h1", {}, t("nav.settings")),
      el("p", { class: "muted small hero-note" }, t("settings.intro"))),
    form));
}

/* ============================================================================
   Costi
   ========================================================================== */
async function viewCosts(nav, params, token) {
  const data = await api("/costs");
  const { cells, projects, months } = data;
  const names = new Map(projects.map((p) => [p.id, p]));

  // Il filtro è locale di proposito: le celle sono poche centinaia e cambiare
  // un menu non deve costare un giro sul server. Ogni somma passa da un
  // arrotondamento: sei decimali di costo sommati in floating point producono
  // code come 0.30000000000000004, che a schermo sono già invisibili ma
  // rendono inutile qualunque confronto.
  const round = (n) => Math.round(n * 1e6) / 1e6;
  const sumFor = (list, key) => round(list.reduce((t, c) => t + (c[key] || 0), 0));

  // Il valore del menu va letto dalla *proprietà*, non dall'attributo: quando
  // l'utente sceglie un'opzione il browser aggiorna `select.value`, mentre
  // l'attributo `value` resta quello scritto nel markup. Leggere l'attributo
  // significa leggere per sempre la scelta iniziale — il filtro sembrava non
  // fare niente. Si ripiega sull'opzione selezionata per un DOM che non
  // implementa la proprietà.
  const choice = (node) => {
    if (typeof node.value === "string" && node.value) return node.value;
    const selected = [...node.children].find((o) => o.hasAttribute("selected"));
    return (selected && selected.getAttribute("value")) || "";
  };

  function markChoice(node) {
    const current = choice(node);
    [...node.children].forEach((o) => {
      if (o.getAttribute("value") === current) o.setAttribute("selected", "selected");
      else o.removeAttribute("selected");
    });
  }

  function filtered() {
    const monthValue = choice(month);
    const kindValue = choice(kind);
    return cells.filter((c) => (!monthValue || monthValue === "all" || c.month === monthValue)
      && (!kindValue || kindValue === "all" || c.kind === kindValue));
  }

  function aggregate() {
    const list = filtered();
    const byCourse = new Map();
    for (const c of list) {
      const row = byCourse.get(c.project_id)
        || { project_id: c.project_id, total: 0, summary: 0, fix: 0, tokens: 0, jobs: 0 };
      const cost = c.cost_usd || 0;
      row.total = round(row.total + cost);
      if (c.kind === "llm_summary") row.summary = round(row.summary + cost);
      else row.fix = round(row.fix + cost);
      row.tokens += c.tokens || 0;
      row.jobs += c.n_jobs || 0;
      byCourse.set(c.project_id, row);
    }
    return { byCourse: [...byCourse.values()].filter((r) => r.total > 0),
      total: sumFor(list, "cost_usd"),
      summary: sumFor(list.filter((c) => c.kind === "llm_summary"), "cost_usd"),
      fix: sumFor(list.filter((c) => c.kind !== "llm_summary"), "cost_usd"),
      tokens: list.reduce((t, c) => t + (c.tokens || 0), 0),
      jobs: list.reduce((t, c) => t + (c.n_jobs || 0), 0) };
  }

  const monthLabel = (m) => new Date(`${m}-01T00:00:00`).toLocaleDateString(I18N.lang, {
    month: "short", year: "numeric" });

  const month = el("select", { id: "costMonth", "aria-label": t("costs.filter_month") },
    [el("option", { value: "all" }, t("costs.all_months"))].concat(
      months.map((m) => el("option", { value: m }, monthLabel(m)))));
  // Ogni opzione ha un `value` esplicito, compresa "tutti": un `<option>` senza
  // `value` viene identificato dal browser con il suo *testo*, quindi un'etichetta
  // tradotta romperebbe in silenzio il confronto con il filtro.
  const kind = el("select", { id: "costKind", "aria-label": t("costs.filter_kind") },
    el("option", { value: "all" }, t("costs.kind_all")),
    el("option", { value: "llm_summary" }, t("costs.kind_summary")),
    el("option", { value: "llm_guide" }, t("costs.kind_guide")),
    el("option", { value: "llm_ask" }, t("costs.kind_ask")),
    el("option", { value: "llm_cards" }, t("costs.kind_cards")),
    el("option", { value: "llm_fix" }, t("jobkind.llm_fix")));

  const stats = el("div", { class: "stat-bar" });
  const tableHost = el("div", {});
  // Chiesto una volta per disegno: se cambia un menu, cambia il filtro dei
  // riassunti per tipo (che decide anche le colonne).
  const summaryOnly = () => {
    const kindValue = choice(kind);
    return !!kindValue && kindValue !== "all";
  };

  function renderStats(agg) {
    const stat = (k, v, cls = "") => el("div", { class: `stat ${cls}` },
      el("span", { class: "stat-k" }, k), el("span", { class: "stat-n" }, v));
    stats.replaceChildren(
      stat(t("costs.total"), fmtUsd(agg.total), "is-accent"),
      stat(t("settings.summaries"), fmtUsd(agg.summary)),
      stat(t("costs.other_work"), fmtUsd(agg.fix)),
      stat(t("costs.tokens"), Math.round(agg.tokens).toLocaleString(I18N.lang)),
      stat(t("nav.jobs"), String(agg.jobs)));
  }

  function renderTable(agg) {
    // Le barre sono relative al corso più caro: dice a colpo d'occhio dove sono
    // finiti i soldi, senza aggiungere una libreria di grafici.
    const peak = Math.max(...agg.byCourse.map((r) => r.total), 0);
    const bar = (v) => el("span", { class: "cost-bar" },
      el("span", { class: "cost-bar-fill", style: peak ? `width:${(v / peak) * 100}%` : "width:0" }));

    const heads = summaryOnly()
      ? [el("th", {}, t("route.course")), el("th", { class: "right" }, t("costs.cost")),
          el("th", { class: "right" }, t("nav.jobs"))]
      : [el("th", {}, t("route.course")), el("th", { class: "right" }, t("costs.total")),
          el("th", { class: "right" }, t("settings.summaries")), el("th", { class: "right" }, t("costs.other")),
          el("th", { class: "right" }, t("nav.jobs"))];
    const rows = [...agg.byCourse].sort((a, b) => b.total - a.total).map((r) => {
      const project = names.get(r.project_id) || {};
      const title = el("td", {},
        el("a", { href: `#/p/${r.project_id}` }, project.name || `#${r.project_id}`),
        project.code ? el("span", { class: "muted small" }, ` · ${project.code}`) : null);
      const jobs = el("td", { class: "tabnum right" }, String(r.jobs));
      return summaryOnly()
        ? el("tr", {}, title,
            el("td", {}, el("div", { class: "cost-cell" },
              bar(r.total), el("span", { class: "tabnum" }, fmtUsd(r.total)))),
            jobs)
        : el("tr", {}, title,
            el("td", {}, el("div", { class: "cost-cell" },
              bar(r.total), el("span", { class: "tabnum" }, fmtUsd(r.total)))),
            el("td", { class: "tabnum right muted" }, fmtUsd(r.summary)),
            el("td", { class: "tabnum right muted" }, fmtUsd(r.fix)),
            jobs);
    });

    tableHost.replaceChildren(rows.length
      ? el("table", { class: "costs-table" }, el("thead", {}, el("tr", {}, heads)),
          el("tbody", {}, rows))
      : el("div", { class: "empty" },
          el("p", { class: "empty-kicker" }, t("costs.empty_kicker")),
          el("p", {}, t("costs.empty"))));
  }

  function refreshViews() {
    markChoice(month);
    markChoice(kind);
    const agg = aggregate();
    renderStats(agg);
    renderTable(agg);
  }
  month.addEventListener("change", refreshViews);
  kind.addEventListener("change", refreshViews);
  // Il primo disegno va fatto prima del guard di `isCurrent`: il guard decide
  // solo se attaccare il risultato alla pagina, non se calcolarlo.
  refreshViews();

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "card" },
      el("div", { class: "split hero" },
        el("div", { class: "grow" },
          el("p", { class: "empty-kicker" }, t("costs.kicker")),
          el("h1", {}, t("costs.title")),
          el("p", { class: "muted small hero-note" }, t("costs.intro"))),
        el("div", { class: "actions" },
          el("button", { onclick: () => render() }, t("jobs.refresh"))))),
    data.budget_usd > 0
      ? notice(data.month_total_usd > data.budget_usd ? "warn" : "info",
        t("budget.status", { spent: data.month_total_usd.toFixed(2), budget: data.budget_usd.toFixed(2) }))
      : null,
    stats,
    el("div", { class: "card" },
      el("div", { class: "split" },
        el("div", { class: "field", style: "min-width:190px;margin:0" },
          el("label", { for: "costMonth" }, t("costs.period")), month),
        el("div", { class: "field", style: "min-width:230px;margin:0" },
          el("label", { for: "costKind" }, t("costs.work")), kind),
        el("span", { class: "spacer" }),
        el("span", { class: "muted small" }, t("costs.totals_hint"))),
      tableHost),
    el("div", { class: "card" },
      el("h2", {}, t("costs.how")),
      el("p", { class: "muted prose" }, `${t("costs.note")} ${t("costs.died")}`),
      el("table", { class: "details-table" }, el("tbody", {},
        el("tr", {}, el("th", {}, t("costs.cache_hit")), el("td", {}, `$${data.rates.cache_hit}`)),
        el("tr", {}, el("th", {}, t("costs.cache_miss")), el("td", {}, `$${data.rates.cache_miss}`)),
        el("tr", {}, el("th", {}, t("costs.output")), el("td", {}, `$${data.rates.output}`)))),
      el("p", { class: "hint" }, t("costs.local_hint")))));
}

async function viewAbout(nav, params, token) {
  // Lo stato del server è già esposto da /health: mostrarlo qui evita di
  // mandare l'utente a cercare la versione nei log.
  const health = await api("/health").catch(() => null);
  const hw = health?.hardware || {};
  setDocTitle(t("foot.about"));

  const facts = [
    [t("about.version"), health?.version || "—"],
    [t("about.database"), "data/trascrivi.db"],
    [t("about.uploaded"), t("about.uploaded_value")],
    [t("about.access"), t("about.access_value")],
    [t("about.compute"), hw.cuda_devices ? `CUDA · ${hw.gpu_name || "GPU"}` : "CPU"],
  ];

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" },
      el("a", { class: "crumb-back", href: "#/" }, icon("back"), t("nav.library")), " / ",
      el("span", { class: "here" }, t("foot.about"))),
    el("div", { class: "card" },
      el("p", { class: "empty-kicker" }, "Trascrivi"),
      el("h1", {}, t("about.title")),
      el("p", { class: "muted prose", style: "margin-top:6px" }, t("about.intro"))),
    el("div", { class: "card" },
      el("h2", {}, t("about.data_title")),
      el("table", { class: "details-table" }, el("tbody", {},
        facts.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, String(v))))))),
    el("div", { class: "card" },
      el("h2", {}, t("about.llm_title")),
      el("p", { class: "muted prose" }, t("about.llm_text")),
      el("p", { class: "hint" }, t("about.keys_hint"))),
    el("div", { class: "card" },
      el("h2", {}, t("about.keyboard")),
      el("div", { class: "stack" },
        el("div", { class: "split" }, el("kbd", {}, "/"), el("span", { class: "muted" }, t("about.kb_search"))),
        el("div", { class: "split" }, el("kbd", {}, "Ctrl"), el("kbd", {}, "S"),
          el("span", { class: "muted" }, t("about.kb_save"))),
        el("div", { class: "split" }, el("kbd", {}, "Esc"), el("span", { class: "muted" }, t("about.kb_close"))),
        el("div", { class: "split" }, el("kbd", {}, "← →"),
          el("span", { class: "muted" }, t("about.kb_tabs")))),
      el("p", { class: "hint" }, t("about.legal"))),
  ));
}

/* ── Novità ──────────────────────────────────────────────────────────────────
   Le note di versione stanno in web/changelog.json (inglese e italiano). La
   finestra si apre una volta per ogni versione nuova — chi aggiorna l'app vede
   cosa è cambiato — e resta raggiungibile dal link "Novità" nel footer. */
async function showChangelog(force = false) {
  let entries = [];
  try { entries = await (await fetch("/changelog.json", { cache: "no-cache" })).json(); } catch { return; }
  if (!entries.length) return;
  let seen = null;
  try { seen = localStorage.getItem("trascrivi-seen-version"); } catch { /* ignora */ }
  if (!force && seen === entries[0].version) return;
  const lang = I18N.lang in entries[0] ? I18N.lang : "en";
  const dlg = el("dialog", { class: "changelog", "aria-label": t("changelog.title") },
    el("h2", {}, t("changelog.title")),
    el("div", { class: "stack changelog-body" }, entries.slice(0, force ? 10 : 1).map((entry) => el("section", {},
      el("p", { class: "eyebrow" }, `${entry.version} · ${fmtDate(new Date(entry.date).getTime() / 1000).split(",")[0]}`),
      el("ul", { class: "md-list" }, (entry[lang] || entry.en || []).map((item) => el("li", {}, item)))))),
    el("div", { class: "split" }, el("button", { class: "primary", type: "button", onclick: () => dlg.close() },
      t("changelog.ok"))));
  dlg.addEventListener("close", () => {
    try { localStorage.setItem("trascrivi-seen-version", entries[0].version); } catch { /* ignora */ }
    dlg.remove();
  });
  document.body.append(dlg);
  dlg.showModal();
}

/* ── Avvio ───────────────────────────────────────────────────────────────── */
async function init() {
  // Le traduzioni prima di tutto: ogni vista le usa già al primo render.
  await loadI18n();
  applyStaticI18n();
  const langSelect = byId("langSelect");
  if (langSelect) {
    langSelect.value = I18N.lang;
    langSelect.addEventListener("change", (e) => setLang(e.currentTarget.value));
  }
  initTheme();

  // La navigazione si aggancia prima delle chiamate di rete: se l'utente cambia
  // hash mentre la prima richiesta è in volo, il render non va perso.
  window.addEventListener("hashchange", render);

  /* L'elenco dei modelli non passa più da `/api/health`: un health fallito (o
     lento) lasciava `MODELS` vuoto per tutta la vita della scheda, e il menu
     "Whisper model" restava senza voci. Qui l'errore è solo un warning, la
     vista "New lecture" ritenta con `loadModels()`. */
  await loadModels().catch((err) => console.warn("elenco modelli non disponibile:", err));

  const globalSearch = $("#globalSearch");
  globalSearch.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.value.trim()) {
      location.hash = `#/?q=${encodeURIComponent(e.target.value.trim())}`;
    }
  });
  // Un campo di ricerca che non si svuota è una trappola: Esc lo libera.
  globalSearch.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && e.target.value) { e.target.value = ""; }
  });
  $$("header [data-nav]").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.nav; }));

  await render();
  byId("whatsNew")?.addEventListener("click", (e) => { e.preventDefault(); showChangelog(true); });
  showChangelog();
  refreshJobBadge();
  setInterval(refreshJobBadge, 5000);
}

init().catch((err) => {
  console.error(err);
  // Se anche le traduzioni non sono arrivate, t() restituirebbe la chiave: si
  // ricade sul testo inglese scritto qui.
  const say = (key, english) => (t(key) === key ? english : t(key));
  document.getElementById("view").innerHTML =
    `<div class="card"><h2>${esc(say("boot.failed_title", "Could not start"))}</h2>
     <p class="muted prose">${esc(err.message)}</p>
     <p class="hint">${esc(say("boot.failed_hint", "The page loaded but the API did not answer. Check that the server is still running in the console window, then reload."))}</p></div>`;
});
