# Dead code: finding it and deleting it safely

Why it matters here: AI agents add a new function or endpoint rather than edit the old one, and
rarely delete the replacement's predecessor. GitClear's analysis of 211M changed lines found
"moved" (refactored) code collapsing from about 16% of changes in 2021 to about 3% in 2024 while
copy/paste rose, consistent with code that accumulates instead of being consolidated
([GitClear 2025 summary](https://www.devclass.com/ai-ml/2025/02/20/ai-is-eroding-code-quality-states-new-in-depth-report/1626250)).
Known local candidates: `backend/app.py` + `run.sh` (retired in #22, kept for rollback), the
`:8091` URLs, old migration scripts, superseded endpoints in `ereader_api.py`, and JS functions
in `web/reader.html` replaced by later ones.

Dead code has real cost: it is searched, read, "fixed", and copied by agents as a pattern, and
its dependencies keep CVEs in the image.

## Principles

1. **Static tools find candidates, never verdicts.** FastAPI route handlers, APScheduler jobs,
   Pydantic validators, SQLAlchemy models and Jinja-referenced names are used by *decorator or
   string*, so every static tool flags them falsely.
2. **Evidence from two independent sources before deletion**: a static signal plus a runtime or
   grep signal.
3. **Delete in small, separate, revertable commits**, one cluster per ticket, never mixed with
   features. Git is the archive; do not leave commented-out code or `_unused_` renames.
4. **Dynamic dispatch on the frontend.** `onclick="foo()"` attributes in HTML and
   `window[name]` lookups defeat naive "is it referenced" checks. Search HTML attributes too.

## 1. Python static detection

### vulture (primary)

Confidence-scored: unused imports 90%, unused args and unreachable code 100%, functions,
classes, attributes and variables 60%. `--min-confidence` filters; `--make-whitelist` generates
an allowlist for false positives
([vulture on PyPI](https://pypi.org/project/vulture/)). Run through uv without installing:

```bash
uvx vulture greatreads/src/greatreads --min-confidence 80          # high-signal pass
uvx vulture greatreads/src/greatreads --min-confidence 60 \
    --exclude "*/alembic/*" --sort-by-size > /tmp/vulture-60.txt   # candidate list
```

Config (`pyproject.toml`) and a whitelist for framework magic:

```toml
[tool.vulture]
paths = ["src/greatreads", "vulture_whitelist.py"]
min_confidence = 80
ignore_decorators = ["@router.*", "@app.*", "@validator", "@field_validator",
                     "@model_validator", "@asynccontextmanager", "@pytest.fixture"]
ignore_names = ["model_config", "Config", "lifespan"]
```

Pattern: first run at 60 produces a few hundred findings. Generate
`vulture_whitelist.py` for the verified false positives (`--make-whitelist`), commit it, and
gate CI at `--min-confidence 80`. New dead code then fails the build; old debt is tracked at 60
in a monthly report.

### deadcode (alternative)

[`deadcode`](https://pypi.org/project/deadcode/) is newer, AST-based, TOML-configured, has
auto-fix (`--fix`) and can be stricter in CI; summaries suggest vulture suits large-codebase
gradual cleanup while deadcode suits strict enforcement on newer code
([comparison](https://smithery.ai/skills/laurigates/vulture-dead-code)). Pick one gate to avoid
duplicate whitelists. **Recommendation: vulture** (mature, whitelist workflow, 14 years of
false-positive handling). Try `deadcode` once on `src/greatreads` and compare its list for
extra true positives; do not run `--fix` unsupervised.

### Cheap ruff signals (already in the linting plan)

`F401` unused import, `F841` unused local, `ARG` unused arguments, `ERA001`
commented-out code (turn on as a ratchet), `RUF100` unused noqa. Unreachable code after
`return`/`raise` is vulture 100%.

### Unused dependencies

`uvx deptry greatreads` reports declared-but-unimported and imported-but-undeclared packages.
Check `pandas`, `python-jose`, `passlib`, `rich`, `aiofiles`, `requests` (httpx is also
present, so two HTTP clients: consolidate). Mapping import names to distributions needs
`[tool.deptry.package_module_name_map]` for e.g. `pillow -> PIL`, `python-dateutil -> dateutil`.

## 2. Coverage-guided removal (runtime evidence)

Static tools cannot see route usage. Combine two runtime sources.

**a) Test/Coverage.** Lines never executed by any test are *not* dead, but dead code is always
uncovered, so uncovered is the shortlist to examine:

```bash
cd greatreads && uv run pytest ../backend/tests --cov=src/greatreads \
    --cov-report=term-missing:skip-covered --cov-report=lcov:/tmp/cov.lcov
```

(Testing/LCOV has its own ticket; this just consumes the output.) Intersect:
`vulture 60% findings` AND `0% coverage` is a strong candidate.

**b) Production coverage, the best evidence for a single-user app.** Run the live container
with `coverage run` for 2 to 4 weeks, then read what was never hit:

```bash
# in the container entrypoint, behind an env flag (not default):
coverage run --data-file=/app/data/.coverage --source=/app/src/greatreads -m uvicorn ...
# monthly, after a graceful stop:
coverage report --data-file=/app/data/.coverage --show-missing --skip-covered
```

Needs care: coverage data is written on a clean exit, and our restart method kills the process
(CLAUDE.md), so use `coverage`'s `--save-signal`/`COVERAGE_PROCESS_START` or
SIGTERM-handling (`sigterm = true` under `[tool.coverage.run]`). Adds overhead; run it only in
the measurement window. Gated: it needs a container rebuild, so ask first.

**c) Endpoint access evidence.** The app has `event_log` and `/logs` (#184), and uvicorn access
logs. For each route in `ereader_api.py`, count hits over 60 to 90 days:

```bash
docker logs greatreads_ereader --since 2160h 2>&1 | rg -o '"(GET|POST|PUT|DELETE) [^ ?"]+' | sort | uniq -c | sort -n
```

Enumerate declared routes (`python -c "from greatreads.main import app; [print(r.methods, r.path) for r in app.routes]"`)
and diff. Zero hits in 90 days *and* zero references in `web/`, the APK and `scripts/` is a
deletion candidate. Beware endpoints used only by seasonal jobs, scheduled tasks and the Android
app on long offline stretches.

## 3. JavaScript and HTML

- **knip** finds unused files, exports and dependencies in JS/TS projects
  ([knip.dev](https://knip.dev)); but it needs a module graph. Our JS is mostly inline in HTML,
  so knip sees almost nothing until scripts are extracted into modules (linting doc, section 5).
  Do the extraction first, then `npx knip` works well for `web/js/*.js`.
- **Inline-script reality, until extraction:**
  1. ESLint `no-unused-vars` (functions at top level of an inline script count as globals, so
     tell it `vars: "local"`, or use `eslint-plugin-html` per-script scope) and `no-redeclare`.
  2. Function-name census with ripgrep: for every `function name(` in a file, count references
     across `web/`, `app/` assets and templates:

     ```bash
     rg -o "function (\w+)\(" -r '$1' web/reader.html | sort -u | while read f; do
       n=$(rg -c "\b$f\b" web app --glob '!*.min.*' | awk -F: '{s+=$2} END{print s}')
       [ "$n" -le 1 ] && echo "$f  refs=$n"
     done
     ```

     `refs<=1` means only the definition; check `onclick=`, `addEventListener(..., name)` and
     string dispatch before trusting. Names built dynamically need manual check.
  3. **Browser coverage**: Chrome DevTools Coverage tab, or Playwright
     `page.coverage.startJSCoverage()`, while exercising reader, player, library, stats. Unused
     bytes by function highlight dead blocks. Run it on both the desktop browser and the Android
     WebView (via `chrome://inspect`), because the phone path differs.
- **CSS**: DevTools Coverage also lists unused rules. Low priority.
- **Duplicate definitions**: two `function foo()` in one script means the first is dead (the
  last declaration wins). ESLint `no-redeclare` finds these deterministically; this is the
  commonest AI-edit leftover.

## 4. Safe deletion workflow

One cluster per ticket (`STORY: remove dead code in X`):

1. **Collect candidates** (vulture 60+, deadcode, ruff, route census, JS census).
2. **Triage each**: is it referenced by string? decorator? template? cron/scheduler?
   `scripts/`? the Android wrapper or `deploy/`? Grep across the whole repo including
   non-Python files: `rg -n "name" --hidden -g '!.git'`.
3. **Pin behaviour**: if the neighbourhood has no test, add a characterization test or at
   least a smoke check of the endpoints around it. Not for the dead code itself, for what stays.
4. **Delete** the whole cluster including its tests, constants, imports and docs mentions.
   Run `ruff check --select F401,F841` afterwards to catch the newly orphaned imports and
   iterate (deleting dead code exposes more).
5. **Verify**: tests, `python -m compileall`, import the app (`python -c "import greatreads.main"`),
   hit `/api/health` and the routes next to the deleted ones, load the pages in a browser.
   Note CLAUDE.md: a container rebuild is a gated action, ask before running it, and web/*
   changes need an APK rebuild to reach the phone.
6. **Move the ticket to In Review uncommitted**, per the board rules. Commit only on Done.
7. **Deletion lag for risky items**: for routes and scheduler jobs, first make them log a
   deprecation (`log_event("deprecated_route", path)`) for 30 days; delete only if never hit.
8. **Revert is the safety net**: small commits, `git revert` is one command. Data is never part
   of dead-code removal: do not drop tables or columns here (CLAUDE.md data-safety rules; schema
   removal is a separate backed-up migration).

## 5. Keeping it dead

- CI: `vulture --min-confidence 80` and `ruff` with `F401,F841` (blocking).
- Monthly: the 60% vulture report and route-hit census (README cadence).
- Retire rollback scaffolding on a date: for example `backend/app.py` deletes when #22's deferred
  list says "proven stable". Put the date in the ticket, not in a comment that nobody rereads.
- In `CLAUDE.md`, tell agents: "when you replace something, delete the old version in the same
  change." This single rule prevents most accumulation (`ai-generated-code.md`).
