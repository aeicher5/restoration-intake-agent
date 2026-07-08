#!/usr/bin/env bash
# Create one git worktree + branch per agent lane, ready for parallel sessions.
#
# Run from the repo root after the baseline commit. Edit LANES below (or pass
# "dir:branch" pairs as arguments). If a .env exists in the repo root it is
# copied into each worktree (it must already be gitignored — the script
# refuses to proceed otherwise).
#
#   ./playbook/setup-worktrees.sh
#   ./playbook/setup-worktrees.sh ../my-web:feature/web ../my-evals:feature/evals

set -euo pipefail

LANES=(
  "../aa-web:feature/web-admin"
  "../aa-evals:feature/evals"
  "../aa-hardening:feature/hardening"
)
[ "$#" -gt 0 ] && LANES=("$@")

git rev-parse --git-dir >/dev/null || { echo "run from inside the repo" >&2; exit 1; }

if [ -f .env ]; then
  git check-ignore -q .env || { echo "refusing: .env exists but is not gitignored" >&2; exit 1; }
fi

for lane in "${LANES[@]}"; do
  dir="${lane%%:*}"
  branch="${lane##*:}"
  git worktree add "$dir" -b "$branch"
  if [ -f .env ]; then
    cp .env "$dir/.env"
    git -C "$dir" check-ignore -q .env || { echo "refusing: .env not ignored in $dir" >&2; exit 1; }
  fi
done

echo
git worktree list
echo
echo "Next: open one terminal per worktree, start an agent session in each,"
echo "and paste its brief (see playbook/BRIEF-TEMPLATE.md)."
