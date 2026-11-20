FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN SECRET_KEY=build-only DEBUG=1 python manage.py collectstatic --noinput
RUN useradd -m app && mkdir -p /data/media && chown -R app /app /data
USER app
ENV MEDIA_ROOT=/data/media PORT=8000
EXPOSE 8000
CMD ["sh", "start.sh"]
