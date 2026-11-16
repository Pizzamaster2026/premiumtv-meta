#!/usr/bin/env python3
"""sport.json step: classifier, XMLTV parsing, limits, and the sync integration.

Fixtures only (tests/fixtures/xmltv_sport.xml + local mock server). Never contacts a real host.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import mock_server  # noqa: E402
import sport  # noqa: E402
import sync_meta  # noqa: E402
from test_sync import both_providers, expect, run_sync  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "xmltv_sport.xml"
NOW = int(datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc).timestamp())   # 15:00 Berlin


def chunks(data: bytes, n: int = 97):
    for i in range(0, len(data), n):
        yield data[i:i + n]


def parsed(now=NOW, days=3):
    items, st = sport.parse_xmltv(chunks(FIX.read_bytes()), now, days=days)
    return items, st, {(i["epg"], i["title"]): i for i in items}


def test_classifier_title_only():
    c = sport.classify
    expect("prosieben show is no sport even if desc/cat would say so", c("Wer stiehlt mir die Show?", "", ["Show"], "ProSieben") is None)
    expect("RTL NFL title is sport", c("NFL: Kansas City Chiefs – Buffalo Bills", "", [], "RTL")[0] == "nfl")
    expect("news is not sport", c("heute", "", [], "ZDF") is None)
    expect("sport news is not sport", c("Sky Sport News", "", ["Sport"], "Sky Sport News") is None)
    expect("teleshopping on sport channel", c("Teleshopping", "", ["Sport"], "Sky Sport Bundesliga") is None)
    expect("crime show on normal channel", c("Navy CIS", "", ["Krimiserie"], "Kabel Eins") is None)
    expect("Tatort 'Abseits' is not sport", c("Tatort: Abseits", "", ["Krimi"], "Das Erste") is None)
    expect("bundesliga via sub-title", c("Bayern München – Borussia Dortmund", "Bundesliga, 11. Spieltag")[0] == "bl")
    expect("2. Bundesliga", c("2. Bundesliga: HSV – Schalke 04")[0] == "bl2")
    expect("champions league", c("UEFA Champions League: Real – City")[0] == "cl")
    expect("dfb pokal", c("DFB-Pokal: Achtelfinale")[0] == "dfb")
    expect("premier league", c("Premier League: Arsenal – Chelsea")[0] == "pl")
    expect("laliga -> fb", c("Real Madrid – FC Barcelona", "LaLiga")[0] == "fb")
    expect("f1", c("Formel 1: Großer Preis von Brasilien – Rennen")[0] == "f1")
    expect("motogp", c("MotoGP: Valencia")[0] == "moto")
    expect("nhl", c("NHL: Oilers – Kings")[0] == "ih")
    expect("DEL", c("Eishockey: Adler Mannheim – Kölner Haie")[0] == "ih")
    expect("tennis", c("Tennis: ATP Finals Turin")[0] == "tennis")
    expect("ufc", c("UFC Fight Night")[0] == "box")
    expect("handball", c("Handball-EM: Deutschland – Dänemark")[0] == "hb")
    expect("darts", c("Darts: PDC Grand Slam of Darts")[0] == "darts")
    expect("ski -> other", c("Ski Alpin: Weltcup in Levi")[0] == "other")
    # v0.2.12: the channel name never counts – a match-like title needs a sport category or recognisable teams
    expect("match title on sport channel without category -> nothing", c("Alba Berlin – Bayern", "", [], "DAZN 1") is None)
    expect("match title + sport category -> other", c("Alba Berlin – Bayern", "", ["Sport"], "DAZN 1")[0] in ("other", "nba"))
    expect("Hausmeister Krause on Sport1 is no sport", c("Hausmeister Krause - Ordnung muss sein", "", ["Sport"], "DE: Sport1 HD") is None)
    expect("Garagengold on Magenta is no sport", c("Garagengold", "", ["Sport"], "┃DE┃ MAGENTA SPORT 1 HD") is None)
    expect("SpongeBob is no bobsleigh", c("SpongeBob Schwammkopf") is None)
    expect("placeholder title = channel name", c("Magenta Sport 7", "", [], "┃DE┃ MAGENTA SPORT 7 HD") is None)
    expect("placeholder against any display name", c("Magenta Sport 7", "", [], ["┃DE┃ MAGENTA SPORT 7 HD", "magentasport7.de"]) is None)
    expect("Kreisliga is sport", c("Kreisliga A: TSV Hintertupfing - SG Oberdorf")[0] == "fb")
    expect("match title on non-sport channel without category -> nothing", c("Liebe – oder was?", "", [], "Sat.1") is None)
    expect("formel 1 magazine is not live sport", c("Formel 1 – Das Magazin") is None)


def test_shared_cases_with_player():
    """same expectations as the player (tests/unit/sport_cases0212.json)"""
    cases = json.loads((ROOT / "tests" / "fixtures" / "sport_cases.json").read_text(encoding="utf-8"))["cases"]
    bad = []
    for title, cat, ch, exp in cases:
        k = sport.classify(title, "", [cat] if cat else [], ch)
        if (k[0] if k else None) != exp:
            bad.append((title, cat, ch, k, exp))
    expect(f"{len(cases)} shared cases", not bad, bad)


def test_xmltv_parse_window_and_names():
    items, st, by = parsed()
    titles = sorted(t for (_e, t) in by)
    expect("programmes counted", st["programmes"] == 20, st)
    expect("v0.2.12 misfires dropped", not {"Hausmeister Krause - Ordnung muss sein", "Garagengold", "SpongeBob Schwammkopf"} & set(titles), titles)
    expect("prosieben dropped", ("prosieben.de", "Wer stiehlt mir die Show?") not in by)
    expect("rtl nfl kept", by[("rtl.de", "NFL: Kansas City Chiefs – Buffalo Bills")]["kind"] == "nfl")
    expect("bundesliga kept", by[("skysportbundesliga1.de", "Bayern München – Borussia Dortmund")]["kind"] == "bl")
    expect("teleshopping dropped", all(t != "Teleshopping" for t in titles))
    expect("crime + news dropped", not {"Navy CIS", "heute", "Tatort: Abseits", "Sport1 News"} & set(titles), titles)
    expect("future > 3 days dropped", not any("zu weit" in t for t in titles))
    expect("ended dropped", not any("vorbei" in t for t in titles))
    f1 = by[("skysportf1.de", "Formel 1: Großer Preis von Brasilien – Rennen")]
    expect("channel declared after programme gets its name", f1["ch"] == ["DE: Sky Sport F1 HD"], f1)
    expect("times are utc epoch", by[("rtl.de", "NFL: Kansas City Chiefs – Buffalo Bills")]["start"] == NOW - 1800)
    expect("sorted by start", [i["start"] for i in items] == sorted(i["start"] for i in items))
    expect("exact set", len(items) == 9, titles)
    for i in items:
        expect("item shape", set(i) - {"cat"} == {"epg", "ch", "title", "start", "end", "kind", "league"}, i)
    expect("sport category passed on", by[("rtl.de", "NFL: Kansas City Chiefs – Buffalo Bills")].get("cat") == "Sport")
    expect("1 day window is shorter", len(parsed(days=1)[0]) < len(items))


def test_xmltv_ts():
    expect("offset", sport.xmltv_ts("20261115150000 +0100") == NOW)
    expect("no offset = utc", sport.xmltv_ts("20261115140000") == NOW)
    expect("garbage", sport.xmltv_ts("tomorrow") is None)


def test_limits():
    try:
        sport.parse_xmltv(chunks(FIX.read_bytes()), NOW, max_bytes=1000)
        raise AssertionError("size cap not enforced")
    except ValueError:
        pass
    try:
        sport.parse_xmltv(chunks(FIX.read_bytes()), NOW, deadline=time.monotonic() - 1)
        raise AssertionError("deadline not enforced")
    except TimeoutError:
        pass
    # truncated download: keep what was parsed, no exception
    data = FIX.read_bytes()
    items, _st = sport.parse_xmltv([data[: len(data) * 2 // 3]], NOW)
    expect("truncated xml keeps earlier items", len(items) >= 3, len(items))


def test_run_step_never_raises_and_keeps_old_file():
    logs = []
    with tempfile.TemporaryDirectory() as d:
        old = {"v": 1, "generatedAt": 1, "items": [{"title": "alt"}]}
        (Path(d) / "sport.json").write_text(json.dumps(old))
        prov = {"url": "http://127.0.0.1:9", "user": "u$er", "pass": "p@ss wort"}

        def boom(url, deadline, ua):
            raise OSError("connection refused for " + url)
        r = sport.run_step(Path(d), prov, sync_meta.xtream_base, logs.append, sync_meta.mask_url, "ua", now=NOW, opener=boom)
        expect("failed", r == "failed")
        expect("old file kept", json.loads((Path(d) / "sport.json").read_text()) == old)
        joined = "\n".join(logs)
        expect("no secrets in log", "p@ss" not in joined and "u$er" not in joined and "p%40ss" not in joined and "u%24er" not in joined, joined)
        r = sport.run_step(Path(d), None, sync_meta.xtream_base, logs.append, sync_meta.mask_url, "ua")
        expect("skipped without login", r == "skipped")
        r = sport.run_step(Path(d), prov, sync_meta.xtream_base, logs.append, sync_meta.mask_url, "ua", now=NOW,
                           opener=lambda u, dl, ua: chunks(FIX.read_bytes()))
        doc = json.loads((Path(d) / "sport.json").read_text())
        expect("ok writes", r == "ok" and doc["v"] == 1 and doc["generatedAt"] == NOW and len(doc["items"]) == 9 and doc["days"] == 3, doc)


def sync_env(base, **extra):
    env = both_providers(base)
    env.update(extra)
    return env


def test_sync_builds_sport_json(base):
    for mode in ("ok", "gzip"):
        mock_server.XMLTV["mode"] = mode
        mock_server.STATS.reset()
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run_sync(sync_env(base), d)
            out = buf.getvalue()
            expect("sync ok", rc == 0, out)
            p = Path(d) / "sport.json"
            expect("sport.json written " + mode, p.is_file(), out)
            doc = json.loads(p.read_text())
            expect("format", doc["v"] == 1 and doc["source"] == "one-xmltv" and isinstance(doc["items"], list))
            expect("one xmltv request, one provider only", mock_server.STATS.xmltv == [{"provider": "one"}], mock_server.STATS.xmltv)
            expect("meta still written", (Path(d) / "index.json").is_file())
            expect("no secrets in log", mock_server.ONE_PASS not in out and mock_server.ONE_USER not in out)
            expect("no stream calls", mock_server.STATS.forbidden == [])
    mock_server.XMLTV["mode"] = "ok"


def test_sync_survives_sport_failures(base):
    for mode, extra in (("404", {}), ("garbage", {}), ("slow", {"SYNC_SPORT_BUDGET_SEC": "10"})):
        mock_server.XMLTV["mode"] = mode
        with tempfile.TemporaryDirectory() as d:
            old = {"v": 1, "generatedAt": 5, "items": []}
            (Path(d) / "sport.json").write_text(json.dumps(old))
            buf = io.StringIO()
            t0 = time.monotonic()
            with contextlib.redirect_stdout(buf):
                rc = run_sync(sync_env(base, **extra), d)
            out = buf.getvalue()
            expect(f"sync ok despite {mode}", rc == 0 and (Path(d) / "index.json").is_file(), out)
            expect(f"old sport.json kept on {mode}", json.loads((Path(d) / "sport.json").read_text()) == old, out)
            expect(f"logged {mode}", "Sportplan: übersprungen" in out or "alte sport.json bleibt" in out, out)
            if mode == "slow":
                expect("budget bounds the slow download", time.monotonic() - t0 < 25, time.monotonic() - t0)
    mock_server.XMLTV["mode"] = "ok"


def test_sync_sport_switch_off(base):
    mock_server.STATS.reset()
    with tempfile.TemporaryDirectory() as d:
        with contextlib.redirect_stdout(io.StringIO()):
            run_sync(sync_env(base, SYNC_SPORT="0"), d)
        expect("no sport.json when off", not (Path(d) / "sport.json").exists())
        expect("no xmltv request when off", mock_server.STATS.xmltv == [])


def test_sport_only_cli(base):
    mock_server.STATS.reset()
    env = sync_env(base)
    with tempfile.TemporaryDirectory() as d:
        old = dict(os.environ)
        try:
            os.environ.update(env)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sync_meta.main(["--data-dir", d, "--sport-only"])
        finally:
            os.environ.clear()
            os.environ.update(old)
        expect("sport-only rc 0", rc == 0)
        expect("sport-only writes sport.json", (Path(d) / "sport.json").is_file(), buf.getvalue())
        expect("sport-only: no list calls, no tmdb", mock_server.STATS.xtream == [] and mock_server.STATS.tmdb == [])
        expect("sport-only: no meta files", not (Path(d) / "index.json").exists())


def main():
    srv = mock_server.start(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    expect("local mock only", "127.0.0.1" in base)
    mock_server.reset_catalogs()
    tests = [test_classifier_title_only, test_shared_cases_with_player, test_xmltv_parse_window_and_names, test_xmltv_ts, test_limits,
             test_run_step_never_raises_and_keeps_old_file,
             lambda: test_sync_builds_sport_json(base), lambda: test_sync_survives_sport_failures(base),
             lambda: test_sync_sport_switch_off(base), lambda: test_sport_only_cli(base)]
    names = ["classifier", "shared_cases_with_player", "xmltv_parse", "xmltv_ts", "limits", "run_step", "sync_builds_sport_json",
             "sync_survives_sport_failures", "sport_switch_off", "sport_only_cli"]
    fails = 0
    for n, t in zip(names, tests):
        try:
            t()
            print(f"PASS  {n}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"FAIL  {n}: {e}")
    srv.shutdown()
    print(f"{len(tests)} sport tests " + ("ALL PASS" if not fails else f"{fails} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
