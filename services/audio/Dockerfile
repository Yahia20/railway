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
# in any ENV of the image. Never write "$HF_TOKEN" into a RUN line: the build
# log prints each step with its arguments expanded. An ARG already reaches the
# step as an environment variable, and huggingface_hub reads HF_TOKEN itself.
ARG HF_TOKEN
RUN python -c "from huggingface_hub import snapshot_download as d; \
d('nvidia/diar_streaming_sortformer_4spk-v2.1'); d('mobiuslabsgmbh/faster-whisper-large-v3-turbo'); \
d('nvidia/Nemotron-3-Diarization', allow_patterns=['*.nemo','*.json','*.md'])"
# Nemotron-3-Diarization needs NeMo after 3.0.0 (rope attention in the
# Transformer encoder). One pinned commit of NeMo main (2026-11-03), in its own
# folder: the 3.0.0 install Sortformer runs on is untouched, and this copy is
# only put on sys.path when DIARISER=nemotron.
RUN pip install --no-deps --target /opt/nemo-main \
    https://github.com/NVIDIA/NeMo/archive/0c7af3351eb73ec7ad566f770623023e71187c92.zip \
    lhotse==2.0.0a6
# lhotse 2.0.0a6 is what that commit's pyproject pins (3.0.0 runs on 1.33).
RUN python -c "from huggingface_hub import snapshot_download as d; \
d('CohereLabs/cohere-transcribe-arabic-07-2026', allow_patterns=['*.json','*.safetensors','*.model','*.txt','*.py'])"
ENV HF_HUB_OFFLINE=1

COPY pipeline.py app.py ./
CMD ["python", "app.py"]
