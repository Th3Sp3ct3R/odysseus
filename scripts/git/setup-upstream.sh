#!/usr/bin/env bash
# setup-upstream.sh — add the upstream remote and report fork/upstream distance.
#
# Safe by design: adds the 'upstream' remote if missing, fetches it, and prints
# where our fork's main sits relative to upstream/dev. It NEVER merges, never
# pushes, never modifies tracked files.
#
# Usage:  scripts/git/setup-upstream.sh
set -euo pipefail

UPSTREAM_URL="https://github.com/pewdiepie-archdaemon/odysseus.git"
UPSTREAM_BRANCH="dev"          # upstream's active development branch
FORK_BRANCH="main"             # our curated fork branch

cd "$(git rev-parse --show-toplevel)"

# 1. Add upstream remote if missing (idempotent).
if git remote get-url upstream >/dev/null 2>&1; then
  echo "✓ upstream remote already present: $(git remote get-url upstream)"
else
  echo "+ adding upstream remote: $UPSTREAM_URL"
  git remote add upstream "$UPSTREAM_URL"
fi

# 2. Fetch upstream + origin safely (prune stale, tags off to stay lean).
echo "→ fetching upstream ($UPSTREAM_BRANCH) ..."
if ! git fetch --prune --no-tags upstream "$UPSTREAM_BRANCH"; then
  echo "✗ could not fetch upstream/$UPSTREAM_BRANCH (network or access?)." >&2
  exit 1
fi
git fetch --prune --no-tags origin "$FORK_BRANCH" >/dev/null 2>&1 || true

# 3. Report current positions + distance. No merge.
fork_ref="origin/${FORK_BRANCH}"
up_ref="upstream/${UPSTREAM_BRANCH}"
git rev-parse --verify "$fork_ref" >/dev/null 2>&1 || fork_ref="$FORK_BRANCH"

behind=$(git rev-list --count "${fork_ref}..${up_ref}" 2>/dev/null || echo "?")
ahead=$(git rev-list --count "${up_ref}..${fork_ref}" 2>/dev/null || echo "?")

echo
echo "──────────────────────────────────────────────"
echo " fork ${fork_ref} : $(git rev-parse --short "$fork_ref")"
echo " upstream ${up_ref}     : $(git rev-parse --short "$up_ref")"
echo " our fork is BEHIND upstream/${UPSTREAM_BRANCH} by : ${behind} commit(s)"
echo " our fork is AHEAD of upstream/${UPSTREAM_BRANCH} by : ${ahead} commit(s)"
echo "──────────────────────────────────────────────"
echo "No merge performed. Run scripts/git/sync-upstream.sh to preview a sync."
