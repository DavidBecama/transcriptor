# Handoff: ReelScript — App de creadores (RADAR, Guiones, Editor, Teleprónter, Métricas, Onboarding, Ajustes)

## Overview
ReelScript es un SaaS para creadores/agencias. El usuario detecta reels que están explotando en su nicho (RADAR), los "roba" para convertirlos en guiones con su voz (Editor), los graba (Teleprónter), mide qué funciona (Métricas) y gestiona su cuenta (Ajustes). Flujo central: **Radar → robar → Editor → Teleprónter → grabar**, con Métricas y Onboarding alrededor.

Este paquete cubre 7 pantallas de alta fidelidad, ambos temas (oscuro/claro) y todas las interacciones clave.

## About the Design Files
Los archivos `.dc.html` de este bundle son **referencias de diseño hechas en HTML** — prototipos que muestran el aspecto y comportamiento buscados, **no código de producción para copiar tal cual**. Están escritos en un formato propietario ("Design Component"): un `<x-dc>` con plantilla declarativa + una clase `Component extends DCLogic` (similar a un componente React de clase, con `state`/`setState`/`renderVals()`). Dependen de un runtime (`support.js`) que **no debes portar**.

**La tarea es recrear estos diseños en el entorno del codebase real** (React/Next, Vue, SwiftUI, etc.) usando sus patrones y librerías existentes. Si no hay entorno aún, elige el framework más apropiado (recomendado: **React + TypeScript**, ya que la lógica de los DC se traduce casi 1:1 a componentes de función con `useState`). Lee cada `.dc.html`: la plantilla = el JSX, y `renderVals()`/los métodos = la lógica/estado.

## Fidelity
**Alta fidelidad (hifi).** Colores, tipografía, espaciado, radios e interacciones son finales. Recrear pixel-perfect con la librería de componentes del codebase. Todos los valores exactos están en *Design Tokens*.

---

## Design System (fuente de verdad)

### Fuentes
- **Display (titulares + cifras grandes): "Clash Display"** — Fontshare: `https://api.fontshare.com/v2/css?f[]=clash-display@400,500,600,700`. Siempre con fallback `,sans-serif`.
- **UI / cuerpo: "Schibsted Grotesk"** — Google Fonts (400/500/600/700). Cifras tabulares con `font-variant-numeric: tabular-nums`.
- **Acento (momentos de valor/celebración): "Syne"** (600/700/800) — Google Fonts. Uso muy puntual.
- **Mono: "Geist Mono"** (400/500/600) — Google Fonts. **SOLO para telemetría/detección** (ver regla abajo).

### Reglas de oro (no negociables)
1. **Geist Mono SOLO en telemetría/detección**: captions tipo `› analizando tu nicho…`, `Radar · escaneando 3 competidores`, `transcripción detectada`, breadcrumbs `/ ruta`, contadores en vivo, duraciones de reel, créditos. **NUNCA** como label por defecto.
2. **Labels en frase normal (sentence case) en Schibsted**, no MAYÚSCULA-MONO. Ej.: "Views", "Likes", "Explosión", "Rivales activos".
3. **Titulares y cifras grandes en Clash Display.** Syne solo en el "momento aha".
4. **Azul = marca + acción + navegación activa + anillo del Cerebro.** El logo es azul.
5. **Naranja = chispazo de explosión/energía**, uso mínimo (icono de llama/rayo).
6. **Verde = "explota" / multiplicadores** (color + número, canal redundante accesible): `12×`, `9.4×`.
7. **Botones 3D en TODAS las variantes**: `border-bottom: 3–4px` del tono oscuro; al pulsar `transform: translateY(2px)` + `border-bottom-width: 2px` (1–2px en botones pequeños).
8. **Cero emojis.** Iconos de trazo 2px (estilo Lucide) + miniaturas reales de reel 9:16 (placeholder por ahora: gradiente con play + badge multiplicador verde + duración mono).
9. **Voz joven, segunda persona, humor seco.** La gracia en titulares/estados; datos y botones, literales.
10. **Accesibilidad**: contraste AA en ambos temas, foco visible, targets ≥44px, una sola acción primaria por pantalla.

> Nota: en **Métricas** el cliente pidió mantener la paleta **azul** en todos los gráficos; ahí el verde se reserva solo para deltas positivos y para el `×` de "explota".

### Design Tokens (CSS custom properties)

**Tema OSCURO (por defecto)**
```css
--bg:#090C15; --surface:#111726; --surface-2:#172032; --surface-3:#202C46;
--border:rgba(255,255,255,.07); --border-strong:rgba(255,255,255,.13);
--text:#F3F6FC; --text-2:#A6B1C6; --text-3:#69748C;
--blue:#2F5BFF; --blue-press:#1B3FD6; --blue-soft:rgba(47,91,255,.16); --blue-400:#6E92FF; --blue-700:#152FA6;
--orange:#FF8A3D; --orange-soft:rgba(255,138,61,.14);
--green:#2FD18A; --green-soft:rgba(47,209,138,.16);
--danger:#FF5C72;
```

**Tema CLARO (papel cálido) — `[data-theme="light"]`**
```css
--bg:#FAF7F1; --surface:#FFFFFF; --surface-2:#F4EFE6; --surface-3:#E7DFD2;
--border:rgba(24,22,14,.10); --border-strong:rgba(24,22,14,.18);
--text:#171511; --text-2:#574F42; --text-3:#8E8472;
--blue:#2348E6; --blue-press:#1A37B8; --blue-soft:rgba(35,72,230,.10); --blue-400:#4A74FF; --blue-700:#142B8F;
--orange:#BE5510;       /* tono -600 para AA sobre claro */
--green:#0E8A5A;        /* tono -600 para AA sobre claro */
--danger:#C8324A;
```
El tema se aplica con `data-theme="light"` en el contenedor raíz (por defecto = oscuro).

### Escalas
- **Radios**: 8 / 11–12 / 14–16 / 18–20 / 22–24 px · pill `9999px`. (Cards 16–20, botones 11–14, chips pill.)
- **Espaciado**: base 4px → 6/8/10/12/14/16/18/20/22/24/26/34/40.
- **Sombra de frame/card elevada**: `0 30px 70px -24px rgba(8,12,20,.5)` (oscuro). Popover/modal: `0 24px 60px rgba(0,0,0,.4)`.
- **Easing estándar**: `cubic-bezier(.16,1,.3,1)`, duraciones 180–350ms. Sin overshoot.

### Componentes compartidos
- **Shell**: rail lateral de iconos de 66px (logo azul arriba; Radar/Guiones/Métricas/Comunidad/Voz; ajustes/avatar abajo). Item activo = `background: var(--blue-soft)`, icono azul y barra lateral de 3px azul a la izquierda. Topbar 60px: chip de cuenta + breadcrumb mono a la izquierda; "Apunta una idea" (fantasma) + "Pro · N cr" (pill) a la derecha.
- **Botón primario**: `background:var(--blue); color:#fff; border:none; border-bottom:4px solid var(--blue-press); border-radius:14px; padding:14px 24px; font-weight:600`. `:active` → `translateY(2px)` + `border-bottom-width:2px`.
- **Botón fantasma/secundario**: `background:var(--surface-2); border:1px solid var(--border-strong); border-bottom:3px solid var(--border-strong)`.
- **Card**: `background:var(--surface); border:1px solid var(--border); border-radius:16–20px`. Hover: `translateY(-2px)` + sombra.
- **Chip/tag (filtros)**: pill, `surface`/`surface-2`; estado `on` = `var(--blue-soft)` + `border:1px solid var(--blue)` + texto azul.
- **Badge de estado**: pill con `*-soft` de fondo y color del tono (Por grabar=azul, Grabado=verde con ✓, Cocinando=naranja).
- **Tab/sub-nav**: subrayado de 2px azul en el activo (`border-bottom`), inactivo en `--text-3`.
- **Miniatura de reel 9:16**: gradiente `surface-3 → surface-2`, play translúcido centrado, badge multiplicador verde arriba-izq (con llama naranja), duración mono abajo-dcha.

---

## Screens / Views

> Cada pantalla vive en su `.dc.html`. La plantilla (markup) es el equivalente al JSX; los métodos de la clase + `renderVals()` son el estado y los handlers. Todo es inline-styled con las variables de arriba.

### 1. RADAR — `RADAR exploración.dc.html`
- **Propósito**: detectar señales (reels que explotan) y robarlas. *Este archivo es un board de exploración con 3 layouts (A/B/C) + variantes de tarjeta. El layout aprobado es el **A** (shell completo con rail).*
- **Layout A**: rail 66px + columna principal. Hero con copy "Señales de hoy" + 3 cifras vivas (cuentan hacia arriba al cargar; "alcance robable · en directo" sube en tiempo real, punto verde) + **radar animado** (barrido cónico, blips de colores, caption rotando: "Buscando viralidad…", "Manifestando viralidad…", "Escaneando…"). Oportunidad #1 destacada (única acción primaria "Hazlo mío"). **Filtro de competidores** (chips) que filtra la lista y una **galería** de reels grandes (máx 4 + carrusel con flechas y contador "1–4 de N"); cada póster expande un panel con métricas + transcripción. Tarjeta "Tu cerebro · Nivel 2" (progreso IA). Banner "Llena mi semana" (lote, 1 toque). Lista "Más señales" con tabs **Explotando/Recientes/Favoritos** + favoritos (estrella).
- **Interacciones**: tabs filtran; estrella togglea favorito; chips de competidor filtran lista+galería; carrusel pagina de 4 en 4; póster expande/colapsa; cifras del hero animan y el contador "en directo" incrementa con `setInterval`.

### 2. Guiones — `Guiones.dc.html`
- **Propósito**: gestionar guiones robados.
- **Layout**: shell + H1 "Tus guiones" + tabs con conteo **Todos / Por grabar / Grabados**. Sección "Sin desarrollar" (ideas en bruto → "Desarrollar"). Grid 2 col de tarjetas de guion con badge de estado; una en estado **"Cocinando el guion…"** (skeleton shimmer + puntos parpadeando). Cada tarjeta: hook (título Clash), `› gancho detectado` (mono), acciones "Abrir guion"/"Marcar grabado". Banner "Explosión creativa" (3 ideas × 3 ganchos). Empty state con voz: "Aún nada aquí. Roba tu primera señal." (titular en Syne).
- **Interacciones**: tabs filtran por estado; favoritos/estado por tarjeta.

### 3. Editor de guion — `Editor de guion.dc.html`
- **Propósito**: editar el guion robado con tu voz.
- **Layout**: shell + dos columnas. Documento (izq): badge estado + "Robado de @handle" + `12×` verde; **título editable** (`contenteditable`); barra de acciones IA (Acortar / Más gancho / Cambiar tono / Más ejemplos); bloques editables **Gancho / Desarrollo / CTA** (cada uno con "Reescribir"); footer "~38 s · 96 palabras" + "Modo teleprónter". Rail (dcha, 340px): reel fuente (póster + views/likes + "Ver original"), `› transcripción detectada` (mono, scroll), **variantes de gancho** (3, seleccionables, "tu voz"), botones "Regenerar guion" y "Roba como un artista".
- **Interacciones**: seleccionar variante cambia el título en vivo; "Regenerar"/acciones IA → estado **"Cocinando el guion…"** (~1.7s, shimmer en bloques, indicador del topbar en naranja); "Marcar grabado" ↔ "Grabado" (verde + check); save indicator (guardado/cocinando).
- **Estado**: `hookIdx`, `cooking`, `recorded`.

### 4. Teleprónter — `Teleprompter.dc.html`
- **Propósito**: leer el guion mientras grabas.
- **Layout**: topbar (volver al Editor, título, pill REC/pausa, timer mono `mm:ss`). **Stage near-black `#05070D`** (siempre oscuro para grabar con poca luz) con texto grande auto-scroll (Clash, secciones Gancho/Desarrollo/CTA), guía de lectura central (banda azul translúcida + flecha) y degradados arriba/abajo. Barra de controles: Play/Pausa, Reiniciar, **Velocidad** (×1–6), **Texto** (A−/A+, 24–56px), **Espejo** (scaleX(-1)), y botón grande **Grabar/Detener**.
- **Interacciones**: "Grabar" → countdown 3·2·1 (overlay) → REC (pulso rojo) + auto-scroll + timer corre; Play/Pausa controla el scroll (`setInterval` moviendo `translateY`); velocidad/tamaño/espejo en vivo.
- **Estado**: `playing, recording, speed, font, mirror, offset, ms, countdown`.

### 5. Métricas — `Métricas.dc.html`
- **Propósito**: analítica estilo Metricool, **paleta azul**.
- **Layout**: shell + selector de rango + toggle **Free/Pro** + sub-nav **Resumen / Audiencia / Competidores**.
  - *Resumen*: 8 KPIs con sparkline (SVG polyline azul) + delta; tendencia 14 barras; desglose de interacción; ganchos que funcionan; temas que explotan (escala log, `×` verde); duración óptima; **heatmap de mejores horas** (7 días × 6 franjas, intensidad `rgba(blue, α)`, pico marcado ★); top reels.
  - *Audiencia*: crecimiento de seguidores, alcance seguidores/no-seguidores, edad, género, top ubicaciones.
  - *Competidores*: tabla comparativa (tú resaltado en `blue-soft`) con seguidores/repros/interacción/ratio explota; cuota de atención del nicho; "hueco detectado" accionable.
- **Muro (Free)**: cuando `plan==='free'`, el área de analítica se aplica `filter: blur(9px)` + `pointer-events:none` y una tarjeta-paywall centrada: "Tus números, a un clic." / "Borrosos a propósito." + "Desbloquéalos con Pro" + `› analizando tu nicho… sin que se enteren`. En Pro se revela.
- **Estado**: `plan` (free/pro), `view` (Resumen/Audiencia/Competidores).

### 6. Onboarding — `Onboarding.dc.html`
- **Propósito**: alta guiada en 7 pasos.
- **Layout**: card de flujo 760px, barra de progreso (`width = step/7`), "Paso N de 7" (mono), "Saltar". Pasos: (1) handle de Instagram (input, "cuenta encontrada"), (2) nicho (grid selección única), (3) subnicho (tags multi), (4) valor/aha "Inspirándome del éxito" (4 miniaturas + `analizando tu nicho… sin que se enteren`), (5) 2 competidores pre-rellenados (toggle añadir/quitar), (6) objetivo (Crecer/Vender/Educar/Entretener, selección única), (7) cierre con **anillo del Cerebro al 50%** (SVG stroke-dasharray) + "Manifestando viralidad" (Syne) + "Abrir mi primer guion". Nav Atrás/Continuar.
- **Estado**: `step` + selecciones (`nicho, subnichos, comp, objetivo`).

### 7. Ajustes — `Ajustes.dc.html`
- **Propósito**: perfil, plan y afiliados.
- **Layout**: shell + H1 "Ajustes" + sub-nav **Perfil y voz / Plan y créditos / Afiliados**.
  - *Perfil y voz*: tarjeta identidad (avatar, Cerebro Nivel 2), "Tu voz" (derivada de 47 reels, tonos editables como chips, muletillas, slider de humor), preferencias con **toggles** (switch pill).
  - *Plan y créditos*: tarjeta Pro 19€/mes, créditos 10/30 (barra), uso del mes, historial de facturas.
  - *Afiliados*: 3 KPIs (comisión/referidos/pendiente), enlace con "Copiar", lista de referidos con comisión verde.
- **Estado**: `tab`, `tonos`, `prefs`.

---

## Interactions & Behavior (resumen transversal)
- **Tabs/sub-nav**: filtran/cambian la vista vía estado; activo con subrayado azul o pill `surface-2`.
- **Toggles/selección**: chips y cards conmutan `blue-soft + border azul`.
- **Estados async (no bloqueantes)**: "Cocinando el guion…" (shimmer + puntos `@keyframes` blink), "analizando tu nicho…", contador "en directo". Implementar con el equivalente del codebase (timers/efectos), respetando la voz.
- **Botones 3D**: feedback de pulsación con `translateY` (ver tokens).
- **Animaciones**: radar (barrido cónico `@keyframes` + blips), shimmer skeleton, ring de progreso. Easing `cubic-bezier(.16,1,.3,1)`. Evitar overshoot.
- **Temas**: alternar `data-theme` en raíz; todos los colores vía variables.
- **Responsive**: los prototipos son a ancho fijo de escritorio (frames ~1080–1240px). Adaptar a la rejilla responsive del codebase manteniendo jerarquía.

## State Management
Cada pantalla mantiene su estado local (en los DC es `this.state`; en React serían `useState`). Variables clave por pantalla listadas arriba. Datos hoy son mock dentro de cada archivo (métodos `*Data()` / arrays en la clase) — sustituir por las fuentes reales (API de IG/scrape, generación de guion con IA, créditos, etc.).

## Assets
- **Sin imágenes externas**: las miniaturas de reel son placeholders CSS (gradiente + play + badge). Sustituir por frames reales 9:16.
- **Iconos**: SVG inline de trazo 2px (equivalentes a Lucide). Se pueden reemplazar por la librería de iconos del codebase (Lucide recomendado).
- **Logo**: marca "R" sobre gradiente naranja→azul (placeholder). Pedir SVG oficial.
- **Fuentes**: Clash Display (Fontshare), Schibsted Grotesk + Syne + Geist Mono (Google Fonts).

## Files
- `RADAR exploración.dc.html` — Radar (3 layouts + variantes; usar Layout A).
- `Guiones.dc.html` — lista de guiones.
- `Editor de guion.dc.html` — editor de guion.
- `Teleprompter.dc.html` — teleprónter de grabación.
- `Métricas.dc.html` — analítica (Resumen/Audiencia/Competidores).
- `Onboarding.dc.html` — alta 7 pasos.
- `Ajustes.dc.html` — perfil/plan/afiliados.
- `support.js` — runtime del formato DC (**no portar**; solo para abrir los prototipos en el navegador).

### Cómo abrir los prototipos
Abre cualquier `.dc.html` en un navegador (necesitan `support.js` en la misma carpeta, ya incluido). Sirve la carpeta con un server estático si tu navegador bloquea `file://`.
