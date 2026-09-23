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

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

const byId = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== false && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};

/* ── Tema ────────────────────────────────────────────────────────────────── */
function initTheme() {
  const btn = byId("themeToggle");
  const apply = (theme) => {
    document.documentElement.dataset.theme = theme;
    btn.textContent = theme === "dark" ? "☀️" : "🌙";
    localStorage.setItem("trascrivi-theme", theme);
  };
  apply(document.documentElement.dataset.theme || "light");
  btn.addEventListener("click", () => {
    apply(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
}

/* ── Router ──────────────────────────────────────────────────────────────── */
const routes = [
  [/^\/$/, viewDashboard],
  [/^\/p\/(\d+)\/?$/, viewProject],
  [/^\/p\/(\d+)\/new\/?$/, viewNewLecture],
  [/^\/jobs\/?$/, viewJobs],
  [/^\/t\/(\d+)\/?$/, viewTranscript],
  [/^\/settings\/?$/, viewSettings],
];

let currentPoll = null;

function stopPoll() {
  if (currentPoll) { clearInterval(currentPoll); currentPoll = null; }
}

async function render() {
  stopPoll();
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  const nav = byId("view");
  for (const [re, view] of routes) {
    const m = path.match(re);
    if (m) {
      nav.replaceChildren(el("p", { class: "muted" }, "Loading…"));
      try {
        await view(nav, ...m.slice(1).map(Number), params);
      } catch (err) {
        console.error(err);
        nav.replaceChildren(el("div", { class: "card" },
          el("h2", {}, "Something went wrong"),
          el("p", { class: "muted" }, err.message),
          el("button", { onclick: render }, "Retry")));
      }
      return;
    }
  }
  nav.replaceChildren(el("div", { class: "card" }, el("h2", {}, "Not found"),
    el("p", { class: "muted" }, `No route for ${esc(path)}`)));
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

/* ============================================================================
   Dashboard
   ========================================================================== */
async function viewDashboard(nav) {
  const [{ projects }, health] = await Promise.all([api("/projects"), api("/health")]);
  const hw = health.hardware;

  const search = (q) => {
    location.hash = `#/?q=${encodeURIComponent(q)}`;
  };

  const cards = el("div", { class: "grid" });
  projects.forEach((p) => {
    const card = el("div", {
      class: "card link card-accent",
      style: `--color:${p.color || "#4f46e5"}`,
      tabindex: "0",
      role: "link",
      onclick: () => { location.hash = `#/p/${p.id}`; },
      onkeydown: (e) => { if (e.key === "Enter") location.hash = `#/p/${p.id}`; },
    },
      el("h2", {}, p.name),
      p.code ? el("span", { class: "badge" }, p.code) : null,
      p.description ? el("p", { class: "muted small" }, p.description) : null,
      el("div", { class: "split small muted" },
        el("span", {}, `${p.n_transcripts} transcript${p.n_transcripts === 1 ? "" : "s"}`),
        el("span", {}, "·"),
        el("span", {}, `updated ${fmtDate(p.updated_at)}`)),
    );
    cards.append(card);
  });

  const newProject = el("form", { class: "card", onsubmit: submit },
    el("h2", {}, "New course"),
    el("div", { class: "row" },
      el("div", { class: "grow field" }, el("label", { for: "npName" }, "Course name"),
        el("input", { id: "npName", required: "required", placeholder: "Machine Learning" })),
      el("div", { class: "grow field" }, el("label", { for: "npCode" }, "Code (optional)"),
        el("input", { id: "npCode", placeholder: "ML-2025" }))),
    el("div", { class: "field" }, el("label", { for: "npDesc" }, "Description (optional)"),
      el("input", { id: "npDesc", placeholder: "Lectures, labs, exam info…" })),
    el("button", { class: "primary", type: "submit" }, "Create course"));

  async function submit(event) {
    event.preventDefault();
    try {
      const p = await api("/projects", {
        method: "POST",
        body: { name: byId("npName").value, code: byId("npCode").value, description: byId("npDesc").value },
      });
      toast(`Course “${p.name}” created`, "ok");
      location.hash = `#/p/${p.id}`;
    } catch (err) { toast(err.message, "err"); }
  }

  const searchResults = el("div");
  const term = (new URLSearchParams(location.hash.split("?")[1] || "")).get("q");
  if (term) {
    const { results } = await api(`/search?q=${encodeURIComponent(term)}`);
    searchResults.append(el("div", { class: "card" },
      el("h2", {}, `Results for “${term}”`),
      results.length ? el("div", { class: "stack" }, results.map((r) =>
        el("div", {},
          el("a", { href: `#/t/${r.id}?q=${encodeURIComponent(term)}` }, r.title),
          el("span", { class: "badge" }, r.project_name || "—"),
          el("p", { class: "small muted", html: esc(r.snippet).replace(/\[\[/g, "<mark>").replace(/\]\]/g, "</mark>") }),
        ))) : el("p", { class: "muted" }, "Nothing found.")));
  }

  nav.replaceChildren(
    el("div", { class: "stack" },
      el("div", { class: "card" },
        el("div", { class: "split" },
          el("h1", {}, "Library"),
          el("span", { class: "spacer" }),
          el("span", { class: "badge" }, hw.cuda_devices ? `GPU: ${hw.gpu_name || "CUDA"}` : "CPU only"),
          hw.vram_free_gb != null ? el("span", { class: "badge" }, `${hw.vram_free_gb} GB VRAM free`) : null,
          el("span", { class: "badge" }, `${hw.cpu_count} threads`),
          hw.cached_models.length ? el("span", { class: "badge" }, `models in RAM: ${hw.cached_models.length}`) : null),
        el("p", { class: "muted small" },
          "Create a course, drop a lecture recording into it, and follow the transcription in Jobs. ",
          "Audio is transcoded on this machine only.")),
      searchResults,
      el("h2", {}, "Courses"),
      projects.length ? cards : el("div", { class: "empty" },
        el("p", {}, "No courses yet. Create one below — one per university subject works well.")),
      newProject,
    ));
}

/* ============================================================================
   Vista progetto
   ========================================================================== */
async function viewProject(nav, projectId) {
  const [project, { transcripts }] = await Promise.all([
    api(`/projects/${projectId}`), api(`/transcripts?project_id=${projectId}`),
  ]);
  const { terms } = await api(`/terms?project_id=${projectId}`);

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
      el("span", { class: "muted" }, "→"),
      el("span", { class: "mono" }, t.replace || "—"),
      t.auto ? el("span", { class: "badge ok" }, "auto") : null,
      el("span", { class: "spacer" }),
      el("button", { class: "ghost", onclick: async () => { await api(`/terms/${t.id}`, { method: "DELETE" }); render(); } }, "✕"),
    )) : el("p", { class: "muted small" }, "No terms yet. Auto terms are applied silently to every new transcript of this course."));

  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" }, el("a", { href: "#/" }, "Library"), " / ", project.name),
    el("div", { class: "card" },
      el("div", { class: "split" },
        el("h1", {}, project.name),
        project.code ? el("span", { class: "badge" }, project.code) : null,
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "primary", onclick: () => { location.hash = `#/p/${projectId}/new`; } }, "+ New lecture"),
        el("button", { type: "button", onclick: editProject }, "Rename"),
        el("button", { type: "button", class: "danger", onclick: removeProject }, "Delete course")),
      project.description ? el("p", { class: "muted" }, project.description) : null),
    el("div", { class: "card" },
      el("h2", {}, "Transcripts"),
      transcripts.length ? el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Title"), el("th", {}, "Duration"), el("th", {}, "Lang"),
          el("th", {}, "Model"), el("th", {}, "Updated"), el("th", {}, ""))),
        el("tbody", {}, rows)) : el("div", { class: "empty" },
        el("p", {}, "Nothing transcribed yet."),
        el("button", { class: "primary", onclick: () => { location.hash = `#/p/${projectId}/new`; } }, "Add the first lecture"))),
    el("div", { class: "card" },
      el("h2", {}, "Course glossary"),
      el("p", { class: "muted small" }, "Deterministic replacements applied to transcripts of this course. “auto” applies them silently when a transcript is created."),
      termList, el("hr", { style: "border:0;border-top:1px solid var(--border);margin:12px 0" }), termForm),
  ));

  async function editProject() {
    const name = prompt("Course name", project.name);
    if (!name) return;
    await api(`/projects/${projectId}`, { method: "PATCH", body: { name } });
    render();
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
async function viewNewLecture(nav, projectId) {
  const [project, health, { terms }] = await Promise.all([
    api(`/projects/${projectId}`), api("/health"), api(`/terms?project_id=${projectId}`),
  ]);
  const settings = (await api("/settings")).settings;
  const providers = health.providers;

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

  const fileStatus = el("p", { class: "small muted" }, "No file selected.");
  const nameInput = el("input", { id: "srcTitle", placeholder: "Lecture title" });
  const descInput = el("input", { id: "srcDesc", placeholder: "Optional description" });
  const pathInput = el("input", { id: "srcPath", placeholder: "C:\\Users\\you\\lectures\\lesson.mp4" });
  const pathStatus = el("p", { class: "small muted" }, "The file is read where it is: no copy, nothing moved or deleted.");
  const progressFill = el("span");
  const progressBox = el("div", { class: "progress hidden" }, progressFill);

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
    pathStatus.textContent = "The file is read where it is: no copy, nothing moved or deleted.";
    fileStatus.textContent = `${file.name} - ${fmtBytes(file.size)} - ready to upload (it will be copied into data/sources and removed after the job).`;
    if (!nameInput.value) nameInput.value = file.name.replace(/\.[^.]+$/, "");
  }

  async function probePath() {
    const value = pathInput.value.trim();
    if (!value) return;
    pathStatus.textContent = "Checking...";
    try {
      const info = await api(`/media/probe?path=${encodeURIComponent(value)}`);
      if (!info.has_audio) {
        pathStatus.textContent = `No audio track in ${info.name}`;
        source = null;
        return;
      }
      const created = await api(`/projects/${projectId}/sources`, { method: "POST", body: { path: value } });
      source = created.source;
      pickedFile = null;
      fileStatus.textContent = "No file selected.";
      pathStatus.textContent = `${info.name} - ${fmtBytes(info.size)} - ${fmtTime(info.duration)} - will be read in place`;
      if (!nameInput.value) nameInput.value = info.name.replace(/\.[^.]+$/, "");
      if (created.duplicate_of) pathStatus.textContent += ` (duplicate of source #${created.duplicate_of})`;
    } catch (err) {
      source = null;
      pathStatus.textContent = `Rejected: ${err.message}`;
    }
  }
  const fmtTime = (s) => (s ? fmtTs(s) : "unknown length");

  async function uploadPicked() {
    if (!pickedFile) return null;
    const form = new FormData();
    form.append("file", pickedFile, pickedFile.name);
    const res = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/projects/${projectId}/upload`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) progressFill.style.width = `${(e.loaded / e.total) * 100}%`;
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
        el("div", { class: "grow field" }, el("label", { for: "optBeam" }, "Beam size"), opts.beam)),
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
    el("div", { class: "split" },
      el("button", { class: "primary", type: "submit" }, "Start transcription"),
      el("span", { class: "muted small" }, terms.filter((t) => t.auto).length ? `${terms.filter((t) => t.auto).length} auto glossary term(s) will be applied` : ""),
    ));

  // Stato iniziale: una sola sorgente visibile (file), l'altra raggiungibile
  // dallo switch in cima alla card.
  updateMode("file");

  async function submit(event) {
    event.preventDefault();
    const submitBtn = $("button[type=submit]", form);
    submitBtn.disabled = true;
    submitBtn.textContent = "Working...";
    try {
      if (!source && pickedFile) {
        progressBox.classList.remove("hidden");
        fileStatus.textContent = `Uploading ${pickedFile.name}...`;
        await uploadPicked();
        fileStatus.textContent = `${pickedFile.name} uploaded.`;
      }
      if (!source) throw new Error("Choose a file or a path first.");
      const job = await api("/jobs", {
        method: "POST",
        body: {
          project_id: projectId,
          source_id: source.id,
          title: nameInput.value || source.name,
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

  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" }, el("a", { href: "#/" }, "Library"), " / ",
      el("a", { href: `#/p/${projectId}` }, project.name), " / New lecture"),
    el("h1", {}, "New lecture"), form));
}

/* ============================================================================
   Coda dei job
   ========================================================================== */
async function viewJobs(nav, params) {
  const focus = Number(params.get("focus") || 0);
  const container = el("div", { class: "stack" });

  const renderJobs = async () => {
    const { jobs, queue } = await api("/jobs?limit=60");
    const cards = jobs.map((j) => {
      const running = j.status === "running";
      const payload = j.payload || {};
      const pct = Math.round((j.progress || 0) * 100);
      return el("div", { class: "card", id: `job-${j.id}` },
        el("div", { class: "split" },
          el("h3", {}, `#${j.id} · ${j.kind === "transcribe" ? (payload.title || "transcription") : "correction agent"}`),
          el("span", { class: `badge ${j.status === "done" ? "ok" : j.status === "failed" ? "danger" : j.status === "running" ? "accent" : ""}` }, j.status),
          el("span", { class: "spacer" }),
          running ? el("button", { class: "danger", onclick: () => cancel(j.id) }, "Cancel") : null,
          ["failed", "interrupted", "cancelled"].includes(j.status) ? el("button", { onclick: () => retry(j.id) }, "Retry") : null,
          j.transcript_id ? el("a", { class: "btn", href: `#/t/${j.transcript_id}` }, "Open") : null),
        el("div", { class: "progress", style: "margin:10px 0 6px" }, el("span", { style: `width:${pct}%` })),
        el("div", { class: "split small muted" },
          el("span", {}, `${pct}%`),
          j.message ? el("span", {}, j.message) : null,
          j.speed ? el("span", {}, `· ${j.speed}x realtime`) : null,
          j.eta_seconds ? el("span", {}, `· ETA ${fmtTs(j.eta_seconds)}`) : null,
          el("span", { class: "spacer" }),
          el("span", {}, `created ${fmtDate(j.created_at)}`)),
        j.error ? el("p", { class: "small", style: "color:var(--danger)" }, j.error) : null);
    });

    container.replaceChildren(
      el("div", { class: "split" },
        el("h1", {}, "Jobs"),
        el("span", { class: "badge" }, `${queue} queued`),
        el("span", { class: "spacer" }),
        el("button", { onclick: renderJobs }, "Refresh")),
      el("p", { class: "muted small" }, "One transcription at a time: the GPU is the bottleneck, so parallel jobs would only slow each other down."),
      jobs.length ? el("div", { class: "stack" }, cards) : el("div", { class: "empty" }, "No jobs yet."));
  };

  async function cancel(id) {
    try { await api(`/jobs/${id}/cancel`, { method: "POST" }); toast("Cancelling…"); renderJobs(); }
    catch (err) { toast(err.message, "err"); }
  }
  async function retry(id) {
    try { await api(`/jobs/${id}/retry`, { method: "POST" }); toast("Requeued", "ok"); renderJobs(); }
    catch (err) { toast(err.message, "err"); }
  }

  nav.replaceChildren(container);
  await renderJobs();
  if (focus) byId(`job-${focus}`)?.scrollIntoView({ block: "center" });
  currentPoll = setInterval(() => { renderJobs().catch(() => {}); refreshJobBadge(); }, 1500);
}

/* ============================================================================
   Trascrizione
   ========================================================================== */
async function viewTranscript(nav, transcriptId, params) {
  const query = params.get("q") || "";
  const [t, { jobs }] = await Promise.all([
    api(`/transcripts/${transcriptId}`), api(`/jobs?limit=20`),
  ]);
  const providers = (await api("/providers")).providers;

  const textArea = el("textarea", { class: "editor", spellcheck: "false" });
  textArea.value = t.text;
  const saveState = el("span", { class: "small muted" }, "saved");
  let dirty = false;
  let timer = null;

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
    clearTimeout(timer);
    timer = setTimeout(save, 1200);
  });
  textArea.addEventListener("blur", save);
  document.addEventListener("keydown", function onKey(e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
  }, { once: false });

  const tabContent = el("div", { class: "stack" },
    el("div", { class: "split" }, textArea ? el("span", { class: "muted small" }, "Editing the flat text. One line = one segment; timestamps are re-attached automatically on save.") : null,
      el("span", { class: "spacer" }), saveState),
    textArea,
    el("div", { class: "split" },
      el("button", { class: "primary", onclick: save }, "Save now"),
      el("button", { onclick: copyAll }, "Copy all"),
      el("span", { class: "muted small" }, `${t.text.split(/\s+/).filter(Boolean).length} words`)));

  async function copyAll() {
    try { await navigator.clipboard.writeText(textArea.value); toast("Copied", "ok"); }
    catch { toast("Clipboard unavailable", "err"); }
  }

  const segRows = (t.segments || []).map((s, i) => {
    const norm = (s.text || "").toLowerCase();
    const hit = query && norm.includes(query.toLowerCase());
    return el("div", { class: `seg ${hit ? "hit" : ""}`, "data-i": String(i) },
      el("time", {}, fmtTs(s.start)),
      el("p", { html: query ? highlight(s.text, query) : esc(s.text) }));
  });
  const tabSegments = el("div", {},
    el("div", { class: "split", style: "margin-bottom:10px" },
      el("span", { class: "muted small" }, `${(t.segments || []).length} segments`),
      el("span", { class: "spacer" }),
      el("a", { class: "btn", href: `/api/transcripts/${transcriptId}/export?format=segments` }, "Download with timestamps")),
    el("div", { class: "segments" }, segRows.length ? segRows : el("p", { class: "muted" }, "No segments stored.")));

  const proposalBox = el("div", { class: "stack" });
  const tabProposals = el("div", { class: "stack" },
    el("div", { class: "split" },
      el("span", { class: "muted small" }, "The agent returns a list of edits: you decide which ones to apply."),
      el("span", { class: "spacer" }),
      el("select", { id: "fixProvider" }, providers.filter((p) => p.enabled).map((p) => el("option", { value: p.id }, `${p.name} · ${p.model || "?"}`))),
      el("button", { class: "primary", onclick: runFix }, "Run agent"),
      el("button", { onclick: loadProposals }, "Refresh")),
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

  const tabInfo = el("div", { class: "card" },
    el("h3", {}, "Details"),
    el("table", {}, el("tbody", {},
      row("Course", t.project_name || "—"),
      row("Language", (t.language || "—").toUpperCase()),
      row("Model", t.model || "—"),
      row("Device", t.device || "—"),
      row("Duration", fmtTs(t.duration)),
      row("Created", fmtDate(t.created_at)),
      row("Updated", fmtDate(t.updated_at)),
      row("Source", t.source_ref || (t.source_id ? `source #${t.source_id}` : "—"))),
    ),
    el("h3", { style: "margin-top:16px" }, "History"),
    el("div", { class: "stack small" }, (t.revisions || []).length
      ? t.revisions.map((r) => el("div", { class: "split" },
        el("span", { class: "badge" }, r.kind),
        el("span", {}, `${r.n_changes} change(s)`),
        el("span", { class: "muted" }, r.summary || ""),
        el("span", { class: "spacer" }),
        el("span", { class: "muted" }, fmtDate(r.created_at))))
      : el("p", { class: "muted" }, "No revisions yet.")),
  );

  function row(k, v) { return el("tr", {}, el("th", {}, k), el("td", { class: "mono" }, String(v))); }

  const tabs = {
    content: tabContent,
    segments: tabSegments,
    proposals: tabProposals,
    info: tabInfo,
  };
  const tabBar = el("div", { class: "toolbar" },
    Object.keys(tabs).map((key) => el("button", {
      class: "tab", role: "tab", "aria-selected": key === "content" ? "true" : "false",
      onclick: (e) => {
        $$(".tab", tabBar).forEach((b) => b.setAttribute("aria-selected", "false"));
        e.currentTarget.setAttribute("aria-selected", "true");
        panel.replaceChildren(tabs[key]);
      },
    }, key[0].toUpperCase() + key.slice(1))));
  const panel = el("div", {}, tabs.content);

  const runningFix = jobs.find((j) => j.kind === "llm_fix" && j.transcript_id === t.id && ["queued", "running"].includes(j.status));

  nav.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "breadcrumb" },
      el("a", { href: "#/" }, "Library"), " / ",
      el("a", { href: `#/p/${t.project_id}` }, t.project_name || "Course"), " / ", t.title),
    el("div", { class: "split" },
      el("h1", {}, t.title),
      el("span", { class: "spacer" }),
      el("a", { class: "btn", href: `/api/transcripts/${t.id}/export?format=txt` }, "Download .txt"),
      el("a", { class: "btn", href: `/api/transcripts/${t.id}/export?format=md` }, "Download .md"),
      el("a", { class: "btn", href: `/api/transcripts/${t.id}/export?format=json` }, "Download .json"),
      el("button", { onclick: rename }, "Rename"),
      el("button", { class: "danger", onclick: remove }, "Delete")),
    t.description ? el("p", { class: "muted" }, t.description) : null,
    runningFix ? el("div", { class: "card" }, el("div", { class: "split" },
      el("span", { class: "spin" }),
      el("span", {}, `Correction agent running (job #${runningFix.id})`),
      el("span", { class: "spacer" }),
      el("a", { href: `#/jobs?focus=${runningFix.id}` }, "Follow in Jobs"))) : null,
    tabBar, panel));

  await loadProposals();

  async function rename() {
    const title = prompt("Title", t.title);
    if (!title) return;
    await api(`/transcripts/${transcriptId}`, { method: "PATCH", body: { title } });
    render();
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
async function viewSettings(nav) {
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

    el("div", { class: "card" }, el("h2", {}, "LLM providers"),
      el("p", { class: "muted small" }, "Keys are encrypted at rest with a key stored in data/secret.key and are never returned by the API."),
      el("div", { class: "stack" }, providers.map((p) => providerRow(p)))),

    el("div", { class: "card" }, el("h2", {}, "Storage"),
      el("div", { class: "split small" },
        el("span", { class: "badge" }, `audio copies: ${fmtBytes(disk.sources_bytes)} (${disk.sources_files} files)`),
        el("span", { class: "badge" }, `data dir: ${fmtBytes(disk.data_bytes)}`),
        el("span", { class: "badge" }, `free: ${fmtBytes(disk.free_bytes)}`)),
      el("p", { class: "hint" }, "Files used by path are never copied and never deleted by the app. Copies uploaded from the browser are removed after a successful transcription."),
      el("div", { class: "split", style: "margin-top:10px" },
        el("button", { onclick: async () => { toast(JSON.stringify(await api("/maintenance/backup"))); } }, "Back up database"),
        el("button", { onclick: async () => { const r = await api("/maintenance/purge-sources", { method: "POST" }); toast(`Removed ${r.purged} audio copy(ies)`, "ok"); render(); } }, "Delete all audio copies"),
        el("button", { onclick: async () => { const r = await api("/models/unload", { method: "POST" }); toast(`Unloaded ${r.unloaded} model(s)`); } }, "Unload models from RAM"))),

    el("div", { class: "card" }, el("h2", {}, "Runtime"),
      el("div", { class: "split small" },
        cached.length ? cached.map((m) => el("span", { class: "badge ok" }, `${m.model} · ${m.device}`)) : el("span", { class: "badge" }, "no model loaded")),
      el("p", { class: "hint" }, "Models stay in RAM between jobs to skip the load time. Keep a maximum of two: the VRAM is shared.")),
  );

  function providerRow(p) {
    const keyInput = el("input", { type: "password", placeholder: p.has_key ? "•••••• configured" : "API key", autocomplete: "off" });
    const modelInput = el("input", { value: p.model || "", placeholder: "model id" });
    const urlInput = el("input", { value: p.base_url, placeholder: "base url" });
    const enabled = el("input", { type: "checkbox" });
    enabled.checked = !!p.enabled;
    const status = el("span", { class: "small muted" }, "");
    return el("div", { class: "card" },
      el("div", { class: "split" },
        el("h3", { style: "margin:0" }, p.name),
        p.has_key ? el("span", { class: "badge ok" }, "key stored") : el("span", { class: "badge" }, "no key"),
        el("label", { class: "check", style: "margin:0" }, enabled, el("span", {}, "enabled")),
        el("span", { class: "spacer" }),
        el("button", { type: "button", onclick: async () => { status.textContent = "testing…"; status.textContent = JSON.stringify(await api(`/providers/${p.id}/test`, { method: "POST", body: { model: modelInput.value } })); } }, "Test")),
      el("div", { class: "row", style: "margin-top:8px" },
        el("div", { class: "grow field" }, el("label", {}, "Base URL"), urlInput),
        el("div", { class: "grow field" }, el("label", {}, "Model"), modelInput),
        el("div", { class: "grow field" }, el("label", {}, "API key"), keyInput)),
      el("div", { class: "split" },
        el("button", { type: "button", onclick: async () => {
          const body = { base_url: urlInput.value, model: modelInput.value, enabled: enabled.checked };
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

  nav.replaceChildren(el("div", { class: "stack" }, el("h1", {}, "Settings"), form));
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

  $("#globalSearch").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.value.trim()) {
      location.hash = `#/?q=${encodeURIComponent(e.target.value.trim())}`;
    }
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
    `<div class="card"><h2>Startup failed</h2><p class="muted">${esc(err.message)}</p></div>`;
});
