#!/bin/bash
# Commit the given outputs and push them to the branch, robust against a dirty
# worktree and push races. Used by the adult-covers job (sync-meta.yml).
#
#   scripts/git_publish.sh "<commit message>" <path> [<path> ...]
#
# - Files are staged if they exist; directories with `git add -A` (new chunks
#   and deleted old chunks of data/client-v1/ both land).
# - Anything else left modified in the worktree (e.g. a file a newer script
#   version writes but the caller did not list) no longer blocks the rebase:
#   `git pull --rebase --autostash` stashes it and puts it back afterwards.
#   It is listed in the log so a missing path is noticed.
# - Push races: up to PUBLISH_RETRIES attempts (default 5) with fetch + rebase.
#   On a rebase conflict our commit wins for the listed files (-X theirs: in a
#   rebase "theirs" is the commit being replayed = this job's fresh output).
# - Exit 0 when nothing changed or the push landed, exit 1 (with a GitHub
#   ::error annotation) when the push finally failed. No secrets are printed.
set -uo pipefail

msg="${1:?commit message missing}"; shift
[ "$#" -gt 0 ] || { echo "::error::git_publish.sh: no paths given"; exit 1; }
branch="${PUBLISH_BRANCH:-${GITHUB_REF_NAME:-main}}"
retries="${PUBLISH_RETRIES:-5}"
sleep_base="${PUBLISH_SLEEP_SEC:-5}"
remote="${PUBLISH_REMOTE:-origin}"

for p in "$@"; do
  if [ -d "$p" ]; then
    git add -A -- "$p" || { echo "::error::git add -A $p failed"; exit 1; }
  elif [ -e "$p" ]; then
    git add -- "$p" || { echo "::error::git add $p failed"; exit 1; }
  elif git ls-files --error-unmatch -- "$p" >/dev/null 2>&1; then
    git add -u -- "$p" || true   # tracked file deleted on purpose
  fi
done

if git diff --cached --quiet; then
  echo "Keine Änderungen an: $*"
  exit 0
fi

git config user.name >/dev/null 2>&1 || git config user.name "github-actions[bot]"
git config user.email >/dev/null 2>&1 || git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
echo "Übernehme:"; git diff --cached --stat | tail -n 15
git commit -q -m "$msg" || { echo "::error::git commit failed"; exit 1; }

other="$(git status --porcelain --untracked-files=no)"
if [ -n "$other" ]; then
  echo "Hinweis: weitere geänderte Dateien (nicht übernommen, werden per --autostash geparkt):"
  echo "$other"
fi

attempt=1
while [ "$attempt" -le "$retries" ]; do
  if git pull -q --rebase --autostash -X theirs "$remote" "$branch"; then
    if git push -q "$remote" "HEAD:$branch"; then
      echo "Gepusht nach $branch: $(git rev-parse --short HEAD) (Versuch $attempt)"
      exit 0
    fi
    echo "Push abgelehnt (Versuch $attempt/$retries), neuer Versuch …"
  else
    echo "Rebase fehlgeschlagen (Versuch $attempt/$retries), neuer Versuch …"
    git rebase --abort >/dev/null 2>&1 || true
  fi
  sleep $(( sleep_base * attempt ))
  attempt=$(( attempt + 1 ))
done
echo "::error title=Erwachsenen-Cover nicht gespeichert::Push nach $branch nach $retries Versuchen fehlgeschlagen – die Ergebnisse dieses Laufs sind NICHT auf $branch."
exit 1
