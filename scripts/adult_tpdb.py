#!/usr/bin/env python3
"""Adult covers from ThePornDB (TPDB) for titles the sync marks as adult.

Input   data/adult-titles.json   written by sync_meta.py (adult groups only,
                                 same detection as the TMDB exclusion:
                                 title.looks_adult / sync_meta.is_adult_group)
Cache   data/adult-tpdb.json     matches + misses + performer lookups, so a
                                 title is not asked again every night
Output  (via export_client.py)   data/client-v1/adult-*.json, map "adult"

Rules
- Secret only from env TPDB_API_KEY (GitHub secret). Never logged, never written.
- ~1 request/second (TPDB_MIN_INTERVAL_SEC), at most TPDB_MAX_REQUESTS per run,
  at most TPDB_BUDGET_MIN minutes. Unfinished titles simply wait for the next night.
- Only confident matches (title similarity, site check, year check, no ties).
- Performer image via /performers?q= only when exactly one performer has that name.
- Never fails the workflow: every error is logged (without titles or the key)
  and the script exits 0. Logs contain counts only (the Actions log is public).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from title import clean  # noqa: E402

API_BASE = "https://api.theporndb.net"
TITLES_FILE = "adult-titles.json"
CACHE_FILE = "adult-tpdb.json"
CACHE_VERSION = 1
MATCH_REFRESH_DAYS = 180
MISS_RETRY_DAYS = 30
PERFORMER_RETRY_DAYS = 60
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_MAX_REQUESTS = 1500
DEFAULT_BUDGET_MIN = 28.0
THRESHOLD = 0.9
TIE_DELTA = 0.02
PERFORMERS_MAX = 6
OVERVIEW_MAX = 600
HTTP_TIMEOUT = 20
USER_AGENT = "premiumtv-meta/1.0 (+https://github.com/Pizzamaster2026/premiumtv-meta)"

_ADULT_PREFIX = re.compile(r"^\s*\(\s*XXX\s*-?\s*\)\s*", re.I)
_DATED = re.compile(r"^([A-Za-z0-9]{3,40})[ .](\d{2})[ .](\d{2})[ .](\d{2})[ .](.+)$")
_JUNK_TOKEN = re.compile(r"(?=[a-z]*\d)(?=\d*[a-z])[0-9a-f]{10,}|\d{9,}|[a-z]*\d{6,}[a-z0-9]*")
_URL = re.compile(r"https://[A-Za-z0-9.-]+/[^\s\"'<>]{1,480}")


def log(msg: str) -> None:
    print(f"[TPDB] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def norm(s: Any) -> str:
    """Same normalisation as export_client.norm (player key logic)."""
    s = str(s or "").lower().replace("ß", "ss")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def compact(s: Any) -> str:
    return norm(s).replace(" ", "")


def adult_query(name: Any) -> dict:
    """Strip the (XXX-) catalogue marker, then the normal title cleaning."""
    s = _ADULT_PREFIX.sub("", str(name or ""), count=1)
    c = clean(s, "")
    q = c["q"].strip()
    site, part = "", q
    m = re.match(r"^(.{2,80}?)\s+[-–]\s+(.{2,})$", q)
    if m:
        site, part = m.group(1).strip(), m.group(2).strip()
    out = {"q": q, "key": norm(q), "year": c["year"], "left": site, "right": part, "date": ""}
    # Release-name style: "site.yy.mm.dd.performer.names" (TPDB's parser knows it).
    m = _DATED.match(q)
    if m:
        out["left"] = m.group(1)
        out["date"] = f"20{m.group(2)}-{m.group(3)}-{m.group(4)}"
        out["right"] = m.group(5).replace(".", " ").strip()
    return out


def junk_query(aq: dict) -> bool:
    """Hash/ID-only names (e.g. 'PARTY SEX 58e91c6191fa.720') never match; do not spend requests."""
    if len(aq["key"]) < 3:
        return True
    return any(_JUNK_TOKEN.fullmatch(t) for t in aq["key"].split())


def similarity(a: Any, b: Any) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb or compact(a) == compact(b):
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def safe_url(v: Any) -> str:
    s = str(v or "").strip()
    return s if _URL.fullmatch(s) else ""


def _img(obj: Any, *keys: str) -> str:
    """First valid https URL from obj[key], where a value may be a str or {large,full,medium,small}."""
    if not isinstance(obj, dict):
        return ""
    for k in keys:
        v = obj.get(k)
        if isinstance(v, dict):
            for sub in ("large", "full", "medium", "small"):
                u = safe_url(v.get(sub))
                if u:
                    return u
        else:
            u = safe_url(v)
            if u:
                return u
    return ""


def hit_year(hit: dict) -> str:
    m = re.match(r"((?:19|20)\d{2})", str(hit.get("date") or hit.get("year") or ""))
    return m.group(1) if m else ""


def site_names(hit: dict) -> list[str]:
    out = []
    site = hit.get("site") if isinstance(hit.get("site"), dict) else {}
    for obj in (site, site.get("parent"), site.get("network")):
        if isinstance(obj, dict) and obj.get("name"):
            out.append(str(obj["name"]))
    return out


def site_matches(label: str, hit: dict) -> bool:
    cl = compact(label)
    if len(cl) < 3:
        return False
    for n in site_names(hit):
        cn = compact(n)
        if cn and (cn == cl or (len(cn) >= 4 and (cn in cl or cl in cn))):
            return True
    return False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _rows(data: Any) -> list[dict]:
    rows = data.get("data") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def performers_match(label: str, hit: dict) -> bool:
    """'Ashley Woods & Silvia Dellai' → every name is a performer of the hit."""
    names = [n for n in re.split(r"\s*(?:&|,|\band\b|\bund\b|\+)\s*", label or "") if n.strip()]
    if not names:
        return False
    have = set()
    for p in hit.get("performers") or []:
        if isinstance(p, dict):
            have.add(norm(p.get("name")))
            if isinstance(p.get("parent"), dict):
                have.add(norm(p["parent"].get("name")))
    have.discard("")
    return all(norm(n) in have for n in names)


def score_hit(aq: dict, hit: dict, kind: str) -> float:
    title = hit.get("title") or ""
    best = similarity(aq["q"], title)
    if aq.get("date"):
        # Release name: same site and exact release date is the strong signal.
        if kind == "scene" and str(hit.get("date") or "")[:10] == aq["date"] and site_matches(aq["left"], hit):
            return 0.95
        return 0.0 if best < 1.0 else best
    if aq["left"]:
        if kind == "scene" and (site_matches(aq["left"], hit) or performers_match(aq["left"], hit)):
            best = max(best, similarity(aq["right"], title))
        if kind == "movie":
            # "Movie 3 - scene text": the DVD the scene belongs to.
            best = max(best, similarity(aq["left"], title) - 0.001)
    y = aq.get("year")
    hy = hit_year(hit)
    if y and hy and abs(int(y) - int(hy)) > 1:
        return 0.0
    return best


def pick_confident(aq: dict, data: Any, kind: str) -> Optional[dict]:
    scored = []
    for hit in _rows(data):
        if not _img(hit, "posters", "poster", "image", "background"):
            continue
        s = score_hit(aq, hit, kind)
        if s >= THRESHOLD:
            scored.append((s, hit))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    best_s, best = scored[0]
    for s, other in scored[1:]:
        if best_s - s <= TIE_DELTA and str(other.get("id")) != str(best.get("id")):
            return None  # ambiguous
    return best


def pick_performer(name: str, data: Any) -> Optional[dict]:
    """Exactly one performer whose name (or alias) equals the name."""
    target = norm(name)
    if not target:
        return None
    found = []
    for p in _rows(data):
        names = [p.get("name")] + list(p.get("aliases") or [])
        if any(norm(n) == target for n in names if isinstance(n, str)):
            found.append(p)
    ids = {str(p.get("id") or p.get("_id") or id(p)) for p in found}
    return found[0] if len(ids) == 1 else None


def performer_list(hit: dict) -> list[dict]:
    out, seen = [], set()
    for p in hit.get("performers") or []:
        if not isinstance(p, dict):
            continue
        parent = p.get("parent") if isinstance(p.get("parent"), dict) else {}
        name = str(parent.get("name") or p.get("name") or "").strip()[:80]
        if not name or norm(name) in seen:
            continue
        seen.add(norm(name))
        image = _img(parent, "face", "image") or _img(p, "face", "image")
        out.append({"name": name, "image": image})
        if len(out) >= PERFORMERS_MAX:
            break
    return out


def build_match(hit: dict, kind: str, score: float, now_iso: str) -> dict:
    poster = _img(hit, "posters", "poster", "image")
    background = _img(hit, "background", "background_back")
    if background == poster:
        background = ""
    site = site_names(hit)
    return {
        "status": "match",
        "checkedAt": now_iso,
        "source": kind,
        "tpdbId": str(hit.get("id") or "")[:64],
        "title": str(hit.get("title") or "")[:200],
        "site": site[0][:80] if site else "",
        "year": hit_year(hit),
        "poster": poster,
        "background": background,
        "overview": re.sub(r"\s+", " ", str(hit.get("description") or "")).strip()[:OVERVIEW_MAX],
        "performers": performer_list(hit),
        "score": round(score, 3),
    }


# ---------------------------------------------------------------------------
# HTTP client (throttled, capped)
# ---------------------------------------------------------------------------

class StopRun(Exception):
    """Stop this run (auth error, too many errors, cap or budget)."""


class TpdbClient:
    def __init__(self, token: str, base: str = API_BASE, min_interval: float = DEFAULT_MIN_INTERVAL,
                 max_requests: int = DEFAULT_MAX_REQUESTS, budget_sec: float = DEFAULT_BUDGET_MIN * 60,
                 opener: Optional[Callable[..., Any]] = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._token = token
        self.base = base.rstrip("/")
        self.min_interval = max(0.0, min_interval)
        self.max_requests = max_requests
        self.calls = 0
        self.errors = 0
        self._consecutive = 0
        self._last = None
        self._sleep = sleep
        self._clock = clock
        self._deadline = clock() + budget_sec
        self._open = opener or urllib.request.urlopen

    def left(self) -> int:
        return self.max_requests - self.calls

    def _throttle(self) -> None:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def get(self, path: str, params: dict) -> Any:
        """Returns parsed JSON, or None on a non-fatal error. Raises StopRun to end the run."""
        if self.calls >= self.max_requests:
            raise StopRun("cap")
        if self._clock() >= self._deadline:
            raise StopRun("budget")
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(3):
            self._throttle()
            self.calls += 1
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            })
            try:
                with self._open(req, timeout=HTTP_TIMEOUT) as resp:
                    raw = resp.read()
                self._consecutive = 0
                return json.loads(raw.decode("utf-8")) if raw else None
            except urllib.error.HTTPError as e:
                code = e.code
                if code in (401, 403):
                    raise StopRun(f"HTTP {code} (Schlüssel ungültig oder gesperrt?)")
                if code == 429 and attempt < 2 and self.calls < self.max_requests:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait = float(ra) if ra else 30.0
                    except ValueError:
                        wait = 30.0
                    self._sleep(min(max(wait, 1.0), 60.0))
                    continue
                if code == 404:
                    self._consecutive = 0
                    return {"data": []}
                err = f"HTTP {code}"
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                err = type(e).__name__
            self.errors += 1
            self._consecutive += 1
            if self._consecutive >= 5:
                raise StopRun(f"5 Fehler in Folge (zuletzt {err})")
            return None
        return None


# ---------------------------------------------------------------------------
# Lookup per title
# ---------------------------------------------------------------------------

def lookup_title(client: TpdbClient, aq: dict, now_iso: str) -> Optional[dict]:
    """Return a cache record (match/miss), or None when a request failed (retry next run)."""
    q, failed = aq["q"], False
    if aq.get("date"):
        plan = [("scene", "/scenes", q)]
    elif aq["left"]:
        plan = [("scene", "/scenes", q), ("movie", "/movies", aq["left"])]
    else:
        plan = [("movie", "/movies", q), ("scene", "/scenes", q)]
    for kind, path, term in plan:
        data = client.get(path, {"parse": term})
        if data is None:
            failed = True
            continue
        hit = pick_confident(aq, data, kind)
        if hit:
            return build_match(hit, kind, score_hit(aq, hit, kind), now_iso)
    if failed:
        return None
    return {"status": "miss", "checkedAt": now_iso}


def fill_performer_images(client: TpdbClient, rec: dict, perf_cache: dict, now: datetime, now_iso: str) -> None:
    for p in rec.get("performers") or []:
        if p.get("image"):
            continue
        k = norm(p["name"])
        cached = perf_cache.get(k)
        if cached and not is_expired(cached.get("checkedAt"), now, PERFORMER_RETRY_DAYS):
            p["image"] = cached.get("image") or ""
            continue
        if client.left() <= 0:
            return
        data = client.get("/performers", {"q": p["name"]})
        if data is None:
            continue
        hit = pick_performer(p["name"], data)
        img = (_img(hit, "face", "image") if hit else "")
        perf_cache[k] = {"image": img, "checkedAt": now_iso}
        p["image"] = img


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def parse_iso(s: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_expired(ts: Any, now: datetime, days: int) -> bool:
    d = parse_iso(ts)
    return d is None or now - d >= timedelta(days=days)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_cache(data_dir: Path) -> dict:
    doc = load_json(Path(data_dir) / CACHE_FILE, {})
    if not isinstance(doc, dict) or doc.get("version") != CACHE_VERSION:
        doc = {}
    return {"version": CACHE_VERSION,
            "titles": doc.get("titles") if isinstance(doc.get("titles"), dict) else {},
            "performers": doc.get("performers") if isinstance(doc.get("performers"), dict) else {}}


def save_cache(data_dir: Path, cache: dict, now_iso: str) -> Path:
    path = Path(data_dir) / CACHE_FILE
    doc = {"version": CACHE_VERSION, "updatedAt": now_iso,
           "titles": dict(sorted(cache["titles"].items())),
           "performers": dict(sorted(cache["performers"].items()))}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return path


def write_titles_file(data_dir: Path, groups: list, is_adult: Callable[[dict], bool], now_iso: str) -> int:
    """Called by sync_meta.py: list of adult provider items (key, name). Rewritten only when items change."""
    items = {}
    for g in groups:
        if not is_adult(g):
            continue
        for ref in g.get("refs") or []:
            if not isinstance(ref, dict):
                continue
            pid, typ, sid, name = ref.get("provider"), ref.get("type"), str(ref.get("id") or ""), ref.get("name")
            if pid and typ in ("movie", "series") and re.fullmatch(r"[0-9]+", sid) and name:
                items[f"{pid}:{typ}:{sid}"] = str(name)[:300]
    path = Path(data_dir) / TITLES_FILE
    old = load_json(path, {})
    if isinstance(old, dict) and old.get("items") == dict(sorted(items.items())):
        return len(items)
    doc = {"version": 1, "generatedAt": now_iso, "items": dict(sorted(items.items()))}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return len(items)


def pending_queries(titles: dict, cache_titles: dict, now: datetime) -> list[dict]:
    """Unique queries still to ask: never asked first (newest stream id first), then expired misses/matches."""
    best: dict[str, tuple] = {}
    for key, name in titles.items():
        aq = adult_query(name)
        if junk_query(aq):
            continue
        try:
            sid = int(key.rsplit(":", 1)[-1])
        except ValueError:
            sid = 0
        if aq["key"] not in best or sid > best[aq["key"]][0]:
            best[aq["key"]] = (sid, aq)
    fresh, retry = [], []
    for k, (sid, aq) in best.items():
        rec = cache_titles.get(k)
        if not rec:
            fresh.append((sid, aq))
        elif rec.get("status") == "miss" and is_expired(rec.get("checkedAt"), now, MISS_RETRY_DAYS):
            retry.append((parse_iso(rec.get("checkedAt")) or now, aq))
        elif rec.get("status") == "match" and is_expired(rec.get("checkedAt"), now, MATCH_REFRESH_DAYS):
            retry.append((parse_iso(rec.get("checkedAt")) or now, aq))
    fresh.sort(key=lambda x: -x[0])
    retry.sort(key=lambda x: x[0])
    return [aq for _, aq in fresh] + [aq for _, aq in retry]


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def run_step(data_dir: Path, token: Optional[str] = None, client: Optional[TpdbClient] = None,
             now: Optional[datetime] = None) -> dict:
    """Never raises. Returns counts."""
    stats = {"pending": 0, "asked": 0, "matched": 0, "missed": 0, "failed": 0, "requests": 0, "stopped": ""}
    try:
        data_dir = Path(data_dir)
        now = now or datetime.now(timezone.utc)
        now_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        titles_doc = load_json(data_dir / TITLES_FILE, {})
        titles = titles_doc.get("items") if isinstance(titles_doc, dict) else None
        if not isinstance(titles, dict) or not titles:
            log("Keine Erwachsenen-Titelliste (data/adult-titles.json) – nichts zu tun.")
            return stats
        cache = load_cache(data_dir)
        todo = pending_queries(titles, cache["titles"], now)
        stats["pending"] = len(todo)
        if client is None:
            token = (token if token is not None else os.environ.get("TPDB_API_KEY", "")).strip()
            if not token:
                log("TPDB_API_KEY fehlt – Schritt übersprungen.")
                return stats
            client = TpdbClient(
                token,
                base=os.environ.get("TPDB_API_BASE", API_BASE).strip() or API_BASE,
                min_interval=env_float("TPDB_MIN_INTERVAL_SEC", DEFAULT_MIN_INTERVAL),
                max_requests=int(env_float("TPDB_MAX_REQUESTS", DEFAULT_MAX_REQUESTS)),
                budget_sec=env_float("TPDB_BUDGET_MIN", DEFAULT_BUDGET_MIN) * 60,
            )
        log(f"{len(titles)} Erwachsenen-Einträge, {len(todo)} Suchbegriffe offen, "
            f"höchstens {client.max_requests} Anfragen in diesem Lauf.")
        try:
            for aq in todo:
                if client.left() <= 0:
                    raise StopRun("cap")
                rec = lookup_title(client, aq, now_iso)
                stats["asked"] += 1
                if rec is None:
                    stats["failed"] += 1
                    continue
                if rec["status"] == "match":
                    try:
                        fill_performer_images(client, rec, cache["performers"], now, now_iso)
                    except StopRun:
                        cache["titles"][aq["key"]] = rec
                        stats["matched"] += 1
                        raise
                    stats["matched"] += 1
                else:
                    stats["missed"] += 1
                cache["titles"][aq["key"]] = rec
        except StopRun as e:
            stats["stopped"] = str(e)
            log(f"Lauf beendet: {e}.")
        stats["requests"] = client.calls
        save_cache(data_dir, cache, now_iso)
        total_match = sum(1 for r in cache["titles"].values() if r.get("status") == "match")
        log(f"Fertig: {stats['asked']} gefragt, {stats['matched']} Treffer, {stats['missed']} ohne Treffer, "
            f"{stats['failed']} Fehler, {client.calls} Anfragen; im Cache {total_match} Treffer.")
    except Exception as e:  # never fail the workflow
        stats["stopped"] = f"error {type(e).__name__}"
        log(f"Unerwarteter Fehler ({type(e).__name__}) – Schritt übersprungen, Sync bleibt unberührt.")
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Adult covers from ThePornDB (non-fatal)")
    p.add_argument("--data-dir", type=Path, default=SCRIPT_DIR.parent / "data")
    run_step(p.parse_args(argv).data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
