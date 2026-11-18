#!/usr/bin/env python3
"""Offline client cache exporter. No secrets, network, TMDB or provider calls.

Three hash-partitioned maps: provider IDs, exact titles, compact TMDB entries.
Content-addressed chunks + last-written manifest prevent mixed generations.
Old chunks are retained for 35 days for offline clients/CDN manifests.
"""
from __future__ import annotations
import argparse, hashlib, json, math, re, time, unicodedata
from pathlib import Path
from title import names_equivalent

def norm(s):
    s = str(s or '').lower().replace('ß', 'ss')
    s = ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()

def bucket(key, count):
    h = 5381
    for c in key:
        h = ((h * 33) ^ ord(c)) & 0xffffffff
    return str(h % count)

def number(x, maximum):
    try:
        n = float(x)
        return n if math.isfinite(n) and 0 <= n <= maximum else 0
    except (TypeError, ValueError):
        return 0

def image_path(s):
    return s if isinstance(s, str) and re.fullmatch(r'/[\w.-]+\.(?:jpg|jpeg|png|webp)', s) else ''

def compact(e):
    return dict(id=int(number(e.get('tmdbId'), 1000000000)),
                type='series' if e.get('type') == 'series' else 'movie',
                title=str(e.get('title') or '')[:200], original=str(e.get('originalTitle') or '')[:200],
                year=str(e.get('year') or '')[:4],
                genres=[str(g)[:50] for g in (e.get('genres') or []) if isinstance(g, str)][:5],
                va=number(e.get('voteAverage'), 10), vc=int(number(e.get('voteCount'), 100000000)),
                overview=str(e.get('overview') or '')[:1200], poster=image_path(e.get('posterPath')),
                runtime=number(e.get('runtime'), 2000),
                cast=[{'name': str(c.get('name') or '')[:80], 'id': int(number(c.get('id'), 1000000000)),
                       'photo': image_path(c.get('profilePath'))}
                      for c in (e.get('cast') or [])[:8] if isinstance(c, dict)])

def encode(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()

def export(data_dir: Path):
    index = json.loads((data_dir/'index.json').read_text())
    entries = {}
    files = list((index.get('shards') or {}).values()) or [index.get('metaFile')]
    for f in files:
        if not f or not re.fullmatch(r'data/meta(?:-[a-z0-9]+)?\.json', f):
            raise ValueError('Unexpected input chunk path')
        entries.update(json.loads((data_dir/Path(f).name).read_text()).get('entries', {}))
    metas = {k: compact(e) for k,e in entries.items() if isinstance(e, dict)}
    metas = {k:e for k,e in metas.items() if e['id'] and e['title']}
    ids = {k:v for k,v in index.get('byId', {}).items()
           if re.fullmatch(r'(premiumtv|one):(movie|series):[0-9]+', k) and v in metas}
    titles = {}
    rejected = 0
    for k, v in index.get('byTitle', {}).items():
        parts = k.split('|')
        if len(parts) != 3 or v not in metas:
            continue
        typ, q, year = parts
        e = metas[v]
        # Re-audit legacy aliases. Do not publish an old first-result guess.
        if typ != e['type'] or not any(names_equivalent(q, t) for t in (e['title'], e['original'])):
            rejected += 1
            continue
        if year and (not year.isdigit() or not e['year'].isdigit() or abs(int(year)-int(e['year'])) > 1):
            rejected += 1
            continue
        for t in (q, e['title'], e['original']):
            n = norm(t)
            if n:
                key = typ + '|' + n
                titles.setdefault(key, {})[v] = e['year']
    # No bare-ID trust: clients must compare the current line title/year/explicit
    # TMDB ID before using any entry reached through the provider-ID map.
    maps = {'id': (ids, 256), 'title': (titles, 256), 'meta': (metas, 1024)}
    out = data_dir/'client-v1'; out.mkdir(exist_ok=True)
    manifest = {'version':1, 'generatedAt':index.get('generatedAt'), 'maps':{},
                'counts': {'ids':len(ids), 'titles':len(titles), 'entries':len(metas), 'rejectedAliases':rejected}}
    keep = set()
    for kind, (mapping, count) in maps.items():
        groups = {}
        for k,v in mapping.items():
            groups.setdefault(bucket(k,count), {})[k] = v
        paths = {}
        for b, values in groups.items():
            raw = encode({'version':1,'items':values})
            if len(raw) > 2*1024*1024:
                raise ValueError('Client chunk exceeds 2 MiB')
            digest = hashlib.sha256(raw).hexdigest()
            name = f'{kind}-{b}-{digest[:16]}.json'
            p = out/name
            if not p.exists(): p.write_bytes(raw)
            keep.add(name)
            paths[b] = {'file':name,'bytes':len(raw),'sha256':digest}
        manifest['maps'][kind] = {'count':count,'chunks':paths}
    raw = encode(manifest)
    if len(raw) > 512*1024: raise ValueError('Manifest too large')
    tmp = out/'manifest.tmp'; tmp.write_bytes(raw); tmp.replace(out/'manifest.json')
    # Persist timestamps: Git checkouts reset filesystem modification times.
    retention_path = out/'retention.json'
    retention = json.loads(retention_path.read_text()) if retention_path.exists() else {}
    now = time.time()
    tracked = {}
    for p in out.glob('*.json'):
        if not re.fullmatch(r'(id|title|meta)-[0-9]+-[a-f0-9]{16}\.json', p.name):
            continue
        last_used = now if p.name in keep else retention.get(p.name, now)
        if p.name not in keep and last_used < now-35*86400:
            p.unlink()
        else:
            tracked[p.name] = last_used
    tmp = out/'retention.tmp'; tmp.write_bytes(encode(tracked)); tmp.replace(retention_path)
    return manifest

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--data-dir', type=Path, default=Path('data'))
    result = export(p.parse_args().data_dir)
    print(json.dumps(result['counts']))
