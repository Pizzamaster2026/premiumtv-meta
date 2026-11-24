"""Clean IPTV provider titles so they can be matched against TMDB.

Logic mirrors PremiumTV Player Tmdb.clean (www/js/tmdb.js), including
prefixes such as '| DE |', '[DE]', and trailing years like ' - 2013'.
The cache cleaner also strips box-drawing catalogue markers (┃NL┃, ┃HINDI┃)
and leftover quality tags that the player may still see raw.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

_YEAR = re.compile(r"(19|20)\d{2}")
# ┃NL┃, ┃F1TV┃, ┃HINDI┃, ┃PUNJABI┃, |MULTI| — box-drawing or ASCII pipes.
_BOX_PREFIX = re.compile(
    r"^\s*[┃|]{1,3}\s*[A-Z][A-Z0-9]{1,11}\s*[┃|]{1,3}\s*",
    re.I,
)
_PREFIX = re.compile(
    r"^\s*(?:"
    r"\[[^\]]{1,15}\]"
    r"|\((?:[A-Z]{2,4}|4K|UHD|FHD|HD)\)"
    r"|[A-Z][A-Z0-9]{1,11}(?:[-_ ][A-Z][A-Z0-9]{1,11})?\s*[|:–┃-]\s*"
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
# "44-Todesmelodie": 2–3 digit rank glued to the title, but never a year like 2001-…
_RANK_GLUED = re.compile(r"^\s*(?!(?:19|20)\d{2})\d{2,3}[-–.](?=[A-Za-zÄÖÜäöüß])")
_PAREN_YEAR = re.compile(r"\((19|20)\d{2}\)|\[(19|20)\d{2}\]")
_BARE_YEAR = re.compile(r"[\s.]((19|20)\d{2})\s*$")
_LANG_PAREN = re.compile(r"\((?:[A-Z]{2,3})\)")
_BRACKETS = re.compile(r"\[[^\]]*\]")
_TAG_PAREN = re.compile(
    r"\((?:[^)]*\b(?:DE|EN|GER|ENG|DEU|MULTI|OmU|OMU|DUB|SUB|4K|UHD|FHD|HD|SD|"
    r"HDR|HEVC|H\.?265|x265|1080p|720p|480p|2160p|3D|WEB-?DL|CAM|HDCAM|HDRip|"
    r"VFF|VOSTFR|VOST)\b[^)]*)\)",
    re.I,
)
_TAGS = re.compile(
    r"(?:^|[\s.\-])(?:4K|UHD|FHD|HD|SD|HDR10?\+?|DV|HEVC|H\.?26[45]|x26[45]|"
    r"1080p|720p|480p|2160p|3D|MULTI|GERMAN|GER|DEU|DE|ENG|EN|OmU|OMU|DL|AC3|DTS|"
    r"ATMOS|Dolby|NEU|NEW|VFF|VOSTFR|VOST|HDCAM|HDRip|WEB-?DL|"
    r"UK[\s-]?AUDIO)(?=\s|$|[|\-–.])",
    re.I,
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
# Strong transliteration even without Der/Die (Leberkaes, Eiskoenigin, Ausloeschung).
_STRONG_TRANSLIT = re.compile(
    r"(?i)(?:(?<![eE])(?:ae|oe|ue)(?:ck|ch|sch|ll|ss|nig|dchen|hrte?n?|chte|cker|wen|lle|rter|tzt)|kaes)"
)
_PIPES = re.compile(r"[|_┃]+")
_TRAIL_DASH = re.compile(r"\s*[-–:]\s*$")
_LEAD_DASH = re.compile(r"^\s*[-–:]\s*")
_SPACES = re.compile(r"\s{2,}")
_ZERO_WIDTH = re.compile(r"[\u200b\u00a0]")
# Leading articles/prepositions ignored when comparing a query to a TMDB title.
# Remainder must be long enough so "Die Hard" does not equal "Hard".
_ARTICLES = re.compile(
    r"^(?:the|a|an|der|die|das|dem|den|des|ein|eine|einen|einer|"
    r"in|im|le|la|les|el|los|las|il|lo|un|une|het)\s+",
    re.I,
)
_TRAIL_ONE = re.compile(r"\s+1$")


def extract_year(value: object) -> str:
    """First 19xx/20xx year found in a provider year/date field."""
    m = _YEAR.search(str(value or ""))
    return m.group(0) if m else ""


def _strip_leading(s: str) -> str:
    for _ in range(8):
        s2 = _COUNTRY_PAREN.sub("", s, count=1)
        s2 = _BOX_PREFIX.sub("", s2, count=1)
        s2 = _PREFIX.sub("", s2, count=1)
        s2 = _RANK.sub("", s2, count=1)
        s2 = _RANK_GLUED.sub("", s2, count=1)
        if s2 == s:
            break
        s = s2
    return s


def _extract_title_year(s: str, y: str) -> tuple[str, str]:
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
    return s, y


def clean(name: object, year: object = "") -> dict:
    """Return {'q': cleaned title, 'year': 'YYYY' or ''}."""
    s = _ZERO_WIDTH.sub(" ", str(name or "")).strip()
    y = extract_year(year)

    s = _strip_leading(s)
    s, y = _extract_title_year(s, y)

    s = _LANG_PAREN.sub(" ", s)
    s = _BRACKETS.sub(" ", s)
    s = _TAG_PAREN.sub(" ", s)
    s = _MULTI_SUB.sub(" ", s)
    s = _TAGS.sub(" ", s)
    s = _PIPES.sub(" ", s)
    s = _TRAIL_DASH.sub("", s)
    s = _LEAD_DASH.sub("", s)
    s = _SPACES.sub(" ", s).strip()
    # Quality tags after a year ("title 1999 1080p sd") hide the bare year until now.
    s, y = _extract_title_year(s, y)
    s = _TRAIL_DASH.sub("", s)
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


def _fold_diacritics(s: str) -> str:
    parts = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in parts if not unicodedata.combining(ch))


def _collapse_vowels(s: str) -> str:
    # Only aa↔ā style transliteration (Baahubali / Bāhubali), not oo/ee typo folding.
    return s.replace("aa", "a")


def _norm_name(x: object) -> str:
    s = str(x or "").lower().strip()
    s = _umlaut_replace(s)
    s = s.replace("ß", "ss")
    s = _fold_diacritics(s)
    s = re.sub(r"['’`´]", "", s)
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def _strip_article(s: str) -> str:
    m = _ARTICLES.match(s)
    if not m:
        return s
    rest = s[m.end():].strip()
    words = rest.split()
    if len(rest) >= 8 or (len(words) >= 2 and len(rest) >= 6):
        return rest
    return s


def _name_variants(s: str) -> set[str]:
    if not s:
        return set()
    bases = {s, _collapse_vowels(s)}
    stripped = _strip_article(s)
    if stripped != s:
        bases.add(stripped)
        bases.add(_collapse_vowels(stripped))
    out = set(bases)
    for base in bases:
        no1 = _TRAIL_ONE.sub("", base).strip()
        if no1 and no1 != base:
            out.add(no1)
            out.add(_collapse_vowels(no1))
    return {x for x in out if x}


def names_equivalent(a: object, b: object) -> bool:
    """Loose equality for TMDB search hits (case, punctuation, umlauts, articles, sequel 1)."""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    return bool(_name_variants(na) & _name_variants(nb))


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
