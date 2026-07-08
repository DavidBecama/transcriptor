import { spawn, execSync } from "node:child_process";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9800 + Math.floor(Math.random()*150);
const COOKIE = process.argv[2];
const BASE = "http://localhost:5557";
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=/tmp/radarchk-${PORT}`, "--no-first-run", "about:blank"], { stdio:"ignore" });
process.on("exit", ()=>{ try{ chrome.kill("SIGKILL"); }catch{} });
const sleep = ms => new Promise(r=>setTimeout(r,ms));
let ws, id=0; const pending=new Map(); const exceptions=[]; const consoleErrs=[];
function send(m,p={}){ return new Promise((res,rej)=>{ const _id=++id; pending.set(_id,{res,rej}); const t=setTimeout(()=>{pending.delete(_id);rej(new Error("CDP timeout "+m));},15000); pending.get(_id).t=t; ws.send(JSON.stringify({id:_id,method:m,params:p})); }); }
async function ev(expr){ const r=await send("Runtime.evaluate",{expression:expr,returnByValue:true,awaitPromise:true}); return r.result.value; }
(async()=>{
  let info; for(let i=0;i<40;i++){ try{ const l=JSON.parse(execSync(`curl -s http://localhost:${PORT}/json`).toString()); info=l.find(t=>t.type==="page"&&t.webSocketDebuggerUrl); if(info)break; }catch{} await sleep(250); }
  ws=new (globalThis.WebSocket)(info.webSocketDebuggerUrl);
  await new Promise((res,rej)=>{ws.onopen=res;ws.onerror=rej;});
  ws.onmessage=(e)=>{ const m=JSON.parse(e.data.toString());
    if(m.method==="Runtime.exceptionThrown"){ const d=m.params.exceptionDetails; exceptions.push((d.exception&&(d.exception.description||d.exception.value))||d.text); }
    if(m.method==="Runtime.consoleAPICalled" && m.params.type==="error"){ consoleErrs.push(m.params.args.map(a=>a.value||a.description||"").join(" ")); }
    if(m.id&&pending.has(m.id)){ const p=pending.get(m.id); clearTimeout(p.t); pending.delete(m.id); m.error?p.rej(new Error(m.error.message)):p.res(m.result); } };
  await send("Runtime.enable"); await send("Page.enable"); await send("Network.enable");
  await send("Network.setCookie",{name:"session",value:COOKIE,domain:"localhost",path:"/"});
  await send("Page.navigate",{url:BASE+"/profile/radar?b=1fbf3255-2e5b-..."});
  await sleep(11000);
  const st = await ev(`(()=>{var S=(window.RadarLoop&&window.RadarLoop.S)||window.S||{};return {mounted:!!document.getElementById('radarRoot'), reels_len:(S.reels||[]).length, suggToday_len:(S._suggToday||[]).length, heroReelIds:(S._heroReelIds||[]).length, tab:S.tab, brandId:S.brandId, oppSlides:document.querySelectorAll('#radarRoot .opp-slide').length, suggCards:document.querySelectorAll('#radarRoot .stday-card').length, galleryCards:document.querySelectorAll('#radarRoot .rgal-track .reel-card, #radarRoot .rgal-track > *').length, emptyMsg:!!document.querySelector('#radarRoot .rs-empty')};})()`);
  const dump = await ev(`(()=>{var sec=document.querySelector('#radarRoot .stday-sec');var any=document.querySelector('#radarRoot [class*="stday"]');return {stday_sec_html:(sec?sec.outerHTML.slice(0,240):"NO .stday-sec"), any_stday_class:(any?any.className:"ninguno"), all_stday_count:document.querySelectorAll('#radarRoot [class*="stday"]').length};})()`);
  console.log("DASHBOARD:", JSON.stringify(st,null,1));
  console.log("SUGG DOM:", JSON.stringify(dump,null,1));
  console.log("EXCEPCIONES JS:", exceptions.length?JSON.stringify(exceptions.slice(0,4),null,1):"NINGUNA");
  console.log("CONSOLE errors:", consoleErrs.length?JSON.stringify(consoleErrs.slice(0,4)):"ninguno");
  process.exit(exceptions.length?1:0);
})().catch(e=>{console.error("ERR",e.message);process.exit(2);});
