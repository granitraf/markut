# Markut web app — one container: FastAPI + the debate engine.
# Runs on Hugging Face Spaces (free Docker tier, 16 GB RAM) and on Railway.
# CPU-only torch keeps the image a fraction of the default CUDA build; the two
# local models are downloaded at BUILD time so a cold start does not wait on
# Hugging Face.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false \
    MARKUT_PRODUCTION=1 \
    WARM_MODELS=1 \
    PORT=7860

# torch first, from the CPU wheel index — sentence-transformers then sees it
# satisfied and does not pull the multi-GB CUDA build
COPY requirements.txt /tmp/requirements.txt
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r /tmp/requirements.txt

# Spaces run the container as uid 1000 with no root; everything the app
# writes (model cache, edgar/news caches) must belong to that user
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    HF_HOME=/home/user/.hf \
    PATH=/home/user/.local/bin:$PATH
WORKDIR /app

COPY --chown=user markut ./markut
COPY --chown=user run.py README.md ./

# bake the embedding + reranking models into the image (names single-sourced
# from markut.config so a model change here cannot drift from the app)
RUN python -c "from markut import config; \
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer(config.EMBEDDER_MODEL); CrossEncoder(config.RERANKER_MODEL); \
print('models cached')"

# edgar/news caches regenerate on an ephemeral disk; the run log is Supabase
RUN mkdir -p edgar_cache news_cache

# Spaces expects app_port (README metadata) = 7860; Railway injects its own PORT
EXPOSE 7860
CMD ["python", "-m", "markut.web"]
