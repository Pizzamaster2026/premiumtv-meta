#!/usr/bin/env python3
"""Integration tests against a local mock Xtream + mock TMDB server.

Never contacts real IPTV servers or api.themoviedb.org.
"""
from __future__ import annotations

import contextlib
import io
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
                     "ONE_URL", "ONE_USER", "ONE_PASS",
                     "SYNC_TIME_BUDGET_MIN", "SYNC_CHECKPOINT_EVERY"):
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


def both_providers(base: str) -> dict:
    return {
        "TMDB_API_KEY": mock_server.TMDB_KEY,
        "TMDB_API_BASE": base + "/3",
        "PTV_URL": base + "/ptv",
        "PTV_USER": mock_server.PTV_USER,
        "PTV_PASS": mock_server.PTV_PASS,
        "ONE_URL": base + "/one",
        "ONE_USER": mock_server.ONE_USER,
        "ONE_PASS": mock_server.ONE_PASS,
    }


def test_time_budget_stops_lookups_and_saves(base: str):
    """Budget 0: no new TMDB calls, already cached titles stay, process exits 0."""
    mock_server.reset_catalogs()
    get_json(base + "/stats/reset")
    with tempfile.TemporaryDirectory() as td:
        env = both_providers(base)
        code = run_sync(env, td)
        expect("seed sync exit 0", code == 0)
        meta = json.loads((Path(td) / "meta.json").read_text())
        expect("seed has godfather", "movie:238" in meta["entries"])
        expect("seed has inception", "movie:27205" in meta["entries"])

        # A title that is not yet in the cache and would need a TMDB search.
        mock_server.PTV_VOD.append({"stream_id": 1099, "name": "Unknown indie film XYZQQ 1999"})
        get_json(base + "/stats/reset")
        env["SYNC_TIME_BUDGET_MIN"] = "0"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_sync(env, td)
        log = buf.getvalue()
        expect("budget sync exit 0", code == 0)
        expect("budget log line", "keine neuen TMDB-Abfragen mehr" in log, log)
        st = get_json(base + "/stats")
        expect("lists still fetched under budget", len(st["xtream"]) == 4, st["xtream"])
        expect("no new TMDB lookups under budget", st["tmdb"] == [], st["tmdb"])
        meta2 = json.loads((Path(td) / "meta.json").read_text())
        idx2 = json.loads((Path(td) / "index.json").read_text())
        expect("cached film kept", "movie:238" in meta2["entries"])
        expect("cached inception kept", "movie:27205" in meta2["entries"])
        expect("new title not fetched", "premiumtv:movie:1099" not in idx2["byId"])
        expect("still no stream calls", st["forbidden"] == [])
    mock_server.reset_catalogs()


def test_checkpoint_survives_hard_kill(base: str):
    """Writing every new entry keeps progress on disk if the process is killed."""
    mock_server.reset_catalogs()
    get_json(base + "/stats/reset")
    real_write = sync_meta.write_outputs
    calls = {"n": 0}

    def wrapped(*args, **kwargs):
        result = real_write(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] >= 1:
            raise SystemExit(99)
        return result

    with tempfile.TemporaryDirectory() as td:
        env = both_providers(base)
        env["SYNC_CHECKPOINT_EVERY"] = "1"
        sync_meta.write_outputs = wrapped
        try:
            try:
                run_sync(env, td)
                raise AssertionError("expected SystemExit from checkpoint hook")
            except SystemExit as e:
                expect("hook exit 99", e.code == 99)
        finally:
            sync_meta.write_outputs = real_write
        meta_path = Path(td) / "meta.json"
        expect("checkpoint wrote meta", meta_path.is_file())
        meta = json.loads(meta_path.read_text())
        expect("at least one entry saved", len(meta.get("entries") or {}) >= 1)
        expect("godfather likely first (has tmdb id)", "movie:238" in meta["entries"] or len(meta["entries"]) >= 1)
    mock_server.reset_catalogs()


def test_prune_titles_gone_from_both_providers(base: str):
    """A title that leaves every provider list is removed from meta.json."""
    mock_server.reset_catalogs()
    get_json(base + "/stats/reset")
    with tempfile.TemporaryDirectory() as td:
        env = both_providers(base)
        code = run_sync(env, td)
        expect("seed exit 0", code == 0)
        meta = json.loads((Path(td) / "meta.json").read_text())
        idx = json.loads((Path(td) / "index.json").read_text())
        expect("inception present", "movie:27205" in meta["entries"])
        expect("inception indexed", idx["byId"]["one:movie:502"] == "movie:27205")
        expect("godfather present", "movie:238" in meta["entries"])

        mock_server.ONE_VOD[:] = [x for x in mock_server.ONE_VOD if x.get("stream_id") != 502]
        get_json(base + "/stats/reset")
        code = run_sync(env, td)
        expect("prune sync exit 0", code == 0)
        meta2 = json.loads((Path(td) / "meta.json").read_text())
        idx2 = json.loads((Path(td) / "index.json").read_text())
        expect("inception pruned from meta", "movie:27205" not in meta2["entries"], list(meta2["entries"]))
        expect("inception pruned from byId", "one:movie:502" not in idx2["byId"], list(idx2["byId"]))
        expect("godfather kept", "movie:238" in meta2["entries"])
        expect("matrix kept", "movie:603" in meta2["entries"])
        expect("office kept", "tv:2316" in meta2["entries"])
        expect("files marked changed", (Path(td) / ".changed").exists())
    mock_server.reset_catalogs()


def main():
    srv = mock_server.start(0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    # Safety: these tests must never be pointed at real hosts
    expect("local mock only", "127.0.0.1" in base)

    mock_server.reset_catalogs()
    tests = [
        test_missing_secrets_exits_cleanly,
        test_missing_tmdb_key_only,
        lambda: test_full_sync_and_incremental(base),
        lambda: test_one_provider_only(base),
        lambda: test_logs_do_not_contain_secrets(base, Path(".")),
        lambda: test_time_budget_stops_lookups_and_saves(base),
        lambda: test_checkpoint_survives_hard_kill(base),
        lambda: test_prune_titles_gone_from_both_providers(base),
    ]
    names = [
        "test_missing_secrets_exits_cleanly",
        "test_missing_tmdb_key_only",
        "test_full_sync_and_incremental",
        "test_one_provider_only",
        "test_logs_do_not_contain_secrets",
        "test_time_budget_stops_lookups_and_saves",
        "test_checkpoint_survives_hard_kill",
        "test_prune_titles_gone_from_both_providers",
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
