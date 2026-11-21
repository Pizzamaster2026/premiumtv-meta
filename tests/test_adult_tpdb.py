"""ThePornDB adult-cover step: offline, mocked HTTP only (no real TPDB, no IPTV servers)."""
import io, json, sys, tempfile, unittest, urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import adult_tpdb as A  # noqa: E402
from export_client import export, bucket  # noqa: E402
import sync_meta  # noqa: E402

FAKE_TOKEN = 'test-token-not-real'
NOW = datetime(2026, 11, 22, 2, 30, tzinfo=timezone.utc)
CDN = 'https://cdn.example.test/'


def scene(i, title, site, date='2024-05-01', performers=(), network=None, poster=True):
    s = {'id': f'uuid-{i}', '_id': i, 'title': title, 'description': 'Beschreibung  ' + title, 'date': date,
         'site': {'name': site, 'network': {'name': network} if network else None},
         'performers': list(performers)}
    if poster:
        s.update({'poster': CDN + f'p{i}.jpg', 'posters': {'large': CDN + f'pl{i}.jpg'},
                  'background': {'large': CDN + f'b{i}.jpg', 'full': CDN + f'bf{i}.jpg'}})
    return s


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class FakeApi:
    """Routes (path, param) -> JSON or an int HTTP error code."""
    def __init__(self, routes):
        self.routes, self.requests, self.auth = routes, [], []

    def __call__(self, req, timeout=None):
        u = urlparse(req.full_url)
        qs = parse_qs(u.query)
        term = (qs.get('parse') or qs.get('q') or [''])[0]
        self.requests.append((u.path, term))
        self.auth.append(req.get_header('Authorization'))
        r = self.routes.get((u.path, term), {'data': []})
        if isinstance(r, int):
            raise urllib.error.HTTPError(req.full_url, r, 'err', {}, None)
        return Resp(json.dumps(r).encode())


class Clock:
    def __init__(self): self.t, self.slept = 0.0, []
    def __call__(self): return self.t
    def sleep(self, s): self.slept.append(s); self.t += s


def client(api, max_requests=100, budget=3600):
    c = Clock()
    return A.TpdbClient(FAKE_TOKEN, base='https://api.example.test', min_interval=1.0, max_requests=max_requests,
                        budget_sec=budget, opener=api, sleep=c.sleep, clock=c), c


def write_titles(d, items):
    (d / 'adult-titles.json').write_text(json.dumps({'version': 1, 'items': items}))


class AdultTpdbTest(unittest.TestCase):
    def test_query_parsing(self):
        aq = A.adult_query('(XXX-) Tushy - Fortunate Buns')
        self.assertEqual((aq['q'], aq['left'], aq['right'], aq['key']), ('Tushy - Fortunate Buns', 'Tushy', 'Fortunate Buns', 'tushy fortunate buns'))
        aq = A.adult_query('(XXX-) fakehuboriginals.24.08.08.ada.lapiedra')
        self.assertEqual((aq['left'], aq['date']), ('fakehuboriginals', '2024-08-08'))
        self.assertTrue(A.junk_query(A.adult_query('(XXX-) PARTY_SEX 58e91c6191fa.720')))
        self.assertTrue(A.junk_query(A.adult_query('(XXX-) PASSIONHD 732521499164')))
        self.assertFalse(A.junk_query(A.adult_query('(XXX-) RODNEY BLAST 21')))

    def test_scene_match_with_site_and_performer_lookup(self):
        aq = A.adult_query('(XXX-) TushyRaw - Cute And Tiny Blonde Gapes Like a Pro')
        hit = scene(1, 'Cute and Tiny Blonde Gapes Like a Pro', 'Tushy Raw',
                    performers=[{'name': 'Anna A', 'parent': {'name': 'Anna Alpha', 'face': CDN + 'f1.jpg'}},
                                {'name': 'Bella B'}])
        api = FakeApi({('/scenes', aq['q']): {'data': [hit, scene(2, 'Totally different', 'Other')]},
                       ('/performers', 'Bella B'): {'data': [{'id': 'p9', 'name': 'Bella B', 'image': CDN + 'bb.jpg'}]}})
        c, clock = client(api)
        rec = A.lookup_title(c, aq, 'T')
        A.fill_performer_images(c, rec, {}, NOW, 'T')
        self.assertEqual(rec['status'], 'match')
        self.assertEqual(rec['source'], 'scene')
        self.assertEqual(rec['poster'], CDN + 'pl1.jpg')
        self.assertEqual(rec['background'], CDN + 'b1.jpg')
        self.assertEqual(rec['year'], '2024')
        self.assertEqual(rec['overview'], 'Beschreibung Cute and Tiny Blonde Gapes Like a Pro')
        self.assertEqual(rec['performers'], [{'name': 'Anna Alpha', 'image': CDN + 'f1.jpg'},
                                             {'name': 'Bella B', 'image': CDN + 'bb.jpg'}])
        self.assertEqual(api.requests, [('/scenes', aq['q']), ('/performers', 'Bella B')])
        self.assertTrue(all(a == 'Bearer ' + FAKE_TOKEN for a in api.auth))
        self.assertGreaterEqual(min(clock.slept), 0.99)  # ~1 request/second

    def test_wrong_site_or_title_is_rejected_then_movie_fallback(self):
        aq = A.adult_query('(XXX-) Young Girls with Older Men 3 - Tiny Cute Blonde Teen Britt')
        api = FakeApi({('/scenes', aq['q']): {'data': [scene(1, 'Tiny Cute Blonde Teen Britt', 'Unrelated Site')]},
                       ('/movies', 'Young Girls with Older Men 3'): {'data': [
                           scene(5, 'Young Girls With Older Men 3', 'Studio', date='2019-02-02')]}})
        c, _ = client(api)
        rec = A.lookup_title(c, aq, 'T')
        self.assertEqual((rec['status'], rec['source'], rec['tpdbId'], rec['year']), ('match', 'movie', 'uuid-5', '2019'))

    def test_similar_but_not_same_title_is_miss(self):
        aq = A.adult_query('(XXX-) Tushy - Fortunate Buns')
        api = FakeApi({('/scenes', aq['q']): {'data': [scene(1, 'Fortunate Sons', 'Tushy')]}})
        c, _ = client(api)
        self.assertEqual(A.lookup_title(c, aq, 'T')['status'], 'miss')

    def test_ambiguous_tie_is_miss(self):
        aq = A.adult_query('(XXX-) Office Affairs')
        api = FakeApi({('/movies', 'Office Affairs'): {'data': [scene(1, 'Office Affairs', 'A'), scene(2, 'Office Affairs', 'B')]}})
        c, _ = client(api)
        self.assertEqual(A.lookup_title(c, aq, 'T')['status'], 'miss')

    def test_year_conflict_and_no_image_rejected(self):
        aq = A.adult_query('(XXX-) Office Affairs (2012)')
        api = FakeApi({('/movies', 'Office Affairs'): {'data': [scene(1, 'Office Affairs', 'A', date='2020-01-01')]},
                       ('/scenes', 'Office Affairs'): {'data': [scene(2, 'Office Affairs', 'A', date='2012-01-01', poster=False)]}})
        c, _ = client(api)
        self.assertEqual(A.lookup_title(c, aq, 'T')['status'], 'miss')

    def test_release_name_needs_same_date(self):
        aq = A.adult_query('(XXX-) fakehuboriginals.24.08.08.ada.lapiedra')
        good = scene(1, 'Ada Lapiedra Gets X', 'Fakehub Originals', date='2024-08-08')
        api = FakeApi({('/scenes', aq['q']): {'data': [good]}})
        c, _ = client(api)
        self.assertEqual(A.lookup_title(c, aq, 'T')['tpdbId'], 'uuid-1')
        good['date'] = '2024-08-09'
        c, _ = client(api)
        self.assertEqual(A.lookup_title(c, aq, 'T')['status'], 'miss')

    def test_performer_lookup_only_unambiguous(self):
        two = {'data': [{'id': 'a', 'name': 'Mia', 'image': CDN + 'a.jpg'}, {'id': 'b', 'name': 'Mia', 'image': CDN + 'b.jpg'}]}
        self.assertIsNone(A.pick_performer('Mia', two))
        one = {'data': [{'id': 'a', 'name': 'Mia Alpha', 'image': CDN + 'a.jpg'}, {'id': 'b', 'name': 'Mia', 'aliases': [], 'face': CDN + 'b.jpg'}]}
        self.assertEqual(A.pick_performer('Mia', one)['id'], 'b')
        self.assertIsNone(A.pick_performer('Mia', {'data': [{'id': 'a', 'name': 'Mia Alpha'}]}))

    def test_run_step_cache_cap_and_no_requery(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {'premiumtv:movie:1': '(XXX-) Tushy - Fortunate Buns',
                             'premiumtv:movie:2': '(XXX-) Unknown Thing',
                             'premiumtv:movie:3': '(XXX-) Tushy - Fortunate Buns',  # duplicate query
                             'premiumtv:movie:4': '(XXX-) PARTY_SEX 58e91c6191fa.720'})  # junk, never asked
            api = FakeApi({('/scenes', 'Tushy - Fortunate Buns'): {'data': [scene(1, 'Fortunate Buns', 'Tushy')]}})
            c, _ = client(api)
            st = A.run_step(d, client=c, now=NOW)
            self.assertEqual((st['matched'], st['missed'], st['failed']), (1, 1, 0))
            self.assertEqual(len(api.requests), 3)  # 1 scene hit + 2 for the unknown title
            cache = json.loads((d / 'adult-tpdb.json').read_text())
            self.assertEqual(cache['titles']['tushy fortunate buns']['status'], 'match')
            self.assertEqual(cache['titles']['unknown thing']['status'], 'miss')
            # second night: nothing to ask
            api2 = FakeApi({}); c2, _ = client(api2)
            st2 = A.run_step(d, client=c2, now=NOW + timedelta(days=1))
            self.assertEqual((st2['pending'], len(api2.requests)), (0, 0))
            # miss retried after 30 days
            api3 = FakeApi({}); c3, _ = client(api3)
            self.assertEqual(A.run_step(d, client=c3, now=NOW + timedelta(days=31))['pending'], 1)
            st_doc = json.loads((d / 'status.json').read_text())
            # unique queries: fortunate buns (match), unknown thing (miss), junk (noMatch); 3 titles, 1 cover
            ad = st_doc['adult']
            self.assertEqual((ad['withCover'], ad['noMatch'], ad['pending'], ad['titles'], ad['items']), (1, 2, 0, 3, 4))
            self.assertNotIn('tushy', json.dumps(ad))

    def test_coverage_missing_files_are_zero(self):
        with tempfile.TemporaryDirectory() as tdir:
            d = Path(tdir)
            cov = A.coverage(d)
            self.assertEqual((cov['titles'], cov['withCover'], cov['pending'], cov['noMatch'], cov['items']), (0, 0, 0, 0, 0))
            self.assertEqual(cov['coveragePct'], 0.0)
            self.assertIn('updatedAt', cov)

    def test_cap_stops_and_keeps_done_work(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {f'one:movie:{i}': f'(XXX-) Title Number {chr(97+i)}x' for i in range(5)})
            api = FakeApi({}); c, _ = client(api, max_requests=3)
            st = A.run_step(d, client=c, now=NOW)
            self.assertEqual(len(api.requests), 3)
            self.assertEqual(st['stopped'], 'cap')
            self.assertEqual(len(json.loads((d / 'adult-tpdb.json').read_text())['titles']), 1)

    def test_errors_never_raise_and_are_not_cached(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {'one:movie:1': '(XXX-) Some Title', 'one:movie:2': '(XXX-) Other Title'})
            api = FakeApi({('/movies', 'Some Title'): 500, ('/scenes', 'Some Title'): 500,
                           ('/movies', 'Other Title'): 500, ('/scenes', 'Other Title'): 500})
            c, _ = client(api)
            st = A.run_step(d, client=c, now=NOW)
            self.assertEqual(st['failed'], 2)
            self.assertEqual(json.loads((d / 'adult-tpdb.json').read_text())['titles'], {})
            # 401 stops the run at once
            api = FakeApi({('/movies', 'Some Title'): 401}); c, _ = client(api)
            st = A.run_step(d, client=c, now=NOW)
            self.assertIn('401', st['stopped'])
            self.assertEqual(api.requests[-1], ('/movies', 'Some Title'))  # newest id first, then stop at 401
            self.assertNotIn('some title', json.loads((d / 'adult-tpdb.json').read_text())['titles'])
            # broken titles file / missing token
            (d / 'adult-titles.json').write_text('{broken')
            self.assertEqual(A.run_step(d, token='x')['asked'], 0)
            write_titles(d, {'one:movie:1': '(XXX-) Some Title'})
            self.assertEqual(A.run_step(d, token='')['asked'], 0)

    def test_429_waits_retry_after(self):
        calls = {'n': 0}
        api = FakeApi({})
        def opener(req, timeout=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.HTTPError(req.full_url, 429, 'slow', {'Retry-After': '7'}, None)
            return api(req, timeout)
        c, clock = client(opener)
        self.assertEqual(c.get('/scenes', {'parse': 'x'}), {'data': []})
        self.assertIn(7.0, clock.slept)

    def test_token_never_written(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_titles(d, {'one:movie:1': '(XXX-) Tushy - Fortunate Buns'})
            api = FakeApi({('/scenes', 'Tushy - Fortunate Buns'): {'data': [scene(1, 'Fortunate Buns', 'Tushy')]}})
            c, _ = client(api)
            A.run_step(d, client=c, now=NOW)
            for p in d.iterdir():
                self.assertNotIn(FAKE_TOKEN, p.read_text())

    def test_sync_writes_adult_titles_with_same_detection(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            groups = sync_meta.collect_titles({'premiumtv': {'movie': [
                {'stream_id': 11, 'name': '(XXX-) Tushy - Fortunate Buns'},
                {'stream_id': 12, 'name': 'xXx (2002)'},          # Vin Diesel, not adult
                {'stream_id': 13, 'name': 'Der Pate (1972)'}], 'series': []}})
            sync_meta.adult_titles_step(d, groups, 'T1')
            doc = json.loads((d / 'adult-titles.json').read_text())
            self.assertEqual(doc['items'], {'premiumtv:movie:11': '(XXX-) Tushy - Fortunate Buns'})
            sync_meta.adult_titles_step(d, groups, 'T2')  # unchanged items: file untouched
            self.assertEqual(json.loads((d / 'adult-titles.json').read_text())['generatedAt'], 'T1')

    def test_export_adult_map_separate_and_schema(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / 'meta.json').write_text(json.dumps({'entries': {'movie:2': {'tmdbId': 2, 'type': 'movie', 'title': 'Dune', 'year': '2021'}}}))
            (d / 'index.json').write_text(json.dumps({'metaFile': 'data/meta.json', 'byId': {'one:movie:1': 'movie:2'}, 'byTitle': {}}))
            m0 = export(d)
            self.assertNotIn('adult', m0['maps']); self.assertEqual(m0['counts']['adult'], 0)
            write_titles(d, {'premiumtv:movie:11': '(XXX-) Tushy - Fortunate Buns', 'premiumtv:movie:12': '(XXX-) Unknown'})
            api = FakeApi({('/scenes', 'Tushy - Fortunate Buns'): {'data': [scene(1, 'Fortunate Buns', 'Tushy', performers=[{'name': 'X Y', 'image': CDN + 'xy.jpg'}])]}})
            c, _ = client(api)
            A.run_step(d, client=c, now=NOW)
            m = export(d)
            self.assertEqual(m['counts']['adult'], 1)
            self.assertEqual(m['maps']['adult']['count'], 64)
            b = bucket('premiumtv:movie:11', 64)
            chunk = json.loads((d / 'client-v1' / m['maps']['adult']['chunks'][b]['file']).read_text())
            e = chunk['items']['premiumtv:movie:11']
            self.assertEqual(sorted(e), ['adult', 'backdrop', 'overview', 'performers', 'poster', 'q', 'site', 'src', 'title', 'year'])
            self.assertEqual((e['adult'], e['q'], e['src'], e['site'], e['poster']), (True, 'tushy fortunate buns', 'scene', 'Tushy', CDN + 'pl1.jpg'))
            self.assertEqual(e['performers'], [{'name': 'X Y', 'photo': CDN + 'xy.jpg'}])
            # adult data never leaks into the normal maps
            for kind in ('id', 'title', 'meta'):
                for v in m['maps'][kind]['chunks'].values():
                    self.assertNotIn(b'Fortunate', (d / 'client-v1' / v['file']).read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=1)
