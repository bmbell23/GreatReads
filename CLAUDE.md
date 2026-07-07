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
- **Every issue is tagged `STORY:` or `BUG:`** (`BUG:` for defects, `STORY:`
  for everything else — features, chores, refactors, tech-debt). This goes in
  **three places, kept in sync**: (1) the **title** starts with `BUG:`/`STORY:`,
  (2) the **description** starts with `BUG:`/`STORY:`, and (3) a **`bug` or
  `story` label** is applied accordingly. Do this on every new ticket and keep it
  consistent on edits. (Descriptive labels like `enhancement`/`tech-debt` may
  stay alongside the `story` label.)

## ONE ticket at a time, through the board in order

The board is Project #3 "GreatReads" (https://github.com/users/bmbell23/projects/3).
Statuses in order: **Scoping → Ready to Implement → In progress → In Review → Done.**
Never skip columns; keep Status current as work moves.

**No work without a GitHub issue.** When the user asks for something, **create the
ticket as soon as possible** — before touching code.

**New ticket lands in Scoping or Ready to Implement:**
- **Straightforward + you can scope it confidently** (clear tasks, acceptance,
  files/approach, no open decisions) → put it **straight into Ready to Implement**.
- **Needs clarifying questions** → leave it in **Scoping**, **tell the user it needs
  clarification, and ask the questions**. Resolve them with the user, then promote to
  Ready to Implement. Never build from a guess.

**Start implementing → In progress.**

**Made ANY code change → move to In Review, and SAY SO.** The moment you edit code,
move the ticket to **In Review** and **tell the user it's in review**. (This is the
current rule; it overrides any older "not until committed" guidance.)

**The code stays UNCOMMITTED while In Review.** Do **not** commit when you move to
Review. In Review means: work done, **not yet committed**, awaiting the user's verdict.
The commit happens only when the ticket is decided **Done** — or the ticket goes back
to **In progress** for more changes.

**Builds are always explicit — never leave the user guessing.** If a change needs a
**container rebuild** (`./scripts/rebuild-ereader.sh`) and/or an **APK rebuild**
(`./build-app.sh`) to be testable/live, **say so plainly and ask**: *"this needs a
`<container/APK>` rebuild to see it — want me to run it, or will you?"* If they say you,
run it. If they'll do it, wait. If the user would ever have to *discover on their own*
that a rebuild was required, you failed to tell them — that's a bug in the process.

**UNCOMMITTED == IN REVIEW.** At any moment, the list of uncommitted changes and the
list of In-Review tickets should be **identical**. State both clearly whenever
relevant. If they diverge, stop and reconcile.

**Only ONE ticket in In progress + In Review, combined, at any time.** Scoping, Ready
to Implement, and Done may hold many; the active lane holds **exactly one thing**. Work
on one thing at a time.

**User pivots to a new topic while something is In Review? STOP — do not start it.**
Say: to pick that up, we first need to close out the in-review ticket — *is it good to
mark Done and commit?* Only after it's resolved (Done + commit, or sent back to In
progress) do you begin the next thing. **Even "just do this real quick" waits.**

**Done = the user blesses it.** Only the **user** marks a ticket Done. On that bless,
**then commit** (via `gvc`, still a gated action), and the active lane is clear for the
next ticket.

**Update tickets + comment profusely** as work moves — scope, findings, decisions, what
was built, why. The issues (not memory, not docs) are what the next session trusts.

## Autonomy & permissions
- **Work freely without asking for routine, reversible steps** — as long as the
  ticket process above is followed (every change has an issue, work moves to **In
  Review** as soon as code changes) and changes go through code review. Don't
  stop to ask permission to:
  - change directories / `cd` around the repo,
  - read, edit, or create code and other files,
  - run read-only / inspection commands (greps, `curl` health checks, `py_compile`,
    `node --check`, read-only `sqlite3` queries, etc.).
- **Always ask the user first before these three gated actions** (the
  `.claude/settings.local.json` `ask` rules also force a prompt, but treat the
  rule as the source of truth):
  1. **Modifying the database** — any write to `greatreads/data/greatreads.db`
     (or Calibre's `metadata.db`): `INSERT`/`UPDATE`/`DELETE`/`DROP`/schema
     changes, migrations, or data fixes. Read-only queries are fine. Back up the
     DB before any schema/data migration (Jan-2026 data-loss incident).
  2. **Remaking the application** — rebuilding the **container**
     (`./scripts/rebuild-ereader.sh`, see "Rebuild the container" below) **and/or the
     APK** (`./build-app.sh`). Ask before either, and always **tell the user when a
     rebuild is needed** so they never have to discover it themselves (see the board
     flow above). Once they say go, run it yourself; if they'd rather do it, wait.
  3. **Committing code** — `git commit`, `gvc`, `git push`. Commits go through `gvc`,
     only with explicit permission, and **only when a ticket is decided Done** —
     In-Review work stays uncommitted (see the board flow above).

## Rebuild the container — ask first, then do it
- Rebuilding/remaking the application is a **gated action: ask the user before
  rebuilding** (see "Autonomy & permissions" below). Once they say go, run it
  yourself — don't make them type it. The command is:
  `./scripts/rebuild-ereader.sh`
  (the canonical wrapper — it computes the build stamp + a `M`/clean dirty flag on
  the host and passes them as build-args, then runs the same
  `docker compose -p greatreads_ereader -f greatreads/docker-compose.ereader.yml up -d --build`;
  #180. The raw compose command still works but won't set the version's `M` suffix.)
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
  directly; the copies are what get baked.
  - **`version.txt` is now auto-synced** by a repo `pre-commit` hook
    (`scripts/git-hooks/pre-commit`, wired via `core.hooksPath`): every commit
    copies the root `version.txt` into `greatreads/ereader-assets/version.txt`
    and stages it, so it never drifts and there's **no manual `cp` before a
    rebuild** and no dangling change. (If you clone fresh, run
    `git config core.hooksPath scripts/git-hooks` once.)
  - **`summaries/` still needs a manual re-stage** if you edited
    `backend/summaries/*`, before rebuilding, or the container serves stale
    summaries: `cp backend/summaries/*.json greatreads/ereader-assets/summaries/`.
  (A code-only change needs no re-stage.)

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
