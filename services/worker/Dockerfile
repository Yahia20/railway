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

# Bind :: — Railway's private network is IPv6-only, so a service listening on
# 0.0.0.0 is unreachable at <service>.railway.internal no matter what the
# healthcheck says. `::` is dual-stack on Linux and serves IPv4 as well.
#
# ${PORT:-8000} keeps this runnable with plain `docker run`, and exec makes
# uvicorn PID 1 so it receives SIGTERM and shuts down cleanly on redeploy.
CMD ["sh", "-c", "exec uvicorn app.main:app --host :: --port ${PORT:-8000}"]
