#!/usr/bin/env python3
"""Unit tests for data/status.json and live status-branch publish. No IPTV, no TMDB, no GitHub."""
from __future__ import annotations

import json
import os
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

    # no token, no network
    for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY"):
        os.environ.pop(k, None)
    sync_meta.live_publisher = None
    sync_meta._last_live_push = 0.0

    stats = {"new": 2, "refreshed": 0, "tmdbCalls": 9, "partial": False, "budgetStopped": False}
    with tempfile.TemporaryDirectory() as td:
        path = sync_meta.write_status(Path(td), groups, misses, stats, partial=False)
        expect("file written", path.is_file() and path.name == "status.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        blob = json.dumps(doc).lower()
        expect("no secrets", "api_key" not in blob and "github_pat" not in blob and "token" not in blob)
        expect("counts", doc["titles"] == 4 and doc["withTmdb"] == 2 and doc["pending"] == 1 and doc["noMatch"] == 1, doc)
        expect("phase done", doc["phase"] == "done" and doc["running"] is False, doc)
        expect("progress processed default", doc["progress"]["processed"] == 3 and doc["progress"]["total"] == 4, doc)
        expect("updatedAt iso", "T" in doc["updatedAt"] and doc["updatedAt"].endswith("Z"), doc["updatedAt"])

        path2 = sync_meta.write_status(Path(td), groups, misses, {**stats, "budgetStopped": True, "partial": True}, partial=True)
        doc2 = json.loads(path2.read_text(encoding="utf-8"))
        expect("phase tmdb when partial", doc2["phase"] == "tmdb" and doc2["running"] is True, doc2)

        # this-run open/queried/elapsed
        live_stats = {**stats, "openTotal": 9400, "queried": 4120, "elapsedSec": 34 * 60, "phase": "tmdb", "partial": True}
        path3 = sync_meta.write_status(Path(td), groups, misses, live_stats, partial=True)
        doc3 = json.loads(path3.read_text(encoding="utf-8"))
        expect("live progress 4120/9400", doc3["progress"]["processed"] == 4120 and doc3["progress"]["total"] == 9400 and doc3["progress"]["remaining"] == 5280, doc3["progress"])
        expect("elapsed sec", doc3["progress"]["elapsedSec"] == 2040, doc3["progress"])
        expect("open fields", doc3.get("openTotal") == 9400 and doc3.get("queried") == 4120, doc3)

    # live publisher hook + throttle
    pushed = []
    sync_meta._last_live_push = 0.0
    os.environ["SYNC_STATUS_PUSH_SEC"] = "30"

    def hook(doc):
        pushed.append(doc)
        return True

    sync_meta.live_publisher = hook
    with tempfile.TemporaryDirectory() as td:
        st = {"new": 1, "openTotal": 100, "queried": 1, "elapsedSec": 10, "partial": True}
        sync_meta.write_status(Path(td), groups, misses, st, partial=True, force_live=True)
        expect("first live push", len(pushed) == 1 and pushed[0]["progress"]["processed"] == 1, len(pushed))
        st["queried"] = 2
        sync_meta.write_status(Path(td), groups, misses, st, partial=True)  # throttled
        expect("throttled no second push", len(pushed) == 1, len(pushed))
        st["queried"] = 100
        st["partial"] = False
        sync_meta.write_status(Path(td), groups, misses, {**st, "budgetStopped": False}, partial=False)  # final forces
        expect("final force push", len(pushed) == 2 and pushed[1]["running"] is False, (len(pushed), pushed[-1].get("running")))
        expect("hook docs have no token", all("token" not in json.dumps(d).lower() for d in pushed))

    # no hook, no token: publish_live_status is a no-op (does not contact GitHub)
    sync_meta.live_publisher = None
    sync_meta._last_live_push = 0.0
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GITHUB_REPOSITORY", None)
    expect("publish without token is false", sync_meta.publish_live_status({"phase": "tmdb"}) is False)

    # off switch
    os.environ["SYNC_STATUS_BRANCH_PUSH"] = "0"
    called = []
    sync_meta.live_publisher = lambda d: called.append(d) or True
    # hook is used first, before the env check... actually publish_live_status checks hook first.
    # Test live_push_enabled instead.
    expect("push can be disabled", sync_meta.live_push_enabled() is False)
    os.environ.pop("SYNC_STATUS_BRANCH_PUSH", None)
    expect("push enabled by default", sync_meta.live_push_enabled() is True)
    sync_meta.live_publisher = None

    # Mid-run snapshot must count cached TMDB hits, not treat them as pending.
    raw = [
        {"type": "movie", "query": "Der Pate", "year": "1972", "resolved": None,
         "refs": [{"provider": "premiumtv", "type": "movie", "id": "1", "name": "Der Pate", "year": "1972"}]},
        {"type": "movie", "query": "Forrest Gump", "year": "1994", "resolved": None,
         "refs": [{"provider": "premiumtv", "type": "movie", "id": "2", "name": "Forrest Gump", "year": "1994"}]},
        {"type": "series", "query": "Breaking Bad", "year": "2008", "resolved": None,
         "refs": [{"provider": "one", "type": "series", "id": "3", "name": "Breaking Bad", "year": "2008"}]},
        {"type": "movie", "query": "Noch Offen", "year": "2024", "resolved": None,
         "refs": [{"provider": "one", "type": "movie", "id": "4", "name": "Noch Offen", "year": "2024"}]},
    ]
    entries = {"movie:238": {"tmdbId": 238, "title": "Der Pate"}, "tv:1396": {"tmdbId": 1396, "title": "Breaking Bad"}}
    prev = {
        sync_meta.title_key("movie", "Der Pate", "1972"): "movie:238",
        sync_meta.title_key("series", "Breaking Bad", "2008"): "tv:1396",
    }
    mid_misses = {sync_meta.title_key("movie", "Forrest Gump", "1994"): {"tmdbId": None, "query": "Forrest Gump", "year": "1994", "type": "movie"}}
    before = sync_meta.coverage_from_groups(raw, mid_misses)
    expect("before apply cache is pending", before["overall"]["withTmdb"] == 0 and before["overall"]["noMatch"] == 1 and before["overall"]["pending"] == 3, before["overall"])
    n = sync_meta.apply_existing_to_groups(raw, entries, prev)
    expect("apply attaches 2 cache hits", n == 2, n)
    after = sync_meta.coverage_from_groups(raw, mid_misses)
    expect("after apply withTmdb 2 noMatch 1 pending 1", after["overall"]["withTmdb"] == 2 and after["overall"]["noMatch"] == 1 and after["overall"]["pending"] == 1 and after["overall"]["pct"] == 50.0, after["overall"])
    sync_meta.live_publisher = None
    sync_meta._last_live_push = 0.0
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GITHUB_REPOSITORY", None)
    with tempfile.TemporaryDirectory() as td:
        st = {"new": 0, "openTotal": 1, "queried": 0, "elapsedSec": 30, "partial": True, "phase": "tmdb"}
        path = sync_meta.write_status(Path(td), raw, mid_misses, st, partial=True, force_live=True)
        doc = json.loads(path.read_text(encoding="utf-8"))
        expect("mid-run coverage uses cache", doc["withTmdb"] == 2 and doc["noMatch"] == 1 and doc["pending"] == 1 and doc["coveragePct"] == 50.0, doc)
        expect("mid-run progress 0/1 still shown", doc["progress"]["processed"] == 0 and doc["progress"]["total"] == 1, doc["progress"])

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
