# ASMR Corrections Validation

Validation date: 2026-10-23.

## Changed files

- `app/models/schema.py`, `app/controllers/v1/llm.py`, `app/services/llm.py`, `app/services/task.py`: validated per-task category selection; ASMR terms now come from deterministic category queries, independent of narration and script-order matching; API `/terms` propagates category settings.
- `app/services/asmr.py`, `app/services/material.py`: deterministic category query profiles; category-by-category bounded search; metadata score/dedup and source-page dedup; duration-aware top-up; explicit insufficiency errors; rank order is retained for candidate choice and only final playback paths are shuffled in random mode.
- `app/services/material.py`, `app/services/material_cache.py`: preserve descriptions, relevance score, selection reason, public attribution, preview and rendition metadata; remove Pixabay's synthetic `title=tags` value. Cache writes use version 3, while ordinary searches continue to read version 2 entries; an ASMR lookup refreshes an older entry only when it lacks the metadata needed for ranking.
- `app/services/video.py`: legacy shortage looping remains the default; ASMR tasks explicitly disable it and fail if clip processing leaves less than the renderer's measured audio-plus-margin duration.
- `cli.py`, `webui/Main.py`, `webui/i18n/en.json`, `webui/i18n/zh.json`: category profile selection passes through CLI, API/task schema, WebUI and batch `VideoParams`.
- `test/services/test_asmr_stock.py`, `test/services/test_controller_llm.py`, `test/services/test_cli.py`, `test/services/test_webui_i18n.py`: regression coverage for the new contract and caller propagation.
- `docs/research/multi-channel-autopilot-readiness.md`: separate architecture readiness assessment.

## Behavior

The deterministic defaults are `kinetic_sand`, then `soap`. Example independent queries:

- `kinetic_sand` → `kinetic sand cutting`, `kinetic sand slicing`
- `soap` → `soap cutting`, `soap shaving`
- `foam` → `foam slicing`, `sponge cutting`
- `jelly` → `jelly slicing`, `gelatin cutting`

Subject and narration text are not passed into those queries. Custom fact terms are ignored in ASMR mode; the explicit selected categories are authoritative. `match_materials_to_script` and TwelveLabs subject reranking do not affect ASMR selection. Non-ASMR term generation and selection retain their existing path.

For each category in caller priority order, MPT makes at most two search queries (up to 12 total for all six supported categories), filters through existing provider duration/aspect validity, then ranks metadata-confirmed candidates and removes duplicate provider IDs, canonical media URLs, and source-page URLs across the accumulated categories. A candidate scores 8 when metadata confirms both material and action; explicit quality cues add 2 each. A score and human-readable selection reason are retained in source records/cache. In random concat mode, top-ranked candidates are selected to meet the target first; only the resulting playback path order is shuffled.

Coverage uses the measured narration duration plus the render safety margin (`0.1s`) and the same maximum clip-duration cap used by rendering. Category search stops as soon as distinct usable candidate duration meets that target. If selected categories cannot meet it, the materials stage reports required and available usable duration plus searched categories. If downloads or clip processing reduce coverage below the target, ASMR fails clearly. It does not use unrelated generic footage or the legacy looping fallback. Ordinary styles retain the fallback loop.

## Verification

- Focused ASMR and ordinary regression selection: **224 passed, 3 skipped, 1 deselected, 10,378 subtests passed**. Command covered ASMR stock, LLM controller, WebUI i18n, cache manager, audio-stage terms, CLI and task services; the deselected test is `test_cross_post_state_update_retries_transient_backend_failure`, which makes an unmocked social-metadata call in its current test body.
- Render/legacy behavior: `test_short_clip_transitions.py` and `test_combine_clip_isolation.py`: **9 passed**.
- Ruff on touched Python implementation and test files: **passed**.
- `git diff --check`: **passed**.

The first broader run included `test_youtube_audience.py`; its three loopback HTTP receiver cases cannot bind sockets in this sandbox. Another existing `test_task.py` case reached an unmocked social-metadata provider call and received a connection error. Neither is evidence of an ASMR provider result. ASMR tests use local mocks and make no paid-provider requests.

A single real, metadata-only Pixabay request for `kinetic sand cutting` was attempted using the configured provider connection. DNS resolution for `pixabay.com` failed before an HTTP response, so it returned no items; this is a connectivity failure, not evidence that the provider has zero matching assets. No downloads were attempted. Pexels was not queried. Provider supply and visual relevance therefore remain unverified. The request error log redacted the API key.

Existing Upload-Post characterization is in `test/services/test_youtube_audience.py`, `test/services/test_controller_video.py`, `test/services/test_upload_redirect_handle_native.py`, and `test/services/test_social_title_fallback.py`; this phase makes no publishing implementation change.

## Known limitations

- Metadata matching cannot verify what the clip visibly depicts or whether it is live-action, CGI, or AI-generated.
- Stock availability, portrait supply, and distinct usable seconds have not been measured through authenticated provider API results.
- No cross-run visual duplicate prevention is provided.
- ASMR clip processing failure now avoids repetition and fails the render, but it is surfaced as a video-stage processing error after materials download rather than a materials-stage candidate-retrieval error.
- This work prepares explicit per-task settings only. It does not create channel profiles, content planning, schedule/queue durability, QA approval, or account-bound publishing.
