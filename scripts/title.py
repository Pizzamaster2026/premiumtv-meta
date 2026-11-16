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
# (DE-), (EN -), (XXX-) as seen on PremiumTV lists
_COUNTRY_PAREN = re.compile(
    r"^\s*\(\s*(?:DE|EN|GER|ENG|DEU|US|FR|IT|ES|TR|AT|CH|NL|PL|RU|UA|GR|MULTI)\s*-?\s*\)\s*",
    re.I,
)
# Leading catalogue ranks: "11- Title", "-11- Title", "01 | Title", "12. Title"
# Do not strip a bare number that is part of the name ("6 Ballons", "1917", "2012").
_RANK = re.compile(r"^\s*-?\d{1,4}\s*[-–.:|)/]+\s+")
_PAREN_YEAR = re.compile(r"\((19|20)\d{2}\)|\[(19|20)\d{2}\]")
_BARE_YEAR = re.compile(r"[\s.]((19|20)\d{2})\s*$")
_LANG_PAREN = re.compile(r"\((?:[A-Z]{2,3})\)")
_BRACKETS = re.compile(r"\[[^\]]*\]")
_TAG_PAREN = re.compile(
    r"\((?:[^)]*\b(?:DE|EN|GER|ENG|DEU|MULTI|OmU|OMU|DUB|SUB|4K|UHD|FHD|HD|SD|"
    r"HDR|HEVC|H\.?265|x265|1080p|720p|2160p|3D|WEB-?DL)\b[^)]*)\)",
    re.I,
)
_TAGS = re.compile(
    r"(?:^|[\s.\-])(?:4K|UHD|FHD|HD|SD|HDR10?\+?|DV|HEVC|H\.?26[45]|x26[45]|"
    r"1080p|720p|2160p|3D|MULTI|GERMAN|GER|DEU|DE|ENG|EN|OmU|OMU|DL|AC3|DTS|"
    r"ATMOS|Dolby|NEU|NEW)(?=\s|$|[|\-–.])"
)
_MULTI_SUB = re.compile(
    r"(?:^|[\s.\-])(?:Multi(?:[\s-]?Sub(?:s|bed)?)?|Multi(?:[\s-]?Audio)?|"
    r"Multi(?:[\s-]?Lang(?:uage)?)?|WEB-?DL|WEBRip|Blu-?Ray|REMUX|TRUEHD)"
    r"(?=\s|$|[|\-–.])",
    re.I,
)
# German articles/words: only then we invent an umlaut search variant (ae/oe/ue).
_GERMAN_HINT = re.compile(
    r"\b(?:Der|Die|Das|Dem|Den|Des|Ein|Eine|Einen|Einer|Und|Oder|Von|Vom|Zum|Zur|"
    r"Mit|Auf|Aus|Für|Fuer|Im|Ist|Nicht|Kein|Keine|Kein|Weg|Gegen|Über|Ueber)\b",
    re.I,
)
# Do not treat the "ue" inside "euer"/"Steuer" as an umlaut.
_AE_OE_UE = re.compile(r"(?<![eE])(?:ae|oe|ue)", re.I)
# Words where ae/oe/ue is not a German umlaut (zuerst → zürst, Guests → Güsts).
_UMLAUT_SKIP_WORD = re.compile(
    r"(?i)^(?:zuerst|guests?|influencer|sequences?|consequences?|questionnaire|"
    r"unique|dialogue|catalog(?:ue)?|source|true|blue|due|queue)$"
)
# Strong transliteration even without Der/Die (Leberkaes, Eiskoenigin).
_STRONG_TRANSLIT = re.compile(
    r"(?i)(?:(?<![eE])(?:ae|oe|ue)(?:ck|ch|ll|ss|nig|dchen|hrte?n?|chte|cker|wen|lle|rter|tzt)|kaes)"
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

    for _ in range(6):
        s2 = _COUNTRY_PAREN.sub("", s, count=1)
        s2 = _PREFIX.sub("", s2, count=1)
        s2 = _RANK.sub("", s2, count=1)
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
    s = _MULTI_SUB.sub(" ", s)
    s = _PIPES.sub(" ", s)
    s = _TRAIL_DASH.sub("", s)
    s = _LEAD_DASH.sub("", s)
    s = _SPACES.sub(" ", s).strip()
    return {"q": s, "year": y}


# Leading catalogue marker as used on PremiumTV: (XXX-) or (XXX).
# Not a bare "xXx" / "xXx:" title (Vin Diesel), and not the word mid-title.
_ADULT_PREFIX = re.compile(r"^\s*\(\s*XXX\s*-?\s*\)", re.I)


def looks_adult(name: object) -> bool:
    """True for obvious adult catalogue prefixes such as (XXX-) or XXX- at the start."""
    return bool(_ADULT_PREFIX.search(str(name or "")))


def _umlaut_replace(q: str) -> str:
    table = {"ae": "ä", "oe": "ö", "ue": "ü", "Ae": "Ä", "Oe": "Ö", "Ue": "Ü", "AE": "Ä", "OE": "Ö", "UE": "Ü"}

    def repl(m: re.Match) -> str:
        return table.get(m.group(0), m.group(0))

    def word(m: re.Match) -> str:
        w = m.group(0)
        core = re.sub(r"^[^A-Za-zÄÖÜäöüß]+|[^A-Za-zÄÖÜäöüß]+$", "", w)
        if _UMLAUT_SKIP_WORD.match(core):
            return w
        return _AE_OE_UE.sub(repl, w)

    return re.sub(r"\S+", word, q)


def umlaut_variant(query: str) -> str:
    """German ae/oe/ue → ä/ö/ü, only when the title already looks German."""
    q = (query or "").strip()
    if not q or not _AE_OE_UE.search(q):
        return q
    if not _GERMAN_HINT.search(q) and not _STRONG_TRANSLIT.search(q):
        return q
    alt = _umlaut_replace(q)
    return alt if alt != q else q


def search_queries(query: str) -> list[str]:
    """Primary cleaned query plus a safe umlaut variant, without duplicates."""
    q = (query or "").strip()
    out: list[str] = []
    for cand in (q, umlaut_variant(q)):
        if cand and cand not in out:
            out.append(cand)
    return out or [q]


def names_equivalent(a: object, b: object) -> bool:
    """Loose equality for TMDB search hits (case, punctuation, umlauts)."""
    def norm(x: object) -> str:
        s = str(x or "").lower().strip()
        s = _umlaut_replace(s)
        s = s.replace("ß", "ss")
        s = re.sub(r"['’`´]", "", s)
        s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
        return re.sub(r"\s+", " ", s).strip()
    na, nb = norm(a), norm(b)
    return bool(na and na == nb)


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
