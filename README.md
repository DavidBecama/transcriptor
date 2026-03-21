# Transcriptor de Reels de Instagram

Web app que descarga reels de Instagram y los transcribe usando la API de Groq (Whisper).

## Requisitos

- Python 3.10+
- FFmpeg instalado en el sistema
- API key de [Groq](https://console.groq.com/keys) (tier gratuito disponible)

## Instalación

```bash
pip install -r requirements.txt
```

Para instalar FFmpeg:
```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

## Uso

```bash
# Configura tu API key de Groq
export GROQ_API_KEY="tu_api_key_aqui"

# Arranca el servidor
python app.py
```

Abre http://localhost:5000 en tu navegador, pega la URL del reel y listo.
