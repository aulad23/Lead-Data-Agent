#!/usr/bin/env bash
# Auto commit + push to GitHub (origin/main) when a Claude Code turn ends with changed files.
# Runs from the Stop hook in .claude/settings.local.json. Never blocks Claude: always exits 0.
# Safety: respects .gitignore, refuses secret-looking files and files over 50 MB, skips while
# a merge/rebase is in progress, and only pushes the main branch.

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
say() { printf '{"systemMessage": "auto-push: %s"}\n' "$1"; exit 0; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
[ -e .git/MERGE_HEAD ] || [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ] && say "skipped, merge/rebase in progress"
[ "$(git branch --show-current)" = "main" ] || say "skipped, not on main"
[ -z "$(git status --porcelain)" ] && exit 0          # nothing changed -> stay silent

git add -A
staged=$(git diff --cached --name-only)
[ -z "$staged" ] && exit 0

bad=$(printf '%s\n' "$staged" | grep -iE '(^|/)(\.env(\..*)?|credentials[^/]*\.json|.*\.(pem|key|p12|pfx)|id_rsa[^/]*|settings\.local\.json)$')
if [ -n "$bad" ]; then
  git reset -q
  say "NOT pushed - secret-looking file staged: $(echo $bad | tr '\n' ' '). Add it to .gitignore."
fi
big=$(git diff --cached --name-only -z | xargs -0 -I{} sh -c '[ -f "{}" ] && [ $(wc -c < "{}") -gt 52428800 ] && echo "{}"')
if [ -n "$big" ]; then
  git reset -q
  say "NOT pushed - file over 50 MB: $(echo $big | tr '\n' ' '). Add it to .gitignore."
fi

n=$(printf '%s\n' "$staged" | wc -l | tr -d ' ')
files=$(printf '%s\n' "$staged" | head -8 | sed 's/^/- /')
[ "$n" -gt 8 ] && files="$files
- ... and $((n - 8)) more"
git commit -q -m "Auto update: $n file(s) changed

$files

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" || say "commit failed"

if git push -q origin main 2>/dev/null; then
  say "committed and pushed $n file(s) to GitHub ($(git rev-parse --short HEAD))"
else
  say "committed locally ($(git rev-parse --short HEAD)) but push failed (offline?) - it will go up with the next push"
fi
