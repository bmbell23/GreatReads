# Vendored GreatReads

This directory is a **plain copy** of the GreatReads app, vendored into the Ereader repo as
part of the merge (tracked in the GitHub Issues — the merge "stories" #3–#9 + the Step 2 issue).

- **Source repo:** `/home/brandon/projects/GreatReads` (GitHub: `bmbell23/GreatReads`)
- **Source commit:** `dbafbc1421b0345fa79a36e8233fda5b1ac283b3` — *v2.1.7: large improvements to the TBR reordering page*
- **Vendored on:** 2026-06-14
- **What was copied:** `src/`, `scripts/`, `migrations/`, `Dockerfile`, `pyproject.toml`,
  `.dockerignore`. Excluded: `.git`, `data/`, `logs/`, `*.db`, `__pycache__`, `*.egg-info`.

## Local modifications (diffs from upstream)
- `src/greatreads/main.py` — added an `ENABLE_SCHEDULERS` env gate in `lifespan()` so this
  isolated instance does not run the midnight chain-recalc / 15-min Calibre+ABS auto-sync
  jobs. Default remains enabled to match upstream/production behavior.

## How this runs
Built and run as an **isolated** container, **not** the production `greatreads_app`:
```
docker compose -p greatreads_ereader -f greatreads/docker-compose.ereader.yml up -d --build
```
It listens on host port **:8092**, uses the **copied** DB in `greatreads/data/` (gitignored),
and mounts Calibre/ABS **read-only**. See `docker-compose.ereader.yml`.

## Important
We expect to **diverge** from upstream from here on (that's why this is a copy, not a
submodule). Future changes happen here; upstream GreatReads is on its way to being retired
once the merge completes (PLAN Stories 2 & 9). Do not edit the upstream repo to change this.
