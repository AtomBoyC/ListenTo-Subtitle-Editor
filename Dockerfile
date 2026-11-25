FROM python:3.12-slim

LABEL org.opencontainers.image.title="ListenTo Studio" \
      org.opencontainers.image.description="Modern AI Bilingual Subtitle Editor, ASR Transcriber & Context-Reflective Translator" \
      org.opencontainers.image.source="https://github.com/AtomBoyC/ListenTo-Subtitle-Editor" \
      org.opencontainers.image.version="2.4.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LISTENTO_IN_DOCKER=1 \
    LISTENTO_DATA_DIR=/data \
    LISTENTO_EXPORTS_DIR=/exports \
    PORT=8765

WORKDIR /app

# Create non-root runtime user and persistent volume mount points
RUN groupadd -g 1000 appuser && \
    useradd -m -u 1000 -g appuser appuser && \
    mkdir -p /data /exports && \
    chown -R appuser:appuser /app /data /exports

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser server.py ./
COPY --chown=appuser:appuser core/ ./core/
COPY --chown=appuser:appuser static/ ./static/
COPY --chown=appuser:appuser samples/ ./samples/

VOLUME ["/data", "/exports"]
EXPOSE 8765

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; p = os.environ.get('PORT', '8765'); urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=3)" || exit 1

CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8765}"]
