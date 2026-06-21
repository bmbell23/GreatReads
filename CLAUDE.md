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

## Every piece of work has a ticket, and moves through the board in order
- **No work without a GitHub issue.** Every change — feature, fix, chore,
  refactor — needs a ticket first. If you're about to touch code and there's no
  issue for it, create one (in **Scoping**) before starting.
- The board (Project #3 "GreatReads",
  https://github.com/users/bmbell23/projects/3) has one Status flow, and tickets
  move through it **in order**:
  1. **Scoping** — default for new tickets. The ask exists but isn't yet
     detailed enough to build confidently.
  2. **Ready to Implement** — promote here **only when the ticket is thoroughly
     scoped**: clear tasks/acceptance criteria, key files/approach identified,
     open decisions resolved — i.e. someone could pick it up and build it with no
     further questions. If you're not confident you could implement it as
     written, it's not ready — ask the user the questions needed to get it there
     first.
  3. **In progress** — move here when you start implementing.
  4. **In Review** — move here **as soon as you make code changes**. Work stays
     in Review until **the user explicitly blesses it for closing**.
  5. **Done** — only the **user** closes/marks Done. Don't self-close a ticket;
     wait for the bless.
- Don't skip columns (e.g. don't jump Scoping → In progress). Keep the ticket's
  Status current as the work moves.

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
- **Re-stage Ereader assets if their originals changed (#22 fallout).** The
  container bakes in copies at `greatreads/ereader-assets/` — `summaries/`
  (from `backend/summaries/`) and `version.txt` (repo root). The Docker build
  context is `greatreads/`, so it can't `COPY` from `backend/` or the root
  directly; the copies are what get baked. If you edited `backend/summaries/*`
  or bumped `version.txt`, re-stage **before** rebuilding, or the container
  serves stale summaries / an old version pill:
  `cp backend/summaries/*.json greatreads/ereader-assets/summaries/ && cp version.txt greatreads/ereader-assets/version.txt`
  (A code-only change needs no re-stage.) TODO: fold this re-stage into a
  deploy-script wrapper when #8 lands so it can't be forgotten.

## Runtime orientation (post backend-merge — #22 step 2 done)
- `:8090` `web/serve.py` — static reader files + `/greatreads/` reverse proxy (bare-metal).
- `:8092` `greatreads_ereader` container — the **unified** FastAPI app: GreatReads
  (library/TBR/journal/stats + the canonical SQLite DB) **plus** the absorbed Ereader
  routes (`/api/catalog`, `/api/ebooks`, audiobooks, highlights, progress, summaries — all
  still at their original `/api/...` paths via `ereader_api.py`). Code is baked in
  (`COPY src`), so code changes need a rebuild (see above).
- `:8091` `backend/app.py` — **retired** (#22). Folded into `:8092`; the process is stopped
  and nothing listens on the port. `backend/app.py` + `run.sh` are kept in the tree only as
  rollback (slated for deletion once the merge is proven stable — see #22's deferred list).
  Do **not** restart `:8091`; if Calibre/ABS features break, debug `:8092` instead.

## If audio/catalog/covers break, check the `:8092` container (not `:8091`)
- The Ereader routes now live in the `greatreads_ereader` container, which **is**
  supervised (`restart: unless-stopped` + a healthcheck). On reboot it comes back on its
  own — unlike the old unsupervised `:8091`.
- If audiobooks, covers, downloads, highlights, or progress fail, check the container is up
  and healthy (`docker ps --filter name=greatreads_ereader`) and hit
  `curl -sf localhost:8092/api/health` (expect `{"status":"ok",...,"calibre_connected":true}`).
  If `calibre_connected:false`, the container can't reach the host's Calibre/ABS — verify
  `host.docker.internal` resolves (`docker exec greatreads_ereader curl -sf http://host.docker.internal:8083/ajax/library-info`)
  and that the host still publishes `:8083`/`:13378` on `0.0.0.0`.
- **Rollback** (only if the merge regresses): restart `:8091` with
  `cd backend && nohup ./run.sh >/tmp/ereader-backend.log 2>&1 &`, then flip the 5 frontend
  URLs (`web/reader.html` ×2, `web/index.html` ×2, `web/player.js` ×1) from `:8092` back to `:8091`.
