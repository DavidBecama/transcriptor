# Brief · Alineado v3 (rediseño 7 páginas) — estado

**Rama:** `onboarding-v3-align` (clon limpio desde prod `v0.22.1`/`aa100ce`).
**Merge:** `--no-ff` → `prod` ya hecho **en local** (commit `23b5c6d`). **Sin push** — pendiente de coordinar contigo.
**Gates (verde):** harness **30/30** (`node scripts/verify-island.mjs`) · `node --check` · `py_compile`.
**Probar:** `DEMO_MODE=1 PORT=5059 python app.py` → `http://localhost:5059/es/` (Ctrl+Shift+R). Toggle de plan DEMO arriba para Free/Pro.

---

## ✅ HECHO — las 7 páginas a tu mockup

| Página | Qué lleva |
|---|---|
| **RADAR** | hero + radar-scope animado, oportunidad (thumb limpio + insight 12×), competidores + galería, Cerebro/progreso, "Llena mi semana", "Más señales · N", barra superior limpia |
| **Guiones** | cabecera + tabs (Todos/Por grabar/Grabados), "Sin desarrollar" (cards dashed), rejilla de cards con badge de estado (Por grabar / Cocinando / Grabado), "Explosión creativa" |
| **Cerebro** | header + anillo de progreso con nodos animados, "Lo que ya sé de ti", "Niveles del Cerebro" (Aprendiz→Viral), "Misiones para subir al Nivel N" |
| **Editor de guion** | 2 columnas: documento editable (hook H1 + barra IA + bloques Gancho/Desarrollo/CTA) y rail (reel fuente, transcripción, variantes de gancho, Regenerar) |
| **Teleprónter** | barra ‹Editor + pill REC + timer, banda de lectura + marcador + fades, kickers, controles (velocidad/texto/espejo), Grabar/Detener |
| **Ajustes** | página nueva: Perfil y voz · Plan y créditos · Afiliados |
| **Métricas** | clon de las 3 vistas (Resumen / Audiencia / Competidores) + paywall Pro borroso |

**Criterio:** la espina de cada mockup arriba; las features reales que el mockup no muestra quedan **conservadas debajo** (no se ha borrado funcionalidad). Tokens v3 (azul marca · verde multiplicadores · naranja chispazo), Clash/Schibsted/Syne/Geist, botones 3D, cero emojis. Cerebro al **35%** en onboarding (no 50), según tu brief.

---

## ⏭️ QUEDA

1. **RADAR — pulido visual.** La estructura está clavada al Layout A, pero falta repaso fino contra el mockup (espaciados/proporciones, miniaturas de la galería/oportunidad, y los **chips por-competidor** que filtran la galería, que están en el mockup y aún no se filtran de verdad).
2. **CEREBRO — quitar lo que sobra.** Bajo la espina del mockup quedaron bloques de la versión anterior que **no están en tu diseño** (entrenar/votar hooks, Tinder de guiones, re-analizar perfil, analizar reel suelto, selector de tono, captura de voz, "de qué me alimento", asistentes, "lo que funciona", "de quién aprendo"). Hay que **decidir qué se mantiene y limpiar el resto** para que la página quede como el mockup.
3. **MÉTRICAS — coordinar.** El clon es **demo-data** y **pisa el `metricsHTML` con datos reales de Instagram** (lo de fable/Alberto): su función queda definida pero sin uso. Decidir entre los dos qué va a release — el look del mockup o la lógica real (o el marco visual del mockup envolviendo los datos reales).
4. **TUTORIAL (house tour) — ajustar a la nueva Radar.** El tour navega/ilumina elementos por selector; con el rediseño del Radar (hero, oportunidad, galería, barra superior) muchos targets cambiaron → revisar los pasos y el spotlight para que apunten a los elementos nuevos.
5. **Versión MÓVIL — arreglar.** Repaso responsive de las 7 páginas: hay reglas `.rs--mobile` puestas por bloque, pero falta probar de verdad en móvil (rejillas a 1 columna, rails laterales, tablas de Métricas/Competidores, editor/teleprónter, targets ≥44px).
6. **RANKING — falta visual.** La pestaña Ranking (leaderboard) **no se ha alineado a v3** (no entraba en las 7 páginas del mockup) → queda con el look anterior; pendiente de rediseño visual.
7. **Quitar la página «Analizar»** (panel legacy `transc`, icono micro del rail) y **reubicar la función de analizar un reel concreto** (pegar URL → transcribir/analizar sin añadirlo como competidor). Decidir dónde vive ahora (¿en «Añadir reel» del Radar? ¿en el editor? ¿en una acción suelta?).
8. **Push / release a prod.** El merge está hecho en local; falta subirlo y meterlo en tu release.

---

*Notas técnicas del rediseño:* desaparece el header universal `.phead` (cada página tiene el suyo); el editor v3 vive en `view==="editor"` y el reveal "aha" post-robo sigue en `view==="script"`; el harness se actualizó (`.phead`→`.cmd`, selectores de Guiones renombrados) manteniendo la intención de cada test.
