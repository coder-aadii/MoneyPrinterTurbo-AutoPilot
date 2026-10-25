# Multi-Channel Autopilot Phase 1

## Delivered

Phase 1 adds persistent channel configuration and verified Upload-Post YouTube profile binding. It does not create generation tasks, schedule work, upload videos, or publish videos.

- Profiles are stored in `storage/autopilot/profiles.sqlite3` using SQLite and WAL mode. Each profile has a stable UUID and immutable JSON snapshots keyed by monotonically increasing version.
- Updates require the expected current version. Concurrent stale updates return a conflict instead of overwriting newer configuration. Disabling is a versioned edit.
- Profile fields include niche/topics, language and script style, target duration, aspect ratio, stock style and ordered ASMR categories, video source, voice and subtitle settings, BGM, provider identifier and non-secret options, timezone, planned weekly frequency, publishing policy, enabled state, and Upload-Post username.
- Provider credentials are not fields in the profile schema. Credential-like keys are rejected from provider options. Upload-Post's API key is read only from existing global MPT configuration and is never returned in profile responses.
- `build_preview` deterministically maps profile-owned settings to a validated `TaskVideoRequest`. Job-specific subject and script are retained. Target duration is included in the script guidance and preview metadata; current MPT rendering does not enforce an exact profile duration.
- `GET /api/v1/channels/upload-post/profiles` reads Upload-Post's authenticated profile endpoint and returns sanitized YouTube connection state. Creating or changing a profile binding rechecks that the exact provider username exists, has a YouTube account, and does not require reauthorization. An unavailable provider check fails closed.
- The WebUI has a separate **Channel Autopilot** workspace for listing, creating, editing, enabling/disabling profiles, showing account status, and previewing resolved generation settings. It does not touch the manual generation submission path.

## API

All endpoints use MPT's existing API key dependency. As with existing local endpoints, authentication remains disabled when the global `api_key` is unset.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/channels` | List current profile versions |
| `POST` | `/api/v1/channels` | Create a profile (201) |
| `GET` | `/api/v1/channels/{channel_id}?version=N` | Read current or immutable prior version |
| `PUT` | `/api/v1/channels/{channel_id}` | Save a full profile with `expected_version` |
| `POST` | `/api/v1/channels/{channel_id}/disable?expected_version=N` | Create a disabled version; the expected version is required |
| `POST` | `/api/v1/channels/preview` | Resolve `channel_id`, `video_subject`, and `video_script` to a validated request snapshot |
| `GET` | `/api/v1/channels/upload-post/profiles` | Discover provider profile names and sanitized YouTube connection status |

Profile writes with an Upload-Post binding require the configured Upload-Post API key and a successful provider verification. Invalid, disconnected, or reauthorization-required accounts are rejected. Profile reads and previews do not contact Upload-Post.

## Example independent profiles

```json
{
  "display_name": "Hindi ASMR Facts",
  "niche": "Verified surprising facts",
  "topic_preferences": ["science", "history", "nature"],
  "language": "hi",
  "script_style": "Concise, conversational Hindi narration",
  "target_duration_seconds": 28,
  "video_aspect": "9:16",
  "stock_video_style": "satisfying_asmr_cutting",
  "asmr_categories": ["kinetic_sand", "soap"],
  "upload_post_username": "channel-a"
}
```

```json
{
  "display_name": "Science Facts",
  "niche": "English science and space facts",
  "topic_preferences": ["astronomy", "physics"],
  "language": "en",
  "script_style": "Clear, evidence-led explanation",
  "target_duration_seconds": 55,
  "video_aspect": "16:9",
  "stock_video_style": "default",
  "asmr_categories": ["foam", "jelly"],
  "upload_post_username": "channel-b"
}
```

The examples bind distinct Upload-Post usernames. The service verifies each binding against its own global Upload-Post API key and stores only the provider usernames. It does not store the API key or YouTube tokens.

## Upload-Post verification boundary

MPT's existing uploader uses `POST /api/upload` with a `user` profile name, but its existing generation integration still uses the global account snapshot for immediate auto-upload. This phase adds only read-only profile discovery via `GET /api/uploadposts/users`; account routing has **not** been connected to the publisher. Upload-Post's profile endpoint is documented to expose social account state and reauthorization status. Provider-side YouTube token refresh is managed outside MPT and was not exercised here.

References: [Upload-Post User Profiles API](https://docs.upload-post.com/api/user-profiles/) and [Upload Video API](https://docs.upload-post.com/api/upload-video/). No live provider request was made for this implementation; automated tests mock the provider response. Deployments must configure Upload-Post before binding a provider username.

## Persistence and compatibility limits

- SQLite profile storage persists across application restarts when the same MPT storage directory is retained. It is appropriate for a single MPT deployment using a local persistent volume. Multi-host shared database locking/backup behavior is not established.
- A profile version can be retrieved later, but this phase does not create production jobs or persist a job's profile version and resolved parameters. Phase 2 must snapshot both at job creation before work is queued.
- Target duration informs generation guidance and the preview. The present rendering engine sizes output from generated narration and does not enforce a strict profile duration.
- The profile provider identifier/options are configuration metadata. They do not switch MPT's global AI credentials or provider instances; the current `VideoParams` has no per-channel provider credential contract.
- MPT API authentication follows the existing setting: if no API key is configured, local endpoints are unauthenticated. Deployments exposing the API must configure the global key and protect the network boundary.

## Tests and validation

`test/services/test_channel_profiles.py` covers two separate profiles, persistence and historical versions, schema validation, optimistic concurrent updates, profile-to-request mapping, job topic/script precedence, ASMR category isolation, Upload-Post account isolation, disconnected and reauthorization-required accounts, response sanitization, credential-key rejection, and API authentication dependency behavior. Provider calls are mocked; no uploads or paid AI providers are invoked.

Validation on 2026-10-23: the channel profile suite passed (7 tests). The focused ASMR, CLI, LLM controller, and task regression run passed with 206 tests, 3 skipped, 57 subtests, and one test deselected. A first combined run included `test_cross_post_state_update_retries_transient_backend_failure`, which unexpectedly attempted social-metadata provider access because that test does not mock metadata generation; the resulting fallback changed the mocked sleep call count and the test failed. No production code in this phase changes cross-post behavior. Ruff and `git diff --check` passed.

Run with:

```sh
UV_CACHE_DIR=/tmp/mpt-uv-cache uv run python -X utf8 -m pytest -q test/services/test_channel_profiles.py
```

## Later milestones

Phase 2A adds persisted content plans, duplicate prevention, immutable production-job snapshots, a durable manual-start queue, and generation through the existing pipeline. See [autopilot-phase-2a.md](autopilot-phase-2a.md).

The remaining work is:

1. A timezone-aware scheduler with atomic cross-process leases, due-time recurrence, backoff, cancellation, concurrency limits, and recovery.
2. Automated topic research, evidence collection, factual verification, and content history rules beyond operator-supplied topics and references.
3. QA checks and human approval tied to the exact generated artifact and profile version.
4. A publication ledger and publisher entry point that uses the job's pinned Upload-Post username independently of global immediate auto-upload settings.
5. Publication policy enforcement for manual, private, unlisted, scheduled, and public release; durable provider receipts, video IDs/URLs, reconciliation, and duplicate-safe retry behavior.
6. Per-channel operational analytics, publication history, and administration.

Channel profiles and Phase 2A jobs provide the configuration and manual production foundation. They do not enable scheduling or publishing.
