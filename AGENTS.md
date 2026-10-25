# AGENTS.md — MoneyPrinterTurbo

## Mission

Make the smallest correct, tested change while minimizing unnecessary context, tool calls, and output. Prefer verified repository behavior over assumptions.

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

Pipeline: topic → LLM script/keywords → stock media → voice/subtitles/music → FFmpeg video.

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
