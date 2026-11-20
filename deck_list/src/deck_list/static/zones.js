// Shared by the deck page and the categories page: chips, the colour palette, and the drag-zone
// layout. Categories and tags themselves are saved on the server with the deck.

export const PALETTE = [
  "#2f6b3a", "#2c5a86", "#8a6a12", "#5b3a7a", "#a33b2a", "#6b5a48",
  "#1f6b6b", "#7a2f55", "#3d4f9a", "#b0601e", "#4f6b2a", "#555555",
];

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

const SVG = "http://www.w3.org/2000/svg";
export function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// A chip with an optional x. Pressing the x turns the chip into "Remove <name>? Remove / Keep";
// it goes back to a plain chip after a few seconds or on Keep.
export function chip(cat, { onRemove = null } = {}) {
  const node = el("span", { class: "chip", style: `--c:${cat.color}`, "data-category": cat.id }, el("span", { class: "label" }, cat.name));
  if (!onRemove) return node;
  const plain = () => node.replaceChildren(el("span", { class: "label" }, cat.name), x);
  const x = el("button", { class: "x", title: `Remove ${cat.name}`, "aria-label": `Remove ${cat.name}`, onclick: (e) => {
    e.preventDefault(); e.stopPropagation();
    node.classList.add("confirming");
    node.replaceChildren(
      el("span", { class: "label" }, `Remove ${cat.name}?`),
      el("button", { class: "yes", onclick: (ev) => { ev.stopPropagation(); clearTimeout(timer); onRemove(); } }, "Remove"),
      el("button", { class: "no", onclick: (ev) => { ev.stopPropagation(); clearTimeout(timer); node.classList.remove("confirming"); plain(); } }, "Keep"),
    );
    const timer = setTimeout(() => { node.classList.remove("confirming"); plain(); }, 5000);
  } }, "×");
  plain();
  return node;
}

// ---- Zone layout ----
// The screen edge is split into equal stretches, one per category, clockwise, the first one
// centred on the top edge. Each zone is the wedge from the middle of the screen out to its
// stretch of edge, so a card is tagged by the direction it is dragged in. The middle stays
// neutral (dropping there cancels).

export const CENTER_W = 0.34;
export const CENTER_H = 0.24;

function perimeterPoint(s, w, h) {
  // s runs clockwise from the top-left corner.
  const p = 2 * (w + h);
  s = ((s % p) + p) % p;
  if (s < w) return [s, 0];
  if (s < w + h) return [w, s - w];
  if (s < 2 * w + h) return [w - (s - w - h), h];
  return [0, h - (s - 2 * w - h)];
}

function perimeterPosition(x, y, w, h) {
  // Where the ray from the middle through (x, y) leaves the screen, as a clockwise distance.
  const cx = w / 2, cy = h / 2, dx = x - cx, dy = y - cy;
  const t = Math.min(dx ? (dx > 0 ? (w - cx) / dx : -cx / dx) : Infinity, dy ? (dy > 0 ? (h - cy) / dy : -cy / dy) : Infinity);
  const ex = cx + dx * t, ey = cy + dy * t;
  const eps = 0.5;
  if (ey <= eps) return ex;
  if (ex >= w - eps) return w + ey;
  if (ey >= h - eps) return w + h + (w - ex);
  return 2 * w + h + (h - ey);
}

function stretches(n, w, h) {
  const p = 2 * (w + h), len = p / n, start = w / 2 - len / 2;
  return Array.from({ length: n }, (_, i) => [start + i * len, start + (i + 1) * len]);
}

export function inCenter(x, y, w, h) {
  return Math.abs(x - w / 2) <= (w * CENTER_W) / 2 && Math.abs(y - h / 2) <= (h * CENTER_H) / 2;
}

export function zoneAt(x, y, w, h, n) {
  if (!n || inCenter(x, y, w, h)) return -1;
  const p = 2 * (w + h), len = p / n, start = w / 2 - len / 2;
  const s = perimeterPosition(x, y, w, h);
  return Math.floor((((s - start) % p) + p) % p / len) % n;
}

// Polygons and label spots for n zones on a w x h screen.
export function zoneShapes(n, w, h) {
  const corners = [0, w, w + h, 2 * w + h];
  return stretches(n, w, h).map(([a, b]) => {
    const pts = [[w / 2, h / 2], perimeterPoint(a, w, h)];
    const inside = [];
    for (let k = -1; k <= 1; k++) for (const c of corners) { const s = c + k * 2 * (w + h); if (s > a && s < b) inside.push(s); }
    inside.sort((s1, s2) => s1 - s2);
    for (const s of inside) pts.push(perimeterPoint(s, w, h));
    pts.push(perimeterPoint(b, w, h));
    const [mx, my] = perimeterPoint((a + b) / 2, w, h);
    // The label sits a little in from the edge, toward the middle.
    const inset = Math.min(w, h) * 0.17;
    const dx = w / 2 - mx, dy = h / 2 - my, d = Math.hypot(dx, dy) || 1;
    return { points: pts, label: [mx + (dx / d) * inset, my + (dy / d) * inset] };
  });
}

// Draws the zones into an <svg> plus label chips into a container, sized w x h.
export function drawZones(svgNode, labelBox, categories, w, h, { hot = -1, has = [] } = {}) {
  svgNode.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svgNode.replaceChildren();
  labelBox.replaceChildren();
  const shapes = zoneShapes(categories.length, w, h);
  shapes.forEach((shape, i) => {
    const cat = categories[i];
    svgNode.append(svg("polygon", {
      points: shape.points.map((p) => p.join(",")).join(" "),
      class: "zone" + (i === hot ? " hot" : ""), style: `--c:${cat.color}`, "data-zone": cat.id,
    }));
    const [lx, ly] = shape.label;
    labelBox.append(el("div", {
      class: "zone-label" + (i === hot ? " hot" : "") + (has.includes(cat.id) ? " has" : ""),
      style: `--c:${cat.color};left:${(lx / w) * 100}%;top:${(ly / h) * 100}%`,
    }, cat.name, has.includes(cat.id) ? el("small", {}, "tagged, drop to remove") : null));
  });
  // Keep every label fully on screen.
  const boxW = labelBox.clientWidth, boxH = labelBox.clientHeight, margin = 6;
  for (const label of labelBox.children) {
    const r = label.getBoundingClientRect();
    if (!boxW || !r.width) continue;
    const cx = Math.min(Math.max((parseFloat(label.style.left) / 100) * boxW, margin + r.width / 2), boxW - margin - r.width / 2);
    const cy = Math.min(Math.max((parseFloat(label.style.top) / 100) * boxH, margin + r.height / 2), boxH - margin - r.height / 2);
    label.style.left = `${(cx / boxW) * 100}%`;
    label.style.top = `${(cy / boxH) * 100}%`;
  }
  const cw = w * CENTER_W, ch = h * CENTER_H;
  svgNode.append(svg("rect", { x: (w - cw) / 2, y: (h - ch) / 2, width: cw, height: ch, rx: Math.min(cw, ch) / 5, class: "center" }));
}
