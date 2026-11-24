#!/usr/bin/env python3
"""Title reclean / miss retry / safe TMDB pick. No network, no IPTV, no TMDB."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_meta
from title import clean, title_key


def expect(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label} {detail}")
    print(f"PASS  {label}")


def main() -> int:
    now = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)
    fresh = {"tmdbId": None, "query": "11- Pulp Fiction", "year": "1994", "type": "movie",
             "updatedAt": "2026-11-16T16:21:36Z", "searchRev": 1}

    g_new = {"type": "movie", "query": "Pulp Fiction", "year": "1994",
             "refs": [{"provider": "premiumtv", "name": "(DE-) -6- Pulp Fiction - 1994", "type": "movie", "year": "1994"}]}
    expect("query changed after rank strip", sync_meta.miss_query_changed(fresh, g_new))
    expect("retry because query changed", sync_meta.miss_should_retry(fresh, g_new, now))

    g_same = {"type": "movie", "query": "11- Pulp Fiction", "year": "1994",
              "refs": [{"provider": "premiumtv", "name": "(DE-) -6- Pulp Fiction - 1994"}]}
    # stored query already matches group query; searchRev 1 < 2 and not adult → retry once
    same_miss = dict(fresh, query="11- Pulp Fiction")
    expect("retry non-adult old searchRev", sync_meta.miss_should_retry(same_miss, g_same, now))

    current = dict(fresh, query="Pulp Fiction", searchRev=sync_meta.SEARCH_REV)
    g_ok = {"type": "movie", "query": "Pulp Fiction", "year": "1994", "refs": []}
    expect("fresh current rev stays skipped", sync_meta.miss_should_retry(current, g_ok, now) is False)

    adult_g = {"type": "movie", "query": "(XXX-) Tiny4K - Foo", "year": "",
               "refs": [{"name": "(XXX-) Tiny4K - Foo"}]}
    adult_miss = {"tmdbId": None, "query": "(XXX-) Tiny4K - Foo", "year": "", "type": "movie",
                  "updatedAt": "2026-11-16T16:21:36Z", "searchRev": 0}
    expect("adult group detected", sync_meta.is_adult_group(adult_g))
    expect("adult fresh miss not retried for rev bump", sync_meta.miss_should_retry(adult_miss, adult_g, now) is False)
    adult_tag = {"type": "movie", "query": "(XXX-) Amilia Onyx - Test Shoot", "year": "2017",
                 "refs": [{"name": "(XXX-) [RodneyMoore.com] - Amilia Onyx - Test Shoot -1080p"}]}
    adult_old = {"tmdbId": None, "query": "(XXX-) Amilia Onyx - Test Shoot -1080p", "year": "2017",
                 "type": "movie", "updatedAt": "2026-11-16T16:21:36Z", "searchRev": 1}
    expect("adult query change from tag strip is ignored",
           sync_meta.miss_query_changed(adult_old, adult_tag) and
           sync_meta.miss_should_retry(adult_old, adult_tag, now) is False)

    # Expired miss is retried even if adult
    old = dict(adult_miss, updatedAt=(now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    expect("expired miss retried", sync_meta.miss_should_retry(old, adult_g, now))
    expect("adult never queued for TMDB lookup",
           sync_meta.needs_tmdb_lookup(adult_g, {}, {sync_meta.title_key("movie", adult_g["query"], ""): old}, {}, now) is False)

    # _pick_search: exact / equivalent vs first-result / strict
    data = {"results": [
        {"id": 1, "title": "Something Else", "original_title": "Andere"},
        {"id": 238, "title": "Der Pate", "original_title": "The Godfather"},
    ]}
    hit = sync_meta._pick_search(data, "Der Pate")
    expect("exact title preferred over first", hit and hit["id"] == 238, hit)
    hit2 = sync_meta._pick_search(data, "The Godfather")
    expect("original_title matches", hit2 and hit2["id"] == 238, hit2)
    loose = sync_meta._pick_search(data, "Unknown Film", strict=False)
    expect("first-result guess rejected even for old callers", loose is None, loose)
    strict = sync_meta._pick_search(data, "Unknown Film", strict=True)
    expect("strict fallback rejects mismatch", strict is None, strict)
    apos = sync_meta._pick_search({"results": [{"id": 424, "title": "Schindler's List"}]}, "Schindlers List", strict=True)
    expect("strict accepts apostrophe-equivalent", apos and apos["id"] == 424, apos)
    uml = sync_meta._pick_search({"results": [{"id": 330457, "title": "Die Eiskönigin 2"}]}, "Die Eiskoenigin 2", strict=True)
    expect("strict accepts umlaut-equivalent", uml and uml["id"] == 330457, uml)

    # Year filter: with-year still requires ±1; no-year fallback (year="") may accept a unique title.
    dunes = {"results": [
        {"id": 1, "title": "Dune", "release_date": "1984-01-01"},
        {"id": 2, "title": "Dune", "release_date": "2021-01-01"},
    ]}
    expect("two same-name years stay ambiguous without year", sync_meta._pick_search(dunes, "Dune") is None)
    expect("year picks the matching Dune", sync_meta._pick_search(dunes, "Dune", year="2021")["id"] == 2)
    expect("wrong year still rejected", sync_meta._pick_search(dunes, "Dune", year="2000") is None)
    alibi = {"results": [{"id": 967585, "title": "Alibi.com 2", "release_date": "2023-02-08"}]}
    expect("with-year rejects Alibi.com 2 dated 2003", sync_meta._pick_search(alibi, "Alibi.com 2", year="2003") is None)
    expect("no-year unique Alibi.com 2 accepted", sync_meta._pick_search(alibi, "Alibi.com 2", year="")["id"] == 967585)
    article = {"results": [{"id": 384521, "title": "The Cloverfield Paradox", "release_date": "2018-02-04"}]}
    expect("leading The is ignorable", sync_meta._pick_search(article, "Cloverfield Paradox", year="2018")["id"] == 384521)
    boys = {"results": [{"id": 9737, "title": "Bad Boys", "release_date": "1995-04-07"}]}
    expect("trailing sequel 1 is ignorable", sync_meta._pick_search(boys, "Bad Boys 1", year="1995")["id"] == 9737)
    expect("trailing sequel 2 is not", sync_meta._pick_search(boys, "Bad Boys 2", year="1995") is None)
    baahu = {"results": [{"id": 350312, "title": "Bāhubali 2: The Conclusion", "release_date": "2017-04-27"}]}
    expect("diacritics fold", sync_meta._pick_search(baahu, "Baahubali 2: The Conclusion", year="2017")["id"] == 350312)
    # Alternative titles are cheap and still require a unique equivalent name.
    hours = {"results": [
        {"id": 300671, "title": "13 Hours: The Secret Soldiers of Benghazi", "release_date": "2016-01-15"},
        {"id": 99, "title": "Unrelated", "release_date": "2016-01-01"},
    ]}
    expect("subtitle is not a loose prefix match", sync_meta._pick_search(hours, "13 Hours", year="2016") is None)
    alts = {300671: ["13 Hours"]}
    expect("official alt title is a unique match",
           sync_meta._pick_search(hours, "13 Hours", year="2016", alt_names=alts)["id"] == 300671)
    two_alts = {300671: ["13 Hours"], 99: ["13 Hours"]}
    expect("two alt-title hits stay ambiguous",
           sync_meta._pick_search(hours, "13 Hours", year="2016", alt_names=two_alts) is None)
    expect("SEARCH_REV bumped for rematch", sync_meta.SEARCH_REV >= 4)

    # title_key re-cleans, so a miss recorded under "11- Pulp Fiction" is still
    # found after the rank is stripped. Retry is driven by the stored query, not a new key.
    entries, prev = {}, {}
    old_key = title_key("movie", "11- Pulp Fiction", "1994")
    new_key = title_key("movie", "Pulp Fiction", "1994")
    expect("title_key is stable after reclean", old_key == new_key, f"{old_key} vs {new_key}")
    misses = {new_key: fresh}
    expect("lookup when stored miss query still has rank leftover", sync_meta.needs_tmdb_lookup(g_new, entries, misses, prev, now))
    misses2 = {new_key: current}
    expect("skip when current miss already used new query and rev", not sync_meta.needs_tmdb_lookup(g_ok, entries, misses2, prev, now))

    # Cap: more rematch candidates than SYNC_REMATCH_MAX; only the first N increment rematched.
    cap = 2
    rematched = 0
    accepted = skipped_cap = 0
    candidates = [g_new, dict(g_new, query="Die Verurteilten", year="1994"),
                  dict(g_new, query="Der Pate", year="1972")]
    miss_rec = {"tmdbId": None, "query": "11- Old", "year": "1994", "type": "movie",
                "updatedAt": "2026-11-16T16:21:36Z", "searchRev": 1}
    for g in candidates:
        miss = miss_rec
        retry_miss = sync_meta.miss_should_retry(miss, g, now)
        expect("candidate is a rematch", retry_miss)
        if rematched >= cap:
            skipped_cap += 1
            continue
        rematched += 1
        accepted += 1
    expect("cap accepts first N", accepted == 2 and rematched == 2, f"{accepted}/{rematched}")
    expect("cap leaves the rest as misses", skipped_cap == 1, str(skipped_cap))

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
