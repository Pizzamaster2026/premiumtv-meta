#!/usr/bin/env python3
"""Unit tests for title cleaning / matching. No network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from title import clean, extract_tmdb_id, looks_adult, names_equivalent, search_queries, title_key, umlaut_variant

# Cases mirrored from premiumtv-player/tests/unit/tmdbclean021.test.js
PLAYER_CASES = [
    ("DE - Der Pate (1972) 4K", "", "Der Pate", "1972"),
    ("Avatar: The Way of Water (2022)", "", "Avatar: The Way of Water", "2022"),
    ("[DE] Inception", "2010", "Inception", "2010"),
    ("DE| Matrix [4K]", "", "Matrix", ""),
    ("Dune: Part Two 4K UHD", "2024-02-29", "Dune: Part Two", "2024"),
    ("Das Boot (DE)", "", "Das Boot", ""),
    ("Breaking Bad (US) (2008)", "", "Breaking Bad", "2008"),
    ("EN - The Office", "", "The Office", ""),
    ("Oppenheimer 2023", "", "Oppenheimer", "2023"),
    ("1917", "2019", "1917", "2019"),
    ("2012", "", "2012", ""),
    ("Blade Runner 2049", "2017", "Blade Runner 2049", "2017"),
    ("Die letzte Front", "2026", "Die letzte Front", "2026"),
    ("Königsreich 2", "", "Königsreich 2", ""),
    ("DE: Tatort - Kalter Tag", "", "Tatort - Kalter Tag", ""),
    ("Top Gun: Maverick (2022) [DE] HEVC", "", "Top Gun: Maverick", "2022"),
    ("MULTI - Amélie", "", "Amélie", ""),
    ("Herr der Ringe - Die Gefährten (2001) FHD", "", "Herr der Ringe - Die Gefährten", "2001"),
    ("TR - Kurtlar Vadisi", "", "Kurtlar Vadisi", ""),
    ("Fast & Furious 9 (2021) (OmU)", "", "Fast & Furious 9", "2021"),
    ("Up", "", "Up", ""),
]

# PremiumTV catalogue forms (ranks after (DE-), quality glued on, Multi-Sub)
PREMIUMTV_CASES = [
    ("(DE-) -11- Der Herr Der Ringe: Die Gefaehrten - 2001", "", "Der Herr Der Ringe: Die Gefaehrten", "2001"),
    ("(DE-) -1- Die Verurteilten - 1994", "", "Die Verurteilten", "1994"),
    ("(DE-) -6- Pulp Fiction - 1994", "", "Pulp Fiction", "1994"),
    ("(DE-) -7- Schindler's List - 1993", "", "Schindler's List", "1993"),
    ("11- Der Herr der Ringe", "", "Der Herr der Ringe", ""),
    ("01 | The Matrix", "", "The Matrix", ""),
    ("12. Inception", "", "Inception", ""),
    # Leading number is part of the name — do not strip
    ("6 Ballons", "2018", "6 Ballons", "2018"),
    ("6 Leberkaes Junkie", "2019", "6 Leberkaes Junkie", "2019"),
    ("1917", "2019", "1917", "2019"),
    ("2012", "", "2012", ""),
    ("Blade Runner 2049", "2017", "Blade Runner 2049", "2017"),
    ("(DE-) Lefter: Die Geschichte Der Fußballlegende Ordinarius 4K - 2025", "", "Lefter: Die Geschichte Der Fußballlegende Ordinarius", "2025"),
    ("Top Gun: Maverick (2022) Multi-Sub 1080p", "", "Top Gun: Maverick", "2022"),
    ("Avatar (2022) -1080p", "", "Avatar", "2022"),
    ("Dune Part Two 3D UHD", "2024", "Dune Part Two", "2024"),
    # Adult prefix stays, so existing miss keys are not churned
    ("(XXX-) Tiny4K - Big Dick and Bubbles", "", "(XXX-) Tiny4K - Big Dick and Bubbles", ""),
]

# Extra forms seen on provider lists / detail pages
EXTRA_CASES = [
    ("| DE | The Matrix - 1999", "", "The Matrix", "1999"),
    ("| DE | Stallone", "", "Stallone", ""),
    ("DE | DE | Oppenheimer 2023", "", "Oppenheimer", "2023"),
    ("Something - 2013", "", "Something", "2013"),
    ("[DE] Foo Bar [4K]", "2018", "Foo Bar", "2018"),
    ("  DE -  The Godfather (1972) ", "", "The Godfather", "1972"),
]


def expect(label, got, want):
    if got != want:
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def test_clean_cases():
    for name, year, q, yy in PLAYER_CASES + EXTRA_CASES + PREMIUMTV_CASES:
        r = clean(name, year)
        expect(f"clean({name!r}, {year!r})", r, {"q": q, "year": yy})


def test_title_key_normalized():
    a = title_key("movie", "DE - Der Pate (1972) 4K", "")
    b = title_key("movie", "Der Pate", "1972")
    expect("same film keys", a, b)
    expect("key form", a, "movie|der pate|1972")
    s = title_key("series", "Breaking Bad (US) (2008)", "")
    expect("series key", s, "series|breaking bad|2008")
    expect("tv alias", title_key("tv", "Breaking Bad", "2008"), s)


def test_umlaut_and_match_helpers():
    expect("umlaut german", umlaut_variant("Die Eiskoenigin 2"), "Die Eiskönigin 2")
    expect("umlaut gefaehrten", umlaut_variant("Der Herr Der Ringe: Die Gefaehrten"), "Der Herr Der Ringe: Die Gefährten")
    expect("no umlaut in english True", umlaut_variant("True Romance"), "True Romance")
    expect("search queries both", search_queries("Die Eiskoenigin 2"), ["Die Eiskoenigin 2", "Die Eiskönigin 2"])
    expect("equiv apostrophe", names_equivalent("Schindler's List", "Schindlers List"), True)
    expect("equiv umlaut", names_equivalent("Die Eiskönigin", "Die Eiskoenigin"), True)
    expect("not equiv", names_equivalent("Inception", "Pulp Fiction"), False)
    expect("adult xxx", looks_adult("(XXX-) Tiny4K - Foo"), True)
    expect("adult xxx paren space", looks_adult("  (XXX) Tiny4K"), True)
    expect("not bare XXX-dash", looks_adult("XXX- Some Title"), False)
    expect("not adult", looks_adult("Pulp Fiction"), False)
    expect("not the movie xXx", looks_adult("(DE-) xXx - 2002"), False)
    expect("not xXx sequel", looks_adult("xXx: Return of Xander Cage - 2017"), False)
    expect("not mid-title xxx word after prefix strip", looks_adult("(DE-) School Girls In XXX Training - 2010"), False)
    expect("no false umlaut Influencer", umlaut_variant("Influencer: Trau Niemandem Dem Du Folgst"),
           "Influencer: Trau Niemandem Dem Du Folgst")
    expect("no false umlaut zuerst", umlaut_variant("Wildland: Die Familie Kommt Immer Zuerst"),
           "Wildland: Die Familie Kommt Immer Zuerst")
    expect("no false umlaut Guests word", umlaut_variant("Guests Das Tor Zur Hoelle"),
           "Guests Das Tor Zur Hölle")
    expect("no false umlaut Abenteuer", umlaut_variant("One Piece: Abenteuer In Alabasta Die Wüstenprinzessin"),
           "One Piece: Abenteuer In Alabasta Die Wüstenprinzessin")
    expect("umlaut Zurueck", umlaut_variant("U Turn: Kein Weg Zurueck"), "U Turn: Kein Weg Zurück")
    expect("umlaut leberkaes without article", umlaut_variant("6 Leberkaes Junkie"), "6 Leberkäs Junkie")


def test_extract_tmdb_id():
    expect("tmdb_id int", extract_tmdb_id({"tmdb_id": 238}), 238)
    expect("tmdb string", extract_tmdb_id({"tmdb": "238"}), 238)
    expect("nested info", extract_tmdb_id({"info": {"tmdb_id": "1396"}}), 1396)
    expect("empty", extract_tmdb_id({"tmdb": ""}), None)
    expect("zero", extract_tmdb_id({"tmdb_id": 0}), None)
    expect("url", extract_tmdb_id({"tmdb": "https://www.themoviedb.org/movie/238-the-godfather"}), 238)
    expect("garbage", extract_tmdb_id({"tmdb": "n/a"}), None)


def main():
    tests = [test_clean_cases, test_title_key_normalized, test_umlaut_and_match_helpers, test_extract_tmdb_id]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
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
