# AGENTS.md — MoneyPrinterTurbo

## Mission

Make the smallest correct, tested change while minimizing unnecessary context, tool calls, and output. Prefer verified repository behavior over assumptions.

## Top-priority rule — prove the user-facing path

For every user-visible feature or bug fix, identify each affected entry point
(WebUI, API, CLI, scheduler, or other applicable interface) and trace the complete
path from the control or request through submitted settings, persisted task/job
state, and the code that consumes that state. Add a focused regression test that
exercises each affected interface and verifies the resulting saved state. Service
tests alone do not prove a UI feature works. Do not call the work complete until
these paths are verified; clearly report any interface or persistence behavior
that could not be tested.

## Repository map

MoneyPrinterTurbo is a Python 3.11+ AI short-video generator.

| Area                | Location                                           |
| ------------------- | -------------------------------------------------- |
| API                 | `app/asgi.py`, `app/router.py`, `app/controllers/` |
| Business logic      | `app/services/`                                    |
| Data models         | `app/models/`                                      |
| Configuration       | `app/config/`, `config.example.toml`               |
| Streamlit UI        | `webui/`                                           |
| CLI                 | `cli.py`, `app/launcher.py`                        |
| Tests               | `test/`                                            |
| Generated artifacts | `storage/`                                         |
| Dependencies        | `pyproject.toml`, `uv.lock`                        |

Pipeline:
topic → AI script → optional fact verification → Hindi/Hinglish
normalization → TTS → word alignment → emotion storyboard →
stock/original visuals → FFmpeg render → QA → YouTube publishing.

Autopilot:
persistent schedule → queued generation → validated video buffer →
private upload → scheduled publication → reconciliation.

Visual styles: AUTO, Savage Animated Explainer,
Relevant Stock, ASMR Cutting.

Optional Redis task queue. No StoryWeaver/Remotion/Next.js architecture here.

## Context efficiency — mandatory

1. Start with `git status --short` and identify the smallest relevant subsystem.
2. Search filenames/symbols first using `rg --files` and `rg -n`; exclude `.git`, `storage`, caches and generated artifacts.
3. Read only relevant functions, nearby context and focused tests. Expand to callers only when necessary.
4. Do not read the entire repository, all documentation or unrelated tests upfront.
5. Check existing helpers, tests and conventions before introducing new abstractions.
6. Do not repeatedly read unchanged files or repeat failed commands without a new hypothesis.
7. Use repository-relative paths. Consult docs only when needed:
   - `README-en.md` — setup and workflows
   - `test/README.md` — test instructions
   - `docs/video-projects.md` — video projects
   - `docs/packaging.md` — distribution
8. Keep explanations proportional to task complexity. Avoid narrating routine tool calls.

Optimize total tokens and correctness, not just initial context size.

## Token and execution efficiency — mandatory

Optimize for minimum total tokens while maintaining correctness. Avoid reducing essential verification just to save tokens.

### Task complexity

Choose the smallest appropriate investigation scope:

- **Trivial:** Single known file or configuration. Inspect the target, change if needed, verify locally.
- **Focused:** One subsystem. Trace the relevant functions and tests.
- **Complex:** Multiple interacting subsystems. Trace dependencies and affected interfaces before modifying.
- **Investigation-only:** Gather sufficient evidence, report findings and stop. Do not implement.

Do not perform repository-wide analysis unless the task requires it.

### Tool efficiency

- Batch related filename/symbol searches when useful.
- Restrict search scope before expanding.
- Limit command output to relevant matches and failures.
- Avoid printing entire files, test logs, dependency trees or generated artifacts.
- Use targeted `sed`, `rg` and `git diff` output.
- Prefer focused tests; expand only when changes affect shared interfaces or failures indicate broader risk.
- Do not repeatedly run successful checks unless changes invalidate their results.
- Avoid unnecessary network requests, dependency installations and provider calls.
- Stop investigating when sufficient evidence supports a safe implementation or conclusion.
- If a command fails, inspect the cause before retrying.

### Reasoning efficiency

- Reuse established findings within the task.
- Do not repeatedly summarize previously inspected code.
- Avoid speculative alternatives when an existing repository convention provides a suitable solution.
- Do not generate extensive implementation plans for straightforward changes.
- Ask for clarification only when ambiguity materially affects correctness or safety.

### Output efficiency

Default to concise final responses.

- Trivial change: 1–3 lines.
- Focused implementation: 3–6 lines.
- Complex change: Brief summary, verification and remaining risks.
- Investigation: Findings, evidence and recommendation.

Do not include routine narration, lengthy introductions, repeated context or full command output.

Report failures and limitations explicitly. Never omit significant risks or unverified claims merely to satisfy a length target.

If detailed documentation or a report was explicitly requested, write the full content to the requested file and keep the chat response short.

## Task execution

**Investigation:** Read-only unless explicitly authorized. Trace actual behavior, identify root cause, provide evidence and a concise recommendation. Do not implement speculative changes.

**Implementation:** Trace relevant caller → service → output → test. Make the smallest compatible change. Preserve APIs, config names, artifact formats, task states, queue behavior and retries. No unrelated refactoring.

**Bug fix:** Reproduce where practical, fix the root cause, add a regression test, run focused verification.

**Uncertainty:** Verify material assumptions in code. Do not describe mocked or untested paths as live-working.

## Engineering rules

- Controllers: request handling; services: workflows/providers; models: shared data; config: settings.
- Reuse provider abstractions and existing helpers; avoid duplicate logic.
- Preserve offline/test fallbacks without silently using mocks in production.
- Keep `webui/`, API and CLI behavior compatible where shared services change.
- Synchronize configuration templates, packaging and dependency files when relevant.
- Treat `config.toml`, secrets, Redis data and `storage/` as user data. Do not expose, reset or delete.
- No live paid-provider requests without explicit authorization.
- No new dependencies unless justified by the task.
- Never stage, commit, push or reset user changes without explicit permission.
- Never solve a failing pipeline by repeatedly adding new validation, prompts or dependencies without proving the root cause. Prefer recovering existing artifacts and fixing the smallest demonstrated defect.

## Production correctness — mandatory

The primary project goal is reliable, unattended generation and scheduled YouTube publication of original Hindi Shorts.

Prioritize correctness, recoverability, security and user-visible functionality over adding features.

### Critical workflow invariants

- A user-selected setting must reach every relevant downstream consumer. Verify UI → API/service → saved task → provider → renderer, not merely UI persistence.
- Visual Style is a complete production preset. Preserve resolved settings and explicit overrides consistently across manual generation and Autopilot.
- Fact verification is user-configurable: Off, Advisory or Strict. Never silently upgrade Advisory/Off to Strict or label unchecked claims verified.
- Flexible duration accepts actual narration duration without truncating, stretching or unnecessary regeneration.
- Preserve the canonical spoken script, display script and word mapping across Hindi/Hinglish TTS, alignment and captions.
- Do not fabricate subtitle timestamps, silently drop spoken words, or represent uncertain alignment as verified.
- For Savage narration, direct sarcasm/profanity at jargon, misconceptions or absurd situations, not the viewer.
- For animated explainers, use narration-linked character reactions and meaningful stock/original visuals; avoid random pose changes or repeated generic diagrams.
- Reuse intermediate artifacts only when their content fingerprints and relevant settings match.
- Never allow production code to silently substitute mocks or dummy provider responses.
- Never solve a failing pipeline by repeatedly adding new validation, prompts or dependencies without proving the root cause. Prefer recovering existing artifacts and fixing the smallest demonstrated defect.

### Autopilot and YouTube reliability

- Scheduled jobs must be persistent, restart-safe and idempotent.
- Do not create duplicate jobs, videos or public uploads for the same scheduled slot.
- Resume safe checkpoints and reconcile uncertain external-provider outcomes before retrying.
- Apply bounded retries, backoff, rate limits and configured provider budgets.
- Do not repeat potentially billable operations merely because a response was lost.
- Prepare videos ahead of publication slots; don't assume generation completes instantly.
- Use actual media duration and verify final MP4/audio integrity.
- Validate the destination YouTube channel and selected publishing policy.
- Never silently publish to a different channel or change Private to Public.
- A failed or missed slot must be recorded accurately; never report success without remote confirmation.
- Respect existing stock-media provenance, license and approval requirements.
- Never bypass security, asset or publication safeguards simply to make a scheduled job complete.

### Security boundaries

Treat user input, generated scripts, provider responses, retrieved web pages, uploads, file paths and task metadata as untrusted.

- Enforce authentication and authorization for every protected API and media route.
- Validate file paths and prevent traversal outside intended storage roots.
- Do not execute LLM/provider output as shell commands or executable code.
- Protect against SSRF in remote URL/page/media fetching.
- Keep OAuth credentials, API keys and encryption keys out of logs, commits and generated reports.
- Use least-privilege permissions for credentials and external integrations.
- Validate uploaded files and avoid trusting filenames, MIME types or provider metadata alone.
- Avoid introducing unsafe deserialization or command injection through FFmpeg and subprocess arguments.
- Use established project security patterns rather than inventing new authentication mechanisms.
- Do not remove working authorization, validation or rate-limiting checks to resolve unrelated errors.

### Verification standards

For every nontrivial bug fix:

1. Reproduce the bug or establish a failing test where practical.
2. Identify the root cause and affected callers.
3. Implement the smallest correct fix.
4. Add a focused regression test.
5. Run the relevant tests and static checks.
6. Verify the affected user-visible workflow where feasible.

For changes spanning multiple pipeline stages, include an integration test covering the actual data flow.

Mocked tests prove local behavior only. Do not claim live TTS, YouTube publishing, browser interaction, rendered video quality or external-provider availability without testing them.

Browser UI changes require checking the actual rendered UI, including relevant mobile layouts.

Video changes require inspecting representative rendered frames and media metadata; audio/synchronization claims require corresponding playback or alignment evidence.

Distinguish confirmed defects, plausible risks and environment limitations.

### Safe releases

- Develop changes on focused branches where practical; keep `main` suitable for tested releases.
- Preserve user changes, local credentials, databases and generated task artifacts.
- Inspect the diff before staging or committing.
- Do not commit, merge, push, deploy or publish unless explicitly authorized.
- Never force-push or rewrite shared history without explicit authorization.
- Before a production release, confirm critical tests, configuration compatibility, secrets handling and rollback readiness.
- Prefer reverting a faulty release over making uncontrolled production hotfixes.

### Stop conditions

Stop and report when:

- A change could delete or corrupt user data.
- An operation would require an unapproved billable provider call.
- A proposed retry could duplicate a YouTube upload or another irreversible external action.
- A security-sensitive operation cannot be validated safely.
- The task requires a decision about public publishing, credentials or destructive migration.

Do not stop routine local development merely because optional research, creative-quality checks or other advisory features report warnings.

### Reporting

For completed changes, report only:

- What changed.
- What actually passed verification.
- What remains untested or blocked.

Never equate passing unit tests with end-to-end production readiness.

## Verification

Use `uv` and locked dependencies.

Focused tests:
```
uv run python -X utf8 -m pytest -q test/services/<matching_file>.py
```

Full suite, only when warranted:
```
uv run python -X utf8 -m pytest -q test
```

Lint:
```
uv run --no-sync ruff check app cli.py main.py webui test docs/skill
```

- Run the smallest relevant tests first; expand based on risk and affected interfaces.
- Redis write tests require isolated test Redis (CI uses DB 15).
- Native rendering tests require FFmpeg.
- Consult `.github/workflows/ci.yml` for broader CI commands.
- Do not modify production data to run tests.
- Check `git diff --check`, `git diff` and `git status --short` before finishing.

## Response format

For simple tasks, answer briefly.

For investigations:

- **Finding:** Root cause or relevant behavior.
- **Evidence:** Exact paths/functions.
- **Recommendation:** Minimal next action.
- **Uncertainty:** Only meaningful unverified claims.

For implementations:

- **Changed:** Files and behavior.
- **Verified:** Tests actually executed.
- **Remaining:** Failures or blockers, if any.

Avoid lengthy summaries, repeated explanations and unnecessary documentation.

## Critical pitfalls

- Multiple launch paths: `main.py`, `app/launcher.py`, CLI, Streamlit, Docker and packaged `mpt`.
- Configuration defaults and first-run behavior may differ.
- Rendering depends on FFmpeg, codecs and host environment.
- `pyproject.toml` / `uv.lock` are authoritative; keep compatibility requirements synchronized when relevant.
- StoryWeaver documentation applies to a different repository. Never import its architectural assumptions.
