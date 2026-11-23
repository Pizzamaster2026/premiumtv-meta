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
                    if 'sha256' in v:
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
                    'cast': movie_cast[:8],
                    'castExtra': movie_cast[8:],
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
            self.assertIn('adam sandler', san['sandler']['l'])
            self.assertNotIn('r', san.get('adam sandler') or {})
            ode = json.loads((d / 'client-v1' / m['maps']['people']['chunks']['ode']['file']).read_text())['items']
            self.assertIn('bob odenkirk', ode['odenkirk']['l'])
            self.assertEqual(people['cameo star']['i'], 99)
            self.assertEqual(people['cameo star']['m'], [55])
            self.assertIn(1396, people['bob odenkirk']['s'])
            self.assertEqual(people['dennis dugan']['d'], [55])
            # Prefix buckets: adam -> ada, sandler -> san
            self.assertEqual(people_prefix('adam sandler'), 'ada')
            self.assertEqual(people_prefix('sandler'), 'san')
            self.assertIn('ada', m['maps']['people']['chunks'])
            self.assertIn('san', m['maps']['people']['chunks'])
            for b, v in m['maps']['people']['chunks'].items():
                self.assertEqual(set(v.keys()), {'file'})
                self.assertTrue(v['file'].startswith('people-'))
            self.assertEqual(m['maps']['people']['index'], 'people-index.json')
            idx = json.loads((d / 'client-v1' / 'people-index.json').read_text())
            self.assertIn('sha256', idx['chunks']['ada'])
            man_bytes = (d / 'client-v1' / 'manifest.json').stat().st_size
            self.assertLessEqual(man_bytes, 2 * 1024 * 1024)

    def test_people_listing_is_compact_enough_for_thousands_of_prefixes(self):
        # Regression for "Manifest too large" at 512 KiB: ~6700 prefix3 buckets
        # with sha256/bytes were ~1118 KiB; 0.2.20 only needs {file}.
        from export_client import encode
        prefixes = [f'{a}{b}{c}' for a in 'abcdefghijklmnopqrstuvwxyz'
                    for b in 'abcdefghijklmnopqrstuvwxyz' for c in 'abcdefghijklmnopqrstuvwxyz'][:6720]
        compact = {p: {'file': f'people-{p}-0123456789abcdef.json'} for p in prefixes}
        full = {p: {'file': compact[p]['file'], 'bytes': 12345, 'sha256': 'ab'*32} for p in prefixes}
        man = encode({'version':1,'maps':{
            'id':{'count':256,'chunks':{str(i):{'file':f'id-{i}-0123456789abcdef.json','bytes':1,'sha256':'cd'*32} for i in range(256)}},
            'title':{'count':256,'chunks':{str(i):{'file':f'title-{i}-0123456789abcdef.json','bytes':1,'sha256':'cd'*32} for i in range(256)}},
            'meta':{'count':1024,'chunks':{str(i):{'file':f'meta-{i}-0123456789abcdef.json','bytes':1,'sha256':'cd'*32} for i in range(1024)}},
            'people':{'count':len(compact),'scheme':'prefix3','index':'people-index.json','chunks':compact},
        }})
        bloated = encode({'version':1,'maps':{'people':{'chunks':full}}})
        self.assertGreater(len(bloated), 512*1024)
        self.assertLessEqual(len(man), 2*1024*1024)
        # {file} is what 0.2.20 reads: MetaCache.chunk(..., info.file)
        self.assertIn(b'"file":"people-aaa-0123456789abcdef.json"', man)
        people_map = json.loads(man)['maps']['people']['chunks']
        self.assertEqual(set(people_map['aaa'].keys()), {'file'})
        self.assertNotIn('sha256', people_map['aaa'])

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
