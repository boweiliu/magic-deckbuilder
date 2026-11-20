// Grouped view of a deck: cards and groups of cards are units. Tap units to select them, then
// Group or Ungroup (Ctrl+G / Ctrl+Shift+G on a keyboard). A card in a cycle (the five eggs, the
// ten landscapes, the basic lands) can be added with the rest of its cycle as one group.
// Every change is saved on the server, like on the plain page.

import { el, chip } from "/static/zones.js";
import { api, report } from "/static/api.js";

const LAST_DECK = "deck-list:last-deck";
function rememberedDeck(write) {
  try {
    if (write) localStorage.setItem(LAST_DECK, write);
    return localStorage.getItem(LAST_DECK);
  } catch {
    return null;
  }
}
const named = new URLSearchParams(location.search).get("deck");
const deckName = named || rememberedDeck() || "ulalek";
rememberedDeck(deckName);
if (!named) history.replaceState(null, "", `/groups?deck=${encodeURIComponent(deckName)}`);

const TARGET = 99;
const $ = (id) => document.getElementById(id);
const title = (name) => name.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const input = $("add-input");
const list = $("suggestions");
const message = $("add-message");

let deck = { commander: [], cards: [], categories: [], tags: {}, groups: [] };
let loaded = false;
const selected = new Set(); // "g:<group id>" or "c:<card name>"
const expanded = new Set(); // group ids shown card by card

function showPreview(card) {
  if (!card?.image) return;
  $("preview-img").src = card.image;
  const back = $("preview-back");
  if (card.image_back) { back.src = card.image_back; back.hidden = false; } else { back.hidden = true; }
}

let toastTimer;
function toast(text) {
  $("toast-text").textContent = text;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4000);
}

// ---- Drawing the list ----

const mv = (entry) => (entry.card ? entry.card.mana_value : Infinity);
const tagsOf = (name) => deck.categories.filter((c) => (deck.tags[name] || []).includes(c.id));

function units() {
  const byName = new Map(deck.cards.map((e) => [e.name, e]));
  const grouped = new Set();
  const out = deck.groups.map((g) => {
    const entries = g.cards.map((n) => byName.get(n)).filter(Boolean);
    entries.sort((a, b) => mv(a) - mv(b) || a.name.localeCompare(b.name));
    entries.forEach((e) => grouped.add(e.name));
    return { key: `g:${g.id}`, group: g, entries, mv: Math.min(...entries.map(mv)), name: g.name };
  });
  for (const e of deck.cards) if (!grouped.has(e.name)) out.push({ key: `c:${e.name}`, entries: [e], mv: mv(e), name: e.name });
  return out.sort((a, b) => a.mv - b.mv || a.name.localeCompare(b.name));
}

function cardRow(entry, { selectable }) {
  return el("div", {
    class: "row" + (selectable ? " unit" : "") + (selectable && selected.has(`c:${entry.name}`) ? " selected" : ""),
    "data-key": selectable ? `c:${entry.name}` : null, "data-card": entry.name, onmouseenter: () => showPreview(entry.card),
  },
    el("span", { class: "qty" }, entry.qty > 1 ? `${entry.qty}` : ""),
    el("span", { class: "card-name" }, entry.name, el("span", { class: "chips" }, ...tagsOf(entry.name).map((c) => chip(c)))),
    el("button", { title: "Remove one", "aria-label": `Remove ${entry.name}`, onclick: (e) => { e.stopPropagation(); change("remove", entry.name); } }, "×"),
  );
}

function groupBlock(unit) {
  const { group, entries } = unit;
  const open = expanded.has(group.id);
  const count = entries.reduce((n, e) => n + e.qty, 0);
  return el("div", { class: "unit group" + (selected.has(unit.key) ? " selected" : ""), "data-key": unit.key, "data-group": group.id },
    el("div", { class: "group-head" },
      el("button", { class: "toggle", "aria-label": open ? "Collapse" : "Expand",
        onclick: (e) => { e.stopPropagation(); open ? expanded.delete(group.id) : expanded.add(group.id); render(); } }, open ? "▾" : "▸"),
      el("span", {}, el("span", { class: "name" }, group.name), el("span", { class: "n" }, `${count} cards`)),
      el("button", { class: "rename", onclick: (e) => {
        e.stopPropagation(); renameGroup(group, e.target.closest(".group-head").querySelector(".name"));
      } }, "Rename"),
    ),
    open
      ? el("div", { class: "members" }, ...entries.map((e) => cardRow(e, { selectable: false })))
      : el("div", { class: "summary", onmouseenter: () => showPreview(entries[0]?.card) }, entries.map((e) => e.name).join(", ")),
  );
}

function render() {
  const count = deck.cards.reduce((n, c) => n + c.qty, 0);
  const countEl = $("deck-count");
  countEl.textContent = `${count} / ${TARGET}`;
  countEl.className = "count" + (count === TARGET ? " full" : count > TARGET ? " over" : "");
  $("plain-link").href = `/?deck=${encodeURIComponent(deckName)}`;
  document.title = `${title(deckName)} (groups) - Deck List`;

  const cmd = $("commander");
  cmd.replaceChildren();
  for (const c of deck.commander) {
    cmd.append(
      c.card?.image ? el("img", { src: c.card.image, alt: c.name, onmouseenter: () => showPreview(c.card) }) : null,
      el("div", {}, el("div", { class: "label" }, "Commander"), el("div", { class: "name" }, c.name)),
    );
    if (!$("preview-img").getAttribute("src")) showPreview(c.card);
  }

  const all = units();
  const keys = new Set(all.map((u) => u.key));
  for (const k of [...selected]) if (!keys.has(k)) selected.delete(k);
  $("units").replaceChildren(...all.map((u) => (u.group ? groupBlock(u) : cardRow(u.entries[0], { selectable: true }))));
  $("empty").hidden = deck.cards.length > 0;
  renderActions();
}

// ---- Selecting, grouping, ungrouping ----

function selection() {
  const groups = deck.groups.filter((g) => selected.has(`g:${g.id}`));
  const loose = [...selected].filter((k) => k.startsWith("c:")).map((k) => k.slice(2));
  return { groups, cards: [...groups.flatMap((g) => g.cards), ...loose] };
}

function renderActions() {
  const { groups, cards } = selection();
  $("actions").hidden = selected.size === 0;
  $("actions-count").textContent = `${selected.size} selected`;
  $("do-group").disabled = cards.length < 2 || (groups.length === 1 && selected.size === 1);
  $("do-ungroup").disabled = groups.length === 0;
}

$("units").addEventListener("click", (e) => {
  const unit = e.target.closest(".unit");
  if (!unit || e.target.closest("a")) return;
  const key = unit.dataset.key;
  selected.has(key) ? selected.delete(key) : selected.add(key);
  render();
});

// Grouping is instant, like in a slide editor; the new group can be renamed in place.
function doGroup() {
  const { groups, cards } = selection();
  if (cards.length < 2) return;
  selected.clear();
  save("groups/make", { cards, name: groups.length === 1 ? groups[0].name : "Group" }, `Grouped ${cards.length} cards`);
}

async function doUngroup() {
  const { groups } = selection();
  selected.clear();
  for (const g of groups) await save("groups/ungroup", { id: g.id }, `Ungrouped ${g.name}`);
}

function renameGroup(group, nameEl) {
  const box = el("input", { class: "rename-box", type: "text", maxlength: "40", value: group.name, "aria-label": "Group name",
    onclick: (e) => e.stopPropagation() });
  let finished = false;
  const finish = (keep) => {
    if (finished) return;
    finished = true;
    const name = box.value.trim();
    if (keep && name && name !== group.name) save("groups/rename", { id: group.id, name }, `Renamed to ${name}`);
    else render();
  };
  box.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); finish(true); } else if (e.key === "Escape") finish(false);
  });
  box.addEventListener("blur", () => finish(true));
  nameEl.replaceWith(box);
  box.focus();
  box.select();
}

$("do-group").addEventListener("click", doGroup);
$("do-ungroup").addEventListener("click", doUngroup);
$("do-clear").addEventListener("click", () => { selected.clear(); render(); });
document.addEventListener("keydown", (e) => {
  if (e.target.closest?.("input, form")) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "g") {
    e.preventDefault();
    if (e.shiftKey) doUngroup(); else doGroup();
  } else if (e.key === "Escape" && selected.size) {
    selected.clear(); render();
  }
});

// ---- Saving: one change at a time, in order ----

let saving = Promise.resolve();
function queued(work) {
  $("save-status").textContent = "Saving...";
  const run = saving.then(work);
  saving = run.catch(() => {});
  return run;
}

function applyServerDeck(data) {
  deck = { commander: data.commander, cards: data.cards, categories: data.categories, tags: data.tags, groups: data.groups || [] };
  $("save-status").textContent = data.backed_up ? "Saved, copied to GitHub" : "Saved";
  render();
}

function save(action, body, done) {
  if (!loaded) return Promise.resolve(false);
  return queued(async () => {
    try {
      const { ok, status, data } = await api(`/api/decks/${deckName}/${action}`, { method: "POST", body });
      if (status === 409) { toast(`${data.detail}.`); await load(); return false; }
      if (!ok) throw new Error(data.detail || status);
      applyServerDeck(data);
      if (done) toast(done);
      return true;
    } catch (e) {
      $("save-status").textContent = "Not saved";
      report("save failed", { action, error: String(e?.message || e) });
      toast(`Could not save: ${e?.message || e}. Nothing was lost; try again.`);
      await load();
      return false;
    }
  });
}

const change = (action, name) => save(action, { name });

// ---- Adding cards, one or a whole cycle ----

let suggestions = [];
let active = -1;
let suggestSeq = 0;

function taken(card) {
  if (deck.commander.some((c) => c.name === card.name)) return "your commander";
  if (/\bBasic\b/.test(card.type_line)) return "";
  return deck.cards.some((c) => c.name === card.name) ? "already in the list" : "";
}

function nextPickable(from, dir) {
  for (let n = 1; n <= suggestions.length; n++) {
    const i = (((from + dir * n) % suggestions.length) + suggestions.length) % suggestions.length;
    if (!taken(suggestions[i])) return i;
  }
  return -1;
}

function closeSuggestions() {
  suggestions = [];
  active = -1;
  list.hidden = true;
  list.replaceChildren();
}

function addOne(card) {
  message.textContent = "";
  input.value = "";
  closeSuggestions();
  change("add", card.name);
  input.focus();
}

function renderSuggestions() {
  list.replaceChildren();
  suggestions.forEach((card, i) => {
    const inList = taken(card);
    list.append(el("li", {
      class: (i === active ? "active" : "") + (inList ? " taken" : ""), role: "option", "data-card": card.name,
      "aria-disabled": inList ? "true" : null,
      onmousedown: (e) => { e.preventDefault(); if (!inList) addOne(card); },
      onmouseenter: () => { if (!inList) { active = i; renderActive(); } showPreview(card); },
    },
      el("span", {}, card.name),
      card.cycles ? el("button", { class: "cycle-btn", type: "button",
        onmousedown: (e) => { e.preventDefault(); e.stopPropagation(); openCycle(card); } }, "+ whole cycle") : null,
      el("span", { class: "type" }, card.type_line, inList ? ` - ${inList}` : ""),
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
    if (suggestions[active] && !taken(suggestions[active])) addOne(suggestions[active]);
  } else if (e.key === "Escape") {
    closeSuggestions();
  }
});
input.addEventListener("blur", () => setTimeout(closeSuggestions, 100));

const inDeck = (name) => deck.cards.some((c) => c.name === name);

function updateCycleButton() {
  const picked = [...$("cycle-list").querySelectorAll("input:checked")].map((b) => b.value);
  const fresh = picked.filter((n) => !inDeck(n)).length;
  $("cycle-add").textContent = picked.length >= 2 ? `Add ${fresh} and group ${picked.length}` : `Add ${fresh}`;
  $("cycle-add").disabled = picked.length === 0 || (fresh === 0 && picked.length < 2);
}

async function openCycle(card) {
  closeSuggestions();
  input.value = "";
  let cycles = [];
  try {
    const { ok, data } = await api(`/api/cards/cycles?name=${encodeURIComponent(card.name)}`);
    if (ok) cycles = data.cycles;
  } catch (e) {
    report("could not load cycles", { card: card.name, error: String(e?.message || e) });
  }
  if (!cycles.length) { message.textContent = `No cycle found for ${card.name}.`; return; }
  $("cycle-list").replaceChildren(...cycles.flatMap((cycle, i) => [
    el("h3", {}, cycle.name),
    el("div", { class: "members" }, ...cycle.cards.map((c) => {
      const have = inDeck(c.name);
      const box = el("input", { type: "checkbox", value: c.name, onchange: updateCycleButton });
      // The basic-land cycles include colorless Wastes; most decks want just the five colors.
      box.checked = i === 0 && !/\bWastes$/.test(c.name);
      return el("label", { class: have ? "have" : "", onmouseenter: () => showPreview(c) }, box, c.name, have ? " (in list)" : "");
    })),
  ]));
  $("cycle-name").value = cycles[0].name;
  $("cycle").hidden = false;
  updateCycleButton();
}

$("cycle-cancel").addEventListener("click", () => { $("cycle").hidden = true; });
$("cycle").addEventListener("submit", async (e) => {
  e.preventDefault();
  const picked = [...$("cycle-list").querySelectorAll("input:checked")].map((b) => b.value);
  if (!picked.length) return;
  $("cycle").hidden = true;
  const name = $("cycle-name").value.trim() || "Group";
  await save("add-group", { cards: picked, name }, picked.length >= 2 ? `Added ${name}` : `Added ${picked[0]}`);
});

// ---- Loading ----

async function fillPicker() {
  let saved = [];
  try {
    const { ok, data } = await api("/api/decks");
    if (ok) saved = data.decks;
  } catch {
    // Only this list shows until the next load.
  }
  if (!saved.some((d) => d.name === deckName)) saved.push({ name: deckName });
  const picker = $("deck-pick");
  picker.replaceChildren(...saved.map((d) => el("option", { value: d.name }, title(d.name))));
  picker.value = deckName;
}
$("deck-pick").addEventListener("change", (e) => { location.href = `/groups?deck=${encodeURIComponent(e.target.value)}`; });

async function load() {
  try {
    const { ok, status, data } = await api(`/api/decks/${deckName}`);
    if (!ok) throw new Error(data.detail || status);
    applyServerDeck(data);
    loaded = true;
    input.disabled = false;
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
