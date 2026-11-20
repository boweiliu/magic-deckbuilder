// AI Card Search page: shows the searches Claude writes in the chat, runs them, and lets you tweak and re-run them.
// Your edits are saved (Claude can read them); Claude's changes appear here on their own.

const $ = (id) => document.getElementById(id);
const POLL_MS = 2000;

let list = [];           // search summaries, newest change first
let current = null;      // the open search
let activeQuery = 0;     // which tab is shown
let results = [];        // per query: {sql, highlight, ranSql, data, error, running}
let pending = null;      // a newer version Claude wrote while you had unsaved edits
// You picked a search yourself (in the list, or a link naming one): don't jump to Claude's newest one.
let pinned = new URLSearchParams(location.search).has("s");
const wide = window.matchMedia("(min-width: 900px)");
let sqlOpen = wide.matches; // the search editor starts folded away on a phone
let order = localStorage.getItem("card-search-order") || "popular";
const ORDER_TEXT = { popular: "most played first", mv: "by mana value, most played first within each", name: "by name" };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

function manaCost(cost) {
  const wrap = el("span", { class: "cost" });
  if (!cost) return wrap;
  cost.split(" // ").forEach((part, i) => {
    if (i) wrap.append(el("span", { class: "sep" }, "//"));
    for (const m of part.matchAll(/\{([^}]+)\}/g)) {
      const sym = m[1].replace("/", "");
      const color = /^[WUBRGC]$/.test(m[1]) ? m[1] : (m[1].match(/[WUBRG]/) || [""])[0];
      wrap.append(el("span", { class: `pip ${color}`, title: `{${m[1]}}` }, sym));
    }
  });
  return wrap;
}

function showPreview(card) {
  if (!card?.image || !wide.matches) return;
  $("preview-img").src = card.image;
  const back = $("preview-back");
  if (card.image_back) { back.src = card.image_back; back.hidden = false; } else { back.hidden = true; }
}

function ago(iso) {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function when(iso) {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

const who = (by) => (by === "you" ? "you" : "Claude");

async function getJson(url, options) {
  const resp = await fetch(url, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || data.error || `error ${resp.status}`);
  return data;
}

const isDirty = () => results.some((r) => r.sql !== r.savedSql);

// ---- the list, and following Claude's changes ----

let listKey = "";

function renderList() {
  const key = JSON.stringify([current?.id, list.map((s) => [s.id, s.version, s.title, s.updated_by])]);
  if (key === listKey) return;
  listKey = key;
  const ul = $("history");
  ul.replaceChildren();
  for (const s of list) {
    ul.append(el("li", { class: current?.id === s.id ? "current" : "", onclick: () => { pinned = true; openSearch(s.id, true); } },
      s.title.length > 110 ? s.title.slice(0, 110) + "..." : s.title,
      el("span", { class: "when" }, `${when(s.updated_at)}, by ${who(s.updated_by)}, version ${s.version}`)));
  }
  $("welcome").hidden = list.length > 0;
  const picker = $("picker");
  picker.replaceChildren(...list.map((s) => {
    const option = el("option", { value: s.id }, `${s.title} (v${s.version}, ${who(s.updated_by)})`);
    option.selected = current?.id === s.id;
    return option;
  }));
  picker.hidden = list.length === 0;
}

async function poll() {
  try {
    const before = list;
    list = (await getJson("/api/searches")).searches;
    renderList();
    const newest = list[0];
    const isNewSearch = newest && before.length && !before.some((s) => s.id === newest.id);
    if (!current && newest) {
      await openSearch(new URLSearchParams(location.search).get("s") || newest.id, false);
    } else if (newest && newest.id !== current.id && newest.updated_by === "claude" && (isNewSearch || !pinned)
               && !isDirty() && newest.updated_at > current.updated_at) {
      await openSearch(newest.id, true);  // Claude is working on another search: follow it
    } else if (current) {
      const mine = list.find((s) => s.id === current.id);
      if (mine && mine.version !== current.version) await refreshCurrent();
    }
  } catch (e) {
    $("search-status").textContent = `Lost contact with the app (${e.message}); retrying`;
  }
  setTimeout(poll, POLL_MS);
}

async function refreshCurrent() {
  const record = await getJson(`/api/searches/${current.id}`);
  if (record.version === current.version) return;
  if (isDirty() && record.updated_by === "claude") { pending = record; $("update-banner").hidden = false; return; }
  apply(record);
}

// ---- one search ----

async function openSearch(id, push) {
  let record;
  try {
    record = await getJson(`/api/searches/${id}`);
  } catch (e) {
    if (list[0] && id !== list[0].id) return openSearch(list[0].id, push);
    return;
  }
  if (push) history.replaceState({}, "", `/?s=${id}`);
  current = null;
  results = [];
  activeQuery = 0;
  apply(record);
}

function apply(record) {
  const sameSearch = current?.id === record.id;
  const old = results;
  current = record;
  pending = null;
  $("update-banner").hidden = true;
  results = record.queries.map((q, i) => {
    const prev = sameSearch ? old[i] : null;
    const r = { sql: q.sql, savedSql: q.sql, highlight: q.highlight, data: null, error: null, running: false, ranSql: null };
    if (prev && prev.ranSql === q.sql && prev.highlight === q.highlight && prev.ranOrder === order) Object.assign(r, { data: prev.data, error: prev.error, ranSql: prev.ranSql, ranOrder: prev.ranOrder });
    // Keep an unsaved edit to another tab when only this one changed.
    if (prev && prev.sql !== prev.savedSql && prev.savedSql === q.sql) r.sql = prev.sql;
    return r;
  });
  if (activeQuery >= results.length) activeQuery = 0;
  render();
  renderList();
  results.forEach((r, i) => { if (r.ranSql !== r.sql) runQuery(i); });
}

function render() {
  const s = current;
  $("welcome").hidden = true;
  $("search").hidden = false;
  $("search-title").textContent = s.title;
  $("search-notes").textContent = s.notes;
  $("file-link").href = `/api/searches/${s.id}/file`;
  $("search-status").textContent = `v${s.version}, changed ${ago(s.updated_at)} by ${who(s.updated_by)}`;
  document.title = `${s.title.slice(0, 60)} - AI Card Search`;
  renderTabs();
  renderQuery();
}

// After switching tier or sort deep in a long list, start the new list at its top (just under the bar).
function toListTop() {
  const bar = $("bar").getBoundingClientRect();
  const top = $("query").getBoundingClientRect().top;
  if (top < bar.bottom) window.scrollBy(0, top - bar.bottom);
}

function renderTabs() {
  const tabs = $("tabs");
  tabs.replaceChildren();
  current.queries.forEach((q, i) => {
    const r = results[i];
    const n = r.running ? "..." : r.error ? "!" : r.data ? String(r.data.total) : "";
    tabs.append(el("button", { type: "button", role: "tab", class: i === activeQuery ? "active" : "",
      onclick: () => { activeQuery = i; renderTabs(); renderQuery(); toListTop(); } }, q.label, el("span", { class: "n" }, n)));
  });
}

function cardText(card) {
  const lines = card.oracle_text.split("\n");
  const marked = new Set(card.highlight_lines);
  const box = el("div", { class: "text" });
  const show = (all) => {
    box.replaceChildren();
    lines.forEach((line, i) => {
      if (marked.has(i)) box.append(el("mark", {}, line));
      else if (all || !marked.size) box.append(el("div", {}, line));
    });
    const hidden = lines.length - marked.size;
    if (!all && marked.size && hidden > 0) {
      box.append(el("span", { class: "more", onclick: () => show(true) }, `+ ${hidden} more line${hidden > 1 ? "s" : ""}`));
    }
  };
  show(false);
  return box;
}

function renderQuery() {
  const box = $("query");
  box.replaceChildren();
  const q = current.queries[activeQuery];
  if (!q) return;
  const i = activeQuery;
  const r = results[i];
  const agentSql = current.agent_sql[i];

  const sqlBox = el("textarea", { spellcheck: "false", class: "sql-text", "aria-label": "The search (SQL)" });
  sqlBox.value = r.sql;
  sqlBox.rows = Math.min(28, Math.max(6, r.sql.split("\n").length + 1));
  const hlBox = el("input", { type: "text", spellcheck: "false", class: "hl-text", "aria-label": "Highlight pattern" });
  hlBox.value = r.highlight || "";
  const state = el("span", { class: "edited" });
  const showState = () => {
    state.textContent = r.sql !== r.savedSql ? "not run yet" : q.by === "you" ? "your edit" : "";
  };
  showState();
  sqlBox.addEventListener("input", () => { r.sql = sqlBox.value; showState(); });
  sqlBox.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveAndRun(i, hlBox.value); } });
  hlBox.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); saveAndRun(i, hlBox.value); } });

  box.append(
    q.description ? el("p", { class: "query-desc" }, q.description) : null,
    el("details", { class: "sql", open: sqlOpen ? "" : null, ontoggle: (e) => { sqlOpen = e.currentTarget.open; } },
      el("summary", {}, q.by === "you" ? "Edit the search (your edit)" : "Edit the search"), sqlBox,
      el("label", { class: "hl" }, "Highlight lines matching ", hlBox),
      el("div", { class: "actions" },
        el("button", { type: "button", onclick: () => saveAndRun(i, hlBox.value) }, "Run (Ctrl+Enter)"),
        agentSql && agentSql !== r.sql
          ? el("button", { type: "button", class: "quiet", onclick: () => { r.sql = agentSql; saveAndRun(i, hlBox.value); } }, "Back to Claude's version")
          : null,
        state)),
  );
  const out = el("div", { id: "cards" });
  box.append(out);
  renderCards(out, r);
}

function renderCards(out, r) {
  if (r.running) { out.append(el("p", { class: "run-status" }, "Running...")); return; }
  if (r.error) { out.append(el("p", { class: "error" }, `The search did not run: ${r.error}`)); return; }
  if (!r.data) return;
  const shown = r.data.cards.length;
  out.append(el("p", { class: "run-status" },
    `${r.data.total} cards, ${ORDER_TEXT[r.ranOrder] || ""}${shown < r.data.total ? ` (first ${shown} shown)` : ""} - ${r.data.seconds}s`));
  let group = null;
  for (const card of r.data.cards) {
    if (r.ranOrder === "mv") {
      const mv = card.cmc == null ? "?" : String(card.cmc);
      if (mv !== group) {
        group = mv;
        const n = r.data.cards.filter((c) => String(c.cmc ?? "?") === mv).length;
        out.append(el("h3", { class: "mv-head" }, `Mana value ${mv}`, el("span", {}, `${n} card${n === 1 ? "" : "s"}`)));
      }
    }
    const row = el("div", { class: "card", onmouseenter: () => showPreview(card), onclick: (e) => toggleImage(e, row, card) },
      card.scryfall_uri ? el("a", { href: card.scryfall_uri, target: "_blank", rel: "noopener" }, card.name) : el("span", {}, card.name),
      manaCost(card.mana_cost),
      el("span", { class: "type" }, card.type_line),
      cardText(card));
    out.append(row);
  }
  if (!$("preview-img").getAttribute("src") && r.data.cards[0]) showPreview(r.data.cards[0]);
}

// On a phone there is no hover: tapping a card shows its picture under it (tap again to hide).
function toggleImage(e, row, card) {
  if (wide.matches || e.target.closest("a, .more") || !card.image) return;
  const shown = row.querySelector(".inline-img");
  if (shown) { shown.remove(); return; }
  row.append(el("div", { class: "inline-img" },
    el("img", { src: card.image, alt: card.name, loading: "lazy" }),
    card.image_back ? el("img", { src: card.image_back, alt: `${card.name} (back)`, loading: "lazy" }) : null));
}

function rerenderCards(i) {
  renderTabs();
  if (i !== activeQuery) return;
  const out = $("cards");
  if (!out) { renderQuery(); return; }
  out.replaceChildren();
  renderCards(out, results[i]);
}

async function saveAndRun(i, highlight) {
  const r = results[i];
  r.highlight = highlight || null;
  if (r.sql !== r.savedSql || r.highlight !== current.queries[i].highlight) {
    try {
      const record = await getJson(`/api/searches/${current.id}/queries/${i}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sql: r.sql, highlight: r.highlight }),
      });
      apply(record);
      return;
    } catch (e) {
      r.error = `could not save your edit (${e.message})`;
      rerenderCards(i);
      return;
    }
  }
  runQuery(i);
}

async function runQuery(i) {
  const r = results[i];
  const searchId = current.id;
  const sql = r.sql;
  r.running = true; r.error = null;
  rerenderCards(i);
  try {
    r.data = await getJson("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql, highlight: r.highlight, order }),
    });
  } catch (e) {
    r.data = null; r.error = e.message;
  }
  r.running = false;
  r.ranSql = sql;
  r.ranOrder = order;
  if (current.id !== searchId) return;
  rerenderCards(i);
}

$("sort").value = order;
$("sort").addEventListener("change", (e) => {
  order = e.target.value;
  localStorage.setItem("card-search-order", order);
  if (current) results.forEach((_, i) => runQuery(i));
  toListTop();
});
$("picker").addEventListener("change", (e) => { pinned = true; openSearch(e.target.value, true); });
$("search-notes").addEventListener("click", (e) => e.currentTarget.classList.toggle("open"));
$("load-update").addEventListener("click", () => { if (pending) apply(pending); });

poll();
