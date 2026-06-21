# Handoff v3 — para el Claude de fable

Estado del rediseño **design system v3** (mockups de David). Pensado para que otra instancia de Claude continúe sin contexto previo.

---

## 0. Setup rápido

- **Repo / rama:** `DavidBecama/transcriptor`, rama **`onboarding-v3-align`** (brancheada de prod `v0.22.1`/`aa100ce`). `origin/prod` está **intacto** a propósito — no se mergea hasta validar.
- **Clon local de trabajo:** `C:\dev\transcriptor-v3`.
- **Arrancar demo (Windows):**
  `DEMO_MODE=1 PORT=5059 FLASK_DEBUG=0 /c/dev/transcriptor/.venv/Scripts/python.exe app.py`
  La isla vive en **http://localhost:5059/es/** (la home redirige a `/es/`). Tras editar, **reinicia el server** (Flask cachea el template) y recarga con **Ctrl+Shift+R** (la caché del navegador es la causa #1 de "no veo cambios"). El server es **flaky** (se cae solo) → si HTTP 000, relánzalo.
- **Cache-bust de la isla:** `templates/index.html` referencia `static/js/radar-loop.js?v=N`. **Sube N** en cada cambio de JS (vamos por **v=68**).

## 1. Gates (obligatorio antes de pushear)
- **Harness:** `node scripts/verify-island.mjs <URL>` → debe dar **30/30**. El script tiene hardcodeado el Chrome de macOS; en Windows usa una copia parcheada:
  `sed 's#/Applications/Google Chrome.app/Contents/MacOS/Google Chrome#C:/Program Files/Google/Chrome/Application/chrome.exe#' scripts/verify-island.mjs > /tmp/vi.mjs && node /tmp/vi.mjs http://localhost:5059`
- `node --check static/js/radar-loop.js`
- `python -m py_compile app.py`
- Al editar la UI v3 hubo que actualizar selectores del harness (lo hecho, intención intacta): detector de montaje `.phead`→`.cmd`; Guiones `.ideas-zone`→`.guiu`, `.gui-card`→`.guic`, `.gui-pill`→`.guic-badge`; T1 cuenta solo el primario VISIBLE (carrusel); test de tabs de marca → ahora valida `.brand-switch`.

## 2. Arquitectura (lo mínimo para no romper nada)
- **La isla** = `static/js/radar-loop.js` (vanilla JS, estado `S`, `render()` reconstruye `#radarRoot`). El **CSS v3 es inline** en `templates/index.html` (scope `.rs`).
- **Tabs:** `S.tab` ∈ dashboard·guiones·metrics·leaderboard·brain·**settings**(nuevo, Ajustes)·team. **Vistas overlay:** `S.view` ∈ feed·gen·**script**(reveal «aha» tras robar, `.script-hook`)·**editor**(editor v3 de guion, abierto con «Abrir guion»)·result·prompter·fillweek.
- **Móvil:** clase `.rs--mobile` en `#radarRoot` si `max-width:720px`. Hay reglas `.rs--mobile`/`@media` por componente.
- **Scroll preservado** entre repintados (no salta arriba al pulsar): en `render()`, clave `tab|view|creatorFilter`.
- **Tour (tutorial):** TODO en `index.html` (`TOUR_STEPS`, `startTour`, `renderTourStep`…). Lanzar solo el tour: **`?tour=1`** o `startTour()` en consola.
- **Tokens v3** (`.rs`): `--brand-500:#2F5BFF` (azul=marca/acción), `--success-fg`/verde (multiplicadores), `#FF8A3D` (naranja=chispazo, hardcoded). Fonts: Clash (display) · Schibsted (body) · Syne (accent/"aha") · Geist Mono (telemetría). Reglas: cero emojis (iconos Lucide 2px), CTA de robar = **«Roba la idea»** (decisión usuario, NO «Hazlo mío»), targets ≥44px, un primario por pantalla.

## 3. HECHO (todo en la rama, gates 30/30)

**7 páginas a mockup David:** RADAR (Layout A), Guiones, Cerebro, Editor de guion, Teleprónter, Ajustes, Métricas. Patrón: espina del mockup arriba + features reales conservadas/demotadas.

**Afinado RADAR:** oportunidad (eyebrow/labels en frase, sin botón X, card más alta + más aire, estrella «Guardar» 3D, 3 CTA del carrusel en azul); **«Competidores en el radar»** = chips compactos por competidor que filtran la galería + desplegable «+N» con buscador (escala a 20+, `gal-menu`); fuera la fila de tabs de marca (el cambio de marca vive en `.brand-switch` de la barra); quitados de la espina: «Tus competidores», activación, «Enséñame tu voz», «Tu próxima serie», «Más señales». Quedan: añadir competidor/reel, «Te lo sugiero», «Creaciones de la comunidad».

**CEREBRO:** solo la espina (header + anillo + «Lo que ya sé de ti» + niveles + misiones); anillo con cerebro centrado y «NIVEL» debajo más grande; nivel Estratega ya no dice «ilimitados» (el tope real es por plan, `TRACKED_CREATORS_LIMITS` en app.py: free/pro 1, creator 5, estudio 15, agency 20 +10/marca).

**ONBOARDING:** tag seleccionado legible (azul sólido + texto blanco), CTA «Sigamos», **sin «Saltar configuración»** (obligatorio), paso 5 sin flash del botón + «Seguir a N», paso 4 al mismo ancho que el resto.

**TUTORIAL:** adaptado a la nueva Radar; **Ranking antes de Métricas**; NO se cierra con Esc ni clic fuera (solo sus botones); el botón resaltado solo es clicable en pasos `hold`; tras robar explica el guión creado (paso `noFrame`, sin encuadre, texto a un lado) + pasos «Grábalo ahora» y «Roba la siguiente señal»; «Atrás» salta pasos efímeros (`ephemeral`); scroll instantáneo (sin recolocar el foco); re-consulta el target cada frame (Cerebro re-renderiza). Orden: Radar→Oportunidad→Competidores→Roba la idea→Guión creado→Grábalo ahora→Roba la siguiente señal→Guiones→Cerebro→Ranking→Métricas.

**MÓVIL:** auditado a 390px (headless) → ninguna de las 7 páginas desborda; oportunidad colapsa a 1 col; carruseles/galerías/chips scrollean en horizontal a propósito; tabla de competidores de Métricas scrollable en móvil.

**MÉTRICAS (marco David + datos reales, parcial):** la vista **Resumen** deriva de tus vídeos reales (`metricVideos()` ← `/api/metrics`): **KPIs** (Reproducciones, Interacciones, Me gusta, Comentarios, Compartidos), **Desglose de interacción** y **Top reels**. Fallback a la muestra del diseño si no hay vídeos.

**RANKING (leaderboard) — rediseñado a v3** (`leaderboardPageHTML`, clases `.rk-*`): cabecera Clash + sub + sync pill, **hero «tu posición»** (#N de M + tu valor + objetivo/momentum), lista de filas con podio (top-3 medalla), tú resaltado, growth ↑/↓ y botón **«Supéralo»** (versus). Conserva datos/handlers reales (`leaderboardRows`/`lbReal`/`versus-start`). NO había mockup de David — hecho al sistema v3.

## 4. QUEDA — para fable

1. **MÉTRICAS — completar con backend real (tu lane).** En `metResumenHTML`/`metAudienciaHTML`/`metCompetidoresHTML` (radar-loop.js) siguen como **muestra del diseño**: la **tendencia 14 días**, **ganchos que funcionan**, **temas que explotan**, **heatmap de horas**, toda la vista **Audiencia** (crecimiento, alcance por tipo, edad/género/ubicaciones) y **Competidores** (tabla, cuota de atención). El dato actual de `S.metrics.videos` no las contiene → hay que traerlas de `/api/metrics*` (IG insights). El marco visual `.mt-*` ya está; solo enchufar datos. ⚠️ La función antigua de métricas reales quedó **definida sin uso** — revisa si reaprovechas su lógica.
2. ~~Quitar la página «Analizar» + reubicar analizar-reel~~ **HECHO** (commit `cd95099`): nueva acción **«Analizar un reel»** en la barra de añadir del Radar (junto a «Añadir reel») → transcribe un reel suelto y muestra el texto SIN seguir al autor (seguirlo = botón secundario). Reusa `/transcribe`+poll `/task`. Quitado el icono «Analizar» del rail; la maquinaria legacy `transc`/`#profPanelTransc` queda **inerte** (sin punto de entrada) — fable puede borrarla del monolito cuando quiera.
3. **Chips por-competidor / galería:** funcionan; revisar con datos reales (en prod los competidores salen de `/api/tracked-creators/reels`).
4. **Merge `--no-ff` a `prod` + release** (lo coordina David). Hay un merge local de prueba (`23b5c6d`) en el clon, NO pusheado.

## 5. Notas de coordinación
- **Lane de fable:** Métricas (datos reales IG) y todo lo de backend. El frontend v3 (`ed-*`, `guic-*`, `ce-*`, `mt-*`, `aj-*`, `tp-*`, `rgal-*`, `rdr-*`, `rk-*`, `fb-*`) es CSS nuevo aislado.
- **Feedback (menú de cuenta) — COMPLETO (front + back + tabla + flujo):**
  - Front: `openFeedback`/`submitFeedback` (radar-loop.js, sheet con textarea + tipo Bug/Idea + **adjuntar imagen** comprimida en cliente y enviada base64). En demo solo hace toast.
  - Endpoint público `POST /api/feedback` (`@require_auth`): `track_event` siempre + insert best-effort en tabla `feedback`.
  - **Tabla `feedback`** creada en `supabase/migrations/20260621120000_feedback.sql` (+ reflejada en `schema.sql`): `id,user_id,type,text,page,plan,image_b64,status,credits_awarded,admin_note,resolved_by,resolved_at,created_at`. RLS on, service_role only. ⚠️ **Aplicar la migración a la Supabase de prod** por el flujo normal de migraciones (no se ha ejecutado contra prod desde aquí).
  - **Flujo admin + recompensa** (`app.py`, `@admin_required`): `GET /admin/api/feedback?status=` (cola), `GET /admin/api/feedback/<id>` (detalle con imagen), `POST /admin/api/feedback/<id>/resolve` con `{status: confirmed|implemented|rejected, credits?, note?}`. Al aprobar abona créditos al autor (`credits_cents += N×COST_CENTS`), **idempotente** (no paga dos veces). Recompensa por defecto: **bug confirmado = 25 cr, idea implementada = 10 cr** (overrideable con `body.credits`).
  - **UI del panel admin** (`index.html`, overlay `#adminPage`): nueva pestaña **«Feedback»** (entre Usuarios y Ajustes). Filtro por estado, tabla (fecha/tipo/reporte/página/estado/créditos), botones **«Confirmar +25» / «Implementar +10» / «Rechazar»** (el botón pide los créditos con prompt → override), y **«Ver»** abre modal con texto completo + captura. En `window.__DEMO__` usa datos de muestra (sin tocar la API) y en demo se muestra el **chip de entrada** del panel (en prod el chip lo decide `/auth/me`). Verificado headless: chip→panel→tabla→aprobar, 0 errores JS.
  - **Cómo restringir en prod:** el gate es `@admin_required` → `_is_admin` = email en env `ADMIN_EMAILS` (CSV) **o** `profiles.is_admin = true`. Para que solo vosotros lo veáis: poner vuestros emails de dev en `ADMIN_EMAILS` (o `is_admin=true` en esos perfiles).
- **NO** pushear skills privados (`C:\dev\_skills-privado\`). **NO** `git add -A` — añadir archivos concretos.
- Real-mode escribe en la Supabase de **prod** → solo cuentas test, limpiar.
