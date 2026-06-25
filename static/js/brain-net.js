/* ════════════════════════════════════════════════════════════════════════
   BrainNet — el Cerebro de ReelScript como red neuronal viva.
   Vanilla JS + Canvas 2D. Cero dependencias de framework. Portable.

       const brain = new BrainNet(canvasEl, { brand:'#4f7cff' });
       brain.setLevel(2);                 // densidad de la red por nivel (1..5)
       brain.update({ progress:0.6, streak:3, voice:0.64 });
       brain.feed('guion');               // partículas tipadas volando al cerebro
       brain.levelUp();                   // evolución: destello + onda + crece
       brain.destroy();

   Respeta prefers-reduced-motion (estado estático bonito por nivel).
   ════════════════════════════════════════════════════════════════════════ */
(function (root) {
  'use strict';

  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function hexRgb(h) {
    h = h.replace('#', '');
    if (h.length === 3) h = h.split('').map(function (c) { return c + c; }).join('');
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }
  function now() { return (root.performance && performance.now()) || Date.now(); }

  function glowSprite(rgb, soft) {
    var s = 64, c = document.createElement('canvas');
    c.width = c.height = s;
    var g = c.getContext('2d');
    var grad = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
    grad.addColorStop(0, 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',1)');
    grad.addColorStop(soft ? 0.18 : 0.34, 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',0.5)');
    grad.addColorStop(1, 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',0)');
    g.fillStyle = grad; g.fillRect(0, 0, s, s);
    return c;
  }

  // Typed feed colors — distinct material the Brain swallows.
  var TYPE_COLORS = {
    guion:    '#f5a83d',  // un guion que petó
    hook:     '#6f93ff',  // un hook
    muletilla:'#a68cfa',  // muletillas
    estilo:   '#33d499',  // estilo / preferencias
    _default: '#6f93ff'
  };

  // Energy (brightness/speed) per level 1..5.
  var ENERGY = [0, 0.44, 0.58, 0.72, 0.86, 1.0];
  // Fraction of the node cloud revealed at each level.
  var REVEAL = [0, 0.30, 0.50, 0.70, 0.86, 1.0];

  function BrainNet(canvas, opts) {
    opts = opts || {};
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.brand = opts.brand || '#4f7cff';
    this.brand2 = opts.brand2 || '#6f93ff';
    this.seed = opts.seed || 7;
    this.brandRgb = hexRgb(this.brand);
    this.brand2Rgb = hexRgb(this.brand2);
    this.level = 1;
    this.progress = 0;   // 0..1 toward next level
    this.voice = 0;
    this.streak = 0;
    this.t = 0; this.last = 0;
    this.particles = [];
    this.rings = [];     // level-up shockwaves
    this.dpr = Math.min(2, root.devicePixelRatio || 1);
    this.reduced = !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);

    this.spriteNode = glowSprite(this.brand2Rgb, false);
    this.spriteCore = glowSprite([236, 243, 255], true);
    this.spriteBg = glowSprite(this.brandRgb, true);
    this._typeSprites = {};
    for (var k in TYPE_COLORS) this._typeSprites[k] = glowSprite(hexRgb(TYPE_COLORS[k]), false);

    this._build();
    this._onResize = this._resize.bind(this);
    this._resize();
    if (root.ResizeObserver) { this._ro = new ResizeObserver(this._onResize); this._ro.observe(canvas.parentElement || canvas); }
    root.addEventListener('resize', this._onResize);

    this._tick = this._tick.bind(this);
    this.setLevel(1);
    if (this.reduced) { this._render(); }
    else { this._running = true; this._raf = requestAnimationFrame(this._tick); }
  }

  BrainNet.prototype._build = function () {
    var rng = mulberry32(this.seed);
    var nodes = [{ x: 0, y: 0, r: 1.8, core: true, appear: 1, imp: 1 }];

    function inside(x, y) {
      var ex = x / 1.04, ey = y / 0.80;
      if (ex * ex + ey * ey > 1) return false;
      if (Math.abs(x) < 0.13 && y < -0.34) return false; // top notch (two lobes)
      return true;
    }
    var right = [], guard = 0;
    while (right.length < 92 && guard < 11000) {
      guard++;
      var x = rng() * 1.12, y = (rng() * 2 - 1) * 0.86;
      if (!inside(x, y)) continue;
      if (x < 0.08 && rng() < 0.58) continue;
      var ok = true;
      for (var k = 0; k < right.length; k++) {
        var dx = right[k].x - x, dy = right[k].y - y;
        if (dx * dx + dy * dy < 0.0108) { ok = false; break; }
      }
      if (!ok) continue;
      right.push({ x: x, y: y });
    }
    right.forEach(function (p) {
      nodes.push({ x: p.x, y: p.y, r: 0, appear: 0 });
      nodes.push({ x: -p.x, y: p.y + (rng() - 0.5) * 0.03, r: 0, appear: 0 });
    });

    var inner = nodes.filter(function (n) { return !n.core; });
    inner.forEach(function (n) { n.d = Math.hypot(n.x, n.y); });
    inner.sort(function (a, b) { return a.d - b.d; });
    var maxD = inner.length ? inner[inner.length - 1].d : 1;
    inner.forEach(function (n, i) {
      var f = i / inner.length;
      // map fraction -> the level at which this node is revealed
      n.appear = f <= REVEAL[1] ? 1 : f <= REVEAL[2] ? 2 : f <= REVEAL[3] ? 3 : f <= REVEAL[4] ? 4 : 5;
      n.imp = 1 - n.d / (maxD || 1);
      n.r = 0.8 + n.imp * 1.5;
    });

    this.nodes = nodes;

    // edges: k-nearest among nodes
    var edges = [], seen = {};
    for (var i = 1; i < nodes.length; i++) {
      var nbr = [];
      for (var j = 0; j < nodes.length; j++) {
        if (j === i) continue;
        nbr.push([j, Math.hypot(nodes[j].x - nodes[i].x, nodes[j].y - nodes[i].y)]);
      }
      nbr.sort(function (a, b) { return a[1] - b[1]; });
      for (var m = 0; m < 3 && m < nbr.length; m++) {
        var key = Math.min(i, nbr[m][0]) + '_' + Math.max(i, nbr[m][0]);
        if (seen[key]) continue; seen[key] = 1;
        var a = i, b = nbr[m][0];
        var inA = Math.hypot(nodes[a].x, nodes[a].y) <= Math.hypot(nodes[b].x, nodes[b].y);
        edges.push({ a: a, b: b, inner: inA ? a : b, outer: inA ? b : a });
      }
    }
    this.edges = edges;

    this.ambient = [];
    for (var av = 0; av < 12; av++) {
      this.ambient.push({ a: rng() * 6.28, rr: 0.42 + rng() * 0.5, sp: (0.05 + rng() * 0.1) * (rng() < 0.5 ? 1 : -1), ph: rng() * 6.28 });
    }
  };

  BrainNet.prototype.setLevel = function (n) {
    n = clamp(Math.round(n), 1, 5);
    var tnow = now(), nodes = this.nodes;
    for (var i = 0; i < nodes.length; i++) {
      var nd = nodes[i];
      if (nd.appear <= n) { if (nd.born == null) nd.born = tnow; }
      else { nd.born = null; }
    }
    this.level = n;
    this._activeEdges = this.edges.filter(function (e) { return nodes[e.a].born != null && nodes[e.b].born != null; });
    if (this.reduced) this._render();
  };

  BrainNet.prototype.update = function (state) {
    state = state || {};
    if (state.progress != null) this.progress = clamp(state.progress, 0, 1);
    if (state.voice != null) this.voice = clamp(state.voice, 0, 1);
    if (state.streak != null) this.streak = state.streak;
    if (state.level != null && state.level !== this.level) this.setLevel(state.level);
    if (this.reduced) this._render();
  };

  // Material flying into the brain, absorbed by neurons that light up.
  BrainNet.prototype.feed = function (type, count) {
    if (this.reduced) { this._render(); return; }
    type = TYPE_COLORS[type] ? type : '_default';
    count = count || 14;
    var pool = (this._activeEdges || []);
    var tnow = now();
    for (var i = 0; i < count; i++) {
      // origin just outside the brain (mostly from below — the feed card)
      var ang = Math.PI * (0.62 + Math.random() * 0.76); // lower hemisphere bias
      var or = 1.5 + Math.random() * 0.5;
      var tgt = this._randActiveNode();
      this.particles.push({
        ox: Math.cos(ang) * or, oy: Math.abs(Math.sin(ang)) * or * 0.9 + 0.15,
        target: tgt, t: -Math.random() * 0.25, sp: 0.7 + Math.random() * 0.5, type: type, feed: true
      });
    }
    this._pulse = tnow;            // brain pulse
    // light up a fresh connection cluster around an outer node
    var hub = this._randActiveNode();
    this._flashNode(hub, tnow + 90);
    void pool;
  };

  BrainNet.prototype._randActiveNode = function () {
    var act = [];
    for (var i = 1; i < this.nodes.length; i++) if (this.nodes[i].born != null) act.push(i);
    if (!act.length) return 0;
    // bias toward outer nodes (they receive first)
    return act[(Math.random() * act.length) | 0];
  };
  BrainNet.prototype._flashNode = function (idx, until) {
    var nd = this.nodes[idx]; if (nd) nd.flash = until;
  };

  BrainNet.prototype.levelUp = function () {
    var next = clamp(this.level + 1, 1, 5);
    var tnow = now();
    if (!this.reduced) {
      this.rings.push({ at: tnow, dur: 1100 });
      // outward spray of brand particles
      for (var i = 0; i < 28; i++) {
        this.particles.push({ burst: true, ang: Math.random() * 6.28, rad: 0, sp: 0.6 + Math.random() * 0.9, t: 0, type: '_default' });
      }
      this._flash = tnow + 420;
    }
    this.setLevel(next);
    this._pulse = tnow;
    if (this.reduced) this._render();
  };

  BrainNet.prototype._resize = function () {
    var c = this.canvas, p = c.parentElement || c;
    var w = p.clientWidth || c.clientWidth || 480;
    var h = p.clientHeight || c.clientHeight || 420;
    this.w = w; this.h = h;
    c.width = Math.round(w * this.dpr); c.height = Math.round(h * this.dpr);
    this.cx = w / 2; this.cy = h / 2 - h * 0.015;
    this.scale = Math.min(w, h) * 0.46;
    if (this.reduced) this._render();
  };

  BrainNet.prototype._tick = function (ts) {
    if (!this._running) return;
    if (!this.last) this.last = ts;
    var dt = Math.min(0.05, (ts - this.last) / 1000);
    this.last = ts; this.t += dt;

    var e = ENERGY[this.level];
    var pool = this._activeEdges || [];
    // ambient impulses flowing inward along synapses
    if (pool.length && this.particles.length < 110 && Math.random() < e * 0.6) {
      var ed = pool[(Math.random() * pool.length) | 0];
      this.particles.push({ edge: ed, t: 0, sp: 0.5 + Math.random() * 0.6, type: '_default' });
    }
    for (var i = this.particles.length - 1; i >= 0; i--) {
      var p = this.particles[i];
      p.t += dt * p.sp;
      if (p.burst) { p.rad += dt * p.sp * 1.4; if (p.t >= 1) this.particles.splice(i, 1); }
      else if (p.t >= 1) {
        if (p.feed && p.target != null) this._flashNode(p.target, now() + 360);
        this.particles.splice(i, 1);
      }
    }
    for (var r = this.rings.length - 1; r >= 0; r--) if (now() - this.rings[r].at > this.rings[r].dur) this.rings.splice(r, 1);

    this._render(dt);
    this._raf = requestAnimationFrame(this._tick);
  };

  BrainNet.prototype._sx = function (x) { return this.cx + x * this.scale; };
  BrainNet.prototype._sy = function (y) { return this.cy + y * this.scale; };

  BrainNet.prototype._render = function () {
    var ctx = this.ctx, nodes = this.nodes, tnow = now();
    var e = ENERGY[this.level];
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    var br = this.brandRgb, b2 = this.brand2Rgb;
    var pulse = this.reduced ? 1 : (0.5 + 0.5 * Math.sin(this.t * 1.9));

    // progress-driven "about to level up" energy
    var nearFull = this.progress > 0.78 ? (this.progress - 0.78) / 0.22 : 0;
    // feed pulse (decays ~700ms)
    var fp = this._pulse ? Math.max(0, 1 - (tnow - this._pulse) / 700) : 0;
    // level-up flash
    var flash = this._flash ? Math.max(0, (this._flash - tnow) / 420) : 0;
    var surge = fp + nearFull * (this.reduced ? 0.2 : (0.5 + 0.5 * Math.sin(this.t * 5)));

    // ── background bloom ──
    ctx.globalCompositeOperation = 'lighter';
    var bgS = this.scale * (2.5 + e * 1.6 + (this.reduced ? 0 : pulse * 0.16) + surge * 0.7 + flash * 1.6);
    ctx.globalAlpha = 0.10 + e * 0.15 + surge * 0.10 + flash * 0.30;
    ctx.drawImage(this.spriteBg, this.cx - bgS / 2, this.cy - bgS / 2, bgS, bgS);

    // ── ambient motes ──
    if (!this.reduced) {
      var amA = 0.30 * (1 - e * 0.6);
      for (var a = 0; a < this.ambient.length; a++) {
        var m = this.ambient[a];
        var ang = m.a + this.t * m.sp;
        var x = this._sx(Math.cos(ang) * m.rr), y = this._sy(Math.sin(ang) * m.rr * 0.82);
        var fl = 0.4 + 0.6 * Math.abs(Math.sin(this.t * 0.8 + m.ph));
        var s = 5 + fl * 5;
        ctx.globalAlpha = amA * fl;
        ctx.drawImage(this.spriteNode, x - s / 2, y - s / 2, s, s);
      }
    }

    // ── synapses ──
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    var pool = this._activeEdges || [];
    for (var i = 0; i < pool.length; i++) {
      var ed = pool[i], na = nodes[ed.a], nb = nodes[ed.b];
      var pa = na.born == null ? 0 : (this.reduced ? 1 : easeOut(clamp((tnow - na.born) / 650, 0, 1)));
      var pb = nb.born == null ? 0 : (this.reduced ? 1 : easeOut(clamp((tnow - nb.born) / 650, 0, 1)));
      var grow = Math.min(pa, pb);
      if (grow <= 0.01) continue;
      var x1 = this._sx(na.x), y1 = this._sy(na.y), x2 = this._sx(nb.x), y2 = this._sy(nb.y);
      ctx.strokeStyle = 'rgba(' + b2[0] + ',' + b2[1] + ',' + b2[2] + ',' + (grow * (0.26 + e * 0.22 + surge * 0.18)) + ')';
      ctx.lineWidth = 1.1;
      var gx = x1 + (x2 - x1) * grow, gy = y1 + (y2 - y1) * grow;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(gx, gy); ctx.stroke();
    }

    // ── particles (ambient impulses + feed + burst) ──
    ctx.globalCompositeOperation = 'lighter';
    for (var p2 = 0; p2 < this.particles.length; p2++) {
      var pt = this.particles[p2];
      var sprite = this._typeSprites[pt.type] || this.spriteNode;
      var px, py, sz, al;
      if (pt.burst) {
        var rr = pt.rad;
        px = this._sx(Math.cos(pt.ang) * rr); py = this._sy(Math.sin(pt.ang) * rr * 0.9);
        sz = 12 * (1 - pt.t * 0.5); al = 0.8 * (1 - pt.t);
        sprite = this.spriteCore;
      } else if (pt.feed) {
        if (pt.t < 0) continue;
        var nd = nodes[pt.target] || nodes[0];
        var tt = easeOut(pt.t);
        px = this._sx(pt.ox + (nd.x - pt.ox) * tt); py = this._sy(pt.oy + (nd.y - pt.oy) * tt);
        sz = 13 * (1 - pt.t * 0.35); al = 0.95 * (1 - pt.t * 0.25);
      } else {
        var ee = pt.edge, no = nodes[ee.outer], ni = nodes[ee.inner];
        var t2 = easeOut(pt.t);
        px = this._sx(no.x + (ni.x - no.x) * t2); py = this._sy(no.y + (ni.y - no.y) * t2);
        sz = 8 * (1 - pt.t * 0.4); al = 0.5 * (1 - pt.t * 0.3);
      }
      ctx.globalAlpha = al; ctx.drawImage(sprite, px - sz / 2, py - sz / 2, sz, sz);
    }

    // ── nodes ──
    for (var n = 0; n < nodes.length; n++) {
      var ndn = nodes[n];
      if (ndn.born == null || ndn.core) continue;
      var gp = this.reduced ? 1 : easeOut(clamp((tnow - ndn.born) / 650, 0, 1));
      if (gp <= 0.01) continue;
      var x = this._sx(ndn.x), y = this._sy(ndn.y);
      var imp = ndn.imp == null ? 0.3 : ndn.imp;
      var fla = ndn.flash && ndn.flash > tnow ? (ndn.flash - tnow) / 360 : 0;
      var gs = (12 + imp * 30) * gp * (0.7 + e * 0.4 + surge * 0.3) + fla * 16;
      ctx.globalAlpha = (0.40 + imp * 0.5 + e * 0.16 + surge * 0.16) * gp + fla * 0.5;
      ctx.drawImage(this.spriteNode, x - gs / 2, y - gs / 2, gs, gs);
      ctx.globalAlpha = (0.7 + imp * 0.3) * gp;
      var dot = (ndn.r * 1.25) * gp * (1 + fla * 0.6);
      ctx.fillStyle = 'rgba(' + (192 + imp * 55) + ',' + (214 + imp * 40) + ',255,1)';
      ctx.beginPath(); ctx.arc(x, y, Math.max(0.9, dot), 0, 6.2832); ctx.fill();
    }

    // ── core ──
    var cx = this._sx(0), cy = this._sy(0);
    var coreS = (this.scale * (0.18 + e * 0.13)) * (1 + (this.reduced ? 0 : pulse * 0.16) + surge * 0.4 + flash * 0.8);
    ctx.globalCompositeOperation = 'lighter';
    ctx.globalAlpha = 0.55 + e * 0.35 + surge * 0.2;
    ctx.drawImage(this.spriteCore, cx - coreS / 2, cy - coreS / 2, coreS, coreS);
    ctx.globalAlpha = 1;
    var coreR = (3.5 + e * 4) * (1 + (this.reduced ? 0 : pulse * 0.16) + surge * 0.4);
    ctx.fillStyle = '#eef4ff';
    ctx.beginPath(); ctx.arc(cx, cy, coreR, 0, 6.2832); ctx.fill();

    // ── level-up shockwaves ──
    if (this.rings.length) {
      ctx.globalCompositeOperation = 'source-over';
      for (var ri = 0; ri < this.rings.length; ri++) {
        var rg = this.rings[ri], k = (tnow - rg.at) / rg.dur;
        if (k < 0 || k > 1) continue;
        var R = this.scale * (0.3 + k * 1.5);
        ctx.globalAlpha = (1 - k) * 0.5;
        ctx.lineWidth = Math.max(1.5, this.scale * 0.03 * (1 - k));
        ctx.strokeStyle = 'rgba(' + br[0] + ',' + br[1] + ',' + br[2] + ',1)';
        ctx.beginPath(); ctx.arc(cx, cy, R, 0, 6.2832); ctx.stroke();
      }
    }

    ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'source-over';
  };

  BrainNet.prototype.destroy = function () {
    this._running = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    if (this._ro) this._ro.disconnect();
    root.removeEventListener('resize', this._onResize);
  };

  root.BrainNet = BrainNet;
  root.RSBrainNet = BrainNet;
  if (typeof module !== 'undefined' && module.exports) module.exports = { BrainNet: BrainNet };
})(typeof window !== 'undefined' ? window : this);
