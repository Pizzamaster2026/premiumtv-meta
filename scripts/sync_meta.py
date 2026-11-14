#!/usr/bin/env python3
"""Build a public TMDB metadata cache for PremiumTV / One catalogue titles.

Fetches only Xtream *list* endpoints (get_vod_streams, get_series) and then
TMDB details for new or stale titles. Never requests video streams or
get_vod_info / get_series_info. Incremental: keeps existing entries and only
contacts TMDB for missing titles or entries older than REFRESH_DAYS.

Credentials come from environment / GitHub Actions secrets and are never logged.
"""
from __future__ import annotations

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

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from title import clean, extract_tmdb_id, extract_year, title_key  # noqa: E402

REFRESH_DAYS = 30
MISS_DAYS = 14
TMDB_DEFAULT_BASE = "https://api.themoviedb.org/3"
LANGUAGE = "de-DE"
CAST_MAX = 12
OVERVIEW_MAX = 2000
SHARD_BYTES = 8 * 1024 * 1024
RATE_MAX = 40
RATE_WINDOW_S = 10.0
HTTP_TIMEOUT_LIST = 120
HTTP_TIMEOUT_TMDB = 25
HTTP_RETRIES = 4
# Some Xtream panels ignore or stall on unknown clients; use a browser-like UA.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# Leave headroom under the workflow's timeout-minutes: 120 so a partial
# result can be written and committed before the job is killed.
DEFAULT_TIME_BUDGET_MIN = 100
DEFAULT_CHECKPOINT_EVERY = 500

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


def http_get_json(url: str, timeout: int, retries: int = HTTP_RETRIES) -> Any:
    """GET JSON. The URL must already be safe to log only via mask_url()."""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
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


def fetch_xtream_list(base: str, user: str, password: str, action: str) -> list:
    if action not in ALLOWED_XTREAM_ACTIONS:
        raise ValueError(f"refusing Xtream action {action!r} (only list endpoints are allowed)")
    url = xtream_list_url(base, user, password, action)
    log(f"  Liste {action} von {mask_url(url)} …")
    try:
        data = http_get_json(url, timeout=HTTP_TIMEOUT_LIST)
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
    key = "series_id" if kind == "series" else "stream_id"
    return str(item.get(key) or "").strip()


def collect_titles(catalogues: dict[str, dict[str, list]]) -> list[dict]:
    """Deduplicate titles across providers. Each group maps many stream ids to one TMDB lookup."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for provider, lists in catalogues.items():
        for kind, items in lists.items():
            typ = "series" if kind == "series" else "movie"
            for item in items:
                if not isinstance(item, dict):
                    continue
                sid = item_id(item, kind)
                name = item_name(item)
                if not sid or not name:
                    continue
                year = item_year(item, typ)
                tmdb_id = extract_tmdb_id(item)
                c = clean(name, year)
                if tmdb_id:
                    gid = f"{typ}:id:{tmdb_id}"
                elif c["q"] and len(c["q"]) >= 2:
                    gid = f"{typ}:title:{title_key(typ, name, year)}"
                else:
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
    return [groups[k] for k in order]


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
        tv = typ == "series"
        path = "/search/tv" if tv else "/search/movie"
        params = {"query": query, "language": LANGUAGE, "include_adult": "false", "page": "1"}
        if year:
            params["first_air_date_year" if tv else "year"] = year
        data = self.get(path, params)
        hit = _pick_search(data, query)
        if not hit and year:
            params.pop("first_air_date_year", None)
            params.pop("year", None)
            data = self.get(path, params)
            hit = _pick_search(data, query)
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


def _pick_search(data: Any, query: str) -> Optional[dict]:
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


def missing_secrets_message() -> Optional[str]:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    provs = configured_providers()
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


def sync(data_dir: Path) -> int:
    msg = missing_secrets_message()
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

    catalogues: dict[str, dict[str, list]] = {}
    errors = []
    for p in configured_providers():
        log(f"Anbieter {p['id']}: {mask_url(p['url'])} (Benutzer {mask_secret(p['user'])})")
        lists: dict[str, list] = {}
        for kind, action in (("movie", "get_vod_streams"), ("series", "get_series")):
            try:
                items = fetch_xtream_list(p["url"], p["user"], p["pass"], action)
                lists[kind] = items
                log(f"    {len(items)} Einträge")
            except Exception as e:
                errors.append(str(e))
                log(f"    Fehler: {e}")
                lists[kind] = []
        catalogues[p["id"]] = lists

    total_lists = sum(len(v) for lists in catalogues.values() for v in lists.values())
    if total_lists == 0:
        die("Keine Listen empfangen. Abbruch, vorhandene Cache-Dateien bleiben unverändert.", 1)

    groups = collect_titles(catalogues)
    log(f"{len(groups)} eindeutige Titel über alle Anbieter.")

    entries, misses, prev_by_title = load_existing(data_dir)
    fetched = skipped = refreshed = new_hits = new_misses = 0
    budget_min, budget_sec = time_budget_seconds()
    ckpt_every = checkpoint_every()
    started = time.monotonic()
    progress_since_ckpt = 0
    budget_stopped = False
    log(f"Zeitbudget: {budget_min:g} Min. (Zwischenstand alle {ckpt_every} neuen Einträge).")

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
        }
        write_outputs(data_dir, entries, by_id, by_title, misses, generated_at, stats_partial)
        (data_dir / ".changed").write_text("1\n", encoding="utf-8")
        log(f"Zwischenstand geschrieben ({len(entries)} Einträge).")

    for g in groups:
        typ, query, year = g["type"], g["query"], g["year"]
        tid = g["tmdb_id"]
        miss_key = title_key(typ, query, year)
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
        if not tid and miss and is_fresh(miss, now, MISS_DAYS):
            skipped += 1
            g["resolved"] = None
            continue

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
            misses[miss_key] = {"tmdbId": None, "query": query, "year": year, "type": typ, "updatedAt": generated_at}
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
            misses[miss_key] = {"tmdbId": None, "query": query, "year": year, "type": typ, "updatedAt": generated_at}
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
        log("Keine inhaltlichen Änderungen – Dateien bleiben unverändert, kein Commit nötig.")
        return 0

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
    args = p.parse_args(argv)
    if args.time_budget_min is not None:
        os.environ["SYNC_TIME_BUDGET_MIN"] = str(args.time_budget_min)
    if args.checkpoint_every is not None:
        os.environ["SYNC_CHECKPOINT_EVERY"] = str(args.checkpoint_every)
    data_dir = Path(args.data_dir)
    try:
        return sync(data_dir)
    except SystemExit:
        raise
    except Exception as e:
        die(f"Unerwarteter Fehler: {type(e).__name__}: {e}", 1)
        return 1


if __name__ == "__main__":
    sys.exit(main())
