/* brain3d.js — Cerebro 3D del panel Cerebro (isla Radar). Three.js (vendado en
   static/js/vendor/three.min.js) se carga lazy SOLO al abrir Cerebro.
   API global window.RSBrain:
     mount(stageEl, state) → construye/ancla la escena y arranca el loop. Idempotente:
       si ya existe, RE-ANCLA el mismo canvas (preserva el contexto WebGL) al nuevo
       stage — necesario porque la isla reconstruye su innerHTML en cada render().
     update(state)         → actualiza knowledge (colorea/ilumina el cerebro).
     feed(type)            → lanza una partícula ('reel'|'comp'|'guio'|'metr') al cerebro.
     pause()/resume()      → para/reanuda el loop (al salir/entrar de la pestaña).
   state = { knowledge: 0..100 }.  Los contadores/% los pinta la isla (datos reales). */
(function () {
  if (window.RSBrain) return;
  var T = null, B = null;   // T=THREE, B=estado del cerebro (singleton)
  var onAbsorbCb = null;    // callback (type) que la isla pone para el toast "+1"
  var resizeBound = false;

  var COL = {
    reel: [0.43, 0.52, 1.0], comp: [0.65, 0.55, 0.98], metr: [0.20, 0.83, 0.60],
    guio: [0.96, 0.66, 0.24], dim: [0.22, 0.27, 0.55], active: [0.62, 0.80, 1.0]
  };

  function glowTex() {
    var c = document.createElement('canvas'); c.width = c.height = 64;
    var g = c.getContext('2d');
    var grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grd.addColorStop(0, 'rgba(255,255,255,1)');
    grd.addColorStop(0.28, 'rgba(255,255,255,0.7)');
    grd.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = grd; g.fillRect(0, 0, 64, 64);
    var t = new T.Texture(c); t.needsUpdate = true; return t;
  }
  function noise(x, y, z) { return Math.sin(3.1 * x + 1) * Math.sin(3.3 * y + 2) * Math.sin(2.9 * z + 3); }

  function build(stage) {
    var W = stage.clientWidth || 600, H = stage.clientHeight || 430;
    var scene = new T.Scene();
    var camera = new T.PerspectiveCamera(45, W / H, 0.1, 100); camera.position.set(0, 0, 4.4);
    var renderer = new T.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(W, H);
    var group = new T.Group(); scene.add(group);
    var gtex = glowTex();

    var N = 360, R = 1.35;
    var pos = new Float32Array(N * 3), col = new Float32Array(N * 3);
    var thr = new Float32Array(N), act = new Float32Array(N), glow = new Float32Array(N);
    for (var i = 0; i < N; i++) {
      var u = Math.random() * 2 - 1, a = Math.random() * Math.PI * 2, s = Math.sqrt(1 - u * u);
      var dx = s * Math.cos(a), dy = u, dz = s * Math.sin(a);
      var rad = R * (0.80 + 0.20 * Math.random()); rad *= 1 + 0.17 * noise(dx, dy, dz);
      pos[i * 3] = dx * rad * 1.28; pos[i * 3 + 1] = dy * rad * 0.96; pos[i * 3 + 2] = dz * rad * 1.06;
      thr[i] = Math.random(); act[i] = 0.15; glow[i] = 0;
    }
    var nodeGeo = new T.BufferGeometry();
    nodeGeo.setAttribute('position', new T.BufferAttribute(pos, 3));
    nodeGeo.setAttribute('color', new T.BufferAttribute(col, 3));
    var nodeMat = new T.PointsMaterial({ size: 0.115, map: gtex, vertexColors: true, transparent: true, blending: T.AdditiveBlending, depthWrite: false, sizeAttenuation: true });
    group.add(new T.Points(nodeGeo, nodeMat));

    // KNN (2 vecinos) en UNA pasada, sin array+sort por nodo → mucho más barato
    // (evita el lag al construir la escena al abrir Cerebro).
    var edges = [], capE = 620;
    for (var iA = 0; iA < N && edges.length < capE; iA++) {
      var ax = pos[iA * 3], ay = pos[iA * 3 + 1], az = pos[iA * 3 + 2];
      var b1 = Infinity, j1 = -1, b2 = Infinity, j2 = -1;
      for (var jB = 0; jB < N; jB++) {
        if (jB === iA) continue;
        var bx = ax - pos[jB * 3], by = ay - pos[jB * 3 + 1], bz = az - pos[jB * 3 + 2];
        var d2 = bx * bx + by * by + bz * bz;
        if (d2 < b1) { b2 = b1; j2 = j1; b1 = d2; j1 = jB; }
        else if (d2 < b2) { b2 = d2; j2 = jB; }
      }
      if (j1 >= 0 && iA < j1) edges.push([iA, j1]);
      if (j2 >= 0 && iA < j2) edges.push([iA, j2]);
    }
    var epos = new Float32Array(edges.length * 6);
    for (var e = 0; e < edges.length; e++) {
      var Ae = edges[e][0], Be = edges[e][1];
      epos[e * 6] = pos[Ae * 3]; epos[e * 6 + 1] = pos[Ae * 3 + 1]; epos[e * 6 + 2] = pos[Ae * 3 + 2];
      epos[e * 6 + 3] = pos[Be * 3]; epos[e * 6 + 4] = pos[Be * 3 + 1]; epos[e * 6 + 5] = pos[Be * 3 + 2];
    }
    var edgeGeo = new T.BufferGeometry(); edgeGeo.setAttribute('position', new T.BufferAttribute(epos, 3));
    group.add(new T.LineSegments(edgeGeo, new T.LineBasicMaterial({ color: 0x5b73f0, transparent: true, opacity: 0.14, blending: T.AdditiveBlending, depthWrite: false })));

    var haloMat = new T.SpriteMaterial({ map: gtex, color: 0x4458d8, transparent: true, opacity: 0.5, blending: T.AdditiveBlending, depthWrite: false });
    var halo = new T.Sprite(haloMat); halo.scale.set(5.2, 5.2, 1); scene.add(halo);

    var P = 70;
    var ppos = new Float32Array(P * 3), pcol = new Float32Array(P * 3);
    var pGeo = new T.BufferGeometry();
    pGeo.setAttribute('position', new T.BufferAttribute(ppos, 3));
    pGeo.setAttribute('color', new T.BufferAttribute(pcol, 3));
    group.add(new T.Points(pGeo, new T.PointsMaterial({ size: 0.16, map: gtex, vertexColors: true, transparent: true, blending: T.AdditiveBlending, depthWrite: false, sizeAttenuation: true })));

    return {
      stage: stage, scene: scene, camera: camera, renderer: renderer, group: group,
      N: N, pos: pos, col: col, thr: thr, act: act, glow: glow, nodeGeo: nodeGeo,
      edges: edges, halo: halo, haloMat: haloMat,
      P: P, ppos: ppos, pcol: pcol, pGeo: pGeo, pool: [],
      knowledge: 56, knowledgeTarget: 56, pulse: 0,
      raf: 0, t0: performance.now(), reduce: false, ambient: true, _amb: 0
    };
  }

  function spawn(type, silent) {
    if (!B) return;
    var ni = (Math.random() * B.N) | 0;
    // prefers-reduced-motion: sin vuelo — el nodo se ilumina en el sitio (absorción directa).
    if (B.reduce) { absorb({ node: ni, type: type, silent: !!silent }); return; }
    if (B.pool.length >= B.P) return;
    var u = Math.random() * 2 - 1, a = Math.random() * Math.PI * 2, s = Math.sqrt(1 - u * u);
    B.pool.push({ x: s * Math.cos(a) * 2.7, y: u * 2.7, z: s * Math.sin(a) * 2.7,
      tx: B.pos[ni * 3], ty: B.pos[ni * 3 + 1], tz: B.pos[ni * 3 + 2], node: ni, t: 0,
      sp: 0.013 + Math.random() * 0.01, c: COL[type] || COL.reel, type: type, silent: !!silent });
  }
  function absorb(p) {
    B.glow[p.node] = 1.0;
    B.pulse = Math.min(1.3, B.pulse + 0.5);   // destello de impacto del halo al alimentar
    for (var e = 0; e < B.edges.length; e++) {
      if (B.edges[e][0] === p.node) B.glow[B.edges[e][1]] = Math.max(B.glow[B.edges[e][1]], 0.55);
      else if (B.edges[e][1] === p.node) B.glow[B.edges[e][0]] = Math.max(B.glow[B.edges[e][0]], 0.55);
    }
    if (!p.silent && onAbsorbCb) { try { onAbsorbCb(p.type || 'reel'); } catch (e) {} }
  }

  function frame(now) {
    if (!B) return;
    var dt = Math.min(0.05, (now - B.t0) / 1000); B.t0 = now;
    if (!B.reduce) { B.group.rotation.y += dt * 0.18; B.group.rotation.x = Math.sin(now * 0.0002) * 0.12; }
    // easing del conocimiento: cuando sube el % real, el cerebro «se enciende» suave.
    if (Math.abs(B.knowledge - B.knowledgeTarget) > 0.05) B.knowledge += (B.knowledgeTarget - B.knowledge) * Math.min(1, dt * 2.2);
    else B.knowledge = B.knowledgeTarget;
    B.pulse *= 0.9;
    var kf = B.knowledge / 100;
    for (var i = 0; i < B.N; i++) {
      var target = kf > B.thr[i] ? 1.0 : 0.16;
      B.act[i] += (target - B.act[i]) * Math.min(1, dt * 3);
      B.glow[i] *= 0.94;
      var am = B.act[i], gl = B.glow[i];
      B.col[i * 3] = Math.min(1, COL.dim[0] + (COL.active[0] - COL.dim[0]) * am + gl * (1 - COL.active[0] * am));
      B.col[i * 3 + 1] = Math.min(1, COL.dim[1] + (COL.active[1] - COL.dim[1]) * am + gl * (1 - COL.active[1] * am));
      B.col[i * 3 + 2] = Math.min(1, COL.dim[2] + (COL.active[2] - COL.dim[2]) * am + gl);
    }
    B.nodeGeo.attributes.color.needsUpdate = true;
    var wc = 0;
    for (var p = B.pool.length - 1; p >= 0; p--) {
      var P2 = B.pool[p]; P2.t += P2.sp;
      var tt = P2.t < 1 ? (P2.t * P2.t * (3 - 2 * P2.t)) : 1;
      if (P2.t >= 1) { absorb(P2); B.pool.splice(p, 1); continue; }
      B.ppos[wc * 3] = P2.x + (P2.tx - P2.x) * tt; B.ppos[wc * 3 + 1] = P2.y + (P2.ty - P2.y) * tt; B.ppos[wc * 3 + 2] = P2.z + (P2.tz - P2.z) * tt;
      B.pcol[wc * 3] = P2.c[0]; B.pcol[wc * 3 + 1] = P2.c[1]; B.pcol[wc * 3 + 2] = P2.c[2]; wc++;
    }
    B.pGeo.setDrawRange(0, wc);
    B.pGeo.attributes.position.needsUpdate = true; B.pGeo.attributes.color.needsUpdate = true;
    B.haloMat.opacity = 0.30 + kf * 0.28 + Math.sin(now * 0.002) * 0.05 + B.pulse * 0.45;
    B.renderer.render(B.scene, B.camera);
    B.raf = requestAnimationFrame(frame);
  }

  function resize() {
    if (!B) return;
    var W = B.stage.clientWidth || 600, H = B.stage.clientHeight || 430;
    B.camera.aspect = W / H; B.camera.updateProjectionMatrix(); B.renderer.setSize(W, H);
  }

  function startAmbient() {
    if (!B || B._amb || !B.ambient) return;
    // ambiente: goteo suave de inputs (silencioso, sin toast) para que el cerebro "viva".
    B._amb = setInterval(function () {
      if (B && B.ambient && document.body.contains(B.stage)) spawn(['reel', 'comp', 'guio', 'metr'][(Math.random() * 4) | 0], true);
    }, 1800);
  }
  function stopAmbient() { if (B && B._amb) { clearInterval(B._amb); B._amb = 0; } }

  window.RSBrain = {
    mount: function (stage, state) {
      T = window.THREE; if (!T || !stage) return false;
      if (B) {
        // re-ancla el canvas existente (preserva WebGL) al nuevo stage.
        if (B.renderer.domElement.parentNode !== stage) { stage.appendChild(B.renderer.domElement); B.stage = stage; resize(); }
      } else {
        B = build(stage);
        stage.appendChild(B.renderer.domElement);
      }
      B.reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
      if (state && state.ambient === false) B.ambient = false;
      if (!resizeBound) { window.addEventListener('resize', resize); resizeBound = true; }
      if (state) this.update(state);
      this.resume();
      startAmbient();
      return true;
    },
    update: function (state) {
      if (B && state && typeof state.knowledge === 'number') B.knowledgeTarget = Math.max(0, Math.min(100, state.knowledge));
    },
    // type ∈ reel|comp|guio|metr. silent=true → sin toast "+1" (uso interno/ambiente).
    feed: function (type, silent) { spawn(type, silent); },
    burst: function () { for (var i = 0; i < 6; i++) (function (d) { setTimeout(function () { spawn(['reel', 'comp', 'guio', 'metr'][(Math.random() * 4) | 0], true); }, d * 90); })(i); },
    // destello global al subir de nivel: enciende nodos al azar + pulso de halo.
    levelup: function () {
      if (!B) return;
      B.pulse = Math.min(1.3, B.pulse + 0.9);
      for (var i = 0; i < B.N; i++) if (Math.random() < 0.5) B.glow[i] = Math.max(B.glow[i], 0.5 + Math.random() * 0.5);
    },
    setOnAbsorb: function (fn) { onAbsorbCb = (typeof fn === 'function') ? fn : null; },
    setAmbient: function (on) { if (!B) return; B.ambient = !!on; if (B.ambient) startAmbient(); else stopAmbient(); },
    pause: function () { if (B && B.raf) { cancelAnimationFrame(B.raf); B.raf = 0; } },
    resume: function () { if (B && !B.raf) { B.t0 = performance.now(); B.raf = requestAnimationFrame(frame); } },
    // unmount real: limpia loop, ambiente, listener, geometrías/materiales/texturas. Sin fugas.
    dispose: function () {
      if (!B) return;
      this.pause(); stopAmbient();
      if (resizeBound) { try { window.removeEventListener('resize', resize); } catch (e) {} resizeBound = false; }
      try {
        B.scene.traverse(function (o) {
          if (o.geometry) o.geometry.dispose();
          if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); }
        });
        if (B.haloMat) { if (B.haloMat.map) B.haloMat.map.dispose(); B.haloMat.dispose(); }
        B.renderer.dispose();
        var dom = B.renderer.domElement;
        if (dom && dom.parentNode) dom.parentNode.removeChild(dom);
      } catch (e) {}
      onAbsorbCb = null; B = null;
    }
  };
})();
