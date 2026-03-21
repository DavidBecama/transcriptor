"""Transcriptor de Reels de Instagram — Web App."""

import os
import tempfile

import requests
import yt_dlp
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def download_reel_audio(url: str, output_dir: str) -> str:
    """Descarga el audio de un reel de Instagram y devuelve la ruta del archivo."""
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
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_path + ".mp3"


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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/transcribe", methods=["POST"])
def transcribe():
    body = request.get_json()
    url = body.get("url", "").strip()
    language = body.get("language", "").strip() or None

    if not url:
        return jsonify({"error": "Debes proporcionar una URL"}), 400

    if not GROQ_API_KEY:
        return jsonify({"error": "GROQ_API_KEY no configurada en el servidor"}), 500

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = download_reel_audio(url, tmpdir)
            text = transcribe_with_groq(audio_path, language)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Error al descargar el reel: {e}"}), 400
    except requests.HTTPError as e:
        return jsonify({"error": f"Error de la API de Groq: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"text": text})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
