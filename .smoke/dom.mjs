/* Minimal DOM shim: just enough to boot app.js and render every view.
   Not a test suite — a smoke check that the redesigned frontend runs. */

export function makeDom() {
  const listeners = [];
  const registry = new Map();

  class NodeBase {
    constructor(tag = "") {
      this.tagName = String(tag).toUpperCase();
      this.children = [];
      this.parentNode = null;
      this.attrs = {};
      this.dataset = {};
      this.style = new Proxy({}, { set: (t, k, v) => { t[k] = v; return true; } });
      this._text = "";
      this._html = "";
      this.classList = makeClassList(this);
      this.id = "";
    }
    get textContent() {
      if (this._text) return this._text;
      if (this._html) return this._html.replace(/<[^>]*>/g, "");
      return this.children.map((c) => c.textContent).join("");
    }
    set textContent(v) { this._text = String(v); this._html = ""; this.children = []; }
    get innerHTML() { return this._html; }
    set innerHTML(v) { this._html = String(v); this._text = ""; this.children = []; }
    get className() { return [...this.classList._set].join(" "); }
    set className(v) {
      this.classList._set.clear();
      String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList._set.add(c));
    }
    setAttribute(k, v) {
      this.attrs[k] = String(v);
      if (k === "id") this.id = String(v);
      if (k === "class") { this.classList._set.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList._set.add(c)); }
      if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-(\w)/g, (m, c) => c.toUpperCase())] = String(v);
    }
    getAttribute(k) { return this.attrs[k] ?? null; }
    removeAttribute(k) { delete this.attrs[k]; }
    hasAttribute(k) { return k in this.attrs; }
    append(...nodes) {
      for (const n of nodes.flat(Infinity)) {
        if (n === null || n === undefined || n === false) continue;
        const node = n instanceof NodeBase || n instanceof TextNode ? n : new TextNode(String(n));
        node.parentNode = this;
        this.children.push(node);
      }
    }
    appendChild(n) { this.append(n); return n; }
    replaceChildren(...nodes) { this.children = []; this._text = ""; this.append(...nodes); }
    addEventListener(type, fn) { listeners.push({ target: this, type, fn }); }
    removeEventListener(type, fn) {
      const i = listeners.findIndex((l) => l.target === this && l.type === type && l.fn === fn);
      if (i >= 0) listeners.splice(i, 1);
    }
    dispatch(type, extra = {}) {
      const ev = { type, target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...extra };
      for (const l of [...listeners]) if (l.target === this && l.type === type) l.fn(ev);
      return ev;
    }
    contains(n) {
      if (n === this) return true;
      return this.children.some((c) => c.contains && c.contains(n));
    }
    focus() { doc.activeElement = this; }
    blur() { if (doc.activeElement === this) doc.activeElement = null; }
    select() {}
    click() { this.dispatch("click"); }
    scrollIntoView() {}
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    querySelectorAll(sel) { return queryAll(this, sel); }
    get firstChild() { return this.children[0] || null; }
    remove() {
      if (this.parentNode) {
        const i = this.parentNode.children.indexOf(this);
        if (i >= 0) this.parentNode.children.splice(i, 1);
      }
    }
  }

  class TextNode {
    constructor(t) { this._text = t; this.children = []; this.tagName = "#text"; }
    get textContent() { return this._text; }
    set textContent(v) { this._text = String(v); }
    contains() { return false; }
  }

  function makeClassList(node) {
    const set = new Set();
    return {
      add: (...c) => c.forEach((x) => set.add(x)),
      remove: (...c) => c.forEach((x) => set.delete(x)),
      toggle: (c, force) => (force === undefined ? (set.has(c) ? set.delete(c) : set.add(c)) : (force ? set.add(c) : set.delete(c))),
      contains: (c) => set.has(c),
      _set: set,
    };
  }

  /* Very small selector engine: #id, .class, tag, [attr=value], and the
     comma-free descendant combinators the app actually uses. */
  function matches(node, sel) {
    if (!node.classList) return false;
    return sel.split(/(?=[.#\[])/).every((part) => {
      if (!part) return true;
      if (part.startsWith("#")) return node.id === part.slice(1);
      if (part.startsWith(".")) return node.classList.contains(part.slice(1));
      if (part.startsWith("[")) {
        const m = part.match(/^\[([\w-]+)(?:=["']?([^\]"']*)["']?)?\]$/);
        if (!m) return true;
        const [, k, v] = m;
        const actual = node.attrs[k];
        return v === undefined ? actual !== undefined : actual === v;
      }
      return node.tagName === part.toUpperCase();
    });
  }

  function queryAll(root, selector) {
    const parts = selector.trim().split(/\s+/);
    let current = [root];
    for (const part of parts) {
      const next = [];
      for (const node of current) {
        const walk = (n) => {
          for (const c of n.children) {
            if (c.tagName && matches(c, part)) next.push(c);
            if (c.children) walk(c);
          }
        };
        walk(node);
      }
      current = next;
    }
    return current;
  }

  const doc = new NodeBase("#document");
  doc.documentElement = new NodeBase("html");
  doc.documentElement.dataset = {};
  doc.body = new NodeBase("body");
  doc.head = new NodeBase("head");
  doc.append(doc.documentElement);
  doc.documentElement.append(doc.body);
  doc.activeElement = null;
  doc.createElement = (tag) => new NodeBase(tag);
  doc.createElementNS = (ns, tag) => new NodeBase(tag);
  doc.createTextNode = (t) => new TextNode(t);
  doc.getElementById = (id) => queryAll(doc, `#${id}`)[0] || null;
  doc.querySelector = (s) => queryAll(doc, s)[0] || null;
  doc.querySelectorAll = (s) => queryAll(doc, s);
  doc.addEventListener = NodeBase.prototype.addEventListener.bind(doc);
  doc.removeEventListener = NodeBase.prototype.removeEventListener.bind(doc);
  doc.dispatch = NodeBase.prototype.dispatch.bind(doc);
  doc.contains = () => true;

  return { doc, listeners, NodeBase, TextNode, makeClassList };
}

export function installGlobals(dom) {
  const { doc } = dom;
  globalThis.document = doc;
  globalThis.Node = dom.NodeBase;
  globalThis.localStorage = {
    _v: new Map(),
    getItem(k) { return this._v.has(k) ? this._v.get(k) : null; },
    setItem(k, v) { this._v.set(k, String(v)); },
    removeItem(k) { this._v.delete(k); },
  };
  globalThis.location = { hash: "#/", href: "http://127.0.0.1:8000/" };
  const win = dom.win || (dom.win = {});
  win.addEventListener = (type, fn) => doc.addEventListener(type, fn);
  win.removeEventListener = (type, fn) => doc.removeEventListener(type, fn);
  win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  globalThis.window = win;
  globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  // Node 24 definisce `navigator` come getter di sola lettura: lo ridefiniamo.
  Object.defineProperty(globalThis, "navigator", {
    value: { clipboard: { writeText: async () => {} } },
    configurable: true,
    writable: true,
  });
  // I timer devono davvero scattare: l'app li usa per il salvataggio
  // differito e il polling. Il conteggio serve a fermare il processo.
  const timers = new Set();
  const realSetTimeout = globalThis.setTimeout.bind(globalThis);
  const realClearTimeout = globalThis.clearTimeout.bind(globalThis);
  const realSetInterval = globalThis.setInterval.bind(globalThis);
  const realClearInterval = globalThis.clearInterval.bind(globalThis);
  globalThis.setTimeout = (fn, ms, ...rest) => realSetTimeout(fn, ms, ...rest);
  globalThis.clearTimeout = (t) => realClearTimeout(t);
  globalThis.setInterval = (fn, ms, ...rest) => {
    const t = realSetInterval(fn, ms, ...rest);
    timers.add(t);
    return t;
  };
  globalThis.clearInterval = (t) => { timers.delete(t); realClearInterval(t); };
  return { timers, doc };
}
