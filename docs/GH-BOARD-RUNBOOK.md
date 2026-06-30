# GitHub Project board — ops runbook

How to read and drive the **GreatReads** board (Project **#3**) from the CLI.
This is the *new* Projects experience, driven by `gh project` — **not** classic
project cards.

## The "errors" you may have seen are a red herring
`gh issue view <n>` prints:
> GraphQL: Projects (classic) is being deprecated … (repository.issue.projectCards)
That's `gh` trying to fetch *classic* cards, which we don't use. It is **noise,
not a failure** — the board is fine. Avoid it by reading issues with explicit
fields (`gh issue view <n> --json number,title,state,body,labels`) and by using
`gh project …` for board state.

## IDs (stable — captured 2026-06-30)
- Owner: `bmbell23`  ·  Project number: `3`
- Project id: `PVT_kwHOBiWWa84BawXC`
- **Status** field id: `PVTSSF_lAHOBiWWa84BawXCzhVlotc`
  - Scoping `f75ad846`
  - Ready to Implement `61e4505c`
  - In progress `47fc9ee4`
  - In Review `52062cd2`
  - Done `98236657`

Re-derive any of these if they ever drift:
```bash
gh project list --owner bmbell23
gh project field-list 3 --owner bmbell23 --format json   # field + option ids
```

## Read the board
```bash
# All items with their status + item id (the item id is what you edit):
gh project item-list 3 --owner bmbell23 --format json --limit 100 \
  | python3 -c "import sys,json;[print(i.get('status'),(i.get('content') or {}).get('number'),i['id']) for i in json.load(sys.stdin)['items'] if (i.get('content') or {}).get('number')]"
```

## Move a ticket's Status
Needs the **item id** (from item-list above), not the issue number.
```bash
gh project item-edit \
  --id <PVTI_…item id> \
  --project-id PVT_kwHOBiWWa84BawXC \
  --field-id  PVTSSF_lAHOBiWWa84BawXCzhVlotc \
  --single-select-option-id <option id from the list above>
```

## Add an issue that isn't on the board yet
New issues usually auto-add, but if one is missing:
```bash
gh project item-add 3 --owner bmbell23 --url https://github.com/bmbell23/GreatReads/issues/<n>
```

## Status policy (confirmed by the user 2026-06-30 — overrides CLAUDE.md timing)
- I move freely between **Scoping → Ready to Implement → In progress**.
- **In Review:** move here **only when the user explicitly says a ticket is
  done.** Committing code is *not* enough — committed work stays **In progress**
  until the user blesses it.
- **Done:** **never** set by me. The user alone marks Done.
- Auth: the `gh` token needs the `project` scope (already present).
