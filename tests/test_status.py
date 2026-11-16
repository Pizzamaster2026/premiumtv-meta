#!/usr/bin/env python3
"""Unit tests for data/status.json. No IPTV, no TMDB, no GitHub."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sync_meta  # noqa: E402


def expect(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label} {detail}")
    print(f"PASS  {label}")


def sample_groups():
    return [
        {"type": "movie", "query": "Der Pate", "year": "1972", "resolved": "movie:238",
         "refs": [{"provider": "premiumtv", "type": "movie", "id": "1", "name": "Der Pate", "year": "1972"},
                  {"provider": "one", "type": "movie", "id": "11", "name": "Der Pate", "year": "1972"}]},
        {"type": "movie", "query": "Forrest Gump", "year": "1994", "resolved": None,
         "refs": [{"provider": "premiumtv", "type": "movie", "id": "2", "name": "12- Forrest Gump", "year": "1994"}]},
        {"type": "series", "query": "Breaking Bad", "year": "2008", "resolved": "tv:1396",
         "refs": [{"provider": "premiumtv", "type": "series", "id": "3", "name": "Breaking Bad", "year": "2008"}]},
        {"type": "movie", "query": "Nur Bei One", "year": "2020", "resolved": None,
         "refs": [{"provider": "one", "type": "movie", "id": "4", "name": "Nur Bei One", "year": "2020"}]},
    ]


def sample_misses():
    # title_key is typ + cleaned query + year — use the real function
    k = sync_meta.title_key("movie", "Forrest Gump", "1994")
    return {k: {"tmdbId": None, "query": "Forrest Gump", "year": "1994", "type": "movie"}}


def main() -> int:
    groups = sample_groups()
    misses = sample_misses()
    cov = sync_meta.coverage_from_groups(groups, misses)
    o = cov["overall"]
    expect("overall totals", o["total"] == 4 and o["withTmdb"] == 2 and o["noMatch"] == 1 and o["pending"] == 1, o)
    expect("overall pct 50", o["pct"] == 50.0, o)
    ptv = cov["providers"]["premiumtv"]
    expect("ptv movies 1/2 noMatch", ptv["movies"]["total"] == 2 and ptv["movies"]["withTmdb"] == 1 and ptv["movies"]["noMatch"] == 1, ptv)
    expect("ptv series 100", ptv["series"]["withTmdb"] == 1 and ptv["series"]["pct"] == 100.0, ptv)
    one = cov["providers"]["one"]
    expect("one movie shared + pending", one["movies"]["total"] == 2 and one["movies"]["withTmdb"] == 1 and one["movies"]["pending"] == 1, one)
    expect("one has no series key", "series" not in one, one)

    stats = {"new": 2, "refreshed": 0, "tmdbCalls": 9, "partial": False, "budgetStopped": False}
    with tempfile.TemporaryDirectory() as td:
        path = sync_meta.write_status(Path(td), groups, misses, stats, partial=False)
        expect("file written", path.is_file() and path.name == "status.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        expect("no secrets", "api_key" not in json.dumps(doc).lower() and "token" not in json.dumps(doc).lower())
        expect("counts", doc["titles"] == 4 and doc["withTmdb"] == 2 and doc["pending"] == 1 and doc["noMatch"] == 1, doc)
        expect("phase done", doc["phase"] == "done" and doc["running"] is False, doc)
        expect("progress processed", doc["progress"]["processed"] == 3 and doc["progress"]["total"] == 4, doc)
        expect("updatedAt iso", "T" in doc["updatedAt"] and doc["updatedAt"].endswith("Z"), doc["updatedAt"])

        path2 = sync_meta.write_status(Path(td), groups, misses, {**stats, "budgetStopped": True, "partial": True}, partial=True)
        doc2 = json.loads(path2.read_text(encoding="utf-8"))
        expect("phase tmdb when partial", doc2["phase"] == "tmdb" and doc2["running"] is True, doc2)

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
