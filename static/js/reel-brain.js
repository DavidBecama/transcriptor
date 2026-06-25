/* ════════════════════════════════════════════════════════════════════════
   ReelBrain — el Cerebro que se construye paso a paso.
   Vanilla JS + Canvas 2D. Sin dependencias, sin build, sin WebGL.
   Portable a cualquier isla:

       const brain = new ReelBrain(canvasEl, { brand:'#4f7cff' });
       brain.setStep(1..8);   // crece de forma continua (suma, no reinicia)
       brain.feed();          // chispazo extra (paso "Alimentar el Cerebro")
       brain.destroy();

   Respeta prefers-reduced-motion (estado estático bonito por paso).
   ════════════════════════════════════════════════════════════════════════ */
(function (root) {
  'use strict';

  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function hexRgb(h) {
    h = h.replace('#', '');
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }

  // Glow sprite (radial gradient) so we never pay per-frame gradient cost.
  function glowSprite(rgb, soft) {
    const s = 64, c = document.createElement('canvas');
    c.width = c.height = s;
    const g = c.getContext('2d');
    const grad = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
    grad.addColorStop(0, 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',1)');
    grad.addColorStop(soft ? 0.18 : 0.32, 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',0.55)');
    grad.addColorStop(1, 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',0)');
    g.fillStyle = grad; g.fillRect(0, 0, s, s);
    return c;
  }

  // Energy per step (0..1): how "awake / bright / fast" the brain feels.
  var ENERGY = [0, 0.30, 0.42, 0.58, 0.68, 0.78, 0.87, 0.95, 1];

  function ReelBrain(canvas, opts) {
    opts = opts || {};
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.brand = opts.brand || '#4f7cff';
    this.brand2 = opts.brand2 || '#6f93ff';
    this.brandRgb = hexRgb(this.brand);
    this.brand2Rgb = hexRgb(this.brand2);
    this.step = 1;
    this.t = 0;
    this.last = 0;
    this.particles = [];
    this.dpr = Math.min(2, root.devicePixelRatio || 1);
    this.reduced = !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);

    this.spriteNode = glowSprite(this.brand2Rgb, false);
    this.spriteCore = glowSprite([235, 242, 255], true);
    this.spriteBg = glowSprite(this.brandRgb, true);

    this._build();

    this._onResize = this._resize.bind(this);
    this._resize();
    if (root.ResizeObserver) {
      this._ro = new ResizeObserver(this._onResize);
      this._ro.observe(canvas.parentElement || canvas);
    }
    root.addEventListener('resize', this._onResize);

    this._tick = this._tick.bind(this);
    // Mark step-1 nodes as already born.
    this.setStep(1);
    if (this.reduced) { this._render(0); }
    else { this._running = true; this._raf = requestAnimationFrame(this._tick); }
  }

  ReelBrain.prototype._build = function () {
    var rng = mulberry32(opts_seed(this) || 11);
    var nodes = [];
    // Core.
    nodes.push({ x: 0, y: 0, r: 1.7, core: true, appear: 1, sat: false });

    // Brain silhouette: rounded blob, top-centre notch (two lobes), faint
    // vertical fissure. Generate the right half, mirror to the left.
    function inside(x, y) {
      var ex = x / 1.04, ey = y / 0.80;
      if (ex * ex + ey * ey > 1) return false;              // outer blob
      if (Math.abs(x) < 0.13 && y < -0.34) return false;     // top centre notch
      return true;
    }
    var right = [];
    var guard = 0;
    while (right.length < 98 && guard < 11000) {
      guard++;
      var x = rng() * 1.12;
      var y = (rng() * 2 - 1) * 0.86;
      if (!inside(x, y)) continue;
      // thin out along the central fissure
      if (x < 0.08 && rng() < 0.58) continue;
      // blue-noise-ish spacing
      var ok = true;
      for (var k = 0; k < right.length; k++) {
        var dx = right[k].x - x, dy = right[k].y - y;
        if (dx * dx + dy * dy < 0.0104) { ok = false; break; }
      }
      if (!ok) continue;
      right.push({ x: x, y: y });
    }
    right.forEach(function (p) {
      nodes.push({ x: p.x, y: p.y, r: 0, appear: 0, sat: false });
      nodes.push({ x: -p.x, y: p.y + (rng() - 0.5) * 0.03, r: 0, appear: 0, sat: false });
    });

    // Radial order → assign the step each node is born on (brain grows outward).
    var inner = nodes.filter(function (n) { return !n.core; });
    inner.forEach(function (n) { n.d = Math.hypot(n.x, n.y); });
    inner.sort(function (a, b) { return a.d - b.d; });
    var maxD = inner.length ? inner[inner.length - 1].d : 1;
    inner.forEach(function (n, i) {
      var f = i / inner.length;
      n.appear = f < 0.10 ? 2 : f < 0.30 ? 3 : f < 0.55 ? 4 : f < 0.82 ? 5 : 7;
      n.imp = 1 - n.d / (maxD || 1);           // importance → size/brightness
      n.r = 0.8 + n.imp * 1.5;
    });

    // Step 5 dendrites: a few fine branch nodes hanging off outer nodes.
    var outer = inner.filter(function (n) { return n.appear >= 5 && !n.core; }).slice(0, 16);
    outer.forEach(function (p, i) {
      if (i % 2) return;
      var ang = Math.atan2(p.y, p.x) + (rng() - 0.5) * 0.5;
      var rr = p.d + 0.14 + rng() * 0.07;
      nodes.push({ x: Math.cos(ang) * rr, y: Math.sin(ang) * rr, r: 0.7, appear: 5, sat: false, dend: true, imp: 0.12 });
    });

    // Step 6 satellites: external referents that plug into the network.
    var satAng = [-2.5, -2.0, 2.0, 2.5, -1.2, 1.2];
    satAng.forEach(function (a, i) {
      var rr = 1.30 + (i % 2) * 0.12;
      nodes.push({ x: Math.cos(a) * rr, y: Math.sin(a) * rr * 0.86, r: 1.5, appear: 6, sat: true, imp: 0.5 });
    });

    this.nodes = nodes;

    // Edges: k-nearest among non-satellite nodes; satellites get one long
    // tether to their nearest inner node.
    var edges = [];
    var seen = {};
    var core = nodes[0];
    for (var i = 1; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.sat) {
        var best = -1, bd = 1e9;
        for (var j = 0; j < nodes.length; j++) {
          if (nodes[j].sat) continue;
          var dd = Math.hypot(nodes[j].x - n.x, nodes[j].y - n.y);
          if (dd < bd) { bd = dd; best = j; }
        }
        edges.push(mkEdge(i, best));
        continue;
      }
      var nbr = [];
      for (var j2 = 0; j2 < nodes.length; j2++) {
        if (j2 === i || nodes[j2].sat) continue;
        nbr.push([j2, Math.hypot(nodes[j2].x - n.x, nodes[j2].y - n.y)]);
      }
      nbr.sort(function (a, b) { return a[1] - b[1]; });
      var kk = n.dend ? 1 : 3;
      for (var m = 0; m < kk && m < nbr.length; m++) {
        var key = Math.min(i, nbr[m][0]) + '_' + Math.max(i, nbr[m][0]);
        if (seen[key]) continue;
        seen[key] = 1;
        edges.push(mkEdge(i, nbr[m][0]));
      }
    }
    function mkEdge(a, b) {
      var inA = Math.hypot(nodes[a].x, nodes[a].y) <= Math.hypot(nodes[b].x, nodes[b].y);
      return { a: a, b: b, inner: inA ? a : b, outer: inA ? b : a };
    }
    this.edges = edges;

    // Ambient void particles (the brain "alive in the vacuum" feel).
    this.ambient = [];
    for (var av = 0; av < 11; av++) {
      this.ambient.push({ a: rng() * 6.28, rr: 0.42 + rng() * 0.5, sp: (0.05 + rng() * 0.1) * (rng() < 0.5 ? 1 : -1), ph: rng() * 6.28 });
    }
    void core;
  };

  function opts_seed(self) { return self._seed; }

  ReelBrain.prototype.setStep = function (n) {
    n = clamp(Math.round(n), 1, 8);
    var now = (root.performance && performance.now()) || Date.now();
    var grew = n > this.step;
    var nodes = this.nodes;
    for (var i = 0; i < nodes.length; i++) {
      var nd = nodes[i];
      if (nd.appear <= n) { if (nd.born == null) nd.born = now; }
      else { nd.born = null; }
    }
    this.step = n;
    this._activeEdges = this.edges.filter(function (e) {
      return nodes[e.a].born != null && nodes[e.b].born != null;
    });
    if (grew) this._burst(n, now);
    this._render(0);
  };

  ReelBrain.prototype.feed = function () {
    var now = (root.performance && performance.now()) || Date.now();
    this._burst(this.step, now, 26);
  };

  ReelBrain.prototype._burst = function (n, now, count) {
    if (this.reduced) return;
    count = count || 16;
    var pool = this._activeEdges || [];
    // prefer edges touching freshly-born nodes
    var fresh = pool.filter(function (e) {
      return this.nodes[e.outer].appear === n || this.nodes[e.inner].appear === n;
    }, this);
    var src = fresh.length ? fresh : pool;
    for (var i = 0; i < count && src.length; i++) {
      this.particles.push({ e: src[(Math.random() * src.length) | 0], t: Math.random() * 0.15, sp: 0.9 + Math.random() * 0.9, big: true });
    }
  };

  ReelBrain.prototype._resize = function () {
    var c = this.canvas, p = c.parentElement || c;
    var w = p.clientWidth || c.clientWidth || 600;
    var h = p.clientHeight || c.clientHeight || 600;
    this.w = w; this.h = h;
    c.width = Math.round(w * this.dpr);
    c.height = Math.round(h * this.dpr);
    this.cx = w / 2;
    this.cy = h / 2 - h * 0.03;
    this.scale = Math.min(w, h) * 0.475;
    if (this.reduced) this._render(0);
  };

  ReelBrain.prototype._tick = function (ts) {
    if (!this._running) return;
    if (!this.last) this.last = ts;
    var dt = Math.min(0.05, (ts - this.last) / 1000);
    this.last = ts;
    this.t += dt;

    var e = ENERGY[this.step];
    // spawn flowing particles toward the core
    var pool = this._activeEdges || [];
    if (pool.length && this.particles.length < 90 && Math.random() < e * 0.55) {
      this.particles.push({ e: pool[(Math.random() * pool.length) | 0], t: 0, sp: 0.5 + Math.random() * 0.6 });
    }
    for (var i = this.particles.length - 1; i >= 0; i--) {
      var pt = this.particles[i];
      pt.t += dt * pt.sp;
      if (pt.t >= 1) this.particles.splice(i, 1);
    }
    this._render(dt);
    this._raf = requestAnimationFrame(this._tick);
  };

  ReelBrain.prototype._sx = function (x) { return this.cx + x * this.scale; };
  ReelBrain.prototype._sy = function (y) { return this.cy + y * this.scale; };

  ReelBrain.prototype._render = function () {
    var ctx = this.ctx, nodes = this.nodes;
    var now = (root.performance && performance.now()) || Date.now();
    var e = ENERGY[this.step];
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);

    var br = this.brandRgb, b2 = this.brand2Rgb;
    var pulse = this.reduced ? 1 : (0.5 + 0.5 * Math.sin(this.t * 1.9));
    // heartbeat once the brain is fully formed — a calm double-thump throb
    var beat = 0;
    if (this.step >= 8 && !this.reduced) {
      var hp = (this.t % 1.25) / 1.25;
      beat = Math.exp(-Math.pow(hp * 7.5, 2)) + 0.5 * Math.exp(-Math.pow((hp - 0.26) * 6.5, 2));
    }
    this._beat = beat;

    // ── ambient background bloom ─────────────────────────────────────────
    ctx.globalCompositeOperation = 'lighter';
    var bgS = this.scale * (2.6 + e * 1.6 + (this.reduced ? 0 : pulse * 0.18) + beat * 0.5);
    ctx.globalAlpha = 0.12 + e * 0.16 + beat * 0.12;
    ctx.drawImage(this.spriteBg, this.cx - bgS / 2, this.cy - bgS / 2, bgS, bgS);

    // ── void / ambient drifting motes (life in the vacuum) ───────────────
    var amA = 0.34 * (1 - e * 0.7);
    if (!this.reduced) {
      for (var a = 0; a < this.ambient.length; a++) {
        var m = this.ambient[a];
        var ang = m.a + this.t * m.sp;
        var x = this._sx(Math.cos(ang) * m.rr);
        var y = this._sy(Math.sin(ang) * m.rr * 0.82);
        var fl = 0.4 + 0.6 * Math.abs(Math.sin(this.t * 0.8 + m.ph));
        var s = 5 + fl * 5;
        ctx.globalAlpha = amA * fl;
        ctx.drawImage(this.spriteNode, x - s / 2, y - s / 2, s, s);
      }
    }

    // ── synapses (edges) ─────────────────────────────────────────────────
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    var pool = this._activeEdges || [];
    for (var i = 0; i < pool.length; i++) {
      var ed = pool[i];
      var na = nodes[ed.a], nb = nodes[ed.b];
      var pa = na.born == null ? 0 : (this.reduced ? 1 : easeOut(clamp((now - na.born) / 620, 0, 1)));
      var pb = nb.born == null ? 0 : (this.reduced ? 1 : easeOut(clamp((now - nb.born) / 620, 0, 1)));
      var grow = Math.min(pa, pb);
      if (grow <= 0.01) continue;
      var x1 = this._sx(na.x), y1 = this._sy(na.y);
      var x2 = this._sx(nb.x), y2 = this._sy(nb.y);
      var sat = na.sat || nb.sat;
      ctx.strokeStyle = 'rgba(' + b2[0] + ',' + b2[1] + ',' + b2[2] + ',' + (grow * (sat ? 0.26 : (0.30 + e * 0.22))) + ')';
      ctx.lineWidth = (sat ? 0.9 : (na.dend || nb.dend ? 0.8 : 1.2)) ;
      // draw partial during growth
      var gx = x1 + (x2 - x1) * grow, gy = y1 + (y2 - y1) * grow;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(gx, gy); ctx.stroke();
    }

    // ── particles flowing inward ─────────────────────────────────────────
    ctx.globalCompositeOperation = 'lighter';
    for (var p = 0; p < this.particles.length; p++) {
      var pt = this.particles[p], ee = pt.e;
      var no = nodes[ee.outer], ni = nodes[ee.inner];
      var tt = easeOut(pt.t);
      var x = this._sx(no.x + (ni.x - no.x) * tt);
      var y = this._sy(no.y + (ni.y - no.y) * tt);
      var s = (pt.big ? 16 : 9) * (1 - pt.t * 0.45);
      ctx.globalAlpha = (pt.big ? 0.9 : 0.55) * (1 - pt.t * 0.3);
      ctx.drawImage(pt.big ? this.spriteCore : this.spriteNode, x - s / 2, y - s / 2, s, s);
    }

    // ── nodes ────────────────────────────────────────────────────────────
    for (var n = 0; n < nodes.length; n++) {
      var nd = nodes[n];
      if (nd.born == null || nd.core) continue;
      var gp = this.reduced ? 1 : easeOut(clamp((now - nd.born) / 620, 0, 1));
      if (gp <= 0.01) continue;
      var x = this._sx(nd.x), y = this._sy(nd.y);
      var imp = nd.imp == null ? 0.3 : nd.imp;
      var twk = this.reduced ? 0.8 : (0.6 + 0.4 * Math.sin(this.t * 2 + n));
      // glow
      var gs = (13 + imp * 32) * gp * (0.7 + e * 0.4 + beat * 0.5);
      ctx.globalAlpha = (0.42 + imp * 0.5 + e * 0.18 + beat * 0.28) * gp * (nd.sat ? 0.78 : 1);
      ctx.drawImage(this.spriteNode, x - gs / 2, y - gs / 2, gs, gs);
      // solid dot
      ctx.globalAlpha = (0.7 + imp * 0.3) * gp;
      var dot = (nd.r * 1.25) * gp * (1 + twk * 0.12);
      ctx.fillStyle = nd.sat ? ('rgba(' + br[0] + ',' + br[1] + ',' + br[2] + ',1)')
        : ('rgba(' + (190 + imp * 55) + ',' + (212 + imp * 40) + ',255,1)');
      ctx.beginPath(); ctx.arc(x, y, Math.max(0.9, dot), 0, 6.2832); ctx.fill();
    }

    // ── core (always there, pulsing bloom) ───────────────────────────────
    var cx = this._sx(0), cy = this._sy(0);
    var coreS = (this.scale * (0.18 + e * 0.14)) * (1 + (this.reduced ? 0 : pulse * 0.18) + beat * 0.6);
    ctx.globalAlpha = 0.6 + e * 0.35 + beat * 0.2;
    ctx.drawImage(this.spriteCore, cx - coreS / 2, cy - coreS / 2, coreS, coreS);
    ctx.globalAlpha = 1;
    var coreR = (3.5 + e * 4.5) * (1 + (this.reduced ? 0 : pulse * 0.18) + beat * 0.5);
    ctx.fillStyle = '#eef4ff';
    ctx.beginPath(); ctx.arc(cx, cy, coreR, 0, 6.2832); ctx.fill();

    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
  };

  ReelBrain.prototype.destroy = function () {
    this._running = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    if (this._ro) this._ro.disconnect();
    root.removeEventListener('resize', this._onResize);
  };

  root.ReelBrain = ReelBrain;
  if (typeof module !== 'undefined' && module.exports) module.exports = { ReelBrain: ReelBrain };
})(typeof window !== 'undefined' ? window : this);
