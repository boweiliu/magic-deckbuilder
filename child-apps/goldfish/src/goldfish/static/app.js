// Goldfish: play a deck alone. The whole game lives in `game` (plain JSON) and is
// saved to the server after every move, so a reload or another device picks it up.
"use strict";

const ZONES = ["library", "hand", "battlefield", "graveyard", "exile", "command"];
const ZONE_LABEL = {
  library: "Library", hand: "Hand", battlefield: "Battlefield", graveyard: "Graveyard", exile: "Exile",
  command: "Command zone",
};
const NARROW = window.matchMedia("(max-width: 699px)");

let game = null;          // the saved game
let cards = {};           // card name -> card details from the server
let history = [];         // earlier games, for Undo
let selected = null;      // instance id of the card the preview shows with its moves
let hovered = null;       // instance id the mouse last rested on (laptop only)
let saveTimer = null;
let lastTap = { id: null, at: 0 };
const openZones = new Set();

const $ = (id) => document.getElementById(id);

// ---- settings kept on this device (phone and laptop each keep their own) ----

function layoutKey() { return NARROW.matches ? "narrow" : "wide"; }

function settings() {
  const defaults = NARROW.matches
    ? { preview: "text", scale: 1, board: "text" }
    : { preview: "image", scale: 1, board: "image" };
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem("goldfish.settings." + layoutKey()) || "{}") };
  } catch {
    return defaults;
  }
}

function saveSettings(change) {
  localStorage.setItem("goldfish.settings." + layoutKey(), JSON.stringify({ ...settings(), ...change }));
}

// ---- game state ----

function shuffle(list) {
  const out = list.slice();
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

function newGame(deck, deckData) {
  const names = {};
  let next = 1;
  const library = [];
  const command = [];
  for (const name of deckData.commander) { names[next] = name; command.push(next++); }
  for (const name of deckData.cards) { names[next] = name; library.push(next++); }
  const g = {
    v: 1, deck, rev: 0, turn: 0, mulligans: 0, toBottom: 0, names,
    commanders: command.slice(), casts: 0, tapped: [],
    zones: { library: shuffle(library), hand: [], battlefield: [], graveyard: [], exile: [], command },
  };
  g.zones.hand = g.zones.library.splice(0, 7);
  return g;
}

function zoneOf(id) {
  return ZONES.find((z) => game.zones[z].includes(id));
}

function nameOf(id) { return game.names[id]; }
function cardOf(id) { return cards[nameOf(id)]; }
function isLand(id) { return /\bLand\b/.test((cardOf(id)?.type_line || "").split(" // ")[0]); }

// Every move goes through here: remember the game before it (for Undo), change it, save, redraw.
function act(change) {
  history.push(JSON.stringify(game));
  if (history.length > 300) history.shift();
  change();
  game.rev += 1;
  scheduleSave();
  render();
}

function moveCard(id, to, where = "top") {
  const from = zoneOf(id);
  game.zones[from] = game.zones[from].filter((x) => x !== id);
  game.tapped = game.tapped.filter((x) => x !== id);
  if (where === "bottom") game.zones[to].push(id);
  else if (to === "library") game.zones[to].unshift(id);
  else game.zones[to].push(id);
  if (from === "command" && to === "battlefield") game.casts += 1;
}

function draw(n = 1) {
  game.zones.hand.push(...game.zones.library.splice(0, n));
}

function toggleTap(id) {
  if (game.tapped.includes(id)) game.tapped = game.tapped.filter((x) => x !== id);
  else game.tapped.push(id);
}

// ---- saving and loading ----

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 300);
}

// A failed save is retried (1 s, 2 s, 4 s, ... up to 30 s apart); the game stays on this page meanwhile,
// so a moment of the app restarting loses nothing. Every failure goes to the debug log.
let saveFailures = 0;

async function saveNow() {
  saveTimer = null;
  let problem;
  try {
    const r = await fetch("/api/game", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ game }),
    });
    if (r.ok) {
      if (saveFailures) debugLog("save-recovered", `saved after ${saveFailures} failed tries`);
      saveFailures = 0;
      showError(null);
      return;
    }
    problem = `the app answered ${r.status} ${r.statusText}`;
  } catch (e) {
    problem = `no answer from the app (${e.message})`;
  }
  saveFailures += 1;
  debugLog("save-failed", problem, { attempt: saveFailures });
  if (saveFailures >= 2) showError(`Not saved yet: ${problem}. Retrying; your game is safe on this page.`);
  saveTimer = setTimeout(saveNow, Math.min(30000, 1000 * 2 ** (saveFailures - 1)));
}

// ---- debug log: page errors and failed requests go to the app's log (data/.apps/goldfish/client-log.jsonl) ----

const debugQueue = [];
let debugTimer = null;

function debugLog(kind, message, detail = {}) {
  const where = game ? { turn: game.turn, rev: game.rev, zones: Object.fromEntries(ZONES.map((z) => [z, game.zones[z].length])) } : {};
  debugQueue.push({ at: new Date().toISOString(), kind, message: String(message).slice(0, 2000), detail: { ...where, ...detail } });
  if (debugQueue.length > 200) debugQueue.shift();
  clearTimeout(debugTimer);
  debugTimer = setTimeout(flushDebugLog, 500);
}

async function flushDebugLog() {
  if (!debugQueue.length) return;
  const entries = debugQueue.splice(0);
  try {
    const r = await fetch("/api/client-log", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        entries, page: location.href, user_agent: navigator.userAgent,
        viewport: `${innerWidth}x${innerHeight}@${devicePixelRatio}`,
      }),
    });
    if (!r.ok) throw new Error(String(r.status));
  } catch {
    debugQueue.unshift(...entries.slice(-100));  // kept for the next try
    debugTimer = setTimeout(flushDebugLog, 5000);
  }
}

window.addEventListener("error", (e) => {
  if (e.target && e.target.tagName === "IMG") return;  // card pictures that fail fall back to text
  debugLog("page-error", e.message, { source: `${e.filename}:${e.lineno}:${e.colno}`, stack: e.error?.stack });
});
window.addEventListener("unhandledrejection", (e) => {
  debugLog("page-error", e.reason?.message || String(e.reason), { stack: e.reason?.stack });
});

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) {
    debugLog("request-failed", `${url} answered ${r.status} ${r.statusText}`);
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `${url} answered ${r.status}`);
  }
  return r.json();
}

async function loadDeck(deck) {
  const data = await getJSON("/api/decks/" + encodeURIComponent(deck));
  cards = { ...cards, ...data.card_data };
  return data;
}

// A saved game can hold cards its deck list has since dropped: look those up by name.
async function loadGameCards(g) {
  const missing = [...new Set(Object.values(g.names))].filter((n) => !cards[n]);
  if (!missing.length) return;
  const r = await fetch("/api/cards", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ names: missing }),
  });
  if (!r.ok) throw new Error(`/api/cards answered ${r.status}`);
  cards = { ...cards, ...(await r.json()).card_data };
}

// ---- the deck list changed in the Deck List app since this game started ----

let listDiff = null;       // { data, added, removed, key } when the saved list differs from this game's cards
let dismissedList = null;  // the list "Not now" was pressed on; the notice comes back when it changes again

function countNames(names) {
  const counts = new Map();
  for (const n of names) counts.set(n, (counts.get(n) || 0) + 1);
  return counts;
}

function minus(a, b) {
  const out = [];
  for (const [name, n] of a) for (let i = (b.get(name) || 0); i < n; i++) out.push(name);
  return out;
}

async function checkList() {
  if (!game) return;
  let data;
  try {
    data = await getJSON("/api/decks/" + encodeURIComponent(game.deck));
  } catch {
    return;
  }
  const inGame = countNames(Object.values(game.names));
  const inList = countNames(data.commander.concat(data.cards));
  const added = minus(inList, inGame), removed = minus(inGame, inList);
  const key = JSON.stringify([...inList].sort());
  listDiff = (added.length || removed.length) && key !== dismissedList ? { data, added, removed, key } : null;
  renderListNotice();
}

function cardCount(names) {
  const shown = names.slice(0, 3).join(", ");
  return `${names.length} ${names.length === 1 ? "card" : "cards"} (${shown}${names.length > 3 ? ", ..." : ""})`;
}

function renderListNotice() {
  const notice = $("list-changed");
  if (!listDiff) { notice.hidden = true; return; }
  const parts = [];
  if (listDiff.added.length) parts.push(`added ${cardCount(listDiff.added)}`);
  if (listDiff.removed.length) parts.push(`removed ${cardCount(listDiff.removed)}`);
  notice.innerHTML = `<span>Your deck list changed since this game started: ${escapeHTML(parts.join("; "))}.</span>
    <button type="button" class="primary" id="btn-use-list">Start over with the new list</button>
    <button type="button" id="btn-list-later">Not now</button>`;
  notice.hidden = false;
}

function startOver(data, deck = game.deck) {
  cards = { ...cards, ...data.card_data };
  const rev = game.rev;
  history = [];
  selected = hovered = null;
  game = newGame(deck, data);
  game.rev = rev + 1;
  listDiff = null;
  renderListNotice();
  scheduleSave();
  render();
}

async function loadSymbols() {
  try {
    symbolImages = (await getJSON("/api/symbols")).symbols;
    if (game) render();
  } catch (e) {
    debugLog("symbols-failed", e.message);
  }
}

async function start() {
  loadSymbols();
  loadDeckNames();
  const wanted = new URLSearchParams(location.search).get("deck");
  const saved = (await getJSON("/api/game")).game;
  if (saved && saved.v === 1 && (!wanted || wanted === saved.deck)) {
    await loadDeck(saved.deck);
    await loadGameCards(saved);
    game = saved;
  } else {
    const deck = wanted || "ulalek";
    game = newGame(deck, await loadDeck(deck));
    scheduleSave();
  }
  render();
  checkList();
}

// ---- picking which of the Deck List app's lists to play ----

const deckTitle = (name) => name.replace(/-/g, " ");
let deckNames = [];

function renderDeckPicker() {
  const picker = $("deck-name");
  const names = deckNames.includes(game.deck) ? deckNames : [game.deck, ...deckNames];
  if ([...picker.options].map((o) => o.value).join() !== names.join()) {
    picker.replaceChildren(...names.map((n) => {
      const option = document.createElement("option");
      option.value = n;
      option.textContent = deckTitle(n);
      return option;
    }));
  }
  picker.value = game.deck;
}

async function loadDeckNames() {
  try {
    deckNames = (await getJSON("/api/decks")).decks;
  } catch (e) {
    debugLog("deck-names-failed", e.message);
  }
  if (game) renderDeckPicker();
}

async function switchDeck(deck) {
  if (deck === game.deck) return;
  if (game.turn > 0 && !confirm(`Start a new game with ${deckTitle(deck)}? This one is thrown away.`)) {
    $("deck-name").value = game.deck;
    return;
  }
  try {
    startOver(await loadDeck(deck), deck);
    window.history.replaceState(null, "", "/?deck=" + encodeURIComponent(deck));
  } catch (e) {
    showError(`Could not load ${deckTitle(deck)}: ${e.message}`);
    $("deck-name").value = game.deck;
  }
}

// A game moved on from another device (the phone while the laptop sat idle): take it.
async function catchUp() {
  checkList();
  if (!game || saveTimer) return;
  try {
    const saved = (await getJSON("/api/game")).game;
    if (saved && saved.deck === game.deck && saved.rev > game.rev) {
      await loadGameCards(saved);
      history.push(JSON.stringify(game));
      game = saved;
      render();
    }
  } catch {
    // Offline for a moment; the next move saves as usual.
  }
}

function showError(text) {
  $("error").hidden = !text;
  $("error").textContent = text || "";
}

// ---- drawing mana costs and rules text ----

function escapeHTML(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

// Scryfall's symbol images ({T}, {W/U}, ...), loaded at start; until then, or without them, symbols are drawn as text.
let symbolImages = {};

function symbols(text) {
  return escapeHTML(text).replace(/\{([^}]+)\}/g, (_, sym) => {
    const src = symbolImages[`{${sym}}`];
    if (src) return `<img class="sym" src="${src}" alt="{${sym}}" data-sym="${sym}">`;
    const colors = sym.replace(/[^WUBRG]/g, "");
    let cls = colors.length === 1 ? "pip-" + colors : colors.length > 1 ? "pip-multi" : sym === "C" ? "pip-C" : "pip-generic";
    if (sym.includes("/")) cls += " pip-hybrid";
    return `<span class="pip ${cls}">${sym}</span>`;
  });
}

function rulesText(text) {
  return text.split("\n").map((line) =>
    `<p>${symbols(line).replace(/\(([^)]*)\)/g, '<i class="reminder">($1)</i>')}</p>`).join("");
}

function stats(face) {
  if (face.power != null) return `${face.power}/${face.toughness}`;
  if (face.loyalty != null) return `Loyalty ${face.loyalty}`;
  if (face.defense != null) return `Defense ${face.defense}`;
  return "";
}

// ---- the board ----

function tile(id) {
  const card = cardOf(id);
  const face = card.faces[0];
  const el = document.createElement("button");
  el.type = "button";
  el.className = "tile";
  el.dataset.id = id;
  el.dataset.card = card.name;
  if (game.tapped.includes(id)) el.classList.add("tapped");
  if (id === selected) el.classList.add("selected");
  const colors = card.colors.length ? card.colors.join("") : "C";
  el.dataset.colors = colors.length > 1 ? "multi" : colors;
  const st = stats(face);
  el.innerHTML = `
    <span class="t-top"><span class="t-name">${escapeHTML(face.name)}</span><span class="t-cost">${symbols(face.mana_cost)}</span></span>
    <span class="t-bottom"><span class="t-type">${escapeHTML(face.type_line.replace(/^Legendary /, ""))}</span>${st ? `<span class="t-stats">${st}</span>` : ""}</span>`;
  // Pictures on the board: the picture covers the text, which shows again if the picture can't load.
  if (settings().board === "image" && face.image) {
    el.classList.add("pic");
    el.insertAdjacentHTML("beforeend", `<img src="${face.image}" alt="" draggable="false">`);
  }
  if (game.commanders.includes(id) && zoneOf(id) === "command" && game.casts) {
    el.insertAdjacentHTML("beforeend", `<span class="t-badge">tax +${2 * game.casts}</span>`);
  }
  return el;
}

function fill(containerId, ids) {
  const box = $(containerId);
  box.replaceChildren(...ids.map(tile));
  box.classList.toggle("empty", ids.length === 0);
}

function render() {
  const z = game.zones;
  renderDeckPicker();
  $("turn").textContent = game.turn === 0 ? "Opening hand" : `Turn ${game.turn}`;
  fill("cards-battlefield-other", z.battlefield.filter((id) => !isLand(id)));
  fill("cards-battlefield-lands", z.battlefield.filter(isLand));
  fill("cards-hand", z.hand);
  fill("cards-command", z.command);
  fill("cards-graveyard", openZones.has("graveyard") ? z.graveyard : []);
  fill("cards-exile", openZones.has("exile") ? z.exile : []);
  for (const zone of ["library", "hand", "graveyard", "exile"]) $("count-" + zone).textContent = z[zone].length;
  $("zone-graveyard").classList.toggle("open", openZones.has("graveyard"));
  $("zone-exile").classList.toggle("open", openZones.has("exile"));
  $("btn-undo").disabled = history.length === 0;
  const pictures = settings().board === "image";
  $("btn-board-view").textContent = NARROW.matches ? (pictures ? "Pics" : "Text") : (pictures ? "Board: pictures" : "Board: text");
  for (const b of ["btn-next", "btn-draw", "btn-untap"]) $(b).disabled = game.turn === 0;
  renderBanner();
  if (selected != null && !zoneOf(selected)) selected = null;
  renderPreview();
}

function renderBanner() {
  const banner = $("banner");
  if (game.turn === 0) {
    const free = game.mulligans === 0 ? "" : game.mulligans === 1
      ? " Your first mulligan is free." : ` You'll put ${game.mulligans - 1} on the bottom.`;
    banner.innerHTML = `<span>Opening hand${game.mulligans ? ` after ${game.mulligans} mulligan${game.mulligans > 1 ? "s" : ""}` : ""}.${free}</span>
      <button type="button" id="btn-keep" class="primary">Keep <kbd>K</kbd></button>
      <button type="button" id="btn-mull">Mulligan <kbd>M</kbd></button>`;
    banner.hidden = false;
  } else if (game.toBottom > 0) {
    banner.innerHTML = `<span>Put ${game.toBottom} card${game.toBottom > 1 ? "s" : ""} from your hand on the bottom: tap a card, then "Bottom of library" (or hover it and press Shift L), or drag it onto the library.</span>`;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

// ---- the preview (readable card, resizable on its own) ----

function previewId() { return selected ?? hovered; }

// --hand-h: how much of the window, from the bottom, the hand takes up.
function placeAboveHand() {
  const top = $("zone-hand").getBoundingClientRect().top;
  document.documentElement.style.setProperty("--hand-h", Math.max(0, Math.round(window.innerHeight - top)) + "px");
}

function renderPreview() {
  placeAboveHand();
  const id = previewId();
  const panel = $("preview");
  if (id == null || !zoneOf(id)) { panel.hidden = true; document.body.classList.remove("sheet-open"); return; }
  const card = cardOf(id);
  const s = settings();
  panel.hidden = false;
  document.body.classList.toggle("sheet-open", selected != null);
  panel.style.setProperty("--scale", s.scale);
  $("pv-mode").textContent = s.preview === "image" ? "Show text" : "Show picture";
  const source = $("pv-source");
  source.hidden = !card.scryfall_uri;
  if (card.scryfall_uri) source.href = card.scryfall_uri;
  const body = $("preview-body");
  if (s.preview === "image" && card.faces.some((f) => f.image)) {
    const images = [...new Set(card.faces.map((f) => f.image).filter(Boolean))];
    body.className = "preview-body as-image";
    body.innerHTML = images.map((src) => `<img src="${src}" alt="${escapeHTML(card.name)}">`).join("");
  } else {
    body.className = "preview-body as-text";
    body.innerHTML = card.faces.map((f) => `
      <div class="face">
        <div class="f-head"><span class="f-name">${escapeHTML(f.name)}</span><span class="f-cost">${symbols(f.mana_cost)}</span></div>
        <div class="f-type">${escapeHTML(f.type_line)}</div>
        <div class="f-text">${rulesText(f.oracle_text)}</div>
        ${stats(f) ? `<div class="f-stats">${stats(f)}</div>` : ""}
      </div>`).join("");
  }
  renderActions(id);
}

// Moving a card anywhere goes through here, so a drop, a key and a button all do the same thing.
function moveTo(id, zone, where = "top") {
  const from = zoneOf(id);
  if (from === zone && zone !== "library") return;
  act(() => {
    moveCard(id, zone, where);
    if (from === "hand" && zone === "library" && where === "bottom" && game.toBottom > 0) game.toBottom -= 1;
  });
}

// The moves a card has where it is now. `key` is its keyboard shortcut (shift: the key with Shift held).
function cardMoves(id) {
  const zone = zoneOf(id);
  const moves = [];
  const add = (key, label, fn, primary = false, shift = false) => moves.push({ key, shift, label, fn, primary });
  const to = (zoneName, where) => () => moveTo(id, zoneName, where);
  const bottomFirst = zone === "hand" && game.toBottom > 0;
  if (bottomFirst) add("L", "Bottom of library", to("library", "bottom"), true, true);
  if (zone === "hand") add("B", "Play", to("battlefield"), !bottomFirst);
  if (zone === "command") add("B", "Cast", to("battlefield"), true);
  if (zone === "battlefield") add("T", game.tapped.includes(id) ? "Untap" : "Tap", () => act(() => toggleTap(id)), true);
  if (["graveyard", "exile"].includes(zone)) add("B", "Battlefield", to("battlefield"));
  if (game.commanders.includes(id) && zone !== "command") add("C", "Command zone", to("command"));
  if (zone !== "hand" && zone !== "command") add("H", "Hand", to("hand"));
  if (zone !== "graveyard" && zone !== "command") add("G", "Graveyard", to("graveyard"));
  if (zone !== "exile" && zone !== "command") add("E", "Exile", to("exile"));
  if (zone !== "command") {
    add("L", "Top of library", to("library"));
    if (!bottomFirst) add("L", "Bottom of library", to("library", "bottom"), false, true);
  }
  return moves;
}

function keyLabel(m) { return (m.shift ? "Shift " : "") + m.key; }

function renderActions(id) {
  const box = $("preview-actions");
  if (id !== selected) { box.replaceChildren(); box.hidden = true; return; }
  box.hidden = false;
  const zone = zoneOf(id);
  box.innerHTML = `<div class="where">In ${ZONE_LABEL[zone].toLowerCase()}</div>`;
  for (const m of cardMoves(id)) {
    const b = document.createElement("button");
    b.type = "button";
    b.innerHTML = `${escapeHTML(m.label)} <kbd>${keyLabel(m)}</kbd>`;
    if (m.primary) b.className = "primary";
    b.addEventListener("click", () => afterMove(id, zone, m.fn));
    box.appendChild(b);
  }
}

// Run a move on a card, then let go of it unless it stayed on the battlefield (tapping keeps it picked).
function afterMove(id, zone, fn) {
  fn();
  if (zoneOf(id) !== "battlefield" || zone !== "battlefield") {
    if (selected === id) selected = null;
    if (hovered === id) hovered = null;
    render();
  }
}

function setScale(scale) {
  saveSettings({ scale: Math.max(0.6, Math.min(2.4, Math.round(scale * 100) / 100)) });
  renderPreview();
}

// ---- wiring ----

function wire() {
  document.body.addEventListener("click", (e) => {
    const t = e.target.closest(".tile");
    if (t && !e.target.closest("#preview")) {
      const id = Number(t.dataset.id);
      // A second tap on the same battlefield card right away taps or untaps it (double-click on a laptop).
      const now = Date.now();
      if (lastTap.id === id && now - lastTap.at < 400 && zoneOf(id) === "battlefield") {
        lastTap = { id: null, at: 0 };
        selected = id;
        act(() => toggleTap(id));
        return;
      }
      lastTap = { id, at: now };
      selected = selected === id ? null : id;
      render();
      return;
    }
    const toggle = e.target.closest(".zone-toggle");
    if (toggle) {
      const z = toggle.dataset.zone;
      if (openZones.has(z)) openZones.delete(z); else openZones.add(z);
      render();
      return;
    }
    // A tap on empty board closes the preview.
    if (e.target.closest("#board") && (selected != null || hovered != null)) {
      selected = hovered = null;
      render();
    }
  });
  document.body.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse" || drag?.live) return;
    const t = e.target.closest(".tile");
    if (t && Number(t.dataset.id) !== hovered) { hovered = Number(t.dataset.id); renderPreview(); }
  });

  $("btn-next").addEventListener("click", () => act(() => { game.tapped = []; game.turn += 1; draw(); }));
  $("btn-draw").addEventListener("click", () => act(() => draw()));
  $("btn-untap").addEventListener("click", () => act(() => { game.tapped = []; }));
  $("btn-undo").addEventListener("click", () => {
    if (!history.length) return;
    const rev = game.rev;
    game = JSON.parse(history.pop());
    game.rev = rev + 1;
    scheduleSave();
    render();
  });
  $("deck-name").addEventListener("change", (e) => switchDeck(e.target.value));
  $("btn-new").addEventListener("click", async () => {
    if (game.turn > 0 && !confirm("Start a new game? This one is thrown away.")) return;
    startOver(await loadDeck(game.deck));
  });
  $("list-changed").addEventListener("click", (e) => {
    if (e.target.closest("#btn-use-list")) {
      if (game.turn > 0 && !confirm("Start a new game with the new list? This one is thrown away.")) return;
      startOver(listDiff.data);
    } else if (e.target.closest("#btn-list-later")) {
      dismissedList = listDiff.key;
      listDiff = null;
      renderListNotice();
    }
  });
  $("banner").addEventListener("click", (e) => {
    if (e.target.closest("#btn-keep")) {
      act(() => { game.toBottom = Math.max(0, game.mulligans - 1); game.turn = 1; draw(); });
    } else if (e.target.closest("#btn-mull")) {
      act(() => {
        game.mulligans += 1;
        game.zones.library = shuffle(game.zones.library.concat(game.zones.hand));
        game.zones.hand = [];
        draw(7);
      });
    }
  });
  $("btn-board-view").addEventListener("click", () => {
    saveSettings({ board: settings().board === "image" ? "text" : "image" });
    render();
  });

  $("pv-mode").addEventListener("click", () => {
    saveSettings({ preview: settings().preview === "image" ? "text" : "image" });
    renderPreview();
  });
  $("pv-smaller").addEventListener("click", () => setScale(settings().scale - 0.1));
  $("pv-bigger").addEventListener("click", () => setScale(settings().scale + 0.1));
  $("pv-close").addEventListener("click", () => { selected = hovered = null; render(); });

  // Dragging the preview's left edge resizes it (laptop); the board underneath does not move.
  const handle = $("preview-resize");
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    const startX = e.clientX;
    const startScale = settings().scale;
    const width = $("preview").getBoundingClientRect().width;
    const onMove = (m) => setScale(startScale * (width + (startX - m.clientX)) / width);
    const onUp = () => { handle.removeEventListener("pointermove", onMove); handle.removeEventListener("pointerup", onUp); };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  });

  document.addEventListener("error", (e) => {
    if (e.target.tagName === "IMG" && e.target.classList.contains("sym")) {
      e.target.outerHTML = `<span class="pip pip-generic">${escapeHTML(e.target.dataset.sym)}</span>`;
      return;
    }
    if (e.target.tagName === "IMG" && e.target.closest(".tile")) {
      e.target.closest(".tile").classList.remove("pic");
      e.target.remove();
    }
  }, true);
  // On a phone the preview docks above the hand; keep it told where the hand starts.
  new ResizeObserver(placeAboveHand).observe($("zone-hand"));
  window.addEventListener("resize", placeAboveHand);
  window.addEventListener("scroll", placeAboveHand, { passive: true });
  wireKeys();
  wireDrag();
  NARROW.addEventListener("change", render);
  window.addEventListener("focus", catchUp);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") catchUp(); });
}

// ---- keyboard: game keys anywhere, card keys act on the card under the mouse (or the picked one) ----

const GAME_KEYS = {
  d: "btn-draw", n: "btn-next", u: "btn-untap", z: "btn-undo", k: "btn-keep", m: "btn-mull",
};

function wireKeys() {
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;
    const key = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && key === "z") { e.preventDefault(); $("btn-undo").click(); return; }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape") { $("keys").hidden = true; selected = hovered = null; render(); return; }
    if (e.key === "?") { $("keys").hidden = !$("keys").hidden; return; }
    const id = previewId();
    if (id != null && zoneOf(id)) {
      const move = cardMoves(id).find((m) => m.key.toLowerCase() === key && m.shift === e.shiftKey);
      if (move) { e.preventDefault(); afterMove(id, zoneOf(id), move.fn); return; }
    }
    const button = GAME_KEYS[key] && document.getElementById(GAME_KEYS[key]);
    if (button && !e.shiftKey && !button.disabled) { e.preventDefault(); button.click(); }
  });
  $("btn-keys").addEventListener("click", () => { $("keys").hidden = !$("keys").hidden; });
  $("keys").addEventListener("click", (e) => { if (e.target.closest(".keys-close")) $("keys").hidden = true; });
}

// ---- drag and drop: a mouse drags at once; a finger holds a card for a moment first, so the hand still scrolls ----

const HOLD_MS = 280;
let drag = null;          // the drag in progress: { id, pointerId, ghost, startX, startY, live, timer }
let swallowClick = false;  // the click a browser sends after a drop is not a tap

function dropZoneAt(x, y) {
  const el = document.elementFromPoint(x, y);
  return el ? el.closest("[data-zone]") : null;
}

function startDragging(e) {
  drag.live = true;
  const tileEl = document.querySelector(`.tile[data-id="${drag.id}"]`);
  const ghost = tileEl.cloneNode(true);
  ghost.classList.add("ghost");
  ghost.classList.remove("selected");
  const r = tileEl.getBoundingClientRect();
  ghost.style.width = r.width + "px";
  drag.offX = drag.startX - r.left;
  drag.offY = drag.startY - r.top;
  document.body.appendChild(ghost);
  drag.ghost = ghost;
  tileEl.classList.add("dragging");
  document.body.classList.add("is-dragging");
  moveGhost(e.clientX, e.clientY);
}

function moveGhost(x, y) {
  drag.ghost.style.transform = `translate(${x - drag.offX}px, ${y - drag.offY}px)`;
  document.querySelectorAll(".drop-target").forEach((z) => z.classList.remove("drop-target"));
  const zone = dropZoneAt(x, y);
  if (zone) zone.classList.add("drop-target");
}

function endDrag(e, drop) {
  clearTimeout(drag.timer);
  if (drag.live) {
    drag.ghost.remove();
    document.body.classList.remove("is-dragging");
    document.querySelectorAll(".drop-target, .dragging").forEach((z) => z.classList.remove("drop-target", "dragging"));
    swallowClick = true;
    setTimeout(() => { swallowClick = false; }, 0);
    const zone = drop ? dropZoneAt(e.clientX, e.clientY) : null;
    const id = drag.id;
    drag = null;
    if (zone) {
      const target = zone.dataset.zone;
      const from = zoneOf(id);
      // Dropped on the library: on top, unless it is a mulligan card going to the bottom.
      const where = target === "library" && from === "hand" && game.toBottom > 0 ? "bottom" : "top";
      afterMove(id, from, () => moveTo(id, target, where));
    } else {
      render();
    }
    return;
  }
  drag = null;
}

function wireDrag() {
  document.addEventListener("pointerdown", (e) => {
    const t = e.target.closest(".tile");
    if (!t || e.target.closest("#preview") || e.button !== 0 || drag) return;
    drag = { id: Number(t.dataset.id), pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, live: false, touch: e.pointerType !== "mouse" };
    if (drag.touch) drag.timer = setTimeout(() => { if (drag && !drag.live) { startDragging(drag.lastEvent || e); navigator.vibrate?.(15); } }, HOLD_MS);
  });
  document.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    drag.lastEvent = e;
    if (drag.live) { moveGhost(e.clientX, e.clientY); return; }
    const moved = Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY);
    if (drag.touch) { if (moved > 10) { clearTimeout(drag.timer); drag = null; } return; }  // a finger moving early is a scroll
    if (moved > 6) startDragging(e);
  });
  document.addEventListener("pointerup", (e) => { if (drag && e.pointerId === drag.pointerId) endDrag(e, true); });
  document.addEventListener("pointercancel", (e) => { if (drag && e.pointerId === drag.pointerId) endDrag(e, false); });
  // While a finger drags, the page must not scroll under it.
  document.addEventListener("touchmove", (e) => { if (drag && drag.live) e.preventDefault(); }, { passive: false });
  document.addEventListener("contextmenu", (e) => { if (drag || e.target.closest(".tile.dragging")) e.preventDefault(); }, true);
  document.addEventListener("click", (e) => { if (swallowClick) { e.stopPropagation(); e.preventDefault(); } }, true);
}

wire();
debugLog("page-opened", "page opened");
start().catch((e) => { debugLog("load-failed", e.message, { stack: e.stack }); showError("Couldn't load the game: " + e.message); });
