import os
import re
import time
import logging
import tempfile
from datetime import datetime, timedelta, timezone
import requests
import yt_dlp
from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger(__name__)


def _download_thumbnail_b64(url, attempts=2):
    """Download an image URL and return it as a small data-URL base64 JPEG.

    Hardened (isla-v2 T3): los thumbnails NULL del 2026-06-09 fueron fallos
    TRANSITORIOS de descarga (la misma displayUrl de Apify baja 200/JPEG al
    reintentar). El CDN de Instagram a veces 403ea o corta peticiones en ráfaga
    (varias transcripciones seguidas). Mitigamos con (1) cabeceras de navegador
    + Referer y (2) un reintento con backoff. Si aun así falla, devolvemos None
    y la UI degrada a un placeholder limpio.
    """
    if not url:
        return None
    from PIL import Image
    from io import BytesIO
    import base64
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Referer": "https://www.instagram.com/",
        "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*",
    }
    last_err = None
    for attempt in range(max(1, attempts)):
        try:
            r = requests.get(url, timeout=12, headers=headers)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content))
            img.thumbnail((320, 400))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=75, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            last_err = e
            if attempt + 1 < attempts:
                time.sleep(0.8)
    logger.warning("Thumbnail download failed for %s after %d attempts: %s", url, attempts, last_err)
    return None

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("reelscript", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    task_track_started=True,
)

# v0.14.24: beat schedule — process_pending_emails cada 1h
# v0.15.8: sweep_stale_resources cada 5min (transcript stale + locks huérfanos)
# v0.16.x Radar: send_radar_digests diario 08:00 UTC ("Lo que petó en tu nicho").
celery_app.conf.beat_schedule = {
    "process-pending-emails": {
        "task": "tasks.process_pending_emails",
        "schedule": 3600.0,
    },
    # reverse-trial: barrido horario del ciclo de trial (acaba mañana / expirado).
    "send-trial-lifecycle-emails": {
        "task": "tasks.send_trial_lifecycle_emails",
        "schedule": 3600.0,
    },
    # Ola Agencia B5: informe mensual white-label (1º de mes, 08:00 UTC).
    "send-monthly-brand-reports": {
        "task": "tasks.send_monthly_brand_reports",
        "schedule": crontab(day_of_month=1, hour=8, minute=0),
    },
    "sweep-stale-resources": {
        "task": "tasks.sweep_stale_resources",
        "schedule": 300.0,
    },
    "send-radar-digests": {
        "task": "tasks.send_radar_digests",
        "schedule": crontab(hour=8, minute=0),
    },
    # growth-6: digest SEMANAL para FREE/lapsed (lunes 09:00 UTC). El diario es
    # solo de pago → este no duplica, es el gancho de vuelta de los gratis.
    "send-weekly-digests": {
        "task": "tasks.send_weekly_digests",
        "schedule": crontab(day_of_week=1, hour=9, minute=0),
    },
    # Fathom 18/06: NOVEDAD DIARIA — re-scrape diario de competidores seguidos a las
    # 06:00 UTC (antes del digest de las 08:00, para que el correo lleve lo fresco).
    "refresh-radar-daily": {
        "task": "tasks.refresh_radar_daily",
        "schedule": crontab(hour=6, minute=0),
    },
    # Fathom 18/06: AUTO-SCRAPE del perfil propio 2×/semana (lunes y jueves 07:00 UTC).
    "scrape-user-profiles": {
        "task": "tasks.scrape_user_profiles",
        "schedule": crontab(day_of_week="1,4", hour=7, minute=0),
    },
    # Fathom 18/06: alerta DIARIA "tu ejercicio del Cerebro está listo" (09:00 UTC).
    # Solo a onboarded que no lo hayan hecho hoy. Cadencia ajustable si satura.
    "send-train-hooks-nudges": {
        "task": "tasks.send_train_hooks_nudges",
        "schedule": crontab(hour=9, minute=0),
    },
}
celery_app.conf.timezone = "UTC"


# v0.14.24: email tasks
@celery_app.task(name="tasks.send_email_now")
def send_email_now(user_id, template_key):
    """Welcome email (T+0) — disparado fire-and-forget desde signup.
    Para day1/day7 usar process_pending_emails (beat)."""
    try:
        from emails import send_template, process_pending_row
        if template_key in ("day1_pending", "day7_pending"):
            process_pending_row({"user_id": user_id, "template_key": template_key})
        else:
            send_template(user_id, template_key)
    except Exception as e:
        logger.warning("send_email_now failed user=%s template=%s err=%s",
                       user_id, template_key, e)


@celery_app.task(name="tasks.send_monthly_brand_reports")
def send_monthly_brand_reports():
    """Ola Agencia B5: el 1º de mes, avisa a cada owner de Agencia de que el
    informe white-label de cada una de sus marcas está listo (enlace al generador
    in-app). Idempotente por (owner, marca, mes) vía email_log."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {"error": "supabase_not_configured"}
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        from app import _build_brand_report
        from emails import send_brand_report_ready
    except Exception as e:
        logger.error("send_monthly_brand_reports: import failed: %s", e)
        return {"error": "import"}

    now = datetime.now(timezone.utc)
    month_label = f"{now.year}-{now.month:02d}"
    try:
        agencies = (db.table("profiles").select("id").eq("plan", "agency").execute()).data or []
    except Exception as e:
        logger.error("send_monthly_brand_reports: agencies query failed: %s", e)
        return {"error": "query"}

    sent = 0
    for ag in agencies:
        owner_id = ag["id"]
        try:
            projs = (db.table("projects").select("id, name").eq("user_id", owner_id).execute()).data or []
        except Exception:
            projs = []
        for p in projs:
            try:
                data = _build_brand_report(owner_id, p["id"])
                if not data["reels"] and not data["scripts"]:
                    continue   # nada que reportar este mes
                res = send_brand_report_ready(owner_id, p.get("name") or "Tu marca",
                                              p["id"], len(data["reels"]), len(data["scripts"]), month_label)
                if res.get("sent"):
                    sent += 1
            except Exception as e:
                logger.warning("monthly_brand_report failed owner=%s proj=%s err=%s", owner_id, p.get("id"), e)
    logger.info("send_monthly_brand_reports done agencies=%s sent=%s", len(agencies), sent)
    return {"agencies": len(agencies), "sent": sent}


@celery_app.task(name="tasks.send_agency_invite_email")
def send_agency_invite_email(owner_id, invited_email, invite_url, owner_name=None):
    """Ola Agencia B2: envía la invitación de equipo fuera del request."""
    try:
        from emails import send_agency_invite
        return send_agency_invite(owner_id, invited_email, invite_url, owner_name)
    except Exception as e:
        logger.warning("send_agency_invite_email failed owner=%s err=%s", owner_id, e)
        return {"error": str(e)[:200]}


@celery_app.task(name="tasks.send_payment_failed_email")
def send_payment_failed_email(user_id, invoice_id, attempt=None):
    """growth-5: dunning — dispara el email de fallo de cobro fuera del webhook
    (no bloquea la respuesta a Stripe)."""
    try:
        from emails import send_payment_failed
        return send_payment_failed(user_id, invoice_id, attempt)
    except Exception as e:
        logger.warning("send_payment_failed_email failed user=%s invoice=%s err=%s",
                       user_id, invoice_id, e)
        return {"error": str(e)[:200]}


@celery_app.task(name="tasks.send_trial_lifecycle_emails")
def send_trial_lifecycle_emails():
    """reverse-trial: barrido horario del ciclo de trial.
      - trial_ends_at en las próximas 24h (y futuro) → 'acaba mañana' (día 2).
      - trial_ends_at ya pasado (últimas 48h) y aún en Free → 'ha expirado'.
    Idempotente por (user_id, template_key) en email_log. Solo free con trial."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {"error": "supabase_not_configured"}
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        from emails import send_trial_ending, send_trial_expired
    except Exception as e:
        logger.error("send_trial_lifecycle_emails: import emails failed: %s", e)
        return {"error": "import_emails"}

    now = datetime.now(timezone.utc)
    try:
        rows = (db.table("profiles")
                  .select("id, trial_ends_at, plan")
                  .eq("plan", "free")
                  .not_.is_("trial_ends_at", "null")
                  .execute()).data or []
    except Exception as e:
        logger.error("send_trial_lifecycle_emails: query failed: %s", e)
        return {"error": "query"}

    ending = expired = 0
    for r in rows:
        ends = r.get("trial_ends_at")
        try:
            dt = datetime.fromisoformat(str(ends).replace("Z", "+00:00"))
        except Exception:
            continue
        delta_h = (dt - now).total_seconds() / 3600.0
        try:
            if 0 < delta_h <= 24:                      # acaba en <24h → 'mañana'
                if send_trial_ending(r["id"]).get("sent"):
                    ending += 1
            elif -48 <= delta_h <= 0:                  # expiró en las últimas 48h
                if send_trial_expired(r["id"]).get("sent"):
                    expired += 1
        except Exception as e:
            logger.warning("send_trial_lifecycle_emails: send failed user=%s err=%s", r.get("id"), e)

    logger.info("send_trial_lifecycle_emails done ending=%s expired=%s", ending, expired)
    return {"ending": ending, "expired": expired}


@celery_app.task(name="tasks.process_pending_emails")
def process_pending_emails():
    """Beat task: cada 1h chequea email_log queued and due.
    Bifurca day1_pending / day7_pending y envía. Idempotente."""
    try:
        from emails import fetch_pending_emails, process_pending_row
        rows = fetch_pending_emails(limit=200)
        if not rows:
            return {"processed": 0}
        sent = 0
        for row in rows:
            try:
                process_pending_row(row)
                sent += 1
            except Exception as e:
                logger.warning("process row failed user=%s template=%s err=%s",
                               row.get("user_id"), row.get("template_key"), e)
        return {"processed": sent}
    except Exception as e:
        logger.error("process_pending_emails failed: %s", e, exc_info=True)
        return {"error": str(e)[:200]}


# ── v0.16.x Radar: email diario "Lo que petó en tu nicho" ──────────────────
# Helpers locales (duplican la lógica de app._creator_view_baselines para no
# acoplar el worker al import de app/Flask en el beat). Mediana de views de los
# reels recientes de cada creador → índice de explosión = views / baseline.

RADAR_ENABLED_PLANS = {"pro", "creator", "agency"}


def _radar_baselines(db, creator_ids):
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
        logger.warning("_radar_baselines failed: %s", e)
        return {}
    buckets = {}
    for r in rows:
        v = int(r.get("views") or 0)
        if v > 0:
            buckets.setdefault(r["creator_id"], []).append(v)
    out = {}
    for cid, vs in buckets.items():
        vs.sort()
        n = len(vs)
        out[cid] = vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2.0
    return out


def _radar_explosion(views, baseline):
    if not baseline or baseline < 1:
        return None
    return round(float(views or 0) / float(baseline), 2)


@celery_app.task(name="tasks.send_radar_digests")
def send_radar_digests():
    """Beat diario: email "Lo que petó en tu nicho" a usuarios con competidores.

    Por usuario: reels de sus competidores en las últimas 48h con explosión
    >= 2x (máx 3, ordenados por explosión). Idempotente por día vía email_log.
    Solo planes con la feature (pro/creator/agency) y email_marketing on.
    """
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {"error": "supabase_not_configured"}
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    try:
        from emails import send_radar_digest
    except Exception as e:
        logger.error("send_radar_digests: import emails failed: %s", e)
        return {"error": "import_emails"}

    now = datetime.now(timezone.utc)
    day_key = now.date().isoformat()
    since = (now - timedelta(hours=48)).isoformat()

    # 1. Usuarios con competidores activos → {user_id: set(creator_id)}.
    try:
        tracked = (db.table("user_tracked_creators")
                     .select("user_id, creator_id")
                     .is_("archived_at", "null")
                     .execute()).data or []
    except Exception as e:
        logger.error("send_radar_digests: tracked query failed: %s", e)
        return {"error": "tracked_query"}
    by_user = {}
    for t in tracked:
        by_user.setdefault(t["user_id"], set()).add(t["creator_id"])
    if not by_user:
        return {"users": 0, "sent": 0}

    # 2. Filtrar por plan con la feature activa (un único query a profiles).
    uids = list(by_user.keys())
    try:
        profs = (db.table("profiles")
                   .select("id, plan")
                   .in_("id", uids)
                   .execute()).data or []
        plan_by_uid = {p["id"]: (p.get("plan") or "free") for p in profs}
    except Exception as e:
        logger.warning("send_radar_digests: profiles query failed: %s", e)
        plan_by_uid = {}

    sent = skipped = 0
    for uid, cid_set in by_user.items():
        if plan_by_uid.get(uid, "free") not in RADAR_ENABLED_PLANS:
            skipped += 1
            continue
        cids = list(cid_set)
        baselines = _radar_baselines(db, cids)
        try:
            rows = (db.table("creator_reels_global")
                      .select("id, ig_reel_id, creator_id, caption, views, likes, "
                              "posted_at, creator:creators_global(ig_username)")
                      .in_("creator_id", cids)
                      .eq("is_archived", False)
                      .gte("posted_at", since)
                      .limit(200)
                      .execute()).data or []
        except Exception as e:
            logger.warning("send_radar_digests: reels query failed user=%s err=%s", uid, e)
            skipped += 1
            continue

        scored = []
        for r in rows:
            sc = _radar_explosion(r.get("views"), baselines.get(r.get("creator_id")))
            if sc is not None and sc >= 2.0:
                scored.append({
                    "username": ((r.get("creator") or {}).get("ig_username") or ""),
                    "caption": r.get("caption") or "",
                    "views": r.get("views") or 0,
                    "explosion": sc,
                })
        if not scored:
            skipped += 1
            continue
        scored.sort(key=lambda x: x["explosion"], reverse=True)
        top = scored[:3]

        # Sugerencia "el siguiente de esa serie": tu guión que mejor rinde.
        # Réplica ligera de app.next_series_suggestion (evita importar Flask).
        next_suggestion = None
        try:
            sr = (db.table("scripts").select("id, title, views_count")
                    .eq("user_id", uid).not_.is_("views_count", "null")
                    .order("views_count", desc=True).limit(1).execute()).data or []
            if sr:
                next_suggestion = {
                    "script_id": sr[0]["id"],
                    "title": sr[0].get("title"),
                    "views": sr[0].get("views_count"),
                }
        except Exception as e:
            logger.warning("send_radar_digests: next_suggestion failed user=%s err=%s", uid, e)
            next_suggestion = None

        try:
            res = send_radar_digest(uid, top, day_key, next_suggestion)
            if res.get("sent"):
                sent += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("send_radar_digests: send failed user=%s err=%s", uid, e)
            skipped += 1

    logger.info("send_radar_digests done users=%s sent=%s skipped=%s",
                len(by_user), sent, skipped)
    return {"users": len(by_user), "sent": sent, "skipped": skipped}


@celery_app.task(name="tasks.send_weekly_digests")
def send_weekly_digests():
    """Beat semanal (lunes): "lo que petó en tu nicho esta semana" a usuarios
    FREE (y lapsed) con competidores — los que el digest diario NO toca
    (RADAR_ENABLED_PLANS = solo pago). Es el gancho de vuelta + paywall.

    Por usuario: reels de sus competidores en los últimos 7 días con explosión
    >= 2x (máx 3). Idempotente por semana ISO vía email_log.
    """
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {"error": "supabase_not_configured"}
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    try:
        from emails import send_weekly_digest
    except Exception as e:
        logger.error("send_weekly_digests: import emails failed: %s", e)
        return {"error": "import_emails"}

    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    since = (now - timedelta(days=7)).isoformat()

    try:
        tracked = (db.table("user_tracked_creators")
                     .select("user_id, creator_id")
                     .is_("archived_at", "null")
                     .execute()).data or []
    except Exception as e:
        logger.error("send_weekly_digests: tracked query failed: %s", e)
        return {"error": "tracked_query"}
    by_user = {}
    for t in tracked:
        by_user.setdefault(t["user_id"], set()).add(t["creator_id"])
    if not by_user:
        return {"users": 0, "sent": 0}

    uids = list(by_user.keys())
    try:
        profs = (db.table("profiles").select("id, plan").in_("id", uids).execute()).data or []
        plan_by_uid = {p["id"]: (p.get("plan") or "free") for p in profs}
    except Exception as e:
        logger.warning("send_weekly_digests: profiles query failed: %s", e)
        plan_by_uid = {}

    sent = skipped = 0
    for uid, cid_set in by_user.items():
        # SOLO FREE/lapsed: a los de pago ya les llega el diario (no duplicar).
        if plan_by_uid.get(uid, "free") in RADAR_ENABLED_PLANS:
            skipped += 1
            continue
        cids = list(cid_set)
        baselines = _radar_baselines(db, cids)
        try:
            rows = (db.table("creator_reels_global")
                      .select("id, ig_reel_id, creator_id, caption, views, likes, "
                              "posted_at, creator:creators_global(ig_username)")
                      .in_("creator_id", cids)
                      .eq("is_archived", False)
                      .gte("posted_at", since)
                      .limit(200)
                      .execute()).data or []
        except Exception as e:
            logger.warning("send_weekly_digests: reels query failed user=%s err=%s", uid, e)
            skipped += 1
            continue

        scored = []
        for r in rows:
            sc = _radar_explosion(r.get("views"), baselines.get(r.get("creator_id")))
            if sc is not None and sc >= 2.0:
                scored.append({
                    "username": ((r.get("creator") or {}).get("ig_username") or ""),
                    "caption": r.get("caption") or "",
                    "views": r.get("views") or 0,
                    "explosion": sc,
                })
        if not scored:
            skipped += 1
            continue
        scored.sort(key=lambda x: x["explosion"], reverse=True)
        top = scored[:3]

        try:
            res = send_weekly_digest(uid, top, week_key)
            if res.get("sent"):
                sent += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("send_weekly_digests: send failed user=%s err=%s", uid, e)
            skipped += 1

    logger.info("send_weekly_digests done users=%s sent=%s skipped=%s",
                len(by_user), sent, skipped)
    return {"users": len(by_user), "sent": sent, "skipped": skipped}


def detect_platform(url):
    if "instagram.com" in url:
        return "instagram"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "tiktok.com" in url:
        return "tiktok"
    return "otro"

def _is_rate_limited(err) -> bool:
    """Detecta el rate-limit / muro de login de IG/TikTok en el error de yt-dlp."""
    m = str(err or "").lower()
    return any(k in m for k in (
        "rate-limit", "rate limit", "429", "too many requests",
        "login required", "please log in", "log in", "sign in",
        "requested content is not available", "restricted video",
    ))


def _ytdlp(url, output_dir):
    out = os.path.join(output_dir, "audio")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": out,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
        "quiet": True,
        "no_warnings": True,
    }
    # B (fallback cookies): si David configura un cookies.txt (YTDLP_COOKIES_FILE),
    # yt-dlp lo usa para sortear el muro de login de IG. Degrada si no existe.
    _ck = os.environ.get("YTDLP_COOKIES_FILE", "")
    if _ck and os.path.exists(_ck):
        opts["cookiefile"] = _ck
    thumbnail_url = None
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info:
            thumbnail_url = info.get("thumbnail")
            if not thumbnail_url:
                thumbs = info.get("thumbnails") or []
                if thumbs:
                    thumbnail_url = thumbs[-1].get("url")
    return out + ".mp3", thumbnail_url

def _apify_instagram(url, output_dir):
    """Returns (mp3_path, thumbnail_url, apify_item) where apify_item is the
    full Apify response dict (contains views/likes/comments/shares/timestamp).
    apify_item is None when scraping fails (caller falls back to yt-dlp)."""
    APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
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
    # Apify devuelve la imagen en displayUrl casi siempre; cubrimos también las
    # variantes conocidas (carruseles → childPosts, lotes → images) por robustez.
    thumbnail_url = (item.get("displayUrl") or item.get("display_url")
                     or item.get("thumbnailUrl") or item.get("imageUrl"))
    if not thumbnail_url:
        imgs = item.get("images") or []
        if imgs:
            first = imgs[0]
            thumbnail_url = first if isinstance(first, str) else (first.get("url") if isinstance(first, dict) else None)
    if not thumbnail_url:
        childs = item.get("childPosts") or []
        if childs and isinstance(childs[0], dict):
            thumbnail_url = childs[0].get("displayUrl") or childs[0].get("display_url")
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
    return mp3_path, thumbnail_url, item

class DownloadError(Exception):
    """Fallo de descarga con causa identificada (rate_limit | unavailable)."""
    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(detail or code)


def download_audio(url, output_dir, platform):
    """Returns (mp3_path, thumbnail_url, apify_item|None). Endurecido (B):
      - IG: Apify PRIMARIO con 1 reintento (fallos transitorios) → yt-dlp fallback.
      - yt-dlp con 2-3 reintentos y backoff; detecta rate-limit/login de IG.
      - Si yt-dlp topa rate-limit y hay Apify, Apify hace de fallback final.
    Lanza DownloadError(code) si todo falla (el caller muestra mensaje limpio)."""
    APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
    apify_avail = (platform == "instagram") and bool(APIFY_TOKEN)
    attempts = []
    rate_seen = False

    # 1) Apify primario para IG (2 intentos: cubre fallos transitorios del actor).
    if apify_avail:
        for i in range(2):
            try:
                return _apify_instagram(url, output_dir)
            except Exception as e:
                attempts.append("apify#%d:%s" % (i + 1, str(e)[:80]))
                if i == 0:
                    time.sleep(2)

    # 2) yt-dlp con reintentos + backoff (fallback IG · primario TikTok/otros).
    last = None
    for i in range(3):
        try:
            mp3, thumb = _ytdlp(url, output_dir)
            return mp3, thumb, None
        except Exception as e:
            last = e
            attempts.append("ytdlp#%d:%s" % (i + 1, str(e)[:80]))
            if _is_rate_limited(e):
                rate_seen = True
                # rate-limit de yt-dlp → si hay Apify (IG), úsala como salida.
                if apify_avail:
                    try:
                        return _apify_instagram(url, output_dir)
                    except Exception as e2:
                        attempts.append("apify_fb:%s" % (str(e2)[:80]))
            if i < 2:
                time.sleep(2 * (i + 1))   # backoff 2s, 4s

    logger.warning("download_audio agotado url=%s attempts=%s", url, " | ".join(attempts[-4:]))
    raise DownloadError("rate_limit" if rate_seen else "unavailable", str(last or "")[:120])


def _apify_metrics_only(url):
    """Apify call solo para métricas (no audio). Reusa apify~instagram-scraper.
    Devuelve dict normalizado o None si falla."""
    APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
    if not APIFY_TOKEN:
        return None
    actor_url = (
        f"https://api.apify.com/v2/acts/apify~instagram-scraper"
        f"/run-sync-get-dataset-items?token={APIFY_TOKEN}&memory=256"
    )
    try:
        resp = requests.post(actor_url, json={"directUrls": [url], "resultsLimit": 1}, timeout=120)
        resp.raise_for_status()
        items = resp.json()
        if not items:
            return None
        return items[0]
    except Exception as e:
        logger.warning("apify metrics-only failed for %s: %s", url, e)
        return None


def _extract_metrics(apify_item):
    """Convierte un item de apify-instagram-scraper en columnas de la tabla."""
    if not apify_item:
        return {}
    return {
        "views":    apify_item.get("videoPlayCount") or apify_item.get("videoViewCount") or 0,
        "likes":    apify_item.get("likesCount") or 0,
        "comments": apify_item.get("commentsCount") or 0,
        "shares":   apify_item.get("sharesCount") or 0,
        "published_at": apify_item.get("timestamp"),
        "metrics_updated_at": datetime.utcnow().isoformat() + "Z",
    }

def _refund_transcribe_charge(db, user_id, ip, charge):
    """Devuelve lo que /transcribe cobró por adelantado cuando la task falla.
    Read-modify-write sin lock: el refund es raro y el peor caso de carrera es
    un crédito de cortesía — preferible a quemar análisis free por URLs rotas."""
    if not charge or not charge.get("kind"):
        return
    kind = charge["kind"]
    try:
        if kind == "monthly" and user_id:
            prof = db.table("profiles").select("monthly_usage").eq("id", user_id).single().execute().data or {}
            db.table("profiles").update(
                {"monthly_usage": max(0, (prof.get("monthly_usage") or 0) - 1)}
            ).eq("id", user_id).execute()
        elif kind == "free_analysis" and user_id:
            prof = db.table("profiles").select("free_analysis_uses").eq("id", user_id).single().execute().data or {}
            db.table("profiles").update(
                {"free_analysis_uses": max(0, (prof.get("free_analysis_uses") or 0) - 1)}
            ).eq("id", user_id).execute()
        elif kind == "credits" and user_id:
            prof = db.table("profiles").select("credits_cents").eq("id", user_id).single().execute().data or {}
            db.table("profiles").update(
                {"credits_cents": (prof.get("credits_cents") or 0) + (charge.get("cents") or 0)}
            ).eq("id", user_id).execute()
        elif kind == "ip" and ip:
            row = db.table("ip_usage").select("used_today").eq("ip", ip).single().execute().data or {}
            db.table("ip_usage").update(
                {"used_today": max(0, (row.get("used_today") or 0) - 1)}
            ).eq("ip", ip).execute()
        logger.info("transcribe refund ok kind=%s user=%s ip=%s", kind, user_id, ip)
    except Exception as e:
        logger.error("transcribe refund FAILED kind=%s user=%s ip=%s err=%s", kind, user_id, ip, e)


@celery_app.task(bind=True)
def transcribe_task(self, url, language, user_id, ip, is_paid=False, charge=None):
    """v0.14.7: is_paid=True (plan pro/creator/agency) → guarda métricas
    Apify (views/likes/comments/shares/published_at) en la fila.
    addreel: `charge` = lo que /transcribe cobró por adelantado
    ({kind: monthly|free_analysis|credits|ip, cents}) — si la descarga o la
    transcripción fallan, se REEMBOLSA aquí (antes una URL rota quemaba 1 de
    los 3 análisis free de por vida). Default None → compat con tasks en vuelo."""
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    COST_CENTS = 8

    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    platform = detect_platform(url)

    self.update_state(state="PROGRESS", meta={"step": "Descargando audio..."})

    thumbnail_url = None
    apify_item = None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path, thumbnail_url, apify_item = download_audio(url, tmpdir, platform)

            self.update_state(state="PROGRESS", meta={"step": "Transcribiendo con IA..."})

            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            with open(audio_path, "rb") as f:
                files = {"file": ("audio.mp3", f, "audio/mpeg")}
                data = {"model": "whisper-large-v3", "response_format": "json"}
                if language:
                    data["language"] = language
                resp = requests.post(GROQ_URL, headers=headers, files=files, data=data, timeout=120)
                resp.raise_for_status()
                text = resp.json()["text"]

    except Exception as e:
        _refund_transcribe_charge(db, user_id, ip, charge)
        # B3: el usuario nunca ve el error crudo de yt-dlp/Apify en su 1ª acción.
        if isinstance(e, DownloadError) and e.code == "rate_limit":
            msg = ("Instagram va saturado ahora mismo. No te hemos cobrado — "
                   "prueba de nuevo en un par de minutos.")
        elif isinstance(e, DownloadError):
            msg = ("No pude descargar ese reel (puede ser privado o no estar "
                   "disponible). No te hemos cobrado — revisa el enlace o prueba otro.")
        else:
            msg = ("No pude procesar ese reel ahora mismo. No te hemos cobrado — "
                   "inténtalo de nuevo en un momento.")
        logger.warning("transcribe_task download failed url=%s err=%s", url, str(e)[:120])
        return {"ok": False, "error": msg}

    thumb_b64 = None
    try:
        thumb_b64 = _download_thumbnail_b64(thumbnail_url)
    except Exception as e:
        logger.warning("Thumbnail capture failed for %s: %s", url, e)

    # Autor del reel analizado → permite "añadir como competidor" desde el resultado.
    # IG: ownerUsername del scrape Apify. TikTok: se extrae del path de la URL
    # (tiktok.com/@usuario/…). Los short-links (vm./vt.) no lo llevan → None.
    author_username = None
    if apify_item:
        author_username = (apify_item.get("ownerUsername") or "").strip().lstrip("@").lower() or None
    if not author_username and platform == "tiktok":
        m = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", url)
        if m:
            author_username = m.group(1).lower()

    insert_data = {
        "user_id": user_id,
        "ip": ip if not user_id else None,
        "url": url,
        "platform": platform,
        "language": language,
        "text": text,
        "cost_cents": COST_CENTS if user_id else 0,
        "thumbnail_b64": thumb_b64,
        "author_username": author_username,
    }
    # v0.14.7: métricas solo para paid plans con datos Apify reales (Instagram).
    if is_paid and apify_item and platform == "instagram":
        insert_data.update(_extract_metrics(apify_item))

    db.table("transcriptions").insert(insert_data).execute()

    return {"ok": True, "text": text, "platform": platform, "username": author_username}


@celery_app.task(bind=True)
def refresh_metrics_bulk(self, user_id, tid_list):
    """Refresca métricas Apify para una lista de IDs de transcripciones.
    Solo Instagram. Sequential con sleep 0.5s entre llamadas para no
    saturar Apify."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
    if not APIFY_TOKEN:
        return {"ok": False, "reason": "no_apify_token", "updated": 0}

    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        rows_r = (db.table("transcriptions")
                    .select("id, url, platform")
                    .in_("id", tid_list)
                    .eq("user_id", user_id)
                    .execute())
        rows = [r for r in (rows_r.data or []) if r.get("platform") == "instagram"]
    except Exception as e:
        logger.warning("refresh_metrics_bulk fetch failed: %s", e)
        return {"ok": False, "updated": 0}

    updated = 0
    for r in rows:
        try:
            item = _apify_metrics_only(r["url"])
            if not item:
                continue
            metrics = _extract_metrics(item)
            (db.table("transcriptions")
               .update(metrics)
               .eq("id", r["id"])
               .eq("user_id", user_id)
               .execute())
            updated += 1
            self.update_state(state="PROGRESS", meta={"updated": updated, "total": len(rows)})
        except Exception as e:
            logger.warning("refresh_metrics_bulk row %s failed: %s", r.get("id"), e)
        time.sleep(0.5)

    return {"ok": True, "updated": updated, "total": len(rows)}


# ── v0.15.2.a: scrape tracked competitors (async) ────────────────────────────
# Movido desde app.py para no bloquear workers HTTP de Flask. Patrón cliente
# Supabase local + env vars vía os.environ (mismo que transcribe_task).
# Lógica de negocio idéntica a la versión sync previa: anti-race lock +
# Apify run-sync-get-dataset-items + UPSERT reels + UPDATE final.
# DEUDA PRIORITARIA v0.15.4: si el worker muere mid-task, scrape_status queda
# 'scraping' permanente y el anti-race bloquea re-scrape para siempre. Fix:
# guard "stale" = si last_scraped_at < now() - 10min con status='scraping',
# considerar abandonado y permitir re-encolar.
SCRAPE_TIMEOUT_SEC = 240


@celery_app.task(name="tasks.scrape_creator")
def scrape_creator_task(creator_id: str) -> dict:
    """Scrape async de reels de un creator. Encolada desde POST /admin/scrape.

    Returns dict con:
      - status: "ok" | "failed" | "private" | "not_found" | "in_progress" | "creator_not_found"
      - reels_count: int (solo si status=ok)
      - error: str (solo si status=failed)
      - creator_id, ig_username (echo).
    """
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    # 1. Cargar creator.
    try:
        cr = (db.table("creators_global")
                .select("id, ig_username, scrape_status")
                .eq("id", creator_id)
                .single()
                .execute())
        creator = cr.data
    except Exception:
        creator = None
    if not creator:
        logger.warning("scrape_creator: creator_not_found id=%s", creator_id)
        return {"status": "creator_not_found", "creator_id": creator_id}

    ig_username = creator["ig_username"]
    logger.info("scrape_creator started for %s (creator_id=%s)", ig_username, creator_id)

    # 2. Anti-race: UPDATE scrape_status='scraping' WHERE != 'scraping'.
    lock = (db.table("creators_global")
              .update({"scrape_status": "scraping", "last_error": None})
              .eq("id", creator_id)
              .neq("scrape_status", "scraping")
              .execute())
    if not lock.data:
        logger.info("scrape_creator skip (in_progress) for %s", ig_username)
        return {"status": "in_progress", "creator_id": creator_id, "ig_username": ig_username}

    # 3. Llamada Apify sync (timeout 240s; server-side limit Apify ~300s).
    # v0.15.2.b: memory 512→1024 (default oficial del actor apify/instagram-reel-scraper).
    actor_url = (
        f"https://api.apify.com/v2/acts/xMc5Ga1oCONPmWJIa"
        f"/run-sync-get-dataset-items?token={APIFY_TOKEN}&memory=1024"
    )
    payload = {
        "username": [ig_username],
        "resultsLimit": 10,
        "includeSharesCount": True,
    }

    final_status = "ok"
    last_error = None
    items: list = []
    try:
        # v0.15.2.b: logging Apify request/response (cierra laguna observabilidad).
        logger.info("[scrape] Apify request — url: %s, payload: %s",
                    actor_url.split("?")[0], payload)
        resp = requests.post(actor_url, json=payload, timeout=SCRAPE_TIMEOUT_SEC)
        logger.info("[scrape] Apify response — status: %d, body: %s",
                    resp.status_code, (resp.text or "")[:500])
        if resp.status_code == 404:
            final_status = "not_found"
        elif resp.status_code >= 400:
            body_lc = (resp.text or "").lower()
            if "private" in body_lc:
                final_status = "private"
            else:
                final_status = "failed"
                last_error = f"http_{resp.status_code}: {(resp.text or '')[:300]}"
        else:
            items = resp.json() or []
            if items and isinstance(items[0], dict):
                first = items[0]
                # v0.15.2.b: concatenar error + errorDescription. Caso real:
                # pedrocavadas devuelve error="no_items" pero errorDescription
                # incluye "private" — antes caía a 'failed', ahora a 'private'.
                err = first.get("error") or ""
                err_desc = first.get("errorDescription") or ""
                combined_lc = (str(err) + " " + str(err_desc)).lower()
                if first.get("error"):
                    if "private" in combined_lc:
                        final_status = "private"
                        items = []
                    elif "not found" in combined_lc or "not_found" in combined_lc:
                        final_status = "not_found"
                        items = []
                    else:
                        final_status = "failed"
                        last_error = (
                            (str(err) + " — " + str(err_desc))[:300] if err_desc else str(err)[:300]
                        )
                        items = []
                elif first.get("ownerIsPrivate") is True:
                    final_status = "private"
                    items = []
    except requests.Timeout:
        logger.warning("scrape_creator timeout for %s after %ds", ig_username, SCRAPE_TIMEOUT_SEC)
        final_status = "failed"
        last_error = "timeout"
    except Exception as e:
        logger.exception("scrape_creator apify call failed for %s: %s", ig_username, e)
        final_status = "failed"
        last_error = str(e)[:300]

    # 4. UPSERT reels si tenemos items y status ok.
    reels_count = 0
    if final_status == "ok" and items:
        rows = []
        for item in items:
            sc = item.get("shortCode") or item.get("id")
            if not sc:
                continue
            # v0.15.3.a: descarga thumb + b64 (URL IG expira/bloquea hotlink).
            # Reusa helper _download_thumbnail_b64 ya disponible en tasks.py:17
            # (mismo helper que transcribe_task). En serie (~1-3s × 10 reels);
            # paralelizar si se detecta lentitud (deuda anotada).
            display_url = item.get("displayUrl")
            rows.append({
                "creator_id": creator_id,
                "ig_reel_id": sc,
                "caption": (item.get("caption") or "")[:2000],
                "views": item.get("videoPlayCount") or item.get("videoViewCount") or 0,
                "likes": item.get("likesCount") or 0,
                "comments": item.get("commentsCount") or 0,
                "posted_at": item.get("timestamp"),
                "thumb_url": display_url,
                "thumb_b64": _download_thumbnail_b64(display_url),
                "video_url": item.get("videoUrl"),
                "video_duration_sec": item.get("videoDuration"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })
        if rows:
            try:
                db.table("creator_reels_global").upsert(
                    rows, on_conflict="creator_id,ig_reel_id"
                ).execute()
                reels_count = len(rows)
            except Exception as e:
                logger.exception("scrape_creator upsert failed for %s: %s", ig_username, e)
                final_status = "failed"
                last_error = f"upsert: {str(e)[:200]}"

    # 5. UPDATE final del creator.
    update_payload = {
        "scrape_status": final_status,
        "last_scraped_at": datetime.now(timezone.utc).isoformat(),
        "last_error": last_error,
    }
    try:
        db.table("creators_global").update(update_payload).eq("id", creator_id).execute()
    except Exception as e:
        logger.exception("scrape_creator final update failed for %s: %s", ig_username, e)

    # Log final con detalle según status.
    if final_status == "ok":
        logger.info("scrape_creator done for %s: status=ok reels=%d", ig_username, reels_count)
    elif final_status in ("private", "not_found"):
        logger.warning("scrape_creator %s: %s", ig_username, final_status)
    else:
        logger.warning("scrape_creator %s: %s (%s)", ig_username, final_status, last_error or "no detail")

    out = {"status": final_status, "creator_id": creator_id, "ig_username": ig_username}
    if final_status == "ok":
        out["reels_count"] = reels_count
    if last_error:
        out["error"] = last_error
    return out


# ── Fathom 18/06: NOVEDAD DIARIA + AUTO-SCRAPE del perfil propio ─────────────
# Beat re-scrapea a diario los competidores SEGUIDOS (radar con reels nuevos cada
# día = hábito) y 2×/semana el perfil PROPIO de cada usuario (detecta lo publicado
# → sube nivel del Cerebro sin que el usuario haga nada). Ambas RESPETAN staleness
# y CAPAN el nº de scrapes por corrida para no disparar el coste de Apify.
_RADAR_DAILY_CAP = int(os.environ.get("RADAR_DAILY_SCRAPE_CAP", "400"))
_USER_SCRAPE_CAP = int(os.environ.get("USER_PROFILE_SCRAPE_CAP", "400"))


def _enqueue_if_stale(db, creator_ids, stale_hours, cap):
    """Encola scrape_creator_task para los creadores stale (>stale_hours) y no
    privados/inexistentes. Una sola lectura a creators_global (.in_) + filtro en
    memoria. Devuelve (queued, candidates)."""
    creator_ids = [c for c in dict.fromkeys(creator_ids) if c]  # únicos, orden estable
    if not creator_ids:
        return 0, 0
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).isoformat()
    queued = 0
    # Supabase limita el tamaño de .in_(); troceamos de 100 en 100.
    rows = []
    for i in range(0, len(creator_ids), 100):
        chunk = creator_ids[i:i + 100]
        try:
            r = (db.table("creators_global")
                   .select("id, scrape_status, last_scraped_at")
                   .in_("id", chunk).execute())
            rows.extend(r.data or [])
        except Exception:
            logger.exception("refresh: creators_global read failed (chunk)")
    for cr in rows:
        if queued >= cap:
            break
        if cr.get("scrape_status") in ("private", "not_found", "scraping"):
            continue
        last = cr.get("last_scraped_at")
        if last and str(last) > cutoff:   # fresco → skip (ISO comparable lexicográficamente)
            continue
        try:
            scrape_creator_task.delay(cr["id"])
            queued += 1
        except Exception:
            logger.exception("refresh: enqueue failed for %s", cr.get("id"))
    return queued, len(creator_ids)


@celery_app.task(name="tasks.refresh_radar_daily")
def refresh_radar_daily():
    """NOVEDAD DIARIA (Fathom 18/06): re-scrapea a diario los competidores que alguien
    sigue (stale >20h) → cada día hay reels nuevos en el radar. Capado a _RADAR_DAILY_CAP."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        tr = (db.table("user_tracked_creators").select("creator_id")
                .is_("archived_at", "null").limit(5000).execute())
        creator_ids = [r["creator_id"] for r in (tr.data or []) if r.get("creator_id")]
    except Exception:
        logger.exception("refresh_radar_daily: tracked read failed")
        return {"status": "error"}
    queued, cand = _enqueue_if_stale(db, creator_ids, stale_hours=20, cap=_RADAR_DAILY_CAP)
    logger.info("refresh_radar_daily: queued=%d candidates=%d", queued, cand)
    return {"queued": queued, "candidates": cand}


@celery_app.task(name="tasks.scrape_user_profiles")
def scrape_user_profiles():
    """AUTO-SCRAPE 2×/semana (Fathom 18/06): scrapea el perfil PROPIO de cada usuario
    (de ig_profiles) → detecta reels recién publicados → sube nivel del Cerebro solo.
    Asegura que el handle esté en creators_global. Capado a _USER_SCRAPE_CAP."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        pr = db.table("ig_profiles").select("ig_username").limit(5000).execute()
        usernames = sorted({(p.get("ig_username") or "").strip().lstrip("@").lower()
                            for p in (pr.data or []) if p.get("ig_username")})
    except Exception:
        logger.exception("scrape_user_profiles: ig_profiles read failed")
        return {"status": "error"}
    if not usernames:
        return {"queued": 0, "candidates": 0}
    # Asegura/recoge los creator_id de esos handles (upsert idempotente).
    creator_ids = []
    for uname in usernames:
        try:
            ins = (db.table("creators_global")
                     .upsert({"ig_username": uname}, on_conflict="ig_username").execute())
            row = (ins.data or [None])[0]
            if not row:
                sel = db.table("creators_global").select("id").eq("ig_username", uname).single().execute()
                row = sel.data
            if row and row.get("id"):
                creator_ids.append(row["id"])
        except Exception:
            logger.exception("scrape_user_profiles: upsert failed for %s", uname)
    # Stale 60h: el perfil propio se refresca como mucho ~cada 2,5 días (2×/sem).
    queued, cand = _enqueue_if_stale(db, creator_ids, stale_hours=60, cap=_USER_SCRAPE_CAP)
    logger.info("scrape_user_profiles: queued=%d candidates=%d", queued, cand)
    return {"queued": queued, "candidates": cand}


@celery_app.task(name="tasks.send_train_hooks_nudges")
def send_train_hooks_nudges():
    """Beat DIARIO (Fathom 18/06): alerta "tu ejercicio del Cerebro está listo" a usuarios
    ONBOARDED que NO han hecho el ejercicio de hoy. Idempotente por día (dentro de
    send_train_hooks_nudge) + filtro brain_exercise_date != hoy para no avisar al que ya lo hizo."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {"error": "supabase_not_configured"}
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        from emails import send_train_hooks_nudge
    except Exception as e:
        logger.error("send_train_hooks_nudges: import emails failed: %s", e)
        return {"error": "import_emails"}
    today = datetime.now(timezone.utc).date().isoformat()
    # Onboarded = tienen % de Cerebro sembrado. No avisar si ya hicieron el de hoy.
    try:
        profs = (db.table("profiles").select("id, brain_progress, brain_exercise_date")
                   .not_.is_("brain_progress", "null").limit(20000).execute()).data or []
    except Exception as e:
        logger.error("send_train_hooks_nudges: profiles query failed: %s", e)
        return {"error": "profiles_query"}
    sent = skipped = 0
    for p in profs:
        uid = p.get("id")
        if not uid or str(p.get("brain_exercise_date") or "") == today:
            skipped += 1
            continue
        try:
            r = send_train_hooks_nudge(uid, today)
            sent += 1 if r.get("sent") else 0
            skipped += 0 if r.get("sent") else 1
        except Exception:
            logger.exception("send_train_hooks_nudges: send failed user=%s", uid)
            skipped += 1
    logger.info("send_train_hooks_nudges (daily brain exercise): sent=%d skipped=%d", sent, skipped)
    return {"users": len(profs), "sent": sent, "skipped": skipped}


# ── v0.15.5: generar guion desde reel de competidor (async) ──────────────────
# Patrón: la task se encarga del flujo completo cuando el endpoint detecta
# cache miss (transcript falta/failed/stale). Pasos:
#   1. Anti-race lock con stale guard 15min (transcript_started_at).
#   2. Descargar audio del reel vía pipeline existente (download_audio).
#   3. Whisper transcribir → UPDATE creator_reels_global.transcript.
#   4. Lazy import adapt_with_ai (rompe circular tasks↔app).
#   5. Generar guion + INSERT scripts con from_competitor_*.
#   6. Cobrar 1 unit monthly_usage / 18¢ free (sin refund — la task ya
#      garantiza no cobrar si LLM/insert falla, igual que el endpoint sync).
# DEUDA v0.15.6: sweeper periódico para liberar locks 'transcribing' viejos
# sin esperar a que un user trigger la task de nuevo.

_TRANSCRIBE_STALE_MIN = 15
_TRANSCRIBE_POLL_MAX_SEC = 90
_BUILTIN_SCRIPT_STYLES_LOCAL = {"viral", "divertido", "storytelling", "hooks"}


@celery_app.task(bind=True, name="tasks.generate_script_competitor")
def generate_script_competitor_task(self, reel_id, user_id, assistant_id, language=None):
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
    SCRIPT_COST = 18
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    def _fail(error, message):
        logger.warning("gen_script_task reel=%s user=%s fail: %s", reel_id, user_id, error)
        return {"ok": False, "error": error, "message": message}

    # v0.15.8: liberar lock siempre (try/finally envolvente). Cubre éxito,
    # fail controlado, _fail() retornado y excepciones no capturadas (worker
    # OOM, kill). El sweeper periódico cubre solo el caso 'task murió antes
    # del finally' — try/finally protege todos los demás.
    def _release_lock():
        try:
            (db.table("script_generation_locks").delete()
               .eq("user_id", user_id).eq("reel_id", reel_id).execute())
        except Exception as _e:
            logger.warning("gen_script_task release_lock failed reel=%s user=%s: %s", reel_id, user_id, _e)

    try:
        # 1. Cargar reel.
        try:
            rr = (db.table("creator_reels_global")
                    .select("id, ig_reel_id, caption, transcript, transcript_status, "
                            "transcript_started_at, "
                            "creator:creators_global(ig_username)")
                    .eq("id", reel_id)
                    .single()
                    .execute())
            reel = rr.data
        except Exception:
            reel = None
        if not reel:
            return _fail("reel_not_found", "Reel no encontrado.")
        ig_username = (reel.get("creator") or {}).get("ig_username") or ""

        # 2. Cargar profile del user. Incluye los campos que necesita la MISMA
        # contabilidad que el endpoint sync (trial + free mensual), no solo plan.
        try:
            pr = (db.table("profiles")
                    .select("plan, monthly_usage, credits_cents, default_idea_assistant, "
                            "stripe_subscription_id, trial_ends_at, free_lifetime_uses, free_month_reset_at")
                    .eq("id", user_id)
                    .single()
                    .execute())
            profile = pr.data or {}
        except Exception:
            profile = {}
        plan = profile.get("plan", "free")
        # FIX free-counter: alinear el cobro async con el sync. Antes la task
        # gateaba por plan in (pro/creator/agency) y cobraba créditos a los free
        # → NO tocaba free_lifetime_uses (la pill no bajaba) y dejaba créditos en
        # negativo; el trial tampoco contaba contra su tope. Ahora usa los mismos
        # helpers que el endpoint.
        try:
            from app import (paid_features_active as _pfa, free_lifetime_left as _fll,
                             _next_month_boundary as _nmb, _parse_ts as _pts)  # lazy (circular)
        except Exception:
            _pfa = _fll = _nmb = _pts = None
        is_paid_unlimited = _pfa(profile) if _pfa else (plan in ("pro", "creator", "agency"))

        self.update_state(state="PROGRESS", meta={"step": "preparing"})

        # 3. Anti-race + stale guard.
        transcript_text = (reel.get("transcript") or "").strip()
        transcript_status = reel.get("transcript_status")

        if transcript_status == "ok" and transcript_text:
            pass  # Otra task ya transcribió mientras encolábamos. Salto a guion.
        else:
            is_stale = False
            if transcript_status == "transcribing":
                started_at = reel.get("transcript_started_at")
                if started_at:
                    try:
                        started_dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                        age_min = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60.0
                        is_stale = age_min > _TRANSCRIBE_STALE_MIN
                    except Exception:
                        is_stale = True
                else:
                    is_stale = True

            if transcript_status != "transcribing" or is_stale:
                # Adquirir lock (UPDATE simple — race trivial aceptable).
                now_iso = datetime.now(timezone.utc).isoformat()
                try:
                    db.table("creator_reels_global").update({
                        "transcript_status": "transcribing",
                        "transcript_started_at": now_iso,
                        "transcript_error": None,
                    }).eq("id", reel_id).execute()
                except Exception as e:
                    logger.exception("gen_script_task lock UPDATE failed reel=%s: %s", reel_id, e)
                    return _fail("db_error", "Error preparando la transcripción.")
                transcript_text = ""
            else:
                # Otra task fresca está transcribiendo → poll BD hasta 'ok' o 'failed'.
                self.update_state(state="PROGRESS", meta={"step": "waiting_other"})
                polled = False
                for _ in range(_TRANSCRIBE_POLL_MAX_SEC // 5):
                    time.sleep(5)
                    try:
                        re_r = (db.table("creator_reels_global")
                                  .select("transcript, transcript_status")
                                  .eq("id", reel_id)
                                  .single()
                                  .execute())
                        if re_r.data and re_r.data.get("transcript_status") == "ok":
                            transcript_text = (re_r.data.get("transcript") or "").strip()
                            polled = True
                            break
                        if re_r.data and re_r.data.get("transcript_status") == "failed":
                            return _fail("transcribe_failed", "No se pudo procesar este reel.")
                    except Exception:
                        pass
                if not polled:
                    return _fail("transcribe_timeout", "La transcripción tardó demasiado. Inténtalo de nuevo.")

            # Si tenemos el lock, transcribir.
            if not transcript_text:
                self.update_state(state="PROGRESS", meta={"step": "transcribing"})
                url = "https://www.instagram.com/reel/{}/".format(reel["ig_reel_id"])
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        audio_path, _thumb, _apify_item = download_audio(url, tmpdir, "instagram")
                        headers = {"Authorization": "Bearer " + GROQ_API_KEY}
                        with open(audio_path, "rb") as f:
                            files = {"file": ("audio.mp3", f, "audio/mpeg")}
                            data = {"model": "whisper-large-v3", "response_format": "json"}
                            resp = requests.post(GROQ_URL, headers=headers, files=files, data=data, timeout=120)
                            resp.raise_for_status()
                            transcript_text = (resp.json().get("text") or "").strip()
                except Exception as e:
                    err_msg = str(e)[:300]
                    logger.exception("gen_script_task transcribe failed reel=%s url=%s: %s", reel_id, url, e)
                    try:
                        db.table("creator_reels_global").update({
                            "transcript_status": "failed",
                            "transcript_error": err_msg,
                        }).eq("id", reel_id).execute()
                    except Exception:
                        pass
                    return _fail("transcribe_error", "No se pudo procesar este reel.")

                # Guardar transcript en cache.
                try:
                    db.table("creator_reels_global").update({
                        "transcript": transcript_text,
                        "transcript_status": "ok",
                        "transcript_error": None,
                    }).eq("id", reel_id).execute()
                except Exception as e:
                    logger.exception("gen_script_task save transcript failed reel=%s: %s", reel_id, e)

        # 4. Generar guion vía adapt_with_ai (lazy import).
        self.update_state(state="PROGRESS", meta={"step": "generating_script"})
        today_str = datetime.now(timezone.utc).strftime("%-d de %B de %Y")
        caption = (reel.get("caption") or "").strip()

        if not transcript_text:
            transcript_block = "Sin transcripción disponible; usa solo el caption."
        else:
            transcript_block = transcript_text

        user_content = (
            "[Reel de un competidor del usuario · @" + ig_username + "]\n\n"
            "Caption del reel:\n" + (caption or "(sin caption)") + "\n\n"
            "Transcripción del audio del reel (lo que el creador realmente dice):\n"
            + transcript_block + "\n\n"
            "Fecha actual: " + today_str + ". Cualquier modelo, herramienta, versión, "
            "empresa o producto que aparezca en el caption o la transcripción es "
            "REAL y ACTUAL aunque no lo conozcas de tu entrenamiento — úsalo tal "
            "cual, NO lo sustituyas por una versión que te resulte más familiar. "
            "Confiar en el reel sobre qué existe ahora es regla NO negociable.\n\n"
            "Tarea: este es un reel de un competidor del usuario. Genera un guion "
            "completo de 30-45 segundos hablados para que el usuario grabe SOBRE "
            "EL MISMO TEMA que este reel. Reescribe el hook con tus palabras (NO "
            "copies palabra por palabra el del competidor), reescribe el "
            "desarrollo y los ejemplos con un enfoque propio. La diferencia con "
            "el competidor está en la EJECUCIÓN, no en el tema. Respeta "
            "exactamente los nombres, versiones y herramientas que aparecen en "
            "el reel.\n\n"
            # B: «copia lo que funciona» = preservar lo que hace funcionar al
            # original (espejo del builder sync en app.py).
            "PRESERVA lo que hace funcionar al original — regla NO negociable: "
            "(a) las cifras, nombres, comparaciones y el ángulo CONCRETO del reel "
            "se mantienen (traducidos a otras palabras, no sustituidos por "
            "generalidades); (b) el REGISTRO también se mantiene: si el original "
            "es humor/sátira/diálogo, el guion resultante es humor/sátira/diálogo "
            "— no lo conviertas en consejo serio ni motivacional; (c) prohibido "
            "inventar anécdotas o logros propios del usuario ('mi equipo hizo X') "
            "— los ejemplos salen del reel o son claramente hipotéticos. Si el "
            "guion final pierde la especificidad del original y podría valer para "
            "cualquier nicho, está mal: reescríbelo.\n\n"
            "Total: 100-140 palabras, mínimo 8 frases en body."
        )
        # P1 idioma de salida: el guion sale en el idioma del usuario (espejo del builder sync).
        try:
            from app import _out_lang_instruction  # lazy (rompe circular)
            user_content += _out_lang_instruction(language)
        except Exception:
            pass

        # Resolver style + custom_prompt.
        style_arg = "viral"
        custom_prompt = ""
        style_label = "viral"
        if assistant_id in _BUILTIN_SCRIPT_STYLES_LOCAL:
            style_arg = assistant_id
            style_label = assistant_id
        elif assistant_id:
            try:
                ar = (db.table("assistants")
                        .select("name, instructions")
                        .eq("id", assistant_id)
                        .eq("user_id", user_id)
                        .execute())
                if ar.data and ar.data[0].get("instructions"):
                    style_arg = "custom"
                    custom_prompt = ar.data[0]["instructions"]
                    style_label = ar.data[0].get("name") or "custom"
            except Exception:
                pass

        # P0-3: "hooks" produce 5 one-liners sueltos, no un guion — el camino
        # async guardaba esos hooks como guion. Degradar a viral (mismo patrón
        # que idea_scripts_generate_batch en app.py).
        if style_arg == "hooks":
            style_arg = style_label = "viral"

        # v0.15.7.b: cortar pre-LLM si custom prompt corto (sin cobrar). El
        # endpoint pre-valida también; este check es defense in depth — si la
        # task se encola con assistant_id válido y luego el user edita el
        # asistente a algo más corto, este guard cubre la race.
        _CUSTOM_MIN = 30
        if style_arg == "custom" and len((custom_prompt or "").strip()) < _CUSTOM_MIN:
            msg = (
                "Las instrucciones de tu asistente '" + style_label +
                "' son muy cortas (mínimo " + str(_CUSTOM_MIN) +
                " caracteres). Edítalas en Asistentes."
            )
            return _fail("assistant_too_short", msg)

        try:
            from app import adapt_with_ai, get_voice_profile  # lazy import (rompe circular tasks↔app).
            result = adapt_with_ai(user_content, style_arg, custom_prompt,
                                   voice=get_voice_profile(user_id), user_id=user_id)  # moat: voz + few-shot
        except Exception as e:
            logger.exception("gen_script_task LLM failed reel=%s: %s", reel_id, e)
            # v0.15.7.b: mensaje contextual si custom + empty content.
            if style_arg == "custom" and "empty content" in str(e).lower():
                return _fail(
                    "assistant_empty_response",
                    "El asistente '" + style_label + "' devolvió respuesta vacía. "
                    "Edita sus instrucciones o usa otro estilo.",
                )
            return _fail("llm_error", "No se pudo generar el guion. Inténtalo de nuevo.")

        llm_title = ""
        if isinstance(result, dict) and result.get("title"):
            llm_title = str(result["title"]).strip()[:80]
        if isinstance(result, dict) and "hook" in result:
            flat = (result["hook"] + "\n" +
                    "\n".join(result.get("body", [])) + "\n" +
                    result.get("closing", ""))
            result = flat.strip()
        elif isinstance(result, dict) and isinstance(result.get("hooks"), list):
            result = "\n".join(h.get("text", "") for h in result["hooks"]
                               if isinstance(h, dict) and h.get("text")).strip()
        elif not isinstance(result, str):
            result = str(result)

        today_short = datetime.now(timezone.utc).strftime("%d %b %Y").lower()
        script_title = llm_title or ("Guion desde @" + ig_username + " · " + today_short)

        # 5. Guard anti doble-cobro pre-INSERT: cierra la ventana async-async
        # (otra task del mismo user+reel ya insertó hace <60s mientras esta
        # generaba). Aborta SIN cobrar SIN insertar → 1 script, 1 cobro.
        try:
            _dup_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            dup_r = (db.table("scripts")
                       .select("id, from_competitor_username")
                       .eq("user_id", user_id)
                       .eq("from_competitor_reel_id", reel["id"])
                       .gte("created_at", _dup_cutoff)
                       .order("created_at", desc=True)
                       .limit(1)
                       .execute())
            if dup_r.data:
                existing = dup_r.data[0]
                logger.info("gen_script_task duplicate skip reel=%s user=%s existing=%s",
                            reel_id, user_id, existing.get("id"))
                return {
                    "ok": True,
                    "duplicate": True,
                    "script_id": existing.get("id"),
                    "title": None,
                    "from_competitor_username": existing.get("from_competitor_username"),
                }
        except Exception as e:
            logger.warning("gen_script_task pre-insert dup check failed reel=%s: %s", reel_id, e)

        # 6. INSERT scripts + cobrar.
        script_id = None
        try:
            _TASK_LABELS = {
                "viral": "Viral", "divertido": "Divertido", "hooks": "Hooks",
                "storytelling": "Storytelling", "story": "Storytelling", "linkedin": "LinkedIn",
            }
            ins = db.table("scripts").insert({
                "user_id":                user_id,
                "transcription_id":       None,
                "idea_id":                None,
                "title":                  script_title,
                "script":                 result,
                "project_id":             None,
                "from_competitor_reel_id": reel["id"],
                "from_competitor_username": ig_username,
                "assistant_name":         _TASK_LABELS.get(style_label, style_label) if style_label else None,
            }).execute()
            if ins.data:
                script_id = ins.data[0].get("id")
        except Exception as e:
            logger.exception("gen_script_task scripts insert failed reel=%s: %s", reel_id, e)
            return _fail("insert_error", "Error guardando el guion.")

        # Cobrar SOLO tras insert OK. Mismo orden que el endpoint sync:
        #   pago/trial → monthly_usage · free con cuota del mes → free_lifetime_uses
        #   (reset mensual) · resto → créditos.
        try:
            if is_paid_unlimited:
                db.table("profiles").update({
                    "monthly_usage": (profile.get("monthly_usage") or 0) + 1
                }).eq("id", user_id).execute()
            elif _fll and _fll(profile) > 0:
                # Consumo del free MENSUAL (espejo de app._free_month_consume) con
                # la db de la task: reset si el boundary pasó, luego +1.
                now = datetime.now(timezone.utc)
                reset_dt = _pts(profile.get("free_month_reset_at")) if _pts else None
                rolled = reset_dt is None or now >= reset_dt
                if rolled and _nmb:
                    db.table("profiles").update({
                        "free_analysis_uses": 0, "free_lifetime_uses": 1,
                        "free_month_reset_at": _nmb(now).isoformat(),
                    }).eq("id", user_id).execute()
                else:
                    db.table("profiles").update({
                        "free_lifetime_uses": (profile.get("free_lifetime_uses") or 0) + 1
                    }).eq("id", user_id).execute()
            else:
                db.table("profiles").update({
                    "credits_cents": (profile.get("credits_cents") or 0) - SCRIPT_COST
                }).eq("id", user_id).execute()
        except Exception as e:
            logger.error("gen_script_task charge failed reel=%s user=%s: %s", reel_id, user_id, e)

        logger.info("gen_script_task ok reel=%s user=%s script=%s style=%s",
                    reel_id, user_id, script_id, style_label)

        # growth-1: activación — first_script_generated si es el 1º (path async).
        try:
            from emails import track_script_generated
            track_script_generated(user_id, {"source": "competitor_reel", "mode": "async", "style": style_label})
        except Exception:
            pass

        return {
            "ok": True,
            "script_id": script_id,
            "title": script_title,
            "from_competitor_username": ig_username,
        }
    finally:
        _release_lock()


# ── v0.15.8: sweeper periódico de recursos stale ─────────────────────────────
@celery_app.task(bind=True, name="tasks.transcribe_reel")
def transcribe_reel_task(self, reel_id):
    """Transcribe-only de un reel de competidor (detalle del reel en la isla):
    misma caché y lock que generate_script_competitor_task
    (creator_reels_global.transcript/_status), pero SIN generar guion ni
    cobrar. Idempotente: si ya está 'ok' devuelve el cacheado; si otra task
    fresca lo está transcribiendo, no relanza (el front pollea el endpoint).
    El sweeper de stale (>15min) ya cubre los 'transcribing' huérfanos."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_URL_L = "https://api.groq.com/openai/v1/audio/transcriptions"
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    try:
        reel_r = (db.table("creator_reels_global")
                    .select("id, ig_reel_id, transcript, transcript_status, transcript_started_at")
                    .eq("id", reel_id).single().execute())
    except Exception as e:
        logger.exception("transcribe_reel load failed reel=%s: %s", reel_id, e)
        return {"ok": False, "error": "db_error"}
    reel = reel_r.data if reel_r else None
    if not reel:
        return {"ok": False, "error": "reel_not_found"}

    status = reel.get("transcript_status")
    cached = (reel.get("transcript") or "").strip()
    if status == "ok" and cached:
        return {"ok": True, "transcript": cached, "cached": True}

    # Lock fresco de otra task → no relanzar (el front pollea la BD vía endpoint).
    if status == "transcribing":
        started_at = reel.get("transcript_started_at")
        try:
            started_dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - started_dt).total_seconds() / 60.0 <= _TRANSCRIBE_STALE_MIN:
                return {"ok": True, "pending": True}
        except Exception:
            pass  # started_at ilegible → tratar como stale y retranscribir

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        db.table("creator_reels_global").update({
            "transcript_status": "transcribing",
            "transcript_started_at": now_iso,
            "transcript_error": None,
        }).eq("id", reel_id).execute()
    except Exception as e:
        logger.exception("transcribe_reel lock failed reel=%s: %s", reel_id, e)
        return {"ok": False, "error": "db_error"}

    url = "https://www.instagram.com/reel/{}/".format(reel["ig_reel_id"])
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path, _thumb, _apify_item = download_audio(url, tmpdir, "instagram")
            headers = {"Authorization": "Bearer " + GROQ_API_KEY}
            with open(audio_path, "rb") as f:
                files = {"file": ("audio.mp3", f, "audio/mpeg")}
                data = {"model": "whisper-large-v3", "response_format": "json"}
                resp = requests.post(GROQ_URL_L, headers=headers, files=files, data=data, timeout=120)
                resp.raise_for_status()
                transcript_text = (resp.json().get("text") or "").strip()
    except Exception as e:
        logger.exception("transcribe_reel transcribe failed reel=%s: %s", reel_id, e)
        try:
            db.table("creator_reels_global").update({
                "transcript_status": "failed",
                "transcript_error": str(e)[:300],
            }).eq("id", reel_id).execute()
        except Exception:
            pass
        return {"ok": False, "error": "transcribe_error"}

    try:
        db.table("creator_reels_global").update({
            "transcript": transcript_text,
            "transcript_status": "ok",
            "transcript_error": None,
        }).eq("id", reel_id).execute()
    except Exception as e:
        logger.exception("transcribe_reel save failed reel=%s: %s", reel_id, e)
    return {"ok": True, "transcript": transcript_text}


# Limpia 2 estados huérfanos cada 5min vía Celery beat:
#   1. creator_reels_global.transcript_status='transcribing' >15min → 'failed'
#      con transcript_error='stale_timeout'. El próximo intento del user
#      re-encola limpio (código existente en generate_script_competitor_task
#      ya trata 'failed' como re-encolable).
#   2. script_generation_locks con started_at >10min → DELETE. Cubre el caso
#      "worker murió antes del try/finally que normalmente libera el lock".
#
# Un solo job (no separados): mismo timing, mismo overhead, queries
# independientes. Si en el futuro uno crece, se separan trivial.

_TRANSCRIBE_STALE_SWEEP_MIN = 15
_LOCK_STALE_SWEEP_MIN = 10


def _sweep_transcribing_stale(db):
    """Marca como failed los reels con transcript_status='transcribing' viejos."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_TRANSCRIBE_STALE_SWEEP_MIN)).isoformat()
    try:
        rows = (db.table("creator_reels_global")
                  .select("id")
                  .eq("transcript_status", "transcribing")
                  .lt("transcript_started_at", cutoff)
                  .execute())
    except Exception as e:
        logger.error("sweep_transcribing: select failed: %s", e, exc_info=True)
        return 0
    ids = [r["id"] for r in (rows.data or [])]
    if not ids:
        return 0
    try:
        (db.table("creator_reels_global")
           .update({
               "transcript_status": "failed",
               "transcript_error": "stale_timeout",
               "transcript_started_at": None,
           })
           .in_("id", ids)
           .execute())
    except Exception as e:
        logger.error("sweep_transcribing: update failed ids=%s: %s", ids, e, exc_info=True)
        return 0
    logger.info("sweep_transcribing: liberados=%d ids=%s", len(ids), ids)
    return len(ids)


def _sweep_generation_locks(db):
    """Borra locks de script_generation_locks con started_at > umbral.
    Cubre el caso 'worker murió antes del finally que libera el lock'."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_LOCK_STALE_SWEEP_MIN)).isoformat()
    try:
        rows = (db.table("script_generation_locks")
                  .select("user_id, reel_id")
                  .lt("started_at", cutoff)
                  .execute())
    except Exception as e:
        logger.error("sweep_locks: select failed: %s", e, exc_info=True)
        return 0
    pairs = [(r["user_id"], r["reel_id"]) for r in (rows.data or [])]
    if not pairs:
        return 0
    # DELETE por par (Supabase python no soporta DELETE WHERE en tupla compuesta).
    deleted = 0
    for uid, rid in pairs:
        try:
            (db.table("script_generation_locks").delete()
               .eq("user_id", uid).eq("reel_id", rid).execute())
            deleted += 1
        except Exception as e:
            logger.warning("sweep_locks: delete failed user=%s reel=%s: %s", uid, rid, e)
    logger.info("sweep_locks: liberados=%d", deleted)
    return deleted


@celery_app.task(name="tasks.sweep_stale_resources")
def sweep_stale_resources():
    """v0.15.8: beat cada 5min. Cierra deuda anotada desde v0.15.5
    (transcribing stale + locks huérfanos). Idempotente, best-effort."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    transc = _sweep_transcribing_stale(db)
    locks = _sweep_generation_locks(db)
    return {"transcribing_freed": transc, "locks_freed": locks}
