FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin timeline \
    && mkdir -p /data && chown timeline:timeline /data
# Starts as root only to fix the volume's owner, then runs the server as `timeline`.
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Every account, key, and page lives in /data: mount a persistent volume there (docker-compose
# does; on Railway attach a volume at /data). No VOLUME line, because Railway rejects Dockerfiles that have one.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('PORT', '8000'), timeout=4)"

# Platforms like Railway pick the port through $PORT.
CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
