# ASMR Implementation Review for Channel Autopilot

**Review scope:** Current MoneyPrinterTurbo worktree, as of 2026-10-23. Read-only review; this document is the only file added by this review. No production code was changed.

## Executive finding

The preset is a useful retrieval prototype, but it does not yet support the intended “Hindi facts narration + independent ASMR background” behavior reliably. Automatic ASMR terms are generated from the fact subject and script, so the two content tracks are coupled. Candidate ranking is also undone for download choice by the default random shuffle. The selector searches every expanded query up front, does not systematically top up missing categories, and only errors when there are zero metadata matches. If it finds too little footage, the shared renderer loops the available clips to fill the narration duration.

**Recommendation:** Keep the opt-in schema/UI/CLI wiring, deterministic metadata gates, attribution, and clear zero-match failure. Before Channel Autopilot, change ASMR terms to come only from an explicit ASMR category/profile input, implement category-by-category top-up against required distinct footage duration, preserve top-ranked selection, and fail when the usable pool is too short. Never route ASMR shortages to ordinary stock search. Provider relevance and supply are still unverified against real API results.

## 1. Actual worktree and Git inventory

`git log -8` shows only `cedd8ea Initial commit: MoneyPrinterTurbo`; there are no later commits to review. ASMR implementation work is uncommitted. The tracked diff has 12 ASMR-related files; there are also two relevant untracked implementation/test files:

| Status | File | ASMR change |
|---|---|---|
| Modified | `app/models/schema.py` | `StockVideoStyle`; defaults on `VideoParams` and `VideoTermsParams`; `TaskVideoRequest` inherits `VideoParams`. |
| Modified | `app/services/llm.py` | Optional ASMR keyword prompt and output-term validation in `generate_terms`. |
| Untracked | `app/services/asmr.py` | Material/action vocabulary, bounded synonyms, metadata score, URL normalization, ID/URL deduplication. |
| Modified | `app/services/material.py` | Pexels/Pixabay metadata extraction and persistence; ASMR search, rank, candidate download, and no-match exception. |
| Modified | `app/services/material_cache.py` | Cache source metadata whitelist and format version 2 → 3. |
| Modified | `app/services/task.py` | ASMR style passed into keyword/material services, invalid terms rejected, no-match reported as a materials-stage failure. |
| Modified | `app/controllers/v1/llm.py` | `/terms` forwards `stock_video_style`. |
| Modified | `cli.py` | `--stock-video-style`; style enters regular and batch `VideoParams`. |
| Modified | `webui/Main.py` | Stock-style selector, session state, restored task settings, and keyword-generation forwarding. |
| Modified | `webui/i18n/en.json`, `webui/i18n/zh.json` | Selector/help strings. |
| Modified | `test/services/test_task.py`, `test/services/test_webui_i18n.py` | Default forwarding expectation and locale fallback keys. |
| Untracked | `test/services/test_asmr_stock.py` | Mocked query, ranking, dedupe, provider metadata, cache/source record, and failure tests. |

`docs/research/satisfying-asmr-stock-video-investigation.md` is the prior untracked research reference in this checkout; it is not a committed file. `AGENTS.md` is also untracked and unrelated to ASMR. Neither was changed in this review.

## 2. Code-level behavior and gaps

### Query generation is tied to the fact narration

- `task.generate_terms` calls `llm.generate_terms(video_subject=params.video_subject, video_script=..., stock_video_style=...)` when `video_terms` is empty ([`task.py:380-393`](../../app/services/task.py#L380)).
- The ASMR prompt still says terms should be grounded in the requested subject and script, includes both as context, and has generic subject-related constraints ([`llm.py:1062-1105`](../../app/services/llm.py#L1062)). Thus “interesting Hindi facts” can generate fact-related terms, which are then rejected unless they coincidentally include one supported material and action ([`llm.py:1148-1153`](../../app/services/llm.py#L1148)). The task layer also rejects custom terms that lack a material/action pair ([`task.py:415-426`](../../app/services/task.py#L415)).
- The fallback instruction only chooses common materials when the subject/script itself “asks for ASMR cutting”; a fact brief does not satisfy that instruction. Therefore an empty generated query set or a task failure is plausible for the intended product prompt.
- `match_materials_to_script` remains coupled to term count/order generation and later sequential material/final-video settings. This setting is irrelevant to an independent background track and should not constrain ASMR selection.
- **Verified conclusion:** ASMR keyword generation is not independent today. Supplying explicit `video_terms` bypasses LLM term generation in `task.generate_terms` ([`task.py:382-405`](../../app/services/task.py#L382)); that is the existing programmatic workaround.

### Search, ranking, deduplication, and selection

- The helper recognizes six material families and four actions, but only has some action/material expansion mappings ([`asmr.py:16-42`](../../app/services/asmr.py#L16)). Expansion filters invalid terms and caps the total at 12 queries ([`asmr.py:80-103`](../../app/services/asmr.py#L80)). This is a total query cap, not a per-category budget.
- Pexels records `alt`, optional `tags`, preview `image`, asset ID, page, creator, and rendition; Pixabay records tags, thumbnail, asset/page/creator/rendition ([`material.py:437-450`](../../app/services/material.py#L437), [`material.py:579-600`](../../app/services/material.py#L579)). Cache and task-source whitelists retain tags/title/thumbnail/score and public attribution ([`material_cache.py:60-105`](../../app/services/material_cache.py#L60), [`material.py:116-170`](../../app/services/material.py#L116)). Pixabay's `title` is currently just a copy of its tags string, not a provider title ([`material.py:586`](../../app/services/material.py#L586)); that field name is misleading.
- Ranking requires metadata to include both the query's material and action, adds small bonuses for ASMR/close-up cues, and sorts by score ([`asmr.py:106-139`](../../app/services/asmr.py#L106), [`asmr.py:161-190`](../../app/services/asmr.py#L161)). It deliberately does not use the query itself as evidence. Its score is a string-match heuristic, not visual verification.
- Deduplication uses `(provider, asset_id)` and normalized **download media URL** ([`asmr.py:142-156`](../../app/services/asmr.py#L142), [`asmr.py:173-189`](../../app/services/asmr.py#L173)). It does not deduplicate via source-page URL when two renditions/URLs differ and have no shared asset ID. That is a residual duplicate risk, not a reason to undo current checks.
- The ASMR branch searches all expanded terms before it ranks any results; it raises only if the resulting ranked list is empty ([`material.py:2314-2344`](../../app/services/material.py#L2314)). It does not iterate categories based on remaining duration or stop provider searches when enough candidates are already available.
- **Ranking is not currently the selection order:** after ranking, the shared code calls `random.shuffle(valid_video_items)` when `video_concat_mode` is random, which is the default ([`material.py:2379-2381`](../../app/services/material.py#L2379)). Download order therefore does not prefer higher-scored candidates. Keep ranking order through candidate choice; randomize the chosen playback sequence afterward if desired.
- The downloader selects from the available pool to cover `audio_duration`; if some relevant candidates exist but their duration is insufficient, it returns the partial set rather than raising ([`material.py:2383-2443`](../../app/services/material.py#L2383)). The shared renderer then repeats processed clips to cover audio plus its safety margin ([`video.py:1243-1258`](../../app/services/video.py#L1243)). ASMR mode must validate enough distinct suitable footage before this fallback can run.
- If there are zero metadata matches, `NoRelevantAsmrMaterialsError` is marked as a materials failure; no generic stock fallback happens ([`material.py:2331-2337`](../../app/services/material.py#L2331), [`task.py:848-850`](../../app/services/task.py#L848)). This is correct for avoiding irrelevant visuals but does not meet “try other categories before failing.”

## 3. Against the Hindi-facts Shorts use case

The narration and visuals need separate inputs. The fact subject/script should continue to drive Hindi script, voice, and subtitles; an ASMR category list/profile should drive stock search. A saved profile can supply categories such as kinetic sand, soap, foam, jelly, crystal, or lava, with the current supported actions. ASMR search terms must not be derived from the Hindi fact or ordered to match its chronology.

The normal task pipeline can already synthesize supplied narration, produce subtitles, find background materials, and render portrait video. Its clip target is based on measured audio duration, not a requested “25–30 seconds” field. The Autopilot caller must provide a script sized for that interval and validate the synthesized duration, or future pipeline work must add an explicit duration check. Existing `video_language`, voice, subtitle, and portrait parameters can be set for Hindi output; actual TTS voice availability remains environment-dependent.

The current style defaults to five generated LLM terms and only searches the category terms the model happens to emit. It has no user-selectable category list in `VideoParams`. It does not implement systematic category fallback. To satisfy the stated behavior, search selected categories in a deterministic priority order, expand each within a bounded request budget, rank/dedupe accumulated candidates, and continue to another selected category while distinct usable duration is short. Fail clearly if the combined pool cannot cover the output without unintended clip reuse. Do not use generic fact-related stock as a backup.

## 4. KEEP / MODIFY / REMOVE / DEFER

| Decision | Change | Reason / location |
|---|---|---|
| **KEEP** | Opt-in `satisfying_asmr_cutting` schema value with `default` as the default; API, CLI, and WebUI pass-through. | Clean entry point; default mode remains explicit. [`schema.py:63,112-126`](../../app/models/schema.py#L63), [`cli.py:351-363`](../../cli.py#L351), [`Main.py:5253-5269`](../../webui/Main.py#L5253). |
| **KEEP** | Strict metadata gate requiring material and action, bounded query expansion, within-run asset/media URL dedup, and explicit no-match failure. | Prevents broad unrelated fallbacks and controls query growth. [`asmr.py`](../../app/services/asmr.py), [`material.py:2314-2337`](../../app/services/material.py#L2314). |
| **KEEP** | Public source-page and creator attribution with safe thumbnail URLs. | Retains useful provenance without copying signed query strings. [`material.py:116-170`](../../app/services/material.py#L116), [`material_cache.py:60-105`](../../app/services/material_cache.py#L60). |
| **KEEP** | Mocked tests for ranking, provider metadata mapping, cache/source metadata, and errors. | These test local contracts; they must not be presented as evidence of real relevance. [`test_asmr_stock.py`](../../test/services/test_asmr_stock.py). |
| **MODIFY** | Replace script-grounded ASMR term generation with category/profile-grounded queries. | Current fact narration and ASMR visuals are coupled. Keep `video_script` for Hindi narration/subtitles, but remove it and `video_subject` from ASMR keyword inputs. [`llm.py:1062-1105`](../../app/services/llm.py#L1062), [`task.py:380-426`](../../app/services/task.py#L380). |
| **MODIFY** | Add explicit selected ASMR categories and fallback/top-up until sufficient distinct duration; fail on insufficient pool. | Current code expands emitted terms only, searches them all at once, and accepts partial footage that the renderer may loop. [`schema.py:112-126`](../../app/models/schema.py#L112), [`material.py:2314-2443`](../../app/services/material.py#L2314), [`video.py:1243-1258`](../../app/services/video.py#L1243). |
| **MODIFY** | Preserve score order while choosing assets; randomize only the final playback sequence if selected. | The current default random shuffle undoes ranking before download. [`material.py:2379-2381`](../../app/services/material.py#L2379). |
| **MODIFY** | Keep provider metadata additions isolated to ASMR or explicitly accept/test their shared effect; remove Pixabay's synthetic `title=tags`. | Shared adapters now add metadata to normal provider searches and task artifacts. Cache v3 invalidates all existing search caches. The synthetic title misstates source data. [`material.py:437-447,579-597`](../../app/services/material.py#L437), [`material_cache.py:24-25`](../../app/services/material_cache.py#L24). |
| **MODIFY** | Make ASMR ignore narration-order matching and TwelveLabs subject reranking by construction. | ASMR is an independent visual layer; current LLM still receives script ordering if that general option is enabled. [`task.py:384-437`](../../app/services/task.py#L384). |
| **REMOVE** | Do not remove the preset itself, provider adapters, or helper as a whole. Remove only the false Pixabay title mapping and any ASMR branch behavior that silently uses narration terms or loops inadequate footage. | Removing the whole prototype would discard useful isolated machinery; correct the coupled/ambiguous behavior instead. |
| **DEFER** | Automated image/video visual scoring, authenticity classification (real/CGI/AI), loop detection, and cross-run diversity guarantees. | No current visual model or verified stock dataset. First establish candidate supply and audit precision with human thumbnail review; do not infer action from tags. |
| **DEFER** | Channel profiles, scheduler, publication calendar, retries/idempotency, and scheduled YouTube publishing. | Outside current MPT preset; needs explicit data and lifecycle design. Existing auto-upload is immediate post-generation, not a schedule. |

## 5. Default-workflow regression risks

- **Core selection path:** Default `download_videos` still uses its prior search, URL dedup, random/sequential selection path; ASMR scoring is gated by the new style. Existing default behavior is therefore mostly preserved.
- **Shared provider metadata:** Pexels/Pixabay adapters now attach ASMR metadata on every stock search, and source records/cache retain some of it. Ordinary MPT tasks can write larger `script.json` source records. Pixabay's duplicate tags-as-title adds redundant data. No ordinary video rendering logic consumes these fields.
- **Global cache invalidation:** `_CACHE_FORMAT_VERSION` changes from 2 to 3. Existing caches for ordinary searches and all cached providers are invalidated on next lookup, causing a one-time repeat provider search. This can increase latency or quota usage after upgrading.
- **Schema/API surface:** Every `VideoParams` now serializes `stock_video_style="default"`, and API schemas expose the extra optional field. This is backward-compatible for callers that ignore response fields, but clients that reject unknown response keys should be checked. Batch manifests accept it because they validate against `VideoParams.model_fields`.
- **UI:** The selector is displayed only for Pexels/Pixabay. Session state and restored task params keep the choice in a running UI/task history; it is not saved as a global `config.toml` setting.
- **ASMR-specific:** `match_materials_to_script` can still affect LLM prompt/order and final concat mode even though ASMR is independent. In random mode, the ranked candidates are shuffled before download.

## 6. Footage evidence and fallback limits

**Verified from the earlier investigation document, not from this code review:** A Pixabay public detail page lists a kinetic-sand candidate (`144459`) with kinetic-sand/cutting/ASMR tags and a six-second duration. Public pages suggested kinetic sand and soap are the strongest stock prospects; generic foam was noisy, jelly slicing was unverified, crystal cutting ambiguous, and no surreal lava-cutting candidate was verified. These were page snippets/counters, not authenticated API results or frame review ([investigation](satisfying-asmr-stock-video-investigation.md#phase-2--mpt-retrieval-validation-2026-10-23)).

**Unverified:** API candidate counts, portrait availability, distinct usable duration, repeatability after cache expiry, exact physical cutting action, and whether each clip is camera footage, CGI, or AI-generated. The previous probe failed DNS before receiving provider data; this review made no provider requests. Metadata score 8–18 reflects text cues only. It cannot establish visible action or authenticity.

**Current fallback behavior:** Zero matching items → explicit failure. Some matching items but insufficient duration → return the partial set, then shared rendering can loop those clips. Missing material categories are not searched deterministically. There is no silent generic stock fallback in ASMR mode, but the partial-pool loop violates the desired insufficiency rule.

## 7. Channel Autopilot compatibility

- **Programmatic video generation exists.** `POST /videos` accepts `TaskVideoRequest`, which inherits `VideoParams`; `create_task` stores `body.model_dump()` and schedules `task.start` ([`schema.py:266-272`](../../app/models/schema.py#L266), [`video.py:214-218,235-270`](../../app/controllers/v1/video.py#L214), [`task.py:1929-1957`](../../app/services/task.py#L1929)). `stock_video_style`, explicit `video_script`, `video_terms`, subtitle settings, source, aspect, voice, and other normal parameters can be supplied without UI interaction.
- **CLI and batch generation exist.** CLI exposes the style flag ([`cli.py:351-363`](../../cli.py#L351)) and batch input validates/creates `VideoParams` objects ([`cli.py:1346-1365`](../../cli.py#L1346)). A caller can supply a Hindi fact script and explicit category terms as separate fields, bypassing ASMR LLM query generation.
- **The regular pipeline can produce the assets.** `_run_pipeline` obtains the script, terms, audio, subtitles, video materials, and final render ([`task.py:1680-1717,1737-1815`](../../app/services/task.py#L1680)); render duration follows measured audio. The task API and CLI are suitable generation entry points for an external profile runner once the query-independence and insufficient-stock behavior are corrected.
- **No saved channel profile model or generation scheduler was found.** Current generation params are per task. Upload-Post settings are global config fields (API key, username, enabled platforms, auto-upload, YouTube privacy/audience) shown in the WebUI settings ([`Main.py:3285-3354`](../../webui/Main.py#L3285)); task completion snapshots that configured account and immediately queues cross-posting ([`task.py:1871-1917`](../../app/services/task.py#L1871)). Cross-post has a bounded in-process executor/recovery path, but it is not a calendar scheduler ([`task.py:1485-1523`](../../app/services/task.py#L1485)).
- **Integration needs before Autopilot:** persisted channel/profile configuration, a profile-to-account binding, a scheduler/job store, idempotent due-job execution and retry rules, per-channel content/category settings, and publication outcome tracking. Do not assume current global Upload-Post config represents multiple channel profiles or supports scheduled publishing. Existing auto-upload publishes after generation, not at a future time.

## 8. Minimal corrective plan before Channel Autopilot

1. Add an explicit ASMR category/profile field (or validated separate selection object) to task parameters. Default it to common categories only if product intent says so; allow configured categories and priority. Keep narration fields unchanged.
2. In ASMR mode, generate/search terms solely from those selected categories and action preferences. If no explicit categories are sent, use a deterministic documented ASMR default; never infer categories from fact text. Bypass narration-order term generation and subject-based reranking.
3. Search categories/variants within a per-category and total request cap. Accumulate metadata-qualified candidates, rank and dedupe across the provider/category pool, and query the next selected category only while usable distinct duration is below the required amount.
4. Keep highest scores in the chosen asset set. Do not use `random.shuffle` until after choosing the set. Track distinct duration, and fail clearly when it cannot cover measured narration duration plus render margin; do not let the generic renderer repeat a tiny ASMR set silently.
5. Keep public attribution and previews. Remove Pixabay's fake `title`; decide whether adapter/cache changes are globally useful or scope them to ASMR. Avoid global cache invalidation if metadata compatibility can be handled without it; if retaining version 3, document/test the one-time ordinary cache refresh.
6. Add tests for Hindi fact/script independence, explicit category selection and cross-category top-up/early stop, rank-preserving choice despite random playback, insufficient distinct-duration failure, no unrelated fallback, API/CLI batch parameter passage, and default-mode/cache behavior. Then manually inspect a small, rate-limited thumbnail sample on a network-enabled host before making availability claims.

## 9. Existing tests and evidence limits

`test/services/test_asmr_stock.py` covers helper expansion, mocked LLM filtering, metadata scoring, ID/URL dedupe, provider metadata mapping, cache/attribution fields, zero-match handling, task failure handling, and the default model value. `test/services/test_task.py` updates default-style forwarding. `test/services/test_webui_i18n.py` covers translation fallback. Focused unittest execution in the previous implementation turn passed 29 cases; this is local/mocked coverage, not a current rerun or provider relevance result.

Missing tests include the newly identified critical paths: fact/script independence, selected-category fallback, stopping once adequate stock is found, rank-preserving choice under random concat, and failing when usable distinct duration is insufficient. No provider API/visual review was performed for this review.
