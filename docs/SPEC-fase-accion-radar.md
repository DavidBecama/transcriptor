# SPEC · Reforzar la fase "Acción" del loop (Radar)

> Origen: plan-acción Reelscript (Hook Model). Estado tras sesión onboarding-v2 del 2026-06-16.
> Esto es la spec de las **apuestas grandes de backend** (#3 y #4), que NO se construyen
> a ciegas: gastan Groq/Apify y escriben en Supabase prod → las construye/aprueba el equipo
> con David. Anclada a líneas reales del árbol (v0.21.35 + cambios de la sesión).

## Estado del plan original (cruzado con el código real)

| # | Palanca | Estado real |
|---|---------|-------------|
| #1 | P4 · CTA "Roba la siguiente señal" en la cinta | **HECHO** esta sesión (`conveyorHTML`/`chain('next')` en radar-loop.js). Verificado smoke 4✓ + harness 30✓. |
| #2 | P1 · Endowed progress 1/4 | La tarjeta "0/3" original es **código muerto** (`onboardingHTML()` → `''`, radar-loop.js:608). El onboarding canónico ya es el **wizard v2** (`S.onb`/`ONB_STEPS`). Endowed-progress sería un tweak menor del wizard, bajo valor → no prioritario. |
| #2(P2) | Defaults inteligentes en "Roba la idea" | **Ya resuelto** (1 clic→resultado; el doc lo confirma). Residuo: esconder dropdown asistente legacy = #6, opcional. |
| #3 | P3 · Estado vacío nunca vacío | **PENDIENTE** (esta spec). El feed `get_tracked_creators_reels()` devuelve vacío sin competidores. |
| #4 | P5 · Pre-transcripción top-N | **PENDIENTE** (esta spec). Presupuesto confirmado: **N=3 conservador**. |
| #5 | P4 · "5 hooks" formato real | Después. Endpoint nuevo + toca créditos/coste. |

## Decisiones tomadas (2026-06-16)
- **Fuente del seed (#3): SIN DECIDIR aún** — David. Opciones: editorial a mano · algorítmico (top global) · híbrido. Ver abajo: **el algorítmico ya está medio construido**.
- **Presupuesto pre-transcripción (#4): SÍ, N=3** por creador.
- **Cómo proceder: spec, no construir** — gasto/riesgo cero hasta aprobación.

---

## #3 · Seed curado: que el radar nunca esté vacío

**Problema.** Usuario nuevo sin competidores → `get_tracked_creators_reels()`
([app.py:8516](../app.py#L8516)) devuelve `{"reels": [], "total": 0, "has_more": False}`
([app.py:8544](../app.py#L8544), [:8559](../app.py#L8559)). La primera señal exige:
seguir handle → elegir competidores → **esperar el scrape async** (`scrape_creator_task`,
encolado en [app.py:6993](../app.py#L6993)/[:7390](../app.py#L7390)). Es el acantilado
del estado vacío = principal asesino de la activación.

**Hallazgo clave — el seed ALGORÍTMICO ya existe a medias.** `niche_trending_reels()`
([app.py:7002](../app.py#L7002)) ya recicla reels que petaron de creadores tageados con
el subnicho del usuario, leyendo `creators_global` + `creator_reels_global` (orden por
views, mult vs mediana), **sin scrape** (cero coste/latencia). Hoy lo usa el onboarding
aha. Es exactamente la "Cambio A · fallback curado del nicho" del plan.

**Cambio A — fallback en el feed (bajo coste).**
En `get_tracked_creators_reels()`, cuando `creator_ids` está vacío (o el page sale vacío):
- Reutilizar la lógica de `niche_trending_reels` (extraer a helper compartido, p.ej.
  `_recycled_reels(subniches, limit)`), devolviendo los reels con una marca
  `"source": "seed"` para que el front pueda etiquetarlos ("del nicho", no "de @tu_competidor").
- Front (radar-loop.js): si el reel trae `source:"seed"`, mostrar microcopy honesto
  ("Mientras llenas tu radar, esto está petando en tu nicho").
- **Decisión pendiente (David):**
  - *Algorítmico* → ya está: `_recycled_reels` sobre `creator_reels_global`. ¿OK mostrar
    creadores que el usuario no eligió? (sí, con etiqueta de nicho).
  - *Editorial* → tabla/JSON curado por nicho (set fijo de reels ganadores). Más calidad,
    más mantenimiento. Requiere taxonomía de nichos acordada.
  - *Híbrido* → editorial donde lo haya, algorítmico de relleno.
- **Sin tabla nueva** si vamos algorítmico (reusa `creator_reels_global`). Editorial sí
  pediría tabla o JSON en `app.py`.

**Cambio B — pre-warm en el onboarding (cierra el acantilado del todo).**
El onboarding v2 ya auto-trackea sugeridos (ingest en ~[app.py:7204](../app.py#L7204),
`_track_and_tag_creator` [app.py:6935](../app.py#L6935) → `scrape_creator_task`). Asegurar
que ese scrape se **dispara al entrar al wizard** (no al terminar), para que al llegar al
Radar ya haya señales reales. Combinado con Cambio A, el radar nunca aparece vacío.

**Riesgo.** Mostrar reels de no-seguidos puede confundir → mitigar con etiqueta clara y
quitar el seed en cuanto el scrape real devuelva ≥N reels propios.

---

## #4 · Pre-transcripción top-N (primer "Roba la idea" instantáneo)

**Problema.** Si el transcript no está cacheado, "Roba la idea" encola transcripción async
con polling (`transcribe_task`, [app.py:1249](../app.py#L1249)/[:1355](../app.py#L1355)) →
espera en la primera vez (el eslabón 2 de P5).

**Cambio.** En `scrape_creator_task` (tasks.py), tras guardar los reels del creador,
encolar transcripción de los **top-3 por views** (N=3 confirmado) → al primer "Roba la
idea" hay **cache hit** = instantáneo.
- Idempotencia: saltar si el reel ya tiene transcript (reusar el check de `transcribe_task`).
- Encolar como tarea aparte/baja prioridad para no bloquear el scrape.
- Marcar el reel pre-transcrito (`pretranscribed=true` o por presencia de transcript).

**Coste (presupuesto confirmado, N=3).** 3 transcripciones Groq por creador seguido, por
adelantado. Acotar: solo en el primer scrape del creador, no en refrescos. Apify ya se
paga en el scrape; el extra es Groq.

**Riesgo.** Pre-transcribir reels que el usuario nunca robará = coste hundido. N=3 y
"solo primer scrape" lo acotan. Medir ratio cache-hit antes de subir N.

---

## Orden sugerido de construcción (equipo + David)
1. Decidir **fuente del seed** (editorial / algorítmico / híbrido) + taxonomía de nichos.
2. **#3 Cambio A** (fallback algorítmico, reusa `niche_trending_reels`) — el de más ROI y
   menor coste. Extraer `_recycled_reels`, enganchar en `get_tracked_creators_reels`, marca `source:"seed"` + microcopy.
3. **#3 Cambio B** (pre-warm scrape al entrar al wizard).
4. **#4** (pre-transcripción N=3 en `scrape_creator_task`).
5. Medir activación (time-to-first-script) antes de #5 ("5 hooks" real).

## Verificación (cuando se construya)
- No es demo-verificable (toca Supabase prod + APIs). Probar en modo real con login.
- Métricas: % de radares no-vacíos en first-run; time-to-first-"Roba la idea";
  ratio cache-hit de la pre-transcripción.
