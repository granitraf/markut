"""Markut on Modal — free Starter plan ($30/month compute credits, no card).

    pip install modal && modal setup              # once: browser login
    modal secret create markut ANTHROPIC_API_KEY=... FMP_API_KEY=... \
        DATABASE_URL=... CONSOLE_PASSWORD=...        # once
    modal deploy modal_app.py                     # -> https://<workspace>--markut-web.modal.run

WHY Modal: it runs a real container with a long-lived process (the debate
takes minutes and needs ~1 GB for the models), scales to zero when idle so
the free credits cover a demo site many times over, and serves our FastAPI
app unchanged through an ASGI endpoint. The models are baked into the image
at build time so a cold start is seconds, not minutes.
"""
import os

import modal

MODELS_DIR = "/models"   # HF cache baked into the image


def _bake_models():
    # runs at IMAGE BUILD time: download both local models into the image so
    # a cold container never waits on the Hugging Face hub
    from sentence_transformers import CrossEncoder, SentenceTransformer

    from markut import config
    SentenceTransformer(config.EMBEDDER_MODEL)
    CrossEncoder(config.RERANKER_MODEL)
    print("models cached in", os.environ["HF_HOME"])


image = (
    modal.Image.debian_slim(python_version="3.12")
    .env({
        "HF_HOME": MODELS_DIR, "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false",
        "PYTHONUNBUFFERED": "1",
        # production behaviour: console fails closed without CONSOLE_PASSWORD,
        # models warmed at start, bind 0.0.0.0
        "MARKUT_PRODUCTION": "1", "WARM_MODELS": "1",
    })
    # CPU-only torch first (a fraction of the CUDA build), then the app deps
    .pip_install("torch", extra_index_url="https://download.pytorch.org/whl/cpu")
    .pip_install_from_requirements("requirements.txt")
    # the package is copied INTO the image (copy=True) so the model-baking
    # step can import markut.config — model names stay single-sourced
    .add_local_dir("markut", remote_path="/root/markut", copy=True)
    .run_function(_bake_models)
)

app = modal.App("markut", image=image)


@app.function(
    # the Starter plan's credits cover this easily: the container only bills
    # while it is up, and it scales to zero after scaledown_window idle seconds
    cpu=2.0,
    memory=2048,
    timeout=60 * 30,                 # a web request may stream for up to 30 min
    scaledown_window=60 * 10,        # stay warm 10 min after the last request
    max_containers=1,                # one process: the live-debate lock + warmed models live in memory
    secrets=[modal.Secret.from_name("markut")],
)
@modal.concurrent(max_inputs=20)     # one container serves many viewers; live runs are still one at a time
@modal.asgi_app()
def web():
    # the SAME FastAPI app the CLI runner and Railway/Spaces use — nothing forked
    os.chdir("/root")                # caches (edgar_cache/, news_cache/) land on the container's scratch disk
    from markut.web.app import app as fastapi_app
    return fastapi_app
