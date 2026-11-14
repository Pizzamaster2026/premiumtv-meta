"""Local mock Xtream list API + mock TMDB. No upstream network."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


PTV_USER, PTV_PASS = "ptv-test", "ptv-secret"
ONE_USER, ONE_PASS = "one-test", "one-secret"
TMDB_KEY = "mock-tmdb-key-not-real"

PTV_VOD = [
    {"stream_id": 1001, "name": "DE - Der Pate (1972) 4K", "year": "1972", "tmdb_id": 238},
    {"stream_id": 1002, "name": "| DE | The Matrix - 1999", "releaseDate": "1999-03-31"},
    {"stream_id": 1003, "name": "Unknown indie film XYZQQ 1998"},
    {"stream_id": 1001, "name": "DE - Der Pate (1972) 4K", "year": "1972", "tmdb_id": 238},  # duplicate id
]
PTV_SERIES = [
    {"series_id": 2001, "name": "Breaking Bad (US) (2008)", "tmdb": "1396"},
]
ONE_VOD = [
    {"stream_id": 501, "name": "Der Pate (1972)", "tmdb_id": 238},  # same film, other provider id
    {"stream_id": 502, "name": "Inception [DE]", "year": "2010"},
]
ONE_SERIES = [
    {"series_id": 801, "name": "The Office", "year": "2005"},
]

MOVIES = {
    238: {
        "id": 238, "title": "Der Pate", "original_title": "The Godfather",
        "release_date": "1972-03-24", "vote_average": 8.7, "vote_count": 21000,
        "overview": "Don Vito Corleone …", "runtime": 175,
        "poster_path": "/pate.jpg", "backdrop_path": "/pate-bg.jpg",
        "genres": [{"id": 80, "name": "Krimi"}, {"id": 18, "name": "Drama"}],
        "credits": {"cast": [
            {"id": 3084, "name": "Marlon Brando", "character": "Don Vito Corleone", "profile_path": "/brando.jpg", "order": 0},
            {"id": 1158, "name": "Al Pacino", "character": "Michael Corleone", "profile_path": "/pacino.jpg", "order": 1},
        ]},
    },
    603: {
        "id": 603, "title": "Matrix", "original_title": "The Matrix",
        "release_date": "1999-03-31", "vote_average": 8.2, "vote_count": 25000,
        "overview": "Neo …", "runtime": 136,
        "poster_path": "/matrix.jpg", "backdrop_path": "/matrix-bg.jpg",
        "genres": [{"id": 28, "name": "Action"}, {"id": 878, "name": "Science Fiction"}],
        "credits": {"cast": [
            {"id": 6384, "name": "Keanu Reeves", "character": "Neo", "profile_path": "/keanu.jpg", "order": 0},
        ]},
    },
    27205: {
        "id": 27205, "title": "Inception", "original_title": "Inception",
        "release_date": "2010-07-15", "vote_average": 8.4, "vote_count": 36000,
        "overview": "Cobb …", "runtime": 148,
        "poster_path": "/inception.jpg", "backdrop_path": "/inception-bg.jpg",
        "genres": [{"id": 28, "name": "Action"}, {"id": 878, "name": "Science Fiction"}],
        "credits": {"cast": [
            {"id": 6193, "name": "Leonardo DiCaprio", "character": "Cobb", "profile_path": "/leo.jpg", "order": 0},
        ]},
    },
}
TV = {
    1396: {
        "id": 1396, "name": "Breaking Bad", "original_name": "Breaking Bad",
        "first_air_date": "2008-01-20", "vote_average": 8.9, "vote_count": 14000,
        "overview": "Walter White …", "episode_run_time": [47],
        "poster_path": "/bb.jpg", "backdrop_path": "/bb-bg.jpg",
        "genres": [{"id": 18, "name": "Drama"}, {"id": 80, "name": "Krimi"}],
        "credits": {"cast": [
            {"id": 17419, "name": "Bryan Cranston", "character": "Walter White", "profile_path": "/bryan.jpg", "order": 0},
        ]},
    },
    2316: {
        "id": 2316, "name": "The Office", "original_name": "The Office",
        "first_air_date": "2005-03-24", "vote_average": 8.5, "vote_count": 4000,
        "overview": "Dunder Mifflin …", "episode_run_time": [22],
        "poster_path": "/office.jpg", "backdrop_path": "/office-bg.jpg",
        "genres": [{"id": 35, "name": "Komödie"}],
        "credits": {"cast": [
            {"id": 17647, "name": "Steve Carell", "character": "Michael Scott", "profile_path": "/steve.jpg", "order": 0},
        ]},
    },
}

SEARCH_MOVIE = {
    "der pate": [238],
    "the matrix": [603],
    "inception": [27205],
}
SEARCH_TV = {
    "breaking bad": [1396],
    "the office": [2316],
}


class Stats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.xtream = []
        self.tmdb = []
        self.forbidden = []


STATS = Stats()


def _json(handler, code, obj):
    raw = json.dumps(obj).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path

        # Block Xtream *stream* and *info* endpoints only.
        # Do not treat TMDB's "/3/movie/{id}" as a stream URL.
        q_raw = u.query or ""
        xtream_stream = (
            path.startswith("/live/") or path.startswith("/movie/") or path.startswith("/series/")
            or path.startswith("/timeshift/") or path.startswith("/ptv/movie/") or path.startswith("/one/movie/")
            or path.startswith("/ptv/live/") or path.startswith("/one/live/")
            or path.startswith("/ptv/series/") or path.startswith("/one/series/")
            or "action=get_vod_info" in q_raw or "action=get_series_info" in q_raw
            or "action=get_live_streams" in q_raw
        )
        if xtream_stream:
            STATS.forbidden.append(self.path)
            _json(self, 404, {"error": "forbidden-in-mock"})
            return

        if path.endswith("/player_api.php") or path.endswith("/player_api.php/"):
            self._xtream(path, q)
            return
        if path.startswith("/3/") or path.startswith("/tmdb/3/"):
            self._tmdb(path, q)
            return
        if path == "/stats":
            _json(self, 200, {"xtream": STATS.xtream, "tmdb": STATS.tmdb, "forbidden": STATS.forbidden})
            return
        if path == "/stats/reset":
            STATS.reset()
            _json(self, 200, {"ok": True})
            return
        _json(self, 404, {"error": "not found", "path": path})

    def _xtream(self, path, q):
        user, pw, action = q.get("username", ""), q.get("password", ""), q.get("action", "")
        provider = "one" if "/one/" in path or path.startswith("/one") else "ptv"
        STATS.xtream.append({"provider": provider, "action": action, "user": user})
        want = (ONE_USER, ONE_PASS) if provider == "one" else (PTV_USER, PTV_PASS)
        if (user, pw) != want:
            _json(self, 200, {"user_info": {"auth": 0}})
            return
        if action == "get_vod_streams":
            _json(self, 200, ONE_VOD if provider == "one" else PTV_VOD)
            return
        if action == "get_series":
            _json(self, 200, ONE_SERIES if provider == "one" else PTV_SERIES)
            return
        STATS.forbidden.append(self.path)
        _json(self, 400, {"error": f"unexpected action {action}"})

    def _tmdb(self, path, q):
        key = q.get("api_key", "")
        # normalize /tmdb/3/ or /3/
        ep = path
        for prefix in ("/tmdb/3/", "/3/"):
            if ep.startswith(prefix):
                ep = ep[len(prefix):]
                break
        STATS.tmdb.append({"ep": ep, "q": q.get("query", ""), "year": q.get("year") or q.get("first_air_date_year", "")})
        if key != TMDB_KEY:
            _json(self, 401, {"status_code": 7})
            return
        if ep.startswith("search/movie"):
            ids = SEARCH_MOVIE.get((q.get("query") or "").lower(), [])
            results = [_search_row(MOVIES[i], "movie") for i in ids if i in MOVIES]
            _json(self, 200, {"results": results})
            return
        if ep.startswith("search/tv"):
            ids = SEARCH_TV.get((q.get("query") or "").lower(), [])
            results = [_search_row(TV[i], "tv") for i in ids if i in TV]
            _json(self, 200, {"results": results})
            return
        if ep.startswith("movie/"):
            mid = int(ep.split("/")[1].split("?")[0])
            if mid in MOVIES:
                _json(self, 200, MOVIES[mid])
            else:
                _json(self, 404, {"status_code": 34})
            return
        if ep.startswith("tv/"):
            mid = int(ep.split("/")[1].split("?")[0])
            if mid in TV:
                _json(self, 200, TV[mid])
            else:
                _json(self, 404, {"status_code": 34})
            return
        _json(self, 404, {"error": ep})


def _search_row(doc, typ):
    if typ == "tv":
        return {"id": doc["id"], "name": doc["name"], "original_name": doc.get("original_name"),
                "first_air_date": doc.get("first_air_date"), "overview": doc.get("overview"),
                "poster_path": doc.get("poster_path"), "vote_average": doc["vote_average"],
                "vote_count": doc["vote_count"]}
    return {"id": doc["id"], "title": doc["title"], "original_title": doc.get("original_title"),
            "release_date": doc.get("release_date"), "overview": doc.get("overview"),
            "poster_path": doc.get("poster_path"), "vote_average": doc["vote_average"],
            "vote_count": doc["vote_count"]}


def start(port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return srv
