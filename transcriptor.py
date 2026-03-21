#!/usr/bin/env python3
"""Transcriptor de Reels de Instagram usando yt-dlp + Whisper."""

import argparse
import os
import sys
import tempfile

import whisper
import yt_dlp


def download_reel(url: str, output_path: str) -> str:
    """Descarga el audio de un reel de Instagram."""
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


def transcribe_audio(audio_path: str, model_name: str, language: str | None) -> dict:
    """Transcribe el audio usando Whisper."""
    model = whisper.load_model(model_name)
    options = {}
    if language:
        options["language"] = language
    result = model.transcribe(audio_path, **options)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Transcriptor de Reels de Instagram"
    )
    parser.add_argument("url", help="URL del reel de Instagram")
    parser.add_argument(
        "-m",
        "--model",
        default="base",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Modelo de Whisper a usar (default: base)",
    )
    parser.add_argument(
        "-l", "--language", default=None, help="Idioma del audio (ej: es, en, fr)"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="Archivo de salida para la transcripción"
    )

    args = parser.parse_args()

    print(f"Descargando audio de: {args.url}")
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_file = os.path.join(tmpdir, "audio")
        try:
            audio_path = download_reel(args.url, audio_file)
        except Exception as e:
            print(f"Error al descargar el reel: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Transcribiendo con modelo '{args.model}'...")
        try:
            result = transcribe_audio(audio_path, args.model, args.language)
        except Exception as e:
            print(f"Error al transcribir: {e}", file=sys.stderr)
            sys.exit(1)

    text = result["text"].strip()

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Transcripción guardada en: {args.output}")
    else:
        print("\n--- Transcripción ---")
        print(text)


if __name__ == "__main__":
    main()
