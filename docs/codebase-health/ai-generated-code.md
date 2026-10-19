# AI-generated code: failure modes and guardrails

GreatReads is mostly written by Claude agents. That is fine, but AI-written code fails in
characteristic, predictable ways, and the countermeasures are mostly process and automation,
not "review harder". Evidence quality varies: some of this is peer-reviewed or large-sample,
some is vendor research with an agenda. Each claim is labelled.

## 1. Failure modes (with evidence)

### 1.1 Duplication instead of reuse
Agents with limited context add a new block rather than find the existing helper.
GitClear (211M changed lines, 2020 to 2024, vendor study): copy/pasted lines rose from 10.64%
to 13.90%, "moved" (refactored) lines fell from 15.88% to 3.10%, and blocks of 5+ duplicated
lines rose about 8x in 2024; 2024 was the first year copy/paste exceeded moved code
([DevClass](https://www.devclass.com/ai-ml/2025/02/20/ai-is-eroding-code-quality-states-new-in-depth-report/1626250),
[LeadDev](https://leaddev.com/software-quality/how-ai-generated-code-accelerates-technical-debt)).
Caveat: methodology is the vendor's own; the direction matches wide practitioner experience.
**In GreatReads:** the "drift between copies" is visible in the recent history (#383, ebook
and audiobook linking written separately, co-author ordering handled in one place not the
other) and in two parallel stacks: `backend/app.py` and `ereader_api.py`, `requests` and `httpx`.

### 1.2 Plausible but wrong code
Code that reads well, passes the happy path, and is subtly incorrect: wrong edge cases, wrong
API semantics, invented parameters. Veracode's 2025 study of 100+ LLMs on 80 tasks (vendor
study) found 45% of generated code introduced an OWASP Top 10 vulnerability, with Python at
38 to 45% and cross-site scripting failing in 86% of relevant cases
([Veracode](https://www.veracode.com/press-release/ai-generated-code-poses-major-security-risks-in-nearly-half-of-all-development-tasks-veracode-research-reveals/),
[Help Net Security](https://www.helpnetsecurity.com/2025/08/07/create-ai-code-security-risks/)).
Relevant to us: a vanilla-JS app that builds HTML strings (`innerHTML`) from book titles and
journal text is exactly the XSS shape; auth uses `passlib`/`python-jose`.
**Hallucinated dependencies**: a USENIX-track study of 576k samples from 16 LLMs found at least
5.2% (commercial) and 21.7% (open) of suggested packages did not exist, and 43% of hallucinated
names recurred across runs, which makes registering them ("slopsquatting") practical for
attackers ([arXiv 2406.10279](https://arxiv.org/abs/2406.10279v3),
[Dark Reading](https://www.darkreading.com/application-security/ai-code-tools-widely-hallucinate-packages)).
Guard: every new dependency name is verified on PyPI/npm by a human or CI, and the lockfile
(modernization.md) makes additions visible in the diff.

### 1.3 Dead branches and leftover scaffolding
Fallbacks "just in case", compatibility shims for things that never existed, old versions left
beside new ones, duplicate function definitions in the same inline script where the later one
wins. See `dead-code.md`.

### 1.4 Inconsistent patterns
Five ways to do the same thing: DB sessions opened ad hoc vs dependency-injected, `requests` vs
`httpx`, sync endpoints vs async endpoints calling blocking I/O (cf. #363), error responses as
dict vs `HTTPException`, ad hoc JSON shapes. Each session starts from a clean context and
imitates whatever file it happened to read. Convention drift is the main reason a codebase
"feels" vibe-coded.

### 1.5 Over-long files and functions
Agents append to the file they are in. `web/reader.html` (~8.8k lines) and `ereader_api.py`
(~4.4k lines) are the result. Long files make every future agent session worse: more context
spent, more chance of unseen duplicates, more merge conflict. (Analysis of the CodeScene
"Code Health" research: unhealthy code carries significantly higher defect risk and
slower change, and AI assistants perform worse on unhealthy code. See
[CodeScene: AI-ready code](https://codescene.com/blog), verify the specific figures there
before quoting them; not independently checked here.)

### 1.6 Silent exception swallowing
`except Exception: pass`, or logging at debug and returning a default, so failures become
missing data rather than errors. This is the single most dangerous habit for a data app:
sync jobs "succeed" while writing nothing. Local measure: about 46 `except ...: pass` blocks
and 214 `except Exception` sites in `greatreads/src` (counted 2026-10-19; re-run the
greps below). This is also how tests pass on broken code.

### 1.7 Drift between copies
Constants, URLs, and logic duplicated across `web/reader.html`, `player.js`, the APK-bundled
copy of `web/`, `greatreads/ereader-assets/` (baked copies of summaries and version.txt), and
the Python side. A fix lands in one copy; the others rot. The :8091 to :8092 URL flip needing 5
edits in 3 files (CLAUDE.md rollback) is the pattern.

### 1.8 Speed outruns stability
DORA 2025 (about 5,000 respondents): AI adoption is about 90%, individual productivity rises, but
delivery stability falls, because AI inflates change size and larger batches are riskier and
slower to review; AI is an "amplifier" of existing engineering practices, good or bad
([InfoQ](https://www.infoq.com/news/2025/09/dora-state-of-ai-in-dev-2025),
[Google summary](https://blog.google/technology/developers/dora-report-2025/),
[RedMonk](https://redmonk.com/rstephens/2025/12/18/dora2025/)). Only about 3% report high
trust in AI output, so verification cost is the real bill.

## 2. Guardrails that work (ordered by leverage)

### 2.1 Linters, types and tests as hard gates (CI, not honour system)
Machines enforce what instructions only suggest. See `linting-and-typing.md`. For AI-specific
failure modes, add:

```toml
# silent exceptions, blocking calls in async, unused code, shadowed names
select = ["S110", "S112", "BLE001", "TRY", "ASYNC", "B", "F", "ARG", "PLW", "RUF100"]
```

Ratchet counters, checked in CI against committed numbers
(`.health-baseline.json`), failing on any increase:

```bash
echo "except_pass $(rg -c --glob '*.py' -U 'except[^\n]*:\s*\n\s*pass' greatreads/src | awk -F: '{s+=$2} END{print s+0}')"
echo "noqa $(rg -c '# noqa' greatreads/src | awk -F: '{s+=$2} END{print s+0}')"
echo "type_ignore $(rg -c '# type: ignore' greatreads/src | awk -F: '{s+=$2} END{print s+0}')"
echo "max_file_lines $(git ls-files 'greatreads/src/*.py' 'web/*' | xargs wc -l | sort -n | tail -2 | head -1)"
```

Preferred replacement for a swallowed exception: catch the narrow type, log with
`logger.exception(...)`, and either re-raise, or `log_event(...)` and surface it in `/logs`. A
bare `pass` needs a comment saying why swallowing is correct.

### 2.2 Tests as the specification
The most effective instruction to an agent is a failing test. Rules:
- Bug fix: write a failing regression test first, then the fix. (#383 style cases: write the
  co-author ordering test, then change the matcher.)
- Characterization tests before refactoring a hotspot: pin current outputs of
  `ereader_api.py` endpoints (use FastAPI `TestClient` against a temp SQLite file; never the
  live DB).
- Agents cannot mark their own homework: a test the agent wrote with the code proves little.
  Prefer property-style and example tables from the user's real data shapes (anonymised).
- Mutation testing (`mutmut`, `cosmic-ray`) occasionally, on the 3 most critical modules, to
  detect tests that assert nothing. This is a quarterly luxury, not a gate.
Coverage details live in the testing ticket.

### 2.3 Small PRs, one concern each
Matches the board rules (one ticket, uncommitted In Review, user blesses). Add a size budget:
over about 400 changed lines or about 10 files, split it. Large mechanical changes (format,
rename) go in their own commit and are listed in `.git-blame-ignore-revs`.

### 2.4 CLAUDE.md conventions that agents actually follow
Put short, checkable, repo-specific rules in `CLAUDE.md` (the file is auto-loaded). Candidates
(each one is a rule an agent can obey literally):
- "Before writing a helper, `rg` for existing ones; reuse or extend. Say which you found."
- "When you replace code, delete the old code in the same change."
- "Never write `except: pass` or bare `except Exception`; catch the narrow type and log."
- "New endpoints use a Pydantic response model and `Depends(get_db)`; never open sessions inline."
- "Never call blocking I/O (`requests`, `time.sleep`, file/DB-heavy loops) inside `async def`;
  use `def` endpoints or `run_in_threadpool`/`httpx.AsyncClient`."
- "New JS goes in `web/js/*.js`, not inline in HTML. Do not add to files over the size budget."
- "No new dependency without naming it in the ticket and confirming it exists on the registry."
- "Run `ruff check`, the type baseline and the tests before saying a task is done, and paste
  the output (CLAUDE.md verification honesty rule)."
Keep it short: long instruction files get partially ignored, and anything enforceable should be
a CI gate instead. Review CLAUDE.md quarterly and delete rules a linter now enforces.

### 2.5 A second agent reviews every diff
A fresh-context reviewer (the `mirror` agent here) with a checklist prompt finds what the author
agent rationalises: duplicates of existing helpers, missing edge cases, swallowed errors,
rule breaks. Give it the diff, not the author's explanation, so it is not anchored. Treat
its output as a lead to verify, not a verdict. Humans still own the final "blessed Done".

Reviewer checklist: (1) does something equivalent already exist (rg for it)? (2) every new
`except`: is the failure visible? (3) blocking calls in async? (4) are inputs validated at the
boundary and HTML escaped on output? (5) was something replaced but not deleted? (6) is there
a test that would fail without this change? (7) any new dependency or secret?

### 2.6 Architecture decision records (ADRs)
Agents re-litigate and contradict past decisions because they never saw them. Keep short ADRs
(10 to 20 lines: context, decision, consequences) so the decision survives the session. Here the
**GitHub issue is the ADR** (CLAUDE.md: issues, not local docs, hold plans), so:
- Label decision issues `adr`; link them from the code with a one-line comment
  (`# see #22: unified app, :8091 retired`).
- Existing candidates already in memory notes: metadata override layer (GR never writes to
  Calibre/ABS), Calibre write safety, SQLite as the canonical DB, APK bundles web assets.
- A decision with a "do not" is worth a CI check where possible (for example a grep gate that
  fails if anything writes to Calibre's `metadata.db`).
Template: `ADR: <decision>` / Context / Decision / Consequences / How it's enforced.

### 2.7 Structure that limits blast radius
- Size budget: Python files over about 800 lines and any single function over about 80 lines
  get a ticket to split; new code never lands in a file already over budget. Split
  `ereader_api.py` into `APIRouter` modules by domain (catalog, audiobooks, highlights,
  progress, summaries) one router per ticket, behind characterization tests.
- Frontend: extract inline script into modules, as in `linting-and-typing.md`.
- One source of truth for shared constants (base URLs, ports); generate or import, don't copy.
  The APK build should assert its bundled `web/` matches the repo (checksum step in
  `build-app.sh`).

### 2.8 Security-specific
- `bandit`/ruff `S` rules and `osv-scanner` in CI; `gitleaks` pre-commit for secrets.
- Review every place that renders user/book text into HTML (`innerHTML`, Jinja `|safe`).
- Never let an agent run destructive commands unreviewed; the CLAUDE.md safety section exists
  for this reason.

## 3. Measuring whether it is working

Monthly numbers (README cadence), tracked in a standing issue comment: lines in the 5
largest files; `# noqa` count; mypy baseline length; `except: pass` count; vulture 60%
findings; duplicate-code percentage (`jscpd`); open `BUG:` count; test count and coverage
(testing ticket). Direction matters more than level; a rising number identifies the PR to
discuss. DORA's own metrics (change failure rate, time to restore, and the newer *rework
rate*) are overkill for a one-person project, but "how often did a shipped change need an
immediate follow-up fix?" is worth counting from git history: `git log --grep 'BUG'`.

## Sources

- GitClear, AI Copilot Code Quality 2025 (summarised by
  [DevClass](https://www.devclass.com/ai-ml/2025/02/20/ai-is-eroding-code-quality-states-new-in-depth-report/1626250),
  [LeadDev](https://leaddev.com/software-quality/how-ai-generated-code-accelerates-technical-debt))
- Veracode, 2025 GenAI Code Security Report
  ([press release](https://www.veracode.com/press-release/ai-generated-code-poses-major-security-risks-in-nearly-half-of-all-development-tasks-veracode-research-reveals/))
- Spracklen et al., package hallucinations ([arXiv](https://arxiv.org/abs/2406.10279v3))
- DORA 2025 State of AI-assisted Software Development
  ([InfoQ](https://www.infoq.com/news/2025/09/dora-state-of-ai-in-dev-2025))
