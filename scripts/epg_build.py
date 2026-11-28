#!/usr/bin/env python3
"""Nightly EPG preprocessing (Programmquelle "Automatisch" of the PremiumTV player).

Input : epgshare01 epg_ripper_DE1.xml.gz (German XMLTV, ~22 MB unpacked)
        optional: the One provider's xmltv.php (only to CROSS-CHECK the channel mapping, never as a data source)
Output: epg-de.json (compact, ~2 MB) for the private EPG worker. NOTHING is written into the public repository.

Format v1 (UTF-8 JSON, times = UTC epoch seconds):
  { "v": 1, "generatedAt": 1794550000, "source": "epgshare01-DE1",
    "ch":    { "RTL.de": { "n": "RTL", "p": [[start, end, "Titel", "Beschreibung (gekürzt, optional)"], ...] } },
    "names": { "rtl": "RTL.de" },          # normalized channel name -> ext id (unambiguous only)
    "byEpg": { "rtl.de": "RTL.de" },       # provider epg_channel_id (lower case, verified) -> ext id
    "block": ["..."] }                     # names that must never be mapped

Mapping rules (correctness first, a wrong programme is worse than the provider's):
  * a name maps only if its normalized key is UNAMBIGUOUS in the file or listed in epg/overrides.json
  * names with a foreign country tag ("AT|", "UK:") are never mapped (player side), "+1" / "+24" / HD+ variants never equal the base channel
  * byEpg only when the id AND the name agree, or when titles agree with the One schedule (>= 60 % of >= 6 comparable shows)
The player's name_key() MUST stay identical to name_key() here (tests/fixtures/epg_keys.json is used by both test suites).
Never prints credentials.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

EXT_URL = "https://epgshare01.online/epgshare01/epg_ripper_DE1.xml.gz"
FORMAT_VERSION = 1
PAST_SEC = 6 * 3600
AHEAD_SEC = 96 * 3600
DESC_MAX = 220
MAX_BYTES = 2_600_000          # target upper bound of the JSON (player accepts up to 3 MB)
MIN_SHOWS_TO_VERIFY = 6
MIN_AGREE = 0.6

# ---------------------------------------------------------------------------
# Names (keep in sync with www/js/extepg.js ExtEpg.nameKey)
# ---------------------------------------------------------------------------
_QUALITY = {"fhd", "uhd", "hd", "sd", "4k", "hevc", "raw", "h265", "h264", "50fps", "60fps", "backup", "vip", "de", "hq", "lq"}
_PREFIX_RES = (
    re.compile(r"^\s*[|┃]\s*[a-z0-9]{2,4}\s*[|┃]\s*"),
    re.compile(r"^\s*[a-z0-9]{2,4}\s*[|┃:]\s*"),
)


_VIP_RE = re.compile(r"^\s*vip\b\s*")


def _fold(s: str) -> str:
    s = (s or "").lower().replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def country_tag(name: str) -> str:
    """Leading provider country tag in lower case ('de', 'at', 'uk', …) or '' when the name has none."""
    s = _VIP_RE.sub("", _fold(name), count=1)
    for rx in _PREFIX_RES:
        m = rx.match(s)
        if m:
            return re.sub(r"[^a-z0-9]", "", m.group(0))
    return ""


def name_key(name: str) -> str:
    s = _VIP_RE.sub("", _fold(name), count=1)
    for _ in range(3):
        for rx in _PREFIX_RES:
            s = rx.sub("", s, count=1)
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = re.sub(r"\bhd\s*\+", " ", s)
    s = re.sub(r"\+\s*(\d+)", r" plus\1 ", s)
    s = s.replace("+", " plus ")
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _QUALITY]
    return "".join(words)


def id_key(epg_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(epg_id))


def title_key(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(t))


# ---------------------------------------------------------------------------
# XMLTV
# ---------------------------------------------------------------------------
_TS_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?\s*([+-]\d{4}|Z|UTC|GMT)?")


def xmltv_ts(s: str) -> Optional[int]:
    from datetime import datetime, timezone
    m = _TS_RE.match((s or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, se, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se or 0), tzinfo=timezone.utc)
    off = 0
    if tz and tz[0] in "+-":
        off = (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60) * (1 if tz[0] == "+" else -1)
    return int(dt.timestamp()) - off


def parse_xmltv(chunks: Iterable[bytes], now: int, want_desc: bool = True, past: int = PAST_SEC, ahead: int = AHEAD_SEC,
                only_ids: Optional[set] = None) -> tuple[dict, dict, dict]:
    """-> (channels {id: [names]}, programmes {id: [[s, e, title, desc]]}, stats). Sorted, de-duplicated, clipped to the window."""
    parser = ET.XMLPullParser(events=("end",))
    names: dict = {}
    progs: dict = {}
    lo, hi = now - past, now + ahead
    st = {"bytes": 0, "channels": 0, "programmes": 0, "kept": 0}
    for chunk in chunks:
        st["bytes"] += len(chunk)
        parser.feed(chunk)
        for _ev, el in parser.read_events():
            if el.tag == "channel":
                cid = (el.get("id") or "").strip()
                if cid:
                    names[cid] = [(x.text or "").strip() for x in el.findall("display-name") if (x.text or "").strip()][:3]
                st["channels"] += 1
                el.clear()
            elif el.tag == "programme":
                st["programmes"] += 1
                cid = (el.get("channel") or "").strip()
                s, e = xmltv_ts(el.get("start") or ""), xmltv_ts(el.get("stop") or "")
                if cid and s and e and e > s and e > lo and s < hi and (only_ids is None or cid in only_ids):
                    title = " ".join((el.findtext("title") or "").split())
                    if title:
                        desc = " ".join((el.findtext("desc") or "").split())[:DESC_MAX] if want_desc else ""
                        progs.setdefault(cid, []).append([s, e, title[:140], desc])
                        st["kept"] += 1
                el.clear()
    try:
        parser.close()
    except ET.ParseError:
        pass
    for cid, l in progs.items():
        l.sort(key=lambda x: x[0])
        out, last = [], None
        for p in l:
            if last and p[0] == last[0]:
                continue
            if last and p[0] < last[1]:      # overlap: cut the previous one, never show two shows at once
                last[1] = p[0]
                if last[1] <= last[0]:
                    out.pop()
            out.append(p)
            last = p
        progs[cid] = out
    return names, progs, st


def gunzip_chunks(path: Path, chunk: int = 256 * 1024) -> Iterable[bytes]:
    with gzip.open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                return
            yield b


def download(url: str, dest: Path, timeout: float = 120.0) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; premiumtv-epg-sync)"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        n = 0
        while True:
            b = r.read(256 * 1024)
            if not b:
                break
            f.write(b)
            n += len(b)
    return n


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def build_name_index(ext_names: dict, overrides: dict) -> tuple[dict, dict]:
    """-> (names {key: ext_id}, ambiguous {key: [ids]}). Overrides win, blocked names are removed."""
    cand: dict = {}
    for cid, dn in ext_names.items():
        keys = {name_key(re.sub(r"\.de$", "", cid, flags=re.I))}
        keys.update(name_key(x) for x in dn)
        for k in keys:
            if k:
                cand.setdefault(k, set()).add(cid)
    names = {k: next(iter(v)) for k, v in cand.items() if len(v) == 1}
    amb = {k: sorted(v) for k, v in cand.items() if len(v) > 1}
    for k, cid in (overrides.get("map") or {}).items():
        kk = name_key(k)
        if cid in ext_names:
            names[kk] = cid
            amb.pop(kk, None)
    for k in overrides.get("block") or []:
        names.pop(name_key(k), None)
    return names, amb


def build_id_index(ext_names: dict) -> dict:
    idx: dict = {}
    for cid in ext_names:
        idx.setdefault(id_key(cid), set()).add(cid)
    return {k: next(iter(v)) for k, v in idx.items() if len(v) == 1}


def titles_agree(ext: list, one: list, now: int, ahead: int = 48 * 3600) -> tuple[int, int]:
    """(comparable, matching) over One shows in [now, now+ahead]: comparable = an ext show starts within 10 min."""
    starts = sorted((p[0], title_key(p[2])) for p in ext)
    comp = match = 0
    for p in one:
        if p[1] <= now or p[0] > now + ahead:
            continue
        best = min(starts, key=lambda x: abs(x[0] - p[0]), default=None)
        if not best or abs(best[0] - p[0]) > 600:
            continue
        comp += 1
        a, b = title_key(p[2]), best[1]
        if a and b and (a == b or (len(a) >= 4 and len(b) >= 4 and (a in b or b in a))):
            match += 1
    return comp, match


def cross_check(ext_names: dict, ext_progs: dict, one_names: dict, one_progs: dict, names: dict, ids: dict, now: int) -> tuple[dict, dict]:
    """One channel ids (lower case) -> ext id, only when verified. Returns (byEpg, report)."""
    by: dict = {}
    rep = {"strong": 0, "titles": 0, "disagree": [], "conflict": [], "unmapped": 0, "unverifiable": 0}
    for oid, dn in one_names.items():
        if not oid.lower().endswith(".de"):
            continue
        via_id = ids.get(id_key(oid))
        via_name = None
        for x in dn:
            k = name_key(x)
            if country_tag(x) not in ("", "de"):
                continue
            if k in names:
                via_name = names[k]
                break
        cand = via_id or via_name
        if not cand:
            rep["unmapped"] += 1
            continue
        if via_id and via_name and via_id != via_name:
            rep["conflict"].append(oid)       # id and name point to different channels: distrust both
            continue
        comp, match = titles_agree(ext_progs.get(cand, []), one_progs.get(oid, []), now)
        ok_titles = comp >= MIN_SHOWS_TO_VERIFY and match / comp >= MIN_AGREE
        if via_id and via_name:
            by[oid.lower()] = cand
            rep["strong"] += 1
            if comp >= MIN_SHOWS_TO_VERIFY and not ok_titles:
                rep["disagree"].append(f"{oid} ({match}/{comp})")   # provider data probably wrong here
        elif ok_titles:
            by[oid.lower()] = cand
            rep["titles"] += 1
        else:
            rep["unverifiable"] += 1
    return by, rep


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------

def build_doc(ext_names: dict, ext_progs: dict, names: dict, by_epg: dict, overrides: dict, now: int, max_bytes: int = MAX_BYTES) -> dict:
    prio = [c for c in (overrides.get("priority") or []) if c in ext_progs]
    rest = [c for c in sorted(ext_progs) if c not in set(prio)]

    def shape(cid: str, with_desc: bool) -> dict:
        dn = ext_names.get(cid) or [cid]
        return {"n": dn[0][:40], "p": [([p[0], p[1], p[2], p[3]] if (with_desc and p[3]) else [p[0], p[1], p[2]]) for p in ext_progs[cid]]}

    ch = {}
    for cid in prio:
        ch[cid] = shape(cid, True)
    for cid in rest:
        ch[cid] = shape(cid, False)
    doc = {"v": FORMAT_VERSION, "generatedAt": int(now), "source": "epgshare01-DE1", "ch": ch, "names": {}, "byEpg": {}, "block": []}

    def size() -> int:
        return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    # still too big: drop the least important channels (rest first, then desc of priority tail)
    drop = list(reversed(rest))
    while size() > max_bytes and drop:
        ch.pop(drop.pop(), None)
    for cid in reversed(prio):
        if size() <= max_bytes:
            break
        ch[cid] = shape(cid, False)
    doc["names"] = {k: v for k, v in names.items() if v in ch}
    doc["byEpg"] = {k: v for k, v in by_epg.items() if v in ch}
    doc["block"] = sorted({name_key(x) for x in (overrides.get("block") or []) if name_key(x)})
    return doc


def load_overrides(path: Optional[Path]) -> dict:
    if path and path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    return {}


def run(ext_gz: Path, out: Path, overrides_path: Optional[Path], one_chunks: Optional[Iterable[bytes]], now: Optional[int] = None,
        log=print) -> dict:
    now = int(now or time.time())
    ov = load_overrides(overrides_path)
    ext_names, ext_progs, st = parse_xmltv(gunzip_chunks(ext_gz), now)
    if st["kept"] < 1000 or len(ext_progs) < 50:
        raise ValueError(f"epgshare01-Datei unplausibel ({len(ext_progs)} Sender, {st['kept']} Sendungen) – nichts veröffentlicht")
    log(f"epgshare01: {st['channels']} Sender, {st['programmes']} Sendungen, im Zeitfenster {st['kept']} bei {len(ext_progs)} Sendern.")
    for k in (ov.get("map") or {}).values():
        if k not in ext_names:
            log(f"Hinweis: Override-Sender {k} fehlt in der Datei.")
    names, amb = build_name_index(ext_names, ov)
    ids = build_id_index(ext_names)
    log(f"Namen-Index: {len(names)} eindeutig, {len(amb)} mehrdeutig (nicht zugeordnet).")
    by_epg: dict = {}
    if one_chunks is not None:
        try:
            on, op, ost = parse_xmltv(one_chunks, now, want_desc=False, past=0, ahead=48 * 3600)
            by_epg, rep = cross_check(ext_names, ext_progs, on, op, names, ids, now)
            log(f"One-Gegenprüfung: {rep['strong']} sicher (ID+Name), {rep['titles']} per Titel, {rep['unverifiable']} nicht prüfbar, "
                f"{rep['unmapped']} ohne Treffer, {len(rep['conflict'])} Widerspruch ID/Name, {len(rep['disagree'])} Anbieter weicht ab.")
            if rep["disagree"]:
                log("Anbieter weicht ab bei: " + ", ".join(rep["disagree"][:30]))
        except Exception as e:  # noqa: BLE001 – the cross-check is optional
            log(f"One-Gegenprüfung übersprungen ({type(e).__name__}).")
    doc = build_doc(ext_names, ext_progs, names, by_epg, ov, now)
    data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(out)
    log(f"epg-de.json: {len(doc['ch'])} Sender, {len(doc['names'])} Namen, {len(doc['byEpg'])} Anbieter-IDs, {len(data) / 1024:.0f} KB.")
    return doc


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="epg-de.json")
    ap.add_argument("--ext-file", help="local epg_ripper_DE1.xml.gz (otherwise downloaded)")
    ap.add_argument("--overrides", default=str(Path(__file__).resolve().parent.parent / "epg" / "overrides.json"))
    ap.add_argument("--no-one", action="store_true", help="skip the One cross-check")
    a = ap.parse_args(argv)
    out = Path(a.out)
    ext = Path(a.ext_file) if a.ext_file else Path("epg_ripper_DE1.xml.gz")
    try:
        if not a.ext_file:
            n = download(EXT_URL, ext)
            print(f"Download epgshare01: {n} Bytes")
        one_chunks = None
        if not a.no_one:
            import sport
            import sync_meta
            prov = next((p for p in sync_meta.configured_providers() if p["id"] == "one" and p.get("url") and p.get("user") and p.get("pass")), None)
            if prov:
                url = sport.xmltv_url(sync_meta.xtream_base(prov["url"]), prov["user"], prov["pass"])
                print(f"One-Gegenprüfung: {sync_meta.mask_url(url)}")
                one_chunks = sport.stream_url(url, time.monotonic() + 240, sync_meta.USER_AGENT)
            else:
                print("One-Gegenprüfung: kein One-Zugang hinterlegt – übersprungen.")
        run(ext, out, Path(a.overrides), one_chunks)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"EPG-Aufbau fehlgeschlagen: {type(e).__name__}: {str(e)[:200]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
