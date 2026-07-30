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

ENV WORK_DIR=/tmp/customer360 \
    PORT=8000
EXPOSE 8000

# No Docker HEALTHCHECK on purpose. Railway probes `healthcheckPath` itself, and
# a second probe with a hardcoded port would report the container unhealthy the
# moment Railway assigns a different $PORT.

# Exec form, no shell, no variable expansion anywhere in the command.
#
# Railway tokenizes a service's start command and runs it WITHOUT a shell, so
# `--port $PORT` and `--port ${PORT:-8000}` both arrive at uvicorn as literal
# text and it exits immediately. app/serve.py reads PORT and HOST from the
# environment itself, which behaves the same however it is invoked.
CMD ["python", "-m", "app.serve"]
