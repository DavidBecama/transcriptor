#!/usr/bin/env node
// Harness del CONTRATO de sugerencias (pinnea puntos 1 y 5 a nivel API).
// Uso:  node scripts/verify-suggestions.mjs <baseURL> <sessionCookie> [project_id]
// Ejercita GET /api/radar/suggestions + POST /more en bucle y ASEGURA:
//   · Ningún reel se sirve dos veces en la sesión (contrato punto 1).
//   · Cuando se agota lo fresco: exhausted=true y suggestions=[] (punto 5) — nunca repite.
//   · has_more coherente (no miente: si no hay más frescos, has_more=false).
// Sale 0 si el contrato se cumple, 1 si se viola.
const BASE = process.argv[2] || "http://localhost:3100";
const COOKIE = process.argv[3];
const PID = process.argv[4] || null;
if (!COOKIE) { console.error("falta sessionCookie"); process.exit(2); }

const H = { "Content-Type": "application/json", "Cookie": "session=" + COOKIE };
const q = PID ? ("?project_id=" + encodeURIComponent(PID)) : "";
const body = PID ? { project_id: PID } : {};

async function jget(path) { const r = await fetch(BASE + path, { headers: H }); return { status: r.status, d: await r.json().catch(() => ({})) }; }
async function jpost(path, b) { const r = await fetch(BASE + path, { method: "POST", headers: H, body: JSON.stringify(b || {}) }); return { status: r.status, d: await r.json().catch(() => ({})) }; }

const fail = [];
const idOf = (r) => r.id;

const g = await jget("/api/radar/suggestions" + q);
if (g.d.needs_niche) { console.log("marca sin nicho — nada que verificar"); process.exit(0); }
let served = (g.d.suggestions || []).map(idOf);
console.log(`GET: ${served.length} reels · has_more=${g.d.has_more} · exhausted=${g.d.exhausted}`);
const seen = new Set(served);

let tanda = 0, off = served.length;
while (tanda < 30) {
  const m = await jpost("/api/radar/suggestions/more", { ...body, offset: off });
  const ids = (m.d.suggestions || []).map(idOf);
  const rep = ids.filter((x) => seen.has(x));
  console.log(`  Ver más #${++tanda}: ${ids.length} reels · has_more=${m.d.has_more} · exhausted=${m.d.exhausted} · REPETIDOS=${rep.length}`);
  // CONTRATO 1: ningún repetido
  if (rep.length) fail.push(`tanda ${tanda}: ${rep.length} reels repetidos (${rep.map(x => String(x).slice(0, 8)).join(",")})`);
  // CONTRATO 5: exhausted ⇒ vacío; vacío sin exhausted es incoherente
  if (m.d.exhausted && ids.length) fail.push(`tanda ${tanda}: exhausted=true pero devolvió ${ids.length} reels`);
  ids.forEach((x) => seen.add(x));
  served = served.concat(ids);
  off += ids.length;
  if (!ids.length || m.d.exhausted) {
    // CONTRATO 5: al llegar a vacío debe declararse exhausted (no has_more mudo)
    if (!m.d.exhausted && !ids.length) fail.push(`tanda ${tanda}: 0 reels pero exhausted!=true (agotamiento no honesto)`);
    if (m.d.has_more) fail.push(`tanda ${tanda}: agotado pero has_more=true (miente)`);
    break;
  }
  if (!m.d.has_more) { /* siguiente iteración confirmará exhausted */ }
}

const uniq = new Set(served);
console.log(`\nTOTAL servidos=${served.length} · únicos=${uniq.size} · DUPLICADOS=${served.length - uniq.size}`);
if (served.length !== uniq.size) fail.push(`DUPLICADOS GLOBALES: ${served.length - uniq.size}`);

if (fail.length) { console.log("\n✗ CONTRATO VIOLADO:\n  - " + fail.join("\n  - ")); process.exit(1); }
console.log("\n✓ Contrato OK: cero repetidos en sesión + agotamiento honesto.");
process.exit(0);
