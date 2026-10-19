# Codebase health playbook

How to keep GreatReads (largely AI-written, FastAPI + SQLite + vanilla JS + Android WebView)
from drifting off the rails. These docs are the **how**. GitHub issues are the **what to fix**
(per `CLAUDE.md`: no work-tracking markdown files). Audit findings live as tickets, never here.

Written 2026-10-19 for ticket #385. Sources are linked inline in each doc; re-check them when
you revisit, because tool versions and EOL dates move.

## The docs

| Doc | Question it answers |
|---|---|
| [modernization.md](modernization.md) | Which Python do we run, how are deps pinned/updated/scanned, how is the Docker image kept fresh? |
| [linting-and-typing.md](linting-and-typing.md) | ruff, pylint, mypy/pyright, pre-commit, and how to adopt them on a repo that has never had them |
| [dead-code.md](dead-code.md) | How to find and safely delete code nobody uses (Python and JS) |
| [ai-generated-code.md](ai-generated-code.md) | What goes wrong in vibe-coded repos and which guardrails actually work |

Topics in #385 not given their own doc here (architecture review, performance/concurrency,
stability) are audit work: they are done as ticketed findings, not as standing process. Testing
and coverage have their own ticket.

## Where GreatReads stands today (facts, 2026-10-19)

- Python 3.11 (`python:3.11-slim`), `pyproject.toml` with `>=` floors only, **no lockfile**,
  `requires-python = ">=3.8"` (false: the code runs on 3.11 only).
- black/isort/mypy/ruff are listed as dev deps but **nothing runs them**: no CI (`.github/`
  absent), no `.pre-commit-config.yaml`. The only hook is `scripts/git-hooks/pre-commit`
  (version sync).
- `[tool.mypy] python_version = "2.1.7"` is the *app* version pasted into the wrong key. mypy is
  effectively misconfigured.
- About 46 `except ...: pass` blocks and 214 `except Exception` sites in `greatreads/src`.
- Tests exist under `backend/tests/`, none under `greatreads/`, where the live app is.
- Hotspots: `web/reader.html` (~8.8k lines, inline JS), `greatreads/src/greatreads/ereader_api.py`
  (~4.4k lines).

## Principles

1. **Baseline, then ratchet.** Never require a clean slate. Record today's violations, fail only
   on *new* ones, and tighten over time. Big-bang fixes in a vibe-coded repo create regressions.
2. **Automate or it will not happen.** Anything in this playbook that matters becomes a hook, a
   CI job, or a scheduled bot PR. Prose rules are for agents (`CLAUDE.md`), gates are for
   everyone.
3. **One concern per PR.** Formatting-only, dependency-only, and behaviour changes never share a
   commit. DORA 2025 found AI inflates batch size and that larger batches hurt stability
   ([InfoQ summary](https://www.infoq.com/news/2025/09/dora-state-of-ai-in-dev-2025)).
4. **Tests before deletion or refactor.** See `dead-code.md` and `ai-generated-code.md`.
5. **Everything follows the board rules in `CLAUDE.md`.** Each adoption step below is a ticket,
   one at a time, moved to In Review uncommitted, committed via `gvc` only at Done.

## Maintenance cadence

### Per PR / per ticket (automated where possible)
- `ruff check` and `ruff format --check` on changed files (pre-commit + CI).
- Type check ratchet: no new mypy/pyright errors versus the baseline.
- Tests for touched modules pass; new behaviour has a test, bug fixes have a regression test.
- No new bare `except: pass`, no new `# noqa` / `# type: ignore` without a reason comment.
- If a file grew past its size budget (see `ai-generated-code.md`), extract instead of appending.
- Diff reviewed by a second agent (`mirror`) or human before In Review.
- Any new dependency is justified in the PR and verified to exist on PyPI/npm (hallucinated
  names are a real attack vector, see `ai-generated-code.md`).

### Monthly (30 minutes, first week of the month)
- Merge the batched Renovate PRs (grouped minor/patch; one at a time, run tests, rebuild).
- `uv audit` (or `pip-audit`) / `osv-scanner` results reviewed; any HIGH/CRITICAL becomes a `BUG:`.
- Rebuild the container with `--pull` so the base image's Debian security patches land.
- Run `vulture` (`dead-code.md`); file one ticket per cluster of confirmed dead code.
- Re-read the ratchet numbers (ruff violations, mypy errors, `except: pass` count, largest file
  sizes). They go down or stay flat; if one rose, find the PR.
- Check the backup of `greatreads/data/greatreads.db` actually restores.

### Quarterly (half a day)
- Python version check: any supported version newer than ours that all deps support? Plan the
  bump if we are within 12 months of EOL (`modernization.md`).
- Major dependency upgrades (FastAPI, SQLAlchemy, Pydantic, Pillow, pandas) done deliberately,
  one at a time.
- Tighten one ratchet: enable one more ruff rule family, or remove one `per-file-ignores` entry.
- Architecture pass: top 5 largest/most-churned files, duplication report, module boundaries.
  Output is tickets.
- Prune: unused deps (`deptry`), unused JS functions in `web/`, stale feature flags, retired
  code kept "for rollback" (e.g. `backend/app.py`, see CLAUDE.md #22 deferred list).
- Re-read this playbook; fix anything that no longer matches reality.

## Order of adoption (each is its own ticket)

1. `ruff` config + baseline + pre-commit (`linting-and-typing.md`). Fixes the mypy misconfig.
2. `uv` lockfile + Dockerfile on `uv sync --locked` + Renovate (`modernization.md`).
3. CI workflow: ruff, type ratchet, tests, `uv audit`.
4. Silent-exception ratchet and size budgets (`ai-generated-code.md`).
5. Dead-code sweep with vulture, JS first pass with a coverage run.
6. Python 3.13 evaluation.
7. Audit tickets from #385 items 3, 4, 5 (architecture, performance, stability).
