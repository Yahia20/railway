# calls-audio: Sortformer v2.1 (who speaks when) + Cohere Transcribe Arabic per
# turn (words; faster-whisper kept as the fallback engine), CPU only.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models OMP_NUM_THREADS=8

RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

# Every model is baked into the image, so a cold start after app sleeping
# loads from disk instead of downloading ~6 GB, and the service never needs
# network access to the hub at run time. Cohere's repo is gated (terms
# accepted on the company's Hugging Face account): its read token is a
# Railway variable that reaches the build only, as HF_TOKEN, and is not kept
# in any ENV of the image.
ARG HF_TOKEN
RUN python -c "from huggingface_hub import snapshot_download as d; \
d('nvidia/diar_streaming_sortformer_4spk-v2.1'); d('mobiuslabsgmbh/faster-whisper-large-v3-turbo')"
RUN HF_TOKEN="$HF_TOKEN" python -c "from huggingface_hub import snapshot_download as d; \
d('CohereLabs/cohere-transcribe-arabic-07-2026', allow_patterns=['*.json','*.safetensors','*.model','*.txt','*.py'])"
ENV HF_HUB_OFFLINE=1

COPY pipeline.py app.py ./
CMD ["python", "app.py"]
