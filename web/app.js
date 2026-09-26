/* ============================================================================
   Trascrivi — SPA arancione-free: solo DOM, fetch e hash routing.
   Nessuna dipendenza, nessun build step: il server serve questi file così come
   sono. Il routing è a hash perché un `history.pushState` avrebbe richiesto un
   fallback lato server per ogni rotta.
   ========================================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* I modelli sono definiti dal backend (trascrivi_audio.MODELS): la UI non li
   duplica a mano, li carica in init(). */
const MODELS = [];

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

/* ── Toast ───────────────────────────────────────────────────────────────── */
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 8000 : 4000);
}

/* ── Dialoghi ────────────────────────────────────────────────────────────── */
function confirmDialog(title, text, okLabel = "Delete") {
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
function promptDialog({ title, label, value = "", okLabel = "Save", placeholder = "" }) {
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

const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString([], {
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
  { id: "auto", label: "System", icon: ICON_AUTO },
  { id: "light", label: "Light", icon: ICON_SUN },
  { id: "dark", label: "Dark", icon: ICON_MOON },
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
    iconBox.innerHTML = active.icon;
    nameBox.textContent = active.label;

    THEME_CHOICES.forEach((c) => {
      const btn = $(`[data-theme-choice="${c.id}"]`, menu);
      if (!btn) return;
      const on = c.id === choice;
      btn.setAttribute("aria-current", on ? "true" : "false");
      const ico = $(".theme-ico", btn);
      if (ico) ico.innerHTML = c.icon;
      const hint = $(".theme-hint", btn);
      if (hint) hint.textContent = on ? "on" : "";
    });

    trigger.setAttribute("aria-label", `Theme: ${active.label}. Choose a theme`);
    trigger.title = `Theme: ${active.label}`;
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
  return el("div", { class: "stack", role: "status", "aria-label": "Loading content" },
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
   titolo della trascrizione) e lo accoda dopo il caricamento. */
const ROUTE_TITLES = [
  [/^\/$/, "Library"],
  [/^\/p\/\d+\/new\/?$/, "New lecture"],
  [/^\/p\/\d+\/?$/, "Course"],
  [/^\/jobs\/?$/, "Jobs"],
  [/^\/t\/\d+\/?$/, "Transcript"],
  [/^\/costs\/?$/, "Costs"],
  [/^\/settings\/?$/, "Settings"],
  [/^\/about\/?$/, "About & privacy"],
];

const APP_TITLE = "Trascrivi";

function setDocTitle(part) {
  document.title = part ? `${part} · ${APP_TITLE}` : `${APP_TITLE} — lecture registry`;
}

function setRouteTitle(path, part) {
  const hit = ROUTE_TITLES.find(([re]) => re.test(path));
  setDocTitle(part || (hit ? hit[1] : "Not found"));
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
  setDocTitle("Not found");
  nav.replaceChildren(notFoundView(path));
}

/* ── Badge dei job in corso ──────────────────────────────────────────────── */
async function refreshJobBadge() {
  try {
    const { jobs } = await api("/jobs?status=running,queued&limit=20");
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
};

const icon = (name, cls = "") => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
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
      el("p", { class: "empty-kicker" }, "Error 404"),
      el("h1", {}, "This route is not in the registry"),
      el("p", { class: "muted prose" },
        "The address was typed by hand, or a link points at a page this version no longer has. ",
        "Nothing was lost: the registry lives in data/trascrivi.db."),
      el("p", {}, el("span", { class: "nf-path" }, `#${path}`)),
      el("div", { class: "nf-links" },
        el("a", { href: "#/" }, el("span", { class: "nf-k" }, "Library"), "All courses"),
        el("a", { href: "#/jobs" }, el("span", { class: "nf-k" }, "Jobs"), "Queue and progress"),
        el("a", { href: "#/settings" }, el("span", { class: "nf-k" }, "Settings"), "Defaults and providers"))));
}

function errorPanel(err) {
  return el("div", { class: "card" },
    el("h2", {}, "That request did not go through"),
    el("p", { class: "muted prose" }, err.message),
    el("p", { class: "hint" },
      "If the server was restarted, jobs that were running are marked as interrupted; ",
      "they can be requeued from the Jobs page."),
    el("div", { class: "split", style: "margin-top:12px" },
      el("button", { class: "primary", onclick: render }, "Try again"),
      el("a", { class: "btn", href: "#/jobs" }, "Open Jobs")));
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

const EXPORTS = [
  { format: "txt", label: "Plain text", meta: ".txt" },
  { format: "md", label: "Markdown", meta: ".md" },
  { format: "json", label: "Full data", meta: ".json" },
  { format: "segments", label: "With timestamps", meta: ".txt" },
];

const exportMenu = (t) => popupMenu("Export", [
  ...EXPORTS.map((e) => ({
    label: e.label, meta: e.meta,
    href: `/api/transcripts/${t.id}/export?format=${e.format}`,
  })),
  { sep: true },
  {
    label: "Copy full text", meta: "clipboard",
    onclick: async () => {
      try { await navigator.clipboard.writeText(t.text || ""); toast("Transcript copied", "ok"); }
      catch { toast("Clipboard unavailable", "err"); }
    },
  },
]);


/* ============================================================================
   Dashboard
   ========================================================================== */
async function viewDashboard(nav, params, token) {
  const [{ projects }, health] = await Promise.all([api("/projects"), api("/health")]);
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
      "aria-label": `${p.name}${p.code ? ` (${p.code})` : ""}: ${p.n_transcripts} transcript(s)`,
      onclick: open,
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      },
    },
      el("h2", {}, p.name),
      p.code ? el("span", { class: "badge" }, p.code) : null,
      // La descrizione resta sempre presente, anche vuota: serve a tenere
      // allineate le tre fasce (descrizione, metadati, numero) in ogni card.
      el("p", { class: "muted small" }, p.description || "No description yet."),
      el("div", { class: "split small muted" },
        el("span", {}, `${p.n_transcripts} transcript${p.n_transcripts === 1 ? "" : "s"}`),
        el("span", { "aria-hidden": "true" }, "·"),
        el("span", {}, `updated ${fmtDate(p.updated_at)}`)),
    );
    cards.append(card);
  });

  const newProject = el("form", { class: "card", onsubmit: submit },
    el("h2", {}, "New course"),
    el("p", { class: "hint", style: "margin:-6px 0 14px" },
      "One course per university subject. Name it the way it appears on the syllabus: ",
      "you will read it every time you look for a lecture."),
    el("div", { class: "row" },
      el("div", { class: "grow field" }, el("label", { for: "npName" }, "Course name"),
        el("input", { id: "npName", required: "required", placeholder: "Distributed systems" })),
      el("div", { class: "grow field" }, el("label", { for: "npCode" }, "Code (optional)"),
        el("input", { id: "npCode", placeholder: "DS-2025" }))),
    el("div", { class: "field" }, el("label", { for: "npDesc" }, "Description (optional)"),
      el("input", { id: "npDesc", placeholder: "Lectures, labs, exam info…" })),
    el("button", { class: "primary", type: "submit" }, "Create course"));

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
      toast(`Course “${p.name}” created`, "ok");
      location.hash = `#/p/${p.id}`;
    } catch (err) { toast(err.message, "err"); }
  }

  const searchResults = el("div");
  const term = (new URLSearchParams(location.hash.split("?")[1] || "")).get("q");
  if (term) {
    const { results } = await api(`/search?q=${encodeURIComponent(term)}`);
    setDocTitle(`“${term}” · Library`);
    searchResults.append(el("div", { class: "card" },
      el("div", { class: "split" },
        el("h2", { style: "margin:0;padding:0;border:0" }, "Search"),
        el("span", { class: "badge accent" }, `${results.length} hit${results.length === 1 ? "" : "s"}`),
        el("span", { class: "spacer" }),
        el("a", { class: "btn", href: "#/" }, "Clear")),
      el("p", { class: "hint", style: "margin-bottom:12px" }, `Matches for “${term}” across every transcript.`),
      results.length ? el("div", { class: "stack" }, results.map((r) =>
        el("div", { class: "split" },
          el("a", { href: `#/t/${r.id}?q=${encodeURIComponent(term)}` }, r.title),
          el("span", { class: "badge" }, r.project_name || "—"),
          el("span", { class: "spacer" }),
          el("span", { class: "small muted" }, "open"),
          el("p", { class: "small muted", style: "flex-basis:100%;margin:0",
            html: esc(r.snippet).replace(/\[\[/g, "<mark>").replace(/\]\]/g, "</mark>") }),
        ))) : el("p", { class: "muted" }, "Nothing matched. Try a shorter term or a different spelling.")));
  }

  // Riga di stato dell'hardware: badge separati quando i valori ci sono,
  // altrimenti il solo fatto che conta (CPU o GPU).
  const hwBadges = el("div", { class: "toolbar trailing" },
    hw.cuda_devices
      ? el("span", { class: "badge ok" }, `GPU · ${hw.gpu_name || "CUDA"}`)
      : el("span", { class: "badge" }, "CPU only"),
    hw.vram_free_gb != null ? el("span", { class: "badge" }, `${hw.vram_free_gb} GB VRAM free`) : null,
    el("span", { class: "badge" }, `${hw.cpu_count} threads`),
    hw.cached_models.length ? el("span", { class: "badge" }, `${hw.cached_models.length} model(s) in RAM`) : null);

  const totalTranscripts = projects.reduce((sum, p) => sum + (p.n_transcripts || 0), 0);

  if (!isCurrent(token)) return;
  nav.replaceChildren(
    el("div", { class: "stack" },
      el("div", { class: "card" },
        el("div", { class: "split hero" },
          el("div", { class: "grow" },
            el("p", { class: "empty-kicker" }, "Lecture registry"),
            el("h1", {}, "Library"),
            el("p", { class: "muted small hero-note" },
              projects.length
                ? `${projects.length} course${projects.length === 1 ? "" : "s"} · ${totalTranscripts} transcript${totalTranscripts === 1 ? "" : "s"}. Audio is transcoded on this machine only.`
                : "Create a course, drop a lecture recording into it, and follow the transcription in Jobs. Audio is transcoded on this machine only.")),
          hwBadges)),
      searchResults,
      projects.length
        ? el("p", { class: "eyebrow" }, "Courses", el("span", { class: "count" }, String(projects.length)))
        : null,
      projects.length ? cards : el("div", { class: "empty" },
        el("p", { class: "empty-kicker" }, "Empty registry"),
        el("p", {}, "No courses yet. Create the first one below — one per university subject works well."),
        el("div", { class: "empty-actions" },
          el("a", { class: "btn", href: "#/settings" }, "Check transcription defaults"))),
      newProject,
    ));
}

/* ============================================================================
   Vista progetto
   ========================================================================== */
async function viewProject(nav, params, projectId, token) {
  const [project, { transcripts }] = await Promise.all([
    api(`/projects/${projectId}`), api(`/transcripts?project_id=${projectId}`),
  ]);
  const { terms } = await api(`/terms?project_id=${projectId}`);
  setDocTitle(`${project.name} · Course`);

  const totalSeconds = transcripts.reduce((sum, t) => sum + (t.duration || 0), 0);

  const rows = transcripts.map((t) => el("tr", {},
    el("td", {}, el("a", { href: `#/t/${t.id}` }, t.title),
      t.description ? el("div", { class: "small muted" }, t.description) : null),
    el("td", { class: "nowrap small" }, fmtTs(t.duration)),
    el("td", { class: "nowrap small muted" }, t.language ? t.language.toUpperCase() : "—"),
    el("td", { class: "nowrap small muted" }, t.model || "—"),
    el("td", { class: "nowrap small muted" }, fmtDate(t.updated_at)),
    el("td", { class: "actions" },
      el("a", { class: "btn", href: `/api/transcripts/${t.id}/export?format=txt` }, "txt"),
      el("a", { class: "btn", href: `/api/transcripts/${t.id}/export?format=md` }, "md"),
      el("button", { class: "danger", onclick: () => removeTranscript(t) }, "Delete"))));

  async function removeTranscript(t) {
    if (!await confirmDialog("Delete transcript", `Delete “${t.title}”? This cannot be undone.`)) return;
    await api(`/transcripts/${t.id}`, { method: "DELETE" });
    toast("Transcript deleted", "ok");
    render();
  }

  const termForm = el("form", { class: "split", onsubmit: addTerm },
    el("input", { id: "termFind", placeholder: "misheard text", required: "required", style: "flex:1 1 140px" }),
    el("span", { class: "muted" }, "→"),
    el("input", { id: "termReplace", placeholder: "correct text", style: "flex:1 1 140px" }),
    el("label", { class: "check", style: "margin:0" },
      el("input", { id: "termAuto", type: "checkbox" }), el("span", {}, "auto")),
    el("button", { type: "submit" }, "Add"));

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
      toast("Term saved", "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  }

  const termList = el("div", { class: "stack" },
    terms.length ? terms.map((t) => el("div", { class: "split" },
      el("span", { class: "mono" }, t.find),
      el("span", { class: "muted", "aria-hidden": "true" }, "→"),
      el("span", { class: "mono" }, t.replace || "—"),
      t.auto ? el("span", { class: "badge ok" }, "auto") : null,
      el("span", { class: "spacer" }),
      el("button", { class: "ghost", title: `Remove “${t.find}”`,
        onclick: async () => { await api(`/terms/${t.id}`, { method: "DELETE" }); render(); } }, "✕"),
    )) : el("p", { class: "muted small" },
      "No terms yet. Auto terms are applied silently to every new transcript of this course."));

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" },
      el("a", { class: "crumb-back", href: "#/" }, icon("back"), "Library"), " / ",
      el("span", { class: "here" }, project.name)),
    el("div", { class: "card" },
      el("div", { class: "split hero" },
        el("div", { class: "grow" },
          el("p", { class: "empty-kicker" }, "Course"),
          el("h1", {}, project.name),
          project.code ? el("span", { class: "badge" }, project.code) : null),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "primary",
            onclick: () => { location.hash = `#/p/${projectId}/new`; } }, "+ New lecture"),
          el("button", { type: "button", onclick: editProject }, "Rename"),
          el("button", { type: "button", class: "danger", onclick: removeProject }, "Delete course"))),
      project.description ? el("p", { class: "muted" }, project.description) : null),
    transcripts.length ? el("div", { class: "stat-bar" },
      el("div", { class: "stat" },
        el("span", { class: "stat-k" }, "Transcripts"),
        el("span", { class: "stat-n" }, String(transcripts.length))),
      el("div", { class: "stat" },
        el("span", { class: "stat-k" }, "Audio transcribed"),
        el("span", { class: "stat-n" }, totalSeconds ? fmtTs(totalSeconds) : "—")),
      el("div", { class: "stat" },
        el("span", { class: "stat-k" }, "Glossary terms"),
        el("span", { class: "stat-n" }, String(terms.length))),
      el("div", { class: `stat ${terms.some((t) => t.auto) ? "is-accent" : "is-idle"}` },
        el("span", { class: "stat-k" }, "Auto-applied"),
        el("span", { class: "stat-n" }, String(terms.filter((t) => t.auto).length)))) : null,
    el("div", { class: "card" },
      el("h2", {}, "Transcripts"),
      transcripts.length ? el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Title"), el("th", {}, "Duration"), el("th", {}, "Lang"),
          el("th", {}, "Model"), el("th", {}, "Updated"), el("th", {}, ""))),
        el("tbody", {}, rows)) : el("div", { class: "empty" },
        el("p", { class: "empty-kicker" }, "No transcripts"),
        el("p", {}, "This course has no lectures yet. Add the first recording and it will appear here with its own transcript."),
        el("div", { class: "empty-actions" },
          el("button", { class: "primary",
            onclick: () => { location.hash = `#/p/${projectId}/new`; } }, "Add the first lecture")))),
    el("div", { class: "card" },
      el("h2", {}, "Course glossary"),
      el("p", { class: "muted small" },
        "Deterministic replacements applied to transcripts of this course. “auto” applies them silently when a transcript is created."),
      termList, el("hr"), termForm),
  ));

  async function editProject() {
    const name = await promptDialog({
      title: "Rename course", label: "Course name", value: project.name, okLabel: "Rename",
    });
    if (name === null) return;
    if (!name) return toast("A course needs a name", "err");
    try {
      await api(`/projects/${projectId}`, { method: "PATCH", body: { name } });
      toast("Course renamed", "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  }

  async function removeProject() {
    const n = transcripts.length;
    const detail = n
      ? `“${project.name}” and its ${n} transcript${n === 1 ? "" : "s"} will be deleted. This cannot be undone.`
      : `Delete “${project.name}”? This cannot be undone.`;
    if (!await confirmDialog("Delete course", detail, "Delete course")) return;
    try {
      const res = await api(`/projects/${projectId}`, { method: "DELETE" });
      toast(`Course deleted (${res.deleted_transcripts} transcript(s), ${res.deleted_sources} audio copy(ies))`, "ok");
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
  setDocTitle(`New lecture · ${project.name}`);

  let source = null;   // {id, name, original_path, bytes, stored_path}
  let pickedFile = null;

  // L'input file va costruito con il suo `onchange` già agganciato: finché non è
  // nel documento `byId("fileInput")` restituisce null e l'evento di selezione
  // andrebbe perso (era il bug: il dialogo si apriva e non succedeva niente).
  const fileInput = el("input", {
    id: "fileInput", type: "file", class: "hidden", accept: "audio/*,video/*",
    onchange: (e) => { if (e.target.files.length) setFile(e.target.files[0]); },
  });
  const drop = el("div", { class: "drop" },
    el("p", {}, "Drop a lecture audio/video here"),
    el("p", { class: "small" }, "or click to choose a file - mp4, mkv, mov, webm, mp3, wav, m4a, flac..."),
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
    if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
  });

  // Gli stati non sono più la stessa riga grigia: file scelto, file rifiutato
  // e attesa hanno colore e icona diversi, e si vedono a distanza.
  const fileStatus = el("div", { class: "muted small" }, "No file selected.");
  const nameInput = el("input", { id: "srcTitle", placeholder: "Lecture title" });
  const descInput = el("input", { id: "srcDesc", placeholder: "Optional description" });
  const pathInput = el("input", { id: "srcPath", placeholder: "C:\\Users\\you\\lectures\\lesson.mp4" });
  const pathStatus = el("div", { class: "muted small" },
    "The file is read where it is: no copy, nothing moved or deleted.");
  const progressFill = el("span");
  const progressBox = el("div", { class: "progress hidden", role: "progressbar",
    "aria-label": "Upload progress", "aria-valuemin": "0", "aria-valuemax": "100" }, progressFill);

  /* Uno stato può essere testo semplice (attesa) o un avviso con icona (esito).
     `kind` vuoto = testo nudo. */
  const setStatus = (node, kind, text) => {
    node.replaceChildren(...(kind ? [notice(kind, text)] : [text]));
  };

  // Una sola sorgente per volta: sezione "file" oppure sezione "path".
  const useFileBtn = el("button", { type: "button", class: "tab", "aria-selected": "true", onclick: () => updateMode("file") }, "My computer");
  const usePathBtn = el("button", { type: "button", class: "tab", "aria-selected": "false", onclick: () => updateMode("path") }, "Path on this machine");
  const modeBar = el("div", { class: "toolbar", role: "tablist" }, useFileBtn, usePathBtn);
  const sourceCard = el("div", { class: "card" }, el("h2", {}, "1 - Lecture file"), modeBar);

  const pathRow = el("div", { class: "row hidden" },
    el("div", { class: "grow" }, pathInput),
    el("button", { type: "button", onclick: probePath }, "Check & use"));

  function updateMode(mode) {
    const isFile = mode === "file";
    useFileBtn.setAttribute("aria-selected", String(isFile));
    usePathBtn.setAttribute("aria-selected", String(!isFile));
    drop.classList.toggle("hidden", !isFile);
    fileStatus.classList.toggle("hidden", !isFile);
    pathRow.classList.toggle("hidden", isFile);
    pathStatus.classList.toggle("hidden", isFile);
    sourceCard.replaceChildren(
      el("h2", {}, "1 - Lecture file"),
      modeBar,
      isFile ? el("div", {}, drop, fileStatus, progressBox) : el("div", {}, pathRow, pathStatus),
      el("p", { class: "hint" },
        isFile
          ? "The file is copied into data/ and removed once the transcription succeeds. Prefer Path to avoid the copy."
          : "The file stays where it is: it is never copied, moved or deleted."),
    );
  }

  function setFile(file) {
    pickedFile = file;
    // L'ultima scelta vince: un file selezionato sostituisce il path validato.
    source = null;
    setStatus(pathStatus, null, "The file is read where it is: no copy, nothing moved or deleted.");
    setStatus(fileStatus, "ok",
      `${file.name} · ${fmtBytes(file.size)} · ready. It is copied into data/sources and removed once the job succeeds.`);
    if (!nameInput.value) nameInput.value = file.name.replace(/\.[^.]+$/, "");
    syncNote();
  }

  async function probePath() {
    const value = pathInput.value.trim();
    if (!value) {
      pathInput.setAttribute("aria-invalid", "true");
      pathInput.focus();
      return;
    }
    pathInput.removeAttribute("aria-invalid");
    setStatus(pathStatus, null, "Checking…");
    try {
      const info = await api(`/media/probe?path=${encodeURIComponent(value)}`);
      if (!info.has_audio) {
        setStatus(pathStatus, "warn", `No audio track in ${info.name}. Pick a recording that has sound.`);
        source = null;
        return;
      }
      const created = await api(`/projects/${projectId}/sources`, { method: "POST", body: { path: value } });
      source = created.source;
      pickedFile = null;
      setStatus(fileStatus, null, "No file selected.");
      const length = info.duration ? fmtTs(info.duration) : "unknown length";
      let text = `${info.name} · ${fmtBytes(info.size)} · ${length} · will be read in place`;
      if (created.duplicate_of) text += ` (duplicate of source #${created.duplicate_of})`;
      setStatus(pathStatus, "ok", text);
      if (!nameInput.value) nameInput.value = info.name.replace(/\.[^.]+$/, "");
      syncNote();
    } catch (err) {
      source = null;
      setStatus(pathStatus, "err", `Rejected: ${err.message}`);
    }
  }

  async function uploadPicked() {
    if (!pickedFile) return null;
    const form = new FormData();
    form.append("file", pickedFile, pickedFile.name);
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
      xhr.onerror = () => reject(new Error("Upload failed"));
      xhr.send(form);
    });
    source = res.source;
    return source;
  }

  const opts = {
    model: el("select", { id: "optModel" }, MODELS.map((m) => el("option", { value: m, selected: m === settings.default_model ? "selected" : null }, m))),
    language: el("input", { id: "optLang", value: settings.default_language || "en" }),
    device: el("select", { id: "optDevice" },
      ["auto", "cuda", "cpu"].map((d) => el("option", { value: d, selected: d === (settings.default_device || "auto") ? "selected" : null }, d))),
    compute: el("select", { id: "optCompute" },
      ["auto", "float16", "int8", "int8_float16", "float32", "bfloat16"].map((c) => el("option", { value: c, selected: c === (settings.default_compute_type || "auto") ? "selected" : null }, c))),
    beam: el("input", { id: "optBeam", type: "number", min: "1", max: "20", value: "5" }),
    prompt: el("input", { id: "optPrompt", placeholder: "e.g. PyTorch, gradient descent, backpropagation, SGD" }),
    wordTs: el("input", { id: "optWordTs", type: "checkbox" }),
    noVad: el("input", { id: "optNoVad", type: "checkbox" }),
  };

  const agentToggle = el("input", { id: "optAgent", type: "checkbox" });
  const providerSelect = el("select", { id: "optProvider" },
    providers.filter((p) => p.enabled).map((p) => el("option", { value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null }, `${p.name} · ${p.model || "no model"}`)));
  const instruction = el("input", { id: "optInstruction", placeholder: "Extra instructions for the agent (optional)" });

  const summaryToggle = el("input", { id: "optSummary", type: "checkbox" });
  const summaryProvider = el("select", { id: "optSummaryProvider" },
    providers.filter((p) => p.enabled).map((p) => el("option", { value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null }, `${p.name} · ${p.model || "no model"}`)));
  const summaryStyle = el("select", { id: "optSummaryStyle" },
    [["study", "Study notes (default)"], ["brief", "Brief"], ["detailed", "Detailed"]]
      .map(([v, label]) => el("option", { value: v, selected: v === (settings.summary_style || "study") ? "selected" : null }, label)));
  const summaryInstruction = el("input", { id: "optSummaryInstruction", placeholder: "e.g. focus on the formulas (optional)" });
  const autoTerms = terms.filter((t) => t.auto);
  const submitNote = el("span", { class: "muted small" });

  const form = el("form", { class: "stack", onsubmit: submit },
    sourceCard,
    el("div", { class: "card" }, el("h2", {}, "2 - Metadata"),
      el("div", { class: "field" }, el("label", { for: "srcTitle" }, "Title"), nameInput),
      el("div", { class: "field" }, el("label", { for: "srcDesc" }, "Description (optional)"), descInput)),
    el("div", { class: "card" }, el("h2", {}, "3 - Transcription options"),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "optModel" }, "Whisper model"), opts.model),
        el("div", { class: "grow field" }, el("label", { for: "optLang" }, "Language (or “auto”)"), opts.language)),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "optDevice" }, "Device"), opts.device),
        el("div", { class: "grow field" }, el("label", { for: "optCompute" }, "Compute type"), opts.compute),
        el("div", { class: "grow field" }, el("label", { for: "optBeam" }, "Beam size"),
          opts.beam,
          el("p", { class: "hint" }, "Candidates kept while decoding. 5 is the balanced default; 1 is a bit faster (greedy), 10+ costs time for very little gain."))),
      el("details", { class: "adv" }, el("summary", {}, "Advanced"),
        el("div", { class: "field", style: "margin-top:10px" },
          el("label", { for: "optPrompt" }, "Initial prompt / course vocabulary"),
          opts.prompt,
          el("p", { class: "hint" }, "Names, acronyms and technical terms improve recognition a lot.")),
        el("label", { class: "check" }, opts.wordTs, el("span", {}, "Word-level timestamps (slower)")),
        el("label", { class: "check" }, opts.noVad, el("span", {}, "Disable VAD silence filtering"))),
      el("p", { class: "hint" }, "Timestamps are always stored per segment: they power resume, the segments tab and the editor.")),
    el("div", { class: "card" }, el("h2", {}, "4 - Correction agent (optional)"),
      providers.length ? el("div", { class: "stack" },
        el("label", { class: "check" }, agentToggle, el("span", {}, "Run the correction agent when the transcription finishes")),
        el("div", { class: "row" },
          el("div", { class: "grow field" }, el("label", { for: "optProvider" }, "Provider"), providerSelect),
          el("div", { class: "grow field" }, el("label", { for: "optInstruction" }, "Instruction"), instruction)),
        el("p", { class: "hint" }, "The agent proposes find→replace edits, it never rewrites the transcript. You review and accept them one by one later."),
      ) : el("p", { class: "muted" }, "No provider enabled. Configure one in Settings → Providers.")),
    el("div", { class: "card" }, el("h2", {}, "5 - Summary (optional)"),
      providers.length ? el("div", { class: "stack" },
        el("label", { class: "check" }, summaryToggle, el("span", {}, "Write the summary when the transcription finishes")),
        el("div", { class: "row" },
          el("div", { class: "grow field" }, el("label", { for: "optSummaryProvider" }, "Provider"), summaryProvider),
          el("div", { class: "grow field" }, el("label", { for: "optSummaryStyle" }, "Style"), summaryStyle)),
        el("div", { class: "field" },
          el("label", { for: "optSummaryInstruction" }, "Extra instructions"),
          summaryInstruction),
        el("p", { class: "hint" },
          "The summary is grounded: every point must carry a sentence quoted verbatim from the ",
          "transcript, and each quote is checked against the transcript before it is stored. ",
          "A quote that cannot be found is flagged instead of being trusted. You can also generate ",
          "it later from the Summary tab of the transcript."),
      ) : el("p", { class: "muted" }, "No provider enabled. Configure one in Settings → Providers.")),
    el("div", { class: "card" },
      el("div", { class: "split" },
        el("button", { class: "primary", type: "submit" }, "Start transcription"),
        submitNote),
      el("p", { class: "hint" }, "Jobs run one at a time. You can close this tab: the queue keeps going and the Jobs page shows progress.")),
    );

  // Stato iniziale: una sola sorgente visibile (file), l'altra raggiungibile
  // dallo switch in cima alla card.
  updateMode("file");

  /* La riga accanto al pulsante dice cosa manca per partire, invece di lasciare
     il pulsante muto e l'utente a indovinare. */
  function syncNote() {
    const bits = [];
    if (!source && pickedFile) bits.push("the file is uploaded first");
    if (!source && !pickedFile) bits.push("choose a file or a path to start");
    if (autoTerms.length) bits.push(`${autoTerms.length} auto glossary term(s) applied on creation`);
    submitNote.textContent = bits.join(" · ");
  }
  syncNote();

  async function submit(event) {
    event.preventDefault();
    const submitBtn = $("button[type=submit]", form);
    const title = nameInput.value.trim();

    if (!source && !pickedFile) {
      setStatus(fileStatus, "err", "Choose a file or a path first.");
      fileStatus.classList.remove("hidden");
      drop.classList.add("over");
      setTimeout(() => drop.classList.remove("over"), 900);
      return;
    }
    if (!title) {
      nameInput.setAttribute("aria-invalid", "true");
      nameInput.focus();
      return;
    }
    nameInput.removeAttribute("aria-invalid");

    submitBtn.disabled = true;
    submitBtn.textContent = "Queuing…";
    try {
      if (!source && pickedFile) {
        progressBox.classList.remove("hidden");
        setStatus(fileStatus, null, `Uploading ${pickedFile.name}…`);
        await uploadPicked();
        progressBox.classList.add("hidden");
        setStatus(fileStatus, "ok", `${pickedFile.name} uploaded.`);
      }
      if (!source) throw new Error("Choose a file or a path first.");
      const job = await api("/jobs", {
        method: "POST",
        body: {
          project_id: projectId,
          source_id: source.id,
          title,
          description: descInput.value,
          model: opts.model.value,
          language: opts.language.value,
          device: opts.device.value,
          compute_type: opts.compute.value,
          beam_size: Number(opts.beam.value) || 5,
          word_timestamps: opts.wordTs.checked,
          no_vad: opts.noVad.checked,
          initial_prompt: opts.prompt.value,
          // `run_agent` viene letto dal worker: il job agente nasce quando la
          // trascrizione esiste, così l'ordine è garantito.
          run_agent: agentToggle.checked,
          provider_id: providerSelect.value ? Number(providerSelect.value) : null,
          instruction: instruction.value,
          // Stesso meccanismo del correttore: il job del riassunto nasce quando
          // la trascrizione esiste già, così trova il testo pronto.
          run_summary: summaryToggle.checked,
          summary_provider_id: summaryProvider.value ? Number(summaryProvider.value) : null,
          summary_style: summaryStyle.value,
          summary_instruction: summaryInstruction.value,
        },
      });
      toast("Transcription queued", "ok");
      location.hash = `#/jobs?focus=${job.job.id}`;
    } catch (err) {
      toast(err.message, "err");
      submitBtn.disabled = false;
      submitBtn.textContent = "Start transcription";
    }
  }

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" },
      el("a", { class: "crumb-back", href: "#/" }, icon("back"), "Library"), " / ",
      el("a", { href: `#/p/${projectId}` }, project.name), " / ",
      el("span", { class: "here" }, "New lecture")),
    el("div", { class: "card" },
      el("p", { class: "empty-kicker" }, `Course · ${project.name}`),
      el("h1", {}, "New lecture"),
      el("p", { class: "muted small hero-note" },
        "Pick the recording, check the options and queue it. Nothing leaves this machine: the model runs locally.")),
    form));
}

/* ============================================================================
   Coda dei job
   ========================================================================== */
async function viewJobs(nav, params, token) {
  const focus = Number(params.get("focus") || 0);
  const container = el("div", { class: "stack" });
  let onlyActive = false;

  const statusClass = (s) => (
    s === "done" ? "ok"
      : s === "failed" ? "danger"
        : s === "running" ? "accent"
          : ""
  );

  const renderJobs = async () => {
    const { jobs, queue } = await api("/jobs?limit=60");

    const counts = {
      running: jobs.filter((j) => j.status === "running").length,
      queued: jobs.filter((j) => ["queued", "pending"].includes(j.status)).length,
      done: jobs.filter((j) => j.status === "done").length,
      failed: jobs.filter((j) => ["failed", "interrupted", "cancelled"].includes(j.status)).length,
    };

    const shown = onlyActive
      ? jobs.filter((j) => ["running", "queued", "pending"].includes(j.status))
      : jobs;

    const cards = shown.map((j) => {
      const running = j.status === "running";
      const queued = ["queued", "pending"].includes(j.status);
      const payload = j.payload || {};
      const pct = Math.round((j.progress || 0) * 100);
      const failed = ["failed", "interrupted", "cancelled"].includes(j.status);
      const barClass = `progress${j.status === "done" ? " done" : failed ? " failed" : ""}`;
      return el("div", { class: `card${failed ? " has-error" : ""}`, id: `job-${j.id}` },
        el("div", { class: "split" },
          el("h3", { style: "margin:0" },
            `#${j.id} · ${j.kind === "transcribe" ? (payload.title || "transcription") : "correction agent"}`),
          el("span", { class: `badge ${statusClass(j.status)}` }, j.status),
          j.speed && running ? el("span", { class: "badge" }, `${j.speed}x realtime`) : null,
          el("span", { class: "spacer" }),
          running ? el("button", { class: "danger", onclick: () => cancel(j.id) }, "Cancel") : null,
          failed ? el("button", { onclick: () => retry(j.id) }, "Retry") : null,
          j.transcript_id ? el("a", { class: "btn", href: `#/t/${j.transcript_id}` }, "Open") : null),
        el("div", {
          class: running && pct === 0 ? `${barClass} indeterminate` : barClass,
          style: "margin:12px 0 7px",
          role: "progressbar",
          "aria-label": `Job ${j.id} progress`,
          "aria-valuemin": "0", "aria-valuemax": "100",
          "aria-valuenow": running && pct === 0 ? null : String(pct),
        }, el("span", { style: `width:${pct}%` })),
        el("div", { class: "split small muted" },
          el("span", { class: "tabnum" }, running && pct === 0 ? "starting" : `${pct}%`),
          j.message ? el("span", {}, j.message) : null,
          j.eta_seconds && running ? el("span", {}, `· ETA ${fmtTs(j.eta_seconds)}`) : null,
          queued ? el("span", {}, "· waiting for the GPU") : null,
          el("span", { class: "spacer" }),
          el("span", {}, `created ${fmtDate(j.created_at)}`)),
        j.error ? notice("err", j.error) : null);
    });

    if (!isCurrent(token)) return;
    container.replaceChildren(
      el("div", { class: "card" },
        el("div", { class: "split hero" },
          el("div", { class: "grow" },
            el("p", { class: "empty-kicker" }, "Queue"),
            el("h1", {}, "Jobs"),
            el("p", { class: "muted small hero-note" },
              "One transcription at a time: the GPU is the bottleneck, so parallel jobs would only slow each other down.")),
          el("div", { class: "actions" },
            el("button", { onclick: renderJobs }, "Refresh")))),
      el("div", { class: "stat-bar" },
        el("div", { class: `stat ${counts.running ? "is-accent" : "is-idle"}` },
          el("span", { class: "stat-k" }, "Running"),
          el("span", { class: "stat-n" }, String(counts.running))),
        el("div", { class: `stat ${queue ? "is-accent" : ""}` },
          el("span", { class: "stat-k" }, "Queued"),
          el("span", { class: "stat-n" }, String(queue))),
        el("div", { class: "stat" },
          el("span", { class: "stat-k" }, "Done"),
          el("span", { class: "stat-n" }, String(counts.done))),
        el("div", { class: `stat ${counts.failed ? "is-danger" : "is-idle"}` },
          el("span", { class: "stat-k" }, "Failed"),
          el("span", { class: "stat-n" }, String(counts.failed)))),
      el("div", { class: "split" },
        el("div", { class: "toolbar", role: "tablist", "aria-label": "Job filter" },
          el("button", {
            class: "tab", type: "button",
            "aria-selected": onlyActive ? "false" : "true",
            onclick: (e) => { onlyActive = false; setFilter(e.currentTarget); },
          }, `All jobs · ${jobs.length}`),
          el("button", {
            class: "tab", type: "button",
            "aria-selected": onlyActive ? "true" : "false",
            onclick: (e) => { onlyActive = true; setFilter(e.currentTarget); },
          }, `Active · ${counts.running + counts.queued}`)),
        el("span", { class: "spacer" }),
        el("span", { class: "muted small" }, "Refreshes every 1.5s")),
      shown.length ? el("div", { class: "stack" }, cards)
        : el("div", { class: "empty" },
          el("p", { class: "empty-kicker" }, onlyActive ? "Nothing running" : "Queue empty"),
          el("p", {}, onlyActive
            ? "No transcription is running or waiting. Jobs appear here the moment you queue one."
            : "No jobs yet. Open a course, add a lecture recording and the transcription will show up here."),
          el("div", { class: "empty-actions" },
            el("a", { class: "btn", href: "#/" }, "Go to the library"))));
  };

  // Il filtro è locale: non rifà la richiesta, ridisegna solo la lista.
  function setFilter(btn) {
    $$(".tab", btn.parentElement).forEach((b) => b.setAttribute("aria-selected", "false"));
    btn.setAttribute("aria-selected", "true");
    renderJobs().catch((err) => toast(err.message, "err"));
  }

  async function cancel(id) {
    try { await api(`/jobs/${id}/cancel`, { method: "POST" }); toast("Cancelling…"); renderJobs(); }
    catch (err) { toast(err.message, "err"); }
  }
  async function retry(id) {
    try { await api(`/jobs/${id}/retry`, { method: "POST" }); toast("Requeued", "ok"); renderJobs(); }
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
async function viewTranscript(nav, params, transcriptId, token) {
  const query = params.get("q") || "";
  // `settings` non è globale: ogni vista che lo usa deve chiederlo. Senza,
  // `settings.default_provider_id` esplode già al render e la pagina non si apre.
  const [t, { jobs }, providersRes, settingsRes] = await Promise.all([
    api(`/transcripts/${transcriptId}`), api(`/jobs?limit=20`),
    api("/providers"), api("/settings"),
  ]);
  const providers = providersRes.providers;
  const settings = settingsRes.settings;

  const textArea = el("textarea", { class: "editor", spellcheck: "false", "aria-label": "Transcript text" });
  textArea.value = t.text;
  const saveState = el("span", { class: "small muted tabnum" }, "saved");
  let dirty = false;
  let timer = null;

  const words = () => textArea.value.split(/\s+/).filter(Boolean).length;
  const wordCount = el("span", { class: "muted small tabnum" }, `${words()} words`);

  const save = async () => {
    if (!dirty) return;
    saveState.textContent = "saving…";
    try {
      const updated = await api(`/transcripts/${transcriptId}`, { method: "PATCH", body: { text: textArea.value } });
      t.text = updated.text;
      t.segments = updated.segments;
      dirty = false;
      saveState.textContent = `saved ${new Date().toLocaleTimeString()}`;
    } catch (err) { saveState.textContent = "save failed"; toast(err.message, "err"); }
  };

  textArea.addEventListener("input", () => {
    dirty = true;
    saveState.textContent = "unsaved changes";
    wordCount.textContent = `${words()} words`;
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
      el("span", { class: "muted small" },
        "Editing the flat text. One line = one segment; timestamps are re-attached automatically on save."),
      el("span", { class: "spacer" }),
      saveState),
    textArea,
    el("div", { class: "split" },
      el("button", { class: "primary", onclick: save }, "Save now"),
      el("button", { onclick: copyAll }, "Copy all"),
      el("span", { class: "spacer" }),
      wordCount,
      el("span", { class: "muted small" }, `${(t.segments || []).length} segments`)));

  async function copyAll() {
    try { await navigator.clipboard.writeText(textArea.value); toast("Copied", "ok"); }
    catch { toast("Clipboard unavailable", "err"); }
  }

  const segRows = (t.segments || []).map((s, i) => {
    const norm = (s.text || "").toLowerCase();
    const hit = query && norm.includes(query.toLowerCase());
    return el("div", { class: `seg ${hit ? "hit" : ""}`, "data-i": String(i) },
      // Il timestamp è un comando: copia il minutaggio da citare altrove.
      el("button", {
        type: "button", class: "ts-btn", title: `Copy ${fmtTs(s.start)}`,
        onclick: () => copyStamp(fmtTs(s.start)),
      }, fmtTs(s.start)),
      el("p", { html: query ? highlight(s.text, query) : esc(s.text) }));
  });

  async function copyStamp(stamp) {
    try { await navigator.clipboard.writeText(stamp); toast(`${stamp} copied`, "ok"); }
    catch { toast("Clipboard unavailable", "err"); }
  }

  const segmentCount = (t.segments || []).length;
  const tabSegments = el("div", {},
    el("div", { class: "split", style: "margin-bottom:10px" },
      el("span", { class: "muted small tabnum" }, `${segmentCount} segment${segmentCount === 1 ? "" : "s"}`),
      el("span", { class: "muted small" }, "· click a timestamp to copy it"),
      el("span", { class: "spacer" }),
      el("a", { class: "btn", href: `/api/transcripts/${transcriptId}/export?format=segments` }, "Download with timestamps")),
    el("div", { class: "segments" }, segRows.length ? segRows
      : el("div", { class: "empty", style: "border:0;background:none" },
        el("p", { class: "empty-kicker" }, "No segments"),
        el("p", {}, "This transcript was saved as plain text without timestamps. The editor tab still works."))));

  const proposalBox = el("div", { class: "stack" });
  const tabProposals = el("div", { class: "stack" },
    el("div", { class: "split" },
      el("span", { class: "muted small" }, "The agent returns a list of edits: you decide which ones to apply."),
      el("span", { class: "spacer" }),
      providers.filter((p) => p.enabled).length
        ? el("select", { id: "fixProvider", "aria-label": "Provider for the correction agent" },
          providers.filter((p) => p.enabled).map((p) => el("option", { value: p.id }, `${p.name} · ${p.model || "?"}`)))
        : el("span", { class: "badge warn" }, "no provider enabled"),
      el("button", { class: "primary", onclick: runFix,
        disabled: providers.filter((p) => p.enabled).length ? null : "disabled" }, "Run agent"),      el("button", { onclick: loadProposals }, "Refresh")),
    el("div", { id: "proposalBox" }, proposalBox));

  async function runFix() {
    const providerId = Number(byId("fixProvider")?.value) || null;
    try {
      const res = await api(`/transcripts/${transcriptId}/fix`, { method: "POST", body: { provider_id: providerId } });
      toast(`Agent job #${res.job.id} queued — follow it in Jobs`, "ok");
      location.hash = `#/jobs?focus=${res.job.id}`;
    } catch (err) { toast(err.message, "err"); }
  }

  const selected = new Set();

  async function loadProposals() {
    const { proposals } = await api(`/transcripts/${transcriptId}/proposals`);
    proposalBox.replaceChildren();
    if (!proposals.length) {
      proposalBox.append(el("div", { class: "empty" }, "No proposals. Run the agent to generate them."));
      return;
    }
    const groups = {};
    proposals.forEach((p) => { (groups[p.kind || "other"] ||= []).push(p); });

    Object.entries(groups).forEach(([kind, items]) => {
      const body = el("div");
      const box = el("details", { open: "open" },
        el("summary", {}, `${kind} · ${items.length}`), body);
      items.forEach((p) => {
        const check = el("input", { type: "checkbox" });
        check.checked = p.status !== "rejected";
        if (p.status === "accepted") check.disabled = true;
        check.addEventListener("change", () => {
          if (check.checked) selected.add(p.id); else selected.delete(p.id);
          updateCount();
        });
        if (check.checked && !check.disabled) selected.add(p.id);
        const row = el("div", { class: `prop flag-${p.flag}` },
          check,
          el("div", {},
            el("div", { class: "change" },
              el("del", {}, p.find), " → ", el("ins", {}, p.replace || "∅")),
            el("div", { class: "small muted" },
              p.reason || "",
              p.flag !== "ok" ? el("span", { class: `badge ${p.flag === "ambiguous" ? "warn" : ""}` }, p.flag) : null,
              p.segment_index != null ? el("span", { class: "badge" }, `segment ${p.segment_index}`) : null,
              p.confidence != null ? el("span", { class: "badge" }, `${Math.round(p.confidence * 100)}%`) : null),
            p.flag === "ambiguous" && p.status === "pending"
              ? el("p", { class: "small muted" }, "This text occurs more than once. Pick the segment in the list below if you want it applied.")
              : null));
        body.append(row);
      });
      proposalBox.append(box);
    });

    const counter = el("span", { class: "badge" }, "");
    const targets = {};
    const ambiguous = proposals.filter((p) => p.flag === "ambiguous" && p.status === "pending");
    if (ambiguous.length) {
      const sel = el("div", { class: "stack" });
      ambiguous.forEach((p) => {
        const segSelect = el("select", {});
        segSelect.append(el("option", { value: "" }, "— skip (ambiguous) —"));
        (t.segments || []).forEach((s, i) => {
          if (normalize(s.text).includes(normalize(p.find))) {
            segSelect.append(el("option", { value: String(i) }, `#${i} · ${fmtTs(s.start)} · ${s.text.slice(0, 60)}`));
          }
        });
        segSelect.addEventListener("change", () => {
          if (segSelect.value === "") delete targets[p.id];
          else targets[p.id] = Number(segSelect.value);
        });
        sel.append(el("div", {}, el("span", { class: "small muted" }, `“${p.find}” → “${p.replace}”`), segSelect));
      });
      proposalBox.append(el("details", { class: "card" }, el("summary", {}, `${ambiguous.length} ambiguous edit(s) — choose a segment`), sel));
    }

    const updateCount = () => {
      counter.textContent = `${selected.size} selected`;
    };
    updateCount();

    proposalBox.append(el("div", { class: "split" },
      counter,
      el("button", { class: "primary", onclick: () => apply(proposals, targets) }, "Apply selected"),
      el("button", { onclick: () => preview(proposals, targets) }, "Preview diff"),
      el("button", { onclick: () => decide(proposals.map((p) => p.id), "rejected") }, "Reject all"),
      el("span", { class: "spacer" }),
      el("button", { class: "ghost", onclick: async () => { await api(`/transcripts/${transcriptId}/proposals/undo`, { method: "POST" }); loadProposals(); } }, "Clear pending")));
  }

  async function preview(proposals, targets) {
    const ids = [...selected];
    if (!ids.length) return toast("Nothing selected", "err");
    const res = await api(`/transcripts/${transcriptId}/proposals/preview`, { method: "POST", body: { ids, targets } });
    const box = byId("diffBox") || el("pre", { id: "diffBox", class: "export" });
    box.textContent = res.diff.map((d) => `${d.kind} ${d.text}`).join("\n") || "(no change)";
    if (!byId("diffBox")) proposalBox.append(box);
  }

  async function apply(proposals, targets) {
    const ids = [...selected];
    if (!ids.length) return toast("Nothing selected", "err");
    try {
      const res = await api(`/transcripts/${transcriptId}/proposals/accept`, { method: "POST", body: { ids, targets } });
      toast(`Applied ${res.applied} replacement(s)`, "ok");
      if (res.skipped.length) toast(`${res.skipped.length} skipped (ambiguous, unmatched or conflicting)`);
      t.text = res.text;
      t.segments = res.segments;
      textArea.value = res.text;
      loadProposals();
    } catch (err) { toast(err.message, "err"); }
  }

  async function decide(ids, status) {
    await api(`/transcripts/${transcriptId}/proposals/${status === "rejected" ? "reject" : "accept"}`, { method: "POST", body: { ids } });
    loadProposals();
  }

  const normalize = (s) => (s || "").replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, " ").trim().toLowerCase();

  const detailsCard = el("div", { class: "card" },
    el("h2", {}, "Details"),
    el("table", { class: "details-table" }, el("tbody", {},
      row("Course", t.project_name || "—"),
      row("Language", (t.language || "—").toUpperCase()),
      row("Model", t.model || "—"),
      row("Device", t.device || "—"),
      row("Duration", fmtTs(t.duration)),
      row("Created", fmtDate(t.created_at)),
      row("Updated", fmtDate(t.updated_at)),
      row("Source", t.source_ref || (t.source_id ? `source #${t.source_id}` : "—")))));

  const historyCard = el("div", { class: "card" },
    el("h2", {}, "History"),
    (t.revisions || []).length
      ? el("div", { class: "stack" }, t.revisions.map((r) => el("div", { class: "split" },
        el("span", { class: "badge" }, r.kind),
        el("span", { class: "tabnum" }, `${r.n_changes} change${r.n_changes === 1 ? "" : "s"}`),
        r.summary ? el("span", { class: "muted" }, r.summary) : null,
        el("span", { class: "spacer" }),
        el("span", { class: "muted small tabnum" }, fmtDate(r.created_at)))))
      : el("div", { class: "empty", style: "border:0;background:none;padding:24px 12px" },
        el("p", { class: "empty-kicker" }, "No edits yet"),
        el("p", {}, "Revisions appear here after you save an edit or apply a correction.")));

  // ── Tab Summary ────────────────────────────────────────────────────────────
  const summaryHost = el("div", { class: "stack" });
  const summaryBar = el("div", { class: "split" });
  const tabSummary = el("div", { class: "stack" }, summaryBar, summaryHost);

  let summaryList = [];
  let summaryFull = null;
  let activeSummaryId = t.latest_summary_id || null;

  const sumProvider = el("select", { id: "sumProvider", "aria-label": "Provider for the summary" },
    providers.filter((p) => p.enabled).map((p) => el("option", {
      value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null,
    }, `${p.name} · ${p.model || "no model"}`)));
  const sumStyle = el("select", { id: "sumStyle", "aria-label": "Summary style" },
    [["study", "Study notes"], ["brief", "Brief"], ["detailed", "Detailed"]]
      .map(([v, label]) => el("option", {
        value: v, selected: v === (settings.summary_style || "study") ? "selected" : null,
      }, label)));
  const sumInstruction = el("input", {
    id: "sumInstruction", placeholder: "Extra instructions (optional)",
    style: "min-width:220px",
  });

  // Stato del job, non una costante: il polling lo aggiorna mentre è in corso
  // (vedi `jobTick` più sotto), altrimenti dopo il click la tab continuerebbe a
  // dire "No summary yet" fino a un ricaricamento della pagina.
  let summaryJob = jobs.find((j) => j.kind === "llm_summary" && j.transcript_id === t.id
    && ["queued", "running"].includes(j.status)) || null;

  function renderSummaryBar() {
    const hasProvider = providers.some((p) => p.enabled);
    setChildren(summaryBar,
      ...(hasProvider ? [sumProvider, sumStyle, sumInstruction] : []),
      el("button", {
        class: "primary",
        disabled: hasProvider && !summaryJob ? null : "disabled",
        onclick: runSummary,
      }, summaryJob ? "Writing…" : (summaryFull ? "Regenerate" : "Generate summary")),
      summaryList.length > 1
        ? el("select", { id: "sumHistory", "aria-label": "Previous versions",
            onchange: (e) => selectSummary(Number(e.currentTarget.value)) },
            summaryList.map((s) => el("option", {
              value: s.id, selected: s.id === activeSummaryId ? "selected" : null,
            }, `${fmtDate(s.created_at)} · ${s.style} · ${s.n_words || 0}${s.target_words ? `/${s.target_words}` : ""} words`)))
        : null,
      el("span", { class: "spacer" }),
      summaryFull
        ? el("a", { class: "btn", href: `/api/summaries/${summaryFull.id}/export?format=md` },
            "Download .md")
        : null,
      summaryFull ? el("button", { onclick: removeSummary }, "Delete") : null,
    );
  }

  async function runSummary() {
    if (!providers.some((p) => p.enabled)) {
      toast("Configure an LLM provider first", "err");
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
      summaryJob = res.job || { id: null, status: "queued", progress: 0, message: "queued" };
      toast("Summary queued", "ok");
      renderJobBanners();
      renderSummaryBar();
      renderSummaryBody();
      startJobPoll();
    } catch (err) { toast(err.message, "err"); }
  }

  async function removeSummary() {
    if (!summaryFull) return;
    if (!(await confirmDialog("Delete summary", "Delete this summary? This cannot be undone."))) return;
    try {
      await api(`/summaries/${summaryFull.id}`, { method: "DELETE" });
      toast("Summary deleted", "ok");
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
        summaryBodyRefs.msg.textContent = summaryJob.message || "Writing the summary…";
        summaryBodyRefs.meta.textContent = `job #${summaryJob.id} · ${pct}%${elapsed(summaryJob)}`;
        summaryBodyRefs.fill.style.width = `${pct}%`;
      }
      return;
    }
    summaryBodyKey = key;
    summaryBodyRefs = null;

    if (summaryJob && !summaryFull) {
      const pct = Math.round((summaryJob.progress || 0) * 100);
      const msg = el("p", {}, summaryJob.message || "Writing the summary…");
      const meta = el("p", { class: "muted small tabnum" },
        `job #${summaryJob.id} · ${pct}%${elapsed(summaryJob)}`);
      const fill = el("span", { style: `width:${pct}%` });
      summaryBodyRefs = { msg, meta, fill };
      setChildren(summaryHost, el("div", { class: "card" },
        el("div", { class: "split" },
          el("span", { class: "spin" }),
          el("div", { class: "grow" }, msg, meta),
          el("a", { href: `#/jobs?focus=${summaryJob.id}` }, "Follow in Jobs")),
        el("div", { class: "progress", role: "progressbar", style: "margin-top:10px" }, fill)));
      return;
    }
    if (!summaryFull) {
      setChildren(summaryHost, el("div", { class: "empty" },
        el("p", { class: "empty-kicker" }, "No summary yet"),
        el("p", {}, "Turn this lecture into study notes: a single Markdown document with the topics, ",
          "the definitions, lists and tables where they help. You can download it as a .md file.")));
      return;
    }
    const s = summaryFull;
    const meta = [
      s.style,
      s.model || s.provider,
      // Obiettivo e risultato insieme: è il numero che dice se il budget di
      // lunghezza ha retto, e quanto la lezione è stata compressa.
      s.n_words ? `${s.n_words} words${s.target_words ? ` of ${s.target_words}` : ""}` : null,
      s.source_words && s.n_words ? `1:${(s.source_words / s.n_words).toFixed(1)}` : null,
      s.cost_usd ? `~$${s.cost_usd.toFixed(4)}` : null,
      s.elapsed_s ? `${Math.round(s.elapsed_s)}s` : null,
      s.edited ? "edited" : null,
    ].filter(Boolean).join(" · ");

    // Niente "Download .md" qui: il documento è la card sotto il titolo e il
    // comando è già nella testata della card. La prosa usa tutta la larghezza.
    setChildren(summaryHost,
      el("div", { class: "card" },
        el("h2", {}, s.title || "Summary"),
        el("p", { class: "muted small tabnum" }, meta),
        // Il modello ha finito i token a metà frase: il documento c'è, ma non è
        // completo, e dirlo è l'unico modo perché non venga preso per finito.
        s.truncated ? notice("warn", "The model ran out of output tokens: these notes stop "
          + "early. Regenerate them with a shorter style, or raise the output cap in Settings.") : null,
        el("div", { class: "md", html: renderMarkdown(s.markdown || "") })));
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

  const tabInfo = el("div", { class: "stack" }, detailsCard, historyCard);

  function row(k, v) { return el("tr", {}, el("th", {}, k), el("td", {}, String(v))); }

  const tabs = {
    content: tabContent,
    segments: tabSegments,
    proposals: tabProposals,
    summary: tabSummary,
    info: tabInfo,
  };
  const panel = el("div", { role: "tabpanel", "aria-label": "Transcript view" }, tabs.content);

  const tabBar = el("div", { class: "toolbar", role: "tablist", "aria-label": "Transcript sections" },
    Object.entries(tabs).map(([key, node], i) => el("button", {
      class: "tab", role: "tab", id: `tab-${key}`,
      "aria-selected": key === "content" ? "true" : "false",
      tabindex: key === "content" ? "0" : "-1",
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
    }, key[0].toUpperCase() + key.slice(1))));

  /* ── Lavori in corso su questa trascrizione ─────────────────────────────────
     Un elemento stabile che il polling riscrive. Prima il banner dell'agente era
     un figlio condizionale costruito una volta sola, quindi compariva solo
     ricaricando la pagina: lo stesso problema del riassunto. */
  let runningFix = jobs.find((j) => j.kind === "llm_fix" && j.transcript_id === t.id
    && ["queued", "running"].includes(j.status)) || null;
  const jobsHost = el("div", { class: "stack" });
  let bannerKey = "";
  let bannerEntries = [];

  function renderJobBanners() {
    const active = [["Correction agent", runningFix], ["Summary", summaryJob]]
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
      el("a", { href: `#/jobs?focus=${job.id}` }, "Follow in Jobs")));
    return { node, msg, meta };
  }

  function updateBanner(entry, label, job) {
    entry.job = job;
    entry.msg.textContent = `${label}: ${job.message || "running"}`;
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
    if (jobPoll || !(runningFix || summaryJob)) return;
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

    const mine = (kind) => latest.find((j) => j.kind === kind && j.transcript_id === t.id
      && ["queued", "running"].includes(j.status)) || null;
    const finishedSummary = summaryJob && !mine("llm_summary");
    runningFix = mine("llm_fix");
    summaryJob = mine("llm_summary");

    renderJobBanners();
    if (finishedSummary) {
      // Il documento è pronto: si carica da solo invece di chiedere un refresh.
      await loadSummaries();
      if (isCurrent(token)) toast("Summary ready", "ok");
    } else {
      renderSummaryBar();
      renderSummaryBody();
    }
    if (!runningFix && !summaryJob) stopJobPoll();
  }

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" },
      el("a", { class: "crumb-back", href: "#/" }, icon("back"), "Library"), " / ",
      el("a", { href: `#/p/${t.project_id}` }, t.project_name || "Course"), " / ",
      el("span", { class: "here" }, t.title)),
    el("div", { class: "card" },
      el("div", { class: "split hero" },
        el("div", { class: "grow" },
          el("p", { class: "empty-kicker" }, `${t.project_name || "Course"} · ${fmtDate(t.created_at)}`),
          el("h1", {}, t.title),
          el("p", { class: "muted small hero-note" },
            `${fmtTs(t.duration)} of audio · ${t.language ? t.language.toUpperCase() : "unknown language"} · `,
            `${(t.segments || []).length} segments · ${words()} words`)),
        el("div", { class: "actions" },
          exportMenu(t),
          el("button", { onclick: rename }, "Rename"),
          el("button", { class: "danger", onclick: remove }, "Delete"))),
      t.description ? el("p", { class: "muted" }, t.description) : null),
    jobsHost,
    tabBar, panel));

  await loadProposals();
  await loadSummaries();
  renderJobBanners();
  startJobPoll();
  onViewTeardown(stopJobPoll);

  async function rename() {
    const title = await promptDialog({
      title: "Rename transcript", label: "Title", value: t.title, okLabel: "Rename",
    });
    if (title === null) return;
    if (!title) return toast("A transcript needs a title", "err");
    try {
      await api(`/transcripts/${transcriptId}`, { method: "PATCH", body: { title } });
      toast("Transcript renamed", "ok");
      render();
    } catch (err) { toast(err.message, "err"); }
  }
  async function remove() {
    if (!await confirmDialog("Delete transcript", `Delete “${t.title}”?`)) return;
    await api(`/transcripts/${transcriptId}`, { method: "DELETE" });
    toast("Deleted", "ok");
    location.hash = `#/p/${t.project_id}`;
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
  const [{ settings, disk }, { providers }, { models, cached }] = await Promise.all([
    api("/settings"), api("/providers"), api("/models"),
  ]);

  const field = (label, id, value, type = "text") => el("div", { class: "field" },
    el("label", { for: id }, label), el("input", { id, type, value: value ?? "" }));

  const form = el("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    try {
      await api("/settings", { method: "PUT", body: {
        default_model: byId("sModel").value,
        default_language: byId("sLang").value,
        default_device: byId("sDevice").value,
        default_compute_type: byId("sCompute").value,
        default_provider_id: Number(byId("sProvider").value) || null,
        flush_every: Number(byId("sFlush").value) || 10,
        llm_chunk_chars: Number(byId("sChunk").value) || 8000,
        summary_max_tokens: Number(byId("sSumTokens").value) || 8000,
        summary_style: byId("sSumStyle").value,
        summary_language: byId("sSumLang").value.trim(),
        backup_keep: Number(byId("sKeep").value) || 5,
      } });
      toast("Settings saved", "ok");
    } catch (err) { toast(err.message, "err"); }
  } },
    el("div", { class: "card" }, el("h2", {}, "Transcription defaults"),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sModel" }, "Model"),
          el("select", { id: "sModel" }, models.map((m) => el("option", { value: m, selected: m === settings.default_model ? "selected" : null }, m)))),
        el("div", { class: "grow field" }, el("label", { for: "sLang" }, "Language"),
          el("input", { id: "sLang", value: settings.default_language }))),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sDevice" }, "Device"),
          el("select", { id: "sDevice" }, ["auto", "cuda", "cpu"].map((d) => el("option", { value: d, selected: d === settings.default_device ? "selected" : null }, d)))),
        el("div", { class: "grow field" }, el("label", { for: "sCompute" }, "Compute type"),
          el("select", { id: "sCompute" }, ["auto", "float16", "int8", "int8_float16", "float32", "bfloat16"].map((c) => el("option", { value: c, selected: c === settings.default_compute_type ? "selected" : null }, c))))),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sFlush" }, "Flush interval (s of audio)"),
          el("input", { id: "sFlush", type: "number", min: "5", max: "120", value: settings.flush_every })),
        el("div", { class: "grow field" }, el("label", { for: "sChunk" }, "Agent chunk size (characters)"),
          el("input", { id: "sChunk", type: "number", min: "2000", max: "30000", step: "500", value: settings.llm_chunk_chars })),
        el("div", { class: "grow field" }, el("label", { for: "sKeep" }, "Backups to keep"),
          el("input", { id: "sKeep", type: "number", min: "1", max: "50", value: settings.backup_keep }))),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sProvider" }, "Default LLM provider"),
          el("select", { id: "sProvider" }, [el("option", { value: "" }, "— none —")].concat(
            providers.map((p) => el("option", { value: p.id, selected: p.id === settings.default_provider_id ? "selected" : null }, `${p.name} · ${p.model || "?"}`)))))),
      el("button", { class: "primary", type: "submit" }, "Save settings")),

    el("div", { class: "card" }, el("h2", {}, "Summaries"),
      el("div", { class: "row" },
        el("div", { class: "grow field" }, el("label", { for: "sSumStyle" }, "Default style"),
          el("select", { id: "sSumStyle" },
            [["study", "Study notes"], ["brief", "Brief"], ["detailed", "Detailed"]]
              .map(([v, label]) => el("option", { value: v, selected: v === (settings.summary_style || "study") ? "selected" : null }, label)))),
        el("div", { class: "grow field" }, el("label", { for: "sSumLang" }, "Output language"),
          el("input", { id: "sSumLang", value: settings.summary_language || "", placeholder: "empty = same as the transcript" })),
        el("div", { class: "grow field" }, el("label", { for: "sSumTokens" }, "Max output tokens"),
          el("input", { id: "sSumTokens", type: "number", min: "1000", max: "32000", step: "500", value: settings.summary_max_tokens }))),
      el("p", { class: "hint" },
        "The summary is written in a single call: the whole transcript goes in, which is why the ",
        "length budget can be respected. This is a ceiling for the output only — the real limit is ",
        "the word budget, which follows the style and the length of the transcript."),
      el("button", { class: "primary", type: "submit" }, "Save settings")),

    el("div", { class: "card" }, el("h2", {}, "LLM providers"),
      el("p", { class: "muted small" }, "Keys are encrypted at rest with a key stored in data/secret.key and are never returned by the API."),
      el("div", { class: "stack" }, providers.map((p) => providerRow(p)))),

    el("div", { class: "card" }, el("h2", {}, "Correction agent"),
      el("p", { class: "muted small" }, "The agent never rewrites the transcript: it returns anchored find -> replace edits that you review and accept one by one. It only proposes words the recogniser heard wrong when the sentence makes the intended word obvious (acronyms, names, technical terms), never style or punctuation preferences. These are the exact instructions it receives."),
      agentPromptBox()),

    el("div", { class: "card" }, el("h2", {}, "Storage"),
      el("div", { class: "stat-bar", style: "margin-bottom:14px" },
        el("div", { class: "stat" },
          el("span", { class: "stat-k" }, "Audio copies"),
          el("span", { class: "stat-n" }, fmtBytes(disk.sources_bytes)),
          el("span", { class: "stat-k" }, `${disk.sources_files} file${disk.sources_files === 1 ? "" : "s"}`)),
        el("div", { class: "stat" },
          el("span", { class: "stat-k" }, "Data directory"),
          el("span", { class: "stat-n" }, fmtBytes(disk.data_bytes))),
        el("div", { class: "stat" },
          el("span", { class: "stat-k" }, "Free on disk"),
          el("span", { class: "stat-n" }, fmtBytes(disk.free_bytes)))),
      el("p", { class: "hint" }, "Files used by path are never copied and never deleted by the app. Copies uploaded from the browser are removed after a successful transcription."),
      el("div", { class: "split", style: "margin-top:12px" },
        el("button", { type: "button", onclick: async () => {
          try {
            const r = await api("/maintenance/backup");
            toast(`Database backed up to ${r.path || "data/backups"}`, "ok");
          } catch (err) { toast(err.message, "err"); }
        } }, "Back up database"),
        el("button", { type: "button", onclick: async () => {
          try {
            const r = await api("/maintenance/purge-sources", { method: "POST" });
            toast(`Removed ${r.purged} audio copy(ies)`, "ok");
            render();
          } catch (err) { toast(err.message, "err"); }
        } }, "Delete all audio copies"),
        el("button", { type: "button", onclick: async () => {
          try {
            const r = await api("/models/unload", { method: "POST" });
            toast(`Unloaded ${r.unloaded} model(s)`);
          } catch (err) { toast(err.message, "err"); }
        } }, "Unload models from RAM"))),

    el("div", { class: "card" }, el("h2", {}, "Runtime"),
      el("div", { class: "split small" },
        cached.length
          ? cached.map((m) => el("span", { class: "badge ok" }, `${m.model} · ${m.device}`))
          : el("span", { class: "badge" }, "no model loaded")),
      el("p", { class: "hint" }, "Models stay in RAM between jobs to skip the load time. Keep a maximum of two: the VRAM is shared.")),
  );

  function agentPromptBox() {
    const box = el("div", { class: "stack" });
    const badges = el("div", { class: "split small" });
    const prompt = el("pre", { class: "export" }, "loading...");
    box.append(badges, el("details", { class: "adv" },
      el("summary", {}, "System prompt"), prompt));
    api("/agent/prompt").then((info) => {
      prompt.textContent = info.system_prompt;
      badges.replaceChildren(
        el("span", { class: "badge" }, `max output: ${info.max_output_tokens} tokens`),
        el("span", { class: "badge" }, `temperature: ${info.temperature}`),
        el("span", { class: "badge" }, `chunk: ${info.chunk_chars} chars`),
        el("span", { class: "badge" }, `timeout: ${Math.round(info.timeout_seconds)}s`));
    }).catch(() => { prompt.textContent = "Could not load the prompt."; });
    return box;
  }

  function providerRow(p) {
    const keyInput = el("input", { type: "password", placeholder: p.has_key ? "•••••• configured" : "API key", autocomplete: "off" });
    const modelInput = el("input", { value: p.model || "", placeholder: "model id" });
    const fallbackInput = el("input", { value: p.fallback_model || "", placeholder: "fallback model (optional)" });
    const urlInput = el("input", { value: p.base_url, placeholder: "base url" });
    const enabled = el("input", { type: "checkbox" });
    enabled.checked = !!p.enabled;
    const status = el("span", { class: "small muted" }, "");
    const keyBadge = p.key_state === "unreadable"
      ? el("span", { class: "badge danger" }, "key unreadable")
      : p.has_key
        ? el("span", { class: "badge ok" }, "key stored")
        : el("span", { class: "badge" }, "no key");
    const isReasoning = /reason|think|r1|o[1-4]\b/i.test(p.model || "");
    return el("div", { class: "card" },
      el("div", { class: "split" },
        el("h3", { style: "margin:0" }, p.name),
        keyBadge,
        el("label", { class: "check", style: "margin:0" }, enabled, el("span", {}, "enabled")),
        el("span", { class: "spacer" }),
        el("button", { type: "button", onclick: async () => { status.textContent = "testing…"; status.textContent = JSON.stringify(await api(`/providers/${p.id}/test`, { method: "POST", body: { model: modelInput.value } })); } }, "Test")),
      p.key_state === "unreadable"
        ? notice("err", "The saved key can no longer be decrypted (data/secret.key changed). Paste the key again and press Save.")
        : null,
      isReasoning
        ? notice("warn", "This looks like a reasoning model. It may spend the whole token budget thinking and return nothing: the agent would then fall back to the fallback model below.")
        : null,
      el("div", { class: "row", style: "margin-top:8px" },
        el("div", { class: "grow field" }, el("label", {}, "Base URL"), urlInput),
        el("div", { class: "grow field" }, el("label", {}, "Model"), modelInput),
        el("div", { class: "grow field" }, el("label", {}, "API key"), keyInput)),
      el("div", { class: "field" },
        el("label", {}, "Fallback model"),
        fallbackInput,
        el("p", { class: "hint" }, "Used once, per chunk, when the main model returns no text at all (typical of reasoning models that exhaust the token budget).")),
      el("div", { class: "split" },
        el("button", { type: "button", onclick: async () => {
          const body = { base_url: urlInput.value, model: modelInput.value,
                         fallback_model: fallbackInput.value, enabled: enabled.checked };
          if (keyInput.value) body.api_key = keyInput.value;
          await api(`/providers/${p.id}`, { method: "PATCH", body });
          toast("Provider saved", "ok");
          render();
        } }, "Save"),
        el("button", { type: "button", onclick: async () => {
          const r = await api(`/providers/${p.id}/models`).catch((e) => { toast(e.message, "err"); return null; });
          if (r) status.textContent = r.models.slice(0, 12).join(", ");
        } }, "List models"),
        status));
  }

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "card" },
      el("p", { class: "empty-kicker" }, "Instance"),
      el("h1", {}, "Settings"),
      el("p", { class: "muted small hero-note" },
        "Defaults for new transcriptions, the LLM providers the correction agent can use, and where the data lives.")),
    form));
}

/* ============================================================================
   Costi
   ========================================================================== */
async function viewCosts(nav, params, token) {
  const data = await api("/costs");
  const { cells, projects, months, note } = data;
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
      fix: sumFor(list.filter((c) => c.kind === "llm_fix"), "cost_usd"),
      tokens: list.reduce((t, c) => t + (c.tokens || 0), 0),
      jobs: list.reduce((t, c) => t + (c.n_jobs || 0), 0) };
  }

  const monthLabel = (m) => new Date(`${m}-01T00:00:00`).toLocaleDateString([], {
    month: "short", year: "numeric" });

  const month = el("select", { id: "costMonth", "aria-label": "Filter by month" },
    [el("option", { value: "all" }, "All months")].concat(
      months.map((m) => el("option", { value: m }, monthLabel(m)))));
  // Ogni opzione ha un `value` esplicito, compresa "tutti": un `<option>` senza
  // `value` viene identificato dal browser con il suo *testo*, quindi un'etichetta
  // tradotta romperebbe in silenzio il confronto con il filtro.
  const kind = el("select", { id: "costKind", "aria-label": "Filter by kind of work" },
    el("option", { value: "all" }, "Summaries and corrections"),
    el("option", { value: "llm_summary" }, "Summaries only"),
    el("option", { value: "llm_fix" }, "Corrections only"));

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
      stat("Total", fmtUsd(agg.total), "is-accent"),
      stat("Summaries", fmtUsd(agg.summary)),
      stat("Corrections", fmtUsd(agg.fix)),
      stat("Tokens", Math.round(agg.tokens).toLocaleString()),
      stat("Jobs", String(agg.jobs)));
  }

  function renderTable(agg) {
    // Le barre sono relative al corso più caro: dice a colpo d'occhio dove sono
    // finiti i soldi, senza aggiungere una libreria di grafici.
    const peak = Math.max(...agg.byCourse.map((r) => r.total), 0);
    const bar = (v) => el("span", { class: "cost-bar" },
      el("span", { class: "cost-bar-fill", style: peak ? `width:${(v / peak) * 100}%` : "width:0" }));

    const heads = summaryOnly()
      ? [el("th", {}, "Course"), el("th", { class: "right" }, "Cost"),
          el("th", { class: "right" }, "Jobs")]
      : [el("th", {}, "Course"), el("th", { class: "right" }, "Total"),
          el("th", { class: "right" }, "Summaries"), el("th", { class: "right" }, "Corrections"),
          el("th", { class: "right" }, "Jobs")];
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
          el("p", { class: "empty-kicker" }, "Nothing in this filter"),
          el("p", {}, "No summary or correction was run in this month. ",
            "Pick another month, or go back to all months.")));
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
          el("p", { class: "empty-kicker" }, "Ledger"),
          el("h1", {}, "LLM costs"),
          el("p", { class: "muted small hero-note" },
            "Whisper runs on this machine and costs nothing. What has a price is the ",
            "optional work done by a model: summaries and the correction agent. ",
            "Every call is recorded on the job that made it, at the time it was made.")),
        el("div", { class: "actions" },
          el("button", { onclick: () => render() }, "Refresh")))),
    stats,
    el("div", { class: "card" },
      el("div", { class: "split" },
        el("div", { class: "field", style: "min-width:190px;margin:0" },
          el("label", { for: "costMonth" }, "Period"), month),
        el("div", { class: "field", style: "min-width:230px;margin:0" },
          el("label", { for: "costKind" }, "Work"), kind),
        el("span", { class: "spacer" }),
        el("span", { class: "muted small" }, "Totals follow the two menus.")),
      tableHost),
    el("div", { class: "card" },
      el("h2", {}, "How these numbers are made"),
      el("p", { class: "muted prose" },
        note, " A job that died halfway still appears under its course: its tokens ",
        "were spent all the same."),
      el("table", { class: "details-table" }, el("tbody", {},
        el("tr", {}, el("th", {}, "Cache hit (per 1M)"), el("td", {}, `$${data.rates.cache_hit}`)),
        el("tr", {}, el("th", {}, "Cache miss (per 1M)"), el("td", {}, `$${data.rates.cache_miss}`)),
        el("tr", {}, el("th", {}, "Output (per 1M)"), el("td", {}, `$${data.rates.output}`)))),
      el("p", { class: "hint" },
        "Local models cost nothing at the provider, but they still report tokens: ",
        "the numbers are real even when the price is not."))));
}

async function viewAbout(nav, params, token) {
  // Lo stato del server è già esposto da /health: mostrarlo qui evita di
  // mandare l'utente a cercare la versione nei log.
  const health = await api("/health").catch(() => null);
  const hw = health?.hardware || {};
  setDocTitle("About & privacy");

  const facts = [
    ["Version", health?.version || "—"],
    ["Database", "data/trascrivi.db"],
    ["Uploaded copies", "data/sources/ (deleted after a successful job)"],
    ["Access", "127.0.0.1 only — not reachable from the network"],
    ["Compute", hw.cuda_devices ? `CUDA · ${hw.gpu_name || "GPU"}` : "CPU"],
  ];

  if (!isCurrent(token)) return;
  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" },
      el("a", { class: "crumb-back", href: "#/" }, icon("back"), "Library"), " / ",
      el("span", { class: "here" }, "About & privacy")),
    el("div", { class: "card" },
      el("p", { class: "empty-kicker" }, "Trascrivi"),
      el("h1", {}, "What runs, and what leaves this machine"),
      el("p", { class: "muted prose", style: "margin-top:6px" },
        "Trascrivi is a local application. It listens on the loopback address, stores everything under the ",
        "data/ directory next to the code, and sends no telemetry. There is no account and no cloud service.")),
    el("div", { class: "card" },
      el("h2", {}, "Where your data goes"),
      el("table", { class: "details-table" }, el("tbody", {},
        facts.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, String(v))))))),
    el("div", { class: "card" },
      el("h2", {}, "The one exception: the correction agent"),
      el("p", { class: "muted prose" },
        "Transcription itself is offline — Whisper runs on this machine and the audio never leaves it. ",
        "The optional correction agent is different: when you queue it, the transcript text is sent to the ",
        "LLM provider you configured in Settings, over that provider's API, with your own key. ",
        "It is off by default and runs only when you start it. If you would rather keep everything local, ",
        "point a provider at a model running on this machine and disable the others."),
      el("p", { class: "hint" },
        "API keys are encrypted at rest with data/secret.key. If that file is deleted the saved keys become ",
        "unreadable and must be entered again — the app says so on the provider card instead of failing silently.")),
    el("div", { class: "card" },
      el("h2", {}, "Keyboard"),
      el("div", { class: "stack" },
        el("div", { class: "split" }, el("kbd", {}, "/"), el("span", { class: "muted" }, "Focus the search field")),
        el("div", { class: "split" }, el("kbd", {}, "Ctrl"), el("kbd", {}, "S"),
          el("span", { class: "muted" }, "Save the transcript you are editing")),
        el("div", { class: "split" }, el("kbd", {}, "Esc"), el("span", { class: "muted" }, "Close a menu or a dialog")),
        el("div", { class: "split" }, el("kbd", {}, "← →"),
          el("span", { class: "muted" }, "Move between the transcript tabs"))),
      el("p", { class: "hint" },
        "There are no legal pages to read: this app has no users other than the person running it, ",
        "collects nothing, and shares nothing. The licence of the source code governs its use.")),
  ));
}

/* ── Avvio ───────────────────────────────────────────────────────────────── */
async function init() {
  initTheme();

  const health = await api("/health").catch(() => null);
  if (health?.hardware) {
    // I modelli sono definiti dal backend: la UI non li duplica a mano.
    const res = await api("/models");
    MODELS.push(...res.models);
  }

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

  window.addEventListener("hashchange", render);
  await render();
  refreshJobBadge();
  setInterval(refreshJobBadge, 5000);
}

init().catch((err) => {
  console.error(err);
  document.getElementById("view").innerHTML =
    `<div class="card"><h2>Could not start</h2>
     <p class="muted prose">${esc(err.message)}</p>
     <p class="hint">The page loaded but the API did not answer. Check that the server is still running
     in the console window, then reload.</p></div>`;
});
