# Multi-Channel Autopilot Phase 2A

## Architecture

Phase 2A adds persistent per-channel content plans and manually started production jobs on top of the Phase 1 SQLite profile database. It uses MPT's existing `llm.generate_script` and `task.start` paths. It does not add a scheduler or a publisher.

- A plan is built against the current immutable profile version. The user can provide a topic and script, or leave the script blank to draft it through the configured MPT text provider.
- Hindi ASMR script drafts include the requested Hook → Interesting Fact → Explanation → Payoff structure. The material pipeline continues to receive ASMR categories from the profile, independently of narration text.
- Source references are stored as user supplied evidence. Drafts begin as `unverified` or `evidence_supplied_unverified`. An operator may mark a plan reviewed only when it has at least one source reference; this records an operator assertion and is not an automated fact check.
- The duration planner estimates spoken time using a coarse language word-rate estimate and the selected voice rate. By default the accepted range is 90–110% of the profile target. A user can supply a narrower range, such as 25–30 seconds, and must explicitly accept an out-of-range estimate before marking that plan ready.
- Creating a job copies the profile version, Upload-Post username identifier, full profile snapshot, resolved/validated `TaskVideoRequest`, topic, script, evidence state, duration bounds, and optional intended publication slot. Profile edits after that point do not retarget or rewrite the job.
- Starting a ready job inserts a durable `queued` state, then hands the work to a bounded in-process worker pool. The worker atomically claims `queued → generating`, persists the MPT task ID and attempt count, and calls the normal `task.start` pipeline with `suppress_auto_upload=True`. Script drafting and generation hold MPT's existing runtime configuration lock; this keeps process-wide provider and voice settings stable during a call and serializes pipeline access within a process.
- Successful output files are probed once for actual duration. Out-of-range renders become failed jobs with artifacts retained and an explanation; there is no automatic rerender loop.
- Failed jobs can be explicitly retried using the pinned snapshot. Queued jobs persist across restart and can be manually resumed. A new service instance marks a generating job failed only when its saved local worker PID is no longer alive. It never automatically repeats an interrupted generation request.

MPT's existing manual generation path and default immediate auto-upload behavior remain unchanged. Autopilot generation bypasses the pipeline's global Upload-Post auto-upload branch; account identifiers are retained only in the immutable job profile snapshot for later publishing work.

## Persistence and schema

Phase 2A reuses `storage/autopilot/profiles.sqlite3`, the Phase 1 SQLite file, in WAL mode. The schema is extended additively with:

### `content_plans`

Stores `plan_id`, `channel_id`, pinned `profile_version` and profile snapshot, topic/script and normalized SHA-256 fingerprints, source references, factual-review state, estimated duration and accepted bounds, optional intended publication slot, content state, cross-channel duplicate policy, and timestamps. A unique `(channel_id, topic_fingerprint)` constraint reserves exact topics for the channel's full recorded history. A unique `(channel_id, script_fingerprint)` constraint blocks exact script reuse.

Each versioned channel profile has a default cross-channel duplicate-check policy, and an individual content-plan request can override it. Near duplicate checks use normalized Unicode text and a bounded recent-history scan: 500 recent plans for the channel, or 1,000 recent plans across channels when cross-channel checking is enabled. Topic similarity uses `SequenceMatcher`; longer scripts use word-trigram Jaccard similarity. The scan is deliberately bounded, so near duplicates older than the window may not be detected. Exact per-channel fingerprints remain protected by database constraints regardless of age.

### `production_jobs`

Stores `job_id`, unique `plan_id`, `channel_id`, pinned profile version and immutable profile snapshot, serialized `TaskVideoRequest`, topic/script/source references, factual-review state, optional intended slot, target and accepted duration bounds, estimate and actual duration, lifecycle state, failure detail, attempt count, latest MPT task ID, output artifact references, worker owner, and timestamps. Indexes support per-channel history and queue-state lookups.

Tables and indexes are created with idempotent `CREATE TABLE/INDEX IF NOT EXISTS`. A `worker_owner` column is added with `ALTER TABLE` when an earlier Phase 2A database lacks it. No new database or service dependency is introduced.

## Job lifecycle

```text
content plan: planned → ready → job_created
production job: ready → queued → generating → generated
                                      ↘ failed → ready → queued (explicit retry)
                         queued → cancelled
```

Transitions are checked and written inside SQLite `BEGIN IMMEDIATE` transactions. A plan can create only one job. Concurrent plan submissions serialize their reservation check and insert; concurrent job creation can consume a ready plan only once. A queued job can be cancelled before the worker claims it. The current MPT pipeline has no cooperative interruption interface, so a generating job cannot be cancelled safely.

On restart, queued jobs remain durable and are offered through a manual **Resume queued job** action. Dead local generation workers are marked failed with an inspect-before-retry message. An unknown remote host cannot be checked from the local SQLite process and remains untouched.

## API

All routes use the existing V1 API key dependency. Existing local behavior remains: when the global API key is unset, routes are unauthenticated.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/channels/{channel_id}/content-plans` | Save a topic and supplied script, or draft the script through MPT's configured text provider |
| `GET` | `/api/v1/channels/{channel_id}/content-plans` | List that channel's content history |
| `POST` | `/api/v1/channels/{channel_id}/content-plans/{plan_id}/ready` | Mark reviewed/ready; out-of-range estimates require explicit acceptance |
| `POST` | `/api/v1/channels/{channel_id}/production-jobs` | Create a ready job from a ready plan and return its parameter preview |
| `GET` | `/api/v1/production-jobs?channel_id=...` | List jobs, optionally filtered by channel |
| `GET` | `/api/v1/production-jobs/{job_id}` | Read job state and persisted outputs/failure |
| `POST` | `/api/v1/production-jobs/{job_id}/start` | Manually enqueue a ready job or resume a persisted queued job |
| `POST` | `/api/v1/production-jobs/{job_id}/retry` | Explicitly retry a failed job with its pinned snapshot |
| `POST` | `/api/v1/production-jobs/{job_id}/cancel` | Cancel a queued job before it is claimed |
| `POST` | `/api/v1/production-jobs/{job_id}/recover` | Mark a dead local generating worker failed; refuses active or unverifiable workers |

Duplicate content returns `409`, invalid state transitions return `409`, invalid plans return `422`, and a full durable queue returns `429`. Provider generation failures are persisted without logging raw exception text; URLs and configured secret values are redacted before job failure details are stored.

## WebUI

The separate **Channel Autopilot** workspace now shows content plans and script previews per selected profile, evidence references and review state, duration estimates and requested bounds, and production-job state, attempts, MPT task ID, failure details, actual duration, and generated artifacts. Operators can mark a plan ready, create a job, start/resume queued work, retry a failed pinned job, cancel queued work, and check for an interrupted local worker. The manual Video Generator remains on its existing path.

## Tests and validation

`test/services/test_autopilot.py` covers two independently configured channels and ASMR category/account snapshots, profile-version pinning, exact and near deduplication, optional cross-channel checks, concurrent topic reservation and single-use plan-to-job creation, persistence/reopen behavior, state transitions, Hindi script prompt construction through the configured MPT LLM abstraction, duration planning and post-render validation, artifact retention, manual retry, dead-worker recovery, and failure redaction. `test/services/test_task.py` adds a regression test proving Autopilot bypasses global immediate Upload-Post auto-upload.

Tests use mocked LLM, MPT task runner, state backend and media-duration probe. They do not call paid providers or publish videos. The focused run on 2026-10-23 passed with 244 tests, 3 skipped, 1 deselected, and 10,378 subtests; the deselected cross-post state test performs unmocked social-metadata generation. Ruff and `git diff --check` passed.

## Limitations and next requirements

- Topic selection is currently operator supplied. Script drafting is optional; source discovery, fact checking, claim extraction, and citation verification are not implemented. An operator-reviewed state is an explicit user assertion only.
- MPT's provider registry/configuration remains global during a production run. Profile provider identifier/options are stored as profile metadata but do not select independent provider credentials or runtime settings. The runtime configuration lock serializes same-process generation; it does not provide a distributed configuration snapshot across MPT hosts.
- The spoken-duration estimate is approximate. Final output is checked after rendering, and a mismatch leaves the job failed with the artifact available for review.
- SQLite queue operations target one MPT host with a persistent local volume. Dead-worker detection uses host and PID; remote host recovery, PID reuse fencing, and distributed leases are not provided.
- A queued job must be manually resumed after restart. In-flight generation is marked failed after its local process exits and must be inspected before retry. MPT's own task state may remain memory-only when Redis is disabled, while the Autopilot job record remains durable.
- The current pipeline cannot stop a running FFmpeg/provider stage cooperatively. Cancellation is limited to queued work.
- A `local` video source still needs per-job local material references; profile planning does not yet capture or upload those inputs.
- No publication state or side effect exists in this phase. YouTube account routing, private upload, publication scheduling, idempotency, receipt reconciliation, video IDs, and duplicate-safe publish retries remain future work.

The next milestone must add a timezone-aware scheduler, explicit due-time policy, atomic cross-process job leases, content planning automation/history rules, QA gates, and a separate publication ledger/dispatcher that uses the pinned account only after the configured approval policy is satisfied. Scheduled jobs must never flow through MPT's global immediate auto-upload trigger.
