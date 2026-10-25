# Test Guide

This directory contains automated tests for the MoneyPrinterTurbo AutoPilot fork.

## Test layout

- `services/` contains service, workflow, UI, and controller tests.
- `test_main.py` covers application entry points.
- `conftest.py` blocks external socket connections during tests while allowing local loopback test servers. Provider-facing tests should mock their clients and HTTP requests.

## Run tests

Install the locked project dependencies from the repository root, then run:

```bash
# Entire test suite
uv run python -X utf8 -m pytest -q test

# One test module
uv run python -X utf8 -m pytest -q test/services/test_voice.py

# One test class or test case
uv run python -X utf8 -m pytest -q test/services/test_voice.py::TestVoiceService
uv run python -X utf8 -m pytest -q test/services/test_voice.py::TestVoiceService::test_parse_gemini_voice_name_supports_new_and_legacy_labels
```

The repository test guard rejects external connections, including calls to paid LLM, TTS, image, and video providers. Do not enable live-provider tests in the normal automated suite. Use deterministic mocks and fixtures instead.

## Add tests

1. Keep each `test_<domain>.py` focused on one subsystem.
2. Use pytest functions or `unittest.TestCase`; pytest collects both.
3. Mock provider clients and network calls. Do not add credentials to fixtures.
4. Put small, redistributable test assets under `test/resources` and document their origin when needed.
