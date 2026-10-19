# Modernization: Python, dependencies, Docker

Current state: `python:3.11-slim`, `pyproject.toml` with `>=` floors, no lockfile,
`requires-python = ">=3.8"`. Every rebuild can silently pull different versions of everything.
That is the main risk this doc fixes.

## 1. Python version policy

### EOL dates

From [endoflife.date/python](https://endoflife.date/python) and the
[PEP release schedules](https://peps.python.org/) (security-fix end of life is October of the
fifth year after release):

| Version | Bugfix ends | Security EOL |
|---|---|---|
| 3.11 | 2024-04 (already) | **2027-10-31** |
| 3.12 | 2025-04 (already) | 2028-10-31 |
| 3.13 | 2026-10-01 | 2029-10-31 |
| 3.14 | 2027-10-01 | 2030-10-31 |

Sources for the table: [endoflife.date](https://endoflife.date/python),
[Python version support timeline](https://hidekazu-konishi.com/entry/python_version_support_and_eol_timeline.html).
Verify against endoflife.date before acting; I did not read the PEPs directly.

### Policy for GreatReads

- **Rule: run a version that has at least 18 months of security support left, and one that
  every dependency declares support for.** Move when the version you run drops under 12 months.
- 3.11 has ~12 months left today (2026-10-19). That is the trigger: **plan the move now,
  execute within the next quarter.**
- **Target 3.13** (security to 2029-10). It is the conservative choice: a year-plus old, wheels
  exist for pandas, Pillow, cryptography, bcrypt. 3.14 is fine later (2027), but being on the
  newest release costs wheel gaps and gives this app nothing it needs. Skip 3.12: it is only a
  one-year gain.
- Do the bump as its own ticket: change `FROM`, regenerate the lock, run tests, rebuild, smoke
  test the scheduler jobs and Calibre/ABS sync. No feature work in the same PR.
- Python bumps in the Dockerfile are a *deliberate* change. Renovate must not auto-bump the
  Python minor (see config below).
- Set `requires-python = ">=3.11"` today (honest about what we test), then `">=3.13"` after the
  bump. The current `>=3.8` is a lie and makes resolvers consider ancient dependency versions.
  Also update ruff `target-version` and `[tool.black]`/`mypy` leftovers (see linting doc).
- Read the "What's New" pages for removed APIs. Typical breakers 3.11 to 3.13: removed
  `asyncio.get_event_loop()` implicit creation edge cases, removed `cgi`, `imghdr`,
  `telnetlib`, `pipes` (PEP 594, removed in 3.13), `datetime.utcnow()` deprecation noise
  (3.12). `ruff check --select UP,DTZ,PLW` plus the test suite finds most of it.

## 2. Dependency management

### Recommendation: uv with a committed `uv.lock`

Why uv over pip-tools: one tool for Python install, venv, resolve, lock, sync; the lockfile is
cross-platform and hash-pinned; first-class Docker guidance; Renovate and Dependabot both
update it. pip-tools (`pip-compile --generate-hashes`) is still correct and boring; choose it
only if you refuse to add a new tool. Poetry/PDM offer nothing we need.
See [uv in Docker](https://docs.astral.sh/uv/guides/integration/docker/),
[pydevtools: uv lockfile](https://pydevtools.com/handbook/how-to/how-to-use-a-uv-lockfile-for-reproducible-python-environments/),
[pydevtools: uv in a Dockerfile](https://pydevtools.com/handbook/how-to/how-to-use-uv-in-a-dockerfile/).

### Pinning strategy (the usual split)

- **`pyproject.toml` = intent**: loose compatible ranges, floors that reflect real requirements,
  optional ceiling on majors that bit us (`sqlalchemy>=2.0,<3`, `pydantic>=2.4,<3`).
- **`uv.lock` = reality**: exact versions + hashes of the whole tree. Deployed builds use *only*
  the lock. This is an application, not a library, so the lock is the source of truth.
- Move dev tools to a dependency group so production images do not install them:

```toml
[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio", "pytest-cov", "ruff", "mypy", "vulture", "deptry", "pre-commit"]
```

- Delete the `[tool.black]`, `[tool.isort]` sections and the black/isort dev deps once ruff
  formats (linting doc).
- `passlib` and `python-jose` are both effectively unmaintained (passlib last release 2020;
  python-jose has had CVEs and slow releases). Put them on the audit list: replace with
  `bcrypt` directly / `pyjwt` or `authlib`. File as tickets, not part of this adoption.
- `pandas` is a heavy dependency; check whether it is truly needed (`deptry` and grep). Dropping
  it shrinks the image and the CVE surface.

### Adoption steps (one ticket)

```bash
cd greatreads
uv lock                                    # creates uv.lock from current ranges
uv sync --locked --group dev               # local env
uv run pytest ../backend/tests             # prove it still works
git add uv.lock pyproject.toml
```

Dockerfile (replace the `pip install` step; layer-cached deps, locked, no dev group):

```dockerfile
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"
```

`--locked` fails the build if `pyproject.toml` and `uv.lock` disagree, which is what you want
in CI and Docker. `--frozen` skips the check and is only for when you already validated.
(Distinction per the [uv Docker guide](https://docs-next.astral.sh/uv/guides/integration/docker).)
Pin the uv image to an exact version (and digest, see below). Check the exact tag exists before
copying the snippet above; `0.9` is illustrative.

Note CLAUDE.md: the container bakes `src` via `COPY`, so every dependency change needs a
rebuild. That is already the gated action; this does not change it.

## 3. Automated updates: Renovate (preferred) or Dependabot

[uv's own docs](https://docs.astral.sh/uv/guides/integration/dependency-bots) say both work;
Renovate is more complete for uv (`lockFileMaintenance`, `minimumReleaseAge`), Dependabot's uv
support has gaps. Dependabot is zero-setup on GitHub; Renovate needs the GitHub App install
(free). Pick Renovate. `renovate.json`:

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended", ":semanticCommits", "helpers:pinGitHubActionDigests"],
  "schedule": ["before 6am on the first monday of the month"],
  "timezone": "America/New_York",
  "minimumReleaseAge": "7 days",
  "prConcurrentLimit": 3,
  "lockFileMaintenance": { "enabled": true, "schedule": ["before 6am on the first monday of the month"] },
  "packageRules": [
    { "matchUpdateTypes": ["minor", "patch"], "groupName": "python minor+patch" },
    { "matchUpdateTypes": ["major"], "dependencyDashboardApproval": true },
    { "matchPackageNames": ["python"], "matchManagers": ["dockerfile"],
      "matchUpdateTypes": ["major", "minor"], "enabled": false }
  ],
  "vulnerabilityAlerts": { "labels": ["bug", "security"], "schedule": ["at any time"] }
}
```

Why these settings:
- **Monthly schedule, grouped**: matches the cadence in README; a legacy app with a rebuild gate
  cannot absorb daily bot PRs.
- **`minimumReleaseAge: 7 days`**: a cooldown lets the ecosystem catch compromised or yanked
  releases before we adopt them. Match it with `exclude-newer` in uv if you set one (uv docs).
- **Majors need manual approval** via the dashboard.
- **Python minor bumps disabled**: that is a policy decision (section 1).
- Security alerts bypass the schedule.

Each Renovate PR is a code change, so it follows the board rules: merged only through a ticket.
Simplest: one standing `STORY: monthly dependency update` ticket per month.

## 4. Vulnerability scanning

- **`uv audit`** (uv >= 0.10.12, reads `uv.lock`, queries OSV; see
  [Astral announcement](https://astral.sh/blog/uv-audit)). Verify it exists in your pinned uv
  before relying on it.
- **`pip-audit`** is the established alternative
  ([pydevtools guide](https://pydevtools.com/handbook/how-to/how-to-scan-python-dependencies-for-vulnerabilities/)):
  `uvx pip-audit --strict -r <(uv export --no-hashes --no-dev)`.
- **`osv-scanner`** scans lockfiles, plus Dockerfile base images and the JS side:
  `osv-scanner scan --lockfile greatreads/uv.lock --lockfile package-lock.json`.
- CI: run weekly on a schedule *and* on PRs touching `uv.lock`. A new CVE against an unchanged
  lock still matters, so the schedule is the one that catches things.
- Triage rule: HIGH/CRITICAL with a fix available becomes a `BUG:` ticket; unfixable ones get
  an ignore with an expiry date and a reason, never a silent ignore.
- Also scan the image (`trivy image greatreads_ereader`, or `docker scout`) monthly: the base
  OS packages carry CVEs your Python lock never shows.

## 5. Docker base-image hygiene

- **Pin deliberately.** `python:3.11-slim` floats across Debian releases and patch versions, so
  two rebuilds give different images. Use an explicit Debian codename and patch version, and a
  digest once stable: `FROM python:3.13.x-slim-trixie@sha256:...`
  ([Docker Hub python tags](https://hub.docker.com/_/python)). Renovate can bump the digest
  (`pinDigests`), giving reviewable base-image PRs.
- **Rebuild monthly with `--pull`** (`docker compose build --pull`; our wrapper
  `./scripts/rebuild-ereader.sh` should gain the flag) even with no code change, for security
  patches. Gated action: ask first.
- **Multi-stage**: build stage (uv + compilers) and runtime stage; the runtime image has no
  uv, no build tools, no pip cache. Currently the image installs `curl` and `tzdata` only, which
  is already lean; keep `curl` only because of the healthcheck, or switch the healthcheck to a
  Python one-liner and drop it.
- **Run as non-root** (`USER app`). The bind-mounted `./data` must be writable by that UID;
  check ownership before changing, because a wrong UID breaks the SQLite DB. Back up first.
- `.dockerignore` must exclude `.git`, `data/`, `*.db`, `.env`, venvs, docs.
- `HEALTHCHECK` stays (we have one). Add `STOPSIGNAL SIGTERM` handling so APScheduler shuts down
  cleanly; verify uvicorn runs as PID 1 or under `tini`.
- Keep the `ereader-assets/` staging rule from CLAUDE.md in mind: the build context is
  `greatreads/`.

## 6. JavaScript dependencies

There are almost none: the web app is vanilla JS/HTML (`web/reader.html`, `player.js`) with no
`package.json` bundling. That is a strength (tiny supply-chain surface); keep it.

- Any vendored libs (hls.js, epub.js or similar `<script src>`/copied files) are the real JS
  dependencies and invisible to bots. Inventory them in one file (`web/vendor/README` listing
  name, version, source URL, hash) and check them quarterly with
  `osv-scanner` / the library's changelog. Prefer pinned local copies with SRI hashes over CDN
  links, since the app also runs offline in the Android WebView.
- The Android wrapper (`app/`) has Gradle dependencies: add the `gradle` ecosystem to Renovate
  (`config:recommended` includes it), and note it follows its own build (`./build-app.sh`).
- If a `package.json` is added later only for tooling (eslint, knip), treat it as dev-only:
  commit the lockfile, never ship it, include in `osv-scanner`.

## Checklist (ticket-sized)

1. `requires-python` and tooling targets honest; fix mypy `python_version`.
2. `uv.lock` committed, dev group split, Dockerfile on `uv sync --locked`.
3. Renovate installed with the config above.
4. `uv audit` / `osv-scanner` in CI (weekly + on lock change).
5. Base image pinned to codename + digest; monthly `--pull` rebuild in the cadence.
6. Python 3.13 bump ticket (do after step 2 so the lock exists).
7. Replace `passlib`/`python-jose`; evaluate dropping `pandas`.
