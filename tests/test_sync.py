#!/usr/bin/env python3
"""Integration tests against a local mock Xtream + mock TMDB server.

Never contacts real IPTV servers or api.themoviedb.org.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import mock_server  # noqa: E402
import sync_meta  # noqa: E402


def expect(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label} {detail}")


def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode())


def run_sync(env, data_dir):
    old = dict(os.environ)
    try:
        for k in list(os.environ):
            if k in ("TMDB_API_KEY", "TMDB_API_BASE", "PTV_URL", "PTV_USER", "PTV_PASS",
                     "ONE_URL", "ONE_USER", "ONE_PASS"):
                del os.environ[k]
        os.environ.update(env)
        return sync_meta.sync(Path(data_dir))
    finally:
        for k in list(os.environ):
            if k not in old:
                del os.environ[k]
        os.environ.update(old)


def test_missing_secrets_exits_cleanly():
    with tempfile.TemporaryDirectory() as td:
        code = run_sync({}, td)
        expect("exit 0 without secrets", code == 0)
        expect("no data written", not (Path(td) / "meta.json").exists())


def test_missing_tmdb_key_only():
    with tempfile.TemporaryDirectory() as td:
        code = run_sync({
            "PTV_URL": "http://127.0.0.1:9",
            "PTV_USER": "x",
            "PTV_PASS": "y",
        }, td)
        expect("exit 0 without tmdb key", code == 0)
        expect("no data written", not (Path(td) / "meta.json").exists())


def test_full_sync_and_incremental(base: str):
    stats0 = get_json(base + "/stats/reset")
    with tempfile.TemporaryDirectory() as td:
        env = {
            "TMDB_API_KEY": mock_server.TMDB_KEY,
            "TMDB_API_BASE": base + "/3",
            "PTV_URL": base + "/ptv",
            "PTV_USER": mock_server.PTV_USER,
            "PTV_PASS": mock_server.PTV_PASS,
            "ONE_URL": base + "/one",
            "ONE_USER": mock_server.ONE_USER,
            "ONE_PASS": mock_server.ONE_PASS,
        }
        code = run_sync(env, td)
        expect("first sync exit 0", code == 0)
        meta = json.loads((Path(td) / "meta.json").read_text())
        idx = json.loads((Path(td) / "index.json").read_text())
        expect("godfather entry", "movie:238" in meta["entries"])
        g = meta["entries"]["movie:238"]
        expect("german title", g["title"] == "Der Pate")
        expect("runtime", g["runtime"] == 175)
        expect("genres", g["genres"] == ["Krimi", "Drama"])
        expect("vote", g["voteAverage"] == 8.7)
        expect("poster", g["posterPath"] == "/pate.jpg")
        expect("cast brando", g["cast"][0]["name"] == "Marlon Brando" and g["cast"][0]["id"] == 3084)
        expect("both providers share film",
               idx["byId"]["premiumtv:movie:1001"] == "movie:238"
               and idx["byId"]["one:movie:501"] == "movie:238")
        expect("title key", idx["byTitle"]["movie|der pate|1972"] == "movie:238")
        expect("matrix via search", idx["byId"]["premiumtv:movie:1002"] == "movie:603")
        expect("inception via search", idx["byId"]["one:movie:502"] == "movie:27205")
        expect("breaking bad", idx["byId"]["premiumtv:series:2001"] == "tv:1396")
        expect("the office", idx["byId"]["one:series:801"] == "tv:2316")
        expect("unmatched recorded", "movie|unknown indie film xyzqq|1998" in idx["misses"])
        expect("series runtime", meta["entries"]["tv:1396"]["runtime"] == 47)

        st = get_json(base + "/stats")
        expect("only list actions", all(x["action"] in ("get_vod_streams", "get_series") for x in st["xtream"]))
        expect("both providers listed", {x["provider"] for x in st["xtream"]} == {"ptv", "one"})
        expect("no stream/info calls", st["forbidden"] == [])
        first_tmdb = len(st["tmdb"])
        expect("tmdb was used", first_tmdb > 0)

        # Incremental: second run must not hit TMDB again for fresh titles
        get_json(base + "/stats/reset")
        code = run_sync(env, td)
        expect("second sync exit 0", code == 0)
        st2 = get_json(base + "/stats")
        expect("lists still fetched", len(st2["xtream"]) == 4, st2["xtream"])
        expect("no tmdb on fresh rerun", st2["tmdb"] == [], st2["tmdb"])
        expect("still no stream calls", st2["forbidden"] == [])
        expect("no rewrite when unchanged", not (Path(td) / ".changed").exists())

        # Age one entry past 30 days -> refresh details only
        meta = json.loads((Path(td) / "meta.json").read_text())
        meta["entries"]["movie:238"]["updatedAt"] = "2000-01-01T00:00:00Z"
        (Path(td) / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        get_json(base + "/stats/reset")
        code = run_sync(env, td)
        expect("refresh sync exit 0", code == 0)
        st3 = get_json(base + "/stats")
        eps = [x["ep"] for x in st3["tmdb"]]
        expect("refreshed godfather details", any(e.startswith("movie/238") for e in eps), eps)
        expect("did not re-search all titles", len(eps) <= 2, eps)
        expect("files marked changed", (Path(td) / ".changed").exists())


def test_one_provider_only(base: str):
    get_json(base + "/stats/reset")
    with tempfile.TemporaryDirectory() as td:
        env = {
            "TMDB_API_KEY": mock_server.TMDB_KEY,
            "TMDB_API_BASE": base + "/3",
            "ONE_URL": base + "/one",
            "ONE_USER": mock_server.ONE_USER,
            "ONE_PASS": mock_server.ONE_PASS,
        }
        code = run_sync(env, td)
        expect("exit 0", code == 0)
        idx = json.loads((Path(td) / "index.json").read_text())
        expect("only one ids", all(k.startswith("one:") for k in idx["byId"]), list(idx["byId"]))
        st = get_json(base + "/stats")
        expect("only one xtream", {x["provider"] for x in st["xtream"]} == {"one"})


def test_logs_do_not_contain_secrets(base: str, cap_path: Path):
    # run and inspect what sync_meta would print – already goes to stdout;
    # we just assert the mask helpers never echo the password.
    sample = sync_meta.mask_secret(mock_server.PTV_PASS)
    expect("password masked", sample == "********" and mock_server.PTV_PASS not in sample)
    expect("url host only", "secret" not in sync_meta.mask_url(base + "/ptv?password=ptv-secret"))
    expect("url no query", "password" not in sync_meta.mask_url(base + "/ptv?password=ptv-secret"))


def main():
    srv = mock_server.start(0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    # Safety: these tests must never be pointed at real hosts
    expect("local mock only", "127.0.0.1" in base)

    tests = [
        test_missing_secrets_exits_cleanly,
        test_missing_tmdb_key_only,
        lambda: test_full_sync_and_incremental(base),
        lambda: test_one_provider_only(base),
        lambda: test_logs_do_not_contain_secrets(base, Path(".")),
    ]
    names = [
        "test_missing_secrets_exits_cleanly",
        "test_missing_tmdb_key_only",
        "test_full_sync_and_incremental",
        "test_one_provider_only",
        "test_logs_do_not_contain_secrets",
    ]
    failed = 0
    try:
        for name, fn in zip(names, tests):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL  {name}: {e}")
    finally:
        srv.shutdown()
        srv.server_close()
    if failed:
        print(f"{failed} failed")
        return 1
    print(f"{len(tests)} tests ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
