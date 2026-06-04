#!/usr/bin/env bash
# sync-upstream.sh — preview (and optionally stage) an upstream/dev -> fork sync.
#
# DRY-RUN BY DEFAULT: fetches, compares origin/main against upstream/dev, and
# prints commits behind/ahead, a changed-file summary, likely conflict files,
# and which high-risk areas are touched. It does NOT branch/merge/commit/push
# unless you pass --create-branch, and even then it NEVER pushes.
#
# Usage:
#   scripts/git/sync-upstream.sh                 # dry-run report only
#   scripts/git/sync-upstream.sh --create-branch # also create sync branch + merge
set -euo pipefail

UPSTREAM_BRANCH="dev"
FORK_BRANCH="main"
CREATE_BRANCH=0
[ "${1:-}" = "--create-branch" ] && CREATE_BRANCH=1

cd "$(git rev-parse --show-toplevel)"

if ! git remote get-url upstream >/dev/null 2>&1; then
  echo "✗ no 'upstream' remote. Run scripts/git/setup-upstream.sh first." >&2
  exit 1
fi

echo "→ fetching origin + upstream ..."
git fetch --prune --no-tags origin "$FORK_BRANCH" >/dev/null 2>&1 || true
git fetch --prune --no-tags upstream "$UPSTREAM_BRANCH"

fork_ref="origin/${FORK_BRANCH}"
git rev-parse --verify "$fork_ref" >/dev/null 2>&1 || fork_ref="$FORK_BRANCH"
up_ref="upstream/${UPSTREAM_BRANCH}"
base=$(git merge-base "$fork_ref" "$up_ref")

behind=$(git rev-list --count "${fork_ref}..${up_ref}")
ahead=$(git rev-list --count "${up_ref}..${fork_ref}")

echo
echo "════════════════ UPSTREAM SYNC PREVIEW (dry-run) ════════════════"
echo " fork     ${fork_ref} = $(git rev-parse --short "$fork_ref")"
echo " upstream ${up_ref}      = $(git rev-parse --short "$up_ref")"
echo " merge-base                  = $(git rev-parse --short "$base")"
echo " behind upstream/${UPSTREAM_BRANCH}: ${behind}   ahead: ${ahead}"
echo "─────────────────────────────────────────────────────────────────"

if [ "$behind" -eq 0 ]; then
  echo "✓ Already up to date with upstream/${UPSTREAM_BRANCH}. Nothing to sync."
  exit 0
fi

echo "Incoming upstream commits (newest first):"
git log --oneline --no-decorate -n 25 "${fork_ref}..${up_ref}" | sed 's/^/   /'

echo
echo "Changed file summary (upstream brings):"
git diff --stat "${base}" "${up_ref}" | tail -40 | sed 's/^/   /'

# Likely conflicts = files changed on BOTH sides since the merge-base.
echo
echo "Likely conflict files (touched by BOTH fork and upstream since base):"
ours=$(git diff --name-only "${base}" "${fork_ref}" | sort)
theirs=$(git diff --name-only "${base}" "${up_ref}" | sort)
conflicts=$(comm -12 <(printf '%s\n' "$ours") <(printf '%s\n' "$theirs") || true)
if [ -n "$conflicts" ]; then printf '%s\n' "$conflicts" | sed 's/^/   ⚠ /'; else echo "   (none detected)"; fi

# High-risk areas.
echo
echo "High-risk areas touched by upstream:"
risk_hit=0
while IFS='|' read -r label pattern; do
  hits=$(git diff --name-only "${base}" "${up_ref}" | grep -E "$pattern" || true)
  if [ -n "$hits" ]; then risk_hit=1; echo "   ⚠ ${label}:"; printf '%s\n' "$hits" | sed 's/^/       /'; fi
done <<'AREAS'
memory|^src/memory|^data/memory
mcp_servers|^mcp_servers/
routes|^routes/
services|^services/
src|^src/
package files|^(package\.json|package-lock\.json|requirements.*\.txt|pyproject\.toml)$
app.py|^app\.py$
github workflows|^\.github/
AREAS
[ "$risk_hit" -eq 0 ] && echo "   (no high-risk areas touched)"
echo "═════════════════════════════════════════════════════════════════"

if [ "$CREATE_BRANCH" -eq 0 ]; then
  echo "Dry-run only. Re-run with --create-branch to stage a local sync branch."
  exit 0
fi

# ---- --create-branch: stage a local sync branch (NEVER pushes) ----
SYNC_BRANCH="sync/upstream-dev-$(date +%Y%m%d-%H%M)"
echo
echo "→ creating local branch ${SYNC_BRANCH} from ${fork_ref} ..."
git branch "$SYNC_BRANCH" "$fork_ref"
git switch "$SYNC_BRANCH"

echo "→ merging ${up_ref} (no auto-commit) ..."
if git merge --no-ff --no-commit "$up_ref"; then
  echo "✓ conflict-free merge — committing."
  git commit --no-edit -m "Merge upstream/${UPSTREAM_BRANCH} into ${SYNC_BRANCH}

upstream base: $(git rev-parse "$base")
upstream head: $(git rev-parse "$up_ref")
commits:       ${behind}"
  echo "✓ committed. Review, then push manually if desired: git push origin ${SYNC_BRANCH}"
else
  echo "⚠ merge has CONFLICTS — left UNCOMMITTED for manual resolution."
  echo "  Conflicted files:"; git diff --name-only --diff-filter=U | sed 's/^/     /'
  echo "  Resolve, then: git add -A && git commit   (or: git merge --abort)"
fi
echo "NOTE: nothing was pushed. Fork only."
