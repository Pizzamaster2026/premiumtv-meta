"""Clean IPTV provider titles so they can be matched against TMDB.

Logic mirrors PremiumTV Player Tmdb.clean (www/js/tmdb.js), including
prefixes such as '| DE |', '[DE]', and trailing years like ' - 2013'.
"""
from __future__ import annotations

import re
from typing import Optional

_YEAR = re.compile(r"(19|20)\d{2}")
_PREFIX = re.compile(
    r"^\s*(?:"
    r"\[[^\]]{1,15}\]"
    r"|\((?:[A-Z]{2,4}|4K|UHD|FHD|HD)\)"
    r"|[A-Z]{2,4}(?:[-_ ][A-Z]{2,4})?\s*[|:–-]\s*"
    r"|[A-Z]{2}\s*\|\s*"
    r")\s*"
)
_PAREN_YEAR = re.compile(r"\((19|20)\d{2}\)|\[(19|20)\d{2}\]")
_BARE_YEAR = re.compile(r"[\s.]((19|20)\d{2})\s*$")
_LANG_PAREN = re.compile(r"\((?:[A-Z]{2,3})\)")
_BRACKETS = re.compile(r"\[[^\]]*\]")
_TAG_PAREN = re.compile(
    r"\((?:[^)]*\b(?:DE|EN|GER|ENG|DEU|MULTI|OmU|OMU|DUB|SUB|4K|UHD|FHD|HD|SD|"
    r"HDR|HEVC|H\.?265|x265|1080p|720p|2160p)\b[^)]*)\)",
    re.I,
)
_TAGS = re.compile(
    r"(?:^|\s)(?:4K|UHD|FHD|HD|SD|HDR10?\+?|DV|HEVC|H\.?26[45]|x26[45]|"
    r"1080p|720p|2160p|MULTI|GERMAN|GER|DEU|DE|ENG|EN|OmU|OMU|DL|AC3|DTS|"
    r"ATMOS|Dolby|NEU|NEW)(?=\s|$|[|\-–])"
)
_PIPES = re.compile(r"[|_]+")
_TRAIL_DASH = re.compile(r"\s*[-–:]\s*$")
_LEAD_DASH = re.compile(r"^\s*[-–:]\s*")
_SPACES = re.compile(r"\s{2,}")
_ZERO_WIDTH = re.compile(r"[\u200b\u00a0]")


def extract_year(value: object) -> str:
    """First 19xx/20xx year found in a provider year/date field."""
    m = _YEAR.search(str(value or ""))
    return m.group(0) if m else ""


def clean(name: object, year: object = "") -> dict:
    """Return {'q': cleaned title, 'year': 'YYYY' or ''}."""
    s = _ZERO_WIDTH.sub(" ", str(name or "")).strip()
    y = extract_year(year)

    for _ in range(3):
        s2 = _PREFIX.sub("", s, count=1)
        if s2 == s:
            break
        s = s2

    ym = _PAREN_YEAR.search(s)
    if ym and s.replace(ym.group(0), "").strip():
        if not y:
            y = extract_year(ym.group(0))
        s = s.replace(ym.group(0), " ")
    else:
        yb = _BARE_YEAR.search(s)
        if yb and s[: yb.start()].strip() and (not y or y == yb.group(1)):
            if not y:
                y = yb.group(1)
            s = s[: yb.start()]

    s = _LANG_PAREN.sub(" ", s)
    s = _BRACKETS.sub(" ", s)
    s = _TAG_PAREN.sub(" ", s)
    s = _TAGS.sub(" ", s)
    s = _PIPES.sub(" ", s)
    s = _TRAIL_DASH.sub("", s)
    s = _LEAD_DASH.sub("", s)
    s = _SPACES.sub(" ", s).strip()
    return {"q": s, "year": y}


def title_key(kind: str, name: object, year: object = "") -> str:
    """Normalized lookup key: 'movie|der pate|1972' or 'series|breaking bad|2008'."""
    typ = "series" if kind in ("series", "tv", "show") else "movie"
    c = clean(name, year)
    q = re.sub(r"\s+", " ", (c["q"] or "").lower()).strip()
    return f"{typ}|{q}|{c['year']}"


def extract_tmdb_id(item: dict) -> Optional[int]:
    """Read a numeric TMDB id from common Xtream list fields, if the panel sends one."""
    if not isinstance(item, dict):
        return None
    for key in ("tmdb_id", "tmdbId", "tmdb", "tmdbID"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("tmdb_id") or raw.get("tmdb")
        s = str(raw).strip()
        if s.startswith("http") or "/" in s:
            m = re.search(r"(?:themoviedb\.org/(?:movie|tv)/|tmdb(?:id)?=)(\d+)", s, re.I)
            if m:
                n = int(m.group(1))
                return n if n > 0 else None
            continue
        if re.fullmatch(r"\d+", s):
            n = int(s)
            return n if n > 0 else None
    info = item.get("info")
    if isinstance(info, dict):
        return extract_tmdb_id(info)
    return None
