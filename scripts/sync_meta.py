#!/usr/bin/env python3
"""Build a public TMDB metadata cache for PremiumTV / One catalogue titles.

Fetches only Xtream *list* endpoints (get_vod_streams, get_series) and then
TMDB details for new or stale titles. Never requests video streams or
get_vod_info / get_series_info. Incremental: keeps existing entries and only
contacts TMDB for missing titles or entries older than REFRESH_DAYS.

Credentials come from environment / GitHub Actions secrets and are never logged.

Titles files: if a provider blocks GitHub's IP ranges (PremiumTV does), the
PremiumTV Manager app uploads a reduced title list (no credentials, no stream
URLs) to data/titles_<provider>.json.gz. A fresh file (<= SYNC_TITLES_MAX_AGE_DAYS,
default 7) is used instead of the live list fetch for that provider. A stale
file is only used when the live fetch fails or no login is configured.

Sport schedule (optional, scripts/sport.py): with the One login set, one
xmltv.php request (EPG only, no streams) builds data/sport.json for the
player's "Sport live" row – next SYNC_SPORT_DAYS days (default 3), strict
title-based classifier, bounded by SYNC_SPORT_BUDGET_SEC (default 300) and
SYNC_SPORT_MAX_MB. Any failure only logs and keeps the old file; the TMDB
sync continues. SYNC_SPORT=0 switches the step off.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sport  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from title import clean, extract_tmdb_id, extract_year, looks_adult, names_equivalent, search_queries, title_key  # noqa: E402

REFRESH_DAYS = 30
MISS_DAYS = 14
# Bump when search fallbacks change so non-adult misses can be tried once more.
SEARCH_REV = 2
DEFAULT_REMATCH_MAX = 1500
TMDB_DEFAULT_BASE = "https://api.themoviedb.org/3"
LANGUAGE = "de-DE"
CAST_MAX = 12
OVERVIEW_MAX = 2000
SHARD_BYTES = 8 * 1024 * 1024
RATE_MAX = 40
RATE_WINDOW_S = 10.0
# Long read timeout for the large Xtream list endpoints; TMDB stays short.
# Overridable via SYNC_LIST_TIMEOUT_SEC / SYNC_LIST_TIMEOUT_<PROVIDER>_SEC.
HTTP_TIMEOUT_LIST = 300
HTTP_TIMEOUT_TMDB = 25
HTTP_RETRIES = 4
DEFAULT_LIST_RETRIES = 2
# Some Xtream panels ignore or stall on Python's default urllib UA.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# Leave headroom under the workflow's timeout-minutes: 120 so a partial
# result can be written and committed before the job is killed.
DEFAULT_TIME_BUDGET_MIN = 100
DEFAULT_JOB_TIMEOUT_MIN = 120
DEFAULT_JOB_MARGIN_MIN = 12
DEFAULT_CHECKPOINT_EVERY = 500

DEFAULT_TITLES_MAX_AGE_DAYS = 7.0
TITLES_FORMAT = "premiumtv-titles"
TITLES_MAX_BYTES = 256 * 1024 * 1024  # decompressed safety cap

PROVIDERS = (
    {"id": "premiumtv", "url": "PTV_URL", "user": "PTV_USER", "pass": "PTV_PASS"},
    {"id": "one", "url": "ONE_URL", "user": "ONE_USER", "pass": "ONE_PASS"},
)


# ---------------------------------------------------------------------------
# Logging (never print credentials)
# ---------------------------------------------------------------------------

def mask_secret(value: str) -> str:
    if not value:
        return "(leer)"
    return "********"


def mask_url(url: str) -> str:
    try:
        p = urllib.parse.urlparse(url)
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme or 'https'}://{host}{port}"
    except Exception:
        return "(url)"


def log(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def die(msg: str, code: int = 1) -> "None":
    log(msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, max_calls: int = RATE_MAX, window: float = RATE_WINDOW_S):
        self.max_calls = max_calls
        self.window = window
        self._times: list[float] = []

    def wait(self) -> None:
        now = time.monotonic()
        self._times = [t for t in self._times if now - t < self.window]
        if len(self._times) >= self.max_calls:
            sleep = self.window - (now - self._times[0]) + 0.05
            if sleep > 0:
                time.sleep(sleep)
        self._times.append(time.monotonic())


def describe_http_error(exc: BaseException) -> str:
    """Human-readable error for logs. Never includes credentials or full URLs."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            rmsg = str(reason).strip()
            detail = f"{type(reason).__name__}: {rmsg}" if rmsg else type(reason).__name__
        else:
            detail = str(reason).strip() or "unknown"
        detail = re.sub(r"(?i)(password|passwd|pwd|token|key|user(name)?)=[^&\s]+", r"\1=***", detail)
        detail = re.sub(r"https?://[^\s]+", "[url]", detail)
        return f"URLError ({detail})"
    if isinstance(exc, TimeoutError):
        msg = str(exc).strip()
        return f"TimeoutError ({msg})" if msg else "TimeoutError"
    if isinstance(exc, json.JSONDecodeError):
        return "JSONDecodeError"
    return type(exc).__name__


def format_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n} Bytes ({n / (1024 * 1024):.1f} MiB)"
    if n >= 1024:
        return f"{n} Bytes ({n / 1024:.1f} KiB)"
    return f"{n} Bytes"


def http_get_json(url: str, timeout: int, retries: int = HTTP_RETRIES, log_timing: bool = False) -> Any:
    """GET JSON. The URL must already be safe to log only via mask_url()."""
    last_err: Optional[Exception] = None
    if retries < 1:
        retries = 1
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                "User-Agent": USER_AGENT,
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                raw = resp.read()
            elapsed = time.monotonic() - started
            if log_timing:
                log(f"    Antwort HTTP {status}, {format_bytes(len(raw) if raw else 0)} in {elapsed:.1f}s")
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(retry_after) if retry_after else (2 ** attempt) + 1
                except ValueError:
                    wait = (2 ** attempt) + 1
                log(f"  TMDB/HTTP 429, warte {wait:.0f}s …")
                time.sleep(min(wait, 60))
                continue
            if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                wait = 2 ** attempt
                log(f"  {describe_http_error(e)}, Versuch {attempt + 1}/{retries}, warte {wait}s …")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < retries - 1:
                wait = 2 ** attempt
                log(f"  {describe_http_error(e)}, Versuch {attempt + 1}/{retries}, warte {wait}s …")
                time.sleep(wait)
                continue
            raise
    raise last_err or RuntimeError("http_get_json failed")


XTREAM_ENDPOINTS = ("player_api.php", "get.php", "panel_api.php", "xmltv.php")


def xtream_base(url: str) -> str:
    """Normalize an Xtream base URL.

    Accepts a bare host:port, a trailing slash, or a full player_api.php / get.php
    (or panel_api.php / xmltv.php) URL including a query string. Subpaths such as
    /ptv are kept. Query strings are dropped so credentials never stay in the base.
    A missing scheme defaults to http://, which is the usual Xtream panel setup.
    """
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        u = "http:" + u
    else:
        parsed = urllib.parse.urlparse(u)
        if not (parsed.scheme in ("http", "https") and parsed.netloc):
            u = "http://" + u

    p = urllib.parse.urlparse(u)
    path = p.path or ""
    leaf = path.rsplit("/", 1)[-1].lower()
    if leaf in XTREAM_ENDPOINTS or any(leaf.startswith(ep) for ep in XTREAM_ENDPOINTS):
        path = path[: len(path) - len(path.rsplit("/", 1)[-1])].rstrip("/")
    path = path.rstrip("/")
    scheme = p.scheme if p.scheme in ("http", "https") else "http"
    netloc = p.netloc
    if not netloc:
        return ""
    return f"{scheme}://{netloc}{path}"


def xtream_list_url(base: str, user: str, password: str, action: str) -> str:
    q = urllib.parse.urlencode({"username": user, "password": password, "action": action})
    return f"{xtream_base(base)}/player_api.php?{q}"


ALLOWED_XTREAM_ACTIONS = frozenset({"get_vod_streams", "get_series"})


def fetch_xtream_list(
    base: str,
    user: str,
    password: str,
    action: str,
    timeout: Optional[int] = None,
    retries: Optional[int] = None,
) -> list:
    if action not in ALLOWED_XTREAM_ACTIONS:
        raise ValueError(f"refusing Xtream action {action!r} (only list endpoints are allowed)")
    url = xtream_list_url(base, user, password, action)
    timeout = HTTP_TIMEOUT_LIST if timeout is None else int(timeout)
    retries = DEFAULT_LIST_RETRIES if retries is None else int(retries)
    log(f"  Liste {action} von {mask_url(url)} (Timeout {timeout}s, {retries} Versuche) …")
    try:
        data = http_get_json(url, timeout=timeout, retries=retries, log_timing=True)
    except Exception as e:
        raise RuntimeError(
            f"Liste {action} von {mask_url(url)} fehlgeschlagen: {describe_http_error(e)}"
        ) from e
    if isinstance(data, dict):
        # Typical error envelope: {"user_info": {"auth": 0, ...}}
        info = data.get("user_info") if isinstance(data.get("user_info"), dict) else {}
        if info and str(info.get("auth", "1")) in ("0", "false", "False"):
            raise RuntimeError(f"Zugang zu {mask_url(url)} wurde abgelehnt (auth=0).")
        for key in ("streams", "series", action, "data"):
            if isinstance(data.get(key), list):
                return data[key]
        raise RuntimeError(f"Unerwartete Antwort von {mask_url(url)} (kein Listen-Array).")
    if not isinstance(data, list):
        raise RuntimeError(f"Unerwartete Antwort von {mask_url(url)}.")
    return data


# ---------------------------------------------------------------------------
# Titles file uploaded by the PremiumTV Manager app
# ---------------------------------------------------------------------------

def titles_file_path(data_dir: Path, provider_id: str) -> Path:
    pid = re.sub(r"[^a-z0-9]+", "", (provider_id or "").lower())
    return Path(data_dir) / f"titles_{pid}.json.gz"


def titles_max_age_days(provider_id: str) -> float:
    n = float(env_for_provider(provider_id, "SYNC_TITLES_MAX_AGE_DAYS", DEFAULT_TITLES_MAX_AGE_DAYS, float))
    return n if n >= 0 else 0.0


def _titles_rows(rows: Any, kind: str) -> list[dict]:
    """Convert titles-file rows to Xtream-like list items (only safe fields)."""
    out: list[dict] = []
    if not isinstance(rows, list):
        return out
    id_key = "series_id" if kind == "series" else "stream_id"
    for r in rows:
        if not isinstance(r, dict):
            continue
        sid = str(r.get("id") if r.get("id") is not None else "").strip()
        name = str(r.get("name") or "").strip()
        if not sid or not name:
            continue
        item: dict = {id_key: sid, "name": name[:300]}
        year = extract_year(str(r.get("year") or ""))
        if year:
            item["year"] = year
        tmdb = r.get("tmdb")
        if tmdb not in (None, "", 0, "0"):
            s = str(tmdb).strip()
            if re.fullmatch(r"\d{1,9}", s):
                item["tmdb_id"] = s
        cat = r.get("cat")
        if cat not in (None, ""):
            item["category_id"] = str(cat)[:40]
        out.append(item)
    return out


def load_titles_file(path: Path, now: datetime) -> tuple[dict[str, list], dict]:
    """Read data/titles_<provider>.json.gz.

    Returns ({"movie": [...], "series": [...]}, info) where info has
    generatedAt, age_days, movies, series. Raises ValueError when the file is
    unreadable, has the wrong format, has no timestamp or no titles.
    """
    try:
        with gzip.open(path, "rb") as fh:
            raw = fh.read(TITLES_MAX_BYTES + 1)
    except (OSError, EOFError) as e:
        raise ValueError(f"nicht lesbar ({type(e).__name__})") from e
    if len(raw) > TITLES_MAX_BYTES:
        raise ValueError("zu groß")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("kein gültiges JSON") from e
    if not isinstance(doc, dict) or doc.get("format") != TITLES_FORMAT:
        raise ValueError("falsches Format")
    try:
        version = int(doc.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != 1:
        raise ValueError(f"unbekannte Version {doc.get('version')!r}")
    ts = parse_iso(str(doc.get("generatedAt") or ""))
    if not ts:
        raise ValueError("generatedAt fehlt")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = (now - ts).total_seconds() / 86400.0
    if age_days < -1.0:
        raise ValueError("generatedAt liegt in der Zukunft")
    age_days = max(0.0, age_days)
    lists = {"movie": _titles_rows(doc.get("movie"), "movie"), "series": _titles_rows(doc.get("series"), "series")}
    if not lists["movie"] and not lists["series"]:
        raise ValueError("enthält keine Titel")
    info = {
        "generatedAt": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "age_days": age_days,
        "movies": len(lists["movie"]),
        "series": len(lists["series"]),
    }
    return lists, info


def try_titles_file(data_dir: Path, provider_id: str, now: datetime) -> tuple[Optional[dict], Optional[dict], str]:
    """Return (lists, info, status) with status 'missing', 'invalid', 'fresh' or 'stale'."""
    path = titles_file_path(data_dir, provider_id)
    if not path.is_file():
        return None, None, "missing"
    try:
        lists, info = load_titles_file(path, now)
    except ValueError as e:
        log(f"  Titeldatei {path.name} unbrauchbar: {e} – wird ignoriert.")
        return None, None, "invalid"
    max_age = titles_max_age_days(provider_id)
    status = "fresh" if info["age_days"] <= max_age else "stale"
    info["max_age_days"] = max_age
    info["file"] = path.name
    return lists, info, status


def describe_titles_info(info: dict) -> str:
    return (
        f"{info['file']} vom {info['generatedAt']} "
        f"(Alter {info['age_days']:.1f} Tage, Grenze {info['max_age_days']:g} Tage; "
        f"{info['movies']} Filme, {info['series']} Serien)"
    )


# ---------------------------------------------------------------------------
# Provider catalogue
# ---------------------------------------------------------------------------

def item_name(item: dict) -> str:
    return str(item.get("name") or item.get("title") or "").strip()


def item_year(item: dict, kind: str) -> str:
    for key in ("year", "releaseDate", "release_date", "releasedate", "first_air_date"):
        y = extract_year(item.get(key, ""))
        if y:
            return y
    return clean(item_name(item), "").get("year") or ""


def item_id(item: dict, kind: str) -> str:
    """Provider stream/series id.

    Official Xtream lists use series_id / stream_id, but some panels only send
    `id` (the player already copies id → series_id/stream_id). Accept those
    fallbacks so a whole series list is not silently dropped.
    """
    keys = ("series_id", "id", "stream_id") if kind == "series" else ("stream_id", "vod_id", "id")
    for key in keys:
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        s = str(raw).strip()
        if s:
            return s
    return ""


def collect_titles(catalogues: dict[str, dict[str, list]]) -> list[dict]:
    """Deduplicate titles across providers. Each group maps many stream ids to one TMDB lookup."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    skipped_no_id = skipped_no_name = skipped_short = skipped_not_dict = 0
    for provider, lists in catalogues.items():
        for kind, items in lists.items():
            typ = "series" if kind == "series" else "movie"
            raw_n = len(items) if isinstance(items, list) else 0
            first = next((x for x in items if isinstance(x, dict)), None) if isinstance(items, list) else None
            if first is not None:
                fields = ", ".join(sorted(str(k) for k in first.keys()))
                log(f"  {provider}/{typ}: {raw_n} Roh-Einträge, Felder: {fields}")
            elif raw_n:
                log(f"  {provider}/{typ}: {raw_n} Roh-Einträge, erster Typ: {type(items[0]).__name__}")
            for item in items:
                if not isinstance(item, dict):
                    skipped_not_dict += 1
                    continue
                sid = item_id(item, kind)
                name = item_name(item)
                if not sid:
                    skipped_no_id += 1
                    continue
                if not name:
                    skipped_no_name += 1
                    continue
                year = item_year(item, typ)
                tmdb_id = extract_tmdb_id(item)
                c = clean(name, year)
                if tmdb_id:
                    gid = f"{typ}:id:{tmdb_id}"
                elif c["q"] and len(c["q"]) >= 2:
                    gid = f"{typ}:title:{title_key(typ, name, year)}"
                else:
                    skipped_short += 1
                    continue
                if gid not in groups:
                    groups[gid] = {
                        "gid": gid,
                        "type": typ,
                        "tmdb_id": tmdb_id,
                        "query": c["q"],
                        "year": c["year"],
                        "samples": [],
                        "refs": [],
                    }
                    order.append(gid)
                g = groups[gid]
                if not g["tmdb_id"] and tmdb_id:
                    g["tmdb_id"] = tmdb_id
                if not g["year"] and c["year"]:
                    g["year"] = c["year"]
                g["refs"].append({"provider": provider, "type": typ, "id": sid, "name": name, "year": year})
                if len(g["samples"]) < 3:
                    g["samples"].append(name)
    out = [groups[k] for k in order]
    n_movie = sum(1 for g in out if g["type"] == "movie")
    n_series = sum(1 for g in out if g["type"] == "series")
    log(f"{len(out)} eindeutige Titel ({n_movie} Filme, {n_series} Serien) über alle Anbieter.")
    skipped = skipped_no_id + skipped_no_name + skipped_short + skipped_not_dict
    if skipped:
        log(
            f"  Übersprungen: {skipped_no_id} ohne ID, {skipped_no_name} ohne Namen, "
            f"{skipped_short} ohne brauchbaren Titel, {skipped_not_dict} kein Objekt."
        )
    return interleave_by_type(out)


def interleave_by_type(groups: list[dict]) -> list[dict]:
    """Round-robin movies and series so a TMDB time budget cannot starve one type."""
    buckets: dict[str, list[dict]] = {"movie": [], "series": []}
    other: list[dict] = []
    for g in groups:
        typ = g.get("type")
        if typ in buckets:
            buckets[typ].append(g)
        else:
            other.append(g)
    out: list[dict] = []
    n = max((len(v) for v in buckets.values()), default=0)
    for i in range(n):
        for typ in ("movie", "series"):
            if i < len(buckets[typ]):
                out.append(buckets[typ][i])
    out.extend(other)
    return out


# ---------------------------------------------------------------------------
# TMDB
# ---------------------------------------------------------------------------

class TmdbClient:
    def __init__(self, api_key: str, base: str):
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.limiter = RateLimiter()
        self.calls = 0

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        p = dict(params or {})
        p["api_key"] = self.api_key
        url = self.base + path + "?" + urllib.parse.urlencode(p)
        self.limiter.wait()
        self.calls += 1
        try:
            return http_get_json(url, timeout=HTTP_TIMEOUT_TMDB)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 401:
                die("TMDB hat den API-Schlüssel abgelehnt (HTTP 401). Bitte TMDB_API_KEY prüfen.", 1)
            raise RuntimeError(f"TMDB {path} von {mask_url(self.base)} fehlgeschlagen: HTTP {e.code}") from e
        except Exception as e:
            raise RuntimeError(f"TMDB {path} von {mask_url(self.base)} fehlgeschlagen: {type(e).__name__}") from e

    def search(self, typ: str, query: str, year: str) -> Optional[int]:
        """Search TMDB. Fallbacks (no year, umlaut variant, en-US) only accept a safe title match."""
        tv = typ == "series"
        path = "/search/tv" if tv else "/search/movie"
        year_key = "first_air_date_year" if tv else "year"
        queries = search_queries(query)
        langs = [LANGUAGE]
        if not LANGUAGE.lower().startswith("en"):
            langs.append("en-US")

        def run(q: str, y: str, lang: str, strict: bool) -> Optional[dict]:
            params = {"query": q, "language": lang, "include_adult": "false", "page": "1"}
            if y:
                params[year_key] = y
            return _pick_search(self.get(path, params), q, strict=strict)

        # Primary: current language, with year if we have one; existing acceptance rules.
        hit = run(queries[0], year, LANGUAGE, False)
        if not hit and year:
            hit = run(queries[0], "", LANGUAGE, True)
        if not hit:
            for q in queries:
                for lang in langs:
                    if q == queries[0] and lang == LANGUAGE:
                        continue
                    hit = run(q, year, lang, True)
                    if hit:
                        break
                    if year:
                        hit = run(q, "", lang, True)
                    if hit:
                        break
                if hit:
                    break
        if not hit:
            return None
        try:
            n = int(hit.get("id") or 0)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    def details(self, typ: str, tmdb_id: int) -> Optional[dict]:
        path = f"/tv/{tmdb_id}" if typ == "series" else f"/movie/{tmdb_id}"
        data = self.get(path, {"language": LANGUAGE, "append_to_response": "credits"})
        if not isinstance(data, dict) or not data.get("id"):
            return None
        return normalize_details(typ, data)


def _pick_search(data: Any, query: str, strict: bool = False) -> Optional[dict]:
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return None
    want = query.lower().strip()
    for r in results:
        if not isinstance(r, dict):
            continue
        names = [r.get("title"), r.get("name"), r.get("original_title"), r.get("original_name")]
        if any(t and str(t).lower().strip() == want for t in names):
            return r
        if any(t and names_equivalent(t, query) for t in names):
            return r
    if strict:
        return None
    return results[0] if isinstance(results[0], dict) else None


def _safe_path(value: Any) -> str:
    s = value if isinstance(value, str) else ""
    return s if re.fullmatch(r"/[\w.-]+\.(jpg|jpeg|png|webp|svg)", s, re.I) else ""


def normalize_details(typ: str, data: dict) -> dict:
    genres = []
    for g in data.get("genres") or []:
        if isinstance(g, dict):
            name = str(g.get("name") or "").strip()
            if name and name not in genres:
                genres.append(name)
    runtime = 0
    if typ == "series":
        ert = data.get("episode_run_time")
        if isinstance(ert, list) and ert:
            try:
                runtime = int(ert[0] or 0)
            except (TypeError, ValueError):
                runtime = 0
    else:
        try:
            runtime = int(data.get("runtime") or 0)
        except (TypeError, ValueError):
            runtime = 0
    year = ""
    for key in ("release_date", "first_air_date"):
        year = extract_year(data.get(key, ""))
        if year:
            break
    title = str(data.get("title") or data.get("name") or "").strip()
    original = str(data.get("original_title") or data.get("original_name") or "").strip()
    credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
    cast_in = credits.get("cast") if isinstance(credits.get("cast"), list) else []
    # honour official 'order' when present
    def _order(c: dict) -> int:
        try:
            return int(c.get("order") if c.get("order") is not None else 999)
        except (TypeError, ValueError):
            return 999

    rows = [c for c in cast_in if isinstance(c, dict)]
    rows.sort(key=_order)
    cast = []
    for c in rows[:CAST_MAX]:
        try:
            pid = int(c.get("id") or 0)
        except (TypeError, ValueError):
            pid = 0
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        cast.append({
            "id": pid or None,
            "name": name,
            "character": str(c.get("character") or "").strip(),
            "profilePath": _safe_path(c.get("profile_path")),
        })
    try:
        va = round(float(data.get("vote_average") or 0) + 1e-9, 1)
    except (TypeError, ValueError):
        va = 0.0
    try:
        vc = int(data.get("vote_count") or 0)
    except (TypeError, ValueError):
        vc = 0
    try:
        tid = int(data.get("id") or 0)
    except (TypeError, ValueError):
        tid = 0
    return {
        "tmdbId": tid,
        "type": typ,
        "title": title,
        "originalTitle": original,
        "year": year,
        "genres": genres,
        "voteAverage": va,
        "voteCount": vc,
        "overview": str(data.get("overview") or "")[:OVERVIEW_MAX],
        "runtime": runtime if runtime > 0 else 0,
        "posterPath": _safe_path(data.get("poster_path")),
        "backdropPath": _safe_path(data.get("backdrop_path")),
        "cast": cast,
    }


def entry_key(typ: str, tmdb_id: int) -> str:
    return f"{'tv' if typ == 'series' else 'movie'}:{tmdb_id}"


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_fresh(entry: dict, now: datetime, days: int) -> bool:
    ts = parse_iso(str(entry.get("updatedAt") or ""))
    if not ts:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() < days * 86400


def env_number(name: str, default: float, kind=float):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return kind(raw)
    except (TypeError, ValueError):
        return default


def env_for_provider(provider_id: str, base: str, default, kind=float):
    """Prefer a per-provider override, then the shared env var, then default.

    Example: base SYNC_LIST_TIMEOUT_SEC + provider premiumtv
    reads SYNC_LIST_TIMEOUT_PREMIUMTV_SEC, then SYNC_LIST_TIMEOUT_SEC.
    """
    pid = re.sub(r"[^A-Z0-9]+", "_", (provider_id or "").strip().upper()).strip("_")
    if pid:
        if base.endswith("_SEC"):
            specific = f"{base[:-4]}_{pid}_SEC"
        elif base.endswith("_MIN"):
            specific = f"{base[:-4]}_{pid}_MIN"
        else:
            specific = f"{base}_{pid}"
        raw = os.environ.get(specific, "").strip()
        if raw:
            try:
                return kind(raw)
            except (TypeError, ValueError):
                pass
    return env_number(base, default, kind)


def list_timeout_sec(provider_id: str) -> int:
    n = int(env_for_provider(provider_id, "SYNC_LIST_TIMEOUT_SEC", HTTP_TIMEOUT_LIST, float))
    return n if n >= 1 else 1


def list_retries(provider_id: str) -> int:
    n = int(env_for_provider(provider_id, "SYNC_LIST_RETRIES", DEFAULT_LIST_RETRIES, float))
    return n if n >= 1 else 1


def tmdb_budget_after(elapsed_sec: float) -> tuple[float, float]:
    """TMDB lookup budget: leftover job time minus a write/commit margin, capped by SYNC_TIME_BUDGET_MIN."""
    job_min = env_number("SYNC_JOB_TIMEOUT_MIN", DEFAULT_JOB_TIMEOUT_MIN, float)
    margin_min = env_number("SYNC_JOB_MARGIN_MIN", DEFAULT_JOB_MARGIN_MIN, float)
    cap_min = env_number("SYNC_TIME_BUDGET_MIN", DEFAULT_TIME_BUDGET_MIN, float)
    if job_min < 0:
        job_min = 0.0
    if margin_min < 0:
        margin_min = 0.0
    if cap_min < 0:
        cap_min = 0.0
    remaining_min = max(0.0, job_min - max(0.0, elapsed_sec) / 60.0 - margin_min)
    minutes = min(remaining_min, cap_min)
    return minutes, minutes * 60.0


def time_budget_seconds() -> tuple[float, float]:
    minutes = env_number("SYNC_TIME_BUDGET_MIN", DEFAULT_TIME_BUDGET_MIN, float)
    if minutes < 0:
        minutes = 0.0
    return minutes, minutes * 60.0


def checkpoint_every() -> int:
    n = int(env_number("SYNC_CHECKPOINT_EVERY", DEFAULT_CHECKPOINT_EVERY, float))
    return n if n > 0 else DEFAULT_CHECKPOINT_EVERY


def build_indexes(groups: list[dict]) -> tuple[dict, dict]:
    by_id: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for g in groups:
        ekey = g.get("resolved")
        if not ekey:
            continue
        for ref in g["refs"]:
            by_id[f"{ref['provider']}:{ref['type']}:{ref['id']}"] = ekey
            by_title[title_key(ref["type"], ref["name"], ref["year"])] = ekey
        by_title[title_key(g["type"], g["query"], g["year"])] = ekey
    return by_id, by_title


def catalogue_title_keys(groups: list[dict]) -> set[str]:
    keys: set[str] = set()
    for g in groups:
        keys.add(title_key(g["type"], g["query"], g["year"]))
        for ref in g["refs"]:
            keys.add(title_key(ref["type"], ref["name"], ref["year"]))
    return keys


def prune_unreferenced_entries(entries: dict, by_id: dict, by_title: dict) -> dict:
    """Drop cache entries that no current catalogue title points at."""
    referenced = set(by_id.values()) | set(by_title.values())
    return {k: e for k, e in entries.items() if k in referenced}


def prune_misses(misses: dict, groups: list[dict], now: datetime) -> dict:
    """Drop misses for titles that left the catalogue; keep others for 180 days."""
    live = catalogue_title_keys(groups)
    return {k: m for k, m in misses.items() if k in live and is_fresh(m, now, 180)}


# ---------------------------------------------------------------------------
# Load / write cache
# ---------------------------------------------------------------------------

def load_existing(data_dir: Path) -> tuple[dict, dict, dict]:
    entries: dict = {}
    misses: dict = {}
    by_title: dict = {}
    index_path = data_dir / "index.json"
    meta_path = data_dir / "meta.json"
    files: list[Path] = []
    if index_path.is_file():
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            idx = {}
        misses = idx.get("misses") if isinstance(idx.get("misses"), dict) else {}
        by_title = idx.get("byTitle") if isinstance(idx.get("byTitle"), dict) else {}
        shards = idx.get("shards")
        if isinstance(shards, dict):
            files = [data_dir / Path(str(s)).name for s in shards.values()]
        elif idx.get("metaFile"):
            files = [data_dir / Path(str(idx["metaFile"])).name]
    if not files and meta_path.is_file():
        files = [meta_path]
    files += sorted(data_dir.glob("meta-*.json"))
    seen = set()
    for f in files:
        f = Path(f)
        if not f.is_file() or f.resolve() in seen:
            continue
        seen.add(f.resolve())
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        block = doc.get("entries") if isinstance(doc, dict) else None
        if isinstance(block, dict):
            entries.update(block)
    return entries, misses, by_title


def content_fingerprint(entries: dict, index_body: dict) -> str:
    # Include updatedAt so a TMDB refresh is persisted (otherwise the next
    # nightly run would fetch the same stale entry again). generatedAt on the
    # index is ignored so a no-op run does not rewrite files.
    slim_idx = {k: v for k, v in index_body.items() if k != "generatedAt"}
    return json.dumps({"e": entries, "i": slim_idx}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))



def coverage_from_groups(groups: list[dict], misses: dict) -> dict:
    """Split unique titles (and per-provider views) into withTmdb / noMatch / pending."""
    def empty() -> dict:
        return {"total": 0, "withTmdb": 0, "noMatch": 0, "pending": 0, "adultSkipped": 0}

    def kind_of(g: dict) -> str:
        if g.get("resolved"):
            return "withTmdb"
        miss_key = title_key(g["type"], g["query"], g["year"])
        return "noMatch" if miss_key in misses else "pending"

    def add(block: dict, typ: str, kind: str, adult: bool = False) -> None:
        slot = "movies" if typ == "movie" else "series"
        row = block.setdefault(slot, empty())
        if adult:
            row["adultSkipped"] += 1
            return
        row["total"] += 1
        row[kind] += 1

    def with_pct(row: dict) -> dict:
        total = row["total"]
        row["pct"] = round(100.0 * row["withTmdb"] / total, 1) if total else 0.0
        return row

    overall = empty()
    providers: dict[str, dict] = {}
    for g in groups:
        typ = g.get("type") or "movie"
        adult = is_adult_group(g)
        kind = "pending" if adult else kind_of(g)
        if adult:
            overall["adultSkipped"] += 1
        else:
            overall["total"] += 1
            overall[kind] += 1
        seen = set()
        for ref in g.get("refs") or []:
            if not isinstance(ref, dict):
                continue
            pid = str(ref.get("provider") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            block = providers.setdefault(pid, {"adultSkipped": 0})
            if adult:
                block["adultSkipped"] = int(block.get("adultSkipped") or 0) + 1
            add(block, typ, kind, adult=adult)
    with_pct(overall)
    for block in providers.values():
        for slot in ("movies", "series"):
            if slot in block:
                with_pct(block[slot])
    return {"overall": overall, "providers": providers}


def build_status(
    *,
    groups: list[dict],
    misses: dict,
    stats: dict,
    updated_at: str,
    phase: str,
    running: bool,
) -> dict:
    cov = coverage_from_groups(groups, misses)
    overall = cov["overall"]
    titles = int(overall["total"])
    with_tmdb = int(overall["withTmdb"])
    no_match = int(overall["noMatch"])
    pending = int(overall["pending"])
    queried = stats.get("queried")
    open_total = stats.get("openTotal")
    prog_processed = int(queried) if queried is not None else with_tmdb + no_match
    prog_total = int(open_total) if open_total is not None else titles
    progress = {
        "processed": prog_processed,
        "total": prog_total,
        "remaining": pending if open_total is None else max(0, prog_total - prog_processed),
        "new": int(stats.get("new") or 0),
        "refreshed": int(stats.get("refreshed") or 0),
        "tmdbCalls": int(stats.get("tmdbCalls") or 0),
    }
    if stats.get("elapsedSec") is not None:
        try:
            progress["elapsedSec"] = int(stats["elapsedSec"])
        except (TypeError, ValueError):
            pass
    doc = {
        "version": 1,
        "updatedAt": updated_at,
        "phase": phase,
        "running": running,
        "partial": bool(stats.get("partial")),
        "budgetStopped": bool(stats.get("budgetStopped")),
        "titles": titles,
        "withTmdb": with_tmdb,
        "noMatch": no_match,
        "pending": pending,
        "coveragePct": overall.get("pct") or 0.0,
        "adultSkipped": int(overall.get("adultSkipped") or 0),
        "providers": cov["providers"],
        "progress": progress,
    }
    if open_total is not None:
        doc["openTotal"] = int(open_total)
        doc["queried"] = int(queried or 0)
    return doc


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Test hook: callable(doc) -> bool. When set, publish_live_status uses it instead of GitHub.
live_publisher = None
_last_live_push = 0.0
DEFAULT_STATUS_PUSH_SEC = 240


def live_push_enabled() -> bool:
    return os.environ.get("SYNC_STATUS_BRANCH_PUSH", "1").strip().lower() not in ("0", "false", "no", "off")


def github_api(method: str, path: str, token: str, body: Optional[dict] = None) -> tuple[int, Any]:
    """GitHub REST call. Token only in the Authorization header, never logged."""
    base = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    url = base + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "premiumtv-meta-sync")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if e.fp else ""
        try:
            parsed: Any = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        return e.code, parsed


def publish_live_status(doc: dict) -> bool:
    """Force-update branch 'status' so it contains only data/status.json. Never logs tokens."""
    if live_publisher is not None:
        return bool(live_publisher(doc))
    if not live_push_enabled():
        return False
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        return False
    branch = (os.environ.get("SYNC_STATUS_BRANCH") or "status").strip() or "status"
    try:
        text = json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"
        code, blob = github_api("POST", f"/repos/{repo}/git/blobs", token, {"content": text, "encoding": "utf-8"})
        if code >= 300 or not isinstance(blob, dict) or not blob.get("sha"):
            log("Live-Status: Blob konnte nicht angelegt werden.")
            return False
        code, tree = github_api(
            "POST",
            f"/repos/{repo}/git/trees",
            token,
            {"tree": [{"path": "data/status.json", "mode": "100644", "type": "blob", "sha": blob["sha"]}]},
        )
        if code >= 300 or not isinstance(tree, dict) or not tree.get("sha"):
            log("Live-Status: Tree konnte nicht angelegt werden.")
            return False
        code, commit = github_api(
            "POST",
            f"/repos/{repo}/git/commits",
            token,
            {"message": "chore: Sync-Fortschritt", "tree": tree["sha"], "parents": []},
        )
        if code >= 300 or not isinstance(commit, dict) or not commit.get("sha"):
            log("Live-Status: Commit konnte nicht angelegt werden.")
            return False
        sha = commit["sha"]
        code, _ref = github_api("GET", f"/repos/{repo}/git/ref/heads/{branch}", token)
        if code == 404:
            code, _ref = github_api("POST", f"/repos/{repo}/git/refs", token, {"ref": f"refs/heads/{branch}", "sha": sha})
        else:
            code, _ref = github_api(
                "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", token, {"sha": sha, "force": True}
            )
        if code >= 300:
            log("Live-Status: Zweig konnte nicht aktualisiert werden.")
            return False
        log("Live-Status auf den Zweig status geschrieben.")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Live-Status: übersprungen ({type(e).__name__}).")
        return False


def maybe_publish_live(doc: dict, force: bool = False) -> bool:
    """Publish to the status branch, at most every SYNC_STATUS_PUSH_SEC (default 4 Min.)."""
    global _last_live_push
    interval = max(30.0, float(env_number("SYNC_STATUS_PUSH_SEC", DEFAULT_STATUS_PUSH_SEC, float)))
    now = time.monotonic()
    if not force and _last_live_push and (now - _last_live_push) < interval:
        return False
    ok = publish_live_status(doc)
    if ok:
        _last_live_push = now
    return ok


def write_status(data_dir: Path, groups: list[dict], misses: dict, stats: dict, partial: bool, force_live: bool = False) -> Path:
    """Small public data/status.json for the Manager dashboard. Never contains secrets.

    Also publishes a snapshot to the 'status' branch (throttled), so the Manager
    can show live progress without committing to main mid-run.
    """
    phase = "tmdb" if partial else ("budget_stopped" if stats.get("budgetStopped") else "done")
    if stats.get("phase"):
        phase = str(stats["phase"])
    doc = build_status(
        groups=groups,
        misses=misses,
        stats=stats,
        updated_at=utc_now_iso(),
        phase=phase,
        running=partial,
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "status.json"
    _atomic_write(path, doc)
    maybe_publish_live(doc, force=force_live or not partial)
    return path


def existing_entry_key(g: dict, entries: dict, prev_by_title: dict) -> Optional[str]:
    typ, query, year = g["type"], g["query"], g["year"]
    tid = g.get("tmdb_id")
    if tid:
        return entry_key(typ, tid)
    miss_key = title_key(typ, query, year)
    prev = prev_by_title.get(miss_key)
    if not prev:
        for ref in g.get("refs") or []:
            if not isinstance(ref, dict):
                continue
            prev = prev_by_title.get(title_key(ref.get("type") or typ, ref.get("name") or "", ref.get("year") or ""))
            if prev:
                break
    if prev and prev in entries:
        return prev
    return None


def apply_existing_to_groups(groups: list[dict], entries: dict, prev_by_title: dict) -> int:
    """Set group['resolved'] from the on-disk cache so mid-run coverage counts
    already-known TMDB matches. Does not mark misses; coverage_from_groups
    treats those via the misses dict. Pending is then only titles with no
    answer yet (no cache hit and no miss).
    """
    n = 0
    for g in groups:
        if g.get("resolved"):
            n += 1
            continue
        key = existing_entry_key(g, entries, prev_by_title)
        if key:
            g["resolved"] = key
            n += 1
    return n


def is_adult_group(g: dict) -> bool:
    if looks_adult(g.get("query") or ""):
        return True
    for ref in g.get("refs") or []:
        if isinstance(ref, dict) and looks_adult(ref.get("name") or ""):
            return True
    return False


def miss_query_changed(miss: dict, g: dict) -> bool:
    """True if the stored miss was recorded under a weaker cleaning of the title."""
    stored = (miss or {}).get("query") or ""
    stored_year = str((miss or {}).get("year") or "")
    new_q = g.get("query") or ""
    new_y = str(g.get("year") or "")
    # Re-cleaning the stored query catches misses recorded before a cleaner upgrade.
    old = clean(stored, stored_year)
    return stored != new_q or old["q"] != new_q or (new_y and stored_year and stored_year != new_y and old["year"] != new_y)


def miss_should_retry(miss: Optional[dict], g: dict, now: datetime) -> bool:
    """Retry a miss if it expired, the cleaned query changed, or search fallbacks were added."""
    if not miss:
        return True
    if not is_fresh(miss, now, MISS_DAYS):
        return True
    # Adult titles stay unmatched (include_adult=false). Do not churn them
    # when only a quality tag such as -1080p was stripped from the stored query.
    if miss_query_changed(miss, g) and not is_adult_group(g):
        return True
    try:
        rev = int(miss.get("searchRev") or 0)
    except (TypeError, ValueError):
        rev = 0
    if rev < SEARCH_REV and not is_adult_group(g):
        return True
    return False


def needs_tmdb_lookup(g: dict, entries: dict, misses: dict, prev_by_title: dict, now: datetime) -> bool:
    """True if this run still needs a TMDB search or details call for the title."""
    if is_adult_group(g):
        return False
    typ, query, year = g["type"], g["query"], g["year"]
    tid = g.get("tmdb_id")
    existing_key = existing_entry_key(g, entries, prev_by_title)
    entry = entries.get(existing_key) if existing_key else None
    miss = misses.get(title_key(typ, query, year))
    if entry and is_fresh(entry, now, REFRESH_DAYS):
        return False
    if not tid and miss and not miss_should_retry(miss, g, now):
        return False
    return True


def write_outputs(data_dir: Path, entries: dict, by_id: dict, by_title: dict, misses: dict, generated_at: str, stats: dict) -> list[Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    # Drop unused shard/meta files after write
    previous = set(data_dir.glob("meta*.json"))

    payload_entries = dict(sorted(entries.items()))
    probe = json.dumps({"version": 1, "entries": payload_entries}, ensure_ascii=False, separators=(",", ":"))
    written: list[Path] = []
    shards = None
    meta_file = "data/meta.json"

    if len(probe.encode("utf-8")) > SHARD_BYTES:
        buckets: dict[str, dict] = {}
        for key, ent in payload_entries.items():
            letter = shard_letter(ent, key)
            buckets.setdefault(letter, {})[key] = ent
        shards = {}
        for letter, bucket in sorted(buckets.items()):
            rel = f"data/meta-{letter}.json"
            path = data_dir / f"meta-{letter}.json"
            doc = {"version": 1, "generatedAt": generated_at, "shard": letter, "entries": bucket}
            _atomic_write(path, doc)
            shards[letter] = rel
            written.append(path)
        meta_file = None
    else:
        path = data_dir / "meta.json"
        _atomic_write(path, {"version": 1, "generatedAt": generated_at, "language": LANGUAGE, "entries": payload_entries})
        written.append(path)

    index = {
        "version": 1,
        "generatedAt": generated_at,
        "language": LANGUAGE,
        "metaFile": meta_file,
        "shards": shards,
        "byId": dict(sorted(by_id.items())),
        "byTitle": dict(sorted(by_title.items())),
        "misses": dict(sorted(misses.items())),
        "stats": stats,
    }
    ip = data_dir / "index.json"
    _atomic_write(ip, index)
    written.append(ip)

    keep = set(p.resolve() for p in written)
    for old in previous:
        if old.resolve() not in keep:
            try:
                old.unlink()
            except OSError:
                pass
    return written


def shard_letter(ent: dict, key: str) -> str:
    title = ""
    if isinstance(ent, dict):
        title = str(ent.get("title") or ent.get("originalTitle") or "")
    if not title:
        title = key.split(":", 1)[-1]
    ch = (title[:1] or "#").lower()
    return ch if re.fullmatch(r"[a-z0-9]", ch) else "other"


def _atomic_write(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(doc, ensure_ascii=False, sort_keys=False, separators=(",", ":")) + "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Secrets / main
# ---------------------------------------------------------------------------

def configured_providers() -> list[dict]:
    out = []
    for p in PROVIDERS:
        url, user, password = os.environ.get(p["url"], "").strip(), os.environ.get(p["user"], "").strip(), os.environ.get(p["pass"], "").strip()
        if url and user and password:
            out.append({"id": p["id"], "url": url, "user": user, "pass": password, "env": p})
        elif url or user or password:
            missing = [k for k, v in ((p["url"], url), (p["user"], user), (p["pass"], password)) if not v]
            log(f"Hinweis: {p['id']} ist unvollständig (fehlt: {', '.join(missing)}) – wird übersprungen.")
    return out


def titles_only_providers(data_dir: Optional[Path], configured: list[dict]) -> list[dict]:
    """Providers without a complete login that still have a titles file."""
    if data_dir is None:
        return []
    have = {p["id"] for p in configured}
    out = []
    for p in PROVIDERS:
        if p["id"] not in have and titles_file_path(data_dir, p["id"]).is_file():
            out.append({"id": p["id"], "url": "", "user": "", "pass": "", "env": p, "titles_only": True})
    return out


def missing_secrets_message(data_dir: Optional[Path] = None) -> Optional[str]:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    provs = configured_providers()
    provs = provs + titles_only_providers(data_dir, provs)
    if not key and not provs:
        return (
            "Es fehlen die GitHub-Secrets. Bitte unter "
            "Settings → Secrets and variables → Actions anlegen:\n"
            "  TMDB_API_KEY\n"
            "  PTV_URL, PTV_USER, PTV_PASS   (PremiumTV, eigener Zugang)\n"
            "  ONE_URL, ONE_USER, ONE_PASS   (One, eigener Zugang; optional, wenn nur ein Anbieter genutzt wird)\n"
            "Danach unter Actions den Workflow „Sync TMDB-Metadaten“ manuell starten."
        )
    if not key:
        return (
            "Secret TMDB_API_KEY fehlt. Bitte unter "
            "Settings → Secrets and variables → Actions anlegen und den Workflow danach erneut starten."
        )
    if not provs:
        return (
            "Es ist kein Anbieter-Zugang vollständig gesetzt. Bitte mindestens "
            "PTV_URL + PTV_USER + PTV_PASS oder ONE_URL + ONE_USER + ONE_PASS "
            "unter Settings → Secrets and variables → Actions eintragen."
        )
    return None


def sport_step(data_dir: Path, configured: list[dict]) -> str:
    """Optional data/sport.json from the One provider's XMLTV. Never raises, never fails the sync."""
    try:
        if os.environ.get("SYNC_SPORT", "1").strip().lower() in ("0", "false", "no", "off"):
            log("Sportplan: abgeschaltet (SYNC_SPORT=0).")
            return "skipped"
        pid = os.environ.get("SYNC_SPORT_PROVIDER", "one").strip().lower() or "one"
        prov = next((p for p in configured if p["id"] == pid and p.get("url") and p.get("user") and p.get("pass")), None)
        budget = max(10.0, env_number("SYNC_SPORT_BUDGET_SEC", sport.DEFAULT_BUDGET_SEC, float))
        days = min(7.0, max(0.5, env_number("SYNC_SPORT_DAYS", sport.DEFAULT_DAYS, float)))
        max_mb = max(1.0, env_number("SYNC_SPORT_MAX_MB", sport.DEFAULT_MAX_MB, float))
        return sport.run_step(data_dir, prov, xtream_base, log, mask_url, USER_AGENT,
                              budget_sec=budget, days=days, max_mb=max_mb)
    except Exception as e:  # noqa: BLE001
        log(f"Sportplan: übersprungen ({type(e).__name__}).")
        return "failed"


def sync(data_dir: Path) -> int:
    msg = missing_secrets_message(data_dir)
    if msg:
        log(msg)
        return 0

    changed_marker = data_dir / ".changed"
    if changed_marker.exists():
        try:
            changed_marker.unlink()
        except OSError:
            pass

    now = datetime.now(timezone.utc)
    generated_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    tmdb = TmdbClient(os.environ["TMDB_API_KEY"].strip(), os.environ.get("TMDB_API_BASE", TMDB_DEFAULT_BASE).strip() or TMDB_DEFAULT_BASE)
    job_started = time.monotonic()

    catalogues: dict[str, dict[str, list]] = {}
    errors = []
    sources: dict[str, dict] = {}
    configured = configured_providers()
    for p in configured + titles_only_providers(data_dir, configured):
        file_lists, file_info, file_status = try_titles_file(data_dir, p["id"], now)
        if file_status == "fresh":
            log(f"Anbieter {p['id']}: Quelle Titeldatei {describe_titles_info(file_info)} – Server wird nicht abgefragt.")
            catalogues[p["id"]] = file_lists
            sources[p["id"]] = {"source": "titles-file", "generatedAt": file_info["generatedAt"],
                                "ageDays": round(file_info["age_days"], 2)}
            continue
        if file_status == "stale":
            log(f"Anbieter {p['id']}: Titeldatei ist zu alt: {describe_titles_info(file_info)}.")
        if p.get("titles_only"):
            if file_lists is not None:
                log(f"Anbieter {p['id']}: kein Zugang hinterlegt – nutze die alte Titeldatei, "
                    "es wird nichts aus dem Cache entfernt.")
                catalogues[p["id"]] = file_lists
                errors.append(f"{p['id']}: nur veraltete Titeldatei")
                sources[p["id"]] = {"source": "titles-file-stale", "generatedAt": file_info["generatedAt"],
                                    "ageDays": round(file_info["age_days"], 2)}
            else:
                log(f"Anbieter {p['id']}: kein Zugang und keine brauchbare Titeldatei – übersprungen.")
            continue

        timeout = list_timeout_sec(p["id"])
        retries = list_retries(p["id"])
        log(
            f"Anbieter {p['id']}: Quelle Server {mask_url(p['url'])} (Benutzer {mask_secret(p['user'])}, "
            f"Listen-Timeout {timeout}s, {retries} Versuche)"
        )
        lists: dict[str, list] = {}
        live_errors: list[str] = []
        for kind, action in (("movie", "get_vod_streams"), ("series", "get_series")):
            try:
                items = fetch_xtream_list(
                    p["url"], p["user"], p["pass"], action, timeout=timeout, retries=retries
                )
                lists[kind] = items
                log(f"    {len(items)} Einträge")
            except Exception as e:
                live_errors.append(str(e))
                log(f"    Fehler: {e}")
                lists[kind] = []
        if live_errors and file_lists is not None:
            log(f"Anbieter {p['id']}: Server-Abruf fehlgeschlagen – nutze stattdessen die alte Titeldatei "
                f"({file_info['generatedAt']}); es wird nichts aus dem Cache entfernt.")
            for kind in ("movie", "series"):
                if not lists.get(kind):
                    lists[kind] = file_lists.get(kind, [])
            sources[p["id"]] = {"source": "titles-file-stale", "generatedAt": file_info["generatedAt"],
                                "ageDays": round(file_info["age_days"], 2)}
        else:
            sources[p["id"]] = {"source": "server"}
        errors.extend(live_errors)
        catalogues[p["id"]] = lists

    sport_step(data_dir, configured)

    total_lists = sum(len(v) for lists in catalogues.values() for v in lists.values())
    if total_lists == 0:
        die("Keine Listen empfangen. Abbruch, vorhandene Cache-Dateien bleiben unverändert.", 1)

    groups = collect_titles(catalogues)

    entries, misses, prev_by_title = load_existing(data_dir)
    fetched = skipped = refreshed = new_hits = new_misses = 0
    elapsed_lists = time.monotonic() - job_started
    budget_min, budget_sec = tmdb_budget_after(elapsed_lists)
    ckpt_every = checkpoint_every()
    started = time.monotonic()
    progress_since_ckpt = 0
    budget_stopped = False
    rematch_max = max(0, int(env_number("SYNC_REMATCH_MAX", DEFAULT_REMATCH_MAX, int)))
    rematched = 0
    open_total = sum(1 for g in groups if needs_tmdb_lookup(g, entries, misses, prev_by_title, now))
    cached = apply_existing_to_groups(groups, entries, prev_by_title)
    adult_n = sum(1 for g in groups if is_adult_group(g))
    log(f"{open_total} Titel in diesem Lauf noch abzufragen, {cached} schon mit TMDB-Daten im Cache.")
    log(f"Erneute Suche nach bereinigten Fehlversuchen: höchstens {rematch_max} in diesem Lauf.")
    if adult_n:
        log(f"{adult_n} Erwachsenenfilme werden nicht bei TMDB gesucht und nicht in der Abdeckung gezählt.")
    cap_min = env_number("SYNC_TIME_BUDGET_MIN", DEFAULT_TIME_BUDGET_MIN, float)
    log(
        f"Zeitbudget: {budget_min:g} Min. für TMDB "
        f"(Listen dauerten {elapsed_lists / 60.0:.1f} Min., "
        f"Rest bis Job-Limit minus Reserve, höchstens {cap_min:g} Min.; "
        f"Zwischenstand alle {ckpt_every} neuen Einträge)."
    )

    def budget_hit() -> bool:
        nonlocal budget_stopped
        if budget_stopped or (time.monotonic() - started) >= budget_sec:
            if not budget_stopped:
                budget_stopped = True
                log(
                    f"Zeitbudget von {budget_min:g} Min. erreicht – keine neuen TMDB-Abfragen mehr. "
                    f"Zwischenstand wird gespeichert ({new_hits} neu, {refreshed} aktualisiert). "
                    "Der nächste Lauf macht bei den restlichen Titeln weiter."
                )
            return True
        return False

    def attach_cached(g: dict, typ: str, tid, miss_key: str, existing_key) -> None:
        """Keep a previously stored match without starting a new TMDB lookup."""
        if existing_key and existing_key in entries:
            g["resolved"] = existing_key
            return
        if tid:
            ekey = entry_key(typ, tid)
            if ekey in entries:
                g["resolved"] = ekey
                misses.pop(miss_key, None)
                return
        miss = misses.get(miss_key)
        if miss and is_fresh(miss, now, MISS_DAYS):
            g["resolved"] = None
            return
        g["resolved"] = None

    def write_checkpoint(partial: bool) -> None:
        by_id, by_title = build_indexes(groups)
        stats_partial = {
            "providers": sorted(catalogues.keys()),
            "titles": len(groups),
            "movies": sum(1 for k in entries if k.startswith("movie:")),
            "series": sum(1 for k in entries if k.startswith("tv:")),
            "unmatched": sum(1 for g in groups if not g.get("resolved")),
            "new": new_hits,
            "refreshed": refreshed,
            "misses": new_misses,
            "skippedFresh": skipped,
            "tmdbCalls": tmdb.calls,
            "partial": partial,
            "budgetStopped": budget_stopped,
            "sources": sources,
            "openTotal": open_total,
            "queried": new_hits + new_misses + refreshed,
            "elapsedSec": int(time.monotonic() - job_started),
            "phase": "tmdb",
        }
        write_status(data_dir, groups, misses, stats_partial, partial=True)
        write_outputs(data_dir, entries, by_id, by_title, misses, generated_at, stats_partial)
        (data_dir / ".changed").write_text("1\n", encoding="utf-8")
        log(f"Zwischenstand geschrieben ({len(entries)} Einträge).")

    def progress_stats(phase: str = "tmdb") -> dict:
        return {
            "providers": sorted(catalogues.keys()),
            "titles": len(groups),
            "movies": sum(1 for k in entries if k.startswith("movie:")),
            "series": sum(1 for k in entries if k.startswith("tv:")),
            "unmatched": sum(1 for g in groups if not g.get("resolved")),
            "new": new_hits,
            "refreshed": refreshed,
            "misses": new_misses,
            "skippedFresh": skipped,
            "tmdbCalls": tmdb.calls,
            "partial": True,
            "budgetStopped": budget_stopped,
            "sources": sources,
            "openTotal": open_total,
            "queried": new_hits + new_misses + refreshed,
            "elapsedSec": int(time.monotonic() - job_started),
            "phase": phase,
        }

    write_status(data_dir, groups, misses, progress_stats("tmdb"), partial=True, force_live=True)

    for g in groups:
        # Time-based live snapshot (default every 4 min), independent of the 500-entry checkpoint.
        interval = max(30.0, float(env_number("SYNC_STATUS_PUSH_SEC", DEFAULT_STATUS_PUSH_SEC, float)))
        if _last_live_push and (time.monotonic() - _last_live_push) >= interval:
            write_status(data_dir, groups, misses, progress_stats("tmdb"), partial=True)
        typ, query, year = g["type"], g["query"], g["year"]
        tid = g["tmdb_id"]
        miss_key = title_key(typ, query, year)
        if is_adult_group(g):
            # Keep any already attached cache hit for the player; never search or add a miss.
            skipped += 1
            continue
        existing_key = entry_key(typ, tid) if tid else None
        if not existing_key:
            # Already resolved in a previous run (by cleaned title, including other providers).
            prev = prev_by_title.get(miss_key)
            if not prev:
                for ref in g["refs"]:
                    prev = prev_by_title.get(title_key(ref["type"], ref["name"], ref["year"]))
                    if prev:
                        break
            if prev and prev in entries:
                existing_key = prev
                if not tid:
                    try:
                        tid = int((entries[prev] or {}).get("tmdbId") or 0) or None
                    except (TypeError, ValueError):
                        tid = None

        entry = entries.get(existing_key) if existing_key else None
        miss = misses.get(miss_key)

        if entry and is_fresh(entry, now, REFRESH_DAYS):
            skipped += 1
            g["resolved"] = existing_key
            continue
        retry_miss = miss_should_retry(miss, g, now) if miss else bool(not tid)
        if not tid and miss and not retry_miss:
            skipped += 1
            g["resolved"] = None
            continue
        if not tid and miss and retry_miss:
            if rematched >= rematch_max:
                skipped += 1
                g["resolved"] = None
                continue
            rematched += 1

        # Stop starting new TMDB lookups once the budget is exhausted, but
        # still attach already cached matches so later pruning stays correct.
        if budget_hit():
            attach_cached(g, typ, tid, miss_key, existing_key)
            continue

        # If we don't have a tmdb id, search (unless we can reuse a stale entry via title — we don't store title on miss only)
        if not tid:
            # Reuse a previously stored id if the stale entry still exists under a known key in misses? misses have tmdbId maybe
            if miss and miss.get("tmdbId"):
                tid = int(miss["tmdbId"])
            else:
                if budget_hit():
                    attach_cached(g, typ, tid, miss_key, existing_key)
                    continue
                try:
                    tid = tmdb.search(typ, query, year)
                except Exception as e:
                    log(f"  Suche fehlgeschlagen für {query!r} ({year}): {e}")
                    continue
                fetched += 1

        if not tid:
            misses[miss_key] = {"tmdbId": None, "query": query, "year": year, "type": typ, "updatedAt": generated_at, "searchRev": SEARCH_REV}
            g["resolved"] = None
            new_misses += 1
            progress_since_ckpt += 1
            if progress_since_ckpt >= ckpt_every:
                progress_since_ckpt = 0
                write_checkpoint(partial=True)
            continue

        ekey = entry_key(typ, tid)
        stale = entries.get(ekey)
        if stale and is_fresh(stale, now, REFRESH_DAYS):
            skipped += 1
            g["resolved"] = ekey
            misses.pop(miss_key, None)
            continue

        if budget_hit():
            attach_cached(g, typ, tid, miss_key, existing_key)
            continue

        try:
            detail = tmdb.details(typ, tid)
        except Exception as e:
            log(f"  Details fehlgeschlagen für {typ} {tid}: {e}")
            if stale:
                g["resolved"] = ekey
            continue
        fetched += 1
        if not detail or not detail.get("tmdbId"):
            misses[miss_key] = {"tmdbId": None, "query": query, "year": year, "type": typ, "updatedAt": generated_at, "searchRev": SEARCH_REV}
            g["resolved"] = None
            new_misses += 1
            progress_since_ckpt += 1
            if progress_since_ckpt >= ckpt_every:
                progress_since_ckpt = 0
                write_checkpoint(partial=True)
            continue

        detail["updatedAt"] = generated_at
        entries[ekey] = detail
        g["resolved"] = ekey
        misses.pop(miss_key, None)
        if stale:
            refreshed += 1
        else:
            new_hits += 1
        progress_since_ckpt += 1
        if progress_since_ckpt >= ckpt_every:
            progress_since_ckpt = 0
            write_checkpoint(partial=True)

    # Build indexes from current catalogue only (drop ids that left the lists)
    by_id, by_title = build_indexes(groups)

    # Drop misses and entries that no longer appear in either provider's list.
    # Only prune when every list arrived; a failed provider list must not wipe
    # that provider's titles from the cache.
    if not errors:
        misses = prune_misses(misses, groups, now)
        entries = prune_unreferenced_entries(entries, by_id, by_title)
    else:
        log("Mindestens eine Liste fehlte, daher wird diesmal nichts aus dem Cache entfernt.")

    stats = {
        "providers": sorted(catalogues.keys()),
        "titles": len(groups),
        "movies": sum(1 for k in entries if k.startswith("movie:")),
        "series": sum(1 for k in entries if k.startswith("tv:")),
        "unmatched": sum(1 for g in groups if not g.get("resolved")),
        "new": new_hits,
        "refreshed": refreshed,
        "misses": new_misses,
        "skippedFresh": skipped,
        "tmdbCalls": tmdb.calls,
        "partial": False,
        "budgetStopped": budget_stopped,
        "sources": sources,
        "openTotal": open_total,
        "queried": new_hits + new_misses + refreshed,
        "elapsedSec": int(time.monotonic() - job_started),
        "phase": "budget_stopped" if budget_stopped else "done",
    }
    log(
        f"Fertig: {stats['movies']} Filme, {stats['series']} Serien, "
        f"{stats['unmatched']} ohne Treffer, {tmdb.calls} TMDB-Aufrufe "
        f"({new_hits} neu, {refreshed} aktualisiert, {skipped} unverändert"
        f"{', Zeitbudget erreicht' if budget_stopped else ''})."
    )

    index_preview = {"byId": by_id, "byTitle": by_title, "misses": misses}
    old_entries, _old_misses, _old_titles = load_existing(data_dir)
    old_idx = {}
    ip = data_dir / "index.json"
    if ip.is_file():
        try:
            old_idx = json.loads(ip.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old_idx = {}
    old_fp = content_fingerprint(old_entries, {k: old_idx.get(k) for k in ("byId", "byTitle", "misses")})
    new_fp = content_fingerprint(entries, index_preview)
    if old_fp == new_fp:
        write_status(data_dir, groups, misses, stats, partial=False)
        log("Keine inhaltlichen Änderungen am Cache – Statusdatei wird trotzdem aktualisiert.")
        return 0

    write_status(data_dir, groups, misses, stats, partial=False)
    write_outputs(data_dir, entries, by_id, by_title, misses, generated_at, stats)
    # marker file for the workflow
    (data_dir / ".changed").write_text("1\n", encoding="utf-8")
    log(f"Dateien geschrieben unter {data_dir}.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="PremiumTV TMDB metadata sync")
    p.add_argument("--data-dir", default=str(REPO_DIR / "data"), help="Output directory (default: repo data/)")
    p.add_argument(
        "--time-budget-min",
        default=None,
        help=f"Stop new TMDB lookups after this many minutes (default: env SYNC_TIME_BUDGET_MIN or {DEFAULT_TIME_BUDGET_MIN})",
    )
    p.add_argument(
        "--checkpoint-every",
        default=None,
        help=f"Write meta/index every N new entries (default: env SYNC_CHECKPOINT_EVERY or {DEFAULT_CHECKPOINT_EVERY})",
    )
    p.add_argument(
        "--sport-only",
        action="store_true",
        help="Only rebuild data/sport.json from the One EPG (no list fetch, no TMDB). Never fails.",
    )
    args = p.parse_args(argv)
    if args.time_budget_min is not None:
        os.environ["SYNC_TIME_BUDGET_MIN"] = str(args.time_budget_min)
    if args.checkpoint_every is not None:
        os.environ["SYNC_CHECKPOINT_EVERY"] = str(args.checkpoint_every)
    data_dir = Path(args.data_dir)
    if args.sport_only:
        data_dir.mkdir(parents=True, exist_ok=True)
        sport_step(data_dir, configured_providers())
        return 0
    try:
        return sync(data_dir)
    except SystemExit:
        raise
    except Exception as e:
        die(f"Unerwarteter Fehler: {type(e).__name__}: {e}", 1)
        return 1


if __name__ == "__main__":
    sys.exit(main())
