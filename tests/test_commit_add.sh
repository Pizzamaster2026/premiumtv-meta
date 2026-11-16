#!/bin/bash
# Reproduces the status.json staging bug: git add with a missing data/meta.json
# pathspec fails entirely; git add -u then skips the new untracked status.json.
set -euo pipefail
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
cd "$ROOT"
git init -q
git config user.name "test"
git config user.email "test@example.com"
mkdir data
echo '{"v":1}' > data/index.json
echo '{"e":1}' > data/meta-a.json
git add data/index.json data/meta-a.json
git commit -q -m init
echo '{"v":2}' > data/index.json
echo '{"coveragePct":78.8}' > data/status.json
# old recipe (must NOT stage status.json)
git add data/index.json data/meta.json data/meta-*.json data/status.json 2>/dev/null || true
git add -u data/
if git diff --cached --name-only | grep -qx 'data/status.json'; then
  echo 'FAIL  old recipe unexpectedly staged status.json'
  exit 1
fi
echo 'PASS  old recipe misses untracked status.json (bug reproduced)'
git reset -q
# new recipe
git add -u data/
for f in data/index.json data/status.json data/meta.json data/meta-*.json; do
  if [ -e "$f" ]; then
    git add -- "$f"
  fi
done
staged="$(git diff --cached --name-only | sort | tr '\n' ' ')"
echo "staged: $staged"
echo "$staged" | grep -q 'data/status.json' || { echo 'FAIL  new recipe did not stage status.json'; exit 1; }
echo "$staged" | grep -q 'data/index.json' || { echo 'FAIL  new recipe missed index.json'; exit 1; }
echo "$staged" | grep -q 'data/meta.json' && { echo 'FAIL  staged missing meta.json'; exit 1; }
echo 'PASS  new recipe stages status.json and index.json'
echo ALL PASS
