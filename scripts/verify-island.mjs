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
  await click('[data-act="chain"][data-k="record"]'); await sleep(300);
  const prompter = await evaluate(`!!document.querySelector('#radarRoot .overlay.prompter')`);
  await click('[data-act="recorded"]'); await sleep(300);
  const closed = await evaluate(`(function(){
    return { feed: !document.querySelector('#radarRoot .overlay'),
             toast: (document.getElementById('rsToastMsg')||{}).textContent||"" };
  })()`);
  check("robo → reveal → teleprompter → «Ya lo grabé» cierra el loop",
    reveal2 && prompter && closed.feed && /Grabado/.test(closed.toast),
    JSON.stringify({ reveal2, prompter, closed }));

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

  console.log(`\n═══ RESULTADO: ${passed} ✓ · ${failed} ✗ ═══`);
  if (fails.length) { console.log(fails.map((f) => "  ✗ " + f).join("\n")); }
  process.exitCode = failed ? 1 : 0;
}

main().catch((e) => { console.error("HARNESS ERROR:", e.message); process.exitCode = 2; })
  .finally(() => { try { chrome.kill(); } catch {} try { rmSync(profile, { recursive: true, force: true }); } catch {} });
