// Deck List page: type a card name, pick from the suggestions, the list saves itself. Cards can carry
// categories (chips); drag a card toward a category to tag it. Every change is saved on the server.

import { el, chip, zoneAt, drawZones } from "/static/zones.js";
import { api, report } from "/static/api.js";

// Which list: the one named in the address, else the one the server put inside the page.
// Opening the app with no list named goes back to the list that was open last.
const LAST_DECK = "deck-list:last-deck";
const inPageDeck = deckInPage();
const named = new URLSearchParams(location.search).get("deck");
const deckName = named || inPageDeck?.deck || "ulalek";
function rememberedDeck(write) {
  try {
    if (write) localStorage.setItem(LAST_DECK, write);
    return localStorage.getItem(LAST_DECK);
  } catch {
    return null; // storage blocked (private browsing, some framed pages): always open the default list
  }
}
const lastDeck = rememberedDeck();
if (!named && lastDeck && lastDeck !== deckName) {
  location.replace(`/?deck=${encodeURIComponent(lastDeck)}`);
} else {
  rememberedDeck(deckName);
  if (!named) history.replaceState(null, "", `/?deck=${encodeURIComponent(deckName)}`);
}
const TARGET = 99;

const title = (name) => name.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

const $ = (id) => document.getElementById(id);
const input = $("add-input");
const list = $("suggestions");
const message = $("add-message");

let deck = { commander: [], cards: [], categories: [], tags: {} };
let suggestions = [];
let active = -1;
let suggestSeq = 0;

function commanderIdentity() {
  const ids = new Set();
  for (const c of deck.commander) for (const x of c.card?.color_identity || []) ids.add(x);
  return ids;
}

function problems(entry) {
  if (!entry.card) return "not a card name";
  const out = [];
  const legal = entry.card.commander_legality;
  if (legal && legal !== "legal") out.push(legal === "banned" ? "banned in Commander" : "not Commander legal");
  if (deck.commander.length) {
    const ids = commanderIdentity();
    if (entry.card.color_identity.some((x) => !ids.has(x))) out.push("outside commander colors");
  }
  return out.join(", ");
}

function showPreview(card) {
  if (!card?.image) return;
  $("preview-img").src = card.image;
  const back = $("preview-back");
  if (card.image_back) { back.src = card.image_back; back.hidden = false; } else { back.hidden = true; }
}

function render(justTagged = null) {
  const count = deck.cards.reduce((n, c) => n + c.qty, 0);
  const countEl = $("deck-count");
  countEl.textContent = `${count} / ${TARGET}`;
  countEl.className = "count" + (count === TARGET ? " full" : count > TARGET ? " over" : "");
  $("text-link").href = `/api/decks/${deckName}/text`;
  document.title = `${deckName} - Deck List`;

  const cmd = $("commander");
  cmd.replaceChildren();
  for (const c of deck.commander) {
    cmd.append(
      c.card?.image ? el("img", { src: c.card.image, alt: c.name, onmouseenter: () => showPreview(c.card) }) : null,
      el("div", {}, el("div", { class: "label" }, "Commander"),
        el("div", { class: "name" }, c.name)),
    );
    if (!$("preview-img").getAttribute("src")) showPreview(c.card);
  }

  // One list, cheapest first; cards with no known name go last.
  const rows = deck.cards.map((entry, index) => ({ entry, index }));
  const mv = (entry) => (entry.card ? entry.card.mana_value : Infinity);
  rows.sort((a, b) => mv(a.entry) - mv(b.entry) || a.entry.name.localeCompare(b.entry.name));
  const cards = $("cards");
  cards.replaceChildren();
  for (const { entry, index } of rows) {
    const warn = problems(entry);
    cards.append(el("div", {
      class: "row", "data-card": entry.name, onmouseenter: () => showPreview(entry.card),
    },
      el("span", { class: "qty" }, entry.qty > 1 ? `${entry.qty}` : ""),
      el("span", { class: "card-name" },
        entry.card?.scryfall_uri ? el("a", { href: entry.card.scryfall_uri, target: "_blank", rel: "noopener", draggable: "false" }, entry.name) : entry.name,
        el("span", { class: "chips" }, ...tagsOf(entry.name).map((cat) => {
          const c = chip(cat, { onRemove: () => setTag(entry.name, cat.id, false) });
          if (justTagged && justTagged.name === entry.name && justTagged.id === cat.id) c.classList.add("added");
          return c;
        })),
        warn ? el("span", { class: "warn" }, warn) : null),
      el("button", { title: "Remove one", "aria-label": `Remove ${entry.name}`, onclick: () => removeAt(index) }, "×"),
    ));
  }
  $("empty").hidden = deck.cards.length > 0;
  $("legend").replaceChildren(...deck.categories.map((cat) => {
    const c = chip(cat);
    c.append(el("span", { class: "n" }, String(deck.cards.filter((e) => tagsOf(e.name).some((t) => t.id === cat.id)).length)));
    return c;
  }), el("a", { class: "manage", href: `/categories?deck=${encodeURIComponent(deckName)}` }, "Categories"),
  el("button", { class: "manage autotag-link", type: "button", onclick: openAutotag }, "Auto-tag"),
  el("a", { class: "manage grouped-link", href: `/groups?deck=${encodeURIComponent(deckName)}` }, "Grouped view"));
}

let loaded = false;

function tagsOf(name) {
  const ids = deck.tags[name] || [];
  return deck.categories.filter((c) => ids.includes(c.id));
}

function applyServerDeck(data, justTagged = null) {
  deck = { commander: data.commander, cards: data.cards, categories: data.categories, tags: data.tags };
  $("save-status").textContent = data.backed_up ? "Saved, copied to GitHub" : "Saved";
  render(justTagged);
}

// Every change goes to the server as one add or remove; the server applies it to the saved
// list, so this page can never overwrite cards it did not load. Changes are sent one after
// another in the order they were made, so a fast run of changes is applied and shown in order.
let saving = Promise.resolve();
let waiting = 0;

function queued(work) {
  waiting++;
  $("save-status").textContent = "Saving...";
  const run = saving.then(work).finally(() => { waiting--; });
  saving = run.catch(() => {});
  return run;
}

function couldNotSave(e, show) {
  $("save-status").textContent = "Not saved";
  report("save failed", { error: String(e?.message || e) });
  show(`Could not save: ${e?.message || e}. Nothing was lost; try again.`);
}

function change(action, name) {
  if (!loaded) return Promise.resolve(false);
  return queued(async () => {
    try {
      const { ok, status, data } = await api(`/api/decks/${deckName}/${action}`, { method: "POST", body: { name } });
      if (status === 409) { message.textContent = data.detail + "."; await load(); return false; }
      if (!ok) throw new Error(data.detail || status);
      applyServerDeck(data);
      setTimeout(refreshBackupStatus, 4000);
      return true;
    } catch (e) {
      couldNotSave(e, (text) => { message.textContent = text; });
      await load();
      return false;
    }
  });
}

async function refreshBackupStatus() {
  if (waiting) return;
  try {
    const { ok, data } = await api(`/api/decks/${deckName}`);
    if (ok && !waiting) $("save-status").textContent = data.backed_up ? "Saved, copied to GitHub" : "Saved";
  } catch {
    // The status line just stays as it is; the next save shows the real state.
  }
}

function add(card) {
  message.textContent = "";
  const existing = deck.cards.find((c) => c.name === card.name);
  const isBasic = /\bBasic\b/.test(card.type_line) || /any number of cards named/.test(card.oracle_text || "");
  if (deck.commander.some((c) => c.name === card.name)) {
    message.textContent = `${card.name} is your commander.`;
  } else if (existing && !isBasic) {
    message.textContent = `${card.name} is already in the deck.`;
    document.querySelector(`.row[data-card="${CSS.escape(card.name)}"]`)?.scrollIntoView({ block: "center" });
  } else {
    change("add", card.name);
  }
  input.value = "";
  closeSuggestions();
  input.focus();
}

function removeAt(index) {
  change("remove", deck.cards[index].name);
}

function closeSuggestions() {
  suggestions = [];
  active = -1;
  list.hidden = true;
  list.replaceChildren();
}

// A card already in the list can't be picked again (basic lands can: any number of those).
function taken(card) {
  if (deck.commander.some((c) => c.name === card.name)) return "your commander";
  if (/\bBasic\b/.test(card.type_line)) return "";
  return deck.cards.some((c) => c.name === card.name) ? "already in the list" : "";
}

// The next suggestion that can be picked, going one way from `from`; -1 when there is none.
function nextPickable(from, dir) {
  for (let n = 1; n <= suggestions.length; n++) {
    const i = (((from + dir * n) % suggestions.length) + suggestions.length) % suggestions.length;
    if (!taken(suggestions[i])) return i;
  }
  return -1;
}

function renderSuggestions() {
  list.replaceChildren();
  suggestions.forEach((card, i) => {
    const inList = taken(card);
    const flag = inList || problems({ card, name: card.name });
    list.append(el("li", {
      class: (i === active ? "active" : "") + (inList ? " taken" : ""), role: "option", "data-card": card.name,
      "aria-disabled": inList ? "true" : null,
      onmousedown: (e) => { e.preventDefault(); if (!inList) add(card); },
      onmouseenter: () => { if (!inList) { active = i; renderActive(); } showPreview(card); },
    },
      el("span", {}, card.name),
      el("span", { class: "type" }, card.type_line, flag ? el("span", { class: inList ? "" : "flag" }, ` - ${flag}`) : null),
    ));
  });
  list.hidden = suggestions.length === 0;
}

function renderActive() {
  [...list.children].forEach((li, i) => li.classList.toggle("active", i === active));
  list.children[active]?.scrollIntoView({ block: "nearest" });
}

async function updateSuggestions() {
  const q = input.value.trim();
  const seq = ++suggestSeq;
  if (!q) { closeSuggestions(); return; }
  let found;
  try {
    found = await api(`/api/cards/suggest?q=${encodeURIComponent(q)}`);
  } catch {
    return;
  }
  if (seq !== suggestSeq) return;
  suggestions = found.data.cards || [];
  active = suggestions.length ? nextPickable(-1, 1) : -1;
  renderSuggestions();
  if (suggestions[active]) showPreview(suggestions[active]);
}

input.addEventListener("input", () => { message.textContent = ""; updateSuggestions(); });
input.addEventListener("keydown", (e) => {
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && suggestions.length) {
    e.preventDefault();
    const next = nextPickable(active, e.key === "ArrowDown" ? 1 : -1);
    if (next >= 0) { active = next; renderActive(); showPreview(suggestions[active]); }
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (suggestions[active] && !taken(suggestions[active])) add(suggestions[active]);
  } else if (e.key === "Escape") {
    closeSuggestions();
  }
});
input.addEventListener("blur", () => setTimeout(closeSuggestions, 100));

// ---- Switching lists, starting a new one ----

const picker = $("deck-pick");
const NEW = "+new"; // never a list name: those start with a letter or digit

async function fillPicker() {
  let saved = [];
  try {
    const { ok, data } = await api("/api/decks");
    if (ok) saved = data.decks;
  } catch {
    // Only this list shows until the next load.
  }
  if (!saved.some((d) => d.name === deckName)) saved.push({ name: deckName, cards: deck.cards.reduce((n, c) => n + c.qty, 0) });
  picker.replaceChildren(
    ...saved.map((d) => el("option", { value: d.name }, title(d.name))),
    el("option", { value: NEW }, "New list..."),
  );
  picker.value = deckName;
}

picker.replaceChildren(el("option", { value: deckName }, title(deckName)));
picker.addEventListener("change", () => {
  if (picker.value !== NEW) { location.href = `/?deck=${encodeURIComponent(picker.value)}`; return; }
  picker.value = deckName;
  $("new-deck-from").textContent = title(deckName);
  $("new-deck-message").textContent = "";
  $("new-deck").hidden = false;
  $("new-deck-name").focus();
});
$("new-deck-cancel").addEventListener("click", () => { $("new-deck").hidden = true; });
$("new-deck").addEventListener("submit", async (e) => {
  e.preventDefault();
  const say = (text) => { $("new-deck-message").textContent = text; };
  const typed = $("new-deck-name").value;
  const name = typed.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
  if (!name) { say("Give the list a name (letters and numbers)."); return; }
  say("Creating...");
  try {
    const copy = $("new-deck-copy").checked;
    const { ok, status, data } = await api(`/api/decks/${name}/create`, { method: "POST", body: { copy_from: copy ? deckName : null } });
    if (status === 409) { say(`There is already a list called ${title(name)}.`); return; }
    if (!ok) throw new Error(data.detail || status);
    location.href = `/?deck=${encodeURIComponent(name)}`;
  } catch (err) {
    report("could not create a list", { name, error: String(err?.message || err) });
    say(`Could not create it: ${err?.message || err}. Try again.`);
  }
});

// ---- Tags ----

let lastTag = null;
let toastTimer;

function toast(text, undo) {
  $("toast-text").textContent = text;
  $("toast-undo").hidden = !undo;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4000);
}

// One tag on or off, saved on the server like a card change.
function setTag(name, id, on, { undoable = true } = {}) {
  if (!loaded) return Promise.resolve();
  return queued(async () => {
    try {
      const { ok, data } = await api(`/api/decks/${deckName}/tag`, { method: "POST", body: { card: name, category: id, on } });
      if (!ok) { toast(`Not saved: ${data.detail}.`, false); await load(); return; }
      applyServerDeck(data, on ? { name, id } : null);
      setTimeout(refreshBackupStatus, 4000);
      const cat = deck.categories.find((c) => c.id === id);
      lastTag = undoable ? { name, id, on } : null;
      toast(`${on ? "Added" : "Removed"} ${cat?.name || "tag"} ${on ? "to" : "from"} ${name}`, undoable);
    } catch (e) {
      couldNotSave(e, (text) => toast(text, false));
      await load();
    }
  });
}

// ---- Auto-tag: suggested tags, reviewed before they are added ----

const autotagForm = $("autotag");

function updateAutotagButton() {
  const n = autotagForm.querySelectorAll("#autotag-list input:checked").length;
  $("autotag-add").textContent = n ? `Add ${n} tag${n === 1 ? "" : "s"}` : "Add";
  $("autotag-add").disabled = n === 0;
}

async function openAutotag() {
  if (!loaded) return;
  autotagForm.hidden = false;
  $("autotag-list").replaceChildren(el("p", { class: "autotag-why" }, "Looking at your cards..."));
  updateAutotagButton();
  let found;
  try {
    const { ok, data } = await api(`/api/decks/${deckName}/autotag`);
    if (!ok) throw new Error(data.detail);
    found = data;
  } catch (e) {
    report("could not get tag suggestions", { error: String(e?.message || e) });
    $("autotag-list").replaceChildren(el("p", { class: "autotag-why" }, "Could not get suggestions. Try again."));
    return;
  }
  const byId = new Map(deck.categories.map((c) => [c.id, c]));
  const sections = found.guesses.filter((g) => byId.has(g.category)).map((g) => {
    const cat = byId.get(g.category);
    const head = el("h3", {}, chip(cat), el("span", { class: "autotag-why" },
      g.cards.length ? ` ${g.cards.length} to add - ${g.why}` : ` nothing new - ${g.why}`));
    const boxes = g.cards.map((name) => {
      const box = el("input", { type: "checkbox", "data-card": name, "data-category": g.category, onchange: updateAutotagButton });
      box.checked = true;
      const entry = deck.cards.find((c) => c.name === name);
      return el("label", { onmouseenter: () => showPreview(entry?.card) }, box, name);
    });
    if (boxes.length) {
      const all = el("button", { type: "button", class: "autotag-all", onclick: () => {
        const on = !boxes.every((l) => l.firstChild.checked);
        boxes.forEach((l) => { l.firstChild.checked = on; });
        updateAutotagButton();
      } }, "Tick / untick all");
      head.append(all);
    }
    return el("section", {}, head, boxes.length ? el("div", { class: "members" }, ...boxes) : null);
  });
  const missing = found.no_rule.map((id) => byId.get(id)?.name).filter(Boolean);
  if (missing.length) sections.push(el("p", { class: "autotag-why" }, `No way to guess: ${missing.join(", ")}.`));
  $("autotag-list").replaceChildren(...sections);
  updateAutotagButton();
}

$("autotag-cancel").addEventListener("click", () => { autotagForm.hidden = true; });
autotagForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const picked = [...autotagForm.querySelectorAll("#autotag-list input:checked")]
    .map((b) => ({ card: b.dataset.card, category: b.dataset.category }));
  if (!picked.length) return;
  autotagForm.hidden = true;
  queued(async () => {
    try {
      const { ok, data } = await api(`/api/decks/${deckName}/tags/add-many`, { method: "POST", body: { tags: picked } });
      if (!ok) { toast(`Not saved: ${data.detail}.`, false); await load(); return; }
      applyServerDeck(data);
      setTimeout(refreshBackupStatus, 4000);
      lastTag = null;
      toast(`Added ${picked.length} tag${picked.length === 1 ? "" : "s"}`, false);
    } catch (err) {
      couldNotSave(err, (text) => toast(text, false));
      await load();
    }
  });
});

$("toast-undo").addEventListener("click", () => {
  if (!lastTag) return;
  const { name, id, on } = lastTag;
  lastTag = null;
  setTag(name, id, !on, { undoable: false });
});

// ---- Hold a card, drop it on a zone ----
// Mouse: drag a row a few pixels to pick it up. Touch: press and hold, then slide. The card
// does not move; the zone it is dropped on toggles that category.

const zones = $("zones");
const ghost = $("ghost");
let pending = null; // { row, entry, x, y, timer, touch }
let lifted = null; // { row, entry, zone }
let suppressClick = false;

function paintZones(hot) {
  drawZones($("zone-svg"), $("zone-labels"), deck.categories, innerWidth, innerHeight,
    { hot, has: tagsOf(lifted.entry.name).map((c) => c.id) });
}

function lift() {
  const { row, entry, x, y } = pending;
  pending = null;
  lifted = { row, entry, zone: -1 };
  row.classList.add("lifted");
  $("ghost-img").src = entry.card?.image || "";
  $("ghost-name").textContent = entry.name;
  zones.hidden = false;
  document.body.classList.add("dragging");
  navigator.vibrate?.(15);
  paintZones(-1);
  moveLifted(x, y);
}

function moveLifted(x, y) {
  ghost.style.left = `${x}px`;
  ghost.style.top = `${y}px`;
  const zone = zoneAt(x, y, innerWidth, innerHeight, deck.categories.length);
  if (zone !== lifted.zone) { lifted.zone = zone; paintZones(zone); }
}

function drop() {
  const { row, entry, zone } = lifted;
  lifted = null;
  zones.hidden = true;
  document.body.classList.remove("dragging");
  row.classList.remove("lifted");
  suppressClick = true;
  setTimeout(() => { suppressClick = false; }, 0);
  if (zone >= 0) {
    const id = deck.categories[zone].id;
    setTag(entry.name, id, !tagsOf(entry.name).some((c) => c.id === id));
  }
}

function cancelPending() {
  if (pending) clearTimeout(pending.timer);
  pending = null;
}

function startHold(e, x, y, touch) {
  const row = e.target.closest?.(".row");
  if (!row || !loaded || e.target.closest("button, .chip") || !deck.categories.length) return;
  const entry = deck.cards.find((c) => c.name === row.dataset.card);
  if (!entry) return;
  pending = { row, entry, x, y, touch, timer: touch ? setTimeout(lift, 350) : null };
}

function moveHold(x, y) {
  if (lifted) { moveLifted(x, y); return; }
  if (!pending) return;
  const dist = Math.hypot(x - pending.x, y - pending.y);
  if (pending.touch) { if (dist > 10) cancelPending(); } // the finger is scrolling
  else if (dist > 6) { pending.x = x; pending.y = y; lift(); }
}

function endHold() {
  if (lifted) drop();
  cancelPending();
}

document.addEventListener("mousedown", (e) => { if (e.button === 0) startHold(e, e.clientX, e.clientY, false); });
document.addEventListener("mousemove", (e) => moveHold(e.clientX, e.clientY));
document.addEventListener("mouseup", endHold);
document.addEventListener("dragstart", (e) => { if (e.target.closest?.(".row")) e.preventDefault(); });
document.addEventListener("touchstart", (e) => startHold(e, e.touches[0].clientX, e.touches[0].clientY, true), { passive: true });
document.addEventListener("touchmove", (e) => {
  if (lifted) e.preventDefault();
  moveHold(e.touches[0].clientX, e.touches[0].clientY);
}, { passive: false });
document.addEventListener("touchend", endHold);
document.addEventListener("touchcancel", endHold);
document.addEventListener("contextmenu", (e) => { if (lifted || pending?.touch) e.preventDefault(); });
document.addEventListener("click", (e) => { if (suppressClick) { e.preventDefault(); e.stopPropagation(); } }, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && lifted) { lifted.zone = -1; drop(); } });
addEventListener("resize", () => { if (lifted) paintZones(lifted.zone); });

// The first load uses the deck the server put inside the page; later loads ask the server.
function deckInPage() {
  const block = document.getElementById("deck-data");
  if (!block) return null;
  block.remove();
  try {
    return JSON.parse(block.textContent);
  } catch (e) {
    report("deck inside the page could not be read", { error: String(e) });
    return null;
  }
}

function ready() {
  loaded = true;
  input.disabled = false;
  input.focus();
}

let firstLoad = inPageDeck?.deck === deckName ? inPageDeck : null;

async function load() {
  const inPage = firstLoad;
  firstLoad = null;
  if (inPage) { applyServerDeck(inPage); ready(); return; }
  try {
    const { ok, status, data } = await api(`/api/decks/${deckName}`);
    if (!ok) throw new Error(data.detail || status);
    applyServerDeck(data);
    loaded = true;
    input.disabled = false;
    input.focus();
  } catch (e) {
    report("could not load the deck", { error: String(e?.message || e) });
    loaded = false;
    input.disabled = true;
    $("save-status").textContent = "Could not load the deck - retrying";
    setTimeout(load, 3000);
  }
}

input.disabled = true;

load().then(fillPicker);
