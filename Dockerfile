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

# Every account, key, and page lives here: mount a persistent volume.
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
