# Handoff — rama `leo-prepubli` (de Leo → David)

Fixes pre-publi + pulido móvil/desktop. **Todo frontend**, listo para mergear a prod.

## TL;DR
- Rama **`leo-prepubli`** en `origin`, brancheada de **prod v0.24.1 (`27be39d`)**.
- **18 commits**, todos con gates **30/30** (`node scripts/verify-island.mjs`).
- **Solo frontend:** `templates/index.html` + `static/js/radar-loop.js`. **Cero** backend / DB / migraciones / env / Whop.
- `prod` es ancestro de la rama → **merge limpio, sin conflictos**. `origin/prod` intacto.
- Cache de la isla en **v=82**.

## Cómo desplegar
```bash
git fetch origin
git checkout leo-prepubli          # revisar: git log origin/prod..origin/leo-prepubli
# (QA local + en un móvil real, ver abajo)
git checkout prod
git merge --no-ff leo-prepubli
git tag v0.24.2                    # o el que toque
git push origin prod --tags
# Publicar el GitHub Release  ← esto es lo que dispara el deploy (release: published)
```
Tras el deploy el VPS queda en detached HEAD → re-enganchar `prod` al commit.

## Qué se hizo (los 5 bugs pre-publi + pulido)

**Bugs pre-publi (los del handoff de David):**
- 🧠 **Cerebro pillado** — el orbe 3D WebGL vivía oculto (display:none, el anillo es CSS) pero `ensureBrain3D()` lo montaba igual → cargaba Three.js + corría el loop contra un canvas invisible saturando la GPU. Fix: no montar si el stage no es visible.
- 💳 **Pill de créditos → planes** — el pill «X créditos» del topbar ahora abre el modal de planes.
- 🎨 **Botones sin estilo v3** — `btn-3d` es solo la sombra; sin base+relleno los botones de Configuración (y el Transcribir legacy) salían como botón nativo del navegador. Arreglado (audit → 0 nativos en toda la app, claro y oscuro).
- 📱 **Móvil roto (el gordo)** — el rail inferior (`position:fixed`) no tenía `top:auto` → se estiraba a 100vh y **tapaba todo el contenido**. Fix: barra de ~62px abajo + padding del scroll.
- ✨ **Onboarding 1ª sugerencia** — las cards del paso «valor» ahora llevan **miniaturas reales** (era la primera impresión/aha sin imágenes).

**Pulido móvil real (iterando con fotos de Leo) + desktop:**
- Radar 390px: competidores (solo desplegable), bloque «Tu cerebro» apila, add-bar rejilla 2×2, «Te lo sugiero» botones, «Próximamente» 3 textos apilados, hero Ranking no se corta.
- Sheet «Apunta una idea» descuadrado (botones nowrap forzaban ancho > viewport + `.overlay{align-items:flex-end}` del monolito se filtraba a la isla).
- **Editor de guion sin scroll** en móvil (`.ed-body` overflow hidden→auto).
- **Hueco negro bajo el rail** — `@keyframes panelFadeIn` usaba `transform` en `#profPanelIdeas`, lo que rompía el `position:fixed` del rail en móvil real. Fix: el fade es solo opacidad. + rail/iconos más grandes.
- Configuración: cap de ancho (1180 centrado, como la isla); botones; quitar plan «Trial» de Plan y créditos; identidad (badge Cerebro solapaba el nombre) + bloque «Tu voz»; muletillas como chips (sin comillas dobles); quitado el slider decorativo «Nivel de humor seco».
- Modal de planes: texto duplicado (eyebrow). Tour: botón «Siguiente» se desbordaba del tooltip.

## Qué QUEDA

**🔴 Crítico para la publi (David):** mergear + tag + **publicar el Release** (el push no despliega solo).

**QA antes del release (recomendado):**
- Pasada en **móvil físico real** de las pantallas/overlays (Leo verificó headless + Radar/Ajustes en su móvil; falta barrer el resto).
- ⏭️ **Revisar el HOUSE-TOUR (tutorial) en MÓVIL y ajustarlo** — no se barrió en móvil físico; el tooltip ya es bottom-sheet en móvil pero falta verificar spotlight/posicionamiento/scroll por paso. Probar con **`/profile/radar?tour=1`**.

**Encargo de Leo (post-publi, lo puso David en «Fase 1»):** Ranking → **«cuota de atención del nicho»** (necesita histórico agregado completo por competidor vía Apify).

**Otras lanes:** Métricas IG real (audiencia/retención) → Alberto · plan Whop «300@29€» + resend de webhooks Whop → David · eventos `trial_expired`/`instagram_connected` (aparcados).

## Notas
- Probar onboarding/tour en demo: `?onb=1`/`?tour=1` en `/es/` **pierden la query** (la URL se reescribe a `/profile/radar`) → usar **`/profile/radar?onb=1`** o `?tour=1`.
- Gates: `node scripts/verify-island.mjs <URL>` (30/30) · `node --check static/js/radar-loop.js` · `python -m py_compile app.py`.
