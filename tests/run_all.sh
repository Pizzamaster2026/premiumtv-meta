#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
echo "== title cleaning =="
python3 test_title.py
echo "== title rematch / safe pick =="
python3 test_rematch.py
echo "== compact customer cache =="
python3 test_client_export.py
echo "== status.json =="
python3 test_status.py
echo "== sync (mock only) =="
python3 test_sync.py
echo "== git add status.json =="
bash ./test_commit_add.sh
echo "== sport.json (fixtures only) =="
python3 test_sport.py
echo "== adult covers TPDB (mocked API) =="
python3 test_adult_tpdb.py
echo "== adult job: commit/push robustness (local git only) =="
bash ./test_git_publish.sh
echo "== adult job: workflow structure =="
python3 test_workflow_adult.py
echo "ALL OK"
