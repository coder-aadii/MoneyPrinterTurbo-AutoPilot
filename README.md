# MoneyPrinterTurbo AutoPilot

This project is a customized fork of the original [MoneyPrinterTurbo repository](https://github.com/harry0703/MoneyPrinterTurbo), maintained here as **MoneyPrinterTurbo AutoPilot** for short-video creation and channel automation.

MoneyPrinterTurbo turns a topic or a prepared script into a short video. Depending on your configuration, it can draft narration, find or generate visuals, synthesize speech, create subtitles, add optional background music, and render the result. It includes a Streamlit WebUI, a FastAPI service, and a command-line interface.

## What this fork includes

- **Create Video:** generate a script from a topic or edit a prepared script, choose duration, format, language, voice, and visual style, then render a video.
- **Visual styles:** stock-footage workflows, ASMR cutting, and the Savage Animated Explainer with the project’s 22-pose Facts Sun Le Bhai character pack.
- **Hindi and Hinglish support:** script normalization, pronunciation preflight, and word-level subtitle alignment for supported workflows.
- **Autopilot foundation:** channel profiles, scheduled jobs, persistent local state, worker recovery, and supported YouTube upload/scheduling paths. See the operations guide for setup and limitations. This is a single-host SQLite design; do not treat the UI as a guarantee of unattended publishing or three successful posts every day.
- **Provider flexibility:** text, speech, media, and optional background music depend on the providers you configure. Provider calls may incur charges.

Character pack origin and inclusion notes are documented in [`resource/characters/facts_sun_le_bhai/PROVENANCE.md`](resource/characters/facts_sun_le_bhai/PROVENANCE.md). General project license: [MIT](LICENSE). Third-party resources retain their own license notices.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- FFmpeg available to the application for media processing
- API credentials for the providers you choose to use

`uv sync` installs the locked Python dependencies. It does not configure provider credentials or download every optional model. Some integrations download model weights or other assets when first used.

## Quick start

Run these commands from the repository root. Create a local configuration from the safe example, then add only the provider settings and credentials you need.

### Windows PowerShell

```powershell
uv python install 3.11
uv sync --frozen
Copy-Item config.example.toml config.toml
uv run mpt webui
```

### macOS or Linux

```bash
uv python install 3.11
uv sync --frozen
cp config.example.toml config.toml
uv run mpt webui
```

Open the local address printed by the WebUI launcher (normally `http://127.0.0.1:8501`). You can also use `webui.bat` on Windows or `./webui.sh` on macOS/Linux after installing dependencies.

Keep `config.toml`, `.env`, OAuth credentials, tokens, and generated content private. They are local runtime data and should not be committed.

## Other launch paths

```bash
# FastAPI server; API documentation is available at /docs
uv run python main.py

# CLI help and a local prepared-script example
uv run mpt cli --help
uv run mpt cli --video-script "A short narration topic" --stop-at script

# Persistent Autopilot worker, when you need a separate worker process
uv run mpt-autopilot-worker
```

The WebUI, API, and worker must use the same configuration and persistent `storage` directory when sharing Autopilot state. The Autopilot SQLite database is intended for processes on one host, not a multi-host network filesystem. Back it up consistently along with the encryption key needed to read locally stored OAuth credentials. Read [`docs/implementation/multi-channel-autopilot-operations.md`](docs/implementation/multi-channel-autopilot-operations.md) before connecting accounts or enabling scheduled uploads.

## Configuration

1. Copy `config.example.toml` to `config.toml`.
2. Choose an LLM provider and configure its API key and model as needed.
3. Configure speech and visual providers separately; a text-provider key does not automatically enable TTS or media generation.
4. Configure optional Redis, YouTube OAuth, or other integrations only if you use them.

The example file contains defaults and empty credential fields. Do not paste secrets into screenshots, issue reports, or committed configuration files. Check provider pricing and terms before enabling a paid service.

## Tests and lint

Install the locked development dependencies with `uv sync --frozen`, then run:

```bash
uv run python -X utf8 -m pytest -q test
uv run --no-sync ruff check app cli.py main.py webui test docs/skill
```

The test suite blocks external socket connections. Provider-facing tests should use mocks; tests that require live paid APIs are not part of the normal automated run.

## Documentation

- [Autopilot operations and limitations](docs/implementation/multi-channel-autopilot-operations.md)
- [Video project workflow](docs/video-projects.md)
- [Local image server](docs/local-image-server/README.md)
- [Packaging](docs/packaging.md)
- [Test guide](test/README.md)
- [Facts Sun Le Bhai asset provenance](resource/characters/facts_sun_le_bhai/PROVENANCE.md)

## Project

This repository is maintained at [coder-aadii/MoneyPrinterTurbo-AutoPilot](https://github.com/coder-aadii/MoneyPrinterTurbo-AutoPilot). Please report issues and improvements there. It is a customized fork; upstream release badges, sponsorship offers, sample galleries, and support links are not maintained by this repository.
