#!/usr/bin/env python3
"""StashDB fallback for adult covers after ThePornDB has recorded a miss.

Called from adult_tpdb.run_step with whatever is left of the shared
~1 req/s, 1500-request, 28-minute budget. Never asked for junk names,
never overwrites a TPDB match, never logs the API key or titles.

StashDB's public GraphQL API is scene-only (no movie search), so a hit
is always source=stashdb-scene. A miss is stored as
titles[key].fallback.stashdb = {status, checkedAt} and retried after
FALLBACK_RETRY_DAYS.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import adult_tpdb as T

API = "https://stashdb.org/graphql"
SEARCH_LIMIT = 8
FALLBACK_RETRY_DAYS = 30
HTTP_TIMEOUT = 20
USER_AGENT = T.USER_AGENT

# searchScenes is the current field; searchScene is the older alias.
# We query the current one and only request fields needed for scoring/covers.
SEARCH_QUERY = """
query SearchScenes($term: String!, $perPage: Int) {
  searchScenes(term: $term, per_page: $perPage) {
    scenes {
      id
      title
      details
      date
      release_date
      studio { name parent { name } }
      images { url width height }
      performers { performer { name images { url width height } } }
    }
  }
}
"""


def log(msg: str) -> None:
    print(f"[StashDB] {msg}", flush=True)


class StashdbClient:
    """Throttled GraphQL client. Same cap/budget/error rules as TpdbClient."""

    def __init__(self, token: str, base: str = API, min_interval: float = T.DEFAULT_MIN_INTERVAL,
                 max_requests: int = T.DEFAULT_MAX_REQUESTS, budget_sec: float = T.DEFAULT_BUDGET_MIN * 60,
                 opener: Optional[Callable[..., Any]] = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._token = token
        self.base = (base or API).rstrip("/")
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

    def remaining_sec(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def _throttle(self) -> None:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def graphql(self, query: str, variables: dict) -> Any:
        """Parsed JSON body, or None on a non-fatal error. Raises T.StopRun to end the run."""
        if self.calls >= self.max_requests:
            raise T.StopRun("cap")
        if self._clock() >= self._deadline:
            raise T.StopRun("budget")
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        for attempt in range(3):
            self._throttle()
            self.calls += 1
            req = urllib.request.Request(self.base, data=payload, method="POST", headers={
                "ApiKey": self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            })
            try:
                with self._open(req, timeout=HTTP_TIMEOUT) as resp:
                    raw = resp.read()
                self._consecutive = 0
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    self.errors += 1
                    self._consecutive += 1
                    return None
                if not isinstance(body, dict):
                    return None
                # A GraphQL error with no data is not a miss; retry later.
                if body.get("errors") and not isinstance(body.get("data"), dict):
                    self.errors += 1
                    self._consecutive += 1
                    if self._consecutive >= 5:
                        raise T.StopRun("5 Fehler in Folge (zuletzt GraphQL)")
                    return None
                return body
            except urllib.error.HTTPError as e:
                code = e.code
                if code in (401, 403):
                    raise T.StopRun(f"HTTP {code} (Schlüssel ungültig oder gesperrt?)")
                if code == 429 and attempt < 2 and self.calls < self.max_requests:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait = float(ra) if ra else 30.0
                    except ValueError:
                        wait = 30.0
                    self._sleep(min(max(wait, 1.0), 60.0))
                    continue
                err = f"HTTP {code}"
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                err = type(e).__name__
            self.errors += 1
            self._consecutive += 1
            if self._consecutive >= 5:
                raise T.StopRun(f"5 Fehler in Folge (zuletzt {err})")
            return None
        return None

    def search_scenes(self, term: str) -> Any:
        return self.graphql(SEARCH_QUERY, {"term": term, "perPage": SEARCH_LIMIT})


def _img_url(obj: Any) -> str:
    return T.safe_url(obj.get("url") if isinstance(obj, dict) else "")


def pick_images(images: Any) -> tuple[str, str]:
    """Portrait (or largest) https URL as poster; a different landscape as background."""
    scored = []
    for im in images or []:
        if not isinstance(im, dict):
            continue
        u = _img_url(im)
        if not u:
            continue
        try:
            w, h = int(im.get("width") or 0), int(im.get("height") or 0)
        except (TypeError, ValueError):
            w, h = 0, 0
        scored.append((h >= w and h > 0, w * h, u))
    if not scored:
        return "", ""
    portraits = sorted((x for x in scored if x[0]), key=lambda x: -x[1])
    landscapes = sorted((x for x in scored if not x[0]), key=lambda x: -x[1])
    poster = (portraits or landscapes)[0][2]
    background = ""
    if landscapes and landscapes[0][2] != poster:
        background = landscapes[0][2]
    return poster, background


def adapt_scene(scene: dict) -> Optional[dict]:
    """Map a StashDB scene onto the TPDB-shaped dict pick_confident/build_match expect."""
    if not isinstance(scene, dict):
        return None
    poster, background = pick_images(scene.get("images"))
    if not poster:
        return None
    studio = scene.get("studio") if isinstance(scene.get("studio"), dict) else {}
    parent = studio.get("parent") if isinstance(studio.get("parent"), dict) else {}
    site = {"name": studio.get("name") or ""}
    if parent.get("name"):
        site["parent"] = {"name": parent["name"]}
    performers = []
    for row in scene.get("performers") or []:
        if not isinstance(row, dict):
            continue
        p = row.get("performer") if isinstance(row.get("performer"), dict) else row
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        face, _ = pick_images(p.get("images"))
        performers.append({"name": name, "parent": {"name": name, "face": face}, "image": face, "face": face})
    date = str(scene.get("release_date") or scene.get("date") or "")
    hit = {
        "id": str(scene.get("id") or "")[:64],
        "title": scene.get("title") or "",
        "description": scene.get("details") or "",
        "date": date,
        "site": site,
        "performers": performers,
        "poster": poster,
        "posters": {"large": poster},
    }
    if background:
        hit["background"] = {"large": background}
    return hit


def scenes_from(body: Any) -> list[dict]:
    data = body.get("data") if isinstance(body, dict) else None
    block = data.get("searchScenes") if isinstance(data, dict) else None
    rows = block.get("scenes") if isinstance(block, dict) else None
    # Older searchScene alias returns a bare list.
    if rows is None and isinstance(data, dict) and isinstance(data.get("searchScene"), list):
        rows = data["searchScene"]
    if not isinstance(rows, list):
        return []
    out = []
    for s in rows:
        hit = adapt_scene(s)
        if hit:
            out.append(hit)
    return out


def lookup_title(client: StashdbClient, aq: dict, now_iso: str) -> Optional[dict]:
    """Match record, miss record (status=miss only; caller writes fallback), or None on error."""
    body = client.search_scenes(aq["q"])
    if body is None:
        return None
    rows = scenes_from(body)
    hit = T.pick_confident(aq, {"data": rows}, "scene")
    if not hit:
        return {"status": "miss", "checkedAt": now_iso}
    rec = T.build_match(hit, "scene", T.score_hit(aq, hit, "scene"), now_iso)
    rec["source"] = "stashdb-scene"
    rec["stashId"] = str(hit.get("id") or "")[:64]
    rec["tpdbId"] = ""
    return rec


def stashdb_due(rec: Any, now: datetime) -> bool:
    """True when this cache row is a TPDB miss that StashDB should (re)try."""
    if not isinstance(rec, dict) or rec.get("status") != "miss":
        return False
    fb = rec.get("fallback") if isinstance(rec.get("fallback"), dict) else {}
    st = fb.get("stashdb") if isinstance(fb.get("stashdb"), dict) else None
    if not st:
        return True
    return T.is_expired(st.get("checkedAt"), now, FALLBACK_RETRY_DAYS)


def pending_stashdb(titles: dict, cache_titles: dict, now: datetime) -> list[dict]:
    """Unique non-junk queries whose cache row is a due TPDB miss. Newest stream id first."""
    best: dict[str, tuple] = {}
    for key, name in titles.items():
        aq = T.adult_query(name)
        if T.junk_query(aq):
            continue
        rec = cache_titles.get(aq["key"])
        if not stashdb_due(rec, now):
            continue
        try:
            sid = int(key.rsplit(":", 1)[-1])
        except ValueError:
            sid = 0
        if aq["key"] not in best or sid > best[aq["key"]][0]:
            best[aq["key"]] = (sid, aq)
    return [aq for _, aq in sorted(best.values(), key=lambda x: -x[0])]


def record_stashdb_miss(rec: dict, now_iso: str) -> dict:
    """Keep the TPDB miss, add fallback.stashdb.checkedAt (does not change rec['checkedAt'])."""
    out = dict(rec)
    fb = dict(out["fallback"]) if isinstance(out.get("fallback"), dict) else {}
    fb["stashdb"] = {"status": "miss", "checkedAt": now_iso}
    out["fallback"] = fb
    return out


def remaining_from(tpdb: Optional[T.TpdbClient], default_max: int, default_budget_sec: float) -> tuple[int, float]:
    if tpdb is None:
        return default_max, default_budget_sec
    left = max(0, tpdb.left())
    try:
        remain = max(0.0, tpdb._deadline - tpdb._clock())
    except Exception:
        remain = default_budget_sec
    return left, remain


def apply_result(cache_titles: dict, key: str, rec: dict, now_iso: str) -> str:
    """Write a lookup result into the cache. Returns 'match', 'miss', or 'failed' (unused)."""
    if rec.get("status") == "match":
        cache_titles[key] = rec
        return "match"
    prev = cache_titles.get(key) if isinstance(cache_titles.get(key), dict) else {"status": "miss", "checkedAt": now_iso}
    cache_titles[key] = record_stashdb_miss(prev, now_iso)
    return "miss"


def maybe_client(token: Optional[str], max_requests: int, budget_sec: float,
                 min_interval: float, base: str = "") -> Optional[StashdbClient]:
    tok = (token or "").strip()
    if not tok or max_requests <= 0 or budget_sec <= 0:
        return None
    return StashdbClient(
        tok,
        base=(base or os.environ.get("STASHDB_API_BASE", "") or API).strip() or API,
        min_interval=min_interval,
        max_requests=max_requests,
        budget_sec=budget_sec,
    )


def run_fallback(cache: dict, titles: dict, client: StashdbClient, now: datetime, now_iso: str,
                 stats: dict) -> None:
    """Ask due misses. Raises T.StopRun when the shared cap/budget is spent."""
    todo = pending_stashdb(titles, cache["titles"], now)
    stats["stashdbPending"] = len(todo)
    if not todo:
        log("Keine offenen ThePornDB-Fehlversuche für StashDB.")
        return
    log(f"{len(todo)} ThePornDB-Fehlversuche offen, höchstens {client.max_requests} Anfragen für StashDB.")
    for aq in todo:
        if client.left() <= 0:
            raise T.StopRun("cap")
        rec = lookup_title(client, aq, now_iso)
        stats["stashdbAsked"] += 1
        if rec is None:
            stats["stashdbFailed"] += 1
            continue
        kind = apply_result(cache["titles"], aq["key"], rec, now_iso)
        if kind == "match":
            stats["stashdbMatched"] += 1
        else:
            stats["stashdbMissed"] += 1
