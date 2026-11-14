#!/usr/bin/env python3
"""Unit tests for title cleaning / matching. No network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from title import clean, extract_tmdb_id, title_key

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
    for name, year, q, yy in PLAYER_CASES + EXTRA_CASES:
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


def test_extract_tmdb_id():
    expect("tmdb_id int", extract_tmdb_id({"tmdb_id": 238}), 238)
    expect("tmdb string", extract_tmdb_id({"tmdb": "238"}), 238)
    expect("nested info", extract_tmdb_id({"info": {"tmdb_id": "1396"}}), 1396)
    expect("empty", extract_tmdb_id({"tmdb": ""}), None)
    expect("zero", extract_tmdb_id({"tmdb_id": 0}), None)
    expect("url", extract_tmdb_id({"tmdb": "https://www.themoviedb.org/movie/238-the-godfather"}), 238)
    expect("garbage", extract_tmdb_id({"tmdb": "n/a"}), None)


def main():
    tests = [test_clean_cases, test_title_key_normalized, test_extract_tmdb_id]
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
