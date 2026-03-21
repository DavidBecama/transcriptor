"""Transcriptor de Reels y Vídeos — Web App."""

import glob
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone

import requests
import yt_dlp
from flask import Flask, Response, g, jsonify, make_response, render_template, request

app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
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

    if not GROQ_API_KEY:
        return jsonify({"error": "GROQ_API_KEY no configurada en el servidor"}), 500

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = download_audio(url, tmpdir)
            text = transcribe_with_groq(audio_path, language)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Error al descargar el vídeo: {e}"}), 400
    except requests.HTTPError as e:
        return jsonify({"error": f"Error de la API de Groq: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
