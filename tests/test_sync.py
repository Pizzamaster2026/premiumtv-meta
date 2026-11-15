#!/usr/bin/env python3
"""Integration tests against a local mock Xtream + mock TMDB server.

Never contacts real IPTV servers or api.themoviedb.org.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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
                     "ONE_URL", "ONE_USER", "ONE_PASS") or k.startswith("SYNC_"):
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
        expect("sherlock via id field", idx["byId"]["one:series:802"] == "tv:19885")
        expect("stats count series", idx["stats"]["series"] >= 3, idx["stats"])
        expect("unmatched recorded", "movie|unknown indie film xyzqq|1998" in idx["misses"])
        expect("series runtime", meta["entries"]["tv:1396"]["runtime"] == 47)
        expect("sherlock stored as tv", "tv:19885" in meta["entries"])

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


def test_xtream_base_normalizes_urls():
    cases = {
        "http://example.test:8080": "http://example.test:8080",
        "http://example.test:8080/": "http://example.test:8080",
        "  example.test:8080  ": "http://example.test:8080",
        "example.test:8080/": "http://example.test:8080",
        "example.test": "http://example.test",
        "http://example.test:8080/player_api.php": "http://example.test:8080",
        "http://example.test:8080/player_api.php?username=u&password=super-secret": "http://example.test:8080",
        "http://example.test:8080/get.php?username=u": "http://example.test:8080",
        "http://example.test:8080/panel_api.php": "http://example.test:8080",
        "http://example.test:8080/xmltv.php": "http://example.test:8080",
        "http://example.test:8080/ptv": "http://example.test:8080/ptv",
        "http://example.test:8080/ptv/": "http://example.test:8080/ptv",
        "http://example.test:8080/ptv/get.php": "http://example.test:8080/ptv",
        "http://127.0.0.1:1234/ptv": "http://127.0.0.1:1234/ptv",
        "https://example.test": "https://example.test",
        "//example.test:8080": "http://example.test:8080",
        "127.0.0.1:8080": "http://127.0.0.1:8080",
        "http://[2001:db8::1]:8080/get.php": "http://[2001:db8::1]:8080",
        "[2001:db8::1]:8080": "http://[2001:db8::1]:8080",
        "host-no-dot:8080": "http://host-no-dot:8080",
        "": "",
    }
    for raw, want in cases.items():
        got = sync_meta.xtream_base(raw)
        expect(f"xtream_base({raw!r})", got == want, f"got {got!r}, want {want!r}")
        expect("no secret in base", "super-secret" not in got and "password=" not in got)

    # List URL uses the normalized base and keeps a subpath.
    url = sync_meta.xtream_list_url("example.test:8080/get.php?username=u&password=x", "u", "p", "get_vod_streams")
    expect("list url scheme host", url.startswith("http://example.test:8080/player_api.php?"), url)
    expect("list url action", "action=get_vod_streams" in url, url)


def test_describe_http_error_masks_and_names_reason():
    timed = urllib.error.URLError("timed out")
    expect("timeout reason", sync_meta.describe_http_error(timed) == "URLError (timed out)")

    refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    msg = sync_meta.describe_http_error(refused)
    expect("refused type", "URLError" in msg and "ConnectionRefusedError" in msg, msg)

    dns = urllib.error.URLError(OSError(-2, "Name or service not known"))
    msg = sync_meta.describe_http_error(dns)
    expect("dns type", "URLError" in msg and "Name or service not known" in msg, msg)

    leaky = urllib.error.URLError("failed http://example.test/player_api.php?username=alice&password=super-secret")
    msg = sync_meta.describe_http_error(leaky)
    expect("password masked", "super-secret" not in msg and "alice" not in msg, msg)
    expect("url stripped", "http://example.test" not in msg and "[url]" in msg, msg)

    http_err = urllib.error.HTTPError("http://example.test/x", 503, "unavailable", hdrs=None, fp=None)
    expect("http status", sync_meta.describe_http_error(http_err) == "HTTPError 503")


def test_item_id_accepts_panel_fallbacks():
    expect("series_id preferred", sync_meta.item_id({"series_id": 2001, "id": 9}, "series") == "2001")
    expect("series id fallback", sync_meta.item_id({"id": 802, "name": "Sherlock"}, "series") == "802")
    expect("series stream_id fallback", sync_meta.item_id({"stream_id": 77}, "series") == "77")
    expect("movie stream_id preferred", sync_meta.item_id({"stream_id": 501, "id": 1}, "movie") == "501")
    expect("movie id fallback", sync_meta.item_id({"id": 501}, "movie") == "501")
    expect("movie vod_id fallback", sync_meta.item_id({"vod_id": 88}, "movie") == "88")
    expect("empty without ids", sync_meta.item_id({"name": "X"}, "series") == "")


def test_collect_titles_keeps_series_with_id_only():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        groups = sync_meta.collect_titles({
            "one": {
                "movie": [{"stream_id": 1, "name": "Der Pate (1972)"}],
                "series": [{"id": 802, "name": "Sherlock", "year": "2010"}],
            }
        })
    log = buf.getvalue()
    types = [g["type"] for g in groups]
    expect("both types collected", types.count("movie") == 1 and types.count("series") == 1, types)
    series = next(g for g in groups if g["type"] == "series")
    expect("series ref id", series["refs"][0]["id"] == "802" and series["refs"][0]["provider"] == "one")
    expect("log names fields", "Felder:" in log and "id" in log, log)
    expect("log counts series", "1 Filme, 1 Serien" in log, log)
    expect("no skip for id-only series", "ohne ID" not in log, log)


def test_collect_titles_logs_when_series_id_missing():
    """If a panel field is unknown, the skip counter must show it (the previous 0-series symptom)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        groups = sync_meta.collect_titles({
            "one": {
                "movie": [],
                "series": [{"name": "Sherlock", "year": "2010", "cover": "x.jpg"}],
            }
        })
    log = buf.getvalue()
    expect("no series groups", all(g["type"] != "series" for g in groups), groups)
    expect("skipped without id", "1 ohne ID" in log, log)


def test_interleave_by_type_does_not_starve_series():
    movies = [{"type": "movie", "n": i} for i in range(5)]
    series = [{"type": "series", "n": i} for i in range(2)]
    out = sync_meta.interleave_by_type(movies + series)
    expect("order", [g["type"] for g in out] == [
        "movie", "series", "movie", "series", "movie", "movie", "movie"
    ], [g["type"] for g in out])
    expect("same length", len(out) == 7)


def test_list_timeout_env_per_provider():
    old = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("SYNC_")}
    try:
        for k in list(old):
            del os.environ[k]
        expect("default timeout", sync_meta.list_timeout_sec("one") == 300)
        expect("default retries", sync_meta.list_retries("premiumtv") == 2)
        os.environ["SYNC_LIST_TIMEOUT_SEC"] = "400"
        os.environ["SYNC_LIST_TIMEOUT_PREMIUMTV_SEC"] = "600"
        os.environ["SYNC_LIST_RETRIES"] = "3"
        os.environ["SYNC_LIST_RETRIES_ONE"] = "1"
        expect("shared timeout", sync_meta.list_timeout_sec("one") == 400)
        expect("provider timeout", sync_meta.list_timeout_sec("premiumtv") == 600)
        expect("shared retries", sync_meta.list_retries("premiumtv") == 3)
        expect("provider retries", sync_meta.list_retries("one") == 1)
    finally:
        for k in list(os.environ):
            if k.startswith("SYNC_"):
                del os.environ[k]
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


def test_tmdb_budget_shrinks_after_slow_lists():
    old = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("SYNC_")}
    try:
        for k in list(old):
            del os.environ[k]
        # 1 minute of list fetching: leftover 120-1-12=107, capped at 100
        minutes, sec = sync_meta.tmdb_budget_after(60)
        expect("cap still 100 after short lists", minutes == 100 and sec == 100 * 60, minutes)
        # 25 minutes of list fetching: leftover 120-25-12=83
        minutes, sec = sync_meta.tmdb_budget_after(25 * 60)
        expect("shrinks after slow lists", minutes == 83 and sec == 83 * 60, minutes)
        # Almost no time left
        minutes, sec = sync_meta.tmdb_budget_after(115 * 60)
        expect("zero when margin exhausted", minutes == 0 and sec == 0, minutes)
        os.environ["SYNC_JOB_TIMEOUT_MIN"] = "120"
        os.environ["SYNC_JOB_MARGIN_MIN"] = "10"
        os.environ["SYNC_TIME_BUDGET_MIN"] = "50"
        minutes, _ = sync_meta.tmdb_budget_after(20 * 60)
        expect("cap can be lower than leftover", minutes == 50, minutes)
    finally:
        for k in list(os.environ):
            if k.startswith("SYNC_"):
                del os.environ[k]
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


def test_list_fetch_logs_status_size_time(base: str):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        items = sync_meta.fetch_xtream_list(
            base + "/one", mock_server.ONE_USER, mock_server.ONE_PASS, "get_series", timeout=5, retries=1
        )
    log = buf.getvalue()
    expect("got series rows", isinstance(items, list) and len(items) >= 2, items)
    expect("http status logged", "Antwort HTTP 200" in log, log)
    expect("size logged", "Bytes" in log, log)
    expect("time logged", "s)" in log or " in " in log, log)
    expect("no password in log", mock_server.ONE_PASS not in log, log)


# ---------------------------------------------------------------------------
# Titles file uploaded by the Manager app (data/titles_premiumtv.json.gz)
# ---------------------------------------------------------------------------

def _iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_titles_file(data_dir, provider="premiumtv", age_days=0.5, movie=None, series=None, extra=None):
    now = datetime.now(timezone.utc)
    doc = {
        "format": "premiumtv-titles",
        "version": 1,
        "provider": provider,
        "generatedAt": _iso(now - timedelta(days=age_days)),
        "app": "de.premiumtv.manager 1.5.45",
        "movie": movie if movie is not None else [
            {"id": "1001", "name": "DE - Der Pate (1972) 4K", "year": "1972", "tmdb": 238, "cat": "12"},
            {"id": "1002", "name": "| DE | The Matrix - 1999", "year": "1999"},
        ],
        "series": series if series is not None else [
            {"id": "2001", "name": "Breaking Bad (US) (2008)", "tmdb": "1396"},
        ],
    }
    if extra:
        doc.update(extra)
    doc["counts"] = {"movie": len(doc["movie"]), "series": len(doc["series"])}
    path = Path(data_dir) / f"titles_{provider}.json.gz"
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as fh:
        fh.write(json.dumps(doc).encode("utf-8"))
    return path


def run_sync_captured(env, data_dir):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = run_sync(env, data_dir)
    return code, buf.getvalue()


def test_fresh_titles_file_skips_ptv_server(base: str):
    get_json(base + "/stats/reset")
    with tempfile.TemporaryDirectory() as td:
        write_titles_file(td, age_days=1)
        code, out = run_sync_captured(both_providers(base), td)
        expect("exit 0", code == 0, out)
        st = get_json(base + "/stats")
        expect("ptv server not contacted", {x["provider"] for x in st["xtream"]} == {"one"}, st["xtream"])
        idx = json.loads((Path(td) / "index.json").read_text())
        expect("ptv movie from file", idx["byId"].get("premiumtv:movie:1001") == "movie:238", idx["byId"])
        expect("ptv matrix from file", idx["byId"].get("premiumtv:movie:1002") == "movie:603", idx["byId"])
        expect("ptv series from file", idx["byId"].get("premiumtv:series:2001") == "tv:1396", idx["byId"])
        expect("one still live", idx["byId"].get("one:movie:502") == "movie:27205", idx["byId"])
        expect("source logged", "Quelle Titeldatei titles_premiumtv.json.gz" in out, out)
        expect("age logged", "Alter 1.0 Tage" in out, out)
        expect("source in stats", idx["stats"]["sources"]["premiumtv"]["source"] == "titles-file", idx["stats"])
        expect("one source server", idx["stats"]["sources"]["one"]["source"] == "server", idx["stats"])
        expect("no secrets in log", mock_server.PTV_PASS not in out and mock_server.ONE_PASS not in out)


def test_titles_file_without_ptv_secrets(base: str):
    get_json(base + "/stats/reset")
    with tempfile.TemporaryDirectory() as td:
        write_titles_file(td, age_days=0)
        env = {k: v for k, v in both_providers(base).items() if not k.startswith("PTV_")}
        code, out = run_sync_captured(env, td)
        expect("exit 0", code == 0, out)
        idx = json.loads((Path(td) / "index.json").read_text())
        expect("ptv ids present", idx["byId"].get("premiumtv:series:2001") == "tv:1396", idx["byId"])
        expect("providers stat", idx["stats"]["providers"] == ["one", "premiumtv"], idx["stats"])


def test_titles_file_only_no_logins_at_all(base: str):
    get_json(base + "/stats/reset")
    with tempfile.TemporaryDirectory() as td:
        write_titles_file(td, age_days=0)
        env = {"TMDB_API_KEY": mock_server.TMDB_KEY, "TMDB_API_BASE": base + "/3"}
        code, out = run_sync_captured(env, td)
        expect("exit 0", code == 0, out)
        expect("not treated as missing secrets", "kein Anbieter-Zugang" not in out, out)
        st = get_json(base + "/stats")
        expect("no xtream calls", st["xtream"] == [], st["xtream"])
        idx = json.loads((Path(td) / "index.json").read_text())
        expect("only ptv ids", all(k.startswith("premiumtv:") for k in idx["byId"]), list(idx["byId"]))


def test_stale_titles_file_uses_live_server_when_reachable(base: str):
    get_json(base + "/stats/reset")
    with tempfile.TemporaryDirectory() as td:
        write_titles_file(td, age_days=9, movie=[{"id": "9999", "name": "Nur In Alter Datei"}], series=[])
        code, out = run_sync_captured(both_providers(base), td)
        expect("exit 0", code == 0, out)
        st = get_json(base + "/stats")
        expect("ptv server contacted", "ptv" in {x["provider"] for x in st["xtream"]}, st["xtream"])
        expect("stale logged", "Titeldatei ist zu alt" in out and "Alter 9.0 Tage" in out, out)
        idx = json.loads((Path(td) / "index.json").read_text())
        expect("live ptv ids", "premiumtv:movie:1001" in idx["byId"], idx["byId"])
        expect("stale file ignored", "premiumtv:movie:9999" not in idx["byId"], idx["byId"])
        expect("source server", idx["stats"]["sources"]["premiumtv"]["source"] == "server", idx["stats"])


def test_stale_titles_file_fallback_when_server_unreachable(base: str):
    get_json(base + "/stats/reset")
    with tempfile.TemporaryDirectory() as td:
        env = both_providers(base)
        # First a full run so the cache contains entries that must not be pruned.
        code, _ = run_sync_captured(env, td)
        expect("seed exit 0", code == 0)
        write_titles_file(td, age_days=10, movie=[{"id": "1001", "name": "DE - Der Pate (1972) 4K", "year": "1972"}], series=[])
        env = dict(env)
        env["PTV_URL"] = "http://127.0.0.1:9"  # closed local port -> connection refused
        env["SYNC_LIST_TIMEOUT_PREMIUMTV_SEC"] = "2"
        env["SYNC_LIST_RETRIES_PREMIUMTV"] = "1"
        code, out = run_sync_captured(env, td)
        expect("exit 0", code == 0, out)
        expect("fallback logged", "nutze stattdessen die alte Titeldatei" in out, out)
        expect("no prune logged", "nichts aus dem Cache entfernt" in out, out)
        meta = json.loads((Path(td) / "meta.json").read_text())
        expect("breaking bad kept (no prune)", "tv:1396" in meta["entries"], list(meta["entries"]))
        expect("matrix kept (no prune)", "movie:603" in meta["entries"], list(meta["entries"]))
        idx = json.loads((Path(td) / "index.json").read_text())
        expect("ptv id from stale file", idx["byId"].get("premiumtv:movie:1001") == "movie:238", idx["byId"])


def test_titles_age_limit_env():
    with tempfile.TemporaryDirectory() as td:
        write_titles_file(td, age_days=3)
        old = dict(os.environ)
        try:
            os.environ["SYNC_TITLES_MAX_AGE_DAYS"] = "2"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _l, _i, status = sync_meta.try_titles_file(Path(td), "premiumtv", datetime.now(timezone.utc))
            expect("stale with 2 day limit", status == "stale", status)
            os.environ["SYNC_TITLES_MAX_AGE_DAYS_PREMIUMTV"] = "5"
            with contextlib.redirect_stdout(buf):
                _l, _i, status = sync_meta.try_titles_file(Path(td), "premiumtv", datetime.now(timezone.utc))
            expect("fresh with per-provider 5 day limit", status == "fresh", status)
        finally:
            os.environ.clear()
            os.environ.update(old)


def test_titles_file_rejects_bad_input():
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            expect("missing", sync_meta.try_titles_file(d, "premiumtv", now)[2] == "missing")
            (d / "titles_premiumtv.json.gz").write_bytes(b"not gzip at all")
            expect("not gzip", sync_meta.try_titles_file(d, "premiumtv", now)[2] == "invalid")
            write_titles_file(td, extra={"format": "something-else"})
            expect("wrong format", sync_meta.try_titles_file(d, "premiumtv", now)[2] == "invalid")
            write_titles_file(td, extra={"generatedAt": ""})
            expect("no timestamp", sync_meta.try_titles_file(d, "premiumtv", now)[2] == "invalid")
            write_titles_file(td, age_days=-5)
            expect("future timestamp", sync_meta.try_titles_file(d, "premiumtv", now)[2] == "invalid")
            write_titles_file(td, movie=[], series=[])
            expect("empty", sync_meta.try_titles_file(d, "premiumtv", now)[2] == "invalid")
        expect("reasons logged", "unbrauchbar" in buf.getvalue(), buf.getvalue())


def test_titles_file_keeps_only_safe_fields():
    with tempfile.TemporaryDirectory() as td:
        path = write_titles_file(td, movie=[{
            "id": "77", "name": "Film", "year": "2001", "tmdb": "abc", "cat": "3",
            "stream_url": "http://host/movie/u/p/77.mkv", "password": "x", "direct_source": "http://y",
        }], series=[{"id": "", "name": "ohne id"}, {"id": "5", "name": ""}, "kaputt"])
        lists, info = sync_meta.load_titles_file(path, datetime.now(timezone.utc))
        expect("one movie", len(lists["movie"]) == 1, lists)
        expect("invalid series dropped", lists["series"] == [], lists)
        item = lists["movie"][0]
        expect("only safe keys", set(item) == {"stream_id", "name", "year", "category_id"}, item)
        expect("non-numeric tmdb dropped", "tmdb_id" not in item, item)
        expect("counts", info["movies"] == 1 and info["series"] == 0, info)


def main():
    srv = mock_server.start(0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    # Safety: these tests must never be pointed at real hosts
    expect("local mock only", "127.0.0.1" in base)

    mock_server.reset_catalogs()
    tests = [
        test_xtream_base_normalizes_urls,
        test_describe_http_error_masks_and_names_reason,
        test_item_id_accepts_panel_fallbacks,
        test_collect_titles_keeps_series_with_id_only,
        test_collect_titles_logs_when_series_id_missing,
        test_interleave_by_type_does_not_starve_series,
        test_list_timeout_env_per_provider,
        test_tmdb_budget_shrinks_after_slow_lists,
        test_missing_secrets_exits_cleanly,
        test_missing_tmdb_key_only,
        lambda: test_full_sync_and_incremental(base),
        lambda: test_one_provider_only(base),
        lambda: test_logs_do_not_contain_secrets(base, Path(".")),
        lambda: test_time_budget_stops_lookups_and_saves(base),
        lambda: test_checkpoint_survives_hard_kill(base),
        lambda: test_prune_titles_gone_from_both_providers(base),
        lambda: test_list_fetch_logs_status_size_time(base),
        test_titles_age_limit_env,
        test_titles_file_rejects_bad_input,
        test_titles_file_keeps_only_safe_fields,
        lambda: test_fresh_titles_file_skips_ptv_server(base),
        lambda: test_titles_file_without_ptv_secrets(base),
        lambda: test_titles_file_only_no_logins_at_all(base),
        lambda: test_stale_titles_file_uses_live_server_when_reachable(base),
        lambda: test_stale_titles_file_fallback_when_server_unreachable(base),
    ]
    names = [
        "test_xtream_base_normalizes_urls",
        "test_describe_http_error_masks_and_names_reason",
        "test_item_id_accepts_panel_fallbacks",
        "test_collect_titles_keeps_series_with_id_only",
        "test_collect_titles_logs_when_series_id_missing",
        "test_interleave_by_type_does_not_starve_series",
        "test_list_timeout_env_per_provider",
        "test_tmdb_budget_shrinks_after_slow_lists",
        "test_missing_secrets_exits_cleanly",
        "test_missing_tmdb_key_only",
        "test_full_sync_and_incremental",
        "test_one_provider_only",
        "test_logs_do_not_contain_secrets",
        "test_time_budget_stops_lookups_and_saves",
        "test_checkpoint_survives_hard_kill",
        "test_prune_titles_gone_from_both_providers",
        "test_list_fetch_logs_status_size_time",
        "test_titles_age_limit_env",
        "test_titles_file_rejects_bad_input",
        "test_titles_file_keeps_only_safe_fields",
        "test_fresh_titles_file_skips_ptv_server",
        "test_titles_file_without_ptv_secrets",
        "test_titles_file_only_no_logins_at_all",
        "test_stale_titles_file_uses_live_server_when_reachable",
        "test_stale_titles_file_fallback_when_server_unreachable",
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
