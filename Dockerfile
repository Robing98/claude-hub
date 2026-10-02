FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HUB_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"

# The server needs no root rights, and transcripts can contain secrets.
RUN useradd --system --uid 10001 hub && mkdir /data && chown hub /data
USER hub

VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz')"

CMD ["claude-hub", "serve", "--host", "0.0.0.0", "--port", "8787"]
