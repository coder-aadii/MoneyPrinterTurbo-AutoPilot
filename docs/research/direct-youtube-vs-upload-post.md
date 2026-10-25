# Direct YouTube Data API vs Upload-Post for MPT Autopilot

**Assessment date:** 2026-10-23
**Scope:** investigation only; no application code, publishing settings, Autopilot records, or provider accounts were changed. This report describes the checked-out working tree, which already contains substantial uncommitted Autopilot work.

## Executive recommendation

**Recommendation — PROPOSED:** For a self-hosted installation that publishes only to YouTube across several channels, make direct YouTube Data API publishing the long-term primary path, while retaining Upload-Post as an optional backend for cross-platform posting and for existing jobs. Do not remove Upload-Post. The cleanest target is a selectable publisher abstraction (Option C), introduced incrementally with existing Upload-Post jobs pinned to their original backend. If the operator only needs YouTube and wants the least engineering now, Upload-Post Basic is a commercially straightforward bridge: the currently published plan offers unlimited uploads and five profiles at $24/month month-to-month, or $192/year. It moves the cost issue; it does not make Upload-Post necessary for YouTube.

The free tier is unsuitable for the requested cadence. At two videos/channel/day and a 30-day planning month, the expected 60, 120, and 300 monthly posts exceed the 10-upload allowance by 6x, 12x, and 30x. Direct API uploads are technically feasible and the current Google reference explicitly says unverified API projects are not restricted to private viewing. They are still subject to project quota, channel daily limits, OAuth lifecycle, and YouTube API compliance. Do not promise exactly-once publishing: an uncertain insert must be reconciled or held for an operator, not blindly repeated.

### Finding labels

- **VERIFIED** — directly supported by checked-in source/history or official documentation/pricing linked here.
- **INFERRED** — architectural or commercial conclusion drawn from verified facts.
- **UNVERIFIED** — current sources do not establish the claim, or it needs account/provider-specific confirmation.
- **PROPOSED** — recommendation or design for future work, not implemented.

## 1. Why MPT uses Upload-Post

### What the repository verifies

- **VERIFIED:** The checked-in service calls Upload-Post's `POST /api/upload`, sends one or more `platform[]` values, and passes the video file, profile username, title, privacy/audience metadata, and optional schedule, timezone, external ID, request ID, and idempotency key. It polls asynchronous requests and can query history by external ID. See [`app/services/upload_post.py`](../../app/services/upload_post.py#L175) (`upload_video`), [`check_history`](../../app/services/upload_post.py#L405), and the [Upload-Post upload API](https://docs.upload-post.com/api/upload-video/).
- **VERIFIED:** The service docstring describes cross-posting to TikTok, Instagram, and YouTube Shorts. Global config exposes a provider API key, one default username, enabled/platform/automatic-upload settings, YouTube privacy/audience options, and pending-task limit. The example config is at [`config.example.toml`](../../config.example.toml#L581); the WebUI config is in [`webui/Main.py`](../../webui/Main.py#L3268).
- **VERIFIED:** Ordinary task completion submits background cross-posting if global auto-upload is enabled. It creates YouTube title/description, privacy, made-for-kids, and synthetic-media parameters in [`app/services/task.py`](../../app/services/task.py#L1300) and submits at [`app/services/task.py`](../../app/services/task.py#L1391). This is a multi-platform abstraction already present in MPT, not a YouTube API client.
- **VERIFIED:** In the current Autopilot implementation, profiles pin an Upload-Post username, binding validation calls Upload-Post, and publication dispatch/reconciliation instantiate `UploadPostService` directly. See [`app/models/channel_profile.py`](../../app/models/channel_profile.py#L48), [`app/services/channel_profiles.py`](../../app/services/channel_profiles.py#L175), and [`app/services/autopilot.py`](../../app/services/autopilot.py#L722) / [`publish_job`](../../app/services/autopilot.py#L994).
- **VERIFIED:** Git history available in this checkout has only one commit, `cedd8ea` (“Initial commit: MoneyPrinterTurbo”), dated 2026-10-23. The original Upload-Post module and task integration are in that snapshot. There are no earlier commits, commit messages, or review records here that explain the original maintainer's decision. The newer Autopilot files are currently untracked/modified working-tree content, so their present design is evidence of the current architecture, not a historical maintainer statement.

### Intent vs inference

- **UNVERIFIED (maintainer intent):** No surviving commit history or design record in this checkout says why the original maintainers selected Upload-Post. It would be unsafe to state their reason as fact.
- **INFERRED:** Cross-platform convenience is the strongest architectural explanation: a single multipart API call targets multiple social platforms and Upload-Post owns the platform OAuth connections, platform-specific transformations, scheduling, retries/status, and provider-side account connections. The original service itself calls this “cross-posting.”
- **INFERRED:** MPT does **not** technically need Upload-Post to upload to YouTube. Google's API has `videos.insert`, metadata fields, OAuth user authorization, resumable upload, privacy and scheduling support. Upload-Post is currently MPT's chosen integration/provider and provides operational convenience, not an exclusive YouTube capability.
- **INFERRED:** Removing Upload-Post would lose a unified multi-platform posting endpoint, its account/profile connection UX and provider-managed token refresh, per-platform normalization/transform behavior, its API scheduling and job management/status/history, and current MPT's Upload-Post idempotency/request-ID workflow. It would not remove YouTube's native upload/schedule capability; it would require MPT to implement and operate it. TikTok, Instagram, and other network destinations would need another publisher or would cease to work.
- **INFERRED:** Much of MPT's control-plane work is reusable: immutable job/profile snapshots, QA/evidence gates, schedule slots, publication uniqueness, receipt sanitization, and operator review. The media, metadata and final destination mapping are reusable concepts. The provider-specific transport, profile binding, OAuth, status reconciliation, and provider IDs cannot be reused as a direct YouTube implementation.

## 2. Upload-Post limits, plan fit, and functions MPT uses

### Verified public plan information

Upload-Post's [current official pricing](https://www.upload-post.com/social-media-scheduler/) and homepage plan table list Free at 10 uploads/month and two profiles, with YouTube and scheduling included. A profile represents one person/client and allows one account per platform; Free therefore lists up to two YouTube accounts. The official monthly-plan table lists Basic at $24/month with five profiles and unlimited uploads; Professional at $50/month with 25 profiles; Advanced at $147/month with 75 profiles; Business at $438/month with 225 profiles. The pricing page currently also advertises annual billing at $16, $33, $118, and $350 per month-equivalent, respectively (billed annually), with listed annual totals of $192, $400, $1,411, and $4,200. Prices and plan terms can change; recheck before purchase.

| Plan (official published pricing) | Monthly price | Upload allowance | Profiles / YouTube accounts at most* | Fit for these YouTube-only cases |
|---|---:|---:|---:|---|
| Free | $0 | 10/month | 2 | Allowance fails all three workloads |
| Basic | $24/mo ($192/year) | Unlimited | 5 | Covers 1, 2, or 5 channels by profile count |
| Professional | $50/mo ($400/year) | Unlimited | 25 | Covers all listed workloads |
| Advanced | $147/mo ($1,411/year) | Unlimited | 75 | More than required here |
| Business | $438/mo ($4,200/year as listed) | Unlimited | 225 | More than required here |

\*The official pricing page says each profile connects one account per platform and explicitly gives examples of two YouTube accounts for two profiles, etc. It does not state a separate YouTube-account cap beyond profile count.

**VERIFIED with one remaining ambiguity:** Upload-Post's [official FAQ](https://docs.upload-post.com/resources/faq/) says Basic “unlimited uploads” means unlimited API calls per month; it lists profile caps separately and says daily platform caps are per connected social account, not per Upload-Post user/profile. That supports reading the Free 10/month as a plan-level allowance shared across profiles, rather than 10 per profile. The FAQ does not say exactly when a scheduled API call is debited (when scheduled vs when executed) or whether a multi-platform request is debited once or by destination. Treat each planned per-channel publication as consuming one monthly allowance and confirm edge accounting with Upload-Post if it materially affects a forecast.

### Workload arithmetic

**VERIFIED arithmetic / INFERRED quota impact:** use 30 days/month and one YouTube post per channel per publishing event.

| Channels × 2 videos/day | Posts/month | Free 10/month usage | Over free quota | Free profile capacity |
|---:|---:|---:|---:|---|
| 1 × 2 | 60 | 6x | 50 more than allowance | Fits |
| 2 × 2 | 120 | 12x | 110 more than allowance | Fits exactly |
| 5 × 2 | 300 | 30x | 290 more than allowance | Exceeds by 3 profiles |

This counts each channel's post as one publication. Cross-posting one video to several platforms may have different quota accounting and is not included in this simple YouTube-only model.

### MPT's Upload-Post-dependent API surface

**VERIFIED:** The code currently depends on these provider features:

1. `POST /api/upload`, `Authorization: Apikey`, profile `user`, YouTube platform selection, multipart video, optional YouTube title/description/tags, privacy, made-for-kids, and synthetic-media declaration.
2. Optional Upload-Post scheduled date/timezone and a returned provider request/job/schedule identifier.
3. Async status polling using provider `request_id`, per-platform success results, history lookup using `external_id`, profile, and YouTube platform.
4. Provider idempotency via `Idempotency-Key` and stable client-generated request IDs/external IDs.
5. `GET /api/uploadposts/users` for profile discovery and YouTube connection/reauthorization checks (in `channel_profiles.py`).

The API documentation supports separate YouTube fields and schedule parameters (`youtube_publish_at`, which forces private; or generic `scheduled_date`), captions/subtitle files and language, notification setting, tags, `selfDeclaredMadeForKids`, and `containsSyntheticMedia`. MPT currently passes only a subset: title, description, privacy/audience, synthetic declaration, generic scheduled date, timezone, request/idempotency IDs and external ID. Current task cross-posting also sends platform-specific metadata; Autopilot itself currently asks only for YouTube.

Platform restrictions: YouTube is included on Free according to Upload-Post; TikTok posting is explicitly excluded from Free in current official product material. Upload-Post's official FAQ says YouTube has a 30-video rolling 24-hour cap per connected account, while its separate [Limits of Uploads guide](https://docs.upload-post.com/guides/limit-of-uploads/) says YouTube has a 10-post/24-hour cap. **UNVERIFIED:** These current official pages conflict; confirm the effective cap with provider support. Both are above this report's two/day/channel examples, but YouTube's own per-channel daily limit also applies. Upload-Post's supported platform checklist, YouTube metadata restrictions (e.g. custom Shorts thumbnails require a verified channel), and account authorization requirements remain in force. Its limits guide states daily caps are checked again at scheduled execution time, so a schedule can still be rejected at publish time.

## 3. Direct YouTube Data API v3 feasibility

### Project and authorization setup

**VERIFIED:** A direct publisher needs a Google Cloud project, YouTube Data API v3 enabled, and OAuth client credentials. The account owner must authorize each relevant channel/account. YouTube Data API upload is user-scoped; a service account is not a substitute and the API docs state this flow is unsupported for YouTube channel authorization. For a self-hosted installation, operators can create/use their own project and have each channel owner authorize the local MPT instance. Google OAuth supports server-side web app flows with offline consent and refresh tokens; a local installed-app flow is also viable for a single-host setup, subject to Google's current OAuth client restrictions.

**OAuth verification is not the same as YouTube API compliance review.** Google's OAuth verification/branding process concerns whether an external app and its requested user-data scopes can be presented to end users in production. A project left in External/Testing is limited to listed test users (up to 100), shows an unverified warning, and YouTube-scope refresh tokens expire after seven days. Publishing an OAuth app in production and completing whatever OAuth verification applies is one axis. YouTube's API Compliance Audit and quota-extension review are a separate axis. Google says additional quota requires an audit/compliance review; do not create extra Cloud projects to shard the same use case.

**October 2026 clarification — VERIFIED:** The current official [`videos.insert` reference](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert) says: “Videos uploaded from unverified API projects are not restricted to private viewing mode.” It also permits `status.privacyStatus` values public/private/unlisted and `status.publishAt`. This directly supersedes the older, often repeated claim that every upload from an unverified project is forced private. It does not exempt an API client from OAuth verification rules, YouTube API policies, channel limitations, or project quota requirements.

### API operations and metadata mapping

| Need | YouTube Data API | Assessment |
|---|---|---|
| Upload video | `videos.insert` | **VERIFIED:** Supports media upload and metadata. Use resumable upload for interrupted/restart-safe transfer. |
| Set/update metadata or schedule | `videos.update` | **VERIFIED:** Set `status.privacyStatus=private` and `status.publishAt` for first-time future scheduled publication; updates must include private status when setting publishAt. `publishAt` cannot be used for already published videos. |
| Read known video receipt / processing / privacy | `videos.list` | **VERIFIED:** Read by ID (and request the relevant parts); use returned video ID as the primary external receipt. |
| Privacy and audience | `status.privacyStatus`; `status.selfDeclaredMadeForKids` | **VERIFIED:** Supported resource fields. |
| Synthetic-media disclosure | `status.containsSyntheticMedia` | **VERIFIED:** Current `videos.insert` supports this field. MPT's Upload-Post adapter currently sends `containsSyntheticMedia=true` whenever YouTube metadata is present, so a direct adapter must preserve the declaration semantics while allowing a truthful per-video value. |
| Tags/title/description | `snippet.title`, `.description`, `.tags[]`, `.categoryId` | **VERIFIED:** Supported; title length max 100 characters. |
| Thumbnail | `thumbnails.set` | **VERIFIED:** Separate call; YouTube says custom thumbnails require channel verification/eligibility. YouTube Shorts do not offer the ordinary custom-thumbnail workflow in the same way; do not make it a core dependency. |
| Subtitles | `captions.insert` | **VERIFIED:** Possible via separate Captions API call and additional quota; MPT's existing generated subtitles are burned into the video and it currently sends no subtitle file through Upload-Post. Treat sidecar subtitle upload as optional scope. |

### Quota and platform limits

- **VERIFIED:** Current Google quota documentation lists a default project allocation of 100 `videos.insert` calls/day (a dedicated insert bucket; each call costs one unit) and 10,000 units/day for other methods combined. `videos.update` costs 50 units and `videos.list` one unit. The published quota model changed over time; use the current calculator/Cloud Console values, not older figures commonly quoted for uploads.
- **INFERRED:** The listed workloads (2, 4, and 10 inserts/day) are below the default 100-call/day project bucket if all channels use one project and no other upload clients consume that quota. The quota is shared at project level, not a free quota multiplied by each authorized channel. Do not shard projects to evade the cap.
- **VERIFIED:** YouTube separately imposes a channel daily upload limit across Studio, mobile, and API, which varies by region/channel history/eligibility and can be raised via creator feature eligibility. Google does not publish one fixed number appropriate for all channels. A channel may be blocked by strikes, account restrictions, or daily cap even when the API project has quota.
- **INFERRED:** For a few channels at 1–3 Shorts/channel/day, the documented API insert bucket is unlikely to be the first limit; account/channel daily limits and policy/quality operations may be. At larger fleet scale, a single shared project quota still needs monitoring and possibly Google's quota-extension process.

### Compliance and refresh-token lifecycle

**VERIFIED:** YouTube API client operators must follow the API Services Terms and Developer Policies. Google requires the upload client to obtain user certification that content complies with Community Guidelines; quota extension requests require an API Compliance Audit. The publisher should request only needed scopes (start with `youtube.upload`; add read scope only if needed to verify the authorized channel or reconcile listings—avoid broad `youtube.force-ssl` unless a specific operation requires it). OAuth tokens can be revoked, expire after six months without use, be invalidated by user/security action or token limits, or expire after seven days while an External OAuth app remains in Testing. Thus channel owners do not need to reauthorize on a fixed schedule for a production app, but they may need to reconnect after revocation, inactivity, security policy, or scope changes.

**UNVERIFIED:** Whether a specific deployment of MPT as a private personal tool qualifies for a particular OAuth verification exemption or needs a full verification review depends on Cloud project setup, audience, OAuth scopes, and current Google review decisions. Do not assume self-hosting automatically exempts the application. Check the live OAuth Console and official eligibility guidance for the actual app before deployment.

## 4. Architecture choices and operating cost

| Dimension | A. Keep Upload-Post | B. Replace with direct YouTube | C. Support both |
|---|---|---|---|
| Cost for 60/120/300 YouTube posts/mo | Free fails; Basic $24/mo supports up to five profiles, unlimited uploads per published plan. | No per-upload provider fee is documented by Google; Cloud/API project quota is subject to approval and terms. Engineering/operations cost remains. | Upload-Post optional cost; YouTube-only channels can use direct, multi-platform channels retain Upload-Post. |
| Initial implementation | Lowest; already integrated. | Highest; OAuth lifecycle, uploads, processing, scheduling, account UX and recovery new. | Highest overall; abstraction plus two adapters and migration rules. Can be staged. |
| OAuth maintenance | Upload-Post manages each social connection; MPT manages one Upload-Post API key. | MPT/operator stores one OAuth grant per channel and handles reconnect, refresh, revocation, and client verification. | Both systems' auth and support paths remain. |
| Account isolation | Upload-Post profile username is pinned in Autopilot job/profile. A provider profile can contain one account per platform. | Use OAuth grant ID + verified channel ID; must reject mismatched channel selection before publish. | Backend and destination must be pinned together per job. |
| Scheduling | Provider accepts future schedules; Autopilot stores planned time and provider schedule ID. | Native private upload + `status.publishAt`; MPT controls update/processing/reconciliation. | Both schedules operate with distinct receipt formats and failure modes. |
| Retry/recovery | Stable provider ID, idempotency key and provider status/history. Provider retries may also occur. | Resumable sessions recover bytes but `videos.insert` has no equivalent app-level idempotency key. An unknown terminal result must be reconciled before a new insert. | Publisher-neutral state plus provider-specific recovery metadata. |
| Reliability | Convenient managed queue/token handling; adds vendor/network dependency and status mapping. | Fewer intermediaries, but MPT owns OAuth, resumable session persistence, token refresh, upload queue and operations. | More code paths but reduces single-vendor dependency. |
| MPT compatibility | Existing jobs and receipts already match. | Requires additive schema/publisher behavior and careful pinning for old jobs. | Best compatibility if old records remain explicitly Upload-Post and new jobs pin a selected backend. |

**Cost comparison note:** For a YouTube-only operation, Upload-Post Basic's $24/month buys upload allowance and provider operation, while direct API avoids that provider charge but transfers implementation and maintenance to MPT. Google API quotas are not a promise of unlimited distribution. No YouTube API fee per video is shown in Google's published quota docs; exact future Cloud/API terms and quota allocations remain controlled by Google.

## 5. Autopilot data contracts and migration risks

### Existing checked-out contracts

- **VERIFIED:** Channel profiles are versioned JSON records in SQLite; current profile has `upload_post_username`, publishing policy, schedule and channel settings. Production jobs snapshot profile version/config, resolved generation values, planned slot, and generated artifact references. See [`app/models/channel_profile.py`](../../app/models/channel_profile.py#L11), [`app/models/autopilot.py`](../../app/models/autopilot.py#L1), and [`app/services/autopilot.py`](../../app/services/autopilot.py#L179).
- **VERIFIED:** `publications` is a durable SQLite ledger, unique by `job_id`, `idempotency_key`, and `provider_request_id`. It stores channel/profile version, Upload-Post `account_username`, provider request ID, state, provider video ID/URL, receipt JSON, failure details, attempt count, created/updated time, provider schedule ID, and next reconciliation time. It is additive-migrated at initialization. There is no publisher/backend column yet.
- **VERIFIED:** `publish_job` gates on generated state, approved/fresh QA, source-backed fact review for public releases, pinned Upload-Post username, active channel, and future schedule. It derives a stable request/idempotency ID from `job_id`, records an uploading row before outbound upload, passes the job as Upload-Post `external_id`, stores sanitized receipt and provider video/schedule identifiers, and reconciles unknown state using provider request status. A confirmed failure is needed before retry. See [`publish_job`](../../app/services/autopilot.py#L994), [`_safe_receipt`](../../app/services/autopilot.py#L99), and [`reconcile_publications`](../../app/services/autopilot.py#L722).
- **VERIFIED:** Receipt sanitization removes credential-like fields; currently the raw Upload-Post credentials are not stored in profile snapshots or publication receipt. The global API key remains in app config and is loaded at runtime.

### Direct YouTube migration hazards

- **INFERRED:** A direct adapter needs new backend-specific connection identity (OAuth grant reference, channel ID/title verification status), not a raw refresh token embedded in channel profiles or job JSON. Pin a stable connection ID and verified YouTube channel ID in immutable profile/job snapshot. At authorization time call `channels.list(mine=true)` and persist the returned channel identity; before upload, confirm the token and snapshot point to the expected channel to prevent cross-channel posting.
- **INFERRED:** Existing `account_username` is semantically Upload-Post-specific. Preserve it and all existing ledger values; add backend/provider identity and connection reference fields with safe defaults, and treat legacy rows as Upload-Post. Existing jobs must continue reconciling through Upload-Post even after an operator changes the current profile's backend.
- **INFERRED:** Direct upload receipt should preserve common fields (`state`, intended/publish time, video ID, URL, timestamps, error, attempt) and a sanitized provider-specific receipt, while also storing YouTube channel ID, resumable session reference/state, processing status, and last known upload response. OAuth secrets/session URLs should not be exposed in user-facing receipts or logs.
- **UNVERIFIED / critical:** A YouTube insert can finish server-side while MPT loses the response. Resumable transfer helps continue an interrupted transfer while its session URI remains valid, but it is not a documented exactly-once key for a fresh `videos.insert`. If the session outcome cannot be queried/recovered, an automatic new insert could duplicate. The safe state is `unknown`/blocked, followed by reconciliation against known session/status and, where necessary, operator inspection of the authorized channel's uploads. Matching title/time alone is not a proof of identity. Never claim exactly-once delivery.
- **INFERRED:** Existing job-level publication uniqueness is a useful local duplicate guard, but provider idempotency cannot be assumed equal across providers. Keep one publication record per job and pin backend/destination/session before external side effects; failed/unknown rows remain non-retriable until the adapter-specific reconciliation says safe to retry.

## 6. Security and local operations

**PROPOSED requirements for direct publishing:**

1. Encrypt OAuth refresh tokens at rest with a host-level key outside the SQLite database (for example, OS keyring or a file protected by strict permissions plus a key supplied outside the DB). Back up encrypted state and key separately. Do not put tokens in `config.toml`, channel profile JSON, logs, receipts, environment diagnostics, or job artifacts. MPT's existing SQLite database is on local storage and uses WAL; the current code does not implement encryption.
2. Request least scope (`youtube.upload` for upload; add read scope only for identity/reconciliation use), store scopes granted, and provide a clear connect/disconnect/revoke/reconnect path. Use OAuth state/CSRF protection and offline access; preserve a newly rotated refresh token if Google's token response includes one. Expect revocation/`invalid_grant` and pause only the affected channel.
3. On OAuth callback and immediately before publishing, retrieve the authorized channel (`mine=true`) and compare its channel ID with the profile snapshot. Never trust a user-entered channel label or select a destination based on currently active browser account.
4. Isolate secrets and database file permissions per local OS user; localhost OAuth callback should be bound narrowly. For remote UI use HTTPS and existing MPT authentication plus CSRF protections. Avoid sharing one token store among mutually untrusted MPT users; a single Ubuntu installation can safely host several channels only if connection records and operations are keyed by explicit channel ID and credentials are encrypted.
5. Track project insert quota and per-channel errors separately. Back off on quota/429/5xx, honor `Retry-After`, and do not treat an unknown result as a normal retry. On channel upload-limit failure, defer the same pinned job until eligible rather than creating a new publication/job.

**VERIFIED security boundary:** The current Upload-Post global API key is not copied into channel profile settings; profile bindings hold provider usernames, and publication receipts are sanitized. Direct integration must preserve this secret separation and improve on current unencrypted local config storage for long-lived OAuth tokens.

## 7. Recommended minimal safe implementation plan

**PROPOSED; no implementation performed.**

1. Freeze the current contract with representative schema fixtures/backup of `storage/autopilot/profiles.sqlite3`; classify existing publication rows as Upload-Post in the migration default. Do not rewrite receipts or regenerate jobs.
2. Add a publisher interface around `validate_destination`, `publish`, `get_status`, and `reconcile`; leave Upload-Post behavior and wire format unchanged behind its adapter. Add backend and immutable destination fields to new profile/job snapshots; old snapshots default to Upload-Post.
3. Add Google Cloud/OAuth setup and encrypted per-channel connection storage. Start with upload-only scope if identity verification can be done with that scope; otherwise add only the documented minimum read scope. Verify channel ID on connection and before publish; test revocation and token refresh with a private test channel.
4. Implement direct publisher using resumable `videos.insert`, metadata/status fields, response ID capture, `videos.list` processing checks, and private `status.publishAt` scheduling. Persist session/reconciliation state before sending media. Map `published`, `scheduled`, `failed`, and `unknown` into existing publication lifecycle without deleting provider-specific receipt details.
5. Add hard retry rules: stable MPT publication ID; never open a second insert while a previous session/result is uncertain; reconcile a saved session/status first; if no conclusive proof is available, require operator review. Ensure changing a channel's backend cannot retarget queued jobs or old receipts.
6. Roll out opt-in to one private/unlisted channel and low rate; validate processing, metadata/disclosure, schedule, reconnect, restart recovery, quota exhaustion, and cross-channel isolation. Then permit approved public scheduling only after compliance/legal and channel operator checks. Keep Upload-Post available for all existing rows and cross-platform channels.

## 8. Exact code areas likely involved

**PROPOSED future scope:**

- Publisher abstraction and adapters: new `app/services/publishers/` modules; refactor dispatch points in [`app/services/autopilot.py`](../../app/services/autopilot.py#L994) and ordinary immediate cross-post routing in [`app/services/task.py`](../../app/services/task.py#L1391) only if the regular task flow also needs backend selection.
- OAuth and secure connection store: new service and model module(s), API routes under `app/controllers/v1/`, and additive schema changes in the existing Autopilot/channel store. Avoid adding tokens to [`app/models/channel_profile.py`](../../app/models/channel_profile.py#L48); prefer a secret reference.
- Profile and immutable job snapshot: [`app/models/channel_profile.py`](../../app/models/channel_profile.py#L11), job snapshot creation/serialization in [`app/services/autopilot.py`](../../app/services/autopilot.py#L698), and Autopilot API/UI in [`app/controllers/v1/channels.py`](../../app/controllers/v1/channels.py) and [`webui/Main.py`](../../webui/Main.py#L8580).
- Publication persistence/recovery: publications DDL and additive migrations in [`app/services/autopilot.py`](../../app/services/autopilot.py#L283), plus `publish_job` and `reconcile_publications` in the same module. Add connection/backend/session columns without changing historical rows' provider identity or receipt JSON.
- Config and docs: [`config.example.toml`](../../config.example.toml#L581), WebUI setup/config section in [`webui/Main.py`](../../webui/Main.py#L3268), and [`docs/implementation/multi-channel-autopilot-operations.md`](../implementation/multi-channel-autopilot-operations.md).
- Tests/fixtures: existing Upload-Post, Autopilot, channel profile, WebUI config tests; add mocked OAuth/API tests and explicit legacy-ledger migration/reconciliation fixtures. No tests were run for this investigation-only report.

## 9. Final answers

1. **Why does MPT currently use Upload-Post?** **VERIFIED:** It has an existing integration for cross-platform publishing. **INFERRED:** A unified endpoint and provider-managed account connections are the likely architectural motivations. **UNVERIFIED:** This checkout contains no maintainer record confirming the historical decision.
2. **Is Upload-Post necessary for YouTube?** No. **VERIFIED:** YouTube Data API v3 supports the required upload, privacy, scheduling, metadata and disclosure operations. Upload-Post is a convenience/provider dependency, not a technical requirement.
3. **Is its free tier suitable?** No for the stated plan: 60/120/300 posts/month exceed 10/month. Two profiles cover two accounts but do not multiply the account quota. Five accounts require the Basic plan or higher under current plan tables.
4. **Is direct API publishing feasible?** Yes, technically. It needs per-channel OAuth and secure tokens, native resumable upload and reconciliation, Google project setup and compliance. Current official docs no longer say unverified API projects are private-only; they expressly state public viewing is allowed. This is not an exemption from OAuth verification, API compliance, project quota or channel upload limits.
5. **Best approach for multiple self-hosted YouTube channels?** **PROPOSED:** Option C, selectable backends, with direct YouTube as the economical YouTube-only route and Upload-Post retained for cross-platform use and legacy jobs. If short-term engineering effort dominates and $24/month is acceptable, Basic is a reasonable bridge for up to five channels.
6. **Effort and major risks?** **INFERRED:** Moderate-to-large feature, approximately 2–4 engineer-weeks for a robust single-host direct publisher and another 1–2 weeks to add/roll out a clean dual-provider UX, tests, and legacy-safe migration. This is a rough engineering estimate, not a verified schedule. Highest risks: OAuth publishing/verification friction, credential protection, wrong-channel isolation, silent/unknown upload outcomes causing duplicates, and changes in Google quota/policy.
7. **How to preserve current records?** Keep the SQLite publication row unique by job, add an immutable backend discriminator with legacy default `upload_post`, preserve `account_username`/request IDs/receipts, and route all old jobs and unknown-state reconciliations to Upload-Post. Do not transform an unknown historical publication into a direct API retry.

**Direct answer:** For a self-hosted MPT Autopilot managing several YouTube channels and publishing dozens or hundreds of Shorts each month, **support both** is the best product architecture, with **direct YouTube Data API as the default backend for YouTube-only publishing** and Upload-Post retained when cross-platform convenience is valuable or to service existing jobs. The free Upload-Post tier is too small; paid Basic is a low-effort bridge, not a YouTube necessity. Never silently retry a direct upload whose result is uncertain.

## Evidence links

### Upload-Post official

- [Pricing and plans](https://www.upload-post.com/social-media-scheduler/) — Free, profile counts, platforms, scheduling, monthly/annual paid plan prices and paid upload allowance.
- [Upload Video API](https://docs.upload-post.com/api/upload-video/) — parameters, idempotency, schedule, YouTube-specific fields, subtitle and disclosure options.
- [Manage Scheduled Posts](https://docs.upload-post.com/api/schedule-posts/) — schedule job management/status.
- [Upload History](https://docs.upload-post.com/api/upload-history/) — filter by profile, platform, request/job/external ID.
- [API Overview](https://docs.upload-post.com/api/overview/) — API access and free monthly upload claim.
- [Upload-Post FAQ](https://docs.upload-post.com/resources/faq/) and [Limits of Uploads](https://docs.upload-post.com/guides/limit-of-uploads/) — monthly API-call plan allowance, per-connected-account daily limits, scheduling enforcement, and a currently inconsistent YouTube daily cap.

### Google / YouTube official

- [YouTube Data API `videos.insert`](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert) — current unverified-project public upload clarification, OAuth scopes, quota and insert fields.
- [Video resource (`videos.update`, `videos.list`, `status.publishAt`)](https://developers.google.com/youtube/data/reference/rest/v3/videos) — scheduling semantics and mutable metadata.
- [Quota calculator](https://developers.google.com/youtube/data/determine_quota_cost) and [quota/compliance audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits) — project quota costs and audit process.
- [OAuth for YouTube Data API](https://developers.google.com/youtube/v3/guides/authentication) and [web-server OAuth](https://developers.google.com/youtube/data/guides/auth/server-side-web-apps) — per-user consent and offline refresh tokens.
- [Google OAuth 2.0 overview and refresh-token expiration](https://developers.google.com/identity/protocols/oauth2/) — seven-day Testing behavior and revocation/inactivity causes.
- [OAuth branding verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification) — external published app branding verification.
- [YouTube API Developer Policies guide](https://developers.google.com/youtube/terms/developer-policies-guide) and [API Terms of Service](https://developers.google.com/youtube/terms/api-services-terms-of-service) — API client obligations and compliance.
- [Upload videos help](https://support.google.com/youtube/answer/57407) and [creator feature eligibility](https://support.google.com/youtube/answer/9890437) — per-channel daily limits and feature-dependent higher limits.
