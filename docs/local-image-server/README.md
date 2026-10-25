# Local image server for `openai_image`

The optional local image server generates one image per script segment through an OpenAI-compatible image endpoint. It can reduce per-image provider costs by running the image model on your own GPU. It is a separate optional service; the main MPT application does not install its GPU stack or model weights.

The server exposes `/v1/images/generations` and defaults to [RealVisXL V5.0 Lightning](https://huggingface.co/SG161222/RealVisXL_V5.0_Lightning). The first model use downloads roughly 6.6 GB of weights. Store those weights outside the Git repository. A local GPU and compatible PyTorch build are required for practical performance.

The performance figures below were measured on one RTX 5070 Ti (16 GB) and are illustrative, not a hardware guarantee:

- About 3.9 seconds per 832×1216 image over HTTP after warm-up.
- About 5–7 minutes for an 8-scene, 50-second video, including voice, Whisper subtitles, and rendering; online TTS accounts for much of the variation.

## Setup

Create a separate Python 3.11 environment for this service. Install the PyTorch build appropriate for your operating system and CUDA version using the [official PyTorch instructions](https://pytorch.org/get-started/locally/), then install this service's requirements.

### Windows PowerShell

```powershell
cd docs/local-image-server
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install torch
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python server.py
```

### macOS or Linux

```bash
cd docs/local-image-server
python3.11 -m venv .venv
.venv/bin/python -m pip install torch
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

Select the PyTorch wheel for your hardware before installing `torch`; the generic command above may install a CPU-only build. The server listens on `http://127.0.0.1:7861` by default. `uvicorn server:app --port 7861` is an alternative. A `size` that is not `WIDTHxHEIGHT` with positive integers receives a 422 response.

Tests run from the repository root without a GPU or model download:

```bash
uv run python -X utf8 -m pytest -q test/services/test_local_image_policy.py test/services/test_local_image_server.py
```

The tests exercise the HTTP app with a stand-in image pipeline and fake Ollama responses.

## MPT configuration

Set these fields in your local `config.toml`:

```toml
[app]
video_source = "openai_image"
openai_image_base_url = "http://127.0.0.1:7861/v1"
openai_image_model = "realvisxl-v5-lightning"
match_materials_to_script = true
video_clip_duration = 6
```

When `match_materials_to_script` is enabled, MPT can request up to eight ordered visual keywords for the narration. The local server creates one image per keyword; images are then used as scenes in the rendered video. Scene count and clip duration determine how much narration the visuals can cover.

## Sharing a GPU with Ollama

On the tested 16 GB GPU, SDXL used about 6.7 GB for weights and peaked near 10.6 GB while generating an image. The tested `qwen3:14b` Ollama model used about 9 GB, so they cannot stay on the GPU together. The server checks loaded Ollama models before image generation, releases PyTorch's cached buffers after requests, and moves SDXL to the CPU after the configured idle period.

This coordination cannot prevent Ollama from loading a model while an image is already being generated. Concurrent LLM and image work on the same limited GPU is not supported; memory pressure may slow generation or cause failures.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `IMAGE_MODEL` | `SG161222/RealVisXL_V5.0_Lightning` | Hugging Face SDXL model identifier |
| `IMAGE_STEPS` | `6` | Diffusion steps |
| `IMAGE_GUIDANCE` | `1.5` | Guidance scale |
| `IMAGE_IDLE_SECONDS` | `90` | Idle time before the model moves to CPU |
| `IMAGE_IDLE_CHECK_SECONDS` | `10` | Idle-worker polling interval |
| `IMAGE_PORT` | `7861` | Port used by `python server.py` |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API base URL |
| `OLLAMA_UNLOAD_TIMEOUT` | `30` | Time to wait for Ollama to unload models |
| `CONTENT_POLICY` | `policy.toml` | Policy file path, or `off` to disable it |

## Content policy

Scene keywords come from an LLM and can request imagery that needs careful handling. Before a prompt reaches the image model, `policy.toml` can replace selected scenes with symbolic alternatives. Review and adapt that policy to your audience. Keyword rules can miss unlisted phrasing or match names unintentionally; they are not a substitute for reviewing generated images.
