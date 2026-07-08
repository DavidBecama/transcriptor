#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   verify-island.mjs — harness funcional de la isla .rs (Signal / RadarLoop)
   Cero dependencias: Chrome headless + CDP por WebSocket nativo (Node ≥22).

   Uso:  node scripts/verify-island.mjs [URL_BASE]
         (por defecto http://localhost:3100 — la app debe correr con DEMO_MODE=1)

   Qué verifica (docs/TODO-mejoras-ux-idi.md · Verificación final):
   - La isla monta y renderiza el feed sin errores de consola (demo).
   - Deep-links del demo (?plan=creador/agencia, ?t=, ?b=) montan.
   - T1: un solo .btn-primary sobre el fold del Dashboard ("Hazlo mío").
   - T2: los flujos abren sheet propio (sin window.prompt).
   - T3: Esc cierra overlays; Enter envía la idea.
   - T4: toast con aria-live; error persistente.
   - T5: overlays role=dialog + foco dentro.
   ═══════════════════════════════════════════════════════════════════════════ */
import { spawn, execSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.argv[2] || "http://localhost:3100";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9222 + Math.floor(Math.random() * 500);

// Watchdog: si algo cuelga (CDP mudo, promesa sin resolver), salimos con código 3
// en vez de quedarnos vivos para siempre. stderr no se bufferiza → trazas visibles.
const WATCHDOG_MS = 150000;
const watchdog = setTimeout(() => { console.error("WATCHDOG: harness colgado >150s — abortando"); process.exit(3); }, WATCHDOG_MS);
watchdog.unref?.();
const trace = (m) => process.stderr.write(`· ${m}\n`);

// Pase lo que pase (watchdog, kill, error), Chrome muere con el proceso.
// Sin esto, cada run abortado deja un Chrome headless huérfano comiendo CPU
// y los siguientes runs se vuelven glaciales (los evaluate caducan en cascada).
process.on("exit", () => { try { chrome.kill("SIGKILL"); } catch {} });
process.on("SIGINT", () => process.exit(130));
process.on("SIGTERM", () => process.exit(143));

let passed = 0, failed = 0;
const fails = [];
function check(name, ok, extra) {
  if (ok) { passed++; console.log(`  ✓ ${name}`); }
  else { failed++; fails.push(name + (extra ? ` — ${extra}` : "")); console.log(`  ✗ ${name}${extra ? " — " + extra : ""}`); }
}

/* ── CDP plumbing ──────────────────────────────────────────────────────── */
let ws, msgId = 0;
const pending = new Map();
const events = [];
function send(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++msgId;
    // Si Chrome no responde (renderer crasheado, WS muerto) el harness no se
    // queda colgado: cada llamada CDP caduca a los 15s con el método en el error.
    const t = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    pending.set(id, { resolve: (v) => { clearTimeout(t); resolve(v); }, reject: (e) => { clearTimeout(t); reject(e); } });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
}
function connect(url) {
  return new Promise((resolve, reject) => {
    ws = new WebSocket(url);
    ws.onopen = () => resolve();
    ws.onerror = (e) => reject(e);
    ws.onmessage = (m) => {
      const d = JSON.parse(m.data);
      if (d.id && pending.has(d.id)) {
        const { resolve, reject } = pending.get(d.id);
        pending.delete(d.id);
        d.error ? reject(new Error(d.error.message)) : resolve(d.result);
      } else if (d.method) {
        events.push(d);
        // Un window.prompt/alert nativo BLOQUEA el renderer (y con él, todo
        // Runtime.evaluate posterior) → auto-descartar para no colgar el harness.
        if (d.method === "Page.javascriptDialogOpening") {
          trace("diálogo JS nativo detectado → auto-descartado (" + (d.params?.type || "?") + ")");
          send("Page.handleJavaScriptDialog", { accept: false }, d.sessionId).catch(() => {});
        }
        if (d.method === "Inspector.targetCrashed" || d.method === "Target.targetCrashed") trace("⚠ TARGET CRASHED");
      }
    };
    ws.onclose = () => {
      trace("⚠ WS cerrado");
      for (const { reject } of pending.values()) reject(new Error("WS cerrado"));
      pending.clear();
    };
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let sid; // session id de la pestaña
async function evaluate(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }, sid);
  if (r.exceptionDetails) throw new Error("eval: " + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
}
async function nav(url) {
  events.length = 0;
  try { await send("Page.navigate", { url }, sid); }
  catch (e) { trace("nav: " + e.message); return false; }
  // espera a que la isla monte (#radarRoot con contenido)
  for (let i = 0; i < 60; i++) {
    await sleep(250);
    try {
      const ready = await evaluate(`!!document.querySelector('#radarRoot .cmd')`); // v3: cada página tiene su header propio (no .phead); .cmd (command bar) = vista real montada
      if (ready) { await sleep(400); return true; }
    } catch {}
  }
  return false;
}
async function key(keyName, modifiers = 0) {
  const defs = { Escape: { key: "Escape", code: "Escape", keyCode: 27 }, Enter: { key: "Enter", code: "Enter", keyCode: 13 } };
  const d = defs[keyName];
  for (const type of ["rawKeyDown", "keyUp"])
    await send("Input.dispatchKeyEvent", { type, key: d.key, code: d.code, windowsVirtualKeyCode: d.keyCode, nativeVirtualKeyCode: d.keyCode, modifiers }, sid);
}
async function click(selector) {
  return evaluate(`(function(){ var el=document.querySelector(${JSON.stringify(selector)}); if(!el) return false; el.click(); return true; })()`);
}
const consoleErrors = () => events.filter((e) => e.method === "Runtime.exceptionThrown").map((e) => e.params?.exceptionDetails?.exception?.description || "exception");

/* ── arranque de Chrome ───────────────────────────────────────────────── */
const profile = mkdtempSync(join(tmpdir(), "rs-verify-"));
const chrome = spawn(CHROME, [
  `--headless=new`, `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  `--no-first-run`, `--no-default-browser-check`, `--disable-extensions`, `--window-size=1440,900`,
  // headless=new throttlea timers/raf de pestañas "ocultas" → la isla (setTimeout
  // teatral, polling) se congela y los evaluate se quedan sin responder. Apagarlo.
  `--disable-background-timer-throttling`, `--disable-renderer-backgrounding`,
  `--disable-backgrounding-occluded-windows`, `--disable-ipc-flooding-protection`,
  "about:blank",
], { stdio: "ignore" });

async function main() {
  // espera al endpoint CDP
  let info;
  trace("esperando CDP en :" + PORT);
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    try { info = await (await fetch(`http://127.0.0.1:${PORT}/json/version`, { signal: AbortSignal.timeout(2000) })).json(); break; } catch {}
  }
  if (!info) throw new Error("Chrome CDP no responde");
  trace("CDP listo, conectando WS");
  await connect(info.webSocketDebuggerUrl);
  trace("WS conectado");

  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  ({ sessionId: sid } = await send("Target.attachToTarget", { targetId, flatten: true }));
  await send("Page.enable", {}, sid);
  await send("Runtime.enable", {}, sid);
  // Sin GPU, las animaciones infinitas (orbe conic-gradient) saturan el main
  // thread del renderer headless y los Runtime.evaluate caducan. Para el harness
  // las matamos: probamos lógica/estado, no la estética del movimiento.
  await send("Page.addScriptToEvaluateOnNewDocument", { source:
    `document.addEventListener('DOMContentLoaded',function(){var s=document.createElement('style');s.textContent='*,*::before,*::after{animation:none!important;transition:none!important}';document.head.appendChild(s);});`
  }, sid);
  trace("target listo");

  /* ═══ 1. Montaje base (demo creador) ═══ */
  console.log("\n■ Montaje base (?plan=creador)");
  // OJO: entrar por /es/ pierde la query (el shim demo hace replaceState a /profile/radar).
  // Los deep-links solo sobreviven entrando directo por /profile/radar?….
  const mounted = await nav(`${BASE}/profile/radar?plan=creador`);
  check("la isla monta (rail presente)", mounted);
  check("feed/dashboard renderiza", await evaluate(`!!document.querySelector('#radarRoot .feature, #radarRoot .rs-empty')`));
  check("sin excepciones JS", consoleErrors().length === 0, consoleErrors()[0]);

  /* ═══ T1: una sola acción primaria sobre el fold ═══ */
  console.log("\n■ T1 · acción primaria única");
  const t1 = await evaluate(`(function(){
    var work=document.querySelector('#radarRoot .work'); if(!work) return {err:'no work'};
    var fold=window.innerHeight;
    var prim=[].slice.call(work.querySelectorAll('.btn-primary')).filter(function(b){
      var r=b.getBoundingClientRect();
      // visible de verdad: dentro del fold vertical Y horizontal (los slides 2/3 del
      // carrusel de oportunidades están desplazados fuera de pantalla a la derecha).
      return r.top < fold && r.bottom > 0 && r.width>0 && r.left < window.innerWidth && r.right > 0;
    });
    return { n: prim.length, labels: prim.map(function(b){return b.textContent.trim();}) };
  })()`);
  check("único .btn-primary sobre el fold = «Roba la idea»", t1.n === 1 && /Roba la idea/.test(t1.labels?.[0] || ""), JSON.stringify(t1));

  /* ═══ T3: bombilla → modal → Enter «Guardar idea» (gratis) → en Guiones › Sin desarrollar ═══ */
  console.log("\n■ T3 · teclado");
  await click('[data-act="idea-capture"]'); await sleep(250);
  await evaluate(`(function(){ var i=document.getElementById('rsSheetInput'); if(i){ i.value='Idea de prueba desde harness'; i.focus(); } })()`);
  await key("Enter");        // Enter → acción primaria GRATIS «Guardar idea»
  await sleep(400);
  await click('[data-act="tab"][data-k="guiones"]'); await sleep(300);
  const onIdeas = await evaluate(`!!document.querySelector('#radarRoot .guiu') && document.body.textContent.indexOf('Idea de prueba desde harness')>-1`);
  check("bombilla → «Guardar idea» (gratis) aparece en Guiones", onIdeas);

  // overlay + Esc: robar desde Dashboard
  trace("T3: tab dashboard");
  await click('[data-act="tab"][data-k="dashboard"]'); await sleep(250);
  trace("T3: click steal");
  await click('.feature [data-act="steal"]'); await sleep(300);
  trace("T3: check overlay");
  const hasOverlay = await evaluate(`!!document.querySelector('#radarRoot .overlay')`);
  check("robar abre overlay (gen)", hasOverlay);
  await sleep(2200); // deja terminar la espera teatral demo (1.7s)
  trace("T3: check reveal");
  const reveal = await evaluate(`!!document.querySelector('#radarRoot .script-hook')`);
  check("reveal del guión llega", reveal);
  await key("Escape"); await sleep(300);
  check("Esc cierra el overlay", await evaluate(`!document.querySelector('#radarRoot .overlay')`));

  /* ═══ T5: overlay como diálogo accesible ═══ */
  console.log("\n■ T5 · diálogo accesible");
  // Esc desde el reveal ahora aterriza en el workspace de la idea (Ideas robadas),
  // no en el feed: volvemos al Radar antes de re-robar.
  await click('[data-act="tab"][data-k="dashboard"]'); await sleep(250);
  await click('.feature [data-act="steal"]'); await sleep(400);
  const dlg = await evaluate(`(function(){
    var o=document.querySelector('#radarRoot .overlay'); if(!o) return {};
    return { role:o.getAttribute('role'), modal:o.getAttribute('aria-modal'),
             focusInside: o.contains(document.activeElement) };
  })()`);
  check("overlay role=dialog + aria-modal", dlg.role === "dialog" && dlg.modal === "true", JSON.stringify(dlg));
  check("foco dentro del overlay al abrir", !!dlg.focusInside);
  await key("Escape"); await sleep(250);

  /* ═══ T2: sheets sin window.prompt ═══ */
  console.log("\n■ T2 · promptSheet");
  trace("T2: nav fresco");
  await nav(`${BASE}/profile/radar?plan=creador`);   // estado limpio (sin overlays colgando de checks previos)
  trace("T2: nav ok, override prompt");
  await evaluate(`window.__promptCalled=false; window.prompt=function(){ window.__promptCalled=true; return null; };`);
  trace("T2: click add-reel");
  await click('[data-act="add-reel"]'); await sleep(300);
  trace("T2: click ok");
  const sheet = await evaluate(`(function(){
    var s=document.querySelector('#radarRoot .sheet, #radarRoot .overlay');
    return { open: !!s, hasField: !!(s&&s.querySelector('input,textarea')), prompt: window.__promptCalled };
  })()`);
  check("«Añadir reel» abre sheet con campo (sin window.prompt)", sheet.open && sheet.hasField && !sheet.prompt, JSON.stringify(sheet));
  await key("Escape"); await sleep(200);

  /* ═══ T4: toast accesible + error persistente ═══ */
  console.log("\n■ T4 · toast accesible");
  const toast = await evaluate(`(function(){
    var t=document.getElementById('rsToast'); if(!t) return {};
    return { role:t.getAttribute('role'), live:t.getAttribute('aria-live') };
  })()`);
  check("#rsToast role=status + aria-live", toast.role === "status" && toast.live === "polite", JSON.stringify(toast));
  const err = await evaluate(`(function(){
    var t=document.getElementById('rsErr'); if(!t) return {};
    t.classList.add('show');                                  // simula un error visible
    var stillThere = t.classList.contains('show');            // no se auto-oculta
    var btn=t.querySelector('[data-act="err-close"]'); if(btn) btn.click();
    return { role:t.getAttribute('role'), live:t.getAttribute('aria-live'), persiste:stillThere, cierra:!t.classList.contains('show') };
  })()`);
  check("#rsErr role=alert + assertive, persiste y cierra con su botón", err.role === "alert" && err.live === "assertive" && err.persiste && err.cierra, JSON.stringify(err));
  // Fix review (a11y): las regiones live deben ser nodos ESTABLES — si render() las
  // recreara, el lector de pantalla no anunciaría el patrón render()+showToast().
  const stable = await evaluate(`(function(){
    window.__rsT=document.getElementById('rsToast');
    var f=document.querySelector('#radarRoot .fchip'); if(f) f.click();   // fuerza un render()
    return document.getElementById('rsToast')===window.__rsT;
  })()`);
  check("#rsToast sobrevive a un re-render (nodo estable, aria-live fiable)", stable === true, String(stable));
  // Fix review (T2/T6): el texto sin enviar de un sheet sobrevive a un render() de fondo.
  await click('[data-act="add-reel"]'); await sleep(250);
  const kept = await evaluate(`(function(){
    var i=document.getElementById('rsSheetInput'); if(!i) return 'no-sheet';
    i.value='texto a medio escribir';
    var f=document.querySelector('#radarRoot .rail-btn'); if(f) f.click();   // render() de fondo (no cambia de tab: ya activo)
    var i2=document.getElementById('rsSheetInput');
    return i2?i2.value:'gone';
  })()`);
  check("sheet conserva el texto tecleado tras un re-render", kept === "texto a medio escribir", String(kept));
  await key("Escape"); await sleep(200);

  /* ═══ deep-links ═══ */
  console.log("\n■ Deep-links demo");
  for (const q of ["?plan=agencia", "?plan=creador&t=ideas", "?plan=creador&t=guiones", "?plan=creador&t=metrics", "?plan=creador&t=brain", "?plan=agencia&t=team", "?plan=creador&t=perf", "?plan=agencia&b=b2"]) {
    const ok = await nav(`${BASE}/profile/radar${q}`);
    const errs = consoleErrors();
    check(`monta ${q}`, ok && errs.length === 0, errs[0]);
  }

  /* ═══ Fix review (T6): re-robar un reel en vuelo NO duplica el guion ═══ */
  console.log("\n■ T6 · robo en vuelo sin duplicados");
  await nav(`${BASE}/profile/radar?plan=creador&t=guiones`);
  const g0 = await evaluate(`document.querySelectorAll('#radarRoot .guic').length`);
  await click('[data-act="tab"][data-k="dashboard"]'); await sleep(200);
  await click('.feature [data-act="steal"]'); await sleep(150);
  await key("Escape"); await sleep(150);              // manda el robo a background
  await click('.feature [data-act="steal"]'); await sleep(2500);   // re-robo del MISMO reel + espera resolución
  const dup = await evaluate(`(function(){
    var reveal=!!document.querySelector('#radarRoot .script-hook');
    return { reveal: reveal };
  })()`);
  await key("Escape"); await sleep(150);
  await click('[data-act="tab"][data-k="guiones"]'); await sleep(250);
  const g1 = await evaluate(`document.querySelectorAll('#radarRoot .guic').length`);
  check("re-robo del mismo reel en vuelo → 1 solo guion nuevo (y reveal)", dup.reveal && g1 === g0 + 1, JSON.stringify({ g0, g1, reveal: dup.reveal }));

  /* ═══ Loop completo (los 4 momentos): despertar → robo → reveal → grabar ═══ */
  console.log("\n■ Loop completo demo");
  await nav(`${BASE}/profile/radar?plan=creador`);
  await click('.feature [data-act="steal"]'); await sleep(2300);   // espera teatral 1.7s
  const reveal2 = await evaluate(`!!document.querySelector('#radarRoot .script-hook')`);
  await click('[data-act="chain"][data-k="record"]'); await sleep(600);
  // teleprompter UNIFICADO: el loop abre el teleprompter REAL (#tpOverlay), NO el mock (.overlay.prompter).
  const prompter = await evaluate(`(function(){var ov=document.getElementById('tpOverlay');return !!(ov&&ov.classList.contains('on')) && !document.querySelector('#radarRoot .overlay.prompter');})()`);
  await click('#tpDoneBtn'); await sleep(400);   // «Ya lo grabé» (botón de modo loop → recorded())
  const closed = await evaluate(`(function(){
    var ov=document.getElementById('tpOverlay');
    return { feed: !(ov&&ov.classList.contains('on')),
             toast: (document.getElementById('rsToastMsg')||{}).textContent||"" };
  })()`);
  check("robo → reveal → teleprompter REAL → «Ya lo grabé» cierra el loop",
    reveal2 && prompter && closed.feed && /Grabado/.test(closed.toast),
    JSON.stringify({ reveal2, prompter, closed }));

  /* ═══ Ideas robadas: robo → workspace directo + agrupación por reel ═══ */
  console.log("\n■ Ideas robadas · flujo directo + agrupación");
  await nav(`${BASE}/profile/radar?plan=creador`);
  await click('.feature [data-act="steal"]'); await sleep(2300);
  await key("Escape"); await sleep(300);   // cerrar el reveal → workspace de la idea
  const ws = await evaluate(`(function(){
    return { ws: !!document.querySelector('#radarRoot .ws-canvas'),
             notes: !!document.getElementById('rsWsNotes'),
             cards: document.querySelectorAll('#radarRoot .ws-canvas .guic').length };
  })()`);
  check("Esc tras el reveal aterriza en el workspace de la idea (reel + notas + guion)", ws.ws && ws.notes && ws.cards === 1, JSON.stringify(ws));
  await click('[data-act="ws-steal-again"]'); await sleep(2300);   // 2º guion del MISMO reel
  await key("Escape"); await sleep(300);
  const ws2 = await evaluate(`(function(){
    var cards=document.querySelectorAll('#radarRoot .ws-canvas .guic').length;
    var b=document.querySelector('#radarRoot [data-act="ws-close"]'); if(b) b.click();
    return { cards: cards, groups: document.querySelectorAll('#radarRoot .igc').length };
  })()`);
  check("«Robar otro guion» agrupa: mismo reel → 1 idea con 2 guiones", ws2.cards === 2 && ws2.groups === 1, JSON.stringify(ws2));

  /* ═══ P1: robo en background → elección pendiente → reveal al abrir ═══ */
  console.log("\n■ P1 · elección pendiente tras robo en background");
  await nav(`${BASE}/profile/radar?plan=creador`);
  await click('.feature [data-act="steal"]'); await sleep(150);
  await key("Escape"); await sleep(2500);            // background → resuelve y avisa
  await click('#rsToastAct'); await sleep(400);      // «Elegir mi versión»
  const p1 = await evaluate(`(function(){
    return { reveal: !!document.querySelector('#radarRoot .script-hook'),
             opts: document.querySelectorAll('#radarRoot .opt-tab').length };
  })()`);
  check("robo en background → «Elegir mi versión» presenta el reveal con opciones", p1.reveal && p1.opts >= 2, JSON.stringify(p1));

  /* ═══ T9: deshacer al descartar guion ═══ */
  console.log("\n■ T9 · deshacer descarte");
  await nav(`${BASE}/profile/radar?plan=creador&t=guiones`);
  const antes = await evaluate(`document.querySelectorAll('#radarRoot .guic').length`);
  await click('[data-act="gui-discard"]'); await sleep(250);
  const tras = await evaluate(`document.querySelectorAll('#radarRoot .guic').length`);
  await click('#rsToastAct'); await sleep(250);
  const t9 = await evaluate(`(function(){
    var n=document.querySelectorAll('#radarRoot .guic').length;
    var pill=document.querySelector('#radarRoot .guic-badge'); // primera card = la restaurada
    return { n:n, pill:pill?pill.textContent.trim():null };
  })()`);
  check("descartar quita la card y «Deshacer» la restaura a «Por grabar»",
    antes > 0 && tras === antes - 1 && t9.n === antes && t9.pill === "Por grabar",
    JSON.stringify({ antes, tras, despues: t9 }));

  /* ═══ T8: targets táctiles en móvil + dato no dependiente del color ═══ */
  console.log("\n■ T8 · móvil y colorblind");
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true }, sid);
  await nav(`${BASE}/profile/radar?plan=creador`);
  const t8 = await evaluate(`(function(){
    var rootEl=document.querySelector('#radarRoot');
    var out={ mobile: rootEl && rootEl.className.indexOf('rs--mobile')>-1, cortos:[] };
    ['.fchip','.btn-sm','.iconbtn','.idea-caret','.chip-sm'].forEach(function(sel){
      [].slice.call(document.querySelectorAll('#radarRoot '+sel)).slice(0,8).forEach(function(b){
        var h=b.getBoundingClientRect().height; if(h>0 && h<43.5) out.cortos.push(sel+':'+Math.round(h));
      });
    });
    return out;
  })()`);
  check("móvil: targets táctiles ≥44px", t8.mobile && t8.cortos.length === 0, JSON.stringify(t8));
  // El reel explosivo debe leerse por ETIQUETA (texto), no solo por color (colorblind).
  // Tras Radar v2 los más explosivos viven en el card destacado (carrusel), no en la
  // lista: vale la etiqueta del card (.opp-mega «el más explosivo» / .feature-data
  // .dmetric.big con label «Explosión» + multiplicador ×) O la del row (.row-score.hi
  // .sx «explota»). Cualquiera satisface la regla (canal redundante al color).
  const t8b = await evaluate(`(function(){
    var rowSx=document.querySelector('#radarRoot .row-score.hi .sx');
    if(rowSx && /explota/i.test(rowSx.textContent)) return 'row:'+rowSx.textContent.trim();
    var mega=document.querySelector('#radarRoot .feature .opp-mega');
    if(mega && /explosiv/i.test(mega.textContent)) return 'feat:'+mega.textContent.trim();
    var big=document.querySelector('#radarRoot .feature-data .dmetric.big');
    if(big){ var dk=big.querySelector('.dk'), dv=big.querySelector('.dv');
      if(dk && /explos/i.test(dk.textContent) && dv && /×/.test(dv.textContent)) return 'data:'+dk.textContent.trim()+' '+dv.textContent.trim(); }
    return null;
  })()`);
  check("reel explosivo identificable por etiqueta (no solo color)", !!t8b, String(t8b));
  // Fix review (T8): el botón cerrar de los overlays también debe llegar a 44px.
  await click('.feature [data-act="steal"]'); await sleep(400);
  const backH = await evaluate(`(function(){ var b=document.querySelector('#radarRoot .overlay .obar .back'); return b?Math.round(b.getBoundingClientRect().height):0; })()`);
  check("móvil: botón cerrar/volver de overlay ≥44px", backH >= 44, String(backH));
  await key("Escape"); await sleep(2000);   // deja resolver el robo demo en background
  // v3 (mockup David): los tabs de marca se quitaron del Radar; el cambio de marca
  // vive en el selector de la barra superior (.brand-switch). Comprobamos que existe
  // y es target táctil ≥44px en agencia móvil.
  await nav(`${BASE}/profile/radar?plan=agencia&b=b1`);
  const bsw = await evaluate(`(function(){ var b=document.querySelector('#radarRoot .brand-switch'); return b?Math.round(b.getBoundingClientRect().height):0; })()`);
  check("móvil agencia: selector de marca ≥44px", bsw >= 44, String(bsw));
  await send("Emulation.clearDeviceMetricsOverride", {}, sid);

  /* ═══ Contrato B1 · ninguna marca nace MUDA (señales suficientes o estado honesto) ═══
     Recorre TODAS las marcas del portfolio de agencia. Cada una debe servir O bien señales
     (reels/sugerencias/competidores en pantalla) O bien el estado HONESTO «poblando tu radar»
     (nicho aún sin pool). Se pone ROJO si una marca renderiza un dashboard en blanco. La marca
     b5 (nicho fino recién creado, 0 reels/0 competidores) es la trampa: debe caer en el estado
     honesto, nunca en blanco. */
  console.log("\n■ B1 · ninguna marca servida muda");
  for (const b of ["b1", "b2", "b3", "b4", "b5"]) {
    await nav(`${BASE}/profile/radar?plan=agencia&b=${b}`);
    await sleep(250);
    const st = await evaluate(`(function(){
      var root=document.querySelector('#radarRoot');
      var signals=root.querySelectorAll('.feature, .row, .stday-card, .disc-card, .pcomp-card').length;
      var pop=root.querySelector('.stday-pop-sec, .seed-banner');
      return { signals: signals, honest: !!pop };
    })()`);
    check(`marca ${b}: señales suficientes o estado honesto (no muda)`, st.signals >= 2 || st.honest, JSON.stringify(st));
  }
  // La marca de nicho fino (b5) DEBE resolver en el estado honesto «poblando», no en blanco.
  await nav(`${BASE}/profile/radar?plan=agencia&b=b5`);
  await sleep(250);
  const muteState = await evaluate(`(function(){
    var pop=document.querySelector('#radarRoot .stday-pop-sec');
    return { honest: !!pop, txt: (pop&&pop.textContent||'').replace(/\\s+/g,' ').trim().slice(0,90) };
  })()`);
  check("marca muda (b5) muestra estado honesto «poblando tu radar»",
        muteState.honest && /poblando|populating/i.test(muteState.txt), JSON.stringify(muteState));

  /* ═══ Contrato David · «Refrescar ahora» NO cobra por vacío ═══
     Un refresh que no trae nada nuevo debe mostrar mensaje HONESTO («no hay nada nuevo, vuelve
     mañana») y NO tocar los créditos. Se fuerza el caso «sin novedades» con ?refresh=empty. Rojo
     si el refresh vacío muestra «Radar actualizado» (falso positivo) o si cambian los créditos. */
  console.log("\n■ Refresh honesto · no cobrar por vacío");
  await nav(`${BASE}/profile/radar?plan=creador&refresh=empty`);
  const credBefore = await evaluate(`(function(){var m=document.body.textContent.match(/([\\d.]+)\\s*cr[eé]ditos/i);return m?m[1]:null;})()`);
  const clicked = await click('#radarRoot [data-act="refresh-radar"]');
  await sleep(500);
  const refreshOut = await evaluate(`(function(){
    var t=document.getElementById('rsToast');
    var m=document.body.textContent.match(/([\\d.]+)\\s*cr[eé]ditos/i);
    return { toast:(t&&t.textContent||'').trim().slice(0,90), creds:(m?m[1]:null) };
  })()`);
  const honestMsg = /nada nuevo|vuelve mañana|nothing new|come back tomorrow/i.test(refreshOut.toast);
  const notFakeSuccess = !/radar actualizado|radar (updated|refreshed)/i.test(refreshOut.toast);
  check("refresh sin novedades: mensaje honesto (no «actualizado»)",
        clicked && honestMsg && notFakeSuccess, JSON.stringify(refreshOut));
  check("refresh sin novedades: créditos sin cambio (no cobra por vacío)",
        credBefore !== null && refreshOut.creds === credBefore, `${credBefore} -> ${refreshOut.creds}`);

  /* ═══ Contrato David 08/07 · sugerencias NUNCA dead-end ═══
     Estado agotado del carrusel: FUERA el dead-end «vuelve mañana» y el «Refrescar sugerencias 5cr».
     Único control de ampliar = «Ver más · 3cr» (data-act=sugg-more, scrapea competidores related
     NUEVOS; reembolso si 0). Rojo si aparece el dead-end o el viejo refresh-pool. */
  console.log("\n■ Sugerencias agotadas · sin dead-end, ofrece «Ver más · 3cr»");
  for (const q of ["sugg=exhausted", "sugg=exhausted-norefresh"]) {
    await nav(`${BASE}/profile/radar?plan=creador&${q}`);
    const exh = await evaluate(`(function(){
      var root=document.querySelector('#radarRoot');
      var sec=root.querySelector('.stday-sec');
      return { moreBtn: !!root.querySelector('[data-act="sugg-more"]'),
               oldPay: !!root.querySelector('[data-act="refresh-pool"]'),
               deadEnd: /vuelve mañana|come back tomorrow/i.test((sec&&sec.textContent)||'') };
    })()`);
    check(`${q}: ofrece «Ver más · 3cr» y NO dead-end/refresh-pool`,
          exh.moreBtn && !exh.oldPay && !exh.deadEnd, JSON.stringify(exh));
  }

  /* ═══ Feature David · «Guardar idea con datos» (de pago, junto a «Robar guion») ═══
     Botón hermano de «Robar» que guarda el reel + datos sin generar guión. Rojo si no aparece
     junto a «Robar» o si al pulsarlo no confirma el guardado. */
  console.log("\n■ Guardar idea con datos (junto a «Robar»)");
  await nav(`${BASE}/profile/radar?plan=creador`);
  const siBtn = await evaluate(`(function(){
    var root=document.querySelector('#radarRoot');
    return { hasSave: !!root.querySelector('[data-act="save-idea-data"]'),
             hasSteal: !!root.querySelector('[data-act="steal"]') };
  })()`);
  check("«Guardar idea» aparece junto a «Robar guion»", siBtn.hasSave && siBtn.hasSteal, JSON.stringify(siBtn));
  await click('#radarRoot [data-act="save-idea-data"]');
  await sleep(400);
  const saved = await evaluate(`(function(){
    var t=document.getElementById('rsToast');
    return { toast:(t&&t.textContent||'').trim().slice(0,70) };
  })()`);
  check("«Guardar idea» confirma el guardado con datos", /guardada|saved/i.test(saved.toast), JSON.stringify(saved));
  // #2 (David): la idea guardada DEBE aparecer en su lista (Guiones → Ideas robadas). El bug
  // era que la inserción optimista no llevaba _brand → invisible en marcas ≠ default. Navega a
  // Guiones y comprueba que hay al menos un grupo de «Ideas robadas» (la que acabamos de guardar).
  await click('#radarRoot [data-act="goto-saved-ideas"]');
  await sleep(500);
  const inList = await evaluate(`(function(){
    var root=document.querySelector('#radarRoot');
    return { tab: !!root.querySelector('.igc, .ws-canvas, .idea-group, [data-act="ws-open"]'),
             groups: root.querySelectorAll('.igc, .idea-group').length };
  })()`);
  check("idea guardada APARECE en su lista (Guiones · Ideas robadas)", inList.tab || inList.groups > 0, JSON.stringify(inList));

  /* ═══ #5 (David) · AISLAMIENTO POR MARCA — fuga entre clientes (crítico agencia) ═══
     El demo siembra una CANARY: un guion robado de OTRA marca (_brand ajeno). stolenGroups DEBE
     filtrarlo → NUNCA aparece en «Ideas robadas» de la marca activa. Rojo si se cuela (fuga). */
  console.log("\n■ #5 · aislamiento por marca (cliente A no ve lo de cliente B)");
  await nav(`${BASE}/profile/radar?plan=agencia&t=guiones`);
  await sleep(400);
  const iso = await evaluate(`(function(){
    var root=document.querySelector('#radarRoot');
    return { leak: /CANARY|fuga entre marcas/i.test(root.textContent||'') };
  })()`);
  check("guion robado de OTRA marca NO aparece (sin fuga entre clientes)", !iso.leak, JSON.stringify(iso));

  /* ═══ #5 CLASE (David) · aislamiento de «Análisis guardados» por marca ═══
     La página Analizar mostraba análisis de OTRAS marcas (en IA salían moda/salud/viajes). El demo
     siembra una CANARY (análisis de marca ajena) que DEBE filtrarse. Rojo si se cuela. */
  console.log("\n■ #5 clase · aislamiento «Análisis guardados» (marca)");
  await nav(`${BASE}/profile/radar?plan=agencia&t=analizar`);
  await sleep(400);
  const anzIso = await evaluate(`(function(){
    var root=document.querySelector('#radarRoot');
    return { leak: /CANARY|análisis de otra marca|fuga entre clientes/i.test(root.textContent||''),
             cards: root.querySelectorAll('.anz-card').length };
  })()`);
  check("análisis de OTRA marca NO aparece en «Análisis guardados»", !anzIso.leak, JSON.stringify(anzIso));

  console.log(`\n═══ RESULTADO: ${passed} ✓ · ${failed} ✗ ═══`);
  if (fails.length) { console.log(fails.map((f) => "  ✗ " + f).join("\n")); }
  process.exitCode = failed ? 1 : 0;
}

main().catch((e) => { console.error("HARNESS ERROR:", e.message); process.exitCode = 2; })
  .finally(() => { try { chrome.kill(); } catch {} try { rmSync(profile, { recursive: true, force: true }); } catch {} });
