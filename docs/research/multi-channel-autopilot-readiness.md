# Multi-Channel Autopilot Readiness

Assessment date: 2026-10-23. This is a compatibility assessment; no scheduler or publishing changes are included.

## 1. Current generation entry points

- **API:** `POST /videos` accepts `TaskVideoRequest` (`VideoParams` plus API bounds), echoes submitted params in the immediate response, creates a minimal task-state record, and submits `task.start` through the API task manager. The full parameter set is in a Redis queue payload until dispatch when Redis queueing is enabled; after dispatch it is written to the script artifact. It is not a separately versioned profile/job snapshot. This is still the cleanest future profile-to-job boundary; a runner can construct the request model directly without a browser.
- **CLI:** regular generation builds `VideoParams` from CLI options. `--batch-file` validates each manifest task against `VideoParams` and then runs each task through the same `task.start` pipeline. The new `asmr_categories` parameter is part of that schema.
- **WebUI:** `webui/Main.py` builds `VideoParams` and submits through `app/services/webui_task.py`. Its task manager is in process and serializes WebUI generation under the runtime config lock.
- **Shared pipeline:** `task._run_pipeline` creates/reuses the script, terms, narration, subtitles, stock materials, and final video. Narration/render duration is based on measured audio.

## 2. Task state and queue persistence

- Task state uses Redis hashes when `enable_redis=true`; otherwise it uses `MemoryState`. Redis task fields are serialized individually and updated atomically. A Redis task record can survive process restart, while memory state cannot.
- API work uses `RedisTaskManager` when Redis is enabled and otherwise `InMemoryTaskManager`. Redis queue entries survive restart and queued work is resumed by the API manager; already-dispatched in-flight generation is not a scheduled-job lease with full recovery semantics. These are task execution queues, not a durable content calendar with due-time semantics.
- WebUI uses an in-process `InMemoryTaskManager` and bounded in-memory log buffers. Its queued/running work is not restart durable.
- Upload work uses a separate bounded in-process thread pool. On startup `recover_interrupted_cross_posts` marks orphaned `pending`/`processing` posts failed; it does not resume them. This deliberately avoids pretending an upload with unknown external outcome is safe to repeat.

## 3. Existing Upload-Post YouTube integration

`app/services/upload_post.py` calls Upload-Post `POST /api/upload` with an API key, profile `user` value, requested platforms, local video, privacy and made-for-kids settings. MPT does not call YouTube Data API directly. The settings in `config.example.toml` and WebUI are global: enabled, API key, profile username, platform list, auto-upload, privacy, and made-for-kids.

When a video-generation task completes and global auto-upload is enabled, `task._run_pipeline` snapshots the current upload account and YouTube privacy/audience settings and immediately submits a background cross-post job. This behavior is not schedule-aware. A future scheduled job must carry an explicit publishing policy and must not inherit this global immediate-upload trigger accidentally.

The integration preserves the Upload-Post request ID while polling async status, stores per-video results in `cross_post_results`, and retains the provider result payload (including any IDs/URLs the provider returns). Current behavior does not normalize or require a YouTube video ID, reconcile against upload history, or guarantee those receipts survive when task state is memory-only. A generated UUID `request_id` is sent per submission; an uncertain response is surfaced for manual lookup, but a re-execution creates a fresh ID and MPT has no durable dedupe key spanning retries.

### Verified Upload-Post capability vs MPT support

| Capability | Upload-Post contract | Current MPT behavior |
|---|---|---|
| Multiple connected YouTube accounts | Profiles identify a `user`; each profile can have connected social accounts. | One global `upload_post_username` is configured. No channel/profile registry or account binding exists. |
| Select intended account per job | Upload endpoint requires the `user` profile value. | The service can accept an account snapshot internally, but generation jobs cannot supply a validated channel-to-profile binding; all queued jobs snapshot the same global settings. |
| Private/unlisted upload | YouTube `privacyStatus` accepts provider-supported privacy values; MPT config exposes public, unlisted, and private. | Supported for the global configured target and snapshot, without per-channel policy. |
| Scheduled publication | Upload-Post documents `scheduled_date`/timezone and YouTube `youtube_publish_at` (which forces private) plus schedule job IDs. | MPT sends neither scheduled field and does not store schedule job IDs. Its auto-upload is immediate. |
| Token refresh/reconnection | Upload-Post profile APIs expose connection state and `reauth_required`; account connections are managed by Upload-Post. | MPT has no profile discovery, reauthorization, or reconnection workflow. Provider-side refresh behavior is not implemented or directly verified by MPT. |
| Durable result/video ID tracking | Status/history APIs expose request/job IDs, profile username, platform post IDs and URLs. | MPT stores provider result JSON in task state and a request ID before polling. It has no durable independent publication ledger or reconciliation worker. |
| Idempotent retry | Current Upload-Post API docs describe client request/idempotency IDs to return an existing job on duplicate submissions. | MPT sends a UUID but does not persist a stable publication operation key before all side effects or reuse it on retry. Retrying can create a new provider operation. |
| Concurrent account isolation | Provider routes by profile `user`; MPT uses an account-specific service instance for a queued upload snapshot. | Current global settings are snapshotted at enqueue to avoid later config edits changing an upload. There is no authorization check tying a task to a chosen profile, nor an explicit multi-profile concurrency test/contract. |

References: [Upload Video API](https://docs.upload-post.com/api/upload-video), [User Profiles API](https://docs.upload-post.com/api/user-profiles/), [Upload Status](https://docs.upload-post.com/api/upload-status/), [Upload History](https://docs.upload-post.com/api/upload-history/), and [Schedule Posts](https://docs.upload-post.com/api/schedule-posts/). These are provider-documented contracts, not claims that MPT currently implements them.

## 4. Channel profile fields needed

Persist a stable internal channel ID and enabled/paused state; display name; niche and topic policy; language, script/story style, duration target, aspect ratio and voice; subtitle and BGM settings; stock source/style and explicit ASMR category priority; full generation/provider parameters; timezone and schedule; publishing policy (`manual approval`, `private`, `unlisted`, scheduled public, or immediate public); Upload-Post profile username and permitted platforms; credential reference/secret-manager key rather than raw credentials; duplicate/history policy; QA gates; and retention/analytics settings. The profile must be immutable or versioned per job so an edit cannot change in-flight work.

## 5. Future schedule, job, and publication state

Use separate durable records for:

1. **ChannelProfile / ProfileVersion:** configuration, destination binding, schedule and policy.
2. **ContentPlan:** planned topic, source/evidence references, dedupe key, intended slot and approval state.
3. **ProductionJob:** immutable profile version and full `VideoParams` snapshot; scheduled/due time, attempt number, lease/owner, task ID, artifact references, stage/status/error and cancellation state.
4. **QAApproval:** checks/results, reviewer, decision, timestamp, and approved artifact hash/version.
5. **Publication:** stable idempotency key, target profile/platform, provider request/job ID, status, attempts, timestamps, provider video/post ID, URL, privacy, scheduled/published time, and last error.

Transitions should be explicit (planned → queued → generating → QA pending → approved/rejected → upload queued → scheduled/published or failed). A task must not transition to publishable without the channel binding and required approval/policy.

## 6. Concurrency, restart recovery, idempotency

- Lease a due job atomically so two scheduler processes cannot produce or publish the same slot.
- Persist the immutable profile version, prompt/terms, provider account binding, and idempotency key before external side effects.
- Use a transactional outbox or equivalent durable handoff from completed generation/QA to publication.
- On restart, recover expired generation leases and query Upload-Post by the saved request/job/external ID before deciding whether to resubmit. Never blindly retry an unknown upload outcome with a new key.
- Serialize or rate-limit per provider account as required; keep credentials scoped to the job's selected profile and do not put raw secrets into task params, logs, artifacts, caches, or reports.
- Enforce channel/account authorization at job creation and again immediately before publishing. Account/profile edits must not retarget already queued jobs.
- Keep generation completion independent from publishing failure, as current MPT does, while exposing separate terminal states and operator actions.

## 7. Recommended milestones

1. **Profile and routing foundation:** durable versioned channel profiles, provider profile discovery/connection checks, secret references, per-job account binding, and authorization tests.
2. **Content planning and duplicate prevention:** topic/evidence planning, history/dedupe keys, slot reservations and configurable channel policies.
3. **Durable scheduler and queue:** timezone-aware recurrence, durable leases, retries/backoff, cancellation, concurrency limits and restart recovery.
4. **QA and approval:** artifact validation, human approval/rejection, approval tied to exact artifact hash and profile version.
5. **Publication ledger:** Upload-Post scheduled/immediate policies, privacy handling, stable idempotency/external IDs, durable receipt reconciliation, video IDs/URLs, reconnect state and duplicate protection.
6. **Operations and analytics:** per-channel dashboard, status/error handling, retries/manual recovery, publication history and analytics.

This report does not implement any of these milestones. The present ASMR corrections only make per-task footage category settings explicit and independent of narration; they are not a persistent channel-profile system.
