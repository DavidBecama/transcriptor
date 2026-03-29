FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 5555
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5555", "--workers", "2", "--timeout", "300"]
