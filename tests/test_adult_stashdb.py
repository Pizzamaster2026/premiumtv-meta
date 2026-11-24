"""StashDB adult-cover fallback: offline, mocked HTTP only (no live StashDB, no TPDB, no IPTV)."""
import http.client, io, json, sys, tempfile, unittest, urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import adult_tpdb as A  # noqa: E402
import adult_stashdb as S  # noqa: E402
from export_client import export, bucket  # noqa: E402

NOW = datetime(2026, 11, 22, 2, 30, tzinfo=timezone.utc)
CDN = 'https://cdn.example.test/'
FAKE_STASH = 'stash-test-token-not-real'
FAKE_TPDB = 'tpdb-test-token-not-real'


def scene(i, title, studio, date='2024-05-01', parent=None, performers=(), portrait=True, extra=None):
    w, h = (800, 1200) if portrait else (1280, 720)
    img = {'url': CDN + f's{i}.jpg', 'width': w, 'height': h}
    body = {
        'id': f'stash-uuid-{i}',
        'title': title,
        'details': 'Beschreibung ' + title,
        'date': date,
        'release_date': date,
        'studio': {'name': studio, 'parent': {'name': parent} if parent else None},
        'images': [img],
        'performers': [{'performer': {'name': n, 'images': [{'url': CDN + f'p{n}.jpg', 'width': 200, 'height': 300}]}}
                       for n in performers],
    }
    if extra:
        body['images'].append(extra)
    return body


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class FakeGraphql:
    """Routes search term -> {data: {searchScenes: {scenes: [...]}}} or an HTTP status int."""

    def __init__(self, routes):
        self.routes, self.requests, self.auth = routes, [], []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode()) if req.data else {}
        term = (body.get('variables') or {}).get('term', '')
        self.requests.append(term)
        # urllib stores headers with various capitalizations; compare values only.
        headers = {k.lower(): v for k, v in req.header_items()}
        self.auth.append(headers.get('apikey', ''))
        r = self.routes.get(term, {'data': {'searchScenes': {'scenes': []}}})
        if isinstance(r, int):
            raise urllib.error.HTTPError(req.full_url, r, 'err', {}, None)
        return Resp(json.dumps(r).encode())


class Clock:
    def __init__(self): self.t, self.slept = 0.0, []
    def __call__(self): return self.t
    def sleep(self, s): self.slept.append(s); self.t += s


def stash_client(api, max_requests=100, budget=3600):
    c = Clock()
    return S.StashdbClient(FAKE_STASH, base='https://stash.example.test/graphql', min_interval=1.0,
                           max_requests=max_requests, budget_sec=budget, opener=api, sleep=c.sleep, clock=c), c


def tpdb_scene(i, title, site, date='2024-05-01'):
    return {'id': f'uuid-{i}', 'title': title, 'date': date, 'site': {'name': site},
            'poster': CDN + f't{i}.jpg', 'posters': {'large': CDN + f't{i}.jpg'}}


class FakeTpdb:
    def __init__(self, routes):
        self.routes, self.requests, self.auth = routes, [], []

    def __call__(self, req, timeout=None):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(req.full_url)
        term = (parse_qs(u.query).get('parse') or [''])[0]
        self.requests.append((u.path, term))
        self.auth.append(req.get_header('Authorization'))
        r = self.routes.get((u.path, term), {'data': []})
        if isinstance(r, int):
            raise urllib.error.HTTPError(req.full_url, r, 'err', {}, None)
        return Resp(json.dumps(r).encode())


def tpdb_client(api, max_requests=100, budget=3600):
    c = Clock()
    return A.TpdbClient(FAKE_TPDB, base='https://api.example.test', min_interval=1.0,
                        max_requests=max_requests, budget_sec=budget, opener=api, sleep=c.sleep, clock=c), c


def write_titles(d, items):
    (d / 'adult-titles.json').write_text(json.dumps({'version': 1, 'items': items}))


def wrap(scenes):
    return {'data': {'searchScenes': {'scenes': scenes}}}


class StashdbFallbackTest(unittest.TestCase):
    def test_confident_scene_match_and_export(self):
        aq = A.adult_query('(XXX-) TushyRaw - Cute And Tiny Blonde Gapes Like a Pro')
        api = FakeGraphql({aq['q']: wrap([scene(1, 'Cute and Tiny Blonde Gapes Like a Pro', 'Tushy Raw',
                                                performers=('Anna Alpha',),
                                                extra={'url': CDN + 'bg1.jpg', 'width': 1920, 'height': 1080})])})
        c, clock = stash_client(api)
        rec = S.lookup_title(c, aq, 'T')
        self.assertEqual(rec['status'], 'match')
        self.assertEqual(rec['source'], 'stashdb-scene')
        self.assertEqual(rec['stashId'], 'stash-uuid-1')
        self.assertEqual(rec['tpdbId'], '')
        self.assertEqual(rec['poster'], CDN + 's1.jpg')
        self.assertEqual(rec['background'], CDN + 'bg1.jpg')
        self.assertEqual(rec['year'], '2024')
        self.assertEqual(rec['performers'][0]['name'], 'Anna Alpha')
        self.assertTrue(all(a == FAKE_STASH for a in api.auth))
        # first request has no throttle wait; nothing to assert on an empty list
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {'premiumtv:movie:11': '(XXX-) TushyRaw - Cute And Tiny Blonde Gapes Like a Pro',
                             'premiumtv:movie:12': '(XXX-) Unknown Film'})
            cache = {'version': 1, 'titles': {
                aq['key']: {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z'},
                'unknown film': {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z'},
            }, 'performers': {}}
            (d / 'adult-tpdb.json').write_text(json.dumps(cache))
            (d / 'meta.json').write_text(json.dumps({'entries': {}}))
            (d / 'index.json').write_text(json.dumps({'metaFile': 'data/meta.json', 'byId': {}, 'byTitle': {}}))
            c2, _ = stash_client(api)
            st = A.run_step(d, token='', stashdb_client=c2, now=NOW)
            self.assertEqual((st['stashdbMatched'], st['stashdbMissed']), (1, 1))
            cov = A.coverage(d)
            self.assertEqual((cov['withCover'], cov['noMatch'], cov['pending']), (1, 1, 0))
            m = export(d)
            self.assertEqual(m['counts']['adult'], 1)
            b = bucket('premiumtv:movie:11', 64)
            e = json.loads((d / 'client-v1' / m['maps']['adult']['chunks'][b]['file']).read_text())['items']['premiumtv:movie:11']
            self.assertEqual((e['src'], e['poster'], e['adult']), ('scene', CDN + 's1.jpg', True))

    def test_tie_and_weak_title_are_misses(self):
        aq = A.adult_query('(XXX-) Office Affairs')
        api = FakeGraphql({aq['q']: wrap([scene(1, 'Office Affairs', 'A'), scene(2, 'Office Affairs', 'B')])})
        c, _ = stash_client(api)
        self.assertEqual(S.lookup_title(c, aq, 'T')['status'], 'miss')
        aq = A.adult_query('(XXX-) Tushy - Fortunate Buns')
        api = FakeGraphql({aq['q']: wrap([scene(1, 'Fortunate Sons', 'Tushy')])})
        c, _ = stash_client(api)
        self.assertEqual(S.lookup_title(c, aq, 'T')['status'], 'miss')

    def test_only_due_misses_are_asked_never_junk_or_pending_or_match(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {
                'premiumtv:movie:1': '(XXX-) Tushy - Fortunate Buns',          # TPDB match, must not be asked
                'premiumtv:movie:2': '(XXX-) Unknown Thing',                    # miss, due
                'premiumtv:movie:3': '(XXX-) Fresh Pending Title',              # not in cache = pending TPDB
                'premiumtv:movie:4': '(XXX-) PARTY_SEX 58e91c6191fa.720',       # junk
                'premiumtv:movie:5': '(XXX-) Already Tried Miss',               # miss, stashdb checked yesterday
            })
            cache = {'version': 1, 'titles': {
                'tushy fortunate buns': {'status': 'match', 'checkedAt': 'T', 'source': 'scene',
                                         'poster': CDN + 'x.jpg', 'tpdbId': '1'},
                'unknown thing': {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z'},
                'already tried miss': {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z',
                                       'fallback': {'stashdb': {'status': 'miss', 'checkedAt': '2026-11-21T00:00:00Z'}}},
            }, 'performers': {}}
            (d / 'adult-tpdb.json').write_text(json.dumps(cache))
            api = FakeGraphql({})
            c, _ = stash_client(api)
            st = A.run_step(d, token='', stashdb_client=c, now=NOW)
            self.assertEqual(api.requests, ['Unknown Thing'])
            self.assertEqual(st['stashdbPending'], 1)
            self.assertEqual(json.loads((d / 'adult-tpdb.json').read_text())['titles']['tushy fortunate buns']['source'], 'scene')
            # next day: the new miss is not retried; after 30 days it is
            api2 = FakeGraphql({})
            c2, _ = stash_client(api2)
            st2 = A.run_step(d, token='', stashdb_client=c2, now=NOW + timedelta(days=1))
            self.assertEqual(st2['stashdbPending'], 0)
            self.assertEqual(api2.requests, [])
            api3 = FakeGraphql({})
            c3, _ = stash_client(api3)
            self.assertEqual(A.run_step(d, token='', stashdb_client=c3, now=NOW + timedelta(days=31))['stashdbPending'], 2)

    def test_tpdb_runs_first_stashdb_gets_leftover_budget(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {
                'one:movie:3': '(XXX-) Tushy - Fortunate Buns',
                'one:movie:2': '(XXX-) Unknown Thing',
                'one:movie:1': '(XXX-) Another Miss Title',
            })
            # Pre-seed two misses so StashDB has work, plus one fresh TPDB pending
            cache = {'version': 1, 'titles': {
                'unknown thing': {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z'},
                'another miss title': {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z'},
            }, 'performers': {}}
            (d / 'adult-tpdb.json').write_text(json.dumps(cache))
            tapi = FakeTpdb({('/scenes', 'Tushy - Fortunate Buns'): {'data': [tpdb_scene(1, 'Fortunate Buns', 'Tushy')]}})
            # Shared budget: 3 requests. TPDB will spend 1 on the pending match (scene hit).
            # leftover 2 go to StashDB.
            tc, _ = tpdb_client(tapi, max_requests=3)
            sapi = FakeGraphql({})
            sc, _ = stash_client(sapi, max_requests=100)
            # Injected stashdb_client keeps its own cap, so to test leftover we construct
            # after TPDB via remaining_from in a dedicated sub-case below.
            # Here: run with a tiny shared-style stash client after confirming order in logs is TPDB first.
            st = A.run_step(d, client=tc, stashdb_client=sc, now=NOW)
            self.assertEqual(st['matched'], 1)
            self.assertEqual(st['stashdbAsked'], 2)
            self.assertEqual(tapi.requests[0], ('/scenes', 'Tushy - Fortunate Buns'))
            self.assertEqual(sorted(sapi.requests), ['Another Miss Title', 'Unknown Thing'])
            # leftover helper
            left, remain = S.remaining_from(tc, 3, 3600)
            self.assertEqual(left, 2)

    def test_missing_secret_skips_stashdb(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {'one:movie:1': '(XXX-) Unknown Thing'})
            (d / 'adult-tpdb.json').write_text(json.dumps({
                'version': 1, 'titles': {'unknown thing': {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z'}},
                'performers': {},
            }))
            st = A.run_step(d, token='', stashdb_token='', now=NOW)
            self.assertEqual(st['stashdbAsked'], 0)
            # Isolate main() from a live key that may be set on the developer box.
            import os
            saved = {k: os.environ.pop(k) for k in ('STASHDB_API_KEY', 'TPDB_API_KEY') if k in os.environ}
            try:
                self.assertEqual(A.main(['--data-dir', str(d)]), 0)
            finally:
                os.environ.update(saved)

    def test_token_never_written(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {'one:movie:1': '(XXX-) Tushy - Fortunate Buns'})
            (d / 'adult-tpdb.json').write_text(json.dumps({
                'version': 1,
                'titles': {'tushy fortunate buns': {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z'}},
                'performers': {},
            }))
            api = FakeGraphql({'Tushy - Fortunate Buns': wrap([scene(1, 'Fortunate Buns', 'Tushy')])})
            c, _ = stash_client(api)
            A.run_step(d, token='', stashdb_client=c, now=NOW)
            for p in d.iterdir():
                if p.is_file():
                    self.assertNotIn(FAKE_STASH, p.read_text())

    def test_unexpected_error_saves_done_stashdb_work(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {
                'one:movie:2': '(XXX-) Tushy - Fortunate Buns',
                'one:movie:1': '(XXX-) Other Studio - Unknown Film Title',
            })
            (d / 'adult-tpdb.json').write_text(json.dumps({
                'version': 1,
                'titles': {
                    'tushy fortunate buns': {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z'},
                    'other studio unknown film title': {'status': 'miss', 'checkedAt': '2026-11-01T00:00:00Z'},
                },
                'performers': {},
            }))
            api = FakeGraphql({'Tushy - Fortunate Buns': wrap([scene(1, 'Fortunate Buns', 'Tushy')])})
            calls = {'n': 0}

            def opener(req, timeout=None):
                calls['n'] += 1
                if calls['n'] > 1:
                    raise http.client.IncompleteRead(b'partial', 20)
                return api(req, timeout)

            c, _ = stash_client(opener)
            st = A.run_step(d, token='', stashdb_client=c, now=NOW)
            self.assertTrue(st['stopped'].startswith('error IncompleteRead'), st)
            self.assertEqual(st['stashdbMatched'], 1)
            cache = json.loads((d / 'adult-tpdb.json').read_text())
            self.assertEqual(cache['titles']['tushy fortunate buns']['source'], 'stashdb-scene')
            self.assertEqual(cache['titles']['other studio unknown film title']['status'], 'miss')
            self.assertNotIn('stashdb', (cache['titles']['other studio unknown film title'].get('fallback') or {}))

    def test_existing_tpdb_tests_still_see_plain_run_step(self):
        # Injecting only a TPDB client must not read STASHDB_API_KEY from the environment.
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {'one:movie:1': '(XXX-) Unknown Thing'})
            tapi = FakeTpdb({})
            tc, _ = tpdb_client(tapi)
            st = A.run_step(d, client=tc, now=NOW)
            self.assertEqual(st['missed'], 1)
            self.assertEqual(st['stashdbAsked'], 0)
            rec = json.loads((d / 'adult-tpdb.json').read_text())['titles']['unknown thing']
            self.assertEqual(rec['status'], 'miss')
            self.assertNotIn('fallback', rec)


if __name__ == '__main__':
    unittest.main(verbosity=1)
