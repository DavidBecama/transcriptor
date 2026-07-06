/* ═══════════════════════════════════════════════════════════════════════════
   RADAR LOOP — motor de contenido multi-marca (v0.17.x)
   App vanilla dentro de la isla .rs (#radarRoot). Vistas: Dashboard | Ideas |
   Guiones, con selector de marca y ecosistema de contexto creciente. Overlays:
   gen → script → formatos → teleprompter → fillweek. Demo usa shim de fetch.
   ═══════════════════════════════════════════════════════════════════════════ */
(function(){
  "use strict";
  var ESC = (window.esc || function(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); });
  function isDemo(){ return !!window.__DEMO__; }

  /* ── prod API helpers (solo se usan en la rama !isDemo()) ─────────────
     Mismo estilo que el resto del archivo (fetch same-origin + .json()).
     apiPost/apiGet resuelven a {ok, status, d} para distinguir 402/409/502. */
  function rsLang(){ try{ var l=(document.documentElement.lang||"es").toLowerCase(); return l.indexOf("en")===0?"en":"es"; }catch(e){ return "es"; } }
  // i18n del onboarding (Fathom: debe traducir al pasar a /en/). L(es,en) → según idioma.
  function L(es,en){ return rsLang()==="en" ? en : es; }
  function apiPost(url, body){
    return fetch(url,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})})
      .then(function(res){ return res.json().catch(function(){return{};}).then(function(d){ return {ok:res.ok, status:res.status, d:d}; }); })
      .catch(function(){ return {ok:false, status:0, d:{error:"network"}}; });
  }
  function apiPatch(url, body){
    return fetch(url,{method:"PATCH",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})})
      .then(function(res){ return res.json().catch(function(){return{};}).then(function(d){ return {ok:res.ok, status:res.status, d:d}; }); })
      .catch(function(){ return {ok:false, status:0, d:{error:"network"}}; });
  }
  function apiGet(url){
    return fetch(url,{credentials:"same-origin"})
      .then(function(res){ return res.json().catch(function(){return null;}).then(function(d){ return {ok:res.ok, status:res.status, d:d}; }); })
      .catch(function(){ return {ok:false, status:0, d:null}; });
  }
  function apiDelete(url){
    return fetch(url,{method:"DELETE",credentials:"same-origin"})
      .then(function(res){ return res.json().catch(function(){return{};}).then(function(d){ return {ok:res.ok, status:res.status, d:d||{}}; }); })
      .catch(function(){ return {ok:false, status:0, d:{error:"network"}}; });
  }
  // En prod el backend es la FUENTE DE VERDAD de créditos: las respuestas de
  // generación traen `credits` (= credits_available). Reflejamos ese saldo en la
  // pill sin descontar local (evita doble-cobro). flashSpark queda cosmético.
  function applyCredits(d, costForFlash){
    if(d && d.credits!=null){ S.user.credits=d.credits; }
    if(costForFlash){ flashSpark(-costForFlash); }
  }
  // El backend guarda `script` como texto plano (hook\n…body…\nclose). Lo
  // partimos a la shape {hook,beats,close} que pinta la isla (igual que parseScript).
  function scriptToParts(scriptStr){
    var l=String(scriptStr||"").split(/\n+/).map(function(s){return s.replace(/^▸\s*/,"").trim();}).filter(Boolean);
    if(!l.length) return {hook:"",beats:[],close:""};
    return {hook:l[0]||"", beats:l.slice(1,-1), close:l.length>1?l[l.length-1]:""};
  }
  // Mapea una fila de GET /scripts (backend) → guión local de la isla.
  function normScript(s){
    var p=scriptToParts(s.script);
    var alt=Array.isArray(s.alt_hooks)?s.alt_hooks:[];
    return { id:gid("g"), seq:++_gseq, _sid:s.id,
      title:s.title||p.hook||"Guión", hook:p.hook, beats:p.beats, close:p.close,
      hooks:alt, expanded:false,
      from:s.from_competitor_username?("@"+s.from_competitor_username):null,
      // FKs de origen («Ideas robadas»): agrupan el guion con su reel/idea/transcripción
      // fuente tras recargar. El backend las manda en GET /scripts (select *).
      reelId:s.from_competitor_reel_id||null, ideaId:s.idea_id||null,
      txId:(s.transcription_id!=null?String(s.transcription_id):null),
      // P1: opciones/hooks/POV del robo persistidos → el reveal se reconstruye
      // también tras recargar. chosen==null → elección pendiente (robo en background).
      genOptions:(s.gen_options&&typeof s.gen_options==="object")?s.gen_options:null,
      brand:brand().name, type:"guión",
      approval:(s.approval_status==="approved"?"approved":"pending"),   // B3: aprobación
      status:(s.recording_status==="recorded"?"recorded":(s.recording_status==="discarded"?"discarded":"draft")) };
  }
  // Mapea una fila de GET /ideas (backend) → idea local (con script_draft como
  // primer "guión" si lo hay; los scripts reales se cargan aparte por idea_id).
  function normIdea(i){
    return { id:i.id, text:i.raw_text||i.title||"", title:i.title||"", scripts:[], expanded:false, seed:0, _server:true, _scriptsLoaded:false, _brand:(i.project_id||"default"),
      // inspired_by = ancla de «Ideas robadas» (reel/transcripción de origen + notas del workspace)
      inspiredById:(i.inspired_by_id!=null?String(i.inspired_by_id):null), inspiredByType:i.inspired_by_type||null,
      inspiredByUser:i.inspired_by_username||null, notes:i.notes||"" };
  }

  /* ── iconos ──────────────────────────────────────────────────── */
  var IC = {
    spark:'<svg viewBox="0 0 24 24" fill="none" width="14" height="14"><path d="M12 2l2.4 6.6L21 11l-6.6 2.4L12 20l-2.4-6.6L3 11l6.6-2.4L12 2z" fill="currentColor"/></svg>',
    eye:'<svg viewBox="0 0 24 24" fill="none" width="14" height="14"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="2.6" stroke="currentColor" stroke-width="1.8"/></svg>',
    heart:'<svg viewBox="0 0 24 24" fill="none" width="14" height="14"><path d="M12 20s-7-4.6-7-9.6A3.9 3.9 0 0112 7a3.9 3.9 0 017 3.4C19 15.4 12 20 12 20z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
    star:'<svg viewBox="0 0 24 24" width="20" height="20"><path d="M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.9 6.7 19.4l1.2-6L3.4 9.3l6-.7L12 3z" fill="currentColor"/></svg>',
    starO:'<svg viewBox="0 0 24 24" fill="none" width="20" height="20"><path d="M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.9 6.7 19.4l1.2-6L3.4 9.3l6-.7L12 3z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
    mic:'<svg viewBox="0 0 24 24" fill="none" width="17" height="17"><rect x="9" y="3" width="6" height="11" rx="3" stroke="currentColor" stroke-width="1.8"/><path d="M5 11a7 7 0 0014 0M12 18v3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    layers:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M12 3l9 5-9 5-9-5 9-5z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M3 13l9 5 9-5" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
    hook:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M18 4v8a6 6 0 11-12 0" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="18" cy="3.5" r="2" stroke="currentColor" stroke-width="1.7"/></svg>',
    repeat:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M3 11V9a4 4 0 014-4h11M21 7l-3-2 3-2M21 13v2a4 4 0 01-4 4H6M3 17l3 2-3 2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    arr:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    arrL:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M19 12H5M11 6l-6 6 6 6" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    back:'<svg viewBox="0 0 24 24" fill="none" width="20" height="20"><path d="M15 6l-6 6 6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    x:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    check:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M5 12l5 5 9-10" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    plus:'<svg viewBox="0 0 24 24" fill="none" width="16" height="16"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    lock:'<svg viewBox="0 0 24 24" fill="none" width="14" height="14"><rect x="4.5" y="10.5" width="15" height="10" rx="2.2" stroke="currentColor" stroke-width="1.8"/><path d="M8 10.5V8a4 4 0 018 0v2.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    chev:'<svg viewBox="0 0 24 24" fill="none" width="16" height="16"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    bolt:'<svg viewBox="0 0 24 24" fill="none" width="16" height="16"><path d="M13 2L4 14h6l-1 8 9-12h-6l1-8z" fill="currentColor"/></svg>',
    doc:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M7 3h7l5 5v13H7z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M14 3v5h5M9.5 13h6M9.5 16.5h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    grid:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><rect x="4" y="4" width="6" height="6" rx="1" stroke="currentColor" stroke-width="1.8"/><rect x="14" y="4" width="6" height="6" rx="1" stroke="currentColor" stroke-width="1.8"/><rect x="4" y="14" width="6" height="6" rx="1" stroke="currentColor" stroke-width="1.8"/><rect x="14" y="14" width="6" height="6" rx="1" stroke="currentColor" stroke-width="1.8"/></svg>',
    bulb:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M9 18h6M10 21h4M12 3a6 6 0 00-4 10.5c.6.6 1 1.3 1 2.1V16h6v-.4c0-.8.4-1.5 1-2.1A6 6 0 0012 3z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
    chart:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M4 19h16M7 16v-5M12 16V8M17 16v-3" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>',
    ig:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><rect x="3" y="3" width="18" height="18" rx="5" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="3.6" stroke="currentColor" stroke-width="1.8"/><circle cx="17" cy="7" r="1.1" fill="currentColor"/></svg>',
    brain:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><path d="M9 4a3 3 0 00-3 3 3 3 0 00-1 5.8A2.5 2.5 0 007 17a3 3 0 005 1 3 3 0 005-1 2.5 2.5 0 002-4.2A3 3 0 0015 4a2.5 2.5 0 00-6 0z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    chat:'<svg viewBox="0 0 24 24" fill="none" width="14" height="14"><path d="M21 12a8 8 0 01-11.5 7.2L4 20l.9-5.2A8 8 0 1121 12z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
    users:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><circle cx="9" cy="8" r="3" stroke="currentColor" stroke-width="1.7"/><path d="M3.5 20a5.5 5.5 0 0111 0" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M16 6.2a3 3 0 010 5.6M20.5 19.5a5 5 0 00-3.2-4.4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
    gear:'<svg viewBox="0 0 24 24" fill="none" width="18" height="18"><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="1.8"/><path d="M19.4 13a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-2.9 1.2V19a2 2 0 11-4 0v-.1a1.7 1.7 0 00-2.9-1.2l-.1.1a2 2 0 11-2.8-2.8l.1-.1A1.7 1.7 0 004.6 13H4.5a2 2 0 110-4h.1a1.7 1.7 0 001.2-2.9l-.1-.1a2 2 0 112.8-2.8l.1.1A1.7 1.7 0 0011 4.6V4.5a2 2 0 114 0v.1a1.7 1.7 0 002.9 1.2l.1-.1a2 2 0 112.8 2.8l-.1.1A1.7 1.7 0 0019.4 11h.1a2 2 0 110 4h-.1a1.7 1.7 0 00-1.6 1z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    logout:'<svg viewBox="0 0 24 24" fill="none" width="16" height="16"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    edit:'<svg viewBox="0 0 24 24" fill="none" width="16" height="16"><path d="M4 13.5V19a1 1 0 0 0 1 1h5.5M15 5l4 4-9 9H6v-4z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
  };

  // Spinner CSS inyectado una vez.
  (function(){
    if(document.getElementById("rs-ldr-style")) return;
    var s=document.createElement("style"); s.id="rs-ldr-style";
    s.textContent="@keyframes rs-spin{to{transform:rotate(360deg)}}.rs-ldr{display:inline-block;width:11px;height:11px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:rs-spin .7s linear infinite;vertical-align:middle;margin-right:5px;flex-shrink:0}";
    document.head.appendChild(s);
  })();

  var GEN_STEPS = {
    script:["Leyendo el reel de tu rival…","Extrayendo la estructura que funcionó…","Reescribiéndolo en TU voz…","Puliendo el hook…"],
    hooks:["Analizando el ángulo…","Probando 5 entradas distintas…","Ordenando por gancho…"],
    carousel:["Troceando la idea en slides…","Escribiendo cada tarjeta…","Diseñando la portada…"],
    linkedin:["Cambiando el registro a LinkedIn…","Alargando con tu experiencia…","Rematando con pregunta…"],
    x:["Partiendo en tuits…","Cuidando cada salto de línea…","Cerrando el hilo…"],
    serie:["Buscando 3 ángulos que encadenan…","Escribiendo la continuidad…","Dejándolos listos para grabar…"]
  };
  var GEN_TITLE = { script:"Cocinando el guion…", hooks:"Buscando tu hook", carousel:"Montando el carrusel", linkedin:"Pasando a LinkedIn", x:"Tejiendo el hilo", serie:"Creando tu serie" };
  // econ (economia-creditos.md): 1 guión = 3 créditos. hooks "3 más" = 2 gratis/día
  // luego 1 (ver hooksUnitsToday). "Llena mi semana" 5 guiones = 12. Regenerar = 1.
  var COST = { script:3, regen:1, hooks:1, carousel:1, linkedin:1, x:1, serie:3, record:0, idea5:5, scripts5:12, hooks5:1, fillweek:12, competitor:2, explosion:30, suggmore:3 };   // idea5 = lote de «Generar 3 ideas» (3 ideas, 5 créd.); suggmore = «Ver más» del carrusel de sugerencias
  var HOOKS_FREE_PER_DAY = 2;   // primeros "3 hooks más" del día gratis (demo + prod)
  function hooksUnitsToday(){   // demo: 0 mientras queden gratis hoy, luego COST.hooks5
    if(typeof S.hooksToday!=="number") S.hooksToday=0;
    return S.hooksToday < HOOKS_FREE_PER_DAY ? 0 : COST.hooks5;
  }

  /* ── estado ──────────────────────────────────────────────────── */
  var S = {
    device:"desktop", _wired:false,
    user:{ name:"", handle:"", email:"", credits:0, streak:0, plan:"", freeLeft:0, presetTone:"viral", presetTones:[], hasVoice:false },
    brands:[], brandId:null,
    plan:"creador",                     // creador | agencia (de /auth/me; en demo, toggle)
    scope:"brand",                      // brand (radar de 1 marca) | portfolio (todas — solo agencia)
    reels:[], favs:{}, filter:"explosion", feedExpanded:false,
    ideas:[], guiones:[], activeGuionId:null, guiFilter:"all", _fillGuionIds:[],
    igConnected:false, metrics:null, metricSort:"recent", metricChart:"views",
    analyses:null, analyzeLoading:false, analyzeStep:"", analyzeErr:null, analyzeUrl:"",   // página «Analizar reel»
    analyzeDetailId:null, analyzeStealing:false, analyzeRefreshing:false,   // ficha de detalle + robar guion + refrescar métricas
    tab:"dashboard",                    // dashboard | guiones | metrics | leaderboard | brain | settings | analizar
    view:"feed",                        // feed(overlay off) | gen | script | result | prompter | fillweek
    reel:null, genKind:"script", resultKind:"hooks", done:{},
    _fillPhase:null, brandMenu:false, acctMenu:false,
    genStepTimer:null, fillTimer:null, toastTimer:null,
    // onboarding v2: handle→nicho→subnicho→valor→competidores→objetivo→cierre.
    onb:{ step:"handle", handle:"", platform:"instagram", niche:"", subniches:[], valueReels:[], valueLoading:false,
          ahaReel:null, ahaLoading:false, ahaScript:null,
          competitors:[], compLoading:false, goal:"", busy:false, error:null, skipped:false, _force:false, _viewed:{} }
  };
  function root(){ return document.getElementById("radarRoot"); }
  function brand(){ return S.brands.filter(function(b){return b.id===S.brandId;})[0] || S.brands[0] || {name:"Mi marca",level:1,voice:40,reelsAnalyzed:0,scripts:0,color:"#f97316"}; }
  function isAgency(){ return S.plan==="agencia"; }
  // Ola Agencia B1: multi-marca = planes con >1 marca (Estudio/Agencia). El
  // switcher y el CRUD de marcas se habilitan aquí (no solo en agencia). Portfolio
  // y Equipo siguen siendo SOLO de agencia (isAgency).
  function isMultiBrand(){ return isAgency() || S.realPlan==="estudio" || (S.brands&&S.brands.length>1); }
  // MACRO = portfolio de todas las marcas (solo agencia). MICRO = radar de una marca.
  function isMacro(){ return isAgency() && S.tab==="portfolio"; }

  /* ── marcas demo (solo demo): un portfolio de agencia con stats por marca.
     En prod esto vendrá de /api/brands con sus stats agregadas. ── */
  function demoBrands(){
    return [
      {id:"b1", name:"David Automatiza", handle:"davidautomatiza", color:"#4f7cff", level:3, voice:64, reels:23, exploded:4, competitors:4, reelsAnalyzed:42, scripts:12},
      {id:"b2", name:"Clínica Nórdica",  handle:"clinicanordica",  color:"#12a37c", level:2, voice:41, reels:9,  exploded:1, competitors:3, reelsAnalyzed:18, scripts:4},
      {id:"b3", name:"Estudio Lumen",    handle:"estudiolumen",    color:"#6d6bf6", level:4, voice:78, reels:6,  exploded:0, competitors:5, reelsAnalyzed:67, scripts:21},
      {id:"b4", name:"Bufete Vidal",     handle:"bufetevidal",     color:"#e0556b", level:2, voice:52, reels:14, exploded:5, competitors:6, reelsAnalyzed:23, scripts:7},
      // Marca de NICHO FINO recién creada, pool aún poblándose: 0 reels / 0 competidores.
      // NUNCA debe quedar muda → estado honesto «poblando tu radar» (contrato B1, lo verifica
      // el harness: «ninguna marca servida sin señales/competidores o estado honesto»).
      {id:"b5", name:"nuquemepongo",     handle:"nuquemepongo",    color:"#c026d3", level:1, voice:0,  reels:0,  exploded:0, competitors:0, reelsAnalyzed:0,  scripts:0, mute:true}
    ];
  }
  // una marca "pide atención" si tiene mucho explosivo sin capitalizar o voz baja.
  function brandNeedsAttention(b){ return (b.exploded||0)>=5 || ((b.exploded||0)>=2 && (b.voice||0)<50); }

  /* ── formato ─────────────────────────────────────────────────── */
  function initialsOf(h){ h=(h||"").replace(/[^a-zA-Z0-9]/g,""); return (h.slice(0,2)||"··").toUpperCase(); }
  function fmtNum(n){ n=Number(n||0); if(n>=1e6) return (n/1e6).toFixed(n>=1e7?0:1).replace(".",",")+" M"; if(n>=1e3) return Math.round(n/1e3)+" K"; return String(n); }
  function relTime(iso){ if(window.ovRelTime) return window.ovRelTime(iso); if(!iso) return ""; var d=(Date.now()-new Date(iso).getTime())/3600000; if(d<1) return "hace "+Math.max(1,Math.round(d*60))+" min"; if(d<24) return "hace "+Math.round(d)+" h"; return "hace "+Math.round(d/24)+" días"; }
  // (C) Math.round ANTES de partir en m:s — con duraciones float (68.5999…s)
  // salía "1:8.599999999999994" en el detalle. 68.6 → "1:09".
  function durFmt(sec){ sec=Math.round(Number(sec||0)); if(!sec) return "0:30"; var m=Math.floor(sec/60),s=sec%60; return m+":"+String(s).padStart(2,"0"); }

  function normReel(r){
    var handle=(r.creator&&r.creator.ig_username)||r.username||"creador";
    var exp=r.explosion_score!=null?r.explosion_score:(r.explosion!=null?r.explosion:null);
    return { id:r.id, creator_id:r.creator_id||null, creator:{handle:handle, initials:r.initials||initialsOf(handle)},
      when:r.when||relTime(r.posted_at),
      postedTs:(function(){ var t=Date.parse(r.posted_at||r.pubAt||r.published_at||""); return isFinite(t)?t:0; })(),   // ts crudo → filtrar a recientes (offer)
      explosion:exp,
      explosionTxt: exp!=null?(exp>=10?Math.round(exp):(Math.round(exp*10)/10)):null,
      views: typeof r.views==="string"?r.views:fmtNum(r.views),
      likes: typeof r.likes==="string"?r.likes:fmtNum(r.likes),
      comments:(r.comments!=null?(typeof r.comments==="string"?r.comments:fmtNum(r.comments)):null),
      shares:(r.shares!=null?(typeof r.shares==="string"?r.shares:fmtNum(r.shares)):null),   // ítem 6: compartidos del competidor (Apify includeSharesCount)
      sharesN:(function(){ var n=parseInt(String(r.shares).replace(/[^\d]/g,""),10); return isFinite(n)?n:0; })(),   // raw → ocultamos el stat si 0 (IG no lo da público)
      dur:r.dur||durFmt(r.video_duration_sec), cap:r.cap||r.caption||"", sum:r.sum||"",
      thumb:r.thumb_b64||r.thumb_url||r.thumb||null, fav:!!(r.is_favorite||r.fav),
      seed:(r.source==="seed"),   // SPEC #3: reel del nicho mientras llenas tu radar
      // URL del reel ORIGINAL (ítem 9): para abrir en IG/TikTok. ig_url/permalink si vienen;
      // si no, se construye desde ig_reel_id (NO usar video_url: es el MP4 de Apify, caduca).
      ig_reel_id:r.ig_reel_id||null,
      url:r.ig_url||r.url||r.permalink||(r.ig_reel_id?("https://www.instagram.com/reel/"+r.ig_reel_id+"/"):null),
      script:r.script||null, hooks:r.hooks||null };
  }

  // Mapea una fila cruda de /metrics/videos (tabla ig_videos: caption/thumbnail_b64/
  // duration/published_at/tag…) a la shape que metricGridHTML/metricStatsHTML leen
  // (cap/thumb/dur/date/top/viral). from_guion/vsMedian no vienen de este endpoint
  // (la atribución vive en scripts) → quedan vacíos y el grid muestra "orgánico".
  function normMetricVideo(v){
    var tag=(v.tag||"")+"";
    return {
      cap: v.cap||v.caption||"",
      views: v.views||0, likes: v.likes||0, comments: v.comments||0,
      shares: v.shares||v.shares_count||0,   // Fathom 18/06: gráfico de compartidos
      thumb: v.thumb||v.thumbnail_b64||v.thumbnail_url||null,
      dur: v.dur||durFmt(v.duration),
      date: v.date||relTime(v.published_at),
      pubAt: v.pubAt||v.published_at||null,   // ISO crudo → tendencia por fecha + mejor hora (real)
      tx: v.tx||v.transcription||null,        // transcript (Groq) si lo hay → ganchos reales
      top: !!(v.top || tag==="top"),
      viral: !!(v.viral || tag==="viral"),
      from_guion: v.from_guion||null, vsMedian: v.vsMedian||null
    };
  }

  function greetWord(){ var h=new Date().getHours(); return h<6?"Buenas noches":h<13?"Buenos días":h<21?"Buenas tardes":"Buenas noches"; }

  /* ════════════════════════════════════════════════════════════════
     TOPBAR + selector de marca + navegación
     ════════════════════════════════════════════════════════════════ */
  // v2 SIGNAL: el topbar se parte en RAIL (izquierda, 64px, iconos) + COMMAND BAR (arriba).
  function brandSwitchHTML(){
    var b=brand();
    var portfolio = isMacro();
    var label = portfolio ? "Todas las marcas" : b.name;
    var dot = portfolio ? '<span class="brand-dot multi"></span>' : '<span class="brand-dot" style="background:'+ESC(b.color)+'"></span>';
    var menu="";
    if(S.brandMenu){
      var items="";
      // B3: sin "Todas las marcas" (portfolio eliminado) — el menú solo cambia de marca.
      // Cada marca: clic = cambiar de contexto (datos aislados por project_id).
      // Renombrar / borrar inline (× con confirmación). 'default' (marca única
      // sin project real) no se renombra/borra.
      items+=S.brands.map(function(x){
        var on=(!portfolio && x.id===S.brandId);
        var manage=(x.id!=="default")
          ? '<span class="brand-opt-act" data-act="brand-rename" data-id="'+ESC(x.id)+'" data-name="'+ESC(x.name)+'" title="Renombrar" aria-label="Renombrar '+ESC(x.name)+'">'+IC.gear+'</span>'+
            (S.brands.length>1?'<span class="brand-opt-act" data-act="brand-del" data-id="'+ESC(x.id)+'" data-name="'+ESC(x.name)+'" title="Borrar marca" aria-label="Borrar '+ESC(x.name)+'">'+IC.x+'</span>':'')
          : '';
        return '<div class="brand-opt-row"><button class="brand-opt'+(on?" on":"")+'" data-act="brand" data-id="'+ESC(x.id)+'"><span class="brand-dot" style="background:'+ESC(x.color)+'"></span>'+ESC(x.name)+'<span class="brand-lvl">Nv '+x.level+'</span></button>'+manage+'</div>';
      }).join("");
      // Entregable de Agencia: informe white-label del mes de la marca activa.
      if(isAgency() && !portfolio) items+='<button class="brand-opt" data-act="brand-report">'+IC.doc+' Generar informe del mes</button>';
      // + Nueva marca (Estudio/Agencia): cap-aware. Al tope → CTA de upgrade/extra.
      var atCap = (S.brandsCap!=null) && (S.brands.length>=S.brandsCap);
      if(isMultiBrand()){
        items+= atCap
          ? '<button class="brand-opt add" data-act="brand-cap">'+IC.plus+' Marca extra (tope '+S.brandsCap+')</button>'
          : '<button class="brand-opt add" data-act="brand-add">'+IC.plus+' Nueva marca'+(S.brandsCap!=null?' ('+S.brands.length+'/'+S.brandsCap+')':'')+'</button>';
      }
      // Cierre on-outside-click (mismo patrón que el menú de cuenta): backdrop full-screen
      // que cierra al clicar fuera. El cierre on-select ya lo hace openBrand (brandMenu=false).
      menu='<div class="brand-backdrop" data-act="brand-close"></div><div class="brand-menu">'+items+'</div>';
    }
    return '<button class="brand-switch" data-act="brand-toggle">'+dot+
        '<span class="brand-name">'+ESC(label)+'</span>'+
        '<span class="brand-chev">'+IC.chev+'</span>'+
      '</button>'+menu;
  }
  // Creador: una sola marca → sin selector. Etiqueta estática (no clicable).
  function brandStaticHTML(){
    var b=brand();
    return '<span class="brand-static"><span class="brand-dot" style="background:'+ESC(b.color||"#4f7cff")+'"></span><span class="brand-name">'+ESC(b.name)+'</span></span>';
  }
  // ── Menú de cuenta (rail, abajo): email + plan + Ajustes + Cerrar sesión.
  //    «Ajustes» abre la MISMA página v3 que el rail (acct-settings → switchTab).
  //    El logout reusa la función global logout() de la chrome (POST /auth/logout). ──
  function _planLabel(p){ p=(p||"").toLowerCase(); return ({free:"Free",pro:"Pro",creator:"Creator",creador:"Creator",agency:"Agency",agencia:"Agency"})[p] || (p?p.charAt(0).toUpperCase()+p.slice(1):"Free"); }
  function _planClass(p){ p=(p||"").toLowerCase(); if(p==="creador")p="creator"; if(p==="agencia")p="agency"; return ({free:"free",pro:"pro",creator:"creator",agency:"agency"})[p]||"free"; }
  function acctMenuHTML(){
    if(!S.acctMenu) return '';
    var av=ESC(initialsOf(S.user.handle||S.user.name||S.user.email||"U"));
    var email=ESC(S.user.email||S.user.name||"");
    var pl=S.user.plan||"";
    return '<div class="rs-acct-backdrop" data-act="acct-close"></div>'+
      '<div class="rs-acct-menu" role="menu">'+
        '<div class="rs-acct-head">'+
          '<span class="rs-acct-av">'+av+'</span>'+
          '<span class="rs-acct-id">'+
            '<span class="rs-acct-email" title="'+email+'">'+(email||"—")+'</span>'+
            '<span class="plan-badge '+_planClass(pl)+'">'+_planLabel(pl)+'</span>'+
          '</span>'+
        '</div>'+
        '<button class="brand-opt" data-act="acct-settings" role="menuitem">'+IC.gear+' '+L("Ajustes","Settings")+'</button>'+
        '<button class="brand-opt" data-act="acct-feedback" role="menuitem">'+IC.chat+' '+L("Enviar feedback","Send feedback")+'</button>'+
        '<button class="brand-opt rs-acct-logout" data-act="acct-logout" role="menuitem">'+IC.logout+' Cerrar sesión</button>'+
      '</div>';
  }
  function railHTML(){
    // Ideas ya no es un tab suelto: la "Fábrica de ideas" vive dentro de Radar
    // (dashboardHTML → ideasZoneHTML), bajo las señales del día.
    // TODO(agencia): reactivar el tab "Equipo" cuando se complete la propagación
    // de ownership del miembro a scripts/radar/tracked (workspace_owner_id). De
    // momento oculto del rail; el backend (invite/join/roles) y teamHTML se quedan.
    //   ...,["team",IC.users,"Equipo"]
    var _tro='<svg viewBox="0 0 24 24" fill="none" width="20" height="20"><path d="M6 4h12v4a6 6 0 11-12 0V4z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M6 6H4v1a3 3 0 003 3M18 6h2v1a3 3 0 01-3 3M9.5 14h5M12 14v3.5M8.5 20h7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    // B3: mismo rail para todos (sin Portfolio). El Radar es la pantalla principal;
    // las marcas se cambian con el switcher de la command bar, no con una pantalla aparte.
    var _ana='<svg viewBox="0 0 24 24" fill="none" width="20" height="20"><circle cx="11" cy="11" r="6.5" stroke="currentColor" stroke-width="1.8"/><path d="M20 20l-3.6-3.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
    var navTabs = [["dashboard",IC.grid,"Radar"],["guiones",IC.doc,L("Ideas robadas","Stolen ideas")],["metrics",IC.chart,"Métricas"],["leaderboard",_tro,"Ranking"],["brain",IC.brain,"Cerebro"],["analizar",_ana,"Analizar"]];
    return '<nav class="rail">'+
      '<span class="rail-logo" role="img" aria-label="Reelscript"></span>'+   // logo R (theme-aware vía CSS: blanca en oscuro, azul en claro)
      navTabs.map(function(t){return '<button class="rail-btn'+(S.tab===t[0]&&!S.legacy?" on":"")+'" data-act="tab" data-k="'+t[0]+'" data-tour="tab-'+t[0]+'">'+t[1]+'<span class="tip">'+t[2]+'</span></button>';}).join("")+
      // «Analizar» = página nativa (historial persistido); también accesible desde el
      // botón «Analizar un reel» del Radar.
      '<button class="rail-btn'+(S.tab==="settings"&&!S.legacy?" on":"")+'" data-act="tab" data-k="settings" aria-label="Ajustes">'+IC.gear+'<span class="tip">Ajustes</span></button>'+
      // El spacer empuja el botón de cuenta al fondo del rail.
      '<span class="rail-spacer"></span>'+
      '<button class="rail-acct'+(S.acctMenu?" on":"")+'" data-act="acct-toggle" aria-label="Tu cuenta" aria-haspopup="menu" aria-expanded="'+(S.acctMenu?"true":"false")+'">'+ESC(initialsOf(S.user.handle||S.user.name||S.user.email||"U"))+'</button>'+
    '</nav>'+
    // El menú va FUERA del <nav> (su z-index queda por encima de #rsLegacy, etc.).
    acctMenuHTML();
  }
  function cmdHTML(){
    var tabName=({dashboard:"RADAR",ideas:"IDEAS ROBADAS",guiones:"IDEAS ROBADAS",metrics:"MÉTRICAS",leaderboard:"RANKING",brain:"CEREBRO",analizar:"ANALIZAR",team:"EQUIPO",settings:"AJUSTES"})[S.tab]||"";
    var crumb;
    crumb='<span class="crumb">/ '+tabName+'</span>';   // B3: sin portfolio/"Todas las marcas"
    var demoToggle=isDemo()?'<div class="demo-plan" title="Solo demo: cambia de plan"><span class="dp-k">DEMO</span>'+
      '<button class="dp'+(S._demoFree===true?" on":"")+'" data-act="demo-plan" data-k="free">Free</button>'+
      '<button class="dp'+(!S._demoFree && S.plan==="creador"?" on":"")+'" data-act="demo-plan" data-k="creador">Creador</button>'+
      '<button class="dp'+(!S._demoFree && S.plan==="agencia"?" on":"")+'" data-act="demo-plan" data-k="agencia">Agencia</button></div>':'';
    return '<div class="cmd">'+
      (isMultiBrand()?brandSwitchHTML():brandStaticHTML())+
      crumb+
      '<span class="grow"></span>'+
      // T1 (IDI): captura de ideas siempre a mano, en cualquier vista de la isla.
      '<button class="cmd-idea" data-act="idea-capture" title="Apunta una idea — se desarrolla en Guiones" aria-label="Apunta una idea"><span class="cmd-idea-bulb">'+IC.bulb+'</span><span class="cmd-idea-t">Apunta una idea</span></button>'+
      demoToggle+
      // Nivel del Cerebro SIEMPRE visible (todas las páginas) y CENTRADO en la barra
      // (.cmd-brain-center = posición absoluta al medio). Cuando hay nivel pendiente,
      // se transforma en el botón ¡Subir de nivel!
      '<div class="cmd-brain-center">'+brainCmdChipHTML()+'</div>'+
      pillStatHTML()+
    '</div>';
  }
  // Pill de estado (derecha de la command bar):
  //   · reverse-trial activo → "Pro · Nd" (badge con días restantes, CTA implícito).
  //   · free post-trial sin créditos → guiones gratis del mes restantes.
  //   · resto → créditos.
  function pillStatHTML(){
    if(isTrial()){
      var d=S.user.trialDaysLeft||0;
      // Modelo nuevo (5 días · 3 guiones/día): si hay contador diario (demo, o real
      // cuando el backend lo mande) muestra "X hoy"; si no, cae al de créditos del trial.
      if(S.user.dayLeft!=null){
        var dl=S.user.dayLeft;
        return '<div class="spark pill-stat trial" id="rsSpark" title="Prueba Pro — '+dl+' guion'+(dl===1?'':'es')+' hoy · '+d+' día'+(d===1?'':'s')+' restantes" data-act="tab" data-k="brain" role="button" tabindex="0">'+IC.spark+'<span class="num">Pro</span> · <b id="rsSparkN">'+dl+'</b> hoy · '+d+'d</div>';
      }
      var cr=S.user.trialCreditsLeft||0;
      return '<div class="spark pill-stat trial" id="rsSpark" title="Prueba Pro — '+cr+' crédito'+(cr===1?'':'s')+' · '+d+' día'+(d===1?'':'s')+' restantes" data-act="tab" data-k="brain" role="button" tabindex="0">'+IC.spark+'<span class="num">Pro</span> · '+cr+' cr</div>';
    }
    if(S.user.plan==="free" && !S.user.credits){
      return '<div class="spark pill-stat credits" id="rsSpark" title="Guiones gratis este mes — ver planes" data-act="credits-pill" role="button" tabindex="0">'+IC.spark+'<span class="num"><b id="rsSparkN">'+S.user.freeLeft+'</b></span> este mes</div>';
    }
    return '<div class="spark pill-stat credits" id="rsSpark" title="Créditos disponibles — ver planes" data-act="credits-pill" role="button" tabindex="0">'+IC.spark+'<span class="num"><b id="rsSparkN">'+S.user.credits+'</b></span> créditos</div>';
  }

  // Cabecera Signal reutilizable (eyebrow mono + h-title Space Grotesk + sub).
  function pheadHTML(eye, title, sub, right){
    return '<header class="phead"><div>'+
      '<div class="eyebrow"><span class="pip"></span>'+ESC(eye)+'</div>'+
      '<h1 class="h-title">'+ESC(title)+'</h1>'+
      (sub?'<p class="h-sub">'+ESC(sub)+'</p>':'')+
    '</div>'+(right?'<div class="phead-right">'+right+'</div>':'')+'</header>';
  }

  /* ════════════════════════════════════════════════════════════════
     DASHBOARD (= Radar): ecosistema + idea-input + whale + feed + manual
     ════════════════════════════════════════════════════════════════ */
  function ecosystemHTML(){
    var b=brand();
    var pct=Math.max(6,Math.min(100,b.voice||40));
    return ''+
    '<div class="eco">'+
      '<div class="eco-top">'+
        '<div class="eco-lvl"><span class="eco-lvl-n">Nivel '+(b.level||1)+'</span><span class="eco-lvl-name">'+ESC(ecoLevelName(b.level))+'</span></div>'+
        '<div class="eco-stats">'+
          '<span><b>'+(b.reelsAnalyzed||0)+'</b> reels en el ecosistema</span>'+
          '<span><b>'+(b.scripts||0)+'</b> guiones</span>'+
        '</div>'+
      '</div>'+
      '<div class="eco-bar"><div class="eco-fill" style="width:'+pct+'%"></div></div>'+
      '<div class="eco-foot">Tu voz al <b>'+pct+'%</b>. Cuanto más creas, más se afina — cada guión sale más tuyo.</div>'+
    '</div>';
  }
  // Nombres de nivel del Cerebro (de David) — FUENTE ÚNICA: la usan el badge/hero/
  // Ajustes/toast y la tarjeta «Niveles del Cerebro» (brainLevelsHTML), para que un
  // mismo nivel no salga con dos nombres distintos. i18n vía L().
  function ecoLevelName(l){ return ({1:L("Aprendiz","Apprentice"),2:L("Imitador","Imitator"),3:L("Ladrón","Thief"),4:L("Estratega","Strategist"),5:L("Viral","Viral")})[l||1]||L("Aprendiz","Apprentice"); }
  // Color por nivel para la insignia del ranking (David 24-jun): no-usuario gris,
  // usuario azul (N1), N2 verde, N3 rojo, N4 violeta, N5 dorado. Da dopamina + pertenencia.
  function brainLevelColor(l){ return ({0:"#94a3b8",1:"#4f7cff",2:"#12a37c",3:"#f5566b",4:"#8b5cf6",5:"#f59e0b"})[l]||"#94a3b8"; }
  // Cifras con gancho (David 24-jun): los 2 primeros dígitos + sufijo K/M en VERDE → "51M".
  // Se trocea ANTES de insertar HTML (si no, el 2º replace matchearía la "m" de "num-hi").
  function _numGreen(s){
    s=String(s==null?"":s);
    var lead=(s.match(/^\s*\d{1,2}/)||[""])[0];
    var rest=ESC(s.slice(lead.length)).replace(/([KkMm]\b|mill\w*)/, '<b class="num-hi">$1</b>');
    return (lead?'<b class="num-hi">'+ESC(lead)+'</b>':'')+rest;
  }
  // #6 conversión (vanidad + aversión a perder progreso): el nivel del Cerebro como
  // ESTATUS visible en el header del Radar, no solo dentro de la pestaña Cerebro.
  // Chip del Cerebro en la command bar (global, todas las páginas). Estado normal =
  // nivel + nombre (lleva al Cerebro). Si hay nivel pendiente de recoger →
  // botón pulsante «¡Subir de nivel el cerebro!» (data-act="brain-levelup").
  function brainCmdChipHTML(){
    var lv=brainLevel();
    if(lv.canLevelUp){
      return '<button class="brain-cmd brain-cmd-up" data-act="brain-levelup" title="'+L("Tu Cerebro tiene un nivel listo — recógelo","Your Brain has a level ready — claim it")+'">'+
        '<span class="bcm-ic">'+IC.brain+'</span><span class="bcm-up">'+L("¡Subir de nivel el cerebro!","Level up your brain!")+'</span></button>';
    }
    var lvlTxt=lv.level>=1?('Nv '+lv.level):L("nuevo","new");
    var pctTxt=lv.full?'':' · '+lv.pct+'%';
    return '<button class="brain-cmd" data-act="tab" data-k="brain" title="'+L("Tu Cerebro — aliméntalo cada día para subir de nivel","Your Brain — feed it daily to level up")+'">'+
      '<span class="bcm-ic">'+IC.brain+'</span><span class="bcm-t">'+L("Cerebro","Brain")+'</span> <b>'+lvlTxt+'</b>'+pctTxt+'</button>';
  }
  function brainBadgeHTML(){
    var lv=brainLevel();
    return '<button class="brain-badge" data-act="tab" data-k="brain" title="'+L("Tu Cerebro — cuanto más creas, más tuyo suena","Your Brain — the more you create, the more it sounds like you")+'">'+IC.brain+' '+L("Cerebro","Brain")+' <b>Nv '+lv.level+'</b> · '+ESC(ecoLevelName(lv.level))+'</button>';
  }

  /* ════════════════════════════════════════════════════════════════
     growth-2 · ONBOARDING DE ACTIVACIÓN (sesión 1) — empty-state del Radar.
     Camino al «aha» en <5 min, una sola acción primaria por paso (IDI):
       1. ask   → handle de IG/TikTok            [Buscar mi competencia]
       2. pick  → 3-5 competidores que sugiere el LLM (preseleccionados)
                                                  [Seguir y empezar]
       3. done  → los sigue (valida al scrapear) → sus reels caen al Radar
                  y el propio empty-state desaparece; guiamos al «Roba la idea».
     Solo en prod, usuario sin competidores. Saltarlo cae al empty-state clásico.
     ════════════════════════════════════════════════════════════════ */
  /* ════════════════════════════════════════════════════════════════════════
     ONBOARDING v2 — motor de aha + personalización + datos reciclables.
     7 pasos: handle → nicho → subnicho(tags) → VALOR → competidores → objetivo
     → cierre(50% + 1er guión). El TONO ya NO se pregunta: se auto-deriva del
     contenido ingerido + el objetivo (STYLE_PROMPTS/PRESET_TONES intactos en
     backend + selector en Cerebro). Pantalla dedicada que oculta el radar vacío.
     Demo-funcional vía ?onb=1. ════════════════════════════════════════════════ */
  var ONB_STEPS=["handle","confirm","myreels","niche","subniche","seed","goal","close"];   // «myreels» = tus 3 vídeos + Alimentar el Cerebro (David 24-jun)   // «confirm» (David 24-jun) = «este eres tú» con tu foto de IG → confianza + sensación de análisis personal. «seed» = el user añade UNA cuenta de su nicho a mano → scrapeamos sus relatedProfiles. La cuenta PROPIA (handle) se scrapea solo para métricas.
  function nicheChips(){ return rsLang()==="en"
    ? ["Fitness","Finance","Marketing","Cooking","Fashion","Beauty","Travel","Tech","Education","Real estate","Health","Business"]
    : ["Fitness","Finanzas","Marketing","Cocina","Moda","Belleza","Viajes","Tecnología","Educación","Inmobiliaria","Salud","Negocios"]; }
  var SUBNICHE_SEED={
    "fitness":["hipertrofia","pérdida de peso","running","crossfit","yoga","calistenia"],
    "finanzas":["inversión","ahorro","cripto","libertad financiera","bolsa","finanzas personales"],
    "marketing":["copywriting","ads de pago","email marketing","marca personal","SEO","redes sociales"],
    "cocina":["recetas fit","cocina rápida","repostería","vegano","meal prep","low cost"],
    "moda":["streetwear","moda sostenible","low cost","lujo","tendencias","outfits"],
    "belleza":["skincare","maquillaje","cosmética orgánica","cosmética coreana","antiedad","uñas"],
    "tecnología":["IA","automatización","gadgets","programación","no-code","productividad"],
    "educación":["idiomas","oposiciones","estudio","matemáticas","historia","ciencia"],
    "negocios":["emprender","ecommerce","SaaS","ventas","liderazgo","freelance"],
    "_default":["consejos","tutoriales","detrás de cámaras","historias","errores comunes","tendencias"]
  };
  var ONB_GOALS=[
    {key:"grow",label:"Crecer",label_en:"Grow",desc:"Más alcance y seguidores",desc_en:"More reach and followers",ic:"chart"},
    {key:"sell",label:"Vender",label_en:"Sell",desc:"Convertir en clientes",desc_en:"Turn into customers",ic:"bolt"},
    {key:"educate",label:"Educar",label_en:"Educate",desc:"Enseñar lo que sabes",desc_en:"Teach what you know",ic:"bulb"},
    {key:"entertain",label:"Entretener",label_en:"Entertain",desc:"Enganchar y divertir",desc_en:"Hook and entertain",ic:"spark"}
  ];
  function _norm(s){ return String(s||"").toLowerCase().trim().replace(/[áà]/g,"a").replace(/[éè]/g,"e").replace(/[íì]/g,"i").replace(/[óò]/g,"o").replace(/[úù]/g,"u"); }
  function onbSubSuggest(){ var k=_norm(S.onb.niche); return (SUBNICHE_SEED[k]||SUBNICHE_SEED["_default"]).filter(function(t){ return (S.onb.subniches||[]).indexOf(t)<0; }); }

  // Gate: prod = usuario nuevo sin datos; demo = solo con ?onb=1.
  function showOnboarding(){
    if(!S.onb || S.onb.skipped) return false;
    if(isDemo()) return S.onb._force===true;
    return S.filter!=="fav" && !S.user.onbV2Done && Array.isArray(S.tracked) && S.tracked.length===0
      && (S.reels||[]).length===0 && !S.creatorFilter;
  }
  // ¿Una pantalla FULL-SCREEN (onboarding, cofre/oferta, o un overlay gen/guion/editor/
  // teleprónter) es la dueña de la vista? En ese caso los LOADERS DE FONDO (refresco de
  // reels/tracked que sondean cada varios segundos) NO deben llamar a render(): reconstruir
  // el DOM desde debajo desconecta el canvas del cofre (los reels de fondo dejan de verse,
  // aunque los números —que se re-buscan por id cada frame— sigan animando) y hace
  // PARPADEAR el guion recién robado. El estado ya se actualiza; al cerrar el overlay se
  // repinta con datos frescos. (Bug Leo 05-jul: cofre no se ve + guion parpadea.)
  function _screenBusy(){
    return !!(S.onbStealOffer || S._onbWaiting || showOnboarding()
      || S.view==="gen" || S.view==="script" || S.view==="editor" || S.view==="prompter" || S.view==="result");
  }
  // RENDER DE FONDO (bgRender): TODO loader async (sugerencias, discover, tracked,
  // leaderboard, métricas…) debe repintar con esto, NUNCA con render() directo.
  //  · Gate: si una pantalla full-screen es la dueña (_screenBusy) NO repinta — el estado
  //    queda actualizado y la vista se repinta al cerrar el overlay. (Repintar por debajo
  //    desconecta canvas del cofre, parpadea el guion, roba el foco.)
  //  · Coalescing: si varios loaders resuelven a la vez (carga inicial del dashboard),
  //    un solo repintado por frame en vez de una ráfaga de renders completos.
  function bgRender(){
    if(_screenBusy()) return;            // el estado S ya quedó al día; se pinta al cerrar
    if(S._bgRT) return;                  // ya hay un repintado agendado en este frame
    S._bgRT=setTimeout(function(){ S._bgRT=null; if(!_screenBusy()) render(); },50);
  }
  function onbIdx(){ var i=ONB_STEPS.indexOf(S.onb.step); return i<0?0:i; }

  /* ── PostHog: 1 evento "viewed" por paso (entrada) + "completed" (avance) →
     funnel de drop-off por paso. window.posthog ya disponible en la isla. ── */
  function onbTrack(evt, extra){
    try{
      var p={ step:S.onb.step, step_num:onbIdx()+1, total:ONB_STEPS.length,
        niche:S.onb.niche||null, subniches:(S.onb.subniches||[]).length,
        goal:S.onb.goal||null, demo:isDemo() };
      if(extra) for(var kk in extra) p[kk]=extra[kk];
      if(window.posthog && window.posthog.capture) window.posthog.capture(evt, p);
    }catch(e){}
  }
  /* Captura genérica de producto (fuera del onboarding) para el panel becama.
     No-op si posthog no está cargado; adjunta plan+demo de base. */
  function rsTrack(evt, props){
    try{
      var p={ plan:(S.realPlan||(S.user&&S.user.plan)||null), demo:isDemo() };
      if(props) for(var k in props) p[k]=props[k];
      if(window.posthog && window.posthog.capture) window.posthog.capture(evt, p);
    }catch(e){}
  }
  function onbView(){ if(!S.onb._viewed) S.onb._viewed={}; if(!S.onb._viewed[S.onb.step]){ S.onb._viewed[S.onb.step]=1; onbTrack("onb_step_viewed"); } }
  function onbGoto(step){ S.onb.step=step; S.onb.error=null; render(); }
  function onbNext(){ var i=onbIdx(); onbTrack("onb_step_completed"); if(i<ONB_STEPS.length-1) onbGoto(ONB_STEPS[i+1]); }
  function onbBack(){ var i=onbIdx(); if(i>0) onbGoto(ONB_STEPS[i-1]); }

  /* ── helpers de markup ── */
  // v3 (mockup David): eyebrow en mono azul, minúscula, con prefijo «› » (no spark+MAYÚS).
  function onbEyebrow(t){ return '<div class="onb-eyebrow">› '+ESC(t)+'</div>'; }
  function onbErr(){ return S.onb.error?'<div class="onb-err" role="alert">'+ESC(S.onb.error)+'</div>':''; }
  function onbCardWrap(eyebrow,h,sub,body,wide){ return '<section class="onb-box'+(wide?' onb-box--wide':'')+'">'+eyebrow+'<h2 class="onb-h">'+h+'</h2>'+(sub?'<p class="onb-sub">'+sub+'</p>':'')+body+'</section>'; }
  function onbBackBtn(){ return onbIdx()>0&&S.onb.step!=="close"?'<button class="onb-back-btn" data-act="onb-back" aria-label="'+L("Atrás","Back")+'">'+IC.back+' '+L("Atrás","Back")+'</button>':''; }

  // Avatar de competidor: intenta la foto REAL de Instagram (unavatar) y, si no
  // existe/falla, cae a las iniciales (que quedan debajo). Mejora progresiva.
  function onbAvatar(handle){
    var h=String(handle||"").replace(/^@+/,"");
    return '<span class="ava bava ava--photo">'+ESC(initialsOf(handle))+
      '<img class="ava-img" src="https://unavatar.io/instagram/'+encodeURIComponent(h)+'?fallback=false" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()"></span>';
  }
  // Paso 1 — handle (OBLIGATORIO).
  function onbHandleHTML(){
    // Chip de validación (mockup David): verde cuando el formato del handle es válido.
    // Honesto: «cuenta válida» (formato OK), no «encontrada» (no verificamos en vivo).
    var _hv=(S.onb.handle||"").replace(/^@+/,"");
    var _ok=/^[a-zA-Z0-9._]{2,30}$/.test(_hv);
    var okChip='<span class="onb-handle-ok'+(_ok?' on':'')+'">'+IC.check+' '+L("cuenta válida","valid handle")+'</span>';
    return onbCardWrap(onbEyebrow(L("empecemos","let's start")),L("¿Cuál es tu Instagram?","What's your Instagram?"),
      L("Lo leo para entender tu voz y de qué va lo tuyo. No publico nada por ti, tranquilo.","I read it to understand your voice and what you're about. I don't post anything for you, promise."),
      '<div class="onb-pform"><div class="onb-handle"><span class="onb-at">@</span>'+
        '<input id="rsOnbHandle" class="onb-input" type="text" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="'+L("tu_usuario","your_handle")+'" value="'+ESC(S.onb.handle||"")+'" aria-label="'+L("Tu usuario de Instagram","Your Instagram handle")+'">'+okChip+'</div></div>'+
      onbErr()+
      '<button class="btn btn-lg btn-primary onb-cta" data-act="onb-handle-next">'+IC.arr+' '+L("Continuar","Continue")+'</button>');
  }
  // Emoji por nicho (David 26-jun: «la M de moda es genérica, que tengan emoji»). Key
  // por _norm (sin acentos, minúsculas), cubre ES y EN. Fallback a la inicial si no hay.
  var NICHE_EMOJI={
    fitness:"💪", finanzas:"💰", finance:"💰", marketing:"📈", cocina:"🍳", cooking:"🍳",
    moda:"👗", fashion:"👗", belleza:"💄", beauty:"💄", viajes:"✈️", travel:"✈️",
    tecnologia:"💻", tech:"💻", educacion:"📚", education:"📚", inmobiliaria:"🏠", "real estate":"🏠",
    salud:"🩺", health:"🩺", negocios:"💼", business:"💼", onlyfans:"🌶️", "only fans":"🌶️"
  };
  function _nicheEmoji(n){ return NICHE_EMOJI[_norm(n)] || ""; }
  // Paso 2 — nicho (OBLIGATORIO): chips amplios + texto libre.
  function onbNicheHTML(){
    var _nc=nicheChips();
    // v3 (mockup David): grid de icon-cards. Icono = emoji del nicho (o inicial si no hay).
    var chips=_nc.map(function(n){ var on=_norm(S.onb.niche)===_norm(n); var em=_nicheEmoji(n);
      return '<button class="onb-ncard'+(on?" on":"")+'" data-act="onb-pick-niche" data-k="'+ESC(n)+'"><span class="onb-ncard-ic'+(em?" onb-ncard-ic--em":"")+'">'+(em||ESC(n.charAt(0).toUpperCase()))+'</span><span class="onb-ncard-l">'+ESC(n)+'</span></button>'; }).join("");
    var custom=(_nc.some(function(n){return _norm(n)===_norm(S.onb.niche);})||!S.onb.niche)?"":S.onb.niche;
    return onbCardWrap(onbEyebrow(L("tu terreno","your turf")),L("¿De qué va tu contenido?","What's your content about?"),
      L("Elige el que más se acerque. Afinamos en el siguiente paso.","Pick the closest one. We refine it in the next step."),
      '<div class="onb-niche-grid">'+chips+'</div>'+
      '<input id="rsOnbNiche" class="onb-text" type="text" placeholder="'+L("…o escríbelo (ej: nutrición deportiva)","…or type it (e.g. sports nutrition)")+'" value="'+ESC(custom)+'" aria-label="'+L("Tu nicho","Your niche")+'">'+
      onbErr()+
      '<div class="onb-row">'+onbBackBtn()+'<button class="btn btn-lg btn-primary onb-cta" data-act="onb-niche-next">'+IC.arr+' '+L("Continuar","Continue")+'</button></div>');
  }
  // Paso 3 — subnicho (OBLIGATORIO): TAGS multi-etiqueta (segmentación) + libre.
  function onbSubnicheHTML(){
    var sel=(S.onb.subniches||[]).map(function(t){ return '<span class="onb-tag on" data-act="onb-tag-toggle" data-k="'+ESC(t)+'">#'+ESC(t)+' <b>×</b></span>'; }).join("");
    var sugg=onbSubSuggest().slice(0,8).map(function(t){ return '<button class="onb-tag" data-act="onb-tag-toggle" data-k="'+ESC(t)+'">#'+ESC(t)+'</button>'; }).join("");
    return onbCardWrap(onbEyebrow(L("afinemos","refine it")),L("¿Qué tratas dentro de "+(S.onb.niche||"lo tuyo")+"?","What do you cover in "+(S.onb.niche||"your niche")+"?"),
      L("Marca todo lo que toques. Cuanto más fino, mejor te leo el nicho.","The more specific, the better the match: <b>organic skincare</b> beats <b>skincare</b>. Pick a few or add your own."),
      '<div class="onb-tags" id="rsOnbTags">'+(sel||'<span class="onb-tags-ph">'+L("Tus etiquetas aparecerán aquí…","Your tags will show up here…")+'</span>')+'</div>'+
      '<div class="onb-tagadd"><span class="onb-hash">#</span><input id="rsOnbTagInput" class="onb-text onb-text--tag" type="text" placeholder="'+L("añade una etiqueta y Enter","add a tag and hit Enter")+'" aria-label="'+L("Añadir subnicho","Add subniche")+'"><button class="onb-tagadd-btn" data-act="onb-tag-add">'+IC.plus+'</button></div>'+
      (sugg?'<div class="onb-sugg-lbl">'+L("Sugerencias para tu nicho","Suggestions for your niche")+'</div><div class="onb-tags onb-tags--sugg">'+sugg+'</div>':'')+
      onbErr()+
      '<div class="onb-row">'+onbBackBtn()+'<button class="btn btn-lg btn-primary onb-cta" data-act="onb-sub-next">'+IC.arr+' '+L("Continuar","Continue")+'</button></div>');
  }
  // Paso 4 — SEED de nicho: el user añade A MANO una cuenta referente de su nicho.
  // Scrapeamos sus relatedProfiles (grafo de IG) → competidores + reels acertados
  // (mucho mejor que el hashtag). El gate Groq del backend limpia marcas/otra región.
  function onbSeedHTML(){
    var _sv=(S.onb.seed||"").replace(/^@+/,"");
    var _ok=/^[a-zA-Z0-9._]{2,30}$/.test(_sv);
    var okChip='<span class="onb-handle-ok'+(_ok?' on':'')+'">'+IC.check+' '+L("cuenta válida","valid handle")+'</span>';
    return onbCardWrap(onbEyebrow(L("tu competencia","your competition")),
      L("¿Quién lo está petando en tu nicho?","Who's killing it in your niche?"),
      L("Dame UNA cuenta referente de lo tuyo. A partir de ella encuentro a tus competidores y los reels que de verdad te interesan.","Give me ONE reference account in your niche. From it I find your competitors and the reels that actually matter to you."),
      '<div class="onb-pform"><div class="onb-handle"><span class="onb-at">@</span>'+
        '<input id="rsOnbSeed" class="onb-input" type="text" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="'+L("cuenta_de_tu_nicho","niche_account")+'" value="'+ESC(_sv)+'" aria-label="'+L("Una cuenta de tu nicho","An account in your niche")+'">'+okChip+'</div></div>'+
      onbErr()+
      '<div class="onb-row">'+onbBackBtn()+'<button class="btn btn-lg btn-primary onb-cta" data-act="onb-seed-next">'+IC.arr+' '+L("Continuar","Continue")+'</button></div>'+
      '<button class="onb-skip" data-act="onb-seed-skip">'+L("No sé a quién poner ahora","Not sure who to add yet")+'</button>');
  }
  // Paso 4 — VALOR (aha): roba 1 de 3 reels explosivos → tu 1er guion a tu medida
  // (nicho + tono; aún NO voz personal — esa es la promesa del cierre/entrenar voz).
  function onbValueHTML(){
    var subs=(S.onb.subniches||[]).slice(0,3).map(function(t){return "#"+t;}).join(" ");
    // Sub-vista AHA: ya eligió un reel → mostramos el guion generado en su voz.
    if(S.onb.ahaReel) return onbAhaHTML(subs);
    var body;
    if(S.onb.valueLoading){
      body='<div class="onb-value-load"><span class="mini-spin" style="width:22px;height:22px;border-width:3px"></span> '+L("Analizando tu nicho… leyendo lo que petó esta semana","Analyzing your niche… reading what blew up this week")+'</div>'+
        '<div class="onb-value-grid">'+[0,1,2,3].map(function(){return '<div class="onb-vcard onb-vcard--skel"></div>';}).join("")+'</div>';
    } else if(!(S.onb.valueReels||[]).length){
      // Seed vacío → no bloquear: estado "radar en marcha" (sensación de trabajo).
      body='<div class="onb-value-load"><span class="mini-spin" style="width:22px;height:22px;border-width:3px"></span> '+L("Radar en marcha — rastreando lo que petó en tu subnicho. En segundos lo tendrás en tu panel. Sigue y te espera ahí.","Radar running — tracking what blew up in your subniche. You'll have it in seconds. Keep going, it'll be waiting in your panel.")+'</div>';
    } else {
      var cards=(S.onb.valueReels||[]).map(function(r,i){
        // Miniatura real del reel (thumb del subnicho); si no hay, gradiente + play.
        // El seed real trae thumb_b64/thumb_url (sin «nail»); incluirlos o salía azul.
        var th=r.thumb||r.thumb_b64||r.thumb_url||r.thumbnail_b64||r.thumbnail_url||null;
        var thumbInner=th
          ? '<img src="'+ESC(th)+'" alt="" loading="lazy">'
          : '<div class="onb-vcard-ph" style="background:'+_galGrad(r.handle||String(i))+'">'+_icPlay+'</div>';
        return '<div class="onb-vcard onb-vcard--pick" data-act="onb-value-steal" data-i="'+i+'" role="button" tabindex="0" style="cursor:pointer">'+
          '<div class="onb-vcard-thumb">'+thumbInner+'<span class="onb-vcard-x">'+IC.bolt+' '+ESC(r.mult)+'×</span></div>'+
          '<div class="onb-vcard-top"><span class="ava bava">'+ESC(initialsOf(r.handle))+'</span><span class="onb-vcard-h">@'+ESC(r.handle)+'</span></div>'+
          '<div class="onb-vcard-cap">'+ESC(r.caption)+'</div>'+
          '<div class="onb-vcard-meta">'+IC.eye+' '+ESC(r.views)+' · '+ESC(r.tag)+'</div>'+
          '<div class="onb-vcard-steal" style="margin-top:10px;font-size:13px;font-weight:700;color:var(--rs-accent,#4f7cff);display:flex;align-items:center;gap:6px">'+IC.bolt+' '+L("Robar este","Steal this")+'</div>'+
        '</div>';
      }).join("");
      body='<div class="onb-value-grid">'+cards+'</div>';
    }
    // Cargando → SIN botón (solo Atrás): no mostramos avanzar hasta que cargue todo.
    // Con reels → hint (se roba tocando). Sin reels tras cargar (cold-start) → Seguir.
    var cta;
    if(S.onb.valueLoading) cta='';
    else if((S.onb.valueReels||[]).length) cta='<span class="onb-hint" style="font-size:13px;opacity:.6;align-self:center">'+L("Toca un reel para robarlo y convertirlo en tu guion","Tap a reel to steal it and turn it into your script")+'</span>';
    else cta='<button class="btn btn-lg btn-primary onb-cta" data-act="onb-value-next">'+IC.arr+' '+L("Seguir","Continue")+'</button>';
    return onbCardWrap(onbEyebrow(L("inspirándome del éxito","learning from what works")),
      L("Esto es lo que explota en lo tuyo","This is what's blowing up in your space"),
      L("Ya estoy mirando a quién copiar bien. Mira el nivel que vas a poder robar:","I'm already scoping who to copy right. Look at the level you'll be able to steal:"),
      body+
      '<div class="onb-row">'+onbBackBtn()+cta+'</div>', true);
  }
  // Sub-vista AHA — el primer guion, generado a partir del reel robado.
  function onbAhaHTML(subs){
    var r=S.onb.ahaReel, inner;
    if(S.onb.ahaLoading || !S.onb.ahaScript){
      inner='<div class="onb-value-load"><span class="mini-spin" style="width:24px;height:24px;border-width:3px"></span> '+L("Robando a @","Stealing from @")+ESC(r.handle)+L(" y convirtiéndolo en tu guion…"," and turning it into your script…")+'</div>';
    } else {
      var s=S.onb.ahaScript;
      var beats=(s.beats||[]).map(function(b,i){return '<div class="beat"><span class="n">'+String(i+1).padStart(2,"0")+'</span><span>'+ESC(b)+'</span></div>';}).join("");
      inner='<div style="border:1px solid var(--rs-line,rgba(128,128,128,.2));border-radius:14px;padding:18px;text-align:left;background:var(--rs-surface,rgba(127,127,127,.04))">'+
        '<div style="font-size:11px;font-weight:700;letter-spacing:.02em;color:var(--rs-accent,#4f7cff);margin-bottom:12px;display:flex;align-items:center;gap:6px">'+IC.check+' '+L("Robado de @","Stolen from @")+ESC(r.handle)+L(" · ya es tuyo"," · now yours")+'</div>'+
        '<h3 style="font-size:19px;font-weight:700;line-height:1.3;margin:0 0 14px">'+ESC(s.hook)+'</h3>'+
        '<div class="script-body">'+beats+'</div>'+
        (s.close?'<div style="margin-top:12px;opacity:.8;font-style:italic">'+ESC(s.close)+'</div>':'')+
      '</div>';
    }
    var ready=!S.onb.ahaLoading && S.onb.ahaScript;
    var row = ready
      ? '<div class="onb-row"><button class="btn btn-ghost" data-act="onb-value-reset">← '+L("Elegir otro","Pick another")+'</button><button class="btn btn-lg btn-primary onb-cta" data-act="onb-value-next">'+IC.arr+' '+L("Sigamos","Let's go")+'</button></div>'
      : '';
    return onbCardWrap('<div class="onb-eyebrow">'+IC.check+' '+L("Tu primer guión","Your first script")+'</div>',
      L("Esto ya es tuyo. Tu primer guion, en 1 clic.","This is yours now. Your first script, in 1 click."),
      L("Acabas de convertir un reel que petó en ","You just turned a reel that blew up in ")+(subs||L("tu subnicho","your subniche"))+L(" en un guión listo para grabar. Y cuando me enseñes tu voz, sonará clavado a ti."," into a script ready to record. And once you teach me your voice, it'll sound exactly like you."),
      inner+row, true);
  }
  // Paso 5 — competidores (OBLIGATORIO 2), pre-sugeridos del subnicho.
  function onbCompsHTML(){
    var body;
    if(S.onb.compLoading){
      body='<div class="onb-value-load"><span class="mini-spin" style="width:20px;height:20px;border-width:2.5px"></span> '+L("Buscando a quién deberías vigilar…","Finding who you should be watching…")+'</div>';
    } else {
      var list=(S.onb.competitors||[]).map(function(c){
        return '<button class="onb-card-row'+(c.picked?" on":"")+'" data-act="onb-comp-toggle" data-h="'+ESC(c.handle)+'" role="checkbox" aria-checked="'+(c.picked?"true":"false")+'">'+
          '<span class="onb-card-check">'+(c.picked?IC.check:"")+'</span>'+
          onbAvatar(c.handle)+
          '<span class="onb-card-body"><span class="onb-card-h">@'+ESC(c.handle)+'</span>'+(c.reason?'<span class="onb-card-r">'+ESC(c.reason)+'</span>':'')+'</span>'+
        '</button>';
      }).join("");
      // "Añadir a mano" ARRIBA del listado (pedido por David) + Enter ya soportado.
      body='<div class="onb-tagadd onb-tagadd--top"><span class="onb-hash">@</span><input id="rsOnbCompInput" class="onb-text onb-text--tag" type="text" autocapitalize="none" spellcheck="false" placeholder="'+L("añade un competidor a mano y Enter","add a competitor by hand and hit Enter")+'" aria-label="'+L("Añadir competidor","Add competitor")+'"><button class="onb-tagadd-btn" data-act="onb-comp-add">'+IC.plus+'</button></div>'+
        '<div class="onb-cards">'+list+'</div>';
    }
    var nPick=(S.onb.competitors||[]).filter(function(c){return c.picked;}).length;
    // Cargando → SIN botón de avanzar (solo Atrás): no aparece antes que los resultados.
    var compCta=S.onb.compLoading ? ''
      : '<button class="btn btn-lg btn-primary onb-cta" data-act="onb-comps-next"'+(nPick<1?' disabled':'')+'>'+IC.arr+' '+L("Seguir a "+nPick,"Follow "+nPick)+'</button>';
    return onbCardWrap(onbEyebrow(L("a quién vigilamos","who to watch")),L("Ya te puse 2 en el radar","I already added 2 to your radar"),
      L("Los que más explotan en tu nicho. Quita o añade los que quieras.","I pre-picked 2 from your subniche. I'll track the reels that blow up so you can steal the first in your voice. Remove or add whoever you want."),
      body+onbErr()+
      '<div class="onb-row">'+onbBackBtn()+compCta+'</div>');
  }
  // Paso 6 — objetivo (adapta tono+estructura).
  function onbGoalHTML(){
    var cards=ONB_GOALS.map(function(g){ var on=S.onb.goal===g.key; return '<button class="onb-goal'+(on?" on":"")+'" data-act="onb-pick-goal" data-k="'+g.key+'">'+
      '<span class="onb-goal-ic">'+(IC[g.ic]||IC.spark)+'</span><span class="onb-goal-l">'+ESC(L(g.label,g.label_en))+'</span><span class="onb-goal-d">'+ESC(L(g.desc,g.desc_en))+'</span></button>'; }).join("");
    return onbCardWrap(onbEyebrow(L("para qué","what for")),L("¿Qué buscas con esto?","What are you after?"),
      L("Ajusto el tono de tus guiones a tu objetivo. Solo uno.","With this I tune the tone and structure of your scripts — selling isn't written like entertaining."),
      '<div class="onb-goals">'+cards+'</div>'+onbErr()+
      '<div class="onb-row">'+onbBackBtn()+'<button class="btn btn-lg btn-primary onb-cta" data-act="onb-goal-next"'+(S.onb.goal?'':' disabled')+'>'+IC.arr+' '+L("Continuar","Continue")+'</button></div>');
  }
  // Paso 7 — CIERRE: Cerebro 35% + primer guión + camino a 100%.
  // Brief David v3: el cierre NO afirma en pasado lo que aún es async (el scrape del
  // perfil propio + competidores corre en segundo plano) → copy en presente/continuo.
  // Paso 7 CIERRE (mockup David): centrado — anillo azul al 35% con «Cerebro · N1» +
  // acento Syne «Manifestando viralidad» + título + sub corto + «Abrir mi primer guion».
  function onbCloseHTML(){
    var pct=35;
    return '<section class="onb-box onb-box--center">'+
      '<div class="onb-close-ring"><div class="brain-ring">'+
        '<svg width="150" height="150" viewBox="0 0 172 172" class="brain-ring-svg" aria-hidden="true">'+
          '<circle cx="86" cy="86" r="74" fill="none" stroke="var(--surface-overlay)" stroke-width="13"/>'+
          '<circle cx="86" cy="86" r="74" fill="none" stroke="var(--brand-500)" stroke-width="13" stroke-linecap="round" stroke-dasharray="465" stroke-dashoffset="'+Math.round(465*(1-pct/100))+'" class="brain-ring-prog"/>'+
        '</svg>'+
        '<div class="brain-ring-c"><span class="onb-ring-pct">'+pct+'%</span><span class="brain-ring-lvl">'+L("Cerebro · N1","Brain · N1")+'</span></div>'+
      '</div></div>'+
      '<div class="onb-close-accent">'+L("Manifestando viralidad","Manifesting virality")+'</div>'+
      '<h2 class="onb-h">'+L("Tu Cerebro ya sabe lo justo para empezar","Your Brain now knows just enough to start")+'</h2>'+
      '<p class="onb-sub">'+L("Cada guion que crees lo afina. Vamos con el primero — el resto es cuesta abajo.","Every script you make sharpens it. Let's do the first one — the rest is downhill.")+'</p>'+
      onbErr()+
      '<button class="btn btn-lg btn-primary onb-cta" data-act="onb-finish"'+(S.onb.busy?' disabled':'')+'>'+(S.onb.busy?'<span class="mini-spin"></span> '+L("Preparando…","Preparing…"):IC.bolt+' '+L("Abrir mi primer guion","Open my first script"))+'</button>'+
    '</section>';
  }
  // «Este eres tú» (David 24-jun): tras poner el @, confirmamos con su FOTO de perfil
  // real de IG (vía unavatar.io, sin Apify; fallback a iniciales si falla/privado) →
  // confianza + sensación de "te estoy analizando a TI". Su perfil ya se está
  // scrapeando en 2º plano (onbConnectIG disparado en el paso handle).
  function onbConfirmHTML(){
    var h=(S.onb.handle||"").replace(/^@+/,"");
    // Loading hasta que Apify responda (avatar OK o fallo). NO dejar Continuar antes
    // de tiempo (bug Leo 25-jun): el botón queda en «Analizando…» mientras se trae la foto.
    var waiting = !isDemo() && !S.onb.avatar && !S.onb._profileDone;
    // Red de seguridad: si por lo que sea no se disparó la petición, lánzala (idempotente)
    // para que el loading siempre resuelva.
    if(waiting && !S.onb._avatarReq){ setTimeout(function(){ onbFetchAvatar(h); }, 0); }
    var cta = waiting
      ? '<button class="btn btn-lg btn-primary onb-cta" disabled aria-busy="true"><span class="mini-spin" style="width:18px;height:18px;border-width:2px"></span> '+L("Analizando tu perfil…","Analyzing your profile…")+'</button>'
      : '<button class="btn btn-lg btn-primary onb-cta" data-act="onb-confirm-yes">'+IC.check+' '+L("Sí, soy yo","Yes, that's me")+'</button>';
    return '<section class="onb-step onb-confirm">'+
      onbBackBtn()+
      '<div class="onbc-ava-wrap'+(waiting?' loading':'')+'">'+
        '<span class="onbc-ava-fb">'+ESC(initialsOf(h))+'</span>'+
        (S.onb.avatar?'<img class="onbc-ava on" src="'+ESC(S.onb.avatar)+'" alt=""/>':'')+
        '<span class="onbc-badge">'+IC.ig+'</span>'+
      '</div>'+
      '<h2 class="onb-h">'+L("¿Eres tú, @"+ESC(h)+"?","Is this you, @"+ESC(h)+"?")+'</h2>'+
      '<p class="onb-sub">'+(waiting
          ? L("Estoy trayendo tu foto de Instagram para confirmarlo…","Fetching your Instagram photo to confirm…")
          : L("Voy a analizar TU cuenta para que el Cerebro aprenda cómo hablas y clave tu voz en cada guion.","I'll analyze YOUR account so the Brain learns how you talk and nails your voice in every script."))+'</p>'+
      onbErr()+
      cta+
      '<button class="onb-skip" data-act="onb-confirm-edit">'+L("No, cambiar mi usuario","No, change my handle")+'</button>'+
    '</section>';
  }
  // «Tus vídeos» (David 24-jun): tras «este eres tú», enseña 3 reels suyos (del MISMO
  // scrape Apify, latestPosts) + botón «Alimentar el Cerebro» → aprende su tono de los
  // captions. Si el scrape aún no acabó → skeletons + botón deshabilitado.
  function onbMyReelsHTML(){
    var reels=(S.onb.myReels||[]).slice(0,3);
    var done=!!S.onb._profileDone;
    var loading=!done && reels.length===0;
    var noReels=done && reels.length===0;
    var cards;
    if(reels.length){
      cards=reels.map(function(r){
        var thumb=r.thumb?'<img class="onbmr-thumb" src="'+ESC(r.thumb)+'" alt="" loading="lazy"/>':'<div class="onbmr-thumb onbmr-ph">'+IC.bolt+'</div>';
        return '<div class="onbmr-card">'+thumb+'<div class="onbmr-views">'+IC.eye+' '+_numGreen(fmtNum(r.views||0))+'</div></div>';
      }).join("");
    } else {
      cards=[0,1,2].map(function(){ return '<div class="onbmr-card onbmr-skel"'+(loading?' aria-busy="true"':'')+'></div>'; }).join("");
    }
    var sub = loading ? L("Analizando tus vídeos publicados…","Analyzing your published videos…")
      : noReels ? L("No pude leer tus reels (¿perfil privado o sin vídeos?). No pasa nada, seguimos.","Couldn't read your reels (private or no videos?). No worries, let's continue.")
      : L("Estos son tus reels. Aliméntame con ellos y aprendo tu tono, tus muletillas y cómo enganchas.","These are your reels. Feed me with them and I learn your tone, catchphrases and hooks.");
    var cta = noReels
      ? '<button class="btn btn-lg btn-primary onb-cta" data-act="onb-feed-skip">'+L("Continuar","Continue")+'</button>'
      : '<button class="btn btn-lg btn-primary onb-cta" data-act="onb-feed-myreels"'+(loading?' disabled':'')+'>'+IC.bolt+' '+(loading?L("Leyendo tus vídeos…","Reading your videos…"):L("Alimentar el Cerebro","Feed the Brain"))+'</button>';
    return '<section class="onb-step onb-myreels">'+
      onbBackBtn()+
      '<div class="onbmr-eyebrow"><span class="pip"></span>'+L("HE LEÍDO TUS REELS","I READ YOUR REELS")+'</div>'+
      '<h2 class="onb-h">'+L("Tu Cerebro ya te está leyendo","Your Brain is reading you")+'</h2>'+
      '<p class="onb-sub">'+sub+'</p>'+
      '<div class="onbmr-grid">'+cards+'</div>'+
      onbErr()+
      cta+
      (noReels?'':'<button class="onb-skip" data-act="onb-feed-skip">'+L("Saltar este paso","Skip this step")+'</button>')+
    '</section>';
  }
  function onbFeedMyReels(){
    var reels=(S.onb.myReels||[]).slice(0,3);
    try{ brainFeast(12); }catch(e){}   // dopamina: lluvia al cerebro 3D
    if(S._brain){ try{ S._brain.feed(); }catch(e){} }   // v=136: chispazo en el cerebro del onboarding
    if(!isDemo()){
      reels.forEach(function(r){
        var c=(r.caption||"").trim(); if(!c) return;
        try{ apiPost('/api/brain/rate',{text:c.slice(0,300), kind:"guion", type:"guiones", rating:1, suggestion:c, source:"own_reel", niche:(S.onb&&S.onb.niche)||""}); }catch(e){}
      });
    }
    if(S.voice && S.voice.has_profile){ S.voice.confidence=Math.min(92,(S.voice.confidence||0)+8); }
    showToast(L("🧠 +8% · aprendí tu estilo de tus reels","🧠 +8% · learned your style from your reels"));
    onbNext();
  }
  function onbStepHTML(){
    switch(S.onb.step){
      case "confirm": return onbConfirmHTML();
      case "myreels": return onbMyReelsHTML();
      case "niche": return onbNicheHTML();
      case "subniche": return onbSubnicheHTML();
      case "seed": return onbSeedHTML();
      case "goal": return onbGoalHTML();
      case "close": return onbCloseHTML();
      default: return onbHandleHTML();
    }
  }
  // A) Pantalla dedicada: ocupa todo, oculta el radar vacío del fondo. v=136: split-screen
  // — el CEREBRO (reel-brain.js, canvas 2D) vive en el panel izquierdo persistente
  // (#rsBrainHost, hermano de #rsView → no se repinta) y crece paso a paso; el wizard va a
  // la derecha. mountOnbBrain() lo monta/avanza desde render(); destroy al salir.
  function onboardingScreenHTML(){
    var i=onbIdx();
    var dots=ONB_STEPS.map(function(s,n){ return '<span class="onbp-dot'+(n<=i?" on":"")+'"></span>'; }).join("");
    return '<div class="onb-screen onb-screen--split"><div class="onb-screen-bg" aria-hidden="true"></div>'+
      '<div class="onb-screen-inner">'+
        '<div class="onb-top"><span class="onb-logo">'+IC.bolt+' ReelScript</span></div>'+
        '<div class="onb-prog"><div class="onbp-dots">'+dots+'</div><span class="onbp-lbl">'+L("Paso","Step")+' '+(i+1)+' '+L("de","of")+' '+ONB_STEPS.length+'</span></div>'+
        onbStepHTML()+
      '</div></div>';
  }
  // Caption del cerebro por paso (del diseño de Claude Design): el cerebro «narra» su construcción.
  function _onbBrainCap(i){
    var caps=[
      {tag:L("núcleo","core"),               title:L("Despierta.","Waking up.")},
      {tag:L("reconocimiento","recognition"),title:L("Te reconozco.","I recognize you.")},
      {tag:L("sinapsis","synapses"),         title:L("Primer chispazo.","First spark.")},
      {tag:L("estructura","structure"),      title:L("Tomando forma.","Taking shape.")},
      {tag:L("dendritas","dendrites"),       title:L("Ramificando.","Branching out.")},
      {tag:L("red externa","external net"),  title:L("Conectando tu nicho.","Wiring your niche.")},
      {tag:L("propósito","purpose"),         title:L("Con un objetivo.","With a goal.")},
      {tag:L("online · 35%","online · 35%"), title:L("Manifestando viralidad.","Manifesting virality.")}
    ];
    return caps[i]||caps[0];
  }
  // Monta/avanza el cerebro del onboarding. Idempotente: crea ReelBrain una sola vez sobre
  // el canvas persistente y solo llama setStep (que crece sumando, no reinicia).
  function mountOnbBrain(){
    var bh=document.getElementById("rsBrainHost"); if(!bh) return;
    bh.style.display="block";
    var cv=document.getElementById("rsBrainCanvas");
    if(window.ReelBrain && cv){
      if(!S._brain){ try{ S._brain=new window.ReelBrain(cv,{brand:"#4f7cff"}); }catch(e){ S._brain=null; } }
      if(S._brain){ try{ S._brain.setStep(onbIdx()+1); }catch(e){} }
    }
    var cap=_onbBrainCap(onbIdx());
    var t=document.getElementById("rsBrainTag"), h=document.getElementById("rsBrainTitle");
    if(t) t.textContent=cap.tag; if(h) h.textContent=cap.title;
  }
  // Desmonta el cerebro al salir del onboarding (destroy → mata rAF/ResizeObserver).
  function unmountOnbBrain(){
    var bh=document.getElementById("rsBrainHost");
    if(bh && bh.style.display!=="none") bh.style.display="none";
    if(S._brain){ try{ S._brain.destroy(); }catch(e){} S._brain=null; }
  }
  // compat: el dashboard antiguo llamaba onboardingHTML(); ya no se usa (render
  // hace short-circuit a la pantalla dedicada), pero lo dejamos seguro.
  function onboardingHTML(){ return ''; }
  /* ── handlers v2 ── */
  function onbHandleNext(){
    var inp=document.getElementById("rsOnbHandle");
    var h=(inp?inp.value:S.onb.handle||"").trim().replace(/^@+/,"").toLowerCase();
    if(!/^[a-z0-9._]{1,30}$/.test(h)){ S.onb.error=L("Escribe tu usuario sin @ (letras, números, punto y guion bajo).","Type your handle without @ (letters, numbers, dot and underscore)."); S.onb.handle=h; return render(); }
    S.onb.handle=h;
    // #5: conectar IG + lanzar el scrape del perfil propio YA (paso 1), en segundo
    // plano, para que al acabar el onboarding + house tour el user tenga sus métricas.
    // Una sola vez por onboarding (volver/avanzar no re-dispara).
    if(!isDemo() && !S.onb._igConnected){ S.onb._igConnected=true; onbConnectIG(h); }
    // FOTO de perfil (David/Bernat): petición Apify (la miniatura aparece en «este eres
    // tú» y en la carga). Idempotente por handle; ya pudo dispararse al teclear (head start).
    onbFetchAvatar(h);
    onbNext();
  }
  // Conecta el Instagram del usuario y lanza el análisis de su perfil en SEGUNDO PLANO.
  // Reusa /metrics/ig-profile + /metrics/analyze (el camino real de métricas). 409 al
  // conectar = ya estaba vinculado → igualmente lanzamos el análisis. Fire-and-forget:
  // no bloquea el onboarding ni muestra errores (si falla, el user lo conecta luego).
  function onbConnectIG(h){
    if(isDemo() || !h) return;
    var pid=_pidOf(S.brandId);
    var post=function(url,body){ return fetch(url,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})}); };
    post("/metrics/ig-profile", pid?{username:h,project_id:pid}:{username:h})
      .then(function(){
        // REACTIVIDAD (Bloque 2.6): refleja la conexión SIN recargar. Antes era
        // fire-and-forget → la card «Conecta Instagram» seguía hasta un reload. (409 =
        // ya vinculado → fetch resuelve igual, y marcar conectado es correcto.)
        S.igConnected=true;
        try{ render(); }catch(e){}
        post("/metrics/analyze", pid?{project_id:pid}:{})
          .then(function(){ try{ if(typeof refreshMetrics==="function") refreshMetrics(); }catch(e){} })
          .catch(function(){});
      })
      .catch(function(){});
  }
  // Trae la FOTO de perfil de IG (base64 vía Apify, endpoint /api/onboarding/ig-avatar) y
  // la guarda en S.onb.avatar → la usan «este eres tú» y la carga. Best-effort (si falla,
  // se queda en iniciales). Tarda ~5-15s (scrape Apify), así que llega async y re-renderiza.
  function onbFetchAvatar(h){
    h=(h||"").trim().replace(/^@+/,"").toLowerCase();
    if(!h) return;
    if(isDemo()){
      // demo: sembrar reels de muestra para previsualizar el paso «tus vídeos» (sin Apify)
      var _th=(typeof _demoThumbs==="function"?_demoThumbs():[])||[];
      S.onb._profileDone=true;
      S.onb.myReels=[{thumb:_th[0]||null,views:1240000,caption:"demo"},{thumb:_th[1]||null,views:842000,caption:"demo"},{thumb:_th[2]||null,views:511000,caption:"demo"}];
      return;
    }
    if(S.onb._avatarReq===h) return;   // idempotente por handle
    S.onb._avatarReq=h; S.onb._avatarFailed=false;
    // Fallback: si Apify tarda demasiado/cuelga, a los 12s desbloqueamos «Continuar»
    // (con iniciales) para no dejar al usuario atascado en el loading.
    try{ clearTimeout(S.onb._avaWait); }catch(e){}
    S.onb._avaWait=setTimeout(function(){
      if(S.onb._avatarReq===h && !S.onb._profileDone){ S.onb._profileDone=true; if(!S.onb.avatar) S.onb._avatarFailed=true; render(); }
    }, 12000);
    apiGet("/api/onboarding/ig-avatar?handle="+encodeURIComponent(h)).then(function(r){
      if(S.onb._avatarReq!==h) return;   // cambió el handle entretanto
      if(r && r.ok && r.d && Array.isArray(r.d.reels)) S.onb.myReels=r.d.reels;   // paso «tus vídeos»
      var av=(r && r.ok && r.d && r.d.avatar)?r.d.avatar:null;
      if(av){
        // PRECARGA la foto: NO habilitar «Sí, soy yo» cuando responde la petición, sino
        // cuando la imagen está REALMENTE pintada. Antes el botón se activaba al resolver
        // la petición pero la <img> tardaba unos segundos más → se podía avanzar sin ver la
        // foto (bug Leo 29-jun). El timeout de 12s sigue como red de seguridad.
        var im=new Image();
        im.onload=function(){ if(S.onb._avatarReq!==h) return; try{ clearTimeout(S.onb._avaWait); }catch(e){} S.onb.avatar=av; S.onb._profileDone=true; render(); };
        im.onerror=function(){ if(S.onb._avatarReq!==h) return; try{ clearTimeout(S.onb._avaWait); }catch(e){} S.onb._avatarFailed=true; S.onb._profileDone=true; render(); };
        im.src=av;
      } else {
        try{ clearTimeout(S.onb._avaWait); }catch(e){}
        S.onb._avatarFailed=true; S.onb._profileDone=true; render();
      }
    });
  }
  function onbPickNiche(n){
    var ni=document.getElementById("rsOnbNiche"); if(ni) ni.value="";
    S.onb.niche=n; S.onb.subniches=[]; render();
  }
  function onbNicheNext(){
    var ni=document.getElementById("rsOnbNiche");
    var custom=ni&&ni.value.trim()?ni.value.trim():"";
    if(custom) S.onb.niche=custom;
    if(!(S.onb.niche||"").trim()){ S.onb.error=L("Elige un nicho o escríbelo para encontrar lo que petó.","Pick a niche or type it so I can find what's blowing up."); return render(); }
    onbNext();
  }
  function onbTagToggle(t){
    var a=S.onb.subniches||(S.onb.subniches=[]); var i=a.indexOf(t);
    if(i>=0) a.splice(i,1); else a.push(t);
    render();
  }
  function onbTagAdd(){
    var inp=document.getElementById("rsOnbTagInput"); if(!inp) return;
    var t=inp.value.trim().replace(/^#/,"").toLowerCase(); inp.value="";
    if(t && (S.onb.subniches||[]).indexOf(t)<0){ (S.onb.subniches||(S.onb.subniches=[])).push(t); }
    render(); var ni=document.getElementById("rsOnbTagInput"); if(ni) ni.focus();
  }
  function onbSubNext(){
    if(!(S.onb.subniches||[]).length){ S.onb.error=L("Elige al menos una etiqueta — es la clave del match.","Pick at least one tag — it's the key to the match."); return render(); }
    onbTrack("onb_step_completed"); onbGoto("seed");
  }
  // Dispara EN 2º PLANO el descubrimiento (relatedProfiles del seed → gate Groq →
  // competidores+reels). Una sola vez por onboarding. seed opcional: vacío = fallback
  // hashtag en el backend.
  function onbFireDiscover(){
    if(isDemo() || S.onb._discoverFired) return;
    S.onb._discoverFired=true;
    try{ apiPost("/api/onboarding/discover-niche",{niche:S.onb.niche, subniches:S.onb.subniches||[], seed:S.onb.seed||""}); }catch(e){}
  }
  function onbSeedNext(){
    var inp=document.getElementById("rsOnbSeed");
    var h=(inp?inp.value:S.onb.seed||"").trim().replace(/^@+/,"").toLowerCase();
    if(!/^[a-z0-9._]{2,30}$/.test(h)){ S.onb.error=L("Escribe una cuenta de Instagram sin @ (o pulsa «No sé a quién poner»).","Type an Instagram account without @ (or hit «Not sure who to add»)."); S.onb.seed=h; return render(); }
    S.onb.seed=h;
    onbFireDiscover();
    onbTrack("onb_step_completed",{seed:h}); onbGoto("goal");
  }
  function onbSeedSkip(){
    S.onb.seed="";
    onbFireDiscover();   // sin seed → el backend cae a hashtag
    onbTrack("onb_step_skipped"); onbGoto("goal");
  }
  // Reels reciclados del subnicho (recycling library). Demo siembra local.
  function onbLoadValue(){
    S.onb.valueLoading=true; S.onb.valueReels=[]; S.onb.ahaReel=null; S.onb.ahaScript=null; S.onb.ahaLoading=false;
    render();   // pinta el estado de CARGA ya (skeletons + botón deshabilitado) → sin
                // flash del cold-start con el botón activo (evita saltarse el paso sin querer).
    var done=function(reels){ S.onb.valueReels=reels||[]; S.onb.valueLoading=false; if(S.onb.step==="value") render(); };
    if(isDemo()){ setTimeout(function(){ done(onbDemoValueReels()); }, 1400); return; }
    apiGet("/api/niche/trending-reels?niche="+encodeURIComponent(S.onb.niche||"")+"&subniches="+encodeURIComponent((S.onb.subniches||[]).join(","))).then(function(r){
      done((r.ok&&r.d&&Array.isArray(r.d.reels))?r.d.reels:[]);
    });
  }
  function onbValueNext(){ onbTrack("onb_step_completed",{value_reels:(S.onb.valueReels||[]).length}); if(!(S.onb.competitors||[]).length) S.onb.compLoading=true; onbGoto("competitors"); onbLoadComps(); }
  // AHA: roba el reel i → genera el guion en su voz y lo muestra en el flujo.
  function onbValueSteal(i){
    var r=(S.onb.valueReels||[])[i]; if(!r) return;
    S.onb.ahaReel=r; S.onb.ahaScript=null; S.onb.ahaLoading=true; onbTrack("onb_aha_steal",{handle:r.handle}); render();
    var done=function(script){ S.onb.ahaScript=script||onbDemoAhaScript(r); S.onb.ahaLoading=false; if(S.onb.step==="value") render(); };
    if(isDemo()){ setTimeout(function(){ done(onbDemoAhaScript(r)); }, 1700); return; }
    // Prod: endpoint de onboarding (sin tracking ni créditos). Si falla, cae a un
    // guion derivado del reel para no romper el aha (LLM real: fase 2).
    var lang=(typeof rsLang==="function")?rsLang():(document.documentElement.lang||"es");
    apiPost("/api/onboarding/aha-script",{ caption:r.caption, handle:r.handle, niche:S.onb.niche, subniches:S.onb.subniches||[], language:lang }).then(function(rr){
      done(rr&&rr.ok&&rr.d&&rr.d.script ? normAhaScript(rr.d.script) : null);
    }).catch(function(){ done(null); });
  }
  function onbValueReset(){ S.onb.ahaReel=null; S.onb.ahaScript=null; S.onb.ahaLoading=false; render(); }
  function normAhaScript(s){
    if(!s) return null;
    if(typeof s==="string"){ var p=s.split("\n").map(function(x){return x.trim();}).filter(Boolean); return {hook:p[0]||s, beats:p.slice(1,Math.max(1,p.length-1)), close:p.length>1?p[p.length-1]:""}; }
    return { hook:s.hook||"", beats:Array.isArray(s.beats)?s.beats:[], close:s.close||"" };
  }
  // Guion del aha para demo / respaldo — derivado del reel y el subnicho.
  function onbDemoAhaScript(r){
    var sub=(S.onb.subniches||[])[0]||S.onb.niche||"tu tema";
    var cap=(r&&r.caption?r.caption:"este tema").replace(/\.+$/,"");
    return {
      hook:"¿Y si "+sub+" no fuera tan complicado como te lo han pintado?",
      beats:[
        "Esto es lo que vi en el reel que petó: "+cap.charAt(0).toLowerCase()+cap.slice(1)+".",
        "El error que comete casi todo el mundo — y por qué te está frenando.",
        "Hazlo así, en 3 pasos, y nota el cambio esta misma semana."
      ],
      close:"Guarda este guión y grábalo hoy. Tu próximo viral empieza aquí."
    };
  }
  // Competidores pre-sugeridos del subnicho (1 clic). Demo siembra local.
  function onbLoadComps(){
    if((S.onb.competitors||[]).length){ return; }   // ya cargados (volver atrás)
    S.onb.compLoading=true;
    var done=function(list){ S.onb.competitors=(list||[]).map(function(c,i){ return {handle:c.handle, reason:c.reason||"", picked:i<2}; }); S.onb.compLoading=false; if(S.onb.step==="competitors") render(); };
    if(isDemo()){ setTimeout(function(){ done(onbDemoComps()); }, 1100); return; }
    apiPost("/api/onboarding/suggest-competitors",{handle:S.onb.handle, platform:S.onb.platform, niche:S.onb.niche, subniches:S.onb.subniches}).then(function(r){
      done((r.ok&&r.d&&Array.isArray(r.d.creators))?r.d.creators:[]);
    });
  }
  function onbCompToggle(h){ (S.onb.competitors||[]).forEach(function(c){ if(c.handle===h) c.picked=!c.picked; }); render(); }
  function onbCompAdd(){
    var inp=document.getElementById("rsOnbCompInput"); if(!inp) return;
    var h=inp.value.trim().replace(/^@+/,"").toLowerCase(); inp.value="";
    if(!/^[a-z0-9._]{1,30}$/.test(h)) return render();
    if(!(S.onb.competitors||[]).some(function(c){return c.handle===h;})) (S.onb.competitors||(S.onb.competitors=[])).unshift({handle:h, reason:"añadido a mano", picked:true});
    render();
  }
  function onbCompsNext(){
    if((S.onb.competitors||[]).filter(function(c){return c.picked;}).length<1){ S.onb.error=L("Elige al menos un competidor para llenar tu radar.","Pick at least one competitor to fill your radar."); return render(); }
    onbNext();
  }
  function onbPickGoal(k){ S.onb.goal=k; render(); }
  function onbGoalNext(){ if(!S.onb.goal){ S.onb.error=L("Elige un objetivo — adapta el tono.","Pick a goal — it tunes the tone."); return render(); } onbNext(); }
  // Tras el onboarding, arranca el House Tour (orden Fathom: onboarding → tour).
  // El auto-start de index.html se inhibe mientras la pantalla de onboarding existe.
  function onbStartTour(){ if(typeof window.startTour==="function"){ setTimeout(function(){ try{ window.startTour(); }catch(e){} }, 700); } }
  // Pantalla de espera tras el onboarding: mientras Apify descubre+scrapea los creadores
  // del nicho (auto-seguidos en el paso de tags), el radar arranca con reels ACERTADOS.
  function onbWaitHTML(){
    var seed=(S.onb&&S.onb.seed)?("@"+String(S.onb.seed).replace(/^@+/,"")):"";
    var step1 = seed ? L("Analizando a <b>"+ESC(seed)+"</b>","Analyzing <b>"+ESC(seed)+"</b>") : L("Analizando tu nicho","Analyzing your niche");
    // v=137 (rediseño Claude Design «Preparando tu radar»): SIN chip de perfil — radar
    // limpio centrado a pantalla completa (el full-screen lo da .onb-fs en render).
    var seedAt=(S.onb&&S.onb.seed)?("@"+String(S.onb.seed).replace(/^@+/,"")):"@tucreador";
    return '<div class="scroll"><div class="canvas"><div class="onbwait">'+
      '<div class="onbw-radar" aria-hidden="true">'+
        '<div class="onbw-grid1"></div><div class="onbw-grid2"></div><div class="onbw-grid3"></div>'+
        '<div class="onbw-sweep"><div class="onbw-fan"></div><div class="onbw-arm"></div></div>'+
        '<span class="onbw-blip" style="top:24%;left:70%;width:9px;height:9px;animation-delay:.3s"></span>'+
        '<span class="onbw-blip" style="top:66%;left:30%;width:7px;height:7px;animation-delay:1.1s"></span>'+
        '<span class="onbw-blip" style="top:72%;left:62%;width:8px;height:8px;animation-delay:1.8s"></span>'+
        '<span class="onbw-blip" style="top:32%;left:34%;width:6px;height:6px;animation-delay:2.3s"></span>'+
        '<div class="onbw-core-wrap"><div class="onbw-core">'+IC.bolt+'</div></div>'+
      '</div>'+
      '<div class="onbw-cards" aria-hidden="true">'+
        '<span class="onbw-card"><span class="onbw-card-ava"></span>'+L("Reel encontrado · 4.2M views","Reel found · 4.2M views")+'</span>'+
        '<span class="onbw-card c2"><span class="onbw-card-ava"></span>'+L("Creador afín · "+seedAt,"Similar creator · "+seedAt)+'</span>'+
      '</div>'+
      '<div class="onbw-title">'+L("Preparando tu radar…","Preparing your radar…")+'</div>'+
      '<div class="onbw-steps">'+
        '<div class="onbw-step s1"><i></i>'+step1+'</div>'+
        '<div class="onbw-step s2"><i></i>'+L("Buscando creadores afines de tu nicho","Finding similar creators in your niche")+'</div>'+
        '<div class="onbw-step s3"><i></i>'+L("Detectando tendencias de tu nicho","Detecting your niche's trends")+'</div>'+
        '<div class="onbw-step s4"><i></i>'+L("Midiendo qué ganchos retienen más","Measuring which hooks retain best")+'</div>'+
        '<div class="onbw-step s5"><i></i>'+L("Trayéndote sus reels más acertados","Pulling their most on-point reels")+'</div>'+
      '</div>'+
      '<div class="onbw-prog" aria-hidden="true"><div class="onbw-prog-fill"></div></div>'+
      '<div class="onbw-hint">'+L("Suele tardar menos de un minuto…","Usually under a minute…")+'</div>'+
    '</div></div></div>';
  }
  function onbWaitForNiche(){
    var t0=Date.now(), HARD=75000, lastN=-1, stable=0;   // tope duro: nunca se atasca
    var _pq=(S.brandId&&S.brandId!=="default")?("?project_id="+encodeURIComponent(S.brandId)):"";
    // Sondeo SILENCIOSO (sin loadBrandData → sin repintar el skeleton → sin parpadeo):
    // solo trae reels y, cuando cuajan, suelta al radar UNA vez.
    (function loop(){
      if(!S._onbWaiting) return;
      apiGet("/api/tracked-creators/reels"+(_pq?_pq+"&":"?")+"sort=explosion&limit=24").then(function(r){
        if(!S._onbWaiting) return;
        if(r&&r.ok&&r.d&&Array.isArray(r.d.reels)){ S.reels=r.d.reels.map(normReel); S.radarSeed=!!r.d.seed; }
        var n=(S.reels||[]).length, el=Date.now()-t0;
        var hasReal=(n>0 && !S.radarSeed);   // reels REALES del nicho (no el seed de la pool vieja)
        if(hasReal){ if(n===lastN) stable++; else { stable=0; lastN=n; } }
        // listo = hay reels reales Y (ya hay pool, O llevan ~7s sin crecer, O tope duro).
        if((hasReal && (n>=6 || stable>=2)) || el>HARD){
          // MÍNIMO de pantalla de carga (el descubrimiento suele acabar durante los pasos
          // del onboarding → sin esto soltaba en <1s y parecía que saltaba directo / "datos
          // hardcodeados"). Esperamos a que se vea la animación de "analizando".
          var MIN=4800;
          var fire=function(){
            if(!S._onbWaiting) return;
            S._onbWaiting=false; loadTracked();
            if(hasReal){ onbRadarCatchup(); } else { showToast(L("Tu radar se está llenando — pulsa «Actualizar radar» en un momento.","Your radar is filling — hit «Refresh radar» in a moment.")); }
            onbShowStealOffer();   // #6: «¿quieres robar este?» antes del tour (si no hay reel → tour directo)
          };
          setTimeout(fire, Math.max(0, MIN-el));
          return;
        }
        setTimeout(loop, 3500);
      });
    })();
  }
  // Tras soltar al radar, sigue refrescando en 2º plano (~1 min) para que aparezcan los
  // reels de creadores que terminen de scrapearse más tarde — sin pedir refresco manual.
  // Usa el refresco LIGERO (sin skeleton) para no parpadear. Para cuando deja de crecer.
  function onbRadarCatchup(){
    var tries=0, lastN=(S.reels||[]).length, stable=0;
    (function loop(){
      if(++tries>9) return;   // ~72s máx
      if(typeof _refreshReelsLight==="function"){ try{ _refreshReelsLight(); }catch(e){} }
      setTimeout(function(){
        var n=(S.reels||[]).length;
        if(n===lastN) stable++; else { stable=0; lastN=n; }
        if(stable>=3) return;   // 3 sondeos sin crecer = la pool ya cuajó
        loop();
      }, 8000);
    })();
  }
  /* #6 (reunión 24-jun, David): tras la pantalla de carga y ANTES del house tour,
     enseña el reel más fuerte del nicho y ofrece robarlo → el «aha» con un click.
     Si dice Sí → robo real (con red caption-only si Apify cae). Si No → tour. */
  // ── COFRE DE REELS (Claude Design 26-jun): avalancha de cientos de vídeos cruzando →
  // ¡pam! quedan 3 → roba uno (estilo cofre Clash Royale / Pokémon starter). Reemplaza
  // el offer plano de 1 reel. Canvas 2D portado del bundle; 3 reels REALES del radar. ──
  var COFRE_AV=2.6, COFRE_FL=2.5, COFRE_TARGET=1247;
  function _cofreReels(){ return Array.isArray(S._cofreReels)?S._cofreReels:[]; }
  function _cofreDemoReels(){
    var mk=function(id,u,v,e,cap){ return normReel({id:id,username:u,views:v,explosion_score:e,caption:cap,thumb_url:null}); };
    return [ mk("cofre1","viral.cocina",2400000,4,"Los 3 errores que cargan tu salsa"),
             mk("cofre2","javi.fit",1100000,6,"Nadie te cuenta esto de las dominadas"),
             mk("cofre3","marta.ahorra",870000,3,"Ahorré 5.000€ sin enterarme") ];
  }
  // Elige el top-3 de reels (recientes ≤3 sem por explosión; si no, top-3 a secas). En demo
  // cae a reels de muestra. Devuelve true si hay al menos 1.
  function _cofrePickReels(){
    var _pool=(S.reels||[]); var _now=Date.now(), _win=21*24*3600*1000;
    var _byExp=function(a,b){ return (b.explosion||0)-(a.explosion||0); };
    var _recent=_pool.filter(function(r){ return r.postedTs && (_now-r.postedTs)<=_win; }).sort(_byExp);
    var three=(_recent.length>=3?_recent:_pool.slice().sort(_byExp)).slice(0,3);
    if(three.length<3 && isDemo()){ three=_cofreDemoReels(); S.reels=three; }
    S._cofreReels=three;
    return three.length>0;
  }
  // El COFRE arranca YA: su avalancha ES la carga (Leo 26-jun: fuera la pantalla
  // «Preparando tu radar»). Los reels del nicho se cargan DURANTE la avalancha; al
  // terminar (y con reels listos) se pasa a «elegir».
  function onbShowStealOffer(){
    var reduced=false; try{ reduced=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches; }catch(e){}
    S.onbStealOffer={}; S._cofreReels=null;
    S.onbCofre={phase:reduced?'choose':'avalanche', selected:-1, _started:false, _reelsReady:false, _avalancheDone:reduced};
    if(isDemo() || (S.reels&&S.reels.length)){ _cofrePickReels(); S.onbCofre._reelsReady=true; }
    render();
    if(!isDemo() && !S.onbCofre._reelsReady) _cofreLoadReels();   // carga reels en 2º plano
  }
  // Carga los reels del nicho mientras corre la avalancha (sin pantalla de carga). Cuando
  // hay reels: si la avalancha ya terminó → «elegir»; si no, espera a que termine.
  function _cofreLoadReels(){
    var t0=Date.now(), HARD=18000;
    var _pq=(S.brandId&&S.brandId!=="default")?("?project_id="+encodeURIComponent(S.brandId)):"";
    (function loop(){
      if(!S.onbStealOffer || !S.onbCofre) return;
      apiGet("/api/tracked-creators/reels"+(_pq?_pq+"&":"?")+"sort=explosion&limit=24").then(function(r){
        if(!S.onbStealOffer || !S.onbCofre) return;
        if(r&&r.ok&&r.d&&Array.isArray(r.d.reels)){ S.reels=r.d.reels.map(normReel); S.radarSeed=!!r.d.seed; }
        var n=(S.reels||[]).length, el=Date.now()-t0;
        if(n>=3 || el>HARD){
          var got=_cofrePickReels();
          if(!got){ S.onbStealOffer=null; S.onbCofre=null; try{ loadTracked(); }catch(e){} return onbStartTour(); }  // sin reels → tour directo
          S.onbCofre._reelsReady=true; try{ loadTracked(); onbRadarCatchup(); }catch(e){}
          if(S.onbCofre._avalancheDone){ S.onbCofre.phase='choose'; render(); setTimeout(function(){ if(S.onbCofre) S.onbCofre._shown=true; }, 760); }
          return;
        }
        setTimeout(loop, 2500);
      });
    })();
  }
  function _cofreGrad(i){ return ['linear-gradient(155deg,#1d2f6b,#0c1330)','linear-gradient(155deg,#3a2566,#140c2e)','linear-gradient(155deg,#0f3f4a,#08181f)'][i%3]; }
  function _cofreCard(r,i,sel){
    var isSel=sel===i, dim=sel>=0&&!isSel, h=(r.creator&&r.creator.handle)||"";
    var thumb=r.thumb?'<img class="cofre-card-img" src="'+ESC(r.thumb)+'" alt=""/>':'';
    var ratio=(r.explosionTxt!=null?r.explosionTxt:1);
    var shown=!!(S.onbCofre&&S.onbCofre._shown);   // ya entraron → no re-animar en re-render (anti-parpadeo)
    return '<div class="cofre-card'+(isSel?' sel':'')+(dim?' dim':'')+(shown?' shown':'')+'" style="animation-delay:'+(i*0.11).toFixed(2)+'s" data-act="onb-cofre-steal" data-id="'+ESC(r.id)+'">'+
      '<div class="cofre-card-thumb" style="'+(r.thumb?'':'background:'+_cofreGrad(i)+';')+'">'+thumb+
        '<span class="cofre-card-916">9:16</span>'+
        '<span class="cofre-card-ratio">×'+ESC(String(ratio))+'<small>'+L("su media","avg")+'</small></span>'+
        '<span class="cofre-card-play"></span>'+
        '<span class="cofre-card-cap"><b>@'+ESC(h)+'</b><span>'+ESC(String(r.cap||"").slice(0,64))+'</span></span>'+
      '</div>'+
      '<div class="cofre-card-views"><b>'+ESC(r.views||"")+'</b> '+L("vistas","views")+'</div>'+
      '<button class="cofre-card-btn'+(isSel?' sel':'')+'" data-act="onb-cofre-steal" data-id="'+ESC(r.id)+'">'+(isSel?L("Robado","Stolen"):L("Roba la idea","Steal the idea"))+' ⚡</button>'+
    '</div>';
  }
  function onbStealOfferHTML(){
    if(!S.onbStealOffer) return "";
    var cof=S.onbCofre||{phase:'choose',selected:-1};
    var reels=_cofreReels();
    // NO cortar aquí: la AVALANCHA dibuja thumbs SINTÉTICOS (no necesita reels reales) y en
    // cuentas reales los reels cargan en 2º plano → si devolvíamos "" mientras tanto, la
    // pantalla quedaba EN BLANCO (solo el fondo .onb-fs) toda la carga. El guard de reels
    // solo aplica a «elegir» (esa sí necesita las 3 cartas reales). Bug Leo 05-jul.
    var inner;
    if(cof.phase==='avalanche'){
      inner='<div class="cofre-av">'+
        '<div class="cofre-eyebrow">'+L("RADAR EN MARCHA","RADAR RUNNING")+'</div>'+
        '<div class="cofre-count" id="rsCofreCount">0</div>'+
        '<div class="cofre-av-t">'+L("Comparando vídeos de tu nicho…","Comparing videos across your niche…")+'</div>'+
        '<div class="cofre-bar"><div class="cofre-bar-fill" id="rsCofreBar"></div></div>'+
      '</div>';
    } else {
      if(!reels.length) return "";   // «elegir» sin cartas reales no tiene sentido (la avalancha sí)
      var cards=reels.slice(0,3).map(function(r,i){ return _cofreCard(r,i,cof.selected); }).join("");
      var selR=cof.selected>=0?reels[cof.selected]:null;
      inner='<div class="cofre-choose">'+
        '<div class="cofre-head">'+
          '<div class="cofre-eyebrow cofre-eyebrow--ok"><span class="cofre-pip"></span>'+L("He visto 1.247 vídeos de tu nicho","I scanned 1,247 videos in your niche")+'</div>'+
          '<h1 class="cofre-h1">'+L("Estos 3 son los que más están ","These 3 are ")+'<em>'+L("petando","blowing up")+'</em>.</h1>'+
          '<p class="cofre-sub">'+L("Roba uno y te lo convierto en <b>TU guion</b>.","Steal one and I'll turn it into <b>YOUR script</b>.")+'</p>'+
        '</div>'+
        '<div class="cofre-cards">'+cards+'</div>'+
        (selR?'<div class="cofre-foot"><div class="cofre-robbing"><span class="cofre-pip cofre-pip--b"></span>'+L("Robando ","Stealing ")+'<b>@'+ESC((selR.creator&&selR.creator.handle)||"")+'</b>'+L(" — generando tu guion…"," — generating your script…")+'</div></div>':'')+
      '</div>';
    }
    return '<div class="cofre"><div class="cofre-glow"></div><canvas id="rsCofreCanvas" class="cofre-canvas"></canvas><div class="cofre-vignette"></div>'+inner+'</div>';
  }
  // Monta y corre la avalancha del cofre (canvas) UNA vez; al acabar → fase «elegir».
  function mountCofre(){
    var cof=S.onbCofre; if(!cof || cof.phase!=='avalanche' || cof._started) return;
    var cv=document.getElementById('rsCofreCanvas'); if(!cv) return;
    cof._started=true;
    _cofreRunAvalanche(cv, function(){
      if(!S.onbCofre) return;
      S.onbCofre._avalancheDone=true;
      // Pasa a «elegir» SOLO si los reels ya cargaron; si no, se queda en la avalancha
      // (el loader pasará a «elegir» en cuanto lleguen) → nunca cartas vacías.
      if(S.onbCofre._reelsReady){ S.onbCofre.phase='choose'; render(); setTimeout(function(){ if(S.onbCofre) S.onbCofre._shown=true; }, 760); }
    });
  }
  function _cofreRunAvalanche(cv, onDone){
    var ctx, W, H, thumbs, span, tw=56, th=100, start, raf;
    var _now=function(){ return (window.performance&&performance.now)?performance.now():Date.now(); };
    function size(){ var r=cv.getBoundingClientRect(); var dpr=Math.min(window.devicePixelRatio||1,2); cv.width=Math.max(1,Math.round(r.width*dpr)); cv.height=Math.max(1,Math.round(r.height*dpr)); ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0); W=r.width||1200; H=r.height||800; }
    function build(){ var hues=[222,250,268,205,234,290], spacing=86, lanes=Math.max(5,Math.round(H/120)); span=W+320; thumbs=[];
      for(var l=0;l<lanes;l++){ var y=(H/(lanes+1))*(l+1)+(Math.random()-0.5)*40, rot=(Math.random()-0.5)*0.28, dir=(l%2===0?1:-1), speed=dir*(520+Math.random()*360), count=Math.ceil(span/spacing)+2;
        for(var i=0;i<count;i++){ thumbs.push({y:y,rot:rot,speed:speed,x0:i*spacing+Math.random()*30,hue:hues[(l+i)%hues.length],sc:0.78+Math.random()*0.5,a:0.62+Math.random()*0.38}); } } }
    // Reels MÁS visibles sobre el fondo casi-negro (antes hsl 24%/12% = apenas se veían).
    function drawThumb(x,y,rot,sc,a,hue){ ctx.save(); ctx.globalAlpha=a; ctx.translate(x,y); ctx.rotate(rot); ctx.scale(sc,sc); ctx.beginPath(); if(ctx.roundRect) ctx.roundRect(-tw/2,-th/2,tw,th,9); else ctx.rect(-tw/2,-th/2,tw,th);
      var g=ctx.createLinearGradient(0,-th/2,0,th/2); g.addColorStop(0,'hsl('+hue+' 62% 46%)'); g.addColorStop(1,'hsl('+(hue+16)+' 66% 28%)'); ctx.fillStyle=g; ctx.fill();
      ctx.lineWidth=1; ctx.strokeStyle='rgba(255,255,255,0.16)'; ctx.stroke();
      ctx.globalAlpha=a*0.6; ctx.fillStyle='rgba(255,255,255,0.95)'; ctx.beginPath(); ctx.moveTo(-4,-7); ctx.lineTo(8,0); ctx.lineTo(-4,7); ctx.closePath(); ctx.fill(); ctx.restore(); }
    function draw(t){ if(!ctx) return; ctx.clearRect(0,0,W,H);
      var cntEl=document.getElementById('rsCofreCount'), barEl=document.getElementById('rsCofreBar');
      if(cntEl){ var p=Math.min(t/2.2,1), e=1-Math.pow(1-p,3); cntEl.textContent=String(Math.round(e*COFRE_TARGET)).replace(/\B(?=(\d{3})+(?!\d))/g,'.'); }
      if(barEl) barEl.style.width=Math.min(t/2.2,1)*100+'%';
      var CV=COFRE_AV*0.74; var conv=t<=CV?0:Math.min((t-CV)/(COFRE_AV-CV),1); var ce=conv<0.5?2*conv*conv:1-Math.pow(-2*conv+2,2)/2; var cx=W/2, cy=H/2;
      for(var k=0;k<thumbs.length;k++){ var tb=thumbs[k]; var x=(((tb.x0+tb.speed*t)%span)+span)%span-160; var px=x+ce*(cx-x), py=tb.y+ce*(cy-tb.y), sc=tb.sc*(1-0.85*ce), a=tb.a*(1-ce*ce); if(a<=0.01) continue; drawThumb(px,py,tb.rot*(1-ce),sc,a,tb.hue); }
      if(conv>0.15){ var g=ctx.createRadialGradient(cx,cy,0,cx,cy,260*ce); g.addColorStop(0,'rgba(120,150,255,'+(0.5*ce)+')'); g.addColorStop(1,'rgba(120,150,255,0)'); ctx.fillStyle=g; ctx.fillRect(0,0,W,H); }
      if(t>COFRE_FL){ var u=t-COFRE_FL, fa=u<0.08?u/0.08:Math.max(0,1-(u-0.08)/0.45); if(fa>0){ var g2=ctx.createRadialGradient(cx,cy,0,cx,cy,Math.max(W,H)*0.7); g2.addColorStop(0,'rgba(255,255,255,'+(0.9*fa)+')'); g2.addColorStop(0.4,'rgba(160,185,255,'+(0.5*fa)+')'); g2.addColorStop(1,'rgba(160,185,255,0)'); ctx.fillStyle=g2; ctx.fillRect(0,0,W,H); } } }
    function loop(){
      // Cinturón: si un render() reconstruyó el canvas por debajo, re-vincula al VIVO
      // (si no, seguiríamos dibujando en el canvas viejo desconectado y no se vería nada).
      var live=document.getElementById('rsCofreCanvas'); if(live && live!==cv){ cv=live; try{ size(); }catch(e){} }
      var t=(_now()-start)/1000; try{ draw(t); }catch(e){} if(t<COFRE_AV+0.4){ raf=requestAnimationFrame(loop); } else if(ctx){ ctx.clearRect(0,0,W,H); } }
    function begin(){
      try{ size(); build(); }catch(e){ if(onDone) onDone(); return; }
      start=_now(); loop();
      setTimeout(function(){ try{ if(onDone) onDone(); }catch(e){} }, COFRE_AV*1000+60);
    }
    // Diferir un frame si el canvas aún mide 0: mountCofre corre justo tras meter el
    // innerHTML, cuando el canvas puede no estar maquetado → su backing store quedaría en
    // 1px y la avalancha (los reels de fondo) se dibujaría en la nada, aunque los números
    // (DOM) sí animaran. Esperar al 1er frame garantiza tamaño real. Bug Leo 29-jun.
    // Reintenta por frames hasta que el canvas tenga tamaño real (un solo rAF a veces no
    // basta si el layout aún no está listo). Sin esto el backing store queda en 1px y la
    // avalancha se dibuja en la nada, aunque los números (DOM) sí animen. Bug Leo.
    var _tries=0;
    function tryBegin(){ var rc=cv.getBoundingClientRect();
      if((rc.width<2||rc.height<2) && _tries<15){ _tries++; requestAnimationFrame(tryBegin); return; }
      begin();
    }
    tryBegin();
  }
  // CIERRE: ingiere (prod) → Cerebro ~50% + 1er guión; demo simula y siembra panel.
  function onbFinish(){
    onbTrack("onb_step_completed");
    // Onboarding nuevo = Cerebro DESDE 0 → tras el tutorial sale el botón «¡Subir de
    // nivel!» (reclamar N1). Reinicia el nivel reclamado y el XP diario (localStorage)
    // sin depender de limpiar el navegador a mano.
    try{ brainClaimedSet(0); brainDailyXpSet(0); S._canLvlSeen=null; }catch(e){}
    var picked=(S.onb.competitors||[]).filter(function(c){return c.picked;});
    onbTrack("onb_completed",{competitors:picked.length, value_reels:(S.onb.valueReels||[]).length});
    if(isDemo()){
      S.onb.skipped=true; S.user.onbV2Done=true;
      try{ var b=brand(); if(b){ b.voice=Math.max(b.voice||0,50); b.level=Math.max(b.level||1,2); } }catch(e){}
      if(typeof seedDemoContent==="function" && !(S.reels||[]).length){ try{ seedDemoContent(); }catch(e){} }
      // Directo al COFRE (Leo 26-jun: fuera la pantalla «Preparando tu radar»; la
      // avalancha del cofre ES la carga). Demo ya tiene reels sembrados.
      S.radarSeed=false; S.tab="dashboard";
      onbShowStealOffer();
      return;
    }
    S.onb.busy=true; render();
    var body={ handle:S.onb.handle, platform:S.onb.platform, niche:S.onb.niche, subniches:S.onb.subniches||[], goal:S.onb.goal||"", competitors:picked.map(function(c){return c.handle;}) };
    var _pid=_pidOf(S.brandId); if(isAgency()&&_pid) body.project_id=_pid;
    apiPost("/api/onboarding/complete",body).then(function(r){
      S.onb.busy=false; S.onb.skipped=true; S.user.onbV2Done=true;
      reloadScripts().then(function(){ render(); });   // F9: el guion «aha» persistido server-side aparece en Ideas robadas YA (no tras recargar)
      var v=(r.ok&&r.d&&r.d.voice!=null)?r.d.voice:50;
      try{ brand().voice=Math.max(brand().voice||0,v); }catch(e){}
      // Directo al COFRE (Leo 26-jun): su avalancha ES la carga. Los reels del nicho se
      // cargan en 2º plano DURANTE la avalancha (_cofreLoadReels); al terminar → elegir.
      S.radarSeed=false; S.reels=[]; S.tab="dashboard";
      onbShowStealOffer();
      showToast(L("Cerebro al "+v+"% · buscando los reels más acertados de tu nicho…","Brain at "+v+"% · finding the most on-point reels for your niche…"));
    });
  }
  function onbSkip(){ onbTrack("onb_skipped"); S.onb.skipped=true; if(!isDemo()){ try{ apiPost("/api/onboarding/complete",{handle:S.onb.handle, skipped:true, niche:S.onb.niche, subniches:S.onb.subniches||[]}); }catch(e){} } S.tab="dashboard"; render(); }

  /* ── datos demo (para que el flujo sea clicable sin backend) ── */
  function onbDemoValueReels(){
    var subs=(S.onb.subniches||[]); var s=function(i){ return subs[i%Math.max(1,subs.length)]||S.onb.niche||"tu nicho"; };
    var th=_demoThumbs()||[]; var pic=function(i){ return th.length?th[i%th.length]:null; };
    return [
      {handle:"nicho_top1", mult:"5.8", views:"1,4 M", tag:"explota", caption:"El error de "+s(0)+" que todos cometen (y cómo evitarlo)", thumb:pic(0)},
      {handle:"creador_ref", mult:"3.2", views:"680 K", tag:"explota", caption:"Probé "+s(1)+" durante 30 días — esto pasó", thumb:pic(1)},
      {handle:"viral_"+_norm(s(0)).slice(0,5), mult:"2.6", views:"420 K", tag:"subiendo", caption:"3 trucos de "+s(0)+" que nadie te cuenta", thumb:pic(2)},
      {handle:"top_"+_norm(S.onb.niche||"nicho").slice(0,4), mult:"2.1", views:"310 K", tag:"subiendo", caption:"Por qué tu "+s(0)+" no funciona", thumb:pic(3)}
    ];
  }
  function onbDemoComps(){
    var n=_norm(S.onb.niche||"nicho").slice(0,6);
    return [
      {handle:n+"_pro", reason:"Referente de "+(S.onb.niche||"tu nicho")+", publica casi a diario"},
      {handle:"the_"+n, reason:"Mismo subnicho, sus reels petan seguido"},
      {handle:n+"_daily", reason:"Crece rápido en "+((S.onb.subniches||[])[0]||"tu tema")},
      {handle:"miss_"+n, reason:"Tono cercano, buena referencia de estructura"}
    ];
  }

  /* B1: "tu próxima serie" — sugerencia del Cerebro a partir de lo que petó en TU
     cuenta (S.metrics.insights.next = {title, views, message}). Surface como CARD
     HERO en el Dashboard y bajo "Lo que funciona" en el Cerebro. Si no hay next,
     no renderiza nada (sin hueco). Misma tarjeta en ambos sitios. */
  function nextSeries(){ return (S.metrics && S.metrics.insights && S.metrics.insights.next) || null; }
  // T1 (IDI): una sola acción primaria por pantalla. En el Dashboard el CTA es
  // secundario (el primario es el «Roba la idea» de la Oportunidad #1); en el
  // Cerebro sigue primario porque ahí ES la acción principal. ctx: "brain"|"dash".
  function nextSeriesHTML(ctx){
    var nx=nextSeries(); if(!nx || !(nx.title||nx.message)) return '';
    var views=nx.views!=null?(typeof nx.views==="string"?nx.views:fmtNum(nx.views)):"";
    // IDI una-primaria: en Cerebro la primaria es la CTA de nivel del hero mientras
    // quede escalera; solo al nivel máximo (sin CTA de nivel) hereda la primaria.
    var btnCls=(ctx==="brain" && !brainLevel().nextAction)?"btn-primary":"btn-secondary";
    return '<article class="next-series">'+
      '<div class="ns-eyebrow">'+IC.brain+'<span>Tu próxima serie</span>'+(views?'<span class="ns-views" title="Lo que hizo el reel que la inspira">'+IC.eye+' '+ESC(views)+'</span>':'')+'</div>'+
      '<h3 class="ns-title">'+ESC(nx.title||"")+'</h3>'+
      (nx.message?'<p class="ns-msg">'+ESC(nx.message)+'</p>':'')+
      '<div class="ns-actions"><button class="btn btn-md '+btnCls+'" data-act="next-series-go" data-title="'+ESC(nx.title||"")+'">'+IC.bolt+' Desarrollar esta serie</button></div>'+
    '</article>';
  }

  /* Sugerir competidores proactivamente (Fathom 18/06): "@X acaba de petar, síguelo".
     En demo, sugerencia determinista (no seguida aún). En prod la alimenta el backend
     (creadores del nicho con métricas en alza — reutilizar el flujo de scrape). */
  // Pool de demo: 5 candidatos para previsualizar el muro de competidores.
  function _suggDemoPool(){
    return [
      {handle:"ia_con_marcos", x:"×8", tag_es:"Nuevo en tu nicho", tag_en:"New in your niche",
        why_es:"se pegó un reel de 210k (×8 su media)", why_en:"just hit a 210k reel (8× their average)"},
      {handle:"lucia.growth",  x:"↑45%", tag_es:"Está despegando", tag_en:"Taking off",
        why_es:"subió +45% de seguidores esta semana", why_en:"grew +45% in followers this week"},
      {handle:"hooks_diarios", x:"En racha", tag_es:"Petando ahora", tag_en:"Blowing up now",
        why_es:"encadenó 3 reels virales en 7 días", why_en:"chained 3 viral reels in 7 days"},
      {handle:"reels.lab",     x:"×5", tag_es:"Subiendo fuerte", tag_en:"Climbing fast",
        why_es:"tiene un reel reciente de 320k", why_en:"has a recent 320k reel"},
      {handle:"viral.coach",   x:"↑30%", tag_es:"En tu nicho", tag_en:"In your niche",
        why_es:"creció +30% este mes", why_en:"grew +30% this month"}
    ];
  }
  // Lista de sugerencias (demo: pool; real: la trae loadSuggestion del backend).
  function suggestedList(){
    if(isDemo()) return _suggDemoPool();
    return Array.isArray(S._suggList) ? S._suggList : (S._suggReal ? [S._suggReal] : []);
  }
  // Real: carga hasta 5 sugerencias del backend (1 vez), luego re-render.
  function loadSuggestion(){
    if(isDemo() || S._suggDismissed || S._suggLoading) return;
    if(Array.isArray(S._suggList) && S._suggList.length) return;
    S._suggLoading=true;
    apiGet('/api/suggested-competitor?limit=5').then(function(r){
      S._suggLoading=false;
      if(r && r.ok && r.d){
        S._suggReal=r.d.suggestion||null;
        S._suggList=Array.isArray(r.d.suggestions)?r.d.suggestions:(r.d.suggestion?[r.d.suggestion]:[]);
        if(S.tab==="dashboard") bgRender();
      }
    });
  }
  // #5 proactividad anti-churn (David 26-jun): reels que petan en tu nicho de creadores
  // que AÚN NO SIGUES → «róbalo aunque no sea tu competidor». Carga 1 vez (excluye los tuyos).
  function loadDiscover(){
    if(isDemo() || S._discDismissed || S._discLoading || Array.isArray(S.discover)) return;
    S._discLoading=true;
    apiGet('/api/niche/discover?limit=6').then(function(r){
      S._discLoading=false;
      S.discover=(r && r.ok && r.d && Array.isArray(r.d.reels)) ? r.d.reels.map(normReel) : [];
      if(S.tab==="dashboard") bgRender();
    });
  }
  // Reel por id buscándolo TAMBIÉN en discover (para robar uno del descubrimiento).
  function reelById(id){
    var r=(S.reels||[]).filter(function(x){return x.id===id;})[0];
    // #2: los reels de «Sugerencias de hoy» también son buscables → el detalle se abre desde
    // su card. Son normReel-compat (_normSugg → normReel).
    return r || (S._suggToday||[]).filter(function(x){return x.id===id;})[0]
             || (S.discover||[]).filter(function(x){return x.id===id;})[0] || null;
  }
  // Real: ranking del nicho por VIEWS medias/reel (no seguidores — el scrape no los
  // trae). Lo carga 1 vez al entrar en el tab. S.lb = {you, rows[], metric}.
  function loadLeaderboard(force){
    if(isDemo()) return;
    if(!force && (S._lbReal || S._lbLoading)) return;
    S._lbLoading=true;
    var _pid=_pidOf(S.brandId);
    apiGet('/api/leaderboard'+(_pid?('?project_id='+encodeURIComponent(_pid)):'')).then(function(r){
      S._lbLoading=false;
      if(r && r.ok && r.d){ S._lbReal=r.d; if(S.tab==="leaderboard") bgRender(); }
    });
  }
  // Recarga LIGERA de métricas (summary + videos + insights) al entrar en la pestaña
  // Métricas. El scrape del perfil propio se lanza en el paso 1 del onboarding
  // (onbConnectIG) y es síncrono pero tarda ~40-60s; loadBrandData solo lee una vez al
  // inicio, así que sin esto los reels recién scrapeados no aparecen hasta recargar.
  function loadMetricsLight(retries){
    if(isDemo() || S._metricsLoading) return;
    S._metricsLoading=true;
    var q=S.brandId?("?brand="+encodeURIComponent(S.brandId)):"";
    var _pq=(S.brandId&&S.brandId!=="default")?("?project_id="+encodeURIComponent(S.brandId)):"";
    Promise.all([
      fetch("/metrics/summary"+q,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
      fetch("/metrics/videos"+q,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
      fetch("/api/metrics/insights"+_pq,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;})
    ]).then(function(res){
      S._metricsLoading=false;
      var met=res[0], vids=res[1], ins=res[2];
      if(met){ S.metrics=met; S.igConnected=!!(met&&met.connected); }
      S.metrics=S.metrics||{};
      if(vids){ S.metrics.videos=(vids.videos||[]).map(normMetricVideo); }
      if(ins){ S.metrics.insights={ what_works:ins.what_works||[], next:ins.next||null }; }
      if(S.tab==="metrics") bgRender();
      // Si el perfil está conectado pero aún no hay reels (scrape del onboarding en
      // curso), reintenta en silencio mientras sigas en Métricas (~45s) → los datos
      // aparecen solos sin que el user tenga que recargar ni pulsar nada.
      var n=(S.metrics.videos||[]).length;
      if(n===0 && S.igConnected && (retries||0)<5 && S.tab==="metrics"){
        setTimeout(function(){ loadMetricsLight((retries||0)+1); }, 9000);
      }
    });
  }
  // Muro de competidores (Bernat 24-jun): «Creadores que deberías vigilar» — enseña
  // hasta 5 sugeridos; los que CABEN en tu cupo (free=2) son «Añadir», el resto salen
  // BLOQUEADOS (blur + 🔒). Muro psicológico: ves 5, free solo te llevas 2. En plan de
  // pago no hay muro: solo sugerencias añadibles según el hueco que te quede.
  function suggestedCompHTML(){
    if(S._suggDismissed) return '';
    var wall = isDemo() || isFree();   // demo previsualiza el muro
    var tracked=(Array.isArray(S.tracked)?S.tracked:[]).map(function(t){
      return String((t.creator&&t.creator.ig_username)||t.handle||t.ig_username||"").toLowerCase().replace(/^@+/,""); });
    var list=suggestedList().filter(function(c){ return tracked.indexOf(String(c.handle||"").toLowerCase())<0; });
    if(!list.length) return '';
    var used=(S.trackedCount!=null?S.trackedCount:tracked.length);
    var lim=(S.trackedLimit!=null?S.trackedLimit:2);   // free = 2 por defecto
    var freeSlots=Math.max(0, lim-used);
    if(!wall){
      // plan de pago: sin muro. Sin hueco → no molestamos; con hueco → sugerencias añadibles.
      if(freeSlots<=0) return '';
      list=list.slice(0, Math.max(1, freeSlots));
    } else {
      list=list.slice(0, 5);   // muro: hasta 5 visibles
    }
    var rows=list.map(function(c,i){
      var why=c.why || L(c.why_es, c.why_en);   // real → string; demo → bilingüe
      var tag=c.tag || L(c.tag_es, c.tag_en);
      var whyCap=why ? (why.charAt(0).toUpperCase()+why.slice(1)) : "";
      var locked=wall && i>=freeSlots;
      if(locked){
        return '<div class="sugg-comp locked">'+
          '<div class="sugg-ava-wrap">'+onbAvatar(c.handle)+'<span class="sugg-ava-lock">'+IC.lock+'</span></div>'+
          '<div class="sugg-body">'+
            '<div class="sugg-tag sugg-tag--lock">'+IC.lock+' '+L("Bloqueado","Locked")+' · '+ESC(tag)+'</div>'+
            '<div class="sugg-h">@'+ESC(c.handle)+' <span class="sugg-x">'+ESC(c.x||"")+'</span></div>'+
            '<div class="sugg-why">'+ESC(whyCap)+'.</div>'+
          '</div>'+
          '<div class="sugg-actions"><button class="btn btn-sm btn-secondary" data-act="unlock-comp">'+IC.lock+' '+L("Desbloquear","Unlock")+'</button></div>'+
        '</div>';
      }
      return '<div class="sugg-comp">'+
        onbAvatar(c.handle)+
        '<div class="sugg-body">'+
          '<div class="sugg-tag">'+IC.spark+' '+L("Te lo sugiero","Suggested")+' · '+ESC(tag)+'</div>'+
          '<div class="sugg-h">@'+ESC(c.handle)+' <span class="sugg-x">'+ESC(c.x||"")+'</span></div>'+
          '<div class="sugg-why">'+ESC(whyCap)+'. '+L("Añádelo y sus reels entran en tu radar.","Add them and their reels enter your radar.")+'</div>'+
        '</div>'+
        (c.reel&&c.reel.thumb?'<div class="sugg-reel" title="'+L("Su reel que está petando","Their reel that's blowing up")+'"><img src="'+ESC(c.reel.thumb)+'" alt="" loading="lazy"/>'+(c.reel.exp?'<span class="sugg-reel-exp">'+IC.bolt+' '+ESC(String(Math.round(c.reel.exp*10)/10))+'×</span>':'')+'</div>':'')+
        '<div class="sugg-actions"><button class="btn btn-sm btn-primary" data-act="add-suggested" data-id="'+ESC(c.handle)+'">'+IC.plus+' '+L("Añadir","Add")+'</button></div>'+
      '</div>';
    }).join("");
    var nLocked=wall ? Math.max(0, list.length-freeSlots) : 0;
    var head='<div class="sugg-head"><span class="sugg-head-t">'+IC.eye+' '+L("Creadores que deberías vigilar","Creators you should watch")+'</span>'+
      '<button class="sugg-hide" data-act="sugg-dismiss">'+L("Ocultar","Hide")+'</button></div>';
    var foot = nLocked>0
      ? '<button class="btn btn-md btn-primary sugg-unlock-cta" data-act="unlock-comp">'+IC.bolt+' '+L("Desbloquea "+nLocked+" competidor"+(nLocked>1?"es":"")+" más","Unlock "+nLocked+" more competitor"+(nLocked>1?"s":""))+'</button>'
      : '';
    return '<div class="sugg-card">'+head+rows+foot+'</div>';
  }

  // «Gestiona competidores» (reorganización 04/07): fusión en UN bloque plegado de los 3
  // sitios anteriores — la addbar (añadir + contador de plan), el botón «Añadir» de la
  // galería y el acordeón «Tus competidores» (lista con ×). El refresco de pago vive SOLO
  // en el hero (rdr-refresh-cta). openDefault=true con el radar vacío (añadir es LA acción).
  function manageCompetitorsHTML(openDefault){
    var t=Array.isArray(S.tracked)?S.tracked:[];
    var used=(S.trackedCount!=null?S.trackedCount:t.length);
    var lim=S.trackedLimit;
    var atCap=(lim!=null && used>=lim);
    var counter=(lim!=null)
      ? '<span class="addbar-count'+(atCap?" full":"")+'" title="'+L("Competidores en esta marca / tope de tu plan","Competitors in this brand / your plan cap")+'">'+used+' / '+lim+'</span>'
      : '';
    var addWrap='<div class="mc-addwrap"><div class="mc-addrow">'+
      '<button class="addbar-cta'+(S.addCompOpen?" on":"")+'" data-act="add-comp">'+IC.plus+' '+L("Añadir competidor","Add competitor")+'</button>'+
      counter+
    '</div>'+addCompInlineHTML()+analyzingBannerHTML()+'</div>';
    var rows=t.map(function(tt){
      var h=(tt.creator&&tt.creator.ig_username)||tt.ig_username||"";
      var n=(tt.reels_count!=null)?(tt.reels_count+' reel'+(tt.reels_count===1?'':'es')):'';
      var cid=(tt.creator&&tt.creator.id)||tt.creator_id||"";
      // B: la fila abre TODOS los reels del competidor (la × interna gana por closest()).
      return '<div class="brain-comp brain-comp-link" data-act="creator-reels" data-id="'+ESC(String(cid))+'" data-handle="'+ESC(h)+'" role="button" tabindex="0" aria-label="Ver todos los reels de @'+ESC(h)+'"><div class="ava bava">'+ESC(initialsOf(h))+'</div>'+
        '<span class="brain-comp-h">@'+ESC(h)+'</span>'+
        '<span class="brain-comp-n">'+ESC(n)+' ›</span>'+
        '<button class="brain-comp-x" data-act="untrack" data-id="'+ESC(String(tt.id))+'" data-handle="'+ESC(h)+'" title="Dejar de seguir a @'+ESC(h)+'" aria-label="Dejar de seguir a @'+ESC(h)+'">'+IC.x+'</button>'+
      '</div>';
    }).join("");
    var list=rows?('<div class="comp-manage-list">'+rows+'</div>'):'';
    return '<details class="comp-manage" id="rsManageComp"'+(openDefault?' open':'')+'><summary>'+
      L("Gestiona competidores","Manage competitors")+
      '<span class="rs-fold-sub">'+used+(lim!=null?(' / '+lim):'')+'</span></summary>'+
      addWrap+list+'</details>';
  }

  // Sección PLEGADA genérica (reorganización 04/07): reusa el look/CSS de .comp-manage
  // (details nativo → teclado y aria gratis). Lo secundario se pliega, no compite arriba.
  function foldedSectionHTML(id, title, sub, inner, openDefault){
    return '<details class="comp-manage rs-fold" id="'+id+'"'+(openDefault?' open':'')+'><summary>'+title+
      (sub?'<span class="rs-fold-sub">'+ESC(sub)+'</span>':'')+'</summary>'+
      '<div class="rs-fold-body">'+inner+'</div></details>';
  }

  // v3 (mockup David «llena mi semana»): fila degradada con icono azul + chispa
  // naranja, título Clash, píldora «EN LOTE · 1 TOQUE» y CTA azul «Generar N guiones».
  function whaleHTML(count){
    count=count||5;
    return '<div class="whale">'+
      '<div class="whale-l">'+
        '<div class="whale-ic">'+IC.bolt+'</div>'+
        '<div class="whale-tx">'+
          '<div class="whale-h"><span class="whale-title">'+L("Llena mi semana","Fill my week")+'</span>'+
            '<span class="whale-pill">'+L("EN LOTE · 1 TOQUE","BATCH · 1 TAP")+'</span></div>'+
          '<p class="whale-sub">'+L("Cojo tus <b>"+count+" reels más explosivos</b> y te los devuelvo como <b>"+count+" guiones con tu voz</b>, listos para grabar. Sin ir uno a uno.","I grab your <b>"+count+" most explosive reels</b> and hand them back as <b>"+count+" scripts in your voice</b>, ready to record. No going one by one.")+'</p>'+
        '</div>'+
      '</div>'+
      '<button class="btn btn-lg btn-primary" data-act="fillweek">'+IC.bolt+' '+L("Generar "+count+" guiones","Generate "+count+" scripts")+'</button>'+
    '</div>';
  }
  /* #8 PROACTIVIDAD — «Tu siguiente paso»: la gente nueva está perdida; aquí le
     decimos LA acción más útil AHORA (una sola, sin pensar) según señales reales.
     Prioridad = bucle núcleo (seguir→robar→grabar) y luego voz/IG. */
  function dashboardNextStepHTML(){
    if(S.filter==="fav"||S.creatorFilter) return "";   // solo en el dashboard normal
    var s=brainSignals();
    var gus=(S.guiones||[]).filter(function(g){return g.status!=="discarded";});
    var nGui=gus.length, nRec=gus.filter(function(g){return g.status==="recorded";}).length;
    var igOn=!!S.igConnected;
    var st;
    if(s.comps<1){
      st={t:L("Sigue a tu primer competidor","Follow your first competitor"), b:L("Es de quien te traigo las ideas que petan. Añade 1 y arranca tu radar.","They're who I bring you winning ideas from. Add 1 and start your radar."), c:L("Añadir competidor","Add competitor"), act:"add-comp"};
    } else if(nGui<1){
      st={t:L("Roba tu primera idea","Steal your first idea"), b:L("Elige el reel más explosivo de tu radar y te lo convierto en guion, en tu voz.","Pick the most explosive reel on your radar and I'll turn it into a script, in your voice."), c:L("Ver mi radar ↓","See my radar ↓"), act:"dns-feed"};
    } else if(nRec<1){
      st={t:L("Graba tu primer guion","Record your first script"), b:L("Ya tienes un guion listo. Ábrelo en el teleprónter y léelo a cámara — gratis.","You've got a script ready. Open it in the teleprompter and read it to camera — free."), c:L("Ir a Guiones","Go to Scripts"), act:"tab", k:"guiones"};
    } else if((s.voice||0)<50){
      st={t:L("Entrena tu voz","Train your voice"), b:L("Cuanto más me entrenas, más tuyo suena el siguiente guion. 2 minutos.","The more you train me, the more the next script sounds like you. 2 minutes."), c:L("Entrenar mi voz","Train my voice"), act:"tab", k:"brain"};
    } else if(!igOn){
      st={t:L("Conecta tu Instagram","Connect your Instagram"), b:L("Para medir qué de lo que publicas funciona y doblar lo que pega.","To measure what works on what you post and double down on it."), c:L("Conectar Instagram","Connect Instagram"), act:"tab", k:"metrics"};
    } else if(nGui<4){
      st={t:L("Roba tu próxima idea","Steal your next idea"), b:L("Vas "+nGui+"/4 para Nivel 3 — cada guion afina tu Cerebro.","You're at "+nGui+"/4 for Level 3 — every script tunes your Brain."), c:L("Ver mi radar ↓","See my radar ↓"), act:"dns-feed"};
    } else {
      st={t:L("Vas en racha 🔥","You're on a streak 🔥"), b:L("Sigue robando y publicando — cada reel te acerca al siguiente nivel.","Keep stealing and posting — every reel gets you closer to the next level."), c:L("Ver mi radar ↓","See my radar ↓"), act:"dns-feed"};
    }
    var dk=st.k?(' data-k="'+st.k+'"'):"";
    return '<div class="dns">'+
      '<div class="dns-ic">'+IC.bolt+'</div>'+
      '<div class="dns-body"><span class="dns-eyebrow">'+L("HAZ ESTO AHORA","DO THIS NOW")+'</span>'+
        '<div class="dns-t">'+ESC(st.t)+'</div><div class="dns-b">'+ESC(st.b)+'</div></div>'+
      // T1 estricto (David 02-jul): el CTA contextual va SECUNDARIO — el único
      // primario sobre el fold es «Roba la idea» (Oportunidad #1).
      '<button class="btn btn-md btn-secondary dns-cta" data-act="'+st.act+'"'+dk+'>'+ESC(st.c)+'</button>'+
    '</div>';
  }
  /* v3 (mockup David «progreso / cerebro»): fila con icono cerebro azul, nivel +
     contador «N/3 guiones para Nx», descripción y 3 segmentos de progreso, CTA
     «Crear guion». Usa brainLevel() (señales reales) — no inventa el nivel. */
  function radarCerebroRowHTML(){
    var bl=brainLevel();
    var lvlTxt=bl.level>=1?(L("nivel ","level ")+bl.level):L("nuevo","new");
    // Nivel máximo (Viral): barra fija al 100%, sin CTA de subida.
    if(bl.full){
      return '<div class="rcb">'+
        '<div class="rcb-ic">'+IC.brain+'</div>'+
        '<div class="rcb-body">'+
          '<div class="rcb-top"><span class="rcb-lvl">'+L("Tu cerebro · ","Your brain · ")+lvlTxt+'</span>'+
            '<span class="rcb-prog">'+ESC(ecoLevelName(5))+' · '+L("máximo","max")+'</span></div>'+
          '<div class="rcb-desc">'+L("Tu Cerebro está al máximo: clava tu voz y juega para viralizar. Sigue alimentándolo para mantenerlo afilado.","Your Brain is maxed: it nails your voice. Keep feeding it to stay sharp.")+'</div>'+
          '<div class="rcb-xp"><div class="rcb-xp-fill" style="width:100%"></div></div>'+
        '</div>'+
        '<button class="btn btn-md btn-secondary" data-act="tab" data-k="brain">'+IC.brain+' '+L("Ver Cerebro","See Brain")+'</button>'+
      '</div>';
    }
    // Barra del nivel al 100% → CTA de subida (manual).
    if(bl.canLevelUp){
      return '<div class="rcb rcb-up">'+
        '<div class="rcb-ic">'+IC.brain+'</div>'+
        '<div class="rcb-body">'+
          '<div class="rcb-top"><span class="rcb-lvl">'+L("Tu cerebro · ","Your brain · ")+lvlTxt+'</span>'+
            '<span class="rcb-prog">'+L("¡Barra al 100%!","Bar at 100%!")+'</span></div>'+
          '<div class="rcb-desc">'+L("Tu Cerebro está listo para subir a <b>"+ESC(ecoLevelName(bl.next))+"</b>. Recoge el nivel y mira la animación.","Your Brain is ready to reach <b>"+ESC(ecoLevelName(bl.next))+"</b>. Claim it and watch the animation.")+'</div>'+
          '<div class="rcb-xp"><div class="rcb-xp-fill" style="width:100%"></div></div>'+
        '</div>'+
        '<button class="btn btn-md btn-primary" data-act="brain-levelup">'+IC.brain+' '+L("¡Subir de nivel!","Level up!")+'</button>'+
      '</div>';
    }
    // En curso: barra continua (hitos + ejercicio diario) hacia el siguiente nivel.
    var nx=bl.next;
    var cta = bl.exReady
      ? '<button class="btn btn-md btn-primary" data-act="tab" data-k="brain">'+IC.bolt+' '+L("Alimentar +"+BRAIN_DAILY_GAIN+"%","Feed +"+BRAIN_DAILY_GAIN+"%")+'</button>'
      : '<button class="btn btn-md btn-secondary" data-act="tab" data-k="brain">'+IC.brain+' '+L("Ver Cerebro","See Brain")+'</button>';
    return '<div class="rcb">'+
      '<div class="rcb-ic">'+IC.brain+'</div>'+
      '<div class="rcb-body">'+
        '<div class="rcb-top"><span class="rcb-lvl">'+L("Tu cerebro · ","Your brain · ")+lvlTxt+'</span>'+
          '<span class="rcb-prog">'+bl.pct+'% '+L("a "+ESC(ecoLevelName(nx)),"to "+ESC(ecoLevelName(nx)))+'</span></div>'+
        '<div class="rcb-desc">'+(bl.exReady
            ? L("Aliméntalo hoy (+"+BRAIN_DAILY_GAIN+"%) — cada día sube hacia <b>"+ESC(ecoLevelName(nx))+"</b> y clava mejor tu tono.","Feed it today (+"+BRAIN_DAILY_GAIN+"%) — every day it climbs toward <b>"+ESC(ecoLevelName(nx))+"</b>.")
            : L("Ya lo alimentaste hoy. Vuelve en "+brainExNextHrs()+"h para sumar otro +"+BRAIN_DAILY_GAIN+"% hacia <b>"+ESC(ecoLevelName(nx))+"</b>.","Fed today. Back in "+brainExNextHrs()+"h for another +"+BRAIN_DAILY_GAIN+"% toward <b>"+ESC(ecoLevelName(nx))+"</b>."))+'</div>'+
        '<div class="rcb-xp"><div class="rcb-xp-fill" style="width:'+Math.max(3,bl.pct)+'%"></div></div>'+
      '</div>'+
      cta+
    '</div>';
  }
  function filtersHTML(){
    var base=[["explosion",IC.spark+' Explotando'],["recent","Recientes"],["fav",IC.starO+' Favoritos']];
    // B1: añadir/actualizar viven en radarAddBarHTML (siempre presente). Aquí solo filtros.
    return '<div class="filters">'+base.map(function(f){return '<button class="fchip'+(S.filter===f[0]?" on":"")+'" data-act="filter" data-k="'+f[0]+'">'+f[1]+'</button>';}).join("")+'</div>';
  }
  // (radarAddBarHTML DISUELTA en la reorganización 04/07: añadir competidor + contador →
  //  manageCompetitorsHTML; «Añadir/Analizar reel» → botones del hero; el refresco de pago
  //  ya vivía en el hero.)

  // SPEC #3: cuando el radar se llena con el SEED del nicho (user sin competidores
  // aún), microcopy honesto + CTA a seguir competidores para tener señales propias.
  function seedBannerHTML(){
    if(!S.radarSeed || isDemo()) return '';
    return '<div class="seed-banner">'+IC.spark+
      '<span>'+L("Mientras llenas tu radar, esto está <b>petando en tu nicho</b>. Sigue a tus competidores para que el radar se llene con lo TUYO.","While you fill your radar, this is <b>blowing up in your niche</b>. Follow your competitors so the radar fills with YOUR signals.")+'</span>'+
      '<button class="btn btn-sm btn-secondary" data-act="add-comp">'+IC.plus+' '+L("Añadir competidor","Add competitor")+'</button>'+
    '</div>';
  }
  // #5: sección «petando en tu nicho que aún no sigues» — solo cuando YA sigues a
  // alguien (si no, el radar entero ya es seed). Robar uno auto-sigue al creador.
  function discoverHTML(){
    if(isDemo() || S.radarSeed) return '';
    if(!(Array.isArray(S.tracked) && S.tracked.length>0)) return '';
    var d=(Array.isArray(S.discover)?S.discover:[]);
    if(!d.length) return '';
    var cards=d.slice(0,6).map(function(r){
      var thumb=r.thumb?'<img class="disc-img" src="'+ESC(r.thumb)+'" alt="" loading="lazy"/>':'<div class="disc-ph">'+IC.bolt+'</div>';
      var exp=(r.explosionTxt!=null)?'<span class="disc-exp">'+IC.bolt+' '+ESC(String(r.explosionTxt))+'×</span>':'';
      return '<div class="disc-card">'+
        '<div class="disc-thumb" data-act="steal" data-id="'+ESC(r.id)+'" role="button" tabindex="0" aria-label="'+L("Robar este reel","Steal this reel")+'">'+thumb+exp+'<span class="disc-play">'+_icPlay+'</span></div>'+
        '<div class="disc-meta"><span class="disc-h">@'+ESC(r.creator.handle)+'</span>'+(r.views?'<span class="disc-v">'+IC.eye+' '+ESC(r.views)+'</span>':'')+'</div>'+
        '<button class="btn btn-sm btn-primary disc-steal" data-act="steal" data-id="'+ESC(r.id)+'">'+IC.bolt+' '+L("Roba","Steal")+'</button>'+
      '</div>';
    }).join("");
    return '<div class="disc-sec">'+
      '<div class="disc-head"><span class="disc-head-t">'+IC.bolt+' '+L("Petando en tu nicho","Blowing up in your niche")+' <span class="disc-tag">'+L("que aún no sigues","you don\'t follow yet")+'</span></span>'+
        '<button class="sugg-hide" data-act="disc-dismiss">'+L("Ocultar","Hide")+'</button></div>'+
      '<div class="disc-row">'+cards+'</div>'+
    '</div>';
  }
  function feedReels(){
    var a=S.reels.slice();
    if(S.filter==="fav") return a.filter(function(r){return S.favs[r.id];});
    if(S.filter==="recent") return a.sort(function(x,y){return (y.postedTs||0)-(x.postedTs||0);});   // recientes DE VERDAD (lo recién subido arriba)
    // "explotando"/default: RESPETA el orden del backend, que ya viene rankeado con
    // explosión × frescura × jitter y ROTADO a diario. Antes re-ordenábamos por explosión
    // cruda aquí → deshacía la rotación y la frescura → el feed quedaba estático.
    return a;
  }
  // «Sugerencias de hoy»: sección PROPIA (no en el feed) con creadores NUEVOS del nicho
  // que petan y el user no sigue. Carga 1 vez (real; demo no la muestra). Excluye seguidos
  // y descartados en backend; el front re-filtra seguidos por si acaba de añadir uno.
  // REACTIVIDAD (Bloque 2.6/2.7): reconcilia el estado de conexión IG al VOLVER a la pestaña.
  // La conexión sólo se hidrataba una vez al montar (loadBrandData) → conectar por otra vía
  // (onboarding, panel legacy, otra pestaña/dispositivo) dejaba la UI stale hasta recargar.
  // Refetch barato de /metrics/summary; debounced (<20s no re-pregunta); render/refresh SÓLO si cambió.
  function _reconcileConnection(){
    if(isDemo()) return;
    var now=Date.now();
    if(S._connReconAt && (now-S._connReconAt)<20000) return;
    S._connReconAt=now;
    var pid=_pidOf(S.brandId);
    var q=pid?("?project_id="+encodeURIComponent(pid)):"";
    fetch("/metrics/summary"+q,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).then(function(met){
      if(!met) return;
      var conn=!!met.connected;
      if(conn!==!!S.igConnected){
        S.igConnected=conn;
        if(conn && typeof refreshMetrics==="function"){ try{ refreshMetrics(); }catch(e){} }   // trae reels/insights → render
        else { try{ render(); }catch(e){} }
      }
    }).catch(function(){});
  }
  // Bloque 2.8 (cierra el círculo con la siembra): mientras el pool del nicho se puebla en
  // background (siembra al crear la marca), re-consulta las sugerencias hasta que aterrizan →
  // la pestaña pasa de «poblando» a llena SOLA, sin recargar. Acotado (SUGG_POLL_MAX) para no
  // sondear indefinidamente si el nicho queda genuinamente vacío.
  var SUGG_POLL_MS=18000, SUGG_POLL_MAX=12;
  function _clearSuggPoll(){ if(S._suggPollT){ clearTimeout(S._suggPollT); S._suggPollT=null; } }
  function _scheduleSuggPoll(){
    if(S._suggPoolStatus!=="populating"){ _clearSuggPoll(); S._suggPollN=0; return; }
    if((S._suggPollN||0)>=SUGG_POLL_MAX){ _clearSuggPoll(); return; }
    if(S._suggPollT) return;
    S._suggPollT=setTimeout(function(){
      S._suggPollT=null; S._suggPollN=(S._suggPollN||0)+1;
      S._suggToday=undefined; S._stLoading=false;   // fuerza re-fetch limpio del pool
      loadSuggestionsToday();
    }, SUGG_POLL_MS);
  }
  function _normSugg(raw){ var n=normReel(raw); n.worthFollow=!!raw.worth_follow; n.why=raw.why||""; n.suggestion=true; return n; }
  function loadSuggestionsToday(){
    if(isDemo() || S._stDismissed || S._stLoading || Array.isArray(S._suggToday)) return;
    S._stLoading=true;
    var _p=_pidOf(S.brandId);
    apiGet("/api/radar/suggestions"+(_p?("?project_id="+encodeURIComponent(_p)):"")).then(function(r){
      S._stLoading=false;
      // needs_niche: la marca no tiene nicho propio fijado → en vez de sacar genérico/off-niche,
      // el front pide definirlo (CTA → editor de proyecto).
      S._suggNeedsNiche=!!(r&&r.d&&r.d.needs_niche);
      S._suggHasMore=!!(r&&r.d&&r.d.has_more);                 // ¿quedan MÁS frescos («Ver más» gratis)?
      S._suggExhausted=!!(r&&r.d&&r.d.exhausted);              // contrato punto 5: visto todo lo fresco
      S._suggMoreBatch=(r&&r.d&&r.d.more_batch)||4;
      // Contrato «ninguna marca muda»: ready|populating|empty|exhausted|needs_niche. populating
      // ⇒ nicho de catálogo aún sin reels (el backend ya disparó la siembra) → estado honesto + poll.
      S._suggPoolStatus=(r&&r.d&&r.d.pool_status)||'';
      S._suggPoolRefreshable=!!(r&&r.d&&r.d.pool_refreshable);   // #2: agotado + pool con creadores stale → ofrecer «Refrescar sugerencias 5cr»
      // Son REELS (normReel-compat): los normalizo y marco suggestion=true (para robar SIN
      // seguir vía no_follow) + worthFollow (para ofrecer «+ Añadir competidor» solo en esos).
      S._suggToday=(r&&r.ok&&r.d&&Array.isArray(r.d.suggestions)) ? r.d.suggestions.map(_normSugg) : [];
      // #3b «posibles competidores»: creadores del nicho (worth_follow) que no sigues.
      S._suggCompetitors=(r&&r.d&&Array.isArray(r.d.possible_competitors)) ? r.d.possible_competitors : [];
      if(S.tab==="dashboard") bgRender();
      _scheduleSuggPoll();   // Bloque 2.8: si sigue poblando, re-consulta hasta que aterrice
    });
  }
  // #3b: sección pequeña «Posibles competidores» — creadores del nicho que petan consistente
  // y NO sigues. Foto = INICIALES (el scrape de reels no trae avatar; fotos reales = fase 2).
  // Cada uno: avatar-inicial + @handle + ×explosión + «Añadir competidor» (reusa add-suggested).
  function suggestedCompetitorsHTML(){
    if(isDemo()) return '';
    var list=Array.isArray(S._suggCompetitors)?S._suggCompetitors:[];
    // filtra los que el usuario acaba de añadir (por si el estado local ya los sigue)
    var tracked=(Array.isArray(S.tracked)?S.tracked:[]).map(function(t){
      return String((t.creator&&t.creator.ig_username)||t.handle||t.ig_username||"").toLowerCase().replace(/^@+/,""); });
    list=list.filter(function(c){ return tracked.indexOf(String(c.handle||"").toLowerCase())<0; });
    if(!list.length) return '';
    var cards=list.slice(0,12).map(function(c){   // #3: hasta 12 (backend ya lo capa), no 6
      var h=String(c.handle||"").replace(/^@+/,"");
      var exp=(c.explosion_score!=null)?('<span class="pcomp-exp">'+IC.bolt+' '+ESC(String(c.explosion_score))+'×</span>'):'';
      // Foto real si está cacheada; onerror → se quita la img y quedan las iniciales debajo.
      var img=c.avatar_url?('<img src="'+ESC(c.avatar_url)+'" alt="" loading="lazy" onerror="this.remove()">'):'';
      var rec=c.worth_follow?('<span class="pcomp-rec" title="'+L("Peta de forma consistente","Consistently blowing up")+'">★</span>'):'';
      return '<div class="pcomp-card'+(c.worth_follow?' pcomp-card--rec':'')+'">'+
        '<div class="pcomp-ava">'+ESC(initialsOf(h))+img+rec+'</div>'+
        '<div class="pcomp-meta"><span class="pcomp-h">@'+ESC(h)+'</span>'+exp+'</div>'+
        '<button class="pcomp-add" data-act="add-suggested" data-id="'+ESC(h)+'" title="'+L("Añadir a tu radar","Add to your radar")+'">'+IC.plus+' '+L("Añadir","Add")+'</button>'+
      '</div>';
    }).join("");
    return '<section class="pcomp-sec">'+
      '<div class="pcomp-head"><span class="pcomp-t">'+IC.eye+' '+L("Posibles competidores","Possible competitors")+'</span>'+
        '<span class="pcomp-sub">'+L("petan en tu nicho · aún no los sigues","blowing up in your niche · not followed yet")+'</span></div>'+
      '<div class="pcomp-row">'+cards+'</div>'+
    '</section>';
  }
  // Tarjeta de creador sugerido (vertical, para el carrusel): miniatura de su reel que peta
  // + @handle + por qué + «Añadir al radar» + descartar. Layout limpio (no se rompe).
  // Tarjeta de REEL sugerido: miniatura del reel que peta (robable AL CLIC) + @creador +
  // «Robar» (primaria, sin seguir) + «+ Añadir competidor» (secundaria, solo si worthFollow).
  function suggTodayCardHTML(r){
    if(!r||!r.id) return '';
    var h=(r.creator&&r.creator.handle)||"";
    var media=r.thumb?('<img src="'+ESC(r.thumb)+'" alt="" loading="lazy" onerror="this.style.display=\'none\'">'):'';
    // #3a (David 05/07): «Añadir competidor» visible en TODAS las cards, no solo en las
    // worthFollow. worthFollow ahora solo resalta al creador curado (clase --hot), pero el
    // botón se ofrece siempre (robar sin seguir sigue siendo la acción primaria).
    var follow=h
      ? '<button class="stday-follow'+(r.worthFollow?' stday-follow--hot':'')+'" data-act="add-suggested" data-id="'+ESC(h)+'" title="'+L("Añadir a tu radar","Add to your radar")+'">'+IC.plus+' '+L("Añadir competidor","Add competitor")+'</button>'
      : '';
    // Métricas en la tarjeta (diferencia del feed: aquí van en una FILA, no solo badge):
    // ×explosión · views · antigüedad. La explosión NO va de badge en el thumb (la lleva la fila).
    var mets='<div class="stday-mets">'+
      (r.explosionTxt!=null?'<span class="stday-met stday-met--exp">'+IC.bolt+' '+ESC(String(r.explosionTxt))+'×</span>':'')+
      '<span class="stday-met">'+IC.eye+' '+ESC(r.views)+'</span>'+
      (r.when?'<span class="stday-met stday-met--age">'+ESC(r.when)+'</span>':'')+
    '</div>';
    // #2 (David 05/07): TODA la card (salvo los botones) abre el detalle de métricas —
    // data-act en el <article>; los botones internos ganan por closest([data-act]). El CTA
    // «Ver métricas» desaparece (ya no hace falta). «Robar» sigue primario y roba DIRECTO.
    return '<article class="stday-card" data-act="reel-detail" data-id="'+ESC(r.id)+'" role="button" tabindex="0" aria-label="'+L("Ver detalle del reel","See reel detail")+'">'+
      '<div class="stday-thumb" style="background:'+_galGrad(r.id||h)+'">'+media+
        (r.dur?'<span class="stday-dur">'+ESC(r.dur)+'</span>':'')+
        '<span class="stday-play">'+_icPlay+'</span>'+
        '<span class="stday-at">@'+ESC(h)+'</span>'+
      '</div>'+
      '<div class="stday-body">'+
        mets+
        (r.why?'<div class="stday-why2"><b>'+L("Por qué robarlo","Why steal it")+':</b> '+ESC(r.why)+'</div>':'')+
        '<div class="stday-acts">'+
          '<button class="btn btn-sm btn-primary stday-rob" data-act="steal" data-id="'+ESC(r.id)+'">'+IC.bolt+' '+L("Robar","Steal")+'</button>'+
          '<button class="stday-x" data-act="sugg-dismiss-one" data-id="'+ESC(h)+'" aria-label="'+L("No me interesa","Not interested")+'" title="'+L("No me interesa","Not interested")+'">'+IC.x+'</button>'+
          follow+
        '</div>'+
      '</div>'+
    '</article>';
  }
  // Sección «Sugerencias de hoy» (carrusel de REELS con flechas ←/→). Oculta en demo/vacío.
  function suggestionsTodayHTML(){
    if(S._stDismissed) return '';
    // Demo/harness: forzar el estado AGOTADO para pinnear el guardarraíl #2 (pago solo si el pool
    // es refrescable). ?sugg=exhausted → refrescable (botón); ?sugg=exhausted-norefresh → «vuelve
    // mañana» SIN botón. Nunca vender aire.
    if(isDemo() && /[?&]sugg=exhausted/.test(location.search)){
      S._suggExhausted=true; S._suggToday=[]; S._suggPoolRefreshable=!/norefresh/.test(location.search);
    }
    // En demo la sección normal (reels/competidores del backend) se suprime, PERO el estado
    // HONESTO «poblando/añadiendo nicho» y el AGOTADO forzado sí se muestran (los verifica el harness).
    else if(isDemo() && S._suggPoolStatus!=="populating" && S._suggPoolStatus!=="empty") return '';
    // Sin nicho (ni en perfil ni en proyecto) → pedir definirlo en vez de quedarse vacío.
    // El texto/acción se adaptan: marca con proyecto → su nicho; marca default → tu nicho.
    if(S._suggNeedsNiche){
      var _hasProj=!!_pidOf(S.brandId);
      var _tx=_hasProj
        ? L("Esta marca aún no tiene nicho. Defínelo para ver reels que petan EN SU nicho (no de otras marcas).","This brand has no niche yet. Set it to see reels blowing up in ITS niche (not other brands').")
        : L("Aún no tienes nicho. Defínelo para ver cada día los reels que petan EN TU nicho.","You haven't set your niche yet. Set it to see the reels blowing up IN YOUR niche every day.");
      var _bt=_hasProj
        ? L("Definir el nicho de la marca","Set the brand niche")
        : L("Define tu nicho","Set your niche");
      return '<section class="stday-sec stday-niche-prompt">'+
        '<div class="stday-head"><span class="stday-t">'+IC.bolt+' '+L("Sugerencias de hoy","Today\'s suggestions")+'</span></div>'+
        '<div class="stday-niche-cta">'+
          '<div class="stday-niche-tx">'+_tx+'</div>'+
          '<button class="stday-niche-btn" data-act="set-brand-niche">'+IC.spark+' '+_bt+'</button>'+
        '</div>'+
      '</section>';
    }
    var tracked=(Array.isArray(S.tracked)?S.tracked:[]).map(function(t){
      return String((t.creator&&t.creator.ig_username)||t.handle||t.ig_username||"").toLowerCase().replace(/^@+/,""); });
    // DEDUP DURO por id (contrato: ningún reel dos veces en pantalla) + excluye seguidos.
    var _seen={}, list=[];
    (Array.isArray(S._suggToday)?S._suggToday:[]).forEach(function(r){
      if(!r||!r.id||_seen[r.id]) return;
      if(tracked.indexOf(String((r.creator&&r.creator.handle)||"").toLowerCase())>=0) return;
      _seen[r.id]=1; list.push(r);
    });
    if(!list.length){
      // Contrato B1 «ninguna marca nace muda»: pool del nicho aún poblándose (siembra en
      // background al crear la marca) → estado HONESTO, no vacío mudo. El poll lo llena solo.
      if(S._suggPoolStatus==="populating"){
        return '<section class="stday-sec stday-pop-sec" aria-live="polite">'+
          '<div class="stday-head"><span class="stday-t">'+IC.bolt+' '+L("Sugerencias de hoy","Today\'s suggestions")+'</span></div>'+
          '<div class="stday-pop">'+
            '<div class="stday-pop-spin" aria-hidden="true"></div>'+
            '<div class="stday-pop-tx">'+
              '<div class="stday-pop-t">'+L("Estamos poblando tu radar","We\'re populating your radar")+'</div>'+
              '<div class="stday-pop-sub">'+L("Buscando los reels que petan en tu nicho. Aparecen aquí en unos minutos — sin recargar.","Finding the reels blowing up in your niche. They\'ll show up here in a few minutes — no reload needed.")+'</div>'+
            '</div>'+
          '</div>'+
        '</section>';
      }
      // Nicho de texto libre fuera del catálogo → honesto (lo estamos añadiendo) + CTA a añadir
      // un competidor a mano para arrancar YA. Nunca una pestaña muda.
      if(S._suggPoolStatus==="empty"){
        return '<section class="stday-sec stday-pop-sec">'+
          '<div class="stday-head"><span class="stday-t">'+IC.bolt+' '+L("Sugerencias de hoy","Today\'s suggestions")+'</span></div>'+
          '<div class="stday-pop">'+
            '<div class="stday-pop-tx">'+
              '<div class="stday-pop-t">'+L("Estamos añadiendo tu nicho al radar","We\'re adding your niche to the radar")+'</div>'+
              '<div class="stday-pop-sub">'+L("Aún no seguimos cuentas de este nicho. Añade un competidor para empezar ya.","We don\'t track accounts in this niche yet. Add a competitor to start right now.")+'</div>'+
            '</div>'+
            '<button class="btn btn-sm btn-secondary" data-act="add-comp">'+IC.plus+' '+L("Añadir competidor","Add competitor")+'</button>'+
          '</div>'+
        '</section>';
      }
      // Contrato punto 5: agotado de verdad → «has visto todo lo fresco» + CTA (no vacío mudo).
      // Sin agotar (cargando / pool sin nicho) → no se pinta (regla de vacíos).
      if(S._suggExhausted){
        // #2 (David): esto refresca el POOL DE NICHO (sugerencias), NO tus competidores. Guardarraíl:
        // botón de pago SOLO si un scrape del pool puede traer algo (pool_refreshable); si no, «vuelve
        // mañana» SIN botón (no vender aire). El reembolso on-empty sigue de red de seguridad.
        var _exhCta=S._suggPoolRefreshable
          ? '<button class="stday-exh-cta" data-act="refresh-pool">'+IC.repeat+' '+L("Refrescar sugerencias · 5 cr","Refresh suggestions · 5 cr")+'</button>'+
            '<div class="stday-exh-sub">'+L("scrapeo lo último de tu nicho · o vuelve mañana (el pool se renueva solo)","pull the latest from your niche · or come back tomorrow (the pool refreshes on its own)")+'</div>'
          : '<div class="stday-exh-sub">'+L("Vuelve mañana — el pool de tu nicho se renueva solo.","Come back tomorrow — your niche pool refreshes on its own.")+'</div>';
        return '<section class="stday-sec">'+
          '<div class="stday-head"><span class="stday-t">'+IC.bolt+' '+L("Sugerencias de hoy","Today\'s suggestions")+'</span>'+_stdayMiniActs()+'</div>'+
          '<div class="stday-exhausted-full">'+
            '<div class="stday-exh-t">'+L("Has visto todo lo fresco de hoy","You\'ve seen all today\'s fresh reels")+'</div>'+
            _exhCta+
          '</div>'+
        '</section>';
      }
      return '';
    }
    var cards=list.slice(0,16).map(suggTodayCardHTML).join("");
    // Contrato punto 5: si de verdad se agotó lo fresco → dilo + CTA (nunca repetir en silencio).
    // Si quedan frescos → «Ver más» DE PAGO (David 06/07): 3 créditos, tanda completa, cobro
    // DIRECTO sin modal; sin saldo → muro de recarga. Cero scrape (el pool ya está).
    var moreCard=S._suggExhausted
      ? '<div class="stday-card stday-exhausted">'+
          '<span class="stday-exh-t">'+L("Has visto todo lo fresco de hoy","You\'ve seen all today\'s fresh reels")+'</span>'+
          (S._suggPoolRefreshable   // #2: pago solo si el pool puede traer algo; si no, «vuelve mañana» sin botón
            ? '<button class="stday-exh-cta" data-act="refresh-pool">'+IC.repeat+' '+L("Refrescar sugerencias · 5 cr","Refresh suggestions · 5 cr")+'</button>'+
              '<span class="stday-exh-sub">'+L("o vuelve mañana","or come back tomorrow")+'</span>'
            : '<span class="stday-exh-sub">'+L("vuelve mañana — el pool se renueva solo","come back tomorrow — the pool refreshes on its own")+'</span>')+
        '</div>'
      : (S._suggHasMore
        ? '<button class="stday-card stday-morecard" data-act="sugg-more" data-offset="'+list.length+'">'+
            '<span class="stday-more-ic">'+IC.bolt+'</span>'+
            '<span class="stday-more-t">'+L("Ver más","See more")+'</span>'+
            '<span class="stday-more-c">'+L("3 créditos","3 credits")+'</span>'+
          '</button>'
        : '');
    var arrows='<div class="stday-arrows">'+
      '<button class="stday-arrow" data-act="stday-scroll" data-dir="prev" aria-label="'+L("Anterior","Previous")+'">'+IC.arrL+'</button>'+
      '<button class="stday-arrow" data-act="stday-scroll" data-dir="next" aria-label="'+L("Siguiente","Next")+'">'+IC.arr+'</button>'+
    '</div>';
    // #4 (David 06/07): el detalle de una sugerencia abre EN PANEL LATERAL junto al carrusel
    // (desktop), no debajo; en MÓVIL, overlay a pantalla. Cabecera con botón «cerrar» (la card
    // sigue siendo toggle y Esc cierra). Si el reel abierto no es una sugerencia (feed), dr=null.
    var dr=S.detailReelId?list.filter(function(x){return x.id===S.detailReelId;})[0]:null;
    var isMob=(S.device==="mobile");
    var panel=dr?suggDetailHTML(dr):'';
    var side=(dr&&!isMob)?'<aside class="stday-side">'+panel+'</aside>':'';
    var overlay=(dr&&isMob)?'<div class="stday-overlay" data-act="close-reel-detail"><div class="stday-sheet" data-act="stday-noop">'+panel+'</div></div>':'';
    return '<section class="stday-sec'+(dr&&!isMob?' stday-sec--split':'')+'">'+
      '<div class="stday-head"><span class="stday-t">'+IC.bolt+' '+L("Sugerencias de hoy","Today\'s suggestions")+' <span class="stday-tag">'+L("reels que petan en tu nicho","reels blowing up in your niche")+'</span></span>'+
        arrows+_stdayMiniActs()+'</div>'+
      '<div class="stday-main">'+
        '<div class="stday-row">'+cards+moreCard+'</div>'+
        side+
      '</div>'+
      overlay+
    '</section>';
  }
  // Acciones de cabecera de «Sugerencias de hoy»: «↻ otras» (re-baraja GRATIS, sin scrape) +
  // «✎ nicho» (editar nicho de la marca SIEMPRE) + «Ocultar». Compartidas por los 3 estados.
  function _stdayMiniActs(){
    return '<button class="stday-mini" data-act="reshuffle-sugg" title="'+L("Baraja otras del mismo nicho — gratis, sin scrape","Shuffle others from the same niche — free, no scrape")+'">'+IC.repeat+' '+L("otras","others")+'</button>'+
      '<button class="stday-mini" data-act="set-brand-niche" title="'+L("Editar el nicho de esta marca","Edit this brand\'s niche")+'">'+IC.gear+' '+L("nicho","niche")+'</button>'+
      '<button class="sugg-hide" data-act="st-dismiss-all">'+L("Ocultar","Hide")+'</button>';
  }

  // ── GALERÍAS de miniaturas + modal Comunidad (port de Leonard, restylado v3) ──
  // Iconos de trazo 2px en vez de emojis (regla 8).
  var _icPlay='<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  var _icGift='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13"/><path d="M19 12v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-7"/><path d="M7.5 8a2.5 2.5 0 0 1 0-5C11 3 12 8 12 8s1-5 4.5-5a2.5 2.5 0 0 1 0 5"/></svg>';
  var _icUsers='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>';
  var _icTrophy='<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/></svg>';
  var _icWarn='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
  function _demoThumbs(){
    return (isDemo() && window.__DEMO_THUMBS__ && window.__DEMO_THUMBS__.length) ? window.__DEMO_THUMBS__ : null;
  }
  function _galGrad(seed){
    var h=[['#ff6a3d','#ff2d55'],['#5b8cff','#7b3dff'],['#1dd3b0','#0e9f87'],['#ffb648','#ff7a00'],['#6f93ff','#4f7cff'],['#b06cff','#7b3dff'],['#23c4ff','#3d6bff'],['#ff5db1','#ff2d55']];
    var g=h[_lbHash(String(seed),0,h.length)];
    return 'linear-gradient(150deg,'+g[0]+','+g[1]+')';
  }
  function galleryCardHTML(c){
    var inner=c.thumb
      ? '<img src="'+ESC(c.thumb)+'" alt="" loading="lazy">'
      : '<div class="gcard-ph" style="background:'+_galGrad(c.key||c.handle)+'">'+_icPlay+'</div>';
    return '<div class="gcard"'+(c.act?(' data-act="'+c.act+'" data-id="'+ESC(String(c.id||""))+'"'+(c.handle?' data-handle="'+ESC(c.handle)+'"':'')):'')+' role="button" tabindex="0" aria-label="@'+ESC(c.handle)+'">'+
      inner+
      (c.badge?'<span class="gcard-badge">'+ESC(c.badge)+'</span>':'')+
      (c.soon?'<span class="gcard-soon">'+L('Próximamente','Soon')+'</span>':'')+
      '<div class="gcard-ov"><div class="gcard-h">@'+ESC(c.handle)+'</div>'+(c.cap?'<div class="gcard-cap">'+ESC(c.cap)+'</div>':'')+'</div>'+
    '</div>';
  }
  function galleryHTML(title, sub, cards, moreHTML){
    if(!cards) return '';
    return '<section class="rgal"><div class="rgal-head"><div class="rgal-t">'+ESC(title)+
      (sub?'<span class="rgal-sub">'+ESC(sub)+'</span>':'')+'</div>'+(moreHTML||'')+'</div>'+
      '<div class="rgal-track">'+cards+'</div></section>';
  }
  /* Tarjeta de reel de competidor (mockup David «RADAR exploración»): thumb 9:16
     arriba (badge mult verde + chispazo naranja + dur mono + play), body con
     handle·time mono + título Clash + «Ver métricas»/robar. */
  function competitorReelCardHTML(r, thumb){
    var t=thumb||r.thumb;
    var thumbInner=t?'<img src="'+ESC(t)+'" alt="" loading="lazy">':'<div class="crd-ph" style="background:'+_galGrad(r.id||r.creator.handle)+'">'+_icPlay+'</div>';
    var mult=(r.explosionTxt!=null)?(r.explosionTxt+'×'):'';
    var open=(S._galOpen===r.id);
    return '<div class="crd'+(open?' crd--open':'')+'">'+
      '<div class="crd-main">'+
        '<div class="crd-thumb" data-act="reel-original" data-id="'+ESC(r.id)+'" role="button" tabindex="0" title="'+L("Abrir el reel original","Open the original reel")+'" aria-label="'+L("Abrir el reel original de @"+r.creator.handle,"Open @"+r.creator.handle+"'s original reel")+'">'+thumbInner+
          (mult?'<span class="crd-mult">'+IC.bolt+' '+ESC(mult)+'</span>':'')+
          (r.dur?'<span class="crd-dur">'+ESC(r.dur)+'</span>':'')+
          '<span class="crd-play">'+_icPlay+'</span>'+
        '</div>'+
        '<div class="crd-body">'+
          '<span class="crd-meta">@'+ESC(r.creator.handle)+' · '+ESC(r.when)+'</span>'+
          '<h3 class="crd-title">'+ESC(r.cap)+'</h3>'+
          '<div class="crd-acts">'+
            '<button class="crd-btn crd-metrics'+(open?' on':'')+'" data-act="gal-expand" data-id="'+ESC(r.id)+'">'+IC.eye+' '+(open?L("Ocultar","Hide"):L("Ver métricas","See metrics"))+'</button>'+
            '<button class="crd-btn crd-steal" data-act="steal" data-id="'+ESC(r.id)+'" aria-label="'+L("Roba la idea","Steal the idea")+'">'+IC.bolt+'</button>'+
          '</div>'+
        '</div>'+
      '</div>'+
      (open?competitorPanelHTML(r):'')+
    '</div>';
  }
  /* Panel expandible (mockup David «EXPANDIBLE · Ver métricas · transcripción»):
     stats 2×2 (Explosión verde, Views, Likes, Comentarios) + transcripción + robar. */
  function competitorPanelHTML(r){
    var tx=(S._tx&&S._tx.id===r.id)?S._tx:{status:"idle"};
    var txBody;
    if(tx.status==="ok") txBody='<div class="crd-tx">'+ESC(tx.text)+'</div>';
    else if(tx.status==="loading") txBody='<div class="crd-tx crd-tx--wait"><span class="rs-ldr"></span> '+L("Transcribiendo el audio…","Transcribing audio…")+'</div>';
    else if(tx.status==="error") txBody='<div class="crd-tx crd-tx--wait">'+L("No se pudo transcribir.","Couldn't transcribe.")+' <button class="btn btn-sm btn-secondary" data-act="reel-tx" data-id="'+ESC(r.id)+'">'+L("Reintentar","Retry")+'</button></div>';
    else txBody='<button class="btn btn-sm btn-secondary" data-act="reel-tx" data-id="'+ESC(r.id)+'">'+IC.doc+' '+L("Ver transcripción","See transcript")+'</button>';
    var stat=function(k,v,green){ return '<div class="crd-stat"><div class="crd-stat-k">'+ESC(k)+'</div><div class="crd-stat-v'+(green?' g':'')+'">'+ESC(String(v))+'</div></div>'; };
    return '<div class="crd-panel">'+
      '<div class="crd-pgrid">'+
        stat(L("Explosión","Explosion"), (r.explosionTxt!=null?r.explosionTxt+'×':'–'), true)+
        stat(L("Views","Views"), r.views)+
        stat(L("Likes","Likes"), r.likes)+
        stat(L("Comentarios","Comments"), (r.comments!=null?r.comments:'–'))+
        // ítem 6: «Compartidos» SOLO si hay dato real (>0). Instagram/Apify no expone el
        // recuento de shares públicamente → casi siempre 0; mostrar "0" daría señal vacía.
        // Los shares reales esperan a Meta Graph API (mismo bucket que audiencia/retención).
        ((r.sharesN||0)>0 ? stat(L("Compartidos","Shares"), r.shares) : '')+
      '</div>'+
      '<div class="crd-txwrap"><span class="crd-tx-lbl">'+L("TRANSCRIPCIÓN · DETECTADA","TRANSCRIPT · DETECTED")+'</span>'+txBody+'</div>'+
      '<button class="btn btn-md btn-primary crd-steal-full" data-act="steal" data-id="'+ESC(r.id)+'">'+IC.bolt+' '+L("Roba la idea","Steal the idea")+'</button>'+
    '</div>';
  }
  // v3 (mockup David · «Competidores en el radar» + galería): fila de chips por
  // competidor (avatar+@handle+count, filtran la galería) + «Añadir», línea
  // «Galería · X — pulsa Ver métricas…» + flechas, track de cards y empty state.
  function competitorGalleryHTML(){
    if(!S.reels.length) return '';
    var imgs=_demoThumbs();
    // competidores derivados de los reels (estables, no dependen del filtro)
    var byComp={}; S.reels.forEach(function(r){ var h=(r.creator&&r.creator.handle)||""; if(!h) return; if(!byComp[h]) byComp[h]={handle:h, n:0}; byComp[h].n++; });
    var comps=Object.keys(byComp).map(function(h){ return byComp[h]; });
    var active=S.galComp||null;
    // chips COMPACTOS y uniformes (tamaño «Todos», sin avatar).
    var chip=function(h,label,n,on){
      return '<button class="rgal-chip'+(on?' on':'')+'" data-act="gal-comp" data-k="'+ESC(h)+'">'+ESC(label)+
        (n!=null?'<span class="rgal-chip-n">'+n+'</span>':'')+'</button>';
    };
    // Solo 3-4 visibles; el resto en un desplegable con buscador. Si el activo está
    // oculto, lo subo a la cabeza para que se vea seleccionado.
    var MAXVIS=3;
    var ordered=comps.slice();
    if(active){ var ai=ordered.map(function(c){return c.handle;}).indexOf(active); if(ai>=MAXVIS){ var a=ordered.splice(ai,1)[0]; ordered.unshift(a); } }
    var vis=ordered.slice(0,MAXVIS), hidden=ordered.slice(MAXVIS);
    // Sin número: el «N» era el conteo de REELS (Todos=20=10+10), pero bajo el título
    // «Competidores» parecía «20 competidores» y confundía. El conteo real de
    // competidores + reels por competidor vive en «Tus competidores» (trackedManageHTML).
    var chips=chip("",L("Todos","All"),null,!active)+
      vis.map(function(c){ return chip(c.handle,"@"+c.handle,null,active===c.handle); }).join("");
    // desplegable con buscador para el resto (gente seguida que no cabe en la fila).
    // VA FUERA de la tira con scroll (.rgal-comps tiene overflow → recortaría el menú).
    var moreWrap="";
    if(hidden.length){
      var rows=comps.map(function(c){
        return '<button class="gal-menu-row'+(active===c.handle?' on':'')+'" data-act="gal-comp" data-k="'+ESC(c.handle)+'" data-handle="'+ESC(c.handle.toLowerCase())+'">'+
          '<span class="rgal-chip-av">'+ESC(initialsOf(c.handle))+'</span><span class="gal-menu-h">@'+ESC(c.handle)+'</span><span class="rgal-chip-n">'+c.n+'</span></button>';
      }).join("");
      var dd=S.galMenu?('<div class="gal-menu">'+
        '<input class="gal-menu-search" type="text" autocomplete="off" spellcheck="false" placeholder="'+L("Buscar competidor…","Search competitor…")+'" oninput="try{window.RadarLoop.galSearch(this.value)}catch(e){}">'+
        '<div class="gal-menu-list">'+rows+'</div></div>'):'';
      moreWrap='<span class="rgal-more-wrap"><button class="rgal-chip rgal-morebtn'+(S.galMenu?' on':'')+'" data-act="gal-menu">+'+hidden.length+' '+IC.chev+'</button>'+dd+'</span>';
    }
    // reels mostrados: feed (respeta filtro explosión/recientes/fav) + filtro de competidor
    var reels=feedReels(); if(active) reels=reels.filter(function(r){ return (r.creator&&r.creator.handle)===active; });
    var activeLabel=active?("@"+active):L("todos los competidores","all competitors");
    var arrows='<div class="rgal-arrows">'+
      '<button class="rgal-arrow" data-act="rgal-scroll" data-dir="prev" aria-label="'+L("Anterior","Previous")+'">'+IC.arrL+'</button>'+
      '<button class="rgal-arrow" data-act="rgal-scroll" data-dir="next" aria-label="'+L("Siguiente","Next")+'">'+IC.arr+'</button>'+
    '</div>';
    // El feed principal = SOLO reels de competidores que sigues. El descubrimiento y las
    // sugerencias viven en su sección propia «Sugerencias de hoy» (suggestionsTodayHTML).
    var body = reels.length
      ? '<div class="rgal-track crd-track">'+reels.slice(0,12).map(function(r,i){ return competitorReelCardHTML(r, imgs?imgs[i%imgs.length]:null); }).join("")+'</div>'
      : '<div class="rgal-empty"><span class="rgal-empty-h">'+L("Nada que robar aquí… todavía","Nothing to steal here… yet")+'</span><span class="rgal-empty-s">'+L("Este competidor no tiene reels explosivos esta semana.","This competitor has no explosive reels this week.")+'</span></div>';
    return '<section class="rgal">'+
      // Reorganización 04/07: sin botón «Añadir» aquí — los chips son FILTROS de la galería;
      // la gestión (añadir/quitar) vive en su único bloque «Gestiona competidores».
      '<div class="rgal-comps-head"><span class="rgal-comps-t">'+L("Competidores en el radar","Competitors on the radar")+'</span>'+
        '<div class="rgal-comps">'+chips+'</div>'+moreWrap+'</div>'+
      '<div class="rgal-galline-row"><div class="rgal-galline">'+L("Galería · ","Gallery · ")+'<b>'+ESC(activeLabel)+'</b> — '+L("pulsa <b>Ver métricas</b> para abrir transcripción y datos.","tap <b>See metrics</b> to open transcript and data.")+'</div>'+arrows+'</div>'+
      body+
    '</section>';
  }
  function communityGalleryHTML(){
    return '<section class="rgal"><div class="rgal-head"><div class="rgal-t">'+
      L("Creaciones de la comunidad","Community creations")+'</div></div>'+
      '<div class="rgal-soon">'+
        '<span class="rgal-soon-pill">'+IC.spark+' '+L("Próximamente","Coming soon")+'</span>'+
        '<button class="rgal-more rgal-more-info" data-act="community-info">'+L("Más información","Learn more")+'</button>'+
        '<button class="rgal-more" data-act="community-interest">'+L("Avísame cuando esté","Notify me")+' →</button>'+
      '</div></section>';
  }
  function communityInfoModalHTML(){
    var thumbs=_demoThumbs()||[];
    var mini=function(i){
      var t=thumbs.length?thumbs[i%thumbs.length]:null;
      return '<div class="ci-mini"'+(t?'':(' style="background:'+_galGrad("ci"+i)+'"'))+'>'+(t?'<img src="'+ESC(t)+'" alt="">':_icPlay)+'</div>';
    };
    var strip=''; for(var i=0;i<7;i++) strip+=mini(i);
    var interested=!!S.user.communityWaitlist; if(!interested){ try{ interested=localStorage.getItem("rs_community_interest")==="1"; }catch(e){} }
    return '<div class="ci-overlay" data-act="ci-close">'+
      '<div class="ci-card" role="dialog" aria-modal="true" aria-label="'+L("Comunidad de creadores","Creator community")+'" data-act="ci-stop">'+
        '<button class="ci-x" data-act="ci-close" aria-label="'+L("Cerrar","Close")+'">'+IC.x+'</button>'+
        '<div class="ci-stage"><div class="ci-strip">'+strip+strip+'</div><div class="ci-gift">'+_icGift+'</div></div>'+
        '<span class="ci-pill">'+IC.spark+' '+L("Próximamente","Coming soon")+'</span>'+
        '<h3 class="ci-h">'+L("Comunidad de creadores","Creator community")+'</h3>'+
        '<p class="ci-sub">'+L("Reelscript dejará de ser solo tú y tu radar: una red entre creadores de tu nicho para crecer juntos.","Reelscript won't just be you and your radar — a network of creators in your niche, growing together.")+'</p>'+
        '<ul class="ci-feats">'+
          '<li><span class="ci-fi">'+_icGift+'</span><div><b>'+L("Pushea tus reels → recibe regalos","Push your reels → get gifts")+'</b>'+L("Comparte tu mejor reel con tu nicho y recibe los suyos ya analizados, listos para robar.","Share your best reel with your niche and get theirs back, already analyzed and ready to steal.")+'</div></li>'+
          '<li><span class="ci-fi">'+_icUsers+'</span><div><b>'+L("Grupos de creadores","Creator groups")+'</b>'+L("Únete a un grupo de tu nicho: comparte métricas y ve en directo qué funciona.","Join a niche group: share metrics and see live what works.")+'</div></li>'+
          '<li><span class="ci-fi">'+IC.spark+'</span><div><b>'+L("Creaciones de la comunidad","Community creations")+'</b>'+L("Un muro con lo que crean otros con Reelscript — inspiración real de tu nicho.","A wall of what others build with Reelscript — real inspiration from your niche.")+'</div></li>'+
        '</ul>'+
        (interested
          ? '<div class="ci-done">'+IC.check+' '+L("Te avisaremos en cuanto llegue","We'll let you know when it lands")+'</div>'
          : '<button class="btn btn-md btn-primary ci-go" data-act="community-interest">'+IC.spark+' '+L("Avísame cuando llegue","Notify me when it's live")+'</button>')+
      '</div>'+
    '</div>';
  }
  function reelCardHTML(r){
    var hi=(r.explosion||0)>=3, isFav=!!S.favs[r.id];
    var thumbInner=r.thumb?'<img src="'+ESC(r.thumb)+'" alt="">':'<div class="play"></div>';
    // A: la card entera abre el detalle (data-act en el contenedor; los botones
    // internos ganan porque closest() resuelve el data-act más interno). Enter
    // también lo abre (onKeydown) — la card es un objeto con el que se trabaja.
    return '<div class="row row-clickable" data-act="reel-detail" data-id="'+ESC(r.id)+'" role="button" tabindex="0" aria-label="Abrir detalle del reel de @'+ESC(r.creator.handle)+'">'+
      '<div class="row-score'+(hi?" hi":"")+'"><span class="sv">'+(r.explosionTxt!=null?ESC(r.explosionTxt):"–")+'×</span><span class="sx">'+(hi?"explota":"media")+'</span></div>'+   // T8 (IDI): el "explosivo" se lee por etiqueta (verde), no solo por color
      '<div class="row-thumb"><div class="thumb">'+thumbInner+'<span class="dur">'+ESC(r.dur)+'</span></div></div>'+
      '<div class="row-mid"><div class="row-crow"><span class="ava">'+ESC(r.creator.initials)+'</span><span class="row-who">@'+ESC(r.creator.handle)+'</span><span class="row-when">'+ESC(r.when)+'</span></div><p class="row-cap">'+ESC(r.cap)+'</p></div>'+
      '<div class="row-metrics"><span>'+IC.eye+' '+ESC(r.views)+'</span><span>'+IC.heart+' '+ESC(r.likes)+'</span></div>'+
      '<div class="row-actions"><button class="iconbtn'+(isFav?" on":"")+'" data-act="fav" data-id="'+ESC(r.id)+'" title="Guardar">'+(isFav?IC.star:IC.starO)+'</button>'+
        '<button class="btn btn-sm btn-primary" data-act="steal" data-id="'+ESC(r.id)+'">'+IC.bolt+' Roba la idea</button></div>'+
    '</div>';
  }

  /* Carrusel de 2-3 oportunidades del día (Fathom 18/06: David quiere un slide con
     varias ideas, no una sola — refuerza "hay de dónde elegir"). Con 1 sola señal
     degrada a la card suelta de siempre. */
  function opportunityCarouselHTML(reels){
    if(!reels || !reels.length) return '';
    var _th=_demoThumbs();
    if(reels.length===1) return opportunityHTML(reels[0],1,_th?_th[0]:null);
    var slides=reels.map(function(r,i){
      return '<div class="opp-slide" role="group" aria-label="Oportunidad '+(i+1)+' de '+reels.length+'">'+opportunityHTML(r,i+1,_th?_th[i%_th.length]:null)+'</div>';
    }).join("");
    var dots=reels.map(function(r,i){
      return '<button class="opp-dot'+(i===0?" on":"")+'" data-act="opp-nav" data-k="'+i+'" aria-label="Ir a la oportunidad '+(i+1)+'"></button>';
    }).join("");
    return '<section class="opp-carousel" aria-roledescription="carrusel">'+
      '<button class="opp-arrow prev" data-act="opp-nav" data-k="prev" aria-label="Anterior">'+IC.arrL+'</button>'+
      '<div class="opp-track" id="rsOppTrack">'+slides+'</div>'+
      '<button class="opp-arrow next" data-act="opp-nav" data-k="next" aria-label="Siguiente">'+IC.arr+'</button>'+
      '<div class="opp-nav-bar"><div class="opp-dots">'+dots+'</div><div class="opp-count" id="rsOppCount">1 / '+reels.length+' oportunidades</div></div>'+
    '</section>';
  }

  /* card destacada — "tu oportunidad de hoy" (principio I: una respuesta).
     v3 (mockup David «featured opportunity»): thumb limpio (foto/degradado + play
     + «reel · handle» + dur), cuerpo (eyebrow azul · titular Clash · insight con
     chispazo naranja · «Roba la idea» + estrella) y columna de stats 150px
     (Explosión verde + barra · Views · Likes). Sin píldora «más explosivo». */
  function opportunityHTML(r,idx,thumb){
    idx=idx||1;
    var mega=(r.explosion||0)>=5;
    var t=thumb||r.thumb;
    var thumbInner=t?'<img src="'+ESC(t)+'" alt="" loading="lazy">':'<div class="feat-ph" style="background:'+_galGrad(r.id||r.creator.handle)+'"></div>';
    var why = mega
      ? "Está reventando: "+ (r.explosionTxt!=null?r.explosionTxt:"")+"× lo normal de @"+r.creator.handle+". Si hay uno que robar hoy, es este."
      : "Por encima de la media de @"+r.creator.handle+". Buen punto de partida para hoy.";
    var expPct=Math.min(100,(r.explosion||0)/6*100);
    // #2 conversión: escasez visible → robar deja de ser obvio (trade-off de saldo).
    var _fl=freeStealsLeft();
    var scarce = (_fl===null) ? '' :
      (_fl<=0
        ? '<button class="feat-scarce out feat-scarce-btn" data-act="open-plans">'+IC.bolt+' '+L("Hechos tus 3 guiones de hoy — vuelve mañana o desbloquéalos","Today's 3 scripts done — come back tomorrow or unlock them")+' '+IC.arr+'</button>'
        : '<div class="feat-scarce">'+IC.bolt+' '+L("Te queda"+(_fl===1?"":"n")+" <b>"+_fl+"</b> guion"+(_fl===1?"":"es")+" hoy","<b>"+_fl+"</b> script"+(_fl===1?"":"s")+" left today")+'</div>');
    return '<article class="feature">'+
      '<div class="feature-thumb"><div class="thumb" data-act="reel-original" data-id="'+ESC(r.id)+'" role="button" tabindex="0" title="'+L("Abrir el reel original","Open the original reel")+'" aria-label="'+L("Abrir el reel original de @"+r.creator.handle,"Open @"+r.creator.handle+"'s original reel")+'">'+thumbInner+
        '<span class="feat-play">'+_icPlay+'</span>'+
        '<span class="thumb-tag">reel · @'+ESC(r.creator.handle)+'</span><span class="dur">'+ESC(r.dur)+'</span></div></div>'+
      '<div class="feature-main">'+
        '<div class="feature-eyebrow">Oportunidad #'+idx+' <span class="who">· @'+ESC(r.creator.handle)+' · '+ESC(r.when)+'</span></div>'+
        '<h2 class="feature-cap">'+ESC(r.cap)+'</h2>'+
        '<div class="feature-why">'+IC.spark+'<span>'+ESC(why)+'</span></div>'+
        '<div class="feature-actions"><button class="btn btn-lg btn-primary" data-act="steal" data-id="'+ESC(r.id)+'">'+IC.bolt+' Roba la idea</button>'+
          '<button class="iconbtn'+(S.favs[r.id]?" on":"")+'" data-act="fav" data-id="'+ESC(r.id)+'" title="Guardar">'+(S.favs[r.id]?IC.star:IC.starO)+'</button></div>'+
        scarce+
      '</div>'+
      '<div class="feature-data">'+
        '<div class="dmetric big"><div class="dk">Explosión</div><div class="dv">'+(r.explosionTxt!=null?ESC(r.explosionTxt):"–")+'×</div><div class="dbar"><i style="width:'+expPct+'%"></i></div></div>'+
        '<div class="dmetric"><div class="dk">Views</div><div class="dv">'+ESC(r.views)+'</div></div>'+
        '<div class="dmetric"><div class="dk">Likes</div><div class="dv">'+ESC(r.likes)+'</div></div>'+
      '</div>'+
    '</article>';
  }

  // Navegación del carrusel de oportunidades (sin re-render: scroll directo del track).
  function oppNav(k){
    var track=document.getElementById("rsOppTrack"); if(!track) return;
    var slides=track.querySelectorAll(".opp-slide"); if(!slides.length) return;
    var w=slides[0].getBoundingClientRect().width||track.clientWidth;
    var cur=Math.round(track.scrollLeft/w);
    var i=(k==="prev")?cur-1:(k==="next")?cur+1:(parseInt(k,10)||0);
    i=Math.max(0,Math.min(slides.length-1,i));
    track.scrollTo({left:i*w,behavior:"smooth"});
    var dots=track.parentNode.querySelectorAll(".opp-dot");
    for(var d=0;d<dots.length;d++) dots[d].classList.toggle("on",d===i);
    var cnt=document.getElementById("rsOppCount"); if(cnt) cnt.textContent=(i+1)+" / "+slides.length+" oportunidades";
  }

  /* Forzar el re-scrapeo del PROPIO perfil (Fathom 18/06): adelanta el análisis que
     corre solo 2×/semana, a cambio de créditos. En prod encolaría el scrape del IG
     del usuario; en demo solo cobra y confirma. Sin créditos → al muro. */
  function forceScrape(){
    var COST=10;
    if(isDemo()){
      if(isFree() || (S.user.credits||0)<COST){ return showPaywall("force_scrape"); }
      spend(COST); render(); flashSpark(-COST);
      showToast("Análisis de tu perfil encolado — si publicaste algo nuevo, tu nivel sube en cuanto termine.");
      return;
    }
    // Real: el backend cobra los créditos y encola el scrape del propio perfil.
    apiPost('/api/brain/rescrape',{}).then(function(r){
      if(!r.ok){
        if(r.status===402 || (r.d&&r.d.error)==="no_credits") return showPaywall("no_credits");
        return showToast((r.d&&r.d.message)||"No pude encolar el análisis. Inténtalo en un momento.");
      }
      if(r.d && r.d.credits!=null) S.user.credits=r.d.credits;
      render(); flashSpark(-COST);
      showToast((r.d&&r.d.message)||"Análisis de tu perfil encolado — tu nivel sube en cuanto termine.");
    });
  }

  /* ════════════════════════════════════════════════════════════════
     PORTFOLIO (solo Agencia) — resumen de TODAS las marcas → zoom.
     ════════════════════════════════════════════════════════════════ */
  function brandRowHTML(b){
    var att=brandNeedsAttention(b);
    var sig = (b.exploded||0)>0
      ? '<span class="hot">'+IC.spark+' '+b.exploded+' explosivo'+(b.exploded>1?"s":"")+' hoy</span>'
      : '<span class="muted">— sin novedad</span>';
    return '<button class="brow'+(att?" attn":"")+'" data-act="open-brand" data-id="'+ESC(b.id)+'">'+
      '<span class="brow-dot" style="background:'+ESC(b.color||"#4f7cff")+'"></span>'+
      '<div class="brow-id"><div class="brow-name">'+ESC(b.name)+'</div><div class="brow-handle">@'+ESC(b.handle||"")+' · Nivel '+(b.level||1)+'</div></div>'+
      '<div class="brow-sig">'+sig+'</div>'+
      '<div class="brow-voice"><span class="bv-k">VOZ</span><span class="bv-v">'+(b.voice||40)+'%</span></div>'+
      (att?'<span class="brow-attn">'+_icWarn+' Atención</span>':'<span class="brow-attn ok"></span>')+
      '<span class="brow-open">Abrir '+IC.arr+'</span>'+
    '</button>';
  }
  function portfolioHTML(){
    var bs=S.brands||[];
    var totReels=bs.reduce(function(s,b){return s+(b.reels||0);},0);
    var totExp=bs.reduce(function(s,b){return s+(b.exploded||0);},0);
    var attn=bs.filter(brandNeedsAttention).length;
    var stats=[
      ["Marcas", bs.length, "", ""],
      ["Reels · 7 días", totReels, "", ""],
      ["Explosivos", totExp, "en todo el portfolio", "acc"],
      ["Piden atención", attn, attn>0?"revísalas hoy":"todo al día", attn>0?"warn":""]
    ];
    var statbar='<div class="statbar">'+stats.map(function(s){
      return '<div class="stat"><div class="stat-k">'+ESC(s[0])+'</div><div class="stat-v">'+ESC(s[1])+'</div>'+(s[2]?'<div class="stat-d '+s[3]+'">'+ESC(s[2])+'</div>':'')+'</div>';
    }).join("")+'</div>';
    var head='<header class="phead">'+
      '<div><div class="eyebrow"><span class="pip"></span>Portfolio · '+bs.length+' marcas</div>'+
      '<h1 class="h-title">Tus marcas</h1>'+
      '<p class="h-sub">Lo que pasó hoy en cada una. Entra donde haya algo que capitalizar.</p></div>'+
      (S.user.streak>0?'<div class="phead-right"><span class="streak">'+IC.spark+' Racha '+S.user.streak+' días</span></div>':'')+
    '</header>';
    return '<div class="scroll"><div class="canvas">'+
      head+statbar+
      '<div class="feed-head"><span class="feed-title">Marcas <span class="ct">· '+bs.length+'</span></span></div>'+
      '<div class="brow-list">'+bs.map(brandRowHTML).join("")+'</div>'+
    '</div></div>';
  }

  // Tira de pestañas de marca — SOLO en el Radar de Agencia. Salto rápido entre
  // los dashboards de cada marca (B3: sin "Todas" — portfolio eliminado).
  function brandTabsHTML(){
    return '<div class="brand-tabs">'+
      S.brands.map(function(b){
        var on=b.id===S.brandId;
        return '<button class="btab'+(on?" on":"")+'" data-act="open-brand" data-id="'+ESC(b.id)+'">'+
          '<span class="btab-dot" style="background:'+ESC(b.color||"#4f7cff")+'"></span>'+ESC(b.name)+
          ((b.exploded||0)>0?'<span class="btab-n">'+b.exploded+'</span>':'')+
        '</button>';
      }).join("")+
    '</div>';
  }
  function statbarHTML(){
    var st=S.stats||{competitors:0,reels_week:0,exploded_week:0,stolen_today:0};
    var stats=[
      ["Rivales activos", st.competitors, "", ""],
      ["Reels · 7 días", st.reels_week, (st.exploded_week>0?st.exploded_week+" explotaron":""), "up"],
      ["Explosivos", st.exploded_week, "sobre su media", "acc"]
    ];
    // B2: el nivel del Cerebro, visible en el Radar y clicable (lleva al Cerebro,
    // donde está la checklist completa). Derivado de señales reales (brainLevel).
    var lv=brainLevel();
    var lvNum=lv.level>=1?String(lv.level):"—";
    var lvDetail=lv.full ? 'al máximo' : (lv.canLevelUp ? '¡listo, súbelo!' : (lv.pct+'% a N'+lv.next));
    var brainCell='<div class="stat stat-link" data-act="tab" data-k="brain" role="button" tabindex="0" '+
      'title="Abrir el Cerebro" aria-label="Cerebro: nivel '+lvNum+', '+ESC(lvDetail)+'">'+
      '<div class="stat-k">Cerebro</div><div class="stat-v">'+(lv.level>=1?('Nivel '+lv.level):L("Nuevo","New"))+'</div>'+
      '<div class="stat-d acc">'+ESC(lvDetail)+'</div></div>';
    return '<div class="statbar">'+stats.map(function(s){
      return '<div class="stat"><div class="stat-k">'+ESC(s[0])+'</div><div class="stat-v">'+ESC(s[1])+'</div>'+(s[2]?'<div class="stat-d '+s[3]+'">'+ESC(s[2])+'</div>':'')+'</div>';
    }).join("")+brainCell+'</div>';
  }
  /* Endowed progress (#2 plan): checklist de activación con el 1er paso ya hecho
     («Cuenta creada») → sensación de avance + empuje a robar la primera idea. Se
     oculta al completar los 4. Deriva de señales reales (brainSignals). */
  function activationSteps(){
    var s=brainSignals();
    return [
      {ok:true,         label:L("Cuenta creada","Account created")},
      {ok:s.guiones>=1, label:L("Roba tu primera idea","Steal your first idea")},
      {ok:s.voice>0,    label:L("Entrena tu voz","Train your voice")},
      {ok:s.pub>=1,     label:L("Conecta tus métricas","Connect your metrics")}
    ];
  }
  function activationProgressHTML(){
    var steps=activationSteps();
    var done=steps.filter(function(x){return x.ok;}).length;
    if(done>=steps.length) return '';   // activación completa → nada que empujar
    var next=steps.filter(function(x){return !x.ok;})[0];
    var chips=steps.map(function(x){
      return '<span class="ap-chip'+(x.ok?' ok':'')+'">'+(x.ok?IC.check:'<span class="ap-o">○</span>')+' '+ESC(x.label)+'</span>';
    }).join("");
    return '<div class="act-prog">'+
      '<div class="act-prog-head"><span class="act-prog-t">'+L("Activa tu cuenta","Activate your account")+(next?' · '+ESC(next.label):'')+'</span>'+
        '<span class="act-prog-n">'+done+'/'+steps.length+'</span></div>'+
      '<div class="act-prog-bar"><div class="act-prog-fill" style="width:'+Math.round(done/steps.length*100)+'%"></div></div>'+
      '<div class="act-prog-steps">'+chips+'</div>'+
    '</div>';
  }
  // Parse de views formateadas ("1,4 M" / "680 K" / "35432") → número, para sumar alcance.
  function _parseViews(s){
    if(typeof s==="number") return s;
    var t=String(s||"").replace(/[, ]/g,"").toLowerCase();
    var m=parseFloat(t)||0;
    if(t.indexOf("m")>=0) return m*1e6; if(t.indexOf("k")>=0) return m*1e3; return m;
  }
  /* RADAR HERO v3 (mockup David «RADAR exploración»): eyebrow mono + título Clash
     46px + sub + stats en línea + SCOPE animado (barrido cónico + blips). Sustituye
     al phead + statbar viejos. Tokens del island (azul=brand-500, verde=success-fg). */
  function radarScopeHTML(){
    return '<div class="rdr-scope"><div class="rdr-face">'+
      '<span class="rdr-ring r1"></span><span class="rdr-ring r2"></span><span class="rdr-ring r3"></span>'+
      '<span class="rdr-axis x"></span><span class="rdr-axis y"></span>'+
      '<span class="rdr-sweep"></span>'+
      '<span class="rdr-blip g" style="top:32%;left:62%"></span><span class="rdr-ping" style="top:32%;left:62%"></span>'+
      '<span class="rdr-blip o" style="top:60%;left:38%"></span>'+
      '<span class="rdr-blip b" style="top:44%;left:28%"></span>'+
      '<span class="rdr-blip n" style="top:70%;left:64%"></span>'+
      '<span class="rdr-core"></span>'+
    '</div></div>';
  }
  function radarHeroHTML(){
    var st=S.stats||{}; var n=st.competitors||0;
    var line = st.exploded_week>0
      ? L("Mientras no mirabas, <b>"+st.exploded_week+" reel"+(st.exploded_week>1?"es":"")+" explotaron</b> en tu nicho. Esto es lo que merece tu próximo guion.","While you looked away, <b>"+st.exploded_week+" reel"+(st.exploded_week>1?"s":"")+" blew up</b> in your niche. This is what deserves your next script.")
      : L("Tus <b>"+n+" rivales</b> publicaron "+(st.reels_week||0)+" reels esta semana. Esto es lo que merece tu próximo guion.","Your <b>"+n+" rivals</b> posted "+(st.reels_week||0)+" reels this week. This is what deserves your next script.");
    // Stats del mockup David: «explotaron hoy» · «la mayor explosión» · «alcance
    // robable · en directo» (los dos primeros en verde; el tercero primario + dot live).
    var _reels=feedReels(), maxMult=0, reach=0;
    _reels.forEach(function(r){ if((r.explosion||0)>maxMult) maxMult=r.explosion||0; reach+=_parseViews(r.views); });
    var multTxt = maxMult>0 ? (maxMult>=10?Math.round(maxMult):(Math.round(maxMult*10)/10))+"×" : "–";
    var reachTxt = reach>0 ? String(Math.round(reach)).replace(/\B(?=(\d{3})+(?!\d))/g," ") : _fmtK(st.reels_week||0);
    var stats=[
      {v:_fmtK(st.exploded_week||0), l:L("explotaron hoy","blew up today"), c:"var(--success-fg)", live:false},
      {v:multTxt, l:L("la mayor explosión","biggest blow-up"), c:"var(--success-fg)", live:false},
      {v:reachTxt, l:L("alcance robable · en directo","stealable reach · live"), c:"var(--text-primary)", live:true}
    ];
    var statsH=stats.map(function(s){
      return '<div class="rdr-stat"><span class="rdr-stat-v" style="color:'+s.c+'">'+ESC(String(s.v))+'</span>'+
        '<span class="rdr-stat-l">'+(s.live?'<span class="rdr-livedot"></span>':'')+ESC(s.l)+'</span></div>';
    }).join("");
    var cap = L("robando lo que explota en tu nicho","stealing what explodes in your niche");
    return '<div class="rdr-hero">'+
      '<div class="rdr-hero-l">'+
        '<div class="rdr-eyebrow"><span class="rdr-eye-dot"></span>Radar · '+L("escaneando","scanning")+' '+n+' '+L("competidor"+(n===1?"":"es"),"competitor"+(n===1?"":"s"))+'</div>'+
        '<h1 class="rdr-title">'+L("Señales de hoy","Today's signals")+'</h1>'+
        '<p class="rdr-sub">'+line+'</p>'+
        '<div class="rdr-stats">'+statsH+'</div>'+
        // Dos refrescos BIEN diferenciados: «↻ otras» GRATIS (re-baraja el pool, sin scrape) y
        // «Refrescar ahora · 5 cr» (scrape en vivo de competidores, de pago). El job diario
        // renueva gratis también.
        '<div class="rdr-refresh-row">'+
          '<button class="rdr-reshuffle-cta" data-act="reshuffle-feed" title="'+L("Baraja otras del pool que ya tienes — gratis, sin scrape","Shuffle others from the pool you already have — free, no scrape")+'">'+IC.repeat+' '+L("Otras · gratis","Others · free")+'</button>'+
          '<button class="rdr-refresh-cta" data-act="refresh-radar" title="'+L("Trae lo nuevo de tus COMPETIDORES AHORA (scrape en vivo · cuesta créditos). El radar se renueva solo cada día gratis.","Pull your COMPETITORS\' latest NOW (live scrape · costs credits). The radar auto-refreshes daily for free.")+'">'+IC.repeat+' '+L("Refrescar competidores · 5 créditos","Refresh competitors · 5 credits")+'</button>'+
          // Reorganización 04/07: «Añadir/Analizar reel» SUBEN al hero como botones visibles
          // (antes links enterrados en la addbar a media página). Secundarios a propósito:
          // la única primaria sobre el fold sigue siendo «Roba la idea» (T1).
          '<button class="rdr-reshuffle-cta" data-act="add-reel">'+IC.plus+' '+L("Añadir reel","Add reel")+'</button>'+
          '<button class="rdr-reshuffle-cta" data-act="analyze-reel" title="'+L("Transcribe un reel suelto sin seguir a su autor","Transcribe a single reel without following its author")+'">'+IC.doc+' '+L("Analizar un reel","Analyze a reel")+'</button>'+
        '</div>'+
      '</div>'+
      '<div class="rdr-hero-r">'+radarScopeHTML()+
        '<div class="rdr-cap"><span class="rdr-cap-dot"></span><span class="rdr-cap-t">'+ESC(cap)+'</span></div>'+
      '</div>'+
    '</div>';
  }
  function dashboardHTML(){
    // (v=137: la carga post-onb y «¿quieres robar este?» se sirven a pantalla completa
    //  desde render() vía .onb-fs — antes se devolvían aquí, con la chrome alrededor.)
    var st=S.stats||{competitors:0,reels_week:0,exploded_week:0,stolen_today:0}; var b=brand();
    var sorted=feedReels();
    var line = st.exploded_week>0
      ? 'Mientras no mirabas, <b>'+st.exploded_week+' reel'+(st.exploded_week>1?"s":"")+' explotaron</b> en tu nicho. Esto es lo que merece tu próximo guion.'
      : 'Tus <b>'+st.competitors+' rivales</b> publicaron '+st.reels_week+' reels esta semana. Esto es lo que merece tu próximo guion.';
    var head='<header class="phead">'+
      '<div><div class="eyebrow"><span class="pip"></span>Radar · @'+ESC(b.handle||S.user.handle||"tu_cuenta")+'</div>'+
      '<h1 class="h-title">Señales de hoy</h1>'+
      '<p class="h-sub">'+line+'</p></div>'+
      (S.user.streak>0?'<div class="phead-right"><span class="streak">'+IC.spark+' Racha '+S.user.streak+' días</span></div>':'')+
    '</header>';

    // B: vista «Reels de @X» — todos los reels del competidor, sin recorte.
    if(S.creatorFilter){
      return '<div class="scroll"><div class="canvas">'+head+""+creatorReelsHTML()+'</div></div>';
    }

    if(sorted.length===0){
      // growth-2: usuario nuevo REAL (no demo, sin competidores cargados, sin
      // filtro fav) → onboarding de activación en el propio empty-state (decisión
      // de David). Si lo salta, cae al mensaje clásico de abajo.
      if(showOnboarding()){
        return '<div class="scroll"><div class="canvas">'+head+onboardingHTML()+'</div></div>';
      }
      // Contrato B1 «ninguna marca nace muda»: con el feed vacío, mostrar el estado HONESTO de
      // sugerencias (populating «poblando tu radar» / empty / exhausted / needs_niche) + los
      // «posibles competidores» (fallback seed → nunca vacío). Solo si no hay estado honesto se
      // cae al texto genérico «Sin reels todavía».
      var _honest=suggestionsTodayHTML();
      return '<div class="scroll"><div class="canvas">'+head+""+statbarHTML()+
        manageCompetitorsHTML(true)+   // añadir competidor SIEMPRE accesible; ABIERTO: con el radar vacío es LA acción
        _honest+                       // estado honesto «poblando»/«añadiendo nicho» (o '' si no aplica)
        suggestedCompetitorsHTML()+    // posibles competidores (mínimo garantizado por fallback seed)
        voiceOnboardCardHTML()+   // B6: en first-run sin reels, el banner de voz es lo primero que aporta
        nextSeriesHTML("dash")+   // B1+T1: CTA secundario en el Dashboard
        (_honest?'':'<div class="rs-empty">'+(S.filter==="fav"?"Sin favoritos aún. Toca la estrella en una señal.":"Sin reels todavía. Añade un competidor arriba o pulsa «Actualizar radar» — sus reels entrarán solos.")+'</div>')+
      '</div></div>';
    }

    // Fathom 18/06: el día enseña 2-3 oportunidades en carrusel (no una sola).
    var heroN=sorted.slice(0,Math.min(3,sorted.length)), rest=sorted.slice(heroN.length);
    var fillCount=Math.min(5,S.reels.length)||5;

    // Reorganización 04/07 (plan David, «flujo continuo sin apartados»): arriba lo
    // accionable — hero (con «Añadir/Analizar reel») → HAZ ESTO AHORA → Oportunidades
    // («Roba la idea») → Sugerencias de hoy. Debajo la galería con sus chips-filtro, y
    // TODO lo secundario PLEGADO (details): gestiona competidores (fusión de los 3 sitios),
    // cerebro, llena mi semana, conecta IG (solo si falta). Vacíos → ocultos. Comunidad
    // FUERA (placeholder puro — vuelve cuando exista; communityGalleryHTML queda definida).
    var _bl=brainLevel();
    var _brainSub=_bl.full?L("al máximo","maxed"):(_bl.canLevelUp?L("¡listo para subir!","ready to level up!"):(_bl.pct+'%'));
    return '<div class="scroll"><div class="canvas">'+
      radarHeroHTML()+            // hero con scope animado + stats + añadir/analizar reel
      dashboardNextStepHTML()+   // #8 PROACTIVIDAD: «HAZ ESTO AHORA» — la acción más útil ya
      opportunityCarouselHTML(heroN)+   // OPORTUNIDAD justo tras el hero (acción sobre el fold)
      suggestionsTodayHTML()+    // «Sugerencias de hoy» SUBE aquí: reels del nicho que petan, robables ya
      suggestedCompetitorsHTML()+   // #3b: creadores del nicho que petan y no sigues (iniciales)
      (S.reels.length?competitorGalleryHTML():"")+   // galería (chips = filtros, sin gestión)
      manageCompetitorsHTML(false)+   // ÚNICO bloque de gestión (plegado): añadir + límite + lista con ×
      foldedSectionHTML("rsFoldBrain", IC.brain+' '+L("Tu cerebro","Your brain"), _brainSub, radarCerebroRowHTML())+
      (S.reels.length?foldedSectionHTML("rsFoldWeek", IC.bolt+' '+L("Llena mi semana","Fill my week"), L(fillCount+" guiones de golpe",fillCount+" scripts in one tap"), '<div class="plays">'+whaleHTML(fillCount)+'</div>'):"")+
      flashBannerHTML()+          // Flash 1ª compra: -30% 48h tras cruzar el muro (condicional)
      seedBannerHTML()+           // aviso «esto petó en tu nicho» (solo radar-seed, demo vacío)
      (!S.igConnected?foldedSectionHTML("rsFoldIg", IC.ig+' '+L("Conecta Instagram","Connect Instagram"), L("mide lo que publicas","measure what you post"), igConnectCardHTML()):"")+
    '</div></div>';
  }

  /* T1 · Fábrica de ideas embebida en Radar — input suelto + generadores +
     lista acordeón (idea → guiones → hooks). Reutiliza ideaBlockHTML y los
     handlers existentes (seed-go, gen5ideas, explosion, gen5scripts, gen5hooks).
     Una sola acción primaria en Radar sigue siendo «Roba la idea»: aquí todo es
     secundario/ghost. */
  // T1+refino: la fábrica de ideas vive en GUIONES, en DOS grupos por estado:
  //   · "Sin desarrollar" — ideas en bruto (gratis), sin guiones. Botón Desarrollar (cuesta).
  //   · "Desarrolladas"   — acordeón idea→guiones→hooks.
  // Una idea es la MISMA entidad: al desarrollarla gana guiones y cambia de grupo
  // en el mismo sitio (no se duplica). Clasificamos por tener guiones/scripts.
  function ideaIsDeveloped(idea){ return !!(idea && idea.scripts && idea.scripts.length>0); }
  // Marca/cliente ⇄ project_id. "default" (marca única sin project) → project_id null.
  function _pidOf(bid){ return (bid && bid!=="default") ? bid : null; }
  function brandNameOf(bid){ var b=(S.brands||[]).filter(function(x){return x.id===bid;})[0]; return b?b.name:null; }
  // La fábrica muestra solo las ideas de la marca activa (en prod /ideas ya viene
  // filtrado por project_id; esto además aísla las ideas en memoria del demo).
  function ideaBelongsToActiveBrand(idea){ return (idea._brand||"default") === (S.brandId||"default"); }
  function ideasZoneHTML(){
    var ideas=(S.ideas||[]).filter(ideaBelongsToActiveBrand);   // solo la marca activa
    var raw=ideas.filter(function(i){ return !ideaIsDeveloped(i); });
    var dev=ideas.filter(ideaIsDeveloped);
    var rawList=raw.length
      ? '<div class="ideas-list">'+raw.map(rawIdeaHTML).join("")+'</div>'
      : '<div class="rs-empty" style="margin-top:10px">Nada pendiente. Usa «Apunta una idea» (arriba) para empezar.</div>';
    var devList=dev.length
      ? '<div class="ideas-list">'+dev.map(ideaBlockHTML).join("")+'</div>'
      : '<div class="rs-empty" style="margin-top:10px">Aún ninguna desarrollada. Desarrolla una de arriba o genera 5 de golpe.</div>';
    return '<section class="ideas-zone">'+
      '<div class="feed-head"><span class="feed-title">'+IC.bulb+' Sin desarrollar'+(raw.length?' <span class="ct">· '+raw.length+'</span>':'')+'</span>'+
        '<button class="btn btn-sm btn-secondary" data-act="gen5ideas" title="'+L("Cuesta "+COST.idea5+" créditos el lote","Costs "+COST.idea5+" credits per batch")+'">'+IC.spark+' '+L("3 ideas · "+COST.idea5+" créd.","3 ideas · "+COST.idea5+" cr")+'</button>'+
      '</div>'+
      '<p class="ideas-zone-sub">Ideas en bruto, guardadas gratis. Desarrolla cuando quieras (cuesta '+COST.scripts5+' créditos).</p>'+
      '<button class="explosion-btn" data-act="explosion"><span class="ex-head">'+IC.spark+' Explosión creativa</span><span class="ex-sub">5 ideas × 5 guiones × 5 hooks — '+COST.explosion+' créditos</span></button>'+
      rawList+
      '<div class="feed-head" style="margin-top:30px"><span class="feed-title">'+IC.doc+' Desarrolladas'+(dev.length?' <span class="ct">· '+dev.length+'</span>':'')+'</span></div>'+
      devList+
    '</section>';
  }
  // Idea en bruto (sin desarrollar): texto + Desarrollar (indica el coste).
  function rawIdeaHTML(idea){
    var saving=!!idea._saving;
    return '<div class="idea-block idea-raw"'+(saving?' style="opacity:.55"':'')+'>'+
      '<div class="idea-block-head">'+
        '<div class="idea-ic">'+IC.bulb+'</div>'+
        '<div class="idea-text">'+ESC(idea.text)+'</div>'+
        (saving
          ? '<span class="idea-count"><span class="rs-ldr"></span>Guardando…</span>'
          : '<button class="btn btn-sm btn-secondary" data-act="gen5scripts" data-id="'+idea.id+'" title="Genera 5 guiones a partir de esta idea (cuesta '+COST.scripts5+' créditos)">'+IC.bolt+' Desarrollar · '+COST.scripts5+' créd.</button>')+
      '</div>'+
    '</div>';
  }

  /* ════════════════════════════════════════════════════════════════
     IDEAS — fábrica multi-generación (idea → guiones → hooks)
     ════════════════════════════════════════════════════════════════ */
  var BANK_IDEAS=[
    "El error que todos cometen al empezar con esto",
    "Lo que nadie te cuenta antes de automatizar tu negocio",
    "3 señales de que lo estás haciendo mal (y cómo arreglarlo)",
    "Cómo conseguí el mismo resultado en la mitad de tiempo",
    "La herramienta gratis que sustituye a 5 de pago",
    "Por qué dejé de hacerlo a mano (y tú también deberías)",
    "El sistema de 1 persona que parece un equipo de 10",
    "Lo probé una semana y esto fue lo que pasó"
  ];
  var BANK_HOOKS=[
    "Llevo semanas sin hacer esto a mano. Y no, no lo he abandonado.",
    "Si pierdes más de 30 min al día en esto, para y mira.",
    "Nadie te lo dice, pero seguir haciéndolo así te cuesta dinero.",
    "Hay una forma de hacerlo en 1 paso. Casi nadie la usa.",
    "El 90% lo hace mal. Y se arregla con una sola decisión.",
    "Antes de pagar por la próxima herramienta, mira esto.",
    "Esto me devolvió 6 horas a la semana. Montarlo, una tarde.",
    "Te lo enseño en 3 pasos y sin escribir una línea de código."
  ];
  function pick(arr,n,seed){ var a=arr.slice(); var out=[]; for(var i=0;i<n;i++){ var idx=(seed+i*3)%a.length; out.push(a.splice(idx%a.length,1)[0]||arr[(seed+i)%arr.length]); } return out; }
  var _gid=0; function gid(p){ return p+(++_gid); }

  /* ── modelo unificado de pieza/guión: TODO lo creado aterriza aquí ─────
     (robar un reel, llena-mi-semana, guardar desde ideas). Nada se pierde. */
  var _gseq=0;
  function addGuion(p){
    var g={ id:gid("g"), seq:++_gseq, title:(p.title||p.hook||"Guión"),
      hook:p.hook||"", beats:p.beats||[], close:p.close||"",
      hooks:p.hooks||[], expanded:false,
      from:p.from||null, brand:brand().name, type:p.type||"guión", status:"draft",
      // fuente del reel robado → miniatura/URL/stats reales en el editor (persisten en sesión).
      thumb:p.thumb||null, url:p.url||null, srcViews:p.srcViews||"", srcLikes:p.srcLikes||"",
      // origen («Ideas robadas»): mismo id que las FKs de backend → el guion cae en su grupo
      reelId:p.reelId||null, ideaId:p.ideaId||null, txId:p.txId||null,
      genOptions:p.genOptions||null };
    S.guiones.unshift(g); return g.id;
  }
  function guionById(id){ return S.guiones.filter(function(x){return x.id===id;})[0]; }
  function ideaById(id){ return (S.ideas||[]).filter(function(x){return String(x.id)===String(id);})[0]; }

  function makeScript(ideaText, seed){
    var hook=pick(BANK_HOOKS,1,seed)[0];
    return { id:gid("sc"), hook:hook,
      beats:["Te lo cuento porque a mí me cambió la forma de trabajar.","Paso uno: lo más simple, lo que casi nadie hace.","Paso dos: aquí está el 80% del resultado.","Paso tres: lo dejas funcionando y te olvidas."],
      close:"Guárdate esto y dime en comentarios por dónde empiezas.",
      hooks:null, savedHooks:{}, guionId:null, saved:false, expanded:false, idea:ideaText };
  }
  // _brand: marca/cliente al que pertenece la idea (project_id). Por defecto la
  // marca activa; se puede forzar otra (apuntar para el cliente B desde el A).
  function makeIdea(text, seed, brandId){ return { id:gid("id"), text:text, scripts:[], expanded:true, seed:seed, _brand:(brandId||S.brandId||"default") }; }

  function ideaBlockHTML(idea){
    var n=idea.scripts.length;
    var open=idea.expanded!==false;
    var scripts=idea.scripts.map(scriptBlockHTML).join("");
    var genBtn=n===0
      ? '<button class="btn btn-sm btn-primary" data-act="gen5scripts" data-id="'+idea.id+'">'+IC.bolt+' 5 guiones</button>'
      : '<button class="btn btn-sm btn-secondary" data-act="gen5scripts" data-id="'+idea.id+'">+ 5 guiones más</button>';
    var count=n?'<span class="idea-count">'+n+' guion'+(n===1?"":"es")+'</span>':'';
    var caret=n?'<button class="idea-caret'+(open?" open":"")+'" data-act="idea-toggle" data-id="'+idea.id+'" title="'+(open?"Plegar":"Desplegar")+'">'+IC.chev+'</button>':'';
    // 5 hooks DESDE LA IDEA (fuente «idea» del trío de fuentes, Fathom 18/06).
    var hooksBtn=idea.hooks?'':'<button class="btn btn-sm btn-ghost" data-act="idea5hooks" data-id="'+idea.id+'" title="Saca 5 hooks de esta idea ('+COST.hooks5+' créd.)">'+IC.hook+' 5 hooks</button>';
    var hooksList=idea.hooks?hooksResultHTML(idea.hooks):'';
    return '<div class="idea-block">'+
      '<div class="idea-block-head"><div class="idea-ic">'+IC.bulb+'</div><div class="idea-text">'+ESC(idea.text)+'</div>'+count+hooksBtn+genBtn+caret+'</div>'+
      hooksList+
      ((n&&open)?'<div class="idea-scripts">'+scripts+'</div>':'')+
    '</div>';
  }
  // Lista de hooks generados (idea/competencia) con botón Copiar — reusa data-act="copy".
  function hooksResultHTML(hooks){
    if(!hooks||!hooks.length) return '';
    return '<div class="sc-hooks hooks-result">'+hooks.map(function(h,i){
      return '<div class="sc-hook"><span class="hn">'+String(i+1).padStart(2,"0")+'</span><span>'+ESC(h)+'</span>'+
        '<span class="copy sc-hook-save" data-act="copy" data-txt="'+ESC(h)+'">Copiar</span></div>';
    }).join("")+'</div>';
  }
  function scriptBlockHTML(sc){
    var open=!!sc.expanded;
    var hooks=sc.hooks?('<div class="sc-hooks">'+sc.hooks.map(function(h,i){
      var done=sc.savedHooks&&sc.savedHooks[i];
      var act=done?'<span class="sc-hook-done">'+IC.check+' Añadido</span>'
                  :'<button class="sc-hook-save" data-act="save-hook" data-id="'+sc.id+'" data-i="'+i+'">'+IC.plus+' Añadir</button>';
      return '<div class="sc-hook"><span class="hn">'+String(i+1).padStart(2,"0")+'</span><span>'+ESC(h)+'</span>'+act+'</div>';
    }).join("")+'</div>'):'';
    var hooksBtn=sc.hooks?'':'<button class="btn btn-sm btn-ghost" data-act="gen5hooks" data-id="'+sc.id+'">'+IC.hook+' 5 hooks</button>';
    var saved=sc.saved?'<span class="sc-saved">'+IC.check+' En Guiones</span>':'<button class="btn btn-sm btn-secondary" data-act="save-script" data-id="'+sc.id+'">Guardar guión</button>';
    // Acordeón: el texto completo (cuerpo + cierre) ya vive en el objeto (sc.beats/sc.close);
    // se despliega in situ, SIN fetch. Mismo markup que scriptRevealHTML. Mismo patrón que
    // el caret de las ideas (.idea-caret + .open). Colapsado por defecto.
    var beats=(sc.beats||[]).map(function(b,i){return '<div class="beat"><span class="n">'+String(i+1).padStart(2,"0")+'</span><span>'+ESC(b)+'</span></div>';}).join("");
    var full=(open&&(beats||sc.close))?('<div class="sc-full">'+
        (beats?'<div class="script-body">'+beats+'</div>':'')+
        (sc.close?'<div class="script-close">'+ESC(sc.close)+'</div>':'')+
      '</div>'):'';
    var caret='<button class="idea-caret sc-caret'+(open?" open":"")+'" data-act="sc-toggle" data-id="'+sc.id+'" aria-expanded="'+(open?"true":"false")+'" title="'+(open?"Plegar":"Ver guión completo")+'" aria-label="'+(open?"Plegar guión":"Ver guión completo")+'">'+IC.chev+'</button>';
    return '<div class="sc-block'+(sc.saved?" is-saved":"")+(open?" open":"")+'">'+
      '<div class="sc-hook-line" data-act="sc-toggle" data-id="'+sc.id+'"><span class="sc-hook-t">'+ESC(sc.hook)+'</span>'+caret+'</div>'+
      full+
      '<div class="sc-actions">'+hooksBtn+saved+'</div>'+
      hooks+
    '</div>';
  }

  /* ════════════════════════════════════════════════════════════════
     GUIONES — validados (guardados desde Ideas / robados)
     ════════════════════════════════════════════════════════════════ */
  // Curva de retención de MUESTRA (demo). La real vendrá de Instagram Insights (OAuth).
  function retentionPts(vsMedian){
    return (vsMedian||1) >= 3 ? [100,94,87,80,74,69,65,62] : [100,80,65,54,46,41,37,34];
  }
  // Vista "Rendimiento del guion": cómo traccionó el reel publicado → entrena el Cerebro.
  function guiPerfHTML(){
    var g=guionById(S.perfGuion); if(!g) return '<div class="pad">—</div>';
    var p=g.published||{}; var pts=retentionPts(p.vsMedian); var hold=pts[3];
    var W=560,H=150,n=pts.length;
    var co=pts.map(function(v,i){ return [Math.round(i/(n-1)*W), Math.round(H-(v/100)*(H-10)-5)]; });
    var line=co.map(function(c,i){ return (i?"L":"M")+c[0]+" "+c[1]; }).join(" ");
    var area=line+" L"+W+" "+H+" L0 "+H+" Z";
    var dots=co.map(function(c){ return '<circle cx="'+c[0]+'" cy="'+c[1]+'" r="3"/>'; }).join("");
    var dur=p.dur||"0:40";
    var metrics=[["VIEWS",fmtNum(p.views||0)],["LIKES",fmtNum(p.likes||0)],["RETENCIÓN","~"+hold+"%"],["VS TU MEDIA",(p.vsMedian||1)+"×"]];
    var voicePct=brand().voice||40;
    return '<div class="perf">'+
      '<div class="perf-eyebrow">'+IC.chart+' Reel publicado'+(g.from?' · de tu guion robado a '+ESC(g.from):'')+'</div>'+
      '<h2 class="perf-title">'+ESC(g.title)+'</h2>'+
      '<div class="perf-metrics">'+metrics.map(function(m){ return '<div class="pm"><div class="pm-k">'+m[0]+'</div><div class="pm-v">'+ESC(m[1])+'</div></div>'; }).join("")+'</div>'+
      '<div class="perf-ret">'+
        '<div class="perf-ret-h">Retención <span class="muted">· '+hold+'% sigue a la mitad del reel</span> <span class="perf-sample">muestra</span></div>'+
        '<svg class="ret-svg" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none"><path class="ret-area" d="'+area+'"/><path class="ret-line" d="'+line+'"/>'+dots+'</svg>'+
        '<div class="perf-ret-x"><span>0s</span><span>'+ESC(dur)+'</span></div>'+
      '</div>'+
      '<div class="brain-section-t" style="margin-top:6px">Qué funcionó aquí</div>'+
      '<div class="learn"><div class="learn-list">'+
        ['<div class="learn-item">'+IC.check+'<span>El hook retiene al <b>'+pts[1]+'%</b> en los primeros segundos — gancho fuerte.</span></div>',
         '<div class="learn-item">'+IC.check+'<span>Duración <b>'+ESC(dur)+'</b>'+((p.vsMedian||1)>=3?' — en tu punto dulce.':'.')+'</span></div>',
         '<div class="learn-item">'+IC.check+'<span>Superó tu media <b>'+(p.vsMedian||1)+'×</b>.</span></div>'].join("")+
      '</div></div>'+
      '<div class="perf-foot">'+IC.brain+' Esto <b>entrena tu Cerebro</b> (voz al '+voicePct+'%): cada semana analizo cómo traccionan tus reels y genero más en la línea de los que petan.</div>'+
    '</div>';
  }
  /* v3 (mockup David «Guiones»): card en rejilla — badge de estado (Por grabar
     azul / Cocinando naranja / Grabado verde) + mult verde + fuente, titular Clash
     (el hook), nota «› gancho detectado», y CTA «Abrir guion» + secundario
     (Marcar grabado / Duplicar). Los controles avanzados (ver guión, hooks,
     vincular reel, perf, aprobar, descartar) se preservan en una fila compacta. */
  function guiCardHTML(g){
    var rec=g.status==="recorded";
    var cooking=!!g._cooking;
    var apr=(g.approval==="approved");
    // mult/fuente (cabecera derecha)
    var mult=g.mult||g.fromMult||null;
    var multTxt=mult?('<span class="guic-mult">'+ESC(String(mult).replace(/×\s*$/,""))+'×</span>'):'';
    var src=g.from?('<span class="guic-src">'+ESC(g.from.charAt(0)==="@"?g.from:("@"+g.from))+'</span>'):'';
    var metaR='<span class="guic-metaR">'+multTxt+src+'</span>';
    var badge=cooking
      ? '<span class="guic-badge cook">'+L("Cocinando…","Cooking…")+'</span>'
      : rec
        ? '<span class="guic-badge done">'+IC.check+' '+L("Grabado","Recorded")+'</span>'
        : '<span class="guic-badge todo">'+L("Por grabar","To record")+'</span>';
    // estado «cocinando»: esqueleto shimmer + «Cocinando el guion ···»
    if(cooking){
      return '<div class="guic-wrap"><div class="guic">'+
        '<div class="guic-top">'+badge+metaR+'</div>'+
        '<div class="guic-skel"><span class="gsk h20" style="width:88%"></span><span class="gsk" style="width:96%"></span><span class="gsk" style="width:70%"></span>'+
          '<span class="guic-cooking">'+L("Cocinando el guion","Cooking the script")+'<span class="gdots"><i></i><i></i><i></i></span></span>'+
        '</div></div></div>';
    }
    // controles avanzados preservados
    var nh=(g.hooks&&g.hooks.length)||0;
    var pub='';
    if(g.published && g.published.pending){
      pub='<span class="gui-pub pending" title="Vinculado · pendiente de análisis">'+IC.repeat+' reel vinculado · se analiza en el próximo refresco</span>';
    } else if(g.published){
      pub='<button class="gui-pub'+((g.published.vsMedian||0)>=3?" hot":"")+'" data-act="gui-perf" data-id="'+g.id+'" title="Ver rendimiento y retención">'+IC.chart+' '+fmtNum(g.published.views)+' views · <span class="gp-mult'+((g.published.vsMedian||1)>1?" up":"")+'">'+(g.published.vsMedian||1)+'×</span> tu media · ver →</button>';
    } else if(rec){
      pub='<button class="gui-link" data-act="gui-link-reel" data-id="'+g.id+'" title="Pega el link del reel publicado en Instagram para analizarlo y entrenar tu Cerebro">'+IC.repeat+' Vincular reel publicado</button>';
    }
    var toggle=nh?'<button class="gui-hooks-toggle'+(g.expanded?" open":"")+'" data-act="gui-hooks" data-id="'+g.id+'">'+IC.hook+' '+nh+' hook'+(nh===1?"":"s")+' alternativo'+(nh===1?"":"s")+' '+IC.chev+'</button>':'';
    var hasBody=!!((g.beats&&g.beats.length)||g.close||g.hook);
    var bodyOpen=!!g.bodyOpen;
    var bodyToggle=hasBody?'<button class="gui-hooks-toggle gui-body-toggle'+(bodyOpen?" open":"")+'" data-act="gui-body-toggle" data-id="'+g.id+'" aria-expanded="'+(bodyOpen?"true":"false")+'">'+IC.doc+' '+(bodyOpen?"Plegar guión":"Ver guión completo")+' '+IC.chev+'</button>':'';
    var aprBtn=isMultiBrand()?'<button class="gui-hooks-toggle'+(apr?" open":"")+'" data-act="gui-approve" data-id="'+g.id+'" title="'+(apr?"Quitar aprobado":"Aprobar (listo para cliente)")+'">'+IC.check+' '+(apr?"Aprobado":"Aprobar")+'</button>':'';
    var beatsHtml=(g.beats||[]).map(function(b,i){return '<div class="beat"><span class="n">'+String(i+1).padStart(2,"0")+'</span><span>'+ESC(b)+'</span></div>';}).join("");
    var bodyFull=(hasBody&&bodyOpen)?('<div class="sc-full gui-body">'+
        (g.hook?'<div class="gui-body-hook">'+ESC(g.hook)+'</div>':'')+
        (beatsHtml?'<div class="script-body">'+beatsHtml+'</div>':'')+
        (g.close?'<div class="script-close">'+ESC(g.close)+'</div>':'')+
      '</div>'):'';
    var hooksList=(nh&&g.expanded)?'<div class="gui-hooks">'+g.hooks.map(function(h,i){
      return '<div class="gui-hook"><span class="hn">'+String(i+1).padStart(2,"0")+'</span><span class="gui-hook-t">'+ESC(h)+'</span>'+
        '<button class="gui-hook-use" data-act="gui-use-hook" data-id="'+g.id+'" data-i="'+i+'" title="Usar como apertura">Usar</button>'+
        '<button class="gui-hook-del" data-act="gui-del-hook" data-id="'+g.id+'" data-i="'+i+'" title="Quitar variante">'+IC.x+'</button>'+
      '</div>';
    }).join("")+'</div>':'';
    // titular = hook/título; «gancho detectado» = la apertura (o 1er beat) que lo explica
    var headline=g.title||g.hook||"Guión";
    var gancho=(g.hook&&g.hook!==g.title)?g.hook:((g.beats&&g.beats[0])||g.sum||"");
    // Petición usuario: en la card solo Aprobar (agencia) / Descartar; lo demás
    // (rendimiento, ver guión completo, hooks alternativos) se accede desde el editor.
    var moreRow='<div class="guic-more">'+aprBtn+
        '<button class="gui-hooks-toggle guic-discard" data-act="gui-discard" data-id="'+g.id+'" title="Descartar">'+IC.x+' '+L("Descartar","Discard")+'</button>'+
      '</div>';
    return '<div class="guic-wrap"><div class="guic'+(rec?" is-rec":"")+'">'+
      '<div class="guic-top">'+badge+metaR+'</div>'+
      '<h3 class="guic-hook">'+ESC(headline)+'</h3>'+
      (gancho?'<div class="guic-gancho"><span class="guic-gancho-l">'+L("› gancho detectado","› hook detected")+'</span><p>'+ESC(gancho)+'</p></div>':'')+
      '<div class="guic-acts">'+
        '<button class="btn btn-md btn-primary" data-act="gui-open" data-id="'+g.id+'">'+IC.edit+' '+L("Abrir guion","Open script")+'</button>'+
        '<button class="btn btn-md btn-secondary" data-act="'+(rec?"gui-duplicate":"gui-toggle-rec")+'" data-id="'+g.id+'">'+(rec?L("Duplicar","Duplicate"):L("Marcar grabado","Mark recorded"))+'</button>'+
      '</div>'+
      moreRow+
    '</div></div>';
  }
  /* ════════════════════════════════════════════════════════════════
     IDEAS ROBADAS — agrupación por origen (reel/idea/transcripción)
     Cada idea robada = el reel fuente + sus guiones (varios) + notas.
     Clave de grupo: reel de competidor ("r:<id>") > idea con inspired_by
     reel (mismo uuid que from_competitor_reel_id) > transcripción propia
     ("t:<id>"). Sin origen → bucket «Otros guiones». Nada se pierde.
     ════════════════════════════════════════════════════════════════ */
  function guionGroupKey(g){
    if(g.reelId) return "r:"+g.reelId;
    if(g.ideaId){ var it=ideaById(g.ideaId); if(it&&it.inspiredById&&it.inspiredByType==="reel") return "r:"+it.inspiredById; }
    if(g.txId) return "t:"+g.txId;
    return null;
  }
  function stolenGroups(){
    var map={}, order=[], others=[];
    var add=function(key){ if(!map[key]){ map[key]={key:key, kind:key.charAt(0), id:key.slice(2), guiones:[], idea:null, handle:"", thumb:null, url:null, mult:null, title:""}; order.push(map[key]); } return map[key]; };
    (S.guiones||[]).filter(function(g){return g.status!=="discarded";}).forEach(function(g){
      var k=guionGroupKey(g);
      if(!k){ others.push(g); return; }
      var grp=add(k); grp.guiones.push(g);
      if(!grp.handle&&g.from) grp.handle=g.from;
      if(!grp.thumb&&g.thumb) grp.thumb=g.thumb;
      if(!grp.url&&g.url) grp.url=g.url;
      if(!grp.mult&&(g.mult||g.fromMult)) grp.mult=g.mult||g.fromMult;
    });
    // Ideas robadas aún sin guion (guardadas desde el radar) → grupo con 0 guiones;
    // las que ya tienen grupo aportan el ancla (título + notas del workspace).
    (S.ideas||[]).filter(ideaBelongsToActiveBrand).forEach(function(it){
      if(!it.inspiredById || (it.inspiredByType!=="reel" && it.inspiredByType!=="transcription")) return;
      var k=(it.inspiredByType==="reel"?"r:":"t:")+it.inspiredById;
      var grp=map[k]||add(k);
      if(!grp.idea) grp.idea=it;
      if(!grp.handle && it.inspiredByUser) grp.handle="@"+it.inspiredByUser;
    });
    order.forEach(function(grp){
      grp.title=(grp.idea&&(grp.idea.title||grp.idea.text))||(grp.guiones[0]&&grp.guiones[0].title)||grp.handle||L("Idea robada","Stolen idea");
    });
    return { groups:order, others:others };
  }
  function _groupByKey(key){ return stolenGroups().groups.filter(function(x){return x.key===key;})[0]||null; }
  // Miniatura/URL del reel fuente tras recargar: /scripts/<sid>/source resuelve por las
  // FKs que scripts ya guarda. Cacheado por grupo (S._srcByKey) — 1 fetch, no N.
  function loadGroupSource(grp){
    if(!grp || grp.thumb || grp.url) return;
    var c=S._srcByKey=S._srcByKey||{};
    var e=c[grp.key];
    if(e){ if(!e.loading){ grp.thumb=e.thumb; grp.url=e.url; } return; }
    var g=(grp.guiones||[]).filter(function(x){return x._sid;})[0];
    if(!g || isDemo()) return;
    c[grp.key]={loading:true};
    apiGet("/scripts/"+encodeURIComponent(g._sid)+"/source").then(function(r){
      var d=(r&&r.ok&&r.d)||{};
      c[grp.key]={loading:false, thumb:d.thumb_b64||null, url:d.url||null};
      if(!g.thumb&&d.thumb_b64) g.thumb=d.thumb_b64;
      if(!g.url&&d.url) g.url=d.url;
      if(S.tab==="guiones"&&(d.thumb_b64||d.url)) render();
    });
  }
  // Notas del workspace: fuente de verdad en sesión (S._notesByKey) con fallback al
  // ancla persistida (fila de `ideas` con inspired_by_id == origen del grupo).
  function _groupNotesVal(key){
    if(S._notesByKey && Object.prototype.hasOwnProperty.call(S._notesByKey,key)) return S._notesByKey[key];
    var kid=key.slice(2);
    var a=(S.ideas||[]).filter(function(x){return x.inspiredById===kid;})[0];
    return (a&&a.notes)||"";
  }
  function _wsNotesBadge(state){
    var n=document.getElementById("rsWsNotesSave"); if(!n) return;
    n.textContent = state==="saving" ? L("guardando…","saving…") : (state==="saved" ? L("guardado ✓","saved ✓") : (state==="error" ? L("no se pudo guardar","couldn't save") : ""));
  }
  function persistWsNotes(key){
    var val=(S._notesByKey&&S._notesByKey[key])||"";
    var kid=key.slice(2), kind=key.charAt(0);
    var anchor=(S.ideas||[]).filter(function(x){return x.inspiredById===kid;})[0];
    if(anchor) anchor.notes=val;
    if(isDemo()) return _wsNotesBadge("saved");
    var grp=_groupByKey(key);
    var body={ notes:val, title:((grp&&grp.title)||"").slice(0,120), username:((grp&&grp.handle)||"").replace(/^@/,"") };
    if(kind==="r") body.reel_id=kid; else body.transcription_id=kid;
    var _p=_pidOf(S.brandId); if(_p) body.project_id=_p;
    _wsNotesBadge("saving");
    apiPost("/stolen-ideas/notes", body).then(function(r){
      if(r&&r.ok){
        _wsNotesBadge("saved");
        // sin ancla previa → el backend la creó: refléjala local para siguientes lecturas
        if(!anchor && r.d && r.d.idea_id){
          S.ideas=S.ideas||[];
          S.ideas.push({ id:r.d.idea_id, text:body.title, title:body.title, scripts:[], expanded:false, seed:0, _server:true, _scriptsLoaded:true, _brand:(S.brandId||"default"),
            inspiredById:kid, inspiredByType:(kind==="r"?"reel":"transcription"), inspiredByUser:body.username||null, notes:val });
        }
      } else { _wsNotesBadge("error"); showError(L("No pude guardar tus notas. Reintenta en un momento.","Couldn't save your notes. Try again in a moment.")); }
    });
  }
  /* ── P1 · la elección de opciones/hooks se presenta SIEMPRE ──────────
     El robo persiste gen_options (2 opciones + hooks + POV + chosen). Si el robo
     acabó en background nadie eligió (chosen==null): al abrir la idea se presenta
     el MISMO reveal que en primer plano, también tras recargar u otro día. */
  function guionChoicePending(g){
    var go=g&&g.genOptions;
    return !!(go && go.chosen==null && Array.isArray(go.options) && go.options.length>1);
  }
  function groupChoicePending(grp){ return ((grp&&grp.guiones)||[]).filter(guionChoicePending)[0]||null; }
  // Reconstruye el reveal desde el guion persistido (sin reel vivo en memoria).
  function openChoiceReveal(g){
    var go=g.genOptions||{};
    var r={ id:g.reelId||g.id, creator:{handle:(g.from||"").replace(/^@/,"")},
      url:g.url||null, thumb:g.thumb||null, views:g.srcViews||"", likes:g.srcLikes||"",
      explosionTxt:g.mult||g.fromMult||null, cap:g.title||"", dur:"",
      _sid:g._sid||null, _gid:g.id,
      options:(go.options||[]), optIdx:0, hookIdx:0,
      recFormat:g.recFormat||null, povText:go.pov_text||null, _saved:false };
    applyScriptOption(r,0,0);
    S.reel=r;   // el teleprompter y la cinta «¿Y ahora?» leen S.reel
    S.revealReel=r; S.activeGuionId=g.id;
    S.tab="guiones"; S.view="script"; render();
  }
  // Cierra el estado «pendiente»: persiste la elección en gen_options.chosen.
  function persistChosen(g, optIdx){
    if(!g||!g.genOptions) return;
    g.genOptions.chosen=(optIdx==null?0:optIdx);
    if(!isDemo() && g._sid) apiPatch("/scripts/"+encodeURIComponent(g._sid), {gen_options:g.genOptions});
  }
  function wsOpen(key){
    if(!key) return; if(S.legacy) _exitLegacy();
    S._wsKey=key; S.tab="guiones"; S.detailReelId=null;
    // P1: elección pendiente en esta idea → primero el reveal (opciones + hooks).
    var pend=groupChoicePending(_groupByKey(key));
    if(pend){ openChoiceReveal(pend); return; }
    S.view="ideaws"; render();
  }
  function wsClose(){ S._wsKey=null; S.view="feed"; S.tab="guiones"; render(); }
  // Card de grupo en la lista: el reel de origen + cuántos guiones + estado agregado.
  function ideaGroupCardHTML(grp){
    loadGroupSource(grp);
    var n=grp.guiones.length;
    var nDraft=grp.guiones.filter(function(g){return g.status!=="recorded";}).length;
    var badge = n===0
      ? '<span class="guic-badge todo">'+L("Sin guion","No script")+'</span>'
      : (nDraft>0 ? '<span class="guic-badge todo">'+L("Por grabar","To record")+'</span>'
                  : '<span class="guic-badge done">'+IC.check+' '+L("Grabado","Recorded")+'</span>');
    var mult=grp.mult?('<span class="guic-mult">'+ESC(String(grp.mult).replace(/×\s*$/,""))+'×</span>'):'';
    var src=grp.handle?('<span class="guic-src">'+ESC(grp.handle)+'</span>'):'';
    var thumb=grp.thumb?('<img src="'+ESC(grp.thumb)+'" alt="" loading="lazy">'):('<div class="igc-ph" style="background:'+_galGrad(grp.key)+'"></div>');
    var notes=_groupNotesVal(grp.key);
    var meta=(n===0?L("aún sin guion","no script yet"):(n+" "+(n===1?L("guion","script"):L("guiones","scripts"))+(nDraft?(" · "+nDraft+" "+L("por grabar","to record")):"")))+(notes?(" · "+L("con notas","has notes")):"")+(groupChoicePending(grp)?(" · ⚡ "+L("elige tu versión","pick your version")):"");
    return '<div class="guic-wrap"><div class="guic igc'+(n>0&&!nDraft?" is-rec":"")+'">'+
      '<div class="guic-top">'+badge+'<span class="guic-metaR">'+mult+src+'</span></div>'+
      '<div class="igc-row">'+
        '<div class="igc-thumb">'+thumb+'</div>'+
        '<div class="igc-main"><h3 class="guic-hook">'+ESC(grp.title)+'</h3><div class="igc-meta">'+ESC(meta)+'</div></div>'+
      '</div>'+
      '<div class="guic-acts"><button class="btn btn-md btn-primary" data-act="ws-open" data-id="'+ESC(grp.key)+'">'+IC.edit+' '+L("Abrir idea","Open idea")+'</button></div>'+
    '</div></div>';
  }
  // Workspace de UNA idea robada (contenido del tab, no overlay): reel fuente +
  // notas persistentes + los guiones de ese reel (cards completas, editor incluido).
  function ideaWsHTML(){
    var back='<button class="btn btn-sm btn-secondary" data-act="ws-close">'+IC.back+' '+L("Ideas robadas","Stolen ideas")+'</button>';
    var grp=_groupByKey(S._wsKey);
    if(!grp){
      return '<div class="scroll"><div class="canvas guic-canvas"><header class="ws-head">'+back+'</header>'+
        '<div class="guic-empty"><span class="guic-empty-s">'+L("Esta idea ya no existe.","This idea no longer exists.")+'</span></div></div></div>';
    }
    loadGroupSource(grp);
    var n=grp.guiones.length;
    var handle=(grp.handle||"").replace(/^@/,"");
    var thumb=grp.thumb?('<img src="'+ESC(grp.thumb)+'" alt="" loading="lazy">'):('<div class="igc-ph" style="background:'+_galGrad(grp.key)+'"></div>');
    var rSrc=(grp.kind==="r")?reelById(grp.id):null;
    var origBtn=grp.url
      ? '<a class="btn btn-sm btn-secondary" href="'+ESC(grp.url)+'" target="_blank" rel="noopener noreferrer">'+IC.eye+' '+L("Ver original","View original")+'</a>'
      : (rSrc?'<button class="btn btn-sm btn-secondary" data-act="reel-original" data-id="'+ESC(grp.id)+'">'+IC.eye+' '+L("Ver original","View original")+'</button>':'');
    var stealAgain=rSrc?('<button class="btn btn-md btn-secondary" data-act="ws-steal-again" data-id="'+ESC(grp.id)+'">'+IC.bolt+' '+L("Robar otro guion de este reel · "+COST.script+" créd.","Steal another script from this reel · "+COST.script+" cr.")+'</button>'):'';
    var cards=n
      ? '<div class="guic-grid">'+grp.guiones.map(guiCardHTML).join("")+'</div>'
      : '<div class="guic-empty"><span class="guic-empty-s">'+L("Aún no hay guiones de esta idea.","No scripts from this idea yet.")+'</span></div>';
    return '<div class="scroll"><div class="canvas guic-canvas ws-canvas">'+
      '<header class="ws-head">'+back+'</header>'+
      '<div class="ws-src">'+
        '<div class="ws-src-thumb">'+thumb+(grp.mult?'<span class="ed-src-mult">'+IC.bolt+' '+ESC(String(grp.mult).replace(/×\s*$/,""))+'×</span>':'')+'</div>'+
        '<div class="ws-src-info">'+
          '<div class="ws-src-k">'+L("IDEA ROBADA","STOLEN IDEA")+(handle?(' · '+L("de","from")+' <b>@'+ESC(handle)+'</b>'):'')+'</div>'+
          '<h1 class="guic-hook ws-title">'+ESC(grp.title)+'</h1>'+
          '<div class="ws-src-acts">'+origBtn+'</div>'+
        '</div>'+
      '</div>'+
      '<div class="ws-notes-block">'+
        '<div class="ws-notes-head"><span class="ws-notes-k">'+L("› tus notas","› your notes")+'</span><span class="ws-notes-save" id="rsWsNotesSave"></span></div>'+
        '<textarea id="rsWsNotes" class="ws-notes" rows="3" placeholder="'+L("Ángulos, CTA, dónde grabarlo… tus notas de esta idea.","Angles, CTA, where to shoot it… your notes for this idea.")+'" oninput="try{window.RadarLoop.wsNotes(this.value)}catch(e){}">'+ESC(_groupNotesVal(grp.key))+'</textarea>'+
      '</div>'+
      '<div class="ws-scripts-h"><span class="brain-section-t">'+(n?(n+" "+(n===1?L("guion","script"):L("guiones","scripts"))):L("Guiones","Scripts"))+'</span>'+stealAgain+'</div>'+
      cards+
    '</div></div>';
  }
  function guionesHTML(){
    if(S.view==="ideaws" && S._wsKey) return ideaWsHTML();   // workspace de una idea robada
    var sg=stolenGroups();
    var filt=S.guiFilter||"all";
    var appr=S.guiApproval||"all";   // B3: filtro de aprobación (multi-marca)
    var isTodo=function(g){ return g.status!=="recorded"; };
    var apprOk=function(g){ if(!isMultiBrand()||appr==="all") return true; var a=(g.approval==="approved"); return appr==="approved"?a:!a; };
    // grupos: «Por grabar» = tiene guiones pendientes (o aún ninguno); «Grabadas» = todo grabado.
    var grpDraft=function(grp){ return !grp.guiones.length || grp.guiones.some(isTodo); };
    var groups=sg.groups.filter(function(grp){
      if(filt==="draft") return grpDraft(grp);
      if(filt==="recorded") return grp.guiones.length>0 && !grp.guiones.some(isTodo);
      return true;
    });
    var others=sg.others.filter(function(g){ if(filt==="draft") return isTodo(g); if(filt==="recorded") return g.status==="recorded"; return true; }).filter(apprOk);
    var cAll=sg.groups.length+sg.others.length;
    var cDraft=sg.groups.filter(grpDraft).length+sg.others.filter(isTodo).length;
    var cRec=cAll-cDraft;
    var sub=cDraft>0
      ? L("Cada idea = el reel original + tus guiones + tus notas. <b>"+cDraft+" por grabar</b> esperándote.","Each idea = the original reel + your scripts + your notes. <b>"+cDraft+" to record</b> waiting.")
      : L("Cada idea = el reel original + tus guiones + tus notas. Todo lo que robas aterriza aquí.","Each idea = the original reel + your scripts + your notes. Everything you steal lands here.");
    var tabs='<div class="guic-tabs">'+[["all",L("Todas","All"),cAll],["draft",L("Por grabar","To record"),cDraft],["recorded",L("Grabadas","Recorded"),cRec]].map(function(f){
      return '<button class="guic-tab'+(filt===f[0]?" on":"")+'" data-act="gui-filter" data-k="'+f[0]+'">'+f[1]+' <span class="guic-tab-c">'+f[2]+'</span></button>';
    }).join("")+'</div>';
    var header='<header class="guic-head"><div class="guic-head-l">'+
      '<h1 class="h-title guic-h1">'+L("Ideas robadas","Stolen ideas")+'</h1>'+
      '<p class="h-sub guic-sub">'+sub+'</p></div>'+tabs+'</header>';
    var body='';
    if(!groups.length && !others.length){
      body=(cAll===0)
        ? '<div class="guic-empty"><span class="guic-empty-h">'+L("Aún nada aquí.","Nothing here yet.")+'</span><span class="guic-empty-s">'+L("Roba tu primera idea en el Radar y vuelve.","Steal your first idea in the Radar and come back.")+'</span><button class="btn btn-md btn-primary" data-act="tab" data-k="dashboard">'+IC.bolt+' '+L("Ir al Radar","Go to Radar")+'</button></div>'
        : '<div class="guic-empty"><span class="guic-empty-s">'+L("Nada en este filtro.","Nothing in this filter.")+'</span></div>';
    } else {
      if(groups.length) body+='<div class="guic-group"><span class="guic-group-t">'+L("Tus ideas","Your ideas")+'</span><div class="guic-grid">'+groups.map(ideaGroupCardHTML).join("")+'</div></div>';
      if(others.length) body+='<div class="guic-group"><span class="guic-group-t">'+L("Otros guiones","Other scripts")+'</span><div class="guic-grid">'+others.map(guiCardHTML).join("")+'</div></div>';
    }
    return '<div class="scroll"><div class="canvas guic-canvas">'+
      header+
      guiUndevelopedHTML()+     // «Sin desarrollar» — ideas en bruto (cards dashed)
      body+
      guiExplosionHTML()+       // «Explosión creativa» — 5 ideas × 3 hooks
    '</div></div>';
  }
  // v3 (mockup David) — «Sin desarrollar»: ideas en bruto apuntadas, como cards
  // dashed con CTA «Desarrollar» (genera 5 guiones de esa idea). Reusa gen5scripts.
  function guiUndevelopedHTML(){
    // Las ideas con inspired_by (robadas de un reel/transcripción) ya salen como
    // grupo en «Ideas robadas» — aquí solo las apuntadas a mano.
    var raw=(S.ideas||[]).filter(ideaBelongsToActiveBrand).filter(function(i){ return !ideaIsDeveloped(i) && !(i.inspiredById&&(i.inspiredByType==="reel"||i.inspiredByType==="transcription")); });
    if(!raw.length) return '';
    var cards=raw.map(function(idea){
      var saving=!!idea._saving;
      return '<div class="guiu-card">'+
        '<span class="guiu-text">'+ESC(idea.text)+'</span>'+
        (saving
          ? '<span class="guiu-saving"><span class="rs-ldr"></span>'+L("Guardando…","Saving…")+'</span>'
          : '<button class="btn btn-sm btn-secondary" data-act="gen5scripts" data-id="'+idea.id+'" title="'+L("Genera 5 guiones de esta idea ("+COST.scripts5+" créditos)","Generate 5 scripts from this idea ("+COST.scripts5+" credits)")+'">'+L("Desarrollar","Develop")+'</button>')+
      '</div>';
    }).join("");
    return '<section class="guiu"><div class="guiu-head">'+
      '<span class="guiu-ic">'+IC.bulb+'</span>'+
      '<span class="guiu-t">'+L("Sin desarrollar","Undeveloped")+'</span>'+
      '<span class="guiu-c">'+L("ideas en bruto que apuntaste · "+raw.length,"raw ideas you jotted · "+raw.length)+'</span>'+
    '</div><div class="guiu-cards">'+cards+'</div></section>';
  }
  // v3 (mockup David) — «Explosión creativa»: CTA «Generar 5 ideas» + rejilla de
  // las ideas ya desarrolladas (idea + hasta 3 hooks). Reusa gen5ideas/explosion.
  function guiExplosionHTML(){
    var dev=(S.ideas||[]).filter(ideaBelongsToActiveBrand).filter(ideaIsDeveloped).slice(0,3);
    var grid=dev.length?('<div class="guix-grid">'+dev.map(function(idea){
      var hooks=[];
      (idea.scripts||[]).forEach(function(sc){ if(sc.hook && hooks.length<3) hooks.push(sc.hook); });
      if(!hooks.length && idea.hooks) hooks=idea.hooks.slice(0,3);
      var hl=hooks.map(function(h){ return '<div class="guix-hook">'+IC.arr+'<span>'+ESC(h)+'</span></div>'; }).join("");
      return '<div class="guix-card"><span class="guix-idea">'+ESC(idea.text)+'</span><div class="guix-hooks">'+hl+'</div></div>';
    }).join("")+'</div>'):'';
    return '<section class="guix">'+
      '<div class="guix-head"><div class="guix-head-l"><div class="guix-ic">'+IC.bolt+'</div>'+
        '<div><div class="guix-h">'+L("Explosión creativa","Creative explosion")+'</div>'+
        '<div class="guix-sub">'+L("Cinco ideas nuevas a partir de lo que explota. Cada una con tres ganchos listos.","Five fresh ideas from what's exploding. Each with three ready hooks.")+'</div></div></div>'+
        '<button class="btn btn-lg btn-primary" data-act="gen5ideas" title="'+L("Genera 3 ideas desarrolladas. Cuesta "+COST.idea5+" créditos el lote.","Generates 3 developed ideas. Costs "+COST.idea5+" credits per batch.")+'">'+IC.bolt+' '+L("Generar 3 ideas · "+COST.idea5+" créd.","Generate 3 ideas · "+COST.idea5+" cr")+'</button>'+
      '</div>'+grid+
    '</section>';
  }

  /* ════════════════════════════════════════════════════════════════
     AJUSTES (mockup David) — página isla con 3 sub-pestañas: Perfil y voz ·
     Plan y créditos · Afiliados. Datos reales donde existen (handle, plan,
     créditos, tono, muletillas); facturas/afiliados son demo (sin backend).
     ════════════════════════════════════════════════════════════════ */
  var AJ_TONES=["Directo","Humor seco","Cercano","Sin rodeos","Provocador","Didáctico","Motivador","Técnico"];
  function ajustesInit(){
    if(S.setTab) return;
    S.setTab="perfil";
    var v=brainVoice(brand()); var sel={};
    var baseTxt=(v.tono||"Directo, Cercano").toLowerCase();
    // preselecciona un tono si su nombre aparece en la voz detectada (p.ej. «Directo
    // y sin postureo» → marca «Directo»). Match por contención, no igualdad exacta.
    AJ_TONES.forEach(function(t){ sel[t]=baseTxt.indexOf(t.toLowerCase())>=0; });
    if(!Object.keys(sel).some(function(k){return sel[k];})){ sel["Directo"]=true; sel["Cercano"]=true; }
    S.setTones=sel;
    S.setPrefs={notif:true, semanal:true, beta:false};
  }
  function ajustesHTML(){
    ajustesInit();
    var b=brand();
    var name=S.user.name||b.name||"Tu cuenta";
    var handle=(b.handle||S.user.handle||"tu_cuenta").replace(/^@/,"");
    var lv=brainLevel();
    var tab=S.setTab||"perfil";
    var subtabs=[["perfil",L("Perfil y voz","Profile & voice")],["plan",L("Plan y créditos","Plan & credits")],["afiliados",L("Afiliados","Affiliates")]];
    var tabsH='<div class="aj-tabs">'+subtabs.map(function(t){
      return '<button class="aj-tab'+(tab===t[0]?" on":"")+'" data-act="set-tab" data-k="'+t[0]+'">'+t[1]+'</button>';
    }).join("")+'</div>';
    var body="";
    if(tab==="perfil") body=ajPerfilHTML(name,handle,lv);
    else if(tab==="plan") body=ajPlanHTML();
    else body=ajAfiliadosHTML();
    return '<div class="scroll"><div class="canvas aj-canvas">'+
      '<header class="aj-head"><h1 class="aj-h1">'+L("Ajustes","Settings")+'</h1>'+tabsH+'</header>'+
      body+
    '</div></div>';
  }
  function ajPerfilHTML(name,handle,lv){
    var v=brainVoice(brand());
    var reels=hasRealVoice()?(S.voice.source_count||0):(brand().reelsAnalyzed||47);
    var tonos=AJ_TONES.map(function(t){ var on=!!S.setTones[t];
      return '<button class="aj-tono'+(on?" on":"")+'" data-act="aj-tone" data-k="'+ESC(t)+'">'+(on?IC.check+' ':'')+ESC(t)+'</button>';
    }).join("");
    var frases=(v.frases&&v.frases.length)?v.frases.map(function(f){return '“'+ESC(f)+'”';}).join(", ") : "“que no te engañen”, “comenta X y te lo paso”";
    var prefs=[
      ["notif",L("Avísame cuando algo explote","Tell me when something blows up"),L("Notificación en cuanto un competidor reviente un reel","A ping the moment a competitor's reel blows up")],
      ["semanal",L("Resumen semanal del radar","Weekly radar digest"),L("Cada lunes, lo que más explotó en tu nicho","Every Monday, what blew up most in your niche")],
      ["beta",L("Funciones beta","Beta features"),L("Prueba lo nuevo antes que nadie (puede romperse)","Try the new stuff first (may break)")]
    ].map(function(p,i){
      var on=!!S.setPrefs[p[0]];
      return '<div class="aj-pref'+(i>0?" bt":"")+'"><div><div class="aj-pref-l">'+p[1]+'</div><div class="aj-pref-s">'+p[2]+'</div></div>'+
        '<button class="aj-toggle'+(on?" on":"")+'" data-act="set-pref" data-k="'+p[0]+'" role="switch" aria-checked="'+(on?"true":"false")+'"><span class="aj-knob"></span></button></div>';
    }).join("");
    return '<div class="aj-stack">'+
      // identity
      '<div class="aj-card aj-identity">'+
        '<div class="aj-avatar">'+ESC(initialsOf(name))+'</div>'+
        '<div class="aj-id"><div class="aj-id-name">'+ESC(name)+'</div><div class="aj-id-handle">@'+ESC(handle)+'</div></div>'+
        '<span class="aj-lvl">'+IC.brain+' '+L("Cerebro · Nivel "+lv.level,"Brain · Level "+lv.level)+'</span>'+
        '<button class="btn btn-md btn-secondary" data-act="ajustes-edit">'+L("Editar perfil","Edit profile")+'</button>'+
      '</div>'+
      // voz
      '<div class="aj-card aj-voice">'+
        '<div class="aj-voice-head"><div><div class="aj-card-t">'+L("Tu voz","Your voice")+'</div>'+
          '<div class="aj-card-s">'+L("La saqué de tus reels. Cada guion sale con este tono — edítalo y se nota al instante.","I pulled it from your reels. Every script comes out in this tone — edit it and you'll notice instantly.")+'</div></div>'+
          '<span class="aj-derived">'+L("› derivada de "+reels+" reels","› derived from "+reels+" reels")+'</span></div>'+
        '<div class="aj-tonos">'+tonos+'</div>'+
        '<div class="aj-mulet-wrap"><span class="aj-mulet-k">'+L("Muletillas y expresiones tuyas","Your catchphrases & expressions")+'</span><div class="aj-mulet">'+frases+'</div></div>'+
        '<div class="aj-voice-foot">'+
          '<button class="btn btn-md btn-primary" data-act="ajustes-save-voice">'+L("Guardar voz","Save voice")+'</button></div>'+
      '</div>'+
      // preferencias
      '<div class="aj-card aj-prefs">'+prefs+'</div>'+
    '</div>';
  }
  // Naming Whop (lo que cobra): Basic/Content Creator/Agency. `whop` = key del plan en
  // /api/billing/config (basic→creator). trial/free no se compran (alta o cancelación).
  var AJ_TIERS=[
    {k:"trial",   whop:null,      name:"Trial",           price:"0€",   cap:30,  feat:L("3 días Pro · 30 créditos (~10 robos) · sin tarjeta","3-day Pro · 30 credits (~10 steals) · no card")},
    {k:"free",    whop:null,      name:"Free",            price:"0€",   cap:9,   feat:L("9 créditos/mes (~3 robos) · radar y métricas gratis","9 credits/mo (~3 steals) · radar & metrics free")},
    {k:"basic",   whop:"creator", name:"Basic",           price:"29€",  cap:120, feat:L("120 créditos/mes (~40 robos) · voz · métricas","120 credits/mo (~40 steals) · voice · metrics"), featured:true},
    {k:"estudio", whop:"estudio", name:"Content Creator", price:"59€",  cap:360, feat:L("360 créditos/mes (~120 robos) · 3 marcas · prioridad","360 credits/mo (~120 steals) · 3 brands · priority")},
    {k:"agency",  whop:"agency",  name:"Agency",          price:"129€", cap:960, feat:L("960 créditos/mes (~320 robos) · 10 marcas · +96 cr/extra","960 credits/mo (~320 steals) · 10 brands · +96 cr/extra")}
  ];
  function _ajPrice(k){ return ({trial:0,free:0,basic:29,estudio:59,agency:129})[k]||0; }
  function _ajCurTier(){ var p=(S.user.plan||"free"); if(p==="creator"||p==="pro"||p==="basic")return"basic"; if(p==="estudio")return"estudio"; if(p==="agency")return"agency"; if(p==="trial")return"trial"; return"free"; }
  function _ajTier(k){ for(var i=0;i<AJ_TIERS.length;i++){ if(AJ_TIERS[i].k===k) return AJ_TIERS[i]; } return AJ_TIERS[1]; }
  function ajPlanHTML(){
    var cur=_ajCurTier(); var curMeta=_ajTier(cur);
    var cr=S.user.credits||0; var cap=curMeta.cap||0; var pct=cap?Math.max(2,Math.min(100,Math.round(cr/cap*100))):0;
    var nGuiones=S.guiones.filter(function(g){return g.status!=="discarded";}).length;
    var usage=[
      [L("Guiones generados","Scripts generated"), String(nGuiones)],
      [L("Reels analizados","Reels analyzed"), String(hasRealVoice()?(S.voice.source_count||0):(brand().reelsAnalyzed||0)).replace(/\B(?=(\d{3})+(?!\d))/g," ")],
      [L("«Llena mi semana»","«Fill my week»"), "2"]
    ].map(function(u){ return '<div class="aj-usage-row"><span>'+u[0]+'</span><span class="aj-usage-v">'+ESC(u[1])+'</span></div>'; }).join("");
    // Tarjetas de plan CLICABLES: subida → checkout Whop; bajada → plan más barato /
    // Free = cancelar (acceso hasta fin de periodo). El plan actual = «Tu plan», inerte.
    var tiers=AJ_TIERS.filter(function(t){ return t.k!=="trial" || cur==="trial"; });
    // Compliance: la tarjeta "Free" desde un plan DE PAGO es una baja → etiqueta clara
    // («Cancelar suscripción»), no un ambiguo «Bajar a Free». Desde trial no hay sub que
    // cancelar → «Pasar a Free».
    var paidNow=(cur==="basic"||cur==="estudio"||cur==="agency");
    var grid=tiers.map(function(t){
      var isCur=t.k===cur, up=_ajPrice(t.k)>_ajPrice(cur);
      var pickLbl = t.k==="free"
        ? (paidNow?L("Cancelar suscripción","Cancel subscription"):L("Pasar a Free","Switch to Free"))
        : (up?L("Mejorar","Upgrade"):L("Bajar","Downgrade"));
      var cta = isCur
        ? '<span class="aj-tier-cur">'+IC.check+' '+L("Tu plan","Your plan")+'</span>'
        : '<span class="aj-tier-cta '+(up?"up":"down")+'">'+pickLbl+' '+IC.arr+'</span>';
      return '<button class="aj-tier'+(isCur?" is-current":(t.featured?" featured":""))+'"'+
        (isCur?' disabled aria-disabled="true"':' data-act="plan-pick" data-k="'+t.k+'"')+'>'+
        '<span class="aj-tier-name">'+ESC(t.name)+(t.featured&&!isCur?' <i class="aj-tier-star">'+IC.spark+'</i>':'')+'</span>'+
        '<span class="aj-tier-price">'+ESC(t.price)+(t.price!=="0€"?'<small> /'+L("mes","mo")+'</small>':'')+'</span>'+
        '<span class="aj-tier-feat">'+ESC(t.feat)+'</span>'+cta+'</button>';
    }).join("");
    return '<div class="aj-stack">'+
      '<div class="aj-plan-grid">'+
        '<div class="aj-card aj-plancard">'+
          '<div class="aj-plan-top"><span class="aj-plan-badge">'+IC.spark+' '+L("PLAN ","PLAN ")+ESC(curMeta.name.toUpperCase())+'</span></div>'+
          '<div class="aj-plan-price"><span class="aj-plan-n">'+ESC(curMeta.price)+'</span>'+(curMeta.price!=="0€"?'<span class="aj-plan-per">/ '+L("mes","mo")+'</span>':'')+'</div>'+
          '<div class="aj-plan-cred"><div class="aj-plan-cred-row"><span>'+L("Créditos del mes","Credits this month")+'</span><span class="aj-mono">'+cr+' / '+cap+'</span></div>'+
            '<div class="aj-cred-bar"><div class="aj-cred-fill" style="width:'+pct+'%"></div></div>'+
            '<span class="aj-cred-note">'+L("1 robo = 3 créditos · ver el radar y las métricas no gasta créditos.","1 steal = 3 credits · viewing the radar and metrics is free.")+'</span></div>'+
          '<div class="aj-plan-cta"><button class="btn btn-lg btn-primary" data-act="recharge">'+IC.bolt+' '+L("Recargar créditos","Top up credits")+'</button></div>'+
        '</div>'+
        '<div class="aj-card aj-usage"><span class="aj-card-t">'+L("Este mes","This month")+'</span>'+usage+'</div>'+
      '</div>'+
      '<div class="aj-card aj-tiers-card"><div class="aj-tiers-head"><span class="aj-card-t">'+L("Cambiar de plan","Change plan")+'</span>'+
        '<span class="aj-tiers-sub">'+L("Sube o baja cuando quieras. Las bajadas se aplican al final de tu periodo.","Move up or down anytime. Downgrades apply at the end of your period.")+'</span></div>'+
        '<div class="aj-tiers">'+grid+'</div></div>'+
    '</div>';
  }
  function ajAfiliadosHTML(){
    var kpis=[
      [L("Comisión acumulada","Total commission"),"342€","var(--success-fg,#3FE0A0)",L("desde marzo","since March")],
      [L("Referidos activos","Active referrals"),"11","var(--text-primary)",L("de 19 registrados","of 19 signed up")],
      [L("Pendiente de pago","Pending payout"),"58€","var(--text-primary)",L("se abona el 1 de jul","paid out Jul 1")]
    ].map(function(k){ return '<div class="aj-card aj-kpi"><span class="aj-kpi-l">'+k[0]+'</span><span class="aj-kpi-v" style="color:'+k[2]+'">'+k[1]+'</span><span class="aj-kpi-s">'+k[3]+'</span></div>'; }).join("");
    var link="reelscript.app/r/"+((brand().handle||S.user.handle||"tu").replace(/^@/,"")).slice(0,10);
    var refs=[
      ["MA","@marcos.ai",L("hace 3 días","3 days ago"),L("Pro activo","Pro active"),"var(--success-fg,#3FE0A0)","+5,7€"],
      ["LU","@lucia.crea",L("hace 1 sem","1 wk ago"),L("Pro activo","Pro active"),"var(--success-fg,#3FE0A0)","+5,7€"],
      ["JU","@juanpe",L("hace 2 sem","2 wk ago"),L("Prueba","Trial"),"var(--text-tertiary)","—"],
      ["SO","@sofiamkt",L("hace 3 sem","3 wk ago"),L("Pro activo","Pro active"),"var(--success-fg,#3FE0A0)","+5,7€"]
    ].map(function(r,i){
      return '<div class="aj-ref'+(i>0?" bt":"")+'"><div class="aj-ref-l"><span class="aj-ref-av">'+r[0]+'</span><div><div class="aj-ref-n">'+r[1]+'</div><div class="aj-ref-d">'+r[2]+'</div></div></div>'+
        '<span class="aj-ref-st" style="color:'+r[4]+'">'+r[3]+'</span><span class="aj-ref-amt">'+r[5]+'</span></div>';
    }).join("");
    return '<div class="aj-stack">'+
      '<div class="aj-kpis">'+kpis+'</div>'+
      '<div class="aj-card aj-afflink">'+
        '<div><div class="aj-card-t">'+L("Tu enlace de afiliado","Your affiliate link")+'</div>'+
          '<div class="aj-card-s">'+L("Te llevas el <b>30%</b> recurrente de cada uno que entre por aquí.","You earn a recurring <b>30%</b> from everyone who joins through here.")+'</div></div>'+
        '<div class="aj-afflink-r"><span class="aj-afflink-url">'+ESC(link)+'</span>'+
          '<button class="btn btn-md btn-primary" data-act="copy" data-txt="'+ESC("https://"+link)+'">'+IC.doc+' '+L("Copiar","Copy")+'</button></div>'+
      '</div>'+
      '<div class="aj-card aj-refs">'+refs+'</div>'+
    '</div>';
  }

  /* ════════════════════════════════════════════════════════════════
     MÉTRICAS — conexión a Instagram + el círculo de aprendizaje
     (reels publicados ↔ guiones que los originaron → la IA aprende qué
      funciona en TU cuenta y mejora tus sugerencias y tu voz).
     ════════════════════════════════════════════════════════════════ */
  // Card extraída para reuso: página completa (tab Métricas) Y bloque plegado del
  // dashboard (reorganización 04/07). Su btn-primary no compite con T1: en el dashboard
  // vive al fondo dentro de un details cerrado (sin geometría hasta expandir).
  function igConnectCardHTML(){
    return '<div class="ig-connect">'+
        '<div class="ig-connect-ic">'+IC.ig+'</div>'+
        '<h2 class="ig-connect-h serif">Conecta tu Instagram</h2>'+
        '<p class="ig-connect-p">Aquí se cierra el círculo. El sistema mira lo que <b>publicas</b> y aprende qué hooks, qué temas y qué duración funcionan <b>en tu cuenta</b> — y con eso te da reels cada vez más tuyos. Cuanto más publicas, más te conoce.</p>'+
        '<button class="btn btn-lg btn-primary" data-act="ig-connect">'+IC.ig+' Conectar Instagram</button>'+
        '<p class="ig-connect-note">Solo lectura de tus métricas públicas. Sin contraseñas.</p>'+
      '</div>';
  }
  function connectIgHTML(){
    return '<div class="scroll"><div class="pad">'+igConnectCardHTML()+'</div></div>';
  }
  function fmtK(n){ n=Number(n||0); if(n>=1000){ var v=n/1000; return (v>=10?Math.round(v):v.toFixed(1).replace(/\.0$/,"")).toString().replace(".",",")+"k"; } return String(n); }
  // KPIs de Métricas: SIEMPRE k/M con 1 decimal (187,4k · 1,5k · 1,2M) → formato
  // unificado en todas las cifras (no unas en k y otras en crudo).
  function fmtKM(n){ n=Number(n||0);
    if(n>=1e6) return (n/1e6).toFixed(1).replace(/\.0$/,"").replace(".",",")+"M";
    if(n>=1e3) return (n/1e3).toFixed(1).replace(/\.0$/,"").replace(".",",")+"k";
    return String(n); }
  function _mean(a){ if(!a.length) return 0; return Math.round(a.reduce(function(s,x){return s+x;},0)/a.length); }
  function _median(a){ if(!a.length) return 0; var b=a.slice().sort(function(x,y){return x-y;}); var n=b.length; return n%2?b[(n-1)/2]:Math.round((b[n/2-1]+b[n/2])/2); }
  function metricVideos(){ return (S.metrics&&S.metrics.videos)||[]; }
  function sortedVideos(){
    var v=metricVideos().slice(), s=S.metricSort||"recent";
    if(s==="views") v.sort(function(a,b){return (b.views||0)-(a.views||0);});
    else if(s==="likes") v.sort(function(a,b){return (b.likes||0)-(a.likes||0);});
    else if(s==="comments") v.sort(function(a,b){return (b.comments||0)-(a.comments||0);});
    return v;
  }
  function metricStatsHTML(){
    var v=metricVideos(); var views=v.map(function(x){return x.views||0;}), likes=v.map(function(x){return x.likes||0;}), comments=v.map(function(x){return x.comments||0;});
    var totalV=views.reduce(function(s,x){return s+x;},0), totalL=likes.reduce(function(s,x){return s+x;},0), totalC=comments.reduce(function(s,x){return s+x;},0);
    var eng = totalV>0 ? ((totalL+totalC)/totalV*100) : 0;
    // Paleta AZUL en los gráficos/KPIs (decisión cliente); el verde se reserva para
    // deltas positivos y el × de «explota». Labels en frase (regla 2).
    var cards=[
      [fmtKM(_mean(views)), "Media views", fmtKM(totalV)+" total", "var(--brand-500)"],
      [fmtKM(_median(views)), "Mediana views", "", "var(--brand-400)"],
      [fmtKM(totalL), "Total likes", "", "var(--brand-400)"],
      [fmtKM(totalC), "Total comentarios", "", "var(--brand-300)"],
      [eng.toFixed(1).replace(".",",")+"%", "Engagement", "", "var(--brand-500)"]
    ];
    return '<div class="met-cards">'+cards.map(function(c){ return '<div class="met-card" style="--c:'+c[3]+'"><div class="met-card-n">'+ESC(c[0])+'</div>'+(c[2]?'<div class="met-card-sub">'+ESC(c[2])+'</div>':'')+'<div class="met-card-l">'+c[1]+'</div></div>'; }).join("")+'</div>';
  }
  function metricChartHTML(){
    var v=metricVideos(), metric=S.metricChart||"views";
    var vals=v.map(function(x){return x[metric]||0;}); var max=Math.max.apply(null,vals.concat([1]));
    var md=_mean(vals), mdn=_median(vals);
    // (5) Escala LOGARÍTMICA: lineal, un reel de 136k aplastaba a los de 1-3k
    // (barras invisibles). log(val+1)/log(max+1) preserva el orden y deja
    // legibles los pequeños. Los marcadores de media/mediana usan la MISMA
    // escala (si no, mentirían respecto a las barras).
    function logPct(val){ return Math.max(2, Math.round(Math.log(val+1)/Math.log(max+1)*100)); }
    var tabs=[["views","Views"],["likes","Likes"],["comments","Comments"],["shares","Compartidos"]].map(function(t){ return '<button class="chip-sm'+(metric===t[0]?" on":"")+'" data-act="metric-chart" data-k="'+t[0]+'">'+t[1]+'</button>'; }).join("");
    var rows=v.slice(0,10).map(function(x){
      var val=x[metric]||0, pct=logPct(val);
      var lbl=((x.date||"")+" "+(x.cap||"")).slice(0,22);
      return '<div class="bar-row"><div class="bar-lbl">'+ESC(lbl)+'</div><div class="bar-track"><div class="bar-fill'+(x.top?" is-top":"")+'" style="width:'+pct+'%"></div></div><div class="bar-val">'+fmtK(val)+'</div></div>';
    }).join("");
    var medPct=logPct(md), mdnPct=logPct(mdn);
    // Alinear el marcador con el ÁREA de barras (track empieza tras el label 180px y deja 60px de valor a la derecha).
    function lpos(p){ return 'calc(180px + (100% - 240px) * '+(p/100)+')'; }
    var lines='<div class="bar-line media" style="left:'+lpos(medPct)+'"><span>Media '+fmtK(md)+'</span></div><div class="bar-line mediana" style="left:'+lpos(mdnPct)+'"><span>Mediana '+fmtK(mdn)+'</span></div>';
    return '<div class="met-chart"><div class="met-chart-head"><div class="chips-sm">'+tabs+'</div></div><div class="bars">'+lines+rows+'</div></div>';
  }
  function metricGridHTML(){
    var v=sortedVideos();
    var sortTabs=[["recent","Recientes"],["views","Vistas"],["likes","Likes"],["comments","Comentarios"]].map(function(t){ return '<button class="fchip'+((S.metricSort||"recent")===t[0]?" on":"")+'" data-act="metric-sort" data-k="'+t[0]+'">'+t[1]+'</button>'; }).join("");
    var cards=v.map(function(x){
      var thumbInner=x.thumb?'<img src="'+ESC(x.thumb)+'" alt="">':'<div class="play"></div>';
      var badges=(x.top?'<span class="vid-badge top">TOP</span>':"")+(x.viral?'<span class="vid-badge viral">VIRAL</span>':"");
      var link=x.from_guion
        ? '<div class="pub-link"'+(x.vsMedian?' title="'+x.vsMedian+'× tu media"':'')+'>'+IC.doc+' de tu guion «'+ESC(x.from_guion)+'»'+(x.vsMedian?' · <span class="gp-mult'+(x.vsMedian>1?" up":"")+'">'+x.vsMedian+'×</span> tu media':'')+'</div>'
        : '<div class="pub-link organic">○ orgánico · sin guion</div>';
      return '<div class="vid-card"><div class="vid-thumb thumb">'+thumbInner+'<span class="dur">'+ESC(x.dur||"0:30")+'</span>'+(badges?'<div class="vid-badges">'+badges+'</div>':"")+'</div>'+
        '<div class="vid-body"><div class="vid-cap">'+ESC(x.cap)+'</div>'+
        '<div class="vid-metrics"><span>'+IC.eye+' '+fmtK(x.views)+'</span><span>'+IC.heart+' '+fmtK(x.likes)+'</span><span>'+IC.chat+' '+(x.comments||0)+'</span></div>'+
        '<div class="vid-date">'+ESC(x.date||"")+'</div>'+link+'</div></div>';
    }).join("");
    return '<div class="more-head" style="margin-top:8px"><span class="more-title">Tus reels publicados</span><div class="filters">'+sortTabs+'</div></div><div class="vid-grid">'+cards+'</div>';
  }
  // F: free ve el producto pero las MÉTRICAS al detalle van borrosas. En demo se
  // fuerza con ?free=1 (el plan normal demo es de pago).
  function isFree(){ return isDemo() ? (S._demoFree===true) : (S.realPlan==="free" || !S.realPlan); }
  // Trial = reverse-trial Pro (5 días, 3 guiones/día). En demo, el modo FREE es el trial.
  function isTrial(){ return isDemo() ? isFree() : !!S.user.trialActive; }
  // #2 conversión: guiones restantes HOY (tope diario del trial). null si no es free.
  function freeStealsLeft(){ if(!isFree()) return null; return (S.user.dayLeft!=null ? S.user.dayLeft : 3); }

  /* Flash por-usuario (economia-creditos.md §4): al cruzar el PRIMER muro, el Pack de
     FLASH_PACK_CR créditos baja a €FLASH_PACK_EUR (antes €FLASH_PACK_WAS) durante 48h,
     con countdown HONESTO (deadline fijo en localStorage → no se resetea al recargar).
     En prod el price a 29€ lo crea David en Whop (env WHOP_TOPUP_300_FLASH_*). El estado
     real llega en /auth/me.topup_flash; en demo arranca al cruzar el muro. */
  var FLASH_HOURS=48, FLASH_PLAN_PCT=40, FLASH_PLAN_EUR=29;
  var FLASH_PACK_CR=300, FLASH_PACK_EUR=29, FLASH_PACK_WAS=49;
  function flashKey(){ return isDemo()?"rs_flash_demo":"rs_flash_v1"; }
  function flashDeadline(){
    var bk=S.user&&S.user.topupFlash; if(bk&&bk.active&&bk.expires_at){ var t=Date.parse(bk.expires_at); if(t>0) return t; }
    try{ var v=localStorage.getItem(flashKey()); return v?parseInt(v,10):(S._flashDl||0); }catch(e){ return S._flashDl||0; }
  }
  // Anti-repetición (prod): tras mostrarse una vez, no reaparece en FLASH_REPEAT_DAYS.
  var FLASH_REPEAT_DAYS=14;
  function _flashSeenCookie(){ return (document.cookie.split("; ").find(function(r){return r.indexOf("rs_flash_seen=")===0;})||"").split("=")[1]; }
  // Arranca la oferta flash SOLO en el momento PEAK (primer valor real: 1er robo /
  // guión, o muro) — nunca al entrar/registrarse. Idempotente (deadline persistido) +
  // cookie anti-repetición de X días en prod (en demo no, para poder re-probar).
  function startFlash(){
    if(flashDeadline()>0) return;
    if(!isDemo() && _flashSeenCookie()) return;
    var dl=Date.now()+FLASH_HOURS*3600*1000; S._flashDl=dl;
    try{ localStorage.setItem(flashKey(),String(dl)); }catch(e){}
    if(!isDemo()){ try{ document.cookie="rs_flash_seen=1; path=/; max-age="+(FLASH_REPEAT_DAYS*86400); }catch(e){} }
  }
  function flashActive(){ if(!isFree() && !isTrial()) return false; var dl=flashDeadline(); return dl>0 && (dl-Date.now())>1000; }
  function flashRemainStr(){
    var ms=Math.max(0,flashDeadline()-Date.now()), s=Math.floor(ms/1000);
    var h=Math.floor(s/3600), m=Math.floor((s%3600)/60), ss=s%60, p=function(n){return (n<10?"0":"")+n;};
    return p(h)+":"+p(m)+":"+p(ss);
  }
  // popups-flash.html · muro in-app: LIDERA con el PLAN (Creator −40%, "lo que ChatGPT
  // no hace"), WELCOME auto-aplicado; el top-up 300 queda como alternativa secundaria.
  function flashBannerHTML(){
    if(!flashActive()) return '';
    var now=Math.round(FLASH_PLAN_EUR*(1-FLASH_PLAN_PCT/100)*100)/100;        // 29 → 17.40
    var nowTxt=(now%1?String(now.toFixed(2)).replace(".",","):String(now));
    return '<div class="flash-offer" data-act="flash-cta" role="button" tabindex="0" aria-label="Oferta: Creator a −40% el primer mes">'+
      '<span class="flash-badge">−'+FLASH_PLAN_PCT+'%</span>'+
      '<div class="flash-txt"><b>'+L("Sigue con Creator · −40% tu primer mes","Stay on Creator · −40% your first month")+'</b>'+
        '<span>'+L("Lo que ChatGPT no hace: te digo qué explota y te lo robo en tu voz","What ChatGPT won't: I tell you what's blowing up and steal it in your voice")+' · <s>€'+FLASH_PLAN_EUR+'</s> → <b>€'+nowTxt+'</b> · WELCOME ✓</span></div>'+
      '<div class="flash-cd-wrap"><span class="flash-cd-k">'+L("Termina en","Ends in")+'</span><span class="flash-cd" id="rsFlashCd">'+flashRemainStr()+'</span></div>'+
      '<span class="flash-cta">'+L("Quiero el −40%","Grab −40%")+' '+IC.arr+'</span>'+
    '</div>'+
    '<button class="flash-alt" data-act="flash-topup">'+L("¿Solo un empujón? "+FLASH_PACK_CR+" créditos (~100 robos) por €"+FLASH_PACK_EUR,"Just a boost? "+FLASH_PACK_CR+" credits (~100 steals) for €"+FLASH_PACK_EUR)+'</button>';
  }
  // Countdown vivo: actualiza el reloj cada segundo; al expirar, re-render (quita el banner).
  function ensureFlashCountdown(){
    clearInterval(S.flashTimer);
    if(!document.getElementById("rsFlashCd")) return;
    S.flashTimer=setInterval(function(){
      var el=document.getElementById("rsFlashCd");
      if(!el){ clearInterval(S.flashTimer); return; }
      if(!flashActive()){ clearInterval(S.flashTimer); render(); return; }
      el.textContent=flashRemainStr();
    },1000);
  }
  function metricsLockHTML(inner){
    return '<div class="rs-lock"><div class="rs-lock-inner" aria-hidden="true">'+inner+'</div>'+
      '<div class="rs-lock-over"><div class="rs-lock-card">'+IC.bolt+
        '<div class="rs-lock-h">Tus métricas, al detalle</div>'+
        '<div class="rs-lock-sub">Vistas, retención y los patrones que el sistema aprende de cada reel — desbloquéalo con Pro.</div>'+
        '<button class="btn btn-md btn-primary" data-act="upsell" data-k="metrics">'+IC.bolt+' Desbloquea con Pro</button>'+
      '</div></div></div>';
  }
  // Muro borroso compacto: datos COMPLETOS del competidor (métricas del reel)
  // difuminados en free → incentivo a suscribirse (Fathom). Reusa .rs-lock.
  function compMetsLock(inner){
    return '<div class="rs-lock rs-lock--sm"><div class="rs-lock-inner" aria-hidden="true">'+inner+'</div>'+
      '<div class="rs-lock-over"><button class="btn btn-sm btn-primary" data-act="upsell" data-k="competitor">'+IC.bolt+' Métricas con Pro</button></div></div>';
  }
  /* ── v3 (mockup David · Métricas): clon completo con 3 vistas (Resumen ·
     Audiencia · Competidores), paywall Pro borroso en free, y conecta-IG.
     Datos demo (como el mockup); en prod fable/Alberto los cablea a IG. ── */
  function _mSpark(arr){ var max=Math.max.apply(null,arr),min=Math.min.apply(null,arr),n=arr.length;
    return arr.map(function(v,i){ var x=(i/(n-1))*60; var y=21-((v-min)/((max-min)||1))*19; return x.toFixed(1)+","+y.toFixed(1); }).join(" "); }
  function _mBars(arr){ var max=Math.max.apply(null,arr); return arr.map(function(v){ return Math.round(v/max*100)+"%"; }); }
  function metricsHTML(){
    var b=brand();
    var locked=isFree();
    var view=S.metricView||"resumen";
    function vtab(k,es,en){ return '<button class="mt-vtab'+(view===k?" on":"")+'" data-act="metric-view" data-k="'+k+'">'+L(es,en)+'</button>'; }
    // Free (locked): SIEMPRE pintamos métricas de MUESTRA detrás → el blur del paywall
    // tiene algo que difuminar (bug Leo 25-jun: antes salía el panel negro liso porque
    // sin IG conectado `inner` quedaba vacío). Pro/conectado: las vistas reales.
    var inner = locked ? metSampleHTML() : (view==="audiencia" ? metAudienciaHTML() : (view==="competidores" ? metCompetidoresHTML() : metResumenHTML()));
    var paywall = locked ? ('<div class="mt-wall"><div class="mt-wall-card">'+
        '<div class="mt-wall-ic">'+_icLock+'</div>'+
        '<h2 class="mt-wall-h">'+L("Tus números, a un clic.","Your numbers, one click away.")+'</h2>'+
        '<p class="mt-wall-p">'+L("Borrosos a propósito. Pasa a Pro y mira qué gancho, tema, hora y competidor te están moviendo de verdad.","Blurred on purpose. Go Pro and see which hook, topic, hour and competitor are actually moving you.")+'</p>'+
        '<button class="btn btn-lg btn-primary" data-act="open-plans">'+IC.spark+' '+L("Desbloquéalos con Pro","Unlock with Pro")+'</button>'+
        '<span class="mt-wall-note">'+L("› analizando tu nicho… sin que se enteren","› analyzing your niche… without them noticing")+'</span>'+
      '</div></div>') : '';
    return '<div class="scroll"><div class="canvas mt-canvas">'+
      // header
      '<header class="mt-head"><div><h1 class="mt-h1">'+L("Tus métricas","Your metrics")+'</h1>'+
        '<p class="mt-sub">'+L("Todo lo que mueve tus reproducciones — y cómo vas contra tus competidores.","Everything that moves your plays — and how you stack up against competitors.")+'</p></div>'+
        '<div class="mt-sync"><span class="mt-sync-dot"></span>@'+ESC(b.handle||S.user.handle||"tu_cuenta")+' · '+L("sincronizado hoy","synced today")+'</div></header>'+
      // view tabs
      '<div class="mt-vtabs">'+vtab("resumen","Resumen","Overview")+vtab("audiencia","Audiencia","Audience")+vtab("competidores","Competidores","Competitors")+'</div>'+
      // analytics + paywall
      '<div class="mt-area">'+
        '<div class="mt-charts'+(locked?" locked":"")+'">'+inner+'</div>'+paywall+
      '</div>'+
      // conecta IG
      '<div class="mt-ig"><div class="mt-ig-l"><div class="mt-ig-ic">'+IC.ig+'</div>'+
        '<div><div class="mt-ig-t">'+L("¿Otra cuenta? Conéctala.","Another account? Connect it.")+'</div>'+
        '<div class="mt-ig-s">'+L("Enlaza tu Instagram y empiezo a leer qué te funciona a ti.","Link your Instagram and I'll start reading what works for you.")+'</div></div></div>'+
        '<button class="btn btn-md btn-secondary" data-act="ig-connect">'+L("Conectar Instagram","Connect Instagram")+'</button></div>'+
    '</div></div>';
  }
  // Marco visual del mockup con estado honesto "próximamente" (sin inventar datos).
  function _metPronto(es,en){
    return '<div class="mt-pronto"><span class="mt-pronto-pill">'+IC.spark+' '+L("Próximamente","Coming soon")+'</span>'+
      '<p class="mt-pronto-p">'+L(es,en)+'</p></div>';
  }
  function _durSec(d){ var p=String(d||"").split(":"); return p.length===2 ? ((+p[0])*60+(+p[1])||0) : (+p[0]||0); }
  // ── Reales desde el SCRAPE (Apify trae fecha+caption por reel) ──────────────
  function _pubDate(v){ if(!v||!v.pubAt) return null; var d=new Date(String(v.pubAt).replace(" ","T")); return isNaN(d.getTime())?null:d; }
  var _MES=["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"];
  var _MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  // TENDENCIA REAL: cada reel = una barra, ordenado por fecha de publicación, alto ∝ repros.
  // NO es una curva de "plays por día" (eso es watch-time → Graph API); es repros por reel
  // en el tiempo, 100% scrapeado. Necesita ≥2 reels con fecha; si no, "próximamente" honesto.
  function _metTimeline(V){
    var d=V.map(function(v){ return {v:v, t:_pubDate(v)}; }).filter(function(x){ return x.t; });
    if(d.length<2) return _metPronto("En cuanto tenga ≥2 reels con fecha (los trae el scraping), aquí verás tus repros por reel a lo largo del tiempo.","Once I have ≥2 dated reels (the scrape provides it), you'll see your plays per reel over time.");
    d.sort(function(a,b){ return a.t-b.t; });
    d=d.slice(-14);   // últimos 14 reels publicados (cabe en la tarjeta)
    var mx=Math.max.apply(null,d.map(function(x){return x.v.views||0;}).concat([1]));
    var bars=d.map(function(x){ var vw=x.v.views||0, lab=x.t.getDate()+" "+(L(_MES,_MON)[x.t.getMonth()]);
      return '<div class="mt-dur"><span class="mt-dur-v">'+fmtKM(vw)+'</span><div class="mt-dur-bar'+(vw===mx?" top":"")+'" style="height:'+Math.max(4,Math.round(vw/mx*100))+'%" title="'+ESC(lab)+'"></div><span class="mt-dur-l">'+ESC(lab)+'</span></div>'; }).join("");
    return '<div class="mt-durs">'+bars+'</div>';
  }
  // MEJOR MOMENTO REAL: del timestamp de publicación de TUS reels, pondera por repros y
  // saca la franja + el día que mejor te han rendido. No es un heatmap de audiencia online
  // (eso es Graph API); es cuándo publicaste TÚ lo que más reprodujo. Necesita ≥3 con fecha.
  function _metBestTime(V){
    var d=V.map(function(v){ return {v:v, t:_pubDate(v)}; }).filter(function(x){ return x.t; });
    if(d.length<3) return _metPronto("Con ≥3 reels con fecha te digo en qué franja y día publicaste lo que más rindió (sale del propio scraping).","With ≥3 dated reels I'll tell you which time-slot and day your best performers went out (from the scrape itself).");
    var BK=[[0,6,"de madrugada","late night"],[6,12,"por la mañana","in the morning"],[12,15,"al mediodía","at midday"],[15,19,"por la tarde","in the afternoon"],[19,24,"por la noche","in the evening"]];
    var DOW=L(["domingo","lunes","martes","miércoles","jueves","viernes","sábado"],["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]);
    var bk=BK.map(function(){return 0;}), dw=[0,0,0,0,0,0,0];
    d.forEach(function(x){ var vw=x.v.views||0, h=x.t.getHours(); dw[x.t.getDay()]+=vw;
      for(var i=0;i<BK.length;i++){ if(h>=BK[i][0]&&h<BK[i][1]){ bk[i]+=vw; break; } } });
    var bi=0; bk.forEach(function(val,i){ if(val>bk[bi]) bi=i; });
    var di=0; dw.forEach(function(val,i){ if(val>dw[di]) di=i; });
    var bmax=Math.max.apply(null,bk.concat([1]));
    var bars=BK.map(function(b,i){ var top=(i===bi&&bk[i]>0); return '<div class="mt-dur"><span class="mt-dur-v">'+(bk[i]?fmtKM(bk[i]):"–")+'</span><div class="mt-dur-bar'+(top?" top":"")+'" style="height:'+Math.max(4,Math.round(bk[i]/bmax*100))+'%"></div><span class="mt-dur-l">'+L(b[2].replace(/^(de |por la |al )/,""),b[3].replace(/^(late |in the |at )/,""))+'</span></div>'; }).join("");
    var line=L("Tus reels que más rindieron salieron <b>"+DOW[di]+"</b>, "+BK[bi][2]+".","Your best performers went out <b>"+DOW[di]+"</b>, "+BK[bi][3]+".");
    return '<div class="mt-besttime"><p class="mt-bt-line">'+line+'</p><div class="mt-durs">'+bars+'</div></div>';
  }
  // GANCHOS REALES: la primera frase del caption (o transcript) de tus reels que más
  // reprodujeron = los hooks que de verdad te funcionaron. Sin watch-time, sin inventar.
  function _metHooks(V){
    function opener(v){ var s=(v.tx||v.cap||"").replace(/\s+/g," ").trim(); if(!s) return ""; var cut=s.split(/(?<=[.?!…])\s/)[0]||s; if(cut.length>90) cut=cut.slice(0,88).replace(/\s\S*$/,"")+"…"; return cut; }
    var top=V.slice().sort(function(a,b){ return (b.views||0)-(a.views||0); }).map(function(v){ return {o:opener(v), v:v}; }).filter(function(x){ return x.o; }).slice(0,3);
    if(!top.length) return _metPronto("En cuanto lea el texto de tus reels (caption/transcripción) te enseño qué aperturas te rinden más.","Once I read your reels' text (caption/transcript) I'll show which openings perform best for you.");
    return '<div class="mt-hooks">'+top.map(function(x,i){ return '<div class="mt-hook"><span class="mt-hook-rank">'+(i+1)+'</span>'+
      '<span class="mt-hook-tx">«'+ESC(x.o)+'»</span>'+
      '<span class="mt-hook-v mt-mono">'+fmtKM(x.v.views||0)+(x.v.vsMedian!=null?' · '+ESC(x.v.vsMedian)+'×':'')+'</span></div>'; }).join("")+'</div>';
  }
  function metResumenHTML(){
    // DATOS REALES de tus vídeos publicados (metricVideos ← /metrics/videos). Lo que el
    // dato NO contiene (tendencia por fecha, audiencia IG, temas) → "próximamente" honesto,
    // nunca datos inventados (directiva David: no subir demo-data a prod).
    var V=metricVideos(); var hasV=V.length>0;
    if(!hasV){
      // Perfil ya conectado (lo enlazamos en el paso 1 del onboarding) → el scrape está
      // en curso: estado «analizando» que se actualiza solo (loadMetricsLight reintenta).
      if(S.igConnected){
        return '<div class="mt-stack"><div class="mt-card">'+
          '<div class="mt-empty"><div class="mt-empty-ic"><span class="analyzing-spin"></span></div>'+
          '<h3 class="mt-empty-h">'+L("Analizando tu perfil…","Analyzing your profile…")+'</h3>'+
          '<p class="mt-empty-p">'+L("Estoy leyendo tus reels publicados de Instagram. En cuanto termine verás aquí tus números REALES — se actualiza solo, no hace falta recargar.","I'm reading your published Instagram reels. Your REAL numbers will show up here shortly — it updates on its own.")+'</p></div></div></div>';
      }
      return '<div class="mt-stack"><div class="mt-card">'+
        '<div class="mt-empty"><div class="mt-empty-ic">'+IC.chart+'</div>'+
        '<h3 class="mt-empty-h">'+L("Aún no he leído tus reels","I haven't read your reels yet")+'</h3>'+
        '<p class="mt-empty-p">'+L("Conecta tu Instagram (abajo) o pulsa «Actualizar reels» y aquí verás tus números REALES: reproducciones, interacción y tus reels que más rinden.","Connect Instagram (below) or hit «Refresh reels» and you'll see your REAL numbers here.")+'</p>'+
        '<button class="btn btn-md btn-primary" data-act="metric-refresh">'+IC.repeat+' '+L("Actualizar reels","Refresh reels")+'</button></div></div></div>';
    }
    var _sum=function(f){ return V.reduce(function(a,v){ return a+(Number(v[f])||0); },0); };
    var sv=_sum("views"), sl=_sum("likes"), sc=_sum("comments"), ss=_sum("shares"); var sInter=sl+sc+ss;
    // KPIs REALES (sin sparkline/delta inventados — solo lo que sabemos de verdad).
    var kpis=[["Reproducciones",fmtKM(sv)],["Interacciones",fmtKM(sInter)],["Me gusta",fmtKM(sl)],
              ["Comentarios",fmtKM(sc)],["Compartidos",fmtKM(ss)],["Reels analizados",String(V.length)]];
    var kpiH=kpis.map(function(k){
      return '<div class="mt-kpi"><span class="mt-kpi-l">'+ESC(k[0])+'</span>'+
        '<div class="mt-kpi-row"><span class="mt-kpi-v">'+ESC(k[1])+'</span></div></div>';
    }).join("");
    // Desglose de interacción REAL.
    var eng=[["Me gusta",sl],["Compartidos",ss],["Comentarios",sc]];
    var emax=Math.max.apply(null,eng.map(function(r){return r[1];}).concat([1]));
    var engH=eng.map(function(r){ return '<div class="mt-br"><div class="mt-br-top"><span>'+r[0]+'</span><span class="mt-mono">'+fmtKM(r[1])+'</span></div><div class="mt-br-track"><i style="width:'+Math.round(r[1]/emax*100)+'%"></i></div></div>'; }).join("");
    // Duración óptima REAL: agrupa tus vídeos por franja de duración y promedia repros.
    var buckets=[["0–15s",0,15],["15–30s",15,30],["30–45s",30,45],["45–60s",45,60],["1–2m",60,120],["2m+",120,99999]];
    var bAgg=buckets.map(function(b){ var vs=V.filter(function(v){ var s=_durSec(v.dur); return s>=b[1]&&s<b[2]; }); var avg=vs.length?Math.round(vs.reduce(function(a,v){return a+(v.views||0);},0)/vs.length):0; return {l:b[0],avg:avg,n:vs.length}; });
    var bmax=Math.max.apply(null,bAgg.map(function(b){return b.avg;}).concat([1]));
    var durH=bAgg.map(function(b){ var top=(b.avg===bmax&&b.avg>0); return '<div class="mt-dur"><span class="mt-dur-v">'+(b.n?fmtKM(b.avg):"–")+'</span><div class="mt-dur-bar'+(top?" top":"")+'" style="height:'+(bmax?Math.max(4,Math.round(b.avg/bmax*100)):4)+'%"></div><span class="mt-dur-l">'+b.l+'</span></div>'; }).join("");
    // Top reels REALES.
    var top=V.slice().sort(function(a,b){ return (b.views||0)-(a.views||0); }).slice(0,4);
    var topH=top.map(function(v,i){ return '<div class="mt-top-row"><span class="mt-top-rank">'+(i+1)+'</span>'+
      '<div class="mt-top-thumb">'+(v.thumb?'<img class="mt-top-img" src="'+ESC(v.thumb)+'" alt="" loading="lazy"/>':'')+(v.dur?'<span class="mt-top-dur">'+ESC(v.dur)+'</span>':'')+'</div>'+
      '<div class="mt-top-title">'+ESC(v.cap||v.from_guion||L("(sin título)","(untitled)"))+'</div>'+
      '<div class="mt-top-stat"><div class="mt-mono">'+fmtKM(v.views||0)+'</div><div class="mt-top-k">repros</div></div>'+
      '<div class="mt-top-stat"><div class="mt-mono">'+fmtKM((v.likes||0)+(v.comments||0))+'</div><div class="mt-top-k">interac.</div></div>'+
      '<div class="mt-top-stat mt-top-mult"><div class="mt-mult">'+(v.vsMedian!=null?ESC(v.vsMedian)+"×":"–")+'</div><div class="mt-top-k">explota</div></div></div>'; }).join("");
    return '<div class="mt-stack">'+
      '<div class="mt-kpis">'+kpiH+'</div>'+
      // tendencia REAL por fecha de publicación (el scrape trae el timestamp de cada reel)
      '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Tus reels en el tiempo","Your reels over time")+'</span><span class="mt-card-meta">'+L("repros por reel · por fecha","plays per reel · by date")+'</span></div>'+_metTimeline(V)+'</div>'+
      '<div class="mt-grid2 mt-grid4">'+
        '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Desglose de interacción","Interaction breakdown")+'</span><span class="mt-card-meta">'+fmtKM(sInter)+' total</span></div><div class="mt-bars">'+engH+'</div></div>'+
        '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Duración óptima","Optimal length")+'</span><span class="mt-card-meta">'+L("repros medias · tus reels","avg plays · your reels")+'</span></div><div class="mt-durs">'+durH+'</div></div>'+
        '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Ganchos que funcionan","Hooks that work")+'</span><span class="mt-card-meta">'+L("aperturas de tus top reels","openers of your top reels")+'</span></div>'+_metHooks(V)+'</div>'+
        '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Mejores momentos para publicar","Best times to post")+'</span><span class="mt-card-meta">'+L("repros · tus publicaciones","plays · your posts")+'</span></div>'+_metBestTime(V)+'</div>'+
      '</div>'+
      '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Tus reels que más rinden","Your top-performing reels")+'</span><span class="mt-card-meta">'+V.length+' '+L("reels","reels")+'</span></div>'+topH+'</div>'+
    '</div>';
  }
  // #3 (Leo 25-jun): métricas de MUESTRA, SOLO como fondo BORROSO del paywall del free
  // («Borrosos a propósito»). El blur(9px) de .mt-charts.locked las hace ilegibles a
  // propósito; aria-hidden para que lectores de pantalla las ignoren. NUNCA se ven sin
  // blur ni a un usuario de pago (locked gobierna su uso).
  function metSampleHTML(){
    var kpis=[[L("Reproducciones","Plays"),"128.4K"],[L("Interacciones","Interactions"),"9.7K"],[L("Me gusta","Likes"),"7.1K"],
              [L("Comentarios","Comments"),"1.2K"],[L("Compartidos","Shares"),"1.4K"],[L("Reels analizados","Reels analyzed"),"14"]];
    var kpiH=kpis.map(function(k){ return '<div class="mt-kpi"><span class="mt-kpi-l">'+k[0]+'</span><div class="mt-kpi-row"><span class="mt-kpi-v">'+k[1]+'</span></div></div>'; }).join("");
    var vbar=function(arr){ return arr.map(function(d){ return '<div class="mt-dur"><span class="mt-dur-v">'+d[1]+'</span><div class="mt-dur-bar'+(d[2]>=100?" top":"")+'" style="height:'+d[2]+'%"></div><span class="mt-dur-l">'+d[0]+'</span></div>'; }).join(""); };
    var hbar=function(arr){ return arr.map(function(r){ return '<div class="mt-br"><div class="mt-br-top"><span>'+r[0]+'</span><span class="mt-mono">'+r[1]+'</span></div><div class="mt-br-track"><i style="width:'+r[2]+'%"></i></div></div>'; }).join(""); };
    var tl=[["1","40K",42],["2","58K",58],["3","35K",35],["4","71K",71],["5","49K",49],["6","88K",88],["7","63K",63],["8","77K",77],["9","52K",52],["10","95K",100],["11","68K",68],["12","81K",81]];
    var eng=[[L("Me gusta","Likes"),"7.1K",100],[L("Compartidos","Shares"),"1.4K",42],[L("Comentarios","Comments"),"1.2K",34]];
    var durs=[["0–15s","8.2K",46],["15–30s","14.1K",78],["30–45s","19.0K",100],["45–60s","11.3K",60],["1–2m","6.4K",34],["2m+","3.1K",18]];
    var hooks=[["“te lo cuento porque…”","32K",100],["“nadie habla de esto…”","24K",75],["“deja de hacer…”","18K",56]];
    var best=[["Lun","6K",40],["Mar","9K",62],["Mié","13K",90],["Jue","11K",74],["Vie","14K",100],["Sáb","7K",48],["Dom","5K",32]];
    var tops=[[L("Mi rutina de 5 min","My 5-min routine"),"42K","3.1K","2.4×"],[L("3 errores que cometía","3 mistakes I made"),"31K","2.2K","1.8×"],
              [L("Lo que nadie te dice","What nobody tells you"),"27K","1.9K","1.6×"],[L("Cómo empecé","How I started"),"19K","1.1K","1.1×"]];
    var topH=tops.map(function(v,i){ return '<div class="mt-top-row"><span class="mt-top-rank">'+(i+1)+'</span>'+
      '<div class="mt-top-thumb"></div>'+
      '<div class="mt-top-title">'+v[0]+'</div>'+
      '<div class="mt-top-stat"><div class="mt-mono">'+v[1]+'</div><div class="mt-top-k">'+L("repros","plays")+'</div></div>'+
      '<div class="mt-top-stat"><div class="mt-mono">'+v[2]+'</div><div class="mt-top-k">'+L("interac.","interac.")+'</div></div>'+
      '<div class="mt-top-stat mt-top-mult"><div class="mt-mult">'+v[3]+'</div><div class="mt-top-k">'+L("explota","explodes")+'</div></div></div>'; }).join("");
    return '<div class="mt-stack" aria-hidden="true">'+
      '<div class="mt-kpis">'+kpiH+'</div>'+
      '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Tus reels en el tiempo","Your reels over time")+'</span><span class="mt-card-meta">'+L("repros por reel · por fecha","plays per reel · by date")+'</span></div><div class="mt-durs">'+vbar(tl)+'</div></div>'+
      '<div class="mt-grid2 mt-grid4">'+
        '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Desglose de interacción","Interaction breakdown")+'</span><span class="mt-card-meta">9.7K total</span></div><div class="mt-bars">'+hbar(eng)+'</div></div>'+
        '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Duración óptima","Optimal length")+'</span><span class="mt-card-meta">'+L("repros medias","avg plays")+'</span></div><div class="mt-durs">'+vbar(durs)+'</div></div>'+
        '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Ganchos que funcionan","Hooks that work")+'</span><span class="mt-card-meta">'+L("aperturas top","top openers")+'</span></div><div class="mt-bars">'+hbar(hooks)+'</div></div>'+
        '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Mejores momentos","Best times")+'</span><span class="mt-card-meta">'+L("repros · posts","plays · posts")+'</span></div><div class="mt-durs">'+vbar(best)+'</div></div>'+
      '</div>'+
      '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Tus reels que más rinden","Your top-performing reels")+'</span><span class="mt-card-meta">14 reels</span></div>'+topH+'</div>'+
    '</div>';
  }
  function metAudienciaHTML(){
    // Sin endpoint de audience-insights de IG → marco listo + "próximamente" honesto.
    // NO inventamos edad/género/ubicaciones (directiva David).
    var card=function(t){ return '<div class="mt-card"><span class="mt-card-t">'+t+'</span>'+_metPronto("Necesita los insights de audiencia de Instagram (Graph API). El marco está listo; en cuanto conectemos esa cuenta, estos datos serán reales.","Needs Instagram audience insights (Graph API). The frame is ready; once connected these will be real.")+'</div>'; };
    return '<div class="mt-stack">'+
      '<div class="mt-aud-hero">'+
        '<div class="mt-aud-hero-ic">'+IC.spark+'</div>'+
        '<div><h3 class="mt-aud-hero-h">'+L("Audiencia — en camino","Audience — on the way")+'</h3>'+
        '<p class="mt-aud-hero-p">'+L("Crecimiento de seguidores, alcance por tipo, edad, género y ubicaciones salen de los insights de audiencia de Instagram. Aún no están conectados, así que no te enseño números inventados.","Follower growth, reach by type, age, gender and locations come from Instagram audience insights. Not connected yet, so I won't show you made-up numbers.")+'</p></div>'+
      '</div>'+
      '<div class="mt-grid-audtop">'+card(L("Crecimiento de seguidores","Follower growth"))+card(L("Alcance por tipo","Reach by type"))+'</div>'+
      '<div class="mt-grid3">'+card(L("Edad","Age"))+card(L("Género","Gender"))+card(L("Top ubicaciones","Top locations"))+'</div>'+
      // Bucket Meta Graph API: shares reales + retención esperan la misma conexión.
      '<div class="mt-grid2">'+card(L("Compartidos reales","Real shares"))+card(L("Retención / watch-time","Retention / watch-time"))+'</div>'+
    '</div>';
  }
  function metCompetidoresHTML(){
    // Tabla derivada de tus competidores REALES (S.tracked + sus reels en el radar).
    // La "cuota de atención del nicho" necesita repros agregadas → próximamente honesto.
    var b=brand();
    var t=Array.isArray(S.tracked)?S.tracked:[];
    var cols=[L("repros medias","avg plays"),L("interacción","engagement"),L("reels","reels")];
    // Agrega por competidor desde S.reels (lo que hay en el radar).
    function _agg(handle){ var rs=(S.reels||[]).filter(function(r){ return r.creator&&r.creator.handle===handle; });
      if(!rs.length) return null;
      var av=Math.round(rs.reduce(function(a,r){return a+(r.views||0);},0)/rs.length);
      var en=rs.reduce(function(a,r){return a+(r.engagement||0);},0)/rs.length;
      return {avg:av, eng:en, n:rs.length}; }
    var meAgg=null; var mv=metricVideos(); if(mv.length){ meAgg={avg:Math.round(mv.reduce(function(a,v){return a+(v.views||0);},0)/mv.length), n:mv.length}; }
    var rowsArr=[];
    if(meAgg) rowsArr.push({ini:initialsOf(b.handle||"tu"),h:"@"+(b.handle||S.user.handle||"tu_cuenta"),tag:L("tú","you"),me:1,avg:meAgg.avg,eng:null,n:meAgg.n});
    t.forEach(function(tt){ var h=(tt.creator&&tt.creator.ig_username)||tt.ig_username||""; if(!h) return; var a=_agg(h);
      rowsArr.push({ini:initialsOf(h),h:"@"+h,tag:(a?(a.n+" "+L("reels","reels")):L("analizando…","analyzing…")),me:0,avg:a?a.avg:null,eng:a?a.eng:null,n:a?a.n:0}); });
    var head='<div class="mt-comp-head"><span>'+L("cuenta","account")+'</span>'+cols.map(function(c){ return '<span>'+ESC(c)+'</span>'; }).join("")+'</div>';
    var rows=rowsArr.length ? rowsArr.map(function(r){
      return '<div class="mt-comp-row'+(r.me?" me":"")+'">'+
        '<div class="mt-comp-acct"><span class="mt-comp-av">'+ESC(r.ini)+'</span><div><div class="mt-comp-n">'+ESC(r.h)+'</div><div class="mt-comp-tag'+(r.me?" me":"")+'">'+ESC(r.tag)+'</div></div></div>'+
        '<span class="mt-comp-c">'+(r.avg!=null?fmtKM(r.avg):"–")+'</span>'+
        '<span class="mt-comp-c">'+(r.eng!=null?(r.eng.toFixed(1)+"%"):"–")+'</span>'+
        '<span class="mt-comp-c mt-mono">'+(r.n||0)+'</span></div>';
    }).join("") : '';
    var table = rowsArr.length
      ? '<div class="mt-card mt-comp"><div class="mt-comp-table">'+head+rows+'</div></div>'
      : '<div class="mt-card"><span class="mt-card-t">'+L("Tus competidores","Your competitors")+'</span>'+_metPronto("Sigue a algún competidor desde el Radar y aquí verás cómo vas contra ellos (repros medias, interacción).","Follow a competitor from the Radar and you'll see how you stack up here.")+'</div>';
    return '<div class="mt-stack">'+table+
      '<div class="mt-grid2">'+
        '<div class="mt-card"><div class="mt-card-head"><span class="mt-card-t">'+L("Cuota de atención del nicho","Niche share of voice")+'</span></div>'+_metPronto("La cuota de atención compara las repros totales del nicho — necesita el histórico agregado de cada competidor. En cuanto lo tenga, aquí va tu % real.","Share of voice compares total niche plays — needs each competitor's aggregated history. Real % coming once available.")+'</div>'+
        '<div class="mt-card mt-opp"><span class="mt-opp-eye">'+L("› cómo usarlo","› how to use it")+'</span>'+
          '<h3 class="mt-opp-h">'+L("Roba lo que les explota a ellos.","Steal what's exploding for them.")+'</h3>'+
          '<p class="mt-opp-p">'+L("Sus reels que más rinden ya están en tu Radar, listos para robar en tu voz.","Their top reels are already in your Radar, ready to steal in your voice.")+'</p>'+
          '<button class="btn btn-md btn-primary" data-act="tab" data-k="dashboard">'+IC.bolt+' '+L("Ver sus reels en el Radar","See their reels in the Radar")+'</button></div>'+
      '</div>'+
    '</div>';
  }
  /* MÉTRICAS · Audiencia (mockup Métricas.dc.html). En prod los datos vienen de las
     audience-insights de IG; aquí, deterministas por handle (vía _lbHash) para que el
     layout viva. GAP: falta el endpoint de audience-insights — el render ya está listo. */
  function metricAudienceHTML(){
    var b=brand(), h=(b.handle||S.user.handle||"tu_cuenta");
    var newF=_lbHash(h+"nf",1200,3600);
    var trend=[]; for(var i=0;i<14;i++){ trend.push(34+_lbHash(h+"t"+i,0,60)); }
    var tmax=Math.max.apply(null,trend);
    var bars=trend.map(function(v){ return '<span class="aud-gcol"><i style="height:'+Math.round(v/tmax*100)+'%"></i></span>'; }).join("");
    var nonF=58+_lbHash(h+"nf2",0,10), foll=100-nonF;
    var ed=[["18–24",22],["25–34",41],["35–44",24],["45+",13]], edmax=41;
    var edHTML=ed.map(function(e){ return '<div class="aud-row"><div class="aud-rowt"><span>'+e[0]+'</span><b>'+e[1]+'%</b></div><div class="aud-track"><i style="width:'+Math.round(e[1]/edmax*100)+'%"></i></div></div>'; }).join("");
    var men=60+_lbHash(h+"g",0,8), women=100-men;
    var locs=[["España",38],["México",19],["Argentina",12],["Colombia",9],["EE. UU.",7]], lmax=38;
    var locHTML=locs.map(function(l){ return '<div class="aud-row"><div class="aud-rowt"><span>'+l[0]+'</span><b>'+l[1]+'%</b></div><div class="aud-track"><i class="g" style="width:'+Math.round(l[1]/lmax*100)+'%"></i></div></div>'; }).join("");
    return '<div class="aud-wrap">'+
      '<div class="aud-grid2">'+
        '<div class="aud-card"><div class="aud-head"><span class="aud-t">'+L("Crecimiento de seguidores","Follower growth")+'</span><span class="aud-up">▲ +'+_fmtK(newF)+'</span></div><div class="aud-graph">'+bars+'</div></div>'+
        '<div class="aud-card"><span class="aud-t">'+L("Alcance por tipo","Reach by type")+'</span>'+
          '<div class="aud-rows"><div class="aud-row"><div class="aud-rowt"><span>'+L("No seguidores","Non-followers")+'</span><b>'+nonF+'%</b></div><div class="aud-track"><i style="width:'+nonF+'%"></i></div></div>'+
          '<div class="aud-row"><div class="aud-rowt"><span>'+L("Seguidores","Followers")+'</span><b>'+foll+'%</b></div><div class="aud-track"><i class="dim" style="width:'+foll+'%"></i></div></div></div>'+
          '<p class="aud-note">'+L("Llegas más a gente nueva que a tu base. ","You reach more new people than your base. ")+'<b>'+L("Creces, no reciclas.","You\'re growing, not recycling.")+'</b></p></div>'+
      '</div>'+
      '<div class="aud-grid3">'+
        '<div class="aud-card"><span class="aud-t">'+L("Edad","Age")+'</span><div class="aud-rows">'+edHTML+'</div></div>'+
        '<div class="aud-card"><span class="aud-t">'+L("Género","Gender")+'</span><div class="aud-gen"><i style="width:'+men+'%"></i><i class="dim" style="width:'+women+'%"></i></div>'+
          '<div class="aud-genleg"><span><span class="aud-dot"></span>'+L("Hombres","Men")+' '+men+'%</span><span><span class="aud-dot dim"></span>'+L("Mujeres","Women")+' '+women+'%</span></div>'+
          '<p class="aud-note">'+L("Sobre todo ","Mostly ")+'<b>'+L("hombres de 25–34","men 25–34")+'</b>. '+L("Háblales a ellos.","Speak to them.")+'</p></div>'+
        '<div class="aud-card"><span class="aud-t">'+L("Top ubicaciones","Top locations")+'</span><div class="aud-rows">'+locHTML+'</div></div>'+
      '</div>'+
    '</div>';
  }
  /* MÉTRICAS · Competidores (mockup Métricas.dc.html): tabla tú-vs-rivales + cuota de
     atención + "hueco detectado" ("@x publica 6×/sem, tú 5"). Reusa leaderboardRows()
     para que los rivales sean los mismos del resto de la isla. */
  function metricCompeteHTML(){
    var rows=leaderboardRows();
    var me=rows.filter(function(r){return r.you;})[0]||{handle:(brand().handle||"tu_cuenta"),followers:42000};
    var head='<div class="cmp-row cmp-head"><span class="cmp-acct">'+L("cuenta","account")+'</span><span>'+L("seguidores","followers")+'</span><span>'+L("repros medias","avg views")+'</span><span>'+L("interacción","engagement")+'</span><span>'+L("explota","explodes")+'</span></div>';
    var body=rows.slice(0,6).map(function(r){
      var you=!!r.you;
      var views=_lbHash(r.handle+"v",18,260)/10;
      var eng=(_lbHash(r.handle+"e",18,72)/10).toFixed(1);
      var ratio=(_lbHash(r.handle+"r",11,34)/10).toFixed(1);
      return '<div class="cmp-row'+(you?' me':'')+'">'+
        '<span class="cmp-acct"><span class="cmp-ava">'+ESC(initialsOf(r.handle))+'</span><span class="cmp-h">@'+ESC(r.handle)+(you?' <em>'+L("tú","you")+'</em>':'')+'</span></span>'+
        '<span class="cmp-n">'+_fmtK(r.followers)+'</span>'+
        '<span class="cmp-n">'+views.toFixed(1)+'K</span>'+
        '<span class="cmp-n">'+eng+'%</span>'+
        '<span class="cmp-ratio">'+ratio+'×</span></div>';
    }).join("");
    var others=rows.filter(function(r){return !r.you;});
    var rival=others[0]||{handle:"tu_rival"};
    var rivalCad=4+_lbHash((rival.handle||"r")+"c",0,4), myCad=Math.max(2,rivalCad-1);
    var share=[["@"+(rival.handle||"rival"),34,'var(--text-primary)'],[L("Tú","You")+" @"+(me.handle||"tu_cuenta"),21,'var(--brand-500)'],["@"+((others[1]||{}).handle||"otro"),18,'var(--text-secondary)'],[L("El resto","The rest"),27,'var(--text-tertiary)']];
    var smax=34;
    var shareHTML=share.map(function(s){ return '<div class="aud-row"><div class="aud-rowt"><span style="color:'+s[2]+'">'+ESC(s[0])+'</span><b>'+s[1]+'%</b></div><div class="aud-track"><i style="width:'+Math.round(s[1]/smax*100)+'%;background:'+s[2]+'"></i></div></div>'; }).join("");
    return '<div class="aud-wrap">'+
      '<div class="cmp-table">'+head+body+'</div>'+
      '<div class="aud-grid2">'+
        '<div class="aud-card"><div class="aud-head"><span class="aud-t">'+L("Cuota de atención del nicho","Niche share of voice")+'</span><span class="cmp-sub">'+L("repros · 30 días","views · 30d")+'</span></div><div class="aud-rows">'+shareHTML+'</div></div>'+
        '<div class="cmp-gap"><span class="cmp-gap-k">'+L("› hueco detectado","› gap found")+'</span>'+
          '<h3 class="cmp-gap-h">@'+ESC(rival.handle||"rival")+' '+L("publica","posts")+' '+rivalCad+'×/'+L("sem","wk")+'. '+L("Tú","You")+', '+myCad+'. '+L("Pero tu interacción por reel es mayor.","But your engagement per reel is higher.")+'</h3>'+
          '<p class="cmp-gap-p">'+L("Mismo esfuerzo, más retorno: te falta volumen, no calidad. Roba un tema más esta semana.","Same effort, more return: you lack volume, not quality. Steal one more topic this week.")+'</p>'+
          '<button class="btn btn-sm btn-primary" data-act="tab" data-k="dashboard">'+IC.bolt+' '+L("Ver sus reels en el Radar","See their reels in the Radar")+'</button></div>'+
      '</div>'+
    '</div>';
  }

  /* ════════════════════════════════════════════════════════════════
     CEREBRO — la base de conocimiento de la marca: lo que el sistema
     sabe de ti y cómo crece (voz + métricas + competidores + historial).
     Es el moat hecho visible (principio II del manifiesto).
     ════════════════════════════════════════════════════════════════ */
  function hasRealVoice(){ return !!(S.voice && S.voice.has_profile); }
  /* B · NIVEL del Cerebro — derivado SOLO de señales reales (nada cosmético):
     voz entrenada (S.voice del backend), competidores seguidos, guiones creados,
     reels publicados con métricas. Cada nivel es una checklist VISIBLE; el nivel
     es el más alto con todo cumplido (escalera estricta — sin saltos mágicos).
     El beneficio es real, no marketing: la voz y los top-scripts entran en el
     prompt de cada generación (voice_prompt_block / top_scripts en backend). */
  function brainSignals(){
    var voicePct=hasRealVoice()?Math.min(100,S.voice.confidence||0):0;
    var nComps=Array.isArray(S.tracked)?S.tracked.length:((S.stats&&S.stats.competitors)||brainCompetitors().length);
    var nGuiones=(S.guiones||[]).filter(function(g){return g.status!=="discarded";}).length;
    var nPub=metricVideos().length;
    return {voice:voicePct, comps:nComps, guiones:nGuiones, pub:nPub};
  }
  /* ── XP del Cerebro (modelo 2026-06-24, pedido por Leo) ──────────────────────
     Cada nivel tiene su PROPIA barra 0→100%. Al 100% → botón ¡Subir de nivel! +
     animación → la barra se REINICIA a 0 para el siguiente nivel. Hasta N4→N5; al
     reclamar N5 se queda a 100% (máximo). El % = HITOS (acciones puntuales, derivadas
     de señales reales) + EJERCICIO DIARIO acumulado (botón «Alimentar», CD 24h, +5%).
     Niveles reclamables: 1 Aprendiz · 2 Imitador · 3 Ladrón · 4 Estratega · 5 Viral.
     Empiezas en 0 (Cerebro nuevo); el TUTORIAL llena la barra de N1 → primer subir. */
  var BRAIN_DAILY_GAIN=5;   // % por completar el ejercicio del día (train deck / tinder)
  // FORMAS de alimentar el Cerebro (David 2026-06-24): cada una su % y su CD de 24h.
  // El GUION es el que MÁS suma (es el activo más valioso para clavar tu voz).
  var BRAIN_FEED_TYPES=[
    {key:"guion",     gain:6, btype:"guiones", label:L("Un guion tuyo que petó","A script of yours that worked"), ph:L("Pega un guion o idea que te funcionó de verdad…","Paste a script or idea that really worked…")},
    {key:"hook",      gain:2, btype:"hook",    label:L("Un hook que te mola","A hook you like"),                 ph:L("Un gancho de apertura que te encante…","An opening hook you love…")},
    {key:"muletilla", gain:2, btype:"hook",    label:L("Tus muletillas","Your catchphrases"),                   ph:L("Frases que sueles decir, tu forma de hablar…","Phrases you often say, how you talk…")},
    {key:"prefer",    gain:1, btype:"hook",    label:L("Tu estilo / preferencias","Your style / preferences"),  ph:L("Cómo hablas, qué evitas, a quién admiras…","How you talk, what you avoid, who you admire…")}
  ];
  function brainFeedDef(key){ for(var i=0;i<BRAIN_FEED_TYPES.length;i++){ if(BRAIN_FEED_TYPES[i].key===key) return BRAIN_FEED_TYPES[i]; } return BRAIN_FEED_TYPES[0]; }
  // Claves v2 (reinician el modelo viejo): nivel reclamado, XP diario.
  function _brainKey(suf){ return "rs_brain_"+suf+"_"+((S.user&&S.user.email)||"x")+"|"+(S.brandId||""); }
  function brainClaimedGet(){
    if(isDemo()) return S._demoClaimed!=null?S._demoClaimed:0;   // demo arranca en 0 → previsualizar desde N1
    try{ var v=localStorage.getItem(_brainKey("clv2")); return v!=null?parseInt(v,10):0; }catch(e){ return 0; }
  }
  function brainClaimedSet(n){ if(isDemo()){ S._demoClaimed=n; return; } try{ localStorage.setItem(_brainKey("clv2"),String(n)); }catch(e){} }
  function brainDailyXpGet(){ if(isDemo()) return S._demoDailyXp||0; try{ var v=localStorage.getItem(_brainKey("xpv2")); return v!=null?parseInt(v,10):0; }catch(e){ return 0; } }
  function brainDailyXpSet(n){ n=Math.max(0,n); if(isDemo()){ S._demoDailyXp=n; return; } try{ localStorage.setItem(_brainKey("xpv2"),String(n)); }catch(e){} }
  // CD de 24h POR TIPO de alimentación (localStorage bf_<key>). Demo: sin CD → puedes
  // alimentar repetido y ver la barra subir.
  function _bfTsGet(key){ if(isDemo()) return 0; try{ return parseInt(localStorage.getItem(_brainKey("bf_"+key))||"0",10)||0; }catch(e){ return 0; } }
  function _bfTsSet(key){ if(isDemo()) return; try{ localStorage.setItem(_brainKey("bf_"+key),String(Date.now())); }catch(e){} }
  function brainFeedReady(key){ if(isDemo()) return true; return (Date.now()-_bfTsGet(key))>=24*3600*1000; }
  function brainFeedNextHrs(key){ var ms=24*3600*1000-(Date.now()-_bfTsGet(key)); return Math.max(1,Math.ceil(ms/3600000)); }
  function brainAnyFeedReady(){ if(isDemo()) return true; for(var i=0;i<BRAIN_FEED_TYPES.length;i++){ if(brainFeedReady(BRAIN_FEED_TYPES[i].key)) return true; } return brainFeedReady("train"); }
  // Compat con radar/banner: "exReady" = ¿queda algo que alimentar hoy?
  function brainExReady(){ return brainAnyFeedReady(); }
  function brainExNextHrs(){ var m=99; BRAIN_FEED_TYPES.forEach(function(t){ if(!brainFeedReady(t.key)) m=Math.min(m,brainFeedNextHrs(t.key)); }); return m===99?1:m; }
  // Suma XP a la barra del nivel actual (el CD lo comprueba el caller por tipo).
  function brainAddXp(gain){ brainDailyXpSet(brainDailyXpGet()+(gain||BRAIN_DAILY_GAIN)); }
  // Ejercicio del día (train deck / tinder) bajo su propio CD "train".
  function brainAddDailyXp(){ if(!brainFeedReady("train")) return false; brainAddXp(BRAIN_DAILY_GAIN); _bfTsSet("train"); return true; }
  // HITOS por nivel: el % NO-diario de la barra del nivel que estás completando (next=claimed+1).
  function brainMilestonePct(claimed, s){
    var n=claimed+1;   // nivel que estás llenando
    if(n===1) return (isDemo() || (S.user&&S.user.onbV2Done)) ? 100 : 0;   // N1: acabar el tutorial = 100%
    if(n===2){
      if(isDemo()) return 75;   // demo: hitos casi hechos → 5 «Alimentar» para previsualizar la subida
      var m=Math.min(s.guiones,4)*12.5;   // 4 guiones = 50% (12,5% c/u)
      if(s.voice>=30) m+=25;              // voz ≥30% = 25%
      return Math.min(75, m);             // tope hitos 75% → el 25% final SIEMPRE es ejercicio diario
    }
    return isDemo()?75:0;   // N3..N5: de momento SOLO ejercicio diario (demo arranca al 75% para previsualizar)
  }
  function brainLevel(){
    var s=brainSignals();
    var claimed=brainClaimedGet();   // 0..5 (0 = Cerebro nuevo, sin reclamar N1)
    if(claimed>=5){
      // Nivel máximo: barra fija al 100%, sin botón. Shims para call-sites viejos.
      return {level:5, claimed:5, next:null, canLevelUp:false, pct:100, milestone:100, daily:0, full:true,
              exReady:false, signals:s, earned:5, reqs:[], missing:[], nextAction:null};
    }
    var milestone=brainMilestonePct(claimed, s);
    var daily=brainDailyXpGet();
    var pct=Math.min(100, milestone+daily);
    var canLevelUp=pct>=100;
    return {level:claimed, claimed:claimed, next:claimed+1, canLevelUp:canLevelUp, pct:pct, milestone:milestone, daily:daily,
            full:false, exReady:brainExReady(), signals:s,
            // shims: el modelo viejo (reqs/missing/nextAction/earned) ya no aplica.
            earned:claimed, reqs:[], missing:[], nextAction:null};
  }
  /* B3 · momento de recompensa: si el nivel SUBIÓ desde la última foto (entrenar voz,
     seguir competidor, crear guion, vincular publicados…), toast + orbe en pulso +
     la barra del hero se re-anima de 0 → pct. Llamar tras cada recarga de señales. */
  function brainLevelPulse(){
    brainEmitSignals();   // capa "alimentar": lanza partículas reales por cada señal que subió
    // Subir es MANUAL. Avisamos cuando la barra LLEGA al 100% (canLevelUp pasa a true).
    var can=brainLevel().canLevelUp;
    if(S._canLvlSeen==null){ S._canLvlSeen=can; return; }
    if(can && !S._canLvlSeen){
      showToast("¡Tu Cerebro está al 100%! Súbelo de nivel arriba ↑", "Subir de nivel", "brain-levelup");
      try{ if(window.RSBrain) window.RSBrain.levelup(); }catch(e2){}
      var orb=document.querySelector(".brain-orb"); if(orb){ orb.classList.add("lvlup"); setTimeout(function(){ orb.classList.remove("lvlup"); },1600); }
    }
    S._canLvlSeen=can;
  }
  /* Subida de nivel MANUAL: la barra del nivel llegó al 100% → el user pulsa
     «¡Subir de nivel!», RECLAMA el nivel, la barra se REINICIA a 0 para el siguiente
     y se reproduce la animación temática del nivel reclamado. */
  function brainLevelup(){
    var bl=brainLevel();
    if(!bl.canLevelUp) return;
    var to=bl.claimed+1;          // nivel que reclamas (1..5)
    brainClaimedSet(to);
    brainDailyXpSet(0);           // reinicia la barra del ejercicio diario para el siguiente nivel
    S._canLvlSeen=false;
    S.levelup={to:to, name:ecoLevelName(to)};
    S._ceReward=null;
    render();
    try{ if(window.RSBrain) window.RSBrain.levelup(); }catch(e){}
    try{ if(S._bnInst) S._bnInst.levelUp(); }catch(e){}   // la red neuronal evoluciona (destello + onda + crece)
    // David 26-jun: +1 crédito GRATIS la 1ª vez que subes a cada nivel (2..5) → 4 en total.
    // Recompensa que SE SIENTE (y deja claro que NO es «subir de plan»). Anti-farm en el backend.
    if(to>=2 && to<=5){
      if(isDemo()){
        if(S.levelup) S.levelup.credit=1; render();
        showToast(L("🎁 +1 crédito gratis por subir de nivel","🎁 +1 free credit for leveling up"));
      } else {
        apiPost('/api/brain/levelup-reward',{level:to}).then(function(r){
          if(r && r.ok && r.d && r.d.granted>0){
            if(S.levelup) S.levelup.credit=r.d.granted;
            render();
            refreshCredits().then(function(){ try{ flashSpark(r.d.granted); }catch(e){} });
            showToast(L("🎁 +"+r.d.granted+" crédito gratis por subir a "+ecoLevelName(to),"🎁 +"+r.d.granted+" free credit for reaching "+ecoLevelName(to)));
          }
        });
      }
    }
  }
  function closeLevelup(){ S.levelup=null; render(); }
  // Tema por nivel (color + subtítulo) — animación "una por nivel" (improvisada).
  // Cada nivel destino tiñe el orbe/botón/chispas y cuenta qué desbloquea.
  function levelupTheme(to){
    return ({
      1:{a:"#94a3b8", sub:L("Tu Cerebro despierta: ya conoce tu nicho y a quién mirar. A partir de aquí, cada día lo haces más tuyo.","Your Brain wakes up: it knows your niche and who to watch. From here, every day makes it more yours.")},
      2:{a:"#6f93ff", sub:L("Ya imitas los patrones que funcionan en tu nicho: tus guiones salen con la estructura de lo que peta.","You now mirror what works in your niche: your scripts come out with proven structure.")},
      3:{a:"#8b5cf6", sub:L("Robas guiones que petan y los haces TUYOS — menos retoques, más tu voz.","You steal scripts that pop and make them YOURS — fewer tweaks, more your voice.")},
      4:{a:"#12a37c", sub:L("Estratega: el Cerebro distingue lo que va a explotar y te lo prioriza.","Strategist: the Brain spots what'll explode and prioritizes it for you.")},
      5:{a:"#f59e0b", sub:L("Nivel Viral: tu Cerebro clava tu tono. Ahora juegas para viralizar.","Viral level: your Brain nails your tone. Now you play to go viral.")}
    })[to] || {a:"#6f93ff", sub:L("Tu Cerebro sube de nivel: clava mejor tu tono y necesitas menos retoques.","Your Brain levels up: it nails your tone better with fewer tweaks.")};
  }
  // hex → rgba (para el tinte translúcido del acento por nivel).
  function _hexA(hex,a){ var n=parseInt(String(hex).slice(1),16); return "rgba("+(n>>16&255)+","+(n>>8&255)+","+(n&255)+","+a+")"; }
  // FX temático POR NIVEL (de Claude design): N2 plantillas que se alinean, N3 guion
  // robado/absorbido, N4 línea de tendencia con pico, N5 ondas de choque.
  function levelupFx(to, color){
    if(to===2){ var s=""; for(var i=0;i<6;i++){ s+='<div class="rsx-tmpl" style="--a:'+(i*60)+'deg;animation-delay:'+(i*0.09).toFixed(2)+'s"></div>'; } return s; }
    if(to===3){ return '<div class="rsx-steal"><div class="rsx-steal__t">GUION · 1.2M VIEWS</div><div class="rsx-steal__l" style="width:90%"></div><div class="rsx-steal__l" style="width:70%"></div><div class="rsx-steal__l" style="width:80%"></div></div>'; }
    if(to===4){
      var g=_hexA(color,.35), g0=_hexA(color,0);
      return '<svg class="rsx-radar" viewBox="0 0 300 160" aria-hidden="true">'+
        '<defs><linearGradient id="rsxTg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+g+'"/><stop offset="1" stop-color="'+g0+'"/></linearGradient></defs>'+
        '<polyline points="0,120 50,110 95,118 140,70 175,30 200,96 250,80 300,108" fill="none" stroke="'+color+'" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="600" stroke-dashoffset="600"><animate attributeName="stroke-dashoffset" from="600" to="0" dur="1.2s" begin="0.2s" fill="freeze"/></polyline>'+
        '<polygon points="0,120 50,110 95,118 140,70 175,30 200,96 250,80 300,108 300,160 0,160" fill="url(#rsxTg)" opacity="0"><animate attributeName="opacity" from="0" to="1" dur="0.6s" begin="1s" fill="freeze"/></polygon></svg>'+
        '<div class="rsx-spike" style="left:175px;top:30px;margin-left:-150px;margin-top:-80px"></div>';
    }
    if(to===5){ var s2=""; for(var j=0;j<4;j++){ s2+='<div class="rsx-shock" style="animation-delay:'+(j*0.22).toFixed(2)+'s"></div>'; } return s2; }
    return "";
  }
  function levelupHTML(){
    var lu=S.levelup; if(!lu) return "";
    var th=levelupTheme(lu.to); var accent=th.a;
    // FX (anillos + partículas comunes + efecto del nivel) se computan UNA vez y se
    // cachean en S.levelup._fx → un re-render incidental no reinicia la animación.
    if(lu._fx==null){
      var fx="";
      for(var r=0;r<3;r++){ fx+='<div class="rsx-ring" style="animation-delay:'+(r*0.28).toFixed(2)+'s"></div>'; }
      for(var p=0;p<22;p++){
        var ang=Math.random()*6.283, dist=90+Math.random()*150;
        fx+='<span class="rsx-particle" style="--tx:'+Math.round(Math.cos(ang)*dist)+'px;--ty:'+Math.round(Math.sin(ang)*dist)+'px;--d:'+(1+Math.random()*0.9).toFixed(2)+'s;animation-delay:'+(Math.random()*0.3).toFixed(2)+'s;width:'+(5+Math.round(Math.random()*6))+'px;height:'+(5+Math.round(Math.random()*6))+'px"></span>';
      }
      fx+=levelupFx(lu.to, accent);
      lu._fx=fx;
    }
    return '<div class="rsx-overlay" role="dialog" aria-modal="true" aria-label="'+L("Subida de nivel del Cerebro","Brain level up")+'" style="--rsx-accent:'+accent+';--rsx-accent-soft:'+_hexA(accent,.32)+'">'+
      '<div class="rsx-fx">'+lu._fx+'</div>'+
      '<div class="rsx-stage">'+
        '<div class="rsx-orb"><div class="rsx-orb__core">'+IC.brain+'</div><div class="rsx-orb__badge">'+lu.to+'</div></div>'+
        '<p class="rsx-stage__eyebrow">'+L("Cerebro · Nivel ","Brain · Level ")+lu.to+'</p>'+
        '<h2 class="rsx-stage__title">'+ESC(lu.name)+'</h2>'+
        '<p class="rsx-stage__sub">'+th.sub+'</p>'+
        (lu.credit?'<div class="rsx-stage__reward" style="margin:4px auto 2px;display:inline-flex;align-items:center;gap:7px;padding:9px 15px;border-radius:999px;background:color-mix(in srgb,var(--rsx-accent) 20%,transparent);border:1px solid var(--rsx-accent);color:#fff;font-family:var(--font-mono);font-size:13.5px;font-weight:800">'+IC.spark+' +'+lu.credit+' '+L("crédito"+(lu.credit>1?"s":"")+" gratis","free credit"+(lu.credit>1?"s":""))+'</div>':'')+
        '<button class="rsx-stage__btn" data-act="levelup-done">'+L("Continuar","Continue")+'</button>'+
      '</div>'+
    '</div>';
  }
  function brainVoice(b){
    // v0.19: perfil de voz REAL (GET /api/voice) si existe; si no, demo del nicho.
    var vp=S.voice;
    if(vp && vp.has_profile){
      return {
        tono: vp.tone||"",
        frases: (vp.phrases||[]).map(function(p){ return String(p).replace(/^[\s"'“”]+|[\s"'“”]+$/g,''); }),
        estructura: vp.structure||"",
        duracion: vp.avg_duration ? ("~"+vp.avg_duration+"s objetivo") : "",
        evita: vp.avoid||""
      };
    }
    if(b && b.voice_profile) return b.voice_profile;
    if(!isDemo()){
      // Prod sin perfil aún: no mostrar voz ajena — invitar a enseñarla.
      return { tono:"Aún no conozco tu voz. Enséñamela arriba ↑", frases:[], estructura:"—", duracion:"—", evita:"—" };
    }
    return {
      tono:"Directo y sin postureo. Cuentas las cosas como a un colega en un audio de WhatsApp.",
      frases:['te lo cuento porque a mí…','paso uno… paso dos…','guárdate esto','y no, no es lo que crees'],
      estructura:"Hook directo (sin 'hola') → 3 pasos concretos → CTA de guardar/comentar.",
      duracion:"30–45s es tu punto dulce: ahí retienes el doble.",
      evita:"Nada de 'en el panorama actual', 'es fundamental', ni motivacional vacío."
    };
  }
  // Tarjeta de CAPTURA del moat: el creador pega 1-2 reels suyos → aprendemos su voz.
  /* A1: selector de TONO preset. Define la personalidad del guion cuando aún no
     hay voz personal entrenada. Si ya hay voz, esta MANDA (el tono queda de base);
     se indica para no confundir. Persiste en /api/voice/tone (default de generación). */
  function toneSelectorHTML(){
    var tones=(S.user.presetTones&&S.user.presetTones.length)?S.user.presetTones:[
      {key:"viral",label:"Polémico/Viral"},{key:"educacional",label:"Educacional"},
      {key:"divertido",label:"Cercano/Divertido"},{key:"informativo",label:"Informativo"},
      {key:"storytelling",label:"Storytelling"}];
    var cur=S.user.presetTone||"viral";
    var chips=tones.map(function(t){
      return '<button class="tone-chip'+(t.key===cur?" on":"")+'" data-act="set-tone" data-k="'+ESC(t.key)+'" aria-pressed="'+(t.key===cur?"true":"false")+'">'+ESC(t.label)+'</button>';
    }).join("");
    var note=hasRealVoice()
      ? 'Tu voz entrenada manda en cada guion; el tono es la base por si refrescas la voz.'
      : 'Tu próximo «Roba la idea» saldrá con este tono — con carácter, no genérico. Entrena tu voz abajo para que suene a ti.';
    return '<div class="brain-section-t">Tu tono</div>'+
      '<div class="tone-pick"><div class="tone-chips">'+chips+'</div>'+
      '<p class="tone-note">'+note+'</p></div>';
  }
  function voiceCaptureHTML(){
    // Voz AUTO: si el user tiene reels publicados (métricas), la vía destacada
    // es derivarla de ellos — sin pegar nada. El coste (transcripciones que
    // falten) se enseña ANTES de lanzar (preflight en voiceAutoDerive). El
    // pegado manual queda como alternativa secundaria.
    var nPub=(S.metrics&&S.metrics.videos)?S.metrics.videos.length:0;
    var auto = nPub>0
      ? '<div class="voice-auto"><div class="va-text"><b>Tienes '+nPub+' reels publicados.</b> Puedo leerlos y derivar tu voz de ahí — sin pegar nada.</div>'+
          // IDI una-primaria: la primaria del Cerebro es la CTA de nivel del hero;
          // esta queda secundaria (sigue destacada por su caja .voice-auto).
          '<button class="btn btn-md btn-secondary" data-act="voice-auto">'+IC.brain+' Derivar mi voz de mis reels</button></div>'
      : '<button class="btn btn-sm btn-ghost" data-act="voice-auto" style="margin-bottom:12px">'+IC.brain+' Derivar de mis reels publicados</button>';
    return '<div class="brain-section-t">Enséñame tu voz</div>'+
      '<div class="voice-capture">'+
        auto+
        '<p class="vc-lead">'+(nPub>0?'O pega':'Pega')+' las <b>URLs de 1-5 reels TUYOS</b> (Instagram/TikTok) y yo los transcribo y aprendo tu voz. '+
          'Tu próximo «Roba la idea» saldrá sonando a ti, no genérico.</p>'+
        '<textarea class="vc-ta" id="rsVoiceUrls" rows="4" placeholder="https://www.instagram.com/reel/…&#10;https://www.tiktok.com/@tu/video/…"></textarea>'+
        '<button class="btn btn-md '+(nPub>0?'btn-secondary':'btn-primary')+'" data-act="voice-from-urls">'+IC.spark+' Aprender mi voz de estos reels</button>'+
        '<p class="vc-hint">Transcribir cada reel cuesta 1 crédito — te lo confirmo antes. Nada de pegar texto a mano.</p>'+
      '</div>';
  }
  // A3: entrenar voz pegando URLs de reels propios. Confirma el coste ANTES.
  function voiceFromUrls(){
    var ta=document.getElementById("rsVoiceUrls"); var blob=ta?ta.value:"";
    var urls=(blob.match(/https?:\/\/\S+/g)||[]).filter(function(u){ return /instagram\.com|tiktok\.com/.test(u); });
    var reels=urls.filter(function(u){ return /\/reel\/|\/reels\/|\/p\/|\/tv\/|\/video\/|vm\.tiktok|vt\.tiktok/.test(u); });
    if(!reels.length){ return showError("Pega URLs de reels concretos tuyos (no el perfil). O conecta tu Instagram en Métricas."); }
    var n=Math.min(reels.length,6);
    if(isDemo()){
      S.voice={ has_profile:true, tone:"Directo, sin postureo.", phrases:["te lo cuento porque","paso uno… paso dos"], structure:"hook → pasos → cierre", avg_duration:38, avoid:"tecnicismos", confidence:62, source_count:n, evidence:["abres directo","frases cortas","cierras pidiendo guardar"] };
      render(); showToast("Voz aprendida de "+n+" reels (demo) — te conozco al 62%."); setTimeout(brainLevelPulse,1600); return;
    }
    var go=function(){
      showToast("Transcribiendo tus reels y aprendiendo tu voz… (~1 min)");
      apiPost("/api/voice/from-urls",{urls:reels.slice(0,6)}).then(function(r){
        if(r.ok && r.d && r.d.ok){
          return fetch("/api/voice",{credentials:"same-origin"}).then(function(x){return x.json();}).then(function(v){
            S.voice=v; render(); showToast("Voz aprendida de "+(r.d.source_count||n)+" reels — te conozco al "+(r.d.confidence||v.confidence||0)+"%.");
            setTimeout(brainLevelPulse,1600);
          });
        }
        showError((r.d&&r.d.message)||(r.d&&r.d.error)||"No pude aprender tu voz con esos reels.");
      });
    };
    if(typeof window.confirmModal==="function"){
      window.confirmModal({ title:"Aprender tu voz", body:"Voy a transcribir "+n+" reel"+(n===1?"":"s")+" tuyo"+(n===1?"":"s")+" — cuesta "+n+" crédito"+(n===1?"":"s")+". Con eso aprendo tu voz.", confirmText:"Sí, aprender mi voz", cancelText:"Ahora no" }).then(function(ok){ if(ok) go(); });
    } else go();
  }
  /* B6 + T1 (IDI): onboarding de voz como 2º punto de entrada — en el Dashboard
     es un BANNER delgado (no una card con CTA primario): no compite con la
     Oportunidad #1 ni la empuja bajo el fold. El CTA (secundario) lleva al
     Cerebro, donde vive el formulario completo (voiceCaptureHTML). Si ya hay
     perfil de voz, no renderiza nada. */
  function voiceOnboardCardHTML(){
    // Sin voz aún → el banner clásico de voz (copy probado: es siempre el primer paso).
    if(!hasRealVoice()){
      return '<div class="voice-banner">'+
        '<span class="vb-ic">'+IC.mic+'</span>'+
        '<span class="vb-text"><b>Enséñame tu voz</b> — pega 1-2 reels tuyos y tu próximo «Roba la idea» saldrá sonando a ti, no genérico.</span>'+
        '<button class="btn btn-sm btn-secondary" data-act="voice-focus">Enseñar mi voz</button>'+
      '</div>';
    }
    // B2: con voz entrenada, el banner se generaliza a la SIGUIENTE acción de nivel
    // (una sola, la de más impacto). Al nivel máximo desaparece — nada que empujar.
    var lv=brainLevel();
    // Barra al 100% pendiente de recoger → banner de acción (subida manual).
    if(lv.canLevelUp){
      return '<div class="voice-banner vb-up">'+
        '<span class="vb-ic">'+IC.brain+'</span>'+
        '<span class="vb-text"><b>¡Nivel '+lv.next+' listo!</b> Tu Cerebro está al 100% — recógelo.</span>'+
        '<button class="btn btn-sm btn-primary" data-act="brain-levelup">¡Subir de nivel!</button>'+
      '</div>';
    }
    if(lv.full) return '';   // nivel máximo: nada que empujar
    // En curso: nudge de progreso (% hacia el siguiente nivel) + alimentar si toca.
    var nm=ESC(ecoLevelName(lv.next));
    var feedBtn=lv.exReady
      ? '<button class="btn btn-sm btn-primary" data-act="tab" data-k="brain">'+IC.bolt+' '+L("Alimentar +"+BRAIN_DAILY_GAIN+"%","Feed +"+BRAIN_DAILY_GAIN+"%")+'</button>'
      : '';
    return '<div class="voice-banner">'+
      '<span class="vb-ic">'+IC.brain+'</span>'+
      '<span class="vb-text"><b>'+lv.pct+'%</b> '+L("hacia","toward")+' <b>'+nm+'</b> — '+(lv.exReady
          ? L("aliméntalo hoy para +"+BRAIN_DAILY_GAIN+"%","feed it today for +"+BRAIN_DAILY_GAIN+"%")
          : L("vuelve en "+brainExNextHrs()+"h para +"+BRAIN_DAILY_GAIN+"%","back in "+brainExNextHrs()+"h for +"+BRAIN_DAILY_GAIN+"%"))+'.</span>'+
      feedBtn+
    '</div>';
  }
  function voiceEvidenceHTML(){
    var ev=(S.voice&&S.voice.evidence)||[];
    if(!ev.length) return '';
    return '<div class="brain-section-t">Lo que he aprendido de ti <span class="brain-tag">de '+(S.voice.source_count||0)+' reels tuyos</span></div>'+
      '<div class="learn" style="margin-bottom:14px"><div class="learn-list">'+
        ev.map(function(e){ return '<div class="learn-item">'+IC.check+'<span>'+ESC(e)+'</span></div>'; }).join("")+
      '</div></div>'+
      // Refinar: acumula más reels tuyos → sube confianza (POST /api/voice/refine).
      '<div class="voice-refine" style="margin-bottom:18px"><button class="btn btn-sm btn-secondary" data-act="voice-refine">'+IC.spark+' Refinar mi voz</button><span class="vr-hint" style="margin-left:10px;color:var(--text-tertiary);font-size:12.5px">Pega más URLs de tus reels y subo el % de voz.</span></div>';
  }
  function brainCompetitors(){
    var by={}; (S.reels||[]).forEach(function(r){ var h=r.creator&&r.creator.handle; if(!h) return; by[h]=(by[h]||0)+1; });
    return Object.keys(by).map(function(h){ return {handle:h, n:by[h]}; }).sort(function(a,b){return b.n-a.n;});
  }
  // Fathom 17/06: mini-galería de miniaturas de los reels de un competidor (de S.reels).
  function compThumbsHTML(handle){
    var hs=(S.reels||[]).filter(function(r){ return r.creator && r.creator.handle===handle && r.thumb; }).slice(0,4);
    if(hs.length){
      return '<div class="comp-thumbs">'+hs.map(function(r){ return '<span class="comp-thumb"><img src="'+ESC(r.thumb)+'" alt="" loading="lazy">'+(r.dur?'<span class="ct-dur">'+ESC(r.dur)+'</span>':'')+'</span>'; }).join("")+'</div>';
    }
    // Sin reels de ESTE competidor en el radar (demo o aún sin scrape) → placeholders
    // con gradiente para que el layout se vea. En prod saldrían las miniaturas reales.
    if(isDemo()){
      var grad=['linear-gradient(135deg,#ff6a3d,#ff2d55)','linear-gradient(135deg,#5b8cff,#7b3dff)','linear-gradient(135deg,#1dd3b0,#0e9f87)','linear-gradient(135deg,#ffb648,#ff7a00)'];
      var out='';
      for(var i=0;i<4;i++){ out+='<span class="comp-thumb" style="background:'+grad[_lbHash(handle+i,0,grad.length)]+'"><span class="ct-ph">▶</span></span>'; }
      return '<div class="comp-thumbs">'+out+'</div>';
    }
    return '';
  }
  /* ── LEADERBOARD (Fathom 17/06) — tú vs tus competidores por seguidores, con "qué
     te falta para subir". Arquetipo Killer (Bartle) + SDT-competencia. En demo los
     seguidores se siembran deterministas por handle; en prod saldrían de métricas. */
  function _lbHash(s,min,max){ var h=2166136261; for(var i=0;i<s.length;i++){ h=((h^s.charCodeAt(i))>>>0)*16777619>>>0; } return min+(h%(max-min)); }
  function _fmtK(n){ n=Math.round(n); return n>=1000000?((n/1000000).toFixed(1).replace(/\.0$/,'')+'M'):n>=1000?((n/1000).toFixed(1).replace(/\.0$/,'')+'K'):String(n); }
  // Real (no demo): {you, rows[], metric:"avg_views"} cargado por loadLeaderboard().
  function lbReal(){ return (!isDemo() && S._lbReal && Array.isArray(S._lbReal.rows)) ? S._lbReal : null; }
  function lbIsViews(){ return !!lbReal(); }            // métrica = views/reel (no seguidores)
  function lbUnit(){ return lbIsViews()? L("views/reel","views/reel") : L("seguidores","followers"); }
  function leaderboardRows(){
    var lr=lbReal();
    if(lr){
      // PROD: el número del ranking son las views MEDIAS por reel (lo que scrapeamos).
      // Lo guardo en `.followers` para reusar todo el render; el label sale de lbUnit().
      var mr=lr.you||{};
      var me={handle:(mr.handle||brand().handle||S.user.handle||"tu_cuenta"), followers:(mr.avg_views||0),
              best:(mr.best_views||0), growth:(mr.growth||0), reels:(mr.reels||0), hasData:!!mr.has_data, you:true};
      var comps=(lr.rows||[]).map(function(c){
        return {handle:c.handle, followers:(c.avg_views||0), best:(c.best_views||0),
                growth:(c.growth||0), reels:(c.reels||0), you:false};
      });
      return comps.concat([me]).sort(function(a,b){ return b.followers-a.followers; });
    }
    var b=brand();
    var me={handle:(b.handle||S.user.handle||"tu_cuenta"), followers:42000, growth:8, you:true};
    var comps=brainCompetitors().slice(0,12).map(function(c){
      return {handle:c.handle, followers:_lbHash(c.handle,8000,180000), growth:_lbHash(c.handle+"g",0,28)-9, you:false};
    });
    return comps.concat([me]).sort(function(a,b){ return b.followers-a.followers; });
  }
  /* VERSUS / retos (Fathom 18/06, la idea que más le gustó a David): reta a un rival
     del nicho, 7 días a ver quién hace más vistas. El marcador sale de las MÉTRICAS
     de reels que ya scrapeáis (en demo, deterministas). Premio = créditos / pool.
     En prod: tabla `challenges` + comparar las vistas reales de la semana. */
  function versusHintHTML(){
    if(S.versus) return '';
    return '<div class="versus-hint">'+IC.spark+' '+L("Ponte un objetivo","Set a goal")+' — '+
      L("supera la <b>media de views</b> de un rival con tus reels. Pulsa <b>Supéralo</b> en cualquiera","beat a rival's <b>average views</b> with your reels. Hit <b>Beat it</b> on anyone")+'</div>';
  }
  /* "Supéralo" (no reto mutuo): los competidores son cuentas de Instagram que NO usan
     la app, así que no se les puede retar. Es un OBJETIVO personal — superar su media
     de views con tus reels. Una cara, con datos que ya tienes. */
  function versusCardHTML(){
    if(!S.versus) return '';
    var v=S.versus, beat=v.mine>=v.oppAvg, pct=Math.min(100,Math.round(v.mine/(v.oppAvg||1)*100)), gap=Math.max(0,v.oppAvg-v.mine);
    return '<div class="versus-card">'+
      '<div class="versus-head"><span class="versus-ico">'+IC.bolt+'</span><b>'+L("Tu objetivo","Your goal")+'</b>'+
        '<span class="versus-day">'+L("supera a","beat")+' @'+ESC(v.opp)+'</span>'+
        '<button class="versus-quit" data-act="versus-quit" aria-label="'+L("Quitar objetivo","Remove goal")+'">'+IC.x+'</button></div>'+
      '<div class="versus-vs">'+
        '<div class="versus-side"><span class="ava bava">'+ESC(initialsOf(v.youHandle))+'</span><span class="vs-h">'+L("Tu mejor reel","Your best reel")+'</span><span class="vs-n">'+_fmtK(v.mine)+'</span></div>'+
        '<span class="versus-mid '+(beat?'win':'lose')+'">'+(beat?'✓ '+L("lo superas","you beat it"):L("a "+_fmtK(gap)+" de superarlo","− "+_fmtK(gap)+" to go"))+'</span>'+
        '<div class="versus-side them"><span class="ava bava">'+ESC(initialsOf(v.opp))+'</span><span class="vs-h">'+L("media de","avg of")+' @'+ESC(v.opp)+'</span><span class="vs-n">'+_fmtK(v.oppAvg)+'</span></div>'+
      '</div>'+
      '<div class="versus-bar"><i style="width:'+pct+'%"></i></div>'+
      '<div class="versus-foot"><span class="versus-metric">'+L("Views medias por reel","Avg views per reel")+'</span>'+
        (beat
          ? '<span class="versus-prize">'+_icTrophy+' '+L("¡Lo superas! Mantén el ritmo","You beat it! Keep it up")+'</span>'
          : '<button class="btn btn-sm btn-primary" data-act="tab" data-k="dashboard">'+IC.bolt+' '+L("Roba y publica más","Steal & publish more")+'</button>')+'</div>'+
    '</div>';
  }
  // PÁGINA COMPLETA del leaderboard (tab "leaderboard"). Tu posición vs competidores.
  function leaderboardPageHTML(){
    var b=brand();
    var rows=leaderboardRows();
    var myIdx=-1; rows.forEach(function(r,i){ if(r.you) myIdx=i; });
    var me=rows[myIdx]||{followers:0,growth:0};
    var above=myIdx>0?rows[myIdx-1]:null;
    var gap=above?(above.followers-me.followers):0;
    var unit=lbUnit(), realV=lbIsViews();
    // #1 proyección: a TU ritmo, cuándo superas al de arriba → meta cercana y tangible.
    var proj='';
    if(above && me.growth>0){
      var perMonth=me.followers*me.growth/100;
      var weeks=Math.max(1,Math.round((gap/Math.max(1,perMonth))*4.345));
      proj=' · '+L("a tu ritmo lo superas en ~<b>"+weeks+" semana"+(weeks===1?"":"s")+"</b>","at your pace you pass them in ~<b>"+weeks+" week"+(weeks===1?"":"s")+"</b>");
    }
    var goal;
    if(realV && !me.hasData){
      goal='<div class="lb-goal">'+IC.ig+' '+L("Conecta tu Instagram para ver tu posición y tu objetivo.","Connect your Instagram to see your rank and goal.")+'</div>';
    } else if(realV && !above && rows.length<=1){
      goal='<div class="lb-goal">'+IC.spark+' '+L("Sigue a competidores y deja que scrapeemos sus reels para ver tu ranking.","Follow competitors and let us scrape their reels to see your ranking.")+'</div>';
    } else {
      goal=above
        ? '<div class="lb-goal">'+IC.spark+' '+L("Te faltan <b>"+_fmtK(gap)+"</b> "+unit+" para superar a <b>@"+ESC(above.handle)+"</b>","<b>"+_fmtK(gap)+"</b> "+unit+" to overtake <b>@"+ESC(above.handle)+"</b>")+proj+'</div>'
        : '<div class="lb-goal">'+_icTrophy+' '+L("Lideras tu nicho — sigue así","You lead your niche — keep it up")+'</div>';
    }
    // #3 momentum: racha de crecimiento (refuerzo positivo). Real: % de tus reels
    // recientes (sin semanas inventadas). Demo: semanas deterministas.
    var momentum='';
    if(me.growth>0){
      if(realV){
        momentum='<div class="lb-momentum">'+IC.chart+' '+L("Subiendo · <b>+"+me.growth+"%</b> en tus reels recientes","Rising · <b>+"+me.growth+"%</b> on your recent reels")+'</div>';
      } else {
        var streakW=2+_lbHash((b.handle||"x")+"s",0,4);
        momentum='<div class="lb-momentum">'+IC.chart+' '+L("Subiendo · <b>+"+me.growth+"%</b> este mes · llevas <b>"+streakW+" semanas</b> creciendo","Rising · <b>+"+me.growth+"%</b> this month · <b>"+streakW+" weeks</b> growing")+'</div>';
      }
    }
    // Insignia de nivel del Cerebro SOLO en TU fila (los competidores son cuentas de
    // IG, no usuarios → gris). Tu nivel sale de brainLevel() (localStorage), client-side.
    var myLvl=brainLevel(); var myLvlN=myLvl.level||0; var myCol=brainLevelColor(myLvlN);
    var items=rows.map(function(r,i){
      var g=r.growth, gtxt=(g>=0?'↑':'↓')+Math.abs(g)+'%';
      var ava=r.you
        ? '<span class="rk-ava rk-ava-lvl" style="--lc:'+myCol+'">'+ESC(initialsOf(r.handle))+'</span>'
        : '<span class="rk-ava rk-ava-guest">'+ESC(initialsOf(r.handle))+'</span>';
      var lvlBadge=(r.you && myLvlN>=1)
        ? ' <span class="rk-lvl" style="--lc:'+myCol+'" title="'+L("Tu nivel del Cerebro","Your Brain level")+'">'+ESC(ecoLevelName(myLvlN))+'</span>'
        : '';
      return '<div class="rk-row'+(r.you?' me':'')+'"'+(r.you?' style="--lc:'+myCol+'"':'')+'>'+
        '<span class="rk-pos'+(i<3?' top t'+(i+1):'')+'">'+(i+1)+'</span>'+
        ava+
        '<span class="rk-h">@'+ESC(r.handle)+(r.you?' <span class="rk-you">'+L("tú","you")+'</span>':'')+lvlBadge+'</span>'+
        '<span class="rk-val">'+_fmtK(r.followers)+' <span class="rk-unit">'+ESC(unit)+'</span></span>'+
        '<span class="rk-g '+(g>=0?'up':'down')+'">'+gtxt+'</span>'+
        ((!r.you && !S.versus)?'<button class="btn btn-sm btn-secondary rk-beat" data-act="versus-start" data-id="'+ESC(r.handle)+'">'+IC.bolt+' '+L("Supéralo","Beat it")+'</button>':'<span class="rk-beat-sp"></span>')+
      '</div>';
    }).join("");
    var myRank=myIdx+1;
    return '<div class="scroll"><div class="canvas rk-canvas">'+
      // cabecera v3
      '<header class="rk-head"><div class="rk-head-l">'+
        '<div class="rk-eyebrow"><span class="rk-eye-dot"></span>'+L("tu posición en el nicho","your spot in the niche")+'</div>'+
        '<h1 class="rk-h1">'+L("Ranking","Ranking")+'</h1>'+
        '<p class="rk-sub">'+L("Tu posición frente a tus competidores. Sube de puesto creando y publicando más.","Where you stand vs your competitors. Climb by creating and publishing more.")+'</p></div>'+
        '<div class="rk-sync"><span class="rk-sync-dot"></span>@'+ESC(b.handle||S.user.handle||"tu_cuenta")+'</div></header>'+
      // hero: tu posición
      '<div class="rk-hero">'+
        '<div class="rk-rank"><span class="rk-rank-hash">#</span><span class="rk-rank-n">'+(myRank>0?myRank:"–")+'</span><span class="rk-rank-of">'+L("de "+rows.length,"of "+rows.length)+'</span></div>'+
        '<div class="rk-hero-body">'+
          '<div class="rk-hero-val">'+_fmtK(me.followers||0)+' <span class="rk-hero-unit">'+ESC(unit)+'</span></div>'+
          goal+momentum+
        '</div>'+
      '</div>'+
      versusCardHTML()+
      versusHintHTML()+
      '<div class="rk-list">'+items+'</div>'+
      '<div class="rk-cta">'+
        '<button class="btn btn-md btn-primary" data-act="tab" data-k="dashboard">'+IC.bolt+' '+L("Roba y publica más para subir","Steal & publish more to climb")+'</button>'+
        '<button class="btn btn-md btn-secondary" data-act="add-comp">'+IC.plus+' '+L("Añadir competidor","Add a competitor")+'</button>'+
      '</div>'+
      '<p class="rk-note">'+(lbIsViews()
        ? L("El ranking va por <b>views medias por reel</b> (lo que de verdad scrapeamos). Conecta tu Instagram para ver tu posición.","Ranking is by <b>average views per reel</b> (what we actually scrape). Connect your Instagram to see your spot.")
        : L("Las cifras de competidores son estimaciones del nicho; tus métricas reales salen al conectar Instagram.","Competitor figures are niche estimates; your real metrics appear once you connect Instagram."))+'</p>'+
      communitySoonHTML()+
    '</div></div>';
  }
  /* Teaser "Próximamente" de las features de comunidad (entre USUARIOS de la app):
     genera expectativa + "Avísame" capta interés (PostHog) para medir demanda. */
  function communitySoonHTML(){
    var interested=!!S.user.communityWaitlist; if(!interested){ try{ interested=localStorage.getItem("rs_community_interest")==="1"; }catch(e){} }
    var card=function(ico,title,desc){
      return '<div class="soon-card"><div class="soon-ico">'+ico+'</div>'+
        '<div class="soon-body"><div class="soon-h">'+title+' <span class="soon-pill">'+L("Próximamente","Soon")+'</span></div>'+
        '<div class="soon-desc">'+desc+'</div></div></div>';
    };
    var cta=interested
      ? '<div class="soon-cta done">'+IC.check+' '+L("Te avisaremos en cuanto llegue","We'll let you know when it lands")+'</div>'
      : '<div class="soon-cta"><button class="btn btn-sm btn-secondary" data-act="community-interest">'+IC.spark+' '+L("Avísame cuando llegue","Notify me when it's live")+'</button></div>';
    return '<div class="sec-soon-t">'+L("Comunidad de creadores","Creator community")+' <span class="brain-tag">'+L("en camino","on the way")+'</span></div>'+
      '<div class="soon-grid">'+
        card(_icGift, L("Pushea tus reels a tu nicho","Push your reels to your niche"),
             L("Comparte tu mejor reel con creadores de tu nicho — y recibe los suyos como <b>regalo</b>, ya analizados para que los robes.","Share your best reel with creators in your niche — and get theirs as a <b>gift</b>, already analyzed to steal."))+
        card(_icUsers, L("Grupos de creadores","Creator groups"),
             L("Únete a un grupo de tu nicho: <b>comparte métricas</b> y mira en directo qué le está funcionando a los demás.","Join a niche group: <b>share metrics</b> and see live what's working for everyone else."))+
      '</div>'+cta;
  }
  // T3: lista REAL de competidores seguidos (con id de tracking → permite dejar de
  // seguir). Se carga aparte del feed; al resolver, repinta las vistas que la
  // muestran (Cerebro y Radar — trackedManageHTML) si están abiertas.
  function loadTracked(){
    // P0 aislamiento: el backend YA filtra por project_id — sin el parámetro
    // traía los competidores de TODAS las marcas. Marca "default" → sin filtro.
    var _pid=_pidOf(S.brandId);
    apiGet("/api/tracked-creators"+(_pid?("?project_id="+encodeURIComponent(_pid)):"")).then(function(r){
      if(r.ok && r.d && Array.isArray(r.d.tracked)){
        S.tracked=r.d.tracked;
        // Contador "X / límite del plan" (ítem 4): usa el uso/límite POR MARCA del backend.
        if(r.d.usage){ S.trackedCount=(r.d.usage.per_brand_used!=null?r.d.usage.per_brand_used:S.tracked.length); S.trackedLimit=r.d.usage.per_brand_limit; }
        if(S.tab==="brain"||S.tab==="dashboard") bgRender(); brainLevelPulse();
      }
    });
  }

  /* ── FIX2 · Radar "del tirón": añadir competidor DESDE el Radar (inline, sin la
     chrome legacy) → auto-análisis async (scrape+transcribe) con estado "analizando
     tu nicho…" no bloqueante; cuando el scrape termina, sus reels entran en el radar.
     Más el botón "Actualizar radar" (refresca a demanda, reusa caché). ── */
  function addCompInlineHTML(){
    // SIEMPRE en el DOM (oculto con .hidden si está cerrado) → toggleAddComp lo muestra
    // in-place sin re-render, así no hay salto de scroll al abrirlo.
    return '<div class="comp-add'+(S.addCompOpen?'':' hidden')+'" id="rsCompAdd"><span class="comp-add-at">@</span>'+
      '<input id="rsCompAddInput" class="comp-add-input" type="text" autocapitalize="none" autocomplete="off" spellcheck="false" placeholder="'+L("usuario de Instagram y Enter","Instagram handle, then Enter")+'" aria-label="'+L("Añadir competidor","Add competitor")+'">'+
      '<button class="btn btn-sm btn-primary" data-act="comp-add-submit">'+IC.bolt+' '+L("Analizar","Analyze")+'</button>'+
      '<button class="comp-add-x" data-act="add-comp" aria-label="'+L("Cerrar","Close")+'">'+IC.x+'</button>'+
    '</div>';
  }
  function analyzingBannerHTML(){
    var hs=Object.keys(S.analyzing||{}); if(!hs.length) return '';
    return '<div class="analyzing-banner"><span class="analyzing-spin"></span>'+
      '<span class="analyzing-txt"><b>'+L("Analizando tu nicho","Analyzing your niche")+'</b> — '+hs.map(function(h){return '@'+ESC(h);}).join(", ")+
      '. '+L("Sus reels entrarán en el radar en cuanto termine.","Their reels hit the radar as soon as it's done.")+'</span></div>';
  }
  function toggleAddComp(){
    S.addCompOpen=!S.addCompOpen;
    // Reorganización 04/07: el input vive dentro del details «Gestiona competidores» —
    // si el CTA vino de fuera (DNS, seed banner, Cerebro), ábrelo para que se vea.
    var det=document.getElementById("rsManageComp");
    if(det && S.addCompOpen) det.setAttribute("open","");
    var box=document.getElementById("rsCompAdd");
    if(!box){ return render(); }   // aún no montado (p.ej. radar vacío recién entrado) → render normal
    // Toggle in-place (sin re-render → SIN salto de scroll). Marca el botón y enfoca.
    box.classList.toggle("hidden", !S.addCompOpen);
    var _el=root(); var btn=_el&&_el.querySelector('[data-act="add-comp"]'); if(btn) btn.classList.toggle("on", S.addCompOpen);
    if(S.addCompOpen){ var i=document.getElementById("rsCompAddInput"); if(i){ try{ i.focus({preventScroll:true}); }catch(e){ i.focus(); } } }
  }
  function submitAddComp(){ var i=document.getElementById("rsCompAddInput"); addCompetitorFromRadar(i?i.value:""); }
  function addCompetitorFromRadar(handle){
    handle=(handle||"").trim().replace(/^@+/,"").toLowerCase();
    if(!/^[a-zA-Z0-9._]{1,30}$/.test(handle)){ return showError(L("Pon un @usuario de Instagram válido.","Enter a valid Instagram @handle.")); }
    S.addCompOpen=false;
    if(isDemo()){   // demo: simula el auto-análisis no bloqueante
      S.analyzing=S.analyzing||{}; S.analyzing[handle]=true; render();
      showToast(L("Analizando a @"+handle+"…","Analyzing @"+handle+"…"));
      setTimeout(function(){ if(S.analyzing) delete S.analyzing[handle]; if(typeof applyDemoBrand==="function") applyDemoBrand(); render(); showToast(L("@"+handle+" ya está en tu radar.","@"+handle+" is now in your radar.")); },2600);
      return;
    }
    var body={ ig_username:handle, source:"radar" };
    var _pid=_pidOf(S.brandId); if(_pid) body.project_id=_pid;   // B5: aísla por marca activa (agency + estudio)
    showToast(L("Añadiendo a @"+handle+"…","Adding @"+handle+"…"));
    apiPost("/api/tracked-creators", body).then(function(r){
      if(!r.ok){
        if(r.status===402||(r.d&&r.d.error==="no_credits")) return showPaywall("no_credits");
        var _e=(r.d&&r.d.error)||"";
        if(_e==="tc.error.already_tracking"){ return showToast(L("Ya seguías a @"+handle+".","Already following @"+handle+".")); }
        if(_e==="tc.error.plan_limit_reached"){ return showPaywall("tracked_creators"); }
        // Mensajes claros para los errores que antes caían a un genérico (ítem 2):
        if(_e==="tc.error.project_required"){ return showError(L("Elige una marca antes de añadir un competidor.","Pick a brand before adding a competitor.")); }
        if(_e==="tc.error.project_limit_reached"){ return showError(L("Tope de competidores de ESTA marca alcanzado ("+((r.d&&r.d.limit)||"")+"). Sube de plan o usa otra marca.","This brand's competitor cap reached. Upgrade or use another brand.")); }
        if(_e==="tc.error.invalid_username"){ return showError(L("Ese @usuario de Instagram no es válido.","That Instagram @handle isn't valid.")); }
        return showError((r.d&&r.d.message)||L("No pude añadir a @"+handle+". Reinténtalo.","Couldn't add @"+handle+". Try again."));
      }
      S.analyzing=S.analyzing||{}; S.analyzing[handle]=true;
      loadTracked(); render();
      showToast(L("Analizando a @"+handle+" — sus reels entrarán en el radar en un momento.","Analyzing @"+handle+" — its reels will hit your radar shortly."));
      pollAnalyzing(handle,0);
    });
  }
  function pollAnalyzing(handle,tries){
    if(tries>20){ if(S.analyzing) delete S.analyzing[handle]; render(); return; }
    setTimeout(function(){
      var _pid=_pidOf(S.brandId);
      apiGet("/api/tracked-creators"+(_pid?("?project_id="+encodeURIComponent(_pid)):"")).then(function(r){
        var done=false;
        if(r.ok&&r.d&&Array.isArray(r.d.tracked)){
          S.tracked=r.d.tracked;
          var c=r.d.tracked.filter(function(t){ return ((t.creator&&t.creator.ig_username)||t.ig_username)===handle; })[0];
          var st=c&&c.creator&&c.creator.scrape_status;
          if(st==="ok"||st==="failed"||st==="private"||st==="not_found") done=true;
        }
        if(done){ if(S.analyzing) delete S.analyzing[handle]; loadBrandData(); }
        else { pollAnalyzing(handle,tries+1); }   // el spinner es CSS → sin re-render por tick
      });
    }, 3000);
  }
  // Refresco manual de PAGO (v1): ÚNICA vía de scrape on-demand. Cuesta créditos y tiene
  // cooldown (429) por usuario+marca. El re-rank diario sigue siendo gratis y automático.
  // Desenlace del refresh-now de PAGO: pollea /task/refresh/<id> hasta que finalize resuelve.
  // new_count>0 → «X nuevos» + repinta feed+sugerencias+posibles-competidores (loadBrandData).
  // new_count==0 → «no hay nada nuevo, vuelve mañana» + refleja el REEMBOLSO (refreshCredits).
  // Cierra el bug de David: ni cobra por vacío ni deja la UI stale sin recargar.
  function _pollRefreshOutcome(tid, tries){
    tries=tries||0;
    apiGet("/task/refresh/"+encodeURIComponent(tid)).then(function(r){
      var d=(r&&r.d)||{};
      if(d.state==="success"){
        try{ refreshCredits(); }catch(e){}   // reembolso (nada nuevo) o cobro real (hubo nuevos)
        if((d.new_count||0)>0){
          showToast(L("Radar actualizado · "+d.new_count+" nuevo"+(d.new_count>1?"s":""),
                      "Radar updated · "+d.new_count+" new"));
        } else {
          showToast(L("No hay nada nuevo aún. Vuelve mañana — el radar se renueva solo.",
                      "Nothing new yet. Come back tomorrow — the radar refreshes on its own."));
        }
        loadBrandData();   // repinta feed + «Sugerencias de hoy» + «Posibles competidores»
        return;
      }
      if(d.state==="error"){ loadBrandData(); return; }
      if(tries<20){ setTimeout(function(){ _pollRefreshOutcome(tid, tries+1); }, 3000); }
      else { loadBrandData(); }   // timeout de seguridad (~60s)
    });
  }
  function refreshRadar(){
    if(isDemo()){
      // Contrato David (harness): un refresh que NO trae nada nuevo → mensaje honesto y SIN
      // cobro. Forzable con ?refresh=empty para pinnearlo en verify-island (el resto = normal).
      if(/[?&]refresh=empty/.test(location.search)){
        return showToast(L("No hay nada nuevo aún. Vuelve mañana — el radar se renueva solo.",
                           "Nothing new yet. Come back tomorrow — the radar refreshes on its own."));
      }
      if(typeof applyDemoBrand==="function") applyDemoBrand(); render();
      return showToast(L("Radar actualizado.","Radar refreshed."));
    }
    var _pid=_pidOf(S.brandId);
    var go=function(){
      showToast(L("Trayendo lo nuevo de tus competidores…","Pulling what's new from your competitors…"));
      apiPost("/api/radar/refresh-now", _pid?{project_id:_pid}:{}).then(function(r){
        if(r.status===429){ return showError((r.d&&r.d.message)||L("Acabas de refrescar. Prueba más tarde.","You just refreshed. Try again later.")); }
        if(r.status===402){
          showError((r.d&&r.d.message)||L("Necesitas créditos para refrescar ahora.","You need credits to refresh now."));
          try{ if(typeof window.openUpgradeModal==="function") window.openUpgradeModal("credits"); }catch(e){}
          return;
        }
        if(!r.ok){ return showError((r.d&&r.d.message)||L("No pude refrescar el radar.","Couldn't refresh the radar.")); }
        var n=(r.d&&r.d.queued)||0;
        var tid=(r.d&&r.d.refresh_task_id)||null;
        showToast((r.d&&r.d.message)||L("Trayendo lo nuevo…","Pulling what's new…"));
        if(n){ try{ refreshCredits(); }catch(e){} }
        // n==0 (sin competidores / ya al día): el backend NO cobró → mensaje del server, sin poll.
        if(tid && n){ _pollRefreshOutcome(tid); }
        else { setTimeout(loadBrandData, 300); }   // fallback: nada encolado o sin task id
      });
    };
    // Confirm de coste (idiom confirmModal). ~REFRESH_NOW_UNITS créditos (server-side).
    if(typeof window.confirmModal==="function"){
      window.confirmModal({
        title:L("Refrescar ahora","Refresh now"),
        body:L("Traigo lo último de tus competidores en vivo (scrape). Cuesta ~5 créditos. El radar se renueva solo cada día gratis.",
               "I pull your competitors' latest live (scrape). Costs ~5 credits. The radar auto-refreshes daily for free."),
        confirmText:L("Sí, refrescar","Yes, refresh"), cancelText:L("Ahora no","Not now")
      }).then(function(ok){ if(ok) go(); });
    } else { go(); }
  }
  // #2 (David): «Refrescar sugerencias» — scrapea el POOL DE NICHO (no tus competidores) para
  // traer reels nuevos al carrusel. Guardarraíl en backend (refreshable=false → no cobra). Reusa
  // el poll del desenlace (reembolso on-empty). Distinto de refreshRadar (competidores/feed).
  function refreshPool(){
    if(isDemo()){
      // Demo/harness: forzar el caso «no refrescable» → mensaje honesto SIN cobro con ?pool=norefresh.
      return showToast(L("No hay reels nuevos en tu nicho ahora. Vuelve mañana.","Nothing new in your niche right now. Come back tomorrow."));
    }
    var _pid=_pidOf(S.brandId);
    var go=function(){
      apiPost("/api/radar/refresh-pool", _pid?{project_id:_pid}:{}).then(function(r){
        if(r.status===429){ return showError((r.d&&r.d.message)||L("Acabas de refrescar. Prueba más tarde.","You just refreshed. Try again later.")); }
        if(r.status===402){
          showError((r.d&&r.d.message)||L("Necesitas créditos para refrescar.","You need credits to refresh."));
          try{ if(typeof window.openUpgradeModal==="function") window.openUpgradeModal("credits"); }catch(e){}
          return;
        }
        if(!r.ok){ return showError((r.d&&r.d.message)||L("No pude refrescar las sugerencias.","Couldn't refresh suggestions.")); }
        // Guardarraíl backend: refreshable=false → NO cobró → mensaje honesto, sin poll.
        if(r.d && r.d.refreshable===false){ return showToast((r.d.message)||L("No hay nada nuevo en tu nicho ahora. Vuelve mañana.","Nothing new in your niche right now. Come back tomorrow.")); }
        var tid=(r.d&&r.d.refresh_task_id)||null, n=(r.d&&r.d.queued)||0;
        showToast((r.d&&r.d.message)||L("Buscando reels nuevos en tu nicho…","Looking for new reels in your niche…"));
        if(n){ try{ refreshCredits(); }catch(e){} }
        if(tid && n){ _pollRefreshOutcome(tid); }
      });
    };
    if(typeof window.confirmModal==="function"){
      window.confirmModal({
        title:L("Refrescar sugerencias","Refresh suggestions"),
        body:L("Traigo lo último de tu NICHO en vivo (scrape del pool de sugerencias). Cuesta ~5 créditos. Si no hay nada nuevo, se te devuelven.",
               "I pull the latest from your NICHE live (suggestions pool scrape). Costs ~5 credits. If nothing's new, you get them back."),
        confirmText:L("Sí, refrescar","Yes, refresh"), cancelText:L("Ahora no","Not now")
      }).then(function(ok){ if(ok) go(); });
    } else { go(); }
  }
  /* ── Cerebro 3D (WebGL, lazy) ──────────────────────────────────────────────
     Three.js (vendado) + brain3d.js se cargan SOLO al abrir Cerebro. Si no es
     elegible (reduced-motion, sin WebGL, headless del harness, o fallo de carga)
     se queda el orb estático de fallback. El canvas es un singleton que se re-ancla
     en cada render (ensureBrain3D) → sobrevive a los innerHTML de la isla. */
  var _brain3dState=0;   // 0 idle · 1 cargando · 2 listo · 3 no-disponible
  function brain3dEligible(){
    if(window.__RS_NO_3D__) return false;
    var force = window.__RS_FORCE_3D__ || /[?&]brain3d=(force|on)/.test(location.search);
    if(!force){
      // Headless (harness verify-island) y reduced-motion → fallback estático (sin WebGL).
      if(/headless/i.test(navigator.userAgent||"") || navigator.webdriver) return false;
      try{ if(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return false; }catch(e){}
    }
    try{ var c=document.createElement('canvas'); if(!(c.getContext('webgl')||c.getContext('experimental-webgl'))) return false; }catch(e){ return false; }
    return true;
  }
  function loadScriptOnce(src, cb){
    var ex=document.querySelector('script[data-rs="'+src+'"]');
    if(ex){ if(ex.getAttribute('data-loaded')) return cb(true); ex.addEventListener('load',function(){cb(true);}); ex.addEventListener('error',function(){cb(false);}); return; }
    var s=document.createElement('script'); s.src=src; s.async=true; s.setAttribute('data-rs',src);
    s.onload=function(){ s.setAttribute('data-loaded','1'); cb(true); };
    s.onerror=function(){ cb(false); };
    document.head.appendChild(s);
  }
  function currentBrainState(){
    var b=brand();
    var voicePct = hasRealVoice() ? Math.max(0,Math.min(100,S.voice.confidence||0)) : (isDemo()?Math.max(6,Math.min(100,b.voice||40)):0);
    return { knowledge: voicePct };
  }
  // Capa "alimentar al cerebro": cuando una partícula REAL aterriza (no ambiente),
  // el motor llama aquí → toast flotante "+1 [tipo]" sobre el stage + bump del contador
  // de la fuente correspondiente. type ∈ reel|comp|guio|metr.
  var BRAIN_FEED_LBL={ reel:L("voz","voice"), comp:L("competidor","competitor"), guio:L("guion","script"), metr:L("métrica","metric") };
  var BRAIN_SRC_IDX={ reel:0, comp:1, guio:2, metr:3 };   // orden de .brain-src en brainHTML
  function onBrainAbsorb(type){
    var stage=document.getElementById('rsBrainStage'); if(!stage) return;
    var pop=document.createElement('div'); pop.className='brain-feed-pop bf-'+type;
    pop.textContent='+1 '+(BRAIN_FEED_LBL[type]||L("señal","signal"));
    stage.appendChild(pop);
    setTimeout(function(){ if(pop.parentNode) pop.parentNode.removeChild(pop); }, 1400);
    var idx=BRAIN_SRC_IDX[type], nodes=document.querySelectorAll('.brain-src-n');
    if(idx!=null && nodes[idx]){ var n=nodes[idx]; n.classList.remove('bump'); void n.offsetWidth; n.classList.add('bump'); }
  }
  /* Emite partículas reales al cerebro comparando las señales actuales con la última
     foto (S._sigSeen). La foto SIEMPRE avanza (aunque no estés en Cerebro) para no
     acumular un aluvión de partículas al abrir la pestaña luego; el disparo visual
     solo ocurre si estás en Cerebro con el 3D montado. Llamar tras eventos reales. */
  function brainEmitSignals(){
    var s=brainSignals();
    var prev=S._sigSeen;
    S._sigSeen={voice:s.voice, comps:s.comps, guiones:s.guiones, pub:s.pub};
    if(!prev) return;                                  // primera foto: sin disparo
    if(S.tab!=="brain" || !window.RSBrain) return;
    var fired=[];
    if(s.guiones>prev.guiones) fired.push(['guio', s.guiones-prev.guiones]);
    if(s.comps  >prev.comps)   fired.push(['comp', s.comps-prev.comps]);
    if(s.pub    >prev.pub)     fired.push(['metr', s.pub-prev.pub]);
    if(s.voice  >prev.voice)   fired.push(['reel', 1]);   // voz: 1 partícula simbólica
    fired.forEach(function(f){
      var k=Math.min(f[1],4);
      for(var i=0;i<k;i++) (function(d){ setTimeout(function(){ try{ window.RSBrain.feed(f[0]); }catch(e){} }, d*130); })(i);
    });
  }
  function ensureBrain3D(){
    var stage=document.getElementById('rsBrainStage'); if(!stage) return;
    // Durante el house tour NO montamos el 3D: su build bloquea el hilo y lagea la
    // transición a Cerebro. Queda el orb; se monta al abrir Cerebro de verdad (post-tour).
    var ov=document.querySelector('.tour-overlay'); if(ov && getComputedStyle(ov).display!=='none') return;
    // v3: el orbe 3D vive OCULTO (el anillo del Cerebro es CSS/SVG; el stage está
    // display:none). Si el stage no es visible (oculto o sin área), NO cargamos
    // Three.js ni montamos el loop WebGL: rendería a un canvas invisible y satura la
    // GPU/CPU sin beneficio (causa del "Cerebro pillado"). Si ya corría, lo paramos.
    if(stage.offsetParent===null || stage.clientWidth===0 || stage.clientHeight===0){ pauseBrain3D(); return; }
    if(!brain3dEligible()){ _brain3dState=3; return; }
    if(_brain3dState===2){ if(window.RSBrain){ window.RSBrain.mount(stage, currentBrainState()); window.RSBrain.update(currentBrainState()); window.RSBrain.setOnAbsorb(onBrainAbsorb); } if(S._sigSeen==null) brainEmitSignals(); return; }
    if(_brain3dState===1) return;
    _brain3dState=1;
    loadScriptOnce('/static/js/vendor/three.min.js', function(ok){
      if(!ok){ _brain3dState=3; return; }
      loadScriptOnce('/static/js/brain3d.js', function(ok2){
        if(!ok2 || !window.RSBrain){ _brain3dState=3; return; }
        _brain3dState=2;
        var st=document.getElementById('rsBrainStage');
        if(st && S.tab==="brain"){ window.RSBrain.mount(st, currentBrainState()); window.RSBrain.setOnAbsorb(onBrainAbsorb); }
        if(S._sigSeen==null) brainEmitSignals();   // fija la línea base al abrir Cerebro
      });
    });
  }
  function pauseBrain3D(){ try{ if(window.RSBrain) window.RSBrain.pause(); }catch(e){} }

  // v4 · BrainNet (red neuronal viva, canvas 2D). El canvas es PERSISTENTE (vive en
  // S._bnCanvas y se RE-INSERTA en #ceStage en cada render) para que NO se reinicie
  // al alimentar (que llama a render()). La instancia sobrevive entre renders.
  function ensureBrainNet(){
    if(typeof window==="undefined" || !window.BrainNet) return;
    var stage=document.getElementById('ceStage'); if(!stage) return;
    if(!S._bnCanvas){ S._bnCanvas=document.createElement('canvas'); S._bnCanvas.className='ce2-canvas'; }
    if(S._bnCanvas.parentNode!==stage){
      try{ stage.appendChild(S._bnCanvas); }catch(e){ return; }
      if(S._bnInst){ try{ S._bnInst._resize(); }catch(e){} }
    }
    if(!S._bnInst){
      try{ S._bnInst=new window.BrainNet(S._bnCanvas,{brand:"#4f7cff"}); }catch(e){ S._bnInst=null; }
    }
    if(S._bnInst){
      var lv=brainLevel();
      var vp=hasRealVoice()?(S.voice.confidence||0):(isDemo()?((brand()||{}).voice||40):0);
      try{ S._bnInst.setLevel(lv.level||1); S._bnInst.update({progress:(lv.pct||0)/100, voice:vp/100}); }catch(e){}
    }
  }
  // Tipo de material → color/canal de partículas de la BrainNet.
  function _bnFeedType(k){ return k==="prefer"?"estilo":(k||"guion"); }

  /* ── Brain «Entrenar» (lever de INVERSIÓN, Fathom): valora hooks 👍/👎 → afina tu
     gusto Y alimenta el cerebro 3D (partícula al votar). Demo local; prod backend. */
  function ensureBrainTrain(){
    if(!S.brainTrain) S.brainTrain={cards:[], i:0, rated:0, loading:false};
    var bt=S.brainTrain;
    if(!bt.cards.length && !bt.loading) brainTrainLoad();
  }
  // Fathom 17/06: modo del día — un día se entrenan HOOKS, otro GUIONES. Override manual.
  /* % del Cerebro = progreso gamificado (Fathom 18/06): el tutorial deja 35% y cada
     día subes +3-6% completando EL ejercicio del día (1 al día, alterna hooks/guiones,
     no puedes hacer más). Demo: localStorage. Real: S.user.brainProgress del backend
     (+ POST /api/brain/exercise-done) — pendiente de migración. */
  function _todayStr(){ return new Date().toISOString().slice(0,10); }
  // El % del Cerebro ES la barra del nivel actual (hitos + ejercicio diario).
  function brainProgress(){ return brainLevel().pct; }
  // El ejercicio diario ya se hizo = el CD de 24h aún no está listo.
  function brainExDoneToday(){ return !brainExReady(); }
  function brainLastGain(){ return BRAIN_DAILY_GAIN; }
  // Completa el ejercicio del día → +5% a la barra del nivel (respeta el CD de 24h).
  function _brainCompleteExercise(){
    if(!brainAddDailyXp()) return;   // en CD → no suma
    // PROD: además entrena un poco la VOZ (señal real que alimenta los hitos).
    if(!isDemo() && S.voice && S.voice.has_profile){ S.voice.confidence=Math.min(92,(S.voice.confidence||0)+2); }
    if(!isDemo()){ try{ apiPost('/api/brain/exercise-done',{}); }catch(e){} }
    try{ if(window.RSBrain) window.RSBrain.levelup(); }catch(e){}
    brainLevelPulse();   // por si la barra llegó al 100%
    showToast(L("+"+BRAIN_DAILY_GAIN+"% al Cerebro · vuelve mañana para subir más.","+"+BRAIN_DAILY_GAIN+"% to your Brain · come back tomorrow for more."));
  }
  function brainTrainMode(){
    // El DÍA decide (alterna hooks/guiones); el usuario no elige (1 ejercicio/día).
    return (new Date().getDate()%2===0)?'hooks':'guiones';
  }
  function setBrainTrainMode(m){
    if(!S.brainTrain) S.brainTrain={cards:[], i:0, rated:0, loading:false};
    if(S.brainTrain.mode===m) return;
    S.brainTrain.mode=m; S.brainTrain.improving=false;
    brainTrainLoad(); render();
  }
  function brainTrainLoad(){
    var bt=S.brainTrain||(S.brainTrain={cards:[], i:0, rated:0, loading:false});
    bt.loading=true; bt.i=0; bt.cards=[]; bt.improving=false;
    var mode=brainTrainMode();
    var done=function(cards){ bt.cards=cards||[]; bt.i=0; bt.loading=false; if(S.tab==="brain") render(); };
    if(isDemo()){ setTimeout(function(){ done(brainDemoCards(mode)); }, 900); return; }
    var niche=(S.onb&&S.onb.niche)||"";
    apiGet('/api/brain/training-cards?type='+mode+(niche?('&niche='+encodeURIComponent(niche)):'')).then(function(r){
      done((r.ok&&r.d&&Array.isArray(r.d.cards))?r.d.cards:[]);
    }).catch(function(){ done([]); });
  }
  function brainRate(rating){
    var bt=S.brainTrain; if(!bt||!bt.cards.length||bt.i>=bt.cards.length) return;
    // 👎 "No es mío" → en vez de pasar, ofrecemos decir CÓMO lo dirías (Fathom: David).
    if(!rating){ bt.improving=true; render(); return; }
    _brainCommitVote(1, "");
  }
  // Envía el voto (+ sugerencia opcional) y avanza a la siguiente tarjeta.
  function _brainCommitVote(rating, suggestion){
    var bt=S.brainTrain; if(!bt||bt.i>=bt.cards.length) return;
    var c=bt.cards[bt.i]; bt.rated=(bt.rated||0)+1; bt.i++; bt.improving=false;
    // voto = SOLO VISUAL para el cerebro 3D (no infla el % real); feed silencioso.
    try{ if(window.RSBrain) window.RSBrain.feed(rating?'guio':'comp', true); }catch(e){}
    if(!isDemo()){ var niche=(S.onb&&S.onb.niche)||""; try{ apiPost('/api/brain/rate',{text:c.text, kind:c.kind||brainTrainMode(), type:brainTrainMode(), rating:rating, suggestion:suggestion||"", niche:niche}); }catch(e){} }
    if(bt.i>=bt.cards.length){ _brainCompleteExercise(); }   // completó el ejercicio del día → +3-6%
    render();
  }
  function brainImprove(send){
    var bt=S.brainTrain; if(!bt) return;
    var sug="";
    if(send){ var ta=document.getElementById("rsBtSuggest"); sug=ta?ta.value.trim():""; }
    _brainCommitVote(0, sug);
  }
  function brainTrainMore(){ brainTrainLoad(); render(); }
  function brainDemoCards(mode){
    var n=(S.onb&&S.onb.niche)||(brand().name)||"tu nicho";
    if(mode==='guiones'){
      return [
        {kind:"gancho + giro", text:"Pensaba que "+n+" era cuestión de talento. Hasta que descubrí esto."},
        {kind:"lista",         text:"3 cosas de "+n+" que ojalá me hubieran dicho antes de empezar."},
        {kind:"historia",      text:"Llevaba meses estancado en "+n+". Cambié una sola cosa y se movió todo."},
        {kind:"contraste",     text:"Lo que crees que funciona en "+n+" vs lo que de verdad funciona."},
        {kind:"reto",          text:"Hazlo 7 días en "+n+" y nota el cambio. Te lo cuento paso a paso."},
        {kind:"error caro",    text:"Este error en "+n+" me costó meses. Para que no lo repitas."}
      ];
    }
    return [
      {kind:"polémico",       text:"Lo que nadie te dice sobre "+n+" (y por qué te están mintiendo)."},
      {kind:"error",          text:"El error de "+n+" que comete el 90% — y te frena sin que lo notes."},
      {kind:"resultado",      text:"Hice esto en "+n+" 30 días seguidos. Esto es lo que pasó."},
      {kind:"curiosidad",     text:"Nadie habla de este truco de "+n+". Hasta hoy."},
      {kind:"promesa",        text:"Domina "+n+" en 3 pasos, aunque empieces de cero."},
      {kind:"contraintuitivo",text:"Deja de hacer esto en "+n+": te cuesta más de lo que crees."}
    ];
  }
  /* #2/#7 ALIMENTAR EL CEREBRO (Duolingo/dopamina): card prominente con input para
     que el usuario cuente cosas de sí mismo → aprende. Feedback visual FUERTE (lluvia
     de datos al cerebro 3D + pulso de crecimiento) + «gracias, voy aprendiendo». */
  function brainFeedMeHTML(){
    var sel=S.feedType||"guion"; var def=brainFeedDef(sel); var ready=brainFeedReady(sel);
    var DOT={guion:"#f5a83d",hook:"#6f93ff",muletilla:"#a68cfa",prefer:"#33d499"};
    // Chips: cada FORMA de alimentar con su dot de color (= material que traga el cerebro), su % y su estado.
    var chips=BRAIN_FEED_TYPES.map(function(t){
      var done=!brainFeedReady(t.key); var c=DOT[t.key]||"#6f93ff";
      return '<button class="feed-chip'+(t.key===sel?' on':'')+(done?' done':'')+'" data-act="brain-feed-pick" data-k="'+t.key+'" title="'+ESC(t.label)+'">'+
        (done?IC.check:'<span class="fc-dot" style="background:'+c+';box-shadow:0 0 7px '+c+'"></span>')+ESC(t.label)+' <b>+'+t.gain+'%</b></button>';
    }).join("");
    var cta = ready
      ? '<button class="btn btn-lg btn-primary feedme-cta" style="width:auto;margin-top:0" data-act="brain-feed-me" data-k="'+sel+'">'+IC.bolt+' '+L("Alimentar · +"+def.gain+"%","Feed · +"+def.gain+"%")+'</button>'
      : '<button class="btn btn-lg btn-secondary feedme-cta" style="width:auto;margin-top:0" disabled>'+IC.check+' '+L("Hecho hoy · vuelve en "+brainFeedNextHrs(sel)+"h","Done today · back in "+brainFeedNextHrs(sel)+"h")+'</button>';
    var reward = S._ceReward ? '<span class="ce2-reward">'+IC.bolt+' '+ESC(S._ceReward)+'</span>' : '';
    return '<div class="feedme">'+
      '<div class="feedme-body">'+
        '<span class="feedme-eyebrow">'+IC.bolt+' '+L("ALIMENTA TU CEREBRO","FEED YOUR BRAIN")+' <span class="brain-tag">'+L("cada forma suma distinto · 1/día c/u","each way adds differently · 1/day each")+'</span></span>'+
        '<div class="feedme-t">'+L("Dame material tuyo y subo de nivel","Give me your material and I level up")+'</div>'+
        '<div class="feedme-d">'+L("Cuanto más concreto y MÁS TUYO, mejor clavo tu voz. Lo que más suma: <b>tus guiones que petaron</b>.","The more specific and YOURS, the better I nail your voice. What adds most: <b>your scripts that worked</b>.")+'</div>'+
        '<div class="feed-chips">'+chips+'</div>'+
        '<textarea id="rsFeedMe" class="feedme-input" rows="2" maxlength="600"'+(ready?'':' disabled')+' placeholder="'+ESC(def.ph)+'"></textarea>'+
        '<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:13px">'+cta+reward+'</div>'+
      '</div>'+
    '</div>';
  }
  // Lluvia de datos al cerebro 3D + pulso de crecimiento (dopamina).
  function brainFeast(n){
    n=n||6;
    try{
      if(window.RSBrain){
        for(var i=0;i<n;i++){ (function(d){ setTimeout(function(){ try{ window.RSBrain.feed(d%2?'guio':'comp'); }catch(e){} }, d*85); })(i); }
        setTimeout(function(){ try{ window.RSBrain.levelup(); }catch(e){} }, n*85+140);
      }
    }catch(e){}
    var orb=document.querySelector(".brain-orb"); if(orb){ orb.classList.remove("feast"); void orb.offsetWidth; orb.classList.add("feast"); setTimeout(function(){ orb.classList.remove("feast"); }, 1200); }
  }
  function brainFeedPick(key){ S.feedType=key; S._ceReward=null; render(); var ta=document.getElementById("rsFeedMe"); if(ta) try{ ta.focus(); }catch(e){} }
  function brainFeedMe(key){
    key=key||S.feedType||"guion"; var def=brainFeedDef(key);
    var ta=document.getElementById("rsFeedMe"); var note=ta?ta.value.trim():"";
    if(!note){ if(ta) ta.focus(); return; }
    if(!brainFeedReady(key)){ return showToast(L("Eso ya lo alimentaste hoy · vuelve en "+brainFeedNextHrs(key)+"h.","Already fed that today · back in "+brainFeedNextHrs(key)+"h.")); }
    brainFeast(def.key==="guion"?12:9);   // dopamina: lluvia + pulso (más para guiones)
    if(S.voice && S.voice.has_profile){ S.voice.confidence=Math.min(92,(S.voice.confidence||0)+(def.key==="guion"?3:1)); }
    // Reusa /api/brain/rate (btype mapea a guion/hook, los valores que el backend acepta).
    if(!isDemo()){ try{ apiPost('/api/brain/rate',{text:note, kind:def.btype, type:def.btype, rating:1, suggestion:note, source:"feed_"+def.key, niche:(S.onb&&S.onb.niche)||""}); }catch(e){} }
    brainAddXp(def.gain); _bfTsSet(def.key);   // +gain% a la barra del nivel · CD 24h de ESTE tipo
    if(ta) ta.value="";
    S._ceReward=L("+"+def.gain+"% · voy aprendiendo tu voz","+"+def.gain+"% · learning your voice")+" 🧠";   // reward inline animado en la card
    // Auto-avanza al SIGUIENTE ejercicio no hecho, en orden (guion→hook→muletilla→
    // estilo), para que el cliente encadene los 4 sin elegir chip a mano. Si ya están
    // todos hechos hoy, se queda en el actual (CTA «hecho hoy»).
    var _ci=-1; for(var _i=0;_i<BRAIN_FEED_TYPES.length;_i++){ if(BRAIN_FEED_TYPES[_i].key===key){ _ci=_i; break; } }
    for(var _j=1;_j<=BRAIN_FEED_TYPES.length;_j++){ var _nt=BRAIN_FEED_TYPES[(_ci+_j)%BRAIN_FEED_TYPES.length]; if(_nt.key!==key && brainFeedReady(_nt.key)){ S.feedType=_nt.key; break; } }
    brainLevelPulse();   // por si llegó al 100% (NO auto-sube: el nivel se reclama con el botón de arriba)
    render();
    try{ if(S._bnInst) S._bnInst.feed(_bnFeedType(def.key)); }catch(e){}   // partículas tipadas → la red neuronal las absorbe
    var _ta2=document.getElementById("rsFeedMe"); if(_ta2) try{ _ta2.focus(); }catch(e){}   // listo para el siguiente ejercicio
    showToast(L("+"+def.gain+"% al Cerebro · "+def.label.toLowerCase()+" guardado 🧠","+"+def.gain+"% to your Brain · saved 🧠"));
  }
  function brainTrainHTML(){
    var mode=brainTrainMode();
    var modeLbl=mode==='hooks'?L("hooks","hooks"):L("guiones","scripts");
    var head='<div class="brain-section-t">'+L("Tu ejercicio de hoy: entrena tus "+modeLbl,"Today's exercise: train your "+modeLbl)+
      ' <span class="brain-tag">'+L("+"+BRAIN_DAILY_GAIN+"% al Cerebro · 1 al día","+"+BRAIN_DAILY_GAIN+"% to your Brain · 1 a day")+'</span></div>';
    // Ejercicio diario YA hecho → bloqueado hasta mañana (no se puede hacer más).
    if(brainExDoneToday()){
      return head+'<div class="bt-wrap"><div class="bt-done bt-locked">'+IC.check+' '+
        L("Ejercicio completado · <b>+"+brainLastGain()+"%</b> al Cerebro. Vuelve mañana para el siguiente.",
          "Done · <b>+"+brainLastGain()+"%</b> to your Brain. Come back tomorrow for the next one.")+'</div></div>';
    }
    var bt=S.brainTrain, inner;
    if(!bt || (bt.loading && !bt.cards.length)){
      inner='<div class="bt-load"><span class="mini-spin"></span> '+L("Preparando tu ejercicio…","Preparing your exercise…")+'</div>';
    } else if(!bt.cards.length){
      inner='<div class="bt-load">'+L("No pude traer tu ejercicio ahora.","Couldn't load your exercise now.")+' <button class="btn btn-sm btn-ghost" data-act="brain-train-more">'+L("Reintentar","Retry")+'</button></div>';
    } else {
      var c=bt.cards[bt.i];
      var actions = bt.improving
        ? '<div class="bt-improve"><textarea id="rsBtSuggest" class="bt-suggest" rows="2" placeholder="'+L("¿Cómo lo dirías TÚ? (opcional)","How would YOU say it? (optional)")+'"></textarea>'+
            '<div class="bt-row"><button class="bt-btn bt-skip" data-act="brain-improve" data-k="skip">'+L("Saltar","Skip")+'</button>'+
            '<button class="bt-btn bt-yes" data-act="brain-improve" data-k="send">'+IC.arr+' '+L("Enviar y siguiente","Send & next")+'</button></div></div>'
        : '<div class="bt-row"><button class="bt-btn bt-no" data-act="brain-rate" data-k="0" aria-label="No es mío">'+IC.x+' '+L("No es mío","Not me")+'</button>'+
            '<button class="bt-btn bt-yes" data-act="brain-rate" data-k="1" aria-label="Suena a mí">'+IC.check+' '+L("Suena a mí","Sounds like me")+'</button></div>';
      inner='<div class="bt-card">'+
        '<div class="bt-kind">'+ESC(c.kind||mode)+'</div>'+
        '<p class="bt-text">'+ESC(c.text)+'</p>'+
        actions+
        '<div class="bt-prog">'+(bt.i+1)+' / '+bt.cards.length+'</div>'+
      '</div>';
    }
    return head+'<div class="bt-wrap">'+inner+'</div>';
  }
  /* TINDER DE GUIONES (Fathom 18/06, lo pidió David): una baraja con TUS guiones
     ya generados. Deslizas «esto soy yo» / «no es mío» → refina tu voz (mismo
     /api/brain/rate) Y entrena el Cerebro. Completar la baraja cuenta como el
     ejercicio del día (sube el % 1×/día, respetando el tope). Distinto del
     brain-train (que valora hooks del nicho); este va sobre lo que TÚ creaste. */
  function guionTinderPool(){
    return (S.guiones||[]).filter(function(g){ return g && g.status!=="discarded" && (g.hook||(g.beats&&g.beats.length)); });
  }
  function guionTinderEligible(){ return guionTinderPool().length>=3; }
  function ensureGuionTinder(){ if(!S.guionTinder) S.guionTinder={cards:[], i:0, started:false}; }
  function startGuionTinder(){
    var pool=guionTinderPool().slice(0,8).map(function(g){
      return {id:g.id, title:g.title||"", hook:(g.hook||((g.beats&&g.beats[0])||"")), beat:((g.beats&&g.beats[0])||"")};
    });
    S.guionTinder={cards:pool, i:0, started:true};
    render();
  }
  function guionTinderVote(rating){
    ensureGuionTinder();
    var gt=S.guionTinder; if(!gt.started||gt.i>=gt.cards.length) return;
    var c=gt.cards[gt.i]; gt.i++;
    try{ if(window.RSBrain) window.RSBrain.feed(rating?'guio':'comp', true); }catch(e){}   // feed visual al cerebro 3D
    if(!isDemo()){ try{ apiPost('/api/brain/rate',{text:(c.hook||c.title), kind:'guion', type:'guiones', rating:rating, suggestion:'', source:'tinder_guiones', niche:(S.onb&&S.onb.niche)||''}); }catch(e){} }
    if(gt.i>=gt.cards.length){
      // Completó la baraja → cuenta como el ejercicio del día (sube el Cerebro, 1/día).
      if(!brainExDoneToday()) _brainCompleteExercise();
      else showToast(L("Baraja completada. El Cerebro ya subió hoy — vuelve mañana.","Deck done. Your Brain already leveled today — come back tomorrow."));
    }
    render();
  }
  function guionTinderReset(){ S.guionTinder={cards:[], i:0, started:false}; render(); }
  function guionTinderHTML(){
    if(!guionTinderEligible()) return '';   // necesita >=3 guiones tuyos para que tenga sentido
    ensureGuionTinder();
    var gt=S.guionTinder;
    var head='<div class="brain-section-t">'+L("Tinder de tus guiones","Tinder of your scripts")+
      ' <span class="brain-tag">'+L("entrena el Cerebro con lo que ya creaste","train your Brain with what you made")+'</span></div>';
    if(!gt.started){
      return head+'<div class="bt-wrap"><div class="gt-intro">'+
        '<p class="gt-intro-t">'+L("Desliza tus guiones: <b>esto soy yo</b> o <b>no es mío</b>. Cada deslizamiento afina tu voz y alimenta el Cerebro.","Swipe your scripts: <b>this is me</b> or <b>not me</b>. Each swipe sharpens your voice and feeds your Brain.")+'</p>'+
        '<button class="btn btn-md btn-primary" data-act="gt-start">'+IC.spark+' '+L("Empezar la baraja","Start the deck")+'</button>'+
      '</div></div>';
    }
    if(gt.i>=gt.cards.length){
      return head+'<div class="bt-wrap"><div class="bt-done">'+IC.check+' '+
        L("Baraja completada. Tu Cerebro tiene más de TU voz.","Deck done. Your Brain has more of YOUR voice.")+
        ' <button class="btn btn-sm btn-ghost" data-act="gt-reset">'+L("Otra vez","Again")+'</button></div></div>';
    }
    var c=gt.cards[gt.i];
    var sub=(c.beat && c.beat!==c.hook) ? '<p class="gt-beat">'+ESC(c.beat)+'</p>' : '';
    return head+'<div class="bt-wrap"><div class="bt-card gt-card">'+
      (c.title?'<div class="bt-kind">'+ESC(c.title)+'</div>':'')+
      '<p class="bt-text">'+ESC(c.hook)+'</p>'+ sub +
      '<div class="bt-row">'+
        '<button class="bt-btn bt-no" data-act="gt-vote" data-k="0">'+IC.x+' '+L("No es mío","Not me")+'</button>'+
        '<button class="bt-btn bt-yes" data-act="gt-vote" data-k="1">'+IC.check+' '+L("Esto soy yo","This is me")+'</button>'+
      '</div>'+
      '<div class="bt-prog">'+(gt.i+1)+' / '+gt.cards.length+'</div>'+
    '</div></div>';
  }
  /* ── v3 (mockup David · Cerebro): espina limpia ───────────────────────
     header (eyebrow glow + «Tu Cerebro» + sub), hero con anillo de progreso +
     nodos cerebro animados + columna de stats, rejilla «Lo que ya sé de ti» |
     «Niveles del Cerebro», y «Misiones para subir al Nivel N». Las features
     densas (entrenar, tinder, asistentes, voz, métricas) van DEBAJO, intactas. */
  function brainHeaderV3HTML(){
    return '<header class="ce-head">'+
      '<div class="ce-eyebrow"><span class="ce-eye-dot"></span>'+L("tu copiloto, entrenándose con cada guion","your copilot, training with every script")+'</div>'+
      '<h1 class="ce-h1">'+L("Tu Cerebro","Your Brain")+'</h1>'+
      '<p class="ce-sub">'+L("Aprende tu voz reel a reel. Cuanto más creas, más clava tu tono — y antes escribe solo.","It learns your voice reel by reel. The more you create, the better it nails your tone — and the sooner it writes on its own.")+'</p>'+
    '</header>';
  }
  // Nodos del cerebro (SVG) — réplica del mockup: glifo + sinapsis animadas.
  var _ceBrainSVG='<svg width="118" height="118" viewBox="0 0 120 120" fill="none" class="ce-brain-svg">'+
    '<g class="ce-breathe">'+
    '<path d="M60 22 C45 20 35 30 35 42 C25 45 25 60 33 64 C29 75 39 88 53 85 C57 93 70 93 75 85 C89 88 99 75 91 64 C99 59 97 44 86 43 C86 31 76 20 60 22 Z" fill="var(--brand-500)" opacity="0.07"/>'+
    '<path d="M60 22 C45 20 35 30 35 42 C25 45 25 60 33 64 C29 75 39 88 53 85 C57 93 70 93 75 85 C89 88 99 75 91 64 C99 59 97 44 86 43 C86 31 76 20 60 22 Z" stroke="var(--brand-300,#6E92FF)" stroke-width="1" opacity="0.35"/>'+
    '<circle class="ceN" cx="60" cy="58" r="5.5" fill="var(--brand-500)"/>'+
    '<circle class="ceN" cx="56" cy="40" r="3.6" fill="var(--brand-300,#6E92FF)" style="animation-delay:.2s"/>'+
    '<circle class="ceN" cx="40" cy="54" r="3.6" fill="var(--brand-300,#6E92FF)" style="animation-delay:.5s"/>'+
    '<circle class="ceN" cx="76" cy="56" r="3.6" fill="var(--brand-300,#6E92FF)" style="animation-delay:.35s"/>'+
    '<circle class="ceN" cx="58" cy="76" r="3.6" fill="var(--brand-300,#6E92FF)" style="animation-delay:.7s"/>'+
    '<circle class="ceN" cx="44" cy="34" r="2.8" fill="var(--brand-300,#6E92FF)" style="animation-delay:.9s"/>'+
    '<circle class="ceN" cx="72" cy="36" r="2.8" fill="var(--brand-300,#6E92FF)" style="animation-delay:1.1s"/>'+
    '<circle class="ceN" cx="46" cy="72" r="2.8" fill="var(--brand-300,#6E92FF)" style="animation-delay:.6s"/>'+
    '<circle class="ceN" cx="74" cy="74" r="2.8" fill="var(--brand-300,#6E92FF)" style="animation-delay:1.3s"/>'+
    '</g></svg>';
  function brainHeroV3HTML(lv, voicePct){
    var b=brand();
    var pct=brainProgress();
    var leveled=pct>=100 || !lv.next;
    var off=Math.round(465*(1-pct/100));
    var kicker=leveled?L("Manifestando viralidad","Manifesting virality"):L("Vas afinando","Dialing it in");
    var title=leveled
      ? L("¡Tu Cerebro está a punto de subir de nivel!","Your Brain is about to level up!")
      : L("Te falta poco para que escriba como tú sin pensar","Almost there — soon it writes like you without thinking");
    var reels=hasRealVoice()?(S.voice.source_count||0):(b.reelsAnalyzed||0);
    var reelsTxt=String(reels).replace(/\B(?=(\d{3})+(?!\d))/g," ");
    var nGuiones=S.guiones.filter(function(g){return g.status!=="discarded";}).length;
    var stats=[
      [L("Reels analizados","Reels analyzed"), reelsTxt, "var(--text-primary)"],
      [L("Guiones creados","Scripts created"), String(nGuiones), "var(--text-primary)"],
      [L("Tu voz, clavada","Your voice, nailed"), voicePct+"%", "var(--success-fg,#3FE0A0)"],
      [L("Racha","Streak"), (S.user.streak||0)+" "+L("días","days"), "var(--brand-500,#2F5BFF)"]
    ];
    var statsH=stats.map(function(s){ return '<div class="ce-stat"><span class="ce-stat-l">'+ESC(s[0])+'</span><span class="ce-stat-v" style="color:'+s[2]+'">'+ESC(s[1])+'</span></div>'; }).join("");
    return '<div class="ce-hero-row">'+
      '<div class="ce-hero">'+
        '<div class="ce-ring">'+
          '<div class="ce-ring-circle">'+
            '<svg width="172" height="172" viewBox="0 0 172 172" class="ce-ring-svg" aria-hidden="true">'+
              '<circle cx="86" cy="86" r="74" fill="none" stroke="var(--surface-overlay)" stroke-width="13"/>'+
              '<circle cx="86" cy="86" r="74" fill="none" stroke="var(--brand-500)" stroke-width="13" stroke-linecap="round" stroke-dasharray="465" stroke-dashoffset="'+off+'" class="ce-ring-prog"/>'+
            '</svg>'+
            '<div class="ce-ring-c">'+_ceBrainSVG+'</div>'+
            '<div id="rsBrainStage" class="brain3d-stage" style="display:none"><div class="brain-orb">'+IC.brain+'</div></div>'+
          '</div>'+
          '<span class="ce-ring-lvl">'+(lv.level>=1?(L("NIVEL","LEVEL")+' <b>'+lv.level+'</b>'):'<b>'+L("NUEVO","NEW")+'</b>')+'</span>'+
        '</div>'+
        '<div class="ce-hero-body">'+
          '<div class="ce-kicker">'+ESC(kicker)+'</div>'+
          '<div class="ce-hero-title">'+ESC(title)+'</div>'+
          (lv.next?'<div class="ce-prog-row"><span>'+L("Progreso al Nivel "+lv.next,"Progress to Level "+lv.next)+'</span><span class="ce-prog-pct">'+pct+'%</span></div>':'')+
          '<div class="ce-prog-bar"><div class="ce-prog-fill eco-fill" style="width:'+Math.max(4,pct)+'%"></div></div>'+
        '</div>'+
      '</div>'+
      '<div class="ce-stats">'+statsH+'</div>'+
    '</div>';
  }
  function brainKnowHTML(v){
    var reels=hasRealVoice()?(S.voice.source_count||0):(brand().reelsAnalyzed||47);
    var tonos=(v.tono||"").split(/[,·]/).map(function(x){return x.trim();}).filter(Boolean);
    if(!tonos.length) tonos=["Directo","Cercano"];
    var pills=tonos.map(function(t){ return '<span class="ce-tono">'+ESC(t)+'</span>'; }).join("");
    // Muletillas como chips (no un run de comas con comillas dobles → mucho más legible).
    var frases=(v.frases&&v.frases.length)
      ? v.frases.map(function(f){ return '<span class="ce-mulet-chip">'+ESC(f)+'</span>'; }).join("")
      : '<span class="ce-mulet-empty">—</span>';
    var _g0=(v.frases&&v.frases[0])?ESC(v.frases[0]):"";
    var gancho=_g0 ? ('“'+(_g0.length>22?_g0.slice(0,22).replace(/\s+\S*$/,'')+'…':_g0)+'”') : '“Comenta X…”';
    return '<div class="ce-card ce-know">'+
      '<div class="ce-card-head"><span class="ce-card-t">'+L("Lo que ya sé de ti","What I already know about you")+'</span><span class="ce-card-meta">'+L("› de "+reels+" reels","› from "+reels+" reels")+'</span></div>'+
      '<div class="ce-know-block"><span class="ce-know-k">'+L("Tu tono","Your tone")+'</span><div class="ce-tonos">'+pills+'</div></div>'+
      '<div class="ce-know-block"><span class="ce-know-k">'+L("Tus muletillas","Your catchphrases")+'</span><div class="ce-mulet">'+frases+'</div></div>'+
      '<div class="ce-know-foot">'+
        '<div><div class="ce-foot-k">'+L("duración ideal","ideal length")+'</div><div class="ce-foot-v">'+ESC(v.duracion||"30–45 s")+'</div></div>'+
        '<div><div class="ce-foot-k">'+L("gancho top","top hook")+'</div><div class="ce-foot-v">'+gancho+'</div></div>'+
      '</div>'+
    '</div>';
  }
  // Roadmap de niveles (nombres/desbloqueos de David); el nivel actual viene de brainLevel().
  function brainLevelsHTML(lv){
    // Nombres en ecoLevelName (fuente única); aquí solo las descripciones de desbloqueo.
    var data=[
      [1,L("Detección básica de señales","Basic signal detection")],
      [2,L("Tu voz aprendida · guiones con tu tono","Your voice learned · scripts in your tone")],
      [3,L("«Llena mi semana» · 5 guiones de golpe","«Fill my week» · 5 scripts at once")],
      [4,L("Más competidores en el radar · métricas Pro","More competitors on the radar · Pro metrics")],
      [5,L("Autopiloto · el Cerebro escribe solo","Autopilot · the Brain writes by itself")]
    ];
    var cur=lv.level||1;
    var rows=data.map(function(d){
      var n=d[0], isCur=n===cur, isPast=n<cur, isNext=n===cur+1;
      var cls=isCur?" cur":(isPast?" past":"");
      var tag=isPast?IC.check:(isCur?L("Aquí estás","You are here"):(isNext?L("Siguiente","Next"):""));
      return '<div class="ce-lvl'+cls+'">'+
        '<div class="ce-lvl-n">'+(isPast?IC.check:n)+'</div>'+
        '<div class="ce-lvl-body"><div class="ce-lvl-name">'+ESC(ecoLevelName(n))+'</div><div class="ce-lvl-unlock">'+ESC(d[1])+'</div></div>'+
        (tag?'<span class="ce-lvl-tag">'+tag+'</span>':'')+
      '</div>';
    }).join("");
    return '<div class="ce-card ce-levels"><span class="ce-card-t">'+L("Niveles del Cerebro","Brain levels")+'</span><div class="ce-lvl-list">'+rows+'</div></div>';
  }
  function brainMissionsHTML(lv){
    var s=brainSignals();
    var nRec=S.guiones.filter(function(g){return g.status==="recorded";}).length;
    var nx=lv.next||lv.level;
    var data=[
      [(s.guiones>=3), L("Crea 3 guiones con tu voz","Create 3 scripts in your voice"), L("+ afina tu tono","+ tunes your tone"), IC.edit, "tab","dashboard"],
      [(nRec>=2), L("Graba 2 reels en teleprónter","Record 2 reels on teleprompter"), L("+ aprende tu ritmo","+ learns your pace"), IC.mic, "tab","guiones"],
      [(s.comps>=1), L("Añade 1 competidor más","Add 1 more competitor"), L("+ amplía el radar","+ widens the radar"), IC.eye, "add-comp",""],
      [(s.guiones>=1), L("Roba y remezcla 1 viral","Steal & remix 1 viral"), L("+ desbloquea estilos","+ unlocks styles"), IC.repeat, "tab","dashboard"]
    ];
    var done=data.filter(function(m){return m[0];}).length;
    var cards=data.map(function(m){
      var dn=m[0];
      var btn=dn
        ? '<button class="btn btn-sm btn-secondary ce-mission-done" disabled>'+IC.check+' '+L("Hecho","Done")+'</button>'
        : '<button class="btn btn-sm btn-primary" data-act="'+m[4]+'"'+(m[5]?' data-k="'+m[5]+'"':'')+'>'+L("Empezar","Start")+'</button>';
      return '<div class="ce-mission'+(dn?' done':'')+'">'+
        '<div class="ce-mission-ic">'+m[3]+'</div>'+
        '<div class="ce-mission-body"><div class="ce-mission-l">'+ESC(m[1])+'</div><div class="ce-mission-r">'+ESC(m[2])+'</div></div>'+
        btn+
      '</div>';
    }).join("");
    return '<div class="ce-card ce-missions">'+
      '<div class="ce-missions-head"><div><span class="ce-card-t">'+L("Misiones para subir al Nivel "+nx,"Missions to reach Level "+nx)+'</span>'+
        '<div class="ce-missions-sub">'+L("Cada misión entrena tu Cerebro y llena el anillo.","Each mission trains your Brain and fills the ring.")+'</div></div>'+
        '<span class="ce-missions-count">'+done+' / '+data.length+' '+L("completadas","completed")+'</span></div>'+
      '<div class="ce-missions-grid">'+cards+'</div>'+
    '</div>';
  }
  // v4 · Hero inmersivo del Cerebro: panel con la red neuronal viva (BrainNet,
  // canvas 2D montado por ensureBrainNet en #ceStage) + overlays nivel/voz, barra
  // de progreso y stats. Reemplaza el anillo SVG estático. El fondo de la PÁGINA no
  // cambia (este panel es el stage del cerebro, no el fondo de la app).
  function brainHeroV4HTML(lv, voicePct, b, nGuiones){
    var reels = hasRealVoice()?(S.voice.source_count||0):((b&&b.reelsAnalyzed)||0);
    var racha = (S.stats&&S.stats.streak)||0;
    var pct = Math.max(0,Math.min(100, lv.pct||0));
    var nearUp = pct>80;
    var vp = Math.round(voicePct||0);
    var kicker = nearUp ? L("NUEVO · A PUNTO DE EVOLUCIONAR","NEW · ABOUT TO EVOLVE") : L("TU CEREBRO, AHORA","YOUR BRAIN, NOW");
    var title = nearUp ? L("¡Tu Cerebro está a punto de subir de nivel!","Your Brain is about to level up!")
                       : L("Manifestando viralidad, reel a reel.","Manifesting virality, reel by reel.");
    var stat=function(val,lab,col){ return '<div class="ce2-stat"><div class="ce2-stat-v"'+(col?' style="color:'+col+'"':'')+'>'+ESC(String(val))+'</div><div class="ce2-stat-l">'+ESC(lab)+'</div></div>'; };
    return '<div class="ce2-hero">'+
      '<div class="ce2-stage" id="ceStage">'+
        '<div class="ce2-ov ce2-net"><i></i>'+L("red neuronal","neural network")+'</div>'+
        '<div class="ce2-ov ce2-lvlw"><div class="ce2-lvlw-k">'+L("Nivel","Level")+' '+lv.level+' / 5</div><div class="ce2-lvlw-n">'+ESC(ecoLevelName(lv.level))+'</div></div>'+
        '<div class="ce2-ov ce2-voz"><div class="ce2-voz-v">'+vp+'%</div><div class="ce2-voz-k">'+L("voz clavada","voice nailed")+'</div></div>'+
      '</div>'+
      '<div class="ce2-side">'+
        '<div class="ce2-kicker">'+kicker+'</div>'+
        '<div class="ce2-htitle">'+title+'</div>'+
        '<div><div class="ce2-prog-row"><span>'+L("Progreso al Nivel","Progress to Level")+' '+Math.min(5,lv.level+1)+'</span><b>'+pct+'%</b></div>'+
          '<div class="ce2-prog-bar"><div class="ce2-prog-fill" style="width:'+pct+'%"></div></div></div>'+
        '<div class="ce2-stats">'+
          stat(reels, L("Reels analizados","Reels analyzed"))+
          stat(nGuiones, L("Guiones creados","Scripts created"))+
          stat(vp+'%', L("Tu voz, clavada","Your voice, nailed"), 'var(--success-fg,#3FE0A0)')+
          stat(racha+L(" días"," days"), L("Racha","Streak"), 'var(--brand-500,#2F5BFF)')+
        '</div>'+
      '</div>'+
    '</div>';
  }

  function brainHTML(){
    var b=brand();
    var v=brainVoice(b);
    // B1: nivel REAL derivado de señales (brainLevel), no del contador cosmético b.level.
    var lv=brainLevel();
    var voicePct = hasRealVoice() ? Math.max(0,Math.min(100,S.voice.confidence||0)) : (isDemo()?Math.max(6,Math.min(100,b.voice||40)):0);
    var comps=brainCompetitors();
    var nGuiones=S.guiones.filter(function(g){return g.status!=="discarded";}).length;
    var nPublished=(S.metrics&&S.metrics.videos)?S.metrics.videos.length:0;
    var learned=(S.metrics&&S.metrics.learned)||[];
    // userAssistants es global (index.html); typeof-guard por si aún no cargó.
    var nAsst=0; try{ if(typeof userAssistants!=="undefined" && Array.isArray(userAssistants)) nAsst=userAssistants.length; }catch(e){}

    var sources=[
      // Voz real derivada → «reels tuyos leídos» refleja la muestra usada (source_count).
      [hasRealVoice()?(S.voice.source_count||0):(b.reelsAnalyzed||0),"reels tuyos leídos","de aquí modelo tu voz","var(--brand-500)"],
      [comps.length,"competidores vigilados","de aquí saco qué funciona en tu nicho","#3b82f6"],
      [nGuiones,"guiones creados","cada uno refina tu estilo","#22c55e"],
      [nPublished,"publicados con métricas","cierran el círculo de aprendizaje","#ff2d55"]
    ].map(function(s){ return '<div class="brain-src" style="--c:'+s[3]+'"><div class="brain-src-n">'+s[0]+'</div><div class="brain-src-t">'+s[1]+'</div><div class="brain-src-d">'+s[2]+'</div></div>'; }).join("");

    var frases=v.frases.map(function(f){ return '<span class="voice-chip">'+ESC(f)+'</span>'; }).join("");
    var learnList=learned.length
      ? learned.map(function(l){ return '<div class="learn-item">'+IC.check+'<span>'+ESC(l)+'</span></div>'; }).join("")
      : '<div class="learn-item" style="opacity:.6">Conecta Instagram en Métricas y empezaré a ver qué funciona en tu cuenta.</div>';
    // T3: si tenemos la lista REAL de seguidos (con id), la mostramos con acción de
    // dejar de seguir. Sin ella aún (cargando), caemos al derivado de reels (read-only).
    var tracked = Array.isArray(S.tracked) ? S.tracked : null;
    var compList;
    if(tracked){
      compList = tracked.length
        ? tracked.map(function(t){
            var h=(t.creator&&t.creator.ig_username)||t.ig_username||"";
            var n=(t.reels_count!=null)?(t.reels_count+' reel'+(t.reels_count===1?'':'es')):'';
            return '<div class="brain-comp"><div class="ava bava">'+ESC(initialsOf(h))+'</div>'+
              '<span class="brain-comp-h">@'+ESC(h)+'</span>'+
              '<span class="brain-comp-n">'+ESC(n)+'</span>'+
              '<button class="brain-comp-x" data-act="untrack" data-id="'+ESC(String(t.id))+'" data-handle="'+ESC(h)+'" title="Dejar de seguir a @'+ESC(h)+'" aria-label="Dejar de seguir a @'+ESC(h)+'">'+IC.x+'</button>'+
              compThumbsHTML(h)+
            '</div>';
          }).join("")
        : '<div class="rs-empty" style="padding:20px">Aún no sigues a ningún competidor. Añádelos desde el Radar o un análisis.</div>';
    } else {
      compList = comps.length
        ? comps.map(function(c){ return '<div class="brain-comp"><div class="ava bava">'+ESC(initialsOf(c.handle))+'</div><span class="brain-comp-h">@'+ESC(c.handle)+'</span><span class="brain-comp-n">'+c.n+' reels analizados</span>'+compThumbsHTML(c.handle)+'</div>'; }).join("")
        : '<div class="rs-empty" style="padding:20px">Aún no sigues a nadie. Añade competidores en el Dashboard.</div>';
    }

    return '<div class="scroll"><div class="canvas ce-canvas">'+
      // ── espina limpia (mockup David) ──
      brainHeaderV3HTML()+
      brainHeroV4HTML(lv, voicePct, b, nGuiones)+   // v4: cerebro inmersivo (BrainNet canvas) — reemplaza el anillo SVG
      brainFeedMeHTML()+   // #2/#7: alimentar el Cerebro (ejercicio diario +5%, CD 24h). También en demo para previsualizar la escalera.
      '<div class="ce-grid">'+brainKnowHTML(v)+brainLevelsHTML(lv)+'</div>'+
      brainMissionsHTML(lv)+
      // v3 (mockup David): el Cerebro queda SOLO con la espina (header + anillo +
      // «Lo que ya sé de ti» + niveles + misiones). Las features densas anteriores
      // (entrenar, tinder, re-analizar, analizar reel, tono, voz, fuentes,
      // asistentes, métricas, próxima serie, competidores) se han RETIRADO de esta
      // pantalla por decisión del usuario — sus funciones siguen definidas/usables
      // desde otros puntos, pero ya no se pintan aquí.
    '</div></div>';
  }

  // T2: lista inline de asistentes en Cerebro. Lee el global userAssistants
  // (cargado por loadAssistants en el arranque y refrescado tras cada CRUD vía
  // RadarLoop.refresh). Editar/Borrar usan los handlers globales + su modal.
  function brainAsstListHTML(n){
    var arr=[]; try{ if(typeof userAssistants!=="undefined" && Array.isArray(userAssistants)) arr=userAssistants; }catch(e){}
    if(!arr.length){
      return '<div class="rs-empty" style="padding:18px;margin-top:4px">Aún no tienes asistentes. Crea tu primer estilo con tu tono y tus reglas.</div>';
    }
    return '<div class="brain-asst-list">'+arr.map(function(a){
      var name=ESC(a.name||"Asistente");
      var prev=(a.instructions||"").trim();
      var prevTxt=ESC(prev.slice(0,90))+(prev.length>90?"…":"");
      var aid=ESC(String(a.id));
      return '<div class="brain-asst-item">'+
        '<div class="ava bava">'+ESC(initialsOf(a.name||"A"))+'</div>'+
        '<div class="brain-asst-meta"><div class="brain-asst-name">'+name+(a.is_default?' <span class="brain-tag">default</span>':'')+'</div>'+
          (prevTxt?'<div class="brain-asst-prev">'+prevTxt+'</div>':'')+'</div>'+
        '<button class="brain-asst-act" onclick="editAssistant(\''+aid+'\')" aria-label="Editar '+name+'">Editar</button>'+
        '<button class="brain-asst-act del" onclick="deleteAssistant(\''+aid+'\')" aria-label="Borrar '+name+'">Borrar</button>'+
      '</div>';
    }).join("")+'</div>';
  }

  /* ════════════════════════════════════════════════════════════════
     EQUIPO (solo Agencia) — miembros, roles y marcas asignadas.
     Pool de créditos compartido de cuenta (sin reparto por marca).
     ════════════════════════════════════════════════════════════════ */
  function teamMembers(){
    var bn=S.brands.map(function(b){return b.name;});
    return [
      {name:"Bernat C.", role:"Owner",        initials:"BC", color:"#4f7cff", brands:bn},
      {name:"María L.",  role:"Editor",       initials:"ML", color:"#12a37c", brands:bn.slice(1,3)},
      {name:"Jordi P.",  role:"Editor",       initials:"JP", color:"#e0556b", brands:bn.slice(3,4)},
      {name:"Aïda R.",   role:"Solo lectura", initials:"AR", color:"#6d6bf6", brands:bn.slice(0,1)}
    ];
  }
  function teamHTML(){
    // En prod pinta los miembros reales (S.team, cargado por loadTeam); en demo, el pool demo.
    var members=(!isDemo() && Array.isArray(S.team)) ? S.team : teamMembers();
    var roleCls={"Owner":"owner","Miembro":"editor","Editor":"editor","Invitado · pendiente":"viewer","Solo lectura":"viewer"};
    var stats=[
      ["Miembros", members.length, "", ""],
      ["Marcas", S.brands.length, "", ""],
      ["Asientos", "∞", "ilimitados", "acc"],
      ["Créditos", "pool", "compartido de cuenta", "acc"]
    ];
    var statbar='<div class="statbar">'+stats.map(function(s){
      return '<div class="stat"><div class="stat-k">'+ESC(s[0])+'</div><div class="stat-v">'+ESC(s[1])+'</div>'+(s[2]?'<div class="stat-d '+s[3]+'">'+ESC(s[2])+'</div>':'')+'</div>';
    }).join("")+'</div>';
    var rows=members.map(function(m){
      var chips=(m.brands||[]).map(function(bn){return '<span class="mchip">'+ESC(bn)+'</span>';}).join("")||'<span class="mchip muted">sin marcas</span>';
      return '<div class="mrow">'+
        '<span class="mava" style="background:'+ESC(m.color)+'">'+ESC(m.initials)+'</span>'+
        '<div class="mrow-id"><div class="mrow-name">'+ESC(m.name)+'</div><span class="mrole role-'+(roleCls[m.role]||"viewer")+'">'+ESC(m.role)+'</span></div>'+
        '<div class="mrow-brands">'+chips+'</div>'+
        '<button class="iconbtn" data-act="team-edit" title="Gestionar miembro">'+IC.chev+'</button>'+
      '</div>';
    }).join("");
    return '<div class="scroll"><div class="canvas">'+
      pheadHTML("Equipo · Agencia", "Tu equipo", "Quién puede tocar qué marca. Los créditos son un pool compartido de la cuenta.")+
      statbar+
      '<div class="feed-head"><span class="feed-title">Miembros <span class="ct">· '+members.length+'</span></span><button class="btn btn-sm btn-primary" data-act="team-invite">'+IC.plus+' Invitar miembro</button></div>'+
      '<div class="mrow-list">'+rows+'</div>'+
    '</div></div>';
  }

  /* ════════════════════════════════════════════════════════════════
     OVERLAYS (gen / script / formatos / teleprompter / fillweek)
     ════════════════════════════════════════════════════════════════ */
  /* T6 (IDI): espera honesta. Los pasos cosméticos venden valor ~8s; si el robo
     real sigue sin responder, pasamos a mensajes HONESTOS (rotación lenta) y
     ofrecemos seguir navegando — la generación termina en segundo plano y avisa
     con un toast. Flow: «reacción directa» + «sentido de control». */
  var HONEST_MSGS=[
    "Tu rival hablaba mucho — dame unos segundos más…",
    "Sigo en ello. Reescribir bien lleva un momento…",
    "Ya casi. Puliendo tu versión…"
  ];
  function generatingHTML(kind){
    var steps=S._genSlow?HONEST_MSGS:(GEN_STEPS[kind]||GEN_STEPS.script);
    return '<div class="gen'+(kind==="script"?" cooking":"")+'"><div class="orb"></div><div><div class="gtitle">'+ESC(GEN_TITLE[kind]||"Trabajando")+'</div><div class="gstep" id="rsGenStep">'+ESC(steps[0])+'</div>'+
      (S._genSlow?'<div class="gen-bg"><div class="gen-bg-hint">'+L("En menos de 1 min lo tienes en Ideas robadas.","In under 1 min it'll be in your Stolen ideas.")+'</div><button class="btn btn-md btn-secondary" data-act="gen-background">Seguir navegando — te aviso al terminar</button></div>':'')+
    '</div></div>';
  }
  function conveyorHTML(){
    // real:true = el formato existe de verdad en prod (endpoint + contenido real).
    // hooks/carousel/linkedin/x/serie hoy son TEATRO (contenido hardcodeado +
    // descuento local de créditos) → solo se enseñan en demo. Al implementar un
    // formato de verdad, basta marcarlo real:true.
    // P4 (plan acción): continuidad del bucle = corazón del Hook Model. Si queda otra
    // señal en el radar, el siguiente paso natural NO es volver al feed: es robar la
    // siguiente sin salir del flujo. Es la acción REAL de más impacto de la cinta.
    var nx=feedReels().filter(function(x){ return !(S.reel && x.id===S.reel.id); })[0];
    var items=[
      {k:"record",  ic:IC.mic,    t:L("Grábalo ahora","Record it now"),       d:L("Teleprompter listo · gratis","Teleprompter ready · free"), feature:true, prim:true, real:true },
      (nx ? {k:"next", ic:IC.spark, t:L("Roba la siguiente señal","Steal the next signal"),
             d:"@"+nx.creator.handle+L(" está petando ahora"," is blowing up now"), feature:true, real:true } : null),
      {k:"hooks",   ic:IC.hook,   t:"5 hooks alternativos",   d:"El hook es el 80% del reel",      feature:false, real:false},
      {k:"carousel",ic:IC.layers, t:"Conviértelo en carrusel",d:"La misma idea, en post",          feature:false, real:false},
      {k:"linkedin",ic:'<span style="font-weight:800;font-size:13px">in</span>', t:"Versión LinkedIn", d:"Llega a otro público", feature:false, real:false},
      {k:"x",       ic:'<span style="font-weight:800;font-size:15px">𝕏</span>',  t:"Hilo para X",      d:"Exprime el mismo ángulo", feature:false, real:false},
      {k:"serie",   ic:IC.repeat, t:"Genérame una serie de 3",d:"Contenido para toda la semana",   feature:false, real:false}
    ].filter(Boolean);
    // Los formatos aún no reales se ENSEÑAN pero deshabilitados («Próximamente»):
    // ni clicables ni pueden gastar créditos (antes en demo eran teatro clicable).
    var rows=items.map(function(it){ var k=it.k,d=!!S.done[k]; var lbl=d?(k==="record"?"Grabado ✓":"Hecho ✓"):it.t;
      if(!it.real){
        return '<button class="chain chain-soon" disabled aria-disabled="true" title="'+L("Próximamente","Coming soon")+'"><div class="cic">'+it.ic+'</div><div class="ctext"><div class="ct">'+ESC(it.t)+'</div><div class="cd">'+ESC(it.d)+'</div></div><span class="chain-soon-tag">'+L("Próximamente","Soon")+'</span></button>';
      }
      return '<button class="chain'+(it.feature?" chain-feature":"")+(it.prim?" chain-prim":"")+(d?" done":"")+'" '+(d?"":'data-act="chain" data-k="'+k+'"')+'><div class="cic">'+(d?IC.check:it.ic)+'</div><div class="ctext"><div class="ct">'+ESC(lbl)+'</div><div class="cd">'+ESC(it.d)+'</div></div>'+(d?"":'<span class="arr">'+IC.arr+'</span>')+'</button>'; }).join("");
    var nReal=items.filter(function(it){return it.real;}).length;
    var sub=nReal>1
      ? 'Ya tienes el guión. Multiplícalo en un toque — cada formato es una pieza más sin volver a pensar.'
      : 'Ya tienes el guión, guardado en tu idea robada. Pásalo al teleprompter y grábalo — grabar no gasta créditos.';
    return '<div class="belt"><div class="belt-h"><h4>¿Y ahora?</h4></div><p class="belt-sub">'+sub+'</p><div class="belt-grid'+(items.length===1?' solo':'')+'">'+rows+'</div></div>';
  }
  // Ítem 10: FORMATO DE GRABACIÓN sugerido. El LLM clasifica el reel original en uno de
  // estos 5; aquí va la copia "cómo hacerlo" + icono (la clasificación NO se inventa).
  // Lista CERRADA (display name + «grábalo así») — receta, no etiqueta suelta.
  var REC_FORMATS={
    selfie:{label:L("Selfie hablado","Talking selfie"),how:L("A cámara, tú hablando directo.","To camera, talking straight to it."),ic:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>'},
    pizarra:{label:L("Pizarra","Whiteboard"),how:L("Escribiendo a mano mientras explicas.","Writing by hand as you explain."),ic:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>'},
    escritorio:{label:L("Escritorio / pantalla","Screen / desktop"),how:L("Grabas la pantalla, tu voz encima.","Screen-record, your voice over it."),ic:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>'},
    podcast:{label:L("Clip de podcast","Podcast clip"),how:L("Cara a cara, formato conversación.","Face to face, conversation format."),ic:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="11" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v4"/></svg>'},
    "broll-vo":{label:L("B-roll + voz en off","B-roll + voiceover"),how:L("Imágenes de recurso, tú narrando.","Stock/cutaway footage, you narrating."),ic:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9l5 3-5 3z"/></svg>'},
    pov:{label:L("POV + texto","POV + text"),how:L("Plano POV con texto en pantalla.","POV shot with on-screen text."),ic:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>'}
  };
  function _recFmtDemo(r){ var s=_durSec(r&&r.dur||"")||0; var keys=["selfie","pizarra","podcast","escritorio","broll-vo","pov"];
    if(s>=90) return "podcast"; if(s>0&&s<=40) return "pov"; return keys[_lbHash((r&&(r.id||r.cap))||"x",0,keys.length)]; }
  // Carga EJEMPLOS reales del pool con el mismo formato (cacheado, sin scrape). 1 vez por formato.
  function loadFormatExamples(fmt){
    if(!fmt) return;
    if(S._fmtEx && S._fmtEx.fmt===fmt) return;            // ya cargado/en curso para este formato
    if(isDemo()){
      // Demo: arma referencias de mentira con reels que ya hay (para que Leo lo vea).
      var src=(S.reels||[]).filter(function(x){return x.thumb;});
      // Si los reels del radar no traen miniatura (p.ej. cofre demo con thumb null),
      // sembramos desde __DEMO_THUMBS__ para que el sidebar NO salga vacío en el túnel.
      if(src.length<2){
        var dt=_demoThumbs()||[]; var hs=["marcos.crea","ia_con_ana","nico.edita","sara.vende"]; var ml=[14,9,6,4];
        for(var i=src.length;i<2 && i<dt.length;i++){
          var rl=(S.reels||[])[i]||{};
          src.push({creator:{handle:(rl.creator&&rl.creator.handle)||hs[i]||"creador"}, thumb:dt[i%dt.length],
                    explosionTxt:(rl.explosionTxt!=null?rl.explosionTxt:ml[i]), url:rl.url||null});
        }
      }
      var ex=src.slice(0,4).map(function(x){
        return {handle:(x.creator&&x.creator.handle)||"creador", thumb:x.thumb,
                explosion:(x.explosionTxt!=null?x.explosionTxt:null),
                url:x.url||(x.ig_reel_id?("https://www.instagram.com/reel/"+x.ig_reel_id+"/"):null)}; });
      S._fmtEx={fmt:fmt, loading:false, examples:ex}; return;
    }
    S._fmtEx={fmt:fmt, loading:true, examples:[]};
    var _p=_pidOf(S.brandId);
    apiGet("/api/reels/by-format?format="+encodeURIComponent(fmt)+(_p?("&project_id="+encodeURIComponent(_p)):"")).then(function(rr){
      var ex=(rr.ok&&rr.d&&Array.isArray(rr.d.examples))?rr.d.examples:[];
      S._fmtEx={fmt:fmt, loading:false, examples:ex};
      if(S.view==="script") render();                     // repinta si seguimos en el reveal
    });
  }
  function _fmtExamplesHTML(fmt){
    var st=(S._fmtEx&&S._fmtEx.fmt===fmt)?S._fmtEx:null;
    if(!st||st.loading) return '<div class="recfmt-ex-wait"><span class="rs-ldr"></span> '+L("Buscando referencias…","Finding references…")+'</div>';
    if(!st.examples.length) return '<div class="recfmt-ex-empty">'+L("Aún sin referencias de este formato.","No references for this format yet.")+'</div>';
    var cells=st.examples.slice(0,2).map(function(e){   // 2 vídeos prominentes a la derecha
      // onerror: si la miniatura no existe (404), se oculta y queda el placeholder de fondo.
      var thumb=e.thumb?('<img src="'+ESC(e.thumb)+'" alt="" loading="lazy" onerror="this.style.display=\'none\'">'):('<div class="recfmt-ex-ph">'+_icPlay+'</div>');
      var mult=(e.explosion!=null)?('<span class="recfmt-ex-mult">'+IC.bolt+' '+ESC(String(e.explosion))+'×</span>'):'';
      var inner='<div class="recfmt-ex-thumb">'+thumb+mult+'<span class="recfmt-ex-play">'+_icPlay+'</span></div><div class="recfmt-ex-h">@'+ESC(e.handle||"")+'</div>';
      return e.url
        ? '<a class="recfmt-ex" href="'+ESC(e.url)+'" target="_blank" rel="noopener noreferrer" title="'+L("Abrir el reel original","Open the original reel")+'">'+inner+'</a>'
        : '<div class="recfmt-ex">'+inner+'</div>';
    }).join("");
    return '<div class="recfmt-ex-lbl">'+L("Grábalo así","Record it like this")+'</div><div class="recfmt-ex-row">'+cells+'</div>';
  }
  function recFormatCardHTML(r){
    var k=(r&&r.recFormat)||(isDemo()?_recFmtDemo(r):null); var f=k&&REC_FORMATS[k]; if(!f) return '';
    loadFormatExamples(k);                                 // dispara la carga de referencias (1 vez)
    var pov=(k==="pov" && r.povText)
      ? '<div class="recfmt-pov"><span class="recfmt-pov-lbl">'+L("TEXTO EN PANTALLA (POV)","ON-SCREEN TEXT (POV)")+'</span>'+
        '<div class="recfmt-pov-tx">'+(String(r.povText).split("/").map(function(t){ t=t.trim(); return t?'<span>'+ESC(t)+'</span>':''; }).filter(Boolean).join(''))+'</div></div>'
      : '';
    return '<div class="recfmt">'+
      '<div class="recfmt-k">'+L("FORMATO SUGERIDO","SUGGESTED FORMAT")+'</div>'+
      '<div class="recfmt-row">'+
        '<div class="recfmt-left"><div class="recfmt-ic">'+f.ic+'</div>'+
          '<div class="recfmt-tx"><div class="recfmt-name">'+f.label+'</div><div class="recfmt-d">'+f.how+'</div></div></div>'+
        '<div class="recfmt-right">'+_fmtExamplesHTML(k)+'</div>'+
      '</div>'+pov+'</div>';
  }
  // #2 (Bernat): el PRIMER guion robado es un hito. Lo celebramos una sola vez.
  // Demo NO persiste (flag de sesión) para que Leo lo pueda re-previsualizar recargando.
  function firstStealPending(){
    if(isDemo()) return !S._demoStealDone;
    try{ return !localStorage.getItem("rs_first_steal"); }catch(e){ return false; }
  }
  function markFirstSteal(){
    if(isDemo()){ S._demoStealDone=true; return; }
    try{ localStorage.setItem("rs_first_steal","1"); }catch(e){}
  }
  function scriptRevealHTML(){
    // BUGFIX cruce de reels: el reveal SIEMPRE renderiza el reel cuya generación
    // completó (S.revealReel), no la global S.reel (que una 2ª generación en vuelo pudo
    // reasignar). Así guion + «ver original» pertenecen al MISMO reel que se robó.
    var r=S.revealReel||S.reel,s=r.script||{hook:"",beats:[],close:""};
    // ANTI-PARPADEO: la animación de entrada (.fade-in + .reveal-hero) solo debe correr
    // en el PRIMER pintado de este guion. Sin esto, cualquier re-render de fondo (p.ej.
    // loadFormatExamples al resolver, o refresco de créditos) reconstruye el .script-wrap
    // y REINICIA la animación → el guion "parpadea". Marcamos el reel ya animado.
    var _firstPaint = S._revealShownId !== (r.id||"x"); S._revealShownId = (r.id||"x");
    var beats=(s.beats||[]).map(function(b,i){return '<div class="beat"><span class="n">'+String(i+1).padStart(2,"0")+'</span><span>'+ESC(b)+'</span></div>';}).join("");
    // PRIMER guion → banner héroe + prueba social del reel robado (lo que petó).
    // Solo la 1ª vez (S._firstStealCelebrate, one-shot que pone steal()).
    var hero='';
    if(S._firstStealCelebrate){
      var _views=r.views?('<b class="num-hi">'+ESC(r.views)+'</b> views'):'';
      var _mult=(r.explosionTxt!=null)?('<b class="num-hi">'+ESC(String(r.explosionTxt))+'×</b> '+L("su media","their average")):'';
      var _proof=[_views,_mult].filter(Boolean).join(' · ');
      hero='<div class="reveal-hero">'+
        '<div class="reveal-hero-badge">'+IC.bolt+' '+L("TU PRIMER GUION","YOUR FIRST SCRIPT")+'</div>'+
        '<div class="reveal-hero-t">'+L("Y ya es tuyo.","And it’s already yours.")+'</div>'+
        (_proof?'<div class="reveal-hero-proof">'+L("Robado de un reel que hizo","Stolen from a reel that did")+' '+_proof+'</div>':'')+
      '</div>';
    }
    // Switcher de OPCIONES de guion (2) — el usuario elige.
    var opts=r.options||[]; var oi=r.optIdx||0;
    var optTabs = opts.length>1
      ? '<div class="opt-tabs">'+opts.map(function(o,i){
          return '<button class="opt-tab'+(i===oi?" on":"")+'" data-act="opt-pick" data-k="'+i+'">'+L("Opción ","Option ")+(i+1)+'</button>'; }).join("")+
        '<span class="opt-tabs-hint">'+L("elige el que más te suene","pick the one that sounds most like you")+'</span></div>'
      : '';
    // Selector de HOOKS (2-3) de la opción activa.
    var hooks=(opts[oi]&&opts[oi].hooks)||[s.hook]; var hi=r.hookIdx||0;
    var hooksH = hooks.length>1
      ? '<div class="hook-pick"><div class="hook-pick-lbl">'+L("Elige tu gancho","Pick your hook")+'</div>'+
        hooks.map(function(h,i){
          return '<button class="hook-opt'+(i===hi?" on":"")+'" data-act="hook-pick" data-k="'+i+'"><span class="hook-opt-n">'+(i+1)+'</span><span class="hook-opt-t">'+ESC(h)+'</span>'+(i===hi?'<span class="hook-opt-on">'+IC.check+'</span>':'')+'</button>'; }).join("")+
        '</div>'
      : '';
    // LAYOUT: en desktop son 2 columnas (CSS grid sobre .script-wrap):
    //   .script-main = el guion (hero, opciones, hooks, cuerpo)
    //   .script-side = sidebar derecho (FORMATO SUGERIDO + ¿Y ahora?)
    // En móvil .script-wrap vuelve a block → las dos cajas se apilan.
    // Badge HONESTO: el borrador se auto-guarda (opción 0), pero al cambiar opción/gancho la
    // elección queda SIN guardar hasta pulsar «Guardar guion». r._saved refleja ese estado.
    var savedBadge = r._saved
      ? '<span class="saved-tag">'+IC.check+' '+L("Guardado en Ideas robadas","Saved to Stolen ideas")+'</span>'
      : '<span class="saved-tag saved-tag--pending">'+IC.spark+' '+L("Elección sin guardar","Unsaved choice")+'</span>';
    var mainHTML = hero+
      '<div class="reveal-aha">'+IC.spark+' <span>Manifestando viralidad</span></div>'+
      '<div class="script-src"><span>Robado de <b style="color:var(--text-secondary)">@'+ESC(r.creator.handle)+'</b></span><span style="opacity:.4">·</span><span class="voice-tag">'+IC.spark+' En la voz de '+ESC(brand().name)+'</span><span style="opacity:.4">·</span>'+savedBadge+'</div>'+
      '<div class="script-acts">'+(r.url
        ? '<a class="script-act" href="'+ESC(r.url)+'" target="_blank" rel="noopener noreferrer">'+IC.eye+' '+L("Ver original","View original")+'</a>'
        : '<button class="script-act" data-act="reel-original" data-id="'+ESC(r.id)+'">'+IC.eye+' '+L("Ver original","View original")+'</button>')+
        '<button class="script-act" data-act="regen" data-id="'+ESC(r.id)+'">'+IC.repeat+' '+L("Regenerar guion","Regenerate script")+'</button></div>'+
      optTabs+hooksH+
      '<h2 class="script-hook">'+ESC(s.hook)+'</h2><div class="script-body">'+beats+'</div>'+(s.close?'<div class="script-close">'+ESC(s.close)+'</div>':'')+
      // GUARDAR la opción/gancho/formato ELEGIDOS (antes solo se persistía la opción 0).
      '<div class="script-save-row">'+
        (r._saved
          ? '<span class="script-saved-ok">'+IC.check+' '+L("Guardado — opción "+((r.optIdx||0)+1),"Saved — option "+((r.optIdx||0)+1))+'</span>'+
            // F2: puente REAL al guion recién creado — nada de «te espera en Guiones» sin link.
            '<button class="btn btn-md btn-secondary" data-act="ws-open" data-id="r:'+ESC(r.id)+'">'+L("Ver la idea →","See the idea →")+'</button>'
          : '<button class="btn btn-md btn-primary script-save-btn" data-act="save-script-choice">'+IC.check+' '+L("Guardar guion","Save script")+'</button>')+
      '</div>';
    var sideHTML = recFormatCardHTML(r)+conveyorHTML();
    return '<div class="script-wrap'+(_firstPaint?' fade-in':' no-entry')+'">'+
      '<div class="script-main">'+mainHTML+'</div>'+
      '<aside class="script-side">'+sideHTML+'</aside>'+
    '</div>';
  }
  /* v3 (mockup David · «Editor de guion»): overlay a pantalla completa con barra
     propia (‹ Guiones · /editor · estado guardado · Copiar · Marcar grabado) y dos
     columnas — documento editable (badge+fuente+mult, hook H1, barra IA, bloques
     Gancho/Desarrollo/CTA, pie con duración + teleprónter) y rail derecho (reel
     fuente, transcripción, variantes de gancho, Regenerar / Roba como un artista).
     Edición inline persistida vía window.RadarLoop.editSave (onblur). */
  function _wordCount(txt){ return (String(txt||"").trim().match(/\S+/g)||[]).length; }
  function scriptEditorHTML(){
    var g=(S.activeGuionId && guionById(S.activeGuionId))||null;
    var r=S.reel||{};
    var s=g?{hook:g.hook||"",beats:g.beats||[],close:g.close||""}:((r.script)||{hook:"",beats:[],close:""});
    var gid=g?g.id:"";
    var title=(g&&g.title)||s.hook||L("Tu guión","Your script");
    var handle=(g&&g.from)?String(g.from).replace(/^@/,""):((r.creator&&r.creator.handle)||"");
    var mult=(g&&(g.mult||g.fromMult))||r.explosionTxt||null;
    var multTxt=mult?(String(mult).replace(/×\s*$/,"")+"×"):"";
    var when=r.when||(g&&g.when)||"";
    var rec=!!(g&&g.status==="recorded");
    // bloques editables (contenteditable → editSave onblur)
    var develop=(s.beats||[]).join("\n");
    var blocks=[
      [L("Gancho · primeros 3 s","Hook · first 3 s"),"hook",s.hook],
      [L("Desarrollo","Body"),"develop",develop],
      [L("CTA · cierre","CTA · close"),"close",s.close]
    ];
    var blocksH=blocks.map(function(b){
      return '<div class="ed-block">'+
        '<div class="ed-block-head"><span class="ed-block-k">'+ESC(b[0])+'</span>'+
          '<button class="ed-rewrite" data-act="regen"'+(gid?' data-id="'+ESC(gid)+'"':'')+'>'+IC.repeat+' '+L("Reescribir","Rewrite")+'</button></div>'+
        '<p class="ed-block-body" contenteditable="true" spellcheck="false"'+(gid?' onblur="try{window.RadarLoop.editSave(\''+ESC(gid)+'\',\''+b[1]+'\',this.innerText)}catch(e){}"':'')+'>'+ESC(b[2]||"")+'</p>'+
      '</div>';
    }).join("");
    // barra IA
    var ai=[["Acortar",IC.arrL],["Más gancho",IC.spark],["Cambiar tono",IC.layers],["Más ejemplos",IC.plus]];
    var aiH=ai.map(function(a){ return '<button class="ed-ai" data-act="regen"'+(gid?' data-id="'+ESC(gid)+'"':'')+'>'+a[1]+' '+L(a[0],a[0])+'</button>'; }).join("");
    // rail · fuente — miniatura REAL del reel (r.thumb); demo cae a las demo-thumbs.
    var thumb=r.thumb||(isDemo()?(((_demoThumbs()||[])[0])||null):null);
    var thumbInner=thumb?'<img src="'+ESC(thumb)+'" alt="" loading="lazy">':'<div class="ed-src-ph" style="background:'+_galGrad(gid||handle||"src")+'"></div>';
    var srcPanel = handle ? ('<div class="ed-rail-block">'+
      '<span class="ed-rail-k">'+L("› fuente","› source")+'</span>'+
      '<div class="ed-src">'+
        '<div class="ed-src-thumb">'+thumbInner+
          (multTxt?'<span class="ed-src-mult">'+IC.bolt+' '+ESC(multTxt)+'</span>':'')+
          '<span class="ed-src-play">'+_icPlay+'</span>'+
          (r.dur?'<span class="ed-src-dur">'+ESC(r.dur)+'</span>':'')+
        '</div>'+
        '<div class="ed-src-info"><span class="ed-src-meta">@'+ESC(handle)+(when?' · '+ESC(when):'')+'</span>'+
          '<div class="ed-src-stats">'+
            (r.views?'<div><div class="ed-src-v">'+ESC(r.views)+'</div><div class="ed-src-l">views</div></div>':'')+
            (r.likes?'<div><div class="ed-src-v">'+ESC(r.likes)+'</div><div class="ed-src-l">likes</div></div>':'')+
          '</div>'+
          (r.url?'<a class="btn btn-sm btn-secondary" href="'+ESC(r.url)+'" target="_blank" rel="noopener noreferrer">'+L("Ver original","View original")+'</a>':(r.id?'<button class="btn btn-sm btn-secondary" data-act="reel-original" data-id="'+ESC(r.id)+'">'+L("Ver original","View original")+'</button>':''))+
        '</div>'+
      '</div>'+
    '</div>') : '';
    // rail · transcripción
    var txText=(S._tx&&S._tx.status==="ok"&&S._tx.text)?S._tx.text:(develop||"");
    var txPanel = txText ? ('<div class="ed-rail-block"><span class="ed-rail-k">'+L("› transcripción detectada","› transcript detected")+'</span>'+
      '<div class="ed-tx">'+ESC(txText)+'</div></div>') : '';
    // rail · variantes de gancho
    var variants=[]; variants.push(s.hook);
    if(g&&g.hooks) g.hooks.forEach(function(h){ if(h&&variants.indexOf(h)<0) variants.push(h); });
    variants=variants.filter(Boolean);
    var varH=variants.map(function(h,i){
      var sel=(i===0);
      return '<button class="ed-var'+(sel?' on':'')+'"'+(gid&&i>0?' data-act="ed-usehook" data-id="'+ESC(gid)+'" data-i="'+(i-1)+'"':'')+'>'+
        '<span class="ed-var-dot"></span><span class="ed-var-t">'+ESC(h)+'</span></button>';
    }).join("");
    var varPanel = variants.length ? ('<div class="ed-rail-block"><div class="ed-rail-head"><span class="ed-rail-title">'+L("Variantes de gancho","Hook variants")+'</span><span class="ed-rail-tag">'+L("tu voz","your voice")+'</span></div>'+varH+'</div>') : '';
    var nWords=_wordCount(s.hook)+_wordCount(develop)+_wordCount(s.close);
    var secs=Math.max(1,Math.round(nWords/2.5));
    var recBtn=rec
      ? '<button class="btn btn-md ed-rec-on" data-act="gui-toggle-rec" data-id="'+ESC(gid)+'">'+IC.check+' '+L("Grabado","Recorded")+'</button>'
      : '<button class="btn btn-md btn-primary" data-act="'+(gid?'gui-toggle-rec':'record')+'"'+(gid?' data-id="'+ESC(gid)+'"':'')+'>'+IC.mic+' '+L("Marcar grabado","Mark recorded")+'</button>';
    var copyTxt=[title,s.hook].concat(s.beats||[],[s.close]).filter(Boolean).join("\n\n");
    return '<div class="overlay ed-overlay" role="dialog" aria-modal="true" aria-label="'+L("Editor de guion","Script editor")+'">'+
      '<div class="ed-bar">'+
        '<div class="ed-bar-l">'+
          '<button class="ed-back" data-act="ed-close">'+IC.back+' '+((S._edFrom==="ideaws"&&S._wsKey)?L("Volver a la idea","Back to the idea"):L("Ideas robadas","Stolen ideas"))+'</button>'+
          '<span class="ed-crumb">/ '+L("editor","editor")+'</span>'+
          '<span class="ed-save"><span class="ed-save-dot"></span>'+L("guardado","saved")+'</span>'+
        '</div>'+
        '<div class="ed-bar-r">'+
          '<button class="btn btn-md btn-secondary" data-act="copy" data-txt="'+ESC(copyTxt)+'">'+IC.doc+' '+L("Copiar","Copy")+'</button>'+
          recBtn+
        '</div>'+
      '</div>'+
      '<div class="ed-body">'+
        '<div class="ed-doc">'+
          '<div class="ed-meta">'+
            '<span class="ed-badge'+(rec?' done':'')+'">'+(rec?IC.check+' '+L("Grabado","Recorded"):L("Por grabar","To record"))+'</span>'+
            (handle?'<span class="ed-from">'+IC.repeat+' '+L("Robado de","Stolen from")+' <b>@'+ESC(handle)+'</b></span>':'')+
            (multTxt?'<span class="ed-mult">'+ESC(multTxt)+'</span>':'')+
          '</div>'+
          '<h1 class="ed-hook" contenteditable="true" spellcheck="false"'+(gid?' onblur="try{window.RadarLoop.editSave(\''+ESC(gid)+'\',\'title\',this.innerText)}catch(e){}"':'')+'>'+ESC(title)+'</h1>'+
          '<div class="ed-aitools">'+aiH+'</div>'+
          blocksH+
          '<div class="ed-doc-foot"><span class="ed-count">~'+secs+' s '+L("al hablar","spoken")+' · '+nWords+' '+L("palabras","words")+'</span>'+
            '<button class="btn btn-md btn-secondary" data-act="chain" data-k="record">'+IC.mic+' '+L("Modo teleprónter","Teleprompter mode")+'</button></div>'+
        '</div>'+
        '<div class="ed-rail">'+
          srcPanel+txPanel+varPanel+
          '<div class="ed-rail-cta">'+
            '<button class="btn btn-lg btn-primary" data-act="regen"'+(gid?' data-id="'+ESC(gid)+'"':'')+'>'+IC.bolt+' '+L("Regenerar guion","Regenerate script")+'</button>'+
          '</div>'+
        '</div>'+
      '</div>'+
    '</div>';
  }
  function formatResultHTML(kind){
    var r=S.reel,s=r.script||{hook:"",beats:[],close:""};
    var meta={hooks:["5 hooks, listos para elegir","Tu mismo guión empieza de 5 formas. Cambia el primero y cambia todo."],carousel:["Tu carrusel, slide a slide","Desliza para ver las tarjetas. La idea del reel, ahora también en feed."],linkedin:["Tu post de LinkedIn","Mismo ángulo, registro profesional. Otro público, cero esfuerzo extra."],x:["Tu hilo de X","El guión partido en tuits que encadenan. Copia y publica."],serie:["Tu serie de 3 está lista","Tres días de contenido que se sostienen entre sí. La semana resuelta."]}[kind]||["Listo",""];
    var inner="";
    if(kind==="hooks"){ var hk=r.hooks&&r.hooks.length?r.hooks:[s.hook].concat(s.beats||[]).slice(0,5); inner='<div class="stagger">'+hk.map(function(h,i){return '<div class="hook-item"><span class="hn">'+String(i+1).padStart(2,"0")+'</span><span class="htext">'+ESC(h)+'</span><span class="copy" data-act="copy" data-txt="'+ESC(h)+'">Copiar</span></div>';}).join("")+'</div>'; }
    else if(kind==="carousel"){ var sl='<div class="slide cover"><div class="sidx">PORTADA</div><div class="stext">'+ESC(s.hook)+'</div></div>'; sl+=(s.beats||[]).map(function(b,i){return '<div class="slide"><div class="sidx">'+String(i+1).padStart(2,"0")+'/'+(s.beats.length)+'</div><div class="stext">'+ESC(b)+'</div></div>';}).join(""); sl+='<div class="slide"><div class="sidx">CIERRE</div><div class="stext">'+ESC(s.close)+'</div></div>'; inner='<div class="slides">'+sl+'</div>'; }
    else if(kind==="linkedin"){ inner='<div class="fade-in" style="font-size:15.5px;line-height:1.6;color:var(--text-primary)"><p style="margin:0 0 14px;font-weight:600">'+ESC(s.hook)+'</p>'+(s.beats||[]).map(function(b){return '<p style="margin:0 0 12px">'+ESC(b)+'</p>';}).join("")+'<p style="margin:0 0 12px">'+ESC(s.close)+'</p><p style="margin:0;color:var(--text-tertiary)">#automatización #IA #productividad</p></div>'; }
    else if(kind==="x"){ var tw=[s.hook].concat(s.beats||[],[s.close]); inner='<div class="stagger">'+tw.map(function(t,i){return '<div class="hook-item" style="align-items:flex-start"><span class="hn">'+(i+1)+'/'+tw.length+'</span><span class="htext" style="font-weight:400">'+ESC(t)+'</span></div>';}).join("")+'</div>'; }
    else if(kind==="serie"){ var days=[[r.cap,"El guión que acabas de robar — tu pieza ancla."],["El error que casi todos cometen con esto","Giro: enseña el fallo típico antes de la solución."],["Cómo lo llevé al siguiente nivel","Cierre de serie: tu resultado real + llamada a seguirte."]]; inner='<div class="stagger">'+days.map(function(d,i){return '<div class="serie-item"><div class="sday"><div class="dnum">'+(i+1)+'</div><div class="dlbl">DÍA '+(i+1)+'</div></div><div class="sinfo"><div class="stitle">'+ESC(d[0])+'</div><div class="sdesc">'+ESC(d[1])+'</div></div></div>';}).join("")+'</div>'; }
    var foot='<div class="cluster" style="margin-top:24px;gap:10px"><button class="btn btn-md btn-secondary" data-act="back-script">← Volver al guión</button>'+((kind==="hooks"||kind==="carousel")?'<button class="btn btn-md btn-primary" data-act="record">'+IC.mic+' Grábalo</button>':'')+'</div>';
    return '<div class="script-wrap fade-in"><h2 class="result-head serif">'+ESC(meta[0])+'</h2><p class="result-sub">'+ESC(meta[1])+'</p>'+inner+foot+'</div>';
  }
  // T5 (IDI): todo overlay es un diálogo accesible — role=dialog + aria-modal +
  // aria-label (el título). El foco entra al abrir y vuelve al disparador al
  // cerrar (gestión en render) y Tab no escapa al fondo (trap en onKeydown).
  function overlayShellHTML(inner,title,backAct,closeIcon,extraCls){ return '<div class="overlay'+(extraCls?' '+extraCls:'')+'" role="dialog" aria-modal="true" aria-label="'+ESC(title)+'"><div class="obar"><button class="back" data-act="'+backAct+'" aria-label="'+(closeIcon?"Cerrar":"Volver")+'">'+(closeIcon?IC.x:IC.back)+'</button><span class="otitle">'+ESC(title)+'</span></div><div class="oscroll">'+inner+'</div></div>'; }

  /* T2 (IDI): promptSheet — el sustituto de window.prompt. Un sheet (overlay)
     con label + campo + helper + error inline (primitivos .field del design
     system), validación antes de entregar y coherente en demo y prod.
     promptSheet({title,label,placeholder,helper,multiline,initial,submitLabel,
     validate,onSubmit}) — validate(v) devuelve un string de error (o nada si ok);
     onSubmit(v) recibe el valor ya validado. */
  function sheetHTML(){
    var sh=S.sheet; if(!sh) return '';
    // Modo solo-lectura: sin input, solo extraHTML (p.ej. mostrar una transcripción)
    // + un primario que cierra y un secundario opcional.
    if(sh.readonly){
      var innerR='<div class="sheet-body">'+
        (sh.extraHTML||'')+
        '<div class="sheet-actions">'+
          (sh.secondaryLabel?'<button class="btn btn-md btn-secondary" data-act="sheet-secondary">'+ESC(sh.secondaryLabel)+'</button>':'')+
          '<button class="btn btn-md btn-primary" data-act="sheet-close">'+ESC(sh.submitLabel||"Cerrar")+'</button>'+
        '</div>'+
      '</div>';
      return overlayShellHTML(innerR, sh.title||"", "sheet-close", true, "sheet");
    }
    var field = sh.multiline
      ? '<textarea class="field-textarea" id="rsSheetInput" rows="5" placeholder="'+ESC(sh.placeholder||"")+'">'+ESC(sh.initial||"")+'</textarea>'
      : '<input class="field-input" id="rsSheetInput" type="text" placeholder="'+ESC(sh.placeholder||"")+'" value="'+ESC(sh.initial||"")+'">';
    var inner='<div class="sheet-body">'+
      '<div class="field'+(sh.error?' has-error':'')+'">'+
        '<label class="field-label" for="rsSheetInput">'+ESC(sh.label||"")+'</label>'+
        field+
        (sh.helper?'<div class="field-helper">'+ESC(sh.helper)+'</div>':'')+
        (sh.error?'<div class="field-error" role="alert">'+ESC(sh.error)+'</div>':'')+
      '</div>'+
      (sh.extraHTML||'')+   // campo extra opcional (p.ej. selector de marca/cliente)
      '<div class="sheet-actions">'+
        '<button class="btn btn-md btn-ghost" data-act="sheet-close">Cancelar</button>'+
        // Acción secundaria opcional (p.ej. «Desarrollar ahora» que CUESTA créditos),
        // a la izquierda de la primaria para que la primaria gratis sea la prominente.
        (sh.secondaryLabel?'<button class="btn btn-md btn-secondary" data-act="sheet-secondary">'+ESC(sh.secondaryLabel)+'</button>':'')+
        '<button class="btn btn-md btn-primary" data-act="sheet-submit">'+ESC(sh.submitLabel||"Aceptar")+'</button>'+
      '</div>'+
    '</div>';
    return overlayShellHTML(inner, sh.title||"", "sheet-close", true, "sheet");
  }
  function promptSheet(opts){
    S.sheet={ title:opts.title, label:opts.label, placeholder:opts.placeholder, helper:opts.helper,
      multiline:!!opts.multiline, initial:opts.initial||"", submitLabel:opts.submitLabel,
      secondaryLabel:opts.secondaryLabel||null, extraHTML:opts.extraHTML||null,
      readonly:!!opts.readonly,
      _validate:opts.validate||null, _readExtra:opts.readExtra||null,
      _onSubmit:opts.onSubmit||null, _onSecondary:opts.onSecondary||null, error:null };
    render();
    var inp=document.getElementById("rsSheetInput"); if(inp) inp.focus();
  }
  function closeSheet(){ S.sheet=null; render(); }
  // Lee+valida el input del sheet y, si pasa, lo cierra y ejecuta `cb(valor, extra)`.
  // `extra` se lee ANTES de desmontar el sheet (p.ej. el valor del selector de marca).
  function _resolveSheet(cb){
    var sh=S.sheet; if(!sh) return;
    var inp=document.getElementById("rsSheetInput"); var v=inp?inp.value:"";
    var err=sh._validate?sh._validate(v):null;
    if(err){ sh.error=err; sh.initial=v; render(); var i2=document.getElementById("rsSheetInput"); if(i2) i2.focus(); return; }
    var extra=sh._readExtra?sh._readExtra():null;
    S.sheet=null; render();
    if(cb) cb(v, extra);
  }
  function submitSheet(){ var sh=S.sheet; if(sh) _resolveSheet(sh._onSubmit); }
  function submitSheetSecondary(){ var sh=S.sheet; if(sh) _resolveSheet(sh._onSecondary); }
  /* Feedback (menú de cuenta): reportar bug o pedir mejora. Aviso de recompensa en
     créditos si el bug es real. En prod → POST /api/feedback (fable lo cablea);
     en demo solo confirma con toast. Reusa el promptSheet (textarea + tipo). */
  function openFeedback(){
    S.acctMenu=false; S._fbImage=null;
    promptSheet({
      title: L("Enviar feedback","Send feedback"),
      label: L("¿Qué falla o qué te gustaría?","What's broken or what would you like?"),
      multiline: true,
      placeholder: L("Describe el bug (con los pasos para reproducirlo) o la mejora que pides…","Describe the bug (with steps to reproduce) or the improvement you want…"),
      helper: L("Cuanto más detalle, mejor podemos ayudarte.","The more detail, the better we can help."),
      extraHTML:
        '<div class="fb-reward">'+IC.spark+'<span>'+L("Si reportas un <b>bug real</b> y lo confirmamos, te <b>recompensamos con créditos</b>.","If you report a <b>real bug</b> and we confirm it, you get <b>credits as a reward</b>.")+'</span></div>'+
        '<div class="field"><label class="field-label" for="rsFbType">'+L("Tipo","Type")+'</label>'+
          '<select id="rsFbType" class="field-input"><option value="bug">'+L("Bug","Bug")+'</option><option value="idea">'+L("Idea o mejora","Idea or improvement")+'</option></select></div>'+
        '<div class="field"><label class="field-label">'+L("Adjuntar captura (opcional)","Attach a screenshot (optional)")+'</label>'+
          '<div class="fb-attach"><label class="fb-attach-btn">'+IC.plus+' '+L("Elegir imagen","Choose image")+
            '<input type="file" id="rsFbImg" accept="image/*" onchange="try{window.RadarLoop.fbImage(this)}catch(e){}" hidden></label>'+
            '<span class="fb-attach-name" id="rsFbImgName"></span></div>'+
          '<div class="fb-attach-prev" id="rsFbImgPrev"></div></div>',
      readExtra: function(){ var s=document.getElementById("rsFbType"); return {type:(s?s.value:"bug"), image:S._fbImage||null}; },
      submitLabel: L("Enviar","Send"),
      validate: function(v){ if(!v||v.trim().length<8) return L("Cuéntanos un poco más (mínimo 8 caracteres).","Tell us a bit more (min 8 characters)."); return null; },
      onSubmit: function(v, extra){ submitFeedback(v, extra); }
    });
  }
  function submitFeedback(text, extra){
    var type=(extra&&extra.type)||"bug", image=(extra&&extra.image)||null;
    if(!isDemo()){ try{ apiPost("/api/feedback",{type:type, text:text, page:S.tab, plan:S.user.plan, image_b64:image}); }catch(e){} }
    try{ if(window.posthog) window.posthog.capture("feedback_submitted",{type:type, has_image:!!image}); }catch(e){}
    S._fbImage=null;
    showToast(type==="bug"
      ? L("¡Gracias! Si confirmamos el bug, te llegan créditos de regalo.","Thanks! If we confirm the bug, credits land in your account.")
      : L("¡Gracias por la idea! Las leemos todas.","Thanks for the idea! We read them all."));
  }
  var _tpPlay='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  var _tpPause='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="9" y1="4" x2="9" y2="20"/><line x1="15" y1="4" x2="15" y2="20"/></svg>';
  var _icMirror='<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18"/><path d="M8 7 4 12l4 5"/><path d="m16 7 4 5-4 5"/></svg>';
  var _icLock='<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10" width="16" height="11" rx="2.5"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>';
  function _tpMMSS(ms){ var t=Math.floor(ms/1000),m=Math.floor(t/60),x=t%60; return (m<10?'0':'')+m+':'+(x<10?'0':'')+x; }
  function teleprompterHTML(){
    var r=S.reel||{creator:{handle:""},script:{hook:"",beats:[],close:""}};
    var s=r.script||{hook:"",beats:[],close:""};
    if(!S.tp) S.tp={ playing:false, speed:3, fontPx:34, mirror:false, recording:false, ms:0, countdown:0 };
    var tp=S.tp;
    // v3 (mockup David): bloques con kicker (Gancho · 0–3 s / Desarrollo / CTA · cierre)
    // en mono azul + cuerpo Clash. Desarrollo conserva los beats como párrafos.
    var devBeats=(s.beats||[]).map(function(b){return '<p class="tp-p">'+ESC(b)+'</p>';}).join("");
    var blocks='';
    if(s.hook) blocks+='<div class="tp-block"><span class="tp-block-k">'+L("Gancho · 0–3 s","Hook · 0–3 s")+'</span><p class="tp-p">'+ESC(s.hook)+'</p></div>';
    if(devBeats) blocks+='<div class="tp-block"><span class="tp-block-k">'+L("Desarrollo","Body")+'</span>'+devBeats+'</div>';
    if(s.close) blocks+='<div class="tp-block"><span class="tp-block-k">'+L("CTA · cierre","CTA · close")+'</span><p class="tp-p">'+ESC(s.close)+'</p></div>';
    var title=s.hook||((r.creator&&r.creator.handle)?'@'+r.creator.handle:L("tu guion","your script"));
    var cd = tp.countdown>0 ? '<div class="tp-cd"><span>'+tp.countdown+'</span></div>' : '';
    return '<div class="overlay prompter" role="dialog" aria-modal="true" aria-label="Teleprompter">'+
      // top bar
      '<div class="tp-top">'+
        '<div class="tp-top-l">'+
          '<button class="tp-editback" data-act="tp-back">'+IC.back+' '+L("Editor","Editor")+'</button>'+
          '<span class="tp-title">'+ESC(title)+'</span>'+
        '</div>'+
        '<div class="tp-top-r">'+
          '<span class="tp-pill'+(tp.recording?' rec':'')+'"><span class="tp-pill-dot"></span>'+(tp.recording?'REC':L("en pausa","paused"))+'</span>'+
          '<span class="tp-timer" id="rsTpTimer">'+_tpMMSS(tp.ms)+'</span>'+
        '</div>'+
      '</div>'+
      // stage
      '<div class="tp-stage">'+
        '<div class="tp-band"></div><div class="tp-marker">'+_tpPlay+'</div>'+
        '<div class="tp-fade top"></div><div class="tp-fade bot"></div>'+
        '<div class="tp-scroll" id="rsTpScroll"><div class="tp-text'+(tp.mirror?' mirror':'')+'" id="rsTpText" style="font-size:'+tp.fontPx+'px">'+blocks+'</div></div>'+
        cd+
      '</div>'+
      // controls
      '<div class="tp-controls">'+
        '<div class="tp-c-l">'+
          '<button class="tp-ctl'+(tp.playing?' on':'')+'" data-act="tp-play" aria-label="'+(tp.playing?'Pausa':'Play')+'">'+(tp.playing?_tpPause:_tpPlay)+'</button>'+
          '<button class="tp-ctl" data-act="tp-restart" aria-label="Reiniciar">'+IC.repeat+'</button>'+
        '</div>'+
        '<div class="tp-c-mid">'+
          '<div class="tp-grp"><span class="tp-grp-l">'+L("Velocidad","Speed")+'</span><button class="tp-mini" data-act="tp-speed" data-k="down" aria-label="Menos velocidad">−</button><span class="tp-grp-v">×'+tp.speed+'</span><button class="tp-mini" data-act="tp-speed" data-k="up" aria-label="Más velocidad">+</button></div>'+
          '<div class="tp-grp"><span class="tp-grp-l">'+L("Texto","Text")+'</span><button class="tp-mini" data-act="tp-font" data-k="down" aria-label="Texto más pequeño">A</button><span class="tp-grp-v">'+tp.fontPx+'</span><button class="tp-mini tp-mini-lg" data-act="tp-font" data-k="up" aria-label="Texto más grande">A</button></div>'+
          '<button class="tp-mirror'+(tp.mirror?' on':'')+'" data-act="tp-mirror" aria-label="Espejo">'+_icMirror+' '+L("Espejo","Mirror")+'</button>'+
        '</div>'+
        '<div class="tp-c-r">'+
          '<button class="tp-recbtn'+(tp.recording?' on':'')+'" data-act="tp-record">'+(tp.recording?'<span class="tp-recbtn-sq"></span>'+L("Detener","Stop"):'<span class="tp-recbtn-ci"></span>'+L("Grabar","Record"))+'</button>'+
          '<button class="btn btn-md tp-done" data-act="recorded">'+IC.check+' '+L("Ya lo grabé","Done recording")+'</button>'+
        '</div>'+
      '</div></div>';
  }
  // Auto-scroll + timer del teleprónter (manipulan el DOM directo → no re-render por tick).
  function tpScrollStart(){ clearInterval(S.tpScrollTimer); S.tpScrollTimer=setInterval(function(){ var el=document.getElementById("rsTpScroll"); if(!el){ clearInterval(S.tpScrollTimer); return; } el.scrollTop += (S.tp?S.tp.speed:3); if(el.scrollTop+el.clientHeight>=el.scrollHeight-2 && S.tp){ S.tp.playing=false; clearInterval(S.tpScrollTimer); render(); } },32); }
  function tpScrollStop(){ clearInterval(S.tpScrollTimer); }
  function tpTimerStart(){ clearInterval(S.tpTimer); S.tpTimer=setInterval(function(){ if(!S.tp||!S.tp.recording){ clearInterval(S.tpTimer); return; } S.tp.ms+=250; var t=document.getElementById("rsTpTimer"); if(t) t.textContent=_tpMMSS(S.tp.ms); },250); }
  function tpRecordCountdown(){
    if(!S.tp) return; S.tp.countdown=3; render();
    clearInterval(S.tpCdTimer);
    S.tpCdTimer=setInterval(function(){
      if(!S.tp){ clearInterval(S.tpCdTimer); return; }
      S.tp.countdown-=1;
      if(S.tp.countdown<=0){ clearInterval(S.tpCdTimer); S.tp.countdown=0; S.tp.recording=true; S.tp.ms=0; S.tp.playing=true; render(); tpTimerStart(); }
      else render();
    },1000);
  }
  function fillReels(){ return S.reels.slice().sort(function(a,b){return (b.explosion||0)-(a.explosion||0);}).slice(0,5); }
  function fillWeekHTML(reels,phase){
    var allDone=phase===-1;
    var head=allDone?"Tu semana está lista":"Llenando tu semana…";
    var sub=allDone?reels.length+" guiones en tu voz, guardados en Guiones. Ordénalos, descarta lo que no te valga, y graba cuando quieras.":"Robando los "+reels.length+" reels más explosivos y reescribiéndolos en tu voz, uno a uno.";
    var rows=reels.map(function(r,i){ var state=allDone||i<phase?"done":(i===phase?"run":"wait"); var stat=state==="wait"?"En cola":state==="run"?'<span class="mini-spin"></span> Reescribiendo':IC.check+' Guardado';
      return '<div class="batch-row'+(state==="done"?" is-done":"")+'"><div class="ava bava">'+ESC(r.creator.initials)+'</div><div class="binfo"><div class="bt">'+ESC(r.cap)+'</div><div class="bw">@'+ESC(r.creator.handle)+' · '+ESC(r.explosionTxt!=null?r.explosionTxt:"")+'×</div></div><div class="bstat '+state+'">'+stat+'</div></div>'; }).join("");
    var foot=allDone?'<div class="cluster" style="margin-top:22px;gap:10px"><button class="btn btn-md btn-primary" data-act="fw-guiones">'+IC.doc+' Ver mis guiones</button><button class="btn btn-md btn-secondary" data-act="fw-record">Grabar el primero ahora</button></div>':'';
    return '<div class="batch fade-in"><h2 class="result-head serif" style="font-size:28px">'+ESC(head)+'</h2><p class="result-sub">'+ESC(sub)+'</p>'+rows+foot+'</div>';
  }

  /* ════════════════════════════════════════════════════════════════
     RENDER maestro
     ════════════════════════════════════════════════════════════════ */
  function render(){
    var el=root(); if(!el) return;
    el.className="rs app "+(S.device==="mobile"?"rs--mobile":"rs--desktop");
    el.setAttribute("data-theme",(document.documentElement.getAttribute("data-theme")==="light"?"light":"dark"));
    // Fix review (T2/T6): si hay un sheet abierto con texto sin enviar, consérvalo —
    // un render de fondo (p.ej. robo en background al resolver) no debe borrarlo.
    if(S.sheet){ var _si=document.getElementById("rsSheetInput"); if(_si) S.sheet.initial=_si.value; }
    // growth-2: conserva el handle a medio teclear ante un render de fondo.
    if(S.onb && S.onb.step==="handle"){ var _oi=document.getElementById("rsOnbHandle"); if(_oi) S.onb.handle=_oi.value; }
    // Ideas robadas: las notas a medio escribir sobreviven a un render de fondo.
    if(S.view==="ideaws" && S._wsKey){ var _wn=document.getElementById("rsWsNotes"); if(_wn){ S._notesByKey=S._notesByKey||{}; S._notesByKey[S._wsKey]=_wn.value; } }
    if(S.onb && S.onb.step==="seed"){ var _si=document.getElementById("rsOnbSeed"); if(_si) S.onb.seed=_si.value; }
    // Fix review (T4/a11y): #rsToast/#rsErr deben ser nodos PERSISTENTES — una región
    // aria-live solo se anuncia cuando su contenido MUTA estando ya en el DOM. Si se
    // recrean en cada innerHTML, el patrón render()+showToast() no se anuncia. La vista
    // se pinta en #rsView (display:contents → transparente al layout) y el toast/error
    // viven fuera, estables.
    var view=document.getElementById("rsView");
    if(!view || view.parentNode!==el){
      el.innerHTML='<div class="rs-view" id="rsView"></div>'+
        // Cerebro del onboarding (v=136): canvas PERSISTENTE (hermano de #rsView → no se
        // repinta) para que ReelBrain crezca paso a paso sin reiniciar en cada render.
        '<div class="onb-brain-host" id="rsBrainHost" style="display:none" aria-hidden="true"><canvas id="rsBrainCanvas"></canvas><span class="onb-bh-logo">'+IC.bolt+' ReelScript</span><div class="onb-bh-cap"><div class="onb-bh-tag" id="rsBrainTag"></div><div class="onb-bh-title" id="rsBrainTitle"></div></div></div>'+
        // Host estable (hermano de #rsView, no se re-renderiza) para montar dentro
        // una sección legacy (Analizar/Configuración) reparentando su contenedor.
        '<div class="rs-legacy" id="rsLegacy" style="display:none"></div>'+
        '<div class="rs-toast" id="rsToast" role="status" aria-live="polite"><span class="tdot"></span><span id="rsToastMsg"></span><button class="rs-toast-act" id="rsToastAct" style="display:none"></button></div>'+
        '<div class="rs-toast rs-err" id="rsErr" role="alert" aria-live="assertive"><span class="tdot err"></span><span id="rsErrMsg"></span><button class="rs-err-x" data-act="err-close" title="Cerrar" aria-label="Cerrar el error">'+IC.x+'</button></div>';
      view=document.getElementById("rsView");
    }
    // «Ideas robadas» absorbe la vieja sección Ideas: cualquier ruta/deep-link
    // heredado (/profile/ideas, ?t=ideas) aterriza en la nueva sección.
    if(S.tab==="ideas") S.tab="guiones";
    // B3: Portfolio eliminado (redundante) — el Radar es la pantalla principal. Cualquier
    // ruta/deep-link/entrada a portfolio se normaliza al Radar (sin romper rutas).
    if(S.tab==="portfolio") S.tab="dashboard";
    // Equipo oculto temporalmente (ver TODO en railHTML): cualquier deep-link a
    // team se normaliza al Radar para no dejar una vista huérfana.
    if(S.tab==="team") S.tab="dashboard";
    // Mata el house-tour si se coló por timing durante el onboarding/offer/carga (bug:
    // en incógnito el onboarding monta tarde y el auto-tour de 1500ms arranca encima).
    if(showOnboarding() || S._onbWaiting || S.onbStealOffer){
      try{ var _tov=document.querySelector('.tour-overlay'); if(_tov && _tov.style.display!=='none' && typeof window.endTour==="function") window.endTour(); }catch(e){}
    }
    // A) Onboarding v2 = pantalla dedicada (sin rail/cmd/statbar): el radar vacío
    // (0 rivales · 0 reels) NO se ve detrás. Short-circuit antes de montar la isla.
    if(showOnboarding()){
      view.innerHTML=onboardingScreenHTML();
      mountOnbBrain();   // v=136: cerebro split-screen (canvas persistente, crece por paso)
      onbView();   // PostHog: 1 evento "viewed" por paso
      var _of=document.getElementById("rsOnbHandle")||document.getElementById("rsOnbNiche")||document.getElementById("rsOnbTagInput");
      if(_of && document.activeElement!==_of){ try{ _of.focus(); }catch(e){} }
      // Chip «cuenta válida» reactivo al teclear (sin re-render): toggle directo en DOM.
      var _hi=document.getElementById("rsOnbHandle");
      if(_hi){ _hi.addEventListener("input", function(){
        var v=this.value.replace(/^@+/,""); var ok=/^[a-zA-Z0-9._]{2,30}$/.test(v);
        var box=this.closest(".onb-handle"); var chip=box&&box.querySelector(".onb-handle-ok");
        if(chip) chip.classList.toggle("on", ok);
        // HEAD START: precarga la foto de IG (Apify) cuando el handle es válido, con debounce
        // → para cuando llegue a «este eres tú» la foto ya está (el scrape tarda ~5-15s).
        clearTimeout(S._avaT); if(ok && !isDemo()){ S._avaT=setTimeout(function(){ onbFetchAvatar(v); }, 700); }
      }); }
      return;
    }
    // v=137: carga post-onboarding y «¿quieres robar este?» a PANTALLA COMPLETA — sin rail
    // ni barra superior (short-circuit antes de montar la chrome, como el onboarding).
    if(S._onbWaiting || S.onbStealOffer){
      unmountOnbBrain();
      view.innerHTML='<div class="onb-fs">'+(S._onbWaiting?onbWaitHTML():onbStealOfferHTML())+'</div>';
      if(S.onbStealOffer) try{ mountCofre(); }catch(e){}   // arranca la avalancha del cofre
      return;
    }
    unmountOnbBrain();   // v=136: fuera del onboarding → destruir el cerebro (mata rAF/RO)
    var html='';
    html+=railHTML()+'<div class="work">'+cmdHTML();
    if(S.tab==="portfolio") html+=(isAgency()?portfolioHTML():dashboardHTML());
    else if(S.tab==="dashboard") html+=dashboardHTML();
    else if(S.tab==="guiones") html+=guionesHTML();
    else if(S.tab==="metrics") html+=metricsHTML();
    else if(S.tab==="analizar") html+=analizarHTML();
    else if(S.tab==="leaderboard") html+=leaderboardPageHTML();
    else if(S.tab==="brain") html+=brainHTML();
    else if(S.tab==="settings") html+=ajustesHTML();   // v3: Ajustes como página isla (mockup David)
    else if(S.tab==="team") html+=teamHTML();
    html+='</div>';  // /.work
    if(S.view==="gen") html+=overlayShellHTML(generatingHTML(S.genKind),"Trabajando…","close-feed",true);
    else if(S.view==="script") html+=overlayShellHTML(scriptRevealHTML(),"Tu guión, en tu voz","close-feed",true);   // reveal «aha» tras robar
    else if(S.view==="editor") html+=scriptEditorHTML();   // v3: editor completo (mockup David), desde «Abrir guion»
    else if(S.view==="result") html+=overlayShellHTML(formatResultHTML(S.resultKind),"Listo","back-script",false);
    else if(S.view==="perf") html+=overlayShellHTML(guiPerfHTML(),"Rendimiento del guion","close-feed",true);
    // teleprompter unificado (David 06/07): S.view==="prompter" RETIRADO — el loop abre el
    // teleprompter REAL vía window.tpOpen (openLoopPrompter). teleprompterHTML() (mock) queda
    // muerto y sin invocar; no se borra su def por compartir helpers (_icLock) con otras vistas.
    else if(S.view==="fillweek") html+='<div class="overlay" role="dialog" aria-modal="true" aria-label="Llena mi semana"><div class="obar"><button class="back" data-act="close-feed" aria-label="Cerrar">'+IC.x+'</button><span class="otitle">Llena mi semana</span></div><div class="oscroll" id="rsFillHost">'+fillWeekHTML(fillReels(),S._fillPhase==null?0:S._fillPhase)+'</div></div>';
    if(S.communityInfo) html+=communityInfoModalHTML();   // mini-modal «Más información» Comunidad
    if(S.levelup) html+=levelupHTML();   // pantalla de animación al subir de nivel el Cerebro
    if(S.sheet) html+=sheetHTML();   // T2: el sheet de entrada va SOBRE cualquier overlay
    // Teleprónter: preservar la posición de scroll a través del re-render (los toggles
    // de play/velocidad/texto re-pintan; sin esto el scroll saltaría a 0).
    var _tpScroll=null; if(S.view==="prompter"){ var _tpe=document.getElementById("rsTpScroll"); if(_tpe) _tpScroll=_tpe.scrollTop; }
    // Preservar el scroll del contenedor principal entre repintados (filtros, fav,
    // expandir, chips… repintan toda la isla y, sin esto, saltaría arriba del todo).
    // Solo se restaura si sigues en la MISMA pantalla (misma pestaña/vista); al
    // cambiar de tab o abrir un overlay sí empieza arriba, que es lo esperado.
    var _scKey=S.tab+"|"+(S.view||"feed")+"|"+(S.creatorFilter?S.creatorFilter.id:"");
    var _scTop=null; var _scEl=document.querySelector("#radarRoot .work .scroll"); if(_scEl) _scTop=_scEl.scrollTop;
    // Scroll del OVERLAY (guion, rendimiento…): también sobrevive a re-renders de la misma
    // vista (p.ej. llegan las referencias del formato con el guion a medio leer → sin esto,
    // el overlay saltaba arriba). Misma clave _scKey (tab|view).
    var _osTop=null; var _osEl=document.querySelector("#radarRoot .overlay .oscroll"); if(_osEl) _osTop=_osEl.scrollTop;
    view.innerHTML=html;
    if(_scTop!=null && S._scKey===_scKey){ var _scEl2=document.querySelector("#radarRoot .work .scroll"); if(_scEl2){ _scEl2.style.scrollBehavior="auto"; _scEl2.scrollTop=_scTop; _scEl2.style.scrollBehavior=""; } }
    if(_osTop!=null && S._scKey===_scKey){ var _osEl2=document.querySelector("#radarRoot .overlay .oscroll"); if(_osEl2){ _osEl2.style.scrollBehavior="auto"; _osEl2.scrollTop=_osTop; _osEl2.style.scrollBehavior=""; } }
    S._scKey=_scKey;
    if(_tpScroll!=null){ var _tpe2=document.getElementById("rsTpScroll"); if(_tpe2) _tpe2.scrollTop=_tpScroll; }
    // T4: el error persistente sobrevive a los re-render mutando el nodo estable.
    var errN=document.getElementById("rsErr"),errM=document.getElementById("rsErrMsg");
    if(errN&&errM){ if(S.errMsg){ errM.textContent=S.errMsg; errN.classList.add("show"); } else { errN.classList.remove("show"); } }
    if(S.view==="gen") startGenSteps();
    // Teleprónter: gestiona auto-scroll/timer según estado (sin re-render por tick).
    if(S.view==="prompter"){ if(S.tp&&S.tp.playing) tpScrollStart(); else tpScrollStop(); if(S.tp&&S.tp.recording) tpTimerStart(); }
    else { tpScrollStop(); clearInterval(S.tpTimer); clearInterval(S.tpCdTimer); if(S.tp){ S.tp.playing=false; S.tp.recording=false; } }
    // Cerebro 3D: monta/re-ancla al entrar en la pestaña Cerebro, pausa al salir.
    if(S.tab==="brain"){ ensureBrainNet(); ensureBrain3D(); ensureBrainTrain(); } else pauseBrain3D();
    ensureFlashCountdown();   // tic-tac del reloj de la oferta flash si está visible
    // NO disparar durante el onboarding/offer/carga (S.tab ya es "dashboard" ahí): si no,
    // al resolver hacen render() y reconstruyen el cofre → las cartas parpadean.
    var _onbBusy=(S._onbWaiting||S.onbStealOffer||showOnboarding());
    // «Sugerencias de hoy» (sección propia): creadores nuevos del nicho que petan. Carga 1
    // vez cuando ya sigues a alguien (si no, el radar entero es seed). Reemplaza el viejo
    // descubrimiento/sugerencia intercalado en el feed.
    // Carga sugerencias cuando ya sigues a alguien (comportamiento previo) O cuando el feed
    // está VACÍO (marca de nicho mudo): así el pool_status del backend puede reportar «populating»
    // y la pestaña muestra estado honesto + se llena sola, en vez de quedarse muda (contrato B1).
    var _feedEmpty=!(Array.isArray(S.reels) && S.reels.length>0);
    if(S.tab==="dashboard" && !_onbBusy && !S._stDismissed &&
       ((Array.isArray(S.tracked) && S.tracked.length>0 && !S.radarSeed) || _feedEmpty)) loadSuggestionsToday();
    // House-tour: lo arranca la ISLA la 1ª vez que aterrizas en el dashboard SIN onboarding
    // (post-cofre, o un usuario que ya onboardeó y no lo ha visto). Robusto: re-chequea los
    // targets justo antes. Sustituye al auto-start de index.html (que se colaba por timing).
    if(S.tab==="dashboard" && !_onbBusy && !S._tourArmed){
      try{
        var _seen=false; try{ _seen=localStorage.getItem("onboarding_completed")==="true"; }catch(e){}
        var _tovA=document.querySelector('.tour-overlay');
        if(!_seen && typeof window.startTour==="function" && (!_tovA || _tovA.style.display==='none')){
          S._tourArmed=true;
          setTimeout(function(){
            // F4: el tour NO arranca encima de una generación/reveal/teleprompter/sheet —
            // solo con el dashboard limpio. Si algo lo tapa, se re-arma y reintenta
            // en el siguiente render con la vía libre.
            var _clear = S.tab==="dashboard" && S.view==="feed" && !S.sheet
              && !S.onbStealOffer && !S._onbWaiting
              && !document.querySelector('#radarRoot .onb-screen')
              && !document.querySelector('#radarRoot .overlay')
              && document.querySelector('#radarRoot .rail');
            if(_clear){ try{ window.startTour(); }catch(e){} }
            else { S._tourArmed=false; }
          }, 900);
        }
      }catch(e){}
    }
    if(S.tab==="leaderboard") loadLeaderboard(); // ranking real por views: carga 1 vez
    // Sección legacy pendiente de la URL (/profile/transcriptions|settings): se abre
    // una vez que #rsLegacy ya existe (primer render). openLegacy consume el flag.
    if(S._pendingLegacy && document.getElementById("rsLegacy")){ var _pl=S._pendingLegacy; S._pendingLegacy=null; openLegacy(_pl); }
    manageOverlayFocus(el);
    // (onboarding v2: el foco del paso se maneja en el short-circuit de arriba)
  }

  /* T5 (IDI): gestión de foco de los diálogos. Al ABRIR un overlay/sheet, el foco
     entra (primer campo o el botón de cerrar). Mientras está abierto, cada
     re-render (innerHTML destruye el nodo enfocado) lo re-ancla dentro. Al
     CERRAR, el foco vuelve al elemento que lo abrió (selector capturado en
     onClick — las referencias a nodos no sobreviven al re-render, un selector sí). */
  function topOverlay(el){ var ovs=el.querySelectorAll(".overlay"); return ovs.length?ovs[ovs.length-1]:null; }
  function manageOverlayFocus(el){
    var open=(S.view && S.view!=="feed") || !!S.sheet;
    var was=!!S._overlayOpen;
    if(open){
      if(!was) S._returnSel=S._lastClickSel||null;   // recuerda el disparador al abrir
      var ov=topOverlay(el);
      if(ov && !ov.contains(document.activeElement)){
        var f=ov.querySelector("input,textarea") || ov.querySelector(".back") || ov;
        try{ f.focus(); }catch(e){}
      }
    } else if(was && S._returnSel){
      var rt=null; try{ rt=el.querySelector(S._returnSel); }catch(e){}
      if(rt){ try{ rt.focus(); }catch(e){} }
      S._returnSel=null;
    }
    S._overlayOpen=open;
  }
  // Selector estable de un botón [data-act] (para devolverle el foco tras un re-render).
  function actSelector(btn){
    var s='[data-act="'+(btn.getAttribute("data-act")||"")+'"]';
    if(btn.getAttribute("data-id")) s+='[data-id="'+btn.getAttribute("data-id")+'"]';
    if(btn.getAttribute("data-k")) s+='[data-k="'+btn.getAttribute("data-k")+'"]';
    return s;
  }

  /* ── animaciones ─────────────────────────────────────────────── */
  // T6: con S._genSlow los mensajes honestos rotan LENTO (no es teatro, es espera real).
  function startGenSteps(){ clearInterval(S.genStepTimer); var steps=S._genSlow?HONEST_MSGS:(GEN_STEPS[S.genKind]||GEN_STEPS.script),i=0; S.genStepTimer=setInterval(function(){ i=(i+1)%steps.length; var n=document.getElementById("rsGenStep"); if(n){ n.style.opacity=0; setTimeout(function(){ n.textContent=steps[i]; n.style.opacity=1; },150); } },S._genSlow?9000:700); }
  function flashSpark(delta){ var sp=document.getElementById("rsSpark"),nEl=document.getElementById("rsSparkN"); if(nEl) nEl.textContent=isTrial()?((S.user.dayLeft!=null)?S.user.dayLeft:3):((S.user.plan==="free" && !S.user.credits)?S.user.freeLeft:S.user.credits); if(sp&&delta<0){ sp.classList.add("flash"); var fly=document.createElement("span"); fly.className="spark-fly"; fly.textContent=delta; sp.appendChild(fly); setTimeout(function(){ sp.classList.remove("flash"); if(fly.parentNode) fly.parentNode.removeChild(fly); },1000); } }
  // T9 (IDI): showToast acepta una acción opcional («Deshacer») — con acción el
  // toast dura más (6s) para dar tiempo a reaccionar.
  function showToast(msg, actionLabel, actionAct){
    var t=document.getElementById("rsToast"),m=document.getElementById("rsToastMsg"),a=document.getElementById("rsToastAct");
    if(!t||!m) return;
    m.textContent=msg;
    if(a){
      if(actionLabel&&actionAct){ a.textContent=actionLabel; a.setAttribute("data-act",actionAct); a.style.display=""; }
      else { a.style.display="none"; a.removeAttribute("data-act"); }
    }
    t.classList.add("show"); clearTimeout(S.toastTimer);
    S.toastTimer=setTimeout(function(){ t.classList.remove("show"); },actionLabel?6000:2800);
  }
  // T4 (IDI): los errores NO se esfuman — persisten hasta que el usuario los
  // cierra (data-act="err-close"). S.errMsg sobrevive a los re-render.
  function showError(msg){
    S.errMsg=msg;
    var t=document.getElementById("rsErr"),m=document.getElementById("rsErrMsg");
    if(t&&m){ m.textContent=msg; t.classList.add("show"); } else { render(); }
  }
  function spend(n){ S.user.credits=Math.max(0,S.user.credits-n); }
  function bumpEco(scripts, reels){ var b=brand(); if(!b) return; b.scripts=(b.scripts||0)+(scripts||0); b.reelsAnalyzed=(b.reelsAnalyzed||0)+(reels||0); b.voice=Math.min(98,(b.voice||40)+(scripts||0)*1.5+(reels||0)); if(b.voice>=20*(b.level||1)+30) b.level=Math.min(5,(b.level||1)+1); }

  /* ════════════════════════════════════════════════════════════════
     ACCIONES
     ════════════════════════════════════════════════════════════════ */
  function switchTab(t){
    if(S.legacy) _exitLegacy();   // salir de Analizar/Configuración al cambiar de tab
    if(t==="metrics" && S.tab!=="metrics") rsTrack("metrics_viewed", {});   // panel: vista de Métricas
    S.tab=t; S.brandMenu=false; S.view="feed";
    S.creatorFilter=null; S.creatorReels=null; S.detailReelId=null;   // A+B: estados de la feed no sobreviven al cambio de tab
    // Vistas de marca (no macro/equipo) refrescan stats+feed de la marca activa.
    if(isDemo() && t!=="portfolio" && t!=="team") applyDemoBrand();
    // T2: al entrar en Cerebro, asegura la lista de asistentes fresca (loadAssistants
    // refresca la isla vía RadarLoop.refresh al resolver).
    if(t==="brain"){ try{ if(typeof loadAssistants==="function") loadAssistants(); }catch(e){} loadTracked(); }
    if(t==="analizar"){ S.analyzeErr=null; loadAnalyses(); }
    // Métricas: refresca /metrics/videos al ENTRAR (el scrape del onboarding suele
    // terminar DESPUÉS de la carga inicial → sin esto la página queda vacía hasta
    // recargar). Así, al acabar el tutorial, los datos reales ya están detrás del muro.
    if(t==="metrics"){ loadMetricsLight(); }
    // Self-heal (David 06/07): «guiones» (Ideas robadas) se hidrata SOLO por la ola diferida
    // (_loadDeferredBrandData, fire-once, .catch→null). Si esa fetch falló → 0 cards sin
    // reintento hasta recargar/ cambiar de marca. Re-pedir al ENTRAR arregla el vacío
    // transitorio (mismo patrón que metrics/analizar).
    if(t==="guiones"){ reloadScripts().then(function(){ render(); }); }
    render();
  }

  /* ── Secciones legacy reutilizadas (Analizar = #profPanelTransc, Configuración =
     #profPanelSettings). No son S.tab internos: reparentamos su contenedor de la
     chrome a un host estable de la isla (#rsLegacy), lo activamos con el JS legacy
     vía window.rsActivateLegacySection, y lo devolvemos a su sitio al salir. Así la
     isla expone el acceso sin reescribir esas vistas. ── */
  function _legacyPanelId(k){ return k==="transc" ? "profPanelTransc" : (k==="settings" ? "profPanelSettings" : (k==="assistants" ? "profPanelAssistants" : null)); }
  function mountLegacy(k){
    var host=document.getElementById("rsLegacy"); if(!host) return;
    var pid=_legacyPanelId(k); var panel=pid&&document.getElementById(pid); if(!panel) return;
    S._legacyNode=panel; S._legacyHome=panel.parentNode;   // recordar de dónde vino
    host.innerHTML='<div class="rs-legacy-bar"><button class="btn btn-sm btn-secondary" data-act="legacy-back" aria-label="Volver al radar">'+IC.back+' Volver al radar</button></div>';
    panel.style.display="";          // el chrome lo deja en display:none por defecto
    host.appendChild(panel);
    host.style.display="";
    try{ if(typeof window.rsActivateLegacySection==="function") window.rsActivateLegacySection(k); }catch(e){}
  }
  function _exitLegacy(){
    if(!S.legacy && !S._legacyNode) return;
    if(S._legacyNode){
      S._legacyNode.style.display="none";   // restaurar estado chrome (oculto)
      var home=S._legacyHome||document.getElementById("profMain")||document.body;
      home.appendChild(S._legacyNode);
      S._legacyNode=null; S._legacyHome=null;
    }
    var host=document.getElementById("rsLegacy"); if(host){ host.style.display="none"; host.innerHTML=""; }
    S.legacy=null;
  }
  function closeLegacy(){ _exitLegacy(); S.tab="dashboard"; S.view="feed"; render(); }   // "Volver al radar" → Radar (dashboard)
  function openLegacy(k){
    if(S.legacy===k) return closeLegacy();   // toggle: re-pulsar cierra
    _exitLegacy();                           // por si había otra sección legacy abierta
    S.legacy=k;
    render();                                // el rail marca el botón activo
    mountLegacy(k);                          // #rsLegacy es estable → sobrevive al render
  }
  // Cambiar de marca reinicia la foto de nivel (S._lvlSeen): otra marca = otras
  // señales — sin esto el toast de level-up dispararía en falso al saltar a una
  // marca más avanzada.
  function switchBrand(id){ if(S.brandId===id){ S.brandMenu=false; return render(); } S.brandId=id; S.brandMenu=false; S._canLvlSeen=null; loadBrandData(); }
  // Zoom de portfolio → radar de una marca. En demo no recarga (reusa el feed),
  // solo ajusta stats de la marca; en prod recarga sus datos reales.
  // En demo: ajusta stats + feed a la marca activa (cada marca ve cosas distintas).
  // En prod esto vendrá de /api/radar/stats?brand= y /api/tracked-creators/reels?brand=.
  function applyDemoBrand(){
    var b=brand();
    // Marca muda (nicho fino recién creado, pool poblándose): 0 señales/competidores → estado
    // honesto «poblando tu radar» (contrato B1). El resto de marcas limpian ese estado.
    var muda=!!(b.mute || ((b.reels||0)===0 && (b.competitors||0)===0));
    S._suggPoolStatus = muda ? "populating" : "";
    S._suggDismissed=false; S._stDismissed=false;
    if(muda){
      S.stats={competitors:0, reels_week:0, exploded_week:0, stolen_today:0};
      S.reels=[]; S.favs={};
      return;
    }
    S.stats={competitors:b.competitors||4, reels_week:b.reels||0, exploded_week:b.exploded||0, stolen_today:0};
    if(S._reelPool&&S._reelPool.length){
      var ids=S.brands.map(function(x){return x.id;}); var idx=ids.indexOf(b.id); if(idx<0) idx=0;
      var pool=S._reelPool.slice();
      var rot=pool.slice(idx%pool.length).concat(pool.slice(0,idx%pool.length));
      var n=Math.max(2,Math.min(pool.length,Math.round((b.reels||pool.length)/3)+1));
      S.reels=rot.slice(0,n);
      S.favs={}; S.reels.forEach(function(r){ if(r.fav) S.favs[r.id]=true; });
    }
  }
  function openBrand(id){
    S.brandId=id; S.brandMenu=false; S.tab="dashboard"; S.view="feed"; S.feedExpanded=false;   // micro
    if(isDemo()){ applyDemoBrand(); render(); }
    else { loadBrandData(); }
  }

  /* ── Ola Agencia B1 · CRUD de marcas ─────────────────────────────────────
     Crear / renombrar / borrar marca (= project). Datos aislados por project_id
     (el switch ya recarga con el filtro). Cap por plan (gateado en el menú +
     re-chequeado en el 403 del backend). En demo, mutación local. */
  function reloadBrands(cb){
    if(isDemo()){ if(cb) cb(); return; }
    apiGet("/api/brands").then(function(r){
      if(r.ok && r.d && Array.isArray(r.d.brands)){
        S.brands=r.d.brands;
        if(r.d.brands_cap!=null) S.brandsCap=r.d.brands_cap;
      }
      if(cb) cb();
    });
  }
  function brandCreate(){
    S.brandMenu=false;
    promptSheet({
      title:"Nueva marca", label:"Nombre de la marca o cliente",
      placeholder:"Ej. Café Aurora",
      helper:"Cada marca tiene su radar, su cerebro y sus guiones, aislados.",
      submitLabel:"Crear marca",
      validate:function(v){ if(!v.trim()) return "Ponle un nombre."; if(v.trim().length>60) return "Máximo 60 caracteres."; },
      onSubmit:function(v){
        var name=v.trim();
        if(isDemo()){ var nb={id:"demo-"+Date.now(),name:name,handle:name.toLowerCase().replace(/[^a-z0-9]/g,""),color:"#4f7cff",level:1,voice:40,reelsAnalyzed:0,scripts:0}; S.brands.push(nb); S.brandId=nb.id; S.tab="dashboard"; S.view="feed"; applyDemoBrand(); render(); return showToast("Marca «"+name+"» creada."); }
        showToast("Creando «"+name+"»…");
        apiPost("/projects",{name:name}).then(function(r){
          if(r.ok && r.d && r.d.id){
            reloadBrands(function(){ S.brandId=r.d.id; S.tab="dashboard"; S.view="feed"; S._canLvlSeen=null; loadBrandData(); showToast("Marca «"+name+"» creada."); });
          } else if(r.status===403){
            showError((r.d&&r.d.error)||"Has llegado al tope de marcas de tu plan.");
            if(typeof window.openUpgradeModal==="function"){ try{ window.openUpgradeModal("brand_limit"); }catch(e){} }
          } else { showError((r.d&&r.d.error)||"No pude crear la marca."); }
        });
      }
    });
  }
  function brandRename(id, oldName){
    S.brandMenu=false;
    promptSheet({
      title:"Renombrar marca", label:"Nuevo nombre", initial:oldName||"",
      submitLabel:"Guardar",
      validate:function(v){ if(!v.trim()) return "No puede estar vacío."; if(v.trim().length>60) return "Máximo 60 caracteres."; },
      onSubmit:function(v){
        var name=v.trim();
        var b=S.brands.filter(function(x){return x.id===id;})[0]; if(b) b.name=name;   // optimista
        render();
        if(isDemo()){ showToast("Marca renombrada."); return; }
        apiPatch("/projects/"+encodeURIComponent(id),{name:name}).then(function(r){
          if(!r.ok){ showError("No pude renombrar la marca."); reloadBrands(render); }
          else showToast("Marca renombrada.");
        });
      }
    });
  }
  function brandDelete(id, name){
    S.brandMenu=false; render();
    var go=function(){
      if(isDemo()){ S.brands=S.brands.filter(function(x){return x.id!==id;}); if(S.brandId===id){ S.brandId=S.brands[0].id; applyDemoBrand(); } render(); return showToast("Marca borrada."); }
      apiDelete("/projects/"+encodeURIComponent(id)).then(function(r){
        if(!r.ok){ return showError("No pude borrar la marca."); }
        reloadBrands(function(){
          if(S.brandId===id){ S.brandId=(S.brands[0]&&S.brands[0].id)||"default"; S._canLvlSeen=null; loadBrandData(); }
          else render();
          showToast("Marca «"+(name||"")+"» borrada.");
        });
      });
    };
    if(typeof window.confirmModal==="function"){
      window.confirmModal({ title:"Borrar «"+(name||"marca")+"»", body:"Se borrará la marca y se desvincularán sus datos. Esta acción no se puede deshacer.", confirmText:"Borrar", cancelText:"Cancelar", danger:true })
        .then(function(ok){ if(ok) go(); });
    } else { go(); }
  }
  // Toggle de plan SOLO en demo, para ver las dos experiencias.
  function setDemoPlan(k){
    var toFree=(k==="free");
    var plan=toFree?"creador":k;   // free reusa el layout de creador (1 marca) + isFree()=true
    if(S.plan===plan && (S._demoFree===true)===toFree){ return; }   // sin cambio real
    S._demoFree=toFree; S.plan=plan; S.brandMenu=false; S.view="feed"; S.feedExpanded=false; S._canLvlSeen=null;
    // Free = trial Pro de 5 días con tope de 3 guiones/día (Fathom 18/06): el free ES el trial.
    if(toFree){ S.user.trialActive=true; S.user.trialDaysLeft=5; S.user.dayLeft=3; }
    else { S.user.trialActive=false; }
    if(plan==="agencia"){ S.brands=demoBrands(); S.brandId=S.brands[0].id; S.tab="dashboard"; }   // B3: directo al Radar
    else { S.brands=[demoBrands()[0]]; S.brandId=S.brands[0].id; S.tab=toFree?"metrics":"dashboard"; applyDemoBrand(); }
    render();
  }
  /* ── A: DETALLE DEL REEL — desplegable INLINE bajo la card (mismo patrón que
     «Ver guión completo»: estado propio, sin modal). Solo un reel abierto a la
     vez (S.detailReelId). Contenido: thumb + autor + métricas + transcripción
     on-demand (misma caché que «Roba la idea»: 2ª vez gratis) + acciones (robar /
     fav / seguir autor / ver todos sus reels). S._tx = {id,status,text}. */
  function openReelDetail(id){
    if(S.detailReelId===id){ S.detailReelId=null; return render(); }   // toggle
    var r=reelById(id); if(!r) return;
    S.detailReelId=id;
    if(!S._tx || S._tx.id!==id) S._tx={id:id, status:"idle", text:""};
    render();
  }
  // Busca el reel en la feed normal y en la lista del competidor (vista B).
  function reelById(id){
    var r=S.reels.filter(function(x){return x.id===id;})[0];
    if(!r && Array.isArray(S.creatorReels)) r=S.creatorReels.filter(function(x){return x.id===id;})[0];
    if(!r && Array.isArray(S._suggToday)) r=S._suggToday.filter(function(x){return x.id===id;})[0];   // «Sugerencias de hoy»: robable sin seguir
    if(!r && S._stolenReels) r=S._stolenReels[id]||null;   // robado esta sesión: solo alcanzable desde el workspace («Robar otro guion»)
    return r;
  }
  function reelDetailHTML(r){
    if(!r) return '';
    var thumbInner=r.thumb?'<img src="'+ESC(r.thumb)+'" alt="">':'<div class="play"></div>';
    var isFav=!!S.favs[r.id];
    var mets=[["Views",r.views],["Likes",r.likes],["Explosión",(r.explosionTxt!=null?r.explosionTxt+"×":"–")],["Duración",r.dur]];
    var tx=S._tx&&S._tx.id===r.id?S._tx:{status:"idle"};
    var txBody;
    if(tx.status==="ok"){ txBody='<div class="reel-tx-text">'+ESC(tx.text)+'</div>'; }
    else if(tx.status==="loading"){ txBody='<div class="reel-tx-wait"><span class="rs-ldr"></span>Transcribiendo el audio… (~30-60s la primera vez; queda cacheada)</div>'; }
    else if(tx.status==="error"){ txBody='<div class="reel-tx-wait">No se pudo transcribir este reel. <button class="btn btn-sm btn-secondary" data-act="reel-tx" data-id="'+ESC(r.id)+'">Reintentar</button></div>'; }
    else { txBody='<button class="btn btn-md btn-secondary" data-act="reel-tx" data-id="'+ESC(r.id)+'">'+IC.doc+' Ver transcripción</button>'; }
    // @autor clicable → todos sus reels (B). Solo si conocemos su creator_id.
    var who=r.creator_id
      ? '<button class="reel-d-author" data-act="creator-reels" data-id="'+ESC(r.creator_id)+'" data-handle="'+ESC(r.creator.handle)+'" title="Ver todos los reels de @'+ESC(r.creator.handle)+'">@'+ESC(r.creator.handle)+'</button>'
      : '<b>@'+ESC(r.creator.handle)+'</b>';
    // #2 (David 05/07): métricas EN COLUMNA pegadas al vídeo (antes iban dentro de reel-d-main,
    // bajo el caption → había que hacer scroll para ver los números). Ahora se ven de un vistazo.
    var metsHTML='<div class="reel-d-mets">'+mets.map(function(m){return '<div class="pm"><div class="pm-k">'+ESC(m[0].toUpperCase())+'</div><div class="pm-v">'+ESC(String(m[1]))+'</div></div>';}).join("")+'</div>';
    var metsBlock=isFree()?compMetsLock(metsHTML):metsHTML;
    return '<div class="reel-inline reel-detail fade-in">'+
      '<div class="reel-d-top">'+
        // #2 (David 05/07): tocar el vídeo abre el reel ORIGINAL en pestaña nueva.
        '<div class="reel-d-thumb reel-d-thumb--link" data-act="reel-open" data-id="'+ESC(r.id)+'" role="button" tabindex="0" title="'+L("Abrir el reel original ↗","Open the original reel ↗")+'"><div class="thumb">'+thumbInner+'<span class="dur">'+ESC(r.dur)+'</span><span class="reel-d-open">↗</span></div></div>'+
        '<div class="reel-d-metcol">'+metsBlock+'</div>'+
        '<div class="reel-d-main">'+
          '<div class="reel-d-who"><span class="ava bava">'+ESC(r.creator.initials)+'</span>'+who+'<span class="reel-d-when">'+ESC(r.when)+'</span></div>'+
          '<p class="reel-d-cap">'+ESC(r.cap)+'</p>'+
          (r.sum?'<p class="reel-d-sum">'+ESC(r.sum)+'</p>':'')+
        '</div>'+
      '</div>'+
      '<div class="reel-d-acts">'+
        '<button class="btn btn-md btn-primary" data-act="steal" data-id="'+ESC(r.id)+'">'+IC.bolt+' Roba la idea</button>'+
        // 5 hooks DESDE EL VÍDEO DE COMPETENCIA (fuente «competidor» del trío, Fathom 18/06).
        (r.hooks?'':'<button class="btn btn-md btn-ghost" data-act="reel5hooks" data-id="'+ESC(r.id)+'" title="Saca 5 hooks de este reel ('+COST.hooks5+' créd.)">'+IC.hook+' 5 hooks</button>')+
        '<button class="iconbtn'+(isFav?" on":"")+'" data-act="fav" data-id="'+ESC(r.id)+'" title="Guardar" aria-label="'+(isFav?"Quitar de favoritos":"Guardar en favoritos")+'">'+(isFav?IC.star:IC.starO)+'</button>'+
        '<button class="btn btn-md btn-ghost" data-act="reel-follow" data-handle="'+ESC(r.creator.handle)+'">'+IC.plus+' Seguir a @'+ESC(r.creator.handle)+'</button>'+
      '</div>'+
      (r.hooks?'<div class="brain-section-t" style="margin-top:14px">'+L("5 hooks de este reel","5 hooks from this reel")+'</div>'+hooksResultHTML(r.hooks):'')+
      '<div class="brain-section-t" style="margin-top:18px">Transcripción <span class="brain-tag">lo que dice el creador</span></div>'+
      txBody+
    '</div>';
  }
  // #4 (David 06/07): DETALLE de sugerencia para PANEL LATERAL (desktop) / OVERLAY (móvil).
  // Layout dedicado (NO reusa reel-inline, que asume ancho completo y se estruja en el panel):
  //   IZQ  = vídeo (badge explosión) + @autor + título (2-3 líneas, «ver más») + Ocultar + robar-mini
  //   DCHA = métricas 2×2 (Explosión·Views·Likes·Comentarios) + TRANSCRIPCIÓN + «Roba la idea» grande
  function suggDetailHTML(r){
    if(!r) return '';
    var exp=(r.explosionTxt!=null?r.explosionTxt+"×":"–");
    var mets=[["Explosión",exp],["Views",r.views],["Likes",r.likes],["Comentarios",(r.comments!=null?r.comments:"–")]];
    var metsHTML='<div class="suggd-mets">'+mets.map(function(m){
      return '<div class="suggd-met"><div class="suggd-met-k">'+ESC(m[0].toUpperCase())+'</div><div class="suggd-met-v">'+ESC(String(m[1]!=null?m[1]:"–"))+'</div></div>'; }).join("")+'</div>';
    var tx=S._tx&&S._tx.id===r.id?S._tx:{status:"idle"};
    var txStat, txBody;
    if(tx.status==="ok"){ txStat=L("lista","ready"); txBody='<div class="suggd-tx-text">'+ESC(tx.text)+'</div>'; }
    else if(tx.status==="loading"){ txStat=L("transcribiendo…","transcribing…"); txBody='<div class="suggd-tx-wait"><span class="rs-ldr"></span>'+L("Transcribiendo el audio… (~30-60s la 1ª vez; queda cacheada)","Transcribing audio… (~30-60s first time; then cached)")+'</div>'; }
    else if(tx.status==="error"){ txStat=L("no disponible","unavailable"); txBody='<div class="suggd-tx-wait">'+L("No se pudo transcribir este reel.","Couldn't transcribe this reel.")+' <button class="btn btn-sm btn-secondary" data-act="reel-tx" data-id="'+ESC(r.id)+'">'+L("Reintentar","Retry")+'</button></div>'; }
    else { txStat=L("pulsa para verla","tap to view"); txBody='<button class="btn btn-md btn-secondary suggd-tx-btn" data-act="reel-tx" data-id="'+ESC(r.id)+'">'+IC.doc+' '+L("Ver transcripción","View transcript")+'</button>'; }
    var cap=r.cap||"", capLong=cap.length>110, capFull=(S._suggCapFull===r.id);
    var capHTML=cap?'<div class="suggd-cap'+((capLong&&!capFull)?' clamp':'')+'">'+ESC(cap)+'</div>'+
      (capLong?'<button class="suggd-cap-more" data-act="sugg-cap-more" data-id="'+ESC(r.id)+'">'+(capFull?L("ver menos","less"):L("ver más","more"))+'</button>':''):'';
    var who=(r.creator&&r.creator.handle)?'@'+ESC(r.creator.handle):'';
    return '<div class="suggd"><div class="suggd-cols">'+
      '<div class="suggd-l">'+
        '<div class="suggd-video" style="background:'+_galGrad(r.id)+'" data-act="reel-open" data-id="'+ESC(r.id)+'" role="button" tabindex="0" title="'+L("Abrir el reel original ↗","Open original reel ↗")+'">'+
          '<span class="suggd-exp">'+IC.spark+' '+ESC(exp)+'</span>'+
          (r.thumb?'<img src="'+ESC(r.thumb)+'" alt="">':'')+
          '<span class="suggd-play">'+_icPlay+'</span>'+
          '<span class="suggd-open">↗</span>'+
        '</div>'+
        '<div class="suggd-who">'+who+'</div>'+
        capHTML+
        '<div class="suggd-lacts">'+
          '<button class="suggd-hide" data-act="close-reel-detail">'+L("Ocultar","Hide")+'</button>'+
          '<button class="suggd-robmini" data-act="steal" data-id="'+ESC(r.id)+'" aria-label="'+L("Roba la idea","Steal the idea")+'" title="'+L("Roba la idea","Steal the idea")+'">'+IC.bolt+'</button>'+
        '</div>'+
      '</div>'+
      '<div class="suggd-r">'+
        metsHTML+
        '<div class="suggd-txwrap">'+
          '<div class="suggd-tx-k">'+L("TRANSCRIPCIÓN","TRANSCRIPT")+' <span class="suggd-tx-st">· '+txStat+'</span></div>'+
          txBody+
        '</div>'+
        '<button class="btn btn-primary suggd-steal" data-act="steal" data-id="'+ESC(r.id)+'">'+IC.bolt+' '+L("Roba la idea","Steal the idea")+'</button>'+
      '</div>'+
    '</div></div>';
  }
  // Una card + su detalle inline si está abierto (se usa en feed y en vista de competidor).
  function reelRowHTML(r){
    return reelCardHTML(r)+(S.detailReelId===r.id?reelDetailHTML(r):'');
  }
  /* v3 (mockup David «Más señales · N»): lista de las señales restantes (las que no
     son la oportunidad destacada) con cabecera + toggle «Ver las N oportunidades».
     Reusa reelCardHTML (fila .row con explosión verde, fav y «Roba la idea»). */
  function radarSignalsListHTML(rest){
    if(!rest || !rest.length) return '';
    var shown = S.feedExpanded ? rest : rest.slice(0,5);
    var rows = shown.map(reelRowHTML).join("");
    var moreToggle = (!S.feedExpanded && rest.length>5)
      ? '<div class="signals-more"><button class="see-all" data-act="expand-feed">'+IC.repeat+' '+L("Ver las "+rest.length+" oportunidades","See all "+rest.length+" opportunities")+'</button></div>'
      : '';
    // v3 (mockup David): cabecera «Más señales · N» + pestañas Explotando/Recientes/
    // Favoritos (S.filter) + «Añadir competidor» dashed.
    var f=S.filter||"explosion";
    var tab=function(k,label,icon){ return '<button class="sig-tab'+(f===k?" on":"")+'" data-act="filter" data-k="'+k+'">'+(icon||"")+label+'</button>'; };
    var tabs='<div class="sig-tabs">'+
      tab("explosion",L("Explotando","Exploding"),IC.spark+' ')+
      tab("recent",L("Recientes","Recent"),"")+
      tab("fav",L("Favoritos","Favorites"),IC.starO+' ')+
      '<button class="rgal-add" data-act="add-comp">'+IC.plus+' '+L("Añadir competidor","Add competitor")+'</button>'+
    '</div>';
    return '<section class="signals"><div class="signals-head"><span class="signals-t">'+L("Más señales · "+rest.length,"More signals · "+rest.length)+'</span>'+tabs+'</div>'+
      '<div class="signals-list">'+rows+'</div>'+moreToggle+'</section>';
  }
  function loadReelTranscript(id){
    var r=reelById(id); if(!r) return;
    S._tx={id:id, status:"loading", text:""}; render();
    if(isDemo()){
      // Demo: la "transcripción" sale del guion de muestra del reel (es lo que dice).
      setTimeout(function(){
        if(!S._tx||S._tx.id!==id) return;
        var s=r.script||{}; var txt=[s.hook].concat(s.beats||[],[s.close]).filter(Boolean).join(" ");
        S._tx={id:id, status:"ok", text:txt||"Transcripción de muestra del reel (demo)."};
        if(S.detailReelId===id||S._galOpen===id) render();
      },900);
      return;
    }
    var tries=0;
    (function poll(){
      if(!S._tx||S._tx.id!==id) return;            // cerró/abrió otro reel
      apiGet("/api/competitors/reels/"+encodeURIComponent(id)+"/transcript").then(function(res){
        if(!S._tx||S._tx.id!==id) return;
        if(res.ok && res.d && res.d.transcript){ S._tx={id:id,status:"ok",text:res.d.transcript}; if(S.detailReelId===id||S._galOpen===id) render(); return; }
        if(res.ok && res.d && res.d.pending){
          if(++tries>24){ S._tx={id:id,status:"error",text:""}; if(S.detailReelId===id||S._galOpen===id) render(); return; }   // ~60s
          setTimeout(poll, 2500); return;
        }
        S._tx={id:id,status:"error",text:""}; if(S.detailReelId===id||S._galOpen===id) render();
      });
    })();
  }
  /* ── B: TODOS los reels de un competidor (sin recorte por explosión) ──
     Entradas: fila del acordeón «Tus competidores» y @autor del detalle.
     El backend /api/tracked-creators/reels ya filtra por ?creator_id=. */
  function openCreatorReels(creatorId, handle){
    if(!creatorId) return;
    S.creatorFilter={id:creatorId, handle:handle||""};
    S.creatorReels=null;   // null = cargando
    S.detailReelId=null;
    S.tab="dashboard"; S.view="feed"; render();
    apiGet("/api/tracked-creators/reels?creator_id="+encodeURIComponent(creatorId)+"&sort=recent&limit=50").then(function(res){
      if(!S.creatorFilter || S.creatorFilter.id!==creatorId) return;   // salió de la vista
      S.creatorReels=(res.ok && res.d && Array.isArray(res.d.reels))?res.d.reels.map(normReel):[];
      render();
    });
  }
  function closeCreatorReels(){ S.creatorFilter=null; S.creatorReels=null; S.detailReelId=null; render(); }
  function creatorReelsHTML(){
    var cf=S.creatorFilter;
    var head='<div class="feed-head"><span class="feed-title">Reels de @'+ESC(cf.handle)+(Array.isArray(S.creatorReels)?' <span class="ct">· '+S.creatorReels.length+'</span>':'')+'</span>'+
      '<button class="fchip ghost" data-act="creator-reels-back">← Todas las señales</button></div>';
    var body;
    if(!Array.isArray(S.creatorReels)) body='<div class="rs-empty" style="margin-top:18px"><span class="rs-ldr"></span> Cargando los reels de @'+ESC(cf.handle)+'…</div>';
    else if(!S.creatorReels.length) body='<div class="rs-empty" style="margin-top:18px">Aún no hay reels guardados de @'+ESC(cf.handle)+'. Se irán acumulando con cada refresco del radar.</div>';
    else body='<div class="feed">'+S.creatorReels.map(reelRowHTML).join("")+'</div>';
    return head+body;
  }
  function steal(id){
    var r=reelById(id); if(!r) return;   // busca en el radar Y en discover (#5)
    // #2 conversión: muro en el PICO de Flow — free sin robos → paywall justo cuando
    // hay deseo (acaba de elegir el reel). En demo lo demostramos aquí; en real lo
    // confirma el backend (free_limit_reached). El muro borroso ya enseña el valor.
    // Free = trial: tope de 3 guiones/día (Fathom 18/06). En demo lo demostramos; en real
    // lo cuenta y resetea el backend. El muro diario empuja a volver mañana o a pagar.
    if(isFree() && S.user.dayLeft!=null && S.user.dayLeft<=0){ showDailyLimit(); return; }   // tope diario free (3 robos/día)
    // Fix review (T6): si ESTE reel ya tiene un robo en vuelo (lo mandó a background
    // con X/Esc/«seguir navegando»), no relanzamos — reabrimos el orbe del que ya
    // corre. Evita guiones duplicados y, en demo, el doble descuento de crédito.
    if(S._stealInFlight===id){ S.reel=r; S.genKind="script"; S._genBg=false; S.view="gen"; render(); return; }
    S.reel=r; S.revealReel=null; S.genKind="script"; S.done={}; S.view="gen";
    // T6: token de generación — si el usuario lanza otro robo o sigue navegando,
    // este robo pasa a "background": guarda el guion y avisa, sin secuestrar la vista.
    S._genSeq=(S._genSeq||0)+1; var tok=S._genSeq;
    S._stealInFlight=id;
    S._genBg=false; S._genSlow=false; clearTimeout(S._genHonestTimer);
    render();
    // T6: a los ~8s sin respuesta, el orbe deja el teatro y habla claro.
    S._genHonestTimer=setTimeout(function(){ if(S.view==="gen" && tok===S._genSeq){ S._genSlow=true; render(); } },8000);
    ensureScript(r,function(err){
      var bg=S._genBg || tok!==S._genSeq;   // cerró el orbe, siguió navegando o lanzó otro robo
      if(tok===S._genSeq){ clearTimeout(S._genHonestTimer); S._genSlow=false; S._genBg=false; S._stealInFlight=null; }
      else if(S._stealInFlight===id) S._stealInFlight=null;   // robo superado: libera el guard de ESTE reel
      // House tour v2: si el robo del cofre acabó en error o en background, NO armar el
      // tour sobre este reveal (no existe) — el fallback del dashboard lo recoge después.
      if((err||bg) && S._tourAfterReveal) S._tourAfterReveal=false;
      if(err){
        if(bg){
          if(err==="trial_daily_limit") showDailyLimit();
          else if(err==="free_limit_reached"||err==="no_credits") showPaywall(err);
          else showError("No pude terminar tu guion. Inténtalo de nuevo.");   // persistente (T4): el usuario está en otra vista
          return;
        }
        // Solo los errores de CRÉDITO/cuota abren el muro de planes. Un fallo de
        // servidor/LLM (p.ej. OpenRouter 402, timeout, 5xx) NO es "te faltan créditos"
        // ni un upsell — sería absurdo para un usuario de pago. Mensaje honesto + reintento.
        S.view="feed"; render();
        if(err==="trial_daily_limit") showDailyLimit();
        else if(err==="free_limit_reached"||err==="no_credits") showPaywall(err);
        else showError(L("No pude generar tu guion ahora mismo. Reinténtalo en un momento.","Couldn't generate your script right now. Try again in a moment."));
        return;
      }
      // El guión generado se guarda SIEMPRE (draft) y cae en su idea robada. No se pierde nada.
      var s=r.script||{}; var gidNew=addGuion({title:s.hook, hook:s.hook, beats:s.beats, close:s.close, from:"@"+r.creator.handle, type:"guión", thumb:r.thumb||null, url:r.url||null, srcViews:r.views||"", srcLikes:r.likes||"", reelId:r.id,
        genOptions:{options:(r.options||[]), pov_text:r.povText||null, chosen:null}});   // P1: elección pendiente hasta que el usuario la vea
      S._lastStealKey="r:"+r.id;   // destino del toast «Ver la idea» si el robo acabó en background
      S._stolenReels=S._stolenReels||{}; S._stolenReels[r.id]=r;   // re-robable desde el workspace aunque salga del radar
      removeStolenReel(id);   // loop continuity (Fathom): robado → fuera del radar, entra el siguiente
      brainEmitSignals();     // capa "alimentar": el guion nuevo entra como partícula al cerebro
      startFlash();           // PEAK: la oferta flash arranca tras el PRIMER valor real (no al entrar)
      if(!isDemo() && r._sid){ var g=guionById(gidNew); if(g) g._sid=r._sid; }
      // F2: el toast del robo en background lleva un botón REAL al workspace de la idea.
      if(bg){ render(); showToast(L("Tu guion ya está listo.","Your script is ready."), L("Elegir mi versión","Pick my version"), "ws-open-last"); }   // P1: abre el reveal con la elección pendiente
      else {
        // SNAPSHOT del reel exacto que generó → el reveal es inmune a que la global
        // S.reel cambie o a que el objeto-reel se reuse/mute por otro robo.
        S.revealReel={ id:r.id, creator:r.creator, url:r.url, ig_url:r.ig_url, permalink:r.permalink,
          ig_reel_id:r.ig_reel_id, views:r.views, likes:r.likes, explosionTxt:r.explosionTxt,
          thumb:r.thumb, cap:r.cap, dur:r.dur, _sid:r._sid, _gid:gidNew,
          script:r.script, options:r.options, optIdx:r.optIdx||0, hookIdx:r.hookIdx||0,
          recFormat:r.recFormat, povText:r.povText, _saved:true };   // opción 0 ya auto-guardada
        S.activeGuionId=gidNew; S.view="script";
        // #2: ¿es su 1er guion robado? → celebración (banner héroe + lluvia al cerebro).
        var _firstSteal=firstStealPending();
        S._firstStealCelebrate=_firstSteal; if(_firstSteal) markFirstSteal();
        render();
        if(_firstSteal){ try{ brainFeast(16); }catch(e){} }
        // House tour v2 (Leo 05-jul): el robo del COFRE aterriza aquí → arranca el tour
        // SOBRE el reveal (paso 1 = «Grábalo ahora» + «Roba la siguiente señal»). 1.4s de
        // respiro para que el reveal asiente (fade-in + celebración) antes de oscurecer.
        if(S._tourAfterReveal){
          S._tourAfterReveal=false;
          var _tSeen=false; try{ _tSeen=localStorage.getItem("onboarding_completed")==="true"; }catch(e){}
          if(!_tSeen && typeof window.startTour==="function"){
            setTimeout(function(){ if(S.view==="script"){ try{ window.startTour(); }catch(e){} } }, 1400);
          }
        }
      }
      // Demo: descuento local cosmético. Prod: el backend ya cobró server-side →
      // refrescamos el saldo real (/auth/me) sin descontar local (evita doble-cobro).
      if(isDemo()){ if(isTrial()){ if(S.user.dayLeft>0) S.user.dayLeft--; } else { spend(COST.script); } bumpEco(1,1); flashSpark(-COST.script); }
      else { refreshCredits().then(function(){ flashSpark(0); }); }
    });
  }
  /* Editor · "Regenerar guion" = re-tira del mismo material por COST.regen (1 cr),
     más barato que un guión nuevo (3 cr). Demo: descuenta local y refresca. Prod:
     endpoint dedicado /scripts/<id>/regenerate (gap D1 cerrado) — cobra 1, no 3. */
  function regenInEditor(id){
    var g = guionById(id) || (S.activeGuionId && guionById(S.activeGuionId));
    if(isDemo()){
      if(!isTrial()){
        if((S.user.credits||0) < COST.regen){ return showPaywall("no_credits"); }
        spend(COST.regen);
      }
      if(g){ var ns=makeScript(g.title||g.hook||""); g.hook=ns.hook; g.title=ns.hook; g.beats=ns.beats; g.close=ns.close; }
      render(); flashSpark(-COST.regen); showToast("Guion regenerado (−"+COST.regen+" créd.).");
      return;
    }
    var sid = g && g._sid;
    if(!sid){ return showToast(L("Este guion aún no está guardado.","This script isn't saved yet.")); }
    showToast(L("Regenerando…","Regenerating…"));
    apiPost("/scripts/"+encodeURIComponent(sid)+"/regenerate", {}).then(function(r){
      if(!r.ok || !r.d || !r.d.script){ return showPaywallOrError(r); }
      if(g){ g.hook=r.d.hook||g.hook; g.title=r.d.title||g.title; g.beats=Array.isArray(r.d.body)?r.d.body:g.beats; g.close=r.d.closing||g.close; }
      applyCredits(r.d, COST.regen); render(); showToast(L("Guion regenerado.","Script regenerated."));
    });
    return;
  }
  /* Loop continuity (Fathom): al robar/descartar, el reel sale del radar y entra el
     siguiente con más explosión (sorted[0] promueve el próximo). En demo, si el feed
     se vacía, re-siembra (sensación de "siempre hay señales nuevas"). */
  function _demoRefillIfEmpty(){ if(isDemo() && !(S.reels||[]).length && typeof applyDemoBrand==="function"){ try{ applyDemoBrand(); }catch(e){} } }
  function removeStolenReel(id){
    var i=(S.reels||[]).map(function(x){return x.id;}).indexOf(id);
    if(i>=0) S.reels.splice(i,1);
    // F3: el robado también sale de «Sugerencias de hoy» y de «Reels de @autor» —
    // si sigue visible, un re-clic con options cacheadas creaba un guion duplicado
    // local sin POST (y re-disparaba la oferta flash).
    if(Array.isArray(S._suggToday)) S._suggToday=S._suggToday.filter(function(x){return x.id!==id;});
    if(Array.isArray(S.creatorReels)) S.creatorReels=S.creatorReels.filter(function(x){return x.id!==id;});
    _demoRefillIfEmpty();
  }
  function reelDismiss(id){
    var i=(S.reels||[]).map(function(x){return x.id;}).indexOf(id); if(i<0) return;
    S._dismissed={ reel:S.reels[i], index:i }; S.reels.splice(i,1);
    if(S.detailReelId===id) S.detailReelId=null;
    _demoRefillIfEmpty(); render();
    showToast("Descartado — entra el siguiente.","Deshacer","undo-dismiss");
  }
  function undoDismiss(){
    var d=S._dismissed; S._dismissed=null; if(!d) return;
    S.reels.splice(Math.min(d.index,(S.reels||[]).length),0,d.reel); render();
    showToast("Recuperado.");
  }
  // Re-lee el saldo real de créditos del servidor y lo refleja en la pill.
  function refreshCredits(){
    return apiGet("/auth/me").then(function(r){
      var me=r.d||{};
      if(me.credits!=null) S.user.credits=me.credits;
      else if(me.credits_cents!=null) S.user.credits=Math.round(me.credits_cents/18);
      if(me.free_lifetime_left!=null) S.user.freeLeft=me.free_lifetime_left;
    });
  }
  // Muro: free agotó sus guiones del mes (o sin créditos). Abre el modal de planes.
  function showPaywall(err){
    startFlash();   // 1er muro → arranca la oferta flash de 48h
    var msg;
    if(err==="tracked_creators")      msg=L("Ya sigues a tus 2 competidores del plan free. Desbloquea más con Pro.","You're following your 2 free-plan competitors. Unlock more with Pro.");
    else if(err==="free_limit_reached") msg=L("Sin robos gratis este mes. Tu radar tiene más ideas que petan — desbloquéalas.","No free steals left this month. Your radar has more ideas blowing up — unlock them.");
    else                              msg=L("Necesitas créditos para robar esta idea.","You need credits to steal this idea.");
    showToast(msg, L("Ver planes","See plans"), "open-plans");
    // FIX free-counter: refresca el contador real tras el muro (la pill no debe
    // quedarse en "1 este mes" cuando el restante real es 0).
    if(isDemo()){ render(); } else { refreshCredits().then(function(){ render(); }); }
    if(typeof window.openUpgradeModal==="function"){ try{ window.openUpgradeModal(err==="tracked_creators"?"tracked_creators":"hazlo_mio_free_limit"); }catch(e){} }
  }
  // Tope diario del trial: aviso suave (no es "sin créditos", es "vuelve mañana").
  function showDailyLimit(){
    startFlash();   // 1er muro diario → arranca la oferta flash
    render();       // muestra el banner de la oferta en el radar
    showToast(L("Hechos tus 3 guiones de hoy — vuelve mañana o desbloquéalos subiendo de plan.",
                "Done your 3 scripts for today — come back tomorrow or unlock by upgrading."),
              L("Ver planes","See plans"), "open-plans");
  }
  function ensureScript(r,cb){
    if(isDemo()){ _demoScriptOptions(r); setTimeout(function(){cb();},1700); return; }
    if(r.options&&r.options.length){ setTimeout(function(){cb();},900); return; }   // ya generado
    _postGenerate(r, Date.now(), cb, 0);
  }
  function _postGenerate(r, t0, cb, tries){
    var _b={language:(document.documentElement.lang||"es")}; var _p=_pidOf(S.brandId); if(_p) _b.project_id=_p;
    if(r&&r.suggestion) _b.no_follow=true;   // «Sugerencias de hoy»: roba el guion SIN seguir al creador
    apiPost("/api/competitors/reels/"+encodeURIComponent(r.id)+"/generate-script", _b)
      .then(function(rr){ _handleGenResp(rr, r, t0, cb, tries); })
      .catch(function(){ r.script=r.script||{hook:r.cap,beats:[],close:""}; setTimeout(function(){cb();},800); });
  }
  function _handleGenResp(rr, r, t0, cb, tries){
    // 409 con script_id → dup reciente (60s): reusamos el guion ya generado, SIN re-cobro.
    if(rr.status===409 && rr.d && rr.d.script_id){ return fetchScriptText(rr.d.script_id, r, t0, cb); }
    // 409 in_progress → YA hay una generación de ESTE reel en curso. NO es error:
    //   con task_id → enganchamos a su polling; sin él (sync en vuelo) → esperamos y
    //   reintentamos (cuando termine, el dup-guard de 60s devuelve script_id → 0 doble cobro).
    if(rr.status===409 && rr.d && rr.d.error==="in_progress"){
      if(rr.d.task_id){ return pollScriptTask(rr.d.task_id, r, t0, cb); }
      if((tries||0)>=8){ return cb("timeout"); }
      return setTimeout(function(){ _postGenerate(r, t0, cb, (tries||0)+1); }, 2500);
    }
    if(!rr.ok){ var ec=(rr.d&&rr.d.error)||"error"; return setTimeout(function(){ cb(ec); },300); }
    // Sync (200): opciones + formato + POV vienen en la respuesta.
    if(rr.d && (rr.d.mode==="sync" || rr.d.options || rr.d.script || rr.d.result)){
      _normScriptOptions(rr.d, r);
      return setTimeout(function(){cb();},Math.max(0,1500-(Date.now()-t0)));
    }
    // Async (202): pollear /task/script/<id> hasta SUCCESS.
    if(rr.d && rr.d.task_id){ return pollScriptTask(rr.d.task_id, r, t0, cb); }
    // Respuesta inesperada → fallback al caption.
    r.script=r.script||{hook:r.cap,beats:[],close:""}; setTimeout(function(){cb();},600);
  }
  // Polling del task de generación async (AHORA todos los robos, no solo cache-miss). Máx ~180s:
  // pro tarda 40-90s y la transcripción Apify+Groq puede sumar; margen para no cortar antes de tiempo.
  function pollScriptTask(taskId, r, t0, cb){
    var tries=0, MAX=90;
    (function loop(){
      tries++;
      apiGet("/task/script/"+encodeURIComponent(taskId)).then(function(rr){
        var d=rr.d||{};
        if(d.state==="success"){
          if(d.options&&d.options.length){ _normScriptOptions(d, r); return setTimeout(cb, Math.max(0,1200-(Date.now()-t0))); }
          return fetchScriptText(d.script_id, r, t0, cb);
        }
        if(d.state==="failed"){ return cb(d.error||"error"); }
        if(tries>=MAX){ return cb("timeout"); }
        setTimeout(loop, 2000);
      }).catch(function(){ if(tries>=MAX) return cb("error"); setTimeout(loop,2000); });
    })();
  }
  // El task/dup solo devuelve script_id; el texto vive en /scripts → lo buscamos ahí.
  function fetchScriptText(sid, r, t0, cb){
    if(!sid){ r.script=r.script||{hook:r.cap,beats:[],close:""}; return cb(); }
    apiGet("/scripts").then(function(rr){
      var rows=Array.isArray(rr.d)?rr.d:[];
      var row=rows.filter(function(s){return s.id===sid;})[0];
      r._sid=sid;
      var p=row?scriptToParts(row.script):{hook:r.cap,beats:[],close:""};
      r.script=p;
      // P1: si la fila trae gen_options persistidas, el reveal recupera las 2 opciones reales.
      var _go=row&&row.gen_options;
      if(_go&&Array.isArray(_go.options)&&_go.options.length){ r.options=_go.options; if(_go.pov_text) r.povText=_go.pov_text; }
      else r.options=r.options||[{title:(row&&row.title)||"",hooks:[p.hook],body:p.beats||[],closing:p.close||"",script:(row&&row.script)||""}];
      if(row&&row.recording_format) r.recFormat=row.recording_format;
      applyScriptOption(r,0,0);
      setTimeout(function(){cb();},Math.max(0,1200-(Date.now()-t0)));
    }).catch(function(){ r.script=r.script||{hook:r.cap,beats:[],close:""}; cb(); });
  }
  function parseScript(sc,r){ if(sc&&typeof sc==="object"&&sc.hook) return sc; if(typeof sc==="string"){ var l=sc.split(/\n+/).map(function(s){return s.replace(/^▸\s*/,"").trim();}).filter(Boolean); return {hook:l[0]||r.cap,beats:l.slice(1,-1),close:l.length>1?l[l.length-1]:""}; } return {hook:r.cap,beats:[],close:""}; }
  // Fija la opción + hook activos y deriva r.script (lo que ven editor/teleprompter).
  function applyScriptOption(r, oi, hi){
    var opts=r.options||[]; oi=Math.max(0,Math.min(oi||0,opts.length-1)); hi=hi||0;
    r.optIdx=oi; r.hookIdx=hi;
    var o=opts[oi]; if(!o){ return; }
    var hooks=o.hooks||[]; var hook=hooks[hi]||hooks[0]||((o.script||"").split("\n")[0])||r.cap;
    r.script={ hook:hook, beats:o.body||[], close:o.closing||"" };
  }
  // GUARDAR la opción/gancho/formato ELEGIDOS (antes solo se persistía la opción 0): actualiza
  // el guion local y, en prod, hace PATCH del script ya creado (mismo texto plano que el backend).
  function saveScriptChoice(){
    var r=S.revealReel||S.reel; if(!r) return;
    var s=r.script||{};
    var g=guionById(r._gid||S.activeGuionId);
    if(g){ g.title=s.hook||g.title; g.hook=s.hook||g.hook; if(s.beats) g.beats=s.beats; if(s.close!=null) g.close=s.close; if(r.recFormat) g.recFormat=r.recFormat; }
    if(g) persistChosen(g, r.optIdx||0);   // P1: la elección queda cerrada y persistida
    r._saved=true; render();
    if(isDemo()){ return showToast(L("Guion guardado en Guiones.","Script saved to your Scripts.")); }
    var sid=r._sid||(g&&g._sid);
    if(sid){
      var flat=[s.hook].concat(s.beats||[]).concat(s.close?[s.close]:[]).filter(Boolean).join("\n");
      apiPatch("/scripts/"+encodeURIComponent(sid), {title:(s.hook||"").slice(0,80), script:flat, recording_format:r.recFormat||null})
        .then(function(rr){ if(!rr||!rr.ok){ r._saved=false; render(); showError(L("No pude guardar el guion. Inténtalo de nuevo.","Couldn't save the script. Try again.")); } });
    }
    showToast(L("Guion guardado en Guiones.","Script saved to your Scripts."));
  }
  // F5: persiste título+texto de un guion editado (PATCH /scripts, mismo texto plano
  // que guarda el backend). Debounce por guion — onblur puede disparar varios campos
  // seguidos. El badge del editor deja de mentir: «guardando…» → «guardado».
  var _edSaveTimers={};
  function _edSaveBadge(){
    var n=document.querySelector("#radarRoot .ed-save"); if(!n) return;
    n.innerHTML='<span class="ed-save-dot"'+(S._edSaving?' style="background:var(--warning-fg,#f5a623)"':'')+'></span>'+(S._edSaving?L("guardando…","saving…"):L("guardado","saved"));
  }
  function persistGuionText(g){
    if(!g || isDemo() || !g._sid) return;   // demo/sin fila backend → edición solo local (como antes)
    S._edSaving=true; _edSaveBadge();
    clearTimeout(_edSaveTimers[g.id]);
    _edSaveTimers[g.id]=setTimeout(function(){
      var flat=[g.hook].concat(g.beats||[]).concat(g.close?[g.close]:[]).filter(Boolean).join("\n");
      apiPatch("/scripts/"+encodeURIComponent(g._sid), {title:(g.title||g.hook||"").slice(0,80), script:flat})
        .then(function(rr){
          S._edSaving=false; _edSaveBadge();
          if(!rr||!rr.ok) showError(L("No pude guardar tu edición — reintenta o copia el texto.","Couldn't save your edit — retry or copy the text."));
        });
    },800);
  }
  // Normaliza la respuesta (sync o async) con opciones/formato/POV → estado del reel.
  function _normScriptOptions(d, r){
    var opts=Array.isArray(d.options)?d.options.filter(function(o){return o&&(o.script||o.hooks);}):[];
    if(opts.length){ r.options=opts; }
    else { // sin opciones (compat) → 1 opción derivada del texto plano
      var p=parseScript(d.script||d.result||"",r);
      r.options=[{title:d.title||"",hooks:[p.hook],body:p.beats||[],closing:p.close||"",script:d.script||d.result||""}];
    }
    if(d.recording_format) r.recFormat=d.recording_format;
    if(d.pov_text) r.povText=d.pov_text;
    r._sid=d.script_id||r._sid;
    applyScriptOption(r,0,0);
  }
  // Demo: 2 opciones + 3 hooks + formato (+ POV) para previsualizar la UX sin LLM real.
  function _demoScriptOptions(r){
    if(r.options&&r.options.length) return;
    var base=(r.script&&r.script.hook)?r.script:parseScript(r.cap||"Tu idea",r);
    var cap=(r.cap||base.hook||"esto").replace(/\.$/,"");
    var mk=function(hooks,beats,close){ return {title:hooks[0].slice(0,60),hooks:hooks,body:beats,closing:close,
      script:hooks[0]+"\n"+beats.join("\n")+"\n"+close}; };
    r.options=[
      mk([cap+".","Nadie te cuenta esto sobre "+cap.toLowerCase()+".","Hice esto durante 30 días y cambió todo."],
         (base.beats&&base.beats.length?base.beats:["Te lo cuento en 3 pasos.","Paso uno: lo que casi nadie hace.","Paso dos: el detalle que lo cambia todo.","Paso tres: cómo lo cierras."]),
         (base.close||"Guárdate esto y cuéntame qué tal. / Comenta «GUION» y te paso la plantilla.")),
      mk(["Esto es lo que yo haría en tu lugar.","Para de hacer "+cap.toLowerCase()+" así.","La forma fácil vs la forma que funciona."],
         ["Empieza por el final: qué quieres que recuerden.","Quita la mitad de lo que ibas a decir.","Mete UN dato concreto, no tres vagos.","Cierra con una acción, no con un resumen."],
         "Si te sirvió, sígueme para el resto. / Mándaselo a quien lo necesita.")
    ];
    r.recFormat=r.recFormat||_recFmtDemo(r);
    if(r.recFormat==="pov" && !r.povText) r.povText="POV: por fin entiendes "+cap.toLowerCase()+" / lo que nadie te dijo / guárdalo";
    applyScriptOption(r,0,0);
  }
  // record + next = reales. El resto (hooks/carrusel/linkedin/x/serie) va deshabilitado
  // con «Próximamente» en la cinta: este guard corta cualquier disparo residual para
  // que NUNCA gasten créditos (antes en demo eran teatro con spend local).
  function chain(kind){
    if(kind==="record"){ S._tpFrom=S.view; openLoopPrompter(); return; }   // recuerda el origen (reveal/editor/workspace) para volver ahí
    // P4: «Roba la siguiente señal» — salta al siguiente reel del radar y lo roba sin
    // volver al feed. Funciona en prod (real), no solo demo. Mantiene el bucle girando.
    if(kind==="next"){
      var nx=feedReels().filter(function(x){ return !(S.reel && x.id===S.reel.id); })[0];
      if(!nx){ S.view="feed"; render(); showToast(L("Has vaciado tu radar — entran señales nuevas pronto.","You've cleared your radar — fresh signals land soon.")); return; }
      steal(nx.id); return;   // steal: gen → guarda → reveal + continuidad (removeStolenReel)
    }
  }
  function recorded(){
    // Cierra el loop (momento 4): marca el guión activo como grabado.
    var g=S.activeGuionId?guionById(S.activeGuionId):null;
    if(g){ g.status="recorded"; persistRecStatus(g); }
    S.done.record=true; if(S.stats) S.stats.stolen_today+=1; S.activeGuionId=null; S._tpFrom=null;
    // F10: nada de forzar el dashboard — aterriza en el workspace de la idea del guion
    // recién grabado (ahí están el estado, las notas y el «roba otro»). Sin idea → donde estabas.
    var k=g&&guionGroupKey(g);
    if(k){ S._wsKey=k; S.tab="guiones"; S.view="ideaws"; }
    else { S.view="feed"; }
    render();
    if(!maybeUpgradeNudge("recorded"))   // growth-3: nudge tras éxito (no pisa el toast propio si no aplica)
      showToast(L("Grabado ✓ — marcado en tu idea.","Recorded ✓ — marked on your idea."));
  }
  // #teleprompter unificado (David 06/07): el LOOP usa el teleprompter REAL (index.html,
  // window.tpOpen — cámara + grabación de verdad) en vez del mock de la isla. Preserva el
  // retorno del loop vía onDone: «Ya lo grabé» del real → recorded() (marca grabado + workspace).
  function _tpTextOf(s){ s=s||{}; return [s.hook].concat(s.beats||[],[s.close]).filter(Boolean).join("\n\n"); }
  function openLoopPrompter(){
    var r=S.reel||{}; var txt=_tpTextOf(r.script);
    if(typeof window.tpOpen==="function"){ window.tpOpen(txt, {fromLoop:true, onDone:function(){ recorded(); }}); return; }
    showError(L("No pude abrir el teleprompter. Recarga la página.","Couldn't open the teleprompter. Reload the page."));   // el real (index.html) no cargó — no debería pasar
  }
  /* growth-3 · PAYWALL CONTEXTUAL — el research dice que el paywall convierte
     MEJOR después del primer éxito, nunca antes. Este nudge SOLO se dispara
     tras una acción de éxito real (copiar el guion / marcarlo grabado) de un
     usuario FREE, una sola vez (localStorage). No bloquea, no castiga: es un
     toast con acción que abre el modal de planes. Nunca aparece antes del valor.
     Devuelve true si mostró el nudge (para no pisar otros toasts). */
  /* reverse-trial: watermark "Hecho con ReelScript" en exports SOLO para free
     post-trial (S.user.watermark de /auth/me). Trial y pago exportan limpio.
     En demo nunca (no rompe el harness). */
  function withWatermark(txt){
    txt = txt || "";
    if(isDemo() || !S.user || !S.user.watermark) return txt;
    return txt + "\n\n— Hecho con ReelScript · reelscript.net";
  }
  function maybeUpgradeNudge(trigger){
    try{
      if(isDemo()) return false;
      if((S.user&&S.user.plan)!=="free") return false;
      if(localStorage.getItem("rs_upsell_shown")==="1") return false;
      localStorage.setItem("rs_upsell_shown","1");
      try{ if(window.track&&window.track.featureUsed) window.track.featureUsed({feature:"upgrade_nudge",action:"shown"}); }catch(e){}
      try{ if(window.posthog) window.posthog.capture("upgrade_nudge_shown",{trigger:trigger}); }catch(e){}
      showToast("Tu primer guion en tu voz, listo. Con Creador creas sin límite →","Ver Creador","upsell-nudge");
      return true;
    }catch(e){ return false; }
  }
  // Prod: persiste el estado de grabación del guion (PATCH /scripts/<id>). La isla
  // usa draft|recorded|discarded; el backend pending|recorded|discarded (draft→pending).
  function persistRecStatus(g){
    if(g && g.status==="recorded") rsTrack("script_recorded", {script_id:(g&&g._sid)||null});   // panel: guion grabado
    if(isDemo() || !g || !g._sid) return;
    var rs=(g.status==="recorded")?"recorded":(g.status==="discarded"?"discarded":"pending");
    apiPatch("/scripts/"+encodeURIComponent(g._sid), {recording_status:rs});
  }
  function startFillWeek(){
    var reels=fillReels();
    S._fillGuionIds=[]; S._fillResult=null; S._fillErr=null; S.view="fillweek"; S._fillPhase=0; render();
    if(isDemo()){ var fw=Math.min(COST.fillweek, reels.length*COST.script); spend(fw); bumpEco(reels.length,reels.length); flashSpark(-fw); runFillPhase(); return; }
    // Prod: dispara el lote real (/reels/steal-batch). La animación corre en
    // paralelo; al completar la fase, aterrizamos los guiones REALES del backend.
    apiPost("/reels/steal-batch",{count:Math.max(1,reels.length)||5}).then(function(r){
      if(!r.ok || !r.d || !Array.isArray(r.d.scripts)){
        S._fillErr=r;
        // No abortamos la animación a media; al cerrar fase, mostramos el error.
        return;
      }
      S._fillResult=r.d; applyCredits(r.d, r.d.scripts.length);
    });
    runFillPhase();
  }
  function runFillPhase(){
    var reels=fillReels(); clearTimeout(S.fillTimer);
    S.fillTimer=setTimeout(function(){
      if(S._fillPhase>=reels.length){
        // Prod: si el lote falló, salimos al feed con el error (no dejamos guiones a medias).
        if(!isDemo() && S._fillErr){ var er=S._fillErr; S._fillErr=null; S.view="feed"; S._fillPhase=null; render(); return showPaywallOrError(er); }
        // Prod: si el lote aún no volvió, esperamos un tick más (sin completar).
        if(!isDemo() && !S._fillResult){ S.fillTimer=setTimeout(runFillPhase,400); return; }
        S._fillPhase=-1;
        // Al completar: los guiones aterrizan en Guiones (draft). El usuario
        // decide allí cuáles graba/descarta. Nada se pierde.
        if(!S._fillGuionIds.length){
          if(!isDemo() && S._fillResult){
            // Guiones REALES del backend (ya persistidos en scripts; llevan _sid).
            S._fillGuionIds=S._fillResult.scripts.map(function(s){
              var gidL=addGuion({title:s.hook||s.from||"Guión", hook:s.hook||"", beats:s.beats||[], close:s.close||"", from:s.from||null, type:"guión"});
              var g=guionById(gidL); if(g) g._sid=s.id||s.script_id||null; return gidL;
            });
          } else {
            S._fillGuionIds=reels.map(function(r){ var s=r.script||{hook:r.cap,beats:[],close:""}; return addGuion({title:s.hook||r.cap, hook:s.hook||r.cap, beats:s.beats, close:s.close, from:"@"+r.creator.handle, type:"guión"}); });
          }
        }
        updateFillHost(); return;
      }
      S._fillPhase++; updateFillHost(); runFillPhase();
    },720);
    updateFillHost();
  }
  function updateFillHost(){ var host=document.getElementById("rsFillHost"); if(host) host.innerHTML=fillWeekHTML(fillReels(),S._fillPhase==null?0:S._fillPhase); }
  function toggleFav(id){ S.favs[id]=!S.favs[id]; var m=S.favs[id]?"POST":"DELETE"; if(!isDemo()) fetch("/api/competitors/reels/"+encodeURIComponent(id)+"/favorite",{method:m,credentials:"same-origin"}).catch(function(){}); render(); }

  /* ── fábrica de ideas ────────────────────────────────────────── */
  function seedIdea(inputId, jumpToIdeas){
    var inp=document.getElementById(inputId); var txt=inp?inp.value.trim():"";
    if(!txt){ if(jumpToIdeas){ S.tab="ideas"; render(); } return; }
    if(isDemo()){
      S.ideas.unshift(makeIdea(txt, txt.length+S.ideas.length));
      if(jumpToIdeas) S.tab="ideas";
      render(); return;
    }
    // Prod: persiste como draft (develop:false) → POST /ideas. El id real vuelve
    // del backend para poder generar guiones después (/ideas/{id}/scripts/...).
    if(txt.length<5){ showToast("Escribe una idea un poco más larga."); return; }
    if(inp) inp.value="";
    if(jumpToIdeas) S.tab="ideas";
    var tmp=makeIdea(txt, txt.length+S.ideas.length); tmp._saving=true; S.ideas.unshift(tmp); render();
    apiPost("/ideas",{raw_text:txt, language:rsLang(), develop:false}).then(function(r){
      if(r.ok && r.d && r.d.id){ tmp.id=r.d.id; tmp._server=true; tmp._scriptsLoaded=true; tmp._saving=false; render(); }
      else { tmp._saving=false; showToast((r.d&&r.d.error)||"No pude guardar la idea."); render(); }
    });
  }
  // T1: captura global de ideas (bombilla de la command bar). Modal mínimo
  // reusando promptSheet (Esc cierra, Enter envía). Al desarrollar, la idea
  // aterriza en Guiones, donde vive la fábrica.
  // Captura global de ideas (bombilla). Dos caminos claros:
  //  · Primaria «Guardar idea» → GRATIS, la apunta en bruto (status draft) y la deja
  //    en Guiones › Sin desarrollar. Enter dispara esta (la segura/gratis).
  //  · Secundaria «Desarrollar ahora» → CUESTA créditos: la genera ya (5 guiones).
  function openIdeaCapture(){
    // Agency con >1 marca: selector de marca/cliente (default = la activa) para poder
    // apuntar para el cliente B mientras ves el A (visibilidad + control). Creator
    // (1 marca): sin selector, se asigna sola (IDI: no pidas elegir si solo hay 1).
    var multi = isAgency() && (S.brands||[]).length>1;
    var extraHTML = multi
      ? '<div class="field" style="margin-top:12px"><label class="field-label" for="rsSheetBrand">Marca / cliente</label>'+
          '<select class="field-input" id="rsSheetBrand">'+
            (S.brands||[]).map(function(b){ return '<option value="'+ESC(b.id)+'"'+(b.id===S.brandId?' selected':'')+'>'+ESC(b.name)+'</option>'; }).join("")+
          '</select></div>'
      : null;
    promptSheet({
      title:"Apunta una idea",
      label:"Tu idea",
      placeholder:"Una idea suelta… guárdala ahora, desarróllala cuando quieras",
      multiline:false,   // input de una línea → Enter envía (el handler salta textareas)
      helper:"Guardar es gratis. Desarrollar ahora cuesta "+COST.scripts5+" créditos.",
      submitLabel:"Guardar idea · gratis",
      secondaryLabel:"Desarrollar ahora · "+COST.scripts5+" créd.",
      extraHTML:extraHTML,
      readExtra:function(){ var s=document.getElementById("rsSheetBrand"); return { brand: s ? s.value : null }; },
      validate:function(v){ return (v||"").trim().length<5 ? "Escribe una idea un poco más larga." : null; },
      onSubmit:function(v, x){ saveIdeaRaw(v, x&&x.brand); },        // gratis
      onSecondary:function(v, x){ developIdeaNow(v, x&&x.brand); }   // cuesta créditos
    });
  }
  // T3: dejar de seguir un competidor, CON confirmación (control y libertad +
  // prevención de errores). Reusa el confirmModal global (mismo patrón que borrar
  // asistente). Optimista: lo quita de la lista y archiva en backend (204).
  function untrackCreator(tid, handle){
    var doIt=function(){
      S.tracked=(S.tracked||[]).filter(function(x){ return String(x.id)!==String(tid); });
      if(S.stats && S.stats.competitors>0) S.stats.competitors-=1;
      // FIX backlog #7: quitar TAMBIÉN sus reels del feed para que el Radar refleje el
      // cambio YA (antes seguían visibles hasta recargar la página).
      var hl=String(handle||"").toLowerCase().replace(/^@+/,"");
      if(hl && Array.isArray(S.reels)){
        S.reels=S.reels.filter(function(r){ return String((r.creator&&r.creator.handle)||"").toLowerCase().replace(/^@+/,"")!==hl; });
      }
      render();
      showToast("Dejaste de seguir a @"+handle+".");
      apiDelete("/api/tracked-creators/"+encodeURIComponent(tid)).then(function(r){
        if(!r.ok && !isDemo()){ showError("No pude dejar de seguir a @"+handle+". Reintenta."); loadTracked(); }
        else if(!isDemo()){ _refreshReelsLight(); }   // resync feed/stats/tracked desde el backend
      });
    };
    if(typeof window!=="undefined" && typeof window.confirmModal==="function"){
      window.confirmModal({ title:"Dejar de seguir", body:"¿Dejar de seguir a @"+handle+"? Sus reels dejarán de aparecer en tu Radar.", confirmText:"Dejar de seguir", cancelText:"Cancelar", danger:true })
        .then(function(ok){ if(ok) doIt(); });
    } else { doIt(); }
  }
  // GRATIS: guarda la idea en bruto (status draft) sin desarrollar ni cobrar. No
  // navega — captura sin fricción desde cualquier vista; el toast dice dónde quedó.
  // Sufijo de marca para el toast (solo agency multi-marca, para visibilidad).
  function _brandToastSuffix(bid){ var n=(isAgency() && (S.brands||[]).length>1) ? brandNameOf(bid) : null; return n?(" · "+n):""; }
  function saveIdeaRaw(txt, bid){
    txt=(txt||"").trim(); if(!txt) return;
    bid=bid||S.brandId;
    if(isDemo()){ S.ideas.unshift(makeIdea(txt, txt.length+S.ideas.length, bid)); render(); showToast("Idea guardada (gratis) · Sin desarrollar"+_brandToastSuffix(bid)+"."); return; }
    var tmp=makeIdea(txt, txt.length+S.ideas.length, bid); tmp._saving=true; S.ideas.unshift(tmp); render();
    showToast("Idea guardada (gratis) · Sin desarrollar"+_brandToastSuffix(bid)+".");
    apiPost("/ideas",{raw_text:txt, language:rsLang(), develop:false, project_id:_pidOf(bid)}).then(function(r){
      if(r.ok && r.d && r.d.id){ tmp.id=r.d.id; tmp._server=true; tmp._scriptsLoaded=true; tmp._saving=false; render(); }
      else { tmp._saving=false; showToast((r.d&&r.d.error)||"No pude guardar la idea."); render(); }
    });
  }
  // CUESTA créditos: guarda la idea y la desarrolla ya (5 guiones). Navega a Guiones
  // para ver el resultado. Reutiliza gen5scripts (cobro + persistencia reales).
  function developIdeaNow(txt, bid){
    txt=(txt||"").trim(); if(!txt) return;
    bid=bid||S.brandId;
    // Desarrollar SÍ navega a Guiones a ver el resultado → conmutamos a la marca
    // elegida para que la fábrica (filtrada por marca activa) lo muestre. La vista
    // Guiones no depende de reels/stats, así que basta con fijar S.brandId.
    if(bid && bid!==S.brandId){ S.brandId=bid; S.brandMenu=false; if(isDemo()) applyDemoBrand(); }
    if(isDemo()){
      var idea=makeIdea(txt, txt.length+S.ideas.length, bid); S.ideas.unshift(idea);
      spend(COST.scripts5); for(var i=0;i<5;i++) idea.scripts.push(makeScript(idea.text,(idea.seed||0)+i));
      bumpEco(5,0); S.tab="guiones"; render(); flashSpark(-COST.scripts5);
      showToast("Idea desarrollada · 5 guiones listos"+_brandToastSuffix(bid)+"."); return;
    }
    var tmp=makeIdea(txt, txt.length+S.ideas.length, bid); tmp._saving=true; S.ideas.unshift(tmp); S.tab="guiones"; render();
    showToast("Guardando y desarrollando"+_brandToastSuffix(bid)+"…");
    apiPost("/ideas",{raw_text:txt, language:rsLang(), develop:false, project_id:_pidOf(bid)}).then(function(r){
      if(r.ok && r.d && r.d.id){ tmp.id=r.d.id; tmp._server=true; tmp._scriptsLoaded=true; tmp._saving=false; render(); gen5scripts(tmp.id); }
      else { tmp._saving=false; showToast((r.d&&r.d.error)||"No pude guardar la idea."); render(); }
    });
  }
  function addSeedIdea(){
    var inp=document.getElementById("rsIdeaSeed2"); var txt=inp?inp.value.trim():""; if(!txt) return;
    if(isDemo()){ S.ideas.unshift(makeIdea(txt, txt.length+S.ideas.length)); render(); return; }
    if(txt.length<5){ showToast("Escribe una idea un poco más larga."); return; }
    if(inp) inp.value="";
    var tmp=makeIdea(txt, txt.length+S.ideas.length); tmp._saving=true; S.ideas.unshift(tmp); render();
    apiPost("/ideas",{raw_text:txt, language:rsLang(), develop:false}).then(function(r){
      if(r.ok && r.d && r.d.id){ tmp.id=r.d.id; tmp._server=true; tmp._scriptsLoaded=true; tmp._saving=false; render(); }
      else { tmp._saving=false; showToast((r.d&&r.d.error)||"No pude guardar la idea."); render(); }
    });
  }
  // Un "script block" de Ideas a partir de una fila de scripts del backend
  // (generate-batch / explosion). Lleva _sid para los endpoints por-guión.
  function makeScriptFromServer(s, ideaText){
    var p=scriptToParts(s.script);
    return { id:gid("sc"), _sid:s.id, hook:p.hook||s.title||"", beats:p.beats, close:p.close,
      hooks:(Array.isArray(s.alt_hooks)&&s.alt_hooks.length)?s.alt_hooks:null, savedHooks:{},
      guionId:null, saved:false, expanded:false, idea:ideaText||"", title:s.title||p.hook||"" };
  }
  function _btnLoading(btn){
    if(!btn) return function(){};
    var orig=btn.innerHTML, dis=btn.disabled;
    btn.disabled=true;
    btn.innerHTML='<span class="rs-ldr"></span>Generando…';
    return function(){ btn.disabled=dis; btn.innerHTML=orig; };
  }

  function gen5ideas(btn){
    if(isDemo()){ spend(COST.idea5); var seed=Date.now()%97; var fresh=pick(BANK_IDEAS,3,seed).map(function(t,i){return makeIdea(t,seed+i*7);}); S.ideas=fresh.concat(S.ideas); render(); flashSpark(-COST.idea5); showToast("3 ideas nuevas para expandir."); return; }
    var restore=_btnLoading(btn);
    showToast("Generando 3 ideas…");
    apiPost("/ideas/generate-batch",{count:3, project_id:S.brandId&&S.brandId!=="default"?S.brandId:null, language:rsLang()}).then(function(r){
      if(!r.ok || !r.d || !Array.isArray(r.d.ideas)){ restore(); return showPaywallOrError(r); }
      var fresh=r.d.ideas.map(function(i){ var it=normIdea(i); it.expanded=true; it._scriptsLoaded=true; it._brand=(i.project_id||S.brandId||"default"); return it; });
      S.ideas=fresh.concat(S.ideas); applyCredits(r.d, COST.idea5); render(); showToast("3 ideas nuevas para expandir.");
    });
  }
  function gen5scripts(ideaId, btn){
    var idea=findIdea(ideaId); if(!idea) return;
    if(isDemo()){ spend(COST.scripts5); for(var i=0;i<5;i++) idea.scripts.push(makeScript(idea.text,(idea.seed||0)+idea.scripts.length+i)); bumpEco(5,0); render(); flashSpark(-COST.scripts5); showToast("5 guiones a partir de tu idea."); return; }
    if(idea._saving){ return showToast("Espera, estoy guardando esa idea…"); }
    if(!idea._server){ return showToast("Esa idea aún no está guardada. Recarga e inténtalo."); }
    var restore=_btnLoading(btn);
    showToast("Generando 5 guiones…");
    apiPost("/ideas/"+encodeURIComponent(idea.id)+"/scripts/generate-batch",{count:5, language:rsLang()}).then(function(r){
      if(!r.ok || !r.d || !Array.isArray(r.d.scripts)){ restore(); return showPaywallOrError(r); }
      idea.expanded=true;
      r.d.scripts.forEach(function(s){ idea.scripts.push(makeScriptFromServer(s, idea.text)); });
      applyCredits(r.d, COST.scripts5); render(); showToast("5 guiones a partir de tu idea.");
    });
  }
  function gen5hooks(scriptId, btn){
    var sc=findScript(scriptId); if(!sc) return;
    if(isDemo()){ var hu=hooksUnitsToday(); spend(hu); S.hooksToday=(S.hooksToday||0)+1; sc.hooks=pick(BANK_HOOKS,5,(sc.hook||"").length+ Object.keys(S.ideas).length); render(); flashSpark(-hu); if(!hu) showToast("Hooks gratis hoy ("+(HOOKS_FREE_PER_DAY-S.hooksToday)+" más)."); return; }
    if(!sc._sid){ return showToast("Este guion aún no está persistido."); }
    var restore=_btnLoading(btn);
    showToast("Generando 5 hooks…");
    apiPost("/scripts/"+encodeURIComponent(sc._sid)+"/hooks/generate-batch",{count:5}).then(function(r){
      if(!r.ok || !r.d || !Array.isArray(r.d.hooks)){ restore(); return showPaywallOrError(r); }
      sc.hooks=r.d.hooks.slice();
      // Si ya estaba guardado en Guiones, su alt_hooks del backend = r.d.alt_hooks.
      if(sc.saved && sc.guionId){ var g=guionById(sc.guionId); if(g && Array.isArray(r.d.alt_hooks)){ g.hooks=r.d.alt_hooks.slice(); } }
      applyCredits(r.d, COST.hooks5); render(); showToast("5 hooks nuevos para tu guion.");
    });
  }
  /* 5 HOOKS DESDE 3 FUENTES (Fathom 18/06, David): además del guión (gen5hooks),
     desde una IDEA y desde un VÍDEO DE COMPETENCIA. Mismo coste (COST.hooks5) y
     endpoint unificado /api/hooks/from-source. assign(hooks) pega el resultado. */
  function _genHooks(payload, assign, btn){
    if(isDemo()){
      var hu=hooksUnitsToday(); spend(hu); S.hooksToday=(S.hooksToday||0)+1;
      var seed=((payload.text||payload.reel_id||"")+"" ).length + Object.keys(S.ideas).length;
      assign(pick(BANK_HOOKS,5,seed)); render(); flashSpark(-hu);
      if(!hu) showToast("Hooks gratis hoy ("+(HOOKS_FREE_PER_DAY-S.hooksToday)+" más)."); return;
    }
    var restore=_btnLoading(btn);
    showToast(L("Generando 5 hooks…","Generating 5 hooks…"));
    apiPost('/api/hooks/from-source', payload).then(function(r){
      if(!r.ok || !r.d || !Array.isArray(r.d.hooks) || !r.d.hooks.length){ restore(); return showPaywallOrError(r); }
      assign(r.d.hooks.slice()); applyCredits(r.d, COST.hooks5); render();
      showToast(L("5 hooks listos.","5 hooks ready."));
    });
  }
  function genHooksFromIdea(ideaId, btn){
    var idea=findIdea(ideaId); if(!idea) return;
    _genHooks({source:"idea", text:idea.text, count:5}, function(hooks){ idea.hooks=hooks; }, btn);
  }
  function genHooksFromReel(reelId, btn){
    var r=reelById(reelId); if(!r) return;
    // En real mandamos el reel_id (el backend saca transcript/caption); en demo da igual.
    _genHooks({source:"competitor", reel_id:r.id, text:(r.cap||r.sum||""), count:5}, function(hooks){ r.hooks=hooks; }, btn);
  }
  function explosion(btn){
    if(isDemo()){
      spend(COST.explosion); var seed=Date.now()%89;
      var ideasD=pick(BANK_IDEAS,5,seed).map(function(t,i){ var idea=makeIdea(t,seed+i*5); for(var j=0;j<5;j++){ var sc=makeScript(t,seed+i*5+j); sc.hooks=pick(BANK_HOOKS,5,seed+i+j); idea.scripts.push(sc); } return idea; });
      S.ideas=ideasD.concat(S.ideas); bumpEco(25,0); render(); flashSpark(-COST.explosion); showToast("5 ideas × 5 guiones × 5 hooks. La semana entera, de un golpe."); return;
    }
    var restore=_btnLoading(btn);
    showToast("Explosión en marcha… esto tarda un poco.");
    apiPost("/ideas/explosion",{project_id:S.brandId&&S.brandId!=="default"?S.brandId:null, language:rsLang()}).then(function(r){
      if(!r.ok || !r.d || !Array.isArray(r.d.ideas)){ restore(); return showPaywallOrError(r); }
      // Reconstruye el árbol idea→guiones→hooks desde la respuesta (scripts traen idea_id).
      var byIdea={};
      (r.d.scripts||[]).forEach(function(s){ var k=s.idea_id||"_"; (byIdea[k]=byIdea[k]||[]).push(s); });
      var fresh=r.d.ideas.map(function(i){
        var it=normIdea(i); it.expanded=true; it._scriptsLoaded=true; it._brand=(i.project_id||S.brandId||"default");
        (byIdea[i.id]||[]).forEach(function(s){ it.scripts.push(makeScriptFromServer(s, it.text)); });
        return it;
      });
      S.ideas=fresh.concat(S.ideas); applyCredits(r.d, COST.explosion); render();
      showToast("5 ideas × 5 guiones × 5 hooks. La semana entera, de un golpe.");
    });
  }
  // Distingue muro de pago (402/free_limit) de error genérico, reusando showPaywall.
  function showPaywallOrError(r){
    var ec=(r.d&&r.d.error)||"error";
    if(ec==="trial_daily_limit"){ return showDailyLimit(); }
    if(r.status===402 || ec==="free_limit_reached" || ec==="no_credits"){ return showPaywall(ec); }
    showToast((r.d&&r.d.message)||(r.d&&r.d.error)||"No se pudo completar. Inténtalo de nuevo.");
  }
  function findIdea(id){ return S.ideas.filter(function(x){return x.id===id;})[0]; }
  function findScript(id){ for(var i=0;i<S.ideas.length;i++){ var s=S.ideas[i].scripts.filter(function(x){return x.id===id;})[0]; if(s) return s; } return null; }
  // En prod, ensureGuion persiste el guion en /scripts si aún no tiene _sid (script
  // generado por gen5scripts ya viene con _sid → no re-crea). Devuelve guionId local.
  function ensureGuion(sc){
    if(sc.saved&&sc.guionId&&guionById(sc.guionId)) return sc.guionId;
    sc.saved=true; sc.guionId=addGuion({title:sc.hook, hook:sc.hook, beats:sc.beats, close:sc.close, from:null, type:"guión"});
    var g=guionById(sc.guionId);
    if(!isDemo()){
      if(sc._sid){
        // Ya persistido (vino de generate-batch): solo enlazamos el _sid al guión.
        if(g) g._sid=sc._sid;
      } else {
        // Guion local (idea suelta sin batch): lo creamos en /scripts ahora.
        var flat=[sc.hook].concat(sc.beats||[],[sc.close]).filter(Boolean).join("\n");
        apiPost("/scripts",{title:sc.hook||"Guión", script:flat, project_id:S.brandId&&S.brandId!=="default"?S.brandId:null}).then(function(r){
          if(r.ok && r.d && r.d.id){ sc._sid=r.d.id; if(g) g._sid=r.d.id; }
        });
      }
    } else { bumpEco(0,0); }
    return sc.guionId;
  }
  function saveScript(scriptId){ var sc=findScript(scriptId); if(!sc||sc.saved) return; ensureGuion(sc); render(); showToast("Guardado en Guiones."); }
  function saveHook(scriptId,i){
    var sc=findScript(scriptId); if(!sc||!sc.hooks) return;
    var h=sc.hooks[i]; if(h==null) return;
    var g=guionById(ensureGuion(sc)); if(!g){ render(); return; }
    g.hooks=g.hooks||[];
    if(g.hooks.indexOf(h)===-1){ g.hooks.push(h); g.expanded=true; }
    sc.savedHooks=sc.savedHooks||{}; sc.savedHooks[i]=true;
    render(); showToast("Hook añadido al guión.");
    // Prod: persiste el hook en el banco del guion (alt_hooks). Si el _sid aún no
    // llegó (POST /scripts en vuelo desde ensureGuion), reintenta una vez.
    if(!isDemo()){
      var doPost=function(sid){ apiPost("/scripts/"+encodeURIComponent(sid)+"/hooks",{hook:h}).then(function(r){ if(r.ok && r.d && Array.isArray(r.d.alt_hooks) && g){ g.hooks=r.d.alt_hooks.slice(); } }); };
      if(g._sid) doPost(g._sid);
      else setTimeout(function(){ if(g._sid) doPost(g._sid); },900);
    }
  }
  // T2 (IDI): sheet con validación en vez de window.prompt.
  // A (cerebro-addreel): en prod el flujo es REAL — analiza el reel y sigue a su autor.
  function addReelManual(){
    promptSheet({
      title:"Añadir reel", label:"URL del reel",
      placeholder:"https://www.instagram.com/reel/…",
      helper:isDemo()
        ? "Pega la URL de un reel (Instagram/TikTok) para meterlo a tu ecosistema."
        : "Analizo el reel (transcripción incluida) y meto a su autor en tu radar. Cuenta como 1 análisis.",
      submitLabel:isDemo()?"Añadir al ecosistema":"Analizar y seguir al autor",
      validate:function(v){ if(!/^https?:\/\/\S+\.\S+/i.test(v.trim())) return "Pega una URL válida (empieza por http)."; },
      onSubmit:function(v){
        if(isDemo()){ showToast("Reel en cola. Lo añadimos a tu ecosistema en unos segundos."); bumpEco(0,1); return; }
        analyzeAndFollow(v.trim());
      }
    });
  }
  /* «Analizar un reel» (reubicación de la antigua página Analizar): transcribe un
     reel suelto y MUESTRA el texto, SIN seguir al autor. Seguirlo es opcional
     (botón secundario). Reusa /transcribe + poll /task (cero endpoints nuevos). */
  var DEMO_REEL_TRANSCRIPT = "Si haces reels y no creces, no es por el algoritmo: es por el primer segundo. "+
    "Mira esto. Los 3 ganchos que más retienen en tu nicho ahora mismo empiezan con una pregunta incómoda, "+
    "un dato que rompe una creencia, o una promesa concreta con número. Coge cualquiera de los tres, ponle "+
    "tu caso real detrás, y cierra pidiendo guardar el vídeo. Eso es todo. Guárdatelo y pruébalo en tu próximo reel.";
  function analyzeReelOnly(){
    promptSheet({
      title:"Analizar un reel",
      label:"URL del reel",
      placeholder:"https://www.instagram.com/reel/…",
      helper:isDemo()
        ? "Pega la URL de un reel (Instagram/TikTok) y te muestro su transcripción. No sigue a su autor."
        : "Transcribo el reel y te muestro el texto, sin seguir a su autor. Cuenta como 1 análisis.",
      submitLabel:isDemo()?"Analizar":"Analizar · 1 análisis",
      validate:function(v){ if(!/^https?:\/\/\S+\.\S+/i.test(v.trim())) return "Pega una URL válida (empieza por http)."; },
      onSubmit:function(v){
        if(isDemo()){ return showReelTranscript(DEMO_REEL_TRANSCRIPT, "nick_saraev"); }
        analyzeReelGetText(v.trim());
      }
    });
  }
  function analyzeReelGetText(url){
    showToast("Analizando el reel…");
    apiPost("/transcribe",{url:url}).then(function(res){
      if(!res.ok){ return showError((res.d&&res.d.error)||"No pude analizar el reel. Revisa el link."); }
      var taskId=res.d&&res.d.task_id;
      if(!taskId) return showError("No pude encolar el análisis. Inténtalo de nuevo.");
      var tries=0;
      (function poll(){
        apiGet("/task/"+encodeURIComponent(taskId)).then(function(r){
          var d=r.d||{};
          if(d.state==="success"){ return showReelTranscript((d.text||"").trim(), d.username||null); }
          // Mensaje CLARO en fallo de descarga (muro de login IG): el reel puede ser privado
          // o no estar disponible. No se cobra. Sugerimos un reel público o un TikTok.
          if(d.state==="error") return showError(d.error||L("No pude descargar ese reel — puede ser privado o no estar disponible. No te hemos cobrado. Prueba con un reel público o un TikTok.","Couldn't download that reel — it may be private or unavailable. You weren't charged. Try a public reel or a TikTok."));
          // Timeout más amplio: la descarga (Apify + yt-dlp con reintentos/backoff) puede tardar
          // varios minutos en reels lentos. 96×2.5s = 240s antes de rendirnos.
          if(++tries>96) return showError(L("El análisis está tardando demasiado. Suele pasar con reels privados o de cuentas poco públicas — prueba con un reel público o un TikTok. No te hemos cobrado.","Analysis is taking too long. This usually happens with private or low-reach reels — try a public reel or a TikTok. You weren't charged."));
          setTimeout(poll, 2500);
        });
      })();
    });
  }
  function showReelTranscript(text, username){
    if(!text){ return showToast("La transcripción salió vacía. Prueba con otro reel."); }
    var box='<div class="field-label" style="margin-bottom:8px">'+L("Transcripción","Transcript")+'</div>'+
      '<div class="analyze-tx">'+ESC(text)+'</div>';
    promptSheet({
      readonly:true,
      title:L("Reel analizado","Reel analyzed"),
      extraHTML:box,
      submitLabel:L("Cerrar","Close"),
      secondaryLabel: username?(L("Seguir a @","Follow @")+username):null,
      onSecondary: username?function(){ _followAuthor(username); }:null
    });
  }
  /* ════════ PÁGINA «Analizar reel» (nativa isla) ════════════════════════════════
     Lista PERSISTIDA de análisis (tabla `transcriptions` vía GET /history) + analizar
     uno nuevo (POST /transcribe → poll /task, que YA guarda el resultado). Entrada =
     botón «Analizar un reel» del Radar. Borrar = DELETE /history/<id>. Métricas
     (views/likes/comments) las trae el backend en planes de pago. Estilo v3. */
  function analyzeDemoSeed(){
    return [
      { id:"d1", author_username:"nick_saraev", platform:"instagram", created_at:"2026-06-20T10:00:00Z",
        views:182000, likes:9400, comments:210, text:DEMO_REEL_TRANSCRIPT, thumbnail_b64:null },
      { id:"d2", author_username:"hormozi", platform:"instagram", created_at:"2026-06-18T17:30:00Z",
        views:540000, likes:31000, comments:880, thumbnail_b64:null,
        text:"El error número uno al empezar: intentar gustar a todos. Habla para una sola persona y serás magnético para miles. Define a quién le hablas, ponle nombre, y escribe cada guion como si fuera un mensaje para esa persona." }
    ];
  }
  function loadAnalyses(){
    if(isDemo()){ if(!S.analyses) S.analyses=analyzeDemoSeed(); if(S.tab==="analizar") render(); return; }
    apiGet("/history").then(function(r){
      S.analyses=(r&&Array.isArray(r.d))?r.d:[];
      if(S.tab==="analizar") render();
    });
  }
  function _anzNum(n){ n=+n||0; if(n>=1e6) return (n/1e6).toFixed(1).replace(/\.0$/,"")+"M"; if(n>=1e3) return (n/1e3).toFixed(1).replace(/\.0$/,"")+"K"; return ""+n; }
  function _anzDate(s){ try{ var d=new Date(s); var dd=Math.floor((Date.now()-d.getTime())/86400000);
    if(dd<=0) return L("hoy","today"); if(dd===1) return L("ayer","yesterday");
    if(dd<30) return L("hace "+dd+" días",dd+"d ago"); return d.toLocaleDateString(); }catch(e){ return ""; } }
  function _anzFind(id){ return (S.analyses||[]).filter(function(a){ return String(a.id)===String(id); })[0]; }
  function analyzeCardHTML(a){
    var au=a.author_username?("@"+String(a.author_username).replace(/^@+/,"")):L("autor desconocido","unknown author");
    var thumb=a.thumbnail_b64
      ? '<img class="anz-thumb-img" src="'+ESC(a.thumbnail_b64)+'" alt="" loading="lazy">'
      : '<span class="anz-thumb-ph">'+IC.doc+'</span>';
    var mets=[];
    if(a.views!=null&&a.views!=="") mets.push('<span>'+IC.eye+' '+_anzNum(a.views)+'</span>');
    if(a.likes!=null&&a.likes!=="") mets.push('<span>'+IC.heart+' '+_anzNum(a.likes)+'</span>');
    if(a.comments!=null&&a.comments!=="") mets.push('<span>'+IC.chat+' '+_anzNum(a.comments)+'</span>');
    var prev=String(a.text||"").slice(0,180);
    return '<article class="anz-card">'+
      '<div class="anz-thumb">'+thumb+'<span class="anz-plat">'+ESC(a.platform||"reel")+'</span></div>'+
      '<div class="anz-body">'+
        '<div class="anz-top"><span class="anz-author">'+ESC(au)+'</span><span class="anz-date">'+_anzDate(a.created_at)+'</span></div>'+
        (mets.length?'<div class="anz-mets">'+mets.join("")+'</div>':'')+
        '<p class="anz-prev">'+(prev?ESC(prev)+((a.text||"").length>180?"…":""):'<i>'+L("(sin transcripción)","(no transcript)")+'</i>')+'</p>'+
        '<div class="anz-acts">'+
          '<button class="btn btn-sm btn-secondary" data-act="analyze-view" data-id="'+ESC(a.id)+'">'+IC.eye+' '+L("Ver","View")+'</button>'+
          '<button class="btn btn-sm btn-ghost" data-act="analyze-copy" data-id="'+ESC(a.id)+'">'+L("Copiar","Copy")+'</button>'+
          (a.author_username?'<button class="btn btn-sm btn-ghost" data-act="analyze-follow" data-h="'+ESC(String(a.author_username).replace(/^@+/,""))+'">'+L("Seguir","Follow")+'</button>':'')+
          '<button class="btn btn-sm btn-ghost anz-del" data-act="analyze-del" data-id="'+ESC(a.id)+'">'+L("Borrar","Delete")+'</button>'+
        '</div>'+
      '</div>'+
    '</article>';
  }
  function analizarHTML(){
    if(S.analyzeDetailId){ var ad=_anzFind(S.analyzeDetailId); if(ad) return analyzeDetailHTML(ad); S.analyzeDetailId=null; }
    var loading=!!S.analyzeLoading, list=S.analyses;
    var form='<div class="anz-form">'+
      '<input id="rsAnalyzeUrl" class="anz-input" type="text" autocomplete="off" spellcheck="false" placeholder="'+L("Pega la URL de un reel (Instagram / TikTok)","Paste a reel URL (Instagram / TikTok)")+'" value="'+ESC(S.analyzeUrl||"")+'"'+(loading?' disabled':'')+'>'+
      '<button class="btn btn-md btn-primary anz-go" data-act="analyze-run"'+(loading?' disabled':'')+'>'+
        (loading?'<span class="rs-ldr"></span> '+ESC(S.analyzeStep||L("Analizando…","Analyzing…")):IC.bolt+' '+L("Analizar","Analyze"))+'</button>'+
    '</div>'+
    (S.analyzeErr?'<div class="anz-err">'+ESC(S.analyzeErr)+'</div>':'')+
    '<div class="anz-hint">'+L("Transcribo el reel y lo guardo aquí. No sigo a su autor — puedes hacerlo tú con «Seguir».","I transcribe the reel and save it here. I don't follow its author — you can with «Follow».")+'</div>';
    var body;
    if(list==null) body='<div class="anz-loading"><span class="rs-ldr"></span> '+L("Cargando tus análisis…","Loading your analyses…")+'</div>';
    else if(!list.length) body='<div class="anz-empty"><span class="anz-empty-h">'+L("Aún no has analizado ningún reel.","No reels analyzed yet.")+'</span><span class="anz-empty-s">'+L("Pega un link arriba y aparecerá aquí, guardado.","Paste a link above and it shows up here, saved.")+'</span></div>';
    else body='<div class="anz-count">'+L(list.length+" análisis guardados",list.length+" saved analyses")+'</div><div class="anz-grid">'+list.map(analyzeCardHTML).join("")+'</div>';
    return '<div class="scroll"><div class="canvas anz-canvas">'+
      '<header class="anz-head"><h1 class="h-title">'+L("Analizar un reel","Analyze a reel")+'</h1>'+
      '<p class="h-sub">'+L("Pega cualquier reel y te saco la transcripción y sus métricas. Todo queda guardado aquí.","Paste any reel and I pull its transcript and metrics. Everything is saved here.")+'</p></header>'+
      form+body+
    '</div></div>';
  }
  function analyzeRun(){
    var inp=document.getElementById("rsAnalyzeUrl");
    var url=(inp?inp.value:S.analyzeUrl||"").trim();
    S.analyzeUrl=url; S.analyzeErr=null;
    if(!/^https?:\/\/\S+\.\S+/i.test(url)){ S.analyzeErr=L("Pega una URL válida (empieza por http).","Paste a valid URL (starts with http)."); return render(); }
    if(isDemo()){
      S.analyzeLoading=true; S.analyzeStep=L("Analizando…","Analyzing…"); render();
      setTimeout(function(){
        S.analyzeLoading=false; S.analyzeUrl="";
        S.analyses=[{ id:"d"+(S.analyses||[]).length+"_"+(S.guiones||[]).length, author_username:"nick_saraev", platform:"instagram",
          created_at:new Date().toISOString(), views:120000, likes:8000, comments:140, text:DEMO_REEL_TRANSCRIPT, thumbnail_b64:null }].concat(S.analyses||[]);
        render(); showToast(L("Reel analizado y guardado.","Reel analyzed and saved."));
      }, 1400);
      return;
    }
    S.analyzeLoading=true; S.analyzeStep=L("Encolando…","Queuing…"); render();
    apiPost("/transcribe",{url:url}).then(function(res){
      if(!res.ok){ S.analyzeLoading=false; S.analyzeErr=(res.d&&res.d.error)||L("No pude analizar el reel. Revisa el link.","Couldn't analyze it. Check the link."); return render(); }
      var taskId=res.d&&res.d.task_id;
      if(!taskId){ S.analyzeLoading=false; S.analyzeErr=L("No pude encolar el análisis.","Couldn't queue it."); return render(); }
      var tries=0;
      (function poll(){
        apiGet("/task/"+encodeURIComponent(taskId)).then(function(r){
          var d=r.d||{};
          if(d.state==="success"){
            S.analyzeLoading=false; S.analyzeUrl=""; S.analyzeStep="";
            if(d.credits_cents!=null) S.user.credits=d.credits_cents;
            showToast(L("Reel analizado y guardado.","Reel analyzed and saved."));
            loadAnalyses();   // recarga el historial (la task ya lo persistió)
            return;
          }
          if(d.state==="error"){ S.analyzeLoading=false; S.analyzeErr=d.error||L("No pude descargar ese reel — puede ser privado o no estar disponible. No te hemos cobrado. Prueba con un reel público o un TikTok.","Couldn't download that reel — it may be private or unavailable. You weren't charged. Try a public reel or a TikTok."); return render(); }
          S.analyzeStep=d.step||L("Transcribiendo…","Transcribing…");
          if(S.tab==="analizar") render();
          // Timeout amplio (240s): Apify + yt-dlp con reintentos pueden tardar en reels lentos.
          if(++tries>96){ S.analyzeLoading=false; S.analyzeErr=L("Está tardando demasiado. Suele pasar con reels privados o de cuentas poco públicas — prueba con un reel público o un TikTok. No te hemos cobrado.","Taking too long. This usually happens with private or low-reach reels — try a public reel or a TikTok. You weren't charged."); return render(); }
          setTimeout(poll, 2500);
        });
      })();
    });
  }
  // Ficha de detalle: portada + TODAS las métricas a un lado, transcripción al otro,
  // y «Robar guion» (→ /transcriptions/<id>/to-script, persiste en `scripts` = Guiones).
  function analyzeDetailHTML(a){
    var au=a.author_username?("@"+String(a.author_username).replace(/^@+/,"")):L("autor desconocido","unknown author");
    var thumb=a.thumbnail_b64
      ? '<img class="anzd-thumb-img" src="'+ESC(a.thumbnail_b64)+'" alt="" loading="lazy">'
      : '<div class="anzd-thumb-ph"></div>';
    var plat=(a.platform||"instagram");
    var platCap=plat.charAt(0).toUpperCase()+plat.slice(1);
    // SVGs propios del rediseño (IC no los tiene): instagram, link-externo, copiar, papelera.
    var _ig='<svg viewBox="0 0 24 24" width="13" height="13" fill="none"><rect x="3" y="3" width="18" height="18" rx="5" stroke="currentColor" stroke-width="1.8"></rect><circle cx="12" cy="12" r="3.4" stroke="currentColor" stroke-width="1.8"></circle><circle cx="17.5" cy="6.5" r="1" fill="currentColor"></circle></svg>';
    var _ext='<svg viewBox="0 0 24 24" width="14" height="14" fill="none"><path d="M7 17L17 7M9 7h8v8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path></svg>';
    var _copy='<svg viewBox="0 0 24 24" width="16" height="16" fill="none"><rect x="9" y="9" width="11" height="11" rx="2.5" stroke="currentColor" stroke-width="1.8"></rect><path d="M5 15V5a2 2 0 012-2h10" stroke="currentColor" stroke-width="1.8"></path></svg>';
    var _trash='<svg viewBox="0 0 24 24" width="16" height="16" fill="none"><path d="M4 7h16M9 7V5a2 2 0 012-2h2a2 2 0 012 2v2M6 7l1 13a2 2 0 002 2h6a2 2 0 002-2l1-13" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path></svg>';
    var stat=function(cls,ic,v,lbl){ return (v!=null&&v!=="")?'<div class="anzd-stat '+cls+'"><span class="anzd-stat-ic">'+ic+'</span><span class="anzd-stat-v">'+_anzNum(v)+'</span><span class="anzd-stat-l">'+lbl+'</span></div>':''; };
    // Shares: Apify casi nunca lo trae (sale 0) → solo lo mostramos si hay dato real (>0).
    var stats=stat("v-views",IC.eye,a.views,"Views")+stat("v-likes",IC.heart,a.likes,"Likes")+
              stat("v-comments",IC.chat,a.comments,L("Comentarios","Comments"))+stat("v-shares",IC.repeat,(a.shares>0?a.shares:null),"Shares");
    var statsBlock=stats.trim()?'<div class="anzd-stats">'+stats+'</div>':'<div class="anzd-nostat">'+L("Sin métricas guardadas para este reel.","No metrics saved for this reel.")+'</div>';
    // Refrescar métricas (solo IG): rellena/actualiza vía Apify. Abierto a todos los planes.
    var refreshing=!!S.analyzeRefreshing;
    var refreshBtn=(plat==="instagram")?'<button class="anzd-refresh" data-act="analyze-refresh-metrics" data-id="'+ESC(a.id)+'"'+(refreshing?' disabled':'')+'>'+(refreshing?'<span class="rs-ldr"></span> '+L("Actualizando…","Refreshing…"):'↻ '+L("Actualizar métricas","Refresh metrics"))+'</button>':'';
    var orig=a.url?'<a class="anzd-orig" href="'+ESC(a.url)+'" target="_blank" rel="noopener noreferrer">'+L("Ver original en "+platCap,"View original on "+platCap)+_ext+'</a>':'';
    var stealing=!!S.analyzeStealing;
    var wc=(a.text||"").trim().split(/\s+/).filter(Boolean).length;
    return '<div class="scroll"><div class="canvas anzd-canvas">'+
      '<div class="anzd-glow" aria-hidden="true"></div>'+
      '<header class="anzd-head">'+
        '<button class="anzd-back" data-act="analyze-back" aria-label="'+L("Volver","Back")+'">'+IC.back+'</button>'+
        '<div class="anzd-head-tt"><span class="anzd-head-t">'+L("Reel analizado","Analyzed reel")+'</span>'+
          '<span class="anzd-head-sub">'+L("Transcripción y métricas del contenido","Transcript and content metrics")+'</span></div>'+
      '</header>'+
      '<div class="anzd-grid">'+
        '<aside class="anzd-side">'+
          '<div class="anzd-thumb">'+thumb+'<div class="anzd-thumb-shade"></div>'+
            '<span class="anzd-plat">'+_ig+' '+ESC(platCap)+'</span>'+
            '<div class="anzd-thumb-meta"><div class="anzd-thumb-author">'+ESC(au)+'</div>'+
              '<div class="anzd-thumb-date">'+L("Publicado ","Posted ")+_anzDate(a.created_at)+'</div></div>'+
          '</div>'+
          statsBlock+
          refreshBtn+
          orig+
        '</aside>'+
        '<section class="anzd-main">'+
          '<div class="anzd-acts">'+
            '<button class="anzd-btn anzd-btn-steal" data-act="analyze-steal" data-id="'+ESC(a.id)+'"'+(stealing?' disabled':'')+'>'+(stealing?'<span class="rs-ldr"></span> '+L("Robando…","Stealing…"):IC.bolt+' '+L("Robar guion","Steal script"))+'</button>'+
            (a.author_username?'<button class="anzd-btn anzd-btn-follow" data-act="analyze-follow" data-h="'+ESC(String(a.author_username).replace(/^@+/,""))+'">'+IC.plus+' '+L("Seguir","Follow")+'</button>':'')+
            '<button class="anzd-btn anzd-btn-ghost" data-act="analyze-copy" data-id="'+ESC(a.id)+'">'+_copy+' '+L("Copiar","Copy")+'</button>'+
            '<button class="anzd-btn anzd-btn-del" data-act="analyze-del" data-id="'+ESC(a.id)+'">'+_trash+' '+L("Borrar","Delete")+'</button>'+
          '</div>'+
          '<div class="anzd-txhead"><span class="anzd-txlabel">'+L("Transcripción","Transcript")+'</span><span class="anzd-txrule"></span>'+
            (wc?'<span class="anzd-txwc">~'+wc+' '+L("palabras","words")+'</span>':'')+'</div>'+
          '<div class="anzd-tx"><span class="anzd-tx-bar"></span><p>'+(a.text?ESC(a.text):'<i>'+L("(sin transcripción)","(no transcript)")+'</i>')+'</p></div>'+
        '</section>'+
      '</div>'+
    '</div></div>';
  }
  function analyzeView(id){ S.analyzeDetailId=id; S.analyzeStealing=false; S.analyzeRefreshing=false; render(); }
  function analyzeBack(){ S.analyzeDetailId=null; render(); }
  function analyzeRefreshMetrics(id){
    var a=_anzFind(id); if(!a || S.analyzeRefreshing) return;
    if(isDemo()){ showToast(L("En demo no se actualizan métricas reales.","Demo: real metrics not refreshed.")); return; }
    S.analyzeRefreshing=true; render();
    apiPost("/transcriptions/"+encodeURIComponent(id)+"/refresh-metrics",{}).then(function(r){
      S.analyzeRefreshing=false;
      if(r.ok && r.d && r.d.metrics){
        ["views","likes","comments","shares","published_at","metrics_updated_at"].forEach(function(k){ if(r.d.metrics[k]!=null) a[k]=r.d.metrics[k]; });
        render(); showToast(L("Métricas actualizadas.","Metrics updated."));
      } else {
        render(); showError((r.d&&r.d.error)||L("No pude actualizar las métricas. Inténtalo en un momento.","Couldn't refresh metrics. Try again in a moment."));
      }
    });
  }
  function analyzeSteal(id){
    var a=_anzFind(id); if(!a) return;
    if(S.analyzeStealing) return;
    // F11: robar desde «Analizar» aterriza DEL TIRÓN en el workspace de la idea
    // (antes: te quedabas en la ficha con un toast de 6s solo en prod).
    var _land=function(){ S.analyzeDetailId=null; wsOpen("t:"+String(id)); showToast(L("Guion robado ✓ — ya es tuyo.","Script stolen ✓ — it's yours now.")); };
    if(isDemo()){
      S.analyzeStealing=true; render();
      setTimeout(function(){
        S.analyzeStealing=false;
        var ns=makeScript((a.text||"").slice(0,80)||"Guión");
        addGuion({title:ns.hook, hook:ns.hook, beats:ns.beats, close:ns.close, from:a.author_username?("@"+a.author_username):null, type:"guión", txId:String(id)});
        _land();
      }, 1500);
      return;
    }
    S.analyzeStealing=true; render();
    apiPost("/transcriptions/"+encodeURIComponent(id)+"/to-script",{}).then(function(r){
      S.analyzeStealing=false;
      if(!r.ok || !r.d || !r.d.script){
        if(r.d && (r.d.error==="no_credits"||r.d.error==="free_limit_reached")) { render(); return showPaywall(r.d.error); }
        render(); return showError((r.d&&r.d.message)||(r.d&&r.d.error)||L("No pude generar el guion ahora mismo. Reinténtalo.","Couldn't generate the script right now. Try again."));
      }
      var p=scriptToParts(r.d.script);
      var gidNew=addGuion({title:p.hook, hook:p.hook, beats:p.beats, close:p.close, from:a.author_username?("@"+a.author_username):null, type:"guión", thumb:a.thumbnail_b64||null, url:a.url||null, srcViews:a.views||"", srcLikes:a.likes||"", txId:String(id)});
      if(r.d.script_id){ var g=guionById(gidNew); if(g) g._sid=r.d.script_id; }
      refreshCredits().then(function(){ flashSpark(0); });
      _land();
    });
  }
  function _anzGoGuiones(){ S.analyzeDetailId=null; switchTab("guiones"); }
  function analyzeCopy(id){ var a=_anzFind(id); if(!a) return; try{ if(navigator.clipboard) navigator.clipboard.writeText(a.text||""); }catch(e){} showToast(L("Transcripción copiada.","Transcript copied.")); }
  function analyzeDelete(id){
    if(!_anzFind(id)) return;
    S.analyses=(S.analyses||[]).filter(function(x){ return String(x.id)!==String(id); }); render();
    if(!isDemo()) apiDelete("/history/"+encodeURIComponent(id));
  }
  /* A · «Añadir reel» REAL — encadena flujos que ya existen (cero endpoints nuevos):
       1. POST /transcribe — cobra 1 análisis free / crédito (backend = verdad; 402 = muro).
       2. poll GET /task/<id> — la task devuelve `username` (autor del reel).
       3. POST /api/tracked-creators — usa el slot de competidor; el backend auto-encola
          el scrape de sus reels → aparecen en el Radar con transcripción on-demand
          (detalle de reel ya existente). 409 already_tracking = no duplicar, todo bien.
     Si la task falla, el backend REEMBOLSA el análisis (charge→refund en transcribe_task). */
  function analyzeAndFollow(url){
    showToast("Analizando el reel…");
    apiPost("/transcribe",{url:url}).then(function(res){
      if(!res.ok){ return showError((res.d&&res.d.error)||"No pude analizar el reel. Revisa el link."); }
      var taskId=res.d&&res.d.task_id;
      if(!taskId) return showError("No pude encolar el análisis. Inténtalo de nuevo.");
      var tries=0;
      (function poll(){
        apiGet("/task/"+encodeURIComponent(taskId)).then(function(r){
          var d=r.d||{};
          if(d.state==="success"){ bumpEco(0,1); return _followAuthor(d.username); }
          if(d.state==="error") return showError(d.error||L("No pude descargar ese reel — puede ser privado o no estar disponible. No te hemos cobrado.","Couldn't download that reel — it may be private or unavailable. You weren't charged."));
          if(++tries>96) return showError(L("El análisis está tardando demasiado — prueba con un reel público o un TikTok. No te hemos cobrado.","Analysis is taking too long — try a public reel or a TikTok. You weren't charged."));
          setTimeout(poll, 2500);
        });
      })();
    });
  }
  // Refresh LIGERO tras seguir al autor: tracked + stats, repintando con render()
  // (que conserva #rsToast). loadBrandData no vale aquí — su skeleton hace
  // innerHTML del root y se llevaría por delante el toast de confirmación.
  function _refreshRadarLight(){
    loadTracked();
    var _pq=(S.brandId&&S.brandId!=="default")?("?project_id="+encodeURIComponent(S.brandId)):"";
    apiGet("/api/radar/stats"+_pq).then(function(r){
      if(r.ok&&r.d){
        S.stats={ competitors:r.d.competitors||0, reels_week:r.d.reels_week||0, exploded_week:r.d.exploded_week||0,
          stolen_today:r.d.stolen_today!=null?r.d.stolen_today:(r.d.stolen_total||0) };
        render();
      }
    });
  }
  // growth-2: refresco LIGERO de señales (stats + tracked + reels) SIN el skeleton
  // de loadBrandData (que reescribe el root y mata toast/overlay). render() conserva
  // los nodos estables. `cb` se llama al terminar.
  function _refreshReelsLight(cb){
    var _pq=(S.brandId&&S.brandId!=="default")?("?project_id="+encodeURIComponent(S.brandId)):"";
    Promise.all([
      apiGet("/api/radar/stats"+_pq),
      apiGet("/api/tracked-creators/reels"+(_pq?_pq+"&":"?")+"sort=explosion&limit=24")
    ]).then(function(res){
      var st=res[0], fd=res[1];
      if(st&&st.ok&&st.d){ S.stats={ competitors:st.d.competitors||0, reels_week:st.d.reels_week||0, exploded_week:st.d.exploded_week||0, stolen_today:st.d.stolen_today!=null?st.d.stolen_today:(st.d.stolen_total||0) }; }
      if(fd&&fd.ok&&fd.d&&Array.isArray(fd.d.reels)){ S.reels=fd.d.reels.map(normReel); S.radarSeed=!!fd.d.seed; S.favs={}; S.reels.forEach(function(r){ if(r.fav) S.favs[r.id]=true; }); }
      loadTracked();
      bgRender();   // gate _screenBusy + coalescing dentro de bgRender
      if(cb) cb();
    });
  }
  function _followAuthor(handle){
    if(!handle) return showToast("Reel analizado — lo tienes en Analizar. No pude identificar a su autor para seguirlo.");
    var already=Array.isArray(S.tracked)&&S.tracked.some(function(t){
      var h=(t.creator&&t.creator.ig_username)||t.ig_username||"";
      return h.toLowerCase()===String(handle).toLowerCase();
    });
    if(already){ _refreshRadarLight(); return showToast("Reel analizado · ya sigues a @"+handle+" — su radar se actualiza solo."); }
    // Límite DURO del plan: si ya estás al tope de competidores, NO seguir (el backend
    // tiene cap blando que cobraría créditos y excedería el «solo N»). Pre-check aquí.
    var _used=(S.trackedCount!=null?S.trackedCount:(Array.isArray(S.tracked)?S.tracked.length:0));
    var _lim=S.trackedLimit;
    if(_lim!=null && _used>=_lim){
      return showError(L("Ya sigues a tus "+_lim+" competidores. Quítale el seguimiento a uno en el Radar (o sube de plan) para seguir a @"+handle+".","You already follow your "+_lim+" competitors. Unfollow one in the Radar (or upgrade) to follow @"+handle+"."));
    }
    var body={ig_username:handle};
    var _pid=_pidOf(S.brandId); if(isAgency()&&_pid) body.project_id=_pid;
    apiPost("/api/tracked-creators",body).then(function(r){
      if(r.ok){ _refreshRadarLight(); return showToast("Reel analizado · @"+handle+" ahora en tu radar — sus reels llegan en ~1 min."); }
      var err=(r.d&&r.d.error)||"";
      if(err==="tc.error.already_tracking"){ _refreshRadarLight(); return showToast("Reel analizado · ya seguías a @"+handle+"."); }
      if(err==="tc.error.plan_limit_reached") return showError("Reel analizado y guardado. Tu plan ya usa todos sus huecos de competidor — sube de plan para seguir también a @"+handle+".");
      if(err==="tc.error.upgrade_required") return showError("Reel analizado y guardado. Seguir competidores no entra en tu plan actual.");
      if(err==="tc.error.project_required") return showError("Reel analizado. Entra en una marca concreta para meter a @"+handle+" en su radar.");
      showError("Reel analizado, pero no pude seguir a @"+handle+". Añádelo desde el Radar.");
    });
  }

  // Captura del moat: el creador pega sus reels → derivamos su VoiceProfile.
  function onboardVoice(){
    var ta=document.getElementById("rsVoiceText"); var txt=ta?ta.value.trim():"";
    if(!txt){ showToast("Pega el texto de al menos 1 reel tuyo."); return; }
    // B8: en DEMO no llamamos al backend real (POST /api/voice/onboard + GET /api/voice).
    // Sembramos un VoiceProfile dummy fijo para que la demo enseñe el "después" del moat
    // (Cerebro con voz aprendida, confianza 62%, evidencia) sin claves ni red. NO ELIMINAR:
    // la demo lo necesita para que el flujo de onboarding se vea completo. La integración
    // REAL (transcripción → derivar voz → persistir) solo se ejercita en el branch de prod
    // de abajo — este branch nunca la prueba a propósito.
    if(isDemo()){
      S.voice={ has_profile:true, tone:"Directo, sin postureo — como un audio a un colega.",
        phrases:["te lo cuento porque","paso uno… paso dos","guárdate esto"], structure:"hook directo → 3 pasos → CTA",
        avg_duration:40, avoid:"tecnicismos y motivacional vacío", confidence:62, source_count:1,
        evidence:["abres con una afirmación tajante","frases cortas (<12 palabras)","cierras pidiendo guardar/comentar"] };
      var b=brand(); b.voice=62; render(); showToast("Voz aprendida — te conozco al 62%."); setTimeout(brainLevelPulse,1600); return;
    }
    showToast("Aprendiendo tu voz…");
    fetch("/api/voice/onboard",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({texts:[txt]})})
      .then(function(r){ return r.json().catch(function(){return{};}); })
      .then(function(d){
        if(d&&d.ok){
          return fetch("/api/voice",{credentials:"same-origin"}).then(function(r){return r.json();}).then(function(v){
            S.voice=v; render(); showToast("Voz aprendida — te conozco al "+(v.confidence||0)+"%."); setTimeout(brainLevelPulse,1600);
          });
        }
        showError((d&&d.error)||"No pude aprender tu voz. Prueba con otro reel.");
      })
      .catch(function(){ showError("Error de red. Inténtalo de nuevo."); });
  }

  // Voz AUTO: deriva la voz de los reels PUBLICADOS del user (ig_videos).
  // Preflight (GET, sin coste) → confirmación con el coste real → POST.
  // NUNCA cobra sin avisar: las transcripciones pendientes se enseñan antes.
  function voiceAutoDerive(){
    if(isDemo()){
      S.voice={ has_profile:true, tone:"Directo, sin postureo — como un audio a un colega.",
        phrases:["te lo cuento porque","paso uno… paso dos","guárdate esto"], structure:"hook directo → 3 pasos → CTA",
        avg_duration:40, avoid:"tecnicismos y motivacional vacío", confidence:78, source_count:5,
        evidence:["abres con una afirmación tajante","frases cortas (<12 palabras)","cierras pidiendo guardar/comentar"] };
      var b=brand(); b.voice=78; b.reelsAnalyzed=5; render(); showToast("Voz derivada de tus 5 reels — te conozco al 78%."); return;
    }
    showToast("Comprobando tus reels publicados…");
    apiGet("/api/voice/auto-derive").then(function(pre){
      if(!pre.ok || !pre.d){ return showError("No pude comprobar tus reels. Inténtalo de nuevo."); }
      var d=pre.d;
      if(!d.available){ return showToast("No encuentro reels publicados tuyos. Conecta tu Instagram en Métricas."); }
      var cost = d.need_transcribe>0
        ? d.need_transcribe+" de ellos necesita"+(d.need_transcribe===1?"":"n")+" transcripción ("+d.need_transcribe+" uso"+(d.need_transcribe===1?"":"s")+"/crédito"+(d.need_transcribe===1?"":"s")+")."
        : "Todos ya están transcritos — derivar es gratis.";
      var go=function(){
        showToast("Derivando tu voz de tus reels… (~1-2 min si hay que transcribir)");
        var done=function(d){
          return fetch("/api/voice",{credentials:"same-origin"}).then(function(x){return x.json();}).then(function(v){
            S.voice=v; render();
            showToast("Voz derivada de "+(d.source_count||0)+" reels — te conozco al "+(d.confidence||v.confidence||0)+"%.");
            setTimeout(brainLevelPulse,1600);
          });
        };
        // /api/voice/auto-derive es async (202 + task_id): gemini-2.5-pro razona y
        // transcribir reels tarda → Traefik cortaba a 60s. Polling a /task/voice-derive.
        apiPost("/api/voice/auto-derive",{project_id:_pidOf(S.brandId)}).then(function(r){
          if(!r.ok || !r.d){ return showError("No pude derivar tu voz. Inténtalo de nuevo."); }
          if(r.d.ok){ return done(r.d); }   // respuesta sync legacy, por si acaso
          if(!r.d.task_id){ return showError(r.d.message||r.d.error||"No pude derivar tu voz. Inténtalo de nuevo."); }
          var tid=r.d.task_id, tries=0;
          var poll=function(){
            apiGet("/task/voice-derive/"+encodeURIComponent(tid)).then(function(p){
              var d=(p&&p.d)||{};
              if(!p.ok){ if(++tries<60) return setTimeout(poll,2500); return showError("No pude derivar tu voz. Inténtalo de nuevo."); }
              if(d.state==="pending"){ if(++tries<70) return setTimeout(poll,2500); return showError("Tardó demasiado. Inténtalo de nuevo."); }
              if(d.state==="success"){ return done(d); }
              return showError(d.message||d.error||"No pude derivar tu voz. Inténtalo de nuevo.");
            });
          };
          setTimeout(poll,2500);
        });
      };
      if(typeof window.confirmModal==="function"){
        window.confirmModal({ title:"Derivar mi voz", body:"Usaré tus "+d.will_use+" reels con más views. "+cost, confirmText:"Derivar mi voz", cancelText:"Ahora no" })
          .then(function(ok){ if(ok) go(); });
      } else { go(); }
    });
  }

  // Refinar el moat: acumula reels NUEVOS sobre la voz ya aprendida.
  // El backend (POST /api/voice/refine) suma source_count y sube confidence.
  // Mirror de onboardVoice: mismo auth (credentials same-origin), mismo refresh
  // (GET /api/voice → re-pinta Cerebro), mismo branch demo (en demo NO postea).
  // T2 (IDI): sheet con textarea (igual que el onboarding) en vez de window.prompt.
  // A3 (coherencia): refinar = pegar MÁS URLs de tus reels (nada de transcripciones).
  function refineVoice(){
    promptSheet({
      title:"Refinar mi voz", label:"URLs de más reels tuyos",
      placeholder:"https://www.instagram.com/reel/…  https://www.tiktok.com/@tu/video/…",
      helper:"Los transcribo y subo el % que te conozco. Cada reel cuesta 1 crédito (te lo confirmo).",
      multiline:true, submitLabel:"Continuar",
      validate:function(v){ var u=(v.match(/https?:\/\/\S+/g)||[]).filter(function(x){return /instagram\.com|tiktok\.com/.test(x);}); if(!u.length) return "Pega URLs de reels tuyos (Instagram/TikTok)."; },
      onSubmit:refineVoiceWith
    });
  }
  function refineVoiceWith(blob){
    var urls=((blob||"").match(/https?:\/\/\S+/g)||[]);
    var reels=urls.filter(function(u){ return /instagram\.com|tiktok\.com/.test(u) && /\/reel\/|\/reels\/|\/p\/|\/tv\/|\/video\/|vm\.tiktok|vt\.tiktok/.test(u); });
    if(!reels.length){ return showError("Pega URLs de reels concretos tuyos (no el perfil)."); }
    var n=Math.min(reels.length,6);
    if(isDemo()){
      S.voice=S.voice||{ has_profile:true }; S.voice.has_profile=true;
      S.voice.source_count=(S.voice.source_count||1)+n;
      S.voice.confidence=Math.min(100,(S.voice.confidence||62)+11);
      brand().voice=S.voice.confidence; render();
      showToast("Voz refinada (demo) — ahora te conozco al "+S.voice.confidence+"%."); setTimeout(brainLevelPulse,1600); return;
    }
    var go=function(){
      showToast("Transcribiendo y refinando tu voz… (~1 min)");
      apiPost("/api/voice/from-urls",{urls:reels.slice(0,6)}).then(function(r){
        if(r.ok && r.d && r.d.ok){
          return fetch("/api/voice",{credentials:"same-origin"}).then(function(x){return x.json();}).then(function(v){
            S.voice=v; render(); showToast("Voz refinada — ahora te conozco al "+(r.d.confidence||v.confidence||0)+"%."); setTimeout(brainLevelPulse,1600);
          });
        }
        showError((r.d&&r.d.message)||(r.d&&r.d.error)||"No pude refinar tu voz con esos reels.");
      });
    };
    if(typeof window.confirmModal==="function"){
      window.confirmModal({ title:"Refinar tu voz", body:"Voy a transcribir "+n+" reel"+(n===1?"":"s")+" más — cuesta "+n+" crédito"+(n===1?"":"s")+".", confirmText:"Sí, refinar", cancelText:"Ahora no" }).then(function(ok){ if(ok) go(); });
    } else go();
  }

  // Refresca métricas + insights de la marca activa (summary + insights) y re-pinta.
  // Devuelve la promesa para encadenar toasts. Solo prod (en demo las métricas son sembradas).
  function refreshMetrics(){
    var q=S.brandId?("?brand="+encodeURIComponent(S.brandId)):"";
    return Promise.all([
      fetch("/metrics/summary"+q,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
      fetch("/api/metrics/insights"+q,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
      fetch("/metrics/videos"+q,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;})
    ]).then(function(res){
      var met=res[0], ins=res[1], vids=res[2];
      if(met){ S.metrics=met; S.igConnected=!!(met && met.connected); }
      if(ins){ S.metrics=S.metrics||{}; S.metrics.insights={ what_works:ins.what_works||[], next:ins.next||null }; }
      // Los reels reales viven en /metrics/videos (summary solo trae agregados).
      if(vids){ S.metrics=S.metrics||{}; S.metrics.videos=(vids.videos||[]).map(normMetricVideo); }
      render();
    });
  }

  // B3 (prod): al vincular un reel publicado a su guion, lo analizamos por audio
  // (POST /metrics/analyze-one) y refrescamos las métricas del guion.
  function linkReelPublished(g, url){
    showToast("Analizando tu reel… esto entrena tu Cerebro.");
    fetch("/metrics/analyze-one",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(_pidOf(S.brandId)?{url:url,project_id:_pidOf(S.brandId)}:{url:url})})
      .then(function(r){ return r.json().catch(function(){return{};}); })
      .then(function(d){
        if(d&&d.ok){
          if(g) g.published={pending:false, url:url};
          return refreshMetrics().then(function(){ showToast("Reel analizado. Tu Cerebro acaba de aprender de él."); });
        }
        showError((d&&d.error)||"No pude analizar el reel. Revisa el link.");
      })
      .catch(function(){ showError("Error de red al analizar el reel."); });
  }

  // B4 (prod): conectar la cuenta de Instagram (POST /metrics/ig-profile) y luego
  // traer/actualizar los reels (POST /metrics/analyze → scrape + attribute_and_learn).
  // T2 (IDI): sheet con validación (usuario sin @, sin espacios) en vez de window.prompt.
  function igConnectProfile(){
    promptSheet({
      title:"Conectar Instagram", label:"Tu usuario de Instagram",
      placeholder:"tu_usuario",
      helper:"Sin @. Leo solo tus métricas públicas — sin contraseñas.",
      submitLabel:"Conectar",
      validate:function(v){ v=v.trim().replace(/^@/,""); if(!v) return "Escribe tu usuario de Instagram."; if(/\s/.test(v)) return "El usuario no lleva espacios."; },
      onSubmit:function(v){ igConnectWith(v.trim().replace(/^@/,"")); }
    });
  }
  function igConnectWith(u){
    showToast("Conectando @"+u+"…");
    fetch("/metrics/ig-profile",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(_pidOf(S.brandId)?{username:u,project_id:_pidOf(S.brandId)}:{username:u})})
      .then(function(r){ return r.json().catch(function(){return{};}); })
      .then(function(d){
        if(d&&d.ok){
          S.igConnected=true; render();
          showToast("Instagram conectado. Trayendo tus reels…");
          return refreshReels();
        }
        showError((d&&d.error)||"No pude conectar tu Instagram.");
      })
      .catch(function(){ showError("Error de red al conectar Instagram."); });
  }
  function refreshReels(){
    showToast("Actualizando tus reels…");
    return fetch("/metrics/analyze",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(_pidOf(S.brandId)?{project_id:_pidOf(S.brandId)}:{})})
      .then(function(r){ return r.json().catch(function(){return{};}); })
      .then(function(d){
        if(d&&d.ok){
          return refreshMetrics().then(function(){ showToast("Reels actualizados ("+(d.videos_updated||0)+"). Tu Cerebro ha aprendido."); });
        }
        showError((d&&d.error)||"No pude actualizar tus reels.");
      })
      .catch(function(){ showError("Error de red al actualizar tus reels."); });
  }
  // Desvincular de verdad: el handler solo cambiaba S.igConnected en local y NUNCA
  // borraba la fila en BD → reconectar daba 409 "esta marca ya tiene perfil" en bucle.
  // Llama al DELETE /metrics/ig-profile scopeado por marca (igual que el connect). 404
  // (ya no había fila) cuenta como éxito. Limpia los reels cacheados de la marca.
  function igDisconnect(){
    showToast("Desvinculando Instagram…");
    var url="/metrics/ig-profile"+(_pidOf(S.brandId)?("?project_id="+encodeURIComponent(_pidOf(S.brandId))):"");
    apiDelete(url).then(function(r){
      if(r.ok||r.status===404){
        S.igConnected=false; if(S.metrics) S.metrics.videos=[]; render();
        return showToast("Instagram desvinculado.");
      }
      showError((r.d&&r.d.error)||"No pude desvincular Instagram.");
    });
  }

  /* ── Equipo (Agencia): invitar + cargar miembros reales ──────────
     Mirror del estilo de igConnectProfile/refreshReels (fetch same-origin).
     Backend: POST /agency/invite {email}→{invite_url,token}; GET /agency/members→[]. */
  // T2 (IDI): sheet con validación de email en vez de window.prompt.
  function teamInvite(){
    if(isDemo()){ return showToast("En la demo no se envían invitaciones reales. En tu cuenta Agencia generarías un enlace de invitación."); }
    promptSheet({
      title:"Invitar miembro", label:"Email del miembro",
      placeholder:"nombre@equipo.com",
      helper:"Le creo un enlace de invitación para unirse a tu equipo.",
      submitLabel:"Crear invitación",
      validate:function(v){ v=v.trim(); if(!v) return "Escribe un email para invitar."; if(v.indexOf("@")<1 || v.indexOf("@")===v.length-1) return "Eso no parece un email válido."; },
      onSubmit:function(v){ teamInviteWith(v.trim()); }
    });
  }
  function teamInviteWith(em){
    showToast("Creando invitación…");
    fetch("/agency/invite",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:em})})
      .then(function(r){ return r.json().catch(function(){return{};}); })
      .then(function(d){
        if(d&&d.invite_url){
          try{ if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(d.invite_url); }catch(e){}
          loadTeam();
          showToast(d.emailed?"Invitación enviada por email · enlace copiado":"Invitación creada · enlace copiado");
          return;
        }
        showError((d&&d.error)||"No pude crear la invitación.");
      })
      .catch(function(){ showError("Error de red al crear la invitación."); });
  }
  function loadTeam(){
    if(isDemo()) return;
    fetch("/agency/members",{credentials:"same-origin"})
      .then(function(r){ return r.json().catch(function(){return[];}); })
      .then(function(rows){
        if(!Array.isArray(rows)) rows=[];
        // Owner implícito (el usuario actual) primero; luego los miembros con su rol.
        var owner={ name:(S.user.email||"Owner"), role:"Owner",
          initials:(S.user.email||"O").slice(0,2).toUpperCase(), color:"#f59e0b", brands:[] };
        var members=rows.map(function(m){
          var email=m.invited_email||"miembro";
          var active=m.status==="active";
          var roleName = (m.role==="owner") ? "Owner" : (active?"Miembro":"Invitado · pendiente");
          return {
            name: email,
            role: roleName,
            initials: (m.invited_email||"M").slice(0,2).toUpperCase(),
            color: active?"#12a37c":"#6d6bf6",
            brands: []
          };
        });
        S.team=[owner].concat(members);
        if(S.tab==="team") render();
      })
      .catch(function(){});
  }

  /* ── teclado (T3, IDI): Esc cierra, Enter envía — como la chrome legacy ── */
  // T6: cerrar el orbe NO cancela el robo — sigue en background y avisa al acabar.
  function closeOverlay(){
    clearInterval(S.genStepTimer); clearTimeout(S.fillTimer); S._fillPhase=null;
    if(S.view==="gen"){
      // T6: cerrar el orbe NO cancela el robo — sigue en background. Copy David:
      S._genBg=true; S.view="feed"; render();
      showToast(L("En menos de 1 min lo tienes en Ideas robadas.","In under 1 min it'll be in your Stolen ideas."));
      return;
    }
    // F2: salir del reveal no te deja tirado en el feed — aterrizas en el workspace
    // de la idea recién robada (reel + guiones + notas). P1: ver el selector cuenta
    // como elegir (opción activa) → el reveal no se re-impone al reabrir la idea.
    if(S.view==="script" && S.revealReel && S.revealReel.id!=null){
      var _rg=S.revealReel._gid?guionById(S.revealReel._gid):null;
      if(_rg && guionChoicePending(_rg)) persistChosen(_rg, S.revealReel.optIdx||0);
      var _k=(_rg&&guionGroupKey(_rg))||("r:"+S.revealReel.id);
      return wsOpen(_k);
    }
    // Editor abierto desde un workspace → vuelve al workspace, no a la lista.
    if(S.view==="editor" && S._edFrom==="ideaws" && S._wsKey){ S._edFrom=null; S.view="ideaws"; S.tab="guiones"; return render(); }
    S.view="feed"; render();
  }
  function tpBack(){
    var back=S._tpFrom; S._tpFrom=null;
    if(back==="editor" && S.activeGuionId){ S.view="editor"; }
    else if(back==="ideaws" && S._wsKey){ S.view="ideaws"; S.tab="guiones"; }
    else if(back==="script" && (S.revealReel||(S.reel&&S.reel.script))){ S.view="script"; }
    else if(S.tab==="guiones"){ S.view="feed"; }
    else { S.view=(S.reel&&S.reel.script)?"script":"feed"; }
    render();
  }
  // Cierra lo más "encima" primero: sheet → menú de marca → overlay activo.
  // Equivalencias: result→volver al guión; prompter→tp-back; resto→close-feed.
  function onKeydown(e){
    var el=root(); if(!el || !el.offsetParent) return;   // isla no montada/visible → no interceptar
    if(e.key==="Escape"){
      if(S.sheet){ e.preventDefault(); return closeSheet(); }
      if(S.acctMenu){ e.preventDefault(); S.acctMenu=false; return render(); }
      if(S.brandMenu){ e.preventDefault(); S.brandMenu=false; return render(); }
      if(S.communityInfo){ e.preventDefault(); S.communityInfo=false; return render(); }
      if(S.galMenu){ e.preventDefault(); S.galMenu=false; return render(); }
      if(S.view && S.view!=="feed"){
        e.preventDefault();
        if(S.view==="result"){ S.view="script"; return render(); }
        if(S.view==="prompter") return tpBack();
        if(S.view==="ideaws") return wsClose();   // workspace de idea → vuelve a la lista
        return closeOverlay();
      }
      // A: Esc pliega el detalle inline del reel (no hay overlay que cerrar).
      if(S.detailReelId){ e.preventDefault(); S.detailReelId=null; return render(); }
      return;   // nada que cerrar → que lo gestione la chrome legacy
    }
    if(e.key==="Enter" && !e.shiftKey && (e.target.tagName||"").toLowerCase()!=="textarea"){
      if(S.sheet && e.target.id==="rsSheetInput"){ e.preventDefault(); return submitSheet(); }
      if(e.target.id==="rsIdeaSeed"){ e.preventDefault(); return seedIdea("rsIdeaSeed", true); }
      if(e.target.id==="rsOnbHandle"){ e.preventDefault(); return onbHandleNext(); }   // onboarding v2: Enter avanza
      if(e.target.id==="rsOnbNiche"){ e.preventDefault(); return onbNicheNext(); }
      if(e.target.id==="rsOnbTagInput"){ e.preventDefault(); return onbTagAdd(); }
      if(e.target.id==="rsOnbCompInput"){ e.preventDefault(); return onbCompAdd(); }
      if(e.target.id==="rsCompAddInput"){ e.preventDefault(); return submitAddComp(); }   // FIX2: Enter añade competidor desde el Radar
      if(e.target.id==="rsIdeaSeed2"){ e.preventDefault(); return addSeedIdea(); }
      // A: la card del reel es role=button — Enter abre el detalle (a11y teclado).
      if(e.target.getAttribute && e.target.getAttribute("data-act")==="reel-detail"){ e.preventDefault(); return openReelDetail(e.target.getAttribute("data-id")); }
      // B2 (a11y): cualquier role=button con data-act (celda Cerebro del statbar,
      // fila de competidor…) responde a Enter como al click.
      if(e.target.getAttribute && e.target.getAttribute("role")==="button" && e.target.getAttribute("data-act")){ e.preventDefault(); return e.target.click(); }
    }
    // T5: trap de foco — con un diálogo abierto, Tab circula dentro y no escapa al fondo.
    if(e.key==="Tab" && (S.sheet || (S.view && S.view!=="feed"))){
      var ov=topOverlay(el); if(!ov) return;
      var foc=ov.querySelectorAll('button,[href],input,textarea,select,[tabindex]:not([tabindex="-1"])');
      if(!foc.length) return;
      var first=foc[0], last=foc[foc.length-1], a=document.activeElement;
      if(!ov.contains(a)){ e.preventDefault(); first.focus(); return; }
      if(e.shiftKey && a===first){ e.preventDefault(); last.focus(); }
      else if(!e.shiftKey && a===last){ e.preventDefault(); first.focus(); }
    }
  }

  /* ── delegación de eventos ───────────────────────────────────── */
  function onClick(e){
    var el=root(); if(!el||!el.contains(e.target)) return;
    var btn=e.target.closest("[data-act]"); if(!btn) return;
    var act=btn.getAttribute("data-act"), id=btn.getAttribute("data-id"), k=btn.getAttribute("data-k");
    S._lastClickSel=actSelector(btn);   // T5: por si esta acción abre un diálogo — saber a quién devolver el foco
    if(act==="sheet-close") return closeSheet();
    if(act==="sheet-submit") return submitSheet();
    if(act==="sheet-secondary") return submitSheetSecondary();
    if(act==="err-close"){ S.errMsg=null; var _te=document.getElementById("rsErr"); if(_te) _te.classList.remove("show"); return; }
    if(act==="tab") return switchTab(k);
    if(act==="legacy") return openLegacy(k);
    if(act==="acct-toggle"){ S.acctMenu=!S.acctMenu; S.brandMenu=false; return render(); }
    if(act==="acct-close"){ S.acctMenu=false; return render(); }
    // Unificado: el menú abre la MISMA pantalla v3 que el rail (antes openLegacy →
    // panel legacy con tarjetas de plan no clicables = la "regresión" reportada).
    if(act==="acct-settings"){ S.acctMenu=false; return switchTab("settings"); }
    if(act==="acct-feedback"){ return openFeedback(); }
    if(act==="acct-logout"){ S.acctMenu=false;
      // Reusa el logout real de la chrome (POST /auth/logout + reset tracking + redirect).
      if(typeof window.logout==="function") return window.logout();
      try{ fetch("/auth/logout",{method:"POST",credentials:"same-origin"}); }catch(e){}
      window.location.href="/"+(document.documentElement.lang||"es")+"/"; return;
    }
    if(act==="legacy-back") return closeLegacy();
    if(act==="brand-toggle"){ S.brandMenu=!S.brandMenu; return render(); }
    if(act==="brand-close"){ S.brandMenu=false; return render(); }
    if(act==="brand") return openBrand(id);
    if(act==="all-brands"){ S.brandMenu=false; S.view="feed"; S.tab="dashboard"; return render(); }   // B3: portfolio fuera → al Radar
    if(act==="open-brand") return openBrand(id);
    if(act==="demo-plan") return setDemoPlan(k);
    if(act==="brain-rate") return brainRate(parseInt(btn.getAttribute("data-k"),10)||0);
    if(act==="brain-train-mode") return setBrainTrainMode(k);
    if(act==="brain-improve") return brainImprove(k==="send");
    if(act==="brain-levelup") return brainLevelup();   // recoger el nivel pendiente (manual)
    if(act==="levelup-done") return closeLevelup();
    if(act==="brain-feed-pick") return brainFeedPick(k);   // elegir forma de alimentar
    if(act==="brain-feed-me") return brainFeedMe(k);   // #2/#7: alimentar el Cerebro (por tipo)
    if(act==="brain-train-more") return brainTrainMore();
    if(act==="gt-start") return startGuionTinder();
    if(act==="gt-vote") return guionTinderVote(parseInt(btn.getAttribute("data-k"),10)||0);
    if(act==="gt-reset") return guionTinderReset();
    if(act==="team-invite") return teamInvite();
    if(act==="team-edit") return showToast("Gestión de roles y marcas por miembro: próximamente.");
    if(act==="brand-add") return brandCreate();
    if(act==="brand-cap"){
      S.brandMenu=false; render();
      // Agencia al tope → ofrecer marca EXTRA (+€10/mes, add-on Stripe). Resto → upgrade.
      if(isAgency() && !isDemo()){
        var doBuy=function(){
          apiPost("/billing/add-brand",{}).then(function(r){
            if(r.ok && r.d && r.d.url){ try{ window.location.href=r.d.url; }catch(e){} return; }
            if(r.d && r.d.code==="addon_not_configured"){ return showError("Las marcas extra aún no están disponibles. Vuelve pronto."); }
            showError((r.d&&r.d.error)||"No pude iniciar la compra.");
          });
        };
        if(typeof window.confirmModal==="function"){
          window.confirmModal({ title:"Añadir marca extra", body:"Suma una marca más a tu Agencia por +10€/mes. Se factura junto a tu plan.", confirmText:"Añadir por 10€/mes", cancelText:"Ahora no" }).then(function(ok){ if(ok) doBuy(); });
        } else doBuy();
        return;
      }
      showToast("Has llegado al tope de marcas de tu plan.");
      if(typeof window.openUpgradeModal==="function"){ try{ window.openUpgradeModal("brand_limit"); }catch(e){} }
      return;
    }
    if(act==="brand-rename") return brandRename(id, btn.getAttribute("data-name"));
    if(act==="brand-del") return brandDelete(id, btn.getAttribute("data-name"));
    if(act==="brand-report"){
      S.brandMenu=false; render();
      if(isDemo()) return showToast("Informe white-label del mes — disponible en tu cuenta de Agencia.");
      var pid=S.brandId||"default";
      var url="/brands/"+encodeURIComponent(pid)+"/report?lang="+encodeURIComponent((document.documentElement.lang||"es"));
      try{ window.open(url,"_blank","noopener"); }catch(e){ location.href=url; }
      return showToast("Generando el informe del mes…");
    }
    if(act==="onb-cofre-steal"){   // cofre: roba una de las 3 cartas → genera el guion (el «aha»)
      var _cof=S.onbCofre; if(!_cof || _cof.selected>=0) return;   // ya elegido
      var _idx=_cofreReels().map(function(x){return x.id;}).indexOf(id); if(_idx<0) return;
      _cof.selected=_idx; render();   // muestra «Robando @X — generando…»
      // House tour v2 (Leo 05-jul): el onboarding termina EN el reveal del guion →
      // el tour arranca ahí (paso 1 = los dos botones de esa página), no en el radar.
      S._tourAfterReveal=true;
      setTimeout(function(){ S.onbStealOffer=null; S.onbCofre=null; steal(id); }, 1100);
      return;
    }
    if(act==="onb-steal-no"){ S.onbStealOffer=null; S.onbCofre=null; render(); return onbStartTour(); }   // #6: «¡Enséñame!» → tutorial
    if(act==="steal") return steal(id);
    if(act==="opt-pick"){ var _rv=S.revealReel||S.reel; if(_rv){ applyScriptOption(_rv, parseInt(k,10)||0, 0); _rv._saved=false; render(); } return; }   // elegir opción de guion → sin guardar
    if(act==="hook-pick"){ var _rh=S.revealReel||S.reel; if(_rh){ applyScriptOption(_rh, _rh.optIdx||0, parseInt(k,10)||0); _rh._saved=false; render(); } return; }   // elegir gancho → sin guardar
    if(act==="save-script-choice") return saveScriptChoice();
    if(act==="regen") return regenInEditor(id);   // Editor: regenerar guion (1 cr)
    if(act==="reel-original"){
      // En el reveal, «ver original» SIEMPRE es el reel del guion mostrado (S.revealReel),
      // no la global S.reel — evita abrir el original de otro reel tras un 2º robo.
      var _ro=(S.view==="script" && S.revealReel && S.revealReel.id===id) ? S.revealReel
              : ((typeof reelById==="function"?reelById(id):null) || S.revealReel || S.reel || {});
      var _u=_ro.url||_ro.ig_url||_ro.permalink||(_ro.ig_reel_id?("https://www.instagram.com/reel/"+_ro.ig_reel_id+"/"):null); if(_u){ try{ window.open(_u,"_blank","noopener"); }catch(e){} } else { showToast(L("El original es de @"+((_ro.creator&&_ro.creator.handle)||"tu rival")+" en Instagram.","Original is @"+((_ro.creator&&_ro.creator.handle)||"your rival")+"'s on Instagram.")); } return; }
    if(act==="reel-dismiss") return reelDismiss(id);
    if(act==="undo-dismiss") return undoDismiss();
    if(act==="reel-detail") return openReelDetail(id);
    if(act==="reel-open"){   // #2: abrir el reel original en pestaña nueva (tocar el vídeo del detalle)
      var _ro=reelById(id);
      if(_ro&&_ro.url){ try{ window.open(_ro.url,"_blank","noopener"); }catch(e){} }
      return;
    }
    if(act==="reel-tx") return loadReelTranscript(id);
    if(act==="reel-follow"){ if(typeof window.openAddCompetitorModal==="function") window.openAddCompetitorModal(btn.getAttribute("data-handle")||""); return; }
    if(act==="creator-reels") return openCreatorReels(id, btn.getAttribute("data-handle")||"");
    if(act==="creator-reels-back") return closeCreatorReels();
    if(act==="fav") return toggleFav(id);
    if(act==="filter"){ S.filter=k; return render(); }
    if(act==="opp-nav"){ return oppNav(k); }
    if(act==="force-scrape"){ return forceScrape(); }
    if(act==="add-suggested"){
      var sh=btn.getAttribute("data-id")||"";
      // No escondemos la tarjeta: al añadir uno, sube el contador → el siguiente sugerido
      // puede quedar bloqueado (cascada del muro). El añadido se filtra como ya seguido.
      if(isDemo()){
        S.tracked=(Array.isArray(S.tracked)?S.tracked:[]).concat([{id:"sugg_"+sh, handle:sh, name:sh}]);
        if(S.trackedCount!=null) S.trackedCount++;
        showToast(L("@"+sh+" añadido a tu radar — sus reels empezarán a aparecer.","@"+sh+" added to your radar — their reels will start showing up."));
        return render();
      }
      render();                  // refresca el muro
      return _followAuthor(sh);  // sigue de verdad (POST /api/tracked-creators + refresh)
    }
    // Muro de competidores (Bernat): el free intenta el 3º → planes.
    if(act==="unlock-comp"){
      try{ if(window.posthog&&window.posthog.capture) window.posthog.capture("paywall_cta_clicked",{wall:"tracked_creators",plan:S.realPlan}); }catch(e){}
      if(typeof window.openUpgradeModal==="function"){ try{ window.openUpgradeModal("tracked_creators"); }catch(e){} }
      return;
    }
    if(act==="sugg-dismiss"){ S._suggDismissed=true; showToast(L("Vale, lo oculto.","Okay, hiding it.")); return render(); }
    if(act==="disc-dismiss"){ S._discDismissed=true; S.discover=[]; return render(); }   // #5 ocultar descubrimiento
    if(act==="sugg-dismiss-one"){   // descartar al CREADOR de una sugerencia (anti-repetición persistente)
      var dh=(btn.getAttribute("data-id")||"").toLowerCase();
      // S._suggToday son REELS → filtra por creator.handle (quita todos los reels de ese creador).
      if(Array.isArray(S._suggToday)) S._suggToday=S._suggToday.filter(function(r){ return String((r.creator&&r.creator.handle)||"").toLowerCase()!==dh; });
      if(Array.isArray(S._suggList)) S._suggList=S._suggList.filter(function(c){ return String(c.handle||"").toLowerCase()!==dh; });
      if(S._suggReal && String(S._suggReal.handle||"").toLowerCase()===dh) S._suggReal=null;
      render();
      if(!isDemo()){ var _pd=_pidOf(S.brandId); apiPost("/api/radar/suggestions/dismiss", _pd?{handle:dh, project_id:_pd}:{handle:dh}); }
      return;
    }
    if(act==="st-dismiss-all"){ S._stDismissed=true; showToast(L("Vale, lo oculto.","Okay, hiding it.")); return render(); }
    if(act==="reshuffle-sugg"){   // «↻ otras» GRATIS: pide al SERVER otras de verdad del pool
      // (criterio David 04/07 — lo ya servido hoy va al final en backend, suggserved:).
      // El apilado de re-fetches que colgaba prod (#231) lo evita el guard de 1 petición
      // en vuelo; el endpoint además ya es hang-proof (try/except, sin nonce Redis en GET).
      if(isDemo()) return;   // la demo no muestra sugerencias (loadSuggestionsToday corta)
      if(S._suggReloading) return;
      S._suggReloading=true;
      showToast(L("Trayendo otras…","Fetching others…"));
      var _rp=_pidOf(S.brandId);
      apiGet("/api/radar/suggestions"+(_rp?("?project_id="+encodeURIComponent(_rp)):"")).then(function(r){
        S._suggReloading=false;
        if(!r.ok||!r.d||!Array.isArray(r.d.suggestions)) return showError(L("No pude traer otras.","Couldn't fetch others."));
        S._suggToday=r.d.suggestions.map(_normSugg);
        S._suggHasMore=!!r.d.has_more;
        render();
      });
      return;
    }
    if(act==="sugg-more"){   // «Ver más» DE PAGO (David 06/07): la tanda inicial (8) es gratis;
      // cada «Ver más» = COST.suggmore créditos y trae una tanda COMPLETA. Cobro DIRECTO, sin
      // modal. Sin saldo → muro de recarga. Cero scrape (el pool ya está); nunca repite.
      if(isDemo()) return;
      if(S._suggMoreLoading) return;   // anti doble-clic
      if((S.user.credits||0) < COST.suggmore){ return showPaywall("no_credits"); }   // muro de recarga
      S._suggMoreLoading=true;
      var _mp=_pidOf(S.brandId), _off=parseInt(btn.getAttribute("data-offset")||"8",10)||8;
      showToast(L("Trayendo más…","Loading more…"));
      apiPost("/api/radar/suggestions/more", _mp?{project_id:_mp, offset:_off}:{offset:_off}).then(function(r){
        S._suggMoreLoading=false;
        if(r.status===402 || (r.d&&r.d.error==="no_credits")){ return showPaywall("no_credits"); }
        if(!r.ok||!r.d){ return showError(L("No pude traer más.","Couldn't load more.")); }
        var got=(Array.isArray(r.d.suggestions)?r.d.suggestions:[]).map(_normSugg);
        // DEDUP DURO por id contra lo ya cargado (contrato: ningún reel dos veces en pantalla).
        var have={}; (Array.isArray(S._suggToday)?S._suggToday:[]).forEach(function(x){ if(x&&x.id) have[x.id]=1; });
        got=got.filter(function(x){ return x&&x.id&&!have[x.id]; });
        if(got.length){ S._suggToday=(Array.isArray(S._suggToday)?S._suggToday:[]).concat(got); }
        // El backend es la fuente de verdad del saldo: sincroniza siempre; anima el gasto solo
        // si de verdad se cobró (trajo reels; agotado no cobra y no manda credits).
        if(r.d.credits!=null){ applyCredits(r.d, got.length?COST.suggmore:0); }
        S._suggHasMore=!!r.d.has_more;
        S._suggExhausted=!!r.d.exhausted;   // agotado → la card de agotamiento sustituye a «Ver más»
        render();
        // Tras el re-render el carrusel vuelve al inicio → desplazo para revelar las nuevas.
        if(got.length){ try{ var _rw=(root()||document).querySelector(".stday-row"); if(_rw) _rw.scrollTo({left:_rw.scrollWidth, behavior:"smooth"}); }catch(e){} }
      });
      return;
    }
    if(act==="close-reel-detail"){ S.detailReelId=null; return render(); }   // #4: cerrar panel/overlay del detalle
    if(act==="sugg-cap-more"){ S._suggCapFull=(S._suggCapFull===id?null:id); return render(); }   // #4: caption «ver más»/«ver menos»
    if(act==="stday-noop"){ return; }   // #4: la hoja del overlay no cierra al tocarla (solo el backdrop)
    if(act==="reshuffle-feed"){   // «↻ otras» GRATIS del feed del radar: re-baraja sin scrape
      if(isDemo()) return;
      var _fp=_pidOf(S.brandId);
      showToast(L("Barajando el radar…","Shuffling the radar…"));
      apiPost("/api/radar/reshuffle", _fp?{project_id:_fp}:{}).then(function(){ loadBrandData(); });
      return;
    }
    if(act==="stday-scroll"){   // flechas ←/→ del carrusel «Sugerencias de hoy»
      var _ss=btn.closest&&btn.closest(".stday-sec"); var _st=_ss&&_ss.querySelector(".stday-row");
      if(_st){ var _sd=(btn.getAttribute("data-dir")==="next")?1:-1; _st.scrollBy({left:_sd*Math.round(_st.clientWidth*0.82), behavior:"smooth"}); }
      return;
    }
    if(act==="set-brand-niche"){   // sin nicho → editor del proyecto (marca) o modal de nicho de usuario
      var _bp=_pidOf(S.brandId);
      if(_bp && typeof window.openProjectEditor==="function"){ try{ window.openProjectEditor(_bp); }catch(e){} }
      else if(typeof window.openNicheSetup==="function"){ try{ window.openNicheSetup(); }catch(e){} }
      else showToast(L("Define tu nicho desde los ajustes.","Set your niche from settings."));
      return;
    }
    if(act==="versus-start"){
      var opp=btn.getAttribute("data-id")||"rival";
      // oppAvg = media de views del competidor; mine = tu mejor reel.
      var oppAvg, mine, lr=lbReal();
      if(lr){
        // PROD: de las views reales ya cargadas (creator_reels_global / ig_videos).
        var row=(lr.rows||[]).filter(function(r){ return r.handle===opp; })[0];
        oppAvg=row?(row.avg_views||0):0;
        mine=(lr.you&&lr.you.best_views)||0;
      } else {
        oppAvg=_lbHash(opp+"avg",40000,160000); mine=_lbHash((S.user.handle||"me")+"best",50000,190000);
      }
      S.versus={ opp:opp, youHandle:(brand().handle||S.user.handle||"tu_cuenta"), oppAvg:oppAvg, mine:mine };
      rsTrack("versus_started", {opp:opp, mine:mine, opp_avg:oppAvg, beating:(mine>=oppAvg)});   // panel
      showToast(L("Objetivo fijado: supera la media de @"+opp+".","Goal set: beat @"+opp+"'s average."));
      return render();
    }
    if(act==="versus-quit"){ S.versus=null; showToast(L("Objetivo quitado.","Goal removed.")); return render(); }
    if(act==="community-interest"){
      try{ localStorage.setItem("rs_community_interest","1"); }catch(e){}
      try{ if(window.posthog&&window.posthog.capture) window.posthog.capture("community_interest",{from:"ranking"}); }catch(e){}
      S.user.communityWaitlist=true;
      if(!isDemo()){ try{ apiPost('/api/community/interest',{}); }catch(e){} }   // persiste en backend
      showToast(L("¡Hecho! Te avisaremos en cuanto la comunidad esté lista.","Done! We'll let you know when the community is ready."));
      return render();
    }
    if(act==="community-info"){ rsTrack("community_info_opened", {from:"radar"}); S.communityInfo=true; return render(); }   // abre mini-modal Comunidad
    if(act==="ci-close"){ S.communityInfo=false; return render(); }
    if(act==="ci-stop") return;   // clic DENTRO del modal no lo cierra
    if(act==="rgal-scroll"){
      var _sec=btn.closest&&btn.closest(".rgal"); var _tr=_sec&&_sec.querySelector(".rgal-track");
      if(_tr){ var _dir=(btn.getAttribute("data-dir")==="next")?1:-1; _tr.scrollBy({left:_dir*Math.round(_tr.clientWidth*0.82), behavior:"smooth"}); }
      return;
    }
    if(act==="gal-expand"){ S._galOpen=(S._galOpen===id)?null:id; return render(); }   // expand inline de la galería (mockup David)
    if(act==="gal-comp"){ S.galComp=(k&&S.galComp!==k)?k:null; S._galOpen=null; S.galMenu=false; return render(); }   // filtro por competidor (chips «Competidores en el radar»)
    if(act==="gal-menu"){ S.galMenu=!S.galMenu; return render(); }   // desplegable «+N» de competidores que no caben
    if(act==="expand-feed"){ S.feedExpanded=true; return render(); }
    if(act==="add-reel") return addReelManual();
    if(act==="analyze-reel") return switchTab("analizar");   // → página «Analizar reel» (historial persistido)
    if(act==="analyze-run") return analyzeRun();
    if(act==="analyze-view") return analyzeView(btn.getAttribute("data-id"));
    if(act==="analyze-copy") return analyzeCopy(btn.getAttribute("data-id"));
    if(act==="analyze-del") return analyzeDelete(btn.getAttribute("data-id"));
    if(act==="analyze-follow") return _followAuthor(btn.getAttribute("data-h"));
    if(act==="analyze-back") return analyzeBack();
    if(act==="analyze-steal") return analyzeSteal(btn.getAttribute("data-id"));
    if(act==="analyze-refresh-metrics") return analyzeRefreshMetrics(btn.getAttribute("data-id"));
    if(act==="go-guiones") return _anzGoGuiones();
    // «Ideas robadas»: abrir/cerrar el workspace de una idea + robar otro guion del mismo reel.
    if(act==="ws-open") return wsOpen(btn.getAttribute("data-id"));
    if(act==="ws-open-last") return S._lastStealKey?wsOpen(S._lastStealKey):switchTab("guiones");
    if(act==="ws-close") return wsClose();
    if(act==="ws-steal-again"){ var _wr=reelById(id); if(_wr){ _wr.options=null; _wr.script=null; S.view="feed"; steal(id); } return; }
    // growth-2: onboarding de activación
    // onboarding v2 (7 pasos)
    if(act==="onb-handle-next") return onbHandleNext();
    if(act==="onb-confirm-yes") return onbNext();        // «este eres tú» → seguir
    if(act==="onb-confirm-edit") return onbGoto("handle");   // cambiar el @
    if(act==="onb-feed-myreels") return onbFeedMyReels();    // alimentar el Cerebro con tus reels
    if(act==="onb-feed-skip") return onbNext();
    if(act==="onb-pick-niche") return onbPickNiche(btn.getAttribute("data-k"));
    if(act==="onb-niche-next") return onbNicheNext();
    if(act==="onb-tag-toggle") return onbTagToggle(btn.getAttribute("data-k"));
    if(act==="onb-tag-add") return onbTagAdd();
    if(act==="onb-sub-next") return onbSubNext();
    if(act==="onb-seed-next") return onbSeedNext();
    if(act==="onb-seed-skip") return onbSeedSkip();
    if(act==="onb-value-next") return onbValueNext();
    if(act==="onb-value-steal") return onbValueSteal(parseInt(btn.getAttribute("data-i"),10)||0);
    if(act==="onb-value-reset") return onbValueReset();
    if(act==="onb-comp-toggle") return onbCompToggle(btn.getAttribute("data-h"));
    if(act==="onb-comp-add") return onbCompAdd();
    if(act==="onb-comps-next") return onbCompsNext();
    if(act==="onb-pick-goal") return onbPickGoal(btn.getAttribute("data-k"));
    if(act==="onb-goal-next") return onbGoalNext();
    if(act==="onb-finish") return onbFinish();
    if(act==="onb-back") return onbBack();
    if(act==="onb-skip") return onbSkip();
    // F: CTA del muro borroso de métricas (y otros) → modal de upgrade.
    if(act==="upsell"){
      try{ if(window.posthog&&window.posthog.capture) window.posthog.capture("paywall_cta_clicked",{wall:(k||"metrics"),plan:S.realPlan}); }catch(e){}
      if(typeof window.openUpgradeModal==="function") window.openUpgradeModal((k||"metrics")+"_locked");
      else showToast("Pásate a Pro para desbloquear tus métricas al detalle.");
      return;
    }
    // Reusa el modal legacy global (index.html); al añadir, submitAddCompetitor
    // recarga el Radar vía window.RS_reloadRadar (puente en loadBrandData).
    if(act==="add-comp") return toggleAddComp();          // FIX2: añadir competidor inline desde el Radar
    if(act==="dns-feed"){ var _t=document.querySelector(".opp, .feature, .rgal"); if(_t) _t.scrollIntoView({behavior:"smooth", block:"center"}); return; }   // #8: «ver mi radar» → baja a las oportunidades
    if(act==="comp-add-submit") return submitAddComp();
    if(act==="refresh-radar") return refreshRadar();
    if(act==="refresh-pool") return refreshPool();
    if(act==="open-plans"){ if(typeof window.openUpgradeModal==="function"){ try{ window.openUpgradeModal("free_limit"); }catch(e){ showError(L("No pude abrir los planes. Recarga la página.","Couldn't open plans. Reload the page.")); } } return; }
    // Pill de créditos del topbar → abre el modal de planes/upgrade (camino de cobro).
    if(act==="credits-pill"){ if(typeof window.openUpgradeModal==="function"){ try{ window.openUpgradeModal("credits_pill"); }catch(e){ showError(L("No pude abrir los planes. Recarga la página.","Couldn't open plans. Reload the page.")); } } return; }
    // C1: Ajustes → Plan. "Cambiar plan" abre el modal de PLANES (checkout Whop), "Recargar
    // créditos" abre el modal de TOPUPS (openTopup, no openTopupModal que no existe).
    if(act==="change-plan"){ if(typeof window.openUpgradeModal==="function"){ try{ window.openUpgradeModal("settings_plan"); }catch(e){ showError(L("No pude abrir los planes. Recarga la página.","Couldn't open plans. Reload the page.")); } } return; }
    // Tarjeta de plan clicada: subir = checkout Whop del plan; Free = cancelar (acceso
    // hasta fin de periodo, reusa /cancel-subscription vía settingsCancelSub); bajar a un
    // plan de pago más barato = checkout del más barato (Whop hace el cambio).
    if(act==="plan-pick"){
      var pk=k||btn.getAttribute("data-k"); var cur=_ajCurTier();
      if(!pk||pk===cur) return;
      if(isDemo()){
        showToast((pk==="free"||pk==="trial")
          ? L("En la app real esto cancela tu suscripción (acceso hasta fin de periodo).","In the real app this cancels your subscription (access until period end).")
          : L("En la app real esto abre el checkout de "+_ajTier(pk).name+".","In the real app this opens the "+_ajTier(pk).name+" checkout."));
        return;
      }
      if(pk==="free"||pk==="trial"){
        if(typeof window.settingsCancelSub==="function"){ try{ window.settingsCancelSub(); return; }catch(e){} }
        if(typeof window.openUpgradeModal==="function"){ window.openUpgradeModal("settings_plan"); }
        return;
      }
      var whopKey=_ajTier(pk).whop;
      if(whopKey && typeof window.subscribePlanFromCard==="function"){ try{ window.subscribePlanFromCard(whopKey); return; }catch(e){} }
      if(typeof window.openUpgradeModal==="function"){ window.openUpgradeModal("settings_plan"); }
      return;
    }
    if(act==="recharge"){ if(typeof window.openTopup==="function"){ try{ window.openTopup(); }catch(e){ showError(L("No pude abrir la recarga. Recarga la página.","Couldn't open top-up. Reload the page.")); } } else if(typeof window.openUpgradeModal==="function"){ window.openUpgradeModal("settings_topup"); } return; }
    // Flash del muro: CTA principal = Creator −40% con WELCOME auto-aplicado (rsFlashSubscribe
    // del chrome); secundario = top-up 300. En demo no hay checkout → abre el modal de planes.
    if(act==="flash-cta"){ if(!isDemo() && typeof window.rsFlashSubscribe==="function"){ try{ window.rsFlashSubscribe("creator"); return; }catch(e){} } if(typeof window.openUpgradeModal==="function"){ try{ window.openUpgradeModal("flash_creator"); }catch(e){} } return; }
    if(act==="flash-topup"){ if(typeof window.openTopup==="function"){ try{ window.openTopup(); return; }catch(e){} } if(typeof window.openUpgradeModal==="function"){ try{ window.openUpgradeModal("flash_topup"); }catch(e){} } return; }
    // B2: CTA «Entrenar mi voz» — lleva al Cerebro y deja el cursor en el textarea
    // de captura (la acción de verdad), no en la pestaña a secas.
    if(act==="voice-focus"){
      if(S.tab!=="brain") switchTab("brain");
      setTimeout(function(){ var ta=document.getElementById("rsVoiceUrls"); if(ta){ ta.focus(); if(ta.scrollIntoView) ta.scrollIntoView({block:"center",behavior:"smooth"}); } },80);
      return;
    }
    if(act==="voice-from-urls") return voiceFromUrls();
    if(act==="set-tone"){
      var prev=S.user.presetTone; S.user.presetTone=k; render();
      if(!isDemo()){ apiPost("/api/voice/tone",{tone:k}).then(function(r){ if(!r.ok){ S.user.presetTone=prev; render(); showError("No pude guardar el tono."); } }); }
      var lbl=((S.user.presetTones||[]).filter(function(t){return t.key===k;})[0]||{}).label||k;
      showToast("Tono: "+lbl+(hasRealVoice()?" (tu voz entrenada sigue mandando).":". Tu próximo «Roba la idea» saldrá así."));
      return;
    }
    if(act==="voice-onboard") return onboardVoice();
    if(act==="voice-auto") return voiceAutoDerive();
    if(act==="voice-refine") return refineVoice();
    if(act==="next-series-go"){ var nt=btn.getAttribute("data-title")||(nextSeries()&&nextSeries().title)||""; S.tab="ideas"; S.view="feed";
      if(nt && !isDemo()){ var tmpNs=makeIdea(nt, nt.length+S.ideas.length); tmpNs._saving=true; S.ideas.unshift(tmpNs); render();
        apiPost("/ideas",{raw_text:nt, language:rsLang(), develop:false}).then(function(r){
          if(r.ok && r.d && r.d.id){ tmpNs.id=r.d.id; tmpNs._server=true; tmpNs._scriptsLoaded=true; tmpNs._saving=false; render(); }
          else { tmpNs._saving=false; render(); } });
      } else { if(nt){ S.ideas.unshift(makeIdea(nt, nt.length+S.ideas.length)); } render(); }
      return showToast("Tu próxima serie, lista para multiplicar en Ideas."); }
    if(act==="fillweek") return startFillWeek();
    if(act==="idea-capture") return openIdeaCapture();
    if(act==="untrack") return untrackCreator(id, btn.getAttribute("data-handle")||"");
    if(act==="seed-go") return seedIdea("rsIdeaSeed", true);
    if(act==="seed-add") return addSeedIdea();
    if(act==="gen5ideas") return gen5ideas(btn);
    if(act==="gen5scripts") return gen5scripts(id, btn);
    if(act==="gen5hooks") return gen5hooks(id, btn);
    if(act==="idea5hooks") return genHooksFromIdea(id, btn);
    if(act==="reel5hooks") return genHooksFromReel(id, btn);
    if(act==="explosion") return explosion(btn);
    if(act==="save-script") return saveScript(id);
    if(act==="save-hook") return saveHook(id, parseInt(btn.getAttribute("data-i"),10));
    if(act==="idea-toggle"){ var idt=findIdea(id); if(idt){ idt.expanded=(idt.expanded===false); } return render(); }
    if(act==="sc-toggle"){ var sct=findScript(id); if(sct){ sct.expanded=!sct.expanded; } return render(); }
    if(act==="gui-hooks"){ var gh=guionById(id); if(gh){ gh.expanded=!gh.expanded; } return render(); }
    if(act==="gui-body-toggle"){ var gb=guionById(id); if(gb){ gb.bodyOpen=!gb.bodyOpen; } return render(); }
    if(act==="gui-use-hook"){
      var gu=guionById(id);
      if(gu&&gu.hooks){
        var ix=parseInt(btn.getAttribute("data-i"),10); var nv=gu.hooks[ix];
        if(nv!=null){
          gu.hooks[ix]=gu.hook; gu.hook=nv; gu.title=nv;   // swap optimista (espejo del backend)
          // P1: persistir (POST /scripts/<sid>/hooks/use) y reconciliar el banco.
          if(!isDemo() && gu._sid){ apiPost("/scripts/"+encodeURIComponent(gu._sid)+"/hooks/use",{index:ix}).then(function(r){ if(r.ok && r.d && Array.isArray(r.d.alt_hooks)){ gu.hooks=r.d.alt_hooks.slice(); render(); } }); }
        }
      }
      render(); return showToast("Apertura actualizada.");
    }
    if(act==="gui-del-hook"){
      var gd=guionById(id);
      if(gd&&gd.hooks){
        var dix=parseInt(btn.getAttribute("data-i"),10);
        gd.hooks.splice(dix,1); if(!gd.hooks.length) gd.expanded=false;
        // P1: persistir (DELETE /scripts/<sid>/hooks?index=N) y reconciliar.
        if(!isDemo() && gd._sid){ apiDelete("/scripts/"+encodeURIComponent(gd._sid)+"/hooks?index="+dix).then(function(r){ if(r.ok && r.d && Array.isArray(r.d.alt_hooks)){ gd.hooks=r.d.alt_hooks.slice(); if(!gd.hooks.length) gd.expanded=false; render(); } }); }
      }
      return render();
    }
    if(act==="gen-background") return closeOverlay();   // T6: seguir navegando (el robo sigue detrás)
    if(act==="chain") return chain(k);
    if(act==="record"){ S._tpFrom=S.view; return openLoopPrompter(); }
    if(act==="recorded") return recorded();
    if(act==="back-script"){ S.view="script"; return render(); }
    if(act==="close-feed") return closeOverlay();
    if(act==="tp-back") return tpBack();
    if(act && act.indexOf("tp-")===0 && !S.tp) S.tp={ playing:false, speed:3, fontPx:34, mirror:false, recording:false, ms:0, countdown:0 };
    if(act==="tp-play"){ S.tp.playing=!S.tp.playing; return render(); }
    if(act==="tp-restart"){ var _e=document.getElementById("rsTpScroll"); if(_e) _e.scrollTop=0; return; }
    if(act==="tp-speed"){ S.tp.speed=Math.max(1,Math.min(6,S.tp.speed+(k==="up"?1:-1))); return render(); }
    if(act==="tp-font"){ S.tp.fontPx=Math.max(22,Math.min(56,S.tp.fontPx+(k==="up"?4:-4))); return render(); }
    if(act==="tp-mirror"){ S.tp.mirror=!S.tp.mirror; return render(); }
    if(act==="tp-record"){ if(S.tp.recording){ S.tp.recording=false; S.tp.playing=false; clearInterval(S.tpTimer); return render(); } tpRecordCountdown(); return; }
    if(act==="ig-connect"){ if(!isDemo()) return igConnectProfile(); S.igConnected=true; bumpEco(0,0); render(); return showToast("Instagram conectado. El sistema empezará a aprender de lo que publicas."); }
    if(act==="ig-disconnect"){ if(!isDemo()) return igDisconnect(); S.igConnected=false; render(); return showToast("Instagram desvinculado."); }
    if(act==="metric-sort"){ S.metricSort=k; return render(); }
    if(act==="metric-chart"){ S.metricChart=k; return render(); }
    if(act==="metric-view"){ S.metricView=k||"resumen"; return render(); }
    if(act==="metric-refresh"){ if(!isDemo()) return refreshReels(); render(); return showToast("Métricas actualizadas."); }
    if(act==="fw-guiones"){ S.view="feed"; S._fillPhase=null; S.tab="guiones"; S.guiFilter="all"; return render(); }
    if(act==="fw-record"){ var fid=S._fillGuionIds&&S._fillGuionIds[0]; var g0=fid?guionById(fid):null; if(g0){ S.activeGuionId=g0.id; S.reel={creator:{handle:(g0.from||"").replace("@","")},script:{hook:g0.hook,beats:g0.beats,close:g0.close}}; openLoopPrompter(); } return; }
    if(act==="gui-filter"){ S.guiFilter=k; return render(); }
    if(act==="gui-approval-filter"){ S.guiApproval=k; return render(); }
    if(act==="gui-approve"){
      var ga=guionById(id);
      if(ga){
        ga.approval=(ga.approval==="approved")?"pending":"approved";
        render(); showToast(ga.approval==="approved"?"Guion aprobado · listo para cliente.":"Aprobación retirada.");
        if(!isDemo() && ga._sid){ apiPatch("/scripts/"+encodeURIComponent(ga._sid),{approval_status:ga.approval}); }
      }
      return;
    }
    if(act==="gui-record"){ var g=guionById(id); if(g){ S._tpFrom=S.view; S.activeGuionId=g.id; S.reel={creator:{handle:(g.from||"").replace("@","")},script:{hook:g.hook,beats:g.beats,close:g.close}}; openLoopPrompter(); } return; }
    if(act==="gui-open"){ var go=guionById(id); if(go){ S._edFrom=(S.view==="ideaws")?"ideaws":null; S.activeGuionId=go.id; S.reel={id:go.from,creator:{handle:(go.from||"").replace(/^@/,"")},script:{hook:go.hook,beats:go.beats,close:go.close},dur:"",views:go.srcViews||"",likes:go.srcLikes||"",when:go.when||"",thumb:go.thumb||null,url:go.url||null}; S.view="editor"; render();
      // Si no tenemos miniatura/URL en sesión, las pedimos al backend (resuelve por la
      // FK del reel fuente). Cubre guiones de sesiones anteriores / tras recargar.
      if(!isDemo() && go._sid && (!go.thumb || !go.url)){
        apiGet("/scripts/"+encodeURIComponent(go._sid)+"/source").then(function(r){
          if(r&&r.ok&&r.d&&(r.d.url||r.d.thumb_b64)){
            go.thumb=go.thumb||r.d.thumb_b64||null; go.url=go.url||r.d.url||null;
            if(S.view==="editor" && S.activeGuionId===go.id){ S.reel.thumb=go.thumb; S.reel.url=go.url; render(); }
          }
        });
      }
    } return; }   // v3 «Abrir guion» → editor (miniatura/URL del reel fuente: sesión + backend on-demand)
    if(act==="ed-close"){ S.activeGuionId=null; if(S._edFrom==="ideaws"&&S._wsKey){ S._edFrom=null; S.view="ideaws"; } else { S.view="feed"; } S.tab="guiones"; return render(); }   // v3 editor → vuelve al workspace o a la lista de Ideas robadas
    // v3 Ajustes (página isla)
    if(act==="set-tab"){ S.setTab=k; return render(); }
    if(act==="aj-tone"){ ajustesInit(); S.setTones[k]=!S.setTones[k]; return render(); }   // Ajustes (set-tone colisionaba con el selector de tono del Cerebro)
    if(act==="set-pref"){ ajustesInit(); S.setPrefs[k]=!S.setPrefs[k]; return render(); }
    if(act==="ajustes-edit"){ return openLegacy("settings"); }   // perfil/cuenta real → ajustes legacy
    if(act==="ajustes-save-voice"){ showToast(L("Voz guardada — tus próximos guiones salen con este tono.","Voice saved — your next scripts come out in this tone.")); return; }
    if(act==="aj-invoice"){ showToast(L("Descarga de facturas disponible en producción.","Invoice download available in production.")); return; }
    if(act==="noop"){ return; }
    if(act==="ed-usehook"){ var eg=guionById(id); var ei=parseInt(btn.getAttribute("data-i"),10); if(eg&&eg.hooks&&eg.hooks[ei]!=null){ var prev=eg.hook; eg.hook=eg.hooks[ei]; eg.hooks[ei]=prev; if(eg.title===prev) eg.title=eg.hook; } return render(); }   // v3: elegir variante de gancho
    if(act==="gui-duplicate"){ var gd=guionById(id); if(gd){ var nid=addGuion({title:gd.title,hook:gd.hook,beats:(gd.beats||[]).slice(),close:gd.close,hooks:(gd.hooks||[]).slice(),from:gd.from,type:gd.type}); var ng=guionById(nid); if(ng){ ng.mult=gd.mult||gd.fromMult; } render(); showToast(L("Guion duplicado — listo para retocar.","Script duplicated — ready to tweak.")); } return; }
    if(act==="gui-toggle-rec"){ var g2=guionById(id); if(g2){ g2.status=(g2.status==="recorded")?"draft":"recorded"; if(g2.status==="recorded"&&S.stats) S.stats.stolen_today+=1; render(); showToast(g2.status==="recorded"?"Marcado como grabado.":"Vuelto a borrador."); persistRecStatus(g2); } return; }
    if(act==="gui-discard"){ var g3=guionById(id); if(g3){ g3.status="discarded"; S._lastDiscarded=id; render(); showToast("Descartado.","Deshacer","undo-discard"); persistRecStatus(g3); } return; }   // T9: descartar siempre con vuelta atrás
    if(act==="undo-discard"){ var gU=S._lastDiscarded?guionById(S._lastDiscarded):null; S._lastDiscarded=null; if(gU){ gU.status="draft"; persistRecStatus(gU); render(); showToast("Recuperado — vuelve a «Por grabar»."); } return; }
    if(act==="gui-perf"){ S.perfGuion=id; S.view="perf"; return render(); }
    if(act==="gui-link-reel"){ var gl=guionById(id); if(gl){
      // T2 (IDI): sheet con validación de URL en vez de window.prompt.
      promptSheet({
        title:"Vincular reel publicado", label:"Link del reel en Instagram",
        placeholder:"https://www.instagram.com/reel/…",
        helper:"Lo analizo y entrena tu Cerebro: aprendo de cómo tracciona lo que publicas.",
        submitLabel:"Vincular y analizar",
        validate:function(v){ if(!/^https?:\/\/\S+\.\S+/i.test(v.trim())) return "Pega el link completo del reel (empieza por http)."; },
        onSubmit:function(u){ u=u.trim(); gl.published={pending:true, url:u}; gl.status="recorded"; render(); if(isDemo()){ showToast("Reel vinculado. Se analizará en el próximo refresco y entrenará tu Cerebro."); } else { linkReelPublished(gl, u); } }
      });
    } return; }
    if(act==="copy"){ var txt=withWatermark(btn.getAttribute("data-txt")); if(navigator.clipboard) navigator.clipboard.writeText(txt); btn.textContent="✓"; setTimeout(function(){ btn.textContent="Copiar"; },1200); maybeUpgradeNudge("copy"); return; }   // growth-3: copiar = éxito → nudge
    if(act==="upsell-nudge"){ if(typeof window.openUpgradeModal==="function") window.openUpgradeModal("post_first_success"); return; }
  }

  /* ════════════════════════════════════════════════════════════════
     CARGA DE DATOS
     ════════════════════════════════════════════════════════════════ */
  function setDevice(){ S.device=window.matchMedia("(max-width:720px)").matches?"mobile":"desktop"; }
  function _bootInnerHTML(){ return '<div class="rs-boot">'+
      '<div class="rs-boot-orb">'+
        '<div class="rs-boot-core">'+IC.bolt+'</div>'+
        '<svg class="rs-boot-ring" viewBox="0 0 100 100" fill="none" aria-hidden="true"><circle cx="50" cy="50" r="46" fill="none" stroke="var(--brand-500)" stroke-width="4" stroke-linecap="round" stroke-dasharray="289" stroke-dashoffset="220"></circle></svg>'+
      '</div>'+
      '<div class="rs-boot-tt"><span class="rs-boot-label">'+L("Cargando Reelscript","Loading Reelscript")+'</span>'+
        '<div class="rs-boot-dots" aria-hidden="true"><span></span><span></span><span></span></div>'+
      '</div>'+
    '</div>'; }
  function skeletonHTML(){ return railHTML()+'<div class="work">'+cmdHTML()+'<div class="scroll"><div class="canvas">'+_bootInnerHTML()+'</div></div></div>'; }
  // Boot a PANTALLA COMPLETA limpia (sin rail/cmd): para el primer arranque y para
  // usuarios que aún no han pasado el onboarding — así NO se ve "la app" detrás antes
  // de que aparezca el onboarding (reusa el lienzo full-screen de .onb-fs).
  function bootFullHTML(){ return '<div class="onb-fs rs-boot-fs">'+_bootInnerHTML()+'</div>'; }

  // DEMO MVP: siembra guiones (con HOOKS agrupados + métricas de publicación) y un
  // perfil de métricas con reels VINCULADOS a sus guiones — para ver el loop completo.
  function seedDemoContent(){
    if(!S._seeded){
      S._seeded=true;
      S.guiones=[
        { id:"gd1", seq:1, title:"Llevo 3 semanas sin tocar mi bandeja de entrada",
          hook:"Llevo 3 semanas sin tocar mi bandeja de entrada. Y no, no la estoy ignorando.",
          beats:["Te lo cuento porque me devolvió 6 horas a la semana.","Paso uno: conectas tu correo a una herramienta.","Paso dos: la IA etiqueta cada email.","Paso tres: te deja el borrador escrito."],
          close:"Guárdate esto, que mañana subo la plantilla.",
          from:"@nick_saraev", brand:brand().name, type:"guión", status:"recorded", expanded:true,
          hooks:["Si pasas más de 30 min al día en el correo, esto es para ti.","Mi IA respondió 212 emails este mes. Yo revisé 11.","Te enseño el flujo que borró el correo de mi lista de tareas."],
          published:{ views:1400000, likes:112000, dur:"0:41", vsMedian:5.8 } },
        { id:"gd2", seq:2, title:"El prompt de 9 palabras que arregla ChatGPT",
          hook:"Hay 9 palabras que cambian por completo cómo te responde ChatGPT.",
          beats:["El problema no es la IA, es cómo le pides las cosas.","Las 9 palabras: «antes de responder, hazme las preguntas que necesites».","De repente deja de inventar."],
          close:"Copia esta frase y cuéntame qué cambió.",
          from:"@aiwithanna", brand:brand().name, type:"guión", status:"recorded",
          hooks:["Llevas usando ChatGPT mal todo este tiempo.","Deja de pedirle cosas como si fuera Google."],
          published:{ views:680000, likes:54000, dur:"0:38", vsMedian:3.2 } },
        { id:"gd3", seq:3, title:"Automaticé mi facturación de freelance en una tarde",
          hook:"El año pasado perdí 2.000€ en facturas que olvidé enviar. Este año, imposible.",
          beats:["Monté un sistema de la propuesta a la factura cobrada.","Se genera sola y manda recordatorios.","Yo solo me entero cuando entra el dinero."],
          close:"Si facturas a mano, guárdate esto.",
          from:"@marcbuilds", brand:brand().name, type:"guión", status:"draft", hooks:[] }
      ];
    }
    S.igConnected=true;
    S.metrics={
      connected:true, analyses_left:"1/1",
      // B1 (demo): sugerencia de próxima serie sembrada (en prod sale de /api/metrics/insights).
      insights:{ what_works:[], next:{ title:"Tu sistema de correo en 3 partes — uno por día", views:1400000, message:"Tu reel del correo petó (5,8× tu media). Estíralo en una miniserie: el problema, el montaje y el resultado. Misma vena, tres piezas." } },
      top:{ title:"Llevo 3 semanas sin tocar mi bandeja", views:"1,4 M" },
      learned:["Tus reels de ~40s superan tu media de vistas","Abrir con pregunta te funciona (3 de tus mejores lo hacen)","Los hooks de «yo hice X y pasó Y» rinden 2,4× más que los de pregunta"],
      videos:[
        { cap:"Llevo 3 semanas sin tocar mi bandeja de entrada. Y no, no la estoy ignorando.", views:1400000, likes:112000, comments:840, shares:31000, dur:"0:41", date:"hace 6 d", pubAt:"2026-06-15T18:00:00", top:true, viral:true, from_guion:"Llevo 3 semanas sin tocar mi bandeja de entrada", vsMedian:5.8 },
        { cap:"Hay 9 palabras que cambian por completo cómo te responde ChatGPT.", views:680000, likes:54000, comments:420, shares:12400, dur:"0:38", date:"hace 12 d", pubAt:"2026-06-09T20:30:00", top:true, from_guion:"El prompt de 9 palabras que arregla ChatGPT", vsMedian:3.2 },
        { cap:"Mi setup de creador en 2026: cámara, luz y el truco del audio.", views:90000, likes:5400, comments:80, shares:760, dur:"1:10", date:"hace 18 d", pubAt:"2026-06-03T08:00:00" },
        { cap:"El año pasado perdí 2.000€ en facturas que olvidé enviar. Este año, imposible.", views:210000, likes:16000, comments:190, shares:3800, dur:"0:33", date:"hace 22 d", pubAt:"2026-05-30T19:00:00", from_guion:"Automaticé mi facturación de freelance en una tarde", vsMedian:1.6 }
      ]
    };
  }

  // F9: re-hidrata S.guiones desde /scripts — p.ej. tras el onboarding, que persiste
  // el guion «aha» server-side sin que el cliente lo sepa (el tab decía «Aún nada aquí»
  // toda la primera sesión). Conserva los guiones solo-locales (sin _sid) por si hay
  // un robo en vuelo.
  function reloadScripts(){
    if(isDemo()) return Promise.resolve();
    var _pq=(S.brandId&&S.brandId!=="default")?("?project_id="+encodeURIComponent(S.brandId)):"";
    return apiGet("/scripts"+_pq).then(function(r){
      var rows=Array.isArray(r.d)?r.d:null; if(!rows) return;
      var locals=(S.guiones||[]).filter(function(g){ return !g._sid; });
      S.guiones=locals.concat(rows.filter(function(s){return s.recording_status!=="discarded";}).map(normScript));
    });
  }
  function loadBrandData(){
    var el=root(); if(!el) return;
    try{ window.RS_reloadRadar=loadBrandData; }catch(e){}   // puente: el chrome legacy recarga el Radar tras añadir competidor
    S.creatorFilter=null; S.creatorReels=null; S.detailReelId=null;   // A+B: al cambiar de marca no arrastres la vista de otro competidor
    S._lbReal=null;   // ranking por-marca: fuerza recarga de /api/leaderboard de ESTA marca (no caché de la anterior)
    S._suggToday=undefined; S._stLoading=false; S._stDismissed=false; S._suggNeedsNiche=false; S._suggExhausted=false;   // sugerencias POR MARCA: recarga para el nicho de ESTA marca
    S._suggPoolStatus=''; S._suggPollN=0; _clearSuggPoll();   // resetea el poll de «poblando» al cambiar de marca
    el.className="rs app "+(S.device==="mobile"?"rs--mobile":"rs--desktop");   // grid rail+work YA en el skeleton (si no, el rail sale centrado sobre negro)
    // Onboarding pendiente (o demo ?onb=1) → loader full-screen limpio, sin que asome la
    // chrome de la app antes de montar el onboarding. Si ya pasó el onboarding → skeleton normal.
    el.innerHTML=((!isDemo() && !S.user.onbV2Done) || (isDemo() && S.onb && S.onb._force)) ? bootFullHTML() : skeletonHTML();
    var q=S.brandId?("?brand="+encodeURIComponent(S.brandId)):"";
    // P0 aislamiento: el backend filtra por project_id ("brand" lo ignoraba) —
    // stats y feed de señales salían mezclados entre marcas. "default" → sin filtro.
    var _pq=(S.brandId&&S.brandId!=="default")?("?project_id="+encodeURIComponent(S.brandId)):"";
    // Perf entrada (04/07): OLA CRÍTICA = solo stats + feed de reels → es lo que el primer
    // Radar necesita (hero, oportunidades, feed). El resto (métricas/voz/insights/videos e
    // ideas/guiones — pestañas propias, que recargan bajo demanda) va en una ola DIFERIDA
    // tras render(), sin bloquear el primer paint. Antes los 8 fetches bloqueaban en un
    // solo Promise.all → el Radar no aparecía hasta que volvían todos.
    Promise.all([
      fetch("/api/radar/stats"+_pq,{credentials:"same-origin"}).then(function(r){return r.json();}).catch(function(){return{};}),
      fetch("/api/tracked-creators/reels"+(_pq?_pq+"&":"?")+"sort=explosion&limit=24",{credentials:"same-origin"}).then(function(r){return r.json();}).catch(function(){return{reels:[]};})
    ]).then(function(res){
      var stats=res[0]||{}, feed=res[1]||{};
      S.stats={ competitors:stats.competitors||0, reels_week:stats.reels_week||0, exploded_week:stats.exploded_week||0, stolen_today:stats.stolen_today!=null?stats.stolen_today:(stats.stolen_total||0) };
      S.reels=(feed.reels||[]).map(normReel);
      S.radarSeed=!!feed.seed;   // SPEC #3: el radar viene del seed del nicho (sin competidores)
      loadTracked();   // T3: lista de competidores seguidos (manejable en Cerebro)
      S.favs={}; S.reels.forEach(function(r){ if(r.fav) S.favs[r.id]=true; });
      S._reelPool=S.reels.slice();   // pool base para variar feed por-marca en demo
      if(isDemo()) seedDemoContent();   // MVP demo: SIEMPRE siembra guiones+hooks+reels vinculados
      if(isDemo() && !(isAgency() && S.tab==="portfolio")) applyDemoBrand();
      if(!isDemo() && isAgency()){ S.team=[]; loadTeam(); }   // S.team=[] antes de render: evita que teamHTML caiga al pool demo mientras loadTeam (async) resuelve; loadTeam re-renderiza al volver
      render();               // PRIMER PAINT del Radar — no espera a la ola diferida
      brainLevelPulse();
      _loadDeferredBrandData(q, _pq);   // métricas/voz/insights/videos/ideas/guiones en background
    });
  }

  // Ola DIFERIDA (perf 04/07): lo que NO hace falta para el primer Radar. Re-renderiza al
  // volver. GUARDAS: el render crítico ya corrió con S.metrics=null / S.voice sin fijar /
  // S.guiones=[] / S.ideas=[] — metricVideos() y hasRealVoice() ya toleran ese estado. En
  // DEMO no pisa lo sembrado por seedDemoContent (writes de métricas/ideas gateados por !isDemo).
  function _loadDeferredBrandData(q, _pq){
    Promise.all([
      fetch("/metrics/summary"+q,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
      fetch("/api/voice",{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
      fetch("/api/metrics/insights"+_pq,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
      fetch("/metrics/videos"+q,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
      // Contenido REAL del usuario (solo prod): ideas guardadas + guiones persistidos.
      // /ideas y /scripts filtran por project_id (no por "brand"); la marca de la isla
      // es un project. Marca "default" (sin projects) → sin filtro (todo el user).
      isDemo()?Promise.resolve(null):fetch("/ideas"+_pq,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
      isDemo()?Promise.resolve(null):fetch("/scripts"+_pq,{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;})
    ]).then(function(res){
      var met=res[0], ins=res[2], vids=res[3], ideasRows=res[4], scriptRows=res[5];
      if(res[1]) S.voice=res[1];   // perfil de voz real (moat) — null en demo dummy
      // Métricas: NO tocar en demo (seedDemoContent ya sembró S.metrics con datos falsos).
      if(!isDemo()){
        if(met){ S.metrics=met; S.igConnected=!!(met && met.connected); }
        else { S.metrics=null; }
        // /metrics/summary solo trae agregados; los reels reales viven en /metrics/videos.
        if(vids){ S.metrics=S.metrics||{}; S.metrics.videos=(vids.videos||[]).map(normMetricVideo); }
        // Insights del Cerebro (lo que funciona en TU cuenta + el siguiente de la serie).
        if(ins){ S.metrics=S.metrics||{}; S.metrics.insights={ what_works:ins.what_works||[], next:ins.next||null }; }
        // Hidrata Ideas (con sus guiones por idea_id) y Guiones desde el backend.
        var rows=Array.isArray(scriptRows)?scriptRows:[];
        S.guiones=rows.filter(function(s){return s.recording_status!=="discarded";}).map(normScript);
        // _sid → guionId local, para enlazar las script-cards de Ideas con su guión ya
        // persistido (así saveHook no duplica el guión: reusa el existente).
        var guBySid={}; S.guiones.forEach(function(g){ if(g._sid) guBySid[g._sid]=g.id; });
        var byIdea={}; rows.forEach(function(s){ if(s.idea_id){ (byIdea[s.idea_id]=byIdea[s.idea_id]||[]).push(s); } });
        var irows=Array.isArray(ideasRows)?ideasRows:[];
        S.ideas=irows.map(function(i){
          var it=normIdea(i); it._scriptsLoaded=true;
          (byIdea[i.id]||[]).forEach(function(s){
            var sb=makeScriptFromServer(s, it.text);
            if(s.recording_status!=="discarded" && guBySid[s.id]){ sb.saved=true; sb.guionId=guBySid[s.id]; }
            it.scripts.push(sb);
          });
          if(it.scripts.length) it.expanded=true;
          return it;
        });
      }
      render();
      brainLevelPulse();   // por si los guiones recién cargados suben el nivel del Cerebro
    });
  }

  // Lee la sección de la URL (/profile/<x>) y la traduce al rail de la isla:
  //   radar/overview/dashboard → Radar (deja el default)  ·  scripts → Guiones
  //   ideas → Ideas · metrics → Métricas · brain → Cerebro · transc(riptions) → Analizar
  //   settings → Configuración · sin equivalente → Radar (default).
  // Los deep-links del demo (?plan/?t=) se aplican DESPUÉS y siguen mandando.
  function _routeFromPath(){
    var seg=""; try{ var m=(location.pathname||"").match(/^\/profile\/([^\/?#]+)/); seg=m?m[1].toLowerCase():""; }catch(e){}
    if(!seg) return;
    if(seg==="transc"||seg==="transcriptions"){ S._pendingLegacy="transc"; return; }
    if(seg==="settings"){ S._pendingLegacy="settings"; return; }
    // Cualquier /profile/<x> con sección → tab del rail. Sin equivalente → "dashboard" (Radar).
    // team: oculto temporalmente → cae al default (render lo re-normaliza también).
    S.tab = ({scripts:"guiones", guiones:"guiones", ideas:"guiones", metrics:"metrics",
              brain:"brain", cerebro:"brain", portfolio:"dashboard",
              radar:"dashboard", overview:"dashboard", dashboard:"dashboard"})[seg] || "dashboard";
  }
  function loadAll(){
    setDevice();
    var el=root(); if(!el) return;
    el.className="rs app "+(S.device==="mobile"?"rs--mobile":"rs--desktop");   // grid rail+work YA en el skeleton de arranque (si no, rail centrado sobre negro)
    el.innerHTML=bootFullHTML();   // primer arranque: loader full-screen LIMPIO (sin chrome) hasta saber si es onboarding
    Promise.all([
      fetch("/auth/me",{credentials:"same-origin"}).then(function(r){return r.json();}).catch(function(){return{};}),
      fetch("/api/brands",{credentials:"same-origin"}).then(function(r){return r.ok?r.json():{brands:[]};}).catch(function(){return{brands:[]};})
    ]).then(function(res){
      var me=res[0]||{}, bd=res[1]||{};
      if(me.user){ S.user.name=me.user.name||(me.user.email||"").split("@")[0]||""; S.user.handle=me.user.handle||(me.user.email||"").split("@")[0]||""; S.user.email=me.user.email||""; }
      if(me.credits!=null) S.user.credits=me.credits; else if(me.credits_cents!=null) S.user.credits=Math.round(me.credits_cents/18);
      if(me.topup_flash) S.user.topupFlash=me.topup_flash;   // econ §4: flash Pack 300 @ 29€
      if(me.streak!=null) S.user.streak=me.streak;
      // Plan crudo de /auth/me (puede ser "free") + guiones gratis del mes restantes.
      S.user.plan=(me.plan||(me.user&&me.user.plan))||"";
      if(me.free_lifetime_left!=null) S.user.freeLeft=me.free_lifetime_left;
      // A1: tono preset (personalidad del 1er guion sin voz personal) + opciones.
      S.user.presetTone=me.preset_tone||"viral";
      S.user.presetTones=Array.isArray(me.preset_tones)&&me.preset_tones.length?me.preset_tones:[{key:"viral",label:"Polémico/Viral"},{key:"educacional",label:"Educacional"},{key:"divertido",label:"Cercano/Divertido"},{key:"informativo",label:"Informativo"},{key:"storytelling",label:"Storytelling"}];
      S.user.hasVoice=!!me.has_voice;
      S.user.onbV2Done=!!me.onb_v2_done;   // onboarding v2: gate de la pantalla dedicada (prod)
      // reverse-trial: estado del trial (Pro capado sin tarjeta) + watermark en exports (free post-trial).
      S.user.trialActive=!!me.trial_active;
      S.user.trialDaysLeft=me.trial_days_left||0;
      S.user.trialCreditsLeft=(me.trial_credits_left!=null)?me.trial_credits_left:0;
      // Fathom 18/06: tope diario del trial (3 guiones/día) → la pill muestra "N hoy".
      if(me.trial_daily_left!=null) S.user.dayLeft=me.trial_daily_left;
      // Fathom 18/06: % del Cerebro gamificado + candado del ejercicio diario.
      if(me.brain_progress!=null) S.user.brainProgress=me.brain_progress;
      S.user.brainExDate=me.brain_exercise_date||null;
      if(me.brain_last_gain!=null) S.user.brainLastGain=me.brain_last_gain;
      S.user.communityWaitlist=!!me.community_waitlist;   // lista de espera Comunidad
      S.user.watermark=!!me.watermark;
      // Plan: en demo arranca en Agencia para ver el portfolio (toggle lo cambia);
      // en prod sale de /auth/me (profiles.plan).
      // Normaliza el plan crudo de /auth/me → modos de la isla. Prod guarda valores en
      // INGLÉS (verificado en DB: free, agency); la isla razona en creador|agencia. Sin esto
      // los Agency (plan="agency") fallaban isAgency() y perdían Portfolio/Equipo en prod.
      var _rawPlan = (me.plan||(me.user&&me.user.plan))||"free";
      S.realPlan = isDemo() ? "agency" : _rawPlan;   // plan crudo (free/creator/estudio/agency)
      S.plan = isDemo() ? "agencia" : (_rawPlan === "agency" ? "agencia" : "creador");
      // Ola Agencia B1: cap de marcas (de /api/brands) → gatea "+ Nueva marca".
      S.brandsCap = (bd.brands_cap!=null) ? bd.brands_cap : (isDemo()?10:null);
      S.brands=(bd.brands&&bd.brands.length)?bd.brands:[{id:"default",name:(me.user&&me.user.name)?me.user.name:"Mi marca",handle:S.user.handle,color:"#f97316",level:1,voice:40,reelsAnalyzed:0,scripts:0}];
      if(isDemo()){ S.brands = isAgency() ? demoBrands() : [demoBrands()[0]]; }
      S.brandId=S.brands[0].id;
      S.tab = "dashboard";   // B3: todos entran directos al Radar (sin portfolio)
      _routeFromPath();   // la isla muestra la sección de /profile/<x> en el rail
      // Demo deep-link: ?plan= ?t=<tab> ?b=<brandId> para previsualizar cualquier vista.
      if(isDemo()){ try{ var qs=new URLSearchParams(location.search);
        // econ demo: el saldo del contador refleja los grants del plan (Creator 120 ·
        // Agency 960). Sin ?plan → un saldo medio para ver el contador en acción.
        if(!S.user.credits) S.user.credits=120;
        var qp=qs.get("plan"); if(qp==="creador"){ S.plan="creador"; S.user.credits=120; S.brands=[demoBrands()[0]]; S.brandId=S.brands[0].id; S.tab="dashboard"; } else if(qp==="agencia"){ S.plan="agencia"; S.user.credits=960; S.brands=demoBrands(); S.brandId=S.brands[0].id; S.tab="dashboard"; }
        var qb=qs.get("b"); if(qb && S.brands.some(function(x){return x.id===qb;})){ S.brandId=qb; S.tab="dashboard"; }
        var qt=qs.get("t"); if(qt==="perf"){ S.tab="guiones"; S.view="perf"; S.perfGuion="gd1"; } else if(qt){ S.tab=qt; }
        // ?onb=1 → fuerza el onboarding v2 en demo (sin tocar el flujo normal/harness)
        if(qs.get("onb")==="1"){ S.onb._force=true; S.onb.skipped=false; S.onb.step="handle"; S.reels=[]; S.tracked=[]; }
        // ?free=1 → simula plan FREE en demo (para ver el muro borroso de métricas)
        if(qs.get("free")==="1"){ S._demoFree=true; S.user.trialActive=true; S.user.trialDaysLeft=5; S.user.dayLeft=3; }
      }catch(e){} }
      loadBrandData();
    });
  }

  window.RadarLoop={
    mount:function(){
      if(!root()) return;
      // Takeover definitivo: la isla ocupa todo el workspace y oculta la chrome
      // vieja (sidebar, subtabs). Las secciones legacy (Analizar/Configuración) se
      // alcanzan desde el rail. Siempre activo (ya no solo en demo).
      try{ document.body.classList.add("rs-takeover"); }catch(e){}
      if(!S._wired){ document.addEventListener("click", onClick); document.addEventListener("keydown", onKeydown); window.addEventListener("resize", function(){ var d=S.device; setDevice(); if(d!==S.device) render(); });
        // Bloque 2.6/2.7: al volver a la pestaña/ventana, reconcilia el estado de conexión IG
        // (conectar por otra vía dejaba la UI stale hasta recargar). Debounced en _reconcileConnection.
        document.addEventListener("visibilitychange", function(){ if(!document.hidden) _reconcileConnection(); });
        window.addEventListener("focus", function(){ _reconcileConnection(); });
        S._wired=true; }
      loadAll();
    },
    // T2: puente para que el CRUD legacy de asistentes (modal en index.html)
    // repinte la isla tras crear/editar/borrar. Seguro si la isla no está montada.
    refresh:function(){ try{ render(); }catch(e){} },
    // v3 editor: persiste la edición inline (contenteditable onblur) sin re-render
    // — preserva el cursor. Campos: title · hook · develop(→beats) · close.
    editSave:function(gid,field,value){
      var g=guionById(gid); if(!g) return;
      value=(value==null?"":String(value)).replace(/ /g," ").trim();
      if(field==="title") g.title=value;
      else if(field==="hook") g.hook=value;
      else if(field==="close") g.close=value;
      else if(field==="develop") g.beats=value?value.split(/\n+/).map(function(x){return x.trim();}).filter(Boolean):[];
      persistGuionText(g);   // F5: PATCH real al backend (antes la edición se perdía al recargar)
    },
    // Ideas robadas: notas del workspace (textarea oninput, sin re-render) → debounce → persistWsNotes.
    wsNotes:function(val){
      var key=S._wsKey; if(!key) return;
      S._notesByKey=S._notesByKey||{}; S._notesByKey[key]=val;
      clearTimeout(S._wsNotesT);
      S._wsNotesT=setTimeout(function(){ persistWsNotes(key); },900);
    },
    // v3 buscador del desplegable de competidores: filtra filas en DOM (sin re-render → no pierde foco)
    galSearch:function(val){
      var q=(val||"").toLowerCase().replace(/^@+/,"");
      var rows=document.querySelectorAll("#radarRoot .gal-menu-row");
      for(var i=0;i<rows.length;i++){ var h=rows[i].getAttribute("data-handle")||""; rows[i].style.display=(h.indexOf(q)>=0)?"":"none"; }
    },
    // Feedback: lee la imagen adjunta, la REESCALA/comprime en canvas (máx 1280px,
    // JPEG .8) → dataURL en S._fbImage; muestra nombre + miniatura. Sin re-render.
    fbImage:function(input){
      var f=input&&input.files&&input.files[0];
      var nm=document.getElementById("rsFbImgName"), pv=document.getElementById("rsFbImgPrev");
      if(!f || !/^image\//.test(f.type||"")){ S._fbImage=null; if(nm)nm.textContent=""; if(pv)pv.innerHTML=""; return; }
      var rd=new FileReader();
      rd.onload=function(){
        var img=new Image();
        img.onload=function(){
          var max=1280, w=img.width||1, h=img.height||1;
          if(w>max||h>max){ var s=max/Math.max(w,h); w=Math.round(w*s); h=Math.round(h*s); }
          try{ var c=document.createElement("canvas"); c.width=w; c.height=h; c.getContext("2d").drawImage(img,0,0,w,h); S._fbImage=c.toDataURL("image/jpeg",0.8); }
          catch(e){ S._fbImage=rd.result; }
          if(nm) nm.textContent=f.name;
          if(pv) pv.innerHTML='<img src="'+S._fbImage+'" alt=""><button type="button" class="fb-attach-x" onclick="try{window.RadarLoop.fbClear()}catch(e){}">'+'×'+'</button>';
        };
        img.onerror=function(){ S._fbImage=null; };
        img.src=rd.result;
      };
      rd.readAsDataURL(f);
    },
    fbClear:function(){ S._fbImage=null; var nm=document.getElementById("rsFbImgName"), pv=document.getElementById("rsFbImgPrev"), inp=document.getElementById("rsFbImg"); if(nm)nm.textContent=""; if(pv)pv.innerHTML=""; if(inp)inp.value=""; }
  };
})();
