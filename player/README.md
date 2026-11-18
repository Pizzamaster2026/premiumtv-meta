# Zentraler Kunden-Cache / Player Test6

Stand 18.11.2026. `central-meta.js` ersetzt die direkten TMDB-API-Aufrufe
der WebView-App (Test5-Rebuild als Basis). Nach `tmdb.js`, `cast.js`,
`sinfo.js` und vor `app.js` laden. `central_patch.py` enthält die zusätzlich
erforderlichen Änderungen an Bewertungen/Genres in `views.js` und der HTML-Datei.
Diese zwei Dateien sind Integrationsteile, kein vollständiges Android-Projekt.
Die vollständigen Rebuild-Quellen werden separat mit der Test-APK ausgeliefert.

## Veröffentlichungsablauf

`export-client.yml` erzeugt `data/client-v1/manifest.json` plus kleine,
inhaltsadressierte JSON-Dateien. Es arbeitet ausschließlich auf dem vorhandenen
Cache: keine Provider-, TMDB- oder Streamaufrufe und keine API-Secrets erforderlich.
Auslösung nach Abschluss des vorhandenen nächtlichen Abgleichs, bei Änderung
des Exporters auf main oder manuell. Das Dashboard darf parallel seinen bestehenden
Abgleich ausführen. Kunden laden erst bei sichtbaren Titeln/geöffneten Details.

Provider-ID-Tabellen sind nach Anbieter und Film/Serie getrennt. Der Player
prüft Titel/Jahr oder eine explizite TMDB-ID gegen den aktuellen Film, bevor er
eine ID-Zuordnung verwendet. Ohne Jahr werden mehrdeutige Titeltreffer abgelehnt.
Titel-Aliase des alten Caches werden beim Export erneut geprüft. Ausgelassene
Aliase sind nicht automatisch bewiesene Falschzuordnungen: auch unklare
Übersetzungen/abweichende Titel können darunter sein. Keine Deckungsquote mit
der bisherigen Dashboard-TMDB-Quote gleichsetzen.

## Client-Verhalten

- Anbieterbewertung hat Vorrang; TMDB füllt fehlende Bewertungen bei mindestens
  drei Stimmen. Quellenkennzeichnung und einheitliche /10-Skala.
- Genres aus vorhandenen Anbieterdaten behalten; sonst zentrale Ergänzung auf
  Kacheln und Details. Anbieter-Kategorien/Listen werden nicht verändert.
- Fehlende Beschreibung und Poster können ergänzt werden. Keine zusätzliche
  Prüfung sämtlicher Filme gegen Provider-Detailendpunkte.
- Bestehende TMDB-Schlüssel aus Remote-News werden nicht mehr übernommen.
  Alle ursprünglichen TMDB-API-Anfragen werden durch lokale Cache-Adapter ersetzt.
  Statische Bilder von image.tmdb.org bleiben normale Bildabrufe (keine API).
- Maximal zwei parallele JSON-Abrufe, 8 Sekunden Timeout, 2 MiB je Datenstück,
  SHA-256-Prüfung, 8 MiB Arbeitsspeicher- und IndexedDB-Dateicache.
- Manifest höchstens alle 6 Stunden aktualisieren; vorübergehend offline bis
  30 Tage altes Manifest nutzbar. Alte serverseitige Chunks 35 Tage behalten.
- Ohne Cache bleibt die App bedienbar und zeigt vorhandene Anbieterangaben.
  Keine Behauptung, jeder Titel erhalte dadurch eine Bewertung oder ein Genre.

Darstellerfotos aus zentralen Filmdaten bleiben möglich; beliebige Personensuchen,
TMDB-Filmografien und neue Episodenbilder per TMDB-API entfallen, solange dafür
kein zentraler Datenbestand vorliegt. Vorhandene Anbieter-/lokale Daten bleiben.
Bewertungssortierung nutzt weiterhin Anbieterwerte, keinen vollständigen
Download des Kunden-Caches. EPG-Titel können nur im zentralen Filmbestand gefunden
werden; keine allgemeine TMDB-Suche auf Kundengeräten mehr.

## Tests und Grenzen

Server: `bash tests/run_all.sh` (offline Mocks/Fixtures), einschließlich falscher
Ersttreffer, Remakes, wiederverwendeter Altzuordnung, Export und Datenfilterung.
Player-Test: `tests/central-meta.cjs` im vollständigen Rebuild-Projekt.
`player/central-meta.test.cjs` ist dessen Referenzkopie; direkt im Server-Repository
fehlen die benötigten entpackten APK-Assets.
Ein visueller Android-/Fire-TV-Lauf und Geräteprüfung der IndexedDB-Ablage fehlen
in der Buildumgebung. Test6 ist eine separat signierte Test-App, kein freigegebenes
Produktionsupdate. Kategorienfix, Decoder, Handyfenster und TV-Fokus aus Test5
werden beibehalten.
