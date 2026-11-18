"""Customer-cache regression tests, entirely offline."""
import sys, json, tempfile, unittest, hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from export_client import export, bucket, norm
from sync_meta import _pick_search, cached_identity_ok, existing_entry_key

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

if __name__=='__main__':unittest.main()
