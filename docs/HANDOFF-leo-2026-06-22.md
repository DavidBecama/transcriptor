# Handoff Leo → David — 22-jun-2026 (día de la publi)

Resumen de lo que toqué hoy en prod + qué queda. **prod = `v0.25.7`** (subí 7 releases hoy, todos desplegados y con la app sirviendo).

## TL;DR
- Partí de tu **v0.25.0** y fui arreglando bugs que salieron con la publi. Cada fix = PR a `prod` + merge + **release publicado yo** (vía API, porque urgía en launch day). Rama de trabajo: `leo-ranking`.
- **Frontend** = `templates/index.html` + `static/js/radar-loop.js` (isla, cache **v=88**). **Backend** = `app.py` (cambios puntuales y aditivos).
- ⚠️ **El VPS quedó en detached HEAD** tras los deploys → re-engancha `prod` cuando puedas (está en el mismo commit, `8786ea5`).

## Desplegado hoy (v0.25.1 → v0.25.7)
| Tag | Qué |
|---|---|
| v0.25.1 | **Bucle 409 al conectar IG**: el «desvincular» de la isla nunca llamaba al `DELETE /metrics/ig-profile` → la fila seguía viva → reconectar daba 409 en bucle. Ahora borra de verdad. |
| v0.25.2 | `leonard@becamaconsulting.com` añadido a `UNLIMITED_EMAILS` (cuenta dev). |
| v0.25.3 | **Nombres de nivel del Cerebro unificados** (salían con 2 sets distintos: badge=Calentando/En racha… vs tarjeta=Aprendiz/Ladrón…). Fuente única `ecoLevelName` con tus nombres (Aprendiz·Imitador·Ladrón·Estratega·Viral). |
| v0.25.4 | **Transcripción de reel en la galería** no se repintaba al terminar (solo al cerrar/reabrir): el poll re-renderizaba solo si `detailReelId===id`, pero la galería usa `S._galOpen`. |
| v0.25.5 | **Miniaturas en «Tus reels que más rinden»** (métricas): el box no pintaba la `<img>` aunque el `thumbnail_b64` ya venía. |
| v0.25.6 | **Primer guion del onboarding (aha) ahora SE PERSISTE** (era efímero) → aparece en Guiones y cuenta para el nivel (0/3 → 1/3). Solo si el user no tiene ningún guion; sin cobrar. |
| v0.25.7 | **Radar nunca vacío en first-run → arregla el house tour saltando 1→8.** Ver abajo. |

### Detalle del v0.25.7 (el más jugoso)
Tras el onboarding, seguir competidores dispara el scrape **async** (Apify). El radar + el house tour cargan **antes** de que el scrape termine → 0 reels → los pasos 2-7 del tour (apuntan a `.feature`/`.rgal`/«robar»/`.script-hook`) no encuentran target → `renderTourStep` los salta → **cae del paso 1 al 8**. Además, si los competidores no tienen reels recientes, el radar se ve muerto. **Fix:** el feed (`get_tracked_creators_reels`) cae a **reels SEED del subnicho** cuando los competidores aún no tienen reels (antes el seed solo saltaba si NO había competidores). Aditivo: solo cambia el caso vacío.

## 🔴 Apify / Groq — la caída de la mañana
Durante la mañana, **generar guion fallaba** (`transcribe_error`) y **las métricas no entraban**. Causa: la transcripción usa **Apify** (descarga audio) + **Groq Whisper** (STT), y las métricas usan **Apify** — y se cayeron (crédito/rate-limit). NO era OpenRouter (eso es el LLM del guion). Por la tarde **volvió a funcionar** (robar/métricas/transcripción OK). **Confirma si recargaste algo o fue temporal**, porque es el riesgo #1 para la publi.

## Reset de cuenta a «nueva» (por si lo necesitas)
Reseteé `leonard@becamaconsulting.com` varias veces para QA del onboarding/tour. Cómo: con el service_role key, DELETE por `user_id` en `ig_videos`(antes)+`ig_profiles`, `user_tracked_creators`, `scripts`, `ideas`, `voice_profiles`, `brain_ratings`, `user_favorite_reels`; + PATCH `profiles` `{onboarding_v2_done:false, niche:null, goal:null, subniches:[], brain_progress:35, brain_exercise_date:null}` (ojo: `subniches` es NOT NULL → usar `[]`). El onboarding solo reaparece si `onb_v2_done=false` **Y** 0 competidores **Y** 0 reels (`showOnboarding()`). Conservé plan/créditos/admin.

## ⏭️ PENDIENTE — lo llevamos NOSOTROS (Leo), salvo la infra de David

### Nosotros (Leo)
1. **Página «Analizar un reel»** — volver a añadirla para que los reels analizados **se guarden allí** (hoy hay un atajo suelto, no una página donde queden persistidos).
2. **Emails de alerta** — revisar que se **mandan** de verdad + **crear el HTML de cada alerta**.
3. **Cerebro al 100%** — que el % **llegue bien a 100** con sus niveles (hoy el ejercicio diario topa en **95** —`min(95,…)`— y el máximo es **Nivel 5**); **qué hacer pasado el 100% → lo vemos luego** (hoy «Autopiloto» es solo copy, no existe nada).
4. **Onboarding → «guiones (1/4)»** — que tras el onboarding el bloque del Cerebro muestre **1/4** (hoy «1/3»; el aha ya cuenta como 1 desde v0.25.6 → falta poner el target en **4**).
5. **Conectar Instagram en la 1ª pantalla del onboarding** — así, al acabar el house tour, el user **ya tiene sus métricas** (el scrape de su perfil corre desde el principio).
6. **QA end-to-end** — conectar IG y que se muestren las métricas (flujo completo en real tras los fixes de hoy).
7. **House tour en MÓVIL** — revisar spotlight/posición/scroll por paso (`?tour=1`).
8. **Ranking → «cuota de atención del nicho»** — hoy "Próximamente"; necesita histórico agregado por competidor (Apify).

### David (infra/billing)
- **Rate limit de Apify** → **varias cuentas de Apify** + rotación de peticiones para que no se caiga por exceso (fue lo que tumbó guiones+métricas esta mañana).
- **Confirmar** si recargaste Apify/Groq o fue temporal.
- **Re-enganchar `prod`** en el VPS (detached HEAD tras los deploys, mismo commit `8786ea5`).
- **Fragilidad del deploy** (ver «Notas de deploy»).

## Notas de deploy (fragilidad observada)
- `deploy.yml` hace `docker compose down` **antes** de `up -d --build` → cualquier fallo de build = **downtime garantizado**. Hoy un fallo transitorio de Docker Hub (`auth.docker.io/token 404`) tumbó la app unos minutos; lo arreglé con un re-run. **Recomendación:** construir antes de bajar (o un solo `up -d --build` sin `down` previo).
- Mergear a `prod` NO despliega — el deploy lo dispara `release: published`.
