# Linting, formatting, typing, pre-commit

Today none of this is enforced. `pyproject.toml` lists black, isort, mypy, ruff and sets
`[tool.mypy] python_version = "2.1.7"` (the app version in the wrong field), so even mypy would
not run correctly. This doc is the adoption plan: **baseline first, then ratchet.**

## 1. Ruff replaces black + isort + flake8 (+ pyupgrade)

One binary, one config: `ruff check` (lint, with autofix) and `ruff format` (Black-compatible
formatter). isort becomes the `I` rules. See
[pydevtools: replace Black, isort, flake8, pyupgrade with Ruff](https://pydevtools.com/handbook/how-to/how-to-replace-black-isort-flake8-pyupgrade-with-ruff/)
and [Ruff vs flake8](https://pydevtools.com/handbook/explanation/ruff-vs-flake8.md).
Remove black and isort from deps and their `[tool.*]` sections when switching.

### Starting config (`greatreads/pyproject.toml`)

```toml
[tool.ruff]
line-length = 100
target-version = "py311"          # bump with the Python policy
extend-exclude = ["data", "migrations/versions"]

[tool.ruff.lint]
# Start narrow: bugs and safety, not style. Widen one family per quarter (ratchet).
select = [
  "E4", "E7", "E9", "F",   # pyflakes + syntax-level pycodestyle (undefined names, unused imports)
  "B",                     # bugbear: real bug patterns (mutable defaults, bare raise in except)
  "S110", "S112",          # try/except/pass and try/except/continue (silent swallowing)
  "BLE001",                # blind `except Exception`
  "ASYNC",                 # blocking calls inside async def (cf. #363)
  "I",                     # import sorting
  "UP",                    # pyupgrade, modernises syntax for target-version
  "C4", "SIM",             # cheap simplifications
  "RUF",
]
ignore = ["E501"]          # the formatter owns line length

[tool.ruff.lint.per-file-ignores]
"backend/**" = ["B", "BLE001"]            # retired app, kept for rollback only
"**/tests/**" = ["S", "B011"]

[tool.ruff.lint.isort]
known-first-party = ["greatreads"]

[tool.ruff.format]
quote-style = "double"
```

Why these: `ASYNC` catches `requests.get`/`time.sleep`/sync file I/O inside coroutines, the
exact class of bug behind #363. `S110/S112/BLE001` target the silent-exception habit of
AI-written code (see `ai-generated-code.md`). `B` and `F` catch the real defects. Style
families (`N`, `D`, `ANN`, `PL*`) come later or never.

### Adopting on a legacy repo: baseline, don't big-bang

1. **Format commit, alone.** `ruff format .` changes every file once. Land it as a
   whitespace-only commit and list it in `.git-blame-ignore-revs` (`git config
   blame.ignoreRevsFile .git-blame-ignore-revs`) so history stays readable. Never mix with
   behaviour changes. Note: this touches `ereader_api.py` and the Python parts of the repo, not
   `web/*.html` (ruff does not format HTML/JS).
2. **Lint baseline with `--add-noqa`.** Ruff can write the exact `# noqa: CODE` on every existing
   violation ([guide](https://pydevtools.com/handbook/how-to/how-to-replace-black-isort-flake8-pyupgrade-with-ruff/)):

   ```bash
   cd greatreads && ruff check --add-noqa src      # records today's debt inline
   git diff --stat                                  # review: should be comments only
   ```

   After this, `ruff check` is green and **any new violation fails**. The debt is greppable:
   `rg "# noqa" -c`. That count is the ratchet metric. Reduce it opportunistically: when you
   touch a function, remove the noqa and fix it.
3. **Prefer auto-fixes first.** Before step 2 run `ruff check --fix --select I,UP,F401,C4` as its
   own reviewed commit (safe, mechanical). Do *not* use `--unsafe-fixes` wholesale.
4. **Per-file ignores for big hotspots** instead of 500 inline noqas: for example
   `"src/greatreads/ereader_api.py" = ["BLE001"]` with a comment and a ticket number; delete the
   entry when the file is split.
5. **CI gate on the diff** as an alternative to noqa-everywhere: lint only changed lines/files so
   untouched legacy code never blocks:

   ```bash
   git diff --name-only --diff-filter=d origin/main... -- '*.py' | xargs -r ruff check
   ```

6. **Ratchet**: each quarter add one rule family to `select` (`PLE`, `PLW`, `PERF`, `PT`,
   `TRY`, `A`, `DTZ`), run `--add-noqa`, commit the baseline, then burn down.

Rule: **no new `# noqa` without a code and, for non-baseline lines, a reason.** Enforce
with `ruff check --select PGH004,RUF100` (blanket noqa, unused noqa).

## 2. Is pylint still worth it?

Mostly no, with one exception. Ruff reimplements a growing subset of pylint (`PL*` rules) with
autofix and is 10 to 100 times faster, but pylint still does deeper inference and has
`duplicate-code` (R0801) which ruff lacks
([pydevtools: pylint](https://pydevtools.com/handbook/reference/pylint/)). For a vibe-coded repo
with copy-pasted blocks that exception matters.

Recommendation:
- **Ruff is the gate** (pre-commit + CI, blocking).
- **Pylint is a monthly report, non-blocking, narrow**: duplication and a few design checks only.

```bash
uvx pylint greatreads/src/greatreads --disable=all \
  --enable=duplicate-code,too-many-branches,too-many-locals,too-many-statements,cyclic-import \
  --min-similarity-lines=8 --ignore-imports=y --ignore-signatures=y --exit-zero > /tmp/pylint.txt
```

Feed the output into `dead-code.md`/architecture tickets. For language-agnostic duplication,
including the JS in `web/*.html`, `jscpd` is better:
`npx jscpd web greatreads/src --min-lines 10 --min-tokens 70 --reporters console`.
Do not run full pylint as a gate; the noise-to-signal ratio on this repo would be ruinous.

## 3. Type checking: pick mypy now, keep the option open

Landscape in 2026
([pydevtools comparison](https://pydevtools.com/handbook/explanation/how-do-mypy-pyright-and-ty-compare/),
[Pyrefly 1.0 as mypy upgrade](https://pydevtools.com/blog/pyrefly-1-0-is-the-obvious-mypy-upgrade/)):
pyright is the most spec-conformant (about 96% on the typing conformance suite); Pyrefly 1.0
(Meta) about 92% and fast; ty (Astral) fast but younger; mypy (about 60% on the same suite) is
the incumbent with the best plugin ecosystem, and mypy 2.0 added parallel checking
(`--num-workers`). Figures are from the linked secondary sources, not verified against the
conformance repo.

Our situation: FastAPI + Pydantic v2 + SQLAlchemy 2 (all have good type support, mypy plugins
exist for Pydantic and SQLAlchemy 2 needs none), and a team of AI agents that benefit from a
checker with low config. Recommendation:

- **Now: mypy**, because it is already in `dev` deps, has mature baseline tooling
  ([mypy-baseline](https://pypi.org/project/mypy-baseline)), and the Pydantic plugin.
- **Evaluate pyright or Pyrefly in a quarter** as the editor/agent feedback checker. They also
  support baselines; running two checkers in CI is not worth it. Pick one gate.
- Fix the config first:

```toml
[tool.mypy]
python_version = "3.11"
files = ["src/greatreads"]
plugins = ["pydantic.mypy"]
warn_unused_configs = true
warn_redundant_casts = true
warn_unused_ignores = true
no_implicit_optional = true
check_untyped_defs = true        # the big one: type-checks bodies of unannotated functions
disallow_untyped_defs = false    # turn on per-module as modules are cleaned

[[tool.mypy.overrides]]
module = ["apscheduler.*", "passlib.*", "jose.*"]
ignore_missing_imports = true

# Strict where code is new or clean; grow this list.
[[tool.mypy.overrides]]
module = ["greatreads.services.*"]   # example; use real clean modules
disallow_untyped_defs = true
```

### Baseline and ratchet for mypy

```bash
cd greatreads
mypy src | mypy-baseline sync        # record today's errors in mypy-baseline.txt
mypy src | mypy-baseline filter      # CI: fails only on NEW errors
```

Commit `mypy-baseline.txt`. The line count of that file is the ratchet metric; `mypy-baseline
sync` after fixes shrinks it. Policy: PRs may shrink it, never grow it.

Practical expectations: a 4.4k-line `ereader_api.py` written without types will produce hundreds
of errors on `check_untyped_defs`. Do not fix them in bulk. Annotate return types and params on
**new/touched** functions, and prefer Pydantic response models on endpoints (which doubles as
API documentation and input validation, a real quality win for AI-written handlers).

## 4. pre-commit

There is a git hook directory already (`scripts/git-hooks/pre-commit`, wired via
`core.hooksPath`, which syncs `version.txt`). The `pre-commit` framework would normally set
`core.hooksPath` itself and refuse. Two options:

- **Preferred**: keep our hook as the single entry point and have it call `pre-commit run`
  (or just `ruff` directly) before the version sync. No framework install needed for agents.
- Or migrate the version sync into a `local` pre-commit hook and drop the custom directory.

`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.14.0          # pin; let Renovate bump (pre-commit manager is built in)
    hooks:
      - id: ruff-check
        args: [--fix, --exit-non-zero-on-fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: check-added-large-files
        args: [--maxkb=500]
      - id: check-merge-conflict
      - id: detect-private-key
      - id: end-of-file-fixer
      - id: check-toml
  - repo: local
    hooks:
      - id: mypy-baseline
        name: mypy (new errors only)
        entry: bash -c 'cd greatreads && mypy src | mypy-baseline filter'
        language: system
        pass_filenames: false
        types: [python]
        stages: [pre-push]        # slower; run on push and in CI, not every commit
```

Check the `rev` values exist before copying; they are placeholders for "latest at adoption".
Hooks catch things locally, but agents can bypass with `--no-verify`, so **CI is the real gate**.
There is no `.github/` today. Add `.github/workflows/ci.yml` running: `ruff check`, `ruff
format --check`, mypy-baseline filter, pytest (`backend/tests` and any new `greatreads` tests),
`uv audit`. Make it a required check on `main` branch protection.

## 5. JavaScript and HTML

The frontend is inline JS inside HTML (`web/reader.html` 8.8k lines) plus `player.js`. That
shapes the tool choice.

- **ESLint + `eslint-plugin-html`** lints `<script>` blocks in HTML and emulates browser global
  scope across multiple script tags
  ([plugin](https://www.npmjs.com/package/eslint-plugin-html)). **Biome** is faster and
  formats too, but its HTML support is still experimental/partial (Vue/Svelte/Astro first; see
  [Biome roadmap 2026](https://biomejs.dev/blog/roadmap-2026/) and
  [comparison](https://reintech.io/blog/eslint-vs-biome-javascript-linting-comparison-2026)).
  Re-check Biome's HTML status before adopting; if it handles plain `.html` by then, it wins on
  simplicity.
- **Recommendation: ESLint (flat config) with `eslint-plugin-html` for `web/*.html`; Biome (or
  ESLint) for the real `.js` files.** Dev-only `package.json` under `web/` or repo root; never
  shipped (the APK bundles `web/`, so keep `node_modules` out of its asset list).
- **Highest-value long-term fix**: extract inline scripts into `web/js/*.js` ES modules. Then
  every JS tool (eslint, biome, knip, coverage, `node --check`) works with no plugin tricks, and
  the file stops being 8.8k lines. Do it incrementally: move one feature's functions per ticket,
  run in the browser and APK, delete the inline copy. This must account for the APK-bundled
  copy of `web/` (rebuild needed, see memory note on APK assets).
- Start with rules that find bugs only, as a baseline:

```js
// eslint.config.js
import html from "eslint-plugin-html";
export default [{
  files: ["web/**/*.{html,js}"],
  plugins: { html },
  languageOptions: { ecmaVersion: 2022, sourceType: "script",
    globals: { window: "readonly", document: "readonly", fetch: "readonly", localStorage: "readonly" } },
  rules: { "no-undef": "error", "no-unused-vars": ["warn", { args: "none" }],
           "no-redeclare": "error", "no-dupe-keys": "error", "no-empty": "warn",
           "no-unreachable": "error", "eqeqeq": "warn" }
}];
```

`no-undef` and `no-redeclare` are gold on 8.8k lines of globals: they find typos, calls to
functions that were renamed, and duplicate function definitions where the second silently
overrides the first (a classic AI-edit artifact). Baseline with
`npx eslint web -f json > eslint-baseline.json` and fail CI on any rule count increase, or use
ESLint's own bulk suppressions (`--suppress-all`, ESLint 9.24+; verify in your version).
- Cheap extra gates today, no tooling: `node --check` on extracted scripts, and
  `htmlhint`/`html-validate` for duplicate IDs (`id-unique`), which AI-edited HTML accumulates.

## Adoption order

1. Fix `[tool.mypy] python_version`; `requires-python`; remove black/isort.
2. Ruff config, format commit, `--add-noqa` baseline, pre-commit, CI job.
3. mypy config + baseline + CI.
4. ESLint with `no-undef`/`no-redeclare` baseline on `web/`.
5. Quarterly: widen one ruff family; shrink both baselines; monthly pylint/jscpd duplication
   report.
