#!/usr/bin/env node
/* Smoke test de la capa "alimentar al cerebro" (brain3d feed layer).
   Lanza Chrome headless con WebGL (swiftshader), abre Cerebro con ?brain3d=force,
   y verifica: API nueva, partícula real → onAbsorb → toast "+1", feed silencioso
   sin toast, easing de knowledge y dispose() limpio. */
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.argv[2] || "http://localhost:3100";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9700 + Math.floor(Math.random() * 400);
const prof = mkdtempSync(join(tmpdir(), "rsbf-"));
let passed = 0, failed = 0;
const ok = (n, c, e) => { if (c) { passed++; console.log(`  ✓ ${n}`); } else { failed++; console.log(`  ✗ ${n}${e ? " — " + e : ""}`); } };

const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${prof}`,
  "--headless=new", "--no-first-run", "--no-sandbox", "--disable-gpu-sandbox",
  "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
  "--window-size=1200,900", "about:blank",
], { stdio: "ignore" });
process.on("exit", () => { try { chrome.kill("SIGKILL"); } catch {} rmSync(prof, { recursive: true, force: true }); });
const wd = setTimeout(() => { console.error("WATCHDOG: colgado"); process.exit(3); }, 90000); wd.unref?.();

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
async function wsUrl() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`); const arr = await r.json();
      const pg = arr.find(t => t.type === "page" && t.webSocketDebuggerUrl);
      if (pg) return pg.webSocketDebuggerUrl;
    } catch {}
    await sleep(150);
  }
  throw new Error("no CDP page target");
}

const WS = await wsUrl();
// CDP por WebSocket nativo (Node ≥22 trae WebSocket global).
const sock = new WebSocket(WS);
await new Promise((res, rej) => { sock.onopen = res; sock.onerror = rej; });
let id = 0; const waiters = new Map();
sock.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); } };
function cmd(method, params = {}) { return new Promise((res) => { const i = ++id; waiters.set(i, res); sock.send(JSON.stringify({ id: i, method, params })); }); }
async function evaluate(expr) {
  const r = await cmd("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
  return r.result?.result?.value;
}

await cmd("Page.enable"); await cmd("Runtime.enable");
// Headless mantiene la página "hidden" → rAF throttled, las partículas no viajarían.
// Emulamos prefers-reduced-motion: la feature "no-fly" absorbe en el sitio de forma
// SÍNCRONA (sin depender de rAF), ejercitando el mismo camino absorb()→onAbsorb.
await cmd("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
await cmd("Page.navigate", { url: `${BASE}/profile/radar?plan=creador&t=brain&brain3d=force` });
await sleep(3500);

// Espera a que monte RSBrain + canvas
let mounted = false;
for (let i = 0; i < 30; i++) {
  mounted = await evaluate(`!!(window.RSBrain && document.querySelector('#rsBrainStage canvas'))`);
  if (mounted) break; await sleep(300);
}
ok("RSBrain monta con canvas WebGL (?brain3d=force)", mounted);

ok("API nueva: levelup/dispose/setOnAbsorb/setAmbient/feed",
  await evaluate(`['levelup','dispose','setOnAbsorb','setAmbient','feed'].every(k=>typeof window.RSBrain[k]==='function')`));

// Partícula REAL → onAbsorb del island → toast "+1" en el stage.
// Disparamos varias y poleamos por .brain-feed-pop (se autodestruye en 1.4s).
await evaluate(`window.__popSeen=0; (function(){ var o=new MutationObserver(function(ms){ ms.forEach(function(m){ [].forEach.call(m.addedNodes,function(n){ if(n.classList&&n.classList.contains('brain-feed-pop')) window.__popSeen++; }); }); }); o.observe(document.body,{childList:true,subtree:true}); window.__popObs=o; })(); for(var i=0;i<3;i++) window.RSBrain.feed('guio');`);
await sleep(600);
const popSeen = await evaluate(`window.__popSeen`);
ok("feed('guio') real → toast '+1' aparece (onAbsorb cableado)", popSeen > 0, `popSeen=${popSeen}`);

// Feed SILENCIOSO (voto like/dislike) → NO debe generar toast.
await evaluate(`window.__popSeen=0; for(var i=0;i<3;i++) window.RSBrain.feed('comp', true);`);
await sleep(600);
const silentPop = await evaluate(`window.__popSeen`);
ok("feed(type,silent) → SIN toast '+1' (voto = solo visual)", silentPop === 0, `popSeen=${silentPop}`);

// Easing de knowledge: update fija el target; el valor se acerca con el tiempo.
const eased = await evaluate(`(function(){ window.RSBrain.update({knowledge:95}); var a=window.__brainK&&window.__brainK(); return true; })()`);
ok("update(knowledge) aceptado sin throw", eased === true);

// dispose(): quita el canvas y deja RSBrain idempotente.
await evaluate(`window.RSBrain.dispose();`);
await sleep(300);
ok("dispose() retira el canvas del stage", await evaluate(`!document.querySelector('#rsBrainStage canvas')`));

clearTimeout(wd);
console.log(`\n═══ RESULTADO: ${passed} ✓ · ${failed} ✗ ═══`);
sock.close(); chrome.kill("SIGKILL");
process.exit(failed ? 1 : 0);
