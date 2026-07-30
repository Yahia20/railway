# Deliberately slim. No ffmpeg: Asterisk writes 8 kHz 16-bit PCM WAV, which the
# stdlib `wave` module reads directly. Adding ffmpeg would cost build minutes on
# every deploy to decode a format we never receive.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code change does not invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 worker && chown -R worker:worker /app
USER worker

ENV WORK_DIR=/tmp/customer360
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# Railway supplies $PORT. Single worker: the container is I/O-bound on model
# calls, and Railway scales by replica rather than by process.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
