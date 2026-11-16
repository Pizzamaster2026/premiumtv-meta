#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
echo "== title cleaning =="
python3 test_title.py
echo "== status.json =="
python3 test_status.py
echo "== sync (mock only) =="
python3 test_sync.py
echo "== git add status.json =="
bash ./test_commit_add.sh
echo "== sport.json (fixtures only) =="
python3 test_sport.py
echo "ALL OK"
