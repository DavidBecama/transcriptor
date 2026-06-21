# Handoff v3 — para el Claude de fable

Estado del rediseño **design system v3** (mockups de David) + extras. Pensado para que otra instancia de Claude continúe sin contexto previo.

Última actualización: 2026-06-21. Rama al día en `origin` (último commit `a30ef5a`).

---

## 0. Setup rápido

- **Repo / rama:** `DavidBecama/transcriptor`, rama **`onboarding-v3-align`** (brancheada de prod `v0.22.1`/`aa100ce`). `origin/prod` está **intacto** a propósito — no se mergea hasta validar.
- **Clon local de trabajo:** `C:\dev\transcriptor-v3`.
- **Arrancar demo (Windows):**
  `DEMO_MODE=1 PORT=5059 FLASK_DEBUG=0 /c/dev/transcriptor/.venv/Scripts/python.exe app.py`
  La isla vive en **http://localhost:5059/es/** (la home redirige a `/es/`). Tras editar, **reinicia el server** (Flask cachea el template) y recarga con **Ctrl+Shift+R** (la caché del navegador es la causa #1 de "no veo cambios"). El server es **flaky** (se cae solo) → si HTTP 000, relánzalo.
- **Cache-bust de la isla:** `templates/index.html` referencia `static/js/radar-loop.js?v=N`. **Sube N** en cada cambio de JS (vamos por **v=72**).

## 1. Gates (obligatorio antes de pushear)
- **Harness:** `node scripts/verify-island.mjs <URL>` → debe dar **30/30**. El script tiene hardcodeado el Chrome de macOS; en Windows usa una copia parcheada:
  `sed 's#/Applications/Google Chrome.app/Contents/MacOS/Google Chrome#C:/Program Files/Google/Chrome/Application/chrome.exe#' scripts/verify-island.mjs > /tmp/vi.mjs && node /tmp/vi.mjs http://localhost:5059`
- `node --check static/js/radar-loop.js`
- `python -m py_compile app.py`
- Al editar la UI v3 hubo que actualizar selectores del harness (intención intacta): detector de montaje `.phead`→`.cmd`; Guiones `.ideas-zone`→`.guiu`, `.gui-card`→`.guic`, `.gui-pill`→`.guic-badge`; T1 cuenta solo el primario VISIBLE (carrusel); test de tabs de marca → ahora valida `.brand-switch`.

## 2. Arquitectura (lo mínimo para no romper nada)
- **La isla** = `static/js/radar-loop.js` (vanilla JS, estado `S`, `render()` reconstruye `#radarRoot`). El **CSS v3 es inline** en `templates/index.html` (scope `.rs`).
- **Tabs:** `S.tab` ∈ dashboard·guiones·metrics·leaderboard·brain·**settings**(Ajustes)·team. **Vistas overlay:** `S.view` ∈ feed·gen·**script**(reveal «aha» tras robar, `.script-hook`)·**editor**(editor v3 de guion, abierto con «Abrir guion»)·result·prompter·fillweek.
- **Móvil:** clase `.rs--mobile` en `#radarRoot` si `max-width:720px`. Hay reglas `.rs--mobile`/`@media` por componente.
- **Scroll preservado** entre repintados (no salta arriba al pulsar): en `render()`, clave `tab|view|creatorFilter`.
- **Tour (tutorial):** TODO en `index.html` (`TOUR_STEPS`, `startTour`, `renderTourStep`…). Lanzar solo el tour: **`?tour=1`** o `startTour()` en consola.
- **Panel admin:** overlay `#adminPage` en `index.html` (script inline propio, clases `.adm-*`). Se abre en `/admin` o `#admin`. Gate real = `@admin_required`/`_is_admin` en backend.
- **Captura de eventos (front):** helper `rsTrack(evt, props)` en radar-loop.js (no-op si `window.posthog` no está; adjunta `plan`+`demo`). El onboarding usa `onbTrack`.
- **Tokens v3** (`.rs`): `--brand-500:#2F5BFF` (azul=marca/acción), `--success-fg`/verde (multiplicadores), `#FF8A3D` (naranja=chispazo). Fonts: Clash (display) · Schibsted (body) · Syne (accent/"aha") · Geist Mono (telemetría). Reglas: cero emojis (iconos Lucide 2px), CTA de robar = **«Roba la idea»** (decisión usuario, NO «Hazlo mío»), targets ≥44px, un primario por pantalla.

## 3. HECHO (todo en la rama, gates 30/30, pusheado a origin)

### 3.1 Rediseño v3 (7 páginas a mockup David)
RADAR (Layout A), Guiones, Cerebro, Editor de guion, Teleprónter, Ajustes, Métricas. Patrón: espina del mockup arriba + features reales conservadas/demotadas.

- **Afinado RADAR:** oportunidad (eyebrow/labels en frase, sin botón X, card más alta + más aire, estrella «Guardar» 3D, 3 CTA del carrusel en azul); **«Competidores en el radar»** = chips compactos por competidor que filtran la galería + desplegable «+N» con buscador (escala a 20+, `gal-menu`); fuera la fila de tabs de marca (el cambio de marca vive en `.brand-switch` de la barra); quitados de la espina: «Tus competidores», activación, «Enséñame tu voz», «Tu próxima serie», «Más señales». Quedan: añadir competidor/reel, **analizar un reel** (ver 3.4), «Te lo sugiero», «Creaciones de la comunidad».
- **CEREBRO:** solo la espina (header + anillo + «Lo que ya sé de ti» + niveles + misiones); anillo con cerebro centrado y «NIVEL» debajo; nivel Estratega ya no dice «ilimitados» (tope real por plan, `TRACKED_CREATORS_LIMITS`: free/pro 1, creator 5, estudio 15, agency 20 +10/marca).
- **ONBOARDING:** tag seleccionado legible (azul sólido + texto blanco), CTA «Sigamos», **sin «Saltar configuración»**, paso 5 sin flash del botón + «Seguir a N», paso 4 al mismo ancho.
- **TUTORIAL:** adaptado a la nueva Radar; **Ranking antes de Métricas**; NO se cierra con Esc ni clic fuera; el botón resaltado solo es clicable en pasos `hold`; tras robar explica el guión creado (paso `noFrame`) + «Grábalo ahora» y «Roba la siguiente señal»; «Atrás» salta pasos efímeros (`ephemeral`); scroll instantáneo; re-consulta el target cada frame. Orden: Radar→Oportunidad→Competidores→Roba la idea→Guión creado→Grábalo ahora→Roba la siguiente señal→Guiones→Cerebro→Ranking→Métricas.
- **MÓVIL:** auditado a 390px (headless) → ninguna de las 7 páginas desborda.
- **MÉTRICAS (marco David + datos reales, parcial):** la vista **Resumen** deriva de tus vídeos reales (`metricVideos()` ← `/api/metrics`): KPIs, Desglose de interacción, Top reels. Fallback a la muestra si no hay vídeos. (El resto sigue de muestra → ver §4.)
- **RANKING (leaderboard) — v3** (`leaderboardPageHTML`, `.rk-*`): cabecera Clash + hero «tu posición» + lista podio + «Supéralo» (versus). Conserva datos/handlers reales (`leaderboardRows`/`lbReal`/`versus-start`). NO había mockup de David — hecho al sistema v3.

### 3.2 Feedback con recompensa en créditos (front + back + tabla + flujo + UI admin) — `804f1bd`
- **Front:** «Enviar feedback» en el menú de cuenta (`openFeedback`/`submitFeedback`, sheet con textarea + tipo Bug/Idea + **adjuntar imagen** comprimida en cliente → base64). En demo solo toast.
- **Endpoint público** `POST /api/feedback` (`@require_auth`): `track_event` + insert best-effort en tabla `feedback`.
- **Tabla `feedback`** en `supabase/migrations/20260621120000_feedback.sql` (+ `schema.sql`): `id,user_id,type,text,page,plan,image_b64,status,credits_awarded,admin_note,resolved_by,resolved_at,created_at`. RLS service_role-only. ✅ **Ya aplicada en la Supabase de prod** por Leo.
- **Flujo admin + recompensa** (`app.py`, `@admin_required`): `GET /admin/api/feedback?status=` (cola), `GET /admin/api/feedback/<id>` (detalle con imagen), `POST /admin/api/feedback/<id>/resolve` `{status: confirmed|implemented|rejected, credits?, note?}`. Al aprobar abona `credits_cents += N×COST_CENTS`, **idempotente** (no paga dos veces). Default: **bug = 25 cr, idea = 10 cr** (`FEEDBACK_REWARD`, overrideable con `body.credits`).
- **UI admin** (overlay `#adminPage`): pestaña **«Feedback»** — filtro por estado, tabla, botones Confirmar/Implementar/Rechazar (prompt = override de créditos), «Ver» = modal con texto completo + captura. En demo usa datos de muestra + fuerza el chip de entrada.

### 3.3 Panel admin simplificado — `9e1ee22`
Quitadas las pestañas **Planes, Topups y Ajustes** del overlay. Quedan **Métricas · Usuarios · Feedback**. Eliminadas sus funciones JS muertas (`loadPlans/savePlan/loadTopups/saveTopup/loadSettings/saveSettings` + helpers `field`/`sourceTag`). **Los endpoints backend `/admin/api/{plans,topups,settings}` siguen intactos** por si se reactivan.

### 3.4 «Analizar un reel» reubicado al Radar — `cd95099`
Nueva acción **«Analizar un reel»** en la barra de añadir del Radar (junto a «Añadir reel»): transcribe un reel suelto y **muestra el texto, SIN seguir al autor** (seguirlo = botón secundario del resultado). Reusa `/transcribe` + poll `/task` (cero endpoints nuevos). `promptSheet` ganó un modo `readonly` (sin input, solo contenido + cerrar; caja `.analyze-tx` scrollable). **Quitado el icono «Analizar» del rail** → la página legacy `transc`/`#profPanelTransc` queda **inerte** (sin punto de entrada); fable puede borrarla del monolito cuando quiera.

### 3.5 Analytics de producto para el becama-panel — `9f47a67`
El dashboard externo (`C:\dev\becama-panel`, `posthog_provider.py`) listaba eventos que Reelscript NO emitía. Instrumentados:
- **Backend (`app.py`):** `competitor_added` (alta tracked-creator), `trial_started` (al fijar el reverse-trial en signup, idempotente), `subscription_upgraded` + `amount_cents` (MRR del plan vía `_plan_mrr_cents`, en las 3 pasarelas stripe/paddle/whop).
- **Front (`rsTrack`):** `metrics_viewed` (entrar en Métricas), `versus_started` (objetivo en Ranking), `community_info_opened` (teaser Comunidad), `script_recorded` (chokepoint `persistRecStatus`).
- `email_sent` ya se emitía (emails.py). **Sin disparo aún** (decisión de no forzarlos): `trial_expired` (necesita job de boundary), `instagram_connected` (necesita la conexión IG de Métricas → tu lane).

## 4. QUEDA — para fable / David

1. **MÉTRICAS — completar con backend IG real (lane de fable).** En `metResumenHTML`/`metAudienciaHTML`/`metCompetidoresHTML` (radar-loop.js) siguen como **muestra del diseño**: tendencia 14 días, ganchos que funcionan, temas que explotan, heatmap de horas, toda la vista **Audiencia** y **Competidores** (tabla, cuota de atención). Hay que traerlas de `/api/metrics*` (IG insights); el marco `.mt-*` ya está. ⚠️ La función antigua de métricas reales quedó **definida sin uso** — revisar si se reaprovecha. Métricas v3 **pisa** el `metricsHTML` real (IG) de fable/Alberto → coordinar antes de release.
2. **Borrar (opcional) la página legacy «Analizar»** del monolito (`#profPanelTransc` + plumbing `openLegacy`/`legacy`): ya está inerte, sin entrada. No urge.
3. **Chips por-competidor / galería:** funcionan; revisar con datos reales (`/api/tracked-creators/reels`).
4. **Eventos panel pendientes:** `trial_expired` (job de boundary) e `instagram_connected` (cuando exista la conexión IG).
5. **Merge `--no-ff` a `prod` + release** (lo coordina David). Hay un merge local de prueba (`23b5c6d`) en el clon, NO pusheado.

## 5. Notas de coordinación
- **Lane de fable:** Métricas (datos reales IG) y todo lo de backend. El frontend v3 (`ed-*`, `guic-*`, `ce-*`, `mt-*`, `aj-*`, `tp-*`, `rgal-*`, `rdr-*`, `rk-*`, `fb-*`, `adm-*`, `analyze-tx`) es CSS nuevo aislado.
- **Restringir el panel admin en prod:** `@admin_required` → `_is_admin` = email en env `ADMIN_EMAILS` (CSV) **o** `profiles.is_admin = true`. Para que solo lo veáis vosotros: vuestros emails de dev en `ADMIN_EMAILS`.
- **NO** pushear skills privados (`C:\dev\_skills-privado\`). **NO** `git add -A` — añadir archivos concretos.
- Real-mode escribe en la Supabase de **prod** → solo cuentas test, limpiar.

## 6. Historial de commits de esta tanda (en `origin/onboarding-v3-align`)
- `804f1bd` — feedback admin + recompensa créditos + tabla
- `9e1ee22` — quitar pestañas Planes/Topups/Ajustes del panel
- `9f47a67` — analytics de producto para el becama-panel
- `cd95099` — «Analizar un reel» al Radar + quitar página Analizar
- `a30ef5a` — este brief
