// Hashtag map view: a small force-directed graph in SVG, without libraries, so the page works offline.
// Nodes are sized by their count and coloured by role (seed / searched / discovered); a link's width
// follows how many posts tie the two hashtags. Drag a node to move it, drag the background to pan,
// scroll to zoom; hovering a node lights up its links and clicking it selects it (onSelect is told).
(() => {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const make = (name, attrs, parent) => {
    const el = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    if (parent) parent.appendChild(el);
    return el;
  };
  const roleOf = (d) => (d.seed ? "seed" : d.explored ? "searched" : "found");
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  class HashtagGraphView {
    constructor(host, options = {}) {
      this.host = host;
      this.onSelect = options.onSelect || (() => {});
      this.svg = make("svg", { class: "gsvg", role: "img", "aria-label": "Hashtag graph" }, host);
      this.stage = make("g", {}, this.svg);
      this.linkLayer = make("g", { class: "glinks" }, this.stage);
      this.nodeLayer = make("g", { class: "gnodes" }, this.stage);
      this.nodes = new Map(); // tag -> {data, x, y, vx, vy, r, fixed, g, circle, num, label, title}
      this.links = []; // one per pair of hashtags: {a, b, count, dirs, line}
      this.minWeight = 1;
      this.selected = null;
      this.hovered = null;
      this.view = { x: 0, y: 0, k: 1 };
      this.alpha = 0;
      this.frame = 0;
      this.size();
      new ResizeObserver(() => { this.size(); this.kick(0.15); }).observe(host);
      this.wire();
    }

    size() {
      this.w = this.host.clientWidth || 900;
      this.h = this.host.clientHeight || 600;
      this.svg.setAttribute("viewBox", `0 0 ${this.w} ${this.h}`);
    }

    clear() {
      this.nodes.forEach((n) => n.g.remove());
      this.links.forEach((l) => l.line.remove());
      this.nodes.clear();
      this.links = [];
      this.selected = this.hovered = null;
      this.reset();
    }

    // ---- data ----------------------------------------------------------------------------------
    setData(snap) {
      const counts = snap.nodes.map((n) => n.count);
      const top = Math.max(1, ...counts);
      const keep = new Set(snap.nodes.map((n) => n.tag));
      for (const [tag, n] of this.nodes) if (!keep.has(tag)) { n.g.remove(); this.nodes.delete(tag); }
      let added = 0;
      for (const d of snap.nodes) {
        let n = this.nodes.get(d.tag);
        if (!n) {
          // a new hashtag starts next to the one whose posts brought it in, so the graph grows outwards
          const anchor = d.found_via && this.nodes.get(d.found_via);
          const angle = Math.random() * Math.PI * 2, dist = anchor ? 50 + Math.random() * 30 : Math.random() * 40;
          n = {
            tag: d.tag, vx: 0, vy: 0, fixed: false,
            x: (anchor ? anchor.x : this.w / 2) + Math.cos(angle) * dist,
            y: (anchor ? anchor.y : this.h / 2) + Math.sin(angle) * dist,
          };
          n.g = make("g", { class: "gnode" }, this.nodeLayer);
          n.circle = make("circle", {}, n.g);
          n.num = make("text", { class: "gnum", "text-anchor": "middle", dy: "0.35em" }, n.g);
          n.label = make("text", { class: "glabel", "text-anchor": "middle" }, n.g);
          n.title = make("title", {}, n.g);
          this.wireNode(n);
          this.nodes.set(d.tag, n);
          added++;
        }
        n.data = d;
        const scaled = Math.sqrt(d.count / top);
        n.r = d.seed ? Math.max(20, 10 + 22 * scaled) : 9 + 21 * scaled;
        n.circle.setAttribute("r", n.r.toFixed(1));
        n.g.setAttribute("class", `gnode ${roleOf(d)}${this.selected === d.tag ? " sel" : ""}`);
        n.num.textContent = d.count;
        n.num.setAttribute("font-size", Math.max(9, Math.min(15, n.r * 0.72)).toFixed(1));
        n.label.textContent = `#${d.tag}`;
        n.label.setAttribute("y", (n.r + 13).toFixed(1));
        n.title.textContent = [
          `#${d.tag}${d.seed ? " (seed)" : ""}`,
          `count ${d.count}: collected posts carrying it, found by searching other hashtags`,
          d.explored ? `searched: ${plural(d.posts_read, "post")} read` : "not searched yet",
          `${plural(d.degree, "link")}${d.found_via ? ` · found via #${d.found_via}` : ""} · depth ${d.depth}`,
        ].join("\n");
      }
      // A -> B and B -> A are drawn as one line; its tooltip keeps both directions.
      this.links.forEach((l) => l.line.remove());
      const pairs = new Map();
      for (const e of snap.edges) {
        if (!this.nodes.has(e.source) || !this.nodes.has(e.target)) continue;
        const [a, b] = e.source < e.target ? [e.source, e.target] : [e.target, e.source];
        const key = `${a}\u0000${b}`;
        const link = pairs.get(key) || { a, b, count: 0, dirs: [] };
        link.count += e.count;
        link.dirs.push(e);
        pairs.set(key, link);
      }
      const heaviest = Math.max(1, ...[...pairs.values()].map((l) => l.count));
      const degree = new Map();
      this.links = [...pairs.values()].map((l) => {
        l.line = make("line", { class: "glink", "stroke-width": (0.8 + 4.5 * Math.sqrt(l.count / heaviest)).toFixed(2) }, this.linkLayer);
        make("title", {}, l.line).textContent = l.dirs.map((e) => `#${e.source} → #${e.target}: ${plural(e.count, "post")}`).join("\n");
        degree.set(l.a, (degree.get(l.a) || 0) + 1);
        degree.set(l.b, (degree.get(l.b) || 0) + 1);
        return l;
      });
      this.nodes.forEach((n) => { n.deg = degree.get(n.tag) || 0; });
      this.filter();
      this.highlight();
      this.kick(added ? 1 : 0.3);
    }

    setMinWeight(value) {
      this.minWeight = Math.max(1, Number(value) || 1);
      this.filter();
    }

    filter() {
      this.links.forEach((l) => l.line.classList.toggle("off", l.count < this.minWeight));
    }

    // ---- layout ---------------------------------------------------------------------------------
    kick(alpha) {
      this.alpha = Math.max(this.alpha, alpha);
      if (!this.frame) this.frame = requestAnimationFrame(() => this.tick());
    }

    tick() {
      this.frame = 0;
      this.step();
      this.step();
      this.draw();
      this.alpha *= 0.975;
      if (this.alpha > 0.008) this.frame = requestAnimationFrame(() => this.tick());
    }

    step() {
      const list = [...this.nodes.values()];
      if (!list.length) return;
      const a = this.alpha, cx = this.w / 2, cy = this.h / 2;
      const charge = 240 + 3000 / list.length; // a small graph spreads out a little more
      for (let i = 0; i < list.length; i++) {
        const p = list[i];
        for (let j = i + 1; j < list.length; j++) {
          const q = list[j];
          let dx = q.x - p.x, dy = q.y - p.y, l = dx * dx + dy * dy;
          if (l < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; l = 1; }
          const f = (charge * a) / l;
          p.vx -= dx * f; p.vy -= dy * f; q.vx += dx * f; q.vy += dy * f;
          const d = Math.sqrt(l), min = p.r + q.r + 10;
          if (d < min) { // keep circles from overlapping
            const push = ((min - d) / d) * 0.25;
            p.vx -= dx * push; p.vy -= dy * push; q.vx += dx * push; q.vy += dy * push;
          }
        }
      }
      for (const link of this.links) {
        const p = this.nodes.get(link.a), q = this.nodes.get(link.b);
        const dx = q.x - p.x, dy = q.y - p.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
        const rest = 50 + p.r + q.r;
        const f = (((d - rest) / d) * a * 0.5) / Math.max(1, Math.min(p.deg, q.deg));
        p.vx += dx * f; p.vy += dy * f; q.vx -= dx * f; q.vy -= dy * f;
      }
      for (const n of list) {
        n.vx += (cx - n.x) * 0.012 * a;
        n.vy += (cy - n.y) * 0.02 * a;
        if (n.fixed) { n.vx = n.vy = 0; continue; }
        n.vx *= 0.6; n.vy *= 0.6;
        n.x = Math.max(n.r + 4, Math.min(this.w - n.r - 4, n.x + n.vx));
        n.y = Math.max(n.r + 4, Math.min(this.h - n.r - 20, n.y + n.vy)); // room for the label
      }
    }

    draw() {
      for (const n of this.nodes.values()) n.g.setAttribute("transform", `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})`);
      for (const l of this.links) {
        const p = this.nodes.get(l.a), q = this.nodes.get(l.b);
        l.line.setAttribute("x1", p.x.toFixed(1)); l.line.setAttribute("y1", p.y.toFixed(1));
        l.line.setAttribute("x2", q.x.toFixed(1)); l.line.setAttribute("y2", q.y.toFixed(1));
      }
      this.stage.setAttribute("transform", `translate(${this.view.x.toFixed(1)},${this.view.y.toFixed(1)}) scale(${this.view.k.toFixed(3)})`);
    }

    // ---- view -------------------------------------------------------------------------------------
    reset() {
      this.view = { x: 0, y: 0, k: 1 };
      this.draw();
    }

    fit() {
      if (!this.nodes.size) return this.reset();
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of this.nodes.values()) {
        x0 = Math.min(x0, n.x - n.r - 40); x1 = Math.max(x1, n.x + n.r + 40);
        y0 = Math.min(y0, n.y - n.r - 8); y1 = Math.max(y1, n.y + n.r + 22);
      }
      const k = Math.min(2.5, Math.min(this.w / (x1 - x0), this.h / (y1 - y0)));
      this.view = { k, x: (this.w - (x1 - x0) * k) / 2 - x0 * k, y: (this.h - (y1 - y0) * k) / 2 - y0 * k };
      this.draw();
    }

    point(event) {
      const box = this.svg.getBoundingClientRect();
      const sx = ((event.clientX - box.left) / box.width) * this.w, sy = ((event.clientY - box.top) / box.height) * this.h;
      return { sx, sy, x: (sx - this.view.x) / this.view.k, y: (sy - this.view.y) / this.view.k };
    }

    // ---- interaction ------------------------------------------------------------------------------------
    select(tag, notify = true) {
      this.selected = tag && this.selected !== tag ? tag : null;
      this.nodes.forEach((n) => n.g.classList.toggle("sel", n.tag === this.selected));
      this.highlight();
      if (notify) this.onSelect(this.selected);
    }

    highlight() {
      const focus = this.hovered || this.selected;
      const near = new Set(focus ? [focus] : []);
      if (focus) this.links.forEach((l) => { if (l.a === focus) near.add(l.b); if (l.b === focus) near.add(l.a); });
      this.svg.classList.toggle("focus", !!focus);
      this.nodes.forEach((n) => n.g.classList.toggle("hl", near.has(n.tag)));
      this.links.forEach((l) => l.line.classList.toggle("hl", !!focus && (l.a === focus || l.b === focus)));
    }

    wireNode(n) {
      n.g.addEventListener("pointerenter", () => { this.hovered = n.tag; this.highlight(); });
      n.g.addEventListener("pointerleave", () => { this.hovered = null; this.highlight(); });
      n.g.addEventListener("pointerdown", (e) => {
        e.stopPropagation();
        const start = this.point(e);
        let moved = false;
        n.fixed = true;
        this.svg.setPointerCapture(e.pointerId);
        const move = (ev) => {
          const p = this.point(ev);
          if (Math.abs(p.sx - start.sx) + Math.abs(p.sy - start.sy) > 3) moved = true;
          n.x = p.x; n.y = p.y;
          this.kick(0.25);
        };
        const up = () => {
          n.fixed = false;
          this.svg.removeEventListener("pointermove", move);
          this.svg.removeEventListener("pointerup", up);
          if (!moved) this.select(n.tag);
        };
        this.svg.addEventListener("pointermove", move);
        this.svg.addEventListener("pointerup", up);
      });
    }

    wire() {
      this.svg.addEventListener("pointerdown", (e) => {
        const start = this.point(e), from = { ...this.view };
        let moved = false;
        this.svg.setPointerCapture(e.pointerId);
        this.svg.classList.add("panning");
        const move = (ev) => {
          const p = this.point(ev);
          if (Math.abs(p.sx - start.sx) + Math.abs(p.sy - start.sy) > 3) moved = true;
          this.view.x = from.x + p.sx - start.sx;
          this.view.y = from.y + p.sy - start.sy;
          this.draw();
        };
        const up = () => {
          this.svg.classList.remove("panning");
          this.svg.removeEventListener("pointermove", move);
          this.svg.removeEventListener("pointerup", up);
          if (!moved && this.selected) this.select(null);
        };
        this.svg.addEventListener("pointermove", move);
        this.svg.addEventListener("pointerup", up);
      });
      this.svg.addEventListener("wheel", (e) => {
        e.preventDefault();
        const p = this.point(e);
        const k = Math.max(0.3, Math.min(4, this.view.k * Math.exp(-e.deltaY * 0.0015)));
        this.view.x = p.sx - ((p.sx - this.view.x) * k) / this.view.k;
        this.view.y = p.sy - ((p.sy - this.view.y) * k) / this.view.k;
        this.view.k = k;
        this.draw();
      }, { passive: false });
    }
  }

  window.HashtagGraphView = HashtagGraphView;
})();
