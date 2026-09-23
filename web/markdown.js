/* web/markdown.js
   ==============
   Markdown -> HTML per il riassunto, che è un documento Markdown scritto dal
   modello.

   Sta in un file a sé e non dentro `app.js` per una ragione pratica: così è una
   funzione pura richiamabile da node e la si può testare davvero. È il pezzo che
   l'utente guarda per venti minuti, e sbagliare una lista o una tabella si vede
   subito.

   Regola di sicurezza: si **escapa prima** e si formatta dopo, quindi l'HTML che
   il modello dovesse scrivere finisce a schermo come testo. Non si generano link:
   non servono in delle note di lezione e sono la via più facile per un
   `javascript:`.

   Volutamente minimale — titoli, liste, tabelle, blocchi di codice, grassetto,
   corsivo — perché il progetto non ha un build step e non vogliamo dipendenze. */

(function (root) {
  "use strict";

  const mdEscape = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  const inlineMd = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");

  const mdCells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

  function renderMarkdown(src) {
    const lines = mdEscape(String(src ?? "")).split("\n");
    const out = [];
    let listTag = null;   // lista attualmente aperta: "ul" | "ol" | null
    let para = [];

    const flushPara = () => {
      if (para.length) {
        out.push(`<p>${inlineMd(para.join(" "))}</p>`);
        para = [];
      }
    };
    const closeList = () => {
      if (listTag) {
        out.push(`</${listTag}>`);
        listTag = null;
      }
    };

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      // Blocco di codice: si copia alla lettera. È anche dove finiscono gli schemi.
      if (/^\s*```/.test(line)) {
        flushPara();
        closeList();
        const body = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) body.push(lines[i++]);
        out.push(`<pre><code>${body.join("\n")}</code></pre>`);
        continue;
      }

      // Tabella: la riga di separazione `|---|---|` è obbligatoria, altrimenti
      // una riga che inizia con `|` sarebbe solo testo.
      if (/^\s*\|/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
        flushPara();
        closeList();
        const head = mdCells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(mdCells(lines[i++]));
        i--;
        out.push(`<table><thead><tr>${head.map((c) => `<th>${inlineMd(c)}</th>`).join("")}</tr></thead>`
          + `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inlineMd(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
        continue;
      }

      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        flushPara();
        closeList();
        const level = heading[1].length;
        out.push(`<h${level}>${inlineMd(heading[2].trim())}</h${level}>`);
        continue;
      }

      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flushPara();
        closeList();
        out.push("<hr>");
        continue;
      }

      const bullet = /^(\s*)[-*+]\s+(.*)$/.exec(line);
      const numbered = /^(\s*)\d+[.)]\s+(.*)$/.exec(line);
      if (bullet || numbered) {
        flushPara();
        const tag = bullet ? "ul" : "ol";
        const indent = (bullet || numbered)[1].replace(/\t/g, "  ").length;
        if (listTag !== tag) {
          closeList();
          listTag = tag;
          out.push(`<${tag} class="md-list">`);
        }
        // L'annidamento si rende con un rientro, non con una lista dentro una
        // lista: `<ul>` figlio diretto di `<ul>` è HTML non valido e i browser lo
        // riparano a modo loro. Il rientro dà lo stesso risultato e non sorprende.
        const depth = Math.min(3, Math.floor(indent / 2));
        const style = depth ? ` style="margin-left:${depth * 18}px"` : "";
        out.push(`<li${style}>${inlineMd((bullet || numbered)[2])}</li>`);
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        flushPara();
        closeList();
        out.push(`<blockquote>${inlineMd(line.replace(/^\s*>\s?/, ""))}</blockquote>`);
        continue;
      }

      if (!line.trim()) {
        flushPara();
        closeList();
        continue;
      }

      para.push(line.trim());
    }

    flushPara();
    closeList();
    return out.join("\n");
  }

  root.renderMarkdown = renderMarkdown;
  // Riga per i test: in node il file è un modulo CommonJS, nel browser no.
  if (typeof module !== "undefined" && module.exports) module.exports = { renderMarkdown };
})(typeof globalThis !== "undefined" ? globalThis : this);
