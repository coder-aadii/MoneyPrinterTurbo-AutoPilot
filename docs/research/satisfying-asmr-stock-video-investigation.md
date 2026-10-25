# Satisfying ASMR Stock Video Investigation

**Scope:** This checkout is MoneyPrinterTurbo, not StoryWeaver. Findings about MoneyPrinterTurbo (MPT) are verified against this repository. StoryWeaver architecture and implementation findings are unverified because its source, documentation, and data contracts are absent. Provider web-page evidence is directional, not a substitute for authenticated API search results.

## 1. Executive Summary

- **VERIFIED:** MPT has an LLM-generated list of short stock-video terms, Pexels/Pixabay search adapters, orientation and minimum-duration filtering, a 24-hour search cache, downloads, duplicate-URL removal, duration coverage, and task-level source records. It has no visual relevance scorer for stock search results.
- **VERIFIED:** MPT has no ASMR style preset or specialized storyline-free video workflow in the inspected schema or task path. Existing settings cover source, aspect, clip duration, concat mode, and optional script-order matching ([`app/models/schema.py`](../../app/models/schema.py#L96), [`app/services/task.py`](../../app/services/task.py#L379)).
- **VERIFIED (Phase 2 evidence):** Search-page evidence supports a plausible stock supply for kinetic sand and soap, but does not show that the existing MPT pipeline can reliably return/select those clips. Foam queries appear noisy; jelly/crystal cutting is unproven; no glowing-lava cutting result was verified. Actual API reliability and repeatability remain unmeasured because the host-side API was unreachable here.
- **UNVERIFIED:** StoryWeaver could use Pexels/Pixabay to produce reliably on-style ASMR sequences. It is not in this checkout, so its current code, architecture compatibility, and exact edit locations cannot be established.
- **INFERRED:** Prompt changes alone could improve query specificity but cannot create missing supply or verify that a returned clip actually shows the requested action. The MPT path confirms the query is passed to provider search, then candidates are accepted on metadata/technical constraints.
- **PROPOSED:** After locating the StoryWeaver repository, prototype a user-selected ASMR visual preset that operates at shot/scene visual-query generation and adds deterministic metadata gates plus a small, human-reviewed candidate evaluation. Keep narration/story contracts optional and unchanged. Add visual scoring only if metadata-ranking results are inadequate.

### Phase 2 — MPT retrieval validation (2026-10-23)

- **VERIFIED:** The existing MPT adapter `material.search_videos_pixabay("kinetic sand cutting", 1, VideoAspect.portrait)` was invoked once on 2026-10-23. It failed before receiving an HTTP response: DNS resolution for `pixabay.com` failed (`NameResolutionError`), and the adapter returned zero candidates. The error logger redacted the configured key. The prior six-query raw API probe also returned `ConnectionError`. No authenticated result totals, candidate records, tags, durations, orientations, or thumbnail URLs were received; no video was downloaded. Pexels credentials are not configured here.
- **VERIFIED (public-page evidence only):** Search-engine-indexed provider pages indicate exact kinetic-sand and soap-cutting searches have relevant stock-page results. A Pixabay kinetic-sand detail page identifies candidate `144459`, tagged `Kinetic Sand`, `Sand`, `Cutting`, `Asmr`, duration 6 seconds, 4096×2160 (landscape). It is a provider-listed candidate; its actual footage authenticity/action and API eligibility were not visually/API verified.
- **VERIFIED (public-page evidence only):** Pixabay's `foam` page advertises 691+ results but surfaced tags such as `water`, `waves`, `ocean`, and `beach`; generic `foam` is therefore a noisy proxy for foam slicing. `satisfying cutting` advertises 291+ results, with page tags leaning toward food/kitchen/cutting. A generic Pixabay `jelly` page advertises 74+ results, but surfaced candidates were jelly/food-like, not verified jelly slicing. Pexels has indexed `foam cutting` and `crystal cutting` pages, but the search-page totals are broad and the visible snippets do not establish exact action matches.
- **VERIFIED query comparison (provider website search pages; not API results):**

| Query | Public page evidence | Relevance interpretation |
|---|---|---|
| `kinetic sand cutting` | Pixabay page advertises 3,974+; candidate 144459 has kinetic-sand/cutting/ASMR tags and 6 sec, 4K dimensions. | Best evidence for a material + action phrase. Repeatability and API return remain unverified. |
| `soap cutting` / `soap cutting asmr` | Pixabay pages advertise 424+ / 454+; Pexels surfaces soap-cutting pages. | Promising category, but page samples include broad soap/hygiene footage; inspect exact candidate. |
| `foam` vs `foam cutting` | Pixabay generic `foam` results visibly associate with ocean/waves; Pexels has a `foam cutting` page. | Adding action reduces obvious ambiguity; exact foam-slicing results remain unverified. |
| `satisfying cutting` | Pixabay advertises 291+; visible semantic tags/results lean food, kitchen, tools. | Broad intent term; not enough to isolate ASMR materials. |
| `jelly` / `jelly slicing` | Pixabay generic `jelly` advertises 74+; no indexed exact-slicing candidate observed. | Material word alone returns jelly-related supply, not confirmed slicing. |
| `crystal cutting` / `glass cutting asmr` | Pexels has a crystal-cutting page, but indexed snippets include unrelated meanings/objects. | Ambiguous; query should name a specific object/action, but exact ASMR stock supply unknown. |
| `glowing lava cube cutting` | No verified provider-page candidate found in this spot check. | Treat as a stock supply gap until proven otherwise; no inference from ordinary lava footage. |

The website counters (e.g. 3,974+) are visibly broad and may be search-index/page UI counts, not API totals or relevant unique clips. Search pages expose a manually reviewable candidate surface, but the text-only snippets available here did not expose thumbnail URLs for all candidates.
- **UNVERIFIED:** API counts and first-page candidate samples for query variants; exact foam-block slicing, jelly slicing, crystal cutting, lava-cube cutting; authenticity (camera footage vs CGI/AI) of all listed candidates, including Pixabay candidate 144459; and repeatable, distinct portrait results across generation runs.
- **PROPOSED best query probes (not measured API winners):** `kinetic sand cutting` / `kinetic sand slicing`; `soap cutting asmr` / `soap shaving`; `foam slicing` / `sponge cutting`; `jelly slicing` / `gelatin cutting`; `crystal cutting` / `glass cutting`; `glowing lava cube cutting` / `lava cube slicing`. Public page evidence favors the exact kinetic-sand and soap-cutting phrases; broad terms such as `foam`, `jelly`, `crystal`, and `satisfying` should be controls, not assumed useful queries.
- **INFERRED:** Prompt-only changes can replace broad generic terms with more specific material-action phrases and likely improve candidate pool relevance for kinetic sand and soap. They cannot repair query-result noise, rank better candidates, verify video action, or establish non-repetition. A small metadata ranker should precede visual scoring; top-candidate thumbnail/frame review is necessary to make a claim about actual content.

**Search evidence links:** [Pixabay kinetic-sand cutting](https://pixabay.com/videos/kinetic-sand-sand-cutting-asmr-144459/), [Pixabay foam](https://pixabay.com/videos/search/foam/), [Pixabay satisfying cutting](https://pixabay.com/videos/search/satisfying%20cutting/), [Pixabay jelly](https://pixabay.com/videos/search/jelly/), [Pexels foam cutting](https://www.pexels.com/search/videos/foam%20cutting/), [Pexels crystal cutting](https://www.pexels.com/search/videos/crystal%20cutting/). Search-page result counts are provider website UI counts, not authenticated API `totalHits`/`total_results`, relevant unique assets, or API-eligible counts.

**PROPOSED minimal MPT proof of concept:** On the network-enabled Ubuntu host, run the metadata-only host probe below once per configured provider. For each of the six categories, record API total, count surviving MPT's duration/orientation/resolution filters, unique asset IDs, and top-five exact/adjacent/irrelevant labels. Manually inspect thumbnails/source pages; label camera footage, CGI, AI-generated, or uncertain only when the visible evidence supports it. Use a second sample after the providers' 24-hour cache window to estimate repeatability; do not force refresh or evade provider caching. Suggested initial success gate: at least five distinct technically usable candidates in each stock-feasible category and ≥70% exact-or-usable action among the first five selected/ranked results. Treat lava and surreal crystal/jelly separately; a low inventory count is a supply gap, not a prompt failure. Record elapsed time for each request to measure host latency. No paid AI calls are needed.

## 2. Current StoryWeaver Stock Video Architecture

**UNVERIFIED:** No StoryWeaver files were found in this checkout (including searches for `StoryWeaver`, `VisualSpec`, and `storyboard`). Therefore there is no verified StoryWeaver source → approved storyboard → shot → Visual Bible/VisualSpec → provider query → selection/download/cache/approval → timeline/render/version path to cite. The attachment’s claimed architecture and filenames should be checked in the actual StoryWeaver repository before implementation.

The following items remain unanswered pending that source: provider adapters and API settings; prompt/template and LLM call sites; shot-to-query traceability; result ranking; selection approval/versioning; timeline integration; visual style settings; and contracts/ADRs governing story versus independent background visuals.

## 3. MoneyPrinterTurbo Technical Comparison

**VERIFIED — query generation:** [`app/services/task.py`](../../app/services/task.py#L379) calls `llm.generate_terms` when `video_terms` is not supplied. Default mode asks for five terms; script-order mode requests eight. Optional TwelveLabs reranking applies only outside script-order mode. [`app/services/llm.py`](../../app/services/llm.py#L1023) asks for English 1–3 word terms related to the subject, optionally chronological to narration. There is no ASMR-specific prompt or explicit action/material taxonomy. User-supplied terms bypass LLM generation.

**VERIFIED — Pexels:** [`app/services/material.py`](../../app/services/material.py#L341) calls `/v1/videos/search`, sends the term and (except square) orientation, and examines up to 20 results. It requires duration ≥ requested minimum and picks the smallest rendition large enough for the target output; it retains asset ID, source page, creator, and rendition dimensions.

**VERIFIED — Pixabay:** [`app/services/material.py`](../../app/services/material.py#L448) calls `/api/videos/` with `q`, `video_type=all`, and `per_page=50`. It filters by duration, orientation, and minimum target dimensions, then retains asset ID, public page, creator, and rendition. It chooses the first usable rendition in provider response iteration order, not the globally best one.

**VERIFIED — filtering and ranking:** These adapters filter for minimum duration, orientation and sufficient resolution; they do not calculate semantic keyword relevance or rank based on action, material, framing, or motion. Search API order is otherwise preserved. `MaterialInfo` has provider, URL, duration and source metadata ([`app/models/schema.py`](../../app/models/schema.py#L85)). No video frames, thumbnails, embeddings or vision model are evaluated in the stock-search path.

**VERIFIED — orchestration and reuse:** [`download_videos`](../../app/services/material.py#L2161) runs one search per term, in parallel subject to configured concurrency ([`_search_terms_in_parallel`](../../app/services/material.py#L1946)). Search responses are cached by provider/term/minimum duration/aspect for 24 hours; empty/error results are not cached ([`_search_videos_with_cache`](../../app/services/material.py#L1853), [`material_cache.py`](../../app/services/material_cache.py#L24)). Duplicate download URLs are dropped. Default mode may shuffle candidates; script-order mode groups by term and round-robins candidates. Downloads stop after enough source duration is available and material sources are persisted in task `script.json` ([`task_artifacts.py`](../../app/services/task_artifacts.py#L67), [`material.py`](../../app/services/material.py#L2938)).

**VERIFIED — downstream:** [`app/services/task.py`](../../app/services/task.py#L1781) passes downloaded files to `generate_final_videos`; `video.combine_videos` applies fit, transitions, clip duration/speed and concat mode. `app/services/video.py` prioritizes unique source clips before reusing slices when needed. Batch source allocation and material selections are recorded in `script.json` ([`task.py`](../../app/services/task.py#L995)). MPT has no storyboard, shot approval, or Visual Bible/version contract in this path.

**VERIFIED — config:** MPT's local `config.toml` contains no Pexels key and has a Pixabay key configured. No key value is included in this report. The provider configuration is `pexels_api_keys` / `pixabay_api_keys` under app settings, also documented in [`config.example.toml`](../../config.example.toml#L90).

## 4. ASMR Stock Footage Availability

**VERIFIED — limited public-page spot check (2026-10-23):** Search-engine-visible provider pages list clips for kinetic sand cutting and soap cutting. Pixabay’s kinetic-sand page showed examples with durations/resolutions and an advertised “3,974+” search-page count; its soap-cutting page advertised “424+” and listed clips from contributors, including 5–60 second examples. Pexels pages surfaced for kinetic sand cutting and soap cutting, with broad search-page counts. These are page/UI counts, may include loosely tagged items or duplicates, and do not establish API-returnable unique relevant results. No clip was watched, so “provider-listed stock” does not prove it is genuine camera footage rather than CGI/AI. See [Pixabay kinetic sand](https://pixabay.com/videos/search/kinetic%20sand%20cutting/), [Pixabay soap cutting](https://pixabay.com/videos/search/soap%20cutting/), [Pexels kinetic sand](https://www.pexels.com/search/videos/cutting%20kinetic%20sand/), and [Pexels soap cutting](https://www.pexels.com/search/videos/soap%20cutting/).

| Category | Finding |
|---|---|
| Kinetic sand | **VERIFIED (page-level):** Exact phrase `kinetic sand cutting` has a Pixabay detail listing (candidate ID 144459; 6 sec; 4096×2160; tags include Kinetic Sand, Sand, Cutting, Asmr). Pexels has a matching search page. API availability/count and repeatability are unknown. |
| Soap cutting/shaving | **VERIFIED (page-level):** `soap cutting` and `soap cutting asmr` pages exist on both sites. Page snippets show short clips, but API candidates and action quality are unverified. Try `soap shaving` as a distinct probe. |
| Foam slicing | **VERIFIED (page-level):** `foam cutting` is indexed on Pexels. Pixabay's generic `foam` page surfaced mostly ocean/wave tags, illustrating noisy broad-term matches. Exact foam-block slicing remains unverified. |
| Jelly/crystal/glass-like cutting | **VERIFIED (page-level only):** Pixabay's generic `jelly` page has 74+ items but the visible snippets do not establish slicing. Pexels' `crystal cutting` page is highly broad and can match unrelated senses of “crystal” or “cutting.” Exact transparent-object cutting is unverified. |
| Glowing lava cubes | **UNVERIFIED:** No relevant public-page candidate or API result was confirmed. Don't infer availability from general lava/fire footage. Likely needs generated/commissioned material if exact surreal visuals are required. |
| Macro satisfying actions | **VERIFIED (page-level):** Pixabay's `satisfying cutting` page has 291+ results, but visible tags lean toward food/kitchen/tool; broad `satisfying` is noisy. Macro/action precision is unverified. |
| Seamless loops | **VERIFIED:** Neither inspected MPT metadata path contains a loopability feature. Provider duration/tags do not establish seamless start/end continuity. Requires playback inspection or motion analysis. |

**VERIFIED — authenticated API experiment status:** Pixabay credentials were configured, but six low-volume, metadata-only requests (kinetic sand, soap, foam, jelly, lava, macro) failed to connect from this environment. Pexels credentials were absent. No result counts or API candidate metadata were obtained, and no assets were downloaded. See the Phase 2 adapter-call details above.

### Host-side API probe if this environment remains offline

**PROPOSED command:** Run from the MPT repository root on the Ubuntu host with `.venv` and provider keys already configured. It issues at most 12 GET searches per configured provider, uses the providers' search endpoints/parameters and portrait orientation, prints up to five candidate metadata rows per query, and downloads no media. `Pixabay` requires search-response caching for 24 hours per its API terms; retain the printed metadata for that period and do not repeat the same query during the cache window. It never prints configured credentials.

```bash
./.venv/bin/python - <<'PY'
import json
import requests
from app.config import config

queries = [
    "kinetic sand", "kinetic sand cutting",
    "soap cutting", "soap cutting asmr",
    "foam", "foam slicing",
    "jelly", "jelly slicing",
    "crystal cutting", "glass cutting asmr",
    "lava cube", "glowing lava cube cutting",
]

def key_value(name):
    value = config.app.get(name) or []
    return value if isinstance(value, str) else (value[0] if value else "")

def orientation(width, height):
    if not width or not height:
        return "unknown"
    return "landscape" if width > height else "portrait" if height > width else "square"

def report(provider, query, count, rows):
    print(json.dumps({"provider": provider, "query": query,
                      "result_count": count, "candidates": rows[:5]}, ensure_ascii=False))

pexels_key = key_value("pexels_api_keys")
pixabay_key = key_value("pixabay_api_keys")
for query in queries:
    if pexels_key:
        try:
            response = requests.get(
                "https://api.pexels.com/v1/videos/search",
                headers={"Authorization": pexels_key},
                params={"query": query, "per_page": 20, "orientation": "portrait"},
                timeout=(5, 15),
            )
            response.raise_for_status()
            data = response.json()
            rows = []
            for v in data.get("videos", []):
                rows.append({"id": v.get("id"), "duration": v.get("duration"),
                             "width": v.get("width"), "height": v.get("height"),
                             "orientation": orientation(v.get("width"), v.get("height")),
                             "thumbnail": v.get("image"), "source_page": v.get("url")})
            report("pexels", query, data.get("total_results"), rows)
        except Exception as exc:
            print(json.dumps({"provider": "pexels", "query": query,
                              "error_type": type(exc).__name__}))
    if pixabay_key:
        try:
            response = requests.get(
                "https://pixabay.com/api/videos/",
                params={"key": pixabay_key, "q": query, "video_type": "all", "per_page": 50},
                timeout=(5, 15),
            )
            response.raise_for_status()
            data = response.json()
            rows = []
            for v in data.get("hits", []):
                renditions = v.get("videos", {})
                rendition = next((renditions.get(k) for k in ("large", "medium", "small", "tiny")
                                  if renditions.get(k)), {})
                rows.append({"id": v.get("id"), "tags": v.get("tags"),
                             "duration": v.get("duration"),
                             "width": rendition.get("width"), "height": rendition.get("height"),
                             "orientation": orientation(rendition.get("width"), rendition.get("height")),
                             "thumbnail": rendition.get("thumbnail"), "source_page": v.get("pageURL")})
            report("pixabay", query, data.get("totalHits"), rows)
        except Exception as exc:
            print(json.dumps({"provider": "pixabay", "query": query,
                              "error_type": type(exc).__name__}))
PY
```

The Pexels response does not expose keyword tags in the video shape documented here; its sample can only report ID, duration, dimensions/orientation, preview image and source page. Pixabay returns tags. API errors intentionally print only exception class to prevent credentials or request URLs leaking. **Do not paste raw error traces or config values into the report.** For manual relevance review, open only the returned thumbnail/source pages and label exact action, adjacent match, irrelevant, genuine/CGI/uncertain; thumbnail inspection cannot prove the full clip's motion or loopability.

**Provider facts — VERIFIED from official docs:** Pexels video search exposes query, orientation, size, page/per-page and video metadata including duration, dimensions, files and preview pictures; its docs state a default limit of 200 requests/hour and 20,000/month and require prominent attribution. [Pexels API docs](https://www.pexels.com/api/documentation/). Pixabay video search exposes query, type, category, minimum dimensions, page and per-page; results include tags, IDs, duration, preview and rendition URLs/dimensions. Its docs state a default 100 requests/60 seconds and require API response caching for 24 hours; no systematic mass downloads. [Pixabay API docs](https://pixabay.com/api/docs/).

## 5. Search Query and Ranking Analysis

**VERIFIED:** MPT sends generated terms straight to provider APIs; it has no fallback synonym expansion or multi-round query rewrite. Multiple LLM terms already yield parallel provider searches. Existing terms are short (1–3 words), which is a potential mismatch for niche actions: `kinetic sand cutting`, `soap shaving`, and `foam slicing` are more discriminative, but provider support for phrase semantics must be measured.

**VERIFIED (public search-page comparison, not API ranking):** Exact action/material phrases surfaced the clearest relevant page evidence: `kinetic sand cutting`, `soap cutting`, and `soap cutting asmr`. `foam cutting` is more specific than `foam`, while the generic Pixabay `foam` results visibly include ocean/wave content. `jelly` returned jelly-related stock but no verified slicing action. `satisfying cutting` is broad and visibly food/tool heavy. `crystal cutting` is ambiguous. No actual lava-cutting result was verified. These are query-term observations from indexed website pages; only the kinetic sand sample provides a concrete candidate detail page and ID. They do not establish API result ranking or best-performing query performance.

**PROPOSED:** For an ASMR preset, produce a structured query bundle per approved shot: exact object/material + action + framing/motion, with a small capped synonym set (e.g. `kinetic sand slicing`, `kinetic sand cutting close up`, `soap carving close up`). Avoid adding `asmr` to every query blindly; it can match unrelated audio/creator tags. Preserve each original shot intent and store which query returned each candidate.

**PROPOSED ranking order:** Hard-reject wrong orientation, too-small rendition, too-short duration, and explicit negative matches; then score structured metadata (material/action term hits, `close up`/`macro`, target aspect/resolution, duration suitability, provider tags/description). Add URL/asset-ID deduplication across query variants and normalize provider results. Do not treat views/downloads as relevance. Only use provider metadata that exists in the response; a high score must not imply verified visual content.

**VERIFIED MPT limitation:** Current provider adapters discard metadata needed for these enhancements. Pixabay `tags` and thumbnail URLs are not copied into `MaterialInfo.source_info`; Pexels' `image`/`video_pictures` previews and any descriptive text are not retained either. `MaterialInfo` stores URL, duration, provider and source info. Thus even adding a ranker after current adapters would have little semantic signal unless the adapter/result contract is enriched. The selected search ordering also remains provider order, with optional later shuffle; it is not a relevance rank.

**VERIFIED MPT selection weakness:** Search candidates are de-duplicated by download URL, not provider asset ID; aliases/different renditions of one asset can survive. Default mode can shuffle the provider-ordered merged pool; script-order mode preserves term groups but still trusts the provider's order within each term. Selection prioritizes enough duration and source diversity, not ASMR action relevance. `video.py` avoids reusing the same source clip before overflow within a composition, but it cannot prevent a similar or same cached candidate set from recurring in a later task. The 24-hour cache deliberately reuses search results, not the generated video itself.

| Approach | What it could achieve in MPT | Main limitation |
|---|---|---|
| Prompt/query-only | Replace generic terms with concise exact material-action phrases; no new ranking code or model cost | Provider still controls result order; one false-positive result may be selected; no visual/action validation or repeatability guarantee. |
| Metadata ranking | Use retained/added tags, title/description, duration, orientation, resolution and deduplication to promote better candidates | Requires adapter metadata changes; weak/misleading tags remain; does not establish what frames show. |
| Thumbnail/frame inspection | Human can validate object, framing and obvious action from a small top-K set; automated vision could help triage | Preview may show only one moment and miss cutting motion/loop boundaries. MPT currently does not retain preview URLs. |
| Video action/loop analysis | Potential to validate temporal cutting motion and loop continuity | Requires sampling/fetching clip content or a video-capable model, added compute/service cost, latency, and false-positive evaluation. Not currently implemented. |

**INFERRED:** Metadata ranking can improve aspect, size, duration, and tag/query alignment, but cannot reliably distinguish actual slicing from a merely related object or determine loop quality. The official API docs expose descriptions/tags only to the extent included in results; Pexels exposes preview pictures, but neither provider's listed search fields encode a verified action label.

## 6. Visual Relevance Evaluation Options

| Method | Relevance | Cost/latency | Reliability and fit |
|---|---|---|---|
| Metadata score | Low–medium | Low | Easy and provider-independent; fails on weak/misleading tags. |
| Thumbnail/preview image scoring | Medium for object/framing | Low–medium | May miss action and temporal quality; Pexels returns preview pictures, Pixabay returns thumbnails. |
| Sampled-frame vision-language score | Medium–high | Medium/high | Can inspect material/action/framing; frame sampling may miss the cut; external model privacy/cost and deterministic scoring need consideration. |
| Video embeddings | Medium | Medium | Useful for broad semantic match, less clear for action correctness; requires model/index maintenance. |
| Motion/action analysis | Potentially high | High | Better for repeated cutting/motion and loop analysis, but materially more compute/model complexity and false positives. |
| Human preview/approval | High for pilot | Human time | Best ground truth for a small experiment; should be preserved as review step if automated confidence is low. |

**VERIFIED MPT feasibility/performance constraints:** Search adapters use `requests` timeouts `(30, 60)` and do not charge a per-search fee in MPT; provider quotas/rate limits apply. MPT's default material search concurrency is one, configurable up to eight (`app/services/material.py:49–54, 361–368, 472–475, 1956–1988`). Twelve expanded queries therefore add twelve provider requests per provider (up to 24 when both are configured); concurrency can reduce wall time but increases burst pressure. **UNVERIFIED:** Actual API latency from the user's host is unknown because this sandbox cannot reach Pixabay. No visual model cost is estimated because no model/provider was selected and no AI calls were made. Pure metadata scoring adds negligible local compute; loading thumbnail images or sampled video frames adds transfer and decode time.

**PROPOSED:** Start with metadata scoring plus a side-by-side human audit of candidates. If false matches remain high, evaluate a small frame-sampling VLM pass on only the top few candidate clips. Do not download all search hits: where preview image URLs exist use them first; otherwise any video fetch should be explicit, bounded, and separate from the initial metadata-only prototype. No visual scoring of stock videos is implemented in MPT's inspected path.

## 7. Architecture Alternatives

| Option | Benefits | Trade-offs |
|---|---|---|
| A. Global stock-search change | Broad quality improvement; one shared search path | Changes ordinary projects, risks query/ranking regressions, may bias non-ASMR visuals. Not recommended as first move. |
| B. Selectable ASMR preset | Isolated behavior, clear UX, can reuse project/storyboard/visual contracts | Requires confirming where StoryWeaver stores style settings and compiles per-shot search prompts. **Recommended if StoryWeaver supports a style/preset extension point.** |
| C. Dedicated ASMR workflow | Can optimize sequences, repetition, loops, no narration | Highest effort; risks duplicating story/project/render pipelines and weakening traceability. Consider only if product requirements explicitly diverge from story-first workflows. |

**UNVERIFIED:** StoryWeaver’s supported extension points and affected shared navigation/routes cannot be checked in this repo. Before choosing B, inspect its project settings, Visual Bible, storyboard/shot schemas, visual-spec compiler, provider abstraction, asset approval flow, timeline and ADRs.

**PROPOSED:** Make ASMR clips optional visuals attached to scenes/shots, retaining narration relation by default. Permit independent background sequences only as an explicit mode with a distinct label/contract; otherwise visually unrelated cutting can distract from story meaning.

## 8. Feasibility and Gap Analysis

- **VERIFIED in MPT:** Generic query generation and provider search exist; no style-aware generation, semantic candidate ranking, video inspection, or loop detection exists in the stock path.
- **UNVERIFIED in StoryWeaver:** All current capabilities/gaps; source not present.
- **PROPOSED / prompt-only:** Can ask for action/material/frame-specific queries and yield better searches when matching footage exists. Cannot ensure inventory, rank candidates beyond provider ordering, prove a clip depicts the action, or guarantee a loop.
- **PROPOSED / query-generator work:** Needed for shot-grounded query bundles, synonyms, style-specific negative terms, and query provenance.
- **PROPOSED / ranking work:** Needed for metadata weighting, aspect/duration gates, and cross-query duplicate suppression.
- **PROPOSED / visual analysis:** Needed for content confidence (actual cut/action, macro framing) beyond tags; possibly necessary for reliable quality, but not as a first milestone.
- **INFERRED:** Existing providers appear plausible for real kinetic sand and soap cutting at some scale, based on public search pages. Sufficiency for consistent, diverse, portrait clips is not established. Surreal glowing lava/crystal/jelly visuals may require AI video generation or commissioned/curated assets if the stock inventory fails the evaluation.

## 9. Recommended Solution

**PROPOSED:** Prefer Option B, a selectable ASMR preset in the existing project/story visual pipeline, contingent on confirming StoryWeaver has a style extension point. Keep the existing provider abstraction and normal preset behavior intact. Implement in stages: structured shot-aware query terms; bounded provider query expansion; metadata filters/ranking and duplicate handling; review/audit UI; optional top-K visual scoring only if measured outcomes demand it.

**Prompt/keyword strategy:** generate English concise phrases from the approved shot's subject and action, not just the overall project topic. Include one primary query plus at most two provider-safe variants. Encode separate dimensions (material, action, framing) in stored metadata so ranking can compare each. Do not fabricate physical actions for a narrative shot.

**Ranking strategy:** apply deterministic technical filters first; score query-term/tag/description overlap and close-up cues; de-duplicate by provider asset ID and canonical source URL; retain provider rank as a tie-breaker. Store scores/reasons for audit. Surface a no-match/low-confidence result instead of silently selecting unrelated footage.

**Config/data:** **UNVERIFIED** whether StoryWeaver needs a DB migration. Prefer existing project style settings and asset metadata if their contracts allow a versioned preset and query provenance. Only add schema/config fields after inspecting actual models and migration policy. Do not persist provider API keys with artifacts.

**Expected project effect:** the preset should apply only to opted-in ASMR projects. Existing projects must retain the default search behavior and existing artifact compatibility. That must be proven in StoryWeaver tests; impossible to validate here.

## 10. Proposed Implementation Roadmap

1. **P0 — locate/verify StoryWeaver repo:** map approved storyboard scene/shot → visual prompt/query compiler → Pexels/Pixabay adapters → asset selection/download/cache/approval → timeline/render. Read relevant ADRs and tests.
2. **P1 — baseline dataset:** collect a fixed, small set of ASMR shot descriptions; issue capped metadata-only searches; manually label top candidates as exact action, adjacent/similar, irrelevant, usable aspect, and loopable. Record provider, query, date and results.
3. **P2 — preset query generation:** add testable structured output for action/material/framing with bounded synonyms; preserve story/shot IDs and ordinary behavior.
4. **P3 — deterministic ranking:** add aspect/resolution/duration gates, query metadata scoring and cross-query deduplication; record score explanations and support low-confidence review.
5. **P4 — visual evaluator decision:** compare top-K metadata ranking with frame-based evaluation only if baseline metrics miss target. Add cost/latency budget and opt-out/fallback.
6. **P5 — decide supply gap:** if exact footage coverage remains poor, offer generated assets as an explicit separate provider/workflow with cost confirmation and persisted provenance; do not disguise generated visuals as stock.

**Minimal first implementation step:** after opening the real StoryWeaver repo, build a no-code benchmark harness or documented manual matrix of 5–6 fixed queries per ASMR category, with top 5 metadata results and human relevance labels. It answers inventory and noise before application changes.

## 11. Risks, Costs and Performance

- **VERIFIED (provider docs):** API limits and caching obligations apply; Pexels requires prominent attribution, and Pixabay requires caching search responses for 24 hours and disallows systematic mass downloading. Respect each provider's current terms.
- **INFERRED:** Query expansion multiplies API calls and latency; concurrent calls can hit limits. Cap query variants and cache by provider + exact query + filters.
- **INFERRED:** Metadata scoring is cheap but can create false confidence. VLM scoring adds model cost, latency, frame download/processing and possible privacy concerns; motion analysis is more costly still.
- **INFERRED:** More candidates increase storage/network costs if downloaded. Rank metadata/preview candidates first and fetch only selected clips.
- **PROPOSED:** Add explicit per-shot candidate caps, request budgets, bounded timeouts, cache-aware evaluation, and an observable fallback to ordinary visuals or manual selection.
- **UNVERIFIED:** StoryWeaver’s current task/transaction model, storage policy, caching policy, provider usage budget, and deployment hardware are not available to evaluate.

## 12. Test Strategy and Acceptance Criteria

**VERIFIED MPT evidence:** The repository has focused tests under `test/services/` and CI runs pytest, Ruff, and compile checks. This demonstrates service-level test conventions, but does not validate StoryWeaver behavior. No tests were run for this read-only investigation.

**PROPOSED StoryWeaver acceptance criteria:**

1. ASMR preset produces structured queries grounded in approved shot subject/action and remains within a fixed query cap; tests cover each requested material/action plus invalid/empty model output.
2. Default non-ASMR projects yield unchanged queries/results under regression fixtures.
3. Candidate filters reject wrong aspect/insufficient resolution/too-short clips; duplicate assets returned by multiple queries appear once.
4. On a manually labeled benchmark, top-5 exact-or-usable-action precision and coverage meet thresholds set before implementation (suggest initial target: ≥70% precision for stock-feasible categories; report category-specific coverage rather than one aggregate).
5. No-result/low-confidence cases are visible and do not silently select unrelated footage.
6. Cache/rate limits, attribution/provenance, bounded latency and no-provider-credential leakage are tested.
7. Timeline/render output retains shot/source traceability and existing approvals/versioning semantics.

**PROPOSED minimal test dataset:** 5 queries each for kinetic sand, soap, foam, jelly/crystal, lava, and macro slicing; top 5 results each; two reviewers label exact/adjacent/irrelevant, real/CGI/uncertain, portrait usable, and loopable. A loop label requires watching temporal boundaries, not metadata.

## 13. Open Questions

1. Where is the actual StoryWeaver checkout, and what branch/commit should be considered authoritative?
2. Are Pexels/Pixabay enabled there, and what do their response adapters return/store?
3. Does the intended ASMR video include narration/story, or is it a standalone visual compilation? Is audio/ASMR sound required, or visuals only?
4. Is content expected to be genuine physical footage, and are AI-generated/CGI clips acceptable when labeled?
5. What is acceptable per-minute provider spend, end-to-end latency, and manual review effort?
6. Should loop quality be a hard requirement or a ranking preference?
7. What attribution/license UX and user approval are required before rendering/publishing?

## 14. Evidence and Source References

### Repository evidence (MoneyPrinterTurbo)

- `app/services/task.py`: term orchestration (`generate_terms`, lines 379–421); material retrieval and video handoff (1781–1823); source allocation (957–979, 995–1077).
- `app/services/llm.py`: keyword prompt and response validation (`generate_terms`, 1023–1131).
- `app/services/material.py`: provider adapters (341–585); cached search (1853–1944); parallel query execution (1946–2004); download, deduplication and selection (2161–2378); script-order matching (2938–3060); material provenance helper/persistence (116–186).
- `app/services/material_cache.py`: 24-hour TTL, cache key, source-info allowlist and cache read/write (24–158, 203–324, 327–403).
- `app/models/schema.py`: `MaterialInfo` and `VideoParams` (85–165).
- `app/services/video.py`: unique-source clip preference (`_prioritize_unique_source_clips`, 231–296); FFmpeg render path elsewhere in this service.
- `app/services/task_artifacts.py`: atomic task `script.json` persistence and patching (24–105).
- `config.example.toml`: Pexels/Pixabay key setting names (around line 90); `.github/workflows/ci.yml`: CI verification commands.

### External provider evidence

- [Pexels API documentation](https://www.pexels.com/api/documentation/) — video search filters, response metadata, limits, attribution guidance.
- [Pixabay API documentation](https://pixabay.com/api/docs/) — video API query parameters, tags/renditions, rate limit and caching requirements.
- [Pixabay kinetic sand search page](https://pixabay.com/videos/search/kinetic%20sand%20cutting/) and [soap cutting page](https://pixabay.com/videos/search/soap%20cutting/) — page-level evidence/examples, not authenticated API counts.
- [Pexels kinetic sand search page](https://www.pexels.com/search/videos/cutting%20kinetic%20sand/) and [soap cutting page](https://www.pexels.com/search/videos/soap%20cutting/) — page-level evidence/examples, not authenticated API counts.

### Limits of evidence

- **UNVERIFIED:** No StoryWeaver source or architecture docs were available in the working tree.
- **VERIFIED:** The MPT Pixabay adapter call failed at DNS resolution and the prior six raw Pixabay probes returned connection errors; no authenticated provider counts/metadata were observed. Pexels API search was not attempted because no Pexels credential was configured.
- **UNVERIFIED:** Public website search counts do not equal API totals, exact relevant results, license suitability for a particular use, or API eligibility.
