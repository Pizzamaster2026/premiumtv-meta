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
                       'photo': image_path(c.get('profilePath') or c.get('photo'))}
                      for c in (e.get('cast') or [])[:8] if isinstance(c, dict) and c.get('name')],
                directors=[{'name': str(c.get('name') or '')[:80], 'id': int(number(c.get('id'), 1000000000))}
                           for c in (e.get('directors') or [])[:4] if isinstance(c, dict) and c.get('name')])

def https_url(s):
    return s if isinstance(s, str) and re.fullmatch(r'https://[A-Za-z0-9.-]+/[^\s"\'<>]{1,480}', s) else ''

def adult_items(data_dir: Path):
    """Adult covers (TPDB) by provider ID. Separate map 'adult': only behind the PIN, never in
    screensaver/recommendations. Missing/broken input files simply give no adult map."""
    try:
        from adult_tpdb import adult_query
        titles = json.loads((data_dir/'adult-titles.json').read_text()).get('items') or {}
        cache = json.loads((data_dir/'adult-tpdb.json').read_text()).get('titles') or {}
    except (OSError, ValueError, AttributeError, ImportError):
        return {}
    out = {}
    for key, name in titles.items():
        if not re.fullmatch(r'(premiumtv|one):(movie|series):[0-9]+', str(key)):
            continue
        aq = adult_query(name)
        r = cache.get(aq['key'])
        if not isinstance(r, dict) or r.get('status') != 'match' or not https_url(r.get('poster')):
            continue
        out[key] = {'adult': True, 'q': aq['key'], 'src': 'scene' if r.get('source') == 'scene' else 'movie',
                    'title': str(r.get('title') or '')[:200], 'site': str(r.get('site') or '')[:80],
                    'year': str(r.get('year') or '')[:4] if str(r.get('year') or '')[:4].isdigit() else '',
                    'poster': https_url(r.get('poster')), 'backdrop': https_url(r.get('background')),
                    'overview': str(r.get('overview') or '')[:600],
                    'performers': [{'name': str(p.get('name') or '')[:80], 'photo': https_url(p.get('image'))}
                                   for p in (r.get('performers') or [])[:6] if isinstance(p, dict) and p.get('name')]}
    return out


def people_prefix(n, nletters=3):
    """Three-letter bucket so full-cast index chunks stay under 2 MiB.
    A two-letter player query loads every bucket whose key starts with those letters."""
    letters = ''.join(ch for ch in str(n or '') if 'a' <= ch <= 'z')
    if not letters:
        return '0' * nletters
    if len(letters) >= nletters:
        return letters[:nletters]
    return (letters + ('x' * nletters))[:nletters]

def pack_person(rec):
    out = {'n': rec['n']}
    if rec.get('id'):
        out['i'] = rec['id']
    if rec.get('p'):
        out['p'] = rec['p']
    if rec.get('m'):
        out['m'] = rec['m']
    if rec.get('s'):
        out['s'] = rec['s']
    if rec.get('d'):
        out['d'] = rec['d']
    if rec.get('ds'):
        out['ds'] = rec['ds']
    return out

def people_groups(entries):
    """Invert TMDB credits: normalized person name -> compact title-id lists.
    Full records live in the prefix bucket of the whole name; the last-name
    bucket gets a stub so a search for 'Sandler' still finds Adam Sandler."""
    people = {}
    def rec(name, pid, photo):
        k = norm(name)
        if not k or len(k) < 2:
            return None
        r = people.get(k)
        if not r:
            r = {'n': str(name).strip()[:80], 'id': int(pid or 0), 'p': image_path(photo) if photo else '',
                 'm': [], 's': [], 'd': [], 'ds': []}
            people[k] = r
        else:
            if pid and not r['id']:
                r['id'] = int(pid)
            if photo and not r['p']:
                r['p'] = image_path(photo)
        return r
    def add(lst, tid):
        if tid and tid not in lst:
            lst.append(tid)
    for e in entries.values():
        if not isinstance(e, dict):
            continue
        typ = 's' if e.get('type') == 'series' else 'm'
        try:
            tid = int(e.get('tmdbId') or e.get('id') or 0)
        except (TypeError, ValueError):
            tid = 0
        if not tid:
            continue
        for c in e.get('cast') or []:
            if not isinstance(c, dict) or not c.get('name'):
                continue
            r = rec(c['name'], c.get('id'), c.get('profilePath') or c.get('photo'))
            if r:
                add(r['s' if typ == 's' else 'm'], tid)
        for c in e.get('directors') or []:
            if not isinstance(c, dict) or not c.get('name'):
                continue
            r = rec(c['name'], c.get('id'), None)
            if r:
                add(r['ds' if typ == 's' else 'd'], tid)
    groups = {}
    for k, r in people.items():
        packed = pack_person(r)
        groups.setdefault(people_prefix(k), {})[k] = packed
        parts = k.split()
        if len(parts) >= 2:
            last = parts[-1]
            if len(last) >= 2:
                b = people_prefix(last)
                bucket_items = groups.setdefault(b, {})
                if k not in bucket_items:
                    stub = {'r': k, 'n': r['n']}
                    if r.get('id'):
                        stub['i'] = r['id']
                    if r.get('p'):
                        stub['p'] = r['p']
                    bucket_items[k] = stub
    return groups, len(people)

def split_oversized_people(groups, max_bytes=2 * 1024 * 1024):
    """If a 3-letter bucket still exceeds 2 MiB, split it to 4 letters."""
    out = {}
    for b, values in groups.items():
        raw = encode({'version': 1, 'items': values})
        if len(raw) <= max_bytes:
            out[b] = values
            continue
        sub = {}
        for k, v in values.items():
            letters = ''.join(ch for ch in str(k or '') if 'a' <= ch <= 'z')
            nb = (letters + 'xxxx')[:max(4, len(str(b)) + 1)]
            sub.setdefault(nb, {})[k] = v
        for nb, items in sub.items():
            raw2 = encode({'version': 1, 'items': items})
            if len(raw2) > max_bytes:
                raise ValueError(f'People chunk {nb} exceeds 2 MiB after split')
            out[nb] = items
    return out

def write_chunks(kind, groups, out, keep):
    paths = {}
    for b, values in groups.items():
        raw = encode({'version': 1, 'items': values})
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Client chunk exceeds 2 MiB')
        digest = hashlib.sha256(raw).hexdigest()
        name = f'{kind}-{b}-{digest[:16]}.json'
        dest = out / name
        if not dest.exists():
            dest.write_bytes(raw)
        keep.add(name)
        paths[str(b)] = {'file': name, 'bytes': len(raw), 'sha256': digest}
    return paths

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
    adult = adult_items(data_dir)
    maps = {'id': (ids, 256), 'title': (titles, 256), 'meta': (metas, 1024)}
    if adult:
        maps['adult'] = (adult, 64)
    out = data_dir/'client-v1'; out.mkdir(exist_ok=True)
    manifest = {'version':1, 'generatedAt':index.get('generatedAt'), 'maps':{},
                'counts': {'ids':len(ids), 'titles':len(titles), 'entries':len(metas), 'rejectedAliases':rejected,
                           'adult':len(adult), 'people': 0}}
    keep = set()
    people, n_people = people_groups(entries)
    manifest['counts']['people'] = n_people
    for kind, (mapping, count) in maps.items():
        groups = {}
        for k,v in mapping.items():
            groups.setdefault(bucket(k,count), {})[k] = v
        paths = write_chunks(kind, groups, out, keep)
        manifest['maps'][kind] = {'count':count,'chunks':paths}
    if people:
        people = split_oversized_people(people)
        paths = write_chunks('people', people, out, keep)
        manifest['maps']['people'] = {'count': len(paths), 'scheme': 'prefix3', 'chunks': paths}
    raw = encode(manifest)
    if len(raw) > 512*1024: raise ValueError('Manifest too large')
    tmp = out/'manifest.tmp'; tmp.write_bytes(raw); tmp.replace(out/'manifest.json')
    # Persist timestamps: Git checkouts reset filesystem modification times.
    retention_path = out/'retention.json'
    retention = json.loads(retention_path.read_text()) if retention_path.exists() else {}
    now = time.time()
    tracked = {}
    for p in out.glob('*.json'):
        if not re.fullmatch(r'(id|title|meta|adult|people)-[a-z0-9]+-[a-f0-9]{16}\.json', p.name):
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
