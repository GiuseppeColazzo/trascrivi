/* Smoke test: boot app.js against a stub API and render every route.
   Run: node .smoke/smoke.mjs   (from the project root) */

import { makeDom, installGlobals } from "./dom.mjs";
import { readFileSync, writeFileSync, unlinkSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dom = makeDom();
const { doc, timers } = installGlobals(dom);

/* --- Build the shell that index.html provides ---------------------------- */
const IDS = [
  "navJobs", "globalSearch", "themeMenu", "themeToggle", "themeList", "themeIcon",
  "themeName", "metaThemeColor", "view", "toasts", "confirmDialog", "confirmTitle",
  "confirmText", "confirmOk", "promptDialog", "promptTitle", "promptInput", "promptError",
  "promptLabel", "promptOk",
];
for (const id of IDS) {
  const tag = id === "view" || id === "toasts" ? "main" : id.includes("Dialog") ? "dialog" : "div";
  const el = doc.createElement(tag);
  el.id = id;
  if (id.endsWith("List")) el.hidden = true;
  doc.body.append(el);
}
// The theme menu needs its three option buttons.
const themeMenuEl = doc.getElementById("themeMenu");
const themeListEl = doc.getElementById("themeList");
for (const choice of ["auto", "light", "dark"]) {
  const b = doc.createElement("button");
  b.className = "theme-option";
  b.setAttribute("data-theme-choice", choice);
  const ico = doc.createElement("span");
  ico.className = "theme-ico";
  const hint = doc.createElement("span");
  hint.className = "theme-hint";
  b.append(ico, hint);
  themeListEl.append(b);
}
// La lista vive dentro il menu, come nell'HTML vero.
themeMenuEl.append(themeListEl);
// L'header è il contenitore dei nav-item.
const headerEl = doc.createElement("header");
headerEl.className = "app";
headerEl.append(themeMenuEl);
doc.body.append(headerEl);
// Tabs and footer nav that the shell script touches.
for (const nav of ["#/", "#/jobs", "#/settings"]) {
  const b = doc.createElement("button");
  b.setAttribute("data-nav", nav);
  doc.body.append(b);
}

/* --- Stub the API -------------------------------------------------------- */
const jobs = [
  { id: 1, kind: "transcribe", status: "running", progress: 0.42, speed: 3.1, eta_seconds: 210,
    message: "transcribing", payload: { title: "Week 3 — Kalman filters" }, created_at: Date.now() / 1000,
    transcript_id: 7, error: null },
  { id: 2, kind: "transcribe", status: "failed", progress: 0.1, payload: { title: "Week 4" },
    created_at: Date.now() / 1000 - 900, error: "ffmpeg exited with code 1", transcript_id: null },
  { id: 3, kind: "llm_fix", status: "queued", progress: 0, payload: {}, created_at: Date.now() / 1000 - 30,
    transcript_id: 8, error: null },
];

const segments = [
  { start: 0, text: "Good morning everyone, today we look at the Kalman filter." },
  { start: 14.5, text: "The predictor step uses the state transition matrix F." },
  { start: 61.2, text: "Notice the covariance grows between measurements." },
];

const ROUTES = {
  "/health": { version: "0.9.3", hardware: { cuda_devices: 1, gpu_name: "RTX 4070", vram_free_gb: 9.4, cpu_count: 16, cached_models: [{ model: "distil-large-v3", device: "cuda" }] }, providers: [{ id: 1, name: "OpenRouter", enabled: true, model: "qwen2.5-72b" }] },
  "/models": { models: ["distil-large-v3", "large-v3"], cached: [{ model: "distil-large-v3", device: "cuda" }] },
  "/projects": { projects: [
    { id: 1, name: "Distributed systems", code: "DS-2025", color: "#ff4a17", description: "Lectures and labs, second semester.", n_transcripts: 3, updated_at: Date.now() / 1000 },
    { id: 2, name: "Signal processing", code: "", color: "javascript:alert(1)", description: "", n_transcripts: 0, updated_at: Date.now() / 1000 - 86400 },
    { id: 3, name: "Numerical analysis", code: "NA", color: "#0f766e", description: "Floating point, conditioning, iterative solvers.", n_transcripts: 11, updated_at: Date.now() / 1000 - 200000 },
  ] },
  "/projects/1": { id: 1, name: "Distributed systems", code: "DS-2025", description: "Lectures and labs.", color: "#ff4a17" },
  "/transcripts": { transcripts: [
    { id: 7, title: "Week 3 — Kalman filters", description: "From the whiteboard session", duration: 4520, language: "en", model: "distil-large-v3", updated_at: Date.now() / 1000 },
    { id: 8, title: "Week 4 — Consensus", duration: 3710, language: "it", model: "large-v3", updated_at: Date.now() / 1000 - 4000 },
  ] },
  "/terms": { terms: [{ id: 1, find: "kalmann", replace: "Kalman", auto: true }, { id: 2, find: "raft", replace: "Raft", auto: false }] },
  "/transcripts/7": { id: 7, title: "Week 3 — Kalman filters", project_id: 1, project_name: "Distributed systems",
    description: "Whiteboard session on recursive estimation.", duration: 4520, language: "en", model: "distil-large-v3",
    device: "cuda", source_ref: "lectures/week3.mp4", created_at: Date.now() / 1000 - 90000, updated_at: Date.now() / 1000,
    text: segments.map((s) => s.text).join("\n"), segments,
    n_summaries: 1, latest_summary_id: 1,
    revisions: [{ kind: "agent", n_changes: 4, summary: "acronym fixes", created_at: Date.now() / 1000 - 100 }] },
  "/transcripts/7/summaries": { summaries: [
    { id: 1, transcript_id: 7, style: "study", title: "The Kalman filter", overview: "Recursive estimation.",
      n_words: 812, target_words: 900, source_words: 6100, cost_usd: 0.0041, elapsed_s: 31,
      edited: 0, created_at: Date.now() / 1000 - 60 },
  ] },
  "/summaries/1": { id: 1, transcript_id: 7, style: "study", title: "The Kalman filter",
    overview: "Recursive estimation.", model: "deepseek-flash", provider: "deepseek",
    n_words: 812, target_words: 900, source_words: 6100, cost_usd: 0.0041, elapsed_s: 31, edited: 0,
    created_at: Date.now() / 1000 - 60,
    // Il markdown copre tutti i costrutti che il renderer deve saper fare:
    // titoli, elenco, tabella, blocco di codice per uno schema, grassetto.
    markdown: [
      "# The Kalman filter",
      "",
      "Recursive estimation of a hidden state from noisy measurements.",
      "",
      "## The two steps",
      "",
      "- **Predict**: propagate the state with the transition matrix F",
      "- **Update**: correct it with the measurement",
      "  - the gain decides how much to trust the measurement",
      "",
      "| Step | Uses |",
      "|---|---|",
      "| Predict | F, Q |",
      "| Update | H, R |",
      "",
      "```",
      "predict --> update --> predict",
      "```",
      "",
      "## Left open",
      "",
      "The extended filter was promised for next week.",
    ].join("\n") },
  "/transcripts/7/proposals": { proposals: [
    { id: 1, kind: "acronym", find: "kalmann", replace: "Kalman", reason: "course vocabulary", flag: "ok", status: "pending", confidence: 0.94, segment_index: 0 },
    { id: 2, kind: "term", find: "F", replace: "F matrix", reason: "ambiguous single letter", flag: "ambiguous", status: "pending", confidence: 0.5, segment_index: 1 },
  ] },
  "/providers": { providers: [
    { id: 1, name: "OpenRouter", enabled: true, model: "qwen2.5-72b", fallback_model: "", base_url: "https://openrouter.ai/api/v1", has_key: true, key_state: "ok" },
    { id: 2, name: "Local llama.cpp", enabled: false, model: "qwen2.5-14b", base_url: "http://127.0.0.1:8080/v1", has_key: false, key_state: "ok" },
  ] },
  "/settings": { settings: { default_model: "distil-large-v3", default_language: "en", default_device: "auto",
    default_compute_type: "auto", default_provider_id: 1, flush_every: 10, llm_chunk_chars: 8000, backup_keep: 5 },
    disk: { sources_bytes: 4.2e9, sources_files: 6, data_bytes: 4.6e9, free_bytes: 180e9 } },
  "/jobs": { jobs, queue: 1 },
  "/search": { results: [{ id: 7, title: "Week 3 — Kalman filters", project_name: "Distributed systems", snippet: "the [[Kalman]] filter" }] },
  "/agent/prompt": { system_prompt: "You correct transcription errors.", max_output_tokens: 2000, temperature: 0.1, chunk_chars: 8000, timeout_seconds: 120 },
  "/media/probe": { name: "week5.mp4", size: 1.2e9, duration: 3600, has_audio: true },
};




globalThis.fetch = async (url, opts = {}) => {
  const p = url.replace(/^\/api/, "").split("?")[0];
  const body = ROUTES[p];
  if (body === undefined) return { ok: false, status: 404, text: async () => JSON.stringify({ detail: `no stub for ${p}` }) };
  return { ok: true, status: 200, text: async () => JSON.stringify(body) };
};

/* --- Load the app -------------------------------------------------------- */
let failures = [];
process.on("unhandledRejection", (e) => { failures.push(`unhandledRejection: ${e && e.message}`); console.log("UNHANDLED:", e); });
const realError = console.error;
console.error = (...args) => { failures.push(`console.error: ${args.map(String).join(" ")}`); realError("[caught]", ...args); };

/* `markdown.js` è uno script classico che espone `renderMarkdown` su globalThis.
   Va caricato prima di app.js, come fa index.html: senza, la tab del riassunto
   va in ReferenceError al render e la pagina resta vuota. */
await import(pathToFileURL(path.join(root, "web", "markdown.js")).href);

const src = readFileSync(path.join(root, "web", "app.js"), "utf8");
const tmp = path.join(root, ".smoke", "__app_under_test.mjs");
writeFileSync(tmp, src, "utf8");
const mod = await import(pathToFileURL(tmp).href);
void mod;
unlinkSync(tmp);

await new Promise((r) => setTimeout(r, 30));

function viewText() {
  return doc.getElementById("view").textContent.replace(/\s+/g, " ").trim();
}

function assert(label, cond, extra = "") {
  if (cond) { console.log(`  ok   ${label}`); }
  else { console.log(`  FAIL ${label} ${extra}`); failures.push(label); }
}

/* Attende che la vista abbia finito: render() toglie aria-busy quando la vista
   ha committato il DOM. Un timeout fisso correva contro le fetch. */
async function settle() {
  const view = doc.getElementById("view");
  for (let i = 0; i < 200; i++) {
    await new Promise((r) => setTimeout(r, 5));
    if (view.getAttribute("aria-busy") !== "true" && view.children.length > 0) {
      const n = view.children[0].getAttribute?.("class") || "";
      if (!String(n).startsWith("stack") || viewText().length > 0) {
        // un giro ancora per lasciar finire le await interne
        await new Promise((r) => setTimeout(r, 10));
        return;
      }
    }
  }
  throw new Error("view never settled");
}

const routes = [
  ["#/", ["Library", "Distributed systems", "3 courses"]],
  ["#/?q=kalman", ["Search", "Week 3"]],
  ["#/p/1", ["Distributed systems", "Course glossary", "Transcripts"]],
  ["#/p/1/new", ["New lecture", "Lecture file", "Start transcription"]],
  ["#/jobs", ["Jobs", "Running", "ffmpeg exited with code 1", "All jobs"]],
  ["#/t/7", ["Week 3 — Kalman filters", "Kalman", "Segments"]],
  ["#/settings", ["Settings", "Storage", "Correction agent"]],
  ["#/about", ["About & privacy", "correction agent", "Keyboard"]],
  ["#/nope/does-not-exist", ["404", "not in the registry"]],
];

const only = process.env.ONLY;
for (const [hash, needles] of routes) {
  location.hash = hash;
  await doc.dispatch("hashchange");
  await settle();
  if (only && hash !== only) continue;
  const text = viewText();
  console.log(`\n${hash}  ->  ${text.slice(0, 96)}…`);
  if (only && needles.some((n) => !text.includes(n))) {
    const view = doc.getElementById("view");
    const dump = (n, d = 0) => {
      const pad = "  ".repeat(d);
      console.log(`${pad}<${n.tagName}> text="${(n._text || "").slice(0, 40)}" children=${n.children.length}`);
      if (d < 4) n.children.slice(0, 5).forEach((c) => dump(c, d + 1));
    };
    view.children.slice(0, 2).forEach((c) => dump(c));
  }
  for (const n of needles) assert(`contains "${n}"`, text.includes(n));
  assert("no error panel", !text.includes("did not go through"), text.slice(0, 200));
  assert("no startup failure", !text.includes("Could not start"), text.slice(0, 200));
}

/* Color sanitisation: a project with a junk colour must not leak into a style. */
location.hash = "#/";
await doc.dispatch("hashchange");
await settle();
const cards = doc.getElementById("view").querySelectorAll(".card.link");
const styles = cards.map((c) => c.attrs.style || "");
assert("project cards rendered", cards.length === 3, String(cards.length));
assert("junk colour replaced", styles.some((s) => s.includes("#ff4a17")) && !styles.some((s) => s.includes("javascript")), JSON.stringify(styles));

/* Theme menu writes a valid choice and does not throw. */
const trigger = doc.getElementById("themeToggle");
trigger.dispatch("click");
const list = doc.getElementById("themeList");
assert("theme menu opens", list.hidden === false);
const dark = list.querySelector('[data-theme-choice="dark"]');
assert("dark option found", !!dark);
dark.dispatch("click");
assert("theme stored", localStorage.getItem("trascrivi-theme") === "dark", String(localStorage.getItem("trascrivi-theme")));
assert("theme applied", doc.documentElement.dataset.theme === "dark", String(doc.documentElement.dataset.theme));
assert("theme menu closes", list.hidden === true);

/* La tab del riassunto si disegna al click, non al render della vista: qui si
   apre davvero e si controlla che il markdown sia stato reso in HTML. È l'unico
   punto in cui `renderMarkdown` viene esercitato end-to-end. */
location.hash = "#/t/7";
await doc.dispatch("hashchange");
await settle();
const summaryTab = doc.getElementById("tab-summary");
assert("summary tab exists", !!summaryTab);
summaryTab.dispatch("click");
await new Promise((r) => setTimeout(r, 20));
const doc0 = viewText();
const md = doc.getElementById("view").querySelectorAll(".md");
const html = md[0] ? md[0].innerHTML : "";
assert("summary heading rendered", doc0.includes("The Kalman filter"));
assert("summary list rendered", doc0.includes("Predict"));
assert("summary table rendered", doc0.includes("Update"));
assert("summary length reported", doc0.includes("812 words of 900"), doc0.slice(0, 200));
assert("summary compression reported", doc0.includes("1:7.5"), doc0.slice(0, 200));
assert("markdown container rendered", md.length === 1, String(md.length));
assert("markdown became real elements", html.includes("<h2>") && html.includes("<table>")
  && html.includes("<ul class=\"md-list\">") && html.includes("<pre><code>"), html.slice(0, 160));
assert("code block content preserved", /<pre><code>[\s\S]*predict[\s\S]*update[\s\S]*<\/code><\/pre>/.test(html),
  html.slice(0, 200));
// Lo `>` dentro il blocco di codice deve restare testo: se uscisse grezzo
// chiuderebbe il tag e il resto del documento finirebbe fuori dal <pre>.
assert("code block content is escaped", html.includes("--&gt;"), html.slice(0, 200));
assert("no raw markdown left", !html.includes("## ") && !html.includes("|---"), html.slice(0, 160));
assert("no null text node", !doc0.includes("nullnull") && !html.includes(">null<"), html.slice(0, 160));

/* Teardown: navigating away from the transcript must drop its Ctrl+S listener. */
location.hash = "#/t/7";
await doc.dispatch("hashchange");
await settle();
const during = dom.listeners.filter((l) => l.type === "keydown" && l.target === doc).length;
for (let i = 0; i < 5; i++) {
  location.hash = "#/";
  await doc.dispatch("hashchange");
  await settle();
  location.hash = "#/t/7";
  await doc.dispatch("hashchange");
  await settle();
}
const after = dom.listeners.filter((l) => l.type === "keydown" && l.target === doc).length;
console.log(`\ndocument keydown listeners: with transcript view=${during}, after 5 round-trips=${after}`);
assert("no keydown listener leak", after <= during, `${during} -> ${after}`);

console.log(`\n${failures.length ? `${failures.length} FAILURE(S)` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
