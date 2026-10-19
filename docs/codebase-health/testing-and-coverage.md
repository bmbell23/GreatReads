# Testing and code coverage: a working guide

Ticket: GreatReads #386. Audience: an experienced engineer who wants the
modern picture, not a tutorial. Where I cite a source I link it; claims
without a link are general practice or my own judgement.

---

## 1. What coverage actually measures

Coverage answers one question: **which parts of the code ran while the tests
ran?** It says nothing about whether the tests *checked* anything.

| Kind | Unit | Catches | Misses |
|---|---|---|---|
| Statement / line | each executable line | code never run | untaken `if` arms that have no body of their own, short-circuit logic |
| Branch | each edge out of a decision (`if` true/false, loop enter/skip, `except` taken) | the "else I never tested" case | which *condition* inside `a and b or c` mattered |
| Function | each function entered | whole dead functions | everything inside them |
| Condition | each boolean sub-expression evaluated both ways | compound-condition gaps | combinations |
| MC/DC | each condition shown to independently flip the decision | the strictest practical criterion | cost: needs about N+1 tests for N conditions |

Notes:

- **Line vs branch.** `x = a if flag else b` is one line. Line coverage is
  100% after one call; branch coverage tells you the other arm never ran.
  For Python, always turn branch coverage on.
- **MC/DC** (modified condition/decision coverage) comes from avionics
  (DO-178C level A) and automotive safety. No mainstream Python tool measures
  it. Know it exists; do not chase it for a personal library app.
- **Python specifics.** coverage.py measures lines and "arcs" (line-to-line
  transitions). Branch coverage is derived from arcs. It does not do
  condition coverage. See the
  [coverage.py docs on branch coverage](https://coverage.readthedocs.io/en/latest/branch.html).

### What coverage cannot tell you

1. **Assertion quality.** `def test_x(): client.get("/api/books")` gives
   coverage for the whole handler and verifies nothing. Coverage is a
   necessary signal for "untested", never a sufficient one for "tested".
2. **Missing code.** If you forgot to handle an input, there is no line to
   be uncovered. Coverage cannot see absent features or missing validation.
3. **Interaction and data.** A line can run with trivial inputs and still
   break on `None`, empty lists, Unicode, or huge payloads.
4. **Goodhart's law.** Once a percentage is a target, people (and AI agents)
   write tests that touch lines. A high number with weak assertions is worse
   than a lower honest one because it breeds false confidence.
5. **Concurrency, timing, environment.** Races, Docker/network behaviour and
   the Android runtime are invisible to a unit-level number.

The honest framing: **uncovered code is certainly untested; covered code is
only possibly tested.** Mutation testing (section 6) is how you measure the
second half.

---

## 2. Formats and who reads them

| Format | Shape | Produced by | Consumed by |
|---|---|---|---|
| `.coverage` | SQLite DB (coverage.py's native data file) | `coverage run`, `pytest --cov` | only coverage.py itself (`report`, `html`, `lcov`, `xml`, `json`, `combine`, `erase`) |
| JSON | `coverage json` | coverage.py | scripts, custom dashboards, some badges |
| LCOV tracefile | line-oriented text, `coverage.lcov` / `lcov.info` | coverage.py (since 6.3), c8, nyc/Istanbul, gcov via `lcov`, JaCoCo converters, Kover (via plugins) | `genhtml`, Codecov, Coveralls, SonarQube (some langs), VS Code Coverage Gutters, many GitHub Actions |
| Cobertura XML | `coverage.xml` | coverage.py `xml`, JaCoCo converters, Istanbul reporter, Kover (XML is JaCoCo-shaped) | Codecov, Coveralls, SonarQube generic import, Azure DevOps, GitLab MR coverage, `diff-cover`, Jenkins |
| HTML | `htmlcov/` | coverage.py `html`, `genhtml`, c8/nyc | humans |
| Markdown summary | custom | e.g. a step writing to `$GITHUB_STEP_SUMMARY` | humans reading a CI run |

LCOV is the lowest common denominator: trivially diffable, trivially
generated, readable in a text editor. Cobertura is the other lingua franca,
mostly because older CI systems standardised on it.

### 2.1 LCOV tracefile, record by record

Spec: the "TRACEFILE FORMAT" section of the
[`geninfo(1)` man page](https://man.archlinux.org/man/geninfo.1) (part of the
[linux-test-project/lcov](https://github.com/linux-test-project/lcov) tool).

A tiny example for a file with two functions, one `if`:

```
TN:test_matcher
SF:/app/src/matcher.py
FN:3,normalize
FN:9,link_books
FNDA:12,normalize
FNDA:0,link_books
FNF:2
FNH:1
DA:3,12
DA:4,12
DA:5,12
DA:9,0
DA:10,0
LF:5
LH:3
BRDA:4,0,0,12
BRDA:4,0,1,0
BRF:2
BRH:1
end_of_record
```

Line by line:

| Record | Meaning |
|---|---|
| `TN:<name>` | Optional test name. Precedes `SF`. coverage.py writes it empty; `lcov` uses it to label which test produced the data, and merging keeps the names. |
| `SF:<path>` | Start of a per-source-file section. Path style (absolute vs relative) matters when a CI tool maps it back to the repo (see `relative_files` in section 3). |
| `FN:<line>,<name>` | A function definition: starting line and name. (Newer lcov also has `FNL`/`FNA` with an index and an end line; the simple form is still the most common.) |
| `FNDA:<count>,<name>` | Function execution count. `0` means the function was never called. |
| `FNF:<n>` / `FNH:<n>` | Functions found / functions hit (count > 0). |
| `DA:<line>,<count>` | Execution count for one instrumented line. An optional third field is a checksum, which nobody uses. Lines absent from the file are non-executable (comments, blanks, `def` continuation). |
| `LF:<n>` / `LH:<n>` | Lines found (instrumented) / lines hit. Line coverage = `LH/LF`. Here 3/5 = 60%. |
| `BRDA:<line>,<block>,<branch>,<taken>` | One branch edge. `<taken>` is a count, or `-` if the branch was never evaluated (its decision was not reached). Above, the `if` on line 4 went one way 12 times and the other way never. In coverage.py's output the "block" slot carries the destination, a quirk worth knowing when you read it raw. |
| `BRF:<n>` / `BRH:<n>` | Branches found / hit. Branch coverage = `BRH/BRF`, here 1/2 = 50%. |
| `end_of_record` | Closes the section. Files are concatenated, so a tracefile is just many such sections. |

Practical consequences:

- **Merging is concatenation plus summing.** `lcov -a a.info -a b.info -o all.info`
  adds counts per line. This is why a Python LCOV and a JS LCOV can sit in
  one file and one dashboard.
- **Path rewriting is the usual pain.** A report produced inside a container
  (`/app/src/...`) will not match the repo (`greatreads/src/...`) in an
  editor or in Codecov. Fix at generation time with relative files, or with
  `sed` / `[paths]` remapping.
- `lcov --summary coverage.lcov` and `genhtml` give you totals and a browsable
  site without any Python at all.

### 2.2 Cobertura XML in one paragraph

A `<coverage>` root with `line-rate` and `branch-rate` attributes, then
`<packages>` > `<package>` > `<classes>` > `<class filename=...>` containing
`<lines><line number= hits= branch= condition-coverage="50% (1/2)"/>`. The
data is the same as LCOV; the difference is XML and a package/class hierarchy
that fits Java better than Python. Use it only when a consumer demands it
(`diff-cover` accepts it; so does GitLab).

### 2.3 Consumer cheat sheet

- **genhtml** (lcov package): LCOV in, static HTML site out. Offline, no
  account. Good for a second opinion on coverage.py's own HTML.
- **VS Code Coverage Gutters** (`ryanluker.vscode-coverage-gutters`): watches
  `coverage.lcov` / `lcov.info` / `cobertura.xml` and paints covered and
  uncovered lines in the editor gutter. This is the fastest feedback loop.
  VS Code also has a native Test Coverage view that reads results from test
  extensions.
- **Codecov / Coveralls**: upload LCOV or Cobertura from CI, get PR comments,
  patch coverage and trend graphs. They are hosted SaaS; for a private
  Tailscale-only project they are optional.
- **SonarQube**: imports coverage as a generic report and combines it with
  static analysis. Heavy for a solo project.
- **GitHub Actions**: no built-in coverage UI. Common pattern is to write a
  Markdown table to `$GITHUB_STEP_SUMMARY` and/or upload the HTML as an
  artifact.

---

## 3. Tooling for this stack

### 3.1 Python: coverage.py and pytest-cov

[coverage.py](https://coverage.readthedocs.io/) is the engine.
[pytest-cov](https://pytest-cov.readthedocs.io/) is a thin pytest wrapper that
also handles subprocess and xdist plumbing. (There is also
`coverage run -m pytest`, which is equally valid and easier to reason about.)

Everyday commands:

```
pytest --cov=greatreads/src --cov-branch \
       --cov-report=term-missing \
       --cov-report=html \
       --cov-report=lcov:coverage.lcov
```

- `term-missing` prints the uncovered line numbers per file in the terminal.
- `html` writes `htmlcov/index.html`.
- `lcov` writes the tracefile (coverage.py 6.3+ supports it natively; no
  extra converter needed).
- `--cov-fail-under=N` fails the run (exit code 2) below N%.

Configuration belongs in `pyproject.toml` so every invocation agrees
([config reference](https://coverage.readthedocs.io/en/latest/config.html)):

```toml
[tool.coverage.run]
source = ["greatreads/src"]
branch = true
relative_files = true        # paths in data files are repo-relative
parallel = true              # one data file per process, combine later
patch = ["subprocess"]       # measure spawned python processes (coverage 7.10+)
# dynamic_context = "test_function"   # per-test attribution, see below

[tool.coverage.report]
fail_under = 60              # start where you are, ratchet up
show_missing = true
skip_covered = true
exclude_also = [
  "if __name__ == .__main__.:",
  "if TYPE_CHECKING:",
  "raise NotImplementedError",
  "@(abc\\.)?abstractmethod",
]

[tool.coverage.lcov]
output = "coverage.lcov"

[tool.coverage.html]
directory = "htmlcov"
show_contexts = true
```

Features worth knowing:

- **`source` vs `include`.** `source` also reports files that were *never
  imported* as 0%. Without it, an unimported module silently vanishes from
  the total and the percentage lies upward. This matters a lot for a repo
  with dead modules.
- **`fail_under`.** A blunt floor. Pair it with diff coverage
  (section 5) rather than raising it forever.
- **Contexts.** `dynamic_context = test_function` records *which test* ran
  each line. The HTML report with `show_contexts` then answers "which tests
  cover this line?" and, inversely, shows lines covered only by one
  incidental test. Costs some speed and data-file size.
- **`coverage combine`.** With `parallel = true` each process writes
  `.coverage.<host>.<pid>.<rand>`. `coverage combine` merges them into
  `.coverage`; then `coverage report/html/lcov`. This is also how you merge
  results from several test runs or machines. `relative_files` plus a
  `[paths]` section lets a container-produced file map onto repo paths.
- **`# pragma: no cover`** and `exclude_also` remove lines from the
  denominator. Use sparingly and in review: every exclusion is a place a
  reader should be able to defend.
- **Dependency note.** Recent coverage.py versions changed defaults around
  Python 3.12+ monitoring (`sys.monitoring`, the `sysmon` core). Check the
  [changelog](https://coverage.readthedocs.io/en/latest/changes.html) when
  upgrading if branch numbers shift.

### 3.2 Measuring a running server or subprocess

Our real target is a FastAPI app running under uvicorn in a container, plus
subprocesses (workers, scripts). Three options, cheapest first:

1. **Do not measure the server; test it in-process.** With FastAPI's
   `TestClient` / `httpx.AsyncClient(transport=ASGITransport(app=app))`, the
   app runs inside the pytest process and coverage just works. This should
   cover 95% of the value. See section 7.
2. **Subprocess measurement.** Set `patch = ["subprocess"]` (coverage 7.10+)
   or, on older versions, `COVERAGE_PROCESS_START=pyproject.toml` plus a
   `.pth` file / `sitecustomize` that calls `coverage.process_startup()`.
   With `parallel = true` each child writes its own data file; run
   `coverage combine` afterwards.
3. **Measure a live container** (for end-to-end/Playwright runs). Start
   uvicorn as `coverage run --parallel-mode -m uvicorn app:app`, drive it
   with the E2E tests, then stop it **gracefully** (SIGTERM, not SIGKILL; data
   is written at exit), copy the `.coverage.*` files out, and
   `coverage combine`. Mount the source at the same path or set `[paths]` so
   the files map. This is the technique for finding which routes the
   browser tests really exercise. Do it against a throwaway container and DB,
   never production.

### 3.3 JavaScript: c8, Istanbul/nyc, Playwright

Our front end is mostly **inline `<script>` in HTML pages** (e.g.
`web/reader.html`), plus `web/player.js`. That shapes the tooling:

- **V8 coverage** is built into Node and Chromium: the engine records which
  byte ranges/functions executed. It is precise, needs no instrumentation,
  and is what **c8** wraps for Node
  ([c8](https://github.com/bcoe/c8)). Output: LCOV, text, HTML.
- **Istanbul / nyc** ([istanbuljs](https://istanbul.js.org/)) *instruments*
  (rewrites) source to count statements/branches. Older and more portable
  (works where V8 coverage is not available), but needs a build step and
  source-map care. Prefer V8/c8 for new work.
- **Playwright's Coverage API**: `page.coverage.startJSCoverage()` /
  `stopJSCoverage()` returns, per script, URL, source and V8 function/range
  data; **Chromium only**
  ([API docs](https://playwright.dev/docs/api/class-coverage)). Options:
  `resetOnNavigation` (default true; set false to accumulate across page
  loads) and `reportAnonymousScripts` (default false; includes `eval`/
  `new Function` scripts).
  - **Inline scripts** inside an HTML document come back as entries whose
    URL is the page URL, with byte offsets *into the HTML*. That is exactly
    what we need for `reader.html`, but the offsets refer to the whole
    document, not just the script block.
  - To turn V8 output into LCOV, feed it to
    [`v8-to-istanbul`](https://github.com/istanbuljs/v8-to-istanbul) (what
    c8 uses internally) or `monocart-coverage-reports`, then merge with the
    Python LCOV.
  - Remember what this measures: *what the E2E scenario happened to
    execute*, not unit coverage. Good for finding dead UI code; weak as a
    quality gate.
- **APK caveat.** The Android app bundles a copy of `web/` (see project
  notes), so browser coverage of `web/` says nothing about the shipped
  bundle's version drift.

### 3.4 Android / Kotlin (briefly)

- **[JaCoCo](https://www.jacoco.org/jacoco/)** is the long-standing JVM
  coverage agent (bytecode instrumentation); Gradle's `jacoco` plugin with
  the Android Gradle Plugin's `enableUnitTestCoverage` gives unit-test
  coverage; instrumented (device) tests need `createDebugCoverageReport`.
- **[Kover](https://github.com/Kotlin/kotlinx-kover)** is JetBrains' Kotlin-
  native Gradle plugin; understands inline functions and Kotlin quirks that
  confuse JaCoCo line mapping. Emits HTML and XML (JaCoCo-style).
- For the wrapper app in this repo the WebView shell is thin; the Android
  coverage number would be small and low-value compared with the Python and
  JS logic. Revisit only when Kotlin logic (e.g. `PlaybackService`) grows.

---

## 4. Modern practice: what to aim at

### 4.1 Diff (patch) coverage beats a global percentage

A global percentage is dominated by old code. Add 200 untested lines to a
10,000-line codebase at 85% and the number only slips to about 83%, still
"green". **Patch coverage** asks instead: *of the lines this change added or
modified, what fraction are covered?*

- Locally and in CI: [`diff-cover`](https://github.com/Bachmann1234/diff_cover)
  reads a Cobertura/LCOV report plus `git diff` against a branch.
  ```
  diff-cover coverage.xml --compare-branch=origin/main --fail-under=80
  ```
  Intro: [Open edX on diff-cover](https://openedx.org/blog/diff-cover-test-coverage-git-commits/).
- Hosted equivalents: Codecov's *patch* status vs *project* status
  ([discussion of relative vs absolute thresholds](https://community.codecov.io/t/relative-coverage-increase/353)).
- Philosophy: "leave the world better than you found it" without requiring a
  repo-wide cleanup first. It also fits ticket-by-ticket work: the ticket's
  diff is what must be covered.

### 4.2 Ratcheting

Set `fail_under` to just below the current number, and raise it whenever the
real number rises. The metric can only go up (or at worst stay flat), no
heroics needed. For a codebase starting low (we are starting near the
bottom, with tests pointed at a retired app), this is the realistic path:
baseline first, ratchet second.

### 4.3 What targets are reasonable

Rules of thumb, not laws:

- **Overall**: 70-85% line+branch is a healthy zone for application code.
  Google's published guidance lands in the same area (their internal
  "acceptable 60 / commendable 75 / exemplary 90" tiers are described in
  [Code Coverage at Google](https://research.google/pubs/code-coverage-at-google/)).
  Past about 90% the marginal tests tend to be mock-heavy and brittle.
- **New/changed code (patch)**: 80-90%.
- **Pure logic** (matchers, normalizers, parsers, date maths): aim for
  near-100% with branch coverage, because it is cheap and bugs there are
  silent data corruption.
- **Glue, config, `__main__`, one-off scripts**: do not chase. Exclude
  explicitly, or accept low.
- **Trend matters more than level.** A falling line is the alarm.

### 4.4 Coverage as a dead-code finder

The most useful *negative* use. Code at 0% after a decent suite plus an E2E
pass is a candidate for deletion. Cross-check with:

- [`vulture`](https://github.com/jendrikseipp/vulture) (static unused names),
- route-level logs/telemetry (the `/logs` event log: was an endpoint ever hit?),
- coverage from the live-container run in 3.2(3).

Static says "nothing references it"; coverage says "nothing exercised it".
Agreement of both is strong evidence. This directly supports #122's dead-code
pass: the old `backend/app.py` should show up as a large red block (or, more
precisely, as never imported once `source` is set to `greatreads/src`).

### 4.5 Mutation testing: the check on test quality

Mutation testing changes your code in small ways (flip `<` to `<=`, `+` to
`-`, drop a `return`), reruns the tests, and counts how many mutants the
suite **kills**. A surviving mutant is a behaviour change no test noticed,
which is the exact thing line coverage cannot see.

- **[mutmut](https://pypi.org/project/mutmut/)**: the most usable Python
  option. Version 3 records progress incrementally, runs only the tests
  relevant to a mutant, has a terminal UI, and requires `fork` (Linux/WSL).
  One caveat from its docs: mutmut 3 mutates code inside functions, not
  module-level statements.
- **[cosmic-ray](https://github.com/sixty-north/cosmic-ray)**: more
  configurable and distributable (session DB, multiple executors),
  heavier to set up.
- **Use it narrowly.** Run on the pure-logic modules (matcher/normalizer,
  scoring, date parsing), not the whole app; runtimes multiply by the
  number of mutants. A mutation score is a periodic audit, not a per-PR gate.
- JS equivalent: [StrykerJS](https://stryker-mutator.io/).

### 4.6 Test pyramid vs testing trophy

- **Pyramid** (Cohn / Fowler's
  [practical test pyramid](https://martinfowler.com/articles/practical-test-pyramid.html)):
  many fast unit tests, fewer service/integration tests, very few UI/E2E.
  Rationale: speed and isolation.
- **Trophy** ([Kent C. Dodds](https://kentcdodds.com/blog/the-testing-trophy-and-testing-classifications),
  from Guillermo Rauch's "write tests, not too many, mostly integration"):
  static analysis at the base, a small unit layer, a **thick integration
  layer**, a thin E2E top. Rationale: integration tests give the best
  confidence per line written, and mocking everything makes unit tests
  pass while the system is broken.

For GreatReads the trophy fits better. Most behaviour lives in FastAPI
routes talking to SQLite; an in-process `TestClient` against a temp DB is
both fast *and* realistic, so the "integration" layer is cheap here. Keep
true unit tests for the pure functions (matcher, normalizer), and a few
Playwright smoke tests for the HTML pages.

---

## 5. Testing FastAPI in practice

Official guides:
[Testing](https://fastapi.tiangolo.com/tutorial/testing/),
[Async tests](https://fastapi.tiangolo.com/advanced/async-tests/),
[Testing dependencies with overrides](https://fastapi.tiangolo.com/advanced/testing-dependencies/).

**Sync tests: `TestClient`** (Starlette, httpx-based):

```python
from fastapi.testclient import TestClient

def test_health(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

**Async tests: `httpx.AsyncClient` with `ASGITransport`**, needed when the
test itself must `await` (e.g. to inspect the DB):

```python
import httpx, pytest

@pytest.mark.anyio
async def test_catalog(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/catalog")
    assert r.status_code == 200
```

Note `AsyncClient` does not run lifespan events by itself; wrap with
`asgi-lifespan`'s `LifespanManager` if startup hooks matter.

**Dependency overrides**: swap real dependencies (DB session, auth, outbound
Calibre/ABS clients) for test doubles without patching:

```python
app.dependency_overrides[get_db] = lambda: test_db
# always clean up:
app.dependency_overrides.clear()
```

**A temp SQLite DB, never prod.** Pattern for fixtures (conftest.py):

```python
@pytest.fixture
def db_path(tmp_path, monkeypatch):
    src = Path("greatreads/tests/fixtures/seed.db")   # tiny, committed
    dst = tmp_path / "greatreads.db"
    shutil.copy(src, dst)                              # or build schema fresh
    monkeypatch.setenv("GREATREADS_DB", str(dst))
    return dst
```

Rules we want to keep:

1. The suite must **fail loudly if the DB path points at `greatreads/data/`**.
   A guard assertion in `conftest.py` is cheap insurance given the
   Jan-2026 incident.
2. Prefer a **small hand-built seed DB** (or schema + factory functions) over
   a copy of the real one: faster, deterministic, no personal data.
3. Every test gets its own `tmp_path` DB. SQLite is cheap, and isolation
   removes order-dependence.
4. Stub the outside world (Calibre :8083, ABS :13378, Libby) at the HTTP
   boundary (`respx` for httpx, or dependency overrides), not by patching
   internals.
5. Assert on **behaviour** (status, JSON shape, DB rows afterward), not on
   which helper was called.

### 5.1 Property-based testing (Hypothesis) for matcher/normalizer code

Example-based tests encode the cases you thought of. For code like "link an
ebook and an audiobook when co-authors are listed in a different order"
(bug #383) the bug lives in the cases you did *not* think of.
[Hypothesis](https://hypothesis.readthedocs.io/) generates inputs and
**shrinks** failures to a minimal counterexample.

Properties that fit this codebase:

- **Idempotence**: `normalize(normalize(s)) == normalize(s)`.
- **Order-invariance**: `match(authors) == match(shuffled(authors))`. This is
  literally bug #383 expressed as a law.
- **Symmetry**: `same_book(a, b) == same_book(b, a)`.
- **Invariants**: output never contains leading/trailing whitespace; never
  raises on any Unicode string; length bounds.
- **Round trips**: `parse(format(x)) == x`.

```python
from hypothesis import given, strategies as st

names = st.text(min_size=1, max_size=30)

@given(st.lists(names, min_size=1, max_size=5), st.randoms())
def test_author_match_order_independent(authors, rnd):
    shuffled = authors[:]
    rnd.shuffle(shuffled)
    assert match_authors(authors, shuffled)
```

Hypothesis also saves failing examples in `.hypothesis/` (gitignore it) and
you can pin regressions with `@example(...)`. Coverage and Hypothesis
combine well: properties explore, coverage shows what they reached.

---

## 6. Why this matters more in an AI-written codebase

Most of GreatReads is written with an AI agent. That changes the economics:

1. **Tests are the spec.** A human holds intent in their head; an agent
   holds only what is in context. A failing test is the one piece of intent
   that survives across sessions and across rewrites. Without it, the next
   session "fixes" a thing by silently re-breaking an earlier fix. (Bug #383
   was precisely a regression-shaped hole.)
2. **Agents change code quickly and confidently.** Volume of change per day
   is higher than a solo human's, so the safety net has to be automatic and
   fast; manual re-verification does not scale.
3. **Agents also write tests, and can game metrics.** Asked to "raise
   coverage", a model will happily emit assertion-free tests or mock the
   function under test. Therefore pair coverage with (a) review of
   assertions, (b) mutation testing on key modules, and (c) a rule that a
   bug-fix ticket starts with a failing test that reproduces the bug.
4. **Dead code accumulates.** Agents tend to add rather than remove,
   leaving retired paths (`backend/app.py` is the live example) that look
   plausible and mislead the next session. Coverage plus `vulture` makes
   that visible.
5. **Coverage as a prompt.** The `term-missing` list and the LCOV file are
   machine-readable "what is not tested" lists an agent can be pointed at.
   Aim it at *risky uncovered branches*, not at the total.
6. **Be honest about verification.** This repo's rule is "never claim
   complete without evidence". A passing suite with a coverage report *is*
   that evidence; "I think it works" is not.

---

## 7. How to read our report

> Placeholder command: `./scripts/coverage.sh` (being written under #386;
> it runs pytest against `greatreads/src` with a throwaway DB, then writes
> the files below). Check the script itself for the exact flags.

Expected outputs at the repo root (names may differ slightly):

| File | What it is |
|---|---|
| `.coverage` | coverage.py's SQLite data file |
| `coverage.lcov` | LCOV tracefile, for editors and other tools |
| `htmlcov/index.html` | browsable HTML report |

### 7.1 The terminal summary

```
Name                         Stmts   Miss Branch BrPart  Cover   Missing
greatreads/src/matcher.py       58      4     20      3    90%   41-44, 71->73
TOTAL                         2400    900    700    120    62%
```

- **Stmts / Miss**: executable statements and how many never ran.
- **Branch / BrPart**: branch edges, and how many decisions were only
  *partly* taken (one arm never seen).
- **Cover**: combined statement+branch percentage (coverage.py's formula
  weights both, so it is not LCOV's `LH/LF`).
- **Missing**: `41-44` are unrun lines; `71->73` means the jump from line 71
  to 73 never happened (usually an `if` with no `else`, or a loop never
  skipped).

### 7.2 The HTML report

1. Open `htmlcov/index.html` (from the server over Tailscale you can serve it:
   `python3 -m http.server -d htmlcov 8099`, then browse to
   `http://100.69.184.113:8099`; check the port inventory first, or just
   `scp` the folder).
2. The index table is sortable. Click **Cover** or **missing** to find the
   worst files; start with files that are big *and* important, not just red.
3. Click a file. Colours: **green** ran, **red** never ran (a "miss"),
   **yellow** a branch only partly taken (hover shows which arm), and grey
   is excluded/non-executable.
4. Keyboard: `n`/`m`/`x`/`p`/`b` jump to next missed/excluded/partial lines,
   `?` lists shortcuts.
5. If contexts are enabled, the little dropdown on a line lists which tests
   ran it. A line covered by exactly one far-off test is a hint the
   unit-level tests are thin.

### 7.3 The LCOV file in VS Code

1. Install **Coverage Gutters** (`ryanluker.vscode-coverage-gutters`).
2. Make sure `coverage.lcov` is in the workspace (the extension searches for
   `lcov.info`, `coverage.lcov`, `cobertura.xml`, etc.).
3. Command palette > **Coverage Gutters: Display Coverage** (or **Watch**,
   which re-reads when the file changes). Covered/uncovered lines get
   gutter markers; **Toggle Coverage** hides them; the status bar can show
   the percentage for the current file.
4. If nothing shows, the culprit is almost always **path mismatch**: the
   `SF:` paths do not resolve against the workspace folder. Open the file
   and look at the first `SF:` line; it should be repo-relative
   (`greatreads/src/...`). Fix with `relative_files = true` or a `[paths]`
   remap.
5. Reading the raw file: search for `DA:<line>,0` to find unrun lines, or
   `BRDA:.*,0$` / `,-$` for untaken or unreached branches. `lcov --summary
   coverage.lcov` gives totals; `genhtml coverage.lcov -o lcov-html` makes
   an independent HTML view.

### 7.4 A reading discipline

1. Look at the **total** only to record the baseline (post it in #386).
2. Then ignore it. Sort by "important and red": routes, matching/linking,
   DB writes, anything touching Calibre/ABS state.
3. For each red region ask: *is this dead (delete), risky (test), or glue
   (exclude)?* Those three outcomes are the whole point of the report.
4. On every future ticket, run `diff-cover` against `origin/main` and make
   sure the lines you changed are green.
5. Once a quarter, run `mutmut` on the matcher/normalizer to check the
   tests would actually notice a change.

---

## 8. Suggested adoption path for GreatReads

1. Point the suite at `greatreads/src` with a temp DB; delete or port tests
   that import the retired `backend/app.py` (with #122).
2. Record the baseline (statement and branch), set `fail_under` just below it.
3. Add Hypothesis tests for the matcher/normalizer; add a regression test
   for #383 as a property.
4. Add `diff-cover` to the "move to In Review" checklist.
5. One-off live-container + Playwright coverage run to find dead routes and
   dead inline JS.
6. Optional later: mutmut on the pure-logic modules; Kover only if Kotlin
   logic grows.

---

## Sources

- coverage.py: [configuration](https://coverage.readthedocs.io/en/latest/config.html), [branch coverage](https://coverage.readthedocs.io/en/latest/branch.html), [changelog](https://coverage.readthedocs.io/en/latest/changes.html)
- LCOV tracefile format: [geninfo(1)](https://man.archlinux.org/man/geninfo.1), [linux-test-project/lcov](https://github.com/linux-test-project/lcov)
- Playwright: [Coverage API](https://playwright.dev/docs/api/class-coverage)
- Patch coverage: [diff-cover (Open edX)](https://openedx.org/blog/diff-cover-test-coverage-git-commits/), [Codecov community](https://community.codecov.io/t/relative-coverage-increase/353), [diff_cover repo](https://github.com/Bachmann1234/diff_cover)
- Mutation testing: [mutmut on PyPI](https://pypi.org/project/mutmut/), [cosmic-ray](https://github.com/sixty-north/cosmic-ray)
- Testing strategy: [Practical Test Pyramid](https://martinfowler.com/articles/practical-test-pyramid.html), [Testing Trophy](https://kentcdodds.com/blog/the-testing-trophy-and-testing-classifications), [Code Coverage at Google](https://research.google/pubs/code-coverage-at-google/)
- FastAPI: [testing](https://fastapi.tiangolo.com/tutorial/testing/), [async tests](https://fastapi.tiangolo.com/advanced/async-tests/), [dependency overrides](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
- Hypothesis: [docs](https://hypothesis.readthedocs.io/)
- JS / Android: [c8](https://github.com/bcoe/c8), [Istanbul](https://istanbul.js.org/), [v8-to-istanbul](https://github.com/istanbuljs/v8-to-istanbul), [JaCoCo](https://www.jacoco.org/jacoco/), [Kover](https://github.com/Kotlin/kotlinx-kover)
