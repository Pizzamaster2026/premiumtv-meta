"""Install centralized metadata adapter after all old definitions, before boot."""
from pathlib import Path
import sys, shutil
root=Path(sys.argv[1]); base=Path(__file__).resolve().parent
shutil.copy2(base/'assets/js/central-meta.js', root/'assets/js/central-meta.js')
p=root/'assets/index.html';s=p.read_text()
assert '<script src="js/app.js"></script>' in s
s=s.replace('<script src="js/app.js"></script>','<script src="js/central-meta.js"></script>\n<script src="js/app.js"></script>')
s=s.replace('</head>','<style>.provider-rating{display:inline-flex;align-items:center;gap:.25em;background:#111b;color:#fff;border-radius:.4em;padding:.15em .4em;font-size:.85em}.provider-rating em,.tmdbr em{font-size:.7em;font-style:normal}.pimg>.provider-rating{position:absolute;bottom:.4em;left:.4em}.central-genres{display:inline-flex;flex-wrap:wrap;gap:.4em}</style></head>')
p.write_text(s)
p=root/'assets/js/views.js';s=p.read_text()
old="(info.rating || x.rating) ? stars(info.rating || x.rating) : ''"
assert s.count(old)==2
s=s.replace(old,"CentralMeta.detailProvider(info, x)")
s=s.replace("x.rating ? stars(x.rating) : ''", "CentralMeta.providerBadge(x)")
s=s.replace("String(info.genre || '').split(/[,/]/)","infoGenres('movie', x)")
s=s.replace("info.genre || '', CentralMeta.detailProvider", "info.genre || infoGenres('series', x).join(', '), CentralMeta.detailProvider")
s=s.replace("'TMDB-Bewertungen', sw(Tmdb.ratingsOn()) + (Tmdb.key() ? '' : ' · ' + T('kein Schlüssel'))", "'Bewertungen', sw(Tmdb.ratingsOn()) + ' · zentraler Cache'")
p.write_text(s)
