# calls-audio: Sortformer v2.1 (who speaks when) + faster-whisper large-v3-turbo
# (words), CPU only. Versions are the ones the 2026-11-11 bake-off ran.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models OMP_NUM_THREADS=8

RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

# Both models are baked into the image, so a cold start after app sleeping
# loads from disk instead of downloading ~2 GB, and the service never needs a
# Hugging Face token or network access to the hub at run time.
RUN python -c "from huggingface_hub import snapshot_download as d; \
d('nvidia/diar_streaming_sortformer_4spk-v2.1'); d('mobiuslabsgmbh/faster-whisper-large-v3-turbo')"
ENV HF_HUB_OFFLINE=1

COPY pipeline.py app.py ./
CMD ["python", "app.py"]
