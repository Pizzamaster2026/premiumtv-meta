#!/bin/bash
# Regression for run 44953033386 (adult-covers): the job committed only
# data/adult-tpdb.json while adult_tpdb.py had also rewritten data/status.json;
# `git pull --rebase` then aborted with "You have unstaged changes" and
# continue-on-error hid it, so 196 covers never reached main.
# Local bare repos only, no network.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PUB="$HERE/../scripts/git_publish.sh"
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
export PUBLISH_SLEEP_SEC=0 PUBLISH_BRANCH=main
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; exit 1; }

setup() {   # fresh bare remote "origin" + clone "job" at main (like actions/checkout ref: main)
  cd "$ROOT"; rm -rf "$ROOT/remote.git" "$ROOT/job" "$ROOT/other"
  git init -q --bare -b main "$ROOT/remote.git"
  git clone -q "$ROOT/remote.git" "$ROOT/seed" 2>/dev/null
  ( cd "$ROOT/seed" && git checkout -q -b main 2>/dev/null || true
    mkdir -p data/client-v1
    echo '{"version":1,"coveragePct":80}' > data/status.json
    echo '{"items":{"premiumtv:movie:1":"(XXX-) A"}}' > data/adult-titles.json
    echo '{"version":1,"maps":{"id":{}}}' > data/client-v1/manifest.json
    echo '{"items":{}}' > data/client-v1/id-0-aaaaaaaaaaaaaaaa.json
    echo 'gz' > data/titles_premiumtv.json.gz
    git add -A && git commit -q -m init && git push -q origin HEAD:main )
  rm -rf "$ROOT/seed"
  git clone -q -b main "$ROOT/remote.git" "$ROOT/job"
}

job_outputs() {   # what adult_tpdb.py + export_client.py leave in the worktree
  cd "$ROOT/job"
  echo '{"titles":{"a":{"status":"match"}}}' > data/adult-tpdb.json
  echo '{"version":1,"coveragePct":80,"adult":{"withCover":196}}' > data/status.json
  echo '{"version":1,"maps":{"id":{},"adult":{"count":64}}}' > data/client-v1/manifest.json
  echo '{"items":{"premiumtv:movie:1":{"adult":true}}}' > data/client-v1/adult-0-bbbbbbbbbbbbbbbb.json
  rm data/client-v1/id-0-aaaaaaaaaaaaaaaa.json          # expired chunk removed by export
}

remote_show() { git --git-dir="$ROOT/remote.git" show "main:$1" 2>/dev/null; }

# 1) old recipe reproduces the bug
setup; job_outputs
git add -- data/adult-tpdb.json && git commit -q -m old
if git pull -q --rebase origin main 2>"$ROOT/err"; then fail "old recipe unexpectedly pulled"; fi
grep -q "unstaged changes" "$ROOT/err" && pass "old recipe: 'cannot pull with rebase: unstaged changes' reproduced" || fail "unexpected old error: $(cat "$ROOT/err")"

# 2) new script: all outputs land, even with an extra dirty file that was not listed
setup; job_outputs
echo '{"items":{"x":"dirty"}}' > data/adult-titles.json   # unlisted modification
bash "$PUB" "chore: test" data/adult-tpdb.json data/status.json data/client-v1 > "$ROOT/out" 2>&1 || { cat "$ROOT/out"; fail "publish failed on dirty worktree"; }
remote_show data/adult-tpdb.json | grep -q match || fail "adult-tpdb.json not on main"
remote_show data/status.json | grep -q '"adult"' || fail "status.json adult field not on main"
remote_show data/client-v1/manifest.json | grep -q '"adult"' || fail "manifest maps.adult not on main"
remote_show data/client-v1/adult-0-bbbbbbbbbbbbbbbb.json >/dev/null || fail "adult chunk not on main"
remote_show data/client-v1/id-0-aaaaaaaaaaaaaaaa.json >/dev/null && fail "deleted chunk still on main"
remote_show data/adult-titles.json | grep -q dirty && fail "unlisted file was committed"
grep -q dirty "$ROOT/job/data/adult-titles.json" || fail "autostash lost the unlisted change"
grep -q "weitere geänderte Dateien" "$ROOT/out" || fail "unlisted dirty file not reported"
pass "dirty worktree: adult-tpdb.json, status.adult, manifest maps.adult, chunks pushed; unlisted change kept local + reported"

# 3) main moved meanwhile (Manager titles upload + sync status change): rebase, our outputs win
setup; job_outputs
git clone -q -b main "$ROOT/remote.git" "$ROOT/other"
( cd "$ROOT/other" && echo 'gz2' > data/titles_premiumtv.json.gz && echo '{"version":1,"coveragePct":81}' > data/status.json \
  && git commit -qam "manager upload" && git push -q origin HEAD:main )
cd "$ROOT/job"
bash "$PUB" "chore: test" data/adult-tpdb.json data/status.json data/client-v1 > "$ROOT/out" 2>&1 || { cat "$ROOT/out"; fail "publish failed after main moved"; }
[ "$(remote_show data/titles_premiumtv.json.gz)" = gz2 ] || fail "concurrent upload lost"
remote_show data/status.json | grep -q '"adult"' || fail "conflict on status.json not resolved for our output"
pass "main moved + conflicting status.json: rebased, concurrent upload kept, adult status on main"

# 4) push race: first push rejected by the server, retry succeeds
setup; job_outputs
cat > "$ROOT/remote.git/hooks/pre-receive" <<H
#!/bin/sh
f="$ROOT/race"; [ -e "\$f" ] && exit 0; touch "\$f"; echo "simulated race" >&2; exit 1
H
chmod +x "$ROOT/remote.git/hooks/pre-receive"; rm -f "$ROOT/race"
bash "$PUB" "chore: test" data/adult-tpdb.json data/status.json data/client-v1 > "$ROOT/out" 2>&1 || { cat "$ROOT/out"; fail "retry did not recover"; }
grep -q "Versuch 2" "$ROOT/out" || fail "expected success on attempt 2"
remote_show data/adult-tpdb.json >/dev/null || fail "not on main after retry"
pass "push race: rejected once, landed on retry"

# 5) permanent failure is visible (exit 1 + ::error), not silent
setup; job_outputs
printf '#!/bin/sh\nexit 1\n' > "$ROOT/remote.git/hooks/pre-receive"; chmod +x "$ROOT/remote.git/hooks/pre-receive"
if PUBLISH_RETRIES=3 bash "$PUB" "chore: test" data/adult-tpdb.json data/status.json data/client-v1 > "$ROOT/out" 2>&1; then fail "permanent push failure returned 0"; fi
grep -q "::error" "$ROOT/out" || fail "no ::error annotation"
pass "permanent push failure: exit 1 with ::error annotation"

# 6) nothing changed: exit 0, no commit
setup; cd "$ROOT/job"; before="$(git rev-parse HEAD)"
bash "$PUB" "chore: test" data/adult-tpdb.json data/status.json data/client-v1 > "$ROOT/out" 2>&1 || fail "no-op returned non-zero"
[ "$(git rev-parse HEAD)" = "$before" ] || fail "no-op created a commit"
pass "no changes: exit 0, no commit"

# 7) regression run 45006009953: sync job started from a STALE commit, the adult job had
#    meanwhile pushed status.json -> old recipe: rebase conflict; new: lands, our sync output wins
setup
git clone -q -b main "$ROOT/remote.git" "$ROOT/other"
( cd "$ROOT/other" && echo '{"version":1,"coveragePct":80,"adult":{"withCover":256}}' > data/status.json \
  && git commit -qam "adult covers" && git push -q origin HEAD:main )
cd "$ROOT/job"   # still at the old commit (like checkout of the trigger SHA)
echo '{"version":1,"coveragePct":89,"adult":{"withCover":256}}' > data/status.json
echo '{"v":"tmdb"}' > data/index.json
git add -u data/ && git commit -q -m old
if git pull -q --rebase origin main >/dev/null 2>&1; then fail "old recipe unexpectedly rebased"; fi
git rebase --abort >/dev/null 2>&1 || true
git reset -q --soft HEAD~1
pass "old sync recipe: status.json conflict reproduced"
bash "$PUB" "chore: TMDB" data/index.json data/status.json > "$ROOT/out" 2>&1 || { cat "$ROOT/out"; fail "sync publish failed on stale base"; }
remote_show data/status.json | grep -q '"coveragePct":89' || fail "sync status not on main"
remote_show data/index.json | grep -q tmdb || fail "sync index not on main"
pass "stale base + status.json conflict: sync output lands"

# 8) derived files (export): conflict -> rebuild on the newest main instead of overwriting it
setup
git clone -q -b main "$ROOT/remote.git" "$ROOT/other"
( cd "$ROOT/other" && echo '{"version":1,"maps":{"id":{},"adult":{"count":64,"n":300}}}' > data/client-v1/manifest.json \
  && git commit -qam "adult job export" && git push -q origin HEAD:main )
cd "$ROOT/job"
echo '{"version":1,"maps":{"id":{"stale":1}}}' > data/client-v1/manifest.json
PUBLISH_REGEN='python3 -c "import json;p=\"data/client-v1/manifest.json\";m=json.load(open(p));m[\"regen\"]=1;json.dump(m,open(p,\"w\"))"' \
  bash "$PUB" "chore: export" data/client-v1 > "$ROOT/out" 2>&1 || { cat "$ROOT/out"; fail "regen publish failed"; }
m="$(remote_show data/client-v1/manifest.json)"
echo "$m" | grep -q '"n": 300' || fail "regen overwrote newer adult map: $m"
echo "$m" | grep -q '"regen": 1' || fail "regen output not pushed: $m"
echo "$m" | grep -q stale && fail "stale export output pushed: $m"
grep -q "Baue neu" "$ROOT/out" || fail "regen path not taken"
pass "export conflict: rebuilt on newest main, newer adult map kept"
echo ALL PASS
