/* Riproduce il boot REALE di app.js contro il server VERO:
   serve a capire se #optModel resta vuoto (bug "Unsupported model") o no.
   Uso: node .smoke/check_live_new_lecture.mjs [projectId]   (server su :8011) */

import { makeDom, installGlobals } from "./dom.mjs";
import { readFileSync, writeFileSync, unlinkSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const ORIGIN = process.env.ORIGIN || "http://127.0.0.1:8011";
const PROJECT = process.argv[2] || "5";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dom = makeDom();
installGlobals(dom);
const { doc } = dom;

/* --- Shell di index.html --- */
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
const themeMenuEl = doc.getElementById("themeMenu");
const themeListEl = doc.getElementById("themeList");
for (const choice of ["auto", "light", "dark"]) {
  const b = doc.createElement("button");
  b.className = "theme-option";
  b.setAttribute("data-theme-choice", choice);
  const ico = doc.createElement("span"); ico.className = "theme-ico";
  const hint = doc.createElement("span"); hint.className = "theme-hint";
  b.append(ico, hint);
  themeListEl.append(b);
}
themeMenuEl.append(themeListEl);
const headerEl = doc.createElement("header");
headerEl.className = "app";
headerEl.append(themeMenuEl);
doc.body.append(headerEl);
for (const nav of ["#/", "#/jobs", "#/settings"]) {
  const b = doc.createElement("button");
  b.setAttribute("data-nav", nav);
  doc.body.append(b);
}

/* --- fetch vero, con l'origine assoluta che Node pretende --- */
const log = [];
const realFetch = globalThis.fetch;
/* FAIL_FIRST_HEALTH=1 riproduce l'avvio in cui la primissima /api/health non
   risponde (server ancora in fase di import, pagina servita dalla cache del
   browser): è lo stato in cui l'utente ha visto il menu dei modelli vuoto. */
let healthCalls = 0;
const failFirstHealth = process.env.FAIL_FIRST_HEALTH === "1";
globalThis.fetch = async (url, opts = {}) => {
  const abs = String(url).startsWith("http") ? String(url) : ORIGIN + url;
  if (failFirstHealth && abs.includes("/api/health") && ++healthCalls === 1) {
    log.push(`ERR ${abs.replace(ORIGIN, "")} -> simulated network failure`);
    throw new TypeError("Failed to fetch");
  }
  try {
    const res = await realFetch(abs, opts);
    const text = await res.text();
    log.push(`${res.status} ${abs.replace(ORIGIN, "")} -> ${text.slice(0, 120)}`);
    return { ok: res.ok, status: res.status, text: async () => text };
  } catch (err) {
    log.push(`ERR ${abs.replace(ORIGIN, "")} -> ${err.message}`);
    throw err;
  }
};

await import(pathToFileURL(path.join(root, "web", "markdown.js")).href);
const src = readFileSync(path.join(root, "web", "app.js"), "utf8");
const tmp = path.join(root, ".smoke", "__app_live_under_test.mjs");
writeFileSync(tmp, src, "utf8");
const mod = await import(pathToFileURL(tmp).href);
void mod;
unlinkSync(tmp);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(60);

globalThis.location.hash = `#/p/${PROJECT}/new`;
await doc.dispatch("hashchange");
for (let i = 0; i < 200; i++) {
  await sleep(20);
  const sel = doc.getElementById("optModel");
  if (sel && sel.children.length) break;
}
await sleep(200);

console.log("--- chiamate API viste dal frontend ---");
for (const line of log) console.log("   ", line);

const view = doc.getElementById("view");
console.log("\n--- menu dei modelli (bandiere comprese) ---");
const model = doc.getElementById("optModel");
if (!model) {
  console.log("   controllo assente");
} else {
  const wrap = model.parentNode;
  model.dispatch("click");
  const rows = wrap.querySelectorAll(".fs-item");
  console.log(`   voci: ${rows.length}`);
  for (const row of rows.slice(0, 4).concat(rows.slice(-2))) {
    const flags = row.querySelectorAll(".fs-flag").length;
    console.log(`   ${row.textContent.trim().padEnd(16)} bandiere=${flags} checked=${row.getAttribute("aria-checked")}`);
  }
  rows.find((r) => r.textContent.includes("large-v3"))?.dispatch("click");
}

console.log("\n--- menu della lingua ---");
const lang = doc.getElementById("optLang");
const langWrap = lang.parentNode;
lang.dispatch("click");
const langRows = langWrap.querySelectorAll(".fs-item");
console.log(`   voci: ${langRows.length} (la prima è la scelta corrente)`);
console.log("   prime 4:", langRows.slice(0, 4).map((r) => r.textContent.trim()).join(" | "));
console.log("   ultime 3:", langRows.slice(-3).map((r) => r.textContent.trim()).join(" | "));
console.log(`   gruppi: ${langWrap.querySelectorAll(".fs-group").map((g) => g.textContent).join(", ")}`);

console.log("\n--- testo vista (primi 200 char) ---");
console.log("   " + view.textContent.replace(/\s+/g, " ").trim().slice(0, 200));
process.exit(0);
