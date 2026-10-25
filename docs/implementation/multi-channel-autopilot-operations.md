# Multi-Channel Autopilot: Operations and Validation

## Start MPT

Use the normal MPT runtime. The API lifespan starts the persistent Autopilot poller; the Streamlit process does the same when its app starts. SQLite state is shared by the processes at `storage/autopilot/profiles.sqlite3`. Run the API and WebUI on one host with the same working tree, `config.toml`, and persistent `storage` directory. Do not put this SQLite database on a multi-host network filesystem.

For browser-independent scheduling, run the dedicated worker under the host service manager alongside the API. It shares the same SQLite database and runtime configuration, so queued jobs continue when the browser closes:

```sh
uv run mpt-autopilot-worker
```

Configure three daily slots by setting the channel timezone to `Asia/Kolkata`, the weekly count to `21`, local times to `09:00,14:00,20:00`, and all seven weekdays. The scheduler creates work up to the configured generation lead and fills the validated-video buffer target (default six). Visual styles rotate in the saved order. Leave the channel paused or at zero publications per week while testing; automatic public release remains separately opt-in.

```sh
cp config.example.toml config.toml  # only for a first-time setup; preserve an existing config.toml
uv sync --frozen
uv run python main.py
```

Start the WebUI in another terminal:

```sh
sh webui.sh
```

For an API-only deployment, start `uv run python main.py` under the host's normal service manager and expose the API only behind the deployment's usual access controls. Use the API docs at `http://127.0.0.1:8080/docs` (adjust for the configured API port) or the Channel Autopilot workspace in Streamlit. Do not run multiple hosts against the same SQLite file.

Keep the `storage/autopilot` directory on a persistent local volume and back it up with the rest of MPT's state. SQLite/WAL is intended for processes on one host and a filesystem with SQLite locking; this is not a multi-host distributed queue.

## Required setup

1. Configure an MPT text provider, stock video source, voice/subtitle settings, and FFmpeg using the normal `config.toml` setup. Text generation and rendering use MPT's existing global provider configuration; the profile's provider label/options do not currently select independent runtime credentials. Use a persistent `storage` directory and back it up while MPT is stopped (or with a SQLite-aware backup tool).
2. New channel profiles default to `youtube_direct`. Choose `upload_post` when cross-platform distribution or an existing Upload-Post account is needed. Legacy profile rows and publication rows without a backend value are treated as Upload-Post.
3. For Upload-Post, keep its API key in `config.toml`/the runtime secret store, connect each destination to an Upload-Post profile, and choose that profile in Channel Autopilot. Do not put provider keys in channel profiles.
4. For direct YouTube, complete the Google Cloud/OAuth setup below. Configure one MPT channel profile per destination, select **YouTube Data API direct**, then use **Authorize / reconnect this YouTube channel**. Confirm the returned channel title/ID is the intended destination. Repeat independently for every channel.
5. For either backend, configure a distinct niche, language, script style, ASMR/footage settings, duration, timezone, local schedule and publication policy for each channel. Keep channels paused or at zero weekly frequency while testing. Enable them only after checking destination, snapshot and schedule.

The global MPT API key protects authenticated API routes when configured. Direct OAuth start/status/disconnect routes are authenticated; Google's redirect uses a public callback protected by short-lived, single-use random OAuth state. Direct refresh tokens and resumable session URLs are Fernet-encrypted in the local Autopilot SQLite database. Preserve the encryption key with the database backup; losing it requires reconnecting accounts. Upload-Post owns the OAuth lifecycle for its profiles.

## Direct YouTube setup (Ubuntu, self-hosted)

1. In [Google Cloud Console](https://console.cloud.google.com/), create/select an operator-owned project and enable **YouTube Data API v3**. The current [`videos.insert` reference](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert) documents a 100-calls/day video-upload bucket at one unit per upload call. MPT also defaults to a local 100-inserts/day guard. Direct uploads are therefore not unlimited, and neither limit overrides a channel's own YouTube upload limit, which varies by account and feature eligibility.
2. Configure Google Auth Platform/OAuth consent: app name, support email, developer contact, and audience (External for personal Google accounts; Internal only for eligible Workspace). Add test users while in Testing. Complete/publish OAuth app verification when required before general distribution. Testing-mode refresh tokens for non-basic scopes can expire after seven days; expect to reconnect.
3. Create an OAuth client of type **Web application**. Add the exact redirect URI `https://YOUR_HOST/api/v1/youtube/oauth/callback` (local-only development can use exact `http://localhost:8080/api/v1/youtube/oauth/callback`). Set the identical URI in MPT. Do not use wildcard redirect URIs; protect remote callback traffic with HTTPS.
4. Provide `MPT_YOUTUBE_CLIENT_ID`, `MPT_YOUTUBE_CLIENT_SECRET`, `MPT_YOUTUBE_REDIRECT_URI`, and `MPT_YOUTUBE_TOKEN_ENCRYPTION_KEY` to the Ubuntu service manager. Generate the encryption key once with `uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`. Store it in a root-readable secret/environment file, not shell history, source control, logs or chat. Keep it stable and back it up securely. Environment variables are preferred over the optional example TOML values.
5. In Channel Autopilot, create a profile per YouTube destination and authorize each intended channel identity. MPT rejects grants that return multiple channels because the standard upload call cannot select an arbitrary channel ID. The callback stores the verified channel ID; MPT rechecks it before upload and checks returned `snippet.channelId` afterward. Resolve multi-channel Google grants without bypassing this check.
6. Start on `manual` or `private`, keep automatic-public opt-in off, and inspect every artifact. Public schedules require future `status.publishAt` plus factual review, technical QA, originality review, no repetition warnings and per-asset rights evidence.

OAuth app verification is distinct from a **YouTube API Services compliance audit** or **quota-extension audit**. Google's current `videos.insert` reference says uploads from unverified API projects are not restricted to private viewing mode. This removes the obsolete blanket “unverified means private only” claim; it does not waive OAuth consent requirements, YouTube API Terms/developer-policy compliance, project quota, channel limits, or required audits. MPT requests `youtube.upload` and `youtube.readonly` for upload and channel identity verification. Recheck Google's current [OAuth requirements](https://developers.google.com/identity/protocols/oauth2), [YouTube API developer policies](https://developers.google.com/youtube/terms/developer-policies-guide), [quota/compliance audit guidance](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits), and [videos.insert reference](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert).

### Optional independent fact-source retrieval

MPT includes a bounded Brave Search evidence-candidate adapter. Set `fact_research_provider = "brave_search"`, supply `MPT_FACT_RESEARCH_API_KEY` through the service secret store, configure `fact_research_trusted_domains`, and set `fact_research_max_claims` (default 8, hard cap 12). It submits at most five search results per extracted narration sentence. Search excerpts are candidate evidence only: an operator must inspect cited pages, decide every claim, and record claim-to-source notes before public publishing. If retrieval fails or no provider/domain allowlist is configured, public publishing still requires human review; no LLM self-confirmation counts as evidence.

## Current execution and policy behavior

- The recurring scheduler materializes per-channel local-time slots, pins profile version and resolved task parameters, deduplicates slots in SQLite, and queues jobs. It schedules generation up to one hour before a local publish time; missed slots older than three hours are recorded as skipped. A nonexistent daylight-saving wall time is skipped; an ambiguous fall-back time uses the earlier occurrence. Upload-Post receives the slot's local wall time together with the pinned IANA timezone.
- Queued jobs are claimed transactionally and resumed after restart. Slot planning has a one-hour persisted lease and recovers a plan/job already committed before a crash. A dead local generation worker becomes failed for operator inspection; MPT does not repeat an uncertain generation call automatically. Upload-Post status is polled by request ID/history. Direct uploads resume only the persisted resumable session or inspect an existing `videos.list` ID; missing session/ID remains unknown for manual reconciliation.
- Autopilot calls the existing `task.start(..., suppress_auto_upload=True)`. The manual MPT pipeline retains its existing global immediate-upload behavior.
- Video duration is measured after rendering. Out-of-range duration fails the job without an automatic rerender. Technical QA checks artifact readability, duration, orientation, audio, required subtitle output and material references. Each downloaded material retains available provider, asset, source page and provider-reported license/attribution fields. Missing fields remain unknown; provider metadata does not prove commercial-use rights. Public release also requires human originality review and at least one rights record per retained asset. Cropping, recoloring, subtitles or footage shuffling are not treated as transformation proof.
- LLM topic ideas/scripts remain unverified. The bounded retrieval provider returns claim-level source candidates, then an operator records a decision and claim-to-source notes. Search excerpts are not machine verification. If retrieval is unavailable, human source review remains required. Never use LLM self-confirmation as evidence.
- Repetition checks flag duplicate/near-duplicate topics, scripts/hooks, reused asset IDs and very similar structures. These are conservative review signals, not a YouTube/YPP score. Readiness reports `unknown` for copyright claims and YPP eligibility metrics because this integration cannot reliably access them.
- Manual policy requires QA approval and a separate publish action; it uploads private. Private/unlisted policies can upload automatically after technical QA. Scheduled-public and automatic-public require explicit per-channel public opt-in, human fact verification, technical QA, no repetition warnings, originality marked original, and rights marked documented with evidence for every retained asset. Unknown/failing checks block public release.
- New jobs pin provider backend and destination in their immutable profile snapshot and publication record. Old publication rows receive the `upload_post` migration default. Upload-Post retains its request-ID/idempotency/history behavior. Direct YouTube has no server-side idempotency key: MPT stores a stable job key and encrypts the resumable session URL, then reconciles only that same session. If no conclusive result exists, the row stays `unknown`; exactly-once delivery is not claimed and an operator must inspect YouTube Studio before any manual recovery.
- MPT uses `videos.list` for status/reconciliation but does not call `videos.update`, upload custom thumbnails, or upload separate caption tracks; subtitles are rendered into the video. Copyright claims and YPP metrics are unsupported. Direct uploads set title, description, category, privacy, made-for-kids, synthetic-media disclosure and future `status.publishAt` as configured.

## WebUI and API

The Channel Autopilot workspace provides profile editing, local schedules, pause/resume, source research and per-claim decisions, production jobs, technical QA, originality/rights review, artifact previews, approval/rejection, receipts, OAuth authorization and readiness. API routes include `/api/v1/channels/{id}/youtube/oauth`, `/api/v1/youtube/oauth/callback`, `/api/v1/channels/{id}/youtube/connection`, `/api/v1/channels/{id}/monetization-readiness`, `/api/v1/channels/{id}/content-plans/{id}/research`, `/api/v1/production-jobs/{id}/content-quality-review`, and the existing Autopilot routes. Authenticated endpoints use MPT's optional API-key setting; the OAuth callback is protected by the expiring one-use state.

## Known limitations and unverified live behavior

- Search provider credentials/domain allowlists must be configured by the operator; no provider call was made in this task. Candidate excerpts can be weak, incomplete or contradictory, so a human must inspect primary/authoritative sources. Numeric differences only trigger a coarse review flag.
- QA cannot independently verify license terms, factual truth, exact subtitle/audio synchronization, semantic video completeness or YPP acceptance. Copyright claim status and channel monetization metrics remain unknown.
- SQLite leases and worker checks target one host. Cross-host scheduling, distributed locks, and reliable recovery of a remote worker are not supported.
- Upload-Post and Google APIs were mocked only. No OAuth client, account calls, refresh, real upload, scheduled publish, live generation or public upload was tested. Direct YouTube's actual project quota and channel daily limits remain provider/account-controlled.
- A failed resumable session is not automatically replaced with a second insert. If Google expires a session or a complete upload cannot be recovered, inspect YouTube Studio and reconcile manually before an operator-authorized retry.
- Provider-side reconciliation of status `completed` records the provider result payload. Availability and exact shapes of YouTube IDs/URLs vary by Upload-Post account/API response; fields are retained when returned.
- The scheduler's default catch-up policy skips slots more than three hours late. The policy is explicit and persisted, but operators should inspect skipped slots after extended downtime.

## Monetization readiness: what MPT checks and what YouTube decides

MPT's readiness panel separates technical checks from human review and unknown data. `passed` means only the local media checks succeeded. It is not a YPP prediction. YouTube's current policy expects original/authentic content with meaningful viewer value; mass-produced, generic/repetitive or minimally modified reused content can fail channel-level review. Similar series formats may qualify when the bulk substance materially differs. AI tools/voice are not inherently disqualifying; realistic altered/synthetic content must be disclosed when applicable. Copyright permission and YPP reused-content review are separate: licensed footage can still fail reused-content review if the channel adds little original value.

For context, current official eligibility pages list an expanded-YPP entry route of 500 subscribers, three public uploads in 90 days, and either 3,000 qualified public watch hours in 365 days or 3 million qualified public Shorts views in 90 days, in eligible regions. Full ad revenue eligibility lists 1,000 subscribers and either 4,000 qualified public watch hours in 365 days or 10 million qualified public Shorts views in 90 days. Shorts-feed watch hours do not count toward the 4,000-hour route. YouTube says YPP applications are reviewed after thresholds; meeting counts does not guarantee acceptance. Its current YPP/Shorts pages also describe a policy update beginning February 1, 2027; recheck Studio and the official pages before planning future thresholds or accepting updated terms.

Sources: [YouTube channel monetization policies](https://support.google.com/youtube/answer/1311392?hl=en), [YPP overview and eligibility](https://support.google.com/youtube/answer/72851?hl=en), [expanded YPP feature thresholds](https://support.google.com/youtube/answer/94522?hl=en), [Shorts monetization policies](https://support.google.com/youtube/answer/12504220?hl=en), [altered/synthetic disclosure](https://support.google.com/youtube/answer/14328491?hl=en), [copyright basics](https://support.google.com/youtube/answer/2797466?hl=en), [AI tools and YPP clarification from TeamYouTube](https://support.google.com/youtube/blog/356734251/response-to-creator-questions-about-ypp-policies-july-2025?hl=en-AS). YPP counters are not currently read by MPT; inspect the channel's Earn tab in YouTube Studio.

## Recovery and operator checks

- Inspect channels, slots, plans, jobs, QA, and publication receipts in **Channel Autopilot** or the `/api/v1/channels`, `/api/v1/scheduler`, and `/api/v1/production-jobs` endpoints. API routes use the existing MPT API-key configuration when it is set.
- After an outage, restart API and WebUI against the same database. Queued jobs are resumed; stale/failed generation requires operator inspection and retry. Review slots marked skipped/failed after downtime; slots more than three hours late are skipped by policy.
- If a publication is uncertain, use the job receipt/reconcile control. Do not create a replacement job or manually upload until resolved. Upload-Post checks the pinned request/external ID; direct YouTube resumes only the same encrypted session or looks up a stored video ID. If no key exists to reconcile, inspect YouTube Studio and keep the job blocked.
- For a disconnected direct destination, reauthorize from the same channel profile and confirm its channel ID. Reauthorization creates a new immutable connection ID; old jobs keep their original destination and token record while it remains valid. If Google revoked that token, the job fails closed instead of using a newly selected destination. Explicit channel disconnect pauses the profile and revokes all local connections.
- For a QA failure, inspect the generated artifact and QA details, correct the source/profile issue, and retry only after establishing whether the prior MPT task completed. Autopilot does not silently loop short ASMR footage or rerender duration failures.
- Back up `storage/autopilot/profiles.sqlite3` and its SQLite WAL state using a consistent SQLite backup procedure. Preserve generated artifacts under `storage`; job output references point there.

## Final validation status (2026-10-23)

| Status | Capability | Evidence / limitation |
|---|---|---|
| PASS | Mocked Autopilot/provider suite | 143 passed, 12 subtests passed across profiles, production jobs, schedules, receipts, resumable uploader, OAuth/token storage, source research, per-asset rights mapping, licensing metadata and ASMR selection. |
| PASS | Lint, compile, lock and whitespace checks | Focused Ruff passed; modified Python modules compiled; `uv lock --check --offline` and `git diff --check` passed. |
| PASS | Two-channel isolation and immutable routing | Mock OAuth bindings use separate verified channel IDs; reauthorization retains old connection IDs; Upload-Post legacy rows remain defaulted to Upload-Post; direct receipts are not submitted twice. |
| PASS | Public release gates and readiness reporting | Mock checks require opt-in, human-verified claims, original-content review, per-asset rights records, technical QA and no repetition warnings; unsupported YPP/copyright data display as unknown. |
| FAIL | One unrelated test in an overbroad test invocation | `test_task.py::test_cross_post_state_update_retries_transient_backend_failure` uses an unmocked social-metadata path; it hit a connection error/fallback and then failed a sleep-call assertion. It is outside the changed Autopilot path and was not rerun. |
| BLOCKED | Live OAuth, account authorization, refresh/revocation, private/scheduled/public uploads, real YouTube IDs, YouTube quota, Upload-Post calls | No OAuth credentials or provider authorization were supplied; no upload was performed. Mock tests do not validate account/project policy or real-world publishing. |
| BLOCKED | Real Hindi 25–30 second generation and human review of resulting video quality | No live generation was run. Viewer value, narration accuracy, footage fit and YPP compliance cannot be established by mocked tests. |
| NOT IMPLEMENTED | YPP eligibility counters, reliable copyright-claim lookup, `videos.update`, custom thumbnails, separate caption-track upload | Readiness shows unsupported items as unknown; existing generated subtitles are rendered into the video. |
| NOT IMPLEMENTED | Independent per-channel text/media provider credentials and multi-host distributed scheduling | Profiles pin creative settings/destinations, but generation still uses MPT's process-wide provider configuration and SQLite remains single-host. |

No video was uploaded. The accidental unrelated social-metadata test was the only test invocation that entered a path not explicitly mocked; it failed on connection error before returning provider metadata. No YPP outcome, real account isolation or publishing behavior is validated by this report. **YPP approval remains YouTube's decision.**

Upload-Post contract references: [video upload](https://docs.upload-post.com/api/upload-video/), [scheduled posts](https://docs.upload-post.com/api/schedule-posts/), and [upload history](https://docs.upload-post.com/api/upload-history/). These documents describe provider behavior; MPT's integration has not been live-tested here.

## Verification performed

Automated tests use mocked YouTube and Upload-Post services. The final selected run on 2026-10-23 passed 143 tests and 12 subtests. It covers independent channel OAuth bindings, encrypted tokens, resumable offsets, channel-ID validation, local insert quota/rate-limit handling, disclosure/scheduling metadata, unknown-outcome hold, publication idempotency, legacy Upload-Post dispatch, content research review and asset provenance/right-evidence mapping. The separate accidental `test_task.py` failure is listed above. No OAuth client, Google API, Upload-Post API or video upload was used by the passing suite.
