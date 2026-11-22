"""Customer-cache regression tests, entirely offline."""
import sys, json, tempfile, unittest, hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from export_client import export, bucket, norm, people_prefix, people_groups, compact
from sync_meta import _pick_search, cached_identity_ok, existing_entry_key, entry_current, CREDITS_REV
from datetime import datetime, timezone, timedelta

class CustomerCacheTest(unittest.TestCase):
    def test_search_rejects_wrong_and_ambiguous(self):
        rows={'results':[{'id':1,'title':'Wrong','release_date':'2020-01-01'}]}
        self.assertIsNone(_pick_search(rows,'Right',False,'2020'))
        rows={'results':[{'id':1,'title':'Dune','release_date':'1984-01-01'},{'id':2,'title':'Dune','release_date':'2021-01-01'}]}
        self.assertIsNone(_pick_search(rows,'Dune'))
        self.assertEqual(_pick_search(rows,'Dune',year='2021')['id'],2)
        self.assertIsNone(_pick_search(rows,'Dune',year='2000'))

    def test_legacy_wrong_match_not_reused(self):
        g={'type':'movie','query':'Dune','year':'2021','refs':[]}
        e={'tmdbId':1,'type':'movie','title':'Dune','year':'1984'}
        self.assertFalse(cached_identity_ok(g,e))
        self.assertIsNone(existing_entry_key(g,{'movie:1':e},{'movie|dune|2021':'movie:1'}))
        e['year']='2021';self.assertTrue(cached_identity_ok(g,e))
        e['title']='Wrong';self.assertFalse(cached_identity_ok(g,e))

    def test_export_is_compact_verified_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            d=Path(temp)
            e={'tmdbId':2,'type':'movie','title':'Dune','originalTitle':'Dune','year':'2021','voteAverage':8.2,'voteCount':250,
               'genres':['Science Fiction'],'username':'DO_NOT_EXPORT','password':'DO_NOT_EXPORT','stream_url':'DO_NOT_EXPORT'}
            (d/'meta.json').write_text(json.dumps({'entries':{'movie:2':e}}))
            (d/'index.json').write_text(json.dumps({'metaFile':'data/meta.json','byId':{'one:movie:1':'movie:2'},
              'byTitle':{'movie|dune|2021':'movie:2','movie|dune|1984':'movie:2','movie|wrong|2021':'movie:2'}}))
            m=export(d); self.assertEqual(m['counts']['rejectedAliases'],2)
            self.assertEqual(m['counts']['titles'],1)
            for kind,mp in m['maps'].items():
                for v in mp['chunks'].values():
                    raw=(d/'client-v1'/v['file']).read_bytes()
                    self.assertNotIn(b'DO_NOT_EXPORT',raw)
                    self.assertEqual(hashlib.sha256(raw).hexdigest(),v['sha256'])
            self.assertEqual(export(d),m)
            self.assertEqual(norm('Die Eiskönigin: 2'),'die eiskonigin 2')

    def test_people_index_uses_full_cast_and_last_name_stub(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            movie_cast = [{'id': 1, 'name': 'Adam Sandler', 'profilePath': '/adam.jpg'}] + [
                {'id': 10 + i, 'name': f'Extra Actor {i:02d}'} for i in range(1, 16)
            ] + [{'id': 99, 'name': 'Cameo Star'}]
            entries = {
                'movie:55': {
                    'tmdbId': 55, 'type': 'movie', 'title': 'Happy Film', 'originalTitle': 'Happy Film',
                    'year': '2010', 'voteAverage': 7.0, 'voteCount': 100, 'genres': ['Komödie'],
                    'cast': movie_cast,
                    'directors': [{'id': 7, 'name': 'Dennis Dugan'}],
                    'creditsRev': 1,
                },
                'tv:1396': {
                    'tmdbId': 1396, 'type': 'series', 'title': 'Breaking Bad', 'originalTitle': 'Breaking Bad',
                    'year': '2008', 'voteAverage': 8.9, 'voteCount': 100, 'genres': ['Drama'],
                    'cast': [
                        {'id': 17419, 'name': 'Bryan Cranston', 'profilePath': '/bryan.jpg'},
                        {'id': 14319, 'name': 'Bob Odenkirk', 'profilePath': '/saul.jpg'},
                    ],
                    'creditsRev': 1,
                },
            }
            (d / 'meta.json').write_text(json.dumps({'entries': entries}))
            (d / 'index.json').write_text(json.dumps({
                'metaFile': 'data/meta.json',
                'generatedAt': '2026-11-22T00:00:00Z',
                'byId': {'premiumtv:movie:1': 'movie:55', 'premiumtv:series:2': 'tv:1396'},
                'byTitle': {
                    'movie|happy film|2010': 'movie:55',
                    'tv|breaking bad|2008': 'tv:1396',
                },
            }))
            m = export(d)
            self.assertEqual(m['maps']['people']['scheme'], 'prefix3')
            self.assertGreaterEqual(m['counts']['people'], 3)
            # Compact client meta still only keeps the first 8 names.
            meta_chunks = m['maps']['meta']['chunks']
            compact_cast_names = set()
            for v in meta_chunks.values():
                items = json.loads((d / 'client-v1' / v['file']).read_text())['items']
                for e in items.values():
                    for c in e.get('cast') or []:
                        compact_cast_names.add(c.get('name'))
            self.assertNotIn('Cameo Star', compact_cast_names)
            # People index is built from RAW entries, so the 17th billed name is present.
            people = {}
            for b, v in m['maps']['people']['chunks'].items():
                chunk = json.loads((d / 'client-v1' / v['file']).read_text())['items']
                for k, rec in chunk.items():
                    if k not in people or (people[k].get('r') and not rec.get('r')):
                        people[k] = rec
            sandler = people['adam sandler']
            self.assertEqual(sandler['n'], 'Adam Sandler')
            self.assertEqual(sandler['i'], 1)
            self.assertEqual(sandler['p'], '/adam.jpg')
            self.assertEqual(sandler['m'], [55])
            san = json.loads((d / 'client-v1' / m['maps']['people']['chunks']['san']['file']).read_text())['items']
            self.assertEqual(san['adam sandler']['r'], 'adam sandler')
            ode = json.loads((d / 'client-v1' / m['maps']['people']['chunks']['ode']['file']).read_text())['items']
            self.assertEqual(ode['bob odenkirk']['r'], 'bob odenkirk')
            self.assertEqual(people['cameo star']['i'], 99)
            self.assertEqual(people['cameo star']['m'], [55])
            self.assertIn(1396, people['bob odenkirk']['s'])
            self.assertEqual(people['dennis dugan']['d'], [55])
            # Prefix buckets: adam -> ada, sandler -> san
            self.assertEqual(people_prefix('adam sandler'), 'ada')
            self.assertEqual(people_prefix('sandler'), 'san')
            self.assertIn('ada', m['maps']['people']['chunks'])
            self.assertIn('san', m['maps']['people']['chunks'])

    def test_stale_credits_need_refresh(self):
        now = datetime.now(timezone.utc)
        fresh = {'updatedAt': now.isoformat(), 'creditsRev': CREDITS_REV}
        old_rev = {'updatedAt': now.isoformat(), 'creditsRev': 0}
        missing = {'updatedAt': now.isoformat()}
        expired = {'updatedAt': (now - timedelta(days=40)).isoformat(), 'creditsRev': CREDITS_REV}
        self.assertTrue(entry_current(fresh, now))
        self.assertFalse(entry_current(old_rev, now))
        self.assertFalse(entry_current(missing, now))
        self.assertFalse(entry_current(expired, now))
        self.assertFalse(entry_current(None, now))

if __name__=='__main__':unittest.main()
