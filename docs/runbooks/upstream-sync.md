# Runbook: Upstream Sync

How we keep our fork **`Th3Sp3ct3R/odysseus`** current with the public upstream
**`pewdiepie-archdaemon/odysseus`** — safely, through PRs, never auto-merged.

## Why we track `upstream/dev`

Upstream develops on **`dev`** (active) and keeps **`main`** curated/stable.
Tracking `upstream/dev` surfaces changes early. We bring them into **our**
`main` only via a reviewed PR, so our custom Odysseus work is never overwritten.

## One-time / local setup

```bash
scripts/git/setup-upstream.sh
```
Adds the `upstream` remote (if missing), fetches `upstream/dev`, and prints our
`main` SHA, `upstream/dev` SHA, and how far behind/ahead we are. No merge.

## Manual sync (local)

```bash
scripts/git/sync-upstream.sh                 # dry-run: report only
scripts/git/sync-upstream.sh --create-branch # stage a local sync branch
```
Dry-run shows: commits behind/ahead, changed-file summary, **likely conflict
files**, and **high-risk areas** touched (memory, mcp_servers, routes, services,
src, package files, app.py, .github). `--create-branch` creates
`sync/upstream-dev-YYYYMMDD-HHMM` off `origin/main` and merges `upstream/dev`:
- conflict-free → it commits the merge,
- conflicts → it leaves them **uncommitted** for you to resolve.
It **never pushes**.

## Automated workflow

`.github/workflows/upstream-sync.yml` runs on **workflow_dispatch** and **daily
(09:00 UTC)**. It:
1. fetches `upstream/dev`;
2. exits cleanly if there are no new commits;
3. otherwise creates/updates branch **`sync/upstream-dev`** in our fork (merge of
   `upstream/dev` onto `main`) and opens a PR into **`main`** titled
   *"Sync upstream dev into fork"*;
4. the PR body lists base/head SHA, commit count, changed files, and high-risk
   warnings;
5. if the merge conflicts, the PR is opened as a **draft** prefixed `[CONFLICTS]`.

It uses only the built-in `GITHUB_TOKEN`, **never auto-merges**, and **never
pushes to upstream**.

## How to review an upstream sync PR

1. Read the PR body — note commit count and high-risk areas.
2. Check the diff for changes to our customizations (see invariants below).
3. Pull the branch and run the suite: `pytest`.
4. Resolve any conflicts locally (see policy) before merging.
5. Merge only when all invariants pass. Squash or merge-commit per repo norm.

## Conflict resolution policy

- **Ours wins** for our customizations and operational files: anything under our
  custom services/routes, `data/*` (untracked anyway), session/secret files.
- **Theirs considered** for genuine upstream fixes/features — adopt deliberately.
- Resolve locally with `scripts/git/sync-upstream.sh --create-branch`, fix the
  conflicted files, `git add -A && git commit`, then push the branch.
- Never resolve by blindly accepting all of `theirs` (would drop our work).

## Rollback strategy

- The sync lands via PR, so rolling back = `git revert -m 1 <merge_sha>` on
  `main`, or simply close the PR before merge.
- The `sync/upstream-dev` branch is disposable: delete and let the next run
  recreate it.
- `main` is never modified directly by automation, so there is nothing to undo
  there unless a PR was merged.

## Invariants to check before merge

- [ ] **Tests pass** (`pytest`).
- [ ] **Memory baseline** remains accepted and owner-correct.
- [ ] **Memory Tidy** remains **paused**.
- [ ] **Vanta** remains **disabled** unless explicitly enabled.
- [ ] No **`data/memory.json`** committed (it is gitignored under `data/`).
- [ ] No **generated caches** committed (skill registry cache, `data/*`, mp4/mp3).
- [ ] No **secrets** exposed (cookies, sessions, tokens).
- [ ] **Tailscale/Funnel** settings untouched.
