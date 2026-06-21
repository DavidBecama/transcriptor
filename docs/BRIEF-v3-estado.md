# Handoff v3 — para el Claude de fable

Estado del rediseño **design system v3** (mockups de David). Pensado para que otra instancia de Claude continúe sin contexto previo.

---

## 0. Setup rápido

- **Repo / rama:** `DavidBecama/transcriptor`, rama **`onboarding-v3-align`** (brancheada de prod `v0.22.1`/`aa100ce`). `origin/prod` está **intacto** a propósito — no se mergea hasta validar.
- **Clon local de trabajo:** `C:\dev\transcriptor-v3`.
- **Arrancar demo (Windows):**
  `DEMO_MODE=1 PORT=5059 FLASK_DEBUG=0 /c/dev/transcriptor/.venv/Scripts/python.exe app.py`
  La isla vive en **http://localhost:5059/es/** (la home redirige a `/es/`). Tras editar, **reinicia el server** (Flask cachea el template) y recarga con **Ctrl+Shift+R** (la caché del navegador es la causa #1 de "no veo cambios"). El server es **flaky** (se cae solo) → si HTTP 000, relánzalo.
- **Cache-bust de la isla:** `templates/index.html` referencia `static/js/radar-loop.js?v=N`. **Sube N** en cada cambio de JS (vamos por **v=65**).

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

## 4. QUEDA — para fable

1. **MÉTRICAS — completar con backend real (tu lane).** En `metResumenHTML`/`metAudienciaHTML`/`metCompetidoresHTML` (radar-loop.js) siguen como **muestra del diseño**: la **tendencia 14 días**, **ganchos que funcionan**, **temas que explotan**, **heatmap de horas**, toda la vista **Audiencia** (crecimiento, alcance por tipo, edad/género/ubicaciones) y **Competidores** (tabla, cuota de atención). El dato actual de `S.metrics.videos` no las contiene → hay que traerlas de `/api/metrics*` (IG insights). El marco visual `.mt-*` ya está; solo enchufar datos. ⚠️ La función antigua de métricas reales quedó **definida sin uso** — revisa si reaprovechas su lógica.
2. **RANKING (leaderboard) — falta visual v3.** No entraba en los 7 mockups; sigue con el look anterior (`leaderboardPageHTML`). Rediseñar al sistema v3.
3. **Quitar la página «Analizar»** (panel legacy `transc`, icono micro del rail en `railHTML`) y **reubicar la función de analizar un reel concreto** (pegar URL → transcribir/analizar sin añadirlo como competidor). Decidir dónde vive (¿«Añadir reel» del Radar? ¿editor? ¿acción suelta?).
4. **Chips por-competidor / galería:** funcionan; revisar con datos reales (en prod los competidores salen de `/api/tracked-creators/reels`).
5. **Merge `--no-ff` a `prod` + release** (lo coordina David). Hay un merge local de prueba (`23b5c6d`) en el clon, NO pusheado.

## 5. Notas de coordinación
- **Lane de fable:** Métricas (datos reales IG) y todo lo de backend. El frontend v3 (`ed-*`, `guic-*`, `ce-*`, `mt-*`, `aj-*`, `tp-*`, `rgal-*`, `rdr-*`) es CSS nuevo aislado.
- **NO** pushear skills privados (`C:\dev\_skills-privado\`). **NO** `git add -A` — añadir archivos concretos.
- Real-mode escribe en la Supabase de **prod** → solo cuentas test, limpiar.
