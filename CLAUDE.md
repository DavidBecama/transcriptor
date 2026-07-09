# ReelScript — Contexto operativo para Claude Code

SaaS que transcribe reels de Instagram/TikTok y permite adaptar el texto con LLM. Sistema de créditos y planes de pago con Stripe. Dominio: `reelscript.net`.

## Stack
- Python + Flask (`app.py`) — ~2300 líneas
- Celery + Redis para tareas async (`tasks.py`)
- SQLite local (`schema.sql`, `transcriptions.db`) — solo para caché de transcripciones
- Supabase — auth + DB principal (usuarios, créditos, scripts, ideas, proyectos)
- Docker + docker-compose para local y deploy
- Templates Jinja en `templates/` (un solo archivo: `index.html`, ~8700 líneas)
- Estáticos en `static/`

## Servicios externos
- **Groq** — transcripción de audio (Whisper)
- **OpenRouter + Gemini 2.5 Pro** — transformación de texto ("Hazlo tuyo")
- **Supabase** — auth (email + Google OAuth) y DB de usuarios/créditos/contenido
- **Stripe** — pagos, suscripciones y webhooks
- **Apify** — descarga de reels de Instagram (primario)
- **yt-dlp** — fallback para TikTok y otros
- **Traefik** — reverse proxy en el VPS, gestiona HTTPS automático

## Variables de entorno
Requeridas: `GROQ_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `FLASK_SECRET_KEY`.
Opcionales: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `APIFY_TOKEN`, `STRIPE_TOPUP_PRICE`, `REDIS_URL`.

## Ejecución local
`docker compose up` — levanta app + worker + Redis. La app escucha en `:5555`.

**Demo sin claves** (la isla Signal con datos falsos, sin Supabase/Stripe/Redis):
`DEMO_MODE=1 PORT=3100 SUPABASE_URL=https://demo-dummy.supabase.co SUPABASE_SERVICE_KEY=dummy venv/bin/python app.py`
→ http://localhost:3100 entra directo al Radar. Deep-links (`?plan=creador|agencia`, `?t=`, `?b=`) **solo** entrando por `/profile/radar?…` (entrar por `/es/` los pierde en el replaceState del shim).

## Harness funcional de la isla
`node scripts/verify-island.mjs` (cero deps; Chrome headless + CDP) contra el server demo de arriba. 48 checks: montaje, deep-links, criterios IDI T1-T9 (acción primaria única, sheets sin prompt, Esc/Enter, aria/foco, targets móviles, anti-duplicado de robo) + flujo «Ideas robadas» (robo→workspace, agrupación por reel, elección pendiente tras robo en background) + marca muda/refresh honesto/sugerencias agotadas/aislamiento por marca. Correr tras cualquier cambio en `static/js/radar-loop.js`. Trampas que ya resuelve solo: mata su Chrome al salir (los huérfanos a 60% CPU degradan los runs siguientes), desactiva animaciones CSS (el orbe satura el renderer headless) y auto-descarta diálogos JS nativos. TRAMPA del entorno: el rate-limit (200/h por IP en `profile_page`) usa el Redis local si está vivo (persiste entre reinicios) → tras varias corridas todo empieza a fallar con 429 y parece bug del código. Arranca el demo con `REDIS_URL=redis://127.0.0.1:6399` (puerto muerto) para forzar `memory://` y aislar el QA.

---

## Deploy flow
- **Producción**: VPS en `82.165.247.204`, directorio `/app/reelscript`
- **Servidor**: Docker Compose con servicios: app (Flask+Gunicorn), worker (Celery), redis, traefik
- **GitHub Action**: `.github/workflows/deploy.yml` — se dispara al hacer push a la rama `prod`. SSH al VPS → `git fetch && git reset --hard origin/prod && docker compose up -d --build`
- **Tags de versión**: solo cosméticos (para referencia humana). **NO disparan deploy**. El deploy lo dispara únicamente el push a `prod`.
- **Verificación post-deploy**: `curl -I https://reelscript.net/es/` debe devolver `200`.

## Trampas conocidas
- **NUNCA** hacer `git checkout <tag>` en el servidor. Deja HEAD en modo detached y los siguientes deploys no avanzan (el `git reset --hard origin/prod` no hace nada porque HEAD no apunta a ninguna rama).
- Los números de tag **no siguen orden cronológico** por retagueos previos: `v0.9.0` tiene contenido más antiguo que `v0.9.1`. Confiar siempre en el SHA del commit, no en el número de versión.
- Si `/es/` o `/en/` devuelven 404 en producción, el servidor está en un commit anterior a `a3f3bb2` (SEO fase 1). Fix: ssh al servidor → `git fetch && git reset --hard origin/prod && docker compose up -d --build`.
- `templates/index.html` tiene ~8700 líneas. Editarlo con precisión quirúrgica. No rehacer bloques enteros sin leer primero qué hay alrededor.
- Las secciones del perfil (métricas, ideas, teleprompter, proyectos) se muestran/ocultan por JS según `plan` del usuario (`hasPaidPlan = ["pro","creator","agency"]`, ver index.html). Empiezan con `display:none` en el HTML. No eliminar esa lógica.

---

## Arquitectura de rutas (app.py)

| Ruta | Descripción |
|------|-------------|
| `/` | Redirect 302 a `/es/` o `/en/` según `Accept-Language` |
| `/es/`, `/en/` | Landing bilingüe — renderiza `index.html` con `lang="es"/"en"` |
| `/<lang>/<slug>` | Pillar pages SEO — datos en dict `PILLAR_PAGES` en `app.py` |
| `/sitemap.xml` | Sitemap con hreflang para home + 10 pillar pages |
| `/robots.txt` | Robots |
| `/auth/google` | Inicia flujo Google OAuth vía Supabase |
| `/auth/callback` | Callback OAuth, setea cookie de sesión |
| `/auth/me` | Devuelve estado del usuario (plan, créditos, perfil) |
| `/transcribe` | Encola tarea Celery, devuelve `task_id` |
| `/result/<task_id>` | Polling de resultado de transcripción |
| `/ideas` | CRUD ideas con desarrollo IA (POST/GET/DELETE) |
| `/transform` | Transformación de texto con LLM |
| `/stripe/webhook` | Webhook de Stripe para actualizar plan/créditos |

## Supabase — tablas principales
`profiles` (plan, credits_cents, stripe_subscription_id, avatar_seed, monthly_usage)
`transcriptions`, `scripts` (los guiones; `saved_scripts` NO existe, era el nombre legacy), `ideas` (con `inspired_by_id`/`notes` — ancla de «Ideas robadas»), `projects`, `assistants`, `agency_members`

## SEO implementado (desde a3f3bb2 + 5cae2f0)
- Rutas bilingüe `/es/` y `/en/` con hreflang y canonical
- 10 pillar pages: 5 ES (`transcribir-reel-instagram`, `transcribir-tiktok`, `reel-a-linkedin`, `hooks-desde-reel`, `transcribir-audio-video`) + 5 EN (equivalentes)
- Sitemap XML con xhtml:link hreflang para home + 10 pillars
- OG images (`static/og-image-es.png`, `static/og-image-en.png`), favicon multi-tamaño
- JSON-LD: WebApplication + Organization + FAQPage en pillar pages
- La variable `{{ lang }}` llega al template vía Flask; JS lee el lang desde la URL (`/es/` o `/en/`)

---

## Convenciones
- Ramas: `dev` (trabajo) → `prod` (estable, default). Tags semver (`v0.x.y`) al mergear a prod.
- Commits estilo conventional: `feat:`, `fix:`, `chore:`, `refactor:`.
- No tocar `transcriptions.db` ni `venv/` ni `__pycache__/`.
- Cambios en `schema.sql` → avisar antes, implican migración manual en Supabase.
- Cambios en `requirements.txt` → avisar antes.

## Operación
- Respuestas directas, sin preámbulos.
- Resultado primero, explicación solo si pregunto.
- No resumas lo que acabas de hacer.
- Antes de tareas con >3 pasos o que toquen varios archivos: plan corto primero, ejecución después.
- Errores: muestra el error real, no lo parafrasees.
- No instales dependencias sin preguntar.
