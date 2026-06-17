# Working rules for this repo

## GitHub Issues are the source of truth
- **Plans, scoping, next-steps, and status live in GitHub Issues — not in local
  markdown files.** Before starting work, read the open issues
  (`gh issue list`) and treat them as the canonical backlog. When you produce a
  plan or scope, put it **in a GitHub issue** (create one or comment on the
  relevant story), not in a `docs/*.md` file.
- Do **not** create standalone planning/tracking `.md` files. If a design needs
  to be written down, it goes in an issue. (Code/architecture docs, ops
  runbooks, and provenance notes may stay as repo docs — but anything that
  tracks *work to be done* belongs in an issue.)
- Keep issues current: when you finish something, comment/close; when scope
  changes, edit the issue. The issues — not memory, not docs — are what the next
  session should trust.

## Rebuild the container yourself when needed
- When a change to the GreatReads side needs the container rebuilt, **do it** —
  don't ask the user to. The command is:
  `docker compose -p greatreads_ereader -f greatreads/docker-compose.ereader.yml up -d --build`
- This is **data-safe**: the SQLite DB + covers live in the bind-mounted
  `greatreads/data` (`./data:/app/data`), never baked into the image, and host
  prune crons never touch bind mounts. A rebuild only restarts code.
- **Still back up `greatreads/data/greatreads.db` before any schema or data
  migration** (the repo had a Jan-2026 data-loss incident — treat the DB with
  care). Rebuilds that only change code need no backup.

## Runtime orientation (as of the backend-merge work)
- `:8090` `web/serve.py` — static reader files + `/greatreads/` reverse proxy (bare-metal).
- `:8091` `backend/app.py` — FastAPI: Calibre/ABS catalog (`/api/catalog`, `/api/ebooks`),
  audiobooks, highlights, progress, summaries. Bare-metal `uvicorn --reload`. **Being merged
  into the `:8092` container — see the open "Step 2" issue.**
- `:8092` `greatreads_ereader` container — FastAPI: library/TBR/journal/stats + the canonical
  SQLite DB. Code is baked in (`COPY src`), so code changes need a rebuild (see above).
