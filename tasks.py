import os
import tempfile
import requests
import yt_dlp
from celery import Celery
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("reelscript", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    task_track_started=True,
)

def detect_platform(url):
    if "instagram.com" in url:
        return "instagram"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "tiktok.com" in url:
        return "tiktok"
    return "otro"

def _ytdlp(url, output_dir):
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

def _apify_instagram(url, output_dir):
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

def download_audio(url, output_dir, platform):
    APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
    if platform == "instagram" and APIFY_TOKEN:
        try:
            return _apify_instagram(url, output_dir)
        except Exception:
            pass
    return _ytdlp(url, output_dir)

@celery_app.task(bind=True)
def transcribe_task(self, url, language, user_id, ip):
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    COST_CENTS = 8

    db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    platform = detect_platform(url)

    self.update_state(state="PROGRESS", meta={"step": "Descargando audio..."})

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = download_audio(url, tmpdir, platform)

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
        return {"ok": False, "error": str(e)}

    db.table("transcriptions").insert({
        "user_id": user_id,
        "ip": ip if not user_id else None,
        "url": url,
        "platform": platform,
        "language": language,
        "text": text,
        "cost_cents": COST_CENTS if user_id else 0,
    }).execute()

    return {"ok": True, "text": text, "platform": platform}
