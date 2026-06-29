"""Transcriptor — Flask app con Supabase, créditos y Apify."""

# Monkey-patch SOLO si ssl aún NO está importado. Si ya lo está (el CELERY WORKER importa
# requests/yt_dlp en tasks.py ANTES de importar app), parchear ssl AHORA es TARDE → gevent
# envuelve un ssl ya cargado → RecursionError en TODO HTTPS del worker (descarga + LLM).
# El web ya lo parchea bien vía gunicorn `--worker-class gevent` (antes de importar app);
# en runs locales `python app.py` ssl aún no está → se parchea normal.
import sys as _sys
if "ssl" not in _sys.modules:
    from gevent import monkey as _gmonkey; _gmonkey.patch_all()

import json
import logging
import os
import re
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from urllib.parse import urlparse, urlencode

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask, Response, abort, g, jsonify, make_response, redirect, render_template, request, session

load_dotenv()

# ── Validate required env vars ───────────────────────────────────────────────

# Las claves se validan SOLO para el proveedor de pago activo: si PAYMENT_PROVIDER=whop,
# que falten las de Stripe (proveedor inactivo) NO debe tumbar el arranque, y viceversa.
# PAYMENT_PROVIDER se resuelve aquí leyendo el entorno directo (la constante se define más abajo).
_active_provider = (os.environ.get("PAYMENT_PROVIDER", "stripe") or "stripe").strip().lower()
if _active_provider not in ("stripe", "paddle", "whop"):
    _active_provider = "stripe"

# Clave primaria del proveedor activo (sin ella no puede cobrar) → obligatoria. El secreto
# del webhook de Paddle/Whop se rellena tras crear el destination en su panel, así que no es
# fatal al boot (el handler tolera su ausencia y responde 200); solo se avisa.
_PROVIDER_REQUIRED = {
    "stripe": ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"],
    "paddle": ["PADDLE_API_KEY"],
    "whop":   ["WHOP_API_KEY"],
}
REQUIRED_ENV_VARS = [
    "GROQ_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "FLASK_SECRET_KEY",
] + _PROVIDER_REQUIRED[_active_provider]
missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if missing:
    print(f"[FATAL] Missing required environment variables ({_active_provider}): {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

_wh_secret = {"paddle": "PADDLE_WEBHOOK_SECRET", "whop": "WHOP_WEBHOOK_SECRET"}.get(_active_provider)
if _wh_secret and not os.environ.get(_wh_secret):
    print(f"[WARN] {_wh_secret} sin configurar: el webhook de {_active_provider} no verificará firmas hasta definirlo.", file=sys.stderr)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

from supabase import create_client, Client  # noqa: E402 (after dotenv)

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.permanent_session_lifetime = timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

# ── Config ───────────────────────────────────────────────────────────────────

GROQ_API_KEY          = os.environ.get("GROQ_API_KEY", "")
GROQ_URL              = "https://api.groq.com/openai/v1/audio/transcriptions"

# OpenRouter — para transformaciones de texto ("Hazlo tuyo")
OPENROUTER_API_KEY    = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL        = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL      = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
# Modelo SOLO para la GENERACIÓN del guion (calidad > coste: ~€0,04/guion; el gasto gordo
# es el scraping). La clasificación de formato y el resto siguen en OPENROUTER_MODEL (flash).
GENERATION_MODEL      = os.environ.get("GENERATION_MODEL", "google/gemini-2.5-pro")
SUPABASE_URL          = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
APIFY_TOKEN           = os.environ.get("APIFY_TOKEN", "")
STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
CLARITY_PROJECT_ID    = os.environ.get("CLARITY_PROJECT_ID", "")
POSTHOG_API_KEY       = os.environ.get("POSTHOG_API_KEY", "")


def track_event(event, distinct_id, properties=None):
    """Bloque growth-1 — wrapper server-side de PostHog (vía emails.track).
    Centraliza la captura de eventos de funnel desde el backend (registro,
    primer guion, muros, upgrade, cancelación) que el cliente NO puede ver.
    Best-effort total: jamás lanza, jamás bloquea el request."""
    try:
        from emails import track as _t
        _t(event, distinct_id, properties or {})
    except Exception:
        pass
    # econ: el primer muro arranca el flash del Pack 300 (48h). Un solo chokepoint.
    if event == "paywall_shown" and distinct_id:
        try:
            _mark_first_wall(distinct_id)
        except Exception:
            pass

FREE_DAILY_ANON  = 5   # transcripciones gratis para anónimos
FREE_DAILY_USER  = 5   # transcripciones gratis para registrados
FREE_DAILY_ADAPT = 0   # v0.19: "Hazlo tuyo" (adapt) es de compromiso → cuesta créditos/plan
                       # (política A3: transcribir = gancho gratis; adapt/Hazlo mío = cuesta)
COST_CENTS       = 18   # $0.18 por uso de pago (~7 usos por $1.29)

# ── Economía de créditos (economia-creditos.md, 2026-06-20) ───────────────────
# Moneda única = crédito (1 crédito = COST_CENTS de saldo). 1 guión = 3 créditos:
# el ×3 da granularidad (regenerar y hooks cuestan menos que un guión entero).
# Los sinks de "mirar" (Radar, métricas, refresh diario) son GRATIS por diseño.
SCRIPT_UNITS          = 3    # generar guión (Roba la idea / desde idea / desde competidor)
REGEN_UNITS           = 1    # regenerar guión (re-tira del mismo reel)
HOOKS_EXTRA_UNITS     = 1    # "3 hooks más" tras agotar el cupo diario gratis
HOOKS_FREE_PER_DAY    = 2    # hooks-extra gratis por usuario y día
FILL_WEEK_UNITS       = 12   # "Llena mi semana" (5 guiones de golpe; vs 15 sueltos)
ADD_COMPETITOR_UNITS  = 2    # añadir competidor NUEVO más allá del cupo del plan
# ── Modelo free 2026-06-23 (decisión Leo): moneda única, SIN trial ────────────
WELCOME_CREDITS_CENTS = 30 * COST_CENTS   # 540 — créditos de bienvenida al registrarse
FREE_MONTHLY_CENTS    = 15 * COST_CENTS   # 270 — recarga mensual del free (+15 cr/mes)
FREE_DAILY_SCRIPTS    = 3                  # tope de robos/guiones por día (free) → empuja a pagar
ANALYSIS_UNITS        = 2                  # analizar un reel = 2 créditos
VOICE_FREE_REELS      = 5    # primeros reels de voz gratis (llegar al aha sin fricción)
VOICE_REEL_UNITS      = 2    # por reel de voz transcrito tras el cupo gratis
AGENCY_EXTRA_BRAND_CREDITS = 96   # +96 cr/mes por marca extra de Agencia (+10€/marca)

# ── Feed diario del radar (v1, 2026-06-27) ────────────────────────────────────
# Re-rank BARATO del pool YA scrapeado (cero scrape nuevo → protege margen). El feed
# rota cada día: score = explosión × frescura × jitter(reel+uid+fecha). La frescura
# sube lo recién explotado y decae los tops de ayer; el jitter sembrado por fecha
# rota la banda media. Cache Redis del ORDEN (ids) por día (TTL ~26h), sin schema.
RADAR_FEED_HALF_LIFE_DAYS = 3        # vida media de la frescura (días)
RADAR_FEED_EXPLODE_MIN    = 2.0      # umbral "está petando" para el descubrimiento del nicho
RADAR_FEED_CACHE_TTL      = 26 * 3600
# Rotación día-a-día: la frescura sola es un desplazamiento UNIFORME (no reordena) y el
# jitter es débil. El motor real de rotación es penalizar los reels SERVIDOS ayer → hoy
# bajan y suben los del siguiente tramo. served_n = top servidos que se penalizan mañana.
RADAR_FEED_SERVED_N    = 24
RADAR_FEED_SEEN_PENALTY = 0.45       # ×score a los reels servidos ayer (los desbanca)
RADAR_FEED_SEEN_TTL     = 3 * 86400
RADAR_SUGG_MAX_AGE_DAYS = 30         # frescura: «Sugerencias de hoy» solo reels ≤30 días
# Refresco manual de PAGO (única vía de scrape on-demand). Parametrizable (doc economía).
REFRESH_NOW_UNITS         = int(os.environ.get("REFRESH_NOW_UNITS", "5"))   # ~5 créditos
REFRESH_NOW_COST          = REFRESH_NOW_UNITS * COST_CENTS
REFRESH_NOW_COOLDOWN_S    = int(os.environ.get("REFRESH_NOW_COOLDOWN_S", str(8 * 3600)))  # 8h
PER_USER_REFRESH_CAP      = int(os.environ.get("PER_USER_REFRESH_CAP", "10"))  # cap Apify/refresh
# «Sugerencias de hoy»: paginado del carrusel. Primeras SUGG_FREE_N gratis (2 tandas de 4);
# a partir de ahí «ver más» CUESTA créditos (monetización, CERO scrape — el pool ya está).
SUGG_FREE_N               = int(os.environ.get("SUGG_FREE_N", "8"))    # reels gratis (2×4)
SUGG_MORE_BATCH           = int(os.environ.get("SUGG_MORE_BATCH", "4"))  # tanda de «ver más»
SUGG_MORE_UNITS           = int(os.environ.get("SUGG_MORE_UNITS", "3"))  # ~3 créditos/tanda
SUGG_MORE_COST            = SUGG_MORE_UNITS * COST_CENTS
SUGG_MAX_TOTAL            = int(os.environ.get("SUGG_MAX_TOTAL", "40"))  # techo absoluto del carrusel

# Flash por-usuario (economia-creditos.md §4): tras chocar el PRIMER muro, el Pack 300
# baja a TOPUP_FLASH_EUR € (vs 49 €) durante TOPUP_FLASH_HOURS h. Urgencia + ancla.
# El price del checkout a 29 € lo crea David en Whop → env WHOP_TOPUP_300_FLASH_*; si no
# está, la oferta se MUESTRA pero el checkout cae al pack normal (degradado seguro).
TOPUP_FLASH_PACK      = "300"
TOPUP_FLASH_EUR       = 29
TOPUP_FLASH_HOURS     = 48

UNLIMITED_EMAILS = {"davidmiragito@gmail.com", "leonard@becamaconsulting.com"}  # sin límite ni coste (cuentas dev)

# v0.16.x DEMO_MODE: arranque local sin login ni Supabase. El frontend instala
# un shim de fetch con datos falsos y entra directo al Radar con un usuario de
# prueba. SOLO se activa con env DEMO_MODE=1 → producción NO se ve afectada.
DEMO_MODE = os.environ.get("DEMO_MODE", "") == "1"

# v0.15.2: emails con acceso a endpoints admin (separado de UNLIMITED_EMAILS,
# que es "cuenta cortesía sin límites"). Comma-separated en env. Si la env no
# está set → set vacío → _is_admin devuelve False para todos (fallback seguro).
ADMIN_EMAILS = {
    e.strip().lower()
    for e in (os.environ.get("ADMIN_EMAILS", "") or "").split(",")
    if e.strip()
}


def _is_admin(user: dict | None) -> bool:
    if not user:
        return False
    # 1) Fallback sin DB: email en ADMIN_EMAILS env (siempre admin, no depende de tabla).
    if user.get("email", "").lower() in ADMIN_EMAILS:
        return True
    # 2) DB-driven: profiles.is_admin. Tolerante a fallo (columna/tabla ausente → False).
    try:
        prof = (db.table("profiles").select("is_admin")
                  .eq("id", user["id"]).single().execute())
        if prof.data:
            return bool(prof.data.get("is_admin"))
    except Exception:
        pass
    return False


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "No autenticado"}), 401
        if not _is_admin(user):
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper


# ── Matriz de planes (fuente de verdad) ──────────────────────────────────────
# v0.19 pricing: 3 planes (Free · Creador · Agencia). "pro" queda solo como
# legacy (grandfathering) — no se ofrece. Modelo de créditos: 1 crédito = COST_CENTS
# de saldo. La asignación mensual se enforce vía monthly_uses (= credits_month);
# los topups suman a credits_cents. Free no tiene mensuales: es una CATA de por vida
# (1 «Hazlo mío» · 3 análisis · 1 competidor — nada resetea).
PLANS = {
    "free": {
        # econ 2026-06-20 (economia-creditos.md): Free 9 cr/mes ≈ 3 guiones (guión=3cr).
        "credits_month": 9,
        # reverse-trial: tras los 7 días de Pro, el FREE es MENSUAL (resetea cada mes),
        # ya no una cata de por vida. Contadores: free_lifetime_uses=guiones del mes,
        # free_analysis_uses=análisis del mes (reset por free_month_reset_at).
        "free_scripts_monthly": 3,     # 3 "Hazlo mío"/mes (= 9 cr a 3 cr/guion)
        "free_analysis_monthly": 3,    # (legacy) — el límite real de análisis es SEMANAL ↓
        "free_analysis_weekly": 3,     # 3 análisis (transcripciones) cada 7 días (ventana móvil, cuenta transcriptions)
        "monthly_uses": 0,             # → PLAN_LIMITS None (sin límite mensual; usa los free_*_monthly)
        "daily_free": 0,
        "scripts_max": 5,
        "projects_max": 1,
        "brands": 1,
        "assistants_max": 0,
        "history_days": None,          # persistente
        "seats": 1,
        "priority": False,
        "support": None,
    },
    "creator": {
        "credits_month": 120,
        "price_month_eur": 29,
        "price_year_eur": 276,
        "monthly_uses": 120,           # econ: 120 cr/mes ≈ 40 guiones (guión=3cr)
        "daily_free": 0,
        "scripts_max": None,           # ilimitado
        "projects_max": None,
        "brands": 1,
        "assistants_max": 10,
        "history_days": None,
        "seats": 1,
        "priority": True,
        "support": "email",
    },
    # Estudio (nuevo): puente 29→129. "creador pro" para quien factura: 3 marcas
    # y más créditos. Precio anual por defecto (~20% off → 47€/mes).
    "estudio": {
        "credits_month": 360,
        "price_month_eur": 59,
        "price_year_eur": 564,         # 47€/mes facturado anual (~20% off)
        "monthly_uses": 360,           # econ: 360 cr/mes ≈ 120 guiones (~120/marca · 3 marcas)
        "daily_free": 0,
        "scripts_max": None,
        "projects_max": 3,             # 3 marcas
        "brands": 3,
        "assistants_max": None,
        "history_days": None,
        "seats": 1,
        "priority": True,
        "support": "email",
        "name": "Estudio",
    },
    "agency": {
        "credits_month": 960,
        "price_month_eur": 129,
        "price_year_eur": 1290,
        "monthly_uses": 960,           # econ: 960 cr/mes ≈ 320 guiones (~96/marca · 10) · +96 cr/marca extra
        "daily_free": 0,
        "scripts_max": None,
        # base 10 marcas; +10€/marca extra. NOTA: la compra per-marca (Stripe)
        # está FLAGUEADA, no implementada → no hard-cap aún (projects_max=None
        # para no romper agencias existentes). Display = "10 + €10/marca".
        "projects_max": None,
        "brands": 10,                  # 10 marcas incluidas (base)
        "assistants_max": None,
        "history_days": None,
        "seats": None,                 # asientos ILIMITADOS (decisión cerrada)
        "addon_brand_eur": 10,         # marca extra €10/mes (billing pendiente David)
        "priority": True,
        "support": "email+chat",
    },
    # ── Legacy (grandfathering): NO se ofrece, pero suscripciones activas lo conservan.
    "pro": {
        "credits_month": 50,
        "monthly_uses": 50,
        "daily_free": 0,
        "scripts_max": None,
        "projects_max": None,
        "brands": 1,
        "assistants_max": 1,
        "history_days": None,
        "seats": 1,
        "priority": False,
        "support": "email",
    },
}

# Add-ons de Agencia (precio/mes, EUR)
# TODO(econ): "brand" debe sumar +150 créditos/mes al pool de la agencia. Falta la
# lógica de concesión (este dict solo define el precio del add-on).
ADDONS = {"brand": 14.99, "seat": 14.99}

# Topups (one-time): nombre → créditos otorgados + precio EUR. El price ID de Stripe
# se inyecta por env (STRIPE_TOPUP_PRICE_<n>); nunca se hardcodea.
TOPUPS = {
    "100":  {"credits": 100,  "eur": 19},
    "300":  {"credits": 300,  "eur": 49},
    "1000": {"credits": 1000, "eur": 139},
}

# Derivados para compatibilidad con código existente
PLAN_LIMITS = {p: v["monthly_uses"] or None for p, v in PLANS.items()}

# ── Reverse-trial ────────────────────────────────────────────────────────────
# Al registrarse: 5 días de Pro CAPADO sin tarjeta (trial_ends_at). Features de
# pago desbloqueadas pero con TOPE DIARIO de TRIAL_DAILY_SCRIPTS guiones/día (Fathom
# 18/06: David quiere 5 días · 3 guiones/día para crear hábito sin quemarlo el día 1).
# El trial deja de ser usable cuando pasan los 5 días → cae a FREE mensual ligero.
# Tope diario = nº de scripts creados hoy (trial_scripts_today). TRIAL_CREDIT_CAP queda
# como backstop total del trial (= 5×3). NO toca los planes de pago.
TRIAL_DAYS = 5
TRIAL_DAILY_SCRIPTS = 3      # tope de guiones/día durante el trial (reset diario, UTC)
TRIAL_CREDIT_CAP = 30        # econ: 30 cr ≈ 10 guiones (guión=3cr) de prueba
TRIAL_PLAN = "creator"       # tier cuyos límites/feature-set ve el trial (Pro completo)
RESCRAPE_COST_CREDITS = 10   # coste de forzar el re-scrape del propio perfil (Fathom 18/06)


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def in_trial(profile: dict) -> bool:
    """Trial RETIRADO (2026-06-23, decisión Leo): todos entran directos en FREE con
    30 créditos de bienvenida + 15/mes + tope de 3 robos/día. Se conserva la función
    (siempre False) para no romper los call-sites (trial_usable, paid_features_active,
    effective_plan, /auth/me…)."""
    return False


def trial_credits_used(profile: dict) -> int:
    return profile.get("monthly_usage", 0) or 0


def trial_credits_left(profile: dict) -> int:
    return max(0, TRIAL_CREDIT_CAP - trial_credits_used(profile))


def trial_usable(profile: dict) -> bool:
    """Trial efectivamente activo: dentro de la ventana Y por debajo del tope.
    En cuanto se agota el tope (o pasan los días) deja de ser usable → muro."""
    return in_trial(profile) and trial_credits_used(profile) < TRIAL_CREDIT_CAP


def trial_days_left(profile: dict) -> int:
    dt = _parse_ts(profile.get("trial_ends_at"))
    if not dt:
        return 0
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return 0
    return int(secs // 86400) + (1 if secs % 86400 else 0)


def trial_scripts_today(user_id) -> int:
    """Guiones creados HOY (UTC) por el usuario — base del tope diario del trial.
    Sin tabla nueva: se cuenta de `scripts` por fecha (Fathom 18/06)."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        r = (db.table("scripts").select("id", count="exact")
               .eq("user_id", user_id)
               .gte("created_at", start.isoformat()).execute())
        return r.count or 0
    except Exception:
        return 0


def trial_daily_left(profile: dict) -> int:
    """Robos/guiones que le quedan HOY al FREE (tope diario = FREE_DAILY_SCRIPTS, modelo
    2026-06-23 sin trial). Los planes de pago NO tienen tope diario → devuelve el tope
    (el front lo ignora para pago vía isFree())."""
    if (profile.get("plan", "free") or "free") != "free":
        return FREE_DAILY_SCRIPTS
    return max(0, FREE_DAILY_SCRIPTS - trial_scripts_today(profile.get("id")))


def effective_plan(profile: dict) -> str:
    """Plan a efectos de LÍMITES: durante el trial usable, TRIAL_PLAN; si no, el real."""
    if profile.get("plan", "free") == "free" and trial_usable(profile):
        return TRIAL_PLAN
    return profile.get("plan", "free")
ASSISTANT_LIMITS = {p: v["assistants_max"] for p, v in PLANS.items()}

# ── Stripe price IDs (v0.19) — TODOS por env, nunca hardcodeados ──────────────
# Suscripción: <plan>_<ciclo>. Topup: por nº de créditos. Legacy topup conservado.
STRIPE_TOPUP_PRICE = os.environ.get("STRIPE_TOPUP_PRICE", "")  # legacy (7 usos)

STRIPE_PRICES = {
    "creator": {
        "month": os.environ.get("STRIPE_PRICE_CREATOR_MONTH", ""),
        "year":  os.environ.get("STRIPE_PRICE_CREATOR_YEAR", ""),
    },
    # Estudio €59 — FLAG: David crea los price IDs en Stripe y los pone en estas
    # env. Sin ellas, el botón de Estudio devuelve "plan no configurado" (no rompe).
    "estudio": {
        "month": os.environ.get("STRIPE_PRICE_ESTUDIO_MONTH", ""),
        "year":  os.environ.get("STRIPE_PRICE_ESTUDIO_YEAR", ""),
    },
    "agency": {
        "month": os.environ.get("STRIPE_PRICE_AGENCY_MONTH", ""),
        "year":  os.environ.get("STRIPE_PRICE_AGENCY_YEAR", ""),
    },
}
# Add-ons de Agencia (suscripción recurrente adicional)
STRIPE_PRICE_ADDON_BRAND = os.environ.get("STRIPE_PRICE_ADDON_BRAND", "")
STRIPE_PRICE_ADDON_SEAT  = os.environ.get("STRIPE_PRICE_ADDON_SEAT", "")
# Topups one-time: price ID → créditos otorgados
STRIPE_TOPUP_PRICES = {
    os.environ.get("STRIPE_TOPUP_PRICE_100", ""):  TOPUPS["100"]["credits"],
    os.environ.get("STRIPE_TOPUP_PRICE_300", ""):  TOPUPS["300"]["credits"],
    os.environ.get("STRIPE_TOPUP_PRICE_1000", ""): TOPUPS["1000"]["credits"],
}
STRIPE_TOPUP_PRICES.pop("", None)  # descarta los no configurados

# ── Paddle (v0.22) — pasarela alternativa detrás del flag PAYMENT_PROVIDER ─────
# Stripe queda DORMIDO (no se borra): si PAYMENT_PROVIDER=paddle, el checkout y el
# webhook activos son los de Paddle. Credenciales por env (.env, nunca en git).
PAYMENT_PROVIDER = (os.environ.get("PAYMENT_PROVIDER", "stripe") or "stripe").strip().lower()
PADDLE_ENV = (os.environ.get("PADDLE_ENV", "sandbox") or "sandbox").strip().lower()
PADDLE_API_KEY = os.environ.get("PADDLE_API_KEY", "")
PADDLE_CLIENT_TOKEN = os.environ.get("PADDLE_CLIENT_TOKEN", "")  # token público (client-side)
PADDLE_WEBHOOK_SECRET = os.environ.get("PADDLE_WEBHOOK_SECRET", "")  # se rellena al crear el destination
PADDLE_API_BASE = "https://sandbox-api.paddle.com" if "sand" in PADDLE_ENV else "https://api.paddle.com"


def payment_provider() -> str:
    """Proveedor de pago activo: 'paddle' | 'whop' | 'stripe' (default)."""
    if PAYMENT_PROVIDER in ("paddle", "whop"):
        return PAYMENT_PROVIDER
    return "stripe"


# Suscripción: PADDLE_PRICE_<PLAN>_<CICLO>_<MONEDA>. Una price por (plan,ciclo,moneda)
# para respetar el toggle manual EUR/USD de la UI (no localización por IP).
def _pp(name):
    return os.environ.get(name, "")


PADDLE_PRICES = {
    plan: {
        cycle: {
            cur: _pp(f"PADDLE_PRICE_{plan.upper()}_{cycle.upper()}_{cur}")
            for cur in ("EUR", "USD")
        }
        for cycle in ("MONTH", "YEAR")
    }
    for plan in ("creator", "estudio", "agency")
}
# Add-on "Marca extra" (recurrente mensual)
PADDLE_PRICE_ADDON_BRAND = {
    "EUR": _pp("PADDLE_PRICE_ADDON_BRAND_EUR"),
    "USD": _pp("PADDLE_PRICE_ADDON_BRAND_USD"),
}
# Topups one-time: price_id → créditos
PADDLE_TOPUP_PRICES = {}
for _n, _cr in (("100", 100), ("300", 300), ("1000", 1000)):
    for _cur in ("EUR", "USD"):
        _pid = _pp(f"PADDLE_TOPUP_{_n}_{_cur}")
        if _pid:
            PADDLE_TOPUP_PRICES[_pid] = _cr

# price_id → (plan, intervalo) para el webhook. Add-on marcado como plan especial.
PADDLE_PRICE_TO_PLAN = {}
for _plan, _cycles in PADDLE_PRICES.items():
    for _cycle, _curs in _cycles.items():
        for _cur, _pid in _curs.items():
            if _pid:
                PADDLE_PRICE_TO_PLAN[_pid] = {"plan": _plan, "interval": _cycle.lower()}
for _cur, _pid in PADDLE_PRICE_ADDON_BRAND.items():
    if _pid:
        PADDLE_PRICE_TO_PLAN[_pid] = {"plan": "brand_addon", "interval": "month"}

# ── Whop (v0.23) — tercera pasarela detrás del flag (stripe|paddle|whop) ───────
# Mismo modelo de planes/créditos que Paddle/Stripe; solo cambia la fuente del
# evento. Catálogo creado en la cuenta aprobada; los plan IDs se leen por la API
# (no se hardcodean) y viven en .env como WHOP_PLAN_*/WHOP_TOPUP_*.
def _clean_secret(name: str) -> str:
    """Lee un secreto de env y limpia errores de pegado típicos: espacios, comillas
    y los <ÁNGULOS> del placeholder. Un secreto envuelto en <…> (p.ej. copiar literal
    «<ws_…>» del ejemplo) rompía la verificación de firma del webhook → 100% de pagos
    rechazados en silencio. Esto lo neutraliza."""
    v = (os.environ.get(name, "") or "").strip()
    if len(v) >= 2 and v[0] in "<\"'" and v[-1] in ">\"'":
        v = v[1:-1].strip()
    return v

WHOP_API_KEY = _clean_secret("WHOP_API_KEY")
WHOP_WEBHOOK_SECRET = _clean_secret("WHOP_WEBHOOK_SECRET")  # se rellena al crear el webhook
WHOP_API_BASE = "https://api.whop.com"
WHOP_CHECKOUT_BASE = "https://whop.com/checkout"  # checkout link por plan_id

WHOP_PLANS = {
    plan: {
        cycle: {cur: _pp(f"WHOP_PLAN_{plan.upper()}_{cycle.upper()}_{cur}") for cur in ("EUR", "USD")}
        for cycle in ("MONTH", "YEAR")
    }
    for plan in ("creator", "estudio", "agency")
}
WHOP_PLAN_ADDON_BRAND = {
    "EUR": _pp("WHOP_PLAN_ADDON_BRAND_EUR"),
    "USD": _pp("WHOP_PLAN_ADDON_BRAND_USD"),
}
WHOP_TOPUP_PLANS = {}      # plan_id → créditos
for _n, _cr in (("100", 100), ("300", 300), ("1000", 1000)):
    for _cur in ("EUR", "USD"):
        _pid = _pp(f"WHOP_TOPUP_{_n}_{_cur}")
        if _pid:
            WHOP_TOPUP_PLANS[_pid] = _cr
# plan_id → (plan, intervalo) para el webhook.
WHOP_PLAN_TO_PLAN = {}
for _plan, _cycles in WHOP_PLANS.items():
    for _cycle, _curs in _cycles.items():
        for _cur, _pid in _curs.items():
            if _pid:
                WHOP_PLAN_TO_PLAN[_pid] = {"plan": _plan, "interval": _cycle.lower()}
for _cur, _pid in WHOP_PLAN_ADDON_BRAND.items():
    if _pid:
        WHOP_PLAN_TO_PLAN[_pid] = {"plan": "brand_addon", "interval": "month"}

# Allowlist de plan_ids válidos para crear un checkout-session (suscripción + addon +
# topups, incl. el flash). El endpoint /whop/checkout-session solo acepta estos.
WHOP_FLASH_TOPUP_IDS = {_pp(f"WHOP_TOPUP_300_FLASH_{_c}") for _c in ("EUR", "USD")} - {""}
WHOP_ALL_PLAN_IDS = set(WHOP_PLAN_TO_PLAN.keys()) | set(WHOP_TOPUP_PLANS.keys()) | WHOP_FLASH_TOPUP_IDS

db: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ── Config DB-driven (planes / topups / settings) con caché TTL + fallback ────
# Patrón crítico: la DB es OPCIONAL. Si la tabla no existe, está vacía, o Supabase
# falla, se degrada al config actual (PLANS / STRIPE_* / TOPUPS / COST_CENTS env).
# El path de billing NO puede romperse por la ausencia de estas tablas.
import time as _cfg_time  # noqa: E402

_config_cache: dict[str, tuple] = {
    "plans":    (None, 0.0),   # (data, timestamp)
    "topups":   (None, 0.0),
    "settings": (None, 0.0),
}
_CONFIG_TTL_S = 300  # 5 min


def _plans_from_env() -> list[dict]:
    """Construye la lista de planes desde PLANS/STRIPE_PRICES env (fallback)."""
    out = []
    for i, (k, v) in enumerate(PLANS.items()):
        pm = v.get("price_month_eur")
        py = v.get("price_year_eur")
        out.append({
            "key": k,
            "name": v.get("name", k.capitalize()),
            "price_month_cents": int(pm * 100) if pm is not None else None,
            "price_year_cents":  int(py * 100) if py is not None else None,
            "monthly_credits": v.get("credits_month", 0) or 0,
            "stripe_price_month": STRIPE_PRICES.get(k, {}).get("month") or None,
            "stripe_price_year":  STRIPE_PRICES.get(k, {}).get("year") or None,
            "active": k != "pro",          # legacy 'pro' no se ofrece
            "sort_order": i,
        })
    return out


def _topups_from_env() -> list[dict]:
    """Construye la lista de topups desde TOPUPS/STRIPE_TOPUP_PRICES env (fallback)."""
    # invertir price_id→credits a credits→price_id para casar con la key TOPUPS
    credits_to_price = {v: pid for pid, v in STRIPE_TOPUP_PRICES.items()}
    out = []
    for i, (k, v) in enumerate(TOPUPS.items()):
        out.append({
            "key": k,
            "credits": v["credits"],
            "price_cents": int(v.get("eur", 0)) * 100,
            "stripe_price_id": credits_to_price.get(v["credits"], "") or "",
            "active": True,
            "sort_order": i,
        })
    return out


def load_plans_config() -> dict:
    """Lee planes de DB con fallback a PLANS env. Caché TTL 5min.
    Devuelve {"plans": [...], "source": "db"|"env"}."""
    now = _cfg_time.time()
    cached_data, cached_ts = _config_cache["plans"]
    if cached_data is not None and (now - cached_ts) < _CONFIG_TTL_S:
        return cached_data

    result = {"plans": [], "source": "env"}
    try:
        db_plans = (db.table("plans").select("*")
                      .eq("active", True).order("sort_order").execute())
        if db_plans.data:
            result["plans"] = db_plans.data
            result["source"] = "db"
        else:
            result["plans"] = _plans_from_env()
            result["source"] = "env"
    except Exception as e:
        logger.warning(f"load_plans_config fallback a env: {e}")
        result["plans"] = _plans_from_env()
        result["source"] = "env"

    _config_cache["plans"] = (result, now)
    return result


def load_topups_config() -> dict:
    """Lee topups de DB con fallback a TOPUPS env. Caché TTL 5min.
    Devuelve {"topups": [...], "source": "db"|"env"}."""
    now = _cfg_time.time()
    cached_data, cached_ts = _config_cache["topups"]
    if cached_data is not None and (now - cached_ts) < _CONFIG_TTL_S:
        return cached_data

    result = {"topups": [], "source": "env"}
    try:
        db_topups = (db.table("topups").select("*")
                       .eq("active", True).order("sort_order").execute())
        if db_topups.data:
            result["topups"] = db_topups.data
            result["source"] = "db"
        else:
            result["topups"] = _topups_from_env()
            result["source"] = "env"
    except Exception as e:
        logger.warning(f"load_topups_config fallback a env: {e}")
        result["topups"] = _topups_from_env()
        result["source"] = "env"

    _config_cache["topups"] = (result, now)
    return result


def get_setting(key: str, default=None):
    """Lee un valor de app_settings.value (JSONB). Fallback a `default` si falla."""
    try:
        result = (db.table("app_settings").select("value")
                    .eq("key", key).single().execute())
        if result.data is not None:
            return result.data.get("value")
    except Exception:
        pass
    return default


def get_cost_cents() -> int:
    """COST_CENTS efectivo: app_settings.cost_cents (DB) con fallback a env COST_CENTS."""
    val = get_setting("cost_cents", None)
    if isinstance(val, dict):
        val = val.get("value")
    try:
        if val is not None:
            return int(val)
    except (TypeError, ValueError):
        pass
    return COST_CENTS


def invalidate_config_cache():
    """Limpia la caché de config (llamar tras cualquier update admin)."""
    global _config_cache
    _config_cache = {
        "plans":    (None, 0.0),
        "topups":   (None, 0.0),
        "settings": (None, 0.0),
    }


try:
    import stripe as stripe_lib
    stripe_lib.api_key = STRIPE_SECRET_KEY
    STRIPE_OK = bool(STRIPE_SECRET_KEY)
except ImportError:
    STRIPE_OK = False

# ── Rate limiting ────────────────────────────────────────────────────────────

from flask_limiter import Limiter  # noqa: E402
from flask_limiter.util import get_remote_address  # noqa: E402

# ── Store compartido (Redis) para rate-limit y locks de crédito ───────────────
# Antes: limiter en memory:// (por-worker → burlable con varios gunicorn workers).
# Ahora: Redis compartido. Si Redis no está disponible, cae a memoria (degradado).
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
import redis as _redis_lib  # noqa: E402  (ya en el stack: broker de Celery)
import uuid as _uuid        # noqa: E402
try:
    rds = _redis_lib.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
    rds.ping()
    _RATE_STORE = REDIS_URL
except Exception as _e:
    rds = None
    _RATE_STORE = "memory://"
    logger.warning("Redis no disponible (%s) → rate-limit/locks en memoria (degradado).", _e)

limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per hour"], storage_uri=_RATE_STORE)


def _user_rate_key():
    """Clave de rate-limit POR USUARIO (no por IP) para endpoints autenticados: varios
    usuarios tras la MISMA IP (oficinas/CGNAT/móvil, o testeo con varias cuentas) no se
    pisan el cupo (antes un 429 por IP bloqueaba a todos). El abuso real lo frena la
    economía (free = 3 guiones/día + créditos), no este límite. Cae a IP si no hay sesión."""
    try:
        u = current_user()
        if u and u.get("id"):
            return "u:" + u["id"]
    except Exception:
        pass
    return get_remote_address()


def acquire_credit_lock(uid: str, ttl: int = 8, wait_s: float = 4.0):
    """Lock distribuido por-usuario para serializar el gasto de créditos entre
    workers (evita doble-gasto en la carrera read-then-write). Devuelve un token
    si lo adquiere, o None. Si Redis no está, devuelve "" (no bloquea: degradado).
    Libera SIEMPRE con release_credit_lock(uid, token) en un finally."""
    if rds is None:
        return ""  # sin Redis: no hay lock (modo degradado)
    key = "creditlock:" + str(uid)
    token = _uuid.uuid4().hex
    deadline = _time.time() + wait_s
    while _time.time() < deadline:
        try:
            if rds.set(key, token, nx=True, ex=ttl):
                return token
        except Exception:
            return ""  # Redis cayó a mitad → degradado, no bloquear al usuario
        _time.sleep(0.04)
    return None  # no se pudo adquirir (otra operación en curso)


def release_credit_lock(uid: str, token) -> None:
    if rds is None or not token:
        return
    key = "creditlock:" + str(uid)
    try:
        # Suelta solo si seguimos siendo dueños del lock (evita soltar el de otro).
        if rds.get(key) == token.encode():
            rds.delete(key)
    except Exception:
        pass


# ── econ · cupo diario de "3 hooks más" (gratis HOOKS_FREE_PER_DAY/día, luego cobra) ──
# Contador diario por usuario en Redis (sin tocar schema). Si Redis no está → cobra
# siempre (degradado seguro). El contador solo avanza tras entregar (mark_used).
def _hooks_extra_units(uid: str) -> int:
    """Créditos a cobrar por un 'hooks más' AHORA: 0 mientras queden gratis hoy,
    si no HOOKS_EXTRA_UNITS."""
    try:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        used = int(rds.get(f"hooks:{uid}:{day}") or 0)
        return 0 if used < HOOKS_FREE_PER_DAY else HOOKS_EXTRA_UNITS
    except Exception:
        return HOOKS_EXTRA_UNITS


def _hooks_mark_used(uid: str) -> None:
    """Suma 1 al contador diario de hooks (TTL 48h cubre husos horarios)."""
    try:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = f"hooks:{uid}:{day}"
        if rds.incr(key) == 1:
            rds.expire(key, 172800)
    except Exception:
        pass


# ── econ · cupo de entrenamiento de voz (VOICE_FREE_REELS gratis de por vida) ──
# Contador acumulado por usuario en Redis (sin tocar schema). Si Redis no está →
# trata todos como gratis (degradado seguro: nunca cobra de más por un fallo de infra).
def _voice_reels_used(uid: str) -> int:
    try:
        return int(rds.get(f"voicereels:{uid}") or 0)
    except Exception:
        return 0


def _voice_mark_reel(uid: str) -> None:
    try:
        rds.incr(f"voicereels:{uid}")
    except Exception:
        pass


# ── econ · flash del 1er muro (Pack 300 → 29 € durante 48 h) ──────────────────
def _mark_first_wall(uid: str) -> None:
    """Marca el instante del PRIMER muro del usuario (solo la 1ª vez). Arranca la
    ventana del flash. Redis con TTL holgado; si no está → no hay flash (degradado)."""
    try:
        key = f"firstwall:{uid}"
        if rds.set(key, datetime.now(timezone.utc).isoformat(), nx=True):
            rds.expire(key, TOPUP_FLASH_HOURS * 3600 + 86400)
    except Exception:
        pass


def topup_flash_state(uid: str) -> dict:
    """Estado del flash para este usuario: {active, pack, eur, regular_eur, expires_at}.
    Activo si chocó su primer muro hace < TOPUP_FLASH_HOURS."""
    off = {"active": False, "pack": TOPUP_FLASH_PACK, "eur": TOPUP_FLASH_EUR,
           "regular_eur": TOPUPS[TOPUP_FLASH_PACK]["eur"]}
    try:
        raw = rds.get(f"firstwall:{uid}")
        if not raw:
            return off
        started = _parse_ts(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        if not started:
            return off
        expires = started + timedelta(hours=TOPUP_FLASH_HOURS)
        if datetime.now(timezone.utc) >= expires:
            return off
        return {**off, "active": True, "expires_at": expires.isoformat()}
    except Exception:
        return off


def paid_features_active(profile: dict, user: dict | None = None) -> bool:
    """Funciones de pago activas. Bloquea 'fantasmas' (plan seteado sin pagar).
    Activo si:
      - trial usable (reverse-trial: 3 días de Pro, mientras quede tope), o
      - plan de pago Y (stripe_subscription_id [el webhook baja a free al cancelar]
        O email en la allowlist de cortesía UNLIMITED_EMAILS)."""
    if trial_usable(profile):
        return True
    plan = profile.get("plan", "free")
    if plan == "free":
        return False
    if profile.get("stripe_subscription_id"):
        return True
    email = ((user or {}).get("email") or "").lower()
    return email in UNLIMITED_EMAILS


def _demo_thumb_urls():
    """SOLO DEMO: URLs de static/img/demo-thumbs/ para sembrar las galerías del Radar.
    Se inyecta en la plantilla (sin pasar por el shim de fetch del demo). En prod
    (sin DEMO_MODE) → lista vacía → placeholders de color."""
    if not DEMO_MODE:
        return []
    import glob as _glob
    base = os.path.join(app.static_folder or "static", "img", "demo-thumbs")
    out = []
    try:
        for f in sorted(_glob.glob(os.path.join(base, "*"))):
            if os.path.splitext(f)[1].lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
                out.append("/static/img/demo-thumbs/" + os.path.basename(f))
    except Exception:
        logger.warning("demo_thumb_urls: listing failed")
    return out


@app.context_processor
def inject_analytics():
    return dict(
        clarity_project_id=CLARITY_PROJECT_ID,
        posthog_api_key=POSTHOG_API_KEY,
        demo=DEMO_MODE,
        demo_thumbs=_demo_thumb_urls(),
    )


@app.errorhandler(429)
def ratelimit_handler(e):
    if request.path == "/transcribe-preview":
        msg = ("Has usado tus 3 transcripciones de prueba. "
               "Crea cuenta gratis para seguir transcribiendo.")
        return jsonify({"error": msg, "preview_limit_reached": True}), 429
    return jsonify({"error": "Too many requests. Please slow down.", "retry_after": str(e.description)}), 429


# ── Security headers ────────────────────────────────────────────────────────

@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Server"] = "ReelScript"
    return response


# ── Input validators ────────────────────────────────────────────────────────

def validate_url(url):
    if not url:
        return "URL is required"
    if len(url) > 500:
        return "URL too long"
    if not url.startswith(("https://www.instagram.com/", "https://instagram.com/",
                           "https://www.tiktok.com/", "https://vm.tiktok.com/",
                           "https://vt.tiktok.com/")):
        return "Only Instagram and TikTok URLs are supported"
    return None


def validate_adapt(data):
    text = (data.get("text") or "").strip()
    if not text:
        return "Text is required"
    if len(text) > 10000:
        return "Text too long (max 10,000 characters)"
    style = (data.get("style") or "").strip()
    valid = {"viral", "divertido", "linkedin", "storytelling", "hooks", "custom", "educacional", "informativo"}
    if style and style not in valid and not data.get("assistant_id"):
        return f"Invalid style"
    if len(data.get("custom_prompt") or "") > 2000:
        return "Custom prompt too long (max 2,000 characters)"
    return None


def validate_email(email):
    return bool(re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email))


# ── Auth helpers ──────────────────────────────────────────────────────────────

def current_user() -> dict | None:
    return session.get("user")


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "No autenticado"}), 401
        return f(*args, **kwargs)
    return wrapper


def safe_next_url(n: str | None) -> str | None:
    """Validate a `next` URL. Returns the URL if safe, None otherwise.

    Rules: must start with `/`, not `//`, parsed netloc/scheme must be empty,
    and it must not target auth/login endpoints (to prevent redirect loops).
    """
    if not n or not isinstance(n, str):
        return None
    if not n.startswith("/") or n.startswith("//"):
        return None
    if "\\" in n:
        return None
    try:
        parsed = urlparse(n)
    except Exception:
        return None
    if parsed.netloc or parsed.scheme:
        return None
    path = parsed.path or ""
    if path in ("/login", "/logout") or path.startswith("/auth/"):
        return None
    return n


def require_auth_html(f):
    """Like require_auth but for HTML routes: redirects to home with ?next=."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        # v0.16.x DEMO_MODE: sirve el shell del workspace sin sesión real para
        # que refrescar /profile/* no expulse de la demo. Solo con DEMO_MODE=1.
        if DEMO_MODE:
            return f(*args, **kwargs)
        if not current_user():
            accept = request.headers.get("Accept-Language", "")
            home = "/en/" if accept.lower().startswith("en") else "/es/"
            full = request.full_path
            if full.endswith("?"):
                full = full[:-1]
            nxt = safe_next_url(full) or "/app"
            return redirect(f"{home}?{urlencode({'next': nxt})}", code=302)
        return f(*args, **kwargs)
    return wrapper


def get_profile(user_id: str) -> dict:
    """Devuelve el perfil del usuario, reseteando los contadores diarios si hace falta."""
    result = db.table("profiles").select("*").eq("id", user_id).execute()
    if not result.data:
        # Cuenta nueva → 30 créditos de bienvenida (modelo free 2026-06-23, sin trial).
        # usage_reset_at = mes que viene → la 1ª recarga de +15 cr cae el mes siguiente.
        _nm = _next_month_boundary(datetime.now(timezone.utc)).isoformat()
        db.table("profiles").insert({"id": user_id, "credits_cents": WELCOME_CREDITS_CENTS,
                                     "usage_reset_at": _nm}).execute()
        return {"id": user_id, "credits_cents": WELCOME_CREDITS_CENTS,
                "free_used_today": 0, "free_adapt_used_today": 0,
                "free_reset_date": str(date.today()),
                "free_adapt_reset_date": str(date.today())}
    profile = result.data[0]
    # Recarga mensual del FREE (+15 créditos): moneda única, reusa usage_reset_at como
    # frontera (los planes de pago la usan para resetear monthly_usage; el free no la
    # usa para nada más). Existentes sin frontera → se inicializa SIN regalar.
    if (profile.get("plan") or "free") == "free":
        _now = datetime.now(timezone.utc)
        _reset = _parse_ts(profile.get("usage_reset_at"))
        if _reset is None:
            _nm = _next_month_boundary(_now).isoformat()
            db.table("profiles").update({"usage_reset_at": _nm}).eq("id", user_id).execute()
            profile["usage_reset_at"] = _nm
        elif _now >= _reset:
            # TOPE 30 cr (Bernat 2026-06-24): los +15/mes NO se acumulan por encima del tope
            # de bienvenida → rellena HACIA 30, nunca por encima (no farmear). Si ya tiene ≥30
            # (p.ej. compró un pack), NO se le toca (el min recortaría créditos comprados).
            _cur = profile.get("credits_cents") or 0
            _newc = min(WELCOME_CREDITS_CENTS, _cur + FREE_MONTHLY_CENTS) if _cur < WELCOME_CREDITS_CENTS else _cur
            _nm = _next_month_boundary(_now).isoformat()
            db.table("profiles").update({"credits_cents": _newc, "usage_reset_at": _nm}).eq("id", user_id).execute()
            profile["credits_cents"] = _newc
            profile["usage_reset_at"] = _nm
    updates = {}
    if profile.get("free_reset_date") != str(date.today()):
        updates["free_used_today"] = 0
        updates["free_reset_date"] = str(date.today())
        profile["free_used_today"] = 0
    if profile.get("free_adapt_reset_date") != str(date.today()):
        updates["free_adapt_used_today"] = 0
        updates["free_adapt_reset_date"] = str(date.today())
        profile["free_adapt_used_today"] = 0
    if updates:
        db.table("profiles").update(updates).eq("id", user_id).execute()
    # Garantizar que los campos existen aunque la columna sea nueva
    profile.setdefault("free_adapt_used_today", 0)
    profile.setdefault("free_adapt_reset_date", str(date.today()))
    return profile


def get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "unknown")


def get_or_reset_ip_usage(ip: str) -> dict:
    today = str(date.today())
    result = db.table("ip_usage").select("*").eq("ip", ip).execute()
    if not result.data:
        db.table("ip_usage").insert({"ip": ip, "used_today": 0, "reset_date": today}).execute()
        return {"ip": ip, "used_today": 0}
    usage = result.data[0]
    if usage["reset_date"] != today:
        db.table("ip_usage").update({"used_today": 0, "reset_date": today}).eq("ip", ip).execute()
        usage["used_today"] = 0
    return usage


def check_monthly_limit(profile: dict) -> tuple[bool, str | None]:
    """Comprueba si el usuario con plan de pago ha superado su límite mensual.
    Resetea el contador si toca. Devuelve (ok, error_msg)."""
    plan = profile.get("plan", "free")
    limit = plan_credit_limit(profile)   # incl. +96 cr/marca extra (Agencia)
    if limit is None:
        return True, None  # plan free usa otro sistema

    # Resetear si toca
    now = datetime.now(timezone.utc)
    reset_at = profile.get("usage_reset_at")
    if reset_at:
        if isinstance(reset_at, str):
            try:
                reset_dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
            except ValueError:
                reset_dt = now
        else:
            reset_dt = reset_at
        if now >= reset_dt:
            next_reset = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=timezone.utc)
            db.table("profiles").update({
                "monthly_usage": 0,
                "usage_reset_at": next_reset.isoformat(),
            }).eq("id", profile["id"]).execute()
            profile["monthly_usage"] = 0

    usage = profile.get("monthly_usage", 0)
    if usage >= limit:
        return False, f"Has alcanzado el límite de {limit} créditos/mes de tu plan. Mejora tu plan o recarga créditos."
    return True, None


# TODO(econ): credits_available usa PLAN_LIMITS, derivado del dict PLANS hardcodeado.
# Lo ideal es que lea de load_plans_config() (tabla `plans` de la DB) para tener una
# única fuente de verdad y no mantener PLANS sincronizado a mano. No implementar ahora.
def plan_credit_limit(profile: dict) -> int | None:
    """Límite mensual de créditos del plan, incluyendo el extra por marcas de Agencia
    (+AGENCY_EXTRA_BRAND_CREDITS por cada extra_brand_slot). None = sin pool mensual."""
    plan = profile.get("plan", "free")
    base = PLAN_LIMITS.get(plan)
    if not base:
        return base
    if plan == "agency":
        extra = int(profile.get("extra_brand_slots") or 0)
        return base + extra * AGENCY_EXTRA_BRAND_CREDITS
    return base


def credits_available(profile: dict) -> int:
    """Créditos disponibles = restante de la asignación mensual del plan + topups.
    1 crédito = COST_CENTS de saldo. Es el número que muestra la pill del radar."""
    limit = plan_credit_limit(profile)
    monthly_rem = max(0, limit - (profile.get("monthly_usage", 0) or 0)) if limit else 0
    topup = (profile.get("credits_cents", 0) or 0) // COST_CENTS
    return monthly_rem + topup


def _plan_mrr_cents(plan: str) -> int:
    """MRR mensual del plan en céntimos. Lo adjuntamos a subscription_upgraded para
    que el panel (becama-panel) pueda sumar el MRR de expansión por upgrade."""
    try:
        return int((PLANS.get(plan, {}).get("price_month_eur") or 0) * 100)
    except Exception:
        return 0


# ── Free MENSUAL (reverse-trial) ─────────────────────────────────────────────
# Tras el trial, el free resetea cada mes. Reusamos free_lifetime_uses (guiones)
# y free_analysis_uses (análisis) como contadores del mes en curso; el boundary
# es free_month_reset_at. _free_month_used cuenta el rollover en lectura (si ya
# pasó el boundary, el mes rodó → 0 usado) sin escribir; el reset+incremento real
# ocurre bajo lock en _free_month_consume.
def _free_month_used(profile: dict, field: str) -> int:
    reset_dt = _parse_ts(profile.get("free_month_reset_at"))
    if reset_dt is None:
        return 0                                   # nunca inicializado → mes nuevo
    if datetime.now(timezone.utc) >= reset_dt:
        return 0                                   # boundary pasado → mes rodó
    return profile.get(field, 0) or 0


def _next_month_boundary(now: datetime) -> datetime:
    return (now.replace(day=1) + timedelta(days=32)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)


def _free_month_consume(user_id: str, fresh: dict, field: str) -> int:
    """Bajo lock: aplica el reset mensual si toca y suma 1 al contador `field`.
    Devuelve el nuevo valor del contador."""
    now = datetime.now(timezone.utc)
    reset_dt = _parse_ts(fresh.get("free_month_reset_at"))
    rolled = reset_dt is None or now >= reset_dt
    if rolled:
        updates = {"free_analysis_uses": 0, "free_lifetime_uses": 0,
                   "free_month_reset_at": _next_month_boundary(now).isoformat()}
        updates[field] = 1
        db.table("profiles").update(updates).eq("id", user_id).execute()
        return 1
    newv = (fresh.get(field, 0) or 0) + 1
    db.table("profiles").update({field: newv}).eq("id", user_id).execute()
    return newv


def free_lifetime_left(profile: dict) -> int:
    """Guiones «Hazlo mío» gratis que le quedan ESTE MES a un free (post-trial)."""
    cap = PLANS["free"]["free_scripts_monthly"]
    return max(0, cap - _free_month_used(profile, "free_lifetime_uses"))


def free_analysis_left(profile: dict) -> int:
    """(Legacy mensual — conservado por compat; el límite real es semanal ↓.)"""
    cap = PLANS["free"]["free_analysis_monthly"]
    return max(0, cap - _free_month_used(profile, "free_analysis_uses"))


def free_analysis_left_week(user_id: str) -> int:
    """Análisis (transcripciones) gratis que le quedan a un free en los ÚLTIMOS 7 DÍAS.
    Ventana MÓVIL contada directamente de la tabla `transcriptions` (no usa contador):
    decoplado de los guiones gratis (que siguen mensuales) y solo cuentan los análisis
    que de verdad se completaron (la task inserta la fila solo al terminar)."""
    cap = PLANS["free"].get("free_analysis_weekly", 3)
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        r = (db.table("transcriptions").select("id", count="exact")
               .eq("user_id", user_id).gte("created_at", since).limit(1).execute())
        used = r.count or 0
    except Exception:
        used = 0
    return max(0, cap - used)


# ── Download / transcription helpers ─────────────────────────────────────────

def detect_platform(url: str) -> str:
    if "instagram.com" in url:
        return "instagram"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "tiktok.com" in url:
        return "tiktok"
    return "otro"


def _ytdlp(url: str, output_dir: str) -> str:
    out = os.path.join(output_dir, "audio")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": out,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return out + ".mp3"


def _apify_instagram(url: str, output_dir: str) -> str:
    """Descarga un reel de Instagram vía Apify y devuelve la ruta del mp3."""
    actor_url = (
        f"https://api.apify.com/v2/acts/apify~instagram-scraper"
        f"/run-sync-get-dataset-items?token={APIFY_TOKEN}&memory=256"
    )
    resp = requests.post(
        actor_url,
        json={"directUrls": [url], "resultsLimit": 1},
        timeout=120,
    )
    resp.raise_for_status()
    items = resp.json()
    if not items:
        raise ValueError("Apify no devolvió resultados para esta URL")

    item = items[0]
    video_url = item.get("videoUrl") or item.get("video_url")
    if not video_url:
        raise ValueError("No se encontró videoUrl en la respuesta de Apify")

    video_path = os.path.join(output_dir, "video.mp4")
    with requests.get(video_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(video_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    mp3_path = os.path.join(output_dir, "audio.mp3")
    ret = os.system(f'ffmpeg -i "{video_path}" -vn -ar 44100 -ac 2 -b:a 128k "{mp3_path}" -y -loglevel quiet')
    if ret != 0 or not os.path.exists(mp3_path):
        raise ValueError("Error al convertir vídeo a audio con FFmpeg")
    return mp3_path


def download_audio(url: str, output_dir: str, platform: str) -> str:
    """Intenta Apify para Instagram; yt-dlp como fallback y para el resto."""
    if platform == "instagram" and APIFY_TOKEN:
        try:
            return _apify_instagram(url, output_dir)
        except Exception:
            pass  # fallback silencioso
    return _ytdlp(url, output_dir)


def transcribe_with_groq(audio_path: str, language: str | None = None) -> str:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    with open(audio_path, "rb") as f:
        files = {"file": ("audio.mp3", f, "audio/mpeg")}
        data = {"model": "whisper-large-v3", "response_format": "json"}
        if language:
            data["language"] = language
        resp = requests.post(GROQ_URL, headers=headers, files=files, data=data, timeout=120)
    resp.raise_for_status()
    return resp.json()["text"]


# ── Auth routes ───────────────────────────────────────────────────────────────

# v0.14.24: hook centralizado de signup completion
def _on_signup_complete(user_id, lang):
    """
    Idempotente: re-llamadas no insertan duplicados (UNIQUE en email_log).
    Genera unsubscribe_token solo si no existe ya en profiles.
    Encola 3 emails: welcome (T+0), day1_pending (T+24h), day7_pending (T+7d).
    Welcome se envía en background vía Celery (no bloquea response).
    """
    try:
        import emails as _emails
        # 1) ensure unsubscribe_token exists + lang persisted + trial iniciado
        prof = db.table("profiles").select("unsubscribe_token, lang, credits_cents, usage_reset_at").eq("id", user_id).execute()
        existing = (prof.data or [{}])[0]
        updates = {}
        if not existing.get("unsubscribe_token"):
            updates["unsubscribe_token"] = _emails.gen_unsubscribe_token()
        if not existing.get("lang"):
            updates["lang"] = lang or "es"
        # Modelo free 2026-06-23 (SIN trial): 30 créditos de BIENVENIDA al registrarse.
        # Idempotente: solo si aún no tiene saldo (re-llamadas no regalan otra vez).
        # usage_reset_at = mes que viene → la 1ª recarga de +15 cr cae el mes siguiente.
        started_trial = not existing.get("credits_cents")
        if started_trial:
            updates["credits_cents"] = WELCOME_CREDITS_CENTS
            updates["usage_reset_at"] = _next_month_boundary(
                datetime.now(timezone.utc)).isoformat()
        if updates:
            db.table("profiles").update(updates).eq("id", user_id).execute()
        # 2) enqueue email_log rows (idempotente vía UNIQUE)
        _emails.enqueue_signup_emails(user_id, lang or "es")
        # 3) dispatch welcome a Celery (fire-and-forget, no bloquea signup response)
        try:
            from tasks import send_email_now
            send_email_now.delay(user_id, "welcome")
        except Exception as e:
            logger.warning("send_email_now dispatch failed user=%s err=%s", user_id, e)
        # 4) growth-1: evento de funnel «registro» (server-side, fiable)
        track_event("user_registered", user_id, {"lang": lang or "es"})
        # panel: bienvenida (solo la 1ª vez, al regalar los 30 créditos).
        if started_trial:
            track_event("welcome_credits_granted", user_id, {"credits": WELCOME_CREDITS_CENTS // COST_CENTS})
    except Exception as e:
        # NUNCA romper el signup por errores en el flujo de email.
        logger.warning("_on_signup_complete failed user=%s err=%s", user_id, e)


@app.route("/auth/register", methods=["POST"])
@limiter.limit("5 per minute;20 per hour")
def auth_register():
    body = request.get_json()
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos"}), 400
    if len(password) < 6:
        return jsonify({"error": "La contraseña debe tener mínimo 6 caracteres"}), 400

    try:
        result = db.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
        })
        user = result.user
        session.permanent = True
        session["user"] = {"id": str(user.id), "email": user.email}
        # Save terms acceptance + affiliate ref
        upsert_data = {"id": str(user.id), "terms_accepted_at": datetime.now(timezone.utc).isoformat()}
        affiliate_ref = body.get("affiliate_ref", "").strip()
        if affiliate_ref:
            upsert_data["affiliate_ref"] = affiliate_ref
        db.table("profiles").upsert(upsert_data).execute()
        # v0.14.24: trigger email activation flow
        _on_signup_complete(str(user.id), _resolve_lang())
        return jsonify({"ok": True, "email": user.email})
    except Exception as e:
        # Supabase 422 al registrar un email ya existente llega como
        # "email_exists" / "User already been registered" / "already registered"
        # → mapear a 409 (antes caía al 500 genérico). logger.exception para no
        # quedarnos ciegos ante otros fallos.
        msg = str(e).lower()
        if ("already registered" in msg or "already exists" in msg or "duplicate" in msg
                or "email_exists" in msg or "already been registered" in msg
                or "user already" in msg):
            return jsonify({"error": "Este email ya está registrado"}), 409
        logger.exception("auth_register failed email=%s", email)
        return jsonify({"error": "Error al crear la cuenta"}), 500


@app.route("/auth/login", methods=["POST"])
@limiter.limit("10 per minute;30 per hour")
def auth_login():
    body = request.get_json()
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password", "")

    try:
        # Llamada directa a la REST API de GoTrue (funciona con service role key)
        resp = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_SERVICE_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=10,
        )
        if resp.status_code != 200:
            return jsonify({"error": "Email o contraseña incorrectos"}), 401
        data = resp.json()
        user = data["user"]
        session.permanent = True
        session["user"] = {"id": user["id"], "email": user["email"]}
        return jsonify({"ok": True, "email": user["email"]})
    except Exception:
        logger.error("auth_login failed", exc_info=True)
        return jsonify({"error": "Error de conexión"}), 500


@app.route("/auth/forgot-password", methods=["POST"])
@limiter.limit("3 per minute;10 per hour")
def forgot_password():
    body = request.get_json() or {}
    email = (body.get("email") or "").strip().lower()
    if not email or not validate_email(email):
        return jsonify({"error": "Valid email required"}), 400
    # v0.14.12: pass lang into Supabase template via .Data.lang for ES/EN conditional
    lang = _resolve_lang()
    redirect_to = request.host_url.rstrip("/") + "/reset-password?lang=" + lang
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/auth/v1/recover",
            headers={"apikey": SUPABASE_SERVICE_KEY, "Content-Type": "application/json"},
            json={
                "email": email,
                "redirect_to": redirect_to,
                "data": {"lang": lang, "language": lang},
            },
            timeout=10,
        )
        if resp.status_code not in (200, 204):
            return jsonify({"error": "Could not send reset email"}), 500
        return jsonify({"ok": True})
    except Exception:
        logger.error("Forgot password error", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@app.route("/auth/reset-password", methods=["POST"])
@limiter.limit("5 per minute")
def reset_password():
    body = request.get_json() or {}
    access_token = (body.get("access_token") or "").strip()
    new_password = body.get("password", "")

    if not access_token:
        return jsonify({"error": "Token required"}), 400
    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    try:
        resp = requests.put(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"password": new_password},
            timeout=10,
        )
        if resp.status_code != 200:
            return jsonify({"error": "Invalid or expired token"}), 400
        return jsonify({"ok": True})
    except Exception:
        logger.error("Reset password error", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@app.route("/auth/google")
def auth_google():
    """Redirect to Google OAuth via Supabase."""
    redirect_to = request.host_url.rstrip("/")
    url = f"{SUPABASE_URL}/auth/v1/authorize?provider=google&redirect_to={redirect_to}"
    return jsonify({"url": url})


@app.route("/auth/callback", methods=["POST"])
def auth_callback():
    """Exchange OAuth access_token for a Flask session."""
    body = request.get_json() or {}
    access_token = (body.get("access_token") or "").strip()
    if not access_token:
        return jsonify({"error": "Token required"}), 400

    try:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code != 200:
            return jsonify({"error": "Invalid token"}), 401

        user = resp.json()
        session.permanent = True
        session["user"] = {"id": user["id"], "email": user.get("email", "")}

        # Ensure profile exists
        prof = db.table("profiles").select("id, credits_cents").eq("id", user["id"]).execute()
        prow = (prof.data or [{}])[0]
        is_new_signup = not prof.data
        if is_new_signup:
            db.table("profiles").insert({"id": user["id"]}).execute()
        # Welcome (30 créditos) + emails de signup. ROBUSTO ante el trigger de Supabase que
        # PRE-CREA el perfil: si lo gateáramos solo en is_new_signup, los signups de Google
        # (perfil ya existe → is_new_signup=False) se quedaban con 0 créditos y NO podían
        # robar (bug 2026-06-26). Concede también si el perfil existe SIN saldo y la CUENTA
        # de auth es RECIENTE (created_at ≤ 1 día) → cubre el signup nuevo de Google sin
        # tocar a usuarios viejos (created_at antiguo) ni ser farmeable (created_at es fijo).
        _grant = is_new_signup
        if not _grant and not prow.get("credits_cents"):
            _ca = _parse_ts(user.get("created_at"))
            if _ca and (datetime.now(timezone.utc) - _ca).total_seconds() < 86400:
                _grant = True
        if _grant:
            _on_signup_complete(user["id"], _resolve_lang())

        return jsonify({"ok": True, "email": user.get("email", "")})
    except Exception as e:
        logger.error(f"OAuth callback error: {e}", exc_info=True)
        return jsonify({"error": "Authentication failed"}), 500


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/auth/me")
def auth_me():
    user = current_user()
    if not user:
        return jsonify({"user": None, "free_daily_anon": FREE_DAILY_ANON})
    profile = get_profile(user["id"])
    plan = profile.get("plan", "free")
    return jsonify({
        "user": user,
        "is_admin": _is_admin(user),
        "credits_cents":   profile["credits_cents"],
        "free_used_today": profile["free_used_today"],
        "free_daily_limit": FREE_DAILY_USER,
        "plan": plan,
        "monthly_usage": profile.get("monthly_usage", 0),
        "monthly_limit": plan_credit_limit(profile),   # incl. +96 cr/marca extra (Agencia)
        # v0.19: créditos unificados (mensual restante + topups) para la pill del radar.
        "credits": credits_available(profile),
        # econ §4: flash del Pack 300 a 29€ (48h tras el 1er muro) — el front lo muestra.
        "topup_flash": topup_flash_state(user["id"]),
        # reverse-trial: estado del trial (Pro capado sin tarjeta) + free MENSUAL.
        # trial_active = usable (dentro de ventana Y con tope disponible).
        "trial_active": trial_usable(profile),
        "trial_in_window": in_trial(profile),
        "trial_ends_at": profile.get("trial_ends_at"),
        "trial_days_left": trial_days_left(profile),
        "trial_credit_cap": TRIAL_CREDIT_CAP,
        "trial_credits_left": trial_credits_left(profile) if in_trial(profile) else 0,
        # Fathom 18/06: tope diario del trial (3 guiones/día). El front lo muestra "N hoy".
        "trial_daily_cap": TRIAL_DAILY_SCRIPTS,
        "trial_daily_left": trial_daily_left(profile),
        # Fathom 18/06: % del Cerebro gamificado (35% + ejercicio diario +3-6%).
        "brain_progress": profile.get("brain_progress") if profile.get("brain_progress") is not None else 35,
        "brain_exercise_date": (str(profile.get("brain_exercise_date")) if profile.get("brain_exercise_date") else None),
        "brain_last_gain": profile.get("brain_last_gain") or 4,
        # Lista de espera de la Comunidad (teaser "Avísame").
        "community_waitlist": bool(profile.get("community_waitlist_at")),
        "effective_plan": effective_plan(profile),
        # watermark en exports: solo free post-trial (ni pago ni trial).
        "watermark": not paid_features_active(profile, user),
        # Free MENSUAL: guiones «Hazlo mío» (free_scripts_monthly) + análisis.
        "free_lifetime_limit": PLANS["free"]["free_scripts_monthly"],
        "free_lifetime_used": _free_month_used(profile, "free_lifetime_uses"),
        "free_lifetime_left": free_lifetime_left(profile),
        "free_analysis_limit": PLANS["free"].get("free_analysis_weekly", 3),
        "free_analysis_used": max(0, PLANS["free"].get("free_analysis_weekly", 3) - free_analysis_left_week(profile["id"])),
        "free_analysis_left": free_analysis_left_week(profile["id"]),
        "avatar_seed": profile.get("avatar_seed", "default"),
        "has_stripe_sub": bool(profile.get("stripe_subscription_id")),
        # A1: tono preset (personalidad del 1er guion sin voz) + opciones + si hay voz.
        "preset_tone": (profile.get("default_idea_assistant") if profile.get("default_idea_assistant") in PRESET_TONE_KEYS else DEFAULT_PRESET_TONE),
        "preset_tones": PRESET_TONES,
        "has_voice": bool(get_voice_profile(user["id"])),
        # onboarding v2: gatea la pantalla dedicada en prod (None si falta columna → muestra onboarding)
        "onb_v2_done": bool(profile.get("onboarding_v2_done")),
        "niche": profile.get("niche"),
        "subniches": profile.get("subniches") or [],
        "goal": profile.get("goal"),
    })


# v0.14.16 — Subscription detail (Settings panel "Tu suscripción")
_subscription_cache: dict = {}  # uid -> (data, ts)
_SUBSCRIPTION_TTL_S = 60


@app.route("/api/me/subscription")
@require_auth
def api_me_subscription():
    """Detalle de suscripción para Settings. Cacheado 60s por user_id
    para no martillar la API de Stripe en cada apertura del panel."""
    user = current_user()
    uid = user["id"]
    now_ts = _time.time()
    cached = _subscription_cache.get(uid)
    if cached and (now_ts - cached[1]) < _SUBSCRIPTION_TTL_S:
        return jsonify(cached[0])

    profile = get_profile(uid)
    plan = profile.get("plan", "free")
    sub_id = profile.get("stripe_subscription_id")

    # v0.14.17: courtesy / complimentary plan = paid plan tier sin stripe_subscription_id
    # (admin grants, partnerships, agency-internal, etc.). Frontend renderiza un estado
    # dedicado: muestra plan name + msg "sin facturación recurrente", sin botones Stripe.
    is_complimentary = bool(plan and plan != "free" and not sub_id)

    payload = {
        "plan": plan,
        "billing_cycle": None,
        "next_renewal_date": None,
        "amount_cents": None,
        "currency": None,
        "cancel_at_period_end": False,
        "paused": False,            # growth-4: pausa activa (pause_collection)
        "paused_until": None,       # ISO de resumes_at
        "has_stripe_sub": bool(sub_id),
        "is_complimentary": is_complimentary,
    }

    if sub_id and STRIPE_OK:
        try:
            sub = stripe_lib.Subscription.retrieve(sub_id, expand=["items.data.price"])
            item = (sub.get("items") or {}).get("data") or []
            price = (item[0].get("price") if item else {}) or {}
            recurring = price.get("recurring") or {}
            interval = recurring.get("interval")  # "month" | "year"
            payload["billing_cycle"] = "yearly" if interval == "year" else ("monthly" if interval == "month" else None)
            cpe = sub.get("current_period_end")
            if cpe:
                payload["next_renewal_date"] = datetime.fromtimestamp(cpe, tz=timezone.utc).isoformat()
            payload["amount_cents"] = price.get("unit_amount")
            payload["currency"] = price.get("currency")
            payload["cancel_at_period_end"] = bool(sub.get("cancel_at_period_end"))
            # growth-4: estado de pausa leído de Stripe (sin columnas nuevas).
            pc = sub.get("pause_collection") or None
            if pc:
                payload["paused"] = True
                ra = pc.get("resumes_at") if isinstance(pc, dict) else None
                if ra:
                    payload["paused_until"] = datetime.fromtimestamp(ra, tz=timezone.utc).isoformat()
        except Exception as e:
            logger.warning("subscription fetch failed for %s: %s", uid, e)

    _subscription_cache[uid] = (payload, now_ts)
    return jsonify(payload)


# ── Transcription route ───────────────────────────────────────────────────────

from tasks import transcribe_task  # noqa: E402

@app.route("/transcribe", methods=["POST"])
@limiter.limit("10 per minute;50 per hour;200 per day")
def transcribe():
    body = request.get_json() or {}
    url = (body.get("url") or "").strip()
    language = (body.get("language") or "").strip() or None

    err = validate_url(url)
    if err:
        return jsonify({"error": err}), 400
    if not GROQ_API_KEY:
        return jsonify({"error": "Service unavailable"}), 500

    platform = detect_platform(url)
    if platform == "youtube":
        return jsonify({
            "error": "YouTube estará disponible próximamente en el plan de pago. "
                     "Por ahora, puedes transcribir reels de Instagram y vídeos de TikTok."
        }), 400

    user = current_user()

    # ── Comprobar límites / saldo ─────────────────────────────────────────
    if user and user.get("email", "").lower() in UNLIMITED_EMAILS:
        pass
    elif user is None:
        ip = get_client_ip()
        ip_usage = get_or_reset_ip_usage(ip)
        if ip_usage["used_today"] >= FREE_DAILY_ANON:
            return jsonify({
                "error": f"Límite diario alcanzado ({FREE_DAILY_ANON} gratis/día sin cuenta). "
                         "Regístrate para obtener más transcripciones gratuitas."
            }), 429
    else:
        profile = get_profile(user["id"])
        if paid_features_active(profile, user):
            ok, err_msg = check_monthly_limit(profile)
            if not ok:
                return jsonify({"error": err_msg}), 429
        elif profile["credits_cents"] >= ANALYSIS_UNITS * COST_CENTS:
            # Modelo 2026-06-23: analizar un reel = 2 créditos (sin análisis gratis/semana).
            pass
        else:
            track_event("paywall_shown", user["id"], {
                "wall": "analysis", "plan": profile.get("plan", "free"),
                "after_first_value": True,
            })
            return jsonify({
                "error": f"Analizar un reel cuesta {ANALYSIS_UNITS} créditos y no te quedan. "
                         "Recarga créditos o sube de plan."
            }), 402

    # ── Actualizar contador antes de encolar ──────────────────────────────
    is_unlimited = user and user.get("email", "").lower() in UNLIMITED_EMAILS
    cost_cents = 0
    # addreel: qué se cobró → viaja a la task para REEMBOLSAR si falla la
    # descarga/transcripción. Antes una URL rota quemaba 1 de los 3 análisis free.
    charge = None

    if user is None:
        db.table("ip_usage").update(
            {"used_today": ip_usage["used_today"] + 1}
        ).eq("ip", ip).execute()
        charge = {"kind": "ip"}
    elif not is_unlimited:
        # BAJO LOCK por-usuario (Redis) + re-lectura: evita doble-gasto multi-worker.
        _tclock = acquire_credit_lock(user["id"])
        try:
            fresh = get_profile(user["id"])
            if paid_features_active(fresh, user):
                db.table("profiles").update({
                    "monthly_usage": (fresh.get("monthly_usage") or 0) + 1
                }).eq("id", user["id"]).execute()
                charge = {"kind": "monthly"}
            elif (fresh.get("credits_cents") or 0) >= ANALYSIS_UNITS * COST_CENTS:
                cost_cents = ANALYSIS_UNITS * COST_CENTS
                db.table("profiles").update(
                    {"credits_cents": (fresh.get("credits_cents") or 0) - cost_cents}
                ).eq("id", user["id"]).execute()
                charge = {"kind": "credits", "cents": cost_cents}
            else:
                # Carrera: saldo agotado entre el check y el lock.
                return jsonify({
                    "error": f"Analizar un reel cuesta {ANALYSIS_UNITS} créditos y no te quedan. "
                             "Recarga créditos o sube de plan."
                }), 402
        finally:
            release_credit_lock(user["id"], _tclock)

    # ── Encolar tarea ─────────────────────────────────────────────────────
    # v0.14.7: paid plans (pro/creator/agency) → métricas Apify guardadas.
    is_paid = bool(
        user
        and get_profile(user["id"]).get("plan", "free") in ("pro", "creator", "agency")
    )
    task = transcribe_task.delay(
        url,
        language,
        user["id"] if user else None,
        get_client_ip() if not user else None,
        is_paid,
        charge,
    )

    return jsonify({"task_id": task.id, "cost_cents": cost_cents})


@app.route("/transcribe-preview", methods=["POST"])
@limiter.limit("3 per day")
def transcribe_preview():
    """Anonymous preview transcription used by the landing #tryFree section.
    Returns the full transcription via the regular /task/<id> polling endpoint;
    the landing UI truncates to 3 sentences and shows a signup CTA.

    Tech debt v0.15: truncate audio before Whisper to cut Groq cost on previews.
    """
    body = request.get_json() or {}
    url = (body.get("url") or "").strip()
    language = (body.get("language") or "").strip() or None

    err = validate_url(url)
    if err:
        return jsonify({"error": err}), 400
    if not GROQ_API_KEY:
        return jsonify({"error": "Service unavailable"}), 500

    platform = detect_platform(url)
    if platform == "youtube":
        return jsonify({
            "error": "YouTube no disponible. Solo Instagram y TikTok."
        }), 400

    task = transcribe_task.delay(url, language, None, get_client_ip())
    return jsonify({"task_id": task.id, "preview": True})


# ── Public stats (landing social proof) ────────────────────────────────────
import time as _time  # noqa: E402

_stats_cache = {"data": None, "ts": 0.0}

@app.route("/api/stats/today")
def api_stats_today():
    """Public counter feeding the landing 'X reels transcritos hoy' line.
    Cached 60s in-process to avoid hammering Supabase from anonymous traffic."""
    now = _time.time()
    if _stats_cache["data"] and (now - _stats_cache["ts"]) < 60:
        return jsonify(_stats_cache["data"])
    today_count = 0
    week_count = 0
    creators_week = 0
    try:
        today_iso = datetime.utcnow().date().isoformat()
        week_ago_iso = (datetime.utcnow() - timedelta(days=7)).isoformat()
        r1 = db.table("transcriptions").select("id", count="exact").gte("created_at", today_iso).execute()
        today_count = r1.count or 0
        r2 = db.table("transcriptions").select("user_id").gte("created_at", week_ago_iso).execute()
        rows = r2.data or []
        week_count = len(rows)
        creators_week = len({r.get("user_id") for r in rows if r.get("user_id")})
    except Exception as e:
        logger.warning("api_stats_today error: %s", e)
    data = {
        "transcripts_today": today_count,
        "transcripts_week": week_count,
        "creators_week": creators_week,
    }
    _stats_cache["data"] = data
    _stats_cache["ts"] = now
    return jsonify(data)


@app.route("/api/topups", methods=["GET"])
def api_topups_public():
    """Público (sin admin): packs de créditos ACTIVOS para pintar la UI de recarga.
    Devuelve {"topups":[{"key","credits","price_cents"}]} ordenados por sort_order.
    NO expone stripe_price_id. Degrada a [] si la config falla."""
    try:
        topups = load_topups_config().get("topups", [])
    except Exception as e:
        logger.warning("api_topups_public error: %s", e)
        topups = []
    # load_topups_config ya devuelve los topups ordenados por sort_order
    # (DB .order("sort_order") y, en fallback env, en orden de inserción).
    out = []
    for t in sorted(topups, key=lambda x: x.get("sort_order", 0)):
        if not t.get("active", True):
            continue
        out.append({
            "key": str(t.get("key", "")),
            "credits": t.get("credits", 0),
            "price_cents": t.get("price_cents", 0),
        })
    return jsonify({"topups": out})


@app.route("/task/<task_id>")
def task_status(task_id):
    task = transcribe_task.AsyncResult(task_id)

    if task.state == "PENDING":
        return jsonify({"state": "pending", "step": "En cola..."})
    elif task.state == "PROGRESS":
        return jsonify({"state": "progress", "step": task.info.get("step", "Procesando...")})
    elif task.state == "SUCCESS":
        result = task.result
        if not result.get("ok"):
            return jsonify({"state": "error", "error": result.get("error", "Error desconocido")})
        payload = {"state": "success", "text": result["text"], "platform": result["platform"],
                   "username": result.get("username")}
        user = current_user()
        if user:
            updated = get_profile(user["id"])
            payload["credits_cents"] = updated["credits_cents"]
            payload["free_used_today"] = updated["free_used_today"]
        return jsonify(payload)
    elif task.state == "FAILURE":
        return jsonify({"state": "error", "error": str(task.info)})
    else:
        return jsonify({"state": "progress", "step": "Procesando..."})


# ── History routes ────────────────────────────────────────────────────────────

@app.route("/history")
@require_auth
def history():
    user = current_user()
    rows = (
        db.table("transcriptions")
        .select(
            "id, url, platform, language, text, cost_cents, created_at, thumbnail_b64, "
            "author_username, views, likes, comments, shares, published_at, metrics_updated_at"
        )
        .eq("user_id", user["id"])
        .order("id", desc=True)
        .limit(50)
        .execute()
    )
    return jsonify(rows.data)


@app.route("/history/<int:tid>", methods=["DELETE"])
@require_auth
def delete_transcription(tid: int):
    user = current_user()
    db.table("transcriptions").delete().eq("id", tid).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


# ── Feedback (menú de cuenta): reportar bug / pedir mejora ────────────────────
# Recompensa: si el bug es real, se abonan créditos al confirmarlo (flujo manual
# por ahora; ver TODO). La imagen viaja como dataURL base64 (igual que thumbnail_b64).
@app.route("/api/feedback", methods=["POST"])
@require_auth
def submit_feedback():
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    ftype = str(body.get("type") or "bug")[:20]
    text = str(body.get("text") or "").strip()[:4000]
    page = str(body.get("page") or "")[:60]
    plan = str(body.get("plan") or "")[:30]
    image_b64 = body.get("image_b64") or None
    if not text:
        return jsonify({"ok": False, "error": "empty"}), 400
    # Cap de imagen (~3.5 MB de base64); si se pasa, se descarta para no reventar la fila.
    if image_b64 and (not isinstance(image_b64, str) or len(image_b64) > 3_600_000):
        image_b64 = None
    # 1) PostHog — siempre (el feedback queda consultable aunque la tabla no exista aún).
    try:
        track_event("feedback_submitted", uid, {
            "type": ftype, "page": page, "plan": plan,
            "length": len(text), "has_image": bool(image_b64),
        })
    except Exception:
        pass
    # 2) Persistencia best-effort en la tabla `feedback` (si existe). TODO(fable):
    #    crear tabla `feedback` (user_id, type, text, page, plan, image_b64, status,
    #    created_at) + flujo de revisión y abono de créditos al confirmar el bug.
    try:
        db.table("feedback").insert({
            "user_id": uid, "type": ftype, "text": text,
            "page": page, "plan": plan, "image_b64": image_b64, "status": "new",
        }).execute()
    except Exception as e:  # tabla ausente u otro fallo de persistencia → no rompe el envío
        try:
            app.logger.warning("feedback insert skipped: %s", e)
        except Exception:
            pass
    return jsonify({"ok": True})


# ── v0.14.7: refresh métricas Apify ──────────────────────────────────────────

_metrics_refresh_cooldown: dict = {}  # (uid, tid) -> ts (last refresh)
_METRICS_REFRESH_TTL = 30  # seconds
_METRICS_BULK_LIMIT = 50


def _is_metrics_plan(profile: dict) -> bool:
    # v0.19: exige plan de pago REAL (con sub o cortesía), no "fantasma".
    return paid_features_active(profile)


@app.route("/transcriptions/<int:tid>/refresh-metrics", methods=["POST"])
@require_auth
@limiter.limit("30 per minute")
def refresh_transcription_metrics(tid: int):
    user = current_user()
    # Métricas abiertas a TODOS los planes (decisión Leo): el dato es info pública y el
    # cooldown 30s + rate-limit ya frenan el abuso de llamadas Apify. (Antes capado.)

    # Cooldown 30s por (uid, tid) para evitar abuso — en Redis (compartido entre
    # workers; reserva atómica con SET NX EX). Fallback a memoria si Redis no está.
    rkey = "metcd:%s:%s" % (user["id"], tid)
    if rds is not None:
        try:
            if not rds.set(rkey, "1", nx=True, ex=_METRICS_REFRESH_TTL):
                ttl = rds.ttl(rkey)
                wait = ttl if (ttl and ttl > 0) else _METRICS_REFRESH_TTL
                return jsonify({"error": f"Espera {wait}s antes de refrescar de nuevo.",
                                "retry_after": wait}), 429
        except Exception:
            pass  # Redis caído → no bloquear (degradado)
    else:
        mkey = (user["id"], tid)
        now = _time.time()
        last = _metrics_refresh_cooldown.get(mkey, 0)
        if now - last < _METRICS_REFRESH_TTL:
            wait = int(_METRICS_REFRESH_TTL - (now - last))
            return jsonify({"error": f"Espera {wait}s antes de refrescar de nuevo.",
                            "retry_after": wait}), 429
        _metrics_refresh_cooldown[mkey] = now

    # Ownership + platform check
    row_r = (db.table("transcriptions")
               .select("id, url, platform, user_id")
               .eq("id", tid)
               .eq("user_id", user["id"])
               .limit(1)
               .execute())
    if not row_r.data:
        return jsonify({"error": "Transcripción no encontrada"}), 404
    row = row_r.data[0]
    if row.get("platform") != "instagram":
        return jsonify({"error": "Las métricas solo están disponibles para Instagram",
                        "platform_unsupported": True}), 400

    # Llamada Apify (el cooldown ya quedó reservado arriba: Redis SET NX o memoria)
    from tasks import _apify_metrics_only, _extract_metrics  # noqa: E402
    item = _apify_metrics_only(row["url"])
    if not item:
        return jsonify({"error": "No se pudieron obtener las métricas. ¿La URL sigue accesible?"}), 502
    metrics = _extract_metrics(item)
    db.table("transcriptions").update(metrics).eq("id", tid).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True, "metrics": metrics})


@app.route("/transcriptions/refresh-metrics-bulk", methods=["POST"])
@require_auth
@limiter.limit("3 per hour")
def refresh_transcription_metrics_bulk():
    user = current_user()
    # Métricas abiertas a todos los planes (decisión Leo). El rate-limit (3/h) ya frena
    # el abuso. (Antes capado a _is_metrics_plan.)

    # Selecciona hasta 50 más recientes (instagram only) cuyas métricas sean
    # NULL o tengan más de 24h.
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat() + "Z"
    rows_r = (db.table("transcriptions")
                .select("id, metrics_updated_at, platform")
                .eq("user_id", user["id"])
                .eq("platform", "instagram")
                .order("created_at", desc=True)
                .limit(_METRICS_BULK_LIMIT)
                .execute())
    rows = rows_r.data or []
    candidates = [r["id"] for r in rows
                  if not r.get("metrics_updated_at") or r.get("metrics_updated_at") < cutoff]
    if not candidates:
        return jsonify({"ok": True, "queued": 0, "message": "Todas las métricas están al día."})

    # Total de transcripciones IG del user (para informar truncado)
    total_r = (db.table("transcriptions")
                 .select("id", count="exact")
                 .eq("user_id", user["id"])
                 .eq("platform", "instagram")
                 .execute())
    total_ig = total_r.count or 0

    from tasks import refresh_metrics_bulk
    task = refresh_metrics_bulk.delay(user["id"], candidates)
    truncated = total_ig > _METRICS_BULK_LIMIT
    return jsonify({
        "ok": True,
        "queued": len(candidates),
        "task_id": task.id,
        "truncated": truncated,
        "total_ig": total_ig,
        "limit": _METRICS_BULK_LIMIT,
    })


@app.route("/transcriptions/refresh-metrics-bulk/<task_id>")
@require_auth
def refresh_transcription_metrics_bulk_status(task_id: str):
    """Polling endpoint para que el frontend sepa progreso del bulk refresh."""
    from tasks import refresh_metrics_bulk
    task = refresh_metrics_bulk.AsyncResult(task_id)
    if task.state == "PENDING":
        return jsonify({"state": "pending"})
    if task.state == "PROGRESS":
        info = task.info or {}
        return jsonify({"state": "progress", "updated": info.get("updated", 0), "total": info.get("total", 0)})
    if task.state == "SUCCESS":
        return jsonify({"state": "success", **(task.result or {})})
    if task.state == "FAILURE":
        return jsonify({"state": "failure", "error": str(task.info)}), 500
    return jsonify({"state": task.state})


@app.route("/download/<int:tid>")
@require_auth
def download_transcription(tid: int):
    user = current_user()
    result = (
        db.table("transcriptions")
        .select("url, text, created_at")
        .eq("id", tid)
        .eq("user_id", user["id"])
        .execute()
    )
    if not result.data:
        return jsonify({"error": "No encontrado"}), 404
    row = result.data[0]
    content = f"URL: {row['url']}\nFecha: {row['created_at']}\n\n{row['text']}"
    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename=transcripcion_{tid}.txt"},
    )


# ── Stripe / payments ─────────────────────────────────────────────────────────

@app.route("/checkout", methods=["POST"])
@limiter.limit("5 per minute;20 per hour")
@require_auth
def create_checkout():
    if not STRIPE_OK:
        return jsonify({"error": "El sistema de pagos aún no está disponible. Vuelve pronto."}), 503

    body = request.get_json() or {}
    currency = body.get("currency", "usd").lower()
    if currency not in ("usd", "eur"):
        currency = "usd"

    # ── Resolución de topup vía config DB con fallback a env (no rompe el path actual) ──
    # Si el front manda `topup_key` y hay un topup con stripe_price_id configurado
    # (DB o env), se usa ese precio + sus créditos. Si no, se degrada al topup legacy
    # (STRIPE_TOPUP_PRICE + 7 usos = 126¢), idéntico al comportamiento previo.
    cost_cents = get_cost_cents()
    topup_key = body.get("topup_key")
    stripe_price = None
    amount_cents = None
    if topup_key is not None:
        topups = {t["key"]: t for t in load_topups_config().get("topups", [])}
        t = topups.get(str(topup_key))
        if t and t.get("stripe_price_id"):
            stripe_price = t["stripe_price_id"]
            amount_cents = int(t["credits"]) * cost_cents

    if stripe_price is None:
        # Fallback legacy: topup único de 7 usos (126¢) con STRIPE_TOPUP_PRICE.
        if not STRIPE_TOPUP_PRICE:
            return jsonify({"error": "Topup no configurado"}), 500
        stripe_price = STRIPE_TOPUP_PRICE
        amount_cents = 126

    user = current_user()
    try:
        checkout_session = stripe_lib.checkout.Session.create(
            payment_method_types=["card"],
            currency=currency,
            line_items=[{"price": stripe_price, "quantity": 1}],
            mode="payment",
            success_url=request.host_url + "?topup=success",
            cancel_url=request.host_url + "?topup=cancel",
            metadata={"user_id": user["id"], "amount_cents": str(amount_cents)},
        )
        db.table("payments").insert({
            "user_id":          user["id"],
            "stripe_session_id": checkout_session.id,
            "amount_cents":     amount_cents,
            "status":           "pending",
        }).execute()
        return jsonify({"url": checkout_session.url})
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error. Please try again."}), 500


@app.route("/stripe-webhook", methods=["POST"])
@limiter.exempt
def stripe_webhook():
    if not STRIPE_OK or not STRIPE_WEBHOOK_SECRET:
        return "", 200

    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not sig_header:
        return jsonify({"error": "Missing signature"}), 400

    try:
        event = stripe_lib.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return jsonify({"error": "Invalid payload"}), 400
    except Exception:
        logger.warning(f"Invalid Stripe signature from IP: {get_client_ip()}")
        return "", 400

    if event["type"] == "checkout.session.completed":
        obj               = event["data"]["object"]
        stripe_session_id = obj["id"]
        user_id           = obj["metadata"]["user_id"]

        if obj["metadata"].get("type") == "addon_brand":
            # ── Ola Agencia B4: marca extra comprada → +1 slot ───────────
            try:
                prof = (db.table("profiles").select("extra_brand_slots")
                          .eq("id", user_id).single().execute())
                cur = int((prof.data or {}).get("extra_brand_slots") or 0)
                db.table("profiles").update({"extra_brand_slots": cur + 1}).eq("id", user_id).execute()
                track_event("brand_addon_purchased", user_id, {"slots": cur + 1})
            except Exception as e:
                logger.error("addon_brand webhook failed user=%s err=%s", user_id, e)
        elif obj["metadata"].get("type") == "subscription":
            # ── Suscripción ──────────────────────────────────────────
            line_items = stripe_lib.checkout.Session.list_line_items(stripe_session_id)
            price_id = line_items.data[0].price.id if line_items.data else None
            plan = PRICE_TO_PLAN.get(price_id, "creator")
            db.table("profiles").update({
                "plan": plan,
                "stripe_subscription_id": obj.get("subscription"),
            }).eq("id", user_id).execute()
            # Acreditar créditos mensuales del plan (reset del contador + próximo reset).
            grant_monthly_allowance(user_id, plan)
            # growth-1: evento de funnel «upgrade» (conversión free→pago).
            track_event("subscription_upgraded", user_id, {
                "plan": plan, "price_id": price_id,
                "amount_cents": _plan_mrr_cents(plan),
            })
        else:
            # ── Recarga de créditos (topup) ──────────────────────────
            # Nuevo: por price de topup (→ nº de créditos). Legacy: metadata amount_cents.
            meta_amount = obj["metadata"].get("amount_cents")
            if meta_amount is not None:
                amount_cents = int(meta_amount)
            else:
                line_items = stripe_lib.checkout.Session.list_line_items(stripe_session_id)
                price_id = line_items.data[0].price.id if line_items.data else None
                # GATE addon: el price de "marca extra" (STRIPE_PRICE_ADDON_BRAND)
                # apunta a un producto que LEGACY daba 150 créditos (topup). La compra
                # real se enruta por la rama type=="addon_brand" (solo slot). Si por
                # config DB heredada cayera aquí, NO acreditar créditos — solo loguear.
                if STRIPE_PRICE_ADDON_BRAND and price_id == STRIPE_PRICE_ADDON_BRAND:
                    logger.warning("addon_brand price hit topup branch (session=%s) — "
                                   "NO se acreditan créditos legacy", stripe_session_id)
                    credits = 0
                else:
                    # Resolver créditos por price_id: config DB con fallback a env (STRIPE_TOPUP_PRICES).
                    credits = None
                    try:
                        by_price = {
                            t.get("stripe_price_id"): t.get("credits")
                            for t in load_topups_config().get("topups", [])
                            if t.get("stripe_price_id")
                        }
                        credits = by_price.get(price_id)
                    except Exception:
                        credits = None
                    if credits is None:
                        credits = STRIPE_TOPUP_PRICES.get(price_id, 0)
                amount_cents = (credits or 0) * get_cost_cents()

            db.table("payments").update({
                "status":                "completed",
                "stripe_payment_intent": obj.get("payment_intent"),
            }).eq("stripe_session_id", stripe_session_id).execute()

            profile = get_profile(user_id)
            db.table("profiles").update({
                "credits_cents": profile["credits_cents"] + amount_cents
            }).eq("id", user_id).execute()

            # ── Registrar conversión de afiliado ─────────────────────
            ref_result = db.table("profiles").select("affiliate_ref").eq("id", user_id).single().execute()
            ref = ref_result.data.get("affiliate_ref") if ref_result.data else None
            if ref:
                affiliate = db.table("affiliates").select("commission_pct").eq("code", ref).single().execute()
                if affiliate.data:
                    pct = affiliate.data["commission_pct"]
                    commission = int(amount_cents * pct / 100)
                    db.table("affiliate_conversions").insert({
                        "affiliate_code": ref,
                        "user_id": user_id,
                        "amount_cents": amount_cents,
                        "commission_cents": commission,
                        "stripe_session_id": stripe_session_id,
                    }).execute()

    elif event["type"] == "invoice.paid":
        # ── Renovación mensual: re-acreditar créditos del plan ───────────
        inv = event["data"]["object"]
        if inv.get("billing_reason") == "subscription_cycle":
            sub_id = inv.get("subscription")
            _line0 = (inv.get("lines", {}).get("data") or [{}])[0]
            _inv_price = (_line0.get("price") or {}).get("id")
            # GATE addon: el "marca extra" es una suscripción recurrente APARTE; sus
            # invoices NO son renovación de plan ni dan créditos. Saltar siempre.
            if STRIPE_PRICE_ADDON_BRAND and _inv_price == STRIPE_PRICE_ADDON_BRAND:
                logger.info("invoice.paid addon_brand (sub=%s) — no es plan, skip", sub_id)
            elif sub_id:
                prof = (db.table("profiles").select("id")
                          .eq("stripe_subscription_id", sub_id).limit(1).execute())
                if prof.data:
                    uid2 = prof.data[0]["id"]
                    plan2 = PRICE_TO_PLAN.get(_inv_price) or "creator"
                    db.table("profiles").update({"plan": plan2}).eq("id", uid2).execute()
                    grant_monthly_allowance(uid2, plan2)

    elif event["type"] == "invoice.payment_failed":
        # growth-5: DUNNING. Stripe Smart Retries (dashboard) reintenta el cobro;
        # nosotros avisamos al usuario para que actualice su tarjeta antes de
        # caer a Free. Idempotente por invoice (no spamea en cada reintento).
        inv = event["data"]["object"]
        sub_id = inv.get("subscription")
        if sub_id:
            try:
                prof = (db.table("profiles").select("id, plan")
                          .eq("stripe_subscription_id", sub_id).limit(1).execute())
                if prof.data:
                    duid = prof.data[0]["id"]
                    attempt = inv.get("attempt_count")
                    track_event("payment_failed", duid, {
                        "plan": prof.data[0].get("plan"),
                        "attempt": attempt,
                        "amount_due": inv.get("amount_due"),
                    })
                    try:
                        from tasks import send_payment_failed_email
                        send_payment_failed_email.delay(duid, inv.get("id"), attempt)
                    except Exception as e:
                        logger.warning("dunning email dispatch failed user=%s err=%s", duid, e)
            except Exception as e:
                logger.warning("invoice.payment_failed handling failed sub=%s err=%s", sub_id, e)

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        # growth-1: capturar el uid ANTES del update (luego el sub_id se borra).
        _cuid = None
        try:
            _cp = (db.table("profiles").select("id, plan")
                     .eq("stripe_subscription_id", sub["id"]).limit(1).execute())
            if _cp.data:
                _cuid = _cp.data[0]["id"]
                _cplan = _cp.data[0].get("plan")
        except Exception:
            pass
        db.table("profiles").update({
            "plan": "free",
            "stripe_subscription_id": None,
        }).eq("stripe_subscription_id", sub["id"]).execute()
        # growth-1: cancelación efectiva (fin de periodo). El cancel-flow in-app
        # (bloque 4) emitirá su propio evento al solicitar la baja/pausa.
        if _cuid:
            track_event("subscription_cancelled", _cuid, {
                "from_plan": _cplan, "reason": "period_end",
            })

    return "", 200


# ── Subscription endpoints ───────────────────────────────────────────────────

# Legacy price IDs (grandfathering): suscripciones activas conservan su precio.
# NO se ofrecen ya; no borrar hasta que Stripe reporte 0 subs contra ellos.
_LEGACY_PRICE_TO_PLAN = {
    "price_1TI14pCWQn5Tis1WycY83MrR": "pro",     # legacy basic → pro
    "price_1TI15ACWQn5Tis1WKNbdhFW1": "pro",     # legacy pro → pro
    "price_1TI15NCWQn5Tis1WwIIb1TX1": "agency",  # legacy agency
    "price_1TPz2tCWQn5Tis1W5CdUwBmN": "pro",     # legacy Pro monthly
    "price_1TPz2tCWQn5Tis1W1HNRJlL9": "pro",     # legacy Pro yearly
    "price_1TPz8tCWQn5Tis1WJDtNSlr7": "creator", # legacy Creator monthly
    "price_1TPz8tCWQn5Tis1WxxF2WeHs": "creator", # legacy Creator yearly
    "price_1TPzAbCWQn5Tis1WSylC7yNa": "agency",  # legacy Agency monthly
    "price_1TPzAbCWQn5Tis1WRGwN81Ca": "agency",  # legacy Agency yearly
}

# v0.19: price → plan se construye desde env (STRIPE_PRICES) + legacy. Sin hardcodear.
def _build_price_to_plan():
    m = dict(_LEGACY_PRICE_TO_PLAN)
    for plan, cyc in STRIPE_PRICES.items():
        for pid in (cyc.get("month"), cyc.get("year")):
            if pid:
                m[pid] = plan
    return m

PRICE_TO_PLAN = _build_price_to_plan()


def grant_monthly_allowance(user_id: str, plan: str) -> None:
    """Acredita los créditos mensuales del plan: resetea el contador de uso a 0 y
    fija la próxima fecha de reset (1er día del mes siguiente). Se llama al
    activar la suscripción y en cada renovación (invoice.paid)."""
    now = datetime.now(timezone.utc)
    next_reset = (now.replace(day=1) + timedelta(days=32)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    db.table("profiles").update({
        "monthly_usage": 0,
        "usage_reset_at": next_reset.isoformat(),
    }).eq("id", user_id).execute()


# ══════════════════════════════════════════════════════════════════════════════
# PADDLE — checkout client-side (Paddle.js) + webhook server-side. Activo cuando
# PAYMENT_PROVIDER=paddle. Comparte el modelo de planes/créditos con Stripe
# (grant_monthly_allowance, get_profile, credits_cents, extra_brand_slots).
# ══════════════════════════════════════════════════════════════════════════════
def _paddle_api(method: str, path: str, body=None):
    """Llamada a la API de Paddle Billing (server-side, con PADDLE_API_KEY)."""
    try:
        r = requests.request(
            method, PADDLE_API_BASE + path,
            headers={"Authorization": "Bearer " + PADDLE_API_KEY,
                     "Content-Type": "application/json"},
            json=body, timeout=20)
        return r.status_code, (r.json() if r.content else {})
    except Exception as e:
        logger.warning("[paddle] api %s %s failed: %s", method, path, e)
        return 0, {"error": str(e)}


def _whop_api(method: str, path: str, body=None):
    """Llamada a la API de Whop (server-side, con WHOP_API_KEY). Mismo contrato que
    _paddle_api: devuelve (status_code, json|{})."""
    if not WHOP_API_KEY:
        return 0, {"error": "no_api_key"}
    try:
        r = requests.request(
            method, WHOP_API_BASE + path,
            headers={"Authorization": "Bearer " + WHOP_API_KEY,
                     "Content-Type": "application/json"},
            json=body, timeout=20)
        return r.status_code, (r.json() if r.content else {})
    except Exception as e:
        logger.warning("[whop] api %s %s failed: %s", method, path, e)
        return 0, {"error": str(e)}


def _paddle_verify_signature(raw: bytes, sig_header: str) -> bool:
    """Verifica Paddle-Signature: 'ts=<unix>;h1=<hmac_sha256_hex>'.
    HMAC-SHA256 de f'{ts}:{raw}' con PADDLE_WEBHOOK_SECRET."""
    import hmac
    import hashlib
    if not PADDLE_WEBHOOK_SECRET or not sig_header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(";") if "=" in p)
        ts, h1 = parts.get("ts", ""), parts.get("h1", "")
        if not ts or not h1:
            return False
        signed = ts.encode() + b":" + raw
        digest = hmac.new(PADDLE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, h1)
    except Exception:
        return False


def _paddle_event_seen(event_id: str) -> bool:
    """Idempotencia por event id (Redis, TTL 7d). True = ya procesado → skip.
    Si Redis no está, no deduplica (best-effort, como el resto del sistema)."""
    if not event_id or rds is None:
        return False
    try:
        # SET NX devuelve True si la clave es nueva → NO visto aún.
        was_new = rds.set("paddle:evt:" + event_id, "1", nx=True, ex=604800)
        return not bool(was_new)
    except Exception:
        return False


def _paddle_uid(data: dict):
    """Extrae user_id del custom_data del evento (lo pasamos en Checkout.open)."""
    cd = data.get("custom_data") or {}
    return cd.get("user_id") if isinstance(cd, dict) else None


def _paddle_first_price_id(data: dict):
    items = data.get("items") or []
    for it in items:
        pr = it.get("price") or {}
        if pr.get("id"):
            return pr["id"]
    return None


@app.route("/api/billing/config", methods=["GET"])
def billing_config():
    """Config de pago para el front: proveedor activo + (Paddle) token cliente +
    price IDs por plan/ciclo/moneda. El client_token y los price IDs son públicos
    (uso client-side); la API key y el webhook secret NUNCA salen de aquí."""
    prov = payment_provider()
    out = {"provider": prov}
    if prov == "paddle":
        out["paddle"] = {
            "client_token": PADDLE_CLIENT_TOKEN,
            "environment": "sandbox" if "sand" in PADDLE_ENV else "production",
            "prices": {
                "creator": PADDLE_PRICES["creator"],
                "estudio": PADDLE_PRICES["estudio"],
                "agency": PADDLE_PRICES["agency"],
                "addon_brand": PADDLE_PRICE_ADDON_BRAND,
                "topup": {
                    "100": {"EUR": _pp("PADDLE_TOPUP_100_EUR"), "USD": _pp("PADDLE_TOPUP_100_USD")},
                    "300": {"EUR": _pp("PADDLE_TOPUP_300_EUR"), "USD": _pp("PADDLE_TOPUP_300_USD")},
                    "1000": {"EUR": _pp("PADDLE_TOPUP_1000_EUR"), "USD": _pp("PADDLE_TOPUP_1000_USD")},
                },
            },
        }
    elif prov == "whop":
        # Whop: checkout por plan_id (link https://whop.com/checkout/<plan_id>).
        # Los plan IDs son públicos; el front construye el link con metadata user_id.
        out["whop"] = {
            "checkout_base": WHOP_CHECKOUT_BASE,
            "plans": {
                "creator": WHOP_PLANS["creator"],
                "estudio": WHOP_PLANS["estudio"],
                "agency": WHOP_PLANS["agency"],
                "addon_brand": WHOP_PLAN_ADDON_BRAND,
                "topup": {
                    "100": {"EUR": _pp("WHOP_TOPUP_100_EUR"), "USD": _pp("WHOP_TOPUP_100_USD")},
                    "300": {"EUR": _pp("WHOP_TOPUP_300_EUR"), "USD": _pp("WHOP_TOPUP_300_USD")},
                    "1000": {"EUR": _pp("WHOP_TOPUP_1000_EUR"), "USD": _pp("WHOP_TOPUP_1000_USD")},
                    # econ §4: Pack 300 a 29€ del flash. David crea el plan en Whop y pone
                    # estas env; vacío → el front usa el "300" normal (degradado seguro).
                    "300_flash": {"EUR": _pp("WHOP_TOPUP_300_FLASH_EUR"), "USD": _pp("WHOP_TOPUP_300_FLASH_USD")},
                },
            },
        }
    return jsonify(out)


@app.route("/webhooks/paddle", methods=["POST"])
@limiter.exempt
def paddle_webhook():
    """Webhook de Paddle. Verifica firma, deduplica por event id, y mapea los
    eventos a profiles.plan / créditos (mismo modelo que el webhook de Stripe)."""
    if not PADDLE_WEBHOOK_SECRET:
        # Aún sin destination configurado (David lo crea en el dashboard y nos pasa
        # el signing secret). 200 para no acumular reintentos en Paddle.
        logger.info("[paddle] webhook recibido pero PADDLE_WEBHOOK_SECRET no está configurado")
        return "", 200
    raw = request.get_data()
    if not _paddle_verify_signature(raw, request.headers.get("Paddle-Signature", "")):
        logger.warning("[paddle] firma inválida desde %s", get_client_ip())
        return jsonify({"error": "invalid signature"}), 400
    try:
        event = json.loads(raw.decode("utf-8"))
    except Exception:
        return jsonify({"error": "invalid payload"}), 400

    event_id = event.get("event_id") or event.get("notification_id") or ""
    etype = event.get("event_type") or ""
    data = event.get("data") or {}

    if _paddle_event_seen(event_id):
        logger.info("[paddle] evento %s (%s) ya procesado → skip", event_id, etype)
        return "", 200

    try:
        uid = _paddle_uid(data)
        price_id = _paddle_first_price_id(data)
        mapped = PADDLE_PRICE_TO_PLAN.get(price_id or "")

        if etype in ("subscription.created", "subscription.updated"):
            status = (data.get("status") or "").lower()
            sub_id = data.get("id")
            cust_id = data.get("customer_id")
            if not uid:
                logger.warning("[paddle] %s sin user_id (sub=%s)", etype, sub_id)
                return "", 200
            if mapped and mapped["plan"] == "brand_addon":
                # add-on "Marca extra": +1 slot SOLO al crearse (no en updates).
                if etype == "subscription.created" and status in ("active", "trialing"):
                    prof = db.table("profiles").select("extra_brand_slots").eq("id", uid).single().execute()
                    cur = int((prof.data or {}).get("extra_brand_slots") or 0)
                    db.table("profiles").update({"extra_brand_slots": cur + 1}).eq("id", uid).execute()
                    track_event("brand_addon_purchased", uid, {"slots": cur + 1, "provider": "paddle"})
            elif mapped and status in ("active", "trialing"):
                plan = mapped["plan"]
                upd = {"plan": plan}
                # Guardamos los ids de Paddle (best-effort si faltan columnas).
                try:
                    db.table("profiles").update({**upd, "paddle_subscription_id": sub_id,
                                                 "paddle_customer_id": cust_id}).eq("id", uid).execute()
                except Exception:
                    logger.warning("[paddle] cols paddle_* ausentes (¿migración?), set solo plan", exc_info=True)
                    db.table("profiles").update(upd).eq("id", uid).execute()
                grant_monthly_allowance(uid, plan)   # créditos mensuales + limpia trial (plan!=free)
                track_event("subscription_upgraded", uid, {"plan": plan, "price_id": price_id, "provider": "paddle", "amount_cents": _plan_mrr_cents(plan)})
            elif status in ("paused", "canceled"):
                db.table("profiles").update({"plan": "free"}).eq("id", uid).execute()

        elif etype == "subscription.past_due":
            if uid:
                track_event("payment_failed", uid, {"provider": "paddle", "sub": data.get("id")})
                try:
                    from tasks import send_payment_failed_email
                    send_payment_failed_email.delay(uid, data.get("id"), 1)
                except Exception:
                    logger.warning("[paddle] dunning email dispatch failed uid=%s", uid, exc_info=True)

        elif etype == "subscription.canceled":
            sub_id = data.get("id")
            if mapped and mapped["plan"] == "brand_addon":
                if uid:
                    prof = db.table("profiles").select("extra_brand_slots").eq("id", uid).single().execute()
                    cur = int((prof.data or {}).get("extra_brand_slots") or 0)
                    db.table("profiles").update({"extra_brand_slots": max(0, cur - 1)}).eq("id", uid).execute()
            else:
                q = db.table("profiles").update({"plan": "free", "paddle_subscription_id": None})
                try:
                    q.eq("paddle_subscription_id", sub_id).execute()
                except Exception:
                    if uid:
                        db.table("profiles").update({"plan": "free"}).eq("id", uid).execute()
                if uid:
                    track_event("subscription_cancelled", uid, {"provider": "paddle", "reason": "period_end"})

        elif etype == "transaction.completed":
            if not uid:
                logger.warning("[paddle] transaction.completed sin user_id (txn=%s)", data.get("id"))
                return "", 200
            credits = PADDLE_TOPUP_PRICES.get(price_id or "")
            if credits:
                # Topup one-time → suma créditos al saldo (credits_cents).
                amount_cents = credits * get_cost_cents()
                profile = get_profile(uid)
                db.table("profiles").update({
                    "credits_cents": (profile.get("credits_cents") or 0) + amount_cents
                }).eq("id", uid).execute()
                track_event("topup_purchased", uid, {"credits": credits, "provider": "paddle"})
            elif mapped and mapped["plan"] in ("creator", "estudio", "agency"):
                # Pago de suscripción (alta o renovación) → resetea el pool mensual.
                grant_monthly_allowance(uid, mapped["plan"])

    except Exception:
        logger.exception("[paddle] error procesando %s (%s)", etype, event_id)
        # 200: el evento ya quedó marcado como visto; reprocesar no lo arreglaría.
        return "", 200

    return "", 200


# ══════════════════════════════════════════════════════════════════════════════
# WHOP — checkout por link (con metadata user_id) + webhook. Activo si
# PAYMENT_PROVIDER=whop. Mismo modelo de planes/créditos que Paddle/Stripe.
# ══════════════════════════════════════════════════════════════════════════════
def _whop_secret_keys():
    """Derivaciones candidatas de la clave HMAC desde WHOP_WEBHOOK_SECRET ('ws_<…>').
    No conocemos su codificación exacta → probamos base64, hex y cruda."""
    import base64
    part = WHOP_WEBHOOK_SECRET.split("_", 1)[1] if "_" in WHOP_WEBHOOK_SECRET else WHOP_WEBHOOK_SECRET
    keys = []
    for fn in (lambda s: base64.b64decode(s + "=" * (-len(s) % 4)),
               lambda s: bytes.fromhex(s),
               lambda s: s.encode("utf-8"),
               lambda s: WHOP_WEBHOOK_SECRET.encode("utf-8")):
        try:
            k = fn(part)
            if k and k not in keys:
                keys.append(k)
        except Exception:
            pass
    return keys


def _whop_verify_signature(raw: bytes, headers) -> bool:
    """Verifica la firma del webhook de Whop. Whop usa **Svix**: cabecera
    'Webhook-Signature' = 'v1,<base64> [v1,<base64>…]', y se firma
    '<webhook-id>.<webhook-timestamp>.<body>' con HMAC-SHA256 (resultado en base64).
    Probamos las derivaciones de clave de _whop_secret_keys(). Fallback: hex sobre
    el body con X-Whop-Signature (para self-tests)."""
    import hmac
    import hashlib
    import base64
    if not WHOP_WEBHOOK_SECRET:
        return False
    keys = _whop_secret_keys()
    # ── Svix ──
    sig_hdr = (headers.get("Webhook-Signature") or headers.get("Whop-Signature")
               or headers.get("X-Whop-Signature") or "")
    given = [p.split(",", 1)[1] for p in sig_hdr.split() if p.startswith("v1,")]
    msg_id = headers.get("Webhook-Id") or headers.get("Whop-Webhook-Id") or ""
    ts = headers.get("Webhook-Timestamp") or headers.get("Whop-Webhook-Timestamp") or ""
    if given and msg_id and ts:
        signed = (msg_id + "." + ts + ".").encode("utf-8") + raw
        for k in keys:
            exp = base64.b64encode(hmac.new(k, signed, hashlib.sha256).digest()).decode()
            for g in given:
                if hmac.compare_digest(g, exp):
                    return True
    # ── Fallback: hex crudo sobre el body (self-tests) ──
    legacy = headers.get("X-Whop-Signature") or ""
    if legacy and not legacy.startswith("v1,"):
        simple = legacy.split("=", 1)[1].strip() if legacy.startswith("sha256=") else legacy.strip()
        for k in keys:
            if hmac.compare_digest(simple, hmac.new(k, raw, hashlib.sha256).hexdigest()):
                return True
    return False


def _whop_str_id(val):
    """Whop anida el plan/membership a veces como objeto {id:...} y a veces como
    string suelto. Devuelve siempre el string id (o None) para usar como clave."""
    if isinstance(val, dict):
        return val.get("id") or val.get("plan_id") or val.get("plan")
    return val if isinstance(val, str) else None


def _whop_event_seen(key: str) -> bool:
    """Idempotencia por id de evento/entrega (Redis, TTL 7d). True = ya visto."""
    if not key or rds is None:
        return False
    try:
        return not bool(rds.set("whop:evt:" + key, "1", nx=True, ex=604800))
    except Exception:
        return False


def _whop_event_email(data: dict, memb: dict):
    """Email del comprador a partir del evento. Whop a veces lo trae inline
    (membership.email) y a veces solo el id → lo resolvemos por API (el Bot API key
    SÍ lee /memberships/<id>.email y /payments/<id>). Para el fallback de abono."""
    em = (memb.get("email") if isinstance(memb, dict) else None) or data.get("email") or data.get("user_email")
    if em:
        return em
    mid = _whop_str_id(data.get("membership")) or _whop_str_id(memb.get("id") if isinstance(memb, dict) else None)
    if mid:
        code, d = _whop_api("GET", f"/api/v2/memberships/{mid}")
        if code == 200 and isinstance(d, dict) and d.get("email"):
            return d.get("email")
    pid = data.get("id")
    if isinstance(pid, str) and pid.startswith("pay_"):
        code, d = _whop_api("GET", f"/api/v2/payments/{pid}")
        if code == 200 and isinstance(d, dict):
            mid2 = _whop_str_id(d.get("membership"))
            if mid2:
                code2, d2 = _whop_api("GET", f"/api/v2/memberships/{mid2}")
                if code2 == 200 and isinstance(d2, dict) and d2.get("email"):
                    return d2.get("email")
    return None


_WHOP_EMAIL_UID_CACHE: dict = {}
def _whop_uid_by_email(email):
    """email → uid de Supabase (auth.users). Solo en el fallback (metadata ausente),
    así que el escaneo paginado es aceptable. Cachea solo aciertos (un alta posterior
    no queda envenenada por un None)."""
    if not email:
        return None
    key = str(email).strip().lower()
    if not key:
        return None
    if key in _WHOP_EMAIL_UID_CACHE:
        return _WHOP_EMAIL_UID_CACHE[key]
    uid = None
    try:
        for pg in range(1, 11):
            users = db.auth.admin.list_users(page=pg, per_page=200)
            if not users:
                break
            for u in users:
                if (getattr(u, "email", "") or "").lower() == key:
                    uid = getattr(u, "id", None)
                    break
            if uid or len(users) < 200:
                break
    except Exception:
        logger.warning("[whop] lookup email→uid falló", exc_info=True)
    if uid:
        _WHOP_EMAIL_UID_CACHE[key] = uid
    return uid


@app.route("/webhooks/whop", methods=["POST"])
@limiter.exempt
def whop_webhook():
    """Webhook de Whop. Verifica firma, deduplica, y mapea a profiles.plan/créditos.
      membership_activated → plan + créditos · membership_deactivated → free
      cancel_at_period_end_changed → solo registra (acceso sigue hasta fin de periodo)
      payment_succeeded/invoice_paid → topup suma créditos / renovación resetea pool
      payment_failed/invoice_past_due → dunning · refund_created → registra (MVP)."""
    if not WHOP_WEBHOOK_SECRET:
        logger.info("[whop] webhook recibido pero WHOP_WEBHOOK_SECRET no está configurado")
        return "", 200
    raw = request.get_data()
    if not _whop_verify_signature(raw, request.headers):
        logger.warning("[whop] firma inválida desde %s", get_client_ip())
        return jsonify({"error": "invalid signature"}), 400
    try:
        event = json.loads(raw.decode("utf-8"))
    except Exception:
        return jsonify({"error": "invalid payload"}), 400

    action = event.get("action") or event.get("event") or event.get("type") or ""
    data = event.get("data") or {}
    # plan_id y user_id robustos (Whop anida distinto según el evento; el plan
    # puede venir como objeto {id:...} o como string suelto → _whop_str_id).
    _memb = data.get("membership") if isinstance(data.get("membership"), dict) else {}
    plan_id = (_whop_str_id(data.get("plan")) or _whop_str_id(data.get("plan_id"))
               or _whop_str_id(_memb.get("plan")) or _whop_str_id(_memb.get("plan_id")))
    md = data.get("metadata")
    if not (isinstance(md, dict) and md.get("user_id")):
        md = _memb.get("metadata") if isinstance(_memb.get("metadata"), dict) else (md if isinstance(md, dict) else {})
    uid = md.get("user_id") if isinstance(md, dict) else None
    # FALLBACK (metadata vacía): Whop NO captura metadata vía el query-param del link
    # de checkout (whop.com/checkout/<plan>?metadata[user_id]=…) — solo si el checkout
    # se crea como SESSION por API (ver /whop/checkout-session). Hasta que todo migre,
    # resolvemos al comprador por su EMAIL → perfil Supabase. metadata = primario.
    if not uid:
        uid = _whop_uid_by_email(_whop_event_email(data, _memb))
        if uid:
            logger.info("[whop] %s sin metadata.user_id → resuelto por email (uid=%s)", action, uid)
    # id de idempotencia: cabecera de entrega si la hay, si no action+id(+status)
    deliv = request.headers.get("X-Whop-Webhook-Id") or request.headers.get("Whop-Webhook-Id")
    ev_key = deliv or f"{action}:{data.get('id')}:{data.get('status','')}"
    if _whop_event_seen(ev_key):
        logger.info("[whop] evento %s ya procesado → skip", ev_key)
        return "", 200

    mapped = WHOP_PLAN_TO_PLAN.get(plan_id or "")
    try:
        if action == "membership_activated":   # alta / acceso concedido
            if not uid:
                logger.warning("[whop] membership_activated sin metadata.user_id (plan=%s)", plan_id)
                return "", 200
            if mapped and mapped["plan"] == "brand_addon":
                prof = db.table("profiles").select("extra_brand_slots").eq("id", uid).single().execute()
                cur = int((prof.data or {}).get("extra_brand_slots") or 0)
                db.table("profiles").update({"extra_brand_slots": cur + 1}).eq("id", uid).execute()
                track_event("brand_addon_purchased", uid, {"slots": cur + 1, "provider": "whop"})
            elif mapped:
                plan = mapped["plan"]
                # Guardamos el id de membresía en stripe_subscription_id (columna genérica
                # de "sub activa"): habilita la cancelación self-serve por API y hace que
                # paid_features_active reconozca al usuario Whop como pago REAL.
                _upd = {"plan": plan}
                _mid = _whop_str_id(_memb.get("id")) or _whop_str_id(data.get("id"))
                if _mid:
                    _upd["stripe_subscription_id"] = _mid
                db.table("profiles").update(_upd).eq("id", uid).execute()
                grant_monthly_allowance(uid, plan)   # créditos mensuales + limpia trial (plan!=free)
                track_event("subscription_upgraded", uid, {"plan": plan, "plan_id": plan_id, "provider": "whop", "amount_cents": _plan_mrr_cents(plan)})

        elif action == "membership_deactivated":   # baja / fin de acceso → free
            if mapped and mapped["plan"] == "brand_addon":
                if uid:
                    prof = db.table("profiles").select("extra_brand_slots").eq("id", uid).single().execute()
                    cur = int((prof.data or {}).get("extra_brand_slots") or 0)
                    db.table("profiles").update({"extra_brand_slots": max(0, cur - 1)}).eq("id", uid).execute()
            elif uid:
                db.table("profiles").update({"plan": "free"}).eq("id", uid).execute()
                track_event("subscription_cancelled", uid, {"provider": "whop"})

        elif action == "membership_cancel_at_period_end_changed":
            # El acceso SIGUE hasta fin de periodo (la baja real llega vía
            # membership_deactivated) → no se cambia el plan, solo se registra.
            if uid:
                track_event("subscription_cancel_scheduled", uid,
                            {"provider": "whop", "cancel_at_period_end": data.get("cancel_at_period_end")})

        elif action in ("payment_succeeded", "invoice_paid"):
            if not uid:
                logger.warning("[whop] %s sin metadata.user_id (id=%s)", action, data.get("id"))
                return "", 200
            credits = WHOP_TOPUP_PLANS.get(plan_id or "")
            if credits:   # topup → suma créditos
                amount_cents = credits * get_cost_cents()
                profile = get_profile(uid)
                db.table("profiles").update({
                    "credits_cents": (profile.get("credits_cents") or 0) + amount_cents
                }).eq("id", uid).execute()
                track_event("topup_purchased", uid, {"credits": credits, "provider": "whop"})
            elif mapped and mapped["plan"] in ("creator", "estudio", "agency"):
                grant_monthly_allowance(uid, mapped["plan"])   # alta/renovación → resetea pool

        elif action in ("payment_failed", "invoice_past_due"):   # dunning
            if uid:
                track_event("payment_failed", uid, {"provider": "whop", "id": data.get("id")})
                try:
                    from tasks import send_payment_failed_email
                    send_payment_failed_email.delay(uid, data.get("id"), 1)
                except Exception:
                    logger.warning("[whop] dunning email dispatch failed uid=%s", uid, exc_info=True)

        elif action == "refund_created":
            # MVP: se registra; el reverso de créditos/plan no se automatiza aún.
            if uid:
                track_event("refund_created", uid, {"provider": "whop", "id": data.get("id"), "plan_id": plan_id})

        # payment_pending → sin acción (informativo)
    except Exception:
        logger.exception("[whop] error procesando %s (%s)", action, ev_key)
        return "", 200

    return "", 200


@app.route("/whop/checkout-session", methods=["POST"])
@require_auth
@limiter.limit("12 per minute")
def whop_create_checkout_session():
    """Crea un checkout-session de Whop con metadata.user_id atada SERVER-SIDE y devuelve
    su purchase_url. Es la forma fiable de pasar el user_id: el link pelado
    (whop.com/checkout/<plan>?metadata[user_id]=…) NO lo captura — Whop solo persiste la
    metadata si el checkout nace como session por API. Así el webhook payment_succeeded/
    membership_activated trae el user_id y abona a la cuenta correcta (no por email)."""
    if payment_provider() != "whop":
        return jsonify({"error": "whop no activo"}), 400
    user = current_user()
    body = request.get_json(silent=True) or {}
    plan_id = (body.get("plan_id") or "").strip()
    if plan_id not in WHOP_ALL_PLAN_IDS:
        logger.warning("[whop] checkout-session plan desconocido: %s", plan_id[:40])
        return jsonify({"error": "plan no válido"}), 400
    payload = {"plan_id": plan_id, "metadata": {"user_id": user["id"]}}
    code, data = _whop_api("POST", "/api/v2/checkout_sessions", payload)
    url = (data or {}).get("purchase_url") if isinstance(data, dict) else None
    if code not in (200, 201) or not url:
        logger.warning("[whop] checkout-session fallo code=%s data=%s", code, str(data)[:200])
        return jsonify({"error": "no se pudo crear el checkout"}), 502
    return jsonify({"url": url, "session_id": (data or {}).get("id")})


@app.route("/create-subscription-checkout", methods=["POST"])
@limiter.limit("5 per minute;20 per hour")
@require_auth
def create_subscription_checkout():
    if not STRIPE_OK:
        return jsonify({"error": "Pagos no disponibles"}), 503

    body = request.get_json() or {}
    # v0.19: el cliente manda {plan, cycle} y el server resuelve el price desde env
    # (sin price IDs en el cliente). Compat: acepta price_id directo (legacy/upgrade).
    plan_req = body.get("plan", "")
    cycle = body.get("cycle", "month")
    if cycle not in ("month", "year"):
        cycle = "month"
    price_id = body.get("price_id", "")
    if not price_id and plan_req in STRIPE_PRICES:
        price_id = STRIPE_PRICES[plan_req].get(cycle, "")
    if not price_id:
        return jsonify({"error": "Este plan aún no está configurado. Vuelve pronto."}), 400
    if price_id not in PRICE_TO_PLAN:
        return jsonify({"error": "Price ID no válido"}), 400

    currency = body.get("currency", "usd").lower()
    if currency not in ("usd", "eur"):
        currency = "usd"

    user = current_user()
    try:
        checkout_session = stripe_lib.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            currency=currency,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=request.host_url + "?subscribed=true",
            cancel_url=request.host_url + "?sub_cancel=true",
            metadata={"user_id": user["id"], "type": "subscription"},
        )
        return jsonify({"url": checkout_session.url})
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error. Please try again."}), 500


@app.route("/billing/add-brand", methods=["POST"])
@require_auth
def add_brand_addon():
    """Ola Agencia B4: comprar una MARCA EXTRA (add-on +€10/mes) para Agencia
    (base 10). FLAG: David crea el price en Stripe (STRIPE_PRICE_ADDON_BRAND).
    Sin price → degrada limpio ('no configurado'), no rompe. Al completar el pago,
    el webhook incrementa profiles.extra_brand_slots → brands_cap sube +1."""
    if not STRIPE_OK:
        return jsonify({"error": "Pagos no disponibles"}), 503
    user = current_user()
    profile = get_profile(user["id"])
    if profile.get("plan") != "agency":
        return jsonify({"error": "Las marcas extra son del plan Agencia."}), 403
    if not STRIPE_PRICE_ADDON_BRAND:
        # FLAG: price del add-on aún no configurado por David.
        return jsonify({"error": "Las marcas extra aún no están disponibles. Vuelve pronto.",
                        "code": "addon_not_configured"}), 400
    try:
        checkout_session = stripe_lib.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ADDON_BRAND, "quantity": 1}],
            success_url=request.host_url + "profile/settings?addon=brand_ok",
            cancel_url=request.host_url + "profile/settings?addon=cancel",
            metadata={"user_id": user["id"], "type": "addon_brand"},
        )
        return jsonify({"url": checkout_session.url})
    except Exception as e:
        logger.error("add_brand_addon failed user=%s err=%s", user["id"], e, exc_info=True)
        return jsonify({"error": "No se pudo iniciar la compra."}), 500


@app.route("/manage-subscription", methods=["POST"])
@require_auth
def manage_subscription():
    """Portal de gestión de suscripción. Paddle o Stripe según PAYMENT_PROVIDER."""
    if payment_provider() == "paddle":
        user = current_user()
        profile = get_profile(user["id"])
        cust_id = profile.get("paddle_customer_id")
        sub_id = profile.get("paddle_subscription_id")
        if not cust_id:
            return jsonify({"error": "No active subscription"}), 400
        body = {"subscription_ids": [sub_id]} if sub_id else None
        st, d = _paddle_api("POST", f"/customers/{cust_id}/portal-sessions", body or {})
        if st < 300:
            urls = (d.get("data") or {}).get("urls") or {}
            url = ((urls.get("general") or {}).get("overview")) or (urls.get("general") if isinstance(urls.get("general"), str) else None)
            if url:
                return jsonify({"url": url})
        logger.error("[paddle] portal-session fallo %s: %s", st, str(d)[:200])
        return jsonify({"error": "Internal server error. Please try again."}), 500

    # ── Stripe (dormido salvo PAYMENT_PROVIDER=stripe) ──
    if not STRIPE_OK:
        return jsonify({"error": "Payments unavailable"}), 503

    user = current_user()
    profile = get_profile(user["id"])
    sub_id = profile.get("stripe_subscription_id")
    if not sub_id:
        return jsonify({"error": "No active subscription"}), 400

    try:
        sub = stripe_lib.Subscription.retrieve(sub_id)
        portal = stripe_lib.billing_portal.Session.create(
            customer=sub.customer,
            return_url=request.host_url.rstrip("/") + "/profile/settings",
        )
        return jsonify({"url": portal.url})
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error. Please try again."}), 500


@app.route("/cancel-subscription", methods=["POST"])
@require_auth
def cancel_subscription():
    """Cancelación self-serve. Política: acceso hasta el fin del periodo ya pagado,
    sin reembolso de la renovación en curso. Por proveedor:
      - Whop  → API cancel-at-period-end con el id de membresía (stripe_subscription_id);
                si no hay id o la API falla, se devuelve el portal de Whop (self-serve).
      - Stripe→ Subscription.modify(cancel_at_period_end=True)."""
    user = current_user()
    profile = get_profile(user["id"])
    sub_id = profile.get("stripe_subscription_id")
    _msg_ok = ("Suscripción cancelada. Conservas el acceso hasta el final del periodo "
               "que ya pagaste; no se renovará.")

    # ── Whop ──────────────────────────────────────────────────────────────────
    if _active_provider == "whop":
        if sub_id:
            code, _resp = _whop_api("POST", f"/api/v2/memberships/{sub_id}/cancel")
            if 200 <= code < 300:
                track_event("subscription_cancel_requested", user["id"], {
                    "from_plan": profile.get("plan"), "provider": "whop"})
                return jsonify({"ok": True, "mode": "api", "message": _msg_ok})
            logger.warning("[whop] cancel API code=%s sub=%s → portal fallback", code, sub_id)
        # Sin id de membresía o API no disponible → portal de Whop (self-serve real).
        portal = os.environ.get("WHOP_PORTAL_URL", "https://whop.com/orders")
        track_event("subscription_cancel_requested", user["id"], {
            "from_plan": profile.get("plan"), "provider": "whop", "mode": "portal"})
        return jsonify({"ok": True, "mode": "portal", "portal_url": portal,
                        "message": "Gestiona la baja en tu portal de Whop. "
                                   "Conservas el acceso hasta el fin del periodo pagado."})

    # ── Stripe (proveedor por defecto) ─────────────────────────────────────────
    if not STRIPE_OK:
        return jsonify({"error": "Pagos no disponibles"}), 503
    if not sub_id:
        return jsonify({"error": "No tienes suscripción activa"}), 400
    try:
        stripe_lib.Subscription.modify(sub_id, cancel_at_period_end=True)
        # growth-1: solicitud de baja (a fin de periodo). Distinto de la
        # cancelación efectiva (webhook subscription.deleted).
        track_event("subscription_cancel_requested", user["id"], {
            "from_plan": profile.get("plan"),
        })
        return jsonify({"ok": True, "mode": "api", "message": _msg_ok})
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error. Please try again."}), 500


@app.route("/pause-subscription", methods=["POST"])
@require_auth
def pause_subscription():
    """growth-4: cancel-flow con RETENCIÓN. En vez de cancelar, PAUSA el plan
    ~2 meses (Stripe pause_collection): no se cobra, la cuenta baja a Free pero
    CONSERVA todo (voz entrenada, competidores, guiones — el lock-in real) y
    Stripe reanuda el cobro solo (invoice.paid → el webhook restaura el plan).
    El usuario puede reactivar antes con /resume-subscription. El research da
    ~20-25% de churn salvado con la pausa."""
    if not STRIPE_OK:
        return jsonify({"error": "Pagos no disponibles"}), 503
    user = current_user()
    profile = get_profile(user["id"])
    sub_id = profile.get("stripe_subscription_id")
    if not sub_id:
        return jsonify({"error": "No tienes suscripción activa"}), 400

    body = request.get_json() or {}
    try:
        months = int(body.get("months", 2))
    except Exception:
        months = 2
    months = 1 if months < 1 else (3 if months > 3 else months)
    resumes_at = int((datetime.now(timezone.utc) + timedelta(days=30 * months)).timestamp())

    try:
        stripe_lib.Subscription.modify(sub_id, pause_collection={
            "behavior": "void", "resumes_at": resumes_at,
        })
        # Baja a Free durante la pausa (acceso), datos intactos. Al reanudar,
        # invoice.paid restaura el plan vía PRICE_TO_PLAN.
        db.table("profiles").update({"plan": "free"}).eq("id", user["id"]).execute()
        track_event("subscription_paused", user["id"], {
            "from_plan": profile.get("plan"), "months": months,
        })
        _subscription_cache.pop(user["id"], None)   # invalida la caché del estado de sub
        return jsonify({"ok": True, "resumes_at": resumes_at, "months": months})
    except Exception as e:
        logger.error("pause_subscription failed user=%s err=%s", user["id"], e, exc_info=True)
        return jsonify({"error": "No se pudo pausar la suscripción."}), 500


@app.route("/resume-subscription", methods=["POST"])
@require_auth
def resume_subscription():
    """growth-4: reactivar una suscripción pausada antes de tiempo. Quita la
    pausa de Stripe y restaura el plan al instante (sin esperar al próximo cobro)."""
    if not STRIPE_OK:
        return jsonify({"error": "Pagos no disponibles"}), 503
    user = current_user()
    profile = get_profile(user["id"])
    sub_id = profile.get("stripe_subscription_id")
    if not sub_id:
        return jsonify({"error": "No tienes suscripción"}), 400

    try:
        sub = stripe_lib.Subscription.modify(sub_id, pause_collection="",
                                             expand=["items.data.price"])
        # Restaura el plan desde el price de la sub (idéntico a checkout/renovación).
        item = (sub.get("items") or {}).get("data") or []
        price_id = (item[0].get("price") if item else {}).get("id") if item else None
        plan = PRICE_TO_PLAN.get(price_id) or "creator"
        db.table("profiles").update({"plan": plan}).eq("id", user["id"]).execute()
        try:
            grant_monthly_allowance(user["id"], plan)
        except Exception:
            pass
        track_event("subscription_resumed", user["id"], {"plan": plan})
        _subscription_cache.pop(user["id"], None)
        return jsonify({"ok": True, "plan": plan})
    except Exception as e:
        logger.error("resume_subscription failed user=%s err=%s", user["id"], e, exc_info=True)
        return jsonify({"error": "No se pudo reactivar la suscripción."}), 500


# ── Adapt route ───────────────────────────────────────────────────────────────

_JSON_SCRIPT_SCHEMA = (
    'Devuelve ÚNICAMENTE un objeto JSON válido con esta estructura exacta: '
    # B: el título sale del HOOK (lo específico del guion), no de un resumen —
    # los resúmenes de 3-7 palabras degeneraban en genérico ("Éxito no se persigue").
    '{"title": "título sacado del hook: su dato o ángulo específico en 3-8 palabras '
    '(p.ej. si el hook habla de 10 millones de tokens, el título menciona los tokens; '
    'nada de resúmenes temáticos genéricos; sin comillas, sin emojis)", '
    '"hook": "el MEJOR de 3 hooks — 1 línea del tirón que para el scroll", '
    '"alt_hooks": ["2º hook con un DEVICE DISTINTO al del hook", "3er hook con OTRO device distinto"], '
    '"body": ["frase 1 del guión", "frase 2", "..."], '
    '"closing": "DOS cierres separados por  /  (variante 1 / variante 2; p.ej. un comment-CTA y un cierre suave)", '
    # Ítem 10: formato de grabación CLASIFICADO del reel original (por su transcript/
    # caption/duración) — exactamente UNO de estos valores, en minúscula, sin más texto.
    '"recording_format": "uno EXACTO de: selfie | pizarra | podcast | escritorio | broll-vo | pov '
    '(el formato con el que está grabado el reel original y con el que este guion rendiría)", '
    # Si el formato es POV, además el TEXTO EN PANTALLA (overlay) del POV; "" si no aplica.
    '"pov_text": "SOLO si recording_format es pov: el texto que va SOBRESCRITO en pantalla '
    '(corto, frases sueltas separadas por  /  ; el gancho visual del POV). Si no es pov, cadena vacía"}. '
    'Reglas de salida: los 3 hooks (hook + los 2 de alt_hooks) usan 3 devices DISTINTOS. '
    'body = frase a frase (cada elemento del array es UNA frase, se renderiza con ▸). '
    'Sin markdown, sin ```json, sin texto antes ni después. Solo el JSON.'
)

_JSON_HOOKS_SCHEMA = (
    'Devuelve ÚNICAMENTE un objeto JSON válido con esta estructura exacta: '
    '{"hooks": [{"type": "TRANSFORMACIÓN", "text": "hook aquí"}, '
    '{"type": "NEGATIVO", "text": "hook aquí"}, '
    '{"type": "ENEMIGO", "text": "hook aquí"}, '
    '{"type": "CURIOSIDAD", "text": "hook aquí"}, '
    '{"type": "PROMESA", "text": "hook aquí"}]}. '
    'Sin markdown, sin ```json, sin texto antes ni después. Solo el JSON.'
)

# ── MÉTODO del cerebro (GENERADOR.md + METODO.md, cerebro/cerebro-instrucciones) ──
# System prompt base que aplica el método de David. Se antepone al registro/tono.
_METODO_BASE = (
    "Eres el cerebro de guiones de ReelScript. Escribes guiones de reels que SEGMENTAN en "
    "el primer segundo, dispersan el valor con loops, suenan al USUARIO (su voz) y NO suenan "
    "a vendedor ni a IA. Replicas el PORQUÉ de lo que funciona, no plantillas.\n\n"
    "MÉTODO (obligatorio):\n"
    "1. Segmenta en la 1ª frase: que un colectivo concreto se sienta identificado YA (nunca genérico). "
    "Vale 'para mujeres de +40', 'si juegas a Fortnite', 'infoproductor que…'.\n"
    "2. Abre un loop y NO sueltes el valor entero de golpe; dispérsalo y ciérralo al final.\n"
    "3. Conecta con 'pero' / 'por lo tanto', NUNCA con 'y además'. Ramifica, no apiles (regla South Park).\n"
    "4. Rota el DEVICE de apertura entre los 3 hooks: kill-excuse, pregunta, antes-pensaba/hasta-que, "
    "afirmación seca/contrarian, anécdota, enumeración+loop, promesa, newsjack, reflexivo ('profe curioso'). "
    "No repitas molde.\n"
    "5. NO mendigues retención: nada de 'te lo digo al final', 'el último es el mejor', 'aguanta'. "
    "El loop se abre por curiosidad real.\n"
    "6. Mete 1 recurso de concreción: un caso/persona, 'imagínate esto', antes/ahora, meme/actualidad.\n"
    "7. Humaniza con coletillas reales ('si no, luego tenemos un problema', 'y ya está', 'tal cual', "
    "'sin comerte la cabeza'); tacos suaves SOLO si el estilo del usuario lo pide. NADA de metáforas "
    "forzadas tipo 'producción ardiendo'.\n"
    "8. Cuerpo: segmenta → abre loop → valor disperso (pero/por-lo-tanto) → giro/insight (reencuadra: "
    "el problema no era lo obvio) → sentencia memorable (corta, citable, guardable). Frase por frase.\n"
    "9. Cierra según el OBJETIVO: Vender → comment-CTA ('comenta X y te paso…'); Crecer/Educar/Entretener "
    "→ suave o sin CTA. Dosifica el CTA, no en todos.\n"
    "10. Cero AI slop. Si suena a comunicado o a motivacional, reescríbelo.\n"
    "Conserva datos, cifras, nombres y el ÁNGULO concreto del material fuente: un guion que valdría para "
    "cualquier nicho es un guion fallido."
)

# Few-shot: imitar el PATRÓN (cadencia, loops, pero/por-lo-tanto, coletillas), NO copiar.
_FEWSHOT_GUIONES = (
    "\n\n=== EJEMPLOS DE REGISTRO (imita el PATRÓN, NO los copies) ===\n"
    "[Fitness · device kill-excuse]\n"
    "Hook: A los 25 estás fofo por una sola razón.\n"
    "▸ A los 18 te saltabas la cena y ya estabas seco.\n"
    "▸ Pero a los 25 te cambió la vida entera, no el cuerpo.\n"
    "▸ De lunes a viernes comes bien y entrenas; pero el sábado te metes 3.000 calorías en birras.\n"
    "▸ Por lo tanto en dos días borras lo que hiciste en cinco.\n"
    "▸ Te pesas el lunes, no baja, y le echas la culpa al metabolismo.\n"
    "▸ No es el metabolismo. Es que tu finde pesa más que tu semana.\n"
    "Cierre: comenta FINDE y te paso el plan.\n\n"
    "[Marketing · device reflexivo 'profe curioso']\n"
    "Hook: Siempre me ha llamado la atención el Instagram de los negocios locales.\n"
    "▸ Suben cuatro fotos del local y pagan a una agencia para que lo lleve.\n"
    "▸ Pero, ¿hasta qué punto eso funciona de verdad?\n"
    "▸ Imagina un reel que llega a 10.000 personas, la mitad de tu zona.\n"
    "▸ Instagram, por diseño, enseña mucho más los vídeos que las fotos.\n"
    "▸ Bueno, tampoco es que los posts no sirvan.\n"
    "▸ Así que igual el error no es hacer posts; es esperar que hagan el trabajo del vídeo.\n"
    "Cierre: No sé, a mí me cuadra así. ¿Tú cómo lo ves?"
)

# Semilla por nicho (BANCO-nichos-hooks.md): hooks de referencia (fórmula, no copiar).
_NICHE_SEED = {
    "fitness":  ["¿Por qué a los 25 dejas de marcar aunque entrenes más que nunca?",
                 "El cardio en ayunas a los 25 te quema músculo, no grasa."],
    "nutri":    ["¿Comes sano y no bajas de peso? El problema no está en el plato.",
                 "Tu desayuno 'saludable' te tiene con hambre a las 11."],
    "finanz":   ["¿Cobras 2.000 y no llegas a fin de mes? No gastas de más, gastas mal.",
                 "Tu dinero parado en el banco pierde valor cada mes."],
    "marketing":["¿Buen producto y no vendes? El precio no es el problema.",
                 "Bajar precios para vender más te está haciendo vender menos."],
    "negocio":  ["¿Buen producto y no vendes? El precio no es el problema.",
                 "El reel trae a la gente, pero el perfil es el que cierra."],
    "desarrollo":["¿Empiezas hábitos y los dejas en 2 semanas? No es falta de disciplina.",
                  "Si necesitas motivación para empezar, ya empezaste mal."],
    "mentalidad":["Si necesitas motivación para empezar, ya empezaste mal.",
                  "La motivación no viene antes de la acción, viene después."],
    "skincare": ["¿7 productos y la piel sigue mal? Sobran 5.",
                 "Si tu piel tira después de lavarte la cara, lo estás haciendo mal."],
    "belleza":  ["¿7 productos y la piel sigue mal? Sobran 5.",
                 "3 errores que te envejecen la piel, y el 3º lo haces cada mañana."],
}


def _match_niche_seed(niche: str):
    """Devuelve los hooks-semilla del nicho que mejor casa (substring), o None."""
    n = (niche or "").strip().lower()
    if not n:
        return None
    for key, hooks in _NICHE_SEED.items():
        if key in n or n in key:
            return hooks
    return None


def _user_method_context(user_id) -> str:
    """Bloque de contexto del usuario para el generador: nicho + objetivo + hooks-semilla
    del nicho (BANCO). El tono/voz se inyecta aparte (voice_prompt_block)."""
    try:
        prof = get_profile(user_id)
    except Exception:
        return ""
    niche = (prof.get("niche") or "").strip()
    goal = (prof.get("goal") or "").strip()
    subs = prof.get("subniches") or []
    out = ""
    if niche or goal:
        out += "\n\n=== TU CONTEXTO ===\n"
        if niche:
            out += "Nicho: " + niche + (("" if not subs else " (sub: " + ", ".join([str(s) for s in subs[:3]]) + ")")) + "\n"
        if goal:
            out += "Objetivo: " + goal + " — condiciona el ángulo y el CTA según esto.\n"
    seed = _match_niche_seed(niche)
    if seed:
        out += "Hooks-semilla de tu nicho (referencia de FÓRMULA, NO los copies): " + " · ".join(seed) + "\n"
    return out


STYLE_PROMPTS = {

    "viral": (
        "Eres un guionista de reels. Tu trabajo es reescribir este guión para máximo impacto. "
        "Reglas: el hook tiene que parar el scroll en los primeros 3 segundos — sin preámbulo, sin 'hola', sin contexto. "
        "El valor empieza de golpe después del hook, nunca hay transición. "
        "La tensión se mantiene hasta el final desvelando el insight de forma progresiva, nunca de golpe. "
        "Frases de máximo 15 palabras. Cierre contundente que ancla, sin CTA explícito. "
        "Nunca uses: 'increíble', 'brutal', 'chicos', 'os va a flipar', 'en el panorama actual', "
        "'es fundamental entender que', 'descubre cómo', 'cree en ti'. "
        "El resultado tiene que poder leerse frase por frase con viñetas (▸). "
        "Si lo lees en voz alta y no para el scroll en los primeros 3 segundos, reescríbelo. "
        # B: anti-genérico — la especificidad del material fuente es el valor.
        "Regla de especificidad: conserva los datos, cifras, nombres y el ángulo concreto "
        "del material fuente; un guion que podría valer para cualquier nicho es un guion fallido. "
        "Output mínimo: 6-8 frases en body, 100+ palabras totales en el guion. "
        + _JSON_SCRIPT_SCHEMA
    ),

    "divertido": (
        "Eres un guionista de reels. Reescribe este guión con el tono de alguien que cuenta algo en un bar a un colega — sin filtro, sin pose. "
        "Reglas: incluye muletillas naturales donde salgan solas, no forzadas. "
        "Mete al menos un momento de ironía seca o humor que salga de la situación, nunca un chiste preparado. "
        "Si hay un error propio que contar, cuéntalo dentro del desarrollo, nunca al principio. "
        "Las frases incompletas que se corrigen son bienvenidas: 'Es como si... bueno, te lo explico de otra forma.' "
        "Nunca uses entusiasmo artificial, emojis, exclamaciones ni motivacional. "
        "El guión tiene que sonar exactamente igual que un audio de WhatsApp a un colega. "
        "Si lo lees en voz alta y suena raro o artificial, reescríbelo. "
        "Output mínimo: 6-8 frases en body, 100+ palabras totales en el guion. "
        + _JSON_SCRIPT_SCHEMA
    ),

    "linkedin": (
        "Eres un guionista de contenido. Reescribe este guión en formato LinkedIn: tono profesional pero directo, sin distancia. "
        "Primera persona siempre. Una sola idea, desarrollada con lógica clara. "
        "Datos concretos si los hay — ningún dato inventado. "
        "Párrafos de máximo 2-3 líneas con espacio entre ellos. "
        "Sin frases vacías ('en el panorama actual', 'es fundamental', 'cabe destacar', 'valor añadido', 'solución integral'). "
        "Sin motivacional. Cierre que deja una pregunta abierta o una afirmación que genera reacción — nunca una conclusión envuelta en papel de regalo. "
        "El lector tiene que terminar pensando, no sintiéndose inspirado. "
        + _JSON_SCRIPT_SCHEMA
    ),

    "storytelling": (
        "Eres un guionista de reels. Reescribe este guión como una historia real con escena concreta. "
        "Reglas: empieza en el momento exacto donde ocurre algo — no con contexto ni presentación. "
        "Muestra el error o el problema desde dentro: qué pensabas en ese momento, qué hiciste, qué pasó. "
        "El insight tiene que salir de la historia de forma natural, nunca explicado por encima como moraleja. "
        "Tensión narrativa: el lector tiene que querer saber qué pasó después. "
        "Sin 'y esto me enseñó que...', sin conclusiones explícitas, sin motivacional. "
        "El cierre es una frase corta que deja el peso de la historia caer. "
        "Si la historia no genera tensión, no es una historia — es un resumen. Reescríbela. "
        "Output mínimo: 6-8 frases en body, 100+ palabras totales en el guion. "
        + _JSON_SCRIPT_SCHEMA
    ),

    "educacional": (
        "Eres un guionista de reels. Reescribe este guión para ENSEÑAR un concepto concreto y que se entienda a la primera. "
        "Reglas: el hook plantea el problema o promete lo que el viewer va a SABER hacer al final — específico, sin 'hola' ni contexto. "
        "El desarrollo explica paso a paso, cada frase aporta un dato o un porqué, nunca relleno. "
        "Usa un ejemplo concreto del material fuente para aterrizar la idea — nada abstracto. "
        "Frases de máximo 15 palabras, claras, sin jerga innecesaria; si hay un término técnico, se explica al usarlo. "
        "Cierre que fija lo aprendido en una frase memorizable, sin CTA explícito. "
        "Nunca uses: 'es fundamental entender que', 'en el panorama actual', 'descubre cómo', motivacional ni '¿sabías que…?'. "
        "El resultado se lee frase por frase con viñetas (▸). "
        "Regla de especificidad: conserva datos, cifras, nombres y el ángulo concreto del material fuente; un guion que vale para cualquier nicho es fallido. "
        "Output mínimo: 6-8 frases en body, 100+ palabras totales en el guion. "
        + _JSON_SCRIPT_SCHEMA
    ),

    "informativo": (
        "Eres un guionista de reels. Reescribe este guión en tono INFORMATIVO y DIRECTO: máxima densidad de información, cero relleno. "
        "Reglas: el hook es el dato o la conclusión más fuerte, sin rodeos ni preámbulo. "
        "El desarrollo encadena hechos/datos concretos en orden lógico — cada frase es información que el viewer no tenía. "
        "Nada de opinión vacía, hipérbole ni adornos; tono sobrio y seguro, como quien informa de algo que domina. "
        "Frases cortas (máx 15 palabras), afirmativas, sin muletillas ni emojis. "
        "Cierre con el dato o la implicación que el viewer se lleva, sin CTA ni moraleja. "
        "Nunca uses: 'increíble', 'brutal', 'os va a flipar', 'en el panorama actual', 'es fundamental', motivacional. "
        "El resultado se lee frase por frase con viñetas (▸). "
        "Regla de especificidad: conserva datos, cifras, nombres y el ángulo concreto del material fuente; sin ellos no informa. "
        "Output mínimo: 6-8 frases en body, 100+ palabras totales en el guion. "
        + _JSON_SCRIPT_SCHEMA
    ),

    "hooks": (
        "Eres un guionista de reels. Dame exactamente 5 hooks para este guión, uno de cada tipo. "
        "Reglas para todos: tienen que incluir términos específicos del nicho para filtrar a la audiencia correcta desde el primer segundo. "
        "Ningún hook puede dar el valor completo — si el viewer puede llevarse el insight sin ver el vídeo, el hook falla. "
        "Los 5 tipos — "
        "TRANSFORMACIÓN: salto de A a B con dato concreto y creíble. "
        "NEGATIVO: ataca una creencia instalada en el nicho. "
        "ENEMIGO: el error que sigue cometiendo la audiencia. "
        "CURIOSIDAD: abre una puerta sin revelar nada, obliga a seguir para entender. "
        "PROMESA: resultado concreto y específico con condición real. "
        + _JSON_HOOKS_SCHEMA
    ),

}

_JSON_CUSTOM_SUFFIX = (
    " " + _JSON_SCRIPT_SCHEMA
)

CUSTOM_BASE = (
    "Eres un guionista de reels. "
    "Reglas que aplican siempre independientemente de las instrucciones custom: "
    "nunca 'chicos', 'increíble', 'brutal', 'en el panorama actual', 'es fundamental entender que', "
    "'descubre cómo', 'cree en ti', 'todo es posible'. "
    "Nunca empezar con 'Hola', 'En este vídeo' o 'Hoy vamos a hablar de'. "
    "El guión va frase por frase con viñetas (▸). Cada frase máximo 15 palabras. "
    "El valor empieza después del hook sin transición. "
    "Momentos personales van dentro del desarrollo, nunca al principio. "
    "Si lo lees en voz alta y suena a texto escrito, reescríbelo. "
    "Ahora aplica estas instrucciones adicionales:\n"
)

_ASSISTANT_BUILT_IN_LABELS = {
    "viral": "Viral",
    "divertido": "Divertido",
    "hooks": "Hooks",
    "storytelling": "Storytelling",
    "story": "Storytelling",
    "linkedin": "LinkedIn",
    "educacional": "Educacional",
    "informativo": "Informativo",
}

# Tonos PRESET elegibles (personalidad del primer guion cuando aún no hay voz
# personal entrenada). key = STYLE_PROMPTS · label = etiqueta UI. DEFAULT = viral.
PRESET_TONES = [
    {"key": "viral",        "label": "Polémico/Viral"},
    {"key": "educacional",  "label": "Educacional"},
    {"key": "divertido",    "label": "Cercano/Divertido"},
    {"key": "informativo",  "label": "Informativo"},
    {"key": "storytelling", "label": "Storytelling"},
]
PRESET_TONE_KEYS = {t["key"] for t in PRESET_TONES}
DEFAULT_PRESET_TONE = "viral"


def _resolve_assistant_name(payload, user_id, supa):
    """Snapshot legible del asistente para scripts.assistant_name.
    Devuelve label capitalizado ("Viral", "Hooks", nombre custom) o None.
    """
    if not payload:
        return None
    assistant_id = (payload.get("assistant_id") or "").strip() or None
    style = (payload.get("style") or "").strip() or None

    if style and style in _ASSISTANT_BUILT_IN_LABELS:
        return _ASSISTANT_BUILT_IN_LABELS[style]
    if assistant_id and assistant_id in _ASSISTANT_BUILT_IN_LABELS:
        return _ASSISTANT_BUILT_IN_LABELS[assistant_id]
    if assistant_id:
        try:
            r = supa.table("assistants").select("name").eq(
                "id", assistant_id
            ).eq("user_id", user_id).limit(1).execute()
            if r.data and r.data[0].get("name"):
                return r.data[0]["name"]
        except Exception:
            pass
    return None


def _parse_ai_json(raw: str, style: str) -> dict:
    """Parse JSON from LLM response with robust fallback."""
    text = raw.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON object from surrounding text
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                data = None
        else:
            data = None

    if data is None:
        # P0-1: antes degradábamos a {"hook":"","body":[raw]} y el JSON crudo
        # acababa GUARDADO como guion. Mejor fallar: todos los callers de
        # adapt_with_ai capturan la excepción con refund + error limpio
        # (verificado: /adapt, idea_to_script, transcription_to_script,
        # generate_script, batch, steal-batch, explosion, tasks.gen_script).
        app.logger.warning("[adapt] JSON parse failed for style=%s, raw=%s", style, raw[:200])
        raise ValueError("unparseable LLM JSON")

    # Validate structure
    if style == "hooks":
        if "hooks" not in data or not isinstance(data["hooks"], list):
            app.logger.warning("[adapt] Invalid hooks structure for style=%s", style)
            return {"hooks": [{"type": "RESULTADO", "text": raw}]}
    else:
        if "body" in data and isinstance(data["body"], str):
            data["body"] = [data["body"]]
        if "hook" not in data or "body" not in data:
            # P0-1: ídem — JSON válido pero sin la estructura pedida → error
            # limpio en vez de guardar el raw como guion.
            app.logger.warning("[adapt] Missing keys for style=%s, keys=%s", style, list(data.keys()))
            raise ValueError("unparseable LLM JSON")
        if not isinstance(data["body"], list):
            data["body"] = [str(data["body"])]
        data.setdefault("closing", "")
        # Ítem 10: normaliza recording_format al set fijo (o None si no clasifica).
        _fmt = str(data.get("recording_format") or "").strip().lower()
        data["recording_format"] = _fmt if _fmt in REC_FORMAT_KEYS else None
        # POV: el texto en pantalla solo se conserva si el formato es pov.
        _pov = str(data.get("pov_text") or "").strip()
        data["pov_text"] = _pov if (data["recording_format"] == "pov" and _pov) else None

    return data


def _llm_call_provider(provider: str, system: str, user_content: str,
                       temperature: float, max_tokens: int, model: str = None) -> str:
    """Una llamada a UN proveedor (openrouter | groq). Devuelve texto crudo o levanta.
    `model` overridea el modelo de OpenRouter (p.ej. pro para generación); Groq usa el suyo."""
    if provider == "openrouter":
        api_key = OPENROUTER_API_KEY
        url = OPENROUTER_URL
        model = model or OPENROUTER_MODEL
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://reelscript.net", "X-Title": "ReelScript",
        }
        mt = max_tokens
    else:  # groq
        api_key = GROQ_API_KEY
        url = "https://api.groq.com/openai/v1/chat/completions"
        model = "llama-3.3-70b-versatile"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # Groq llama-3.3-70b: ventana 32k → no pedir 20k de salida (revienta el contexto).
        mt = min(max_tokens, 8000)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": mt,
    }
    # P0-1: json-mode. Todos los callers de _call_llm esperan JSON (adapt_with_ai,
    # derive_voice_profile, hook-regen ×3) → evita la prosa/JSON-con-texto que rompía
    # _parse_ai_json. Gemini siempre; Groq/llama TAMBIÉN soporta json_object (probado)
    # pero EXIGE la palabra "json" en los mensajes → guard para no romper la llamada.
    if "gemini" in model.lower():
        payload["response_format"] = {"type": "json_object"}
    elif provider == "groq" and "json" in (system + "\n" + user_content).lower():
        payload["response_format"] = {"type": "json_object"}

    def _do_request(pl):
        resp = requests.post(url, headers=headers, json=pl, timeout=60)
        resp.raise_for_status()
        return resp.json()

    data = _do_request(payload)
    # P0-1: si el modelo cortó por longitud, 1 reintento con el doble de
    # max_tokens — un JSON truncado por length no parsea nunca.
    try:
        finish = data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        finish = None
    if finish == "length":
        app.logger.warning("LLM finish_reason=length provider=%s model=%s — 1 reintento con max_tokens=%s", provider, model, mt * 2)
        data = _do_request(dict(payload, max_tokens=mt * 2))

    # v0.15.7.a: OpenRouter/Gemini puede devolver content=null cuando el modelo
    # emite refusal o cuando system+user no producen salida válida (ej. style
    # 'hooks' + user_content pidiendo guion 30-45s — bug observado en prod).
    # Sin este guard, .strip() reventaba con AttributeError tras 3m52s de
    # retries internos del provider y la UX quedaba 'congelada'.
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        app.logger.warning("LLM response missing expected structure for provider=%s model=%s: %s", provider, model, e)
        raise ValueError("LLM returned malformed response")
    if content is None:
        app.logger.warning("LLM returned empty content for provider=%s model=%s", provider, model)
        raise ValueError("LLM returned empty content")
    return content.strip()


def _call_llm(system: str, user_content: str, temperature: float = 0.8, max_tokens: int = 20000, model: str = None) -> str:
    """Genera con el LLM principal (OpenRouter) y, si falla, CAE A GROQ.

    Motivo (2026-06): OpenRouter se queda sin saldo y las peticiones grandes
    (un guion ~1500 tok) devuelven 402 'requires more credits' → "robar guion"
    fallaba a veces. Con ambos keys, OpenRouter es primario y Groq (llama-3.3-70b)
    es la red de seguridad para que el guion SIEMPRE salga."""
    providers = []
    if OPENROUTER_API_KEY:
        providers.append("openrouter")
    if GROQ_API_KEY:
        providers.append("groq")
    if not providers:
        raise RuntimeError("Sin proveedor LLM configurado (OPENROUTER_API_KEY / GROQ_API_KEY)")

    last_err = None
    for i, prov in enumerate(providers):
        try:
            return _llm_call_provider(prov, system, user_content, temperature, max_tokens, model)
        except Exception as e:
            last_err = e
            has_more = i + 1 < len(providers)
            app.logger.warning(
                "LLM provider=%s falló (%s) — %s",
                prov, e, ("fallback al siguiente proveedor" if has_more else "sin más proveedores"),
            )
    raise last_err


# ══════════════════════════════════════════════════════════════════════════════
#  VOICEPROFILE — el moat: el modelo de voz por marca.
#  - get/save: persistencia en voice_profiles (tolera tabla ausente pre-migración).
#  - derive: extrae la voz de las transcripciones de los reels DEL PROPIO creador.
#  - voice_prompt_block: inyecta esa voz en el system-prompt de generación.
#  Tabla: ver PRE-DEPLOY.md (migración voice_profiles).
# ══════════════════════════════════════════════════════════════════════════════
def get_voice_profile(user_id, brand_id=None):
    """Perfil de voz del usuario/marca → dict o None. Tolera tabla ausente."""
    try:
        # brand_id="" = marca por defecto (NULL rompería el UNIQUE → upsert duplicaría).
        q = db.table("voice_profiles").select("*").eq("user_id", user_id).eq("brand_id", brand_id or "")
        r = q.limit(1).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None


def save_voice_profile(user_id, vp, brand_id=None):
    try:
        db.table("voice_profiles").upsert({
            "user_id": user_id, "brand_id": brand_id or "",
            "tone": vp.get("tone"), "phrases": vp.get("phrases") or [],
            "structure": vp.get("structure"), "avg_duration": vp.get("avg_duration"),
            "avoid": vp.get("avoid"), "confidence": int(vp.get("confidence") or 0),
            "source_count": int(vp.get("source_count") or 0),
            "raw": (vp.get("raw") if isinstance(vp.get("raw"), dict) else vp),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id,brand_id").execute()
        return True
    except Exception as e:
        logger.warning("save_voice_profile failed user=%s err=%s", user_id, e)
        return False


_VOICE_DERIVE_SYS = (
    "Eres un analista de estilo de creadores de contenido corto (reels/TikTok). "
    "Te doy transcripciones de varios reels DEL PROPIO creador. Extrae SU voz. "
    "Responde SOLO un objeto JSON con estas claves: "
    "tone (1 frase), phrases (array de 3-6 expresiones/muletillas LITERALES que usa), "
    "structure (cómo abre → desarrolla → cierra), avg_duration (entero, segundos aprox), "
    "avoid (qué NO suena a él), confidence (0-100: cuán claro es el patrón con esta muestra), "
    "evidence (array de 3 bullets concretos, ej. 'abre con pregunta', 'frases <12 palabras', 'evita tecnicismos')."
)


def derive_voice_profile(transcripts):
    """Deriva un VoiceProfile a partir de las transcripciones de los reels del creador.
    'voz al N%' = confianza del modelo atenuada por tamaño de muestra (cold-start honesto)."""
    texts = [t for t in (transcripts or []) if t and t.strip()]
    if not texts:
        return None
    user = "\n\n---\n\n".join("REEL %d:\n%s" % (i + 1, t[:2000]) for i, t in enumerate(texts))
    try:
        # gemini-2.5-pro es de razonamiento: sus tokens de "thinking" consumen el
        # budget ANTES del JSON. Con 900/1800 el JSON salía truncado (finish_reason=
        # length → no parseable). 4000 deja sitio a razonamiento + el JSON (pequeño).
        raw = _call_llm(_VOICE_DERIVE_SYS, user, temperature=0.4, max_tokens=4000)
    except Exception as e:
        logger.warning("derive_voice_profile LLM failed: %s", e)
        return None
    import json as _json, re as _re
    m = _re.search(r"\{.*\}", raw, _re.S)
    try:
        vp = _json.loads(m.group(0) if m else raw)
    except Exception:
        logger.warning("derive_voice_profile: JSON no parseable")
        return None
    if not isinstance(vp, dict):
        return None
    vp["source_count"] = len(texts)
    base = int(vp.get("confidence") or 0)
    sample_factor = min(1.0, len(texts) / 5.0)          # 5 reels = muestra plena
    vp["confidence"] = max(8, min(95, int(base * (0.5 + 0.5 * sample_factor))))
    return vp


def voice_prompt_block(vp) -> str:
    """Bloque de system-prompt que fuerza al LLM a sonar como el creador (no genérico)."""
    if not vp:
        return ""
    phrases = vp.get("phrases") or []
    if isinstance(phrases, str):
        phrases = [phrases]
    parts = ["\n\n=== VOZ DEL CREADOR (imítala fielmente: el guion debe sonar a ÉL, nada genérico) ==="]
    if vp.get("tone"):         parts.append("Tono: " + str(vp["tone"]))
    if phrases:                parts.append("Usa expresiones suyas como: " + " · ".join(str(p) for p in phrases[:6]))
    if vp.get("structure"):    parts.append("Estructura típica: " + str(vp["structure"]))
    if vp.get("avg_duration"): parts.append("Duración objetivo: ~%ss" % vp["avg_duration"])
    if vp.get("avoid"):        parts.append("EVITA (no suena a él): " + str(vp["avoid"]))
    # Loop de medición (C): lo que MÁS funciona en SU cuenta → priorízalo.
    raw = vp.get("raw") if isinstance(vp.get("raw"), dict) else {}
    ww = raw.get("what_works") or []
    if ww:
        parts.append("LO QUE MÁS FUNCIONA en su cuenta (priorízalo): " + " · ".join(str(w) for w in ww[:4]))
    # SEED (onboarding/auto-derive): así escribe ÉL de verdad — sus propios reels.
    samples = raw.get("samples") or []
    if samples:
        parts.append("\n=== ASÍ ESCRIBE ÉL (transcripciones de sus reels — imita su cadencia y giros, NO copies) ===\n"
                     + "\n--- (otro) ---\n".join(str(s)[:600] for s in samples[:2]))
    # Loop SIN publicar (B): guiones que el usuario VALIDÓ/GRABÓ + hooks que ELIGIÓ.
    vsamp = raw.get("validated_samples") or []
    if vsamp:
        parts.append("\n=== GUIONES QUE ÉL VALIDÓ/GRABÓ (su gusto confirmado — mismo molde) ===\n"
                     + "\n--- (otro) ---\n".join(str(s)[:600] for s in vsamp[:2]))
    phooks = raw.get("preferred_hooks") or []
    if phooks:
        parts.append("HOOKS QUE ÉL ELIGIÓ (su estilo de gancho — replica el patrón): "
                     + " · ".join('“' + str(h)[:90] + '”' for h in phooks[:5]))
    return "\n".join(parts)


def brain_voice_block(user_id, brand_id=None) -> str:
    """Señal del Brain «Entrenar» (Fathom 18/06): hooks que el creador marcó 👍/👎 y
    sus sugerencias ('cómo lo diría él'). Pesa el gusto REAL por encima de lo genérico.
    Tolera que la tabla brain_ratings no exista todavía (devuelve '')."""
    if not user_id:
        return ""
    try:
        q = (db.table("brain_ratings")
               .select("content,rating,suggestion")
               .eq("user_id", user_id))
        if brand_id is not None:
            q = q.eq("brand_id", brand_id or "")
        rows = (q.order("created_at", desc=True).limit(40).execute().data) or []
    except Exception:
        return ""
    liked    = [r["content"] for r in rows if (r.get("rating") or 0) > 0 and r.get("content")][:6]
    disliked = [r["content"] for r in rows if (r.get("rating") or 0) < 0 and r.get("content")][:4]
    sugg     = [r["suggestion"] for r in rows if r.get("suggestion")][:4]
    if not (liked or disliked or sugg):
        return ""
    parts = ["\n\n=== GUSTO DEL CREADOR (Brain «Entrenar» — pesa más que lo genérico) ==="]
    if liked:    parts.append("Hooks que le GUSTAN (imita su tono y estructura): " + " · ".join(liked))
    if disliked: parts.append("Hooks que RECHAZÓ (no van con él, evítalos): " + " · ".join(disliked))
    if sugg:     parts.append("Cómo lo diría ÉL (aplica estas correcciones suyas): " + " · ".join(sugg))
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  LOOP DE MEDICIÓN (C) — el lock-in: reel publicado → guión que lo originó →
#  ¿superó tu media? → realimenta el VoiceProfile (B). El modelo de voz mejora
#  con cada publicación. (El modelo: scripts ya lleva views_count/likes/… del reel
#  publicado en que se convirtió; aquí atribuimos, analizamos y realimentamos.)
# ══════════════════════════════════════════════════════════════════════════════
def _tok(s):
    import re as _re
    return set(_re.findall(r"[a-záéíóúñ0-9]{4,}", (s or "").lower()))


def _reel_transcript_cached(user_id, url):
    """Transcripción del reel ya cacheada en `transcriptions` por (user_id, url).
    Devuelve el texto o None (sin coste). El caché evita re-descargar/re-pagar Groq."""
    if not url:
        return None
    try:
        r = (db.table("transcriptions").select("text")
               .eq("user_id", user_id).eq("url", url).limit(1).execute())
        if r.data and (r.data[0].get("text") or "").strip():
            return r.data[0]["text"]
    except Exception:
        pass
    return None


def _transcribe_reel(user_id, url):
    """Descarga (Apify/yt-dlp) + transcribe (Groq) un reel y lo CACHEA en
    `transcriptions`. COSTE real: 1 descarga + 1 Groq → solo se llama en cache-miss.
    Devuelve el texto o None."""
    import tempfile
    from tasks import download_audio  # lazy import (rompe circular tasks↔app)
    platform = detect_platform(url)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mp3, _thumb, _item = download_audio(url, tmp, platform)
            if not mp3:
                return None
            text = transcribe_with_groq(mp3)
    except Exception as e:
        logger.warning("_transcribe_reel failed url=%s err=%s", url, e)
        return None
    if not (text or "").strip():
        return None
    try:  # cachear para no re-transcribir en próximos refrescos
        db.table("transcriptions").insert({
            "user_id": user_id, "url": url, "platform": platform,
            "text": text, "cost_cents": 0,
        }).execute()
    except Exception as e:
        logger.warning("_transcribe_reel cache insert failed url=%s err=%s", url, e)
    return text


def attribute_reel_to_script(user_id, caption, transcript=None):
    """Atribuye un reel publicado al guión que lo originó. PRIORIZA el CONTENIDO
    HABLADO (transcripción del reel vs cuerpo del guión `scripts.script`, que es lo
    que el creador grabó) — el caption de IG casi nunca es el guión. Caption vs
    título+hook queda solo como respaldo si no hay transcripción. Devuelve
    {script_id, score, method} o None."""
    try:
        r = (db.table("scripts").select("id, title, script, created_at")
               .eq("user_id", user_id).order("created_at", desc=True).limit(60).execute())
    except Exception:
        return None
    rows = r.data or []

    # 1) Señal real: transcripción del reel ↔ cuerpo del guión (Jaccard).
    if transcript and transcript.strip():
        rt = _tok(transcript)
        if rt:
            best, best_score = None, 0.0
            for s in rows:
                sb = _tok(s.get("script") or "")
                if not sb:
                    continue
                score = len(rt & sb) / max(1, len(rt | sb))   # Jaccard
                if score > best_score:
                    best_score, best = score, s
            if best and best_score >= 0.18:   # dos paráfrasis del mismo guion comparten léxico
                return {"script_id": best["id"], "score": round(best_score, 2), "method": "transcript"}

    # 2) Respaldo (señal débil): caption ↔ título+hook. Umbral más alto (era 0.35).
    ct = _tok(caption)
    if ct:
        best, best_score = None, 0.0
        for s in rows:
            _hook_line = ((s.get("script") or "").strip().split("\n")[0])
            st = _tok((s.get("title") or "") + " " + _hook_line)
            if not st:
                continue
            score = len(ct & st) / max(1, len(st))
            if score > best_score:
                best_score, best = score, s
        if best and best_score >= 0.45:
            return {"script_id": best["id"], "score": round(best_score, 2), "method": "caption"}
    return None


def compute_what_works(videos):
    """De los reels publicados, qué supera tu mediana de vistas y por qué (patrón
    de hook/duración/longitud). Devuelve bullets para Métricas + para la voz."""
    vids = [v for v in (videos or []) if (v.get("views") or 0) > 0]
    if len(vids) < 3:
        return []
    vs = sorted(v["views"] for v in vids)
    n = len(vs)
    median = vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2
    winners = [v for v in vids if (v.get("views") or 0) > median]
    if not winners:
        return []
    out = []
    durs = [(v.get("duration_sec") or v.get("duration") or 0) for v in winners]
    durs = [d for d in durs if d]
    if durs:
        out.append("tus reels de ~%ss superan tu media de vistas" % round(sum(durs) / len(durs)))
    q = sum(1 for v in winners if "?" in (v.get("caption") or v.get("hook") or ""))
    if q >= max(2, len(winners) // 2):
        out.append("abrir con pregunta te funciona (%d de tus mejores lo hacen)" % q)
    short = sum(1 for v in winners if len((v.get("caption") or "").split()) <= 12)
    if short >= max(2, len(winners) // 2):
        out.append("los textos cortos (<12 palabras) rinden mejor en tu cuenta")
    out.append("tu mediana son %d vistas; %d reels la superan" % (int(median), len(winners)))
    return out[:5]


def feed_voice_with_metrics(user_id, insights):
    """Realimenta el VoiceProfile (B) con lo que funciona (C). El modelo de voz se
    afina con cada publicación medida → la generación lo prioriza."""
    if not insights:
        return
    vp = get_voice_profile(user_id)
    if not vp:
        return
    raw = vp.get("raw") if isinstance(vp.get("raw"), dict) else {}
    raw["what_works"] = insights
    save_voice_profile(user_id, {
        "tone": vp.get("tone"), "phrases": vp.get("phrases"), "structure": vp.get("structure"),
        "avg_duration": vp.get("avg_duration"), "avoid": vp.get("avoid"),
        "confidence": min(95, (vp.get("confidence") or 0) + 5),  # publicar afina la voz
        "source_count": vp.get("source_count"), "raw": raw,
    })


def refine_voice_from_own_scripts(user_id, brand_id=None):
    """Cierra el loop del Cerebro SIN depender de publicar en IG. Reúsa la señal que el
    usuario ya genera al CREAR/EDITAR/VALIDAR guiones (y elegir hooks) y la guarda en el
    VoiceProfile para que la PRÓXIMA generación la imite. Sin LLM (agregación pura →
    instantáneo y barato). Best-effort: nunca rompe la request.
      - recorded/approved → ejemplos validados (más peso) → raw.validated_samples
      - hook activo de esos guiones + alt_hooks → raw.preferred_hooks (tu estilo de gancho)
      - cada validación sube un poco la confianza (tope 92)."""
    try:
        q = (db.table("scripts")
               .select("script, hook, alt_hooks, recording_status, approval_status, updated_at")
               .eq("user_id", user_id))
        if brand_id:
            q = q.eq("project_id", brand_id)
        rows = (q.order("updated_at", desc=True).limit(40).execute()).data or []
    except Exception:
        return
    # Señal: grabados/aprobados primero; nunca los descartados.
    def _kept(s):
        return s.get("recording_status") == "recorded" or s.get("approval_status") == "approved"
    kept = [s for s in rows if _kept(s) and s.get("recording_status") != "discarded"]
    if not kept:
        # Sin validados aún: usa los editados recientes (no descartados) como señal blanda.
        kept = [s for s in rows if s.get("recording_status") != "discarded"][:6]
    if not kept:
        return
    samples, hooks, seen_h = [], [], set()
    for s in kept:
        body = (s.get("script") or "").strip()
        if body and len(samples) < 3:
            samples.append(body[:700])
        h = (s.get("hook") or (body.split("\n")[0] if body else "")).strip()
        if h and h.lower() not in seen_h:
            seen_h.add(h.lower()); hooks.append(h)
        for a in (s.get("alt_hooks") or []):
            a = str(a).strip()
            if a and a.lower() not in seen_h:
                seen_h.add(a.lower()); hooks.append(a)
    try:
        vp = get_voice_profile(user_id, brand_id) or {}
        raw = vp.get("raw") if isinstance(vp.get("raw"), dict) else {}
        raw["validated_samples"] = samples
        raw["preferred_hooks"] = hooks[:6]
        n_kept = sum(1 for s in rows if _kept(s))
        save_voice_profile(user_id, {
            "tone": vp.get("tone"), "phrases": vp.get("phrases"), "structure": vp.get("structure"),
            "avg_duration": vp.get("avg_duration"), "avoid": vp.get("avoid"),
            "confidence": min(92, max(vp.get("confidence") or 0, 50) + min(20, n_kept * 2)),
            "source_count": vp.get("source_count"), "raw": raw,
        }, brand_id=brand_id)
    except Exception:
        logger.warning("refine_voice_from_own_scripts failed user=%s", user_id, exc_info=True)


REC_FORMAT_KEYS = ("selfie", "pizarra", "podcast", "escritorio", "broll-vo", "pov")


def classify_reel_format(caption, transcript, duration_sec=None):
    """Clasifica el FORMATO de grabación de un reel del pool en uno del set fijo
    (selfie/pizarra/podcast/escritorio/broll-vo) a partir de su transcript/caption/
    duración. Una sola palabra → llamada LLM barata (max_tokens corto). Devuelve la
    clave o None si no clasifica. Pensado para correr en BACKGROUND (no bloquea)."""
    text = ((transcript or "").strip()[:1500]) or ((caption or "").strip()[:500])
    if not text:
        return None
    try:
        dur = "%ss" % int(float(duration_sec)) if duration_sec else "desconocida"
    except (TypeError, ValueError):
        dur = "desconocida"
    system = ("Clasificas el FORMATO de grabación de un reel. Responde SOLO un JSON "
              '{"formato":"<una de: selfie|pizarra|podcast|escritorio|broll-vo|pov>"}. '
              "selfie=persona hablando a cámara (talking head). pizarra=explica escribiendo/"
              "dibujando. podcast=conversación sentada con micrófono a la vista. "
              "escritorio=grabación de pantalla/tutorial digital. broll-vo=imágenes de apoyo "
              "con voz en off, sin presentador a cámara. pov=primera persona/cámara subjetiva "
              "con texto sobreimpreso en pantalla (el clásico «POV: ...»).")
    user = "Duración: %s.\nTranscripción/caption del reel:\n%s" % (dur, text)
    try:
        raw = (_call_llm(system, user, temperature=0, max_tokens=40) or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", raw)
            data = json.loads(m.group()) if m else {}
        v = str((data or {}).get("formato") or "").strip().lower()
        return v if v in REC_FORMAT_KEYS else None
    except Exception:
        logger.warning("classify_reel_format failed", exc_info=True)
        return None


_REEL_TRANSCRIBE_PER_RUN = 6   # tope de transcripciones NUEVAS por refresco (coste Groq+Apify)


def attribute_and_learn(user_id, videos, max_transcribe=_REEL_TRANSCRIBE_PER_RUN):
    """Orquesta el loop al refrescar métricas IG. Para cada reel publicado:
    (1) consigue su TRANSCRIPCIÓN (caché en `transcriptions` por url; si no hay y
        queda presupuesto, descarga+transcribe y la cachea → idempotente, sin re-pagar);
    (2) atribuye por contenido HABLADO (transcripción ↔ cuerpo del guión), caption de
        respaldo; escribe las métricas del reel en su guión;
    (3) calcula qué funciona y realimenta la voz.
    COSTE: solo transcribe reels sin caché, hasta `max_transcribe`."""
    attributed = 0
    transcribed = 0
    for v in (videos or []):
        cap = v.get("caption") or v.get("hook") or ""
        url = v.get("url") or v.get("permalink") or v.get("ig_url") or ""
        transcript = _reel_transcript_cached(user_id, url)
        if transcript is None and url and transcribed < max_transcribe:
            transcript = _transcribe_reel(user_id, url)   # cache-miss → coste real (cacheado)
            if transcript:
                transcribed += 1
        m = attribute_reel_to_script(user_id, cap, transcript=transcript)
        if not m:
            continue
        try:
            db.table("scripts").update({
                "views_count": v.get("views"), "likes": v.get("likes"),
                "comments": v.get("comments"), "saves": v.get("saves"),
            }).eq("id", m["script_id"]).eq("user_id", user_id).execute()
            attributed += 1
        except Exception as e:
            logger.warning("attribute_and_learn: write metrics failed: %s", e)
    insights = compute_what_works(videos)
    feed_voice_with_metrics(user_id, insights)
    return {"attributed": attributed, "transcribed": transcribed, "insights": insights}


def next_series_suggestion(user_id, project_id=None):
    """Para Radar/email diario: tu guión que mejor rinde → 'haz el siguiente de esa serie'.
    Scopeado a la marca activa (project_id) si se pasa — sin cruce entre marcas."""
    try:
        q = (db.table("scripts").select("id, title, views_count")
               .eq("user_id", user_id).not_.is_("views_count", "null"))
        if project_id:
            q = q.eq("project_id", project_id)
        r = q.order("views_count", desc=True).limit(1).execute()
    except Exception:
        return None
    if not r.data:
        return None
    top = r.data[0]
    return {
        "script_id": top["id"], "title": top.get("title"), "views": top.get("views_count"),
        "message": "Lo que grabaste («%s») está rindiendo. ¿Hacemos el siguiente de esa serie, en tu voz?"
                   % (top.get("title") or "tu último guion"),
    }


def top_scripts_for_voice(user_id, n=2, max_chars=700):
    """Tus guiones que MÁS reventaron (mayor views_count) con cuerpo → molde few-shot.
    Truncados para no inflar el prompt (riesgo content=null)."""
    try:
        r = (db.table("scripts").select("script, views_count")
               .eq("user_id", user_id).not_.is_("views_count", "null")
               .order("views_count", desc=True).limit(n * 4).execute())
    except Exception:
        return []
    out = []
    for s in (r.data or []):
        body = (s.get("script") or "").strip()
        if not body:
            continue
        out.append(body[:max_chars])
        if len(out) >= n:
            break
    return out


def underperformers_signal(user_id):
    """Patrón breve de lo que NO te rinde (guiones muy por debajo de tu mediana).
    Solo el PATRÓN (hook), nunca los textos enteros. Devuelve str corto o None."""
    try:
        r = (db.table("scripts").select("script, views_count")
               .eq("user_id", user_id).not_.is_("views_count", "null").limit(80).execute())
    except Exception:
        return None
    rows = [x for x in (r.data or []) if (x.get("views_count") or 0) > 0]
    if len(rows) < 4:
        return None
    vs = sorted(x["views_count"] for x in rows)
    n = len(vs)
    median = vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2
    losers = [x for x in rows if (x.get("views_count") or 0) < 0.4 * median]
    if len(losers) < 2:
        return None
    sig = []
    # El hook es la primera línea del campo `script` (texto plano; no existe columna hook).
    def _hook(row): return (row.get("script") or "").strip().split("\n")[0]
    noq = sum(1 for l in losers if "?" not in _hook(l))
    if noq >= max(2, int(len(losers) * 0.6)):
        sig.append("hooks que no abren con pregunta ni gancho directo")
    lng = sum(1 for l in losers if len(_hook(l).split()) > 14)
    if lng >= max(2, int(len(losers) * 0.5)):
        sig.append("aperturas largas (>14 palabras antes del gancho)")
    return ", ".join(sig) if sig else None


def adapt_with_ai(text: str, style: str, custom_prompt: str = "", voice=None, user_id=None, brand_id=None,
                  model: str = None, temperature: float = 0.8, extra_directive: str = "") -> dict:
    if style == "custom":
        if not custom_prompt:
            raise ValueError("Escribe tus instrucciones en el campo Custom")
        registro = CUSTOM_BASE + custom_prompt + _JSON_CUSTOM_SUFFIX
    else:
        registro = STYLE_PROMPTS.get(style)
        if not registro:
            raise ValueError("Estilo no válido")

    # GENERADOR (cerebro/cerebro-instrucciones): el MÉTODO va primero como base, con el
    # contexto del usuario (nicho/objetivo + semilla de nicho) y few-shot; el registro/tono
    # pedido va después. La voz del usuario se inyecta como CONTEXTO más abajo (ctx).
    method = _METODO_BASE + (_user_method_context(user_id) if user_id else "") + _FEWSHOT_GUIONES
    system = method + "\n\n=== REGISTRO / TONO PEDIDO ===\n" + registro

    # Moat: CONTEXTO de estilo (no cambia el formato de salida). Se reafirma el JSON al final.
    ctx = ""
    if voice:
        ctx += voice_prompt_block(voice)                      # voz + what_works (ya existente)
    if user_id:
        wins = top_scripts_for_voice(user_id)                 # aprendizaje real: tus ganadores
        if wins:
            ctx += ("\n\n=== EJEMPLOS DE TUS GUIONES QUE EXPLOTARON — genera en este MOLDE "
                    "(misma cadencia, estructura y tono; NO los copies literalmente) ===\n"
                    + "\n--- (otro) ---\n".join(wins))
        neg = underperformers_signal(user_id)                 # evita lo que te hunde
        if neg:
            ctx += "\n\nEVITA (no te ha funcionado): " + neg
        ctx += brain_voice_block(user_id, brand_id)           # gusto del Brain «Entrenar» (👍/👎 + sugerencias), por MARCA
    if ctx:
        system = (system + ctx +
                  "\n\nIMPORTANTE: lo anterior es CONTEXTO (voz, ejemplos, método). Responde SOLO "
                  "con el JSON pedido. No copies los ejemplos literalmente: imita el PATRÓN.")
    if extra_directive:
        system += "\n\n" + extra_directive

    raw = _call_llm(system, text, temperature=temperature, model=model)
    return _parse_ai_json(raw, style)


def _shape_script_option(res):
    """De un dict de adapt_with_ai → opción para la UI: hooks (2-3), body, cierre, texto
    plano (lo que se guarda) + título."""
    hooks = [res.get("hook")] + list(res.get("alt_hooks") or [])
    hooks = [str(h).strip() for h in hooks if h and str(h).strip()][:3]
    body = res.get("body") or []
    closing = res.get("closing") or ""
    flat = (str(res.get("hook") or "") + "\n" + "\n".join(str(x) for x in body) +
            ("\n" + closing if closing else "")).strip()
    return {"title": str(res.get("title") or "")[:80], "hooks": hooks,
            "body": [str(x) for x in body], "closing": str(closing), "script": flat}


def _generate_script_options(user_content, style_arg, custom_prompt, uid, brand_id, n=2):
    """Genera N opciones de guion COMPLETAS en PARALELO (modelo de generación = pro),
    reusando método + voz + few-shot. La opción B pide un ángulo/estructura distintos.
    recording_format + pov_text se toman de la opción A. Wall-time ≈ una sola llamada.
    Si B falla, degrada a 1 opción. La voz se lee UNA vez y se comparte (menos DB)."""
    import concurrent.futures
    voice = get_voice_profile(uid, brand_id)
    def _gen(temp, directive):
        return adapt_with_ai(user_content, style_arg, custom_prompt, voice=voice,
                             user_id=uid, brand_id=brand_id, model=GENERATION_MODEL,
                             temperature=temp, extra_directive=directive)
    dirB = ("Esta es la OPCIÓN B (alternativa para que el usuario elija): usa un ÁNGULO de "
            "entrada y una ESTRUCTURA claramente DISTINTOS a una versión estándar — otro tipo "
            "de gancho, otro desarrollo — igual de potente. No repitas la versión obvia.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(2, n)) as ex:
        fA = ex.submit(_gen, 0.8, "")
        fB = ex.submit(_gen, 0.95, dirB) if n >= 2 else None
        a = fA.result()                     # si A falla, propaga → el caller hace refund
        b = None
        if fB is not None:
            try:
                b = fB.result()
            except Exception as e:
                logger.warning("_generate_script_options: opción B falló (degrada a 1): %s", e)
    raw_opts = [a] + ([b] if b else [])
    return raw_opts


@app.route("/save-script", methods=["POST"])
def save_script():
    user = current_user()
    if not user:
        return jsonify({"error": "No autenticado"}), 401
    body = request.get_json() or {}
    style = (body.get("style") or "").strip()
    content = body.get("content") or ""
    assistant_name = (body.get("assistant_name") or "").strip() or None
    if not assistant_name:
        assistant_name = _resolve_assistant_name({"style": style}, user["id"], db)
    today_es = datetime.now(timezone.utc).strftime("%d %b %Y").lower()
    base = "Guión adaptado"
    title = f"{base} · {style} · {today_es}" if style else f"{base} · {today_es}"
    db.table("scripts").insert({
        "user_id": user["id"],
        "title": title,
        "script": content,
        "assistant_name": assistant_name,
    }).execute()
    try:
        refine_voice_from_own_scripts(user["id"])   # loop sin publicar: crear ya afina
    except Exception:
        logger.warning("save_script: refine voice failed", exc_info=True)
    return jsonify({"ok": True})


# ── VoiceProfile API: onboarding (captura del moat) + lectura (Cerebro/voz%) ──
@app.route("/api/voice", methods=["GET"])
@require_auth
def api_voice_get():
    user = current_user()
    vp = get_voice_profile(user["id"])
    if not vp:
        return jsonify({"has_profile": False, "confidence": 0, "source_count": 0})
    raw = vp.get("raw") if isinstance(vp.get("raw"), dict) else {}
    return jsonify({
        "has_profile": True,
        "tone": vp.get("tone"), "phrases": vp.get("phrases") or [],
        "structure": vp.get("structure"), "avg_duration": vp.get("avg_duration"),
        "avoid": vp.get("avoid"),
        "confidence": vp.get("confidence") or 0,        # = "voz al N%"
        "source_count": vp.get("source_count") or 0,
        "evidence": (raw.get("evidence") or []),         # bullets de qué aprendió
    })


@app.route("/api/voice/onboard", methods=["POST"])
@limiter.limit("5 per minute;20 per hour")
@require_auth
def api_voice_onboard():
    """Momento de captura del moat: el creador pega 1-2 reels SUYOS (texto ya
    transcrito por el motor existente) → derivamos y guardamos su VoiceProfile,
    ANTES del primer 'Hazlo mío'. Devuelve evidencia + voz%."""
    if not (OPENROUTER_API_KEY or GROQ_API_KEY):
        return jsonify({"error": "Servicio no disponible"}), 503
    user = current_user()
    body = request.get_json() or {}
    texts = body.get("texts") or []
    if isinstance(texts, str):
        texts = [texts]
    texts = [t for t in texts if isinstance(t, str) and t.strip()]
    if not texts:
        return jsonify({"error": "Pega el texto de al menos 1 reel tuyo."}), 400
    vp = derive_voice_profile(texts[:5])
    if not vp:
        return jsonify({"error": "No pude derivar tu voz. Prueba con otro reel."}), 502
    save_voice_profile(user["id"], vp)
    return jsonify({
        "ok": True,
        "confidence": vp.get("confidence"),
        "source_count": vp.get("source_count"),
        "tone": vp.get("tone"),
        "phrases": vp.get("phrases") or [],
        "evidence": vp.get("evidence") or [],
    })


@app.route("/api/voice/refine", methods=["POST"])
@limiter.limit("5 per minute;20 per hour")
@require_auth
def api_voice_refine():
    """Refina el moat: el creador pega MÁS reels suyos → se ACUMULAN al perfil
    existente (no lo reemplazan) y re-derivamos sobre la muestra completa. Sube
    source_count/confidence → resuelve el '1 reel = 48% para siempre'. Si aún no
    hay perfil, equivale a un onboard."""
    if not (OPENROUTER_API_KEY or GROQ_API_KEY):
        return jsonify({"error": "Servicio no disponible"}), 503
    user = current_user()
    body = request.get_json() or {}
    texts = body.get("texts") or []
    if isinstance(texts, str):
        texts = [texts]
    texts = [t for t in texts if isinstance(t, str) and t.strip()]
    if not texts:
        return jsonify({"error": "Pega el texto de al menos 1 reel tuyo."}), 400

    # Acumular: muestras previas (raw.samples) + las nuevas. Cap a 5 (muestra plena).
    existing = get_voice_profile(user["id"])
    prev_raw = (existing.get("raw") if existing and isinstance(existing.get("raw"), dict) else {})
    prev_samples = [s for s in (prev_raw.get("samples") or []) if isinstance(s, str) and s.strip()]
    all_samples = (prev_samples + texts)[:5]

    vp = derive_voice_profile(all_samples)
    if not vp:
        return jsonify({"error": "No pude refinar tu voz. Prueba con otro reel."}), 502

    # Conservar el aprendizaje del loop (what_works) y dejar traza de la muestra acumulada.
    raw = vp.get("raw") if isinstance(vp.get("raw"), dict) else dict(vp)
    if prev_raw.get("what_works"):
        raw["what_works"] = prev_raw["what_works"]
    raw["samples"] = all_samples
    vp["raw"] = raw

    save_voice_profile(user["id"], vp)
    return jsonify({
        "ok": True,
        "confidence": vp.get("confidence"),
        "source_count": vp.get("source_count"),
        "tone": vp.get("tone"),
        "phrases": vp.get("phrases") or [],
        "evidence": vp.get("evidence") or [],
    })


# ── Voz AUTOMÁTICA desde los reels publicados del propio usuario ─────────────
# Hoy voice_profiles solo se llena si el user pega texto a mano → en prod está
# vacía y el moat apagado. Esto la deriva de sus ig_videos (métricas).
_VOICE_AUTO_SAMPLE = 5   # reels representativos (top views) — mismo cap que onboard/refine


def _voice_auto_candidates(uid):
    """Top ig_videos del user por views (muestra representativa para la voz)."""
    try:
        r = (db.table("ig_videos")
               .select("ig_video_id, ig_url, caption, views, transcription")
               .eq("user_id", uid)
               .order("views", desc=True)
               .limit(_VOICE_AUTO_SAMPLE)
               .execute())
        return r.data or []
    except Exception as e:
        logger.warning("voice_auto candidates failed user=%s err=%s", uid, e)
        return []


@app.route("/api/voice/auto-derive", methods=["GET"])
@require_auth
def api_voice_auto_preview():
    """Preflight SIN coste: cuántos reels propios hay, cuántos ya están
    transcritos y cuántos habría que transcribir (1 uso/crédito cada uno).
    El front lo enseña ANTES de lanzar — no se cobra sin avisar."""
    user = current_user()
    vids = _voice_auto_candidates(user["id"])
    ready = [v for v in vids if (v.get("transcription") or "").strip()]
    need = [v for v in vids if not (v.get("transcription") or "").strip() and v.get("ig_url")]
    return jsonify({
        "available": len(vids) > 0,
        "will_use": len(ready) + len(need),
        "ready": len(ready),
        "need_transcribe": len(need),
        "cost_units": len(need),   # misma unidad que metrics/transcribe-video (1 por reel)
    })


@app.route("/api/voice/auto-derive", methods=["POST"])
@limiter.limit("2 per minute;6 per hour")
@require_auth
def api_voice_auto_derive():
    """Deriva la voz AUTOMÁTICAMENTE de los reels publicados del user:
    1. coge los ~5 con más views (ig_videos),
    2. transcribe los que falten (mismo flujo y cobro que /metrics/transcribe-video),
    3. derive_voice_profile() + save_voice_profile() (por marca via project_id).
    Si la marca por defecto aún no tiene voz, también la guarda ahí (la
    generación lee brand_id='' — así el moat se enciende de inmediato)."""
    if not (OPENROUTER_API_KEY or GROQ_API_KEY):
        return jsonify({"error": "Servicio no disponible"}), 503
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id") or None

    # ASYNC: gemini-2.5-pro (razonamiento) + transcribir reels pueden tardar >60s →
    # Traefik cerraba la conexión a su readTimeout. Encolamos (mismo patrón que /adapt
    # y generate-script). El flujo completo, el cobro por transcripción y la derivación
    # los hace el task; el front hace polling a /task/voice-derive/<task_id>.
    from tasks import voice_auto_derive_task  # noqa: E402
    ar = voice_auto_derive_task.delay(uid, user.get("email", ""), project_id)
    return jsonify({"task_id": ar.id, "status": "queued"}), 202


@app.route("/task/voice-derive/<task_id>", methods=["GET"])
@require_auth
@limiter.limit("60 per minute")
def task_voice_derive_status(task_id: str):
    """Polling de voice_auto_derive_task. En éxito devuelve la MISMA shape que el
    endpoint sync de antes (ok/confidence/source_count/tone/phrases/evidence)."""
    from tasks import voice_auto_derive_task  # noqa: E402
    task = voice_auto_derive_task.AsyncResult(task_id)
    state = task.state
    if state in ("PENDING", "STARTED", "PROGRESS"):
        return jsonify({"state": "pending"})
    if state == "SUCCESS":
        r = task.result or {}
        if not isinstance(r, dict):
            return jsonify({"state": "failed", "error": "bad_result"})
        if r.get("ok"):
            out = {k: v for k, v in r.items() if k != "http"}
            out["state"] = "success"
            return jsonify(out)
        return jsonify({"state": "failed", "error": r.get("error"),
                        "message": r.get("message") or "No pude derivar tu voz. Inténtalo de nuevo."})
    if state == "FAILURE":
        return jsonify({"state": "failed", "error": "task_failure",
                        "message": "Error derivando tu voz. Inténtalo de nuevo."})
    return jsonify({"state": "pending"})


_VOICE_URL_MAX = 6   # máx URLs por entreno (muestra de sobra; controla coste)


def _is_reel_url(url: str) -> bool:
    """True si la URL apunta a un reel/vídeo concreto (no a un perfil)."""
    u = (url or "").lower()
    if detect_platform(url) == "otro":
        return False
    if "tiktok.com" in u:
        return ("/video/" in u) or ("vm.tiktok" in u) or ("vt.tiktok" in u)
    # instagram: reel/post/tv concreto (no el perfil suelto)
    return ("/reel/" in u) or ("/reels/" in u) or ("/p/" in u) or ("/tv/" in u)


def _parse_voice_urls(blob):
    """Extrae URLs de reel válidas de un texto/array. Devuelve (validas, perfil_detectado)."""
    import re as _re
    if isinstance(blob, list):
        cand = blob
    else:
        cand = _re.findall(r"https?://\S+", str(blob or ""))
    out, seen, had_profile = [], set(), False
    for raw in cand:
        url = raw.strip().strip('",)')
        if not url or url in seen:
            continue
        if _is_reel_url(url):
            seen.add(url)
            out.append(url)
        elif detect_platform(url) != "otro":
            had_profile = True   # IG/TikTok pero perfil, no reel
        if len(out) >= _VOICE_URL_MAX:
            break
    return out, had_profile


@app.route("/api/voice/from-urls", methods=["POST"])
@limiter.limit("3 per minute;10 per hour")
@require_auth
def api_voice_from_urls():
    """Bloque A3: entrenar la voz pegando URLs de TUS reels. Transcribe cada uno
    (reusa _transcribe_reel — cobra como una transcripción normal; el front avisa
    del coste antes) y deriva la voz con derive_voice_profile. Nada de pegar
    transcripciones a mano."""
    if not (OPENROUTER_API_KEY or GROQ_API_KEY) or not GROQ_API_KEY:
        return jsonify({"error": "Servicio no disponible"}), 503
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id") or None
    urls, had_profile = _parse_voice_urls(body.get("urls") if body.get("urls") is not None else body.get("text"))
    if not urls:
        msg = ("Pega URLs de TUS reels concretos (no el perfil). O conecta tu Instagram en Métricas y los leo solos."
               if had_profile else
               "Pega 1-5 URLs de reels tuyos (Instagram/TikTok) para aprender tu voz.")
        return jsonify({"error": "no_urls", "message": msg}), 400

    is_unlimited = user.get("email", "").lower() in UNLIMITED_EMAILS
    texts, charged = [], 0
    for url in urls:
        cached = _reel_transcript_cached(uid, url)
        if cached:
            texts.append(cached)
            continue
        # econ: primeros VOICE_FREE_REELS reels de voz GRATIS (de por vida), luego
        # VOICE_REEL_UNITS/reel. Sin presupuesto para el siguiente → deriva con lo que haya.
        reel_units = 0
        if not is_unlimited and _voice_reels_used(uid) >= VOICE_FREE_REELS:
            reel_units = VOICE_REEL_UNITS
            if credits_available(get_profile(uid)) < reel_units:
                break
        txt = _transcribe_reel(uid, url)
        if not (txt or "").strip():
            continue
        if reel_units and not is_unlimited:
            _err, _r, _ = _charge_units_locked(uid, reel_units, user)
            if _err:
                break
        if not is_unlimited:
            _voice_mark_reel(uid)
        charged += 1
        texts.append(txt)

    if not texts:
        return jsonify({"error": "no_transcripts",
                        "message": "No pude transcribir esos reels (privados o no disponibles). Prueba con otros."}), 502

    vp = derive_voice_profile(texts[:_VOICE_URL_MAX])
    if not vp:
        return jsonify({"error": "derive_failed",
                        "message": "No pude derivar tu voz con esta muestra. Prueba con más reels."}), 502
    raw = vp.get("raw") if isinstance(vp.get("raw"), dict) else dict(vp)
    raw["samples"] = texts[:_VOICE_URL_MAX]
    raw["from_urls"] = True
    vp["raw"] = raw
    save_voice_profile(uid, vp, brand_id=project_id)
    if project_id and not get_voice_profile(uid):
        save_voice_profile(uid, vp)
    return jsonify({
        "ok": True, "confidence": vp.get("confidence"),
        "source_count": len(texts[:_VOICE_URL_MAX]), "transcribed_now": charged,
        "tone": vp.get("tone"), "phrases": vp.get("phrases") or [],
        "evidence": vp.get("evidence") or [],
    })


@app.route("/adapt", methods=["POST"])
@limiter.limit("20 per minute;100 per hour")
def adapt():
    body          = request.get_json() or {}
    text          = (body.get("text") or "").strip()
    style         = (body.get("style") or "").strip()
    custom_prompt = (body.get("custom_prompt") or "").strip()
    assistant_id  = (body.get("assistant_id") or "").strip()

    err = validate_adapt(body)
    if err:
        return jsonify({"error": err}), 400
    # P0-5: /adapt es texto-LLM (OpenRouter O Groq sirven) — exigir GROQ_API_KEY
    # tumbaba el endpoint en despliegues solo-OpenRouter. (Los endpoints de
    # transcripción SÍ requieren GROQ: Whisper vive ahí.)
    if not (OPENROUTER_API_KEY or GROQ_API_KEY):
        return jsonify({"error": "Service unavailable"}), 500
    if not style and not custom_prompt and not assistant_id:
        return jsonify({"error": "Select a style"}), 400

    user = current_user()

    # v0.14.30 D.1: filtrar por user_id para evitar que un user use asistentes ajenos.
    # Antes (buggy): SELECT instructions WHERE id = assistant_id (sin user_id).
    if assistant_id:
        if not user:
            return jsonify({"error": "Auth required"}), 401
        ast_result = db.table("assistants").select("instructions").eq(
            "id", assistant_id
        ).eq("user_id", user["id"]).execute()
        if ast_result.data:
            style = "custom"
            custom_prompt = ast_result.data[0]["instructions"]
        else:
            return jsonify({"error": "Assistant not found"}), 404
    cost_cents = 0
    charge_mode = "none"   # none | monthly | credits — qué cobrará el task tras generar

    # ── Comprobar límites / saldo (adapt usa free_adapt_used_today) ──────────
    if user and user.get("email", "").lower() in UNLIMITED_EMAILS:
        pass  # sin límite ni coste para cuentas admin
    elif user is None:
        return jsonify({
            "error": "Regístrate gratis para usar Hazlo tuyo."
        }), 429
    else:
        profile = get_profile(user["id"])
        if paid_features_active(profile, user):   # plan de pago REAL (no fantasma)
            ok, err_msg = check_monthly_limit(profile)
            if not ok:
                return jsonify({"error": err_msg}), 429
            cost_cents = 0
            charge_mode = "monthly"
        elif profile["credits_cents"] >= SCRIPT_UNITS * COST_CENTS:
            cost_cents = SCRIPT_UNITS * COST_CENTS   # econ: 3 créditos por guión
            charge_mode = "credits"
        else:
            # A3: «Hazlo tuyo» es de pago. Free sin créditos → muro de planes.
            # growth-1: paywall_shown. after_first_value=False — este muro bloquea
            # ANTES de cualquier prueba (no hay cata de adapt). Lo marcamos para
            # poder medir si conviene dar 1 adapt gratis (research: el paywall
            # convierte mejor DESPUÉS del primer éxito).
            track_event("paywall_shown", user["id"], {
                "wall": "adapt", "plan": profile.get("plan", "free"),
                "after_first_value": False,
            })
            return jsonify({
                "error": "free_limit_reached",
                "message": "«Hazlo tuyo» es una función de pago. Sube a Creador o recarga créditos."
            }), 402

    # ── ASYNC: Gemini 2.5 Pro tarda >60s en guiones largos; el request sync cruzaba
    # el readTimeout (~60s) de Traefik → 502. Encolamos como el resto de generación
    # (espejo de /api/.../generate-script → /task/script/<id>). El gating ya se hizo
    # arriba (fast-reject); el COBRO lo hace el task tras éxito (no aquí) → sin doble
    # cobro entre polls. Frontend hace polling a /task/adapt/<task_id>.
    from tasks import adapt_task  # noqa: E402
    ar = adapt_task.delay(text, style, custom_prompt,
                          user["id"] if user else None, charge_mode, cost_cents)
    return jsonify({"task_id": ar.id, "status": "queued"}), 202


def _extract_hook(raw: str) -> str:
    """Saca el texto del hook de la respuesta del LLM ({"hook": "..."}), tolerando
    fences markdown. Si no se puede parsear, devuelve "" (NO vuelca el JSON crudo a
    la UI). Reemplaza el uso erroneo de _parse_ai_json(style="hook_regen"), que exigia
    'body' y descartaba el hook bueno."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("hook"), str):
            return data["hook"].strip()
    except Exception:
        pass
    m = re.search(r'"hook"\s*:\s*"([^"]+)"', text)
    if m:
        return m.group(1).strip()
    return ""


_HOOK_REGEN_PROMPT = (
    "Eres un guionista de reels. Se te da un guión ya escrito (body + closing). "
    "Tu trabajo es escribir UN SOLO hook alternativo para este guión. "
    "El hook tiene que parar el scroll en los primeros 3 segundos — sin preámbulo, sin 'hola', sin contexto. "
    "Nunca uses: 'increíble', 'brutal', 'chicos', 'os va a flipar'. "
    "Devuelve ÚNICAMENTE un objeto JSON: {\"hook\": \"tu nuevo hook aquí\"}. "
    "Sin markdown, sin ```json, sin texto antes ni después. Solo el JSON."
)


@app.route("/transform-hook", methods=["POST"])
def transform_hook():
    """Regenerate only the hook, keeping body and closing intact."""
    data = request.get_json() or {}
    original_text = data.get("text", "").strip()
    body = data.get("body", [])
    closing = data.get("closing", "")

    if not original_text and not body:
        return jsonify({"error": "No text provided"}), 400

    context = "\n".join(body) + ("\n" + closing if closing else "")
    user_msg = f"Guión actual:\n{context}\n\nTexto original del que salió:\n{original_text}"
    # P1 idioma de salida: el hook regenerado sale en el idioma del usuario.
    _u = current_user()
    _out_lang = (data.get("language") or (get_profile(_u["id"]).get("lang") if _u else None) or "es")
    user_msg += _out_lang_instruction(_out_lang)

    try:
        raw = _call_llm(_HOOK_REGEN_PROMPT, user_msg, temperature=0.9)
        new_hook = _extract_hook(raw) or original_text
    except Exception as e:
        logger.error(f"Hook regen failed: {e}", exc_info=True)
        return jsonify({"error": "Failed to regenerate hook"}), 502

    return jsonify({"hook": new_hook})


# ── Assistants ────────────────────────────────────────────────────────────────


@app.route("/assistants", methods=["GET"])
@require_auth
def list_assistants():
    user = current_user()
    rows = db.table("assistants").select("*").eq("user_id", user["id"]).order("created_at").execute()
    return jsonify(rows.data)


@app.route("/assistants", methods=["POST"])
@require_auth
def create_assistant():
    user = current_user()
    body = request.get_json()
    name = (body.get("name") or "").strip()
    instructions = (body.get("instructions") or "").strip()

    if not name or not instructions:
        return jsonify({"error": "Name and instructions required"}), 400

    profile = get_profile(user["id"])
    plan = profile.get("plan", "free")
    limit = ASSISTANT_LIMITS.get(plan)

    if plan == "free":
        return jsonify({"error": "Upgrade your plan to create assistants"}), 403

    if limit is not None:
        count = db.table("assistants").select("id", count="exact").eq("user_id", user["id"]).execute()
        current = count.count if hasattr(count, "count") else len(count.data)
        if current >= limit:
            return jsonify({"error": f"Your plan allows up to {limit} assistant(s)"}), 403

    row = db.table("assistants").insert({
        "user_id": user["id"],
        "name": name,
        "instructions": instructions,
    }).execute()
    return jsonify(row.data[0] if row.data else {"ok": True})


@app.route("/assistants/<assistant_id>", methods=["PUT"])
@require_auth
def update_assistant(assistant_id):
    user = current_user()
    body = request.get_json()
    updates = {}
    if "name" in body:
        updates["name"] = body["name"]
    if "instructions" in body:
        updates["instructions"] = body["instructions"]
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    db.table("assistants").update(updates).eq("id", assistant_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


@app.route("/assistants/<assistant_id>", methods=["DELETE"])
@require_auth
def delete_assistant(assistant_id):
    user = current_user()
    db.table("assistants").delete().eq("id", assistant_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


# ── Scripts & Projects (pro/agency) ──────────────────────────────────────────

@app.route("/scripts", methods=["GET"])
@require_auth
def list_scripts():
    user = current_user()
    project_id = request.args.get("project_id")
    q = db.table("scripts").select("*").eq("user_id", user["id"])
    if project_id:
        q = q.eq("project_id", project_id)
    rows = q.order("created_at", desc=True).limit(100).execute()
    return jsonify(rows.data)


@app.route("/scripts/<script_id>/source", methods=["GET"])
@require_auth
def script_source(script_id):
    """Miniatura + URL del reel FUENTE de un guión (para el editor de guion), resuelto
    BAJO DEMANDA vía las FK que `scripts` ya guarda — sin columnas nuevas ni bloat en
    /scripts: from_competitor_reel_id → creator_reels_global (thumb_b64 + ig_reel_id→url),
    o transcription_id → transcriptions (thumbnail_b64 + url)."""
    user = current_user()
    sc_r = (db.table("scripts")
              .select("id, from_competitor_reel_id, transcription_id")
              .eq("id", script_id).eq("user_id", user["id"]).limit(1).execute())
    if not sc_r.data:
        return jsonify({"error": "not_found"}), 404
    sc = sc_r.data[0]
    url = thumb = None
    rid = sc.get("from_competitor_reel_id")
    tid = sc.get("transcription_id")
    try:
        if rid:
            rr = (db.table("creator_reels_global").select("ig_reel_id, thumb_b64")
                    .eq("id", rid).limit(1).execute())
            if rr.data:
                x = rr.data[0]
                thumb = x.get("thumb_b64")
                if x.get("ig_reel_id"):
                    url = "https://www.instagram.com/reel/%s/" % x["ig_reel_id"]
        elif tid:
            tt = (db.table("transcriptions").select("url, thumbnail_b64")
                    .eq("id", tid).eq("user_id", user["id"]).limit(1).execute())
            if tt.data:
                x = tt.data[0]
                url = x.get("url")
                thumb = x.get("thumbnail_b64")
    except Exception:
        logger.warning("script_source lookup failed sid=%s", script_id, exc_info=True)
    return jsonify({"url": url, "thumb_b64": thumb})


@app.route("/scripts", methods=["POST"])
@require_auth
def create_script():
    user = current_user()
    body = request.get_json()
    row = db.table("scripts").insert({
        "user_id": user["id"],
        "title": body.get("title", "Sin título"),
        "transcription": body.get("transcription"),
        "script": body.get("script"),
        "reel_url": body.get("reel_url"),
        "project_id": body.get("project_id"),
    }).execute()
    return jsonify(row.data[0] if row.data else {"ok": True})


@app.route("/scripts/<script_id>", methods=["DELETE"])
@require_auth
def delete_script(script_id):
    user = current_user()
    db.table("scripts").delete().eq("id", script_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


_HEX_COLOR_RX = re.compile(r"^#[0-9a-f]{6}$", re.IGNORECASE)


def _validate_project_color(value):
    """Return (ok, normalized) where normalized is lowercase hex or None."""
    if value is None or value == "":
        return True, None
    if not isinstance(value, str) or not _HEX_COLOR_RX.match(value):
        return False, None
    return True, value.lower()


@app.route("/projects", methods=["GET"])
@require_auth
def list_projects():
    user = current_user()
    rows = db.table("projects").select("*").eq("user_id", user["id"]).order("created_at", desc=True).execute()
    items = rows.data or []
    for p in items:
        try:
            sc = db.table("scripts").select("id", count="exact").eq("project_id", p["id"]).execute()
            p["scripts_count"] = sc.count if hasattr(sc, "count") and sc.count is not None else 0
        except Exception:
            p["scripts_count"] = 0
        try:
            ic = db.table("ideas").select("id", count="exact").eq("project_id", p["id"]).execute()
            p["ideas_count"] = ic.count if hasattr(ic, "count") and ic.count is not None else 0
        except Exception:
            p["ideas_count"] = 0
    return jsonify(items)


def _slugify_handle(name):
    """Deriva un handle estilo @ a partir del nombre de la marca (a-z0-9), o '' si vacío."""
    if not name or not isinstance(name, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def workspace_owner_id(user_id: str) -> str:
    """Ola Agencia B2: si el usuario es MIEMBRO activo de una agencia, su
    'workspace' es el del owner (ve y opera sobre las marcas del owner). Si no,
    su propio id. Best-effort: ante cualquier fallo, devuelve su propio id."""
    try:
        r = (db.table("agency_members").select("agency_owner_id")
               .eq("member_id", user_id).eq("status", "active").limit(1).execute())
        if r.data and r.data[0].get("agency_owner_id"):
            return r.data[0]["agency_owner_id"]
    except Exception:
        pass
    return user_id


@app.route("/api/brands", methods=["GET"])
@require_auth
def api_brands():
    """Marcas del usuario para el loop (Portfolio/multi-marca de radar-loop.js).
    Las "marcas" son la tabla projects. Devuelve {brands:[{id,name,handle,color,
    level,voice,reelsAnalyzed,scripts}, ...]} con la shape EXACTA que consume el JS.
    Agency: projects donde agency_owner_id==user O user_id==user. Resto: sus projects.
    Sin projects → una marca derivada del profile (id:'default')."""
    user = current_user()
    uid = user["id"]
    # B2: si es MIEMBRO de una agencia, opera en el workspace del owner.
    owner_uid = workspace_owner_id(uid)
    is_member = owner_uid != uid
    eff_uid = owner_uid
    profile = get_profile(eff_uid)
    plan = (profile or {}).get("plan", "free")

    projects = []
    try:
        if plan == "agency":
            owned = db.table("projects").select("*").eq("user_id", eff_uid).execute().data or []
            managed = db.table("projects").select("*").eq("agency_owner_id", eff_uid).execute().data or []
            seen = set()
            for p in owned + managed:
                if p.get("id") and p["id"] not in seen:
                    seen.add(p["id"])
                    projects.append(p)
        else:
            projects = db.table("projects").select("*").eq("user_id", eff_uid).execute().data or []
    except Exception as e:
        logger.warning("api_brands query failed user=%s err=%s", uid, e)
        projects = []

    # Voz del workspace: confidence del VoiceProfile (marca por defecto) si existe, si no 40.
    vp = get_voice_profile(eff_uid)
    voice = int(vp.get("confidence") or 0) if vp else 40
    if voice <= 0:
        voice = 40

    def _scripts_count(project_id):
        try:
            sc = db.table("scripts").select("id", count="exact").eq("project_id", project_id).execute()
            return sc.count if hasattr(sc, "count") and sc.count is not None else 0
        except Exception:
            return 0

    brands = []
    for p in projects:
        name = p.get("name") or "Mi marca"
        brands.append({
            "id": p.get("id"),
            "name": name,
            "handle": _slugify_handle(name),
            "color": p.get("color") or "#4f7cff",
            "level": 1,
            "voice": voice,
            "reelsAnalyzed": 0,
            "scripts": _scripts_count(p.get("id")),
        })

    if not brands:
        default_name = (user.get("email") or "").split("@")[0] or "Mi marca"
        brands = [{
            "id": "default",
            "name": default_name,
            "handle": _slugify_handle(default_name),
            "color": "#4f7cff",
            "level": 1,
            "voice": voice,
            "reelsAnalyzed": 0,
            "scripts": 0,
        }]

    # cap de marcas + plan → la isla muestra "N/cap" y gatea "+ Nueva marca".
    _cap = brands_cap(profile)
    return jsonify({"brands": brands, "plan": plan, "brands_cap": _cap})


def get_extra_brand_slots(user_id: str) -> int:
    """Marcas extra compradas (add-on Agencia +10€/marca). Block-4 cablea la
    compra en Stripe; por ahora 0 (sin price ID → degrada limpio)."""
    try:
        r = (db.table("profiles").select("extra_brand_slots")
               .eq("id", user_id).single().execute())
        return int((r.data or {}).get("extra_brand_slots") or 0)
    except Exception:
        return 0


def brands_cap(profile: dict) -> int | None:
    """Tope de marcas (projects) por plan. None = sin tope.
    Estudio 3 · Agencia 10 (+ extras comprados) · free 1 · creator/pro sin tope (no se toca)."""
    plan = profile.get("plan", "free")
    if plan == "agency":
        return 10 + get_extra_brand_slots(profile.get("id"))
    if plan == "estudio":
        return 3
    return PLANS.get(plan, PLANS["free"]).get("projects_max")


@app.route("/projects", methods=["POST"])
@require_auth
def create_project():
    user = current_user()
    profile = get_profile(user["id"])
    max_proj = brands_cap(profile)
    if max_proj is not None:
        count = db.table("projects").select("id", count="exact").eq("user_id", user["id"]).execute()
        current = count.count if hasattr(count, "count") else len(count.data)
        if current >= max_proj:
            return jsonify({"error": f"Has alcanzado el límite de {max_proj} marca(s) de tu plan. Sube de plan o añade una marca extra.",
                            "limit": max_proj, "code": "brand_limit"}), 403
    body = request.get_json() or {}
    ok_color, norm_color = _validate_project_color(body.get("color"))
    if not ok_color:
        return jsonify({"error": "Invalid color (expected hex #rrggbb)"}), 400
    payload = {
        "user_id": user["id"],
        "name": body.get("name", "Sin nombre"),
        "style_prompt": body.get("style_prompt", ""),
        "color": norm_color,
        # Nicho EXPLÍCITO de la marca (fijado al crear) → «Sugerencias de hoy» on-niche.
        "niche": ((body.get("niche") or "").strip()[:80] or None),
        "subniches": [t for t in (_norm_tag(s) for s in (body.get("subniches") or [])) if t][:8],
    }
    row = db.table("projects").insert(payload).execute()
    return jsonify(row.data[0] if row.data else {"ok": True})


@app.route("/projects/<project_id>", methods=["DELETE"])
@require_auth
def delete_project(project_id):
    user = current_user()
    db.table("projects").delete().eq("id", project_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


@app.route("/projects/<project_id>/assistant", methods=["PUT"])
@require_auth
def assign_assistant_to_project(project_id):
    user = current_user()
    body = request.get_json()
    assistant_id = body.get("assistant_id") or None
    db.table("projects").update({"assistant_id": assistant_id}).eq("id", project_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


# ── Avatar ────────────────────────────────────────────────────────────────────

@app.route("/profile/avatar", methods=["POST"])
@require_auth
def update_avatar():
    user = current_user()
    body = request.get_json()
    seed = body.get("seed", "default")
    allowed = {"shadow","reel","script","pixel","ninja","ghost","robot","alien","wizard","punk","hacker","glitch"}
    if seed not in allowed:
        return jsonify({"error": "Invalid seed"}), 400
    db.table("profiles").update({"avatar_seed": seed}).eq("id", user["id"]).execute()
    return jsonify({"ok": True, "avatar_seed": seed})


# ── Projects (PATCH) ─────────────────────────────────────────────────────────

@app.route("/projects/<project_id>", methods=["PATCH"])
@require_auth
def update_project(project_id):
    user = current_user()
    body = request.get_json() or {}
    updates = {}
    if "name" in body:
        updates["name"] = body["name"]
    if "style_prompt" in body:
        updates["style_prompt"] = body["style_prompt"]
    if "assistant_id" in body:
        updates["assistant_id"] = body["assistant_id"] or None
    if "color" in body:
        ok_color, norm_color = _validate_project_color(body["color"])
        if not ok_color:
            return jsonify({"error": "Invalid color (expected hex #rrggbb)"}), 400
        updates["color"] = norm_color
    if "niche" in body:   # nicho EXPLÍCITO de la marca (editable en ajustes)
        updates["niche"] = (body.get("niche") or "").strip()[:80] or None
    if "subniches" in body:
        updates["subniches"] = [t for t in (_norm_tag(s) for s in (body.get("subniches") or [])) if t][:8]
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    db.table("projects").update(updates).eq("id", project_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


# ── Scripts (PATCH for performance) ──────────────────────────────────────────

@app.route("/scripts/<script_id>", methods=["PATCH"])
@require_auth
def update_script(script_id):
    user = current_user()
    body = request.get_json()
    updates = {}
    for key in ("title", "transcription", "script", "performance_notes", "views_count", "engagement_rate", "project_id", "likes", "comments", "saves", "metrics_image_url", "published_at", "recording_status", "alt_hooks", "approval_status", "recording_format"):
        if key in body:
            updates[key] = body[key]
    if "recording_status" in updates and updates["recording_status"] not in ("pending", "recorded", "discarded"):
        return jsonify({"error": "Invalid recording_status"}), 400
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    db.table("scripts").update(updates).eq("id", script_id).eq("user_id", user["id"]).execute()
    # Loop de aprendizaje SIN publicar: si la edición trae señal real (validar/grabar/
    # editar/cambiar hooks), re-afina la voz de ESA marca con los guiones que el user
    # valida/graba. Best-effort, no bloquea la respuesta.
    if any(k in updates for k in ("recording_status", "approval_status", "script", "hook", "alt_hooks")):
        try:
            _bid = updates.get("project_id")
            if _bid is None:
                _r = db.table("scripts").select("project_id").eq("id", script_id).eq("user_id", user["id"]).limit(1).execute()
                _bid = (_r.data or [{}])[0].get("project_id")
            refine_voice_from_own_scripts(user["id"], _bid)
        except Exception:
            logger.warning("update_script: refine voice failed", exc_info=True)
    return jsonify({"ok": True})


# ── Scripts: hooks alternativos (B7) — banco de ganchos por guión ────────────
#  scripts.hook = gancho ACTIVO del guión · scripts.alt_hooks JSONB = banco de
#  alternativas. Las 3 rutas exigen guion PROPIO (user_id = current_user).
def _fetch_own_script_hooks(script_id, user_id):
    """(hook activo, alt_hooks lista) del guión propio, o (None, None) si no existe."""
    r = (db.table("scripts").select("script, alt_hooks")
           .eq("id", script_id).eq("user_id", user_id).limit(1).execute())
    if not r.data:
        return None, None
    row = r.data[0]
    alt = row.get("alt_hooks")
    if not isinstance(alt, list):
        alt = []
    # No existe columna `hook`; el hook es la primera línea del campo `script`.
    hook = (row.get("script") or "").strip().split("\n")[0] or None
    return hook, alt


@app.route("/scripts/<script_id>/hooks", methods=["POST"])
@require_auth
def add_script_hook(script_id):
    """Añade un hook al banco (alt_hooks), con dedupe. Devuelve alt_hooks actualizado."""
    user = current_user()
    body = request.get_json() or {}
    hook = body.get("hook")
    if not isinstance(hook, str) or not hook.strip():
        return jsonify({"error": "Hook required"}), 400
    hook = hook.strip()
    active, alt = _fetch_own_script_hooks(script_id, user["id"])
    if alt is None:
        return jsonify({"error": "Script not found"}), 404
    # dedupe: ni duplicar en el banco ni clonar el gancho ya activo.
    if hook not in alt and hook != (active or ""):
        alt = alt + [hook]
        db.table("scripts").update({"alt_hooks": alt}).eq("id", script_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True, "alt_hooks": alt})


@app.route("/scripts/<script_id>/hooks", methods=["DELETE"])
@require_auth
def delete_script_hook(script_id):
    """Quita un hook del banco por índice (?index=N). Devuelve alt_hooks actualizado."""
    user = current_user()
    try:
        index = int(request.args.get("index", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid index"}), 400
    _active, alt = _fetch_own_script_hooks(script_id, user["id"])
    if alt is None:
        return jsonify({"error": "Script not found"}), 404
    if not (0 <= index < len(alt)):
        return jsonify({"error": "Index out of range"}), 400
    alt = alt[:index] + alt[index + 1:]
    db.table("scripts").update({"alt_hooks": alt}).eq("id", script_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True, "alt_hooks": alt})


@app.route("/scripts/<script_id>/hooks/use", methods=["POST"])
@require_auth
def use_script_hook(script_id):
    """Swap: el hook activo del guión ↔ alt_hooks[index]. No se pierde ninguno
    (el activo saliente cae al banco en la posición del que se promueve)."""
    user = current_user()
    body = request.get_json() or {}
    try:
        index = int(body.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid index"}), 400
    active, alt = _fetch_own_script_hooks(script_id, user["id"])
    if alt is None:
        return jsonify({"error": "Script not found"}), 404
    if not (0 <= index < len(alt)):
        return jsonify({"error": "Index out of range"}), 400
    new_active = alt[index]
    new_alt = list(alt)
    # el activo saliente ocupa el hueco; si no había activo, simplemente se retira del banco.
    if active and active.strip():
        new_alt[index] = active
    else:
        new_alt = new_alt[:index] + new_alt[index + 1:]
    db.table("scripts").update(
        {"hook": new_active, "alt_hooks": new_alt}
    ).eq("id", script_id).eq("user_id", user["id"]).execute()
    # Elegir un hook es la señal de preferencia MÁS fuerte → re-afina la voz.
    try:
        _r = db.table("scripts").select("project_id").eq("id", script_id).eq("user_id", user["id"]).limit(1).execute()
        refine_voice_from_own_scripts(user["id"], (_r.data or [{}])[0].get("project_id"))
    except Exception:
        logger.warning("use_script_hook: refine voice failed", exc_info=True)
    return jsonify({"ok": True, "hook": new_active, "alt_hooks": new_alt})


# ── Agency ───────────────────────────────────────────────────────────────────

@app.route("/agency/invite", methods=["POST"])
@require_auth
def invite_member():
    user = current_user()
    profile = get_profile(user["id"])
    if profile.get("plan") != "agency":
        return jsonify({"error": "Agency plan required"}), 403

    body = request.get_json()
    email = (body.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email required"}), 400

    body_role = (body.get("role") or "member").strip().lower()
    if body_role not in ("member", "owner"):
        body_role = "member"
    # role: por defecto 'member' (crea/edita guiones). El owner es implícito (el
    # dueño de la agencia), no una fila aquí. Resiliente si la columna `role`
    # aún no existe (migración del bloque) → reintenta sin ella.
    _payload = {"agency_owner_id": user["id"], "invited_email": email,
                "status": "pending", "role": body_role}
    try:
        result = db.table("agency_members").insert(_payload).execute()
    except Exception:
        _payload.pop("role", None)
        result = db.table("agency_members").insert(_payload).execute()

    token = result.data[0]["invite_token"] if result.data else None
    invite_url = f"{request.host_url}join?token={token}"
    # B2: enviar la invitación por EMAIL (Resend), además de devolver el enlace.
    try:
        from tasks import send_agency_invite_email
        _owner_name = (user.get("email") or "").split("@")[0] or None
        send_agency_invite_email.delay(user["id"], email, invite_url, _owner_name)
    except Exception as e:
        logger.warning("agency invite email dispatch failed owner=%s err=%s", user["id"], e)
    return jsonify({"invite_url": invite_url, "token": token, "emailed": True})


@app.route("/agency/join", methods=["POST"])
def join_agency():
    user = current_user()
    if not user:
        return jsonify({"error": "Login required"}), 401

    body = request.get_json()
    token = body.get("token", "")
    if not token:
        return jsonify({"error": "Token required"}), 400

    result = db.table("agency_members").select("*").eq("invite_token", token).eq("status", "pending").execute()
    if not result.data:
        return jsonify({"error": "Invalid or expired invite"}), 404

    invite = result.data[0]
    db.table("agency_members").update({
        "member_id": user["id"],
        "status": "active",
    }).eq("invite_token", token).execute()

    return jsonify({"ok": True, "agency_owner_id": invite["agency_owner_id"]})


@app.route("/agency/members")
@require_auth
def get_members():
    user = current_user()
    rows = db.table("agency_members").select("*").eq("agency_owner_id", user["id"]).execute()
    # Enrich with profile data for active members
    members = []
    for row in rows.data:
        member = dict(row)
        if row.get("member_id"):
            prof = db.table("profiles").select("avatar_seed, monthly_usage").eq("id", row["member_id"]).execute()
            if prof.data:
                member["avatar_seed"] = prof.data[0].get("avatar_seed", "default")
                member["monthly_usage"] = prof.data[0].get("monthly_usage", 0)
        members.append(member)
    return jsonify(members)


@app.route("/agency/members/<member_id>", methods=["DELETE"])
@require_auth
def remove_member(member_id):
    user = current_user()
    db.table("agency_members").delete().eq("agency_owner_id", user["id"]).eq("id", member_id).execute()
    return jsonify({"ok": True})


# ── Profile data (extended) ──────────────────────────────────────────────────

@app.route("/profile/data")
@require_auth
def profile_data():
    """Full profile data for the profile hub."""
    user = current_user()
    profile = get_profile(user["id"])
    plan = profile.get("plan", "free")

    data = {
        "email": user["email"],
        "plan": plan,
        "avatar_seed": profile.get("avatar_seed", "default"),
        "credits_cents": profile["credits_cents"],
        "monthly_usage": profile.get("monthly_usage", 0),
        "monthly_limit": PLAN_LIMITS.get(plan),
    }

    # Projects + script counts (all plans)
    projects = db.table("projects").select("*").eq("user_id", user["id"]).order("created_at", desc=True).execute()
    for p in projects.data:
        count = db.table("scripts").select("id", count="exact").eq("project_id", p["id"]).execute()
        p["script_count"] = count.count if hasattr(count, "count") else 0
    data["projects"] = projects.data

    recent = db.table("scripts").select("id, title, project_id, created_at").eq("user_id", user["id"]).order("created_at", desc=True).limit(5).execute()
    data["recent_scripts"] = recent.data

    return jsonify(data)


@app.route("/api/me/stats")
@require_auth
def api_me_stats():
    """Aggregated counters for the workspace dashboard cards."""
    uid = current_user()["id"]

    def _count(query):
        try:
            r = query.execute()
            return r.count if hasattr(r, "count") and r.count is not None else 0
        except Exception:
            return 0

    def _ideas_total():
        return _count(db.table("ideas").select("id", count="exact").eq("user_id", uid))

    def _ideas_pending():
        return _count(db.table("ideas").select("id", count="exact").eq("user_id", uid).eq("status", "draft"))

    def _projects():
        return _count(db.table("projects").select("id", count="exact").eq("user_id", uid))

    def _scripts():
        return _count(db.table("scripts").select("id", count="exact").eq("user_id", uid))

    def _assistants():
        return _count(db.table("assistants").select("id", count="exact").eq("user_id", uid))

    def _transcriptions():
        return _count(db.table("transcriptions").select("id", count="exact").eq("user_id", uid))

    def _ig_videos():
        return _count(db.table("ig_videos").select("id", count="exact").eq("user_id", uid))

    def _has_ig_profile():
        try:
            r = db.table("ig_profiles").select("id", count="exact").eq("user_id", uid).execute()
            return bool(r.count and r.count > 0)
        except Exception:
            return False

    def _team_active():
        return _count(db.table("agency_members").select("id", count="exact").eq("agency_owner_id", uid).eq("status", "active"))

    def _team_pending():
        return _count(db.table("agency_members").select("id", count="exact").eq("agency_owner_id", uid).eq("status", "pending"))

    profile = get_profile(uid)
    weekly = int(profile.get("metrics_analyses_this_week") or 0)

    tasks = {
        "ideas_total":    _ideas_total,
        "ideas_pending":  _ideas_pending,
        "projects":       _projects,
        "scripts":        _scripts,
        "assistants":     _assistants,
        "transcriptions": _transcriptions,
        "ig_videos":      _ig_videos,
        "has_ig_profile": _has_ig_profile,
        "team_active":    _team_active,
        "team_pending":   _team_pending,
    }
    out = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {k: ex.submit(fn) for k, fn in tasks.items()}
        for k, fut in futures.items():
            try:
                out[k] = fut.result(timeout=4)
            except Exception:
                out[k] = 0 if k != "has_ig_profile" else False

    return jsonify({
        "ideas":          {"total": out["ideas_total"], "pending": out["ideas_pending"]},
        "projects":       out["projects"],
        "scripts":        out["scripts"],
        "assistants":     out["assistants"],
        "transcriptions": out["transcriptions"],
        "ig_videos":      out["ig_videos"],
        "metrics":        {"has_profile": out["has_ig_profile"], "weekly_analyses": weekly},
        "team":           {"active": out["team_active"], "pending": out["team_pending"]},
    })


# ── /api/me/overview — dashboard rico para #profPanelOverview ────────────
_overview_cache: dict = {}  # uid -> (data, ts)
_OVERVIEW_TTL_S = 60


def _ov_get_cached(uid: str):
    entry = _overview_cache.get(uid)
    if entry and (_time.time() - entry[1]) < _OVERVIEW_TTL_S:
        return entry[0]
    return None


def _ov_set_cached(uid: str, data: dict) -> None:
    _overview_cache[uid] = (data, _time.time())


def _derive_title_from_text(text: str, max_chars: int = 60) -> str:
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "…"


@app.route("/api/me/overview")
@require_auth
def api_me_overview():
    """Rich dashboard data for #profPanelOverview.
    7 parallel queries via ThreadPoolExecutor. Cached 60s per user_id.
    v0.15 deuda: tabla `adapts` + `assistants.usage_count` para enriquecer
    los widgets Hazlo tuyo y Asistentes (hoy honestos-vacíos).
    """
    uid = current_user()["id"]

    cached = _ov_get_cached(uid)
    if cached is not None:
        return jsonify(cached)

    def _w_transcribe():
        try:
            r = (db.table("transcriptions")
                   .select("id, url, text, platform, thumbnail_b64, created_at, views", count="exact")
                   .eq("user_id", uid)
                   .order("created_at", desc=True)
                   .limit(1)
                   .execute())
            total = r.count or 0
            last = None
            if r.data:
                row = r.data[0]
                last = {
                    "id": row.get("id"),
                    "url": row.get("url"),
                    "title": _derive_title_from_text(row.get("text") or ""),
                    "platform": row.get("platform"),
                    "thumbnail_b64": row.get("thumbnail_b64"),
                    "created_at": row.get("created_at"),
                    "views": row.get("views"),
                }
            return {"total": total, "last": last}
        except Exception as e:
            logger.warning("ov_transcribe error: %s", e)
            return {"total": 0, "last": None}

    def _w_ideas():
        try:
            total_r = (db.table("ideas").select("id", count="exact").eq("user_id", uid).execute())
            pending_r = (db.table("ideas").select("id", count="exact").eq("user_id", uid).eq("status", "draft").execute())
            last_r = (db.table("ideas")
                        .select("id, title, raw_text, status, created_at")
                        .eq("user_id", uid)
                        .order("created_at", desc=True)
                        .limit(1)
                        .execute())
            last = None
            if last_r.data:
                row = last_r.data[0]
                title = (row.get("title") or "").strip() or _derive_title_from_text(row.get("raw_text") or "", 60)
                last = {
                    "title": title,
                    "status": row.get("status") or "draft",
                    "created_at": row.get("created_at"),
                }
            return {"total": total_r.count or 0, "pending": pending_r.count or 0, "last": last}
        except Exception as e:
            logger.warning("ov_ideas error: %s", e)
            return {"total": 0, "pending": 0, "last": None}

    def _w_projects():
        try:
            total_r = db.table("projects").select("id", count="exact").eq("user_id", uid).execute()
            top_r = (db.table("projects")
                       .select("id, name, color")
                       .eq("user_id", uid)
                       .order("created_at", desc=True)
                       .limit(3)
                       .execute())
            top = [
                {"id": p["id"], "name": p.get("name") or "—", "color": p.get("color") or "#ef6a29"}
                for p in (top_r.data or [])
            ]
            return {"total": total_r.count or 0, "top": top}
        except Exception as e:
            logger.warning("ov_projects error: %s", e)
            return {"total": 0, "top": []}

    def _w_metrics():
        try:
            prof_r = db.table("ig_profiles").select("id, ig_username").eq("user_id", uid).limit(1).execute()
            has_profile = bool(prof_r.data)
            sparkline = []
            avg_views = 0
            top_reel = None
            if has_profile:
                vids_r = (db.table("ig_videos")
                            .select("views, published_at")
                            .eq("user_id", uid)
                            .order("published_at", desc=True)
                            .limit(7)
                            .execute())
                rows = vids_r.data or []
                # Cronológico: invertimos (oldest → newest)
                rows = list(reversed(rows))
                sparkline = [int(v.get("views") or 0) for v in rows]
                if sparkline:
                    avg_views = sum(sparkline) // len(sparkline)
                top_r = (db.table("ig_videos")
                           .select("caption, views, published_at")
                           .eq("user_id", uid)
                           .order("views", desc=True)
                           .limit(1)
                           .execute())
                if top_r.data:
                    top = top_r.data[0]
                    caption = (top.get("caption") or "").strip()
                    top_reel = {
                        "caption_snippet": caption[:40] + ("…" if len(caption) > 40 else ""),
                        "views": int(top.get("views") or 0),
                        "published_at": top.get("published_at"),
                    }
            return {
                "has_profile": has_profile,
                "sparkline": sparkline,
                "avg_views_recent": avg_views,
                "top_reel": top_reel,
            }
        except Exception as e:
            logger.warning("ov_metrics error: %s", e)
            return {"has_profile": False, "sparkline": [], "avg_views_recent": 0, "top_reel": None}

    def _w_assistants():
        try:
            total_r = db.table("assistants").select("id", count="exact").eq("user_id", uid).execute()
            last_r = (db.table("assistants")
                        .select("id, name, created_at")
                        .eq("user_id", uid)
                        .order("created_at", desc=True)
                        .limit(1)
                        .execute())
            last_created = None
            if last_r.data:
                row = last_r.data[0]
                last_created = {"name": row.get("name") or "—", "created_at": row.get("created_at")}
            return {"total": total_r.count or 0, "last_created": last_created}
        except Exception as e:
            logger.warning("ov_assistants error: %s", e)
            return {"total": 0, "last_created": None}

    def _w_scripts():
        try:
            total_r = db.table("scripts").select("id", count="exact").eq("user_id", uid).execute()
            last_r = (db.table("scripts")
                        .select("id, title, created_at")
                        .eq("user_id", uid)
                        .order("created_at", desc=True)
                        .limit(1)
                        .execute())
            last = None
            if last_r.data:
                row = last_r.data[0]
                last = {
                    "title": (row.get("title") or "").strip() or "Sin título",
                    "created_at": row.get("created_at"),
                }
            return {"total": total_r.count or 0, "last": last}
        except Exception as e:
            logger.warning("ov_scripts error: %s", e)
            return {"total": 0, "last": None}

    def _w_team():
        try:
            active_r = (db.table("agency_members")
                          .select("id, member_id", count="exact")
                          .eq("agency_owner_id", uid)
                          .eq("status", "active")
                          .limit(4)
                          .execute())
            pending_r = (db.table("agency_members")
                           .select("id", count="exact")
                           .eq("agency_owner_id", uid)
                           .eq("status", "pending")
                           .execute())
            members = [{"id": m.get("member_id")} for m in (active_r.data or [])]
            return {
                "active": active_r.count or 0,
                "pending": pending_r.count or 0,
                "members": members,
            }
        except Exception as e:
            logger.warning("ov_team error: %s", e)
            return {"active": 0, "pending": 0, "members": []}

    # scripts.title se inserta en /save-script como "Guión adaptado · {style} · {fecha}".
    # Como no hay columna style explícita, derivamos el style del título por regex.
    _STYLE_RE = re.compile(r"Guión adaptado · (.+?) ·")

    def _extract_style(title: str) -> str:
        if not title:
            return ""
        m = _STYLE_RE.search(title)
        return m.group(1).strip() if m else ""

    def _w_adapt():
        try:
            total_r = (db.table("scripts")
                         .select("id", count="exact")
                         .eq("user_id", uid)
                         .execute())
            last_r = (db.table("scripts")
                        .select("id, title, script, created_at")
                        .eq("user_id", uid)
                        .order("created_at", desc=True)
                        .limit(1)
                        .execute())
            last = None
            if last_r.data:
                row = last_r.data[0]
                content = (row.get("script") or "").strip()
                last = {
                    "text_snippet": content[:60] + ("…" if len(content) > 60 else ""),
                    "style": _extract_style(row.get("title") or ""),
                    "created_at": row.get("created_at"),
                }
            return {"total": total_r.count or 0, "last": last}
        except Exception as e:
            logger.warning("ov_adapt error: %s", e)
            return {"total": 0, "last": None}

    tasks = {
        "transcribe": _w_transcribe,
        "ideas":      _w_ideas,
        "projects":   _w_projects,
        "metrics":    _w_metrics,
        "assistants": _w_assistants,
        "scripts":    _w_scripts,
        "team":       _w_team,
        "adapt":      _w_adapt,
    }
    out: dict = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {k: ex.submit(fn) for k, fn in tasks.items()}
        for k, fut in futures.items():
            try:
                out[k] = fut.result(timeout=4)
            except Exception as e:
                logger.warning("ov_task %s timeout/error: %s", k, e)
                out[k] = None

    # teleprompter — sin tabla todavía
    out["teleprompter"] = None

    _ov_set_cached(uid, out)
    return jsonify(out)


# ── Metrics ──────────────────────────────────────────────────────────────────

# ── FIX1 · Métricas por MARCA ────────────────────────────────────────────────
# El IG conectado y sus reels pertenecen a la marca (project), no a la cuenta global.
# Modelo: ig_profiles.project_id (1 perfil de IG por marca); ig_videos cuelga del
# ig_profile_id → queda auto-scopeado. project_id NULL = cuenta por defecto (creadores
# de 1 marca y datos previos a la migración). REQUIERE la columna ig_profiles.project_id
# (migración Supabase en docs/migration-metrics-por-marca.sql).
def _req_project_id():
    """project_id de la request (query ?project_id/?brand o body), validado como UUID.
    'default' o cualquier no-UUID → None (cuenta por defecto)."""
    pid = request.args.get("project_id") or request.args.get("brand")
    if not pid:
        try:
            _b = request.get_json(silent=True) or {}
            pid = _b.get("project_id") or _b.get("brand")
        except Exception:
            pid = None
    if not pid:
        return None
    try:
        _uuid.UUID(str(pid))
        return str(pid)
    except Exception:
        return None


def _ig_scope(query, pid):
    """Filtra ig_profiles por marca: project_id = pid, o IS NULL si pid es None."""
    return query.eq("project_id", pid) if pid else query.is_("project_id", "null")


def _get_ig_profile(uid, pid, cols="*"):
    """Perfil de IG de la MARCA activa (user_id + project_id). None si no hay (o si la
    columna aún no existe → degradación segura hasta correr la migración)."""
    try:
        r = _ig_scope(db.table("ig_profiles").select(cols).eq("user_id", uid), pid).limit(1).execute()
        return (r.data or [None])[0]
    except Exception:
        return None


@app.route("/metrics/summary")
@require_auth
def metrics_summary():
    user = current_user()
    # El front (isla) manda ?brand=<id>; el legacy manda ?project_id. brand == project_id.
    project_id = request.args.get("project_id") or request.args.get("brand")
    # FIX: 'brand=default' (centinela del front) no es UUID valido -> no filtrar por marca
    # (Postgres rechazaba el valor en la columna UUID y devolvia 500).
    if project_id:
        try:
            _uuid.UUID(str(project_id))
        except Exception:
            project_id = None

    q = db.table("scripts").select("views_count, likes, comments, saves, engagement_rate").eq("user_id", user["id"])
    if project_id:
        q = q.eq("project_id", project_id)
    rows = q.execute()

    total_views = sum(r.get("views_count") or 0 for r in rows.data)
    total_likes = sum(r.get("likes") or 0 for r in rows.data)
    total_comments = sum(r.get("comments") or 0 for r in rows.data)
    total_saves = sum(r.get("saves") or 0 for r in rows.data)
    rates = [r.get("engagement_rate") for r in rows.data if r.get("engagement_rate")]
    avg_engagement = round(sum(rates) / len(rates), 2) if rates else 0

    # IG conectado: el JS hace S.igConnected=(met.connected!==false). Sin este campo
    # quedaba undefined → siempre true → ocultaba la tarjeta "Conecta Instagram".
    # connected = la MARCA activa tiene IG vinculado (no la cuenta global).
    connected = bool(_get_ig_profile(user["id"], project_id, "id"))

    return jsonify({
        "total_views": total_views,
        "total_likes": total_likes,
        "total_comments": total_comments,
        "total_saves": total_saves,
        "avg_engagement": avg_engagement,
        "scripts_with_metrics": len([r for r in rows.data if r.get("views_count")]),
        "connected": connected,
    })


@app.route("/api/metrics/learn", methods=["POST"])
@limiter.limit("10 per minute;60 per hour")
@require_auth
def api_metrics_learn():
    """Loop de medición (C): el frontend, al traer los reels publicados del usuario
    (Instagram conectado), los manda aquí → atribuimos cada uno a su guión, escribimos
    sus métricas y realimentamos el VoiceProfile. Devuelve qué funciona + siguiente."""
    user = current_user()
    body = request.get_json() or {}
    videos = body.get("videos") or []
    if not isinstance(videos, list):
        return jsonify({"error": "videos inválido"}), 400
    res = attribute_and_learn(user["id"], videos)
    res["next"] = next_series_suggestion(user["id"])
    return jsonify(res)


@app.route("/api/metrics/insights", methods=["GET"])
@require_auth
def api_metrics_insights():
    """Lo que el sistema aprendió de TU cuenta (Métricas/Cerebro) + el siguiente de la
    serie que rinde (Radar/email diario). Persistido en el VoiceProfile."""
    user = current_user()
    pid = _req_project_id()   # insights de la MARCA activa (sin cruce entre marcas)
    vp = get_voice_profile(user["id"], pid)
    raw = (vp.get("raw") if vp and isinstance(vp.get("raw"), dict) else {}) or {}
    return jsonify({
        "what_works": raw.get("what_works") or [],
        "next": next_series_suggestion(user["id"], pid),
    })


@app.route("/metrics/winners")
@require_auth
def metrics_winners():
    user = current_user()
    rank_by = request.args.get("rank_by", "views_count")
    limit = min(int(request.args.get("limit", 5)), 20)
    project_id = request.args.get("project_id")

    valid_fields = {"views_count", "likes", "comments", "saves", "engagement_rate"}
    if rank_by not in valid_fields:
        rank_by = "views_count"

    q = db.table("scripts").select("*").eq("user_id", user["id"]).not_.is_(rank_by, "null")
    if project_id:
        q = q.eq("project_id", project_id)
    rows = q.order(rank_by, desc=True).limit(limit).execute()

    return jsonify(rows.data)


# ── Ideas ─────────────────────────────────────────────────────────────────────

IDEA_CATEGORIES = {"educational", "storytelling", "opinion", "tutorial", "humor",
                   "case_study", "motivation", "trend", "behind_scenes", "list"}

IDEA_BASE_PROMPT = (
    "Eres un asistente de creación de contenido para creators de Instagram, "
    "TikTok y reels. Recibes una idea cruda del usuario y la conviertes en "
    "un borrador de guión estructurado en 3 partes: intro, desarrollo y cierre.\n\n"
    "REGLAS:\n"
    "- No inventes datos, números ni casos. Si la idea es vaga, desarrolla el concepto sin meter ejemplos falsos.\n"
    "- Frases cortas, máximo 15 palabras.\n"
    "- Sin 'increíble', sin 'chicos', sin paja.\n"
    "- El guión debe sonar hablado, no escrito.\n"
    "- La intro tiene que parar el scroll en los primeros 3 segundos.\n\n"
    "CATEGORÍAS DISPONIBLES:\n"
    "educational, storytelling, opinion, tutorial, humor, case_study, motivation, trend, behind_scenes, list\n\n"
    "Devuelve EXCLUSIVAMENTE un JSON válido, sin texto antes ni después, sin markdown:\n"
    '{"title":"string corto max 60 chars","category":"una de las categorías","script_draft":{"intro":"1-2 frases hook","desarrollo":"3-5 frases contenido","cierre":"1-2 frases cierre"}}'
)


def resolve_assistant_prompt(assistant_id, user_id=None):
    """Returns the style instructions for an assistant_id."""
    if not assistant_id:
        return ""
    # Predefined?
    if assistant_id in STYLE_PROMPTS:
        return STYLE_PROMPTS[assistant_id]
    # Custom assistant from DB?
    result = db.table("assistants").select("instructions").eq("id", assistant_id).execute()
    if result.data:
        return CUSTOM_BASE + result.data[0]["instructions"]
    return ""


def develop_idea(raw_text, assistant_id=None, user_id=None, language="es"):
    """Call AI to develop a raw idea into structured script draft."""
    style_block = resolve_assistant_prompt(assistant_id, user_id)
    # P0-4: los STYLE_PROMPTS terminan en _JSON_SCRIPT_SCHEMA ({"hook","body",
    # "closing"}), que CONTRADICE el contrato de idea ({"title","category",
    # "script_draft"}). Con asistente, el modelo obedecía el último schema →
    # script_draft=None → ideas "developed" sin borrador. Recortamos cualquier
    # schema del bloque de estilo y re-afirmamos el contrato de idea al final.
    if style_block:
        style_block = style_block.split("Devuelve ÚNICAMENTE")[0].strip()
    system = IDEA_BASE_PROMPT
    if style_block:
        system += (
            f"\n\nESTILO ESPECÍFICO PARA ESTE GUIÓN (aplica su TONO y estructura; "
            f"NO cambia el formato de salida):\n{style_block}"
            "\n\nRECUERDA: la salida es EXCLUSIVAMENTE el JSON del contrato de arriba — "
            '{"title","category","script_draft":{"intro","desarrollo","cierre"}}. '
            "Ningún otro formato."
        )

    # v0.25.43: usa el helper resiliente (_call_llm_json con FALLBACK a Groq) en vez
    # de pegar inline solo a OpenRouter — con OpenRouter sin saldo (402) esto quedaba
    # 100% roto. El helper fuerza json-mode (Gemini y Groq) y parsea/strip de fences.
    try:
        return _call_llm_json(
            system,
            f"Idioma de salida: {language}. Idea cruda del usuario: {raw_text}",
            max_tokens=4000, temperature=0.7,
        )
    except Exception as e:
        logger.error("develop_idea LLM failed user=%s err=%s", raw_text[:60], e)
        raise


@app.route("/ideas", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def create_idea():
    user = current_user()
    body = request.get_json() or {}
    raw_text = (body.get("raw_text") or "").strip()
    language = body.get("language", "es")
    project_id = body.get("project_id") or None
    assistant_id = body.get("assistant_id") or None

    if not raw_text or len(raw_text) < 5:
        return jsonify({"error": "Idea text too short (min 5 chars)"}), 400
    if len(raw_text) > 5000:
        return jsonify({"error": "Idea text too long (max 5000 chars)"}), 400

    develop = body.get("develop", True)

    # Resolve assistant: body > user default > neutral
    if not assistant_id:
        prof = db.table("profiles").select("default_idea_assistant").eq("id", user["id"]).execute()
        if prof.data and prof.data[0].get("default_idea_assistant"):
            assistant_id = prof.data[0]["default_idea_assistant"]

    if develop:
        try:
            result = develop_idea(raw_text, assistant_id, user["id"], language)
        except Exception as e:
            logger.error(f"Idea development failed: {e}", exc_info=True)
            # Fallback: save as draft instead of failing
            row = db.table("ideas").insert({
                "user_id": user["id"], "project_id": project_id,
                "raw_text": raw_text, "assistant_id": assistant_id, "status": "draft",
            }).execute()
            return jsonify({"ok": True, "status": "draft", "fallback": True,
                            "idea": row.data[0] if row.data else None,
                            "error": "Could not develop — saved as draft"}), 200
    else:
        result = {}

    row = db.table("ideas").insert({
        "user_id": user["id"],
        "project_id": project_id,
        "raw_text": raw_text,
        "assistant_id": assistant_id,
        "title": result.get("title"),
        "category": result.get("category"),
        "script_draft": result.get("script_draft"),
        "status": "developed" if develop else "draft",
    }).execute()

    return jsonify(row.data[0] if row.data else result)


@app.route("/ideas")
@require_auth
def list_ideas():
    user = current_user()
    # v0.15.4: excluir draft_suggested (feature 'generar idea' eliminada en
    # v0.15.5, pero ideas-zombi con ese status pueden seguir en BD; las
    # ocultamos de Guardadas).
    q = (db.table("ideas")
           .select("*")
           .eq("user_id", user["id"])
           .neq("status", "draft_suggested"))
    project_id = request.args.get("project_id")
    category = request.args.get("category")
    assistant_id = request.args.get("assistant_id")
    if project_id:
        q = q.eq("project_id", project_id)
    if category:
        q = q.eq("category", category)
    if assistant_id:
        q = q.eq("assistant_id", assistant_id)
    limit = min(int(request.args.get("limit", 50)), 100)
    offset = int(request.args.get("offset", 0))
    rows = q.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return jsonify(rows.data)


@app.route("/ideas/<idea_id>")
@require_auth
def get_idea(idea_id):
    user = current_user()
    row = db.table("ideas").select("*").eq("id", idea_id).eq("user_id", user["id"]).execute()
    if not row.data:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row.data[0])


@app.route("/ideas/<idea_id>", methods=["PATCH"])
@require_auth
def update_idea(idea_id):
    user = current_user()
    body = request.get_json() or {}
    updates = {}
    for key in ("title", "category", "script_draft", "project_id", "assistant_id", "status", "recorded_at"):
        if key in body:
            updates[key] = body[key]
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    db.table("ideas").update(updates).eq("id", idea_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


@app.route("/ideas/<idea_id>", methods=["DELETE"])
@require_auth
def delete_idea(idea_id):
    user = current_user()
    db.table("ideas").delete().eq("id", idea_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


@app.route("/ideas/<idea_id>/develop", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def develop_idea_endpoint(idea_id):
    """Develop a draft idea that was saved without AI processing."""
    user = current_user()
    row = db.table("ideas").select("*").eq("id", idea_id).eq("user_id", user["id"]).execute()
    if not row.data:
        return jsonify({"error": "Not found"}), 404

    idea = row.data[0]
    if idea.get("status") != "draft":
        return jsonify({"error": "This idea is already developed. Use /regenerate to redo it."}), 400

    body = request.get_json() or {}
    assistant_id = body.get("assistant_id") or idea.get("assistant_id")
    language = body.get("language", "es")

    try:
        result = develop_idea(idea["raw_text"], assistant_id, user["id"], language)
    except json.JSONDecodeError as e:
        # v0.14.15a: LLM returned malformed/truncated JSON. Keep idea as draft,
        # don't return 502 — user retries vs. seeing a hard error.
        logger.error(f"develop_idea JSON parse failed: {e}", exc_info=True)
        return jsonify({
            "ok": False,
            "fallback": "draft",
            "message": "La idea sigue guardada como borrador. Inténtalo de nuevo en unos minutos."
        }), 200
    except Exception as e:
        logger.error(f"Idea development failed: {e}", exc_info=True)
        return jsonify({"error": "Failed to develop. Try again."}), 502

    db.table("ideas").update({
        "assistant_id": assistant_id,
        "title": result.get("title"),
        "category": result.get("category"),
        "script_draft": result.get("script_draft"),
        "status": "developed",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", idea_id).execute()

    return jsonify(result)


@app.route("/ideas/<idea_id>/regenerate", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def regenerate_idea(idea_id):
    user = current_user()
    row = db.table("ideas").select("*").eq("id", idea_id).eq("user_id", user["id"]).execute()
    if not row.data:
        return jsonify({"error": "Not found"}), 404

    idea = row.data[0]
    body = request.get_json() or {}
    assistant_id = body.get("assistant_id") or idea.get("assistant_id")
    language = body.get("language", "es")

    # econ: regenerar = REGEN_UNITS (1 cr), más barato que un guión nuevo (3 cr).
    _err, _refund, _ = _charge_units_locked(user["id"], REGEN_UNITS, user)
    if _err:
        return _err

    try:
        result = develop_idea(idea["raw_text"], assistant_id, user["id"], language)
    except Exception as e:
        _refund()
        logger.error(f"Idea regeneration failed: {e}", exc_info=True)
        return jsonify({"error": "Failed to regenerate. Try again."}), 502

    db.table("ideas").update({
        "assistant_id": assistant_id,
        "title": result.get("title"),
        "category": result.get("category"),
        "script_draft": result.get("script_draft"),
        "status": "developed",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", idea_id).execute()

    return jsonify(result)


# v0.14.30: linkedin queda fuera del set para to-script (no encaja con reel
# 30-45s). El estilo sigue disponible vía /adapt directo para retrocompat.
_BUILTIN_SCRIPT_STYLES = {"viral", "divertido", "storytelling", "hooks", "educacional", "informativo"}


@app.route("/scripts/<script_id>/regenerate", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def regenerate_script(script_id):
    """econ (gap D1): regenerar un guión YA creado = REGEN_UNITS (1 cr), no un guión
    nuevo (3). Re-tira del MISMO material: el reel original si lo hay, si no el propio
    texto. En la voz del usuario. Sync, mismo patrón que /ideas/<id>/regenerate."""
    user = current_user()
    uid = user["id"]
    row = db.table("scripts").select("*").eq("id", script_id).eq("user_id", uid).execute()
    if not row.data:
        return jsonify({"error": "Not found"}), 404
    sc = row.data[0]

    # Material de origen: el reel del que salió (re-tira del mismo reel) o el propio texto.
    source = ""
    reel_id = sc.get("from_competitor_reel_id")
    if reel_id:
        try:
            rr = (db.table("creator_reels_global")
                    .select("caption, transcript").eq("id", reel_id).single().execute())
            if rr.data:
                source = ((rr.data.get("transcript") or "") + "\n"
                          + (rr.data.get("caption") or "")).strip()
        except Exception:
            pass
    if not source:
        source = (sc.get("script") or "").strip()
    if not source:
        return jsonify({"error": "no_source",
                        "message": "No hay material para regenerar este guion."}), 400

    # econ: cobra 1 crédito ANTES; refunda si el LLM falla.
    err, refund, _ = _charge_units_locked(uid, REGEN_UNITS, user)
    if err:
        return err

    style = str(sc.get("assistant_name") or "viral").lower()
    style_arg = style if style in _BUILTIN_SCRIPT_STYLES else "viral"
    try:
        result = adapt_with_ai(source, style_arg, "", voice=get_voice_profile(uid), user_id=uid)
    except Exception as e:
        refund()
        logger.error("regenerate_script LLM failed script=%s: %s", script_id, e, exc_info=True)
        return jsonify({"error": "llm_error",
                        "message": "No se pudo regenerar. Inténtalo de nuevo."}), 502
    if not (isinstance(result, dict) and result.get("hook")):
        refund()
        return jsonify({"error": "llm_error",
                        "message": "No se pudo regenerar. Inténtalo de nuevo."}), 502

    flat = (result["hook"] + "\n"
            + "\n".join(result.get("body", [])) + "\n"
            + result.get("closing", "")).strip()
    upd = {"script": flat, "updated_at": datetime.now(timezone.utc).isoformat()}
    if result.get("title"):
        upd["title"] = str(result["title"]).strip()[:80]
    try:
        db.table("scripts").update(upd).eq("id", script_id).eq("user_id", uid).execute()
    except Exception as e:
        logger.error("regenerate_script persist failed script=%s: %s", script_id, e)

    return jsonify({"ok": True, "script": flat, "title": upd.get("title", sc.get("title")),
                    "hook": result["hook"], "body": result.get("body", []),
                    "closing": result.get("closing", ""), **_credits_display(uid)})

# v0.15.7.b: umbral mínimo de chars en custom_prompt antes de invocar al LLM.
# Custom prompts demasiado cortos ("instruccion base", 16 chars) provocan que
# Gemini devuelva content=null tras 4s — refund OK, pero UX confusa. Cortar
# antes del LLM con mensaje accionable hacia /assistants.
_CUSTOM_PROMPT_MIN_CHARS = 30


def _custom_too_short(style_arg, custom_prompt):
    """True si style='custom' y instructions <_CUSTOM_PROMPT_MIN_CHARS chars."""
    if style_arg != "custom":
        return False
    return len((custom_prompt or "").strip()) < _CUSTOM_PROMPT_MIN_CHARS


def _assistant_too_short_response(assistant_name):
    """JSON 400 estándar para asistente custom con instructions inútiles."""
    return jsonify({
        "error": "assistant_too_short",
        "message": (
            f"Las instrucciones de tu asistente '{assistant_name}' son muy cortas "
            f"para generar guion (mínimo {_CUSTOM_PROMPT_MIN_CHARS} caracteres). "
            f"Edítalas en Asistentes o usa otro estilo."
        ),
        "assistant_name": assistant_name,
        "min_chars": _CUSTOM_PROMPT_MIN_CHARS,
    }), 400


@app.route("/ideas/<idea_id>/to-script", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def idea_to_script(idea_id):
    """v0.14.29 — Guionizar idea con asistente elegido. Cobra 1 crédito antes
    del LLM y refunda si la generación falla. Persiste guion en `scripts`
    vinculado a la idea (scripts.idea_id)."""
    user = current_user()
    uid = user["id"]
    row = db.table("ideas").select("*").eq("id", idea_id).eq("user_id", uid).execute()
    if not row.data:
        return jsonify({"error": "Not found"}), 404

    idea = row.data[0]
    body = request.get_json() or {}

    profile = get_profile(uid)
    plan = profile.get("plan", "free")

    # v0.14.29: cobro 1 crédito (patrón de /api/ideas/suggest pero con 1 unit).
    SCRIPT_COST = SCRIPT_UNITS * COST_CENTS   # econ: 3 créditos por guión
    SCRIPT_USAGE_UNITS = SCRIPT_UNITS
    is_paid_unlimited = plan in ("pro", "creator", "estudio", "agency")
    if not is_paid_unlimited and (profile.get("credits_cents") or 0) < SCRIPT_COST:
        return jsonify({
            "error": "no_credits",
            "message": "Necesitas 3 créditos para generar un guion. Compra créditos o sube de plan.",
        }), 402

    # Resolución de asistente: body > idea.assistant_id > profile.default > None
    assistant_id = body.get("assistant_id") or idea.get("assistant_id")
    if not assistant_id:
        try:
            if profile.get("default_idea_assistant"):
                assistant_id = profile["default_idea_assistant"]
        except Exception:
            assistant_id = None

    # Decide style/custom_prompt/label según tipo de assistant_id.
    style_arg = "viral"
    custom_prompt = ""
    style_label = "viral"
    if assistant_id in _BUILTIN_SCRIPT_STYLES:
        style_arg = assistant_id
        style_label = assistant_id
    elif assistant_id:
        try:
            asst_r = db.table("assistants").select("name, instructions").eq(
                "id", assistant_id
            ).eq("user_id", uid).execute()
            if asst_r.data and asst_r.data[0].get("instructions"):
                style_arg = "custom"
                custom_prompt = asst_r.data[0]["instructions"]
                style_label = asst_r.data[0].get("name") or "custom"
        except Exception as e:
            logger.warning(
                "idea_to_script: asst lookup failed user=%s asst=%s err=%s",
                uid, assistant_id, e,
            )
            # fall through to default "viral"

    # v0.15.7.b: cortar pre-LLM si custom prompt demasiado corto (sin cobrar).
    if _custom_too_short(style_arg, custom_prompt):
        return _assistant_too_short_response(style_label)

    # v0.14.29: cobrar ANTES del LLM. Si LLM falla, refundar abajo.
    try:
        if is_paid_unlimited:
            db.table("profiles").update({
                "monthly_usage": (profile.get("monthly_usage") or 0) + SCRIPT_USAGE_UNITS
            }).eq("id", uid).execute()
        else:
            db.table("profiles").update({
                "credits_cents": (profile.get("credits_cents") or 0) - SCRIPT_COST
            }).eq("id", uid).execute()
    except Exception as e:
        logger.error("idea_to_script: pre-charge failed user=%s err=%s", uid, e, exc_info=True)
        return jsonify({"error": "Internal error", "message": "Inténtalo de nuevo."}), 500

    def _refund():
        """Revertir el descuento aplicado arriba. Best-effort."""
        try:
            if is_paid_unlimited:
                db.table("profiles").update({
                    "monthly_usage": max(0, (profile.get("monthly_usage") or 0))
                }).eq("id", uid).execute()
            else:
                db.table("profiles").update({
                    "credits_cents": (profile.get("credits_cents") or 0)
                }).eq("id", uid).execute()
        except Exception as e:
            logger.error("idea_to_script: refund failed user=%s err=%s", uid, e, exc_info=True)

    # v0.14.29: nuevo user_content. Pasamos contexto rico (raw_text + title +
    # category) en lugar del script_draft cortito que limitaba el output del LLM.
    title = idea.get("title") or ""
    category = idea.get("category") or ""
    raw_text = idea.get("raw_text") or ""
    user_content = (
        f"[Idea original del usuario]\n{raw_text}\n\n"
        f"[Título]\n{title}\n\n"
        f"[Categoría]\n{category or '—'}\n\n"
        f"Tarea: convierte esto en un guion completo de 30-45 segundos hablados "
        f"para un reel de Instagram. Tu output debe tener desarrollo real, "
        f"ejemplos concretos (sin inventar datos numéricos), profundidad y ritmo. "
        f"Total: 100-140 palabras, mínimo 8 frases en body. "
        f"Incluye al menos 1 ejemplo concreto o anécdota dentro del desarrollo."
    )

    try:
        result = adapt_with_ai(user_content, style_arg, custom_prompt, voice=get_voice_profile(uid), user_id=uid)
    except Exception as e:
        logger.error(f"Idea to script failed: {e}", exc_info=True)
        _refund()
        # v0.15.7.b: mensaje contextual si custom + empty content.
        if style_arg == "custom" and "empty content" in str(e).lower():
            return jsonify({
                "error": "assistant_empty_response",
                "message": f"El asistente '{style_label}' devolvió respuesta vacía. "
                           f"Edita sus instrucciones o usa otro estilo.",
                "assistant_name": style_label,
            }), 502
        return jsonify({"error": "Failed to generate script. Try again."}), 502

    # v0.14.30.b: capturar title del LLM antes del flatten (que reasigna result a string).
    llm_title = ""
    if isinstance(result, dict) and result.get("title"):
        llm_title = str(result["title"]).strip()[:80]

    # Flatten a plano para storage + return (el frontend espera string).
    if isinstance(result, dict) and "hook" in result:
        flat = result["hook"] + "\n" + "\n".join(result.get("body", [])) + "\n" + result.get("closing", "")
        result = flat.strip()
    elif isinstance(result, dict) and isinstance(result.get("hooks"), list):
        # style="hooks" devuelve {"hooks": [{"type": "...", "text": "..."}, ...]}
        # No tiene title; cae al fallback de script_title abajo.
        lines = []
        for h in result["hooks"]:
            if isinstance(h, dict) and h.get("text"):
                lines.append(h["text"])
        result = "\n".join(lines).strip()
    elif not isinstance(result, str):
        result = str(result)

    # v0.14.28: persistir guion automáticamente en `scripts` (idea_id link).
    # v0.14.30.b: usa llm_title si el LLM lo devolvió, fallback al formato legacy.
    today = datetime.now(timezone.utc).strftime("%d %b %Y").lower()
    if llm_title:
        script_title = llm_title
    else:
        script_title = f"Guión adaptado · {style_label} · {today}"
    # (6) regenerar desde la misma idea repetía el título del LLM → parecían duplicados.
    script_title = _uniquify_title(script_title, _existing_idea_titles(uid, idea_id))
    script_id = None
    try:
        script_row = db.table("scripts").insert({
            "user_id":      uid,
            "idea_id":      idea_id,
            "title":        script_title,
            "script":       result,
            "project_id":   idea.get("project_id"),
            "assistant_name": _resolve_assistant_name(
                {"assistant_id": assistant_id, "style": style_label}, uid, db
            ),
        }).execute()
        if script_row.data:
            script_id = script_row.data[0].get("id")
    except Exception as e:
        # No refundamos aquí: el LLM SÍ funcionó (el user tiene el guion en la
        # response). Persistencia es recoverable. Loggeamos para postmortem.
        logger.error(
            "idea_to_script: scripts insert failed user=%s idea=%s err=%s",
            uid, idea_id, e, exc_info=True,
        )

    db.table("ideas").update({
        "status": "scripted",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", idea_id).execute()

    return jsonify({"script": result, "idea_id": idea_id, "script_id": script_id})


# ── v0.14.30: Guionizar desde Transcripción ───────────────────────────────

@app.route("/transcriptions/<int:t_id>/to-script", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def transcription_to_script(t_id):
    """v0.14.30 — Guionizar transcripción con asistente elegido. Cobra 1 crédito
    antes del LLM y refunda si la generación falla. Persiste el guion en
    `scripts` vinculado a la transcripción (scripts.transcription_id)."""
    user = current_user()
    uid = user["id"]
    t_row = db.table("transcriptions").select("*").eq("id", t_id).eq("user_id", uid).execute()
    if not t_row.data:
        return jsonify({"error": "Transcription not found"}), 404

    t = t_row.data[0]
    text = (t.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty transcription"}), 400

    body = request.get_json(silent=True) or {}

    profile = get_profile(uid)
    plan = profile.get("plan", "free")

    # Cobro 3 créditos por guión (robar desde un reel analizado).
    SCRIPT_COST = SCRIPT_UNITS * COST_CENTS   # econ: 3 créditos por guión
    SCRIPT_USAGE_UNITS = SCRIPT_UNITS
    is_paid_unlimited = plan in ("pro", "creator", "estudio", "agency")
    # TOPE DIARIO del FREE (modelo 2026-06-23): 3 robos/día → "vuelve mañana".
    if not is_paid_unlimited and trial_scripts_today(uid) >= FREE_DAILY_SCRIPTS:
        track_event("paywall_shown", uid, {"wall": "free_daily", "plan": plan})
        return jsonify({
            "error": "trial_daily_limit",
            "message": f"Hechos tus {FREE_DAILY_SCRIPTS} guiones de hoy — vuelve mañana o desbloquéalos subiendo de plan.",
        }), 402
    if not is_paid_unlimited and (profile.get("credits_cents") or 0) < SCRIPT_COST:
        return jsonify({
            "error": "no_credits",
            "message": "Necesitas 3 créditos para generar un guion. Compra créditos o sube de plan.",
        }), 402

    # Resolución de asistente: body > profile.default > None.
    # NOTA: a diferencia de ideas, transcriptions NO guarda assistant_id propio.
    assistant_id = (body.get("assistant_id") or "").strip() or None
    if not assistant_id:
        try:
            if profile.get("default_idea_assistant"):
                assistant_id = profile["default_idea_assistant"]
        except Exception:
            assistant_id = None

    # Decide style/custom_prompt/label según tipo de assistant_id.
    style_arg = "viral"
    custom_prompt = ""
    style_label = "viral"
    if assistant_id in _BUILTIN_SCRIPT_STYLES:
        style_arg = assistant_id
        style_label = assistant_id
    elif assistant_id:
        try:
            asst_r = db.table("assistants").select("name, instructions").eq(
                "id", assistant_id
            ).eq("user_id", uid).execute()
            if asst_r.data and asst_r.data[0].get("instructions"):
                style_arg = "custom"
                custom_prompt = asst_r.data[0]["instructions"]
                style_label = asst_r.data[0].get("name") or "custom"
        except Exception as e:
            logger.warning(
                "transcription_to_script: asst lookup failed user=%s asst=%s err=%s",
                uid, assistant_id, e,
            )
            # fall through to default "viral"

    # v0.15.7.b: cortar pre-LLM si custom prompt demasiado corto (sin cobrar).
    if _custom_too_short(style_arg, custom_prompt):
        return _assistant_too_short_response(style_label)

    # Cobrar ANTES del LLM. Si LLM falla, refundar abajo.
    try:
        if is_paid_unlimited:
            db.table("profiles").update({
                "monthly_usage": (profile.get("monthly_usage") or 0) + SCRIPT_USAGE_UNITS
            }).eq("id", uid).execute()
        else:
            db.table("profiles").update({
                "credits_cents": (profile.get("credits_cents") or 0) - SCRIPT_COST
            }).eq("id", uid).execute()
    except Exception as e:
        logger.error(
            "transcription_to_script: pre-charge failed user=%s err=%s",
            uid, e, exc_info=True,
        )
        return jsonify({"error": "Internal error", "message": "Inténtalo de nuevo."}), 500

    def _refund():
        """Revertir el descuento aplicado arriba. Best-effort."""
        try:
            if is_paid_unlimited:
                db.table("profiles").update({
                    "monthly_usage": max(0, (profile.get("monthly_usage") or 0))
                }).eq("id", uid).execute()
            else:
                db.table("profiles").update({
                    "credits_cents": (profile.get("credits_cents") or 0)
                }).eq("id", uid).execute()
        except Exception as e:
            logger.error(
                "transcription_to_script: refund failed user=%s err=%s",
                uid, e, exc_info=True,
            )

    user_content = (
        f"[Transcripción del reel original]\n{text}\n\n"
        f"Tarea: adapta esta transcripción a un guion completo de 30-45 segundos hablados "
        f"para un reel de Instagram en TU estilo. Reescribe con desarrollo real, "
        f"ejemplos concretos (sin inventar datos numéricos), profundidad y ritmo. "
        f"Total: 100-140 palabras, mínimo 8 frases en body. "
        f"Incluye al menos 1 ejemplo concreto o anécdota dentro del desarrollo."
    )

    try:
        result = adapt_with_ai(user_content, style_arg, custom_prompt, voice=get_voice_profile(uid), user_id=uid)
    except Exception as e:
        logger.error(f"transcription_to_script LLM failed: {e}", exc_info=True)
        _refund()
        # v0.15.7.b: mensaje contextual si custom + empty content.
        if style_arg == "custom" and "empty content" in str(e).lower():
            return jsonify({
                "error": "assistant_empty_response",
                "message": f"El asistente '{style_label}' devolvió respuesta vacía. "
                           f"Edita sus instrucciones o usa otro estilo.",
                "assistant_name": style_label,
            }), 502
        return jsonify({"error": "Failed to generate script. Try again."}), 502

    # v0.14.30.b: capturar title del LLM antes del flatten.
    llm_title = ""
    if isinstance(result, dict) and result.get("title"):
        llm_title = str(result["title"]).strip()[:80]

    # Flatten a plano (mismo manejo que /ideas/to-script).
    if isinstance(result, dict) and "hook" in result:
        flat = result["hook"] + "\n" + "\n".join(result.get("body", [])) + "\n" + result.get("closing", "")
        result = flat.strip()
    elif isinstance(result, dict) and isinstance(result.get("hooks"), list):
        # No tiene title; cae al fallback abajo.
        lines = []
        for h in result["hooks"]:
            if isinstance(h, dict) and h.get("text"):
                lines.append(h["text"])
        result = "\n".join(lines).strip()
    elif not isinstance(result, str):
        result = str(result)

    # Persistir guion en `scripts` vinculado a la transcripción.
    # v0.14.30.b: usa llm_title si el LLM lo devolvió, fallback al formato legacy.
    today = datetime.now(timezone.utc).strftime("%d %b %Y").lower()
    if llm_title:
        script_title = llm_title
    else:
        script_title = f"Guión adaptado · {style_label} · {today}"
    script_id = None
    try:
        script_row = db.table("scripts").insert({
            "user_id":          uid,
            "transcription_id": t_id,
            "idea_id":          None,
            "title":            script_title,
            "script":           result,
            "project_id":       t.get("project_id"),
            "assistant_name":   _resolve_assistant_name(
                {"assistant_id": assistant_id, "style": style_label}, uid, db
            ),
        }).execute()
        if script_row.data:
            script_id = script_row.data[0].get("id")
    except Exception as e:
        # No refundamos: el LLM funcionó, el user tiene el guion en la response.
        logger.error(
            "transcription_to_script: scripts insert failed user=%s tid=%s err=%s",
            uid, t_id, e, exc_info=True,
        )

    return jsonify({
        "script":           result,
        "transcription_id": t_id,
        "script_id":        script_id,
    })


# ── v0.14.26: Suggest Ideas (Phase 1) ─────────────────────────────────────

def _call_llm_json(system_prompt, user_prompt, max_tokens=4000, temperature=0.7,
                   model=None, timeout=60):
    """Helper centralizado para llamadas LLM con response_format json_object.
    Reusa env vars OpenRouter / Groq fallback. Devuelve dict parseado.
    Levanta json.JSONDecodeError si Gemini trunca; el caller decide qué hacer.

    v0.15.9.b: kwargs model y timeout para tareas ligeras (Flash 2.0, 10s).
    Default mantiene comportamiento previo (OPENROUTER_MODEL, 60s).

    v0.25.43: FALLBACK A GROQ. Antes solo pegaba a OpenRouter (o Groq si no había
    key OR) → con OpenRouter sin saldo (402), «Generar ideas» y demás quedaban 100%
    rotos. Ahora intenta OpenRouter y, si falla (HTTP/parse), cae a Groq llama-3.3."""
    import json as _json_mod

    def _attempt(provider):
        if provider == "openrouter":
            key = OPENROUTER_API_KEY
            url = OPENROUTER_URL
            mdl = model or OPENROUTER_MODEL
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                       "HTTP-Referer": "https://reelscript.net", "X-Title": "ReelScript"}
            mt = max_tokens
        else:  # groq
            key = GROQ_API_KEY
            url = "https://api.groq.com/openai/v1/chat/completions"
            mdl = "llama-3.3-70b-versatile"
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            mt = min(max_tokens, 8000)
        payload = {
            "model": mdl,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": mt,
        }
        combined = (system_prompt + "\n" + user_prompt).lower()
        if "gemini" in mdl.lower():
            payload["response_format"] = {"type": "json_object"}
        elif provider == "groq" and "json" in combined:
            payload["response_format"] = {"type": "json_object"}
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return _json_mod.loads(content)

    providers = []
    if OPENROUTER_API_KEY:
        providers.append("openrouter")
    if GROQ_API_KEY:
        providers.append("groq")
    if not providers:
        raise RuntimeError("Sin proveedor LLM configurado (OPENROUTER_API_KEY / GROQ_API_KEY)")

    last_err = None
    for i, prov in enumerate(providers):
        try:
            return _attempt(prov)
        except Exception as e:
            last_err = e
            has_more = i + 1 < len(providers)
            logger.warning("_call_llm_json provider=%s falló (%s) — %s",
                           prov, e, ("fallback al siguiente" if has_more else "sin más proveedores"))
    raise last_err


SUGGEST_IDEAS_SYSTEM = (
    "Eres un experto en contenido de Instagram Reels y TikTok. "
    "El usuario te enviará data de sus reels que mejor han funcionado "
    "(o transcripciones de reels que ha consumido).\n\n"
    "Tu tarea: generar 5-8 ideas (no más, no menos) de NUEVOS reels que "
    "el user podría grabar, inspiradas en patrones de éxito que detectes "
    "en sus datos.\n\n"
    "Para cada idea, devuelve:\n"
    "- title: título corto y atractivo del reel propuesto\n"
    "- hook: primera frase del reel (la que engancha en los primeros 3 segundos)\n"
    "- style: uno de [educativo, storytelling, listas, hooks, controversia, tutorial, comparativa]\n"
    "- inspired_by_index: índice del reel/transcripción que más inspira esta idea (0-indexed)\n"
    "- reasoning: 1 frase corta explicando por qué esta idea conecta con el patrón del user\n\n"
    "Devuelve SOLO JSON válido en este formato exacto:\n"
    '{"ideas":[{"title":"...","hook":"...","style":"...","inspired_by_index":0,"reasoning":"..."}, ...]}'
)


@app.route("/api/ideas/suggest", methods=["POST"])
@require_auth
@limiter.limit("5 per minute;30 per day")
def api_ideas_suggest():
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    user_context = (body.get("user_context") or "").strip()[:1000]

    profile = get_profile(uid)
    plan = profile.get("plan", "free")

    # 1) Verificar costo (3 créditos = 54 cents). Plan pago paga con monthly_usage.
    SUGGEST_COST = 3 * COST_CENTS  # 54 cents
    SUGGEST_USAGE_UNITS = 3
    is_paid_unlimited = plan in ("pro", "creator", "agency")
    if not is_paid_unlimited and (profile.get("credits_cents") or 0) < SUGGEST_COST:
        return jsonify({
            "error": "no_credits",
            "message": "Necesitas 3 créditos para generar ideas. Compra créditos o sube de plan.",
        }), 402

    # 2) Cargar contexto: top 5 ig_videos por views, fallback transcriptions.
    sources = []
    source_kind = None
    try:
        vids_r = db.table("ig_videos").select(
            "ig_video_id, caption, views, likes, comments, published_at, "
            "thumbnail_b64, thumbnail_url, transcription"
        ).eq("user_id", uid).order("views", desc=True).limit(5).execute()
        if vids_r.data and len(vids_r.data) >= 2:
            source_kind = "ig_videos"
            for v in vids_r.data:
                sources.append({
                    "kind": "reel",
                    "id": v.get("ig_video_id") or "",
                    "caption": (v.get("caption") or "")[:600],
                    "transcription": (v.get("transcription") or "")[:1500],
                    "views": int(v.get("views") or 0),
                    "likes": int(v.get("likes") or 0),
                    "comments": int(v.get("comments") or 0),
                    "published_at": v.get("published_at"),
                    "thumbnail_b64": v.get("thumbnail_b64"),
                    "thumbnail_url": v.get("thumbnail_url"),
                })
    except Exception as e:
        logger.warning("suggest: ig_videos query failed user=%s err=%s", uid, e)

    if not sources:
        try:
            tr_r = db.table("transcriptions").select(
                "id, url, platform, text, thumbnail_b64, created_at, views"
            ).eq("user_id", uid).order("created_at", desc=True).limit(5).execute()
            if tr_r.data:
                source_kind = "transcriptions"
                for t in tr_r.data:
                    sources.append({
                        "kind": "transcription",
                        "id": str(t.get("id") or ""),
                        "url": t.get("url") or "",
                        "platform": t.get("platform") or "",
                        "text": (t.get("text") or "")[:1500],
                        "views": int(t.get("views") or 0) if t.get("views") else None,
                        "created_at": t.get("created_at"),
                        "thumbnail_b64": t.get("thumbnail_b64"),
                    })
        except Exception as e:
            logger.warning("suggest: transcriptions query failed user=%s err=%s", uid, e)

    if not sources and not user_context:
        return jsonify({
            "error": "no_context",
            "message": "Transcribe al menos 1 reel primero o cuéntame sobre qué va tu contenido.",
        }), 400

    # 3) Build user prompt — payload JSON estructurado, sin thumbnails (no relevantes para LLM).
    import json as _json_mod
    sources_for_llm = []
    for i, s in enumerate(sources):
        sl = {"index": i, "kind": s["kind"]}
        if s["kind"] == "reel":
            sl["caption"] = s.get("caption", "")
            sl["transcription"] = s.get("transcription", "")
            sl["views"] = s.get("views")
            sl["likes"] = s.get("likes")
            sl["comments"] = s.get("comments")
            sl["published_at"] = s.get("published_at")
        else:
            sl["text"] = s.get("text", "")
            sl["platform"] = s.get("platform", "")
            sl["created_at"] = s.get("created_at")
            if s.get("views") is not None:
                sl["views"] = s["views"]
        sources_for_llm.append(sl)

    user_payload = {
        "sources": sources_for_llm,
        "extra_context": user_context or None,
    }
    user_prompt = (
        "Datos del usuario:\n" + _json_mod.dumps(user_payload, ensure_ascii=False) +
        "\n\nGenera 5-8 ideas siguiendo el formato JSON especificado en el system prompt."
    )

    # 4) Llamar LLM. Si falla → 502 sin deducir créditos.
    # v0.14.26b: max_tokens=4000 (subido desde 3000) — Gemini truncaba JSON con
    # 5-8 ideas + reasoning. develop_idea sin tocar (su default ya es 4000).
    try:
        result = _call_llm_json(
            SUGGEST_IDEAS_SYSTEM, user_prompt,
            max_tokens=4000, temperature=0.8,
        )
    except _json_mod.JSONDecodeError:
        logger.error("suggest: LLM JSON parse failed user=%s", uid)
        try:
            from emails import track as _ph_track
            _ph_track("ideas_generated_failed", uid, {"error": "llm_parse"})
        except Exception:
            pass
        return jsonify({"error": "llm_parse", "message": "Algo falló generando ideas. Vuelve a intentarlo."}), 502
    except Exception as e:
        logger.error("suggest: LLM call failed user=%s err=%s", uid, e, exc_info=True)
        try:
            from emails import track as _ph_track
            _ph_track("ideas_generated_failed", uid, {"error": "llm_error"})
        except Exception:
            pass
        return jsonify({"error": "llm_error", "message": "Algo falló generando ideas. Vuelve a intentarlo."}), 502

    raw_ideas = result.get("ideas") if isinstance(result, dict) else None
    if not isinstance(raw_ideas, list) or not raw_ideas:
        logger.warning("suggest: empty ideas array user=%s result=%s", uid, repr(result)[:200])
        try:
            from emails import track as _ph_track
            _ph_track("ideas_generated_failed", uid, {"error": "llm_empty"})
        except Exception:
            pass
        return jsonify({"error": "llm_empty", "message": "No se pudieron generar ideas. Vuelve a intentarlo."}), 502

    # 5) Enriquecer con metadata del item inspirador. Slice a 8 max (spec 5-8).
    enriched = []
    for idea in raw_ideas[:8]:
        if not isinstance(idea, dict):
            continue
        idx = idea.get("inspired_by_index")
        src_meta = None
        if isinstance(idx, int) and 0 <= idx < len(sources):
            src = sources[idx]
            src_meta = {
                "kind": src["kind"],
                "id": src.get("id"),
                "thumbnail_b64": src.get("thumbnail_b64"),
                "thumbnail_url": src.get("thumbnail_url"),
                "views": src.get("views"),
                "published_at": src.get("published_at") or src.get("created_at"),
            }
        enriched.append({
            "title": (idea.get("title") or "").strip()[:200],
            "hook": (idea.get("hook") or "").strip()[:500],
            "style": (idea.get("style") or "").strip()[:50],
            "reasoning": (idea.get("reasoning") or "").strip()[:300],
            "inspired_by_index": idx if isinstance(idx, int) else None,
            "inspired_by": src_meta,
        })

    if not enriched:
        try:
            from emails import track as _ph_track
            _ph_track("ideas_generated_failed", uid, {"error": "llm_empty_enriched"})
        except Exception:
            pass
        return jsonify({"error": "llm_empty", "message": "No se pudieron generar ideas. Vuelve a intentarlo."}), 502

    # v0.14.26a: warning si el LLM se sale del rango 5-8 (no bloqueamos, devolvemos lo que hay).
    if len(enriched) < 5 or len(enriched) > 8:
        logger.warning("suggest: unexpected count user=%s count=%d (expected 5-8)",
                       uid, len(enriched))

    # 6) Cobrar créditos (SOLO tras parse exitoso).
    try:
        if is_paid_unlimited:
            db.table("profiles").update({
                "monthly_usage": (profile.get("monthly_usage") or 0) + SUGGEST_USAGE_UNITS
            }).eq("id", uid).execute()
        else:
            db.table("profiles").update({
                "credits_cents": (profile.get("credits_cents") or 0) - SUGGEST_COST
            }).eq("id", uid).execute()
    except Exception as e:
        logger.error("suggest: credit deduction failed user=%s err=%s", uid, e, exc_info=True)
        # No bloqueamos — el LLM ya respondió. Loggeamos y seguimos.

    # 7) PostHog
    try:
        from emails import track as _ph_track
        _ph_track("ideas_generated", uid, {
            "count": len(enriched),
            "source": source_kind or "context_only",
            "credits_spent": SUGGEST_USAGE_UNITS,
        })
    except Exception:
        pass

    return jsonify({"ideas": enriched, "source": source_kind or "context_only"})


@app.route("/api/ideas/save", methods=["POST"])
@require_auth
@limiter.limit("30 per minute")
def api_ideas_save():
    user = current_user()
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    hook = (body.get("hook") or "").strip()
    style = (body.get("style") or "").strip()
    inspired_by_id = (body.get("inspired_by_id") or "").strip() or None
    inspired_by_type = (body.get("inspired_by_type") or "").strip() or None

    if len(title) < 3 or len(title) > 200:
        return jsonify({"error": "Title invalid (3-200 chars)"}), 400
    if len(hook) > 500:
        return jsonify({"error": "Hook too long (max 500 chars)"}), 400
    if inspired_by_type and inspired_by_type not in ("reel", "transcription"):
        return jsonify({"error": "Invalid inspired_by_type"}), 400

    raw_text = title if not hook else f"{title} — {hook}"

    row = db.table("ideas").insert({
        "user_id": user["id"],
        "raw_text": raw_text,
        "title": title,
        "hook": hook or None,
        "style": style or None,
        "inspired_by_id": inspired_by_id,
        "inspired_by_type": inspired_by_type,
        "source": "suggestion",
        "status": "developed",
    }).execute()

    try:
        from emails import track as _ph_track
        _ph_track("idea_saved_from_suggestion", user["id"], {
            "style": style or "unknown",
            "inspired_by_type": inspired_by_type or "none",
        })
    except Exception:
        pass

    return jsonify(row.data[0] if row.data else {"ok": True})


@app.route("/api/voice/tone", methods=["POST"])
@require_auth
def set_preset_tone():
    """Bloque A1: fija el TONO preset del usuario (personalidad del primer guion
    cuando aún no hay voz personal). Se guarda en default_idea_assistant — que YA
    es el estilo por defecto de toda la generación. Solo acepta tonos válidos."""
    user = current_user()
    body = request.get_json() or {}
    tone = (body.get("tone") or "").strip().lower()
    if tone not in PRESET_TONE_KEYS:
        return jsonify({"error": "invalid_tone", "options": [t["key"] for t in PRESET_TONES]}), 400
    db.table("profiles").update({"default_idea_assistant": tone}).eq("id", user["id"]).execute()
    return jsonify({"ok": True, "tone": tone})


@app.route("/me/preferences")
@require_auth
def get_preferences():
    user = current_user()
    prof = db.table("profiles").select("default_idea_assistant").eq("id", user["id"]).execute()
    data = prof.data[0] if prof.data else {}
    return jsonify({"default_idea_assistant": data.get("default_idea_assistant")})


@app.route("/me/preferences", methods=["PATCH"])
@require_auth
def update_preferences():
    user = current_user()
    body = request.get_json() or {}
    updates = {}
    if "default_idea_assistant" in body:
        updates["default_idea_assistant"] = body["default_idea_assistant"]
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    db.table("profiles").update(updates).eq("id", user["id"]).execute()
    return jsonify({"ok": True})


# ── Affiliate program ────────────────────────────────────────────────────────

@app.route("/affiliate/apply", methods=["POST"])
@limiter.limit("3 per hour")
def affiliate_apply():
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    handle = (body.get("handle") or "").strip()
    audience = body.get("audience_size", "")

    if not name or not email:
        return jsonify({"error": "Name and email required"}), 400
    if len(name) > 100:
        return jsonify({"error": "Name too long"}), 400
    if not validate_email(email):
        return jsonify({"error": "Invalid email address"}), 400

    # Check if already an affiliate
    existing = db.table("affiliates").select("code, status").eq("email", email).execute()
    if existing.data:
        code = existing.data[0]["code"]
        return jsonify({
            "ok": True, "already_exists": True,
            "code": code, "link": f"{request.host_url}?ref={code}",
        })

    # Generate unique code from handle or name
    base = re.sub(r"[^a-z0-9]", "", (handle or name).lower())[:12]
    if not base:
        base = "creator"
    code = base
    suffix = 1
    while True:
        check = db.table("affiliates").select("id").eq("code", code).execute()
        if not check.data:
            break
        code = f"{base}{suffix}"
        suffix += 1

    db.table("affiliates").insert({
        "name": name, "email": email, "code": code,
        "handle": handle, "audience_size": audience,
        "commission_pct": 30, "status": "active",
    }).execute()

    return jsonify({
        "ok": True, "already_exists": False,
        "code": code, "link": f"{request.host_url}?ref={code}",
    })


@app.route("/affiliate/click", methods=["POST"])
@limiter.limit("30 per minute")
def affiliate_click():
    body = request.get_json()
    code = (body.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False}), 400
    # Increment click count
    aff = db.table("affiliates").select("id, total_clicks").eq("code", code).execute()
    if aff.data:
        db.table("affiliates").update({
            "total_clicks": (aff.data[0].get("total_clicks") or 0) + 1
        }).eq("code", code).execute()
    return jsonify({"ok": True})


@app.route("/affiliate/dashboard")
@require_auth
def affiliate_dashboard_data():
    user = current_user()
    aff = db.table("affiliates").select("*").eq("user_id", user["id"]).execute()
    if not aff.data:
        return jsonify({"error": "Not an affiliate"}), 404

    affiliate = aff.data[0]
    conversions = db.table("affiliate_conversions").select("*").eq(
        "affiliate_code", affiliate["code"]
    ).order("created_at", desc=True).execute()

    total_earned = sum(c.get("commission_cents", 0) for c in conversions.data)
    total_conversions = len(conversions.data)
    total_clicks = affiliate.get("total_clicks", 0)
    conv_rate = round(total_conversions / total_clicks * 100, 1) if total_clicks > 0 else 0

    return jsonify({
        "affiliate": {
            "code": affiliate["code"],
            "name": affiliate.get("name"),
            "status": affiliate.get("status", "active"),
            "commission_pct": affiliate.get("commission_pct", 30),
        },
        "stats": {
            "total_clicks": total_clicks,
            "total_conversions": total_conversions,
            "conversion_rate": conv_rate,
            "total_earned_cents": total_earned,
        },
        "conversions": conversions.data,
    })


# ── GDPR endpoints ───────────────────────────────────────────────────────────

@app.route("/account/export")
@require_auth
def export_account():
    user = current_user()
    profile = db.table("profiles").select("*").eq("id", user["id"]).execute()
    projects = db.table("projects").select("*").eq("user_id", user["id"]).execute()
    scripts = db.table("scripts").select("*").eq("user_id", user["id"]).execute()
    transcriptions = db.table("transcriptions").select("*").eq("user_id", user["id"]).execute()

    payload = {
        "profile": profile.data[0] if profile.data else None,
        "projects": projects.data,
        "scripts": scripts.data,
        "transcriptions": transcriptions.data,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    response = jsonify(payload)
    response.headers["Content-Disposition"] = "attachment; filename=reelscript-data-export.json"
    return response


@app.route("/account", methods=["DELETE"])
@require_auth
@limiter.limit("2 per hour")
def delete_account():
    user = current_user()
    uid = user["id"]

    try:
        profile = db.table("profiles").select("*").eq("id", uid).execute()
        if not profile.data:
            return jsonify({"error": "Profile not found"}), 404

        prof = profile.data[0]

        # 1. Audit log
        db.table("deletion_log").insert({
            "user_id": uid,
            "email": user.get("email"),
        }).execute()

        # 2. Cancel Stripe subscription if exists
        sub_id = prof.get("stripe_subscription_id")
        if sub_id and STRIPE_OK:
            try:
                stripe_lib.Subscription.delete(sub_id)
            except Exception as e:
                logger.error(f"Stripe cancellation failed for {uid}: {e}")
                return jsonify({"error": "Could not cancel subscription. Contact support."}), 500

        # 3. Delete user data (cascade should handle most, but be explicit)
        db.table("assistants").delete().eq("user_id", uid).execute()
        db.table("scripts").delete().eq("user_id", uid).execute()
        db.table("projects").delete().eq("user_id", uid).execute()
        db.table("transcriptions").delete().eq("user_id", uid).execute()
        db.table("agency_members").delete().eq("agency_owner_id", uid).execute()
        db.table("profiles").delete().eq("id", uid).execute()

        # 4. Delete auth user
        db.auth.admin.delete_user(uid)

        # 5. Clear session
        session.clear()

        return jsonify({"ok": True})

    except Exception as e:
        logger.error(f"Account deletion failed for {uid}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error. Contact support."}), 500


# ── Main ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    accept = request.headers.get("Accept-Language", "")
    if accept.lower().startswith("en"):
        return redirect("/en/", code=302)
    return redirect("/es/", code=302)


LANG_COOKIE = "rs_lang"
SUPPORTED_LANGS = ("es", "en")


@app.before_request
def _resolve_lang_setup():
    """Resolve lang for every request. Order: ?lang= > cookie > Accept-Language.
    If ?lang= is set, mark for cookie persist via after_request hook."""
    q = (request.args.get("lang") or "").lower()
    if q in SUPPORTED_LANGS:
        g.lang = q
        g.persist_lang = True
        return
    c = request.cookies.get(LANG_COOKIE)
    if c in SUPPORTED_LANGS:
        g.lang = c
    else:
        accept = request.headers.get("Accept-Language", "")
        g.lang = "en" if accept.lower().startswith("en") else "es"
    g.persist_lang = False


@app.after_request
def _persist_lang_cookie(resp):
    """Persist resolved lang in cookie when querystring chose it."""
    if getattr(g, "persist_lang", False) and getattr(g, "lang", None) in SUPPORTED_LANGS:
        resp.set_cookie(LANG_COOKIE, g.lang, max_age=365 * 24 * 3600, samesite="Lax")
    return resp


def _resolve_lang():
    """Return lang resolved by before_request hook. Falls back if hook didn't run."""
    lang = getattr(g, "lang", None)
    if lang in SUPPORTED_LANGS:
        return lang
    c = request.cookies.get(LANG_COOKIE)
    if c in SUPPORTED_LANGS:
        return c
    accept = request.headers.get("Accept-Language", "")
    return "en" if accept.lower().startswith("en") else "es"


def _lang_cookie_response(resp, lang):
    resp.set_cookie(LANG_COOKIE, lang, max_age=365 * 24 * 3600, samesite="Lax")
    return resp


@app.route("/es/")
def index_es():
    resp = make_response(render_template("index.html", lang="es",
                                         next_url=safe_next_url(request.args.get("next"))))
    return _lang_cookie_response(resp, "es")


@app.route("/en/")
def index_en():
    resp = make_response(render_template("index.html", lang="en",
                                         next_url=safe_next_url(request.args.get("next"))))
    return _lang_cookie_response(resp, "en")


@app.route("/app")
@require_auth_html
def workspace():
    # v0.16.x: la home pasa a ser el Radar (isla). /app → /profile/radar para que
    # TODO entry-point post-auth (login email, OAuth, botón «Ir a mi app», fallback
    # ?next) caiga en la isla. En /profile/radar, refreshMe enruta seg=radar →
    # coincide con el profTab("radar") que openProfile ya fuerza → SIN la race
    # overview↔radar que dejaba al usuario en un panel legacy vacío.
    return redirect("/profile/radar", code=302)


@app.route("/settings")
@require_auth_html
def settings_page():
    return render_template("index.html", lang=_resolve_lang(), settings_page=True)


PROFILE_SECTIONS = {
    "overview", "scripts", "projects", "ideas", "metrics",
    "assistants", "team", "transcriptions", "privacy", "settings",
}


@app.route("/profile")
@app.route("/profile/<section>")
@require_auth_html
def profile_page(section="overview"):
    # Render the workspace shell; the frontend reads location.pathname and
    # auto-opens the profile drawer to the correct tab. A 301 to /app would
    # lose the route on refresh and dump users on the dashboard.
    if section not in PROFILE_SECTIONS:
        section = "overview"
    return render_template("index.html", lang=_resolve_lang(), workspace=True)


# ── Pillar pages ─────────────────────────────────────────────────────────────

PILLAR_PAGES = {
    "es": {
        "transcribir-reel-instagram": {
            "title": "Transcribir reel de Instagram gratis · ReelScript",
            "description": "Pega el link de cualquier reel de Instagram y saca el texto en 10 segundos. Gratis, sin tarjeta, sin registro.",
            "canonical": "https://reelscript.net/es/transcribir-reel-instagram",
            "alt": "en/instagram-reel-transcript",
            "h2_seo": "Transcribe un reel de Instagram en 10 segundos",
            "how_to_label": "Cómo hacerlo",
            "faq_label": "Preguntas frecuentes",
            "cta_label": "Probar ahora →",
            "intro_paragraphs": [
                "Grabas un reel que funciona. Lo subes. Fin. Mal. Ese audio vale para cinco cosas más y lo estás tirando: un post de LinkedIn, un hilo, cinco hooks para grabar mañana, o leerlo en el teleprompter para el siguiente video. Todo está ahí, en el audio que ya tienes.",
                "ReelScript es un gestor de contenido para creadores. Transcribes el reel, lo reformateas con IA en el estilo que quieras y guardas todo en proyectos organizados. La transcripción la hace Whisper — el modelo de OpenAI, lo mejor que hay para esto. No vamos a ponernos medallas por algo que no hemos inventado nosotros. Jaja.",
                "Pegas el link. Pulsas transcribir. En 10 segundos tienes el guión completo. Luego decides: viral, LinkedIn, historia, hooks, o leerlo en el teleprompter integrado para grabar el siguiente sin improvisar. Un click por cosa.",
            ],
            "how_to_steps": [
                "Copia el link del reel de Instagram desde la app (Compartir → Copiar link) o desde el navegador.",
                "Pégalo en el campo de arriba y pulsa «Transcribir».",
                "En 10 segundos tienes el texto completo. Cópialo, reformatéalo con IA o ábrelo en el teleprompter.",
            ],
            "faq": [
                {"q": "¿Funciona con reels privados o de cuentas privadas?", "a": "No. Solo reels públicos. Si el perfil o el reel están en privado, no podemos acceder al audio."},
                {"q": "¿La transcripción tiene marca de agua o límite de caracteres?", "a": "No. El texto es tuyo. Sin marca de agua, sin cortes. Lo que devuelve Whisper es lo que ves."},
                {"q": "¿Qué más puedo hacer con el texto, además de copiarlo?", "a": "Reformatearlo con IA (viral, LinkedIn, historia, 5 hooks), guardarlo en un proyecto, asignarle un asistente con tu estilo personal, añadir métricas de rendimiento o leerlo en el teleprompter integrado para grabar el siguiente video."},
                {"q": "¿En qué idiomas funciona?", "a": "En los que soporta Whisper — más de 90. Español, inglés, francés, portugués, alemán, italiano, japonés... Si el reel está en ese idioma, lo transcribe."},
                {"q": "¿Es gratis?", "a": "Sí. 5 transcripciones gratis al día sin registrarte. Con cuenta gratuita (sin tarjeta) sube a 250 al mes."},
            ],
            "closing": "Eso es todo. Arriba tienes el input.",
        },
        "transcribir-tiktok": {
            "title": "Transcribir TikTok a texto gratis · ReelScript",
            "description": "Pega el link de cualquier TikTok y obtén la transcripción completa en segundos. Sin registro, sin tarjeta.",
            "canonical": "https://reelscript.net/es/transcribir-tiktok",
            "alt": "en/tiktok-to-text",
            "h2_seo": "Transcribe cualquier TikTok a texto en segundos",
            "how_to_label": "Cómo hacerlo",
            "faq_label": "Preguntas frecuentes",
            "cta_label": "Probar ahora →",
            "intro_paragraphs": [
                "Hay TikToks que explican algo en 60 segundos que llevaría 600 palabras escribir. Ese texto existe — está en el audio. Lo que no tienes es tiempo para transcribirlo a mano. Nadie lo tiene, para ser honestos.",
                "ReelScript no es solo una herramienta de transcripción — es donde gestionas todo tu contenido. Pegas el link del TikTok, en 10 segundos tienes el texto, y desde ahí decides qué hacer: copiarlo, reformatearlo con IA, guardarlo en un proyecto o cargarlo en el teleprompter para grabarte a ti mismo leyendo el guión sin tener que memorizar nada.",
                "Funciona con cualquier TikTok público. La transcripción la hace Whisper, soporta más de 90 idiomas y la precisión es alta si el audio es claro.",
            ],
            "how_to_steps": [
                "Abre el TikTok en la app o el navegador. Pulsa «Compartir» → «Copiar link».",
                "Pega el link en el campo de arriba y pulsa «Transcribir».",
                "Tienes el texto. Cópialo, reformatéalo con IA o ábrelo en el teleprompter.",
            ],
            "faq": [
                {"q": "¿Funciona con TikToks privados?", "a": "No. Solo TikToks públicos. Si la cuenta o el video está en privado, no podemos acceder al audio."},
                {"q": "¿Qué pasa si el TikTok no tiene voz, solo música?", "a": "La transcripción saldrá vacía o con ruido. Whisper transcribe voz humana — si no hay voz, no hay texto útil."},
                {"q": "¿Puedo guardar los textos transcritos?", "a": "Sí. Si tienes cuenta, todos los textos se guardan en tu historial y puedes organizarlos en proyectos. Con cuenta gratuita ya tienes acceso a esto."},
                {"q": "¿Hay límite de duración del video?", "a": "Los TikToks normalmente son cortos, así que no es un problema real. Para videos muy largos la transcripción puede tardar más."},
                {"q": "¿Es gratis?", "a": "Sí. 5 transcripciones gratis al día sin cuenta. Con cuenta gratuita, 250 al mes."},
            ],
            "closing": "El input está arriba. Tarda menos en probarlo que en seguir leyendo esto.",
        },
        "reel-a-linkedin": {
            "title": "Convertir reel a post de LinkedIn con IA · ReelScript",
            "description": "Transcribe cualquier reel o video y conviértelo en un post de LinkedIn con un click. IA que escribe como tú.",
            "canonical": "https://reelscript.net/es/reel-a-linkedin",
            "alt": "en/reel-to-linkedin",
            "h2_seo": "Del reel al post de LinkedIn en dos clicks",
            "how_to_label": "Cómo hacerlo",
            "faq_label": "Preguntas frecuentes",
            "cta_label": "Probar ahora →",
            "intro_paragraphs": [
                "Grabas un video para Instagram o TikTok. Funciona. Y luego no lo reutilizas en LinkedIn porque convertir el audio en un post profesional lleva tiempo que no tienes. Es una pena, porque el mensaje ya lo validaste — solo cambia el canal.",
                "ReelScript lo hace en dos pasos. Primero transcribe el reel — 10 segundos. Luego le dices «LinkedIn» y la IA lo reformatea: tono reflexivo, párrafos cortos, pregunta al final. El texto es tuyo, la IA solo lo ordena. Si tienes un asistente configurado con tu estilo, lo aplicará automáticamente.",
                "Y si quieres leer el guión en cámara antes de publicarlo en LinkedIn, el teleprompter integrado te lo muestra a la velocidad que necesitas. Sin memorizar, sin improvisar.",
            ],
            "how_to_steps": [
                "Pega el link del reel arriba y transcríbelo.",
                "En el panel «Hazlo tuyo», selecciona el estilo «LinkedIn».",
                "La IA reformatea el guión con tono profesional. Cópialo y publícalo, o ábrelo en el teleprompter.",
            ],
            "faq": [
                {"q": "¿El post de LinkedIn suena a IA?", "a": "Depende de tu guión original. La IA reformatea lo que ya dijiste en el video — si tu voz es auténtica en el reel, el post también lo será."},
                {"q": "¿Puedo guardar el resultado en un proyecto?", "a": "Sí. Puedes guardar cualquier guión en proyectos organizados, asignarle métricas de rendimiento y rastrear qué contenido funciona mejor."},
                {"q": "¿Puedo ajustar el tono antes de publicar?", "a": "Sí. El texto que devuelve la IA es editable. Cópialo y ajusta lo que no encaje con tu estilo — o crea un asistente personalizado para que siempre salga como quieres."},
                {"q": "¿Qué pasa si el reel es muy corto, de 15 segundos?", "a": "Sale un post corto. Un reel de 15 segundos bien grabado puede dar un post de LinkedIn perfectamente legible."},
            ],
            "closing": "El reel ya lo tienes. El post tarda 10 segundos más. Arriba el input.",
        },
        "hooks-desde-reel": {
            "title": "Generar hooks desde un reel con IA · ReelScript",
            "description": "Extrae 5 hooks de apertura de cualquier reel o video en un click. Para Instagram, TikTok, YouTube Shorts.",
            "canonical": "https://reelscript.net/es/hooks-desde-reel",
            "alt": "en/hooks-from-video",
            "h2_seo": "5 hooks de apertura desde cualquier reel, en un click",
            "how_to_label": "Cómo hacerlo",
            "faq_label": "Preguntas frecuentes",
            "cta_label": "Probar ahora →",
            "intro_paragraphs": [
                "El hook decide si alguien sigue viendo o no. Los primeros 2 segundos. Y la mayoría de creadores los improvisan, los repiten o los copian de otros sin entender por qué funcionan. Normal, es lo más difícil de escribir.",
                "Si tienes un reel que funcionó — retención, comentarios, guardados — el hook ya está en el guión. ReelScript lo transcribe y genera 5 variaciones de apertura distintas: pregunta directa, dato, confesión, provocación, promesa concreta. Tú eliges cuál graba mañana. Y si quieres leerlo sin improvisar, lo abres en el teleprompter y listo.",
                "No es escribir hooks desde cero. Es extraer los que ya funcionaron y multiplicarlos. ReelScript guarda todo en proyectos para que puedas ver qué hooks tienen mejor rendimiento a lo largo del tiempo.",
            ],
            "how_to_steps": [
                "Transcribe el reel que quieras usar como base.",
                "En «Hazlo tuyo», selecciona el estilo «5 hooks».",
                "La IA te devuelve 5 aperturas distintas. Guarda las que quieras o ábrelas en el teleprompter.",
            ],
            "faq": [
                {"q": "¿Los hooks son genéricos o específicos al video?", "a": "Específicos. La IA trabaja con el guión transcrito — si el reel habla de edición de video, los hooks hablan de edición de video."},
                {"q": "¿Puedo usar hooks de un video de otra persona como referencia?", "a": "Puedes transcribir cualquier reel público y ver cómo estructura los hooks. Lo que hagas con ellos es tu responsabilidad."},
                {"q": "¿Para qué formatos sirven?", "a": "Para cualquier video corto: Instagram Reels, TikTok, YouTube Shorts. También sirven como primera frase de un post o hilo."},
                {"q": "¿Puedo grabarme leyendo los hooks?", "a": "Sí. Puedes abrir cualquier guión en el teleprompter integrado de ReelScript y grabarte leyéndolo a cámara, sin cortes ni improvisar."},
                {"q": "¿Cuántos hooks genera?", "a": "5 por defecto. Suficientes para tener variedad sin saturarte."},
            ],
            "closing": "Los hooks de tus próximas grabaciones están en los reels que ya tienes. Arriba el input.",
        },
        "transcribir-audio-video": {
            "title": "Transcribir audio de video a texto gratis · ReelScript",
            "description": "Convierte el audio de cualquier reel o video a texto en segundos. Compatible con Instagram y TikTok. Sin instalar nada.",
            "canonical": "https://reelscript.net/es/transcribir-audio-video",
            "alt": "en/free-video-transcription",
            "h2_seo": "Convierte el audio de cualquier video a texto",
            "how_to_label": "Cómo hacerlo",
            "faq_label": "Preguntas frecuentes",
            "cta_label": "Probar ahora →",
            "intro_paragraphs": [
                "Transcribir audio de video a mano tarda lo que dura el video, más el tiempo de tipeo. Para un reel de 60 segundos, 5 minutos. Para varios al día, una tarde. Es tiempo que ningún creador tiene — ni debería gastar en esto.",
                "ReelScript coge el link, extrae el audio y lo pasa por Whisper. En 10 segundos tienes el texto. Pero no es solo transcripción — es el punto de partida de tu gestión de contenido. Desde el texto puedes reformatear con IA, organizar en proyectos, crear guiones para futuras grabaciones y leerlos en el teleprompter integrado.",
                "Compatible con Instagram Reels y TikTok. Sin instalar nada, sin subir archivos.",
            ],
            "how_to_steps": [
                "Copia el link del reel o TikTok que quieres transcribir.",
                "Pégalo en el campo de arriba y pulsa «Transcribir».",
                "Tienes el texto del audio en segundos. Cópialo, trabájalo con IA o ábrelo en el teleprompter.",
            ],
            "faq": [
                {"q": "¿Funciona con cualquier tipo de video?", "a": "Con reels de Instagram y TikToks públicos. No con YouTube (de momento), ni con archivos locales."},
                {"q": "¿Qué precisión tiene la transcripción?", "a": "Alta, si el audio es claro. Whisper supera el 95% de precisión en condiciones normales. Con ruido de fondo o música alta puede cometer errores."},
                {"q": "¿Puedo organizar los textos transcritos?", "a": "Sí. Con cuenta gratuita puedes guardarlos en proyectos, asignarles asistentes con tu estilo y rastrear métricas de rendimiento."},
                {"q": "¿Necesito instalar algo?", "a": "No. Funciona en el navegador. Sin extensiones, sin apps."},
                {"q": "¿Es gratis?", "a": "Sí. 5 transcripciones gratis al día sin cuenta. Con cuenta gratuita, 250 al mes sin tarjeta."},
            ],
            "closing": "Nada más que decir. El input está arriba.",
        },
    },
    "en": {
        "instagram-reel-transcript": {
            "title": "Instagram Reel Transcript — Free Online · ReelScript",
            "description": "Paste any Instagram reel URL and get the full transcript in 10 seconds. Free, no card, no signup.",
            "canonical": "https://reelscript.net/en/instagram-reel-transcript",
            "alt": "es/transcribir-reel-instagram",
            "h2_seo": "Transcribe any Instagram reel in 10 seconds",
            "how_to_label": "How to do it",
            "faq_label": "Frequently asked questions",
            "cta_label": "Try it now →",
            "intro_paragraphs": [
                "You record a reel that works. You post it. Done. That audio is worth five more things and you're throwing it away: a LinkedIn post, a thread, five hooks for tomorrow's recording, or reading it on the teleprompter to nail the next one without improvising.",
                "ReelScript is a content manager for creators. You transcribe the reel, reformat it with AI in whatever style you want, and save everything in organized projects. The transcription runs on Whisper — OpenAI's model, the best thing out there for this. Not taking credit for that. Ha.",
                "Paste the link. Hit transcribe. In 10 seconds you have the full script. Then pick: viral, LinkedIn, story, hooks, or load it into the built-in teleprompter to record the next video without winging it. One click per thing.",
            ],
            "how_to_steps": [
                "Copy the Instagram reel link from the app (Share → Copy link) or your browser.",
                "Paste it into the field above and hit «Transcribe».",
                "In 10 seconds you have the full text. Copy it, reformat with AI, or open it in the teleprompter.",
            ],
            "faq": [
                {"q": "Does it work with private reels or private accounts?", "a": "No. Public reels only. If the profile or reel is private, we can't access the audio."},
                {"q": "Does the transcript have a watermark or character limit?", "a": "No. The text is yours. No watermark, no cuts. What Whisper returns is what you see."},
                {"q": "What else can I do with the text besides copy it?", "a": "Reformat it with AI (viral, LinkedIn, story, 5 hooks), save it in a project, assign a custom assistant with your style, add performance metrics, or read it in the built-in teleprompter to record your next video."},
                {"q": "What languages does it support?", "a": "Any language Whisper supports — over 90. Spanish, English, French, Portuguese, German, Italian, Japanese... If the reel is in that language, it transcribes it."},
                {"q": "Is it free?", "a": "Yes. 5 free transcriptions per day without signing up. With a free account (no card) it goes up to 250 per month."},
            ],
            "closing": "That's it. The input is above.",
        },
        "tiktok-to-text": {
            "title": "TikTok to Text — Free Transcript · ReelScript",
            "description": "Paste any TikTok URL and get the full transcript in seconds. No signup, no credit card.",
            "canonical": "https://reelscript.net/en/tiktok-to-text",
            "alt": "es/transcribir-tiktok",
            "h2_seo": "Convert any TikTok to text in seconds",
            "how_to_label": "How to do it",
            "faq_label": "Frequently asked questions",
            "cta_label": "Try it now →",
            "intro_paragraphs": [
                "Some TikToks explain in 60 seconds what would take 600 words to write. That text exists — it's in the audio. What you don't have is time to transcribe it by hand. Nobody does, honestly.",
                "ReelScript isn't just a transcription tool — it's where you manage your content. Paste the TikTok link, in 10 seconds you have the text, and from there: copy it, reformat with AI, save it in a project, or load it into the teleprompter to record yourself reading the script without memorizing a thing.",
                "Works with any public TikTok. Transcription runs on Whisper, supports 90+ languages, high accuracy if the audio is clear.",
            ],
            "how_to_steps": [
                "Open the TikTok in the app or browser. Tap Share → Copy link.",
                "Paste the link in the field above and hit Transcribe.",
                "You have the text. Copy it, reformat with AI, or open it in the teleprompter.",
            ],
            "faq": [
                {"q": "Does it work with private TikToks?", "a": "No. Public TikToks only. If the account or video is private, we can't access the audio."},
                {"q": "What if the TikTok has no voice, just music?", "a": "The transcript will be empty or garbled. Whisper transcribes human speech — no voice, no useful text."},
                {"q": "Can I save the transcripts?", "a": "Yes. With an account, all texts are saved in your history and you can organize them in projects. Free account gets you this."},
                {"q": "Is there a video length limit?", "a": "TikToks are usually short, not a real issue. Very long videos may take a bit longer to process."},
                {"q": "Is it free?", "a": "Yes. 5 free transcriptions per day without an account. With a free account, 250 per month."},
            ],
            "closing": "The input is above. Faster to try than to keep reading.",
        },
        "reel-to-linkedin": {
            "title": "Reel to LinkedIn Post with AI · ReelScript",
            "description": "Transcribe any reel or video and turn it into a LinkedIn post in one click. AI that writes like you.",
            "canonical": "https://reelscript.net/en/reel-to-linkedin",
            "alt": "es/reel-a-linkedin",
            "h2_seo": "From reel to LinkedIn post in two clicks",
            "how_to_label": "How to do it",
            "faq_label": "Frequently asked questions",
            "cta_label": "Try it now →",
            "intro_paragraphs": [
                "You record a video for Instagram or TikTok. It works. Gets engagement. And then you don't repurpose it on LinkedIn because turning audio into a proper post takes time you don't have. Which is a shame — you already validated the message. Just changing the channel.",
                "ReelScript does it in two steps. Transcribe the reel — 10 seconds. Tell it «LinkedIn» — the AI reformats it: reflective tone, short paragraphs, question at the end. Your words, the AI just structures them. If you have a custom assistant set up with your style, it applies it automatically.",
                "And if you want to read the script on camera before publishing, the built-in teleprompter shows it at whatever speed you need. No memorizing, no winging it.",
            ],
            "how_to_steps": [
                "Paste the reel link above and transcribe it.",
                "In the «Make it yours» panel, select the «LinkedIn» style.",
                "The AI reformats the script. Copy and publish, or open it in the teleprompter.",
            ],
            "faq": [
                {"q": "Does the LinkedIn post sound like AI?", "a": "Depends on your original script. The AI reformats what you already said — if your voice is authentic in the reel, the post will be too."},
                {"q": "Can I save the result in a project?", "a": "Yes. You can save any script in organized projects, add performance metrics, and track what content performs best over time."},
                {"q": "Can I edit the tone before publishing?", "a": "Yes. The text the AI returns is fully editable. Or create a custom assistant so it always comes out how you want."},
                {"q": "What if the reel is very short, like 15 seconds?", "a": "You get a short post. A well-recorded 15-second reel can produce a perfectly readable LinkedIn post."},
            ],
            "closing": "You already have the reel. The post takes 10 more seconds. Input is above.",
        },
        "hooks-from-video": {
            "title": "Generate Video Hooks with AI · ReelScript",
            "description": "Extract 5 opening hooks from any reel or video in one click. For Instagram, TikTok, YouTube Shorts.",
            "canonical": "https://reelscript.net/en/hooks-from-video",
            "alt": "es/hooks-desde-reel",
            "h2_seo": "5 opening hooks from any reel, in one click",
            "how_to_label": "How to do it",
            "faq_label": "Frequently asked questions",
            "cta_label": "Try it now →",
            "intro_paragraphs": [
                "The hook decides whether someone keeps watching or not. The first 2 seconds. Most creators improvise them, repeat them, or copy them from others without understanding why they work. Fair enough — it's the hardest thing to write.",
                "If you have a reel that performed — good retention, comments, saves — the hook is already in the script. ReelScript transcribes it and generates 5 different opening variations: direct question, surprising stat, confession, provocation, concrete promise. You pick which one you record tomorrow. Want to read it without improvising? Open it in the teleprompter and go.",
                "Not writing hooks from scratch. Extracting the ones that already worked and multiplying them. ReelScript saves everything in projects so you can track which hooks get the best results over time.",
            ],
            "how_to_steps": [
                "Transcribe the reel you want to use as a base.",
                "In «Make it yours», select the «5 hooks» style.",
                "The AI returns 5 different openings. Save the ones you like or open them in the teleprompter.",
            ],
            "faq": [
                {"q": "Are the hooks generic or specific to the video?", "a": "Specific. The AI works with your transcribed script — if the reel is about video editing, the hooks are about video editing."},
                {"q": "Can I use hooks from someone else's video as reference?", "a": "You can transcribe any public reel and see how their hooks are structured. What you do with them is on you."},
                {"q": "What formats are these hooks for?", "a": "Any short video: Instagram Reels, TikTok, YouTube Shorts. Also work as the first line of a post or thread."},
                {"q": "Can I record myself reading the hooks?", "a": "Yes. Open any script in ReelScript's built-in teleprompter and record yourself reading it on camera. No cuts, no winging it."},
                {"q": "How many hooks does it generate?", "a": "5 by default. Enough variety without overwhelming you."},
            ],
            "closing": "The hooks for your next recordings are in the reels you already have. Input is above.",
        },
        "free-video-transcription": {
            "title": "Free Video Transcription Online · ReelScript",
            "description": "Convert audio from any reel or video to text in seconds. Works with Instagram and TikTok. No install needed.",
            "canonical": "https://reelscript.net/en/free-video-transcription",
            "alt": "es/transcribir-audio-video",
            "h2_seo": "Convert any video audio to text",
            "how_to_label": "How to do it",
            "faq_label": "Frequently asked questions",
            "cta_label": "Try it now →",
            "intro_paragraphs": [
                "Transcribing video audio by hand takes as long as the video, plus typing time. For a 60-second reel, 5 minutes. For several a day, an afternoon. Time no creator has — or should spend on this.",
                "ReelScript takes the link, pulls the audio, runs it through Whisper. In 10 seconds you have the text. But it's not just transcription — it's the starting point for your content management. From the text: reformat with AI, organize in projects, build scripts for future recordings, and read them in the built-in teleprompter.",
                "Works with public Instagram Reels and TikTok. No install, no file uploads.",
            ],
            "how_to_steps": [
                "Copy the reel or TikTok link you want to transcribe.",
                "Paste it in the field above and hit Transcribe.",
                "You have the audio text in seconds. Copy it, work it with AI, or open it in the teleprompter.",
            ],
            "faq": [
                {"q": "Does it work with any video type?", "a": "With public Instagram Reels and TikToks. Not YouTube (for now), not local files."},
                {"q": "How accurate is the transcription?", "a": "High, if the audio is clear. Whisper exceeds 95% accuracy under normal conditions. Heavy background noise or music can cause errors."},
                {"q": "Can I organize the transcribed texts?", "a": "Yes. With a free account you can save them in projects, assign custom assistants, and track performance metrics."},
                {"q": "Do I need to install anything?", "a": "No. Works in the browser. No extensions, no apps."},
                {"q": "Is it free?", "a": "Yes. 5 free transcriptions per day without an account. With a free account, 250 per month, no card required."},
            ],
            "closing": "Nothing more to say. The input is above.",
        },
    },
}


@app.route("/<lang>/<slug>")
def pillar_page(lang, slug):
    if lang not in ("es", "en"):
        abort(404)
    page_data = PILLAR_PAGES.get(lang, {}).get(slug)
    if not page_data:
        abort(404)
    return render_template("index.html", pillar=page_data, lang=lang)


@app.route("/sitemap.xml")
def sitemap():
    BASE = "https://reelscript.net"
    # Home pages
    urls = [
        {"loc": f"{BASE}/es/", "priority": "1.0", "freq": "weekly",
         "hreflang_es": f"{BASE}/es/", "hreflang_en": f"{BASE}/en/"},
        {"loc": f"{BASE}/en/", "priority": "1.0", "freq": "weekly",
         "hreflang_es": f"{BASE}/es/", "hreflang_en": f"{BASE}/en/"},
    ]
    # Pillar pages with cross-language hreflang
    for lang, pages in PILLAR_PAGES.items():
        for slug, data in pages.items():
            alt = data.get("alt", "")
            alt_lang = "en" if lang == "es" else "es"
            urls.append({
                "loc": f"{BASE}/{lang}/{slug}",
                "priority": "0.9",
                "freq": "monthly",
                "hreflang_es": f"{BASE}/es/{slug}" if lang == "es" else f"{BASE}/{alt}",
                "hreflang_en": f"{BASE}/en/{slug}" if lang == "en" else f"{BASE}/{alt}",
            })

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
    xml += 'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
    for u in urls:
        xml += "  <url>\n"
        xml += f"    <loc>{u['loc']}</loc>\n"
        xml += f"    <changefreq>{u['freq']}</changefreq>\n"
        xml += f"    <priority>{u['priority']}</priority>\n"
        xml += f'    <xhtml:link rel="alternate" hreflang="es" href="{u["hreflang_es"]}"/>\n'
        xml += f'    <xhtml:link rel="alternate" hreflang="en" href="{u["hreflang_en"]}"/>\n'
        xml += f'    <xhtml:link rel="alternate" hreflang="x-default" href="{BASE}/"/>\n'
        xml += "  </url>\n"
    xml += "</urlset>"
    return Response(xml, mimetype="application/xml")


# ── Metrics (Instagram analytics) ────────────────────────────────────────────

METRICS_LIMITS = {
    "free":    {"analyses_per_week": 1,    "videos_per_analysis": 5},
    "pro":     {"analyses_per_week": 2,    "videos_per_analysis": 10},
    "creator": {"analyses_per_week": 15,   "videos_per_analysis": 20},
    "agency":  {"analyses_per_week": None, "videos_per_analysis": 20},
}


def _metrics_week_reset(profile: dict) -> dict:
    """Reset weekly metrics counter if current Monday UTC has passed."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)

    reset_at = profile.get("metrics_week_reset_at")
    if reset_at:
        if isinstance(reset_at, str):
            try:
                reset_dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
            except ValueError:
                reset_dt = monday
        else:
            reset_dt = reset_at
        if reset_dt >= monday:
            return profile
    next_monday = monday + timedelta(days=7)
    db.table("profiles").update({
        "metrics_analyses_this_week": 0,
        "metrics_week_reset_at": next_monday.isoformat(),
    }).eq("id", profile["id"]).execute()
    profile["metrics_analyses_this_week"] = 0
    profile["metrics_week_reset_at"] = next_monday.isoformat()
    return profile


def _check_metrics_limit(profile: dict) -> tuple[bool, str | None]:
    plan = profile.get("plan", "free")
    limits = METRICS_LIMITS.get(plan, METRICS_LIMITS["free"])
    max_per_week = limits["analyses_per_week"]
    if max_per_week is None:
        return True, None
    profile = _metrics_week_reset(profile)
    used = profile.get("metrics_analyses_this_week", 0)
    if used >= max_per_week:
        return False, f"Has alcanzado el límite de {max_per_week} análisis/semana de tu plan."
    return True, None


def _download_thumbnail_b64(url: str) -> str | None:
    """Download image URL and return as small base64 JPEG (~5-10KB)."""
    if not url:
        return None
    try:
        from PIL import Image
        from io import BytesIO
        import base64
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content))
        img.thumbnail((320, 400))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.warning(f"[IG-METRICS] Thumbnail download failed: {e}")
        return None


def _scrape_ig_reels(username_or_urls: list[str], limit: int = 10) -> list[dict]:
    """Call Apify instagram-reel-scraper and return normalized items."""
    actor_url = (
        f"https://api.apify.com/v2/acts/xMc5Ga1oCONPmWJIa"
        f"/run-sync-get-dataset-items?token={APIFY_TOKEN}&memory=512"
    )
    payload = {
        "username": username_or_urls,
        "resultsLimit": limit,
        "includeSharesCount": True,
    }
    logger.info(f"[IG-METRICS] Apify request — url: {actor_url.split('?')[0]}, payload: {payload}")
    resp = requests.post(
        actor_url,
        json=payload,
        timeout=180,
    )
    logger.info(f"[IG-METRICS] Apify response — status: {resp.status_code}, body (first 1000): {resp.text[:1000]}")
    resp.raise_for_status()
    items = resp.json()
    results = []
    for item in items:
        sc = item.get("shortCode") or item.get("id", "")
        if not sc:
            continue
        display_url = item.get("displayUrl", "")
        results.append({
            "ig_video_id": sc,
            "ig_url": item.get("url", ""),
            "caption": (item.get("caption") or "")[:2000],
            "thumbnail_url": display_url,
            "thumbnail_b64": _download_thumbnail_b64(display_url),
            "views": item.get("videoPlayCount") or item.get("videoViewCount") or 0,
            "likes": item.get("likesCount", 0) or 0,
            "comments": item.get("commentsCount", 0) or 0,
            "shares": item.get("sharesCount", 0) or 0,
            "published_at": item.get("timestamp"),
            "duration": item.get("videoDuration"),
        })
    return results


@app.route("/api/onboarding/ig-avatar", methods=["GET"])
@require_auth
@limiter.limit("12 per minute")
def onboarding_ig_avatar():
    """Foto de perfil de IG del usuario para el paso «este eres tú» del onboarding
    (David/Bernat 24-jun). Scrapea el perfil con Apify (resultsType=details, 1 item),
    baja la foto a base64 (evita CORS/caducidad del CDN de IG). Best-effort: si Apify
    falla o el perfil es privado → avatar=None y el front cae a las iniciales."""
    handle = (request.args.get("handle") or "").strip().lstrip("@").lower()
    if not re.match(r"^[a-z0-9._]{1,30}$", handle):
        return jsonify({"avatar": None, "error": "bad_handle"}), 400
    token = os.environ.get("APIFY_TOKEN", "")
    if not token:
        return jsonify({"avatar": None}), 200
    try:
        actor_url = ("https://api.apify.com/v2/acts/apify~instagram-scraper"
                     "/run-sync-get-dataset-items?token=" + token + "&memory=256")
        resp = requests.post(actor_url, json={
            "directUrls": ["https://www.instagram.com/" + handle + "/"],
            "resultsType": "details", "resultsLimit": 1,
        }, timeout=45)
        resp.raise_for_status()
        items = resp.json() or []
        item = items[0] if items else {}
        from tasks import _download_thumbnail_b64  # lazy (rompe circular)
        pic = (item.get("profilePicUrlHD") or item.get("profilePicUrl")
               or item.get("profile_pic_url_hd") or item.get("profile_pic_url"))
        b64 = None
        if pic:
            try:
                b64 = _download_thumbnail_b64(pic)
            except Exception as e:
                logger.warning("ig-avatar: b64 fetch failed handle=%s err=%s", handle, e)
        # REELS del propio perfil (paso «tus vídeos» del onboarding): salen del MISMO scrape
        # (latestPosts) → no esperamos al /metrics/analyze lento. Top 3 vídeos por views.
        reels = []
        try:
            posts = item.get("latestPosts") or item.get("latest_posts") or []
            vids = [p for p in posts if isinstance(p, dict) and (
                p.get("type") in ("Video", "Reel") or p.get("videoUrl") or p.get("isVideo") or p.get("productType") == "clips")]
            if not vids:
                vids = [p for p in posts if isinstance(p, dict)]   # fallback: cualquier post
            vids.sort(key=lambda p: -((p.get("videoViewCount") or p.get("videoPlayCount") or p.get("likesCount") or 0)))
            for p in vids[:3]:
                disp = p.get("displayUrl") or p.get("display_url") or p.get("thumbnailUrl")
                tb = None
                if disp:
                    try:
                        tb = _download_thumbnail_b64(disp)
                    except Exception:
                        tb = None
                reels.append({
                    "thumb": tb,
                    "views": p.get("videoViewCount") or p.get("videoPlayCount") or 0,
                    "likes": p.get("likesCount") or 0,
                    "caption": (p.get("caption") or "")[:300],
                    "url": p.get("url") or (("https://www.instagram.com/reel/" + p.get("shortCode") + "/") if p.get("shortCode") else None),
                })
        except Exception as e:
            logger.warning("ig-avatar: reels parse failed handle=%s err=%s", handle, e)
        return jsonify({
            "avatar": b64,
            "full_name": item.get("fullName") or item.get("full_name"),
            "followers": item.get("followersCount") or item.get("followers_count"),
            "reels": reels,
        }), 200
    except Exception as e:
        logger.warning("onboarding_ig_avatar failed handle=%s err=%s", handle, e)
        return jsonify({"avatar": None}), 200


@app.route("/metrics/ig-profile", methods=["POST"])
@require_auth
@limiter.limit("5 per minute")
def metrics_link_profile():
    user = current_user()
    body = request.get_json() or {}
    username = (body.get("username") or "").strip().lstrip("@")
    if not username or len(username) > 60:
        return jsonify({"error": "Username inválido"}), 400
    if not re.match(r"^[a-zA-Z0-9._]+$", username):
        return jsonify({"error": "Username contiene caracteres inválidos"}), 400

    pid = _req_project_id()
    if _get_ig_profile(user["id"], pid, "id"):
        return jsonify({"error": "Esta marca ya tiene un perfil de Instagram vinculado"}), 409

    row = db.table("ig_profiles").insert({
        "user_id": user["id"],
        "ig_username": username,
        "project_id": pid,   # la cuenta de IG pertenece a la MARCA activa
    }).execute()

    return jsonify({"ok": True, "ig_profile_id": row.data[0]["id"], "ig_username": username})


@app.route("/metrics/analyze", methods=["POST"])
@require_auth
@limiter.limit("5 per minute")
def metrics_analyze():
    user = current_user()
    ig_row = _get_ig_profile(user["id"], _req_project_id())
    if not ig_row:
        return jsonify({"error": "No tienes un perfil de Instagram vinculado"}), 404

    profile = get_profile(user["id"])
    ok, err = _check_metrics_limit(profile)
    if not ok:
        return jsonify({"error": err}), 429

    plan = profile.get("plan", "free")
    limits = METRICS_LIMITS.get(plan, METRICS_LIMITS["free"])
    max_videos = limits["videos_per_analysis"]
    body = request.get_json(silent=True) or {}
    requested = body.get("count")
    if requested and isinstance(requested, int) and 1 <= requested <= max_videos:
        max_videos = requested
    username = ig_row["ig_username"]
    ig_profile_id = ig_row["id"]

    try:
        videos = _scrape_ig_reels([username], limit=max_videos)
    except Exception as e:
        logger.error(f"[IG-METRICS] Re-scrape FAILED for @{username}: {e}", exc_info=True)
        return jsonify({"error": "Error al analizar el perfil"}), 500

    if videos:
        for v in videos:
            v["user_id"] = user["id"]
            v["ig_profile_id"] = ig_profile_id
        db.table("ig_videos").upsert(videos, on_conflict="user_id,ig_video_id").execute()

    # ── Cierra el loop de medición ────────────────────────────────────────────
    # Atribuye cada reel publicado a su guión por el AUDIO (transcripción ↔ cuerpo
    # del guión), escribe sus métricas en él, y realimenta el VoiceProfile. Síncrono
    # y acotado (max_transcribe) para no disparar latencia/coste; idempotente (cachea).
    learn = {}
    if videos:
        try:
            learn = attribute_and_learn(user["id"], videos, max_transcribe=4)
        except Exception as e:
            logger.warning("metrics_analyze: attribute_and_learn falló user=%s err=%s", user["id"], e)

    db.table("ig_profiles").update({
        "last_scraped_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", ig_profile_id).execute()

    db.table("profiles").update({
        "metrics_analyses_this_week": profile.get("metrics_analyses_this_week", 0) + 1,
    }).eq("id", user["id"]).execute()

    return jsonify({"ok": True, "videos_updated": len(videos), "learn": learn})


@app.route("/metrics/analyze-one", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def metrics_analyze_one():
    user = current_user()
    body = request.get_json() or {}
    reel_url = (body.get("url") or "").strip()
    if not reel_url or "instagram.com" not in reel_url:
        return jsonify({"error": "URL de reel inválida"}), 400

    ig_row = _get_ig_profile(user["id"], _req_project_id())
    if not ig_row:
        return jsonify({"error": "No tienes un perfil de Instagram vinculado"}), 404
    ig_profile_id = ig_row["id"]

    try:
        videos = _scrape_ig_reels([reel_url], limit=1)
    except Exception as e:
        logger.error(f"Apify single scrape failed: {e}", exc_info=True)
        return jsonify({"error": "No se pudo analizar el reel"}), 500

    if not videos:
        return jsonify({"error": "No se encontraron datos para este reel"}), 404

    video = videos[0]
    video["user_id"] = user["id"]
    video["ig_profile_id"] = ig_profile_id
    db.table("ig_videos").upsert([video], on_conflict="user_id,ig_video_id").execute()

    return jsonify({"ok": True, "video": video})


@app.route("/metrics/transcribe-video", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def metrics_transcribe_video():
    user = current_user()
    body = request.get_json() or {}
    ig_video_id = (body.get("ig_video_id") or "").strip()
    if not ig_video_id:
        return jsonify({"error": "ig_video_id requerido"}), 400

    ig_row = _get_ig_profile(user["id"], _req_project_id(), "id")
    if not ig_row:
        return jsonify({"error": "No tienes un perfil vinculado"}), 404
    ig_profile_id = ig_row["id"]

    vid = db.table("ig_videos").select("*").eq("ig_video_id", ig_video_id).eq("ig_profile_id", ig_profile_id).execute()
    if not vid.data:
        return jsonify({"error": "Vídeo no encontrado"}), 404
    video = vid.data[0]

    if video.get("transcription"):
        return jsonify({"ok": True, "transcript": video["transcription"], "cached": True})

    profile = get_profile(user["id"])
    is_unlimited = user.get("email", "").lower() in UNLIMITED_EMAILS
    if not is_unlimited:
        user_plan = profile.get("plan", "free")
        if user_plan in ("pro", "creator", "agency"):
            ok, err_msg = check_monthly_limit(profile)
            if not ok:
                return jsonify({"error": err_msg}), 429
        elif profile["credits_cents"] >= COST_CENTS:
            pass
        elif profile["free_used_today"] < FREE_DAILY_USER:
            pass
        else:
            return jsonify({"error": "Sin usos disponibles para transcribir"}), 429

    reel_url = video.get("ig_url")
    if not reel_url:
        return jsonify({"error": "No hay URL del reel para transcribir"}), 400

    try:
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = download_audio(reel_url, tmp, "instagram")
            language = (body.get("language") or "").strip() or None
            text = transcribe_with_groq(audio_path, language)
    except Exception as e:
        logger.error(f"Metrics transcribe failed for {ig_video_id}: {e}", exc_info=True)
        return jsonify({"error": "Error al transcribir el vídeo"}), 500

    db.table("ig_videos").update({
        "transcription": text,
        "transcribed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("ig_video_id", ig_video_id).execute()

    if not is_unlimited:
        user_plan = profile.get("plan", "free")
        if user_plan in ("pro", "creator", "agency"):
            db.table("profiles").update({
                "monthly_usage": profile.get("monthly_usage", 0) + 1
            }).eq("id", user["id"]).execute()
        elif profile["credits_cents"] >= COST_CENTS:
            db.table("profiles").update({
                "credits_cents": profile["credits_cents"] - COST_CENTS
            }).eq("id", user["id"]).execute()
        else:
            db.table("profiles").update({
                "free_used_today": profile["free_used_today"] + 1
            }).eq("id", user["id"]).execute()

    return jsonify({"ok": True, "transcript": text})


@app.route("/metrics/videos")
@require_auth
def metrics_list_videos():
    user = current_user()
    ig_profile = _get_ig_profile(user["id"], _req_project_id())
    if not ig_profile:
        return jsonify({"ig_profile": None, "videos": []})

    sort_by = request.args.get("sort", "published_at")
    allowed_sorts = {"published_at", "views", "likes", "comments", "shares"}
    if sort_by not in allowed_sorts:
        sort_by = "published_at"

    videos = db.table("ig_videos").select("*").eq(
        "ig_profile_id", ig_profile["id"]
    ).order(sort_by, desc=True).execute()

    profile = get_profile(user["id"])
    plan = profile.get("plan", "free")
    limits = METRICS_LIMITS.get(plan, METRICS_LIMITS["free"])
    profile = _metrics_week_reset(profile)

    return jsonify({
        "ig_profile": {
            "id": ig_profile["id"],
            "ig_username": ig_profile["ig_username"],
            "last_scraped_at": ig_profile.get("last_scraped_at"),
        },
        "videos": videos.data,
        "limits": {
            "analyses_per_week": limits["analyses_per_week"],
            "analyses_used": profile.get("metrics_analyses_this_week", 0),
            "videos_per_analysis": limits["videos_per_analysis"],
        },
    })


@app.route("/metrics/ig-profile", methods=["DELETE"])
@require_auth
def metrics_unlink_profile():
    user = current_user()
    ig_row = _get_ig_profile(user["id"], _req_project_id(), "id")
    if not ig_row:
        return jsonify({"error": "No hay perfil vinculado"}), 404

    ig_profile_id = ig_row["id"]
    db.table("ig_videos").delete().eq("ig_profile_id", ig_profile_id).execute()
    db.table("ig_profiles").delete().eq("id", ig_profile_id).execute()

    return jsonify({"ok": True})


@app.route("/metrics/video/<ig_video_id>", methods=["DELETE"])
@require_auth
def metrics_delete_video(ig_video_id):
    user = current_user()
    row = db.table("ig_videos").select("id").eq("user_id", user["id"]).eq("ig_video_id", ig_video_id).execute()
    if not row.data:
        return jsonify({"error": "Video not found"}), 404
    db.table("ig_videos").delete().eq("id", row.data[0]["id"]).execute()
    return jsonify({"ok": True})


@app.route("/metrics/video/<ig_video_id>/tag", methods=["PATCH"])
@require_auth
def metrics_tag_video(ig_video_id):
    user = current_user()
    body = request.get_json() or {}
    tag = body.get("tag")
    if tag and len(tag) > 20:
        return jsonify({"error": "Tag too long"}), 400
    row = db.table("ig_videos").select("id").eq("user_id", user["id"]).eq("ig_video_id", ig_video_id).execute()
    if not row.data:
        return jsonify({"error": "Video not found"}), 404
    db.table("ig_videos").update({"tag": tag}).eq("id", row.data[0]["id"]).execute()
    return jsonify({"ok": True, "tag": tag})


@app.route("/metrics/video/<ig_video_id>/tags", methods=["PATCH"])
@require_auth
def metrics_tags_video(ig_video_id):
    user = current_user()
    body = request.get_json() or {}
    tags = body.get("tags", [])
    if not isinstance(tags, list):
        return jsonify({"error": "tags must be an array"}), 400
    if len(tags) > 5:
        return jsonify({"error": "Max 5 tags per video"}), 400
    tags = [t.strip().lower()[:20] for t in tags if t and isinstance(t, str)]
    tags = list(dict.fromkeys(tags))  # deduplicate preserving order
    row = db.table("ig_videos").select("id").eq("user_id", user["id"]).eq("ig_video_id", ig_video_id).execute()
    if not row.data:
        return jsonify({"error": "Video not found"}), 404
    db.table("ig_videos").update({"tags": tags}).eq("id", row.data[0]["id"]).execute()
    return jsonify({"ok": True, "tags": tags})


@app.route("/metrics/video/<ig_video_id>/manual", methods=["PATCH"])
@require_auth
def metrics_manual_video(ig_video_id):
    user = current_user()
    body = request.get_json() or {}
    row = db.table("ig_videos").select("id").eq("user_id", user["id"]).eq("ig_video_id", ig_video_id).execute()
    if not row.data:
        return jsonify({"error": "Video not found"}), 404
    update = {}
    if "note" in body:
        note = (body["note"] or "").strip()
        if len(note) > 500:
            return jsonify({"error": "Note too long (max 500)"}), 400
        update["note"] = note or None
    if "retention_seconds" in body:
        val = body["retention_seconds"]
        if val is not None:
            val = int(val)
            if val < 0 or val > 300:
                return jsonify({"error": "retention_seconds must be 0-300"}), 400
        update["retention_seconds"] = val
    if "saves" in body:
        val = body["saves"]
        if val is not None:
            val = int(val)
            if val < 0:
                return jsonify({"error": "saves must be >= 0"}), 400
        update["saves"] = val
    if "reach" in body:
        val = body["reach"]
        if val is not None:
            val = int(val)
            if val < 0:
                return jsonify({"error": "reach must be >= 0"}), 400
        update["reach"] = val
    if not update:
        return jsonify({"error": "No fields to update"}), 400
    db.table("ig_videos").update(update).eq("id", row.data[0]["id"]).execute()
    return jsonify({"ok": True, **update})


@app.route("/robots.txt")
def robots():
    txt = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /auth/\n"
        "Disallow: /stripe-webhook\n"
        "Disallow: /checkout\n"
        "Disallow: /task/\n"
        "\n"
        "Sitemap: https://reelscript.net/sitemap.xml\n"
    )
    return Response(txt, mimetype="text/plain")


@app.route("/llms.txt")
def llms_txt():
    path = os.path.join(app.root_path, "llms.txt")
    if not os.path.exists(path):
        abort(404)
    with open(path, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/plain")


@app.route("/affiliate")
def affiliate_page():
    return render_template("affiliate.html")


@app.route("/cookies")
def cookies_page():
    return render_template("cookies.html")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.route("/terms")
def terms_page():
    return render_template("terms.html")


@app.route("/legal")
def legal_notice_page():
    return render_template("legal.html")


# v0.14.12 — i18n strings for forgot/reset password (server-side render)
FORGOT_RESET_STRINGS = {
    "es": {
        # forgot-password
        "fp_meta_title": "Restablecer contraseña — Reelscript",
        "fp_title": "Restablece tu contraseña",
        "fp_sub": "Introduce tu email y te enviaremos un enlace para restablecerla.",
        "fp_email_label": "EMAIL",
        "fp_email_placeholder": "tu@ejemplo.com",
        "fp_submit": "Enviar enlace",
        "fp_submitting": "Enviando…",
        "fp_back": "Volver al inicio",
        "fp_success_title": "Mira tu bandeja de entrada",
        "fp_success_text_pre": "Te hemos enviado un enlace a",
        "fp_success_text_post": ". Caduca en 1 hora.",
        "fp_email_required": "Email obligatorio",
        # reset-password
        "rp_meta_title": "Nueva contraseña — Reelscript",
        "rp_title": "Nueva contraseña",
        "rp_sub": "Elige una contraseña segura para tu cuenta.",
        "rp_pass_label": "NUEVA CONTRASEÑA",
        "rp_pass_placeholder": "Mín. 6 caracteres",
        "rp_confirm_label": "CONFIRMAR CONTRASEÑA",
        "rp_confirm_placeholder": "Repite tu contraseña",
        "rp_submit": "Actualizar contraseña",
        "rp_submitting": "Actualizando…",
        "rp_min_chars_err": "La contraseña debe tener al menos 6 caracteres",
        "rp_mismatch_err": "Las contraseñas no coinciden",
        "rp_success_title": "Contraseña actualizada",
        "rp_success_text": "Redirigiendo a la app…",
        "rp_invalid_title": "Enlace caducado o no válido",
        "rp_invalid_text": "Este enlace ya no es válido. Solicita uno nuevo.",
        "rp_request_new": "Solicitar uno nuevo",
        "error_generic": "Error",
    },
    "en": {
        "fp_meta_title": "Reset password — Reelscript",
        "fp_title": "Reset your password",
        "fp_sub": "Enter your email and we'll send you a reset link.",
        "fp_email_label": "EMAIL",
        "fp_email_placeholder": "you@example.com",
        "fp_submit": "Send reset link",
        "fp_submitting": "Sending…",
        "fp_back": "Back to login",
        "fp_success_title": "Check your inbox",
        "fp_success_text_pre": "We sent a reset link to",
        "fp_success_text_post": ". It expires in 1 hour.",
        "fp_email_required": "Email required",
        "rp_meta_title": "New password — Reelscript",
        "rp_title": "New password",
        "rp_sub": "Choose a strong password for your account.",
        "rp_pass_label": "NEW PASSWORD",
        "rp_pass_placeholder": "Min. 6 characters",
        "rp_confirm_label": "CONFIRM PASSWORD",
        "rp_confirm_placeholder": "Repeat your password",
        "rp_submit": "Update password",
        "rp_submitting": "Updating…",
        "rp_min_chars_err": "Password must be at least 6 characters",
        "rp_mismatch_err": "Passwords do not match",
        "rp_success_title": "Password updated",
        "rp_success_text": "Redirecting you to the app…",
        "rp_invalid_title": "Invalid or expired link",
        "rp_invalid_text": "This reset link is no longer valid. Request a new one.",
        "rp_request_new": "Request new reset link",
        "error_generic": "Error",
    },
}


@app.route("/forgot-password")
def forgot_password_page():
    lang = _resolve_lang()
    return render_template("forgot-password.html", lang=lang, s=FORGOT_RESET_STRINGS[lang])


@app.route("/reset-password")
def reset_password_page():
    lang = _resolve_lang()
    return render_template("reset-password.html", lang=lang, s=FORGOT_RESET_STRINGS[lang])


# ── v0.14.24: Email activation flow ────────────────────────────────────────

@app.route("/unsubscribe")
@limiter.limit("30 per minute;200 per hour")
def unsubscribe_page():
    """Sin login. Token único en profiles.unsubscribe_token → email_marketing=false."""
    token = (request.args.get("token") or "").strip()
    lang = _resolve_lang()
    if not token:
        return render_template("unsubscribe.html", lang=lang, ok=False, reason="missing_token"), 400
    try:
        prof = db.table("profiles").select("id, email_marketing").eq(
            "unsubscribe_token", token
        ).single().execute()
        if not prof.data:
            return render_template("unsubscribe.html", lang=lang, ok=False, reason="invalid_token"), 404
        # idempotente: si ya estaba opted-out, devuelve ok=True igualmente
        db.table("profiles").update({"email_marketing": False}).eq("id", prof.data["id"]).execute()
        try:
            import emails as _emails
            _emails.track("email_unsubscribed", prof.data["id"], {})
        except Exception:
            pass
        return render_template("unsubscribe.html", lang=lang, ok=True)
    except Exception as e:
        logger.warning("unsubscribe failed: %s", e)
        return render_template("unsubscribe.html", lang=lang, ok=False, reason="error"), 500


def _verify_resend_signature(headers, body_bytes):
    """Resend usa Svix. Header svix-signature = 'v1,<base64sig> v1,<base64sig> ...'.
    Devuelve True si alguna firma coincide con HMAC-SHA256 del payload."""
    import base64
    import hashlib
    import hmac
    secret = os.environ.get("RESEND_WEBHOOK_SECRET", "")
    if not secret:
        return False
    if secret.startswith("whsec_"):
        secret_b64 = secret[6:]
    else:
        secret_b64 = secret
    try:
        secret_raw = base64.b64decode(secret_b64)
    except Exception:
        return False
    svix_id = headers.get("svix-id") or headers.get("Svix-Id") or ""
    svix_ts = headers.get("svix-timestamp") or headers.get("Svix-Timestamp") or ""
    svix_sig = headers.get("svix-signature") or headers.get("Svix-Signature") or ""
    if not (svix_id and svix_ts and svix_sig):
        return False
    body_str = body_bytes.decode("utf-8", errors="replace") if isinstance(body_bytes, (bytes, bytearray)) else str(body_bytes)
    signed_payload = f"{svix_id}.{svix_ts}.{body_str}".encode()
    expected = base64.b64encode(
        hmac.new(secret_raw, signed_payload, hashlib.sha256).digest()
    ).decode()
    for sig_pair in svix_sig.split(" "):
        parts = sig_pair.split(",", 1)
        if len(parts) != 2:
            continue
        version, sig = parts
        if version == "v1" and hmac.compare_digest(sig, expected):
            return True
    return False


@app.route("/webhooks/resend", methods=["POST"])
@limiter.limit("60 per minute")
def resend_webhook():
    """Resend webhooks: email.opened, email.clicked, email.delivered, email.bounced, etc."""
    raw = request.get_data()
    if not _verify_resend_signature(request.headers, raw):
        return jsonify({"error": "invalid_signature"}), 401
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    event_type = payload.get("type", "")
    data = payload.get("data", {}) or {}
    resend_id = data.get("email_id") or data.get("id")
    if not resend_id:
        return jsonify({"ok": True, "skipped": "no_email_id"}), 200

    try:
        log_row = db.table("email_log").select(
            "id, user_id, template_key"
        ).eq("resend_id", resend_id).single().execute()
        log = log_row.data
    except Exception:
        log = None
    if not log:
        return jsonify({"ok": True, "skipped": "no_log_match"}), 200

    now = datetime.now(timezone.utc).isoformat()
    try:
        import emails as _emails
        if event_type == "email.opened" and not log.get("opened_at"):
            db.table("email_log").update({"opened_at": now}).eq("id", log["id"]).execute()
            _emails.track("email_opened", log["user_id"], {"template_key": log["template_key"]})
        elif event_type == "email.clicked":
            updates = {"clicked_at": now}
            db.table("email_log").update(updates).eq("id", log["id"]).execute()
            _emails.track("email_clicked", log["user_id"], {
                "template_key": log["template_key"],
                "url": data.get("click", {}).get("link") or data.get("link") or "",
            })
        elif event_type == "email.bounced":
            db.table("email_log").update({
                "status": "failed", "error": "bounced"
            }).eq("id", log["id"]).execute()
            db.table("profiles").update({"email_marketing": False}).eq(
                "id", log["user_id"]
            ).execute()
            _emails.track("email_bounced", log["user_id"], {"template_key": log["template_key"]})
    except Exception as e:
        logger.warning("resend_webhook update failed: %s", e)

    return jsonify({"ok": True}), 200


# ── Tracked Competitors (v0.15.0) ────────────────────────────────────────────
# Feature para seguir competidores en IG. BD compartida (creators_global,
# creator_reels_global) deduplicada entre users. user_tracked_creators es
# privado por user; project_id solo lo usa Agency.

# Límites POR MARCA (David, jun 2026): Basic(creator) 5 · Content Creator(estudio) 12 ·
# Agency 15 por marca. base_slots_global = per_brand × nº marcas del plan (techo global);
# per_project_slots = el límite POR marca que enforcea el gate cuando hay project_id.
TRACKED_CREATORS_LIMITS = {
    "free":    {"enabled": True,  "base_slots_global": 2,   "per_project_slots": None, "requires_project": False},   # onboarding sigue seed + 1 afín (decisión Leo)
    "pro":     {"enabled": True,  "base_slots_global": 1,   "per_project_slots": None, "requires_project": False},
    "creator": {"enabled": True,  "base_slots_global": 5,   "per_project_slots": 5,    "requires_project": False},  # Basic · 1 marca × 5
    "estudio": {"enabled": True,  "base_slots_global": 36,  "per_project_slots": 12,   "requires_project": False},  # Content Creator · 3 marcas × 12
    "agency":  {"enabled": True,  "base_slots_global": 150, "per_project_slots": 15,   "requires_project": True},   # Agency · 10 marcas × 15
}


def get_tracked_creators_limit(plan: str) -> dict:
    return TRACKED_CREATORS_LIMITS.get(plan, TRACKED_CREATORS_LIMITS["free"])


def get_user_extra_slots(user_id: str) -> int:
    """Suma extra_slots de user_creator_credits no consumidos y no expirados.

    El partial index del schema usa `WHERE consumed = false` (IMMUTABLE).
    El filtro temporal (expires_at) se aplica en runtime aquí.
    """
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = (db.table("user_creator_credits")
                  .select("extra_slots, expires_at")
                  .eq("user_id", user_id)
                  .eq("consumed", False)
                  .execute())
        total = 0
        for r in (rows.data or []):
            exp = r.get("expires_at")
            if exp is None or exp > now_iso:
                total += int(r.get("extra_slots") or 0)
        return total
    except Exception as e:
        logger.warning("get_user_extra_slots failed for %s: %s", user_id, e)
        return 0


def count_active_tracked(user_id: str, project_id: str | None = None,
                          scope: str = "global") -> int:
    """Cuenta tracked competitors activos (archived_at IS NULL) del user.

    scope:
      - "global": cuenta TODOS los tracking activos del user (sin importar project_id).
      - "project": cuenta solo dentro del project_id pasado (debe ser != None).
      - "no_project": cuenta solo donde project_id IS NULL (Pro/Creator).
    """
    try:
        q = (db.table("user_tracked_creators")
               .select("id", count="exact")
               .eq("user_id", user_id)
               .is_("archived_at", "null"))
        if scope == "project":
            if not project_id:
                return 0
            q = q.eq("project_id", project_id)
        elif scope == "no_project":
            q = q.is_("project_id", "null")
        # scope == "global": no filter extra
        r = q.execute()
        return r.count or 0
    except Exception as e:
        logger.warning("count_active_tracked failed for %s: %s", user_id, e)
        return 0


# El onboarding bilingüe guarda el nicho en el idioma del usuario ("Tech",
# "Finance"…), pero la semilla usa los 12 nichos amplios en español. Alias EN→ES
# para que el match por nicho funcione también para usuarios en inglés.
_SEED_NICHE_ALIAS = {
    "tech": "tecnologia", "finance": "finanzas", "cooking": "cocina",
    "fashion": "moda", "beauty": "belleza", "travel": "viajes",
    "education": "educacion", "business": "negocios", "health": "salud",
    "real estate": "inmobiliaria",
}

# Mapa de nicho de TEXTO LIBRE → nicho canónico del pool (creators_global.niche). El
# onboarding y el modal dejan nichos libres ("inteligencia artificial", "marketing
# digital", "meditacion"…) que NO casan exacto con la taxonomía del pool → 0 sugerencias.
# Este alias los lleva a su nicho canónico para el FALLBACK por nicho amplio.
_POOL_NICHE_ALIAS = {
    "ia": "tecnologia", "ai": "tecnologia", "inteligencia artificial": "tecnologia",
    "automatizacion": "tecnologia", "no-code": "tecnologia", "nocode": "tecnologia",
    "programacion": "tecnologia", "software": "tecnologia", "saas": "negocios",
    "marketing digital": "marketing", "growth": "marketing", "ads": "marketing",
    "publicidad": "marketing", "copywriting": "marketing", "redes sociales": "marketing",
    "creacion de contenido": "marketing", "creador de contenido": "marketing",
    "emprendimiento": "negocios", "emprender": "negocios", "ecommerce": "negocios", "ventas": "negocios",
    "meditacion": "espiritualidad y mindfulness", "mindfulness": "espiritualidad y mindfulness",
    "nutricion": "salud", "psicologia": "salud", "bienestar": "salud",
}


def _pool_niche_canon(niche):
    """Nicho del usuario (texto libre) → nicho canónico de creators_global, para el
    fallback por nicho amplio. Devuelve "" si no hay nicho."""
    n = _norm_tag(niche or "")
    if not n:
        return ""
    if n in _POOL_NICHE_ALIAS:
        return _POOL_NICHE_ALIAS[n]
    return _SEED_NICHE_ALIAS.get(n, n)


def _seed_competitors(niche, subniches, exclude_handles=None, exclude_ids=None, limit=5):
    """Competidores CURADOS (creators_global.niche_source='seed') del nicho/subnichos
    dados que el usuario aún no sigue. Cold-start del radar SIN grafo de co-ocurrencia
    ni LLM (handles reales, no alucinados). Matchea por nicho amplio O por overlap de
    subniches (que incluye el nicho granular del CSV como tag). Degrada a [] si falta
    la semilla o la migración. Devuelve [{'id','handle','niche','subniches'}]."""
    if limit <= 0:
        return []
    exclude_handles = {(h or "").lstrip("@").lower() for h in (exclude_handles or [])}
    exclude_ids = set(exclude_ids or [])
    pn = _norm_tag(niche or "")
    pn = _SEED_NICHE_ALIAS.get(pn, pn)
    tags = [t for t in (_norm_tag(s) for s in (subniches or [])) if t]
    if pn and pn not in tags:
        tags.append(pn)
    found, seen = [], set()
    sel = "id,ig_username,niche,subniches"

    def _collect(q):
        try:
            for x in (q.eq("niche_source", "seed").limit(150).execute()).data or []:
                cid = x.get("id")
                if cid and cid not in seen:
                    seen.add(cid)
                    found.append(x)
        except Exception:
            logger.warning("[seed] query failed (¿migración onboarding_v2_tags?)", exc_info=True)

    if pn:
        _collect(db.table("creators_global").select(sel).eq("niche", pn))
    if tags:
        _collect(db.table("creators_global").select(sel).overlaps("subniches", tags))
    out = []
    for x in found:
        cid = x.get("id")
        h = (x.get("ig_username") or "").lstrip("@")
        if not h or cid in exclude_ids or h.lower() in exclude_handles:
            continue
        out.append({"id": cid, "handle": h, "niche": x.get("niche"),
                    "subniches": x.get("subniches") or []})
        if len(out) >= limit:
            break
    return out


@app.route("/api/onboarding/suggest-competitors", methods=["POST"])
@require_auth
@limiter.limit("12 per hour;40 per day")
def suggest_competitors():
    """Bloque growth-2 — ACTIVACIÓN sesión 1.

    Entrada: el handle de IG/TikTok del usuario (+ nicho opcional). Devuelve
    3-5 creadores del MISMO nicho que el LLM (Gemini/OpenRouter, ya integrado)
    propone como competencia/referencia. NO los sigue ni los scrapea: la
    validación es al seguir (POST /api/tracked-creators ya scrapea y degrada
    limpio si el handle no existe). Así el «aha» es inmediato y barato.

    Decisión de David: LLM sugiere + valida al seguir (sin auto-scrape upfront)."""
    user = current_user()
    uid = user["id"]
    body = request.get_json() or {}
    handle = (body.get("handle") or "").strip().lstrip("@").lower()
    platform = (body.get("platform") or "instagram").strip().lower()
    niche = (body.get("niche") or "").strip()[:160]
    subniches = body.get("subniches") or []

    if not re.match(r"^[a-zA-Z0-9._]{1,30}$", handle):
        return jsonify({"error": "invalid_handle",
                        "message": "Escribe tu usuario sin @ (solo letras, números, punto y guion bajo)."}), 400

    track_event("onboarding_suggest_requested", uid, {"platform": platform, "has_niche": bool(niche)})

    # PRIMARIO: competidores CURADOS de la semilla por nicho/subnicho (handles reales,
    # no alucinados). El LLM solo rellena si la semilla no cubre el nicho (texto libre).
    seed = _seed_competitors(niche, subniches, exclude_handles=[handle], limit=5)
    creators = [{"handle": s["handle"], "reason": "referente de tu nicho"} for s in seed]
    have = {c["handle"] for c in creators}
    if len(creators) >= 5:
        return jsonify({"creators": creators[:5], "handle": handle,
                        "platform": platform, "source": "seed"}), 200

    system = (
        "Eres un estratega de contenido para creadores de Instagram/TikTok. "
        "Dado el usuario de un creador, identificas otros creadores REALES y "
        "conocidos del MISMO nicho, idioma y país probable, con los que compite "
        "o de los que aprendería. Devuelves SOLO JSON válido."
    )
    user_content = (
        f"Creador: @{handle} (plataforma: {platform})."
        + (f" Nicho declarado: {niche}." if niche else "")
        + " Devuelve un objeto JSON con esta forma EXACTA:\n"
        '{"creators": [{"handle": "usuario_sin_arroba", "reason": "por qué es '
        'referencia/competencia en 6-10 palabras"}]}\n'
        "Reglas: 5 creadores; handles REALES de cuentas públicas conocidas "
        "(no inventes usuarios); mismo idioma/nicho que @" + handle + "; "
        "NO incluyas a @" + handle + "; handle sin @, en minúsculas."
    )
    # P0: usar _call_llm (NO _call_llm_json) → hereda el json-mode de Gemini Y el
    # retry-on-length que ya vive ahí. El JSON TRUNCADO ("Unterminated string")
    # reventaba el parse de _call_llm_json y devolvía 502 → bloqueaba la activación.
    # Blindaje total: cualquier fallo (red, parse, modelo) → 200 con creators:[] +
    # fallback, y el front cae a "añade a mano". NUNCA un 502 al usuario.
    try:
        raw_text = _call_llm(system, user_content, temperature=0.6, max_tokens=1500)
        # Parse tolerante (sin raise): fences markdown + extracción del objeto.
        t = (raw_text or "").strip()
        if t.startswith("```"):
            t = re.sub(r"^```(?:json)?\s*", "", t)
            t = re.sub(r"\s*```$", "", t)
        try:
            data = json.loads(t)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", t)
            data = json.loads(m.group()) if m else {}
        raw = (data or {}).get("creators") or []
        for c in raw:
            h = (c.get("handle") or "").strip().lstrip("@").lower() if isinstance(c, dict) else ""
            if not re.match(r"^[a-zA-Z0-9._]{1,30}$", h) or h == handle or h in have:
                continue
            have.add(h)
            creators.append({"handle": h, "reason": (c.get("reason") or "").strip()[:120]})
            if len(creators) >= 5:
                break
    except Exception:
        logger.exception("suggest_competitors failed user=%s handle=%s", uid, handle)

    if not creators:
        # Nunca 502: 200 con lista vacía → el front ofrece "añade a mano".
        return jsonify({"creators": [], "fallback": True,
                        "message": "No pude buscar tu competencia ahora. Añade un competidor a mano para empezar."}), 200

    return jsonify({"creators": creators, "handle": handle, "platform": platform,
                    "source": ("mixed" if seed else "llm")}), 200


# ════════════════════════════════════════════════════════════════════════════
# ONBOARDING v2 — categorización por TAGS (nicho/subnicho) + librería reciclable.
# El foso: cada creador trackeado se etiqueta con el subnicho del usuario que lo
# añade → un usuario nuevo del subnicho X recibe el radar PRE-LLENO con reels que
# ya petaron (reusa cacheado, scrapea SOLO lo nuevo). Matching por TAGS (overlap
# de subniches), no por nicho amplio: cosmética orgánica ≠ cosmética.
# NOTA: requiere la migración 0001_onboarding_v2_tags.sql (columnas niche/
# subniches en creators_global y profiles). Si no está aplicada, los endpoints
# degradan limpio (try/except) — nunca 500.
# ════════════════════════════════════════════════════════════════════════════
NICHE_SUBNICHE_SEED = {
    "fitness": ["hipertrofia", "pérdida de peso", "running", "crossfit", "yoga", "calistenia"],
    "finanzas": ["inversión", "ahorro", "cripto", "libertad financiera", "bolsa", "finanzas personales"],
    "marketing": ["copywriting", "ads de pago", "email marketing", "marca personal", "seo", "redes sociales"],
    "cocina": ["recetas fit", "cocina rápida", "repostería", "vegano", "meal prep", "low cost"],
    "moda": ["streetwear", "moda sostenible", "low cost", "lujo", "tendencias", "outfits"],
    "belleza": ["skincare", "maquillaje", "cosmética orgánica", "cosmética coreana", "antiedad", "uñas"],
    "tecnología": ["ia", "automatización", "gadgets", "programación", "no-code", "productividad"],
    "educación": ["idiomas", "oposiciones", "estudio", "matemáticas", "historia", "ciencia"],
    "negocios": ["emprender", "ecommerce", "saas", "ventas", "liderazgo", "freelance"],
}


def _norm_tag(s: str) -> str:
    s = (s or "").strip().lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9 _-]", "", s)[:40].strip()


def _subniche_suggestions(niche: str):
    """Cold-start: subnichos sugeridos por nicho amplio (sin LLM ni BD)."""
    return NICHE_SUBNICHE_SEED.get(_norm_tag(niche), [
        "consejos", "tutoriales", "detrás de cámaras", "historias", "errores comunes", "tendencias"])


def _fmt_views(n) -> str:
    try:
        n = int(n)
    except Exception:
        return "0"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f} M"
    if n >= 1_000:
        return f"{round(n/1_000)} K"
    return str(n)


def _track_and_tag_creator(uid: str, ig_username: str, subniches, project_id=None) -> bool:
    """Upsert creator + TAG con los subnichos del usuario (enriquece la librería)
    + tracking idempotente + scrape SOLO si nuevo/stale (>24h). Reusa el patrón de
    POST /api/tracked-creators. Devuelve True si quedó trackeado.
    OJO (MVP): no re-aplica el cap por plan aquí — el onboarding ya limita a ≤2
    elegidos; en prod habría que delegar al límite de tracked-creators."""
    ig_username = (ig_username or "").strip().lstrip("@").lower()
    if not re.match(r"^[a-z0-9._]{1,30}$", ig_username):
        return False
    subs = [t for t in (_norm_tag(s) for s in (subniches or [])) if t][:8]
    try:
        ins = (db.table("creators_global")
                 .upsert({"ig_username": ig_username}, on_conflict="ig_username")
                 .execute())
        creator_row = (ins.data or [None])[0]
        if not creator_row:
            creator_row = db.table("creators_global").select("*").eq("ig_username", ig_username).single().execute().data
    except Exception:
        logger.exception("[onb2] upsert creator failed %s", ig_username)
        return False
    creator_id = creator_row["id"]
    # TAG: unión de subnichos (el foso). Degradado si falta la columna.
    if subs:
        try:
            cur = set(creator_row.get("subniches") or [])
            new = cur | set(subs)
            if new != cur:
                db.table("creators_global").update({"subniches": sorted(new)}).eq("id", creator_id).execute()
        except Exception:
            logger.warning("[onb2] tag subniches failed (¿migración?) %s", ig_username, exc_info=True)
    # tracking idempotente
    try:
        q = (db.table("user_tracked_creators").select("id")
             .eq("user_id", uid).eq("creator_id", creator_id).is_("archived_at", "null"))
        q = q.is_("project_id", "null") if project_id is None else q.eq("project_id", project_id)
        if not (q.execute().data):
            db.table("user_tracked_creators").insert(
                {"user_id": uid, "creator_id": creator_id, "project_id": project_id}).execute()
    except Exception:
        logger.exception("[onb2] track insert failed %s", ig_username)
        return False
    # scrape SOLO si nuevo/stale (reusa caché si <24h; nunca si private/not_found)
    _status = creator_row.get("scrape_status")
    _last = creator_row.get("last_scraped_at")
    should = False
    if _status in ("private", "not_found"):
        should = False
    elif _last is None:
        should = True
    else:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(_last).replace("Z", "+00:00"))).total_seconds() / 3600.0
            should = age > 24
        except Exception:
            should = True
    if should and not DEMO_MODE:
        try:
            from tasks import scrape_creator_task  # noqa: E402
            scrape_creator_task.delay(creator_id)
        except Exception:
            logger.warning("[onb2] scrape enqueue failed %s", ig_username, exc_info=True)
    return True


@app.route("/api/niche/trending-reels", methods=["GET"])
@require_auth
@limiter.limit("30 per minute")
def niche_trending_reels():
    """RECICLAJE: reels que ya petaron de creadores tageados con el subnicho del
    usuario — REUSA lo cacheado (cero scrape, baja coste/latencia). Matching por
    overlap de subniches (tags). Cold-start (subnicho sin datos) → fallback con
    sugerencias de subnicho para que el front no quede vacío."""
    niche = (request.args.get("niche") or "").strip()[:80]
    subs = [t for t in (_norm_tag(s) for s in (request.args.get("subniches") or "").split(",")) if t][:8]
    reels = []
    try:
        if subs:
            cg = (db.table("creators_global").select("id,ig_username")
                  .overlaps("subniches", subs).limit(40).execute())
            uname = {c["id"]: c["ig_username"] for c in (cg.data or [])}
            cids = list(uname.keys())
            if cids:
                rr = (db.table("creator_reels_global")
                      .select("id,creator_id,caption,views,thumb_b64,thumb_url")
                      .in_("creator_id", cids).eq("is_archived", False)
                      .order("views", desc=True).limit(20).execute())
                rows = rr.data or []
                vs = sorted(int(r.get("views") or 0) for r in rows)
                med = vs[len(vs) // 2] if vs else 0
                for r in rows[:8]:
                    v = int(r.get("views") or 0)
                    mult = round(v / med, 1) if med > 0 else 1.0
                    reels.append({
                        "id": r["id"], "handle": uname.get(r["creator_id"], ""),
                        "caption": (r.get("caption") or "").strip()[:90],
                        "views": _fmt_views(v), "mult": mult,
                        # el front pinta la miniatura; sin esto salían azules (placeholder).
                        "thumb_b64": r.get("thumb_b64"), "thumb_url": r.get("thumb_url"),
                        "tag": "explota" if mult >= 2.5 else ("subiendo" if mult >= 1.5 else "constante"),
                    })
    except Exception:
        logger.exception("[onb2] niche_trending_reels failed")
        reels = []
    return jsonify({"reels": reels, "cold_start": len(reels) == 0,
                    "subniche_suggestions": _subniche_suggestions(niche)}), 200


@app.route("/api/onboarding/discover-niche", methods=["POST"])
@require_auth
@limiter.limit("6 per hour")
def onboarding_discover_niche():
    """Dispara EN BACKGROUND el descubrimiento de creadores del nicho. SEED = una cuenta
    referente que el user añade a mano en el onboarding → scrapeamos sus relatedProfiles
    (grafo de IG) + gate Groq → competidores+reels acertados. Si no hay seed, el backend
    cae a hashtag. Fire-and-forget (202). NO cobra créditos (es activación)."""
    user = current_user()
    body = request.get_json(silent=True) or {}
    niche = (body.get("niche") or "").strip()[:80]
    subs = [t for t in (_norm_tag(s) for s in (body.get("subniches") or [])) if t][:5]
    seed = (body.get("seed") or "").strip().lstrip("@").lower()
    if not re.match(r"^[a-z0-9._]{2,30}$", seed):
        seed = ""
    if not subs and not seed:
        return jsonify({"ok": False, "error": "no_input"}), 200
    # Cap por plan: el descubrimiento NO debe seguir más competidores de los que el plan
    # permite (free=2 → seed + 1). Se lo pasamos como follow_top; el seed va siempre 1º.
    try:
        cap = get_tracked_creators_limit(effective_plan(get_profile(user["id"])))["base_slots_global"]
    except Exception:
        cap = 2
    follow_top = max(2, min(cap, 6))   # al menos seed+1; tope 6 para no sobre-scrapear
    try:
        from tasks import discover_niche_creators_task  # noqa: E402
        discover_niche_creators_task.delay(user["id"], niche, subs, seed, follow_top)
    except Exception:
        logger.warning("onboarding_discover_niche: enqueue failed", exc_info=True)
        return jsonify({"ok": False, "error": "enqueue_failed"}), 200
    return jsonify({"ok": True}), 202


@app.route("/api/onboarding/aha-script", methods=["POST"])
@require_auth
@limiter.limit("12 per hour;40 per day")
def onboarding_aha_script():
    """Onboarding aha (Fathom): genera el PRIMER guion del usuario a partir de un
    reel del seed/reciclaje de su subnicho, en su voz. GRATIS y sin requerir que
    el creador esté tracked (el reel viene de la librería de reciclaje, no de un
    competidor seguido). Es un teaser de activación: NO cobra créditos ni persiste
    el guion. Reusa adapt_with_ai (mismo generador que «Hazlo mío»)."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    caption = (body.get("caption") or "").strip()[:600]
    handle = (body.get("handle") or "").strip().lstrip("@")[:40]
    niche = (body.get("niche") or "").strip()[:80]
    subs = body.get("subniches") or []
    if isinstance(subs, list):
        subs = ", ".join(s.strip()[:40] for s in (str(x) for x in subs[:5]) if s.strip())
    else:
        subs = str(subs)[:120]
    out_lang = (body.get("language") or "es").lower()[:2]
    if not caption:
        return jsonify({"error": "missing_caption"}), 400

    niche_ctx = niche + ((" · " + subs) if subs else "")
    user_content = (
        f"Un reel está EXPLOTANDO ahora mismo en el nicho «{niche_ctx or 'creadores'}»"
        + (f" (de @{handle})" if handle else "") + ".\n"
        f'Idea/caption del reel: "{caption}"\n\n'
        "Reescríbelo como el PRIMER guion de un creador de ese nicho: mismo ángulo "
        "ganador, pero en su voz, listo para grabar. Responde en "
        + ("español." if out_lang == "es" else "inglés.")
    )
    try:
        result = adapt_with_ai(user_content, "viral",
                               voice=get_voice_profile(uid), user_id=uid)
    except Exception:
        logger.exception("aha_script failed user=%s", uid)
        # El front cae a su respaldo plantillado: nunca rompemos el aha.
        return jsonify({"error": "generation_failed"}), 200

    script = {
        "hook": (result.get("hook") or "").strip(),
        "beats": [str(b) for b in (result.get("body") or []) if str(b).strip()],
        "close": (result.get("closing") or "").strip(),
    }
    try:
        track_event("onboarding_aha_generated", uid, {"niche": niche})
    except Exception:
        pass

    # Persistir el PRIMER guion del usuario (si aún no tiene ninguno) → su «primer
    # guion» deja de ser efímero: aparece en Guiones y CUENTA para el nivel del
    # Cerebro (el bloque del Radar pasa de 0/3 a 1/3). Solo el primero, para no
    # acumular si prueba varios reels en el onboarding. Sigue sin cobrar créditos.
    try:
        flat = "\n".join([script["hook"]] + script["beats"] + [script["close"]]).strip()
        n_scripts = (db.table("scripts").select("id", count="exact").eq("user_id", uid).execute()).count or 0
        if flat and n_scripts == 0:
            db.table("scripts").insert({
                "user_id": uid,
                "title": (f"Mi primer guion · @{handle}" if handle else "Mi primer guion")[:80],
                "script": flat,
                "from_competitor_username": handle or None,
                "project_id": _req_project_id(),
            }).execute()
    except Exception:
        logger.warning("aha_script persist failed user=%s", uid)

    return jsonify({"script": script}), 200


@app.route("/api/brain/training-cards", methods=["GET"])
@require_auth
@limiter.limit("20 per hour")
def brain_training_cards():
    """Brain «Entrenar» (lever de INVERSIÓN, Fathom): genera hooks variados del nicho
    del usuario para que los valore (👍/👎). Cada voto refina su gusto. GRATIS."""
    user = current_user()
    uid = user["id"]
    profile = get_profile(uid)
    niche = (request.args.get("niche") or profile.get("niche") or "").strip()[:80]
    lang = (request.args.get("language") or profile.get("lang") or "es").lower()[:2]
    system = (
        "Eres guionista de reels. Generas HOOKS (1-2 líneas que paran el scroll) para "
        "que un creador los valore (me gusta / no me gusta) y aprendamos su gusto. "
        "Devuelves SOLO JSON válido, sin markdown."
    )
    user_content = (
        f"Nicho del creador: {niche or 'creadores de contenido'}.\n"
        "Genera 6 hooks variados, cada uno con un ángulo distinto (polémico, curiosidad, "
        "error común, resultado, promesa, contraintuitivo), en "
        + ("español" if lang == "es" else "inglés") + ".\n"
        'Forma EXACTA: {"cards":[{"kind":"ángulo en 1 palabra","text":"el hook"}]}'
    )
    cards = []
    try:
        raw = _call_llm(system, user_content, temperature=0.9, max_tokens=900)
        t = (raw or "").strip()
        if t.startswith("```"):
            t = re.sub(r"^```(?:json)?\s*", "", t); t = re.sub(r"\s*```$", "", t)
        try:
            data = json.loads(t)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", t); data = json.loads(m.group()) if m else {}
        for c in (data.get("cards") or [])[:8]:
            if not isinstance(c, dict):
                continue
            tx = (c.get("text") or "").strip()
            if tx:
                cards.append({"kind": (c.get("kind") or "hook").strip()[:24], "text": tx[:240]})
    except Exception:
        logger.exception("brain_training_cards failed uid=%s", uid)
        cards = []
    return jsonify({"cards": cards}), 200


@app.route("/api/brain/levelup-reward", methods=["POST"])
@require_auth
@limiter.limit("30 per hour")
def brain_levelup_reward():
    """Recompensa al subir de nivel el Cerebro (David 26-jun): +1 crédito gratis la
    PRIMERA vez que se alcanza cada nivel (2..5) → 4 créditos gratis en total. El
    nivel vive en el CLIENTE (localStorage), así que para que NO se pueda farmear
    (resetear localStorage y volver a subir) registramos cada recompensa en
    brain_ratings (source='brain_levelup_reward', content=str(nivel)) y no repetimos."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    try:
        level = int(body.get("level") or 0)
    except (TypeError, ValueError):
        level = 0
    if level < 2 or level > 5:
        return jsonify({"error": "bad_level"}), 400
    MARKER = "brain_levelup_reward"
    CREDITS_PER_LEVEL = 1
    # ¿ya recompensado este nivel alguna vez? (persiste aunque borren localStorage)
    try:
        ex = (db.table("brain_ratings").select("id")
              .eq("user_id", uid).eq("source", MARKER).eq("content", str(level))
              .limit(1).execute())
        if ex.data:
            return jsonify({"ok": True, "already": True, "granted": 0,
                            "credits": credits_available(get_profile(uid))}), 200
    except Exception:
        logger.warning("[levelup-reward] dup check failed uid=%s", uid, exc_info=True)
    profile = get_profile(uid)
    new_cents = (profile.get("credits_cents") or 0) + CREDITS_PER_LEVEL * COST_CENTS
    try:
        db.table("profiles").update({"credits_cents": new_cents}).eq("id", uid).execute()
        db.table("brain_ratings").insert({
            "user_id": uid, "kind": "guion", "content": str(level), "rating": 1,
            "suggestion": "levelup reward N" + str(level), "source": MARKER,
        }).execute()
    except Exception:
        logger.exception("[levelup-reward] grant failed uid=%s level=%s", uid, level)
        return jsonify({"error": "internal", "message": "Inténtalo de nuevo."}), 500
    track_event("brain_levelup_reward", uid, {"level": level, "credits": CREDITS_PER_LEVEL})
    return jsonify({"ok": True, "granted": CREDITS_PER_LEVEL,
                    "credits": credits_available(get_profile(uid))}), 200


@app.route("/api/brain/rate", methods=["POST"])
@require_auth
@limiter.limit("180 per hour")
def brain_rate():
    """Guarda el voto del Brain «Entrenar» en la tabla brain_ratings (afina la voz)
    + evento PostHog. Los 👍 y las sugerencias ('¿cómo lo dirías tú?') se realimentan
    en el prompt de generación (ver brain_voice_block)."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    like = body.get("rating") in (1, "1", True, "true")
    rating = 1 if like else -1
    # `type` = modo del día (hooks/guiones); `kind` del front es el ángulo de la tarjeta.
    mode = (body.get("type") or body.get("kind") or "hook").strip().lower()
    kind = "guion" if mode.startswith("gui") else "hook"
    text = (body.get("text") or "").strip()[:1000]
    suggestion = (body.get("suggestion") or "").strip()[:1000]
    brand_id = (body.get("brand_id") or "").strip()[:64]
    # origen del voto: "nicho" (brain-train) o "tinder_guiones" (Tinder de tus guiones).
    source = (body.get("source") or "nicho").strip().lower()[:32] or "nicho"
    if not text:
        return jsonify({"error": "missing_text"}), 400
    try:
        db.table("brain_ratings").insert({
            "user_id": uid, "brand_id": brand_id, "kind": kind,
            "content": text, "rating": rating,
            "suggestion": suggestion or None, "source": source,
        }).execute()
    except Exception:
        logger.exception("brain_rate insert failed uid=%s", uid)
    try:
        track_event("brain_rating", uid, {"rating": rating, "kind": kind, "source": source, "text": text[:120]})
    except Exception:
        pass
    return jsonify({"ok": True}), 200


@app.route("/api/community/interest", methods=["POST"])
@require_auth
@limiter.limit("20 per hour")
def community_interest():
    """Lista de espera de la Comunidad (Pushear reels→regalo + Grupos, Fathom 18/06):
    marca el interés del usuario para avisarle cuando lance. Idempotente."""
    user = current_user()
    uid = user["id"]
    profile = get_profile(uid)
    if not profile.get("community_waitlist_at"):
        try:
            db.table("profiles").update({
                "community_waitlist_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", uid).execute()
        except Exception:
            logger.exception("community_interest: update failed uid=%s", uid)
            return jsonify({"error": "internal"}), 500
    track_event("community_interest", uid, {"feature": "community"})
    return jsonify({"ok": True, "waitlisted": True}), 200


@app.route("/api/brain/exercise-done", methods=["POST"])
@require_auth
@limiter.limit("30 per hour")
def brain_exercise_done():
    """Ejercicio diario del Cerebro completado → +2-3% a la VOZ (confidence), 1 vez/día.
    El % del Cerebro es ahora NIVEL-DRIVEN (frontend: nivel + acciones); el ejercicio
    entrena la voz, que es input del nivel → así el ejercicio mueve el %. Idempotente
    por día (brain_exercise_date). Cap de confidence en 92 (<95 hard cap)."""
    user = current_user()
    uid = user["id"]
    profile = get_profile(uid)
    today = datetime.now(timezone.utc).date().isoformat()
    if str(profile.get("brain_exercise_date") or "") == today:
        return jsonify({"ok": True, "already": True,
                        "voice_gain": profile.get("brain_last_gain") or 0}), 200
    gain = 2 + (abs(hash(uid + today)) % 2)   # +2..3, determinista y persistido
    new_conf = None
    try:
        vr = (db.table("voice_profiles").select("confidence")
                .eq("user_id", uid).eq("brand_id", "").limit(1).execute())
        if vr.data:
            cur = int(vr.data[0].get("confidence") or 0)
            new_conf = min(92, cur + gain)
            (db.table("voice_profiles").update({"confidence": new_conf})
               .eq("user_id", uid).eq("brand_id", "").execute())
    except Exception:
        logger.warning("brain_exercise_done: voice bump failed uid=%s", uid)
    try:
        db.table("profiles").update({
            "brain_exercise_date": today, "brain_last_gain": gain,
        }).eq("id", uid).execute()
    except Exception:
        logger.exception("brain_exercise_done: profile update failed uid=%s", uid)
        return jsonify({"error": "internal"}), 500
    track_event("brain_exercise_done", uid, {"voice_gain": gain, "confidence": new_conf})
    return jsonify({"ok": True, "voice_gain": gain, "confidence": new_conf}), 200


@app.route("/api/brain/rescrape", methods=["POST"])
@require_auth
@limiter.limit("10 per hour")
def brain_rescrape():
    """Fuerza un re-scrape del PERFIL del usuario (adelanta el análisis automático
    2×/sem) para detectar reels recién publicados → sube el nivel del Cerebro.
    Cuesta créditos (Fathom 18/06: 'si quieres más scrapeos, gasta crédito').
    Reusa scrape_creator_task; cobra del mismo pool que la pill (mensual + topups)."""
    user = current_user()
    uid = user["id"]
    profile = get_profile(uid)
    n = RESCRAPE_COST_CREDITS

    # Handle propio del usuario (ig_profiles). Sin él no hay nada que scrapear.
    try:
        ig_r = db.table("ig_profiles").select("ig_username").eq("user_id", uid).limit(1).execute()
        ig_username = ((ig_r.data or [{}])[0].get("ig_username") or "").strip().lstrip("@")
    except Exception:
        ig_username = ""
    if not ig_username:
        return jsonify({"error": "no_handle",
                        "message": "Conecta tu Instagram primero para analizar tu perfil."}), 400

    # Cobro: del mismo pool que credits_available (mensual del plan + topups). 402 si no llega.
    plan = profile.get("plan", "free")
    limit = PLAN_LIMITS.get(plan)
    # "unlimited" SOLO para planes de pago sin tope (no para free-en-trial, cuyo limit
    # también es None → si no, el free se saltaría el cobro).
    unlimited = paid_features_active(profile, user) and limit is None and plan != "free"
    if not unlimited:
        monthly_rem = max(0, limit - (profile.get("monthly_usage", 0) or 0)) if limit else 0
        topup_credits = (profile.get("credits_cents", 0) or 0) // COST_CENTS
        if monthly_rem + topup_credits < n:
            track_event("paywall_shown", uid, {"wall": "rescrape_no_credits"})
            return jsonify({"error": "no_credits",
                            "message": f"Necesitas {n} créditos para forzar el análisis de tu perfil."}), 402

    # Resuelve/crea el creador global del propio usuario.
    try:
        ins = (db.table("creators_global")
                 .upsert({"ig_username": ig_username}, on_conflict="ig_username").execute())
        creator_row = (ins.data or [None])[0]
        if not creator_row:
            creator_row = (db.table("creators_global").select("id")
                             .eq("ig_username", ig_username).single().execute()).data
        creator_id = creator_row["id"]
    except Exception:
        logger.exception("brain_rescrape: upsert creators_global failed uid=%s", uid)
        return jsonify({"error": "internal"}), 500

    # Cobra (primero mensual, luego topups) — mismo orden que credits_available.
    if not unlimited:
        from_monthly = min(n, monthly_rem)
        from_topup = n - from_monthly
        updates = {}
        if from_monthly:
            updates["monthly_usage"] = (profile.get("monthly_usage", 0) or 0) + from_monthly
        if from_topup:
            updates["credits_cents"] = (profile.get("credits_cents", 0) or 0) - from_topup * COST_CENTS
        try:
            if updates:
                db.table("profiles").update(updates).eq("id", uid).execute()
        except Exception:
            logger.exception("brain_rescrape: charge failed uid=%s", uid)
            return jsonify({"error": "internal"}), 500

    # Encola el scrape del perfil propio. Si falla, refund.
    try:
        from tasks import scrape_creator_task  # noqa: E402
        scrape_creator_task.delay(creator_id)
    except Exception:
        logger.exception("brain_rescrape: enqueue failed uid=%s", uid)
        if not unlimited:
            try:
                fresh = get_profile(uid)
                refund = {}
                if from_monthly:
                    refund["monthly_usage"] = max(0, (fresh.get("monthly_usage", 0) or 0) - from_monthly)
                if from_topup:
                    refund["credits_cents"] = (fresh.get("credits_cents", 0) or 0) + from_topup * COST_CENTS
                if refund:
                    db.table("profiles").update(refund).eq("id", uid).execute()
            except Exception:
                logger.exception("brain_rescrape: refund failed uid=%s", uid)
        return jsonify({"error": "enqueue_failed",
                        "message": "No pude encolar el análisis. Inténtalo en un momento."}), 503

    track_event("brain_rescrape", uid, {"handle": ig_username, "cost_credits": (0 if unlimited else n)})
    fresh = get_profile(uid)
    return jsonify({"ok": True, "credits": credits_available(fresh),
                    "message": "Análisis de tu perfil encolado — tu nivel sube en cuanto termine."}), 200


@app.route("/api/suggested-competitor", methods=["GET"])
@require_auth
@limiter.limit("60 per hour")
def suggested_competitor():
    """Sugerir competidores (Fathom 18/06): un creador del nicho que el usuario NO sigue
    aún y está en alza. Heurística por CO-OCURRENCIA (quien sigue a tus competidores
    también sigue a estos) → niche-relevante sin tag explícito. Acompaña el mejor
    candidato de un reel reciente que petó. La UI ya existe (suggestedComp)."""
    user = current_user()
    uid = user["id"]
    try:
        limit = max(1, min(8, int(request.args.get("limit", 1))))
    except Exception:
        limit = 1

    def _seed_shaped(exclude_ids, exclude_handles, n):
        """Cold-start: rellena con competidores curados de la semilla por nicho."""
        if n <= 0:
            return []
        try:
            prof = (db.table("profiles").select("niche,subniches")
                    .eq("id", uid).single().execute()).data or {}
        except Exception:
            prof = {}
        seeds = _seed_competitors(prof.get("niche"), prof.get("subniches") or [],
                                  exclude_handles=exclude_handles, exclude_ids=exclude_ids, limit=n)
        return [{"handle": s["handle"], "why": "es un referente de tu nicho",
                 "x": "top", "tag": "Top en tu nicho"} for s in seeds]

    # 1. Mis competidores.
    try:
        mine_r = (db.table("user_tracked_creators").select("creator_id")
                    .eq("user_id", uid).is_("archived_at", "null").execute())
        mine = {r["creator_id"] for r in (mine_r.data or []) if r.get("creator_id")}
    except Exception:
        mine = set()
    # Anti-repetición: no sugerir creadores que el usuario DESCARTÓ. exclude_ids cubre
    # seguidos + descartados (los peers se buscan con `mine` real, no con descartados).
    dismissed = _dismissed_creator_ids(uid, request.args.get("project_id"))
    exclude_ids = mine | dismissed
    if not mine:
        seeds = _seed_shaped(exclude_ids, set(), limit)
        return jsonify({"suggestion": seeds[0] if seeds else None, "suggestions": seeds}), 200
    # 2. Peers que siguen a alguno de mis competidores.
    peer_ids = set()
    mine_list = list(mine)
    for i in range(0, len(mine_list), 100):
        try:
            pr = (db.table("user_tracked_creators").select("user_id")
                    .in_("creator_id", mine_list[i:i + 100]).is_("archived_at", "null")
                    .neq("user_id", uid).limit(3000).execute())
            peer_ids.update(r["user_id"] for r in (pr.data or []) if r.get("user_id"))
        except Exception:
            pass
    # 3. Creadores que esos peers siguen y yo NO → co-ocurrencia.
    counts = {}
    peer_list = list(peer_ids)
    for i in range(0, len(peer_list), 100):
        try:
            cr = (db.table("user_tracked_creators").select("creator_id")
                    .in_("user_id", peer_list[i:i + 100]).is_("archived_at", "null")
                    .limit(5000).execute())
            for r in (cr.data or []):
                cid = r.get("creator_id")
                if cid and cid not in exclude_ids:
                    counts[cid] = counts.get(cid, 0) + 1
        except Exception:
            pass
    if not counts:
        seeds = _seed_shaped(exclude_ids, set(), limit)
        return jsonify({"suggestion": seeds[0] if seeds else None, "suggestions": seeds}), 200
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    cand_ids = [c for c, _ in ranked]
    # 4. Mejor reel reciente (explosión) de los candidatos.
    baselines = _creator_view_baselines(cand_ids)
    try:
        reels = (db.table("creator_reels_global")
                   .select("creator_id, views, posted_at, thumb_url, thumb_b64")
                   .in_("creator_id", cand_ids).eq("is_archived", False)
                   .order("posted_at", desc=True).limit(240).execute()).data or []
    except Exception:
        reels = []
    best = {}
    for r in reels:
        cid = r.get("creator_id"); v = int(r.get("views") or 0)
        exp = _explosion_score(v, baselines.get(cid))
        score = exp if exp is not None else 0
        if cid and (cid not in best or score > best[cid]["score"]):
            best[cid] = {"views": v, "exp": exp, "score": score,
                         "thumb": r.get("thumb_b64") or r.get("thumb_url")}  # #4: su reel que petó
    # Orden: primero los que tienen un reel que petó (views>0), luego el resto por
    # co-ocurrencia. ?limit=N (muro de competidores, Bernat 24-jun) → lista de N.
    ordered = [cid for cid, _ in ranked if (best.get(cid) or {}).get("views")]
    ordered += [cid for cid, _ in ranked if cid not in ordered]
    try:
        cgs = (db.table("creators_global").select("id, ig_username")
                 .in_("id", ordered).execute()).data or []
        handle_by_id = {r["id"]: (r.get("ig_username") or "").lstrip("@") for r in cgs}
    except Exception:
        handle_by_id = {}

    def _mk_suggestion(cid):
        handle = handle_by_id.get(cid) or ""
        if not handle:
            return None
        info = best.get(cid) or {}
        exp = info.get("exp"); views = info.get("views") or 0
        if exp and exp >= 2:
            why = f"se pegó un reel de {_fmt_views(views)} (×{exp:g} su media)"; xtag = f"×{exp:g}"
        elif views:
            why = f"tiene un reel reciente de {_fmt_views(views)}"; xtag = _fmt_views(views)
        else:
            why = "está creciendo en tu nicho"; xtag = "en alza"
        sug = {"handle": handle, "why": why, "x": xtag, "tag": "Lo siguen en tu nicho"}
        # #4 (David 26-jun): el reel que está PETANDO de ese creador → da ganas de seguirlo.
        if info.get("thumb") and views:
            sug["reel"] = {"thumb": info.get("thumb"), "exp": exp, "views": _fmt_views(views)}
        return sug

    suggestions = [s for s in (_mk_suggestion(cid) for cid in ordered) if s]
    # Top-up con la semilla si la co-ocurrencia no llena el muro (cold-start / nicho
    # poco poblado). Excluye lo que ya sigo y lo ya sugerido.
    if len(suggestions) < limit:
        have = {(s["handle"] or "").lower() for s in suggestions}
        suggestions += _seed_shaped(exclude_ids, have, limit - len(suggestions))
    suggestions = suggestions[:limit]
    return jsonify({
        "suggestion": suggestions[0] if suggestions else None,
        "suggestions": suggestions,
    }), 200


@app.route("/api/niche/discover", methods=["GET"])
@require_auth
@limiter.limit("60 per hour")
def niche_discover():
    """Proactividad anti-churn (David 26-jun): reels que están PETANDO en tu nicho de
    creadores que AÚN NO SIGUES → «fíjate en esto aunque no sea tu competidor, róbalo».
    EXCLUYE a tus competidores activos. Reusa _recycled_reels (cero scrape) → siempre
    hay algo nuevo que robar aunque ya sigas a gente. Cada reel trae creator.ig_username
    para ofrecer «sigue a @X». Robar uno auto-sigue al creador (es seed-válido)."""
    user = current_user()
    uid = user["id"]
    try:
        limit = max(1, min(12, int(request.args.get("limit", "6"))))
    except (TypeError, ValueError):
        limit = 6
    project_id = request.args.get("project_id")
    tq = (db.table("user_tracked_creators").select("creator_id")
          .eq("user_id", uid).is_("archived_at", "null"))
    if project_id:
        tq = tq.eq("project_id", project_id)
    try:
        tracked_ids = list({t["creator_id"] for t in (tq.execute().data or [])})
    except Exception:
        tracked_ids = []
    # Descubrimiento = lo que PETA en tu nicho (no solo lo más visto) de gente que NO
    # sigues NI descartaste. rank="explosion" + umbral RADAR_FEED_EXPLODE_MIN.
    exclude = list(set(tracked_ids) | _dismissed_creator_ids(uid, project_id))
    prof = get_profile(uid)
    reels = _recycled_reels(prof.get("subniches"), prof.get("niche"),
                            limit=limit, exclude_creator_ids=exclude,
                            rank="explosion", min_explosion=RADAR_FEED_EXPLODE_MIN)
    return jsonify({"reels": reels, "total": len(reels)}), 200


def _brand_niche_subniches(profile, tracked_ids):
    """Señal de nicho POR MARCA: subniches del PERFIL + subniches de los competidores que
    ESTA marca sigue (su radar). Distintas marcas → distintos competidores → distintos
    subnichos → distintas sugerencias, todo ON-NICHE (no se sale del nicho)."""
    subs = {t for t in (_norm_tag(s) for s in (profile.get("subniches") or [])) if t}
    if not subs and profile.get("niche"):
        subs = {t for t in (_norm_tag(s) for s in _subniche_suggestions(profile["niche"])) if t}
    ids = [c for c in (tracked_ids or []) if c]
    if ids:
        try:
            for i in range(0, len(ids), 100):
                for r in (db.table("creators_global").select("subniches")
                            .in_("id", ids[i:i + 100]).execute()).data or []:
                    subs |= {t for t in (_norm_tag(s) for s in (r.get("subniches") or [])) if t}
        except Exception:
            logger.warning("[sugg] brand subniches read failed")
    return [s for s in subs][:16]


_FORMATO_SHORT = {
    "selfie": "selfie a cámara", "pizarra": "pizarra", "podcast": "clip de podcast",
    "escritorio": "pantalla/escritorio", "broll-vo": "B-roll + voz en off", "pov": "POV + texto",
}


def _suggestion_reason(exp, formato, age_days):
    """«Por qué robarlo» DETERMINISTA (cero coste, sin LLM, sin caché): se deriva de señales
    que ya tenemos (explosión + formato + nicho/frescura). P.ej.
    «×46 sobre su media · formato POV + texto · pega en tu nicho»."""
    parts = ["×%g sobre su media" % round(exp, 1)]
    fl = _FORMATO_SHORT.get(formato)
    if fl:
        parts.append("formato %s" % fl)
    if age_days is not None and age_days <= 4:
        parts.append("recién publicado")
    else:
        parts.append("pega en tu nicho")
    return " · ".join(parts)


def _niche_suggestion_reels(subniches, niche=None, exclude_creator_ids=None, limit=15,
                            shuffle_seed=None, offset=0):
    """«Sugerencias de hoy» orientado a REELS: reels que PETAN en el nicho de creadores que
    el user NO sigue ni descartó. Cada reel es ROBABLE sin seguir al creador. `worth_follow`
    marca a los creadores que MERECE seguir (listón alto) → la 2ª acción «+ Añadir competidor»
    solo se ofrece en esos (menos sugerencias de seguir, más curadas). SOLO on-niche; cero
    scrape (pool cacheado). `shuffle_seed` re-baraja el orden (botón «↻ otras» GRATIS, sin
    scrape); `offset` pagina (tandas del carrusel). Devuelve reels normReel-compat+worth_follow."""
    subs = [t for t in (_norm_tag(s) for s in (subniches or [])) if t][:16]
    # El propio nicho normalizado suele ser un SUBNICHE válido en la taxonomía rica de
    # creators_global ("inteligencia artificial", "marketing digital", "espiritualidad y
    # mindfulness"…). Inclúyelo como candidato de overlap → recupera nichos de texto libre
    # con subnichos vacíos/genéricos (antes derivaban "consejos/tutoriales" → 0 overlap).
    nnorm = _norm_tag(niche or "")
    if nnorm and nnorm not in subs:
        subs = ([nnorm] + subs)[:16]
    if not subs and niche:
        subs = [t for t in (_norm_tag(s) for s in _subniche_suggestions(niche)) if t][:16]
    if not subs:
        return []
    subs_set = set(subs)
    # ENDURECIDO on-niche: una SOLA subniche genérica compartida (p.ej. Ibai tagueado solo
    # "ia") NO basta para colar a un creador. Pedimos ≥2 subnichos solapados cuando el nicho
    # tiene ≥2 tags.
    min_overlap = 2 if len(subs_set) >= 2 else 1
    exclude = set(exclude_creator_ids or [])
    uname = {}
    try:
        for c in (db.table("creators_global").select("id, ig_username, subniches")
                    .overlaps("subniches", subs).limit(600).execute()).data or []:
            cid = c.get("id")
            if not cid or cid in exclude:
                continue
            csubs = {_norm_tag(s) for s in (c.get("subniches") or []) if s}
            if len(csubs & subs_set) >= min_overlap:   # solape FUERTE → on-niche de verdad
                uname[cid] = c.get("ig_username") or ""
    except Exception:
        logger.warning("[sugg] subniche overlap failed (¿migración subniches?)")
    # FALLBACK por NICHE AMPLIO: si el overlap fuerte no encontró a NADIE (nicho con pocos/0
    # subnichos en el pool), casa por nicho canónico en vez de devolver 0. Solo como red de
    # seguridad (no como widener permanente) → no reintroduce la fuga off-niche del caso normal.
    if not uname:
        pn = _pool_niche_canon(niche)
        if pn:
            try:
                for c in (db.table("creators_global").select("id, ig_username")
                            .eq("niche", pn).limit(600).execute()).data or []:
                    cid = c.get("id")
                    if cid and cid not in exclude:
                        uname[cid] = c.get("ig_username") or ""
            except Exception:
                pass
    cids = list(uname.keys())
    if not cids:
        return []
    baselines = _creator_view_baselines(cids)
    # FRESCURA: solo reels de ≤30 días (mejor pocas recientes que rellenar con viejos).
    fresh_cutoff = (datetime.now(timezone.utc) - timedelta(days=RADAR_SUGG_MAX_AGE_DAYS)).isoformat()
    try:
        rr = (db.table("creator_reels_global")
                .select("id, ig_reel_id, creator_id, caption, views, likes, comments, "
                        "posted_at, video_duration_sec, formato")
                .in_("creator_id", cids).eq("is_archived", False)
                .gte("posted_at", fresh_cutoff)
                .order("views", desc=True).limit(max(limit * 15, 360)).execute()).data or []
    except Exception:
        rr = []
    # explosión por reel.
    by_creator = {}
    for r in rr:
        cid = r.get("creator_id"); v = int(r.get("views") or 0)
        r["_exp"] = _explosion_score(v, baselines.get(cid)) or 0.0
        by_creator.setdefault(cid, []).append(r)
    REEL_FLOOR = 1.2      # MÁS reels: umbral bajo para que un reel APAREZCA
    FOLLOW_MIN_EXP = 2.0  # «petando» a efectos de seguir
    FOLLOW_MIN_HITS = 2   # CURACIÓN: «+ Añadir competidor» solo si el creador peta de FORMA
                          # CONSISTENTE (≥2 reels petando), no un único viral de chiripa.
    PER_CREATOR = 2       # hasta 2 reels por creador → más volumen sin que uno domine
    sseed = str(shuffle_seed) if shuffle_seed else None
    chosen = []
    for cid, lst in by_creator.items():
        lst.sort(key=lambda r: -(r.get("_exp") or 0.0))
        wf = sum(1 for x in lst if (x.get("_exp") or 0.0) >= FOLLOW_MIN_EXP) >= FOLLOW_MIN_HITS
        for r in lst[:PER_CREATOR]:
            if (r.get("_exp") or 0.0) >= REEL_FLOOR:
                # 1ª vista (sseed=None) → orden por explosión (lo que MÁS peta primero).
                # «↻ otras» (sseed) → baraja FUERTE (hash puro): todos ya pasan REEL_FLOOR
                # (petan), así que variar el orden trae otros reels on-niche, sin scrape. El
                # offset (paginado de «ver más») es estable mientras no se rebaraje.
                if sseed:
                    score = _unit_hash("%s:%s" % (r.get("id"), sseed))
                else:
                    score = r.get("_exp") or 0.0
                chosen.append((score, r, wf))
    chosen.sort(key=lambda x: -x[0])
    now = datetime.now(timezone.utc)
    out = []
    for _score, r, wf in chosen[offset:offset + limit]:
        cid = r.get("creator_id"); sc = r.get("ig_reel_id")
        h = (uname.get(cid) or "").lstrip("@")
        ts = _parse_ts(r.get("posted_at"))
        age_days = int((now - ts).total_seconds() // 86400) if ts else None
        rexp = r.get("_exp") or 0.0   # explosión REAL para mostrar (no el score barajado)
        out.append({
            "id": r.get("id"), "ig_reel_id": sc, "creator_id": cid,
            "creator": {"ig_username": h},
            "thumb_url": "/img/reel/%s" % r.get("id"),
            "url": ("https://www.instagram.com/reel/%s/" % sc) if sc else None,
            "caption": (r.get("caption") or "")[:600],
            "views": r.get("views"), "likes": r.get("likes"), "comments": r.get("comments"),
            "posted_at": r.get("posted_at"), "video_duration_sec": r.get("video_duration_sec"),
            "explosion_score": round(rexp, 2), "formato": r.get("formato"),
            "why": _suggestion_reason(rexp, r.get("formato"), age_days),
            "worth_follow": bool(wf), "source": "suggestion",
        })
    return out


def _resolve_brand_niche(uid, project_id):
    """Nicho efectivo de la marca: proyecto → su nicho; si no tiene, HEREDA el del perfil
    (onboarding); marca default → perfil. Devuelve (niche, subs). (None vacío en ambos ⇒
    needs_niche en el caller)."""
    niche, subs = "", []
    if project_id:
        try:
            proj = (db.table("projects").select("niche, subniches")
                      .eq("id", project_id).eq("user_id", uid).single().execute()).data or {}
        except Exception:
            proj = {}
        subs = [t for t in (_norm_tag(s) for s in (proj.get("subniches") or [])) if t]
        niche = (proj.get("niche") or "").strip()
    if not subs and not niche:
        prof = get_profile(uid)
        niche = (prof.get("niche") or "").strip()
        subs = [t for t in (_norm_tag(s) for s in (prof.get("subniches") or [])) if t]
    return niche, subs


def _sugg_exclude(uid, project_id):
    """creator_ids a excluir de las sugerencias: ya seguidos (de la marca) + descartados."""
    tq = (db.table("user_tracked_creators").select("creator_id")
            .eq("user_id", uid).is_("archived_at", "null"))
    if project_id:
        tq = tq.eq("project_id", project_id)
    try:
        tracked = {t["creator_id"] for t in (tq.execute().data or [])}
    except Exception:
        tracked = set()
    return tracked | _dismissed_creator_ids(uid, project_id)


@app.route("/api/radar/suggestions", methods=["GET"])
@require_auth
@limiter.limit("60 per hour")
def radar_suggestions():
    """«Sugerencias de hoy» (carrusel de REELS): reels que PETAN en el nicho de creadores
    que NO sigues ni descartaste. Cada reel se ROBA sin seguir; worth_follow ofrece «+
    Añadir competidor» solo en los que merece. Cero scrape (pool cacheado). Devuelve la
    VENTANA GRATIS (SUGG_FREE_N); `has_more` indica si hay más tras pagar («ver más»)."""
    user = current_user()
    uid = user["id"]
    project_id = request.args.get("project_id")
    exclude = _sugg_exclude(uid, project_id)
    niche, subs = _resolve_brand_niche(uid, project_id)
    if not subs and not niche:
        # Sin nicho en NINGÚN sitio → needs_niche (la UI pide definirlo, no vacío en silencio).
        return jsonify({"suggestions": [], "total": 0, "needs_niche": True}), 200
    sseed = _reshuffle_nonce(uid, project_id)
    # +1 para saber si hay material más allá de la ventana gratis (sin un 2º query).
    batch = _niche_suggestion_reels(subs, niche, exclude, SUGG_FREE_N + 1, shuffle_seed=sseed)
    has_more = len(batch) > SUGG_FREE_N
    return jsonify({"suggestions": batch[:SUGG_FREE_N], "total": len(batch[:SUGG_FREE_N]),
                    "free_n": SUGG_FREE_N, "has_more": has_more, "more_units": SUGG_MORE_UNITS,
                    "more_batch": SUGG_MORE_BATCH}), 200


@app.route("/api/radar/reshuffle", methods=["POST"])
@require_auth
@limiter.limit("30 per minute")
def radar_reshuffle():
    """«↻ otras» GRATIS: re-baraja el orden del pool YA scrapeado (sugerencias + feed),
    SIN scrape. Sube el nonce de re-baraja e invalida la caché del feed → el siguiente load
    varía. NUNCA encola scrape (eso es solo /api/radar/refresh-now, de pago)."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id") or None
    nonce = _reshuffle_bump(uid, project_id)
    _radar_feed_invalidate(uid, project_id)
    track_event("radar_reshuffle", uid, {"project_id": project_id})
    return jsonify({"ok": True, "queued": 0, "nonce": nonce, "message": "Otras sugerencias."}), 200


@app.route("/api/radar/suggestions/more", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def radar_suggestions_more():
    """«Ver más» del carrusel: tras las SUGG_FREE_N gratis, cada tanda CUESTA SUGG_MORE_UNITS
    créditos (monetización, CERO scrape — el pool ya está). Cobra dual-rail (créditos / uso
    mensual). Cobra SOLO si devuelve reels nuevos."""
    user = current_user()
    uid = user["id"]
    email = (user.get("email") or "").lower()
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id") or None
    try:
        offset = max(SUGG_FREE_N, min(SUGG_MAX_TOTAL, int(body.get("offset") or SUGG_FREE_N)))
    except (TypeError, ValueError):
        offset = SUGG_FREE_N
    if offset >= SUGG_MAX_TOTAL:
        return jsonify({"ok": True, "suggestions": [], "has_more": False, "charged": False}), 200
    niche, subs = _resolve_brand_niche(uid, project_id)
    if not subs and not niche:
        return jsonify({"ok": False, "error": "no_niche"}), 400
    exclude = _sugg_exclude(uid, project_id)
    sseed = _reshuffle_nonce(uid, project_id)
    # +1 para has_more sin 2º query. Cobramos ANTES solo si confirmamos que hay tanda nueva.
    batch = _niche_suggestion_reels(subs, niche, exclude, SUGG_MORE_BATCH + 1,
                                    shuffle_seed=sseed, offset=offset)
    reels = batch[:SUGG_MORE_BATCH]
    if not reels:
        return jsonify({"ok": True, "suggestions": [], "has_more": False, "charged": False}), 200
    # Cobro dual-rail (cortesía gratis). Mismo patrón que refresh-now.
    is_courtesy = email in UNLIMITED_EMAILS
    if not is_courtesy:
        profile = get_profile(uid)
        plan = profile.get("plan", "free")
        is_paid_unlimited = plan in ("pro", "creator", "estudio", "agency")
        if not is_paid_unlimited and (profile.get("credits_cents") or 0) < SUGG_MORE_COST:
            return jsonify({"ok": False, "error": "no_credits",
                            "message": "Necesitas %d créditos para ver más." % SUGG_MORE_UNITS}), 402
        try:
            if is_paid_unlimited:
                db.table("profiles").update({
                    "monthly_usage": (profile.get("monthly_usage") or 0) + SUGG_MORE_UNITS
                }).eq("id", uid).execute()
            else:
                db.table("profiles").update({
                    "credits_cents": (profile.get("credits_cents") or 0) - SUGG_MORE_COST
                }).eq("id", uid).execute()
        except Exception:
            logger.warning("[sugg-more] cobro falló uid=%s", uid)
            return jsonify({"ok": False, "error": "charge_failed"}), 500
    track_event("sugg_more", uid, {"project_id": project_id, "offset": offset, "n": len(reels)})
    return jsonify({"ok": True, "suggestions": reels, "charged": not is_courtesy,
                    "has_more": len(batch) > SUGG_MORE_BATCH and (offset + SUGG_MORE_BATCH) < SUGG_MAX_TOTAL,
                    "more_units": SUGG_MORE_UNITS, "more_batch": SUGG_MORE_BATCH}), 200


@app.route("/api/niche/subniche-suggestions", methods=["GET"])
@require_auth
@limiter.limit("120 per hour")
def niche_subniche_suggestions():
    """Sugerencias de subniche-tags para un nicho dado (picker de nicho del proyecto).
    Reusa NICHE_SUBNICHE_SEED (cero coste)."""
    niche = (request.args.get("niche") or "").strip()
    return jsonify({"suggestions": (_subniche_suggestions(niche) if niche else [])}), 200


@app.route("/api/profile/niche", methods=["POST"])
@require_auth
@limiter.limit("20 per hour")
def set_profile_niche():
    """Fija el nicho/subnichos del PERFIL desde el CTA «Define tu nicho» (cuando el usuario
    no tiene nicho ni en perfil ni en proyecto → marca default/free). Solo escribe
    niche/subniches; NO cierra el onboarding ni encola scrapes."""
    user = current_user()
    uid = user["id"]
    body = request.get_json() or {}
    niche = (body.get("niche") or "").strip()[:80]
    subs = [t for t in (_norm_tag(s) for s in (body.get("subniches") or [])) if t][:8]
    if not niche and not subs:
        return jsonify({"error": "empty", "message": "Escribe tu nicho."}), 400
    try:
        db.table("profiles").update({"niche": niche or None, "subniches": subs}).eq("id", uid).execute()
    except Exception:
        logger.warning("[niche] profile niche update failed", exc_info=True)
        return jsonify({"error": "save_failed", "message": "No se pudo guardar el nicho."}), 500
    return jsonify({"ok": True, "niche": niche, "subniches": subs}), 200


@app.route("/api/onboarding/complete", methods=["POST"])
@require_auth
@limiter.limit("10 per hour;30 per day")
def onboarding_complete():
    """CIERRE del onboarding v2: persiste nicho/subnichos/objetivo, etiqueta a los
    competidores elegidos con esos subnichos (enriquece la librería = el foso),
    los sigue (scrape SOLO lo nuevo, async) y siembra un Cerebro ~50% HONESTO:
    un PERFIL DE CONTEXTO (nicho+objetivo+competencia), NO un clon de voz. El
    resto (50→100%) se gana entrenando la voz con los reels propios."""
    user = current_user()
    uid = user["id"]
    body = request.get_json() or {}
    niche = (body.get("niche") or "").strip()[:80]
    subs = [t for t in (_norm_tag(s) for s in (body.get("subniches") or [])) if t][:8]
    goal = (body.get("goal") or "").strip()[:20]
    skipped = bool(body.get("skipped"))
    own_handle = (body.get("handle") or "").strip().lstrip("@").lower()
    comps = []
    for c in (body.get("competitors") or [])[:5]:
        h = (c or "").strip().lstrip("@").lower()
        if re.match(r"^[a-z0-9._]{1,30}$", h):
            comps.append(h)
    track_event("onboarding_v2_completed", uid, {
        "niche": niche, "subniches": len(subs), "goal": goal,
        "competitors": len(comps), "skipped": skipped})

    # 1) persistir nicho/subnichos/objetivo (degradado si faltan columnas)
    #    + seed del % del Cerebro a 35 (Fathom 18/06: sube +3-6%/día con el ejercicio).
    try:
        db.table("profiles").update({
            "niche": niche or None, "subniches": subs,
            "goal": goal or None, "onboarding_v2_done": True,
            "brain_progress": 35}).eq("id", uid).execute()
    except Exception:
        logger.warning("[onb2] profiles update failed (¿migración?)", exc_info=True)
        try:
            db.table("profiles").update({"onboarding_v2_done": True}).eq("id", uid).execute()
        except Exception:
            pass

    if skipped:
        return jsonify({"ok": True, "skipped": True, "voice": 0}), 200

    # 2) seguir + etiquetar competidores (scrape solo lo nuevo, async).
    #    #3 CAP: el onboarding da la "probada" de 2 (parte del aha) PERO sin abrir
    #    la puerta a ilimitados. Tope = max(slots del plan, 2). El conteo activo se
    #    RE-LEE del DB en cada llamada, así que repetir este endpoint no acumula
    #    más allá del tope; y cualquier add posterior via POST /api/tracked-creators
    #    re-aplica el cap estricto del plan (free=1, 2>=1 → bloquea).
    ONBOARDING_GRACE = 2
    project_id = body.get("project_id")
    try:
        _profile = get_profile(uid)
        _slots = get_tracked_creators_limit(effective_plan(_profile))["base_slots_global"] + get_user_extra_slots(uid)
    except Exception:
        _slots = 1
    cap = max(_slots, ONBOARDING_GRACE)
    try:
        active = count_active_tracked(uid, scope="global")
    except Exception:
        active = 0
    tracked = 0
    for h in comps:
        if active + tracked >= cap:
            logger.info("[onb2] cap alcanzado (%s/%s) — no se sigue %s", active + tracked, cap, h)
            break
        try:
            if _track_and_tag_creator(uid, h, subs, project_id):
                tracked += 1
        except Exception:
            logger.exception("[onb2] track competitor %s", h)

    # 3) Cerebro ~50% HONESTO: perfil de contexto sembrado (no clon de voz).
    voice = 50
    try:
        db.table("voice_profiles").upsert({
            "user_id": uid, "brand_id": "", "confidence": voice, "source_count": 0,
            "tone": "", "phrases": [],
            "structure": "perfil de contexto (nicho + objetivo + competencia)",
            "raw": {"seed": "onboarding_v2", "niche": niche, "subniches": subs, "goal": goal,
                    "label": "perfil de contexto, no clon de voz",
                    "evidence": ["Nicho y subnicho definidos",
                                 f"{tracked} competidor(es) en el radar",
                                 "Objetivo: " + (goal or "—")]},
        }, on_conflict="user_id,brand_id").execute()
    except Exception:
        logger.warning("[onb2] voice seed failed", exc_info=True)
        voice = 40

    # 4) SEED REAL del Cerebro (parte A del loop): scrape del handle del usuario →
    #    sus reels → transcribe → deriva voz + guarda «así escribe él» como few-shot.
    #    Async (no bloquea el onboarding); idempotente; coste ~€0,04 una vez.
    if own_handle and re.match(r"^[a-z0-9._]{1,30}$", own_handle):
        try:
            from tasks import seed_voice_from_handle_task  # noqa: E402
            seed_voice_from_handle_task.delay(uid, own_handle, project_id)
            track_event("onboarding_voice_seed_queued", uid, {"handle": own_handle})
        except Exception:
            logger.warning("[onb2] seed voice enqueue failed", exc_info=True)

    return jsonify({"ok": True, "voice": voice, "tracked": tracked, "subniches": subs}), 200


@app.route("/api/tracked-creators", methods=["POST"])
@require_auth
@limiter.limit("10 per minute")
def post_tracked_creator():
    user = current_user()
    profile = get_profile(user["id"])
    # reverse-trial: durante el trial, límites de competidores del plan EFECTIVO
    # (creator → 5 slots). Al expirar → free (1 slot).
    plan = effective_plan(profile)
    body = request.get_json() or {}

    # 1. Validar input
    ig_username = (body.get("ig_username") or "").strip().lstrip("@").lower()
    if not re.match(r"^[a-zA-Z0-9._]{1,30}$", ig_username):
        return jsonify({"error": "tc.error.invalid_username"}), 400

    project_id = body.get("project_id") or None

    # 2. Plan habilitado
    limits = get_tracked_creators_limit(plan)
    if not limits["enabled"]:
        return jsonify({"error": "tc.error.upgrade_required"}), 403

    # 3. project_id según plan (B5: competidores aislados por MARCA en multi-marca).
    if plan == "agency":
        if not project_id:
            return jsonify({"error": "tc.error.project_required"}), 400
    if project_id:
        # Ownership: SOLO el owner del proyecto (vale para agency Y estudio multi-marca).
        proj = (db.table("projects")
                  .select("id, user_id")
                  .eq("id", project_id)
                  .eq("user_id", user["id"])
                  .execute())
        if not proj.data:
            # Creator (1 marca) o project_id ajeno/centinela → competidor global del user.
            project_id = None

    # 4. Validar límites
    extra_slots = get_user_extra_slots(user["id"])
    cap_global = limits["base_slots_global"] + extra_slots

    # econ: más allá del cupo del plan, añadir un competidor NUEVO cuesta
    # ADD_COMPETITOR_UNITS cr (scrape+transcribe inicial). Cupo blando: el free
    # sin créditos sigue topando con el muro; con saldo, paga y expande.
    charge_competitor = False
    if count_active_tracked(user["id"], scope="global") >= cap_global:
        if credits_available(profile) < ADD_COMPETITOR_UNITS:
            # growth-1: paywall_shown. El free llega aquí al intentar el 2º competidor
            # (slot=1). after_first_value=True: ya tiene 1 competidor dándole señales.
            track_event("paywall_shown", user["id"], {
                "wall": "tracked_creators", "plan": plan, "limit": cap_global,
                "after_first_value": cap_global >= 1,
            })
            return jsonify({"error": "tc.error.plan_limit_reached", "limit": cap_global}), 403
        charge_competitor = True

    # Límite POR MARCA: aplica a cualquier plan con per_project_slots cuando se añade a
    # una marca concreta (project_id). Sin project_id (marca por defecto) → solo el global.
    if limits.get("per_project_slots") is not None and project_id:
        active_in_project = count_active_tracked(user["id"], project_id=project_id, scope="project")
        if active_in_project >= limits["per_project_slots"]:
            return jsonify({"error": "tc.error.project_limit_reached", "limit": limits["per_project_slots"]}), 403

    # 5. UPSERT creator en creators_global (idempotente vía ON CONFLICT).
    # Supabase-py no expone ON CONFLICT directo en upsert para retornar fila siempre,
    # pero usar upsert con ignore_duplicates=False respeta unique constraint.
    try:
        ins = (db.table("creators_global")
                 .upsert({"ig_username": ig_username}, on_conflict="ig_username")
                 .execute())
        creator_row = (ins.data or [None])[0]
        if not creator_row:
            # Si el upsert no devolvió fila (algunos drivers), re-leemos.
            sel = db.table("creators_global").select("*").eq("ig_username", ig_username).single().execute()
            creator_row = sel.data
    except Exception as e:
        logger.exception("upsert creators_global failed for %s: %s", ig_username, e)
        return jsonify({"error": "tc.error.internal"}), 500

    creator_id = creator_row["id"]

    # 6. Idempotencia: ¿ya existe tracking activo?
    existing_q = (db.table("user_tracked_creators")
                    .select("id")
                    .eq("user_id", user["id"])
                    .eq("creator_id", creator_id)
                    .is_("archived_at", "null"))
    if project_id is None:
        existing_q = existing_q.is_("project_id", "null")
    else:
        existing_q = existing_q.eq("project_id", project_id)
    existing = existing_q.execute()
    if existing.data:
        return jsonify({"error": "tc.error.already_tracking"}), 409

    # 6.b econ: cobrar el competidor extra (más allá del cupo) ANTES del insert.
    _comp_refund = lambda: None
    if charge_competitor:
        _err, _comp_refund, _ = _charge_units_locked(user["id"], ADD_COMPETITOR_UNITS, user)
        if _err:
            return _err

    # 7. INSERT tracking
    payload = {
        "user_id": user["id"],
        "creator_id": creator_id,
        "project_id": project_id,
    }
    try:
        tr = db.table("user_tracked_creators").insert(payload).execute()
        tracking_row = tr.data[0]
    except Exception as e:
        logger.exception("insert user_tracked_creators failed: %s", e)
        _comp_refund()   # revertir el cargo del competidor si el insert falla
        return jsonify({"error": "tc.error.internal"}), 500

    # growth-2: si este es el 1er competidor del usuario (0→1), el onboarding de
    # activación quedó completado (handle → competencia en el radar). Señal de
    # funnel server-side fiable; `source` distingue el onboarding del alta suelta.
    try:
        _total_tracked = count_active_tracked(user["id"], scope="global")
        if _total_tracked == 1:
            track_event("onboarding_completed", user["id"], {
                "source": (body.get("source") or "manual"),
                "first_creator": ig_username,
            })
        # panel: cada alta de competidor (no solo la 1ª) → tile «competidores añadidos».
        track_event("competitor_added", user["id"], {
            "source": (body.get("source") or "manual"),
            "creator": ig_username,
            "total": _total_tracked,
        })
    except Exception:
        pass

    # 8. v0.15.3: auto-encolar scrape si data nueva o stale (>24h).
    #    Reusa cache si data fresca (<24h, diseñado en Fase 0 para ahorrar Apify).
    #    No re-encolar si status definitivo (private/not_found).
    _last_scraped = creator_row.get("last_scraped_at")
    _status = creator_row.get("scrape_status")
    _should_scrape = False
    _reason = None
    if _status in ("private", "not_found"):
        _reason = f"skip:status={_status}"
    elif _last_scraped is None:
        _should_scrape = True
        _reason = "new_creator"
    else:
        try:
            last_dt = datetime.fromisoformat(str(_last_scraped).replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
            if age_hours > 24:
                _should_scrape = True
                _reason = f"stale:{age_hours:.1f}h"
            else:
                _reason = f"cache:{age_hours:.1f}h"
        except Exception:
            _should_scrape = True
            _reason = "parse_error_treat_as_stale"
    if _should_scrape:
        try:
            from tasks import scrape_creator_task  # noqa: E402
            scrape_creator_task.delay(creator_id)
            logger.info("[scrape] auto-enqueued %s (creator=%s, reason=%s)",
                        ig_username, creator_id, _reason)
        except Exception as e:
            logger.warning("[scrape] auto-enqueue failed for %s: %s", ig_username, e)
    else:
        logger.info("[scrape] reuse cache for %s (creator=%s, reason=%s)",
                    ig_username, creator_id, _reason)

    # Feed v1: invalida la caché del día → el nuevo competidor entra ya en el re-rank
    # (sus reels al completar el scrape los mete scrape_creator_task, que también invalida).
    _radar_feed_invalidate(user["id"], tracking_row.get("project_id"))

    return jsonify({
        "tracking": {
            "id": tracking_row["id"],
            "project_id": tracking_row.get("project_id"),
            "added_at": tracking_row.get("added_at"),
            "creator": {
                "id": creator_row["id"],
                "ig_username": creator_row["ig_username"],
                "scrape_status": creator_row.get("scrape_status"),
                "last_scraped_at": creator_row.get("last_scraped_at"),
                "followers_count_cached": creator_row.get("followers_count_cached"),
            },
        }
    }), 201


@app.route("/api/tracked-creators", methods=["GET"])
@require_auth
def get_tracked_creators():
    user = current_user()
    profile = get_profile(user["id"])
    plan = effective_plan(profile)   # reverse-trial: límites del plan efectivo
    project_id = request.args.get("project_id")

    limits = get_tracked_creators_limit(plan)
    extra_slots = get_user_extra_slots(user["id"])

    q = (db.table("user_tracked_creators")
           .select("id, project_id, added_at, weekly_digest_enabled, "
                   "creator:creators_global(id, ig_username, profile_data, "
                   "followers_count_cached, last_scraped_at, scrape_status, last_error)")
           .eq("user_id", user["id"])
           .is_("archived_at", "null")
           .order("added_at", desc=True))
    if project_id:
        q = q.eq("project_id", project_id)
    rows = q.execute()
    tracked = rows.data or []

    # Conteo reels por creador (1 query agrupada)
    creator_ids = [t["creator"]["id"] for t in tracked if t.get("creator")]
    reels_count_map: dict[str, int] = {}
    if creator_ids:
        try:
            # Supabase-py no soporta GROUP BY directo; hacemos N queries con count.
            # Para N pequeño (<=20) es aceptable. Optimización futura: rpc.
            for cid in creator_ids:
                # v0.15.2: filtrar is_archived=false (retention futura).
                rc = (db.table("creator_reels_global")
                        .select("id", count="exact")
                        .eq("creator_id", cid)
                        .eq("is_archived", False)
                        .execute())
                reels_count_map[cid] = rc.count or 0
        except Exception as e:
            logger.warning("reels count failed: %s", e)

    for t in tracked:
        cid = t.get("creator", {}).get("id")
        t["reels_count"] = reels_count_map.get(cid, 0)

    return jsonify({
        "tracked": tracked,
        "limits": {
            "plan": plan,
            "enabled": limits["enabled"],
            "base_slots_global": limits["base_slots_global"],
            "extra_slots": extra_slots,
            "total_cap_global": limits["base_slots_global"] + extra_slots,
            "per_project_slots": limits["per_project_slots"],
            "requires_project": limits["requires_project"],
        },
        "usage": {
            "active_global": count_active_tracked(user["id"], scope="global"),
            # Contador POR MARCA (UI): si la request trae project_id y el plan tiene
            # límite por marca, devolvemos uso/límite de ESA marca; si no, el global.
            "active_in_project": (count_active_tracked(user["id"], project_id=project_id, scope="project")
                                  if project_id else None),
            "per_brand_used": (count_active_tracked(user["id"], project_id=project_id, scope="project")
                               if (project_id and limits["per_project_slots"] is not None)
                               else count_active_tracked(user["id"], scope="global")),
            "per_brand_limit": (limits["per_project_slots"]
                                if (project_id and limits["per_project_slots"] is not None)
                                else limits["base_slots_global"] + extra_slots),
        }
    })


@app.route("/api/tracked-creators/<tracking_id>", methods=["DELETE"])
@require_auth
def delete_tracked_creator(tracking_id: str):
    user = current_user()
    # Soft delete con ownership inline (RLS también lo enforcearía, pero el
    # backend usa service_role que bypassa; validamos explícitamente).
    now_iso = datetime.now(timezone.utc).isoformat()
    res = (db.table("user_tracked_creators")
             .update({"archived_at": now_iso})
             .eq("id", tracking_id)
             .eq("user_id", user["id"])
             .is_("archived_at", "null")
             .execute())
    if not res.data:
        return jsonify({"error": "tc.error.tracking_not_found"}), 404
    _radar_feed_invalidate(user["id"], (res.data[0] or {}).get("project_id"))  # feed v1: re-rank sin el quitado
    return "", 204


def _views_stats(views_newest_first):
    """De una lista de views (reels ordenados del MÁS reciente al más antiguo)
    devuelve {avg, best, reels, growth}. growth% = media de los 3 reels más
    nuevos vs los 3 siguientes (momentum real, no seguidores). Sin suficientes
    reels → growth=0. avg/best son enteros de views."""
    vs = [int(v or 0) for v in (views_newest_first or [])]
    if not vs:
        return {"avg": 0, "best": 0, "reels": 0, "growth": 0}
    avg = sum(vs) // len(vs)
    best = max(vs)
    growth = 0
    if len(vs) >= 4:
        n = min(3, len(vs) // 2)
        recent = sum(vs[:n]) / n
        prev = sum(vs[n:2 * n]) / n
        if prev > 0:
            growth = max(-95, min(300, round((recent - prev) / prev * 100)))
    return {"avg": avg, "best": best, "reels": len(vs), "growth": growth}


@app.route("/api/leaderboard", methods=["GET"])
@require_auth
def leaderboard():
    """Ranking REAL del nicho (Fathom 18/06) — tú vs tus competidores por VIEWS
    MEDIAS por reel. Nota: el scraper de reels NO trae nº de seguidores, así que
    la métrica honesta y comparable en ambos lados son las views (que ya
    scrapeamos): competidores ← creator_reels_global, tú ← ig_videos. Es además
    la misma base que el Versus «Supéralo». followers se incluye solo si está
    cacheado (hoy NULL); el front no lo muestra si falta."""
    user = current_user()
    uid = user["id"]
    pid = _req_project_id()   # marca activa: el ranking es SOLO de sus competidores + su IG

    # 1. Mis competidores trackeados (de ESTA marca; sin pid = cuenta por defecto).
    try:
        tcq = (db.table("user_tracked_creators")
                .select("project_id, creator:creators_global(id, ig_username, followers_count_cached)")
                .eq("user_id", uid).is_("archived_at", "null").limit(60))
        if pid:
            tcq = tcq.eq("project_id", pid)
        tc = tcq.execute()
    except Exception:
        logger.exception("leaderboard: tracked read failed uid=%s", uid)
        tc = type("X", (), {"data": []})()
    comps = {}
    for t in (tc.data or []):
        c = t.get("creator") or {}
        cid = c.get("id")
        if cid and cid not in comps:
            comps[cid] = {"handle": c.get("ig_username") or "", "followers": c.get("followers_count_cached")}
    cids = list(comps.keys())

    # 2. Reels de TODOS los competidores en una query (.in_), agrupo en Python.
    by_creator = {cid: [] for cid in cids}
    if cids:
        try:
            rr = (db.table("creator_reels_global")
                    .select("creator_id, views, posted_at")
                    .in_("creator_id", cids).eq("is_archived", False)
                    .order("posted_at", desc=True).limit(900).execute())
            for r in (rr.data or []):
                cid = r.get("creator_id")
                if cid in by_creator:
                    by_creator[cid].append(int(r.get("views") or 0))
        except Exception:
            logger.exception("leaderboard: reels read failed uid=%s", uid)

    rows = []
    for cid, info in comps.items():
        st = _views_stats(by_creator.get(cid))
        if st["reels"] == 0:
            continue  # sin reels scrapeados aún → fuera del ranking (no inventamos)
        rows.append({
            "handle": info["handle"], "you": False,
            "followers": info["followers"],
            "avg_views": st["avg"], "best_views": st["best"],
            "reels": st["reels"], "growth": st["growth"],
        })

    # 3. MIS métricas (ig_videos), mismo cálculo de views.
    me = {"handle": "", "you": True, "followers": None,
          "avg_views": 0, "best_views": 0, "reels": 0, "growth": 0, "has_data": False}
    try:
        ig_row = _get_ig_profile(uid, pid, "id, ig_username")
        if ig_row:
            me["handle"] = (ig_row.get("ig_username") or "").lstrip("@")
            mv = (db.table("ig_videos")
                    .select("views, published_at")
                    .eq("ig_profile_id", ig_row["id"]).order("published_at", desc=True)
                    .limit(30).execute())
            myviews = [int(v.get("views") or 0) for v in (mv.data or [])]
            st = _views_stats(myviews)
            me.update({"avg_views": st["avg"], "best_views": st["best"],
                       "reels": st["reels"], "growth": st["growth"],
                       "has_data": st["reels"] > 0})
    except Exception:
        logger.warning("leaderboard: own metrics failed uid=%s", uid)

    rows.sort(key=lambda r: -r["avg_views"])
    return jsonify({"metric": "avg_views", "you": me, "rows": rows}), 200



@app.route("/api/competitors/reels/<reel_id>/generate-script", methods=["POST"])
@require_auth
@limiter.limit("5 per minute;20 per day", key_func=_user_rate_key)   # por USUARIO, no por IP
def generate_script_from_competitor_reel(reel_id: str):
    """Genera un guion ejecutable a partir de un reel de un competidor del
    usuario. Híbrido sync/async:
      - Cache hit (transcript ya ok)  → flow síncrono, devuelve 200 con
        script_id + script. Redirect inmediato a Guiones en frontend.
      - Cache miss (transcript falta/failed/stale)  → encola Celery task
        y devuelve 202 con task_id. Frontend hace polling a /task/script/<id>.

    Coste: 1 unit monthly_usage (paid) o 18¢ (free), refund-on-fail.
    Stale guard: 'transcribing' con transcript_started_at > 15min se trata
    como abandonado y se permite re-encolar.
    Reusa: gating plan, ownership, refund-on-fail, resolve_assistant_prompt,
    fecha actual inyectada. Sustituye al feature 'generar idea' (v0.15.4).
    """
    user = current_user()
    uid = user["id"]
    profile = get_profile(uid)
    plan = profile.get("plan", "free")
    # P1 idioma de salida: el guion sale en el idioma del USUARIO (body.language →
    # profiles.lang → es), no en el del competidor.
    _body0 = request.get_json(silent=True) or {}
    out_lang = (_body0.get("language") or profile.get("lang") or "es").lower()[:2]
    # Voz por MARCA: una agencia genera con la voz de la marca activa, no la por defecto
    # (igual que /api/metrics/insights). brand_id == project_id; None = marca por defecto.
    gen_pid = _req_project_id()

    # 0. Guard anti doble-cobro: si ya hay un script de este (user, reel) en
    # los últimos 60s, redirigir al existente sin cobrar ni encolar. Cubre
    # double-click, recarga y 2-pestañas. Pasados 60s, regeneración legítima OK.
    try:
        _dup_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        dup_r = (db.table("scripts")
                   .select("id, from_competitor_username")
                   .eq("user_id", uid)
                   .eq("from_competitor_reel_id", reel_id)
                   .gte("created_at", _dup_cutoff)
                   .order("created_at", desc=True)
                   .limit(1)
                   .execute())
        if dup_r.data:
            existing = dup_r.data[0]
            return jsonify({
                "error": "duplicate",
                "message": "Ya generaste un guion de este reel hace un momento.",
                "script_id": existing.get("id"),
                "from_competitor_username": existing.get("from_competitor_username"),
            }), 409
    except Exception as e:
        logger.warning("generate_script: dup check failed user=%s reel=%s err=%s",
                       uid, reel_id, e)

    # 1-2. Quién puede generar y cómo se paga este guion (sin cobrar todavía).
    #   paid → asignación mensual (monthly_usage); free → créditos (3/guión).
    SCRIPT_COST = SCRIPT_UNITS * COST_CENTS  # econ: 3 créditos por guión
    SCRIPT_USAGE_UNITS = SCRIPT_UNITS
    is_paid_unlimited = paid_features_active(profile, user)  # plan de pago REAL (no fantasma)

    # TOPE DIARIO del FREE (modelo 2026-06-23): 3 robos/día → "vuelve mañana o
    # desbloquéalos". Empuja al hábito y, al 4º, a pagar. Los planes de pago no tienen tope.
    if not is_paid_unlimited and trial_scripts_today(uid) >= FREE_DAILY_SCRIPTS:
        track_event("paywall_shown", uid, {"wall": "free_daily", "plan": plan})
        return jsonify({
            "error": "trial_daily_limit",
            "message": f"Hechos tus {FREE_DAILY_SCRIPTS} guiones de hoy — vuelve mañana o desbloquéalos subiendo de plan."
        }), 402

    # Free: robar SIEMPRE cuesta créditos (3). Sin saldo → muro.
    if not is_paid_unlimited and (profile.get("credits_cents") or 0) < SCRIPT_COST:
        track_event("paywall_shown", uid, {"wall": "no_credits", "plan": plan, "after_first_value": True})
        return jsonify({
            "error": "no_credits",
            "message": "No te quedan créditos. Recarga o sube de plan para seguir robando guiones."
        }), 402

    # 3. Cargar reel + ownership.
    try:
        reel_r = (db.table("creator_reels_global")
                    .select("id, ig_reel_id, creator_id, caption, transcript, "
                            "transcript_status, transcript_started_at, "
                            "creator:creators_global(id, ig_username)")
                    .eq("id", reel_id)
                    .eq("is_archived", False)
                    .single()
                    .execute())
        reel = reel_r.data
    except Exception:
        reel = None
    if not reel or not reel.get("creator"):
        return jsonify({"error": "reel_not_found"}), 404

    creator = reel["creator"]
    creator_id = creator["id"]
    ig_username = creator.get("ig_username") or ""

    # Activación (Fathom 18/06): funnel tutorial → 1er robo. Marca cada robo y, si es
    # el PRIMERO del usuario, el evento "activated" (la métrica norte de la activación).
    try:
        _n = (db.table("scripts").select("id", count="exact").eq("user_id", uid).execute()).count or 0
        track_event("reel_steal", uid, {"reel_id": reel_id, "nth": _n + 1})
        if _n == 0:
            track_event("activated", uid, {"first_steal_reel": reel_id, "creator": ig_username})
    except Exception:
        pass

    own_r = (db.table("user_tracked_creators")
               .select("id")
               .eq("user_id", uid)
               .eq("creator_id", creator_id)
               .is_("archived_at", "null")
               .limit(1)
               .execute())
    if not own_r.data:
        # «Sugerencias de hoy» (no_follow): a veces al user le gusta el REEL pero NO quiere
        # seguir al creador → roba el guion directamente, SIN auto-seguir (cuesta lo mismo).
        # El front solo manda no_follow desde esa sección (reels on-niche).
        if _body0.get("no_follow"):
            pass  # robar sin seguir: no ownership, no auto-follow
        # SEED (SPEC #3, Fathom 18/06): el user puede robar un reel del SEED de su
        # nicho aunque aún no siga al creador. Si el reel es seed-válido (creador
        # tageado con su subnicho) y le quedan huecos de competidor, lo AUTO-SEGUIMOS
        # y continuamos — convierte el seed en una relación real (activación). Si no
        # encaja o no hay hueco → 404 (igual que antes).
        elif not _autotrack_seed_creator(uid, profile, creator_id):
            return jsonify({"error": "reel_not_found"}), 404

    # 4. Resolver asistente (body > profile.default).
    body = request.get_json(silent=True) or {}
    assistant_id = (body.get("assistant_id") or "").strip() or None
    if not assistant_id:
        assistant_id = profile.get("default_idea_assistant") or None

    # v0.15.7.b: pre-validar custom assistant ANTES de decidir sync/async.
    # Si es un custom del user con instructions <30 chars → 400 inmediato
    # (sin encolar Celery ni gastar el path sync que terminaría en LLM
    # devolviendo content=null tras 4s). El path sync y la task Celery
    # repiten el check tras resolver, defense in depth.
    if assistant_id and assistant_id not in _BUILTIN_SCRIPT_STYLES:
        try:
            asst_pre = (db.table("assistants")
                          .select("name, instructions")
                          .eq("id", assistant_id)
                          .eq("user_id", uid)
                          .execute())
            if asst_pre.data and asst_pre.data[0].get("instructions"):
                _pre_prompt = asst_pre.data[0]["instructions"]
                _pre_name = asst_pre.data[0].get("name") or "custom"
                if _custom_too_short("custom", _pre_prompt):
                    return _assistant_too_short_response(_pre_name)
        except Exception:
            pass  # No bloquear si lookup falla — el path sync/task lo reintenta

    # v0.15.8: ADQUIRIR LOCK (user_id, reel_id) — cierra el 0.1% residual del
    # guard 60s ante 2 POSTs simultáneos del mismo reel. PK compuesta garantiza
    # atomicidad. Si conflict: si lock huérfano (>10min, sweeper caído) → DELETE
    # + reintenta. Si fresco → 409 in_progress con task_id (frontend reusa polling).
    _LOCK_STALE_MIN = 10
    _lock_acquired = False
    try:
        ins_lock = (db.table("script_generation_locks")
                      .insert({"user_id": uid, "reel_id": reel_id})
                      .execute())
        _lock_acquired = bool(ins_lock.data)
    except Exception as e_lock:
        # Insert falló → muy probable PK conflict. Inspeccionamos.
        try:
            cur = (db.table("script_generation_locks")
                     .select("started_at, task_id")
                     .eq("user_id", uid)
                     .eq("reel_id", reel_id)
                     .single()
                     .execute())
            existing = cur.data
        except Exception:
            existing = None
        if not existing:
            logger.error("generate_script: lock INSERT failed sin row existente user=%s reel=%s err=%s",
                         uid, reel_id, e_lock)
            return jsonify({"error": "internal", "message": "Inténtalo de nuevo."}), 500
        # ¿Huérfano?
        try:
            started_dt = datetime.fromisoformat(str(existing["started_at"]).replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60.0
        except Exception:
            age_min = 0
        if age_min > _LOCK_STALE_MIN:
            # Lock huérfano (worker murió antes del finally) → liberar y reintentar.
            logger.info("generate_script: lock huérfano liberado user=%s reel=%s age_min=%.1f",
                        uid, reel_id, age_min)
            try:
                (db.table("script_generation_locks").delete()
                   .eq("user_id", uid).eq("reel_id", reel_id).execute())
                (db.table("script_generation_locks")
                   .insert({"user_id": uid, "reel_id": reel_id}).execute())
                _lock_acquired = True
            except Exception as e_retry:
                logger.error("generate_script: lock retry failed user=%s reel=%s err=%s",
                             uid, reel_id, e_retry)
                return jsonify({"error": "internal", "message": "Inténtalo de nuevo."}), 500
        else:
            # Lock fresco → otra generación en curso.
            return jsonify({
                "error": "in_progress",
                "message": "Ya estás generando un guion de este reel. Espera unos segundos.",
                "task_id": existing.get("task_id"),
                "lock_started_at": existing.get("started_at"),
            }), 409

    def _release_lock():
        """Libera el lock del par (user, reel). Best-effort, idempotente."""
        try:
            (db.table("script_generation_locks").delete()
               .eq("user_id", uid).eq("reel_id", reel_id).execute())
        except Exception as e:
            logger.warning("generate_script: lock DELETE failed user=%s reel=%s err=%s",
                           uid, reel_id, e)

    # 5. Decisión sync vs async:
    #    'ok' → flow sync (transcript ya cacheado).
    #    Cualquier otro estado → flow async (encolar Celery).
    transcript_ok = reel.get("transcript_status") == "ok" and (reel.get("transcript") or "").strip()

    if transcript_ok:
        # ── Flow síncrono ──────────────────────────────────────────────
        # Generar guion ahora con caption + transcript cacheado.
        from datetime import datetime as _dt
        today_str = _dt.now(timezone.utc).strftime("%-d de %B de %Y")
        caption = (reel.get("caption") or "").strip()
        transcript_text = (reel.get("transcript") or "").strip()
        user_content = _build_competitor_script_user_content(
            ig_username=ig_username,
            caption=caption,
            transcript=transcript_text,
            today_str=today_str,
            out_lang=out_lang,
        )

        # Resolver style/custom_prompt (mismo patrón que transcription_to_script).
        style_arg = "viral"
        custom_prompt = ""
        style_label = "viral"
        if assistant_id in _BUILTIN_SCRIPT_STYLES:
            style_arg = assistant_id
            style_label = assistant_id
        elif assistant_id:
            try:
                asst_r = (db.table("assistants")
                            .select("name, instructions")
                            .eq("id", assistant_id)
                            .eq("user_id", uid)
                            .execute())
                if asst_r.data and asst_r.data[0].get("instructions"):
                    style_arg = "custom"
                    custom_prompt = asst_r.data[0]["instructions"]
                    style_label = asst_r.data[0].get("name") or "custom"
            except Exception:
                pass

        # P0-3: "hooks" produce 5 one-liners sueltos, no un guion — «Hazlo mío»
        # los guardaba como guion. Degradar a viral (mismo patrón que
        # idea_scripts_generate_batch).
        if style_arg == "hooks":
            style_arg = style_label = "viral"

        # v0.15.7.b: cortar pre-LLM si custom prompt demasiado corto (sin cobrar).
        if _custom_too_short(style_arg, custom_prompt):
            return _assistant_too_short_response(style_label)

        # Cobrar ANTES del LLM, BAJO LOCK por-usuario (Redis) para que dos workers
        # no doblen el gasto en la carrera read-then-write. Re-leemos el perfil dentro
        # del lock y re-validamos. Si LLM/insert falla → refund.
        _clock = acquire_credit_lock(uid)
        if _clock is None:
            _release_lock()
            return jsonify({"error": "busy", "message": "Otra generación tuya está en curso. Espera un segundo."}), 429
        try:
            fresh = get_profile(uid)  # estado actual bajo lock (otros workers ya pudieron cobrar)
            if is_paid_unlimited:
                db.table("profiles").update({
                    "monthly_usage": (fresh.get("monthly_usage") or 0) + SCRIPT_USAGE_UNITS
                }).eq("id", uid).execute()
            else:
                if (fresh.get("credits_cents") or 0) < SCRIPT_COST:   # re-check bajo lock
                    _release_lock()
                    return jsonify({"error": "no_credits",
                                    "message": "Necesitas créditos para generar guion. Sube de plan."}), 402
                db.table("profiles").update({
                    "credits_cents": (fresh.get("credits_cents") or 0) - SCRIPT_COST
                }).eq("id", uid).execute()
            profile = fresh  # el refund de abajo parte del estado bajo lock
        except Exception as e:
            logger.error("generate_script: pre-charge failed user=%s err=%s", uid, e, exc_info=True)
            _release_lock()
            return jsonify({"error": "internal", "message": "Inténtalo de nuevo."}), 500
        finally:
            release_credit_lock(uid, _clock)

        def _refund():
            try:
                if is_paid_unlimited:
                    db.table("profiles").update({
                        "monthly_usage": max(0, (profile.get("monthly_usage") or 0))
                    }).eq("id", uid).execute()
                else:
                    db.table("profiles").update({
                        "credits_cents": (profile.get("credits_cents") or 0)
                    }).eq("id", uid).execute()
            except Exception as e:
                logger.error("generate_script: refund failed user=%s err=%s", uid, e)

        # LLM call — 2 OPCIONES de guion (el usuario elige), modelo de generación = pro.
        try:
            raw_opts = _generate_script_options(user_content, style_arg, custom_prompt, uid, gen_pid, n=2)
        except Exception as e:
            logger.error("generate_script: LLM failed user=%s err=%s", uid, e, exc_info=True)
            _refund()
            # v0.15.7.b: mensaje contextual si style=custom y empty content
            # (guard v0.15.7.a). Apunta al asistente concreto en vez del
            # mensaje genérico, accionable hacia /assistants.
            _release_lock()
            if style_arg == "custom" and "empty content" in str(e).lower():
                return jsonify({
                    "error": "assistant_empty_response",
                    "message": f"El asistente '{style_label}' devolvió respuesta vacía. "
                               f"Edita sus instrucciones o usa otro estilo.",
                    "assistant_name": style_label,
                }), 502
            return jsonify({"error": "llm_error",
                            "message": "No se pudo generar el guion. Inténtalo de nuevo."}), 502

        # Estructura: opciones para la UI + formato/POV de la opción A. Se guarda la A.
        options = [_shape_script_option(r) for r in raw_opts]
        rec_fmt = raw_opts[0].get("recording_format") if isinstance(raw_opts[0], dict) else None  # ítem 10
        pov_text = raw_opts[0].get("pov_text") if isinstance(raw_opts[0], dict) else None
        llm_title = options[0]["title"]
        result = options[0]["script"]
        today_short = _dt.now(timezone.utc).strftime("%d %b %Y").lower()
        script_title = llm_title or f"Guion desde @{ig_username} · {today_short}"
        script_id = None

        # Re-check antes del INSERT: cierra ventana 2-pestañas que pasaron
        # el guard 0 simultáneamente (ambas ya cobraron en pre-charge; aquí
        # refundamos a la perdedora y devolvemos el script ya existente).
        try:
            _cutoff2 = (_dt.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            dup2 = (db.table("scripts")
                      .select("id, from_competitor_username")
                      .eq("user_id", uid)
                      .eq("from_competitor_reel_id", reel["id"])
                      .gte("created_at", _cutoff2)
                      .order("created_at", desc=True)
                      .limit(1)
                      .execute())
            if dup2.data:
                _refund()
                _release_lock()
                existing = dup2.data[0]
                return jsonify({
                    "error": "duplicate",
                    "message": "Ya generaste un guion de este reel hace un momento.",
                    "script_id": existing.get("id"),
                    "from_competitor_username": existing.get("from_competitor_username"),
                }), 409
        except Exception as e:
            logger.warning("generate_script: pre-insert dup check failed user=%s err=%s", uid, e)

        _script_row = {
            "user_id":                uid,
            "transcription_id":       None,
            "idea_id":                None,
            "title":                  script_title,
            "script":                 result,
            "project_id":             None,
            "from_competitor_reel_id": reel["id"],
            "from_competitor_username": ig_username,
            "assistant_name":         _resolve_assistant_name(
                {"assistant_id": assistant_id, "style": style_label}, uid, db
            ),
            "recording_format":       rec_fmt,   # ítem 10
        }
        try:
            ins = db.table("scripts").insert(_script_row).execute()
            if ins.data:
                script_id = ins.data[0].get("id")
        except Exception as e:
            # Degradación segura: columna recording_format aún sin migrar → reintenta sin ella.
            if "recording_format" in str(e).lower():
                _script_row.pop("recording_format", None)
                try:
                    ins = db.table("scripts").insert(_script_row).execute()
                    if ins.data:
                        script_id = ins.data[0].get("id")
                except Exception as e2:
                    logger.error("generate_script: scripts insert (no-fmt) failed user=%s err=%s", uid, e2, exc_info=True)
            else:
                logger.error("generate_script: scripts insert failed user=%s err=%s", uid, e, exc_info=True)
            # No refundamos: el LLM funcionó, el user tiene el script en la response.

        try:
            from emails import track as _ph_track, track_script_generated as _ph_first
            _ph_track("script_generated_from_competitor", uid, {
                "creator_username": ig_username,
                "reel_id": reel["id"],
                "mode": "sync_cached",
            })
            # growth-1: activación — dispara first_script_generated si es el 1º.
            _ph_first(uid, {"source": "competitor_reel", "mode": "sync_cached"})
        except Exception:
            pass

        _release_lock()
        return jsonify({
            "mode": "sync",
            "script_id": script_id,
            "script": result,
            "title": script_title,
            "from_competitor_username": ig_username,
            "recording_format": rec_fmt,   # ítem 10: formato sugerido para grabarlo
            "pov_text": pov_text,          # texto en pantalla si el formato es POV
            "options": options,            # 2 opciones de guion (cada una con 2-3 hooks)
        }), 200

    # ── Flow asíncrono ─────────────────────────────────────────────────
    # Stale guard: si lleva >15min en 'transcribing', tratar como abandonado.
    STALE_MIN = 15
    if reel.get("transcript_status") == "transcribing":
        started_at = reel.get("transcript_started_at")
        if started_at:
            try:
                from datetime import datetime as _dt
                started_dt = _dt.fromisoformat(str(started_at).replace("Z", "+00:00"))
                age_min = (_dt.now(timezone.utc) - started_dt).total_seconds() / 60.0
                if age_min < STALE_MIN:
                    # Hay otro proceso transcribiendo recientemente → encolar
                    # la task igualmente: la task hará polling interno hasta
                    # que aparezca o reintentará si stale.
                    pass
            except Exception:
                pass

    # Encolar task (no cobramos aquí — la task cobra al final si todo OK).
    from tasks import generate_script_competitor_task  # noqa: E402
    async_result = generate_script_competitor_task.delay(reel["id"], uid, assistant_id, out_lang, gen_pid)
    # v0.15.8: anotar task_id en el lock (la task lo libera al final vía try/finally).
    try:
        (db.table("script_generation_locks")
           .update({"task_id": async_result.id})
           .eq("user_id", uid).eq("reel_id", reel["id"]).execute())
    except Exception as e:
        logger.warning("generate_script: lock UPDATE task_id failed user=%s reel=%s err=%s",
                       uid, reel["id"], e)
    return jsonify({
        "mode": "async",
        "task_id": async_result.id,
        "status": "queued",
        "message": "Analizando reel y generando guion…",
    }), 202


_LANG_NAMES = {"es": "español", "en": "English", "pt": "português", "fr": "français",
               "it": "italiano", "de": "Deutsch"}


def _out_lang_instruction(lang: str | None) -> str:
    """P1 idioma de salida: instrucción para que el guion salga en el idioma del
    USUARIO (no el del competidor). Vacío si lang desconocido → comportamiento
    previo (espejo del original)."""
    name = _LANG_NAMES.get((lang or "").lower()[:2])
    if not name:
        return ""
    return (f"\n\nIDIOMA DE SALIDA (no negociable): escribe TODO el guion (hook, "
            f"desarrollo y cierre) en {name}, aunque el reel original esté en otro "
            f"idioma. Mantén nombres propios, marcas y términos técnicos tal cual.")


def _build_competitor_script_user_content(ig_username: str, caption: str,
                                          transcript: str, today_str: str,
                                          out_lang: str | None = None) -> str:
    """v0.15.5: user_content para adapt_with_ai en el contexto generar-guion
    desde reel de competidor. Consolida lecciones v0.15.4.a-e: respeto a
    versiones/hechos del reel, fecha inyectada, mismo tema/distinta ejecución."""
    if not transcript or not transcript.strip():
        transcript_block = "Sin transcripción disponible; usa solo el caption."
    else:
        transcript_block = transcript.strip()
    return (
        f"[Reel de un competidor del usuario · @{ig_username}]\n\n"
        f"Caption del reel:\n{caption or '(sin caption)'}\n\n"
        f"Transcripción del audio del reel (lo que el creador realmente dice):\n"
        f"{transcript_block}\n\n"
        f"Fecha actual: {today_str}. Cualquier modelo, herramienta, versión, "
        f"empresa o producto que aparezca en el caption o la transcripción es "
        f"REAL y ACTUAL aunque no lo conozcas de tu entrenamiento — úsalo tal "
        f"cual, NO lo sustituyas por una versión que te resulte más familiar. "
        f"Confiar en el reel sobre qué existe ahora es regla NO negociable.\n\n"
        f"Tarea: este es un reel de un competidor del usuario. Genera un guion "
        f"completo de 30-45 segundos hablados para que el usuario grabe SOBRE "
        f"EL MISMO TEMA que este reel. Reescribe el hook con tus palabras (NO "
        f"copies palabra por palabra el del competidor), reescribe el "
        f"desarrollo y los ejemplos con un enfoque propio. La diferencia con "
        f"el competidor está en la EJECUCIÓN, no en el tema. Respeta "
        f"exactamente los nombres, versiones y herramientas que aparecen en "
        f"el reel.\n\n"
        # B: «copia lo que funciona» = preservar lo que hace funcionar al original.
        f"PRESERVA lo que hace funcionar al original — regla NO negociable: "
        f"(a) las cifras, nombres, comparaciones y el ángulo CONCRETO del reel "
        f"se mantienen (traducidos a otras palabras, no sustituidos por "
        f"generalidades); (b) el REGISTRO también se mantiene: si el original "
        f"es humor/sátira/diálogo, el guion resultante es humor/sátira/diálogo "
        f"— no lo conviertas en consejo serio ni motivacional; (c) prohibido "
        f"inventar anécdotas o logros propios del usuario ('mi equipo hizo X') "
        f"— los ejemplos salen del reel o son claramente hipotéticos. Si el "
        f"guion final pierde la especificidad del original y podría valer para "
        f"cualquier nicho, está mal: reescríbelo.\n\n"
        f"Total: 100-140 palabras, mínimo 8 frases en body."
        + _out_lang_instruction(out_lang)
    )


@app.route("/task/script/<task_id>", methods=["GET"])
@require_auth
@limiter.limit("60 per minute")
def task_script_status(task_id: str):
    """Polling dedicado para generate_script_competitor_task. Shape propio
    (no reusa /task/<id> que está acoplado a transcribe_task)."""
    from tasks import generate_script_competitor_task  # noqa: E402
    task = generate_script_competitor_task.AsyncResult(task_id)
    state = task.state  # PENDING | STARTED | SUCCESS | FAILURE | PROGRESS

    if state in ("PENDING", "STARTED", "PROGRESS"):
        return jsonify({"state": "pending", "step": (task.info or {}).get("step") if isinstance(task.info, dict) else None})
    if state == "SUCCESS":
        result = task.result or {}
        if not isinstance(result, dict):
            return jsonify({"state": "failed", "error": "bad_result"})
        if result.get("ok"):
            return jsonify({
                "state": "success",
                "script_id": result.get("script_id"),
                "title": result.get("title"),
                "from_competitor_username": result.get("from_competitor_username"),
                "recording_format": result.get("recording_format"),
                "pov_text": result.get("pov_text"),
                "options": result.get("options"),
            })
        # ok=False → task terminó con error controlado.
        return jsonify({
            "state": "failed",
            "error": result.get("error") or "unknown",
            "message": result.get("message") or "No se pudo generar el guion.",
        })
    if state == "FAILURE":
        return jsonify({"state": "failed", "error": "task_failure",
                        "message": "Error procesando el reel. Inténtalo de nuevo."})
    return jsonify({"state": "pending"})


@app.route("/task/adapt/<task_id>", methods=["GET"])
@require_auth
@limiter.limit("60 per minute")
def task_adapt_status(task_id: str):
    """Polling de adapt_task (/adapt async). En éxito devuelve la MISMA shape que la
    respuesta sync de antes: {state:"success", result, cost_cents, credits_cents,
    free_used_today}, para que los callers del frontend no cambien su contrato."""
    from tasks import adapt_task  # noqa: E402
    task = adapt_task.AsyncResult(task_id)
    state = task.state
    if state in ("PENDING", "STARTED", "PROGRESS"):
        return jsonify({"state": "pending"})
    if state == "SUCCESS":
        r = task.result or {}
        if not isinstance(r, dict):
            return jsonify({"state": "failed", "error": "bad_result"})
        if r.get("ok"):
            out = {"state": "success", "result": r.get("result"), "cost_cents": r.get("cost_cents", 0)}
            if "credits_cents" in r:   out["credits_cents"]   = r["credits_cents"]
            if "free_used_today" in r: out["free_used_today"] = r["free_used_today"]
            return jsonify(out)
        # ok=False → error controlado del task (gating/llm).
        return jsonify({"state": "failed", "error": r.get("error") or "unknown",
                        "message": r.get("message") or "No se pudo generar. Inténtalo de nuevo."})
    if state == "FAILURE":
        return jsonify({"state": "failed", "error": "task_failure",
                        "message": "Internal server error. Please try again."})
    return jsonify({"state": "pending"})


# ── v0.15.6: favoritos de reels de competidores ─────────────────────────────
# Tabla user_favorite_reels (hard-delete). Toggle UI optimista en frontend;
# el método HTTP se deriva del estado deseado tras el click (POST=quiere fav,
# DELETE=quiere no-fav) — requests desordenados convergen porque ambos son
# idempotentes (ON CONFLICT DO NOTHING / DELETE WHERE no-op si no existe).

def _user_owns_reel(uid: str, reel_id: str) -> bool:
    """Comprueba que el reel pertenece a un competidor activo del user.
    Reusa el mismo patrón de ownership que generate-script."""
    try:
        rr = (db.table("creator_reels_global")
                .select("creator_id")
                .eq("id", reel_id)
                .single()
                .execute())
    except Exception:
        return False
    if not rr.data:
        return False
    own_r = (db.table("user_tracked_creators")
               .select("id")
               .eq("user_id", uid)
               .eq("creator_id", rr.data["creator_id"])
               .is_("archived_at", "null")
               .limit(1)
               .execute())
    return bool(own_r.data)


@app.route("/api/competitors/reels/<reel_id>/transcript", methods=["GET"])
@require_auth
@limiter.limit("30 per minute")
def get_competitor_reel_transcript(reel_id: str):
    """Detalle del reel (isla) — «Ver transcripción», SIN generar guion ni cobrar.
    Reusa la misma caché que «Hazlo mío» (creator_reels_global.transcript):
      - status ok    → {transcript} (2ª vez gratis: cache hit).
      - transcribing → {pending:true} (el front pollea este mismo endpoint).
      - falta/failed → encola tasks.transcribe_reel y {pending:true}.
    GET semántico para el hit de caché; el encolado es idempotente (lock por
    transcript_status + sweeper de stale)."""
    user = current_user()
    uid = user["id"]
    if not _user_owns_reel(uid, reel_id):
        return jsonify({"error": "reel_not_found"}), 404
    try:
        rr = (db.table("creator_reels_global")
                .select("transcript, transcript_status, transcript_started_at")
                .eq("id", reel_id).single().execute())
    except Exception as e:
        logger.error("reel_transcript load failed user=%s reel=%s err=%s", uid, reel_id, e)
        return jsonify({"error": "internal"}), 500
    reel = rr.data or {}
    status = reel.get("transcript_status")
    cached = (reel.get("transcript") or "").strip()
    if status == "ok" and cached:
        return jsonify({"transcript": cached})

    # transcribing FRESCO → pending sin re-encolar; stale → re-encolar.
    if status == "transcribing":
        started_at = reel.get("transcript_started_at")
        try:
            started_dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            fresh = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60.0 <= 15
        except Exception:
            fresh = False
        if fresh:
            return jsonify({"pending": True})

    try:
        from tasks import transcribe_reel_task  # lazy (rompe circular tasks↔app)
        transcribe_reel_task.delay(reel_id)
    except Exception as e:
        logger.error("reel_transcript enqueue failed user=%s reel=%s err=%s", uid, reel_id, e)
        return jsonify({"error": "queue_error"}), 503
    return jsonify({"pending": True})


@app.route("/api/competitors/reels/<reel_id>/favorite", methods=["POST"])
@require_auth
@limiter.limit("30 per minute")
def add_favorite_reel(reel_id: str):
    """Marca reel como favorito. Idempotente: ON CONFLICT DO NOTHING."""
    user = current_user()
    uid = user["id"]
    if not _user_owns_reel(uid, reel_id):
        return jsonify({"error": "reel_not_found"}), 404
    try:
        db.table("user_favorite_reels").upsert(
            {"user_id": uid, "reel_id": reel_id},
            on_conflict="user_id,reel_id",
        ).execute()
    except Exception as e:
        logger.error("add_favorite_reel failed user=%s reel=%s err=%s", uid, reel_id, e)
        return jsonify({"error": "internal"}), 500
    return jsonify({"ok": True, "is_favorite": True})


@app.route("/api/competitors/reels/<reel_id>/favorite", methods=["DELETE"])
@require_auth
@limiter.limit("30 per minute")
def remove_favorite_reel(reel_id: str):
    """Desmarca favorito. Idempotente: DELETE WHERE no-op si no existe."""
    user = current_user()
    uid = user["id"]
    # No requerimos ownership para DELETE — un user siempre puede quitar SU
    # favorito aunque haya des-trackeado al creador entretanto.
    try:
        (db.table("user_favorite_reels")
           .delete()
           .eq("user_id", uid)
           .eq("reel_id", reel_id)
           .execute())
    except Exception as e:
        logger.error("remove_favorite_reel failed user=%s reel=%s err=%s", uid, reel_id, e)
        return jsonify({"error": "internal"}), 500
    return jsonify({"ok": True, "is_favorite": False})


# ── v0.15.9: guardar reel de competidor como idea (sin coste, sin LLM) ──────
# El user añade el reel a su panel Ideas con status='draft'. Reusa columnas
# inspired_by_* existentes (v0.15.4 nunca borradas). Idempotente: si ya hay
# idea de este (user, reel) → 200 already_exists:true sin crear duplicado.
# Luego el user puede "Desarrollar con Hooks" desde el panel Ideas (flujo
# existente, ahí sí cobra cuando llama al LLM).

def _build_idea_title_from_reel(caption: str, ig_username: str) -> str:
    """80 chars max. Prefiere primera frase limpia del caption; fallback a fecha."""
    c = (caption or "").strip()
    if c:
        # Primera línea/frase (corta en \n o '.') para evitar caption multilinea.
        first = c.split("\n", 1)[0].strip()
        if "." in first[:120]:
            first = first.split(".", 1)[0].strip()
        if len(first) > 80:
            first = first[:77].rstrip() + "…"
        if first:
            return first
    today_short = datetime.now(timezone.utc).strftime("%d %b %Y").lower()
    return f"Reel de @{ig_username} · {today_short}"


def _build_idea_raw_text_from_reel(ig_username: str, caption: str,
                                   transcript: str, transcript_ok: bool) -> str:
    """Material para raw_text — caption + transcript si está cacheado.
    Fallback determinista cuando el LLM de v0.15.9.b falla."""
    parts = [f"[Reel de @{ig_username}]", ""]
    parts.append("Caption:")
    parts.append((caption or "").strip() or "(sin caption)")
    if transcript_ok and (transcript or "").strip():
        parts.append("")
        parts.append("Transcripción:")
        parts.append(transcript.strip())
    return "\n".join(parts)


# v0.15.9.b: prompt para Gemini Flash 2.0 — extrae title+summary del reel.
# Detecta CTAs ("Comment X to access", "Link in bio") como NO-contenido y
# extrae el tema del transcript en ese caso. Idioma heredado del competidor.
_EXTRACT_IDEA_SYSTEM = (
    "Eres un asistente que extrae el tema central de un reel de Instagram/TikTok y "
    "lo formula como una idea de contenido para el banco de ideas del usuario.\n\n"
    "Recibirás caption del reel + transcripción del audio (cuando esté disponible). "
    "Devolverás JSON estricto con exactamente estos 3 campos:\n\n"
    "- title: 60-80 caracteres. Captura el tema principal en una frase clara y "
    "accionable. NO copies palabra por palabra del caption. NO incluyas CTAs "
    "(\"Comment X to access\", \"Link in bio\"). NO arranques con emoji. Suena a "
    "título de idea de contenido, no a titular de prensa ni a anuncio.\n\n"
    "  Si el caption es principalmente un call-to-action (ej. 'Comment X to access', "
    "'Link in bio', 'DM me for...'), considera que NO es contenido — extrae el tema "
    "ÚNICAMENTE del transcript. Si no hay transcript y el caption es solo CTA, el "
    "title debe ser genérico ('Reel de @<username>') y el summary debe indicar "
    "'Reel sin contenido transcribible'.\n\n"
    "- summary: 2-3 frases (40-80 palabras). Describe DE QUÉ va la idea reformulando "
    "con palabras propias. NO copies frases del caption ni del transcript. Captura "
    "el ángulo único del competidor si es claro (ej. 'trata X desde el punto de "
    "vista de Y' o 'compara X vs Y'). Suficiente para que el user decida si "
    "desarrollarla en guion.\n\n"
    "- language: código ISO de 2 letras (es, en, pt, fr...) detectado del "
    "caption/transcript.\n\n"
    "Regla NO negociable: cualquier modelo, herramienta, versión, empresa o "
    "producto que aparezca en el input es REAL y ACTUAL aunque no lo conozcas — "
    "úsalo TAL CUAL, NO lo sustituyas por una versión más familiar.\n\n"
    "Title y summary deben estar en el MISMO idioma que el caption/transcript del "
    "competidor (detectado, no forzado al idioma del usuario).\n\n"
    "Output: SOLO el JSON, sin markdown, sin explicaciones, sin texto antes ni "
    "después.\n"
    '{"title": "...", "summary": "...", "language": "es"}'
)


def _llm_extract_idea_from_reel(ig_username: str, caption: str,
                                transcript: str, transcript_ok: bool):
    """v0.15.9.b: 1 llamada Gemini Flash 2.0 (~$0.0002/idea, 1-3s) para extraer
    title+summary del reel. Returns (title, summary) o (None, None) si falla.
    El caller usa fallback heurístico cuando devuelve None."""
    transcript_block = transcript.strip() if (transcript_ok and (transcript or "").strip()) else "(no disponible)"
    caption_block = (caption or "").strip() or "(sin caption)"
    user_content = (
        f"[Reel de @{ig_username}]\n\n"
        f"Caption:\n{caption_block}\n\n"
        f"Transcripción:\n{transcript_block}"
    )
    try:
        result = _call_llm_json(
            _EXTRACT_IDEA_SYSTEM,
            user_content,
            model="google/gemini-2.0-flash-001",
            max_tokens=400,
            temperature=0.5,
            timeout=10,
        )
    except Exception as e:
        logger.warning("idea_extract_fallback reason=llm_call_failed err=%s", e)
        return (None, None)

    if not isinstance(result, dict):
        logger.warning("idea_extract_fallback reason=not_dict result=%s", repr(result)[:200])
        return (None, None)

    title = (result.get("title") or "").strip()
    summary = (result.get("summary") or "").strip()

    # Validación: title 1-80 chars, summary 30-500 chars.
    if not title or len(title) > 80:
        logger.warning("idea_extract_fallback reason=title_invalid len=%d", len(title))
        return (None, None)
    if not summary or len(summary) < 30 or len(summary) > 500:
        logger.warning("idea_extract_fallback reason=summary_invalid len=%d", len(summary))
        return (None, None)

    return (title, summary)


def _build_idea_raw_text_from_summary(ig_username: str, summary: str, caption: str) -> str:
    """v0.15.9.b: raw_text con el summary del LLM + caption original truncado
    a 300 chars (trazabilidad sin volcar transcript completo, que ya vive en
    creator_reels_global.transcript para quien lo necesite)."""
    cap = (caption or "").strip()
    if len(cap) > 300:
        cap = cap[:297].rstrip() + "…"
    cap = cap or "(sin caption)"
    return (
        f"[Reel de @{ig_username}]\n\n"
        f"{summary}\n\n"
        f"---\n"
        f"Caption original:\n{cap}"
    )


@app.route("/api/competitors/reels/<reel_id>/save-as-idea", methods=["POST"])
@require_auth
@limiter.limit("20 per minute")
def save_reel_as_idea(reel_id: str):
    """Guarda un reel de competidor como idea (status='draft', sin LLM, sin coste).
    Idempotente: si ya existe idea de este (user, reel) → 200 already_exists:true."""
    user = current_user()
    uid = user["id"]
    if not _user_owns_reel(uid, reel_id):
        return jsonify({"error": "reel_not_found"}), 404

    body = request.get_json(silent=True) or {}
    project_id = (body.get("project_id") or "").strip() or None

    # 1. Idempotency check.
    # v0.15.9.a: whitelist explícita source+status para no capturar zombies
    # de v0.15.4 (status='draft_suggested', invisibles en list_ideas) que
    # apuntan al mismo reel y devolvían already_exists=true con un id
    # invisible en el panel Ideas (falso éxito). Falla cerrada: estados
    # nuevos futuros NO se confunden con duplicado (peor caso = idea
    # duplicada borrable).
    try:
        existing = (db.table("ideas")
                      .select("id, title")
                      .eq("user_id", uid)
                      .eq("inspired_by_id", reel_id)
                      .eq("inspired_by_type", "reel")
                      .eq("source", "competitor_reel")
                      .in_("status", ["draft", "developed"])
                      .limit(1)
                      .execute())
        if existing.data:
            return jsonify({
                "ok": True,
                "idea_id": existing.data[0]["id"],
                "title": existing.data[0].get("title"),
                "already_exists": True,
            }), 200
    except Exception as e:
        logger.warning("save_reel_as_idea: dup check failed user=%s reel=%s err=%s",
                       uid, reel_id, e)

    # 2. Cargar reel + ownership ya validado arriba.
    try:
        rr = (db.table("creator_reels_global")
                .select("id, caption, transcript, transcript_status, "
                        "creator:creators_global(ig_username)")
                .eq("id", reel_id)
                .single()
                .execute())
        reel = rr.data
    except Exception:
        reel = None
    if not reel:
        return jsonify({"error": "reel_not_found"}), 404

    ig_username = (reel.get("creator") or {}).get("ig_username") or ""
    caption = (reel.get("caption") or "").strip()
    transcript_text = (reel.get("transcript") or "").strip()
    transcript_ok = reel.get("transcript_status") == "ok" and bool(transcript_text)

    # v0.15.9.b: extraer title+summary con Gemini Flash 2.0 (sync, ~1-3s,
    # ~$0.0002/idea, infra interna sin coste al user). Si LLM falla/timeout/
    # output inválido → fallback determinista a heurística v0.15.9.
    llm_title, llm_summary = _llm_extract_idea_from_reel(
        ig_username, caption, transcript_text, transcript_ok
    )
    if llm_title and llm_summary:
        title = llm_title
        raw_text = _build_idea_raw_text_from_summary(ig_username, llm_summary, caption)
    else:
        title = _build_idea_title_from_reel(caption, ig_username)
        raw_text = _build_idea_raw_text_from_reel(ig_username, caption, transcript_text, transcript_ok)

    # 3. INSERT idea.
    try:
        ins = db.table("ideas").insert({
            "user_id": uid,
            "project_id": project_id,
            "raw_text": raw_text,
            "title": title,
            "status": "draft",
            "source": "competitor_reel",
            "inspired_by_id": reel_id,
            "inspired_by_type": "reel",
            "inspired_by_username": ig_username,
        }).execute()
    except Exception as e:
        logger.error("save_reel_as_idea: insert failed user=%s reel=%s err=%s",
                     uid, reel_id, e, exc_info=True)
        return jsonify({"error": "internal", "message": "No se pudo guardar la idea."}), 500

    idea_id = ins.data[0]["id"] if ins.data else None
    try:
        from emails import track as _ph_track
        _ph_track("idea_saved_from_competitor", uid, {
            "creator_username": ig_username,
            "reel_id": reel_id,
            "has_transcript": transcript_ok,
        })
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "idea_id": idea_id,
        "title": title,
        "already_exists": False,
    }), 200


def _creator_view_baselines(creator_ids):
    """v0.16.x Radar: mediana de views de los reels recientes de cada creator.

    Devuelve {creator_id: baseline_views} con baseline >= 1 (evita div/0).
    Sirve para el "índice de explosión" = views_reel / baseline_creador.
    v1: fetch global acotado por posted_at desc — cubre de sobra el caso
    típico (1-5 creators). DEUDA: con 20 creators (agency) un creador muy
    prolífico podría sesgar el cap; suficiente para v1.
    """
    if not creator_ids:
        return {}
    cap = min(800, 60 * len(creator_ids))
    try:
        rows = (db.table("creator_reels_global")
                  .select("creator_id, views")
                  .in_("creator_id", creator_ids)
                  .eq("is_archived", False)
                  .order("posted_at", desc=True)
                  .limit(cap)
                  .execute()).data or []
    except Exception as e:
        logger.warning("_creator_view_baselines failed: %s", e)
        return {}
    buckets = {}
    for r in rows:
        v = int(r.get("views") or 0)
        if v <= 0:
            continue
        buckets.setdefault(r["creator_id"], []).append(v)
    out = {}
    for cid, vs in buckets.items():
        vs.sort()
        n = len(vs)
        out[cid] = vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2.0
    return out


def _explosion_score(views, baseline):
    """Índice de explosión de un reel relativo a la media de su creador.
    None si no hay baseline fiable."""
    if not baseline or baseline < 1:
        return None
    return round(float(views or 0) / float(baseline), 2)


def _unit_hash(s: str) -> float:
    """Hash determinista [0,1) (estable entre workers, a diferencia de hash())."""
    import hashlib
    return int(hashlib.sha1(str(s).encode()).hexdigest()[:8], 16) / float(0xFFFFFFFF)


def _radar_feed_order(uid, project_id, creator_ids, day_str=None, use_cache=True):
    """ORDEN rankeado y ROTATIVO por día del feed del radar, del pool YA scrapeado
    (CERO scrape — solo re-rank). score = explosión × frescura × jitter(reel+uid+día):
    la frescura sube lo recién explotado y decae los tops de ayer; el jitter sembrado
    por fecha rota la banda media → el feed cambia cada mañana sin guardar estado en BD.
    Cachea el ORDEN (lista ligera de ids) en Redis por (uid,marca,día); degrada a
    cómputo directo si Redis cae. use_cache=False (p.ej. filtro por 1 creador) computa
    fresco sin leer/escribir la caché del feed completo (evita envenenarla). Devuelve
    [{'id','creator_id'}] ordenado."""
    day_str = day_str or datetime.now(timezone.utc).strftime("%Y%m%d")
    # nonce de re-baraja: el botón «↻ otras» (gratis) lo sube → el siguiente cómputo varía el
    # orden del MISMO pool, sin scrape. Entra en el jitter (no en la ckey: el reshuffle ya
    # invalida la caché del día, así que el recómputo escribe el nuevo orden en la misma key).
    rerank_nonce = _reshuffle_nonce(uid, project_id)
    ckey = "feedcache:%s:%s:%s" % (uid, project_id or "_", day_str)
    if use_cache and rds is not None:
        try:
            cached = rds.get(ckey)
            if cached:
                return json.loads(cached)
        except Exception:
            pass
    creator_ids = [c for c in (creator_ids or []) if c]
    if not creator_ids:
        return []
    try:
        cand = (db.table("creator_reels_global")
                  .select("id, creator_id, views, posted_at")
                  .eq("is_archived", False)
                  .in_("creator_id", creator_ids)
                  .order("posted_at", desc=True)
                  .limit(300).execute()).data or []
    except Exception:
        logger.warning("[feed] candidate read failed uid=%s", uid)
        return []
    # Penaliza los reels SERVIDOS ayer → rotación real (suben los del siguiente tramo).
    yday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    seen = set()
    if rds is not None:
        try:
            seen = {m.decode() if isinstance(m, bytes) else m
                    for m in (rds.smembers("feedserved:%s:%s:%s" % (uid, project_id or "_", yday)) or [])}
        except Exception:
            seen = set()
    baselines = _creator_view_baselines(creator_ids)
    now = datetime.now(timezone.utc)
    scored = []
    for r in cand:
        exp = _explosion_score(r.get("views"), baselines.get(r.get("creator_id"))) or 0.0
        ts = _parse_ts(r.get("posted_at"))
        age_days = ((now - ts).total_seconds() / 86400.0) if ts else 30.0
        fresh = 0.5 ** (max(0.0, age_days) / RADAR_FEED_HALF_LIFE_DAYS)
        # jitter FUERTE sembrado por día (multiplicador [0.4,1.0]): baraja de verdad la
        # banda de mérito similar → el TOP cambia cada día (lo que pide "no repite los
        # mismos tops"), sin que un reel mediocre supere a uno muy explosivo.
        jit = 0.4 + 0.6 * _unit_hash("%s:%s:%s:%s" % (r.get("id"), uid, day_str, rerank_nonce))
        score = exp * fresh * jit
        if r.get("id") in seen:
            score *= RADAR_FEED_SEEN_PENALTY
        scored.append((score, {"id": r.get("id"), "creator_id": r.get("creator_id")}))
    scored.sort(key=lambda s: s[0], reverse=True)
    order = [s[1] for s in scored]
    # NO cachear orden vacío: si los competidores aún no tienen reels (scrape async en
    # curso), cachear [] fijaría el feed a SEED toda la TTL → el primer scrape no entraría.
    if use_cache and rds is not None and order:
        try:
            rds.set(ckey, json.dumps(order), ex=RADAR_FEED_CACHE_TTL)
            served = [o["id"] for o in order[:RADAR_FEED_SERVED_N] if o.get("id")]
            if served:
                tkey = "feedserved:%s:%s:%s" % (uid, project_id or "_", day_str)
                rds.sadd(tkey, *served)
                rds.expire(tkey, RADAR_FEED_SEEN_TTL)
        except Exception:
            pass
    return order


def _radar_feed_invalidate(uid, project_id=None, day_str=None):
    """Invalida la caché del feed del día (tras un refresco manual que trae reels nuevos)."""
    if rds is None:
        return
    day_str = day_str or datetime.now(timezone.utc).strftime("%Y%m%d")
    try:
        rds.delete("feedcache:%s:%s:%s" % (uid, project_id or "_", day_str))
    except Exception:
        pass


def _reshuffle_nonce(uid, project_id=None):
    """Nonce de re-baraja por (usuario, marca). Lo usan el feed y las sugerencias en su
    jitter → «↻ otras» varía el orden del MISMO pool sin scrape. 0 si Redis cae (orden estable)."""
    if rds is None:
        return 0
    try:
        v = rds.get("reshuffle:%s:%s" % (uid, project_id or "_"))
        return int(v) if v else 0
    except Exception:
        return 0


def _reshuffle_bump(uid, project_id=None):
    """Sube el nonce de re-baraja (botón «↻ otras», GRATIS). TTL ~2d (se reinicia solo)."""
    if rds is None:
        return 0
    try:
        n = rds.incr("reshuffle:%s:%s" % (uid, project_id or "_"))
        rds.expire("reshuffle:%s:%s" % (uid, project_id or "_"), 2 * 86400)
        return int(n)
    except Exception:
        return 0


def _dismissed_creator_ids(uid, project_id=None):
    """creator_ids que el usuario DESCARTÓ de las sugerencias del feed → no volver a
    sugerir. Aísla por marca si procede. Degrada a set() si falta la migración."""
    try:
        q = db.table("user_dismissed_creators").select("creator_id").eq("user_id", uid)
        if project_id:
            q = q.eq("project_id", project_id)
        return {r["creator_id"] for r in (q.execute().data or []) if r.get("creator_id")}
    except Exception:
        return set()


@app.route("/img/reel/<reel_id>")
def reel_thumb(reel_id):
    """Sirve la MINIATURA de un reel SIEMPRE desde reelscript.net (nunca hotlink al CDN
    de IG, cuyas URLs firmadas caducan → ERR_CONNECTION_TIMED_OUT). Lee el data-URI
    cacheado (creator_reels_global.thumb_b64), lo decodifica y lo entrega como imagen
    con cache largo. 404 si no hay miniatura (el front muestra placeholder)."""
    try:
        r = db.table("creator_reels_global").select("thumb_b64").eq("id", reel_id).single().execute()
        b64 = ((r.data or {}).get("thumb_b64") or "")
    except Exception:
        b64 = ""
    if not b64.startswith("data:image"):
        return "", 404
    try:
        import base64 as _b64
        header, payload = b64.split(",", 1)
        raw = _b64.b64decode(payload)
        mime = header.split(";")[0].split(":", 1)[1] or "image/jpeg"
    except Exception:
        return "", 404
    resp = app.response_class(raw, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=604800, immutable"   # 7 días
    return resp


@app.route("/api/reels/by-format", methods=["GET"])
@require_auth
def reels_by_format():
    """EJEMPLOS reales del pool con un FORMATO dado (tarjeta «Cómo grabarlo»). Reusa lo
    cacheado (creator_reels_global.formato) — CERO scrape nuevo. DESCUBRIMIENTO: EXCLUYE a
    los creadores que el usuario YA sigue en su radar → las 2 referencias vienen de OTROS
    creadores del pool global (no de su propia competencia). Prioriza mismo nicho/subnicho
    (gente que NO sigue); si no hay suficientes fuera del radar, completa con el pool global
    (siempre fuera del radar primero). Ordena por explosión. Estado honesto si no hay nada."""
    user = current_user()
    uid = user["id"]
    fmt = (request.args.get("format") or "").strip().lower()
    if fmt not in REC_FORMAT_KEYS:
        return jsonify({"error": "bad_format"}), 400
    exclude = (request.args.get("exclude") or "").strip()
    project_id = request.args.get("project_id")
    # SEL ligero: NO traemos thumb_b64 (pesado); la miniatura se sirve por /img/reel/<id>.
    SEL = ("id, ig_reel_id, creator_id, views, video_duration_sec, "
           "creator:creators_global(ig_username)")

    def _pack(rows, baselines):
        out = []
        for r in rows:
            rid = r.get("id")
            if not rid or rid == exclude:
                continue
            sc = r.get("ig_reel_id")
            cr = r.get("creator") or {}
            out.append({
                "id": rid,
                "ig_reel_id": sc,
                "creator_id": r.get("creator_id"),   # interno: dedup por creador (se elimina antes de responder)
                "handle": cr.get("ig_username") or "",
                # SIEMPRE desde reelscript.net (nunca hotlink IG). 404 → placeholder en el front.
                "thumb": "/img/reel/%s" % rid,
                "url": ("https://www.instagram.com/reel/%s/" % sc) if sc else None,
                "explosion": _explosion_score(r.get("views"), baselines.get(r.get("creator_id"))),
            })
        return out

    picked, seen = [], set()

    def _consume(rows, drop_cids):
        """Filtra los creadores del radar (drop_cids), puntúa y acumula sin duplicar reels."""
        rows = [r for r in (rows or []) if r.get("creator_id") not in drop_cids]
        if not rows:
            return
        base = _creator_view_baselines(list({r.get("creator_id") for r in rows}))
        for e in _pack(rows, base):
            if e["id"] not in seen:
                seen.add(e["id"]); picked.append(e)

    try:
        # 0) creadores que el usuario YA sigue (radar) → EXCLUIDOS de las referencias.
        #    Filtrado por marca si procede (cada proyecto tiene su propio radar).
        tq = (db.table("user_tracked_creators").select("creator_id")
                .eq("user_id", uid).is_("archived_at", "null"))
        if project_id:
            tq = tq.eq("project_id", project_id)
        my_cids = {t["creator_id"] for t in (tq.execute().data or [])}

        # 1) DESCUBRIMIENTO por nicho: creadores del MISMO subnicho que el usuario que
        #    NO sigue. Así la referencia se siente «descubrimiento», no reciclar su radar.
        try:
            prof = (db.table("profiles").select("niche,subniches")
                      .eq("id", uid).single().execute()).data or {}
            subs = [t for t in (_norm_tag(s) for s in (prof.get("subniches") or [])) if t][:8]
            if not subs and prof.get("niche"):
                subs = [t for t in (_norm_tag(s) for s in _subniche_suggestions(prof["niche"])) if t][:8]
            if subs:
                cg = (db.table("creators_global").select("id")
                        .overlaps("subniches", subs).limit(120).execute()).data or []
                niche_cids = [c["id"] for c in cg if c.get("id") and c["id"] not in my_cids]
                if niche_cids:
                    nrows = (db.table("creator_reels_global").select(SEL)
                               .eq("formato", fmt).eq("is_archived", False)
                               .in_("creator_id", niche_cids)
                               .order("views", desc=True).limit(24).execute()).data or []
                    _consume(nrows, my_cids)
        except Exception:
            logger.warning("[by-format] niche discovery failed (¿migración subniches?)")

        # 2) completa con el pool GLOBAL del mismo formato — SIEMPRE excluyendo el radar,
        #    para que las referencias nunca sean de creadores que el usuario ya sigue.
        if len(picked) < 4:
            glob = (db.table("creator_reels_global").select(SEL)
                      .eq("formato", fmt).eq("is_archived", False)
                      .order("views", desc=True).limit(60).execute()).data or []
            _consume(glob, my_cids)

        # explosión desc (None al final), top 4.
        picked.sort(key=lambda e: (e["explosion"] is not None, e["explosion"] or 0), reverse=True)
        # DEDUP POR CREADOR: como mucho 1 reel por creador (su mejor explosión, ya ordenado)
        #   → 2 ejemplos = 2 creadores distintos = descubrimiento real, no la misma persona 2×.
        out, used = [], set()
        for e in picked:
            cid = e.pop("creator_id", None)
            if cid in used:
                continue
            used.add(cid); out.append(e)
            if len(out) >= 4:
                break
        return jsonify({"format": fmt, "examples": out})
    except Exception as e:
        # Columna `formato` aún sin migrar u otro fallo → estado honesto (sin ejemplos).
        if "formato" not in str(e).lower():
            logger.warning("reels_by_format failed: %s", e, exc_info=True)
        return jsonify({"format": fmt, "examples": []})


def _fmt_views(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n/1_000:.1f}K".replace(".0K", "K")
    return str(n)


def _build_brand_report(user_id, project_id):
    """Datos del informe white-label de una marca: top reels que petaron en su
    nicho este mes (de sus competidores) + guiones listos del mes. Reusa el
    scoring de explosión del Radar (_creator_view_baselines/_explosion_score)."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    pid = project_id if (project_id and project_id != "default") else None

    # 1. Competidores activos de la marca.
    tq = (db.table("user_tracked_creators")
            .select("creator_id")
            .eq("user_id", user_id)
            .is_("archived_at", "null"))
    tq = tq.eq("project_id", pid) if pid else tq.is_("project_id", "null")
    try:
        tracked = tq.execute().data or []
    except Exception:
        tracked = []
    cids = list({t["creator_id"] for t in tracked if t.get("creator_id")})

    reels = []
    if cids:
        baselines = _creator_view_baselines(cids)
        try:
            rows = (db.table("creator_reels_global")
                      .select("caption, views, creator_id, posted_at, "
                              "creator:creators_global(ig_username)")
                      .in_("creator_id", cids)
                      .eq("is_archived", False)
                      .gte("posted_at", since)
                      .limit(200).execute()).data or []
        except Exception:
            rows = []
        scored = []
        for r in rows:
            sc = _explosion_score(r.get("views"), baselines.get(r.get("creator_id")))
            if sc is not None and sc >= 1.5:
                scored.append({
                    "username": (r.get("creator") or {}).get("ig_username") or "",
                    "caption": (r.get("caption") or "").strip()[:160] or "—",
                    "views": r.get("views") or 0,
                    "views_fmt": _fmt_views(r.get("views")),
                    "_score": sc,
                    "explosion": (f"{sc:.0f}" if sc == int(sc) else f"{sc:.1f}"),
                })
        scored.sort(key=lambda x: x["_score"], reverse=True)
        reels = scored[:5]

    # 2. Guiones listos del mes (de la marca).
    sq = (db.table("scripts").select("title, script, created_at")
            .eq("user_id", user_id).gte("created_at", since))
    if pid:
        sq = sq.eq("project_id", pid)
    try:
        srows = sq.order("created_at", desc=True).limit(8).execute().data or []
    except Exception:
        srows = []
    scripts = []
    for s in srows:
        sc = s.get("script") or {}
        hook = sc.get("hook") if isinstance(sc, dict) else ""
        scripts.append({"title": (s.get("title") or "Guion").strip()[:100],
                        "hook": (hook or "").strip()[:160]})

    return {"reels": reels, "scripts": scripts, "competitors": len(cids)}


def _recycled_reels(subniches, niche=None, limit=20, exclude_creator_ids=None,
                    rank="views", min_explosion=None):
    """SEED ALGORÍTMICO del Radar (SPEC-fase-accion-radar #3, Fathom 18/06):
    reels que YA petaron de creadores tageados con el subnicho del user — SIN
    scrape (cero coste/latencia). Reusa creators_global + creator_reels_global,
    mismo shape que el feed real + marca source='seed' para que el front etiquete
    «del nicho» (no «de tu competidor»). Si el user no tiene subniches, cae a las
    sugerencias del nicho. Devuelve [] si no hay nada (cold-start total)."""
    subs = [t for t in (_norm_tag(s) for s in (subniches or [])) if t][:8]
    if not subs and niche:
        subs = [t for t in (_norm_tag(s) for s in _subniche_suggestions(niche)) if t][:8]
    if not subs:
        return []
    exclude = set(exclude_creator_ids or [])
    try:
        cg = (db.table("creators_global").select("id, ig_username")
                .overlaps("subniches", subs).limit(60).execute()).data or []
    except Exception:
        logger.warning("[seed] creators_global overlaps failed (¿migración subniches?)")
        return []
    uname = {c["id"]: c["ig_username"] for c in cg}
    cids = [c for c in uname if c not in exclude]
    if not cids:
        return []
    # rank="explosion" (descubrimiento "lo que PETA") necesita ventana amplia para
    # ordenar por explosión post-query; "views" (seed/fallback) basta con menos.
    window = max(limit * 4, 60) if rank == "explosion" else max(limit * 2, 30)
    try:
        rr = (db.table("creator_reels_global")
                .select("id, ig_reel_id, creator_id, caption, views, likes, comments, "
                        "posted_at, thumb_url, thumb_b64, video_duration_sec")
                .in_("creator_id", cids).eq("is_archived", False)
                .order("views", desc=True).limit(window).execute()).data or []
    except Exception:
        logger.warning("[seed] creator_reels_global read failed")
        return []
    baselines = _creator_view_baselines(cids)
    for r in rr:
        r["explosion_score"] = _explosion_score(r.get("views"), baselines.get(r.get("creator_id")))
    if rank == "explosion":
        rr.sort(key=lambda r: (r.get("explosion_score") or 0), reverse=True)
    if min_explosion is not None:
        rr = [r for r in rr if (r.get("explosion_score") or 0) >= min_explosion]
    out = []
    for r in rr[:limit]:
        r["creator"] = {"ig_username": uname.get(r.get("creator_id"), "")}
        r["is_favorite"] = False
        r["source"] = "seed"   # el front muestra microcopy honesto «mientras llenas tu radar»
        out.append(r)
    return out


def _autotrack_seed_creator(uid, profile, creator_id) -> bool:
    """SEED (SPEC #3): cuando el user roba un reel del seed de su nicho sin seguir
    aún al creador, lo auto-seguimos SI (a) el creador está tageado con su subnicho
    (es seed-válido, no un creador random) y (b) le quedan huecos de competidor en
    su plan. Inserta el tracking (marca «default», sin project) → el ownership pasa.
    Devuelve True si quedó seguido (o ya lo seguía), False si no procede."""
    try:
        # Subniches del user (o sugerencias del nicho si no tiene) y del creador.
        subs = [t for t in (_norm_tag(s) for s in (profile.get("subniches") or [])) if t]
        if not subs and profile.get("niche"):
            subs = [t for t in (_norm_tag(s) for s in _subniche_suggestions(profile["niche"])) if t]
        if not subs:
            return False
        cg = (db.table("creators_global").select("subniches")
                .eq("id", creator_id).single().execute()).data or {}
        c_subs = set(_norm_tag(s) for s in (cg.get("subniches") or []))
        if not c_subs or c_subs.isdisjoint(set(subs)):
            return False  # no es seed-válido para este user
        # Hueco de competidor en el plan (reverse-trial: plan efectivo).
        plan = effective_plan(profile)
        limits = get_tracked_creators_limit(plan)
        if not limits.get("enabled"):
            return False
        cap = limits["base_slots_global"] + get_user_extra_slots(uid)
        if count_active_tracked(uid, scope="global") >= cap:
            return False  # sin hueco → que vea el muro de plan por la vía normal
        # Reactivar una fila archivada si existe (evita chocar con UNIQUE user+creator).
        existing = (db.table("user_tracked_creators").select("id, archived_at")
                      .eq("user_id", uid).eq("creator_id", creator_id).limit(1).execute()).data or []
        if existing:
            db.table("user_tracked_creators").update({"archived_at": None}) \
              .eq("id", existing[0]["id"]).execute()
        else:
            db.table("user_tracked_creators").insert({
                "user_id": uid, "creator_id": creator_id, "project_id": None,
            }).execute()
        track_event("seed_autotrack", uid, {"creator_id": creator_id})
        return True
    except Exception:
        logger.warning("seed autotrack failed uid=%s creator=%s", uid, creator_id, exc_info=True)
        return False


@app.route("/api/tracked-creators/reels", methods=["GET"])
@require_auth
def get_tracked_creators_reels():
    """Feed unificado de reels de los competidores activos del user.

    Query params:
      - creator_id (uuid, opcional): filtra a un único competidor del user.
                                     404 si no le pertenece.
      - favorites=true (v0.15.6): filtra a reels marcados como favoritos por
                                  el user. No restringe por creator_ids
                                  (favoritos de competidores des-trackeados
                                  siguen visibles — marcar es marcar).
      - sort: recent (posted_at desc) | views | likes. Default 'recent'.
      - limit: 1-50, default 20.
      - offset: paginación. Default 0.

    Return: {reels: [...], total, has_more}. Cada reel incluye is_favorite (v0.15.6).
    """
    user = current_user()
    uid = user["id"]

    # 1. v0.15.6: set de reel_ids favoritos del user (para flag + filtro).
    fav_r = (db.table("user_favorite_reels")
               .select("reel_id")
               .eq("user_id", uid)
               .execute())
    fav_ids = {f["reel_id"] for f in (fav_r.data or [])}

    favorites_only = (request.args.get("favorites") or "").lower() == "true"
    if favorites_only and not fav_ids:
        return jsonify({"reels": [], "total": 0, "has_more": False})

    # 2. Set de creator_ids activos del user (deduplicado).
    # P0 aislamiento por marca: filtrar por project_id (mismo patrón que
    # GET /api/tracked-creators). Sin parámetro → todas (marca "default").
    project_id = request.args.get("project_id")
    tq = (db.table("user_tracked_creators")
            .select("creator_id")
            .eq("user_id", uid)
            .is_("archived_at", "null"))
    if project_id:
        tq = tq.eq("project_id", project_id)
    tracked = tq.execute()
    creator_ids = list({t["creator_id"] for t in (tracked.data or [])})
    # SEED (SPEC #3): reciclamos reels que petaron en el subnicho del user. Sirve para
    # que el radar NUNCA quede vacío (un radar vacío rompe el house tour — sus pasos
    # apuntan a .feature/.rgal/«robar» — y la 1ª impresión). source='seed'.
    def _seed_response():
        try:
            prof = get_profile(uid)
            return _recycled_reels(prof.get("subniches"), prof.get("niche"), limit=20)
        except Exception:
            logger.warning("[seed] feed fallback failed uid=%s", uid)
            return []

    if not creator_ids and not favorites_only:
        # user sin competidores → seed directo (acantilado de activación).
        seed = _seed_response()
        return jsonify({"reels": seed, "total": len(seed),
                        "has_more": False, "seed": True})

    # 3. Validar creator_id si pasa.
    filter_creator_id = request.args.get("creator_id")
    if filter_creator_id:
        if filter_creator_id not in creator_ids:
            return jsonify({"error": "tc.error.tracking_not_found"}), 404
        creator_ids = [filter_creator_id]

    # 4. Sort. v0.16.x Radar: 'explosion' = views relativos a la media del creador.
    sort = request.args.get("sort", "recent")
    explosion_sort = (sort == "explosion")
    sort_col = {"recent": "posted_at", "views": "views", "likes": "likes"}.get(sort, "posted_at")

    # 5. Limit + offset.
    try:
        limit = max(1, min(50, int(request.args.get("limit", "20"))))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except (TypeError, ValueError):
        offset = 0

    SEL = ("id, ig_reel_id, creator_id, caption, views, likes, comments, "
           "posted_at, thumb_url, thumb_b64, video_duration_sec, "
           "creator:creators_global(ig_username)")

    # 6. Baselines por creador → índice de explosión en cada reel.
    baselines = _creator_view_baselines(creator_ids) if creator_ids else {}

    def _attach_shares(reels):
        # ítem 6: los compartidos viven en una columna nueva (creator_reels_global.shares).
        # Best-effort: si la columna aún no está migrada, el except deja shares=None y el
        # feed sigue funcionando (la UI muestra "–").
        ids = [r.get("id") for r in reels if r.get("id")]
        if not ids:
            return
        try:
            sr = db.table("creator_reels_global").select("id, shares").in_("id", ids).execute()
            sm = {x["id"]: x.get("shares") for x in (sr.data or [])}
            for r in reels:
                r["shares"] = sm.get(r.get("id"))
        except Exception:
            pass

    def _annotate(reels):
        for r in reels:
            r["is_favorite"] = r["id"] in fav_ids
            r["explosion_score"] = _explosion_score(
                r.get("views"), baselines.get(r.get("creator_id")))
        _attach_shares(reels)
        return reels

    # 7a. Orden por explosión = feed diario ROTATIVO (score = explosión × frescura ×
    #     jitter por día). El orden se cachea por día en Redis (CERO scrape). Servimos
    #     solo la página pedida (ids cacheados → fetch ligero, no 200 filas pesadas).
    if explosion_sort and not favorites_only:
        # Filtro por 1 creador (creator_id=X) NO debe leer/escribir la caché del feed
        # completo (compartirían slot y se envenenarían). use_cache=False computa fresco.
        order = _radar_feed_order(uid, project_id, creator_ids, use_cache=not filter_creator_id)
        if not order and offset == 0:
            # Competidores aún sin reels (scrape async pendiente) o sin reels → seed,
            # para no dejar el radar (ni el house tour) vacío en el first-run.
            seed = _seed_response()
            if seed:
                return jsonify({"reels": seed, "total": len(seed),
                                "has_more": False, "seed": True})
        total = len(order)
        page_ids = [o["id"] for o in order[offset:offset + limit] if o.get("id")]
        if not page_ids:
            return jsonify({"reels": [], "total": total, "has_more": False})
        rows = (db.table("creator_reels_global").select(SEL)
                  .in_("id", page_ids).execute()).data or []
        by_id = {r.get("id"): r for r in rows}
        page = [by_id[i] for i in page_ids if i in by_id]   # preserva el orden cacheado
        _annotate(page)
        has_more = (offset + len(page)) < total
        return jsonify({"reels": page, "total": total, "has_more": has_more})

    # 7b. recent / views / likes (o favoritos): orden + paginación en DB.
    q = (db.table("creator_reels_global")
           .select(SEL, count="exact")
           .eq("is_archived", False)
           .order(sort_col, desc=True)
           .range(offset, offset + limit - 1))
    if favorites_only:
        # v0.15.6: filtro por favoritos NO restringe por creator_ids — un user
        # puede haber des-trackeado un creator y conservar reels favoritos suyos.
        q = q.in_("id", list(fav_ids))
    else:
        q = q.in_("creator_id", creator_ids)
    rows = q.execute()
    reels = _annotate(rows.data or [])
    if not reels and not favorites_only and offset == 0:
        # mismo fallback seed que el path de explosión (radar nunca vacío en first-run).
        seed = _seed_response()
        if seed:
            return jsonify({"reels": seed, "total": len(seed),
                            "has_more": False, "seed": True})
    total = rows.count or 0
    has_more = (offset + len(reels)) < total

    return jsonify({"reels": reels, "total": total, "has_more": has_more})


@app.route("/brands/<project_id>/report", methods=["GET"])
@require_auth
def brand_report(project_id):
    """Informe white-label PDF (HTML print-ready) por marca — entregable estrella
    de Agencia. 'Lo que funciona en tu nicho este mes + guiones listos'. Logo y
    nombre configurables (agencia / cliente / sin marca ReelScript) vía query.
    Agency-only. Billing per-brand NO implementado (el generador sí)."""
    user = current_user()
    profile = get_profile(user["id"])
    plan = profile.get("plan", "free")
    is_admin = user.get("email", "").lower() in UNLIMITED_EMAILS
    if not (is_admin or (plan == "agency" and paid_features_active(profile, user))):
        return jsonify({"error": "El informe white-label es una función de Agencia."}), 403

    # Ownership de la marca (project) — salvo "default" (marca única).
    if project_id and project_id != "default":
        try:
            proj = (db.table("projects").select("id, name")
                      .eq("id", project_id).eq("user_id", user["id"]).single().execute())
            if not proj.data:
                return abort(404)
            default_label = proj.data.get("name") or "Tu marca"
        except Exception:
            return abort(404)
    else:
        default_label = "Tu marca"

    data = _build_brand_report(user["id"], project_id)

    lang = (request.args.get("lang") or _resolve_lang() or "es")[:2]
    if lang not in ("es", "en"):
        lang = "es"
    # Branding configurable: label (nombre cliente/agencia), logo (URL), white_label.
    brand_label = (request.args.get("label") or default_label).strip()[:60]
    logo_url = (request.args.get("logo") or "").strip()[:500] or None
    if logo_url and not logo_url.startswith(("http://", "https://", "/")):
        logo_url = None   # solo URLs (evita inyección)
    white_label = request.args.get("white_label") in ("1", "true", "yes")

    now = datetime.now(timezone.utc)
    months_es = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto",
                 "septiembre","octubre","noviembre","diciembre"]
    months_en = ["January","February","March","April","May","June","July","August",
                 "September","October","November","December"]
    period = f"{(months_es if lang=='es' else months_en)[now.month-1]} {now.year}"
    generated_on = now.strftime("%d/%m/%Y")

    T = {
        "es": {"report": "Informe de marca", "title": "Lo que funciona en tu nicho",
               "subtitle": "Los reels que petaron entre tus competidores este mes y los guiones que ya tienes listos para grabar.",
               "kpi_reels": "reels que petaron", "kpi_scripts": "guiones listos",
               "kpi_competitors": "competidores vigilados",
               "sec_reels": "Lo que petó en tu nicho", "sec_scripts": "Tus guiones listos",
               "over_avg": "sobre su media", "views": "visitas",
               "empty_reels": "Aún sin reels explosivos este mes. Vuelve cuando tus competidores publiquen.",
               "empty_scripts": "Aún no hay guiones de este mes para esta marca.",
               "made_with": "Hecho con ReelScript", "download": "Descargar PDF"},
        "en": {"report": "Brand report", "title": "What's working in your niche",
               "subtitle": "The reels that blew up among your competitors this month and the scripts you already have ready to record.",
               "kpi_reels": "reels that blew up", "kpi_scripts": "scripts ready",
               "kpi_competitors": "competitors watched",
               "sec_reels": "What blew up in your niche", "sec_scripts": "Your ready scripts",
               "over_avg": "over their avg", "views": "views",
               "empty_reels": "No explosive reels this month yet. Check back when your competitors post.",
               "empty_scripts": "No scripts for this brand this month yet.",
               "made_with": "Made with ReelScript", "download": "Download PDF"},
    }[lang]

    return render_template("brand_report.html",
                           lang=lang, t=T, brand_label=brand_label, logo_url=logo_url,
                           white_label=white_label, period=period, generated_on=generated_on,
                           reels=data["reels"], scripts=data["scripts"],
                           competitors=data["competitors"])


@app.route("/api/radar/stats", methods=["GET"])
@require_auth
def radar_stats():
    """v0.16.x Radar: contadores de la barra de estado (FOMO) de la home.

    Return: {competitors, reels_week, exploded_week, stolen_total, enabled}.
      - exploded_week: reels de la última semana con explosión >= 2x.
      - stolen_total: scripts del user generados desde un reel de competidor.
    """
    user = current_user()
    uid = user["id"]

    # P0 aislamiento por marca: filtrar por project_id (mismo patrón que
    # GET /api/tracked-creators). Sin parámetro → todas (marca "default").
    project_id = request.args.get("project_id")
    tq = (db.table("user_tracked_creators")
            .select("creator_id")
            .eq("user_id", uid)
            .is_("archived_at", "null"))
    if project_id:
        tq = tq.eq("project_id", project_id)
    tracked = tq.execute()
    creator_ids = list({t["creator_id"] for t in (tracked.data or [])})
    competitors = len(creator_ids)

    reels_week = exploded_week = 0
    if creator_ids:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rows = (db.table("creator_reels_global")
                  .select("creator_id, views, posted_at")
                  .in_("creator_id", creator_ids)
                  .eq("is_archived", False)
                  .gte("posted_at", since)
                  .limit(500)
                  .execute()).data or []
        reels_week = len(rows)
        baselines = _creator_view_baselines(creator_ids)
        for r in rows:
            sc = _explosion_score(r.get("views"), baselines.get(r.get("creator_id")))
            if sc is not None and sc >= 2.0:
                exploded_week += 1

    try:
        stolen = (db.table("scripts")
                    .select("id", count="exact")
                    .eq("user_id", uid)
                    .not_.is_("from_competitor_reel_id", "null")
                    .limit(1)
                    .execute())
        stolen_total = stolen.count or 0
    except Exception:
        stolen_total = 0

    # stolen_today: el JS lee stolen_today (label "robados hoy"). Filtramos por hoy
    # via created_at; si falla, lo aliaseamos a stolen_total (no crashea el front).
    try:
        today = str(date.today())
        stolen_t = (db.table("scripts")
                      .select("id", count="exact")
                      .eq("user_id", uid)
                      .not_.is_("from_competitor_reel_id", "null")
                      .gte("created_at", today)
                      .limit(1)
                      .execute())
        stolen_today = stolen_t.count or 0
    except Exception:
        stolen_today = stolen_total

    plan = (get_profile(uid) or {}).get("plan", "free")
    enabled = bool(get_tracked_creators_limit(plan).get("enabled"))

    return jsonify({
        "competitors": competitors,
        "reels_week": reels_week,
        "exploded_week": exploded_week,
        "stolen_total": stolen_total,
        "stolen_today": stolen_today,
        "enabled": enabled,
    })


def _fmt_cooldown(seconds) -> str:
    s = int(seconds or 0)
    if s >= 3600:
        return "%dh" % max(1, round(s / 3600))
    return "%d min" % max(1, round(s / 60))


def _refresh_now_refund(uid, amount, is_paid_unlimited, cd_key=None):
    """Reembolsa el cobro del refresco manual (units o cents según rail) y limpia el
    cooldown. Se usa si el scrape no llega a lanzarse; el reembolso por 'todo falló'
    lo hace finalize_manual_refresh en el worker."""
    if amount:
        try:
            p = get_profile(uid)
            if is_paid_unlimited:
                db.table("profiles").update({
                    "monthly_usage": max(0, (p.get("monthly_usage") or 0) - amount)
                }).eq("id", uid).execute()
            else:
                db.table("profiles").update({
                    "credits_cents": (p.get("credits_cents") or 0) + amount
                }).eq("id", uid).execute()
        except Exception:
            logger.warning("refresh_now refund failed uid=%s", uid)
    if cd_key and rds is not None:
        try:
            rds.delete(cd_key)
        except Exception:
            pass


@app.route("/api/radar/refresh", methods=["POST"])
@require_auth
@limiter.limit("12 per minute")
def radar_refresh():
    """v1 feed diario: "re-rank" GRATIS (CERO scrape). Antes encolaba scrape on-demand;
    ahora ese scrape vive SOLO en /api/radar/refresh-now (de pago) → "el manual es la
    única vía de scrape on-demand" (protege el cap de Apify). Aquí solo invalidamos la
    caché del día: el siguiente load re-rankea el pool YA scrapeado. Aísla por marca."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id") or None
    _radar_feed_invalidate(uid, project_id)
    track_event("radar_rerank", uid, {"project_id": project_id})
    return jsonify({"ok": True, "queued": 0, "message": "Radar al día."})


@app.route("/api/radar/refresh-now", methods=["POST"])
@require_auth
@limiter.limit("6 per minute")
def radar_refresh_now():
    """Refresco manual de PAGO — ÚNICA vía de scrape on-demand. Fuerza scrape fresco de
    los competidores de la marca (ignora staleness), cuesta REFRESH_NOW_UNITS créditos,
    con COOLDOWN por usuario+marca (anti-abuso / cap de coste Apify). Cobra solo si el
    scrape se LANZA; si TODO falla, finalize_manual_refresh reembolsa. El re-rank diario
    sigue gratis. Dev (UNLIMITED_EMAILS) gratis y sin cooldown."""
    user = current_user()
    uid = user["id"]
    email = (user.get("email") or "").lower()
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id") or None
    pid = project_id or "_"
    is_courtesy = email in UNLIMITED_EMAILS
    cd_key = "manualrefresh:%s:%s" % (uid, pid)

    # competidores activos de la marca
    q = (db.table("user_tracked_creators").select("creator_id")
           .eq("user_id", uid).is_("archived_at", "null"))
    if project_id:
        q = q.eq("project_id", project_id)
    try:
        cids = list({r["creator_id"] for r in (q.execute().data or []) if r.get("creator_id")})
    except Exception:
        cids = []
    if not cids:
        return jsonify({"ok": True, "queued": 0, "charged": False,
                        "message": "Aún no sigues a ningún competidor."})

    # 1) COOLDOWN primero (antes de cobrar nada). Cortesía lo salta.
    if not is_courtesy and rds is not None:
        try:
            ttl = rds.ttl(cd_key)
        except Exception:
            ttl = -2
        if ttl and ttl > 0:
            return jsonify({"ok": False, "cooldown": True, "retry_after_s": int(ttl),
                            "message": "Acabas de refrescar. Vuelve en %s." % _fmt_cooldown(ttl)}), 429

    # 2) Elegibles: ignora staleness, descarta private/not_found/scraping, cap Apify.
    eligible = []
    try:
        rows = []
        for i in range(0, len(cids), 100):
            r = (db.table("creators_global").select("id, scrape_status")
                   .in_("id", cids[i:i + 100]).execute())
            rows.extend(r.data or [])
        for cr in rows:
            if cr.get("scrape_status") in ("private", "not_found", "scraping"):
                continue
            eligible.append(cr["id"])
            if len(eligible) >= PER_USER_REFRESH_CAP:
                break
    except Exception:
        logger.warning("refresh_now: eligible read failed uid=%s", uid)
        eligible = cids[:PER_USER_REFRESH_CAP]
    if not eligible:
        # nada scrapeable (todos privados/inexistentes/en-curso) → ni cobro ni cooldown.
        return jsonify({"ok": True, "queued": 0, "charged": False,
                        "message": "Tu radar ya está al día."})

    # 3) Cobro dual-rail (salvo cortesía). Bajo lock de créditos (evita doble-gasto).
    profile = get_profile(uid)
    plan = profile.get("plan", "free")
    is_paid_unlimited = plan in ("pro", "creator", "estudio", "agency")
    charged_amount = 0
    charge_id = _uuid.uuid4().hex   # nonce por-cobro → idempotencia del reembolso (frfinal:<id>)
    if not is_courtesy:
        if not is_paid_unlimited and (profile.get("credits_cents") or 0) < REFRESH_NOW_COST:
            return jsonify({"error": "no_credits",
                            "message": "Necesitas %d créditos para refrescar ahora." % REFRESH_NOW_UNITS}), 402
        tok = acquire_credit_lock(uid)
        if tok is None:
            return jsonify({"error": "busy", "message": "Otra operación en curso. Inténtalo de nuevo."}), 409
        try:
            profile = get_profile(uid)   # re-lee bajo lock
            # Saldo ANTES de tomar el cooldown (no bloquear 8h sin cobrar).
            if not is_paid_unlimited and (profile.get("credits_cents") or 0) < REFRESH_NOW_COST:
                return jsonify({"error": "no_credits", "message": "Sin créditos suficientes."}), 402
            # COOLDOWN ATÓMICO bajo el lock (SET NX): vence la carrera TOCTOU (doble-click /
            # 2 pestañas) — el ttl pre-lock de arriba es solo fast-path. Si otro request ya lo
            # tomó → 429 sin cobrar. Sin Redis (degradado) no hay dedup, como el resto del código.
            if rds is not None:
                try:
                    got = rds.set(cd_key, "1", nx=True, ex=REFRESH_NOW_COOLDOWN_S)
                except Exception:
                    got = True
                if not got:
                    return jsonify({"ok": False, "cooldown": True, "retry_after_s": REFRESH_NOW_COOLDOWN_S,
                                    "message": "Acabas de refrescar. Vuelve en %s." % _fmt_cooldown(REFRESH_NOW_COOLDOWN_S)}), 429
            try:
                if is_paid_unlimited:
                    db.table("profiles").update({
                        "monthly_usage": (profile.get("monthly_usage") or 0) + REFRESH_NOW_UNITS
                    }).eq("id", uid).execute()
                    charged_amount = REFRESH_NOW_UNITS
                else:
                    db.table("profiles").update({
                        "credits_cents": (profile.get("credits_cents") or 0) - REFRESH_NOW_COST
                    }).eq("id", uid).execute()
                    charged_amount = REFRESH_NOW_COST
            except Exception as e:
                logger.error("refresh_now: charge failed uid=%s: %s", uid, e, exc_info=True)
                if rds is not None:   # cobro falló → libera el cooldown (no cobramos)
                    try:
                        rds.delete(cd_key)
                    except Exception:
                        pass
                return jsonify({"ok": False, "message": "No pude cobrar. Inténtalo de nuevo."}), 500
        finally:
            release_credit_lock(uid, tok)

    # 4) Chord: scrape forzado de cada elegible + finalize (reembolsa si TODO falla,
    #    invalida la caché del feed para que entren los reels nuevos). charge_id → idempotencia.
    try:
        from tasks import scrape_creator_task, finalize_manual_refresh
        from celery import chord, group
        chord(group(scrape_creator_task.s(c) for c in eligible))(
            finalize_manual_refresh.s(uid, project_id, charged_amount, is_paid_unlimited, charge_id))
    except Exception as e:
        logger.error("refresh_now: chord enqueue failed uid=%s: %s", uid, e, exc_info=True)
        _refresh_now_refund(uid, charged_amount, is_paid_unlimited, cd_key)  # no se lanzó → no cobramos
        return jsonify({"ok": False, "message": "No pude lanzar el refresco. Inténtalo de nuevo."}), 500

    track_event("radar_refresh_now", uid,
                {"queued": len(eligible), "charged": charged_amount, "project_id": project_id})
    return jsonify({"ok": True, "queued": len(eligible), "charged": bool(charged_amount),
                    "retry_after_s": (0 if is_courtesy else REFRESH_NOW_COOLDOWN_S),
                    "message": "Trayendo lo nuevo de tus competidores…"}), 202


@app.route("/api/radar/suggestions/dismiss", methods=["POST"])
@require_auth
@limiter.limit("60 per minute")
def radar_suggestion_dismiss():
    """Descarta una sugerencia de competidor → no volver a sugerirla (anti-repetición).
    Inserta en user_dismissed_creators. Acepta creator_id o handle. Aísla por marca."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    creator_id = body.get("creator_id")
    handle = (body.get("handle") or "").strip().lstrip("@").lower()
    project_id = body.get("project_id") or None
    if not creator_id and handle:
        try:
            r = db.table("creators_global").select("id").eq("ig_username", handle).single().execute()
            creator_id = (r.data or {}).get("id")
        except Exception:
            creator_id = None
    if not creator_id:
        return jsonify({"error": "bad_request", "message": "Falta creator_id."}), 400
    try:
        # check-then-insert (evita pile-up de duplicados con project_id NULL, que el
        # ON CONFLICT no casa porque Postgres trata NULL como distinto).
        chk = (db.table("user_dismissed_creators").select("creator_id")
                 .eq("user_id", uid).eq("creator_id", creator_id))
        chk = chk.eq("project_id", project_id) if project_id else chk.is_("project_id", "null")
        if not (chk.limit(1).execute().data or []):
            db.table("user_dismissed_creators").insert(
                {"user_id": uid, "creator_id": creator_id, "project_id": project_id}).execute()
    except Exception as e:
        logger.warning("suggestion dismiss failed uid=%s creator=%s: %s", uid, creator_id, e)
        return jsonify({"ok": False, "message": "No pude descartar. Inténtalo de nuevo."}), 500
    _radar_feed_invalidate(uid, project_id)
    track_event("radar_suggestion_dismiss", uid, {"creator_id": creator_id, "project_id": project_id})
    return jsonify({"ok": True}), 200


@app.route("/api/radar/fill-week/candidates", methods=["GET"])
@require_auth
def radar_fill_week_candidates():
    """v0.16.x Radar "Llena mi semana": selecciona los top reels explosivos
    (>= 2x) que el user aún NO ha convertido en guion, para generarlos en
    batch. NO cobra ni genera — solo selecciona. El frontend itera sobre el
    endpoint single-reel existente (que maneja lock/cobro/dedup/refund).

    Query: count (1-7, default 5).
    Return: {reels: [{id, username, explosion, caption, views}], affordable,
             cost_cents, unlimited, balance_cents}.
    """
    user = current_user()
    uid = user["id"]
    profile = get_profile(uid)
    plan = profile.get("plan", "free")

    if not get_tracked_creators_limit(plan)["enabled"]:
        return jsonify({"error": "upgrade_required",
                        "message": "Esta función requiere plan Pro o superior."}), 402

    try:
        count = max(1, min(7, int(request.args.get("count", "5"))))
    except (TypeError, ValueError):
        count = 5

    tracked = (db.table("user_tracked_creators")
                 .select("creator_id")
                 .eq("user_id", uid)
                 .is_("archived_at", "null")
                 .execute())
    creator_ids = list({t["creator_id"] for t in (tracked.data or [])})
    if not creator_ids:
        return jsonify({"reels": [], "affordable": 0, "cost_cents": COST_CENTS,
                        "unlimited": plan in ("pro", "creator", "agency"),
                        "balance_cents": profile.get("credits_cents") or 0})

    # Reels ya convertidos en guion por este user → excluir.
    already = (db.table("scripts")
                 .select("from_competitor_reel_id")
                 .eq("user_id", uid)
                 .not_.is_("from_competitor_reel_id", "null")
                 .execute())
    done_ids = {r["from_competitor_reel_id"] for r in (already.data or [])}

    baselines = _creator_view_baselines(creator_ids)
    rows = (db.table("creator_reels_global")
              .select("id, caption, views, posted_at, creator_id, "
                      "creator:creators_global(ig_username)")
              .in_("creator_id", creator_ids)
              .eq("is_archived", False)
              .order("posted_at", desc=True)
              .limit(200)
              .execute()).data or []

    scored = []
    for r in rows:
        if r["id"] in done_ids:
            continue
        sc = _explosion_score(r.get("views"), baselines.get(r.get("creator_id")))
        if sc is not None and sc >= 2.0:
            scored.append({
                "id": r["id"],
                "username": (r.get("creator") or {}).get("ig_username") or "",
                "explosion": sc,
                "caption": (r.get("caption") or "")[:160],
                "views": r.get("views") or 0,
            })
    scored.sort(key=lambda x: x["explosion"], reverse=True)
    picked = scored[:count]

    unlimited = plan in ("pro", "creator", "agency")
    balance = profile.get("credits_cents") or 0
    affordable = len(picked) if unlimited else min(len(picked), balance // COST_CENTS)

    return jsonify({
        "reels": picked,
        "affordable": affordable,
        "cost_cents": COST_CENTS,
        "unlimited": unlimited,
        "balance_cents": balance,
    })


# ── Scrape admin endpoint (v0.15.2.a: async vía Celery) ──────────────────────
# La función _scrape_creator() vivía aquí en v0.15.2 (sync). En v0.15.2.a
# migrada a tasks.py como scrape_creator_task (@celery_app.task) para no
# bloquear workers HTTP. Ver tasks.py para la lógica de scrape.
# DEUDA v0.15.4: stale guard si scrape muere mid-task (scrape_status queda
# 'scraping' permanente y anti-race bloquea re-scrape del creador).

@app.route("/admin/scrape/<creator_id>", methods=["POST"])
@require_auth
@limiter.limit("3 per minute")
def admin_scrape_creator(creator_id: str):
    """Admin-only: encola scrape async de un creator (tarea Celery).

    Devuelve inmediatamente (202 Accepted). El estado real vive en
    creators_global.scrape_status y es visible vía GET /api/tracked-creators.
    Fallback seguro: si ADMIN_EMAILS env no está set, 403 universal.
    """
    user = current_user()
    if not _is_admin(user):
        return jsonify({"error": "forbidden"}), 403
    # SELECT rápido valida existencia antes de encolar (evita tareas con IDs basura).
    cr = (db.table("creators_global")
            .select("id, ig_username")
            .eq("id", creator_id)
            .execute())
    if not cr.data:
        return jsonify({"status": "creator_not_found", "creator_id": creator_id}), 404
    # Lazy import para evitar coupling top-level con tasks.py (patrón usado para
    # send_email_now, refresh_metrics_bulk).
    from tasks import scrape_creator_task  # noqa: E402
    scrape_creator_task.delay(creator_id)
    return jsonify({
        "status": "queued",
        "creator_id": creator_id,
        "ig_username": cr.data[0]["ig_username"],
    }), 202


# ── Admin API: gestión DB-driven de planes / topups / usuarios / ajustes ──────
# Todas @admin_required. Acceso a Supabase via service_role (bypassa RLS). Las
# lecturas degradan a env si la DB falla; las escrituras devuelven 500 con el
# error real (la tabla puede no existir todavía → el admin la crea con la migración).

_ADMIN_PROFILE_COLS = "id, email, plan, credits_cents, is_admin, monthly_usage, free_lifetime_uses, created_at"


@app.route("/admin/api/plans", methods=["GET"])
@admin_required
def api_admin_plans():
    """Matriz de planes (DB con fallback env). {"plans":[...], "source":"db"|"env"}."""
    return jsonify(load_plans_config())


@app.route("/admin/api/plans/<plan_key>", methods=["POST"])
@admin_required
def api_admin_update_plan(plan_key: str):
    """Crea/actualiza un plan en la tabla `plans` (upsert por key)."""
    try:
        body = request.get_json() or {}
        row = {
            "key": plan_key,
            "name": body.get("name", plan_key),
            "price_month_cents": body.get("price_month_cents"),
            "price_year_cents":  body.get("price_year_cents"),
            "monthly_credits":   body.get("monthly_credits", 0),
            "stripe_price_month": body.get("stripe_price_month"),
            "stripe_price_year":  body.get("stripe_price_year"),
            "active": bool(body.get("active", True)),
            "sort_order": body.get("sort_order", 0),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        db.table("plans").upsert(row).execute()
        invalidate_config_cache()
        return jsonify({"ok": True, "plan": row})
    except Exception as e:
        logger.error(f"api_admin_update_plan({plan_key}): {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/topups", methods=["GET"])
@admin_required
def api_admin_topups():
    """Topups (DB con fallback env). {"topups":[...], "source":"db"|"env"}."""
    return jsonify(load_topups_config())


@app.route("/admin/api/topups/<topup_key>", methods=["POST"])
@admin_required
def api_admin_update_topup(topup_key: str):
    """Crea/actualiza un topup en la tabla `topups` (upsert por key)."""
    try:
        body = request.get_json() or {}
        row = {
            "key": topup_key,
            "credits": body.get("credits", 0),
            "price_cents": body.get("price_cents", 0),
            "stripe_price_id": body.get("stripe_price_id"),
            "active": bool(body.get("active", True)),
            "sort_order": body.get("sort_order", 0),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        db.table("topups").upsert(row).execute()
        invalidate_config_cache()
        return jsonify({"ok": True, "topup": row})
    except Exception as e:
        logger.error(f"api_admin_update_topup({topup_key}): {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/users", methods=["GET"])
@admin_required
def api_admin_users():
    """Listado/búsqueda de usuarios con paginación.
    q: filtra por id (UUID exacto) o email (substring, filtrado server-side).
    """
    q = (request.args.get("q", "") or "").strip().lower()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        limit = min(max(1, int(request.args.get("limit", 20))), 100)
    except (TypeError, ValueError):
        limit = 20
    offset = (page - 1) * limit

    try:
        base = db.table("profiles").select(_ADMIN_PROFILE_COLS)
        if q and len(q) == 36 and q.count("-") == 4:
            # Parece UUID → filtro exacto por id (parametrizado por el cliente).
            rows = base.eq("id", q).execute().data or []
        else:
            rows = base.execute().data or []
            if q:
                rows = [u for u in rows if q in ((u.get("email") or "").lower())]
        total = len(rows)
        users_page = rows[offset:offset + limit]
        return jsonify({
            "users": users_page,
            "total": total,
            "page": page,
            "limit": limit,
        })
    except Exception as e:
        logger.error(f"api_admin_users: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/users/<user_id>", methods=["POST"])
@admin_required
def api_admin_update_user(user_id: str):
    """Aplica cambios selectivos: plan, credits_delta (suma a credits_cents), is_admin.
    No resetea créditos mensuales (admin manual, no llama grant_monthly_allowance).
    """
    try:
        body = request.get_json() or {}
        updates = {}
        if "plan" in body and body["plan"]:
            updates["plan"] = body["plan"]
        if "is_admin" in body:
            updates["is_admin"] = bool(body["is_admin"])
        if "credits_delta" in body:
            try:
                delta = int(body["credits_delta"])
            except (TypeError, ValueError):
                delta = 0
            if delta:
                prof = (db.table("profiles").select("credits_cents")
                          .eq("id", user_id).single().execute())
                current = (prof.data or {}).get("credits_cents", 0) or 0
                updates["credits_cents"] = max(0, current + delta)

        if updates:
            db.table("profiles").update(updates).eq("id", user_id).execute()

        updated = (db.table("profiles").select(_ADMIN_PROFILE_COLS)
                     .eq("id", user_id).single().execute())
        return jsonify({"ok": True, "user": updated.data})
    except Exception as e:
        logger.error(f"api_admin_update_user({user_id}): {e}")
        return jsonify({"error": str(e)}), 500


# ── Feedback: revisión + recompensa en créditos ─────────────────────────────
# Recompensa por defecto al APROBAR (el admin puede sobreescribir con body.credits):
#   bug confirmado = 25 cr · idea implementada = 10 cr. 1 crédito = COST_CENTS de saldo.
FEEDBACK_REWARD = {"bug": 25, "idea": 10}
_FEEDBACK_LIST_COLS = ("id,user_id,type,text,page,plan,status,credits_awarded,"
                       "admin_note,resolved_at,created_at")


@app.route("/admin/api/feedback", methods=["GET"])
@admin_required
def api_admin_feedback_list():
    """Cola de feedback. ?status=new|confirmed|implemented|rejected|all (def: new).
    No devuelve image_b64 (pesa); usar el detalle para verla."""
    status = (request.args.get("status", "new") or "new").strip().lower()
    try:
        limit = min(max(1, int(request.args.get("limit", 50))), 200)
    except (TypeError, ValueError):
        limit = 50
    try:
        q = (db.table("feedback").select(_FEEDBACK_LIST_COLS)
               .order("created_at", desc=True).limit(limit))
        if status != "all":
            q = q.eq("status", status)
        rows = q.execute().data or []
        return jsonify({"feedback": rows, "count": len(rows)})
    except Exception as e:
        logger.error(f"api_admin_feedback_list: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/feedback/<fid>", methods=["GET"])
@admin_required
def api_admin_feedback_detail(fid: str):
    """Detalle completo de un feedback (incluye image_b64 para revisarlo)."""
    try:
        row = (db.table("feedback").select("*").eq("id", fid).single().execute())
        return jsonify({"feedback": row.data})
    except Exception as e:
        logger.error(f"api_admin_feedback_detail({fid}): {e}")
        return jsonify({"error": "not_found"}), 404


@app.route("/admin/api/feedback/<fid>/resolve", methods=["POST"])
@admin_required
def api_admin_feedback_resolve(fid: str):
    """Resuelve un feedback. body.status ∈ confirmed|implemented|rejected.
    Al aprobar (confirmed/implemented) abona créditos al autor sumando a
    profiles.credits_cents (default por tipo; override con body.credits).
    IDEMPOTENTE: si ya se abonó (credits_awarded>0) no vuelve a pagar."""
    admin = current_user()
    body = request.get_json(silent=True) or {}
    new_status = str(body.get("status") or "").strip().lower()
    if new_status not in ("confirmed", "implemented", "rejected"):
        return jsonify({"ok": False, "error": "bad_status"}), 400
    note = (str(body.get("note") or "").strip()[:1000]) or None
    try:
        fb = (db.table("feedback")
                .select("id,user_id,type,status,credits_awarded")
                .eq("id", fid).single().execute()).data
        if not fb:
            return jsonify({"ok": False, "error": "not_found"}), 404

        # Cuántos créditos abonar (solo si se aprueba). Override explícito o default por tipo.
        award = 0
        if new_status in ("confirmed", "implemented"):
            if body.get("credits") is not None:
                try:
                    award = max(0, int(body["credits"]))
                except (TypeError, ValueError):
                    award = 0
            else:
                award = FEEDBACK_REWARD.get(fb.get("type") or "bug", 0)

        # Abona una sola vez (idempotencia: no si ya hay créditos abonados).
        granted = 0
        if award and (fb.get("credits_awarded") or 0) == 0:
            prof = (db.table("profiles").select("credits_cents")
                      .eq("id", fb["user_id"]).single().execute())
            current = (prof.data or {}).get("credits_cents", 0) or 0
            db.table("profiles").update(
                {"credits_cents": current + award * COST_CENTS}
            ).eq("id", fb["user_id"]).execute()
            granted = award

        db.table("feedback").update({
            "status": new_status,
            "credits_awarded": (fb.get("credits_awarded") or 0) + granted,
            "admin_note": note,
            "resolved_by": admin["id"],
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", fid).execute()

        try:
            track_event("feedback_resolved", admin["id"],
                        {"feedback_id": fid, "status": new_status, "credits": granted})
        except Exception:
            pass
        return jsonify({"ok": True, "status": new_status, "credits_granted": granted})
    except Exception as e:
        logger.error(f"api_admin_feedback_resolve({fid}): {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/settings", methods=["GET"])
@admin_required
def api_admin_settings():
    """Lee ajustes globales (cost_cents) con fallback a env COST_CENTS."""
    try:
        cost_cents = get_cost_cents()
        raw = get_setting("cost_cents", None)
        return jsonify({
            "cost_cents": cost_cents,
            "cost_cents_source": "db" if raw is not None else "env",
        })
    except Exception as e:
        logger.error(f"api_admin_settings: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/settings", methods=["POST"])
@admin_required
def api_admin_update_settings():
    """Actualiza ajustes globales en app_settings (cost_cents)."""
    try:
        body = request.get_json() or {}
        if "cost_cents" in body:
            db.table("app_settings").upsert({
                "key": "cost_cents",
                "value": {"value": int(body["cost_cents"])},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            invalidate_config_cache()
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"api_admin_update_settings: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin")
def admin_page():
    user = current_user()
    if not user:
        return redirect("/")
    if not _is_admin(user):
        abort(403)
    # El panel admin vive como overlay (#adminPage) dentro de index.html y se
    # abre solo cuando location.pathname === "/admin". Cargamos el shell de app.
    return render_template("index.html", lang=_resolve_lang(), workspace=True)


@app.route("/admin/metrics")
@admin_required
def admin_metrics():
    # M1 + M2: usuarios totales y nuevos (últimos 7 días) desde auth.users
    auth_users = db.auth.admin.list_users(page=1, per_page=1000)
    total_users = len(auth_users)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    new_users_7d = 0
    uid_to_email: dict[str, str] = {}
    for u in auth_users:
        uid_to_email[u.id] = u.email or ""
        created = getattr(u, "created_at", None)
        if created:
            try:
                if isinstance(created, str):
                    created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= cutoff:
                    new_users_7d += 1
            except Exception:
                pass

    # M3: desglose de planes (desde profiles)
    profiles_r = db.table("profiles").select("plan").execute()
    plan_counts: dict[str, int] = {}
    for p in (profiles_r.data or []):
        plan = p.get("plan") or "free"
        plan_counts[plan] = plan_counts.get(plan, 0) + 1

    # M4: total de guiones
    scripts_count_r = db.table("scripts").select("id", count="exact").limit(1).execute()
    total_scripts = scripts_count_r.count or 0

    # M5: top 10 usuarios por número de guiones
    scripts_uid_r = db.table("scripts").select("user_id").execute()
    uid_script_counts: dict[str, int] = {}
    for s in (scripts_uid_r.data or []):
        uid = s.get("user_id")
        if uid:
            uid_script_counts[uid] = uid_script_counts.get(uid, 0) + 1
    top_uids = sorted(uid_script_counts, key=lambda k: uid_script_counts[k], reverse=True)[:10]
    top_users = [
        {"email": uid_to_email.get(uid, uid[:8] + "…"), "scripts": uid_script_counts[uid]}
        for uid in top_uids
    ]

    # M6: total de ideas
    ideas_count_r = db.table("ideas").select("id", count="exact").limit(1).execute()
    total_ideas = ideas_count_r.count or 0

    # M7: suscripciones Stripe activas (stripe_subscription_id no nulo)
    subs_r = (
        db.table("profiles")
          .select("stripe_subscription_id")
          .not_.is_("stripe_subscription_id", "null")
          .execute()
    )
    stripe_active = len(subs_r.data or [])

    # ── Transcripciones ───────────────────────────────────────────────────────
    # week_cut = cutoff (ya calculado arriba: now - 7 días)
    week_cut  = cutoff.isoformat()
    month_cut = (
        datetime.now(timezone.utc)
        .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )

    # T1: total / esta semana / este mes
    t1_total = (
        db.table("transcriptions").select("id", count="exact").limit(1).execute().count or 0
    )
    t1_week = (
        db.table("transcriptions").select("id", count="exact")
          .gte("created_at", week_cut).limit(1).execute().count or 0
    )
    t1_month = (
        db.table("transcriptions").select("id", count="exact")
          .gte("created_at", month_cut).limit(1).execute().count or 0
    )

    # T2: media por usuario + top transcriptores
    tr_uid_r = db.table("transcriptions").select("user_id").execute()
    uid_tr_counts: dict[str, int] = {}
    for row in (tr_uid_r.data or []):
        uid = row.get("user_id")
        if uid:
            uid_tr_counts[uid] = uid_tr_counts.get(uid, 0) + 1
    t2_users_with = len(uid_tr_counts)
    t2_avg = round(sum(uid_tr_counts.values()) / t2_users_with, 2) if t2_users_with else 0
    top_tr_uids = sorted(uid_tr_counts, key=lambda k: uid_tr_counts[k], reverse=True)[:10]
    t2_top = [
        {"email": uid_to_email.get(uid, uid[:8] + "…"), "transcriptions": uid_tr_counts[uid]}
        for uid in top_tr_uids
    ]

    # T3: split por plataforma
    t3_ig = (
        db.table("transcriptions").select("id", count="exact")
          .eq("platform", "instagram").limit(1).execute().count or 0
    )
    t3_tt = (
        db.table("transcriptions").select("id", count="exact")
          .eq("platform", "tiktok").limit(1).execute().count or 0
    )
    t3_total = t3_ig + t3_tt or 1  # evitar división por cero
    t3_split = {
        "instagram":     t3_ig,
        "tiktok":        t3_tt,
        "instagram_pct": round(t3_ig / t3_total * 100, 1),
        "tiktok_pct":    round(t3_tt / t3_total * 100, 1),
    }

    # ── Competencia ──────────────────────────────────────────────────────────
    # C1: usuarios distintos que usan la feature Competidores
    ut_r = db.table("user_tracked_creators").select("user_id").execute()
    c1_adopters = len({row["user_id"] for row in (ut_r.data or []) if row.get("user_id")})

    # C2/C3: ranking de competidores (join Python-side)
    utc_r = db.table("user_tracked_creators").select("user_id, creator_id").execute()
    cg_r  = db.table("creators_global").select("id, ig_username, followers_count_cached").execute()
    cg_map = {row["id"]: row for row in (cg_r.data or [])}
    creator_users: dict[str, set] = {}
    for row in (utc_r.data or []):
        cid = row.get("creator_id")
        uid = row.get("user_id")
        if cid and uid:
            creator_users.setdefault(cid, set()).add(uid)
    c2c3 = sorted(
        [
            {
                "ig_username":          cg_map[cid]["ig_username"],
                "user_count":           len(users),
                "followers_count":      cg_map[cid].get("followers_count_cached"),
            }
            for cid, users in creator_users.items()
            if cid in cg_map
        ],
        key=lambda x: x["user_count"],
        reverse=True,
    )

    return jsonify({
        "m1_total_users":    total_users,
        "m2_new_users_7d":   new_users_7d,
        "m3_plan_breakdown": plan_counts,
        "m4_total_scripts":  total_scripts,
        "m5_top_users":      top_users,
        "m6_total_ideas":    total_ideas,
        "m7_stripe_active":  stripe_active,
        # Transcripciones
        "t1_total":          t1_total,
        "t1_week":           t1_week,
        "t1_month":          t1_month,
        "t2_avg_per_user":   t2_avg,
        "t2_users_with":     t2_users_with,
        "t2_top":            t2_top,
        "t3_split":          t3_split,
        # Competencia
        "c1_adopters":       c1_adopters,
        "c2c3_ranking":      c2c3,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  ISLA "SIGNAL" — generación/persistencia en lote para los botones del radar
#  (gen 5 ideas · gen 5 guiones · gen 5 hooks · explosión · llenar semana).
#
#  En la demo (window.__DEMO__) estas acciones son estado local (no llaman aquí).
#  En PROD (!isDemo()) la isla pega contra ESTOS endpoints, que reúsan la lógica
#  probada del workspace legacy: develop_idea(), adapt_with_ai() + voz inyectada
#  (voice_prompt_block via get_voice_profile), persistencia en ideas/scripts y el
#  patrón anti-doble-gasto (acquire_credit_lock + re-check + refund-on-fail).
#
#  UNIDAD DE CRÉDITO: el frontend trata 1 crédito = COST_CENTS (radar-loop.js:1393
#  → S.user.credits = credits_cents / COST_CENTS). El COST map de la isla está en
#  CRÉDITOS: {idea5:1, scripts5:5, hooks5:1, explosion:30} y fillweek = nº de reels.
#  Estos endpoints cobran EXACTAMENTE esos créditos × COST_CENTS para que el
#  flashSpark(-COST.x) cosmético del frontend cuadre con el descuento server-side.
#  Backend = fuente de verdad: se cobra ANTES del LLM bajo lock y se refunda si falla.
# ══════════════════════════════════════════════════════════════════════════════

def _charge_units_locked(uid, units, user):
    """Cobro server-side anti-doble-gasto de `units` créditos (1 crédito =
    COST_CENTS). Adquiere el lock por-usuario, re-lee el perfil bajo lock y
    descuenta según el modo del usuario:
      - paid REAL  → suma `units` a monthly_usage (sin tocar credits_cents).
      - free       → consume free_lifetime (de 1 en 1, hasta agotar) y el resto
                     contra credits_cents (topups). Si no llega → corta.

    Devuelve una tupla (err_response, refund_fn, charged_state):
      - err_response: jsonify(...) con código si NO se pudo cobrar (o None si OK).
      - refund_fn:    callable best-effort que revierte el cargo (no-op si err).
      - charged_state: dict con {'mode','free_used','credits_charged'} para logging.
    El llamador hace: err, refund, _ = _charge_units_locked(...); if err: return err
    y luego refund() en cualquier rama de fallo del LLM.
    """
    cost_per_unit = get_cost_cents()
    profile = get_profile(uid)
    is_paid_unlimited = paid_features_active(profile, user)
    is_admin = (user or {}).get("email", "").lower() in UNLIMITED_EMAILS

    if is_admin:
        # Cuentas admin/cortesía: sin coste ni contador.
        return None, (lambda: None), {"mode": "admin", "free_used": 0, "credits_charged": 0}

    total_cost = max(0, int(units)) * cost_per_unit
    _lock = acquire_credit_lock(uid)
    if _lock is None:
        # Contención same-user (otra operación en curso) → 429, como el path legacy (no serializar a ciegas).
        return (jsonify({"error": "busy"}), 429), (lambda: None), {"mode": "busy"}
    try:
        fresh = get_profile(uid)
        if is_paid_unlimited:
            db.table("profiles").update({
                "monthly_usage": (fresh.get("monthly_usage") or 0) + int(units)
            }).eq("id", uid).execute()
            state = {"mode": "paid", "free_used": 0, "credits_charged": 0, "units": int(units)}
        else:
            # Moneda única (modelo 2026-06-23, sin trial ni free_lifetime): TODO cuesta
            # créditos. free_used queda en 0 (el bloque de free_lifetime de abajo no corre).
            free_used = 0
            credits_needed = int(units) * cost_per_unit
            if (fresh.get("credits_cents") or 0) < credits_needed:
                release_credit_lock(uid, _lock)
                return (jsonify({
                    "error": "no_credits",
                    "message": "Necesitas más créditos para esta acción. "
                               "Sube a Creador o recarga créditos.",
                }), 402), (lambda: None), {}
            updates = {}
            if free_used:
                # reverse-trial: reset mensual si el boundary ya pasó, antes de sumar.
                now = datetime.now(timezone.utc)
                reset_dt = _parse_ts(fresh.get("free_month_reset_at"))
                if reset_dt is None or now >= reset_dt:
                    updates["free_analysis_uses"] = 0
                    updates["free_month_reset_at"] = _next_month_boundary(now).isoformat()
                    base = 0
                else:
                    base = fresh.get("free_lifetime_uses") or 0
                updates["free_lifetime_uses"] = base + free_used
            if credits_needed:
                updates["credits_cents"] = (fresh.get("credits_cents") or 0) - credits_needed
            if updates:
                db.table("profiles").update(updates).eq("id", uid).execute()
            state = {"mode": "free", "free_used": free_used,
                     "credits_charged": credits_needed, "units": int(units)}
    except Exception as e:
        logger.error("_charge_units_locked: pre-charge failed user=%s err=%s", uid, e, exc_info=True)
        release_credit_lock(uid, _lock)
        return (jsonify({"error": "internal", "message": "Inténtalo de nuevo."}), 500), (lambda: None), {}
    finally:
        release_credit_lock(uid, _lock)

    def _refund():
        """Revertir el cargo aplicado arriba. Best-effort, bajo lock."""
        _rl = acquire_credit_lock(uid)
        try:
            cur = get_profile(uid)
            if state.get("mode") == "paid":
                db.table("profiles").update({
                    "monthly_usage": max(0, (cur.get("monthly_usage") or 0) - state.get("units", 0))
                }).eq("id", uid).execute()
            elif state.get("mode") == "free":
                upd = {}
                if state.get("free_used"):
                    upd["free_lifetime_uses"] = max(0, (cur.get("free_lifetime_uses") or 0) - state["free_used"])
                if state.get("credits_charged"):
                    upd["credits_cents"] = (cur.get("credits_cents") or 0) + state["credits_charged"]
                if upd:
                    db.table("profiles").update(upd).eq("id", uid).execute()
        except Exception as e:
            logger.error("_charge_units_locked refund failed user=%s err=%s", uid, e)
        finally:
            release_credit_lock(uid, _rl)

    return None, _refund, state


def _credits_display(uid):
    """Créditos que debe mostrar la pill del radar tras una operación
    (= credits_available: restante mensual + topups). El frontend lo lee como
    `credits_cents`/COST_CENTS, así que devolvemos ambos coherentes."""
    prof = get_profile(uid)
    return {
        "credits_cents": prof.get("credits_cents", 0) or 0,
        "credits": credits_available(prof),
    }


def _flatten_script_result(result):
    """Aplana el dict del LLM (hook/body/closing | hooks[]) a string plano + saca
    el título si lo trae. Mismo contrato que idea_to_script/generate_script."""
    llm_title = ""
    if isinstance(result, dict) and result.get("title"):
        llm_title = str(result["title"]).strip()[:80]
    if isinstance(result, dict) and "hook" in result:
        flat = (result["hook"] + "\n" +
                "\n".join(result.get("body", [])) + "\n" +
                result.get("closing", ""))
        return flat.strip(), llm_title
    if isinstance(result, dict) and isinstance(result.get("hooks"), list):
        return ("\n".join(h.get("text", "") for h in result["hooks"]
                          if isinstance(h, dict) and h.get("text")).strip(), llm_title)
    if not isinstance(result, str):
        return str(result), llm_title
    return result, llm_title


def _uniquify_title(title, seen):
    """(6) Anti 'guiones duplicados': el LLM repite título entre variaciones de
    la MISMA idea (5 ángulos del batch → 2× «Éxito no se persigue» con contenido
    distinto) y en la lista de Guiones parecen duplicados. 'X' → 'X · v2' → 'X · v3'.
    `seen` es un set lowercase que se muta (sembrar con los títulos ya en DB)."""
    base = (title or "").strip()
    if not base:
        return title
    if base.lower() not in seen:
        seen.add(base.lower())
        return base
    n = 2
    while f"{base.lower()} · v{n}" in seen:
        n += 1
    seen.add(f"{base.lower()} · v{n}")
    return f"{base} · v{n}"


def _existing_idea_titles(uid, idea_id):
    """Títulos ya persistidos para una idea (siembra de _uniquify_title)."""
    try:
        ex = db.table("scripts").select("title").eq("user_id", uid).eq("idea_id", idea_id).execute()
        return {(r.get("title") or "").strip().lower() for r in (ex.data or []) if r.get("title")}
    except Exception:
        return set()


@app.route("/ideas/generate-batch", methods=["POST"])
@require_auth
@limiter.limit("5 per minute;30 per hour")
def ideas_generate_batch():
    """Isla Signal — botón «Generar 3 ideas». Genera y persiste un lote de ideas
    desarrolladas (develop_idea x N; el frontend manda count=3). Cobra 10 créditos
    por el lote (COST.idea5=5) ANTES del LLM y refunda si NINGUNA idea sale.
    Response: {ideas:[{id,raw_text,title,category,status}], cost_cents, credits_cents, credits}."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    count = max(1, min(int(body.get("count") or 5), 5))
    language = body.get("language", "es")
    project_id = body.get("project_id") or None
    assistant_id = body.get("assistant_id") or None

    if not assistant_id:
        prof = db.table("profiles").select("default_idea_assistant").eq("id", uid).execute()
        if prof.data and prof.data[0].get("default_idea_assistant"):
            assistant_id = prof.data[0]["default_idea_assistant"]

    # Coste del lote: COST.idea5 = 5 créditos (no por idea). Frontend genera 3 ideas.
    err, refund, _ = _charge_units_locked(uid, 5, user)
    if err:
        return err

    # Semillas: variamos el prompt para que las 5 ideas no sean clones.
    seeds = [
        "Dame un ángulo nuevo y específico para un reel sobre tu nicho, evita lo obvio.",
        "Una idea contraintuitiva o que rompa una creencia común de tu audiencia.",
        "Una idea basada en un error típico que comete tu audiencia y cómo evitarlo.",
        "Una idea tipo 'cómo conseguir X sin Y' para tu nicho.",
        "Una idea con gancho de historia personal o caso real (sin inventar datos).",
    ]
    ideas_out = []
    for i in range(count):
        raw_seed = seeds[i % len(seeds)]
        try:
            result = develop_idea(raw_seed, assistant_id, uid, language)
        except Exception as e:
            logger.warning("ideas_generate_batch: develop failed user=%s i=%s err=%s", uid, i, e)
            continue
        try:
            row = db.table("ideas").insert({
                "user_id": uid,
                "project_id": project_id,
                "raw_text": result.get("title") or raw_seed,
                "assistant_id": assistant_id,
                "title": result.get("title"),
                "category": result.get("category"),
                "script_draft": result.get("script_draft"),
                "status": "developed",
            }).execute()
            if row.data:
                r = row.data[0]
                ideas_out.append({
                    "id": r.get("id"),
                    "raw_text": r.get("raw_text"),
                    "title": r.get("title"),
                    "category": r.get("category"),
                    "status": "developed",
                })
        except Exception as e:
            logger.error("ideas_generate_batch: insert failed user=%s err=%s", uid, e, exc_info=True)

    if not ideas_out:
        refund()
        return jsonify({"error": "llm_error",
                        "message": "No se pudieron generar ideas. Inténtalo de nuevo."}), 502

    return jsonify({"ideas": ideas_out, "cost_cents": get_cost_cents(), **_credits_display(uid)})


@app.route("/ideas/<idea_id>/scripts/generate-batch", methods=["POST"])
@require_auth
@limiter.limit("5 per minute;30 per hour")
def idea_scripts_generate_batch(idea_id):
    """Isla Signal — botón «5 guiones» sobre una idea. Genera N guiones con
    variaciones (adapt_with_ai x N, voz del usuario inyectada) y los persiste en
    `scripts` (idea_id link). Cobra 5 créditos (COST.scripts5=5) ANTES del LLM y
    refunda si NINGÚN guion sale. Response: {scripts:[{id,title,script,assistant_name}],
    cost_cents, credits_cents, credits}."""
    user = current_user()
    uid = user["id"]
    row = db.table("ideas").select("*").eq("id", idea_id).eq("user_id", uid).execute()
    if not row.data:
        return jsonify({"error": "Not found"}), 404
    idea = row.data[0]
    body = request.get_json(silent=True) or {}
    count = max(1, min(int(body.get("count") or 5), 5))

    # Resolver asistente/estilo (mismo orden que idea_to_script).
    assistant_id = body.get("assistant_id") or idea.get("assistant_id")
    profile = get_profile(uid)
    if not assistant_id and profile.get("default_idea_assistant"):
        assistant_id = profile["default_idea_assistant"]

    style_arg, custom_prompt, style_label = "viral", "", "viral"
    if assistant_id in _BUILTIN_SCRIPT_STYLES:
        style_arg = style_label = assistant_id
    elif assistant_id:
        try:
            asst_r = db.table("assistants").select("name, instructions").eq(
                "id", assistant_id).eq("user_id", uid).execute()
            if asst_r.data and asst_r.data[0].get("instructions"):
                style_arg = "custom"
                custom_prompt = asst_r.data[0]["instructions"]
                style_label = asst_r.data[0].get("name") or "custom"
        except Exception:
            pass
    if _custom_too_short(style_arg, custom_prompt):
        return _assistant_too_short_response(style_label)

    # "hooks" produce solo hooks sueltos (5 one-liners), no guiones completos.
    # Degradar a viral para que el batch genere scripts estructurados.
    if style_arg == "hooks":
        style_arg = style_label = "viral"

    # Coste econ: lote de 5 guiones desde una idea = 12 cr en bloque (vs 15 sueltos),
    # mismo precio que "Llena mi semana".
    err, refund, _ = _charge_units_locked(uid, FILL_WEEK_UNITS, user)
    if err:
        return err

    title = idea.get("title") or ""
    category = idea.get("category") or ""
    raw_text = idea.get("raw_text") or ""
    # Si la idea viene de gen5ideas, raw_text es solo el título (thin). El
    # script_draft (intro/desarrollo/cierre generado por develop_idea) da contexto
    # rico para que el LLM genere guiones completos en lugar de expandir un título.
    _sd = idea.get("script_draft")
    if isinstance(_sd, dict):
        _sd_intro = _sd.get("intro") or ""
        _sd_dev = _sd.get("desarrollo") or ""
        _sd_cierre = _sd.get("cierre") or ""
        _draft_block = (
            f"\n\n[Borrador de desarrollo]\nIntro: {_sd_intro}\n"
            f"Desarrollo: {_sd_dev}\nCierre: {_sd_cierre}"
        ) if (_sd_intro or _sd_dev or _sd_cierre) else ""
    else:
        _draft_block = ""
    # Ángulos para que los 5 guiones no salgan idénticos.
    angles = [
        "Enfoque directo y práctico, paso a paso.",
        "Enfoque de historia: arranca con una anécdota o caso concreto.",
        "Enfoque contraintuitivo: ataca una creencia común desde el hook.",
        "Enfoque lista: estructura el body como pasos numerados claros.",
        "Enfoque emocional: conecta con la frustración o el deseo de la audiencia.",
    ]
    voice = get_voice_profile(uid)

    def _build_user_content(i):
        return (
            f"[Idea original del usuario]\n{raw_text}\n\n"
            f"[Título]\n{title}\n\n"
            f"[Categoría]\n{category or '—'}{_draft_block}\n\n"
            f"[Ángulo para ESTA variación]\n{angles[i % len(angles)]}\n\n"
            f"Tarea: convierte esto en un guion completo de 30-45 segundos hablados "
            f"para un reel de Instagram, siguiendo el ángulo indicado. Tu output debe tener "
            f"desarrollo real, ejemplos concretos (sin inventar datos numéricos), profundidad y ritmo. "
            f"Total: 100-140 palabras, mínimo 8 frases en body. "
            f"Incluye al menos 1 ejemplo concreto o anécdota dentro del desarrollo."
        )

    def _gen_variation(i):
        try:
            return adapt_with_ai(_build_user_content(i), style_arg, custom_prompt, voice=voice, user_id=uid)
        except Exception as e:
            logger.warning("idea_scripts_batch: LLM failed user=%s idea=%s i=%s err=%s", uid, idea_id, i, e)
            return None

    # v0.19-fix: N llamadas al LLM EN PARALELO (worker gevent) en vez de en serie
    # -> el request deja de colgarse (5 guiones tardan ~= 1 llamada, no 5).
    import gevent
    _jobs = [gevent.spawn(_gen_variation, i) for i in range(count)]
    gevent.joinall(_jobs, timeout=120)

    scripts_out = []
    _seen_titles = _existing_idea_titles(uid, idea_id)
    for i in range(count):
        result = _jobs[i].value
        if not result:
            continue
        flat, llm_title = _flatten_script_result(result)
        if not flat:
            continue
        today = datetime.now(timezone.utc).strftime("%d %b %Y").lower()
        script_title = _uniquify_title(llm_title or f"Guión {i+1} · {style_label} · {today}", _seen_titles)
        asst_name = _resolve_assistant_name(
            {"assistant_id": assistant_id, "style": style_label}, uid, db)
        try:
            ins = db.table("scripts").insert({
                "user_id": uid,
                "idea_id": idea_id,
                "title": script_title,
                "script": flat,
                "project_id": idea.get("project_id"),
                "assistant_name": asst_name,
            }).execute()
            sid = ins.data[0].get("id") if ins.data else None
        except Exception as e:
            logger.error("idea_scripts_batch: insert failed user=%s err=%s", uid, e, exc_info=True)
            sid = None
        scripts_out.append({
            "id": sid, "title": script_title, "script": flat,
            "assistant_name": asst_name,
        })

    if not scripts_out:
        refund()
        return jsonify({"error": "llm_error",
                        "message": "No se pudieron generar guiones. Inténtalo de nuevo."}), 502

    try:
        db.table("ideas").update({
            "status": "scripted",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", idea_id).eq("user_id", uid).execute()
    except Exception:
        pass

    return jsonify({"scripts": scripts_out, "cost_cents": 5 * get_cost_cents(), **_credits_display(uid)})


@app.route("/scripts/<script_id>/hooks/generate-batch", methods=["POST"])
@require_auth
@limiter.limit("10 per minute;40 per hour")
def script_hooks_generate_batch(script_id):
    """Isla Signal — botón «5 hooks» sobre un guion. Regenera N hooks alternativos
    (mismo prompt que /transform-hook, temp=0.9) y los persiste en scripts.alt_hooks
    (JSONB). Cobra 1 crédito (COST.hooks5=1) ANTES del LLM y refunda si ninguno sale.
    Response: {hooks:[str], alt_hooks:[str], cost_cents, credits_cents, credits}."""
    user = current_user()
    uid = user["id"]
    row = db.table("scripts").select("*").eq("id", script_id).eq("user_id", uid).execute()
    if not row.data:
        return jsonify({"error": "Not found"}), 404
    script = row.data[0]
    body = request.get_json(silent=True) or {}
    count = max(1, min(int(body.get("count") or 5), 5))

    # Contexto del guion para el regenerador de hooks (body + closing).
    full = (script.get("script") or "").strip()
    lines = [l.strip() for l in full.split("\n") if l.strip()]
    ctx_body = lines[1:] if len(lines) > 1 else lines
    context = "\n".join(ctx_body)
    original_text = script.get("hook") or (lines[0] if lines else "") or full
    user_msg = f"Guión actual:\n{context}\n\nTexto original del que salió:\n{original_text}"

    # Coste econ: gratis los primeros HOOKS_FREE_PER_DAY/día, luego HOOKS_EXTRA_UNITS.
    _hk_units = _hooks_extra_units(uid)
    err, refund, _ = _charge_units_locked(uid, _hk_units, user)
    if err:
        return err

    new_hooks = []
    for _ in range(count):
        try:
            raw = _call_llm(_HOOK_REGEN_PROMPT, user_msg, temperature=0.9)
            h = _extract_hook(raw)
            if h and h not in new_hooks:
                new_hooks.append(h)
        except Exception as e:
            logger.warning("script_hooks_batch: hook regen failed user=%s script=%s err=%s", uid, script_id, e)
            continue

    if not new_hooks:
        refund()
        return jsonify({"error": "llm_error",
                        "message": "No se pudieron generar hooks. Inténtalo de nuevo."}), 502
    _hooks_mark_used(uid)   # solo avanza el cupo diario tras entregar

    # Persistir en alt_hooks (acumula sobre los existentes).
    existing = script.get("alt_hooks")
    if not isinstance(existing, list):
        existing = []
    merged = existing + [h for h in new_hooks if h not in existing]
    try:
        db.table("scripts").update({"alt_hooks": merged}).eq("id", script_id).eq("user_id", uid).execute()
    except Exception as e:
        logger.error("script_hooks_batch: alt_hooks persist failed user=%s err=%s", uid, e, exc_info=True)

    return jsonify({"hooks": new_hooks, "alt_hooks": merged,
                    "cost_cents": get_cost_cents(), **_credits_display(uid)})


@app.route("/api/hooks/from-source", methods=["POST"])
@require_auth
@limiter.limit("10 per minute;40 per hour")
def hooks_from_source():
    """5 HOOKS DESDE 3 FUENTES (Fathom 18/06, David — «el gancho es el 80/20»):
      (a) source="competitor" → un reel de la competencia (reel_id o texto/caption),
      (b) source="idea"       → una idea tuya en texto libre,
      (c) source="script"     → un guión que ya tienes (script_id).
    Reusa el mismo regenerador de hooks (_HOOK_REGEN_PROMPT) y cobra 1 crédito
    (COST.hooks5=1) antes del LLM, refunda si ninguno sale. NO persiste (no hay
    script destino salvo en (c), que ya tiene su propio botón); devuelve los hooks.
    Response: {hooks:[str], source, cost_cents, credits_cents, credits}."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "idea").strip().lower()
    count = max(1, min(int(body.get("count") or 5), 5))

    context = ""
    original_text = ""
    label = ""
    if source == "script":
        sid = (body.get("script_id") or "").strip()
        row = db.table("scripts").select("script, hook").eq("id", sid).eq("user_id", uid).execute()
        if not row.data:
            return jsonify({"error": "not_found"}), 404
        s = row.data[0]
        full = (s.get("script") or "").strip()
        lines = [l.strip() for l in full.split("\n") if l.strip()]
        context = "\n".join(lines[1:] if len(lines) > 1 else lines)
        original_text = s.get("hook") or (lines[0] if lines else "") or full
        label = "guión"
    elif source == "competitor":
        rid = (body.get("reel_id") or "").strip()
        cap = (body.get("text") or "").strip()
        if rid:
            try:
                rr = (db.table("creator_reels_global")
                        .select("caption, transcript")
                        .eq("id", rid).eq("is_archived", False).single().execute())
                if rr.data:
                    cap = (rr.data.get("transcript") or rr.data.get("caption") or cap).strip()
            except Exception:
                logger.warning("hooks_from_source: reel lookup failed uid=%s reel=%s", uid, rid)
        if not cap:
            return jsonify({"error": "missing_source",
                            "message": "No hay contenido del reel para sacar hooks."}), 400
        context = cap[:2000]
        original_text = cap[:300]
        label = "vídeo de competencia"
    else:  # idea (texto libre)
        txt = (body.get("text") or "").strip()
        if len(txt) < 4:
            return jsonify({"error": "missing_source",
                            "message": "Escribe tu idea para sacar los hooks."}), 400
        context = txt[:2000]
        original_text = txt[:300]
        label = "idea"

    user_msg = (f"Material fuente ({label}):\n{context}\n\n"
                f"Texto base del que parte el hook:\n{original_text}")

    # Coste econ: gratis los primeros HOOKS_FREE_PER_DAY/día, luego HOOKS_EXTRA_UNITS.
    _hk_units = _hooks_extra_units(uid)
    err, refund, _ = _charge_units_locked(uid, _hk_units, user)
    if err:
        return err

    new_hooks = []
    for _ in range(count):
        try:
            raw = _call_llm(_HOOK_REGEN_PROMPT, user_msg, temperature=0.9)
            h = _extract_hook(raw)
            if h and h not in new_hooks:
                new_hooks.append(h)
        except Exception as e:
            logger.warning("hooks_from_source: regen failed user=%s src=%s err=%s", uid, source, e)
            continue

    if not new_hooks:
        refund()
        return jsonify({"error": "llm_error",
                        "message": "No se pudieron generar hooks. Inténtalo de nuevo."}), 502
    _hooks_mark_used(uid)   # solo avanza el cupo diario tras entregar

    try:
        track_event("hooks_from_source", uid, {"source": source, "n": len(new_hooks)})
    except Exception:
        pass
    return jsonify({"hooks": new_hooks, "source": source,
                    "cost_cents": get_cost_cents(), **_credits_display(uid)})


@app.route("/reels/steal-batch", methods=["POST"])
@require_auth
@limiter.limit("3 per minute;15 per hour")
def reels_steal_batch():
    """Isla Signal — botón «Llena mi semana». Convierte un lote de reels (los más
    explosivos del feed, creator_reels_global) en guiones con la voz del usuario y
    los persiste en `scripts`. Cobra 1 crédito por reel generado (fillweek cobra
    nº de reels) ANTES del LLM y refunda los que fallen.
    Request: {reel_ids:[id,...]} o {count:N} (se resuelven N reels del feed trackeado).
    Response: {scripts:[{id,hook,beats,close,from,script_id}], cost_cents, credits_cents, credits}."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    reel_ids = body.get("reel_ids") or []
    count = max(1, min(int(body.get("count") or 5), 5))

    # Si no pasan ids, resolvemos los N reels más explosivos que el usuario sigue
    # (mismo origen que feedReels() en el frontend).
    if not reel_ids:
        try:
            tracked = (db.table("user_tracked_creators")
                         .select("creator_id")
                         .eq("user_id", uid)
                         .is_("archived_at", "null")
                         .execute())
            cids = [t["creator_id"] for t in (tracked.data or []) if t.get("creator_id")]
            if cids:
                rr = (db.table("creator_reels_global")
                        .select("id, explosion_score")
                        .in_("creator_id", cids)
                        .eq("is_archived", False)
                        .order("explosion_score", desc=True)
                        .limit(count)
                        .execute())
                reel_ids = [r["id"] for r in (rr.data or [])]
        except Exception as e:
            logger.warning("reels_steal_batch: feed resolve failed user=%s err=%s", uid, e)
    reel_ids = reel_ids[:count]
    if not reel_ids:
        return jsonify({"error": "no_reels",
                        "message": "No hay reels en tu radar para esta acción."}), 400

    # Cargar reels + ownership (un solo lote).
    voice = get_voice_profile(uid)
    assistant_id = body.get("assistant_id") or get_profile(uid).get("default_idea_assistant") or None
    style_arg, custom_prompt, style_label = "viral", "", "viral"
    if assistant_id in _BUILTIN_SCRIPT_STYLES:
        style_arg = style_label = assistant_id
    elif assistant_id:
        try:
            asst_r = db.table("assistants").select("name, instructions").eq(
                "id", assistant_id).eq("user_id", uid).execute()
            if asst_r.data and asst_r.data[0].get("instructions"):
                style_arg, custom_prompt = "custom", asst_r.data[0]["instructions"]
                style_label = asst_r.data[0].get("name") or "custom"
        except Exception:
            pass
    if _custom_too_short(style_arg, custom_prompt):
        return _assistant_too_short_response(style_label)

    # Reels válidos (que el usuario realmente sigue).
    valid = []
    for rid in reel_ids:
        try:
            reel_r = (db.table("creator_reels_global")
                        .select("id, caption, transcript, "
                                "creator:creators_global(id, ig_username)")
                        .eq("id", rid).eq("is_archived", False).single().execute())
            reel = reel_r.data
        except Exception:
            reel = None
        if not reel or not reel.get("creator"):
            continue
        own = (db.table("user_tracked_creators").select("id")
                 .eq("user_id", uid).eq("creator_id", reel["creator"]["id"])
                 .is_("archived_at", "null").limit(1).execute())
        if own.data:
            valid.append(reel)
    if not valid:
        return jsonify({"error": "no_reels",
                        "message": "No hay reels válidos en tu radar para esta acción."}), 400

    # Cobro econ: "Llena mi semana" = 12 cr en bloque (vs 15 sueltos). Para semanas
    # parciales nunca cobra más que el suelto (3/reel): min(12, n×3).
    week_units = min(FILL_WEEK_UNITS, len(valid) * SCRIPT_UNITS)
    err, refund, state = _charge_units_locked(uid, week_units, user)
    if err:
        return err

    scripts_out, failures = [], 0
    for reel in valid:
        ig_username = reel["creator"].get("ig_username") or ""
        source = (reel.get("transcript") or reel.get("caption") or "").strip()
        if not source:
            failures += 1
            continue
        user_content = (
            f"[Reel de @{ig_username} que queremos versionar con MI voz]\n{source}\n\n"
            f"Tarea: escribe un guion original de 30-45s para un reel de Instagram "
            f"inspirado en el tema/ángulo del anterior, NO una copia. Output con "
            f"desarrollo real y ritmo. 100-140 palabras, mínimo 8 frases en body."
        )
        try:
            result = adapt_with_ai(user_content, style_arg, custom_prompt, voice=voice, user_id=uid)
        except Exception as e:
            logger.warning("reels_steal_batch: LLM failed user=%s reel=%s err=%s", uid, reel["id"], e)
            failures += 1
            continue
        flat, llm_title = _flatten_script_result(result)
        if not flat:
            failures += 1
            continue
        today = datetime.now(timezone.utc).strftime("%d %b %Y").lower()
        script_title = llm_title or f"Guion desde @{ig_username} · {today}"
        sid = None
        try:
            ins = db.table("scripts").insert({
                "user_id": uid,
                "title": script_title,
                "script": flat,
                "from_competitor_reel_id": reel["id"],
                "from_competitor_username": ig_username,
                "assistant_name": _resolve_assistant_name(
                    {"assistant_id": assistant_id, "style": style_label}, uid, db),
            }).execute()
            sid = ins.data[0].get("id") if ins.data else None
        except Exception as e:
            logger.error("reels_steal_batch: insert failed user=%s err=%s", uid, e, exc_info=True)
        parts = flat.split("\n")
        scripts_out.append({
            "id": sid, "script_id": sid,
            "hook": parts[0] if parts else "",
            "beats": [p for p in parts[1:-1] if p.strip()] if len(parts) > 2 else [],
            "close": parts[-1] if len(parts) > 1 else "",
            "from": f"@{ig_username}",
        })

    if not scripts_out:
        refund()
        return jsonify({"error": "llm_error",
                        "message": "No se pudieron generar guiones. Inténtalo de nuevo."}), 502

    # Refund parcial: si algunos reels fallaron, devolvemos esos créditos.
    # (Cobramos len(valid); generamos len(scripts_out); refund de la diferencia.)
    if failures and state.get("mode") != "admin":
        # Refund de `failures` unidades: ajustamos el perfil directamente bajo lock
        # (primero free_lifetime consumido de más, luego créditos de pago).
        _rl = acquire_credit_lock(uid)
        try:
            cur = get_profile(uid)
            cost_per_unit = get_cost_cents()
            if state.get("mode") == "paid":
                db.table("profiles").update({
                    "monthly_usage": max(0, (cur.get("monthly_usage") or 0) - failures)
                }).eq("id", uid).execute()
            else:
                # Devolvemos primero a credits_cents lo cobrado por unidades de pago;
                # los free_lifetime consumidos extra se restauran si los hubo.
                free_used = state.get("free_used", 0)
                paid_units = state.get("units", len(valid)) - free_used
                free_to_restore = max(0, min(failures, free_used))
                credit_units_to_restore = failures - free_to_restore
                upd = {}
                if free_to_restore:
                    upd["free_lifetime_uses"] = max(0, (cur.get("free_lifetime_uses") or 0) - free_to_restore)
                if credit_units_to_restore > 0 and paid_units > 0:
                    upd["credits_cents"] = (cur.get("credits_cents") or 0) + credit_units_to_restore * cost_per_unit
                if upd:
                    db.table("profiles").update(upd).eq("id", uid).execute()
        except Exception as e:
            logger.warning("reels_steal_batch: partial refund failed user=%s err=%s", uid, e)
        finally:
            release_credit_lock(uid, _rl)

    return jsonify({"scripts": scripts_out,
                    "cost_cents": len(scripts_out) * get_cost_cents(),
                    **_credits_display(uid)})


@app.route("/ideas/explosion", methods=["POST"])
@require_auth
@limiter.limit("2 per minute;8 per hour")
def ideas_explosion():
    """Isla Signal — botón «Explosión creativa». Secuencia 5 ideas × 5 guiones ×
    5 hooks, todo persistido. Cobra 30 créditos (COST.explosion=30) ANTES de la
    secuencia y refunda TODO si la primera fase (ideas) no produce nada.
    Reúsa develop_idea + adapt_with_ai + regen de hooks, con la voz inyectada.
    Response: {ideas:[...], scripts:[...], hooks:[str], cost_cents, credits_cents, credits}.

    Nota: el frontend puede en su lugar encadenar 3 llamadas (generate-batch ideas
    → scripts → hooks) con async/await; este endpoint hace la secuencia server-side
    en una sola transacción de crédito (30 créditos, refund-all-on-empty)."""
    user = current_user()
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    language = body.get("language", "es")
    project_id = body.get("project_id") or None
    assistant_id = body.get("assistant_id") or get_profile(uid).get("default_idea_assistant") or None

    # Estilo/voz comunes a toda la secuencia.
    style_arg, custom_prompt, style_label = "viral", "", "viral"
    if assistant_id in _BUILTIN_SCRIPT_STYLES:
        style_arg = style_label = assistant_id
    elif assistant_id:
        try:
            asst_r = db.table("assistants").select("name, instructions").eq(
                "id", assistant_id).eq("user_id", uid).execute()
            if asst_r.data and asst_r.data[0].get("instructions"):
                style_arg, custom_prompt = "custom", asst_r.data[0]["instructions"]
                style_label = asst_r.data[0].get("name") or "custom"
        except Exception:
            pass
    # P0-3: "hooks" produce 5 one-liners sueltos, no guiones — degradar a viral
    # (mismo patrón que idea_scripts_generate_batch).
    if style_arg == "hooks":
        style_arg = style_label = "viral"
    if _custom_too_short(style_arg, custom_prompt):
        return _assistant_too_short_response(style_label)

    # La Explosión es exclusiva de planes de pago.
    profile = get_profile(uid)
    if not paid_features_active(profile, user) and (user.get("email", "").lower() not in UNLIMITED_EMAILS):
        return jsonify({
            "error": "paid_only",
            "message": "La Explosión creativa es de los planes de pago. Sube a Creator o Agency.",
        }), 402

    # Coste total de la explosión: COST.explosion = 30 créditos.
    err, refund, _ = _charge_units_locked(uid, 30, user)
    if err:
        return err

    voice = get_voice_profile(uid)
    seeds = [
        "Dame un ángulo nuevo y específico para un reel sobre tu nicho, evita lo obvio.",
        "Una idea contraintuitiva o que rompa una creencia común de tu audiencia.",
        "Una idea basada en un error típico de tu audiencia y cómo evitarlo.",
        "Una idea tipo 'cómo conseguir X sin Y' para tu nicho.",
        "Una idea con gancho de historia personal o caso real (sin inventar datos).",
    ]
    angles = [
        "Enfoque directo y práctico, paso a paso.",
        "Enfoque de historia: arranca con una anécdota o caso concreto.",
        "Enfoque contraintuitivo: ataca una creencia común desde el hook.",
        "Enfoque lista: estructura el body como pasos numerados claros.",
        "Enfoque emocional: conecta con la frustración o el deseo de la audiencia.",
    ]

    import time as _t
    import gevent
    from gevent.pool import Pool as _GPool

    # Pool de 8 greenlets concurrentes — suficiente para solapar I/O de red sin
    # saturar el rate-limit de OpenRouter/Groq (que rechaza ráfagas de >10 rpm).
    _POOL_SIZE = 8

    # ── Fase 1: 5 ideas en paralelo ──────────────────────────────────────────
    def _gen_idea(i):
        try:
            return develop_idea(seeds[i], assistant_id, uid, language)
        except Exception as e:
            logger.warning("explosion: develop failed user=%s i=%s err=%s", uid, i, e)
            return None

    _t0 = _t.time()
    _pool1 = _GPool(_POOL_SIZE)
    _idea_jobs = [_pool1.spawn(_gen_idea, i) for i in range(5)]
    gevent.joinall(_idea_jobs, timeout=120)
    logger.info("explosion: fase1 ideas %.1fs user=%s", _t.time() - _t0, uid)

    ideas_out = []
    for i, job in enumerate(_idea_jobs):
        result = job.value
        if not result:
            continue
        try:
            r = db.table("ideas").insert({
                "user_id": uid, "project_id": project_id,
                "raw_text": result.get("title") or seeds[i],
                "assistant_id": assistant_id,
                "title": result.get("title"), "category": result.get("category"),
                "script_draft": result.get("script_draft"), "status": "developed",
            }).execute()
            if r.data:
                ideas_out.append(r.data[0])
        except Exception as e:
            logger.error("explosion: idea insert failed user=%s err=%s", uid, e, exc_info=True)

    if not ideas_out:
        refund()
        return jsonify({"error": "llm_error",
                        "message": "La explosión no pudo arrancar. Inténtalo de nuevo."}), 502

    # ── Fase 2: 25 guiones en paralelo (5 ideas × 5 ángulos) ─────────────────
    def _gen_script(idea, j):
        title = idea.get("title") or ""
        raw_text = idea.get("raw_text") or ""
        category = idea.get("category") or ""
        _sd = idea.get("script_draft")
        if isinstance(_sd, dict):
            _sd_i = _sd.get("intro") or ""
            _sd_d = _sd.get("desarrollo") or ""
            _sd_c = _sd.get("cierre") or ""
            draft_block = (
                f"\n\n[Borrador de desarrollo]\nIntro: {_sd_i}\n"
                f"Desarrollo: {_sd_d}\nCierre: {_sd_c}"
            ) if (_sd_i or _sd_d or _sd_c) else ""
        else:
            draft_block = ""
        user_content = (
            f"[Idea original del usuario]\n{raw_text}\n\n"
            f"[Título]\n{title}\n\n[Categoría]\n{category or '—'}{draft_block}\n\n"
            f"[Ángulo para ESTA variación]\n{angles[j]}\n\n"
            f"Tarea: convierte esto en un guion completo de 30-45 segundos hablados "
            f"para un reel de Instagram, siguiendo el ángulo indicado. Tu output debe tener "
            f"desarrollo real, ejemplos concretos (sin inventar datos numéricos), profundidad y ritmo. "
            f"Total: 100-140 palabras, mínimo 8 frases en body. "
            f"Incluye al menos 1 ejemplo concreto o anécdota dentro del desarrollo."
        )
        try:
            return adapt_with_ai(user_content, style_arg, custom_prompt, voice=voice, user_id=uid)
        except Exception as e:
            logger.warning("explosion: script LLM failed user=%s err=%s", uid, e)
            return None

    _t1 = _t.time()
    _pool2 = _GPool(_POOL_SIZE)
    _script_jobs, _script_meta = [], []
    for idea in ideas_out:
        for j in range(5):
            _script_jobs.append(_pool2.spawn(_gen_script, idea, j))
            _script_meta.append((idea, j))
    gevent.joinall(_script_jobs, timeout=180)
    logger.info("explosion: fase2 scripts %.1fs user=%s ok=%d/%d",
                _t.time() - _t1, uid,
                sum(1 for j in _script_jobs if j.value is not None), len(_script_jobs))

    # Aplanar resultados exitosos antes de los inserts y de la fase de hooks.
    _pending = []   # [(idea, j, flat, llm_title)]
    for k, job in enumerate(_script_jobs):
        result = job.value
        if not result:
            continue
        flat, llm_title = _flatten_script_result(result)
        if flat:
            idea, j = _script_meta[k]
            _pending.append((idea, j, flat, llm_title))

    # ── Fase 3: hooks en paralelo (5 por guion) ───────────────────────────────
    def _gen_hook(flat):
        lines = [ln.strip() for ln in flat.split("\n") if ln.strip()]
        ctx = "\n".join(lines[1:]) if len(lines) > 1 else flat
        user_msg = (f"Guión actual:\n{ctx}\n\n"
                    f"Texto original del que salió:\n{lines[0] if lines else flat}")
        try:
            raw = _call_llm(_HOOK_REGEN_PROMPT, user_msg, temperature=0.9)
            return _extract_hook(raw) or None
        except Exception:
            return None

    _t2 = _t.time()
    _pool3 = _GPool(_POOL_SIZE)
    _hook_jobs, _hook_meta = [], []
    for k, (idea, j, flat, llm_title) in enumerate(_pending):
        for _ in range(5):
            _hook_jobs.append(_pool3.spawn(_gen_hook, flat))
            _hook_meta.append(k)
    gevent.joinall(_hook_jobs, timeout=180)
    logger.info("explosion: fase3 hooks %.1fs user=%s ok=%d/%d",
                _t.time() - _t2, uid,
                sum(1 for j in _hook_jobs if j.value), len(_hook_jobs))

    _hooks_by_script = {}
    for m, job in enumerate(_hook_jobs):
        h = job.value
        if not h:
            continue
        k = _hook_meta[m]
        bucket = _hooks_by_script.setdefault(k, [])
        if h not in bucket and len(bucket) < 5:
            bucket.append(h)

    # Insertar guiones y construir respuesta.
    scripts_out, hooks_out = [], []
    today = datetime.now(timezone.utc).strftime("%d %b %Y").lower()
    asst_name = _resolve_assistant_name(
        {"assistant_id": assistant_id, "style": style_label}, uid, db)

    _seen_titles = set()   # (6) ideas recién creadas → basta dedupe intra-run
    for k, (idea, j, flat, llm_title) in enumerate(_pending):
        script_title = _uniquify_title(llm_title or f"Guión · {style_label} · {today}", _seen_titles)
        alt = _hooks_by_script.get(k, [])
        hooks_out.extend(alt)
        sid = None
        try:
            ins = db.table("scripts").insert({
                "user_id": uid, "idea_id": idea.get("id"),
                "title": script_title, "script": flat,
                "project_id": idea.get("project_id"),
                "alt_hooks": alt,
                "assistant_name": asst_name,
            }).execute()
            sid = ins.data[0].get("id") if ins.data else None
        except Exception as e:
            logger.error("explosion: script insert failed user=%s err=%s", uid, e, exc_info=True)
        scripts_out.append({
            "id": sid, "title": script_title, "script": flat,
            "idea_id": idea.get("id"), "alt_hooks": alt,
        })

    for idea in ideas_out:
        try:
            db.table("ideas").update({
                "status": "scripted",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", idea.get("id")).eq("user_id", uid).execute()
        except Exception:
            pass

    return jsonify({
        "ideas": [{"id": i.get("id"), "raw_text": i.get("raw_text"),
                   "title": i.get("title"), "category": i.get("category"),
                   "status": "scripted"} for i in ideas_out],
        "scripts": scripts_out,
        "hooks": hooks_out,
        "cost_cents": 30 * get_cost_cents(),
        **_credits_display(uid),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5555))
    app.run(debug=True, host="0.0.0.0", port=port)
