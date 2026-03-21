"""Transcriptor de Reels y Vídeos — Web App."""

import glob
import os
import re
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone

import requests
import yt_dlp
from flask import Flask, Response, g, jsonify, make_response, render_template, request
from youtube_transcript_api import YouTubeTranscriptApi

app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
APIFY_ACTOR_ID = os.environ.get("APIFY_ACTOR_ID", "scrapearchitect~youtube-audio-mp3-downloader")
DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcriptions.db")


# ── Database ────────────────────────────────────────────────────────────────


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS transcriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            url TEXT NOT NULL,
            platform TEXT NOT NULL,
            language TEXT,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    db.commit()
    db.close()


def get_session_id() -> str:
    """Devuelve el session_id de la cookie, o genera uno nuevo."""
    return request.cookies.get("session_id", "")


# ── Helpers ─────────────────────────────────────────────────────────────────


def detect_platform(url: str) -> str:
    """Detecta la plataforma a partir de la URL."""
    if "instagram.com" in url:
        return "instagram"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "tiktok.com" in url:
        return "tiktok"
    return "otro"


def extract_youtube_id(url: str) -> str | None:
    """Extrae el video ID de una URL de YouTube."""
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def get_youtube_transcript(url: str, language: str | None = None) -> str | None:
    """Intenta obtener subtítulos de YouTube directamente (sin descargar audio)."""
    video_id = extract_youtube_id(url)
    if not video_id:
        return None
    try:
        ytt_api = YouTubeTranscriptApi()
        if language:
            transcript = ytt_api.fetch(video_id, languages=[language])
        else:
            transcript = ytt_api.fetch(video_id)
        return " ".join(snippet.text for snippet in transcript.snippets)
    except Exception:
        return None


def download_audio_apify(url: str, output_dir: str) -> str:
    """Descarga audio de YouTube usando Apify como fallback."""
    if not APIFY_API_TOKEN:
        raise RuntimeError("APIFY_API_TOKEN no configurada. Necesaria para descargar vídeos de YouTube.")

    # Iniciar el actor de Apify (formato compatible con web.harvester~youtube-downloader)
    run_url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/runs?token={APIFY_API_TOKEN}"
    # Formato para scrapearchitect~youtube-audio-mp3-downloader
    input_data = {
        "video_urls": [{"url": url}],
    }
    resp = requests.post(run_url, json=input_data, timeout=30)
    if resp.status_code == 400:
        # Formato alternativo para otros actores
        input_data = {"youtubeUrls": [url]}
        resp = requests.post(run_url, json=input_data, timeout=30)
    resp.raise_for_status()
    run_data = resp.json()["data"]
    run_id = run_data["id"]

    # Esperar a que termine (polling con timeout de 120s)
    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_TOKEN}"
    for _ in range(60):
        time.sleep(2)
        status_resp = requests.get(status_url, timeout=15)
        status_resp.raise_for_status()
        status = status_resp.json()["data"]["status"]
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Error de Apify: el actor terminó con estado {status}")
    else:
        raise RuntimeError("Apify actor tardó demasiado")

    # Obtener resultados del dataset
    dataset_id = run_data["defaultDatasetId"]
    items_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_API_TOKEN}"
    items_resp = requests.get(items_url, timeout=30)
    items_resp.raise_for_status()
    items = items_resp.json()

    # Buscar URL de descarga en los resultados (los actores usan diferentes nombres de campo)
    download_url = None
    url_fields = ["downloadable_audio_link", "downloadUrl", "mediaUrl", "audioUrl", "fileUrl", "link", "mp3Url", "merged_downloadable_link", "downloadable_video_link"]
    for item in (items if items else []):
        for field in url_fields:
            candidate = item.get(field, "")
            if candidate and candidate.startswith("http"):
                download_url = candidate
                break
        if download_url:
            break

    if not download_url:
        # Intentar obtener del key-value store
        kv_store_id = run_data["defaultKeyValueStoreId"]
        kv_url = f"https://api.apify.com/v2/key-value-stores/{kv_store_id}/records/OUTPUT?token={APIFY_API_TOKEN}"
        kv_resp = requests.get(kv_url, timeout=30)
        if kv_resp.status_code == 200:
            content_type = kv_resp.headers.get("content-type", "")
            if "audio" in content_type or "video" in content_type or "octet-stream" in content_type:
                audio_path = os.path.join(output_dir, "audio.mp3")
                with open(audio_path, "wb") as f:
                    f.write(kv_resp.content)
                return audio_path
            # Puede ser JSON con la URL
            try:
                kv_data = kv_resp.json()
                if isinstance(kv_data, dict):
                    for field in url_fields:
                        candidate = kv_data.get(field, "")
                        if candidate and candidate.startswith("http"):
                            download_url = candidate
                            break
            except ValueError:
                pass

    if not download_url:
        app.logger.error("Apify items: %s", items)
        raise RuntimeError("No se encontró URL de descarga en los resultados de Apify")

    # Descargar el archivo de audio/video
    audio_resp = requests.get(download_url, timeout=120, stream=True)
    audio_resp.raise_for_status()
    audio_path = os.path.join(output_dir, "audio.mp3")
    with open(audio_path, "wb") as f:
        for chunk in audio_resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return audio_path


def download_audio(url: str, output_dir: str) -> str:
    """Descarga el audio de un vídeo y devuelve la ruta del archivo."""
    output_path = os.path.join(output_dir, "audio")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "mweb"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    # Buscar el archivo generado (puede variar la extensión según el cliente)
    candidates = glob.glob(os.path.join(output_dir, "audio.*"))
    if not candidates:
        raise FileNotFoundError("No se pudo generar el archivo de audio")
    return candidates[0]


def transcribe_with_groq(audio_path: str, language: str | None = None) -> str:
    """Envía el audio a la API de Groq Whisper y devuelve el texto."""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    with open(audio_path, "rb") as f:
        files = {"file": ("audio.mp3", f, "audio/mpeg")}
        data = {"model": "whisper-large-v3", "response_format": "json"}
        if language:
            data["language"] = language
        resp = requests.post(
            GROQ_TRANSCRIPTION_URL, headers=headers, files=files, data=data, timeout=120
        )
    resp.raise_for_status()
    return resp.json()["text"]


# ── Routes ──────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    resp = make_response(render_template("index.html"))
    if "session_id" not in request.cookies:
        resp.set_cookie("session_id", uuid.uuid4().hex, max_age=60 * 60 * 24 * 365, httponly=True, samesite="Lax")
    return resp


@app.route("/transcribe", methods=["POST"])
def transcribe():
    body = request.get_json()
    url = body.get("url", "").strip()
    language = body.get("language", "").strip() or None

    if not url:
        return jsonify({"error": "Debes proporcionar una URL"}), 400

    platform = detect_platform(url)
    if platform == "youtube" and not any(p in url for p in ["/watch", "youtu.be/", "/shorts/", "/reel"]):
        return jsonify({"error": "URL no válida. Pega la URL de un vídeo específico de YouTube."}), 400
    if platform == "instagram" and "/reel" not in url and "/p/" not in url:
        return jsonify({"error": "URL no válida. Pega la URL de un reel o publicación de Instagram."}), 400
    if platform == "otro":
        return jsonify({"error": "Plataforma no soportada. Usa URLs de YouTube, Instagram o TikTok."}), 400

    try:
        # Para YouTube: intentar obtener subtítulos directamente (rápido y gratis)
        if platform == "youtube":
            text = get_youtube_transcript(url, language)
            if text:
                # Guardar en historial y devolver
                sid = get_session_id()
                if sid:
                    db = get_db()
                    db.execute(
                        "INSERT INTO transcriptions (session_id, url, platform, language, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (sid, url, platform, language, text, datetime.now(timezone.utc).isoformat()),
                    )
                    db.commit()
                return jsonify({"text": text, "platform": platform})

        if not GROQ_API_KEY:
            return jsonify({"error": "GROQ_API_KEY no configurada en el servidor"}), 500

        # Descargar audio y transcribir con Groq
        with tempfile.TemporaryDirectory() as tmpdir:
            if platform == "youtube":
                # Usar Apify para YouTube (yt-dlp bloqueado en servidores)
                audio_path = download_audio_apify(url, tmpdir)
            else:
                # yt-dlp para Instagram/TikTok
                audio_path = download_audio(url, tmpdir)
            text = transcribe_with_groq(audio_path, language)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Error al descargar el vídeo: {e}"}), 400
    except requests.HTTPError as e:
        # No exponer URLs con tokens en el mensaje de error
        status_code = e.response.status_code if e.response is not None else 0
        if "groq" in str(e.request.url).lower() if e.request else False:
            return jsonify({"error": f"Error de la API de Groq (HTTP {status_code})"}), 502
        return jsonify({"error": f"Error al conectar con servicio externo (HTTP {status_code})"}), 502
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "Error interno del servidor"}), 500

    # Guardar en historial
    sid = get_session_id()
    if sid:
        db = get_db()
        db.execute(
            "INSERT INTO transcriptions (session_id, url, platform, language, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, url, platform, language, text, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()

    return jsonify({"text": text, "platform": platform})


@app.route("/history")
def history():
    sid = get_session_id()
    if not sid:
        return jsonify([])
    db = get_db()
    rows = db.execute(
        "SELECT id, url, platform, language, text, created_at FROM transcriptions WHERE session_id = ? ORDER BY id DESC LIMIT 50",
        (sid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/history/<int:tid>", methods=["DELETE"])
def delete_transcription(tid: int):
    sid = get_session_id()
    db = get_db()
    db.execute("DELETE FROM transcriptions WHERE id = ? AND session_id = ?", (tid, sid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/download/<int:tid>")
def download_transcription(tid: int):
    sid = get_session_id()
    db = get_db()
    row = db.execute("SELECT url, text, created_at FROM transcriptions WHERE id = ? AND session_id = ?", (tid, sid)).fetchone()
    if not row:
        return jsonify({"error": "No encontrado"}), 404

    content = f"URL: {row['url']}\nFecha: {row['created_at']}\n\n{row['text']}"
    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename=transcripcion_{tid}.txt"},
    )


# ── Init ────────────────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=True, host="0.0.0.0", port=port)
