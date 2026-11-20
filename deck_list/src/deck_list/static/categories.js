// Categories page: add, rename, recolour, reorder and delete the deck's categories, with a live
// preview of the drag zones they make. Every change is saved on the server with the deck.

import { PALETTE, el, zoneAt, drawZones } from "/static/zones.js";
import { api, report } from "/static/api.js";

const $ = (id) => document.getElementById(id);
const deckName = new URLSearchParams(location.search).get("deck") || "ulalek";
let deck = { cards: [], categories: [], tags: {} };
let openPalette = null; // id of the category whose colour choices are showing
let confirming = null; // id of the category being deleted
let hot = -1;
let busy = false;

const usedBy = (id) => deck.cards.filter((e) => (deck.tags[e.name] || []).includes(id)).length;

async function load() {
  const { data } = await api(`/api/decks/${deckName}`);
  deck = data;
  showSaved(deck);
}

function showSaved(data) {
  $("save-status").textContent = data.backed_up ? "Saved, copied to GitHub." : "Saved.";
}

// One change, applied on the server; the page shows what the server answers.
async function change(action, body, flashId = null) {
  if (busy) return;
  busy = true;
  $("message").textContent = "";
  $("save-status").textContent = "Saving...";
  try {
    const { ok, data } = await api(`/api/decks/${deckName}/categories/${action}`, { method: "POST", body });
    if (!ok) {
      $("message").textContent = `Not saved: ${data.detail}.`;
      await load();
    } else {
      deck = data;
      showSaved(data);
      setTimeout(() => api(`/api/decks/${deckName}`).then(({ data: d }) => showSaved(d)).catch(() => {}), 4000);
    }
  } catch (e) {
    report("save failed", { action, error: String(e?.message || e) });
    $("message").textContent = `Could not save: ${e.message}. Nothing was lost; try again.`;
    await load();
  } finally {
    busy = false;
    render(typeof flashId === "function" ? flashId() : flashId);
  }
}

function render(flashId = null) {
  const categories = deck.categories;
  $("cat-list").replaceChildren(...categories.map((cat, i) => {
    const n = usedBy(cat.id);
    const row = el("li", { class: "cat" + (cat.id === flashId ? " flash" : "") + (i === hot ? " hot" : ""), style: `--c:${cat.color}`, "data-category": cat.id },
      el("span", { class: "num" }, String(i + 1)),
      el("div", { class: "main" },
        el("button", { class: "swatch", title: "Change colour", "aria-label": `Change colour of ${cat.name}`,
          onclick: () => { openPalette = openPalette === cat.id ? null : cat.id; render(); } }),
        el("input", { value: cat.name, maxlength: "24", "aria-label": "Category name",
          onchange: (e) => {
            const name = e.target.value.trim();
            if (name && name !== cat.name) change("edit", { id: cat.id, name }, cat.id); else e.target.value = cat.name;
          } }),
        el("span", { class: "count" }, n === 1 ? "1 card" : `${n} cards`),
      ),
      el("div", { class: "tools" },
        el("button", { class: "tool", title: "Move up", "aria-label": `Move ${cat.name} up`, disabled: i === 0 ? "" : null,
          onclick: () => change("move", { id: cat.id, position: i - 1 }, cat.id) }, "↑"),
        el("button", { class: "tool", title: "Move down", "aria-label": `Move ${cat.name} down`, disabled: i === categories.length - 1 ? "" : null,
          onclick: () => change("move", { id: cat.id, position: i + 1 }, cat.id) }, "↓"),
        el("button", { class: "tool del", title: "Delete", "aria-label": `Delete ${cat.name}`, onclick: () => { confirming = cat.id; render(); } }, "×"),
      ),
    );
    if (openPalette === cat.id) {
      row.append(el("div", { class: "palette" }, ...PALETTE.map((color) => el("button", {
        class: "swatch" + (color === cat.color ? " on" : ""), style: `--c:${color}`, "aria-label": `Use colour ${color}`,
        onclick: () => { openPalette = null; if (color !== cat.color) change("edit", { id: cat.id, color }, cat.id); else render(); },
      }))));
    }
    if (confirming === cat.id) {
      row.append(el("div", { class: "confirm" },
        el("span", {}, n ? `Delete ${cat.name}? ${n === 1 ? "1 card loses" : `${n} cards lose`} this tag.` : `Delete ${cat.name}?`),
        el("button", { class: "yes", onclick: () => { confirming = null; change("delete", { id: cat.id }); } }, "Delete"),
        el("button", { class: "no", onclick: () => { confirming = null; render(); } }, "Keep"),
      ));
    }
    return row;
  }));
  drawPreview();
}

function drawPreview() {
  const screen = $("screen");
  drawZones($("zone-svg"), $("zone-labels"), deck.categories, screen.clientWidth, screen.clientHeight, { hot });
  $("preview-hint").textContent = deck.categories.length
    ? (hot >= 0 ? `A card dropped here gets ${deck.categories[hot].name}.` : "Point at the screen to see which zone a card would land in.")
    : "Add a category to get zones.";
}

function setDevice(device) {
  $("screen").className = device;
  for (const b of $("device").children) b.setAttribute("aria-checked", String(b.dataset.device === device));
  localStorage.setItem("deck-list-preview-device", device);
  drawPreview();
}
$("device").addEventListener("click", (e) => { if (e.target.dataset.device) setDevice(e.target.dataset.device); });

function pointAt(e) {
  const r = $("screen").getBoundingClientRect();
  const z = zoneAt(e.clientX - r.left, e.clientY - r.top, r.width, r.height, deck.categories.length);
  if (z !== hot) { hot = z; render(); }
}
$("screen").addEventListener("pointermove", pointAt);
$("screen").addEventListener("pointerdown", pointAt);
$("screen").addEventListener("pointerleave", () => { if (hot !== -1) { hot = -1; render(); } });

$("add-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("add-name").value.trim();
  if (!name) return;
  $("add-name").value = "";
  change("add", { name }, () => deck.categories.at(-1)?.id);
});
addEventListener("resize", drawPreview);

$("back").href = `/?deck=${encodeURIComponent(deckName)}`;
document.title = `Categories - ${deckName} - Deck List`;
setDevice(localStorage.getItem("deck-list-preview-device") || "phone");
await load();
render();
