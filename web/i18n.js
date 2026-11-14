/* web/i18n.js
   ===========
   Traduzioni dell'interfaccia, senza build e senza librerie.

   - Un file JSON per lingua in `web/i18n/<lang>.json`, chiavi piatte con
     namespace a punti ("jobs.title"). L'inglese è la lingua di riferimento: una
     chiave che manca nell'altra lingua ricade sull'inglese, poi sulla chiave.
   - Interpolazione `{name}`; plurali come oggetto {"one": ..., "other": ...}
     scelto con `Intl.PluralRules` sul parametro `count`.
   - HTML statico: `data-i18n="key"` per il testo, `data-i18n-attr="placeholder:key;title:key"`
     per gli attributi. `applyStaticI18n()` li riempie all'avvio.
   - La lingua scelta sta in localStorage; il default è l'inglese. */

const I18N = { lang: "en", dict: {}, fallback: {} };
const LANGS = { en: "English", it: "Italiano" };

async function loadI18n() {
  let lang = "en";
  try { lang = localStorage.getItem("trascrivi-lang") || "en"; } catch { /* storage bloccato */ }
  if (!LANGS[lang]) lang = "en";
  const fetchDict = async (code) => {
    const res = await fetch(`/i18n/${code}.json`, { cache: "no-cache" });
    return res.ok ? res.json() : {};
  };
  I18N.fallback = await fetchDict("en").catch(() => ({}));
  I18N.dict = lang === "en" ? I18N.fallback : await fetchDict(lang).catch(() => ({}));
  I18N.lang = lang;
  document.documentElement.lang = lang;
}

function t(key, vars = {}) {
  let value = I18N.dict[key] ?? I18N.fallback[key];
  if (value === undefined) return key;
  if (typeof value === "object") {
    const rule = new Intl.PluralRules(I18N.lang).select(Number(vars.count ?? 0));
    value = value[rule] ?? value.other ?? "";
  }
  return String(value).replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m));
}

function setLang(lang) {
  try { localStorage.setItem("trascrivi-lang", lang); } catch { /* ignora */ }
  location.reload();
}

function applyStaticI18n(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  root.querySelectorAll("[data-i18n-attr]").forEach((node) => {
    node.dataset.i18nAttr.split(";").forEach((pair) => {
      const [attr, key] = pair.split(":").map((s) => s.trim());
      if (attr && key) node.setAttribute(attr, t(key));
    });
  });
}

if (typeof module !== "undefined" && module.exports) module.exports = { t, I18N };
