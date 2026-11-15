#!/usr/bin/env python3
"""Sport schedule (data/sport.json) for the PremiumTV player's "Sport live" row.

Built from the One provider's XMLTV EPG (one request: xmltv.php), streamed and
parsed incrementally, strictly time- and size-bounded. Only the programme
TITLE, sub-title and XMLTV <category> are used for the decision – never the
description (a show that only mentions "Fußball" in its text is not sport).

Format (UTF-8 JSON, times = UTC epoch seconds):
{
  "v": 1, "generatedAt": 1794550000, "source": "one-xmltv", "days": 3,
  "items": [
    {"epg": "skysportbundesliga1.de", "ch": ["Sky Sport Bundesliga 1 HD"],
     "title": "Bayern München – Borussia Dortmund", "start": 1794580200,
     "end": 1794587400, "kind": "bl", "league": "Bundesliga"}
  ]
}
kind: bl, bl2, cl, dfb, pl, fb (other football), f1, moto, nfl, nba, ih
(ice hockey / NHL), tennis, box (boxing / UFC / MMA), hb, darts, other.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

SPORT_FORMAT_VERSION = 1
DEFAULT_DAYS = 3
DEFAULT_BUDGET_SEC = 300          # whole step (download + parse)
DEFAULT_MAX_MB = 400              # decompressed XMLTV safety cap
MAX_ITEMS = 6000
PAST_SEC = 3 * 3600               # keep shows that started up to 3 h ago

# ---------------------------------------------------------------------------
# Classification (strict, title / sub-title / category only)
# ---------------------------------------------------------------------------
_W = r"(?<![a-z0-9äöüß])"
_E = r"(?![a-z0-9äöüß])"
KINDS: list[tuple[str, str, re.Pattern]] = [
    ("bl2", "2. Bundesliga", re.compile(r"(2\.|zweite)\s*(fu(ß|ss)ball-?)?bundesliga|" + _W + r"2\.\s*liga" + _E, re.I)),
    ("bl", "Bundesliga", re.compile(_W + r"(fu(ß|ss)ball-)?bundesliga" + _E + r"|" + _W + r"dfl" + _E + r"|frauen-bundesliga", re.I)),
    ("cl", "Champions League", re.compile(r"champions league|uefa cl" + _E + r"|königsklasse", re.I)),
    ("dfb", "DFB-Pokal", re.compile(r"dfb[- ]?pokal", re.I)),
    ("pl", "Premier League", re.compile(r"premier league|" + _W + r"epl" + _E, re.I)),
    ("fb", "Fußball", re.compile(r"europa league|conference league|la ?liga|serie a" + _E + r"|ligue 1|eredivisie|süper lig|super lig|"
                                 r"3\. liga|regionalliga|länderspiel|wm-qualifikation|em-qualifikation|nations league|"
                                 r"fu(ß|ss)ball|" + _W + r"(fc|sv|vfb|vfl|tsg|bvb|rb)" + _E + r".{0,40}[-–—]|copa del rey|fa cup|mls" + _E, re.I)),
    ("f1", "Formel 1", re.compile(r"formel ?1|formula ?1|" + _W + r"f1" + _E + r"|grand prix", re.I)),
    ("moto", "Motorsport", re.compile(r"motogp|moto ?[23]" + _E + r"|superbike|dtm" + _E + r"|formel e|formula e|nascar|indycar|rallye|wrc" + _E, re.I)),
    ("nfl", "NFL", re.compile(_W + r"nfl" + _E + r"|american football|super bowl|redzone|" + _W + r"gfl" + _E, re.I)),
    ("nba", "NBA", re.compile(_W + r"nba" + _E + r"|basketball|" + _W + r"bbl" + _E + r"|euroleague", re.I)),
    ("ih", "Eishockey", re.compile(_W + r"nhl" + _E + r"|eishockey|ice hockey|" + _W + r"del" + _E + r"(?! (?:mar|sol|toro))", re.I)),
    ("tennis", "Tennis", re.compile(r"tennis|" + _W + r"(atp|wta)" + _E + r"|wimbledon|us open|french open|australian open|davis cup|billie jean king", re.I)),
    ("box", "Boxen / UFC", re.compile(r"boxen|boxing|boxkampf|" + _W + r"(ufc|mma|wwe)" + _E + r"|fight night|kampfsport", re.I)),
    ("hb", "Handball", re.compile(r"handball", re.I)),
    ("darts", "Darts", re.compile(r"darts|" + _W + r"pdc" + _E, re.I)),
    ("other", "Sport", re.compile(r"biathlon|ski alpin|skispringen|langlauf|rodeln|bob" + _E + r"|eiskunstlauf|golf" + _E + r"|volleyball|radsport|tour de france|"
                                  r"leichtathletik|schwimmen|turnen|rugby|cricket|snooker|tischtennis|badminton|olympi|paralympi|sportschau|sportstudio|"
                                  r"ran football|" + _W + r"sport" + _E, re.I)),
]
# never sport, whatever the channel or category says
NON_SPORT = re.compile(r"teleshopping|dauerwerbe|programmpause|sendepause|infomercial|shop|poker|casino|lotto|quiz|"
                       r"tagesschau|nachrichten|" + _W + r"news" + _E + r"|wetter|magazin(?! sport)|talk(?!.*sport)|"
                       r"wer stiehlt mir die show|show$|spielfilm|film:|serie:|krimi|dokumentation über|kochen|"
                       r"gottesdienst|kinder|cartoon|anime|reportage(?!.*(sport|fu(ß|ss)ball))", re.I)
# a match: "Team A – Team B", "A vs. B", "A : B"
MATCH_RE = re.compile(r"\S.{1,40}\s(?:[-–—]|vs\.?|v\.|gegen|:)\s.{2,40}", re.I)
SPORT_CH_RE = re.compile(r"sky sport|dazn|eurosport|sport ?1|sportdigital|magenta ?sport|bein|espn|setanta|arena sport|"
                         r"nova sport|sport ?tv|ran ?fighting|bundesliga|laliga|premier sports|tnt sports|ziggo sport|canal\+ sport|"
                         r"(?<![a-z])sport(?![a-z])|fight|nfl|nba|nhl|f1 tv|motorvision|sportschau", re.I)
CAT_SPORT_RE = re.compile(r"sport|fu(ß|ss)ball|football|soccer|tennis|handball|darts|box|motorsport|eishockey|basketball", re.I)


def classify(title: str, sub: str = "", cats: Iterable[str] = (), channel: str = "") -> Optional[tuple[str, str]]:
    """(kind, league) for a sport programme, None for everything else. Description is never used."""
    t = " ".join(x for x in (title or "", sub or "") if x).strip()
    if not t or NON_SPORT.search(title or ""):
        return None
    for kind, label, rx in KINDS:
        if kind == "other":
            continue
        if rx.search(t):
            return kind, label
    cat_sport = any(CAT_SPORT_RE.search(c or "") for c in cats)
    sport_ch = bool(SPORT_CH_RE.search(channel or ""))
    if KINDS[-1][2].search(t):
        return "other", "Sport"
    # weaker evidence: a sport category / sport channel AND a match-like title
    if (cat_sport or sport_ch) and MATCH_RE.search(t):
        return "other", "Sport"
    if cat_sport and sport_ch:
        return "other", "Sport"
    return None


# ---------------------------------------------------------------------------
# XMLTV
# ---------------------------------------------------------------------------
_TS_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?\s*([+-]\d{4}|Z|UTC|GMT)?")


def xmltv_ts(s: str) -> Optional[int]:
    m = _TS_RE.match((s or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, se, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se or 0), tzinfo=timezone.utc)
    off = 0
    if tz and tz[0] in "+-":
        off = (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60) * (1 if tz[0] == "+" else -1)
    return int(dt.timestamp()) - off


def parse_xmltv(chunks: Iterable[bytes], now: int, days: float = DEFAULT_DAYS, deadline: Optional[float] = None,
                max_bytes: int = DEFAULT_MAX_MB * 1024 * 1024) -> tuple[list[dict], dict]:
    """Incremental parse. Returns (items, stats). Raises TimeoutError / ValueError on budget / size limits."""
    parser = ET.XMLPullParser(events=("end",))
    names: dict[str, list[str]] = {}
    items: list[dict] = []
    seen = set()
    lo, hi = now - PAST_SEC, now + int(days * 86400)
    st = {"bytes": 0, "programmes": 0, "channels": 0, "sport": 0}
    for chunk in chunks:
        st["bytes"] += len(chunk)
        if st["bytes"] > max_bytes:
            raise ValueError(f"XMLTV größer als {max_bytes // (1024 * 1024)} MB")
        if deadline and time.monotonic() > deadline:
            raise TimeoutError("Zeitbudget für den Sportplan erreicht")
        parser.feed(chunk)
        for _ev, el in parser.read_events():
            tag = el.tag
            if tag == "channel":
                cid = (el.get("id") or "").strip()
                dn = [(x.text or "").strip() for x in el.findall("display-name") if (x.text or "").strip()]
                if cid:
                    names[cid] = dn[:3]
                st["channels"] += 1
                el.clear()
            elif tag == "programme":
                st["programmes"] += 1
                s, e = xmltv_ts(el.get("start") or ""), xmltv_ts(el.get("stop") or "")
                if s and e and e > lo and s < hi and e > s:
                    cid = (el.get("channel") or "").strip()
                    title = (el.findtext("title") or "").strip()
                    sub = (el.findtext("sub-title") or "").strip()
                    cats = [(c.text or "").strip() for c in el.findall("category")]
                    chn = " ".join(names.get(cid, [])) + " " + cid
                    k = classify(title, sub, cats, chn)
                    if k and (cid, s) not in seen:
                        seen.add((cid, s))
                        items.append({"epg": cid, "ch": names.get(cid, [])[:3], "title": title[:160], "start": s, "end": e,
                                      "kind": k[0], "league": k[1]})
                el.clear()
            elif tag not in ("title", "sub-title", "desc", "category", "display-name", "icon", "url", "episode-num", "rating", "value", "credits"):
                el.clear()
    try:
        parser.close()
    except ET.ParseError:
        pass   # cut-off tail: keep what was parsed
    for it in items:   # channel names declared after their programmes
        if not it["ch"] and it["epg"] in names:
            it["ch"] = names[it["epg"]][:3]
    items.sort(key=lambda x: (x["start"], x["epg"]))
    st["sport"] = len(items)
    return items[:MAX_ITEMS], st


def build_doc(items: list[dict], now: int, days: float = DEFAULT_DAYS, source: str = "one-xmltv") -> dict:
    return {"v": SPORT_FORMAT_VERSION, "generatedAt": int(now), "source": source, "days": days, "items": items}


# ---------------------------------------------------------------------------
# Download (streamed, gzip-aware, time-bounded)
# ---------------------------------------------------------------------------

def xmltv_url(base: str, user: str, password: str) -> str:
    return f"{base}/xmltv.php?" + urllib.parse.urlencode({"username": user, "password": password})


def stream_url(url: str, deadline: float, user_agent: str, chunk: int = 256 * 1024) -> Iterable[bytes]:
    left = max(5.0, deadline - time.monotonic())
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip", "Accept": "application/xml,text/xml,*/*"})
    with urllib.request.urlopen(req, timeout=min(120.0, left)) as resp:
        gz = "gzip" in (resp.headers.get("Content-Encoding") or "").lower()
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if gz else None
        first = True
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("Zeitbudget für den Sportplan erreicht")
            b = resp.read(chunk)
            if not b:
                break
            if first and not gz and b[:2] == b"\x1f\x8b":   # gzip body without header
                dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
            first = False
            yield dec.decompress(b) if dec else b
        if dec:
            tail = dec.flush()
            if tail:
                yield tail


def write_sport(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def run_step(data_dir: Path, provider: Optional[dict], base_fn: Callable[[str], str], log: Callable[[str], None], mask_url: Callable[[str], str],
             user_agent: str, budget_sec: float = DEFAULT_BUDGET_SEC, days: float = DEFAULT_DAYS, max_mb: float = DEFAULT_MAX_MB,
             now: Optional[int] = None, opener: Optional[Callable] = None) -> str:
    """Never raises. Returns 'ok', 'skipped' or 'failed'. On failure the old sport.json stays untouched."""
    t0 = time.monotonic()
    try:
        if not provider:
            log("Sportplan: kein One-Zugang hinterlegt – übersprungen.")
            return "skipped"
        base = base_fn(provider["url"])
        url = xmltv_url(base, provider["user"], provider["pass"])
        log(f"Sportplan: EPG von {mask_url(url)} (höchstens {budget_sec:g}s, {max_mb:g} MB) …")
        deadline = t0 + budget_sec
        now = int(now or time.time())
        chunks = (opener or stream_url)(url, deadline, user_agent)
        items, st = parse_xmltv(chunks, now, days=days, deadline=deadline, max_bytes=int(max_mb * 1024 * 1024))
        if st["programmes"] == 0:
            log("Sportplan: EPG leer oder kein XMLTV – alte sport.json bleibt.")
            return "failed"
        write_sport(Path(data_dir) / "sport.json", build_doc(items, now, days))
        log(f"Sportplan: {len(items)} Sportsendungen aus {st['programmes']} Sendungen / {st['channels']} Sendern "
            f"({st['bytes'] / 1048576:.1f} MB, {time.monotonic() - t0:.1f}s) → sport.json.")
        return "ok"
    except Exception as e:   # noqa: BLE001 – this step must never fail the sync
        msg = str(e)
        if provider:
            for secret in (provider.get("user"), provider.get("pass")):
                if secret:
                    for v in {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}:
                        msg = msg.replace(v, "********")
        log(f"Sportplan: übersprungen ({type(e).__name__}: {msg[:200]}) – alte sport.json bleibt.")
        return "failed"
