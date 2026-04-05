"""Transcriptor — Flask app con Supabase, créditos y Apify."""

import logging
import os
import re
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, session

load_dotenv()

# ── Validate required env vars ───────────────────────────────────────────────

REQUIRED_ENV_VARS = [
    "GROQ_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "FLASK_SECRET_KEY",
]
missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if missing:
    print(f"[FATAL] Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

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

# ── Config ───────────────────────────────────────────────────────────────────

GROQ_API_KEY          = os.environ.get("GROQ_API_KEY", "")
GROQ_URL              = "https://api.groq.com/openai/v1/audio/transcriptions"

# OpenRouter — para transformaciones de texto ("Hazlo tuyo")
OPENROUTER_API_KEY    = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL        = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL      = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-pro-preview-03-25")
SUPABASE_URL          = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
APIFY_TOKEN           = os.environ.get("APIFY_TOKEN", "")
STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

FREE_DAILY_ANON  = 3   # transcripciones gratis para anónimos
FREE_DAILY_USER  = 5   # transcripciones gratis para registrados
FREE_DAILY_ADAPT = 5   # adaptaciones gratis para registrados (hazlo tuyo)
COST_CENTS       = 18   # $0.18 por uso de pago (~7 usos por $1.29)

UNLIMITED_EMAILS = {"davidmiragito@gmail.com"}  # sin límite ni coste

# Límites mensuales por plan (None = usa créditos/gratis diarios)
PLAN_LIMITS = {"free": None, "basic": 30, "pro": 100, "agency": 250}

# Topup: price ID de Stripe (one-time, multi-currency)
STRIPE_TOPUP_PRICE = os.environ.get("STRIPE_TOPUP_PRICE", "")

db: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

try:
    import stripe as stripe_lib
    stripe_lib.api_key = STRIPE_SECRET_KEY
    STRIPE_OK = bool(STRIPE_SECRET_KEY)
except ImportError:
    STRIPE_OK = False

# ── Rate limiting ────────────────────────────────────────────────────────────

from flask_limiter import Limiter  # noqa: E402
from flask_limiter.util import get_remote_address  # noqa: E402

limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per hour"], storage_uri="memory://")


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"error": "Too many requests. Please slow down.", "retry_after": str(e.description)}), 429


# ── Security headers ────────────────────────────────────────────────────────

@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers.pop("Server", None)
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
    valid = {"viral", "divertido", "linkedin", "storytelling", "hooks", "custom"}
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


def get_profile(user_id: str) -> dict:
    """Devuelve el perfil del usuario, reseteando los contadores diarios si hace falta."""
    result = db.table("profiles").select("*").eq("id", user_id).execute()
    if not result.data:
        db.table("profiles").insert({"id": user_id}).execute()
        return {"id": user_id, "credits_cents": 0,
                "free_used_today": 0, "free_adapt_used_today": 0,
                "free_reset_date": str(date.today()),
                "free_adapt_reset_date": str(date.today())}
    profile = result.data[0]
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
    limit = PLAN_LIMITS.get(plan)
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
        return False, f"Has alcanzado el límite de {limit} transcripciones/mes de tu plan. Mejora tu plan para continuar."
    return True, None


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
        session["user"] = {"id": str(user.id), "email": user.email}
        # Guardar referencia de afiliado si viene
        affiliate_ref = body.get("affiliate_ref", "").strip()
        if affiliate_ref:
            db.table("profiles").upsert({
                "id": str(user.id), "affiliate_ref": affiliate_ref
            }).execute()
        return jsonify({"ok": True, "email": user.email})
    except Exception as e:
        msg = str(e).lower()
        if "already registered" in msg or "already exists" in msg or "duplicate" in msg:
            return jsonify({"error": "Este email ya está registrado"}), 409
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
        session["user"] = {"id": user["id"], "email": user["email"]}
        return jsonify({"ok": True, "email": user["email"]})
    except Exception:
        return jsonify({"error": "Error de conexión"}), 500


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
        "credits_cents":   profile["credits_cents"],
        "free_used_today": profile["free_used_today"],
        "free_daily_limit": FREE_DAILY_USER,
        "plan": plan,
        "monthly_usage": profile.get("monthly_usage", 0),
        "monthly_limit": PLAN_LIMITS.get(plan),
        "avatar_seed": profile.get("avatar_seed", "default"),
    })


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
        user_plan = profile.get("plan", "free")
        if user_plan in ("basic", "pro", "agency"):
            ok, err_msg = check_monthly_limit(profile)
            if not ok:
                return jsonify({"error": err_msg}), 429
        elif profile["credits_cents"] >= COST_CENTS:
            pass
        elif profile["free_used_today"] < FREE_DAILY_USER:
            pass
        else:
            return jsonify({
                "error": f"Has usado tus {FREE_DAILY_USER} transcripciones gratuitas de hoy. "
                         "Recarga saldo para continuar sin límite."
            }), 429

    # ── Actualizar contador antes de encolar ──────────────────────────────
    is_unlimited = user and user.get("email", "").lower() in UNLIMITED_EMAILS
    cost_cents = 0

    if user is None:
        db.table("ip_usage").update(
            {"used_today": ip_usage["used_today"] + 1}
        ).eq("ip", ip).execute()
    elif not is_unlimited:
        profile = get_profile(user["id"])
        user_plan = profile.get("plan", "free")
        if user_plan in ("basic", "pro", "agency"):
            # Incrementar uso mensual
            db.table("profiles").update({
                "monthly_usage": profile.get("monthly_usage", 0) + 1
            }).eq("id", user["id"]).execute()
        elif profile["credits_cents"] >= COST_CENTS:
            cost_cents = COST_CENTS
            db.table("profiles").update(
                {"credits_cents": profile["credits_cents"] - cost_cents}
            ).eq("id", user["id"]).execute()
        else:
            db.table("profiles").update(
                {"free_used_today": profile["free_used_today"] + 1}
            ).eq("id", user["id"]).execute()

    # ── Encolar tarea ─────────────────────────────────────────────────────
    task = transcribe_task.delay(
        url,
        language,
        user["id"] if user else None,
        get_client_ip() if not user else None,
    )

    return jsonify({"task_id": task.id, "cost_cents": cost_cents})


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
        payload = {"state": "success", "text": result["text"], "platform": result["platform"]}
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
        .select("id, url, platform, language, text, cost_cents, created_at")
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

    if not STRIPE_TOPUP_PRICE:
        return jsonify({"error": "Topup no configurado"}), 500

    body = request.get_json()
    currency = body.get("currency", "usd").lower()
    if currency not in ("usd", "eur"):
        currency = "usd"

    # Ambas divisas dan 7 usos (7 × 18 = 126 cents de saldo)
    amount_cents = 126

    user = current_user()
    try:
        checkout_session = stripe_lib.checkout.Session.create(
            payment_method_types=["card"],
            currency=currency,
            line_items=[{"price": STRIPE_TOPUP_PRICE, "quantity": 1}],
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

        if obj["metadata"].get("type") == "subscription":
            # ── Suscripción ──────────────────────────────────────────
            line_items = stripe_lib.checkout.Session.list_line_items(stripe_session_id)
            price_id = line_items.data[0].price.id if line_items.data else None
            plan = PRICE_TO_PLAN.get(price_id, "basic")
            db.table("profiles").update({
                "plan": plan,
                "stripe_subscription_id": obj.get("subscription"),
            }).eq("id", user_id).execute()
        else:
            # ── Recarga de créditos (flujo existente) ────────────────
            amount_cents = int(obj["metadata"]["amount_cents"])

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

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        db.table("profiles").update({
            "plan": "free",
            "stripe_subscription_id": None,
        }).eq("stripe_subscription_id", sub["id"]).execute()

    return "", 200


# ── Subscription endpoints ───────────────────────────────────────────────────

PRICE_TO_PLAN = {
    "price_1TI14pCWQn5Tis1WycY83MrR": "basic",
    "price_1TI15ACWQn5Tis1WKNbdhFW1": "pro",
    "price_1TI15NCWQn5Tis1WwIIb1TX1": "agency",
}


@app.route("/create-subscription-checkout", methods=["POST"])
@limiter.limit("5 per minute;20 per hour")
@require_auth
def create_subscription_checkout():
    if not STRIPE_OK:
        return jsonify({"error": "Pagos no disponibles"}), 503

    body = request.get_json()
    price_id = body.get("price_id", "")
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


@app.route("/manage-subscription", methods=["POST"])
@require_auth
def manage_subscription():
    """Create a Stripe Customer Portal session so users can manage/cancel."""
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
            return_url=request.host_url,
        )
        return jsonify({"url": portal.url})
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error. Please try again."}), 500


@app.route("/cancel-subscription", methods=["POST"])
@require_auth
def cancel_subscription():
    if not STRIPE_OK:
        return jsonify({"error": "Pagos no disponibles"}), 503

    user = current_user()
    profile = get_profile(user["id"])
    sub_id = profile.get("stripe_subscription_id")
    if not sub_id:
        return jsonify({"error": "No tienes suscripción activa"}), 400

    try:
        stripe_lib.Subscription.modify(sub_id, cancel_at_period_end=True)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error. Please try again."}), 500


# ── Adapt route ───────────────────────────────────────────────────────────────

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
        "Devuelve ÚNICAMENTE el guión reescrito, nada más."
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
        "Devuelve ÚNICAMENTE el guión reescrito, nada más."
    ),

    "linkedin": (
        "Eres un guionista de contenido. Reescribe este guión en formato LinkedIn: tono profesional pero directo, sin distancia. "
        "Primera persona siempre. Una sola idea, desarrollada con lógica clara. "
        "Datos concretos si los hay — ningún dato inventado. "
        "Párrafos de máximo 2-3 líneas con espacio entre ellos. "
        "Sin frases vacías ('en el panorama actual', 'es fundamental', 'cabe destacar', 'valor añadido', 'solución integral'). "
        "Sin motivacional. Cierre que deja una pregunta abierta o una afirmación que genera reacción — nunca una conclusión envuelta en papel de regalo. "
        "El lector tiene que terminar pensando, no sintiéndose inspirado. "
        "Devuelve ÚNICAMENTE el texto listo para publicar, nada más."
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
        "Devuelve ÚNICAMENTE la historia, nada más."
    ),

    "hooks": (
        "Eres un guionista de reels. Dame exactamente 5 hooks para este guión, uno de cada tipo. "
        "Reglas para todos: tienen que incluir términos específicos del nicho para filtrar a la audiencia correcta desde el primer segundo. "
        "Ningún hook puede dar el valor completo — si el viewer puede llevarse el insight sin ver el vídeo, el hook falla. "
        "Formato: [TIPO] 'hook'. "
        "Los 5 tipos — "
        "TRANSFORMACIÓN: salto de A a B con dato concreto y creíble. "
        "NEGATIVO: ataca una creencia instalada en el nicho. "
        "ENEMIGO: el error que sigue cometiendo la audiencia. "
        "CURIOSIDAD: abre una puerta sin revelar nada, obliga a seguir para entender. "
        "PROMESA: resultado concreto y específico con condición real. "
        "Devuelve ÚNICAMENTE los 5 hooks con su etiqueta, nada más."
    ),

}

# Instrucciones base para el estilo Custom (se anteponen a las instrucciones del usuario)
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
    "Devuelve ÚNICAMENTE el guión reescrito, nada más. "
    "Ahora aplica estas instrucciones adicionales:\n"
)


def adapt_with_ai(text: str, style: str, custom_prompt: str = "") -> str:
    if style == "custom":
        if not custom_prompt:
            raise ValueError("Escribe tus instrucciones en el campo Custom")
        system = CUSTOM_BASE + custom_prompt
    else:
        system = STYLE_PROMPTS.get(style)
        if not system:
            raise ValueError("Estilo no válido")

    api_key = OPENROUTER_API_KEY or GROQ_API_KEY
    url     = OPENROUTER_URL if OPENROUTER_API_KEY else "https://api.groq.com/openai/v1/chat/completions"
    model   = OPENROUTER_MODEL if OPENROUTER_API_KEY else "llama-3.3-70b-versatile"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **({"HTTP-Referer": "https://reelscript.net", "X-Title": "ReelScript"} if OPENROUTER_API_KEY else {}),
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "temperature": 0.8,
        "max_tokens": 20000,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


@app.route("/saved-scripts")
@require_auth
def saved_scripts():
    user = current_user()
    rows = db.table("saved_scripts") \
        .select("*") \
        .eq("user_id", user["id"]) \
        .order("created_at", desc=True) \
        .limit(50) \
        .execute()
    return jsonify(rows.data)


@app.route("/saved-scripts/<script_id>", methods=["DELETE"])
@require_auth
def delete_saved_script(script_id):
    user = current_user()
    db.table("saved_scripts") \
        .delete() \
        .eq("id", script_id) \
        .eq("user_id", user["id"]) \
        .execute()
    return jsonify({"ok": True})


@app.route("/save-script", methods=["POST"])
def save_script():
    user = current_user()
    if not user:
        return jsonify({"error": "No autenticado"}), 401
    body = request.get_json()
    db.table("saved_scripts").insert({
        "user_id": user["id"],
        "style": body.get("style", ""),
        "content": body.get("content", ""),
    }).execute()
    return jsonify({"ok": True})


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
    if not GROQ_API_KEY:
        return jsonify({"error": "Service unavailable"}), 500
    if not style and not custom_prompt and not assistant_id:
        return jsonify({"error": "Select a style"}), 400

    # If assistant_id, override style to use assistant's instructions
    if assistant_id:
        ast_result = db.table("assistants").select("instructions").eq("id", assistant_id).execute()
        if ast_result.data:
            style = "custom"
            custom_prompt = ast_result.data[0]["instructions"]
        else:
            return jsonify({"error": "Assistant not found"}), 404

    user = current_user()
    cost_cents = 0

    # ── Comprobar límites / saldo (adapt usa free_adapt_used_today) ──────────
    if user and user.get("email", "").lower() in UNLIMITED_EMAILS:
        pass  # sin límite ni coste para cuentas admin
    elif user is None:
        return jsonify({
            "error": "Regístrate gratis para usar Hazlo tuyo."
        }), 429
    else:
        profile = get_profile(user["id"])
        user_plan = profile.get("plan", "free")
        if user_plan in ("basic", "pro", "agency"):
            ok, err_msg = check_monthly_limit(profile)
            if not ok:
                return jsonify({"error": err_msg}), 429
            cost_cents = 0
        elif profile["credits_cents"] >= COST_CENTS:
            cost_cents = COST_CENTS
        elif profile.get("free_adapt_used_today", 0) < FREE_DAILY_ADAPT:
            cost_cents = 0
        else:
            return jsonify({
                "error": f"Has usado tus {FREE_DAILY_ADAPT} adaptaciones gratuitas de hoy. "
                         "Recarga saldo para continuar."
            }), 429

    try:
        result = adapt_with_ai(text, style, custom_prompt)
    except requests.HTTPError as e:
        return jsonify({"error": f"Error de la API: {e}"}), 502
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error. Please try again."}), 500

    # Actualizar contadores
    is_unlimited = user and user.get("email", "").lower() in UNLIMITED_EMAILS
    if not is_unlimited and user:
        if profile.get("plan", "free") in ("basic", "pro", "agency"):
            db.table("profiles").update({
                "monthly_usage": profile.get("monthly_usage", 0) + 1
            }).eq("id", user["id"]).execute()
        elif cost_cents > 0:
            db.table("profiles").update(
                {"credits_cents": profile["credits_cents"] - cost_cents}
            ).eq("id", user["id"]).execute()
        else:
            db.table("profiles").update(
                {"free_adapt_used_today": profile.get("free_adapt_used_today", 0) + 1}
            ).eq("id", user["id"]).execute()

    payload: dict = {"result": result, "cost_cents": cost_cents}
    if user:
        updated = get_profile(user["id"])
        payload["credits_cents"]   = updated["credits_cents"]
        payload["free_used_today"] = updated["free_used_today"]
    return jsonify(payload)


# ── Assistants ────────────────────────────────────────────────────────────────

ASSISTANT_LIMITS = {"free": 0, "basic": 1, "pro": 5, "agency": None}


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


@app.route("/projects", methods=["GET"])
@require_auth
def list_projects():
    user = current_user()
    rows = db.table("projects").select("*").eq("user_id", user["id"]).order("created_at", desc=True).execute()
    return jsonify(rows.data)


@app.route("/projects", methods=["POST"])
@require_auth
def create_project():
    user = current_user()
    profile = get_profile(user["id"])
    if profile.get("plan", "free") not in ("pro", "agency"):
        return jsonify({"error": "Tu plan no incluye proyectos. Mejora a Pro."}), 403
    body = request.get_json()
    row = db.table("projects").insert({
        "user_id": user["id"],
        "name": body.get("name", "Sin nombre"),
        "style_prompt": body.get("style_prompt", ""),
    }).execute()
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
    body = request.get_json()
    updates = {}
    if "name" in body:
        updates["name"] = body["name"]
    if "style_prompt" in body:
        updates["style_prompt"] = body["style_prompt"]
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
    for key in ("title", "performance_notes", "views_count", "engagement_rate"):
        if key in body:
            updates[key] = body[key]
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    db.table("scripts").update(updates).eq("id", script_id).eq("user_id", user["id"]).execute()
    return jsonify({"ok": True})


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

    result = db.table("agency_members").insert({
        "agency_owner_id": user["id"],
        "invited_email": email,
        "status": "pending",
    }).execute()

    token = result.data[0]["invite_token"] if result.data else None
    invite_url = f"{request.host_url}join?token={token}"
    return jsonify({"invite_url": invite_url, "token": token})


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

    # Projects + script counts (pro/agency)
    if plan in ("pro", "agency"):
        projects = db.table("projects").select("*").eq("user_id", user["id"]).order("created_at", desc=True).execute()
        for p in projects.data:
            count = db.table("scripts").select("id", count="exact").eq("project_id", p["id"]).execute()
            p["script_count"] = count.count if hasattr(count, "count") else 0
        data["projects"] = projects.data

        recent = db.table("scripts").select("id, title, project_id, created_at").eq("user_id", user["id"]).order("created_at", desc=True).limit(5).execute()
        data["recent_scripts"] = recent.data

    return jsonify(data)


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


# ── Main ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/affiliate")
def affiliate_page():
    return render_template("affiliate.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5555))
    app.run(debug=True, host="0.0.0.0", port=port)
