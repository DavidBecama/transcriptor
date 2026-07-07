import os
import re
import json
import time
import logging
import tempfile
from datetime import datetime, timedelta, timezone
import requests
import yt_dlp
from celery import Celery, chord, group
from celery.schedules import crontab
from dotenv import load_dotenv
from supabase import create_client

from niche_canon import pool_niche_canon, norm_tag

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
    # FASE 2 feed-vivo (04/07): pool de SUGERENCIAS vivo — re-scrape de creadores de nichos
    # activos que nadie sigue, máx 2×/semana por creador, capado. DORMIDO hasta que David
    # suba el cap de Apify: kill-switch RADAR_POOL_REFRESH_ENABLED (default OFF).
    "refresh-suggestion-pools": {
        "task": "tasks.refresh_suggestion_pools",
        "schedule": crontab(hour=5, minute=30),
    },
    # DEEP READ semanal de tracked (04/07): N=10 lunes 05:00 UTC (ANTES del diario de las
    # 06:00, que a N=4 los verá frescos y no duplica) → refresca vistas del baseline.
    "deep-refresh-tracked": {
        "task": "tasks.deep_refresh_tracked",
        "schedule": crontab(day_of_week=1, hour=5, minute=0),
    },
    # Avatares de «posibles competidores» (05/07): re-cachea fotos de perfil de creadores
    # de nichos activos 1×/mes (cambian raro; los bytes cacheados no caducan).
    "refresh-avatars-monthly": {
        "task": "tasks.refresh_avatars_monthly",
        "schedule": crontab(day_of_month=1, hour=4, minute=0),
    },
    # v1 feed diario: RE-RANK gratis del feed (CERO scrape) a las 07:00 UTC — DESPUÉS del
    # re-scrape de las 06:00, para calentar la caché con lo recién traído. Solo re-rankea.
    "rerank-radar-daily": {
        "task": "tasks.rerank_radar_daily",
        "schedule": crontab(hour=7, minute=0),
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
    # Fathom 18/06: 3 EMAILS DE CRECIMIENTO con métricas reales (subiste X% / a un
    # vídeo de superar a @Y / un creador de tu nicho petó). Semanal, miércoles 10:00
    # UTC (día distinto a los digests). 1 email por user/semana, prioridad interna.
    "send-growth-nudges": {
        "task": "tasks.send_growth_nudges",
        "schedule": crontab(day_of_week=3, hour=10, minute=0),
    },
    # Backfill del FORMATO de los reels scrapeados antes de la columna (formato IS NULL):
    # los clasifica por lotes (caption/transcript, flash) para que la tarjeta «FORMATO
    # SUGERIDO» tenga referencias del pool. Cada 12 min hasta vaciar; luego no-op.
    "backfill-reel-formats": {
        "task": "tasks.backfill_reel_formats",
        "schedule": crontab(minute="*/12"),
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

    # Diagnóstico (2026-06-25): incluimos los intentos (incl. el error REAL de Apify,
    # antes tragado) en el detalle → queda en creator_reels_global.transcript_error y
    # podemos ver por qué falló Apify (token ausente/401/402-créditos/actor) sin acceso
    # a los logs del worker. apify_avail=False → no aparece "apify#" = APIFY_TOKEN vacío.
    _att = "apify_avail=%s | %s" % (apify_avail, " | ".join(attempts[-5:]))
    logger.warning("download_audio agotado url=%s attempts=%s", url, _att)
    raise DownloadError("rate_limit" if rate_seen else "unavailable", (_att or str(last or ""))[:280])


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
def transcribe_task(self, url, language, user_id, ip, is_paid=False, charge=None, project_id=None):
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
                data = {"model": os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"), "response_format": "json"}
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
        "project_id": project_id,   # #5: «analizado por esta marca» → aislamiento en /history
    }
    # Métricas para TODOS los planes: el dato (views/likes/comments/shares) ya viene
    # en el `apify_item` de la propia transcripción (Instagram) → guardarlo es gratis y
    # es info pública = el núcleo del valor de «Analizar». (Antes capado a is_paid.)
    if apify_item and platform == "instagram":
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
# Fix deuda v0.15.4 (fase0 feed-vivo): el lock anti-race escribe last_scraped_at al
# ARRANCAR (semántica «último intento», el UPDATE final la reafirma al acabar) → un
# 'scraping' con último intento > SCRAPE_STUCK_MIN es un ZOMBI (worker muerto mid-task)
# y se permite takeover aquí + re-encolado en _enqueue_if_stale.
SCRAPE_TIMEOUT_SEC = 240
SCRAPE_STUCK_MIN = int(os.environ.get("SCRAPE_STUCK_MIN", "10"))
# SPEC-fase-accion-radar #4 (Fathom 18/06): pre-transcripción top-N al primer scrape.
# N=3 confirmado por David (presupuesto conservador). Ajustable por env.
_PRETRANSCRIBE_TOP_N = int(os.environ.get("PRETRANSCRIBE_TOP_N", "4"))


@celery_app.task(name="tasks.scrape_creator")
def scrape_creator_task(creator_id: str, results_limit: int = 10) -> dict:
    """Scrape async de reels de un creator. Encolada desde POST /admin/scrape.

    `results_limit` (David 04/07, ahorro Apify): reels a traer. Default 10 (primera
    pasada / refresh manual / onboarding → historia completa para fijar baseline). Los
    refrescos recurrentes lo bajan a 4 (nadie sube reels entre refrescos); el deep read
    semanal de tracked vuelve a 10 para refrescar vistas del baseline. Clamp [1, 50].

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
    # SPEC #4: ¿es el PRIMER scrape de este creador? (pre-transcribimos top-N solo
    # entonces, no en refrescos — acota el coste Groq). Estado previo al lock.
    was_first_scrape = (creator.get("scrape_status") in (None, "", "pending"))

    # 2. Anti-race: UPDATE scrape_status='scraping' WHERE != 'scraping'. Escribe también
    # last_scraped_at (=último INTENTO) → distingue un scrape vivo de un zombi.
    lock_fields = {"scrape_status": "scraping", "last_error": None,
                   "last_scraped_at": datetime.now(timezone.utc).isoformat()}
    lock = (db.table("creators_global")
              .update(lock_fields)
              .eq("id", creator_id)
              .neq("scrape_status", "scraping")
              .execute())
    if not lock.data:
        # TAKEOVER de zombi: 'scraping' con último intento hace > SCRAPE_STUCK_MIN → el
        # worker murió mid-task. El WHERE condicionado evita la carrera entre workers.
        stuck_iso = (datetime.now(timezone.utc)
                     - timedelta(minutes=SCRAPE_STUCK_MIN)).isoformat()
        lock = (db.table("creators_global")
                  .update(lock_fields)
                  .eq("id", creator_id)
                  .eq("scrape_status", "scraping")
                  .lt("last_scraped_at", stuck_iso)
                  .execute())
        if not lock.data:
            logger.info("scrape_creator skip (in_progress) for %s", ig_username)
            return {"status": "in_progress", "creator_id": creator_id, "ig_username": ig_username}
        logger.warning("scrape_creator: takeover de zombi 'scraping' para %s", ig_username)

    # 3. Llamada Apify sync (timeout 240s; server-side limit Apify ~300s).
    # v0.15.2.b: memory 512→1024 (default oficial del actor apify/instagram-reel-scraper).
    actor_url = (
        f"https://api.apify.com/v2/acts/xMc5Ga1oCONPmWJIa"
        f"/run-sync-get-dataset-items?token={APIFY_TOKEN}&memory=1024"
    )
    # SIN includeSharesCount (decisión David 04/07): el add-on cuesta $0.006/reel = 71% del
    # run ($0.084→$0.024/creador) y en cards de competidor el stat se oculta cuando es 0.
    # El scrape del PERFIL PROPIO (_scrape_ig_reels en app.py) SÍ lo mantiene: Métricas
    # (pestaña Compartidos + engagement) lo usa y su volumen es mínimo.
    try:
        _rl = max(1, min(50, int(results_limit)))
    except (TypeError, ValueError):
        _rl = 10
    payload = {
        "username": [ig_username],
        "resultsLimit": _rl,
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
    new_reels = 0   # net-new (David refresh-now): ig_reel_id que NO existían → «contenido nuevo»
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
                "shares": item.get("sharesCount") or 0,   # add-on shares OFF desde 04/07 → 0 en scrapes nuevos (la card lo oculta)
                "posted_at": item.get("timestamp"),
                "thumb_url": display_url,
                "thumb_b64": _download_thumbnail_b64(display_url),
                "video_url": item.get("videoUrl"),
                "video_duration_sec": item.get("videoDuration"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })
        if rows:
            # NET-NEW: el upsert re-escribe también los reels viejos (actualiza views), así
            # reels_count NO indica «trajo algo nuevo». Contamos los ig_reel_id que NO estaban
            # antes → señal fiable para el reembolso del refresh-now de pago (contrato David).
            try:
                _scraped_ids = [r["ig_reel_id"] for r in rows if r.get("ig_reel_id")]
                _existing = set()
                for _i in range(0, len(_scraped_ids), 100):
                    _ex = (db.table("creator_reels_global").select("ig_reel_id")
                             .eq("creator_id", creator_id)
                             .in_("ig_reel_id", _scraped_ids[_i:_i + 100]).execute())
                    _existing |= {x.get("ig_reel_id") for x in (_ex.data or [])}
                new_reels = sum(1 for s in _scraped_ids if s not in _existing)
            except Exception:
                new_reels = 0
            def _upsert(rs):
                db.table("creator_reels_global").upsert(rs, on_conflict="creator_id,ig_reel_id").execute()
            try:
                _upsert(rows)
                reels_count = len(rows)
            except Exception as e:
                # Degradación segura: si la columna `shares` aún no está migrada en prod,
                # reintenta sin ella (no rompe el scrape hasta correr la migración).
                if "shares" in str(e).lower():
                    for _r in rows:
                        _r.pop("shares", None)
                    try:
                        _upsert(rows); reels_count = len(rows)
                    except Exception as e2:
                        logger.exception("scrape_creator upsert (no-shares) failed for %s: %s", ig_username, e2)
                        final_status = "failed"; last_error = f"upsert: {str(e2)[:200]}"
                else:
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

    # Feed v1: si entraron reels nuevos, invalida la caché del feed (día) de TODOS los
    # usuarios que siguen a este creador → sus reels aparecen el mismo día (no esperan a la
    # TTL de 26h ni al rerank). Sin esto, «añado competidor → veo sus reels» falla en el día.
    if reels_count:
        try:
            import redis as _r_lib
            _rds = _r_lib.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            fr = (db.table("user_tracked_creators").select("user_id, project_id")
                    .eq("creator_id", creator_id).is_("archived_at", "null")
                    .limit(5000).execute()).data or []
            seen = set()
            for row in fr:
                u = row.get("user_id")
                if not u:
                    continue
                key = "feedcache:%s:%s:%s" % (u, row.get("project_id") or "_", day)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    _rds.delete(key)
                except Exception:
                    pass
        except Exception:
            logger.warning("scrape_creator: feed cache invalidate failed creator=%s", creator_id)

    # PERF #2 (David): PRE-TRANSCRIPCIÓN top-N en CADA scrape (antes solo el primero → los
    # reels de los refrescos y del 4º en adelante nunca se pre-transcribían: 75% del pool sin
    # transcript). Ahora cada scrape encola los top-N por views SIN transcript → «Roba» = cache-hit.
    # transcribe_reel_task es idempotente (salta si ya hay transcript) → solo transcribe lo NUEVO,
    # coste acotado a ≤N por scrape. Best-effort, nunca rompe el scrape.
    if final_status == "ok" and reels_count > 0:
        try:
            top = (db.table("creator_reels_global")
                     .select("id, transcript")
                     .eq("creator_id", creator_id).eq("is_archived", False)
                     .order("views", desc=True).limit(_PRETRANSCRIBE_TOP_N).execute()).data or []
            enq = 0
            for r in top:
                if (r.get("transcript") or "").strip():
                    continue   # ya transcrito → no re-encolar
                try:
                    transcribe_reel_task.delay(r["id"])
                    enq += 1
                except Exception as e:
                    logger.warning("scrape_creator: pre-transcribe enqueue failed reel=%s err=%s", r.get("id"), e)
            if enq:
                logger.info("scrape_creator: pre-transcribe queued=%d (first scrape) for %s", enq, ig_username)
        except Exception as e:
            logger.warning("scrape_creator: pre-transcribe step failed for %s: %s", ig_username, e)

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
        out["new_reels"] = new_reels
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
# 'private' ya NO es condena perpetua: el login-wall de IG lo dispara también en cuentas
# públicas (47/57 tracked el 04/07/26) y suele ser transitorio → reintento cada N días
# (coste acotado: 1 run/creador cada N días). 'not_found' sí sigue siendo permanente.
_PRIVATE_RETRY_DAYS = int(os.environ.get("RADAR_PRIVATE_RETRY_DAYS", "3"))
_TRACKED_STALE_HOURS = int(os.environ.get("RADAR_TRACKED_STALE_HOURS", "48"))
# Ahorro Apify (David 04/07): reels por refresco RECURRENTE. 4 basta (nadie sube reels
# entre refrescos y la explosión pasa en los nuevos, que sí se refrescan). El deep read
# SEMANAL de tracked vuelve a 10 para refrescar las vistas del baseline y cazar
# late-bloomers. Primera pasada / manual / onboarding se quedan en 10 (default del task).
_TRACKED_REFRESH_LIMIT = int(os.environ.get("RADAR_TRACKED_REFRESH_LIMIT", "4"))
_POOL_REFRESH_LIMIT = int(os.environ.get("RADAR_POOL_REFRESH_LIMIT", "4"))
_DEEP_REFRESH_LIMIT = int(os.environ.get("RADAR_DEEP_REFRESH_LIMIT", "10"))


def _enqueue_if_stale(db, creator_ids, stale_hours, cap, results_limit=10):
    """Encola scrape_creator_task para los creadores stale (>stale_hours). Skips:
    'not_found' (permanente), 'private' fresco (<_PRIVATE_RETRY_DAYS), 'scraping' vivo
    (<SCRAPE_STUCK_MIN; los zombis más viejos se re-encolan y el takeover del lock los
    roba). Una sola lectura a creators_global (.in_) + filtro en memoria. Devuelve
    (queued, candidates)."""
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
    private_cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=_PRIVATE_RETRY_DAYS)).isoformat()
    stuck_cutoff = (datetime.now(timezone.utc)
                    - timedelta(minutes=SCRAPE_STUCK_MIN)).isoformat()
    for cr in rows:
        if queued >= cap:
            break
        st = cr.get("scrape_status")
        last = cr.get("last_scraped_at")
        if st == "not_found":
            continue
        if st == "private" and last and str(last) > private_cutoff:
            continue   # private fresco → reintento solo cada _PRIVATE_RETRY_DAYS
        if st == "scraping" and last and str(last) > stuck_cutoff:
            continue   # scrape vivo de verdad; los zombis (más viejos) sí se re-encolan
        if st not in ("private", "scraping") and last and str(last) > cutoff:
            continue   # fresco → skip (ISO comparable lexicográficamente)
        try:
            scrape_creator_task.delay(cr["id"], results_limit)
            queued += 1
        except Exception:
            logger.exception("refresh: enqueue failed for %s", cr.get("id"))
    return queued, len(creator_ids)


@celery_app.task(name="tasks.refresh_radar_daily")
def refresh_radar_daily():
    """NOVEDAD DIARIA (Fathom 18/06): re-scrapea los competidores que alguien sigue
    (stale >_TRACKED_STALE_HOURS; 48h desde fase2) → reels nuevos cada 2 días por creador.
    resultsLimit=_TRACKED_REFRESH_LIMIT (4, ahorro David 04/07). Capado a _RADAR_DAILY_CAP."""
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
    queued, cand = _enqueue_if_stale(db, creator_ids, stale_hours=_TRACKED_STALE_HOURS,
                                     cap=_RADAR_DAILY_CAP, results_limit=_TRACKED_REFRESH_LIMIT)
    logger.info("refresh_radar_daily: queued=%d candidates=%d limit=%d",
                queued, cand, _TRACKED_REFRESH_LIMIT)
    return {"queued": queued, "candidates": cand}


@celery_app.task(name="tasks.deep_refresh_tracked")
def deep_refresh_tracked():
    """DEEP READ SEMANAL de tracked (David 04/07): re-scrapea TODOS los competidores
    seguidos con resultsLimit=_DEEP_REFRESH_LIMIT (10) para refrescar las vistas de los
    ~10 reels recientes → mantiene honesto el baseline de explosión y caza late-bloomers
    que el refresco recurrente (N=4) congela. stale_hours=0 = fuerza a todos (freshness no
    salta); respeta private/not_found/zombis igual que el diario. Corre ANTES del diario
    (lunes 05:00) → el diario de las 06:00 los ve frescos y no duplica."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        tr = (db.table("user_tracked_creators").select("creator_id")
                .is_("archived_at", "null").limit(5000).execute())
        creator_ids = [r["creator_id"] for r in (tr.data or []) if r.get("creator_id")]
    except Exception:
        logger.exception("deep_refresh_tracked: tracked read failed")
        return {"status": "error"}
    queued, cand = _enqueue_if_stale(db, creator_ids, stale_hours=0,
                                     cap=_RADAR_DAILY_CAP, results_limit=_DEEP_REFRESH_LIMIT)
    logger.info("deep_refresh_tracked: queued=%d candidates=%d limit=%d",
                queued, cand, _DEEP_REFRESH_LIMIT)
    return {"queued": queued, "candidates": cand}


# FASE 2 feed-vivo (dimensionado David 04/07): pool ≤2×/semana por creador (stale 84h),
# cap diario acorde (~$0.024/creador sin shares), kill-switch por env — default OFF hasta
# «cap subido» (Apify maxMonthlyUsageUsd). Activar: RADAR_POOL_REFRESH_ENABLED=1 en el
# .env del VPS + docker compose up -d. El primer arranque hace de BACKFILL natural: los
# seeds nunca scrapeados van primero (stalest-first) y el cap lo reparte en días.
_POOL_STALE_HOURS = int(os.environ.get("RADAR_POOL_STALE_HOURS", "84"))
_POOL_DAILY_CAP = int(os.environ.get("RADAR_POOL_SCRAPE_CAP", "40"))


@celery_app.task(name="tasks.refresh_suggestion_pools")
def refresh_suggestion_pools():
    """Pool de sugerencias VIVO: re-scrapea creadores de NICHOS ACTIVOS (nicho canónico
    de algún proyecto o perfil). No excluye seguidos: el doble scrape lo evita solo la
    staleness (84h aquí vs 48h del job de tracked). Stalest-first + cap diario."""
    # Default ON desde v0.44 (decisión David #2): el pool de nicho se refresca solo (stalest-first
    # + cap diario _POOL_DAILY_CAP) → las sugerencias no se agotan sin remedio. Killable con
    # RADAR_POOL_REFRESH_ENABLED=0 en el .env del VPS si dispara el coste Apify.
    if os.environ.get("RADAR_POOL_REFRESH_ENABLED", "1") != "1":
        logger.info("refresh_suggestion_pools: OFF (RADAR_POOL_REFRESH_ENABLED=0)")
        return {"status": "disabled"}
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    niches = set()
    try:
        for table in ("projects", "profiles"):
            for row in (db.table(table).select("niche").limit(5000).execute()).data or []:
                pn = pool_niche_canon(row.get("niche") or "")
                if pn:
                    niches.add(pn)
    except Exception:
        logger.exception("refresh_suggestion_pools: niches read failed")
        return {"status": "error"}
    if not niches:
        return {"queued": 0, "candidates": 0, "niches": 0}
    try:
        rows = (db.table("creators_global").select("id, last_scraped_at")
                  .in_("niche", sorted(niches)).limit(3000).execute()).data or []
    except Exception:
        logger.exception("refresh_suggestion_pools: creators read failed")
        return {"status": "error"}
    # Stalest-first: los nunca scrapeados ("" ordena primero) y luego los más viejos → el
    # cap reparte el backfill en días y después mantiene la rotación ≤2×/semana.
    rows.sort(key=lambda r: str(r.get("last_scraped_at") or ""))
    ids = [r["id"] for r in rows if r.get("id")]
    queued, cand = _enqueue_if_stale(db, ids, stale_hours=_POOL_STALE_HOURS,
                                     cap=_POOL_DAILY_CAP, results_limit=_POOL_REFRESH_LIMIT)
    logger.info("refresh_suggestion_pools: queued=%d candidates=%d niches=%d limit=%d",
                queued, cand, len(niches), _POOL_REFRESH_LIMIT)
    return {"queued": queued, "candidates": cand, "niches": len(niches)}


# ── Avatares de creadores («posibles competidores», fotos reales) ────────────
# El scrape de reels no trae foto de perfil; instagram-profile-scraper sí (profilePicUrl,
# ~$0.0023/perfil). Cacheamos los BYTES (la URL del CDN caduca; los bytes no) en
# creators_global.profile_data.avatar_b64 → los sirve /img/creator/<id>. Sin foto → el
# front usa iniciales. Coste: backfill 163 ≈ $0.37; re-scrape mensual ≈ $0.4/mes.
_AVATAR_ACTOR = os.environ.get("AVATAR_ACTOR", "apify~instagram-profile-scraper")
_AVATAR_BATCH = int(os.environ.get("AVATAR_SCRAPE_BATCH", "50"))
_AVATAR_MONTHLY_CAP = int(os.environ.get("AVATAR_MONTHLY_CAP", "500"))


@celery_app.task(name="tasks.refresh_creator_avatars")
def refresh_creator_avatars(creator_ids=None, cap=300):
    """Cachea el avatar de creadores en profile_data.avatar_b64. Con `creator_ids` explícito
    re-scrapea TODOS (force, p.ej. refresh mensual); sin lista → creadores 'ok' SIN avatar
    (backfill). Batch de _AVATAR_BATCH usernames/run. Fallback silencioso a iniciales."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        if creator_ids:
            rows = []
            for i in range(0, len(creator_ids), 100):
                r = (db.table("creators_global").select("id, ig_username, profile_data")
                       .in_("id", creator_ids[i:i + 100]).execute())
                rows.extend(r.data or [])
        else:
            rows = (db.table("creators_global").select("id, ig_username, profile_data")
                      .eq("scrape_status", "ok").limit(3000).execute()).data or []
    except Exception:
        logger.exception("refresh_creator_avatars: read failed")
        return {"status": "error"}
    pend = rows if creator_ids else [
        r for r in rows if not ((r.get("profile_data") or {}).get("avatar_b64"))]
    pend = [r for r in pend if r.get("ig_username")][:cap]
    if not pend:
        return {"scraped": 0, "stored": 0}
    by_handle = {}
    for r in pend:
        by_handle[(r["ig_username"] or "").lstrip("@").lower()] = r
    handles = list(by_handle.keys())
    actor_url = ("https://api.apify.com/v2/acts/%s/run-sync-get-dataset-items"
                 "?token=%s&memory=256" % (_AVATAR_ACTOR, APIFY_TOKEN))
    stored = 0
    for i in range(0, len(handles), _AVATAR_BATCH):
        chunk = handles[i:i + _AVATAR_BATCH]
        try:
            resp = requests.post(actor_url, json={"usernames": chunk}, timeout=SCRAPE_TIMEOUT_SEC)
            items = resp.json() if resp.status_code < 300 else []
        except Exception as e:
            logger.warning("refresh_creator_avatars: apify chunk failed: %s", e)
            continue
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            uname = (it.get("username") or "").lstrip("@").lower()
            cr = by_handle.get(uname)
            if not cr:
                continue
            pic = it.get("profilePicUrl") or it.get("profilePicUrlHD")
            b64 = _download_thumbnail_b64(pic) if pic else None
            if not b64:
                continue
            pd = cr.get("profile_data")
            pd = pd if isinstance(pd, dict) else {}
            pd["avatar_b64"] = b64
            try:
                db.table("creators_global").update({"profile_data": pd}).eq("id", cr["id"]).execute()
                stored += 1
            except Exception:
                logger.warning("refresh_creator_avatars: store failed for %s", uname)
    logger.info("refresh_creator_avatars: handles=%d stored=%d", len(handles), stored)
    return {"scraped": len(handles), "stored": stored}


@celery_app.task(name="tasks.refresh_avatars_monthly")
def refresh_avatars_monthly():
    """Mensual: re-cachea avatares de creadores de nichos ACTIVOS (cambian raro; re-scrape
    por si acaso). Reusa el canon de nicho de refresh_suggestion_pools."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    niches = set()
    try:
        for table in ("projects", "profiles"):
            for row in (db.table(table).select("niche").limit(5000).execute()).data or []:
                pn = pool_niche_canon(row.get("niche") or "")
                if pn:
                    niches.add(pn)
        if not niches:
            return {"scraped": 0}
        rows = (db.table("creators_global").select("id")
                  .in_("niche", sorted(niches)).eq("scrape_status", "ok")
                  .limit(3000).execute()).data or []
    except Exception:
        logger.exception("refresh_avatars_monthly: read failed")
        return {"status": "error"}
    ids = [r["id"] for r in rows if r.get("id")]
    return refresh_creator_avatars(ids, cap=_AVATAR_MONTHLY_CAP)


_RADAR_RERANK_CAP = int(os.environ.get("RADAR_RERANK_CAP", "3000"))


@celery_app.task(name="tasks.rerank_radar_daily")
def rerank_radar_daily():
    """v1 FEED DIARIO (GRATIS, CERO scrape): cada mañana CALIENTA la caché Redis del feed
    de cada usuario activo re-rankeando el pool YA scrapeado. NO scrapea — a propósito
    separado de refresh_radar_daily (que SÍ scrapea). Si no corriera, el feed igual rota
    por el date-seed al abrir la app; este job solo evita el primer cómputo en caliente."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    _db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        tr = (_db.table("user_tracked_creators").select("user_id, creator_id, project_id")
                .is_("archived_at", "null").limit(20000).execute())
        rows = tr.data or []
    except Exception:
        logger.exception("rerank_radar_daily: tracked read failed")
        return {"status": "error"}
    groups = {}
    for r in rows:
        uid = r.get("user_id"); cid = r.get("creator_id")
        if not uid or not cid:
            continue
        groups.setdefault((uid, r.get("project_id")), []).append(cid)
    try:
        from app import _radar_feed_order   # lazy (rompe circular); computa + cachea
    except Exception:
        logger.exception("rerank_radar_daily: import failed")
        return {"status": "error"}
    warmed = 0
    for (uid, pid), cids in list(groups.items())[:_RADAR_RERANK_CAP]:
        try:
            _radar_feed_order(uid, pid, cids)   # NO scrape: solo re-rank del pool
            warmed += 1
        except Exception:
            logger.warning("rerank_radar_daily: warm failed uid=%s", uid)
    logger.info("rerank_radar_daily: warmed=%d groups=%d", warmed, len(groups))
    return {"warmed": warmed, "groups": len(groups)}


@celery_app.task(name="tasks.finalize_manual_refresh")
def _niche_has_unseen_recent(_db, uid, niche_pn, days):
    """#3 (David): ¿el nicho tiene reels ≤`days` que el user NO ha robado? → hay algo RECIENTE que
    servir en «Sugerencias de hoy» aunque el scrape no trajera nada nuevo-nuevo. El refresh-pool
    usa esto para el reembolso: si SÍ hay reciente sin ver, la refresh «dio algo» (no reembolsa);
    si el nicho no tiene nada reciente sin robar, reembolsa («no vender aire»). Conservador: ante
    cualquier error → False (reembolsa)."""
    if not niche_pn:
        return False
    try:
        cids = [c["id"] for c in (_db.table("creators_global").select("id")
                                    .eq("niche", niche_pn).limit(600).execute()).data or [] if c.get("id")]
        if not cids:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days or 14))).isoformat()
        recent = []
        for i in range(0, len(cids), 100):
            rr = (_db.table("creator_reels_global").select("id")
                    .in_("creator_id", cids[i:i + 100]).eq("is_archived", False)
                    .gte("posted_at", cutoff).limit(500).execute()).data or []
            recent.extend([str(x["id"]) for x in rr if x.get("id")])
        if not recent:
            return False
        stolen = set()
        try:
            for r in (_db.table("scripts").select("from_competitor_reel_id")
                        .eq("user_id", uid).not_.is_("from_competitor_reel_id", "null")
                        .limit(4000).execute()).data or []:
                if r.get("from_competitor_reel_id"):
                    stolen.add(str(r["from_competitor_reel_id"]))
        except Exception:
            pass
        return any(rid not in stolen for rid in recent)
    except Exception:
        logger.warning("[pool_refresh] servable check failed niche=%s", niche_pn, exc_info=True)
        return False


def finalize_manual_refresh(results, uid, project_id, charged_amount, is_paid_unlimited,
                            charge_id=None, pool_niche=None):
    """Callback del chord del refresco manual de PAGO. `results` = lista de dicts de
    scrape_creator_task. Reembolsa SOLO si TODO fue fallo duro (failed/creator_not_found);
    ok/private/not_found/in_progress = trabajo hecho → NO reembolsa. Siempre invalida la
    caché del feed (entran reels nuevos). Idempotente por COBRO (guard frfinal:<charge_id>,
    no por-día → un 2º cobro fallido el mismo día también se reembolsa). NO limpia el
    cooldown en fallo: el scrape sí golpeó Apify; el cooldown cap­a ese coste (el reembolso
    ya cumple «si falla, no cobra»)."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    _db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    statuses = [(_r or {}).get("status") for _r in (results or []) if isinstance(_r, dict)]
    new_total = sum(int((_r or {}).get("new_reels") or 0)
                    for _r in (results or []) if isinstance(_r, dict))
    hard_fail = {"failed", "creator_not_found"}
    all_failed = bool(statuses) and all(s in hard_fail for s in statuses)
    # CONTRATO David: reembolsar si TODO falló O si el refresh NO trajo NADA nuevo. Cobrar por
    # vacío (competidores sin novedades) es inaceptable → el usuario recupera sus créditos.
    nothing_new = (new_total == 0)
    # #3 (David): en el refresh del POOL DE NICHO (pool_niche), NO exigir net-new — «se dan ideas
    # recientes igual»: reembolsa solo si el nicho no tiene reels ≤14d SIN robar que servir. En el
    # refresh de COMPETIDORES (pool_niche=None) se mantiene la regla net-new (v0.43.1).
    if pool_niche:
        _servable = _niche_has_unseen_recent(
            _db, uid, pool_niche, int(os.environ.get("RADAR_SUGG_MAX_AGE_DAYS", "14")))
        should_refund_core = all_failed or (not _servable)
    else:
        should_refund_core = all_failed or nothing_new
    pid = project_id or "_"
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    try:
        import redis as _r_lib
        _rds = _r_lib.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
    except Exception:
        _rds = None
    # SIEMPRE: invalidar la caché del feed del día (idempotente).
    if _rds is not None:
        try:
            _rds.delete("feedcache:%s:%s:%s" % (uid, pid, day))
        except Exception:
            pass
    refunded = False
    if charged_amount and should_refund_core:
        # Guard de idempotencia POR COBRO (no por-día): cada cobro tiene su nonce → dos
        # fallos totales el mismo día se reembolsan ambos. Si falta charge_id (compat),
        # cae a la clave por-día (comportamiento previo).
        guard = "frfinal:%s" % charge_id if charge_id else "frfinal:%s:%s:%s" % (uid, pid, day)
        do_refund = True
        if _rds is not None:
            try:
                do_refund = bool(_rds.set(guard, "1", nx=True, ex=86400))
            except Exception:
                do_refund = True
        if do_refund:
            try:
                prof = (_db.table("profiles").select("credits_cents, monthly_usage")
                          .eq("id", uid).single().execute()).data or {}
                if is_paid_unlimited:
                    _db.table("profiles").update({
                        "monthly_usage": max(0, (prof.get("monthly_usage") or 0) - charged_amount)
                    }).eq("id", uid).execute()
                else:
                    _db.table("profiles").update({
                        "credits_cents": (prof.get("credits_cents") or 0) + charged_amount
                    }).eq("id", uid).execute()
                refunded = True
            except Exception:
                logger.warning("finalize_manual_refresh: refund failed uid=%s", uid)
            # NO se limpia el cooldown: el scrape golpeó Apify; mantenerlo capa ese coste.
    logger.info("finalize_manual_refresh uid=%s pool=%s statuses=%s new=%d refunded=%s",
                uid, pool_niche or "-", statuses, new_total, refunded)
    return {"ok": True, "refunded": refunded, "new_count": new_total, "statuses": statuses}


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


def _gate_creators_groq(cands: list, niche_ctx: str) -> list:
    """GATE de relevancia barato (Groq). De una lista de candidatos (relatedProfiles)
    deja solo los CREADORES de contenido personales, del nicho y en español — quita
    MARCAS (Xiaomi, Samsung...) y cuentas de otra región/idioma (LatAm, portugués,
    catalán, inglés...). cands: [{"u":username,"name":full_name}]. Devuelve [usernames].
    Best-effort: si Groq falla → devuelve TODOS (no bloquea el onboarding); si Groq
    responde y no salva ninguno → devuelve [] (deja que el fallback hashtag entre)."""
    GROQ = os.environ.get("GROQ_API_KEY", "")
    if not cands:
        return []
    if not GROQ:
        return [c["u"] for c in cands]
    prompt = (
        "Eres un filtro para una app de creadores españoles. NICHO del cliente: " + (niche_ctx or "general") + ".\n"
        "Te doy cuentas de Instagram sugeridas. Para CADA una decide si es buen COMPETIDOR:\n"
        "- keep=true SOLO si es un CREADOR DE CONTENIDO PERSONAL (una persona/canal), del nicho, "
        "y en ESPAÑOL (España o español neutro).\n"
        "- keep=false si es una MARCA/fabricante/tienda (Xiaomi, Samsung, Honor, Ray-Ban...), "
        "o de otra región/idioma claramente (Ecuador, Colombia, México, Brasil/portugués, "
        "catalán, inglés, Asia...), o fuera del nicho.\n"
        "Devuelve SOLO json: {\"r\":[{\"u\":username,\"keep\":bool}]}.\n\n"
        "CUENTAS:\n" + json.dumps(cands, ensure_ascii=False))
    try:
        gr = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + GROQ},
            json={"model": os.environ.get("GROQ_GATE_MODEL", "llama-3.3-70b-versatile"),
                  "temperature": 0, "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        gr.raise_for_status()
        out = json.loads(gr.json()["choices"][0]["message"]["content"])
        return [x["u"] for x in (out.get("r") or []) if x.get("keep") and x.get("u")]
    except Exception as e:
        logger.warning("[discover] groq gate failed (paso todos): %s", e)
        return [c["u"] for c in cands]


@celery_app.task(name="tasks.discover_niche_creators")
def discover_niche_creators_task(user_id: str, niche: str, subniches: list,
                                 seed_handle: str = "", follow_top: int = 6) -> dict:
    """DESCUBRIMIENTO DE NICHO (onboarding) v2 — SEED = una cuenta referente que el USER
    AÑADE A MANO en el onboarding (no su propia cuenta; la suya se scrapea solo para
    métricas). Scrapea los `relatedProfiles` del seed (mismo nicho + idioma del grafo de
    IG), los pasa por un GATE LLM barato (Groq) que quita MARCAS y cuentas de otra
    región/idioma, AUTO-SIGUE los ~6 creadores limpios (→ el radar los muestra) y encola
    el scrape de sus reels reales. Si no hay seed o da <3 creadores limpios, completa por
    HASHTAG (fallback). Best-effort: si algo falla, no rompe el onboarding."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
    if not APIFY_TOKEN:
        return {"status": "no_apify"}
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    # 0. SEED = cuenta del nicho que el user añadió a mano (validada de nuevo aquí).
    seed_handle = (seed_handle or "").strip().lstrip("@").lower()
    if not re.match(r"^[a-z0-9._]{2,30}$", seed_handle):
        seed_handle = ""
    niche_ctx = ", ".join([x for x in ([niche] + list(subniches or [])) if x]) or (niche or "")

    keep_unames: list = []
    # 1. SEED: seguir SOLO al referente que el user eligió a mano (decisión Leo 2026-06-23).
    #    Los "afines" del nicho ya NO se auto-siguen: el 2º competidor lo añade el user a
    #    mano (botón «+ Añadir») o vía la tarjeta «Te lo sugiero» (co-ocurrencia). Así el
    #    radar arranca SOLO con el referente que él escogió, no con gente que no eligió.
    if seed_handle:
        keep_unames = [seed_handle]

    # 2. FALLBACK: SOLO por hashtag si NO hay seed (el user saltó el paso). Con seed, el
    #    propio seed ya garantiza ≥1 creador relevante → nunca metemos hashtag (= spam).
    if not seed_handle and len(keep_unames) < 3:
        tags = []
        for s in (subniches or [])[:2]:
            t = re.sub(r"[^a-z0-9áéíóúñ]", "", (s or "").strip().lower())
            if t and t not in tags:
                tags.append(t)
        htag_actor = (f"https://api.apify.com/v2/acts/apify~instagram-scraper"
                      f"/run-sync-get-dataset-items?token={APIFY_TOKEN}&memory=1024")
        hscore: dict = {}
        for tag in tags:
            try:
                logger.info("[discover] fallback hashtag #%s", tag)
                resp = requests.post(htag_actor, json={
                    "directUrls": [f"https://www.instagram.com/explore/tags/{tag}/"],
                    "resultsType": "posts", "resultsLimit": 40, "addParentData": False},
                    timeout=SCRAPE_TIMEOUT_SEC)
                if resp.status_code >= 400:
                    continue
                for it in (resp.json() or []):
                    uname = (it.get("ownerUsername") or "").strip().lstrip("@").lower()
                    if not uname or not re.match(r"^[a-z0-9._]{1,30}$", uname):
                        continue
                    sc = hscore.setdefault(uname, {"posts": 0, "eng": 0})
                    sc["posts"] += 1
                    sc["eng"] += int(it.get("likesCount") or 0) + int(it.get("commentsCount") or 0)
            except Exception as e:
                logger.warning("[discover] fallback hashtag #%s failed: %s", tag, e)
        for uname, _ in sorted(hscore.items(), key=lambda kv: (-kv[1]["posts"], -kv[1]["eng"])):
            if len(keep_unames) >= follow_top:
                break
            if uname not in keep_unames and uname != seed_handle:
                keep_unames.append(uname)

    if not keep_unames:
        return {"status": "no_creators", "seed": seed_handle}

    # 3. Auto-seguir (idempotente) + encolar scrape de reels para cada creador limpio.
    followed = 0
    scraped = 0
    for uname in keep_unames:
        try:
            ins = db.table("creators_global").upsert(
                {"ig_username": uname}, on_conflict="ig_username").execute()
            row = (ins.data or [None])[0]
            if not row:
                row = (db.table("creators_global").select("id")
                         .eq("ig_username", uname).single().execute()).data
            cid = row["id"]
            if user_id:
                ex = (db.table("user_tracked_creators").select("id, archived_at")
                        .eq("user_id", user_id).eq("creator_id", cid).limit(1).execute()).data or []
                if ex:
                    if ex[0].get("archived_at"):
                        db.table("user_tracked_creators").update({"archived_at": None}).eq("id", ex[0]["id"]).execute()
                else:
                    db.table("user_tracked_creators").insert(
                        {"user_id": user_id, "creator_id": cid, "project_id": None}).execute()
                followed += 1
            try:
                scrape_creator_task.delay(cid)
                scraped += 1
            except Exception:
                pass
        except Exception as e:
            logger.warning("[discover] creator @%s failed: %s", uname, e)
    logger.info("[discover] seed=@%s followed=%d scraped=%d", seed_handle, followed, scraped)
    return {"status": "ok", "seed": seed_handle, "followed": followed, "scraped": scraped}


@celery_app.task(name="tasks.harvest_related_creators")
def harvest_related_creators_task(handle, niche="", subniches=None, project_id=None,
                                  source="add_competitor"):
    """Fase 2 (David): al AÑADIR un competidor o CONECTAR una cuenta de IG, cosechar los
    `relatedProfiles` que Apify ya devuelve para ese perfil (vecinos on-niche del grafo REAL
    de IG) → upsert en creators_global TAGUEADOS con el nicho/subnichos de la marca + scrape
    en background de unos pocos → «posibles competidores» y «Sugerencias de hoy» se construyen
    desde el grafo del usuario, no solo del catálogo estático (clave para nichos finos que el
    catálogo no cubre). Acotado (HARVEST_N upserts, SCRAPE_N scrapes) y killable
    (RADAR_RELATED_HARVEST_ENABLED). Best-effort: cualquier fallo → no-op, no rompe el alta."""
    if os.environ.get("RADAR_RELATED_HARVEST_ENABLED", "1") != "1":
        return {"status": "disabled"}
    APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
    if not APIFY_TOKEN:
        return {"status": "no_apify"}
    handle = (handle or "").strip().lstrip("@").lower()
    if not re.match(r"^[a-z0-9._]{1,30}$", handle):
        return {"status": "bad_handle"}
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    HARVEST_N = int(os.environ.get("RADAR_RELATED_HARVEST_N", "12"))
    SCRAPE_N = int(os.environ.get("RADAR_RELATED_SCRAPE_N", "6"))
    tags = sorted({norm_tag(s) for s in ([niche] + list(subniches or [])) if norm_tag(s)})
    pn = pool_niche_canon(niche or "")

    # 1. Perfil (resultsType=details) → relatedProfiles (vecinos on-niche del grafo de IG).
    related = []
    try:
        actor_url = ("https://api.apify.com/v2/acts/apify~instagram-scraper"
                     "/run-sync-get-dataset-items?token=" + APIFY_TOKEN + "&memory=256")
        resp = requests.post(actor_url, json={
            "directUrls": ["https://www.instagram.com/" + handle + "/"],
            "resultsType": "details", "resultsLimit": 1,
        }, timeout=SCRAPE_TIMEOUT_SEC)
        resp.raise_for_status()
        items = resp.json() or []
        item = items[0] if items else {}
        for rp in (item.get("relatedProfiles") or [])[:40]:
            u = ((rp.get("username") or rp.get("ownerUsername") or "")
                 .strip().lstrip("@").lower()) if isinstance(rp, dict) else ""
            if re.match(r"^[a-z0-9._]{1,30}$", u) and u != handle:
                related.append(u)
    except Exception as e:
        logger.warning("[related] apify details failed handle=%s: %s", handle, e)
        return {"status": "apify_failed", "handle": handle}
    if not related:
        return {"status": "no_related", "handle": handle}
    seen = set()
    related = [u for u in related if not (u in seen or seen.add(u))][:HARVEST_N]

    # 2. Upsert + TAG con el nicho/subnichos de la marca (enriquece el foso) + scrape bg acotado.
    scraped = 0
    added = 0
    for u in related:
        try:
            ins = (db.table("creators_global")
                     .upsert({"ig_username": u}, on_conflict="ig_username").execute())
            row = (ins.data or [None])[0]
            if not row:
                row = (db.table("creators_global")
                         .select("id, niche, niche_source, subniches, scrape_status, last_scraped_at")
                         .eq("ig_username", u).single().execute()).data
            if not row:
                continue
            cid = row["id"]
            added += 1
            upd = {}
            cur_tags = set(row.get("subniches") or [])
            if tags and (set(tags) - cur_tags):
                upd["subniches"] = sorted(cur_tags | set(tags))
            if pn and not (row.get("niche") or "").strip():
                upd["niche"] = pn
            if not row.get("niche_source"):
                upd["niche_source"] = "related"   # NO pisar 'seed'/'user' (curados) → solo etiqueta lo nuevo
            if upd:
                try:
                    db.table("creators_global").update(upd).eq("id", cid).execute()
                except Exception:
                    logger.warning("[related] tag failed %s (¿migración subniches/niche?)", u, exc_info=True)
            # scrape bg SOLO si sin reels frescos (>7d o nunca) y estado no definitivo, acotado.
            if scraped < SCRAPE_N and row.get("scrape_status") not in ("private", "not_found", "scraping"):
                last = row.get("last_scraped_at")
                stale = True
                if last:
                    try:
                        stale = (datetime.now(timezone.utc) - datetime.fromisoformat(
                            str(last).replace("Z", "+00:00"))).total_seconds() / 3600.0 > 168
                    except Exception:
                        stale = True
                if stale:
                    try:
                        scrape_creator_task.delay(cid)
                        scraped += 1
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("[related] creator @%s failed: %s", u, e)
    logger.info("[related] handle=@%s niche=%r added=%d scraped=%d source=%s",
                handle, niche, added, scraped, source)
    return {"status": "ok", "handle": handle, "added": added, "scraped": scraped}


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


def _user_view_stats(db, uid):
    """Views de los reels propios del user (ig_videos) → (best, growth%). growth =
    media de los 3 más nuevos vs los 3 siguientes. Sin perfil/datos → (0, 0)."""
    try:
        prof = db.table("ig_profiles").select("id").eq("user_id", uid).limit(1).execute()
        if not prof.data:
            return 0, 0
        mv = (db.table("ig_videos").select("views, published_at")
                .eq("user_id", uid).order("published_at", desc=True).limit(12).execute()).data or []
    except Exception:
        return 0, 0
    vs = [int(v.get("views") or 0) for v in mv]
    if not vs:
        return 0, 0
    best = max(vs)
    growth = 0
    if len(vs) >= 4:
        n = min(3, len(vs) // 2)
        recent = sum(vs[:n]) / n
        prev = sum(vs[n:2 * n]) / n
        if prev > 0:
            growth = max(-95, min(300, round((recent - prev) / prev * 100)))
    return best, growth


@celery_app.task(name="tasks.send_growth_nudges")
def send_growth_nudges():
    """Beat SEMANAL (Fathom 18/06): 3 emails de crecimiento con métricas REALES.
    Por user con competidores + perfil propio, elige UN email (prioridad):
      1) climb        — tus reels recientes rinden +X% (X>=15).
      2) almost_beat  — tu mejor reel está cerca (>=60%, <100%) de la media de un rival.
      3) explosion    — un competidor petó un reel reciente (×>=3 su media, >=50k views).
    Idempotente por semana ISO (key en email_log). Solo planes con radar."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {"error": "supabase_not_configured"}
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        from emails import (send_growth_climb, send_growth_almost_beat,
                            send_growth_niche_explosion)
    except Exception as e:
        logger.error("send_growth_nudges: import emails failed: %s", e)
        return {"error": "import_emails"}

    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    recent_since = (now - timedelta(days=7)).isoformat()

    # 1. Competidores activos por user.
    try:
        tracked = (db.table("user_tracked_creators").select("user_id, creator_id")
                     .is_("archived_at", "null").execute()).data or []
    except Exception as e:
        logger.error("send_growth_nudges: tracked query failed: %s", e)
        return {"error": "tracked_query"}
    by_user = {}
    for t in tracked:
        by_user.setdefault(t["user_id"], set()).add(t["creator_id"])
    if not by_user:
        return {"users": 0, "sent": 0}

    uids = list(by_user.keys())
    plan_by_uid = {}
    for i in range(0, len(uids), 300):
        try:
            profs = (db.table("profiles").select("id, plan")
                       .in_("id", uids[i:i + 300]).execute()).data or []
            for p in profs:
                plan_by_uid[p["id"]] = p.get("plan") or "free"
        except Exception:
            pass

    sent = skipped = 0
    for uid, cid_set in by_user.items():
        if plan_by_uid.get(uid, "free") not in RADAR_ENABLED_PLANS:
            skipped += 1
            continue
        cids = list(cid_set)
        baselines = _radar_baselines(db, cids)
        # Métricas de competidores: media de views por creador + mejor explosión reciente.
        avg_by_creator = {}
        for cid, med in baselines.items():
            avg_by_creator[cid] = med  # mediana de views ~ "media" del rival (real)
        handle_by_creator = {}
        try:
            cg = (db.table("creators_global").select("id, ig_username")
                    .in_("id", cids).execute()).data or []
            handle_by_creator = {c["id"]: c.get("ig_username") or "" for c in cg}
        except Exception:
            pass

        best, growth = _user_view_stats(db, uid)
        res = None

        # PRIORIDAD 1 — climb.
        if growth >= 15:
            res = send_growth_climb(uid, week_key, growth)

        # PRIORIDAD 2 — almost_beat (requiere tener datos propios).
        if (res is None or not res.get("sent")) and best > 0 and avg_by_creator:
            cand = None
            for cid, avg in avg_by_creator.items():
                if avg and best < avg and best >= 0.6 * avg:
                    if cand is None or avg < cand[1]:
                        cand = (cid, avg)  # el rival más cercano por encima
            if cand:
                res = send_growth_almost_beat(uid, week_key,
                                              handle_by_creator.get(cand[0], ""),
                                              int(cand[1]), int(best))

        # PRIORIDAD 3 — explosion (reel reciente de un competidor muy por encima de su media).
        if res is None or not res.get("sent"):
            try:
                rows = (db.table("creator_reels_global")
                          .select("creator_id, views, posted_at")
                          .in_("creator_id", cids).eq("is_archived", False)
                          .gte("posted_at", recent_since)
                          .order("views", desc=True).limit(60).execute()).data or []
            except Exception:
                rows = []
            top = None
            for r in rows:
                v = int(r.get("views") or 0)
                mult = _radar_explosion(v, baselines.get(r.get("creator_id")))
                if mult is not None and mult >= 3.0 and v >= 50000:
                    if top is None or v > top[1]:
                        top = (r.get("creator_id"), v, mult)
            if top:
                res = send_growth_niche_explosion(uid, week_key,
                                                  handle_by_creator.get(top[0], ""),
                                                  top[1], top[2])

        if res and res.get("sent"):
            sent += 1
        else:
            skipped += 1

    logger.info("send_growth_nudges done users=%d sent=%d skipped=%d", len(by_user), sent, skipped)
    return {"users": len(by_user), "sent": sent, "skipped": skipped}


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
def generate_script_competitor_task(self, reel_id, user_id, assistant_id, language=None, project_id=None):
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
    SCRIPT_UNITS = 3                 # econ: 1 guión = 3 créditos (economia-creditos.md)
    SCRIPT_COST = SCRIPT_UNITS * 18  # 54 cents = 3 créditos
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    def _fail(error, message):
        # P2: marca ÚNICA de robo fallido con causa — greppeable ([steal_failed]) y
        # medible en PostHog. Antes los fallos no dejaban rastro cuantificable.
        logger.warning("[steal_failed] cause=%s reel=%s user=%s", error, reel_id, user_id)
        try:
            from emails import track as _ph_track
            _ph_track("steal_failed", user_id, {"cause": error, "reel_id": reel_id})
        except Exception:
            pass
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
        # PERF (instrumentación David): desglose real del robo — transcripción vs LLM pro.
        # Greppeable [steal_timing]; live_transcribe=1 ⇒ pagó transcripción en vivo (reel sin cache).
        _t0 = time.time()
        _did_live_transcribe = False
        _t_tr_start = _t0
        _t_llm0 = _t_llm1 = None
        # 1. Cargar reel.
        try:
            rr = (db.table("creator_reels_global")
                    .select("id, ig_reel_id, caption, transcript, transcript_status, "
                            "transcript_started_at, "
                            "formato, video_duration_sec, "   # fallback FORMATO SUGERIDO
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
        _t_tr_start = time.time()
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

            have_lock = False
            if transcript_status != "transcribing" or is_stale:
                # Adquirir lock (UPDATE simple — race trivial aceptable). NOSOTROS transcribimos.
                now_iso = datetime.now(timezone.utc).isoformat()
                try:
                    db.table("creator_reels_global").update({
                        "transcript_status": "transcribing",
                        "transcript_started_at": now_iso,
                        "transcript_error": None,
                    }).eq("id", reel_id).execute()
                    have_lock = True
                except Exception as e:
                    logger.exception("gen_script_task lock UPDATE failed reel=%s: %s", reel_id, e)
                    return _fail("db_error", "Error preparando la transcripción.")
                transcript_text = ""
            else:
                # Otra task fresca está transcribiendo → poll BD hasta 'ok'. Si la otra falla
                # o agotamos el poll, NO volvemos a descargar (evita doble coste): caemos al
                # fallback de caption (gate al final). have_lock=False → no descarga.
                self.update_state(state="PROGRESS", meta={"step": "waiting_other"})
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
                            break
                        if re_r.data and re_r.data.get("transcript_status") == "failed":
                            break
                    except Exception:
                        pass

            # Solo descarga+transcribe quien tiene el lock y aún no tiene texto.
            if have_lock and not transcript_text:
                self.update_state(state="PROGRESS", meta={"step": "transcribing"})
                _did_live_transcribe = True   # instrumentación: este robo paga transcripción en vivo
                url = "https://www.instagram.com/reel/{}/".format(reel["ig_reel_id"])
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        audio_path, _thumb, _apify_item = download_audio(url, tmpdir, "instagram")
                        headers = {"Authorization": "Bearer " + GROQ_API_KEY}
                        with open(audio_path, "rb") as f:
                            files = {"file": ("audio.mp3", f, "audio/mpeg")}
                            data = {"model": os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"), "response_format": "json"}
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
                    transcript_text = ""   # → fallback a caption (gate único más abajo)

                # Guardar transcript en cache SOLO si lo conseguimos.
                if transcript_text:
                    try:
                        db.table("creator_reels_global").update({
                            "transcript": transcript_text,
                            "transcript_status": "ok",
                            "transcript_error": None,
                        }).eq("id", reel_id).execute()
                    except Exception as e:
                        logger.exception("gen_script_task save transcript failed reel=%s: %s", reel_id, e)

        # GATE ÚNICO (bug «videos antiguos no se pueden robar»): toda ruta sin transcript
        # converge aquí. Si hay caption con sustancia → caption-only; si no hay nada
        # transcribible NI caption usable → fallo honesto (única vía de _fail).
        if not transcript_text:
            _cap_g = (reel.get("caption") or "").strip()
            if len(_cap_g) >= 25:
                logger.warning("[steal_degraded] cause=no_transcript reel=%s user=%s caption_chars=%d", reel_id, user_id, len(_cap_g))
                try:
                    from emails import track as _ph_track_deg
                    _ph_track_deg("steal_degraded", user_id, {"cause": "no_transcript", "reel_id": reel_id})
                except Exception:
                    pass
            else:
                return _fail("transcribe_error", "Este reel no tiene audio transcribible ni texto suficiente para generar.")

        _t_tr_end = time.time()   # fin de la fase transcripción (cache-hit ⇒ ~0s)
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
            # 2 OPCIONES en paralelo (modelo de generación = pro), por MARCA. Timeout LARGO:
            # corre en el worker (sin gateway), así pro (40-90s) termina sin cortar ni caer a groq.
            from app import (_generate_script_options, _shape_script_option,  # lazy import (circular).
                             GENERATION_LLM_TIMEOUT)
            _t_llm0 = time.time()
            raw_opts = _generate_script_options(user_content, style_arg, custom_prompt, user_id,
                                                project_id, n=2, timeout=GENERATION_LLM_TIMEOUT)
            _t_llm1 = time.time()
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

        options = [_shape_script_option(r) for r in raw_opts]
        _rec_fmt = raw_opts[0].get("recording_format") or None     # ítem 10
        # FORMATO SUGERIDO SIEMPRE: reintento Flash + heurística si el LLM no clasificó
        # (~26%) → la tarjeta del robo nunca queda vacía. Mismo helper que el path sync.
        try:
            from app import ensure_recording_format  # lazy import (circular)
            _rec_fmt = ensure_recording_format(
                _rec_fmt, caption=caption, transcript=transcript_text,
                duration_sec=reel.get("video_duration_sec"), cached_formato=reel.get("formato"))
        except Exception:
            logger.warning("gen_script_task ensure_recording_format failed reel=%s", reel_id, exc_info=True)
        _pov_text = raw_opts[0].get("pov_text") or None
        _alt_hooks = (options[0]["hooks"][1:] or None)             # los 2 hooks alternativos de la A
        llm_title = options[0]["title"]
        result = options[0]["script"]                              # se guarda la opción A

        # PERF (instrumentación David): desglose real transcripción vs LLM pro. Grep [steal_timing].
        try:
            logger.info("[steal_timing] reel=%s live_transcribe=%d transcribe_sec=%.1f llm_sec=%.1f total_sec=%.1f",
                        reel_id, 1 if _did_live_transcribe else 0,
                        (_t_tr_end - _t_tr_start),
                        ((_t_llm1 or _t_tr_end) - (_t_llm0 or _t_tr_end)),
                        time.time() - _t0)
        except Exception:
            pass

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
            _row = {
                "user_id":                user_id,
                "transcription_id":       None,
                "idea_id":                None,
                "title":                  script_title,
                "script":                 result,
                # P0: la task YA recibía project_id (el endpoint se lo pasa) pero lo tiraba.
                "project_id":             project_id,
                "from_competitor_reel_id": reel["id"],
                "from_competitor_username": ig_username,
                "assistant_name":         _TASK_LABELS.get(style_label, style_label) if style_label else None,
                "alt_hooks":              _alt_hooks,   # 2 hooks alternativos (device distinto)
                "recording_format":       _rec_fmt,     # ítem 10
                # P1: reveal reconstruible siempre (opciones + POV); chosen=None = elección pendiente.
                "gen_options":            {"options": options, "pov_text": _pov_text, "chosen": None},
            }
            try:
                ins = db.table("scripts").insert(_row).execute()
            except Exception as _e1:
                # Degradación segura: columna recording_format/gen_options aún sin migrar → sin ellas.
                if "recording_format" in str(_e1).lower() or "gen_options" in str(_e1).lower():
                    _row.pop("recording_format", None)
                    _row.pop("gen_options", None)
                    ins = db.table("scripts").insert(_row).execute()
                else:
                    raise
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
                    "monthly_usage": (profile.get("monthly_usage") or 0) + SCRIPT_UNITS
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
            "recording_format": _rec_fmt,
            "pov_text": _pov_text,
            "options": options,            # 2 opciones (cada una con 2-3 hooks)
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
                    .select("id, ig_reel_id, transcript, transcript_status, transcript_started_at, "
                            "caption, video_duration_sec")
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
                data = {"model": os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"), "response_format": "json"}
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

    # FORMATO (referencias visuales): clasifica el reel del pool en background — ya estamos
    # en una task, no bloquea nada del usuario. Best-effort + degradación segura si la
    # columna `formato` aún no está migrada en prod.
    try:
        from app import classify_reel_format
        fmt = classify_reel_format(reel.get("caption"), transcript_text, reel.get("video_duration_sec"))
        if fmt:
            try:
                db.table("creator_reels_global").update({"formato": fmt}).eq("id", reel_id).execute()
            except Exception as e:
                if "formato" not in str(e).lower():
                    logger.warning("transcribe_reel formato save failed reel=%s: %s", reel_id, e)
    except Exception:
        logger.warning("transcribe_reel classify failed reel=%s", reel_id, exc_info=True)
    return {"ok": True, "transcript": transcript_text}


@celery_app.task(name="tasks.attach_transcript_to_idea")
def attach_transcript_to_idea_task(idea_id, reel_id):
    """«Guardar idea con datos» (David): transcribe el reel si falta (reusa transcribe_reel_task,
    síncrono DENTRO del worker → sin gateway) y copia el transcript al snapshot de la idea, para
    que la idea guardada conserve sus datos aunque el reel envejezca y salga del pool. Best-effort,
    idempotente (si ya estaba transcrito, transcribe_reel_task devuelve el cacheado)."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        transcribe_reel_task.apply(args=[reel_id])   # eager en el worker: descarga+Groq sin gateway
    except Exception as e:
        logger.warning("[idea_transcript] transcribe failed reel=%s: %s", reel_id, e)
    try:
        reel = (db.table("creator_reels_global").select("transcript, transcript_status")
                  .eq("id", reel_id).single().execute()).data or {}
        if reel.get("transcript_status") == "ok" and (reel.get("transcript") or "").strip():
            db.table("ideas").update({"transcript_snapshot": reel["transcript"].strip()}).eq("id", idea_id).execute()
            logger.info("[idea_transcript] attached idea=%s reel=%s", idea_id, reel_id)
            return {"ok": True, "attached": True}
    except Exception as e:
        logger.warning("[idea_transcript] attach failed idea=%s: %s", idea_id, e)
    return {"ok": True, "attached": False}


_BACKFILL_FORMAT_BATCH = int(os.environ.get("BACKFILL_FORMAT_BATCH", "60"))


@celery_app.task(name="tasks.backfill_reel_formats")
def backfill_reel_formats():
    """Backfill del FORMATO de reels scrapeados antes de la columna (formato IS NULL).
    Clasifica por lotes (caption + transcript si lo hay, modelo flash) para que la
    tarjeta «FORMATO SUGERIDO» tenga referencias del pool global. Idempotente:
    cuando ya no quedan NULL con texto, es no-op. Best-effort por reel."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        rows = (db.table("creator_reels_global")
                  .select("id, caption, transcript, video_duration_sec")
                  .is_("formato", "null").eq("is_archived", False)
                  .order("views", desc=True)            # los más vistos primero (mejores referencias)
                  .limit(_BACKFILL_FORMAT_BATCH).execute()).data or []
    except Exception as e:
        # Columna sin migrar u otro fallo → no-op silencioso.
        if "formato" not in str(e).lower():
            logger.warning("backfill_reel_formats select failed: %s", e)
        return {"ok": False, "classified": 0}
    if not rows:
        return {"ok": True, "classified": 0, "done": True}

    try:
        from app import classify_reel_format
    except Exception as e:
        logger.exception("backfill_reel_formats import failed: %s", e)
        return {"ok": False, "classified": 0}

    classified = 0
    for r in rows:
        cap = (r.get("caption") or "").strip()
        tx = (r.get("transcript") or "").strip()
        if not cap and not tx:
            # Sin texto que clasificar → marca 'desconocido' para no reintentar siempre.
            try:
                db.table("creator_reels_global").update({"formato": "desconocido"}).eq("id", r["id"]).execute()
            except Exception:
                pass
            continue
        try:
            fmt = classify_reel_format(cap, tx, r.get("video_duration_sec"))
        except Exception:
            fmt = None
        try:
            db.table("creator_reels_global").update(
                {"formato": fmt or "desconocido"}).eq("id", r["id"]).execute()
            if fmt:
                classified += 1
        except Exception as e:
            logger.warning("backfill_reel_formats update failed reel=%s: %s", r.get("id"), e)
    logger.info("backfill_reel_formats: lote de %d, clasificados=%d", len(rows), classified)
    return {"ok": True, "classified": classified, "batch": len(rows)}


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


@celery_app.task(name="tasks.adapt_text")
def adapt_task(text, style, custom_prompt, user_id, charge_mode, cost_cents):
    """Async «Hazlo tuyo» (/adapt). Gemini 2.5 Pro tarda >60s en guiones largos y el
    request sync cruzaba el readTimeout (~60s) de Traefik → 502 Bad Gateway. Espeja
    generate_script_competitor_task: corre adapt_with_ai FUERA del request. El gating
    ya lo hizo el endpoint (fast-reject); aquí se genera, se cobra UNA vez (charge_mode)
    bajo lock por-usuario, y se devuelve la MISMA shape que la respuesta sync de antes."""
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        from app import (adapt_with_ai, get_voice_profile, get_profile,
                         acquire_credit_lock, release_credit_lock)
    except Exception as e:
        logger.exception("adapt_task import failed: %s", e)
        return {"ok": False, "error": "import", "message": "Servicio no disponible.", "http": 500}

    # La parte lenta — ya estamos fuera del request HTTP, sin reloj de Traefik.
    try:
        voice = get_voice_profile(user_id) if user_id else None
        result = adapt_with_ai(text, style, custom_prompt, voice=voice, user_id=user_id)
    except Exception as e:
        logger.exception("adapt_task generation failed user=%s: %s", user_id, e)
        return {"ok": False, "error": "llm", "message": "No se pudo generar. Inténtalo de nuevo.", "http": 502}

    # Cobro SOLO tras éxito, bajo lock por-usuario (igual que el endpoint sync).
    if user_id and charge_mode in ("monthly", "credits"):
        lock = acquire_credit_lock(user_id)
        try:
            fresh = get_profile(user_id)
            if charge_mode == "monthly":
                # econ: guión = 3 créditos (monthly_usage cuenta créditos, no guiones).
                db.table("profiles").update({"monthly_usage": (fresh.get("monthly_usage") or 0) + 3}).eq("id", user_id).execute()
            elif charge_mode == "credits":
                db.table("profiles").update({"credits_cents": (fresh.get("credits_cents") or 0) - cost_cents}).eq("id", user_id).execute()
        except Exception as e:
            logger.error("adapt_task charge failed user=%s: %s", user_id, e)
        finally:
            release_credit_lock(user_id, lock)

    out = {"ok": True, "result": result, "cost_cents": cost_cents}
    if user_id:
        updated = get_profile(user_id)
        out["credits_cents"] = updated["credits_cents"]
        out["free_used_today"] = updated["free_used_today"]
    return out


@celery_app.task(name="tasks.voice_auto_derive")
def voice_auto_derive_task(uid, email, project_id):
    """Async «Derivar mi voz de mis reels» (/api/voice/auto-derive). gemini-2.5-pro
    (modelo de razonamiento) + transcribir reels son lentos (>60s posibles) → Traefik
    cerraba a 60s. Espejo EXACTO del flujo sync (mismo cobro por transcripción).
    Devuelve la misma shape que el endpoint sync de antes."""
    import tempfile
    from datetime import datetime, timezone
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    try:
        from app import (_voice_auto_candidates, get_profile, check_monthly_limit,
                         download_audio, transcribe_with_groq, derive_voice_profile,
                         save_voice_profile, get_voice_profile,
                         COST_CENTS, FREE_DAILY_USER, UNLIMITED_EMAILS, _VOICE_AUTO_SAMPLE,
                         _voice_reels_used, _voice_mark_reel, credits_available,
                         _charge_units_locked, VOICE_FREE_REELS, VOICE_REEL_UNITS)
    except Exception as e:
        logger.exception("voice_auto_derive_task import failed: %s", e)
        return {"ok": False, "error": "import", "message": "Servicio no disponible.", "http": 500}

    vids = _voice_auto_candidates(uid)
    if not vids:
        return {"ok": False, "error": "no_videos",
                "message": "No encuentro reels publicados tuyos. Conecta tu Instagram en Métricas.", "http": 404}
    is_unlimited = (email or "").lower() in UNLIMITED_EMAILS
    _user = {"id": uid, "email": email}
    texts, transcribed_now = [], 0
    for v in vids:
        txt = (v.get("transcription") or "").strip()
        if txt:
            texts.append(txt); continue
        if not v.get("ig_url"):
            continue
        # econ: primeros VOICE_FREE_REELS reels de voz GRATIS (de por vida), luego
        # VOICE_REEL_UNITS/reel. Sin presupuesto para el siguiente → para.
        reel_units = 0
        if not is_unlimited and _voice_reels_used(uid) >= VOICE_FREE_REELS:
            reel_units = VOICE_REEL_UNITS
            if credits_available(get_profile(uid)) < reel_units:
                break
        try:
            with tempfile.TemporaryDirectory() as tmp:
                audio_path = download_audio(v["ig_url"], tmp, "instagram")
                txt = transcribe_with_groq(audio_path, None)
        except Exception as e:
            logger.warning("voice_auto_derive_task transcribe failed user=%s err=%s", uid, e)
            continue
        if not (txt or "").strip():
            continue
        try:
            db.table("ig_videos").update({
                "transcription": txt,
                "transcribed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("ig_video_id", v["ig_video_id"]).eq("user_id", uid).execute()
        except Exception:
            pass
        if reel_units and not is_unlimited:
            _err, _r, _ = _charge_units_locked(uid, reel_units, _user)
            if _err:
                break
        if not is_unlimited:
            _voice_mark_reel(uid)
        transcribed_now += 1
        texts.append(txt)

    if not texts:
        return {"ok": False, "error": "no_transcripts",
                "message": "No pude transcribir ninguno de tus reels. Inténtalo de nuevo.", "http": 502}
    vp = derive_voice_profile(texts[:_VOICE_AUTO_SAMPLE])
    if not vp:
        return {"ok": False, "error": "derive_failed",
                "message": "No pude derivar tu voz con esta muestra. Inténtalo de nuevo.", "http": 502}
    raw = vp.get("raw") if isinstance(vp.get("raw"), dict) else dict(vp)
    raw["samples"] = texts[:_VOICE_AUTO_SAMPLE]
    raw["auto_derived"] = True
    vp["raw"] = raw
    save_voice_profile(uid, vp, brand_id=project_id)
    if project_id and not get_voice_profile(uid):
        save_voice_profile(uid, vp)
    return {"ok": True, "confidence": vp.get("confidence"),
            "source_count": len(texts[:_VOICE_AUTO_SAMPLE]), "transcribed_now": transcribed_now,
            "tone": vp.get("tone"), "phrases": vp.get("phrases") or [], "evidence": vp.get("evidence") or []}


# Cuántos reels propios scrapear+transcribir al sembrar la voz en el onboarding.
# ~€0,04/usuario una sola vez (Apify + Groq). Configurable por si se quiere acotar.
_ONBOARDING_SEED_REELS = int(os.environ.get("ONBOARDING_SEED_REELS", "12"))


@celery_app.task(name="tasks.seed_voice_from_handle")
def seed_voice_from_handle_task(uid, handle, project_id=None):
    """SEED del Cerebro en el ONBOARDING (parte A del loop): el usuario mete su handle →
    scrapeamos SU perfil → sus últimos reels → los transcribimos → derivamos la voz Y
    guardamos las transcripciones como few-shot semilla («así escribe ÉL»), para que el
    PRIMER guion ya salga con su patrón real, no genérico. Reusa _scrape_ig_reels +
    download_audio + transcribe_with_groq + derive_voice_profile (todo ya existente).
    Idempotente: si ya hay una voz REAL (samples/auto_derived), no re-gasta."""
    import tempfile
    from datetime import datetime, timezone
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    handle = (handle or "").strip().lstrip("@").lower()
    if not handle:
        return {"ok": False, "error": "no_handle"}
    try:
        from app import (_scrape_ig_reels, download_audio, transcribe_with_groq,
                         derive_voice_profile, save_voice_profile, get_voice_profile)
    except Exception as e:
        logger.exception("seed_voice_from_handle import failed: %s", e)
        return {"ok": False, "error": "import"}

    # Idempotencia: si ya hay voz REAL derivada de reels (samples), no re-gastes.
    try:
        existing = get_voice_profile(uid, project_id) or {}
        eraw = existing.get("raw") if isinstance(existing.get("raw"), dict) else {}
        if eraw.get("samples"):
            return {"ok": True, "skipped": "already_seeded"}
    except Exception:
        pass

    try:
        reels = _scrape_ig_reels([handle], limit=_ONBOARDING_SEED_REELS)
    except Exception as e:
        logger.warning("seed_voice scrape failed handle=%s err=%s", handle, e)
        return {"ok": False, "error": "scrape_failed"}
    if not reels:
        return {"ok": False, "error": "no_reels"}

    texts = []
    for v in reels:
        url = v.get("ig_url")
        if not url:
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                audio_path = download_audio(url, tmp, "instagram")
                txt = transcribe_with_groq(audio_path, None)
        except Exception as e:
            logger.warning("seed_voice transcribe failed handle=%s err=%s", handle, e)
            continue
        if (txt or "").strip():
            texts.append(txt.strip())
        if len(texts) >= _ONBOARDING_SEED_REELS:
            break

    if not texts:
        return {"ok": False, "error": "no_transcripts"}

    vp = derive_voice_profile(texts)
    if not vp:
        return {"ok": False, "error": "derive_failed"}
    raw = vp.get("raw") if isinstance(vp.get("raw"), dict) else dict(vp)
    raw["samples"] = texts[:6]          # few-shot semilla «así escribe él» (inyectado por voice_prompt_block)
    raw["seed_source"] = "onboarding_handle"
    raw["auto_derived"] = True
    vp["raw"] = raw
    try:
        save_voice_profile(uid, vp, brand_id=project_id)
    except Exception as e:
        logger.exception("seed_voice save failed uid=%s err=%s", uid, e)
        return {"ok": False, "error": "save_failed"}
    return {"ok": True, "reels": len(reels), "transcribed": len(texts),
            "confidence": vp.get("confidence")}
