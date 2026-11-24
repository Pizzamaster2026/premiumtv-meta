#!/usr/bin/env python3
"""Offline tests for TMDB language fallback, year=y search, and alt titles. No network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_meta


def expect(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label} {detail}")
    print(f"PASS  {label}")


class FakeClient(sync_meta.TmdbClient):
    def __init__(self, payloads: dict):
        super().__init__(api_key="fake", base="http://127.0.0.1/3")
        self.payloads = payloads
        self.log: list[tuple[str, dict]] = []

    def get(self, path: str, params=None):
        params = dict(params or {})
        self.calls += 1
        key = (path, params.get("language") or "", params.get("query") or "")
        self.log.append((path, params))
        if path in self.payloads:
            val = self.payloads[path]
            if isinstance(val, dict) and any(isinstance(k, tuple) for k in val.keys()):
                lang = params.get("language") or ""
                if (lang,) in val:
                    return val[(lang,)]
                if lang in val:
                    return val[lang]
            return val
        # details keyed by language
        lang = params.get("language") or ""
        if (path, lang) in self.payloads:
            return self.payloads[(path, lang)]
        return None


def test_apply_text_fallback_fills_only_empty_fields():
    detail = {"overview": "Deutscher Text", "posterPath": ""}
    sync_meta.apply_text_fallback(detail, {"overview": "English text", "poster_path": "/en.jpg"})
    expect("keeps German overview", detail["overview"] == "Deutscher Text", detail)
    expect("fills empty poster", detail["posterPath"] == "/en.jpg", detail)
    detail2 = {"overview": "", "posterPath": "/de.jpg"}
    sync_meta.apply_text_fallback(detail2, {"overview": "English text", "poster_path": "/en.jpg"})
    expect("fills empty overview", detail2["overview"] == "English text", detail2)
    expect("keeps existing poster", detail2["posterPath"] == "/de.jpg", detail2)


def test_details_falls_back_en_then_original():
    client = FakeClient({
        ("/movie/15", "de-DE"): {
            "id": 15, "title": "15 ans et demi", "original_title": "15 ans et demi",
            "original_language": "fr", "release_date": "2008-01-01",
            "overview": "", "poster_path": "", "runtime": 90, "genres": [],
            "credits": {"cast": [], "crew": []},
        },
        ("/movie/15", "en-US"): {
            "id": 15, "overview": "A father reconnects with his daughter.", "poster_path": "",
        },
        ("/movie/15", "fr"): {
            "id": 15, "overview": "Un père retrouve sa fille.", "poster_path": "/fr.jpg",
        },
    })
    # Route FakeClient.get: details() calls get(path, {language, append...})
    # Our FakeClient looks up payloads[path] first as a dict - need to hook language.
    # Override get more carefully:
    raw = dict(client.payloads)

    def get(path, params=None):
        params = dict(params or {})
        client.calls += 1
        client.log.append((path, params))
        lang = params.get("language") or ""
        return raw.get((path, lang))

    client.get = get  # type: ignore
    detail = client.details("movie", 15)
    expect("has english overview first", detail["overview"] == "A father reconnects with his daughter.", detail)
    expect("has french poster afterwards", detail["posterPath"] == "/fr.jpg", detail)
    expect("detailsRev set", detail.get("detailsRev") == sync_meta.DETAILS_REV, detail)
    langs = [p.get("language") for _, p in client.log]
    expect("de then en then original fr", langs == ["de-DE", "en-US", "fr"], langs)
    expect("does not overwrite after both filled", True)


def test_details_skips_fallback_when_german_complete():
    client = FakeClient({})
    raw = {
        ("/movie/238", "de-DE"): {
            "id": 238, "title": "Der Pate", "original_title": "The Godfather",
            "original_language": "en", "release_date": "1972-03-24",
            "overview": "Don Vito Corleone …", "poster_path": "/pate.jpg",
            "runtime": 175, "genres": [], "credits": {"cast": [], "crew": []},
        },
    }

    def get(path, params=None):
        params = dict(params or {})
        client.calls += 1
        client.log.append((path, params))
        return raw.get((path, params.get("language") or ""))

    client.get = get  # type: ignore
    detail = client.details("movie", 238)
    expect("german overview kept", detail["overview"].startswith("Don Vito"), detail)
    expect("only one details call", client.calls == 1, client.calls)
    expect("german poster kept", detail["posterPath"] == "/pate.jpg", detail)


def test_search_uses_y_not_outer_year_in_noyer_fallback():
    """No-year fallback must not keep the provider year inside _pick_search."""
    client = FakeClient({})
    searches = []

    def get(path, params=None):
        params = dict(params or {})
        client.calls += 1
        searches.append(params)
        if params.get("year") == "2003":
            return {"results": []}
        if params.get("query") == "Alibi.com 2":
            return {"results": [{"id": 967585, "title": "Alibi.com 2", "release_date": "2023-02-08"}]}
        return {"results": []}

    client.get = get  # type: ignore
    tid = client.search("movie", "Alibi.com 2", "2003")
    expect("found via no-year fallback", tid == 967585, tid)
    years = [p.get("year", "") for p in searches]
    expect("first search used 2003, second used no year", years[:2] == ["2003", ""], years)


def test_search_alternative_titles_cheap_and_unique():
    client = FakeClient({})
    calls = []

    def get(path, params=None):
        params = dict(params or {})
        client.calls += 1
        calls.append(path)
        if path == "/search/movie":
            return {"results": [
                {"id": 300671, "title": "13 Hours: The Secret Soldiers of Benghazi", "release_date": "2016-01-15"},
            ]}
        if path == "/movie/300671/alternative_titles":
            return {"titles": [{"iso_3166_1": "US", "title": "13 Hours"}]}
        return None

    client.get = get  # type: ignore
    tid = client.search("movie", "13 Hours", "2016")
    expect("matched via official alt title", tid == 300671, tid)
    expect("one search plus one alt-title call", calls == ["/search/movie", "/movie/300671/alternative_titles"], calls)


def test_search_does_not_guess_first_result():
    client = FakeClient({})

    def get(path, params=None):
        client.calls += 1
        if path == "/search/movie":
            return {"results": [
                {"id": 1, "title": "Something Else", "release_date": "2016-01-01"},
                {"id": 2, "title": "Also Wrong", "release_date": "2016-06-01"},
            ]}
        # Should not even fetch alt titles when more than ALT_TITLE_MAX? 2 <= 5, so it may fetch.
        if "alternative_titles" in path:
            return {"titles": []}
        return {"results": []}

    client.get = get  # type: ignore
    tid = client.search("movie", "Unknown Film XYZQQ", "2016")
    expect("unknown title stays unmatched", tid is None, tid)


def main() -> int:
    tests = [
        test_apply_text_fallback_fills_only_empty_fields,
        test_details_falls_back_en_then_original,
        test_details_skips_fallback_when_german_complete,
        test_search_uses_y_not_outer_year_in_noyer_fallback,
        test_search_alternative_titles_cheap_and_unique,
        test_search_does_not_guess_first_result,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    if failed:
        print(f"{failed} failed")
        return 1
    print(f"{len(tests)} tests ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
