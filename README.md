# Transcriptor de Reels de Instagram

CLI que descarga reels de Instagram y los transcribe usando OpenAI Whisper.

## Requisitos

- Python 3.10+
- FFmpeg instalado en el sistema

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
# Uso básico
python transcriptor.py https://www.instagram.com/reel/XXXXX/

# Elegir modelo de Whisper (tiny, base, small, medium, large)
python transcriptor.py https://www.instagram.com/reel/XXXXX/ -m medium

# Especificar idioma
python transcriptor.py https://www.instagram.com/reel/XXXXX/ -l es

# Guardar transcripción en archivo
python transcriptor.py https://www.instagram.com/reel/XXXXX/ -o transcripcion.txt
```

## Modelos de Whisper

| Modelo | Tamaño | Velocidad | Calidad |
|--------|--------|-----------|---------|
| tiny   | 39 MB  | Muy rápido | Baja |
| base   | 74 MB  | Rápido    | Media |
| small  | 244 MB | Medio     | Buena |
| medium | 769 MB | Lento     | Muy buena |
| large  | 1550 MB| Muy lento | Excelente |
