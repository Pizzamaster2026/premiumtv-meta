# PremiumTV Metadaten-Cache

Öffentlicher Zwischenspeicher für Filminfos (Genres, Bewertung, Beschreibung, Laufzeit, Schauspieler) der Titel, die es bei **PremiumTV** und **One** gibt. Die Player-App kann die Dateien ohne Schlüssel lesen. Es stehen nur öffentlich verfügbare TMDB-Daten darin, keine Zugangsdaten und keine Streams.

This product uses the TMDB API but is not endorsed or certified by TMDB.

## Was der Job macht

Jede Nacht holt ein GitHub-Action-Job die **Listen** der Filme (`get_vod_streams`) und Serien (`get_series`) von den Anbietern und fragt bei TMDB nur **neue** oder **ältere als 30 Tage** Einträge ab. Es werden keine Streams und keine Einzel-Infos (`get_vod_info` / `get_series_info`) angefordert. Der Zugang wird also nur kurz für die Listen benutzt.

Die Ergebnisse liegen unter:

- `https://raw.githubusercontent.com/Pizzamaster2026/premiumtv-meta/main/data/index.json`
- `https://raw.githubusercontent.com/Pizzamaster2026/premiumtv-meta/main/data/meta.json`

(Wird die Datei sehr groß, teilt das Skript sie automatisch in `data/meta-a.json`, `data/meta-b.json`, … auf. Die Index-Datei zeigt dann auf die Teile.)

## Zeitplan

Der Job läuft **jede Nacht** um **02:00 UTC**.

- Im Winter (MEZ, UTC+1) ist das **03:00 Europe/Berlin**.
- Im Sommer (MESZ, UTC+2) ist das **04:00 Europe/Berlin**.

GitHub kann Cron-Jobs nur in UTC planen, daher diese Verschiebung um eine Stunde in der Sommerzeit. Zusätzlich kannst du den Job jederzeit von Hand starten (siehe unten).

## Secrets selbst eintragen

Die Zugänge kommen **nicht** in den Chat und nicht in Dateien im Repository. Du trägst sie selbst in GitHub ein – gerne mit deinem eigenen PremiumTV- bzw. One-Konto.

1. Öffne das Repository [Pizzamaster2026/premiumtv-meta](https://github.com/Pizzamaster2026/premiumtv-meta).
2. Oben **Settings**.
3. Links **Secrets and variables** → **Actions**.
4. **New repository secret** und nacheinander genau diese Namen anlegen:

| Name | Inhalt |
| --- | --- |
| `TMDB_API_KEY` | dein TMDB-API-Schlüssel (v3) |
| `PTV_URL` | Server-Adresse von PremiumTV, z. B. `http://premiumtvline.com:8080` |
| `PTV_USER` | Benutzername (dein Zugang) |
| `PTV_PASS` | Passwort |
| `ONE_URL` | Server-Adresse von One, z. B. `http://line.premiumtvline.de` |
| `ONE_USER` | Benutzername (dein Zugang) |
| `ONE_PASS` | Passwort |

Wenn du vorerst nur einen Anbieter nutzt, reicht der jeweilige Block (`PTV_*` **oder** `ONE_*`) plus `TMDB_API_KEY`. Fehlt ein Secret, beendet der Job sich ohne Fehler mit einer klaren Meldung und ändert nichts.

## Job manuell starten

1. Im Repository auf den Reiter **Actions**.
2. Links den Workflow **Sync TMDB-Metadaten** wählen.
3. Rechts **Run workflow** → **Run workflow**.

Beim ersten Mal kann der Lauf länger dauern (viele neue Titel). Danach bleiben bekannte Einträge liegen, und es werden nur noch neue oder veraltete Titel bei TMDB abgefragt.

Der Job hat bei GitHub ein Zeitlimit von 120 Minuten. Das Skript holt die großen Listen mit bis zu 300 Sekunden Wartezeit und nur zwei Versuchen (pro Anbieter über `SYNC_LIST_TIMEOUT_PREMIUMTV_SEC` / `SYNC_LIST_TIMEOUT_ONE_SEC` änderbar). Anschließend bleiben für neue TMDB-Abfragen höchstens 100 Minuten, abzüglich der bereits verbrauchten Listenzeit und einer Reserve zum Speichern. Der Zwischenstand wird geschrieben, der Job endet ohne Fehler, und alle 500 neuen Einträge wird zusätzlich gespeichert. Der nächste Lauf macht bei den restlichen Titeln weiter. Filme und Serien werden dabei abwechselnd abgeglichen, damit ein Zeitlimit nicht nur eine Sorte übrig lässt.

Titel, die bei keinem der Anbieter mehr in den Listen stehen, werden aus dem Cache entfernt.

## PremiumTV-Titel aus der Manager-App

Der PremiumTV-Server blockiert die Rechner von GitHub (Verbindung läuft in einen Timeout). Deshalb lädt die **PremiumTV Manager**-App (ab 1.5.45) die Titellisten über das Handy und legt nur eine verkleinerte Liste hier ab:

- Datei: `data/titles_premiumtv.json.gz` (gzip-komprimiertes JSON)
- Inhalt pro Titel nur: `id`, `name`, `year`, `tmdb` (falls der Anbieter eine TMDB-ID mitliefert), `cat` (Kategorie-ID). **Keine** Zugangsdaten, **keine** Stream-Links.

```json
{"format":"premiumtv-titles","version":1,"provider":"premiumtv",
 "generatedAt":"2026-11-15T12:00:00Z","app":"de.premiumtv.manager 1.5.45",
 "counts":{"movie":1,"series":1},
 "movie":[{"id":"1001","name":"DE - Der Pate (1972) 4K","year":"1972","tmdb":238,"cat":"12"}],
 "series":[{"id":"2001","name":"Breaking Bad (2008)","year":"2008","cat":"5"}]}
```

So nutzt der nächtliche Job die Datei:

1. Ist die Datei höchstens **7 Tage** alt (`generatedAt`, änderbar über `SYNC_TITLES_MAX_AGE_DAYS` bzw. `SYNC_TITLES_MAX_AGE_DAYS_PREMIUMTV`), wird der PremiumTV-Server **gar nicht** abgefragt.
2. Ist sie älter, versucht der Job den Server. Klappt das nicht, nimmt er die alte Datei und entfernt diesmal nichts aus dem Cache.
3. Im Protokoll steht pro Anbieter `Quelle Titeldatei … (Alter x Tage …)` oder `Quelle Server …`, in `data/index.json` unter `stats.sources`.

Die PTV-Secrets können bleiben; sie werden nur noch gebraucht, wenn die Datei fehlt oder zu alt ist.

**Token für die App:** Ein Fine-grained Token mit Zugriff nur auf `premiumtv-meta` und den Rechten **Contents: Read and write** (zum Hochladen der Datei) und **Actions: Read and write** (für „Jetzt abgleichen“). Ein klassisches Token braucht `repo` (bzw. `public_repo`) und `workflow`.

## Sportplan (sport.json) für „Sport live“

Der Workflow **„Sportplan aktualisieren“** (`.github/workflows/sync-sport.yml`) läuft alle 6 Stunden und baut `data/sport.json` aus dem EPG von **One** (eine Anfrage an `xmltv.php`, keine Streams, kein TMDB). Er nutzt nur die vorhandenen Secrets `ONE_URL`, `ONE_USER`, `ONE_PASS`.

- Zeitraum: die nächsten 3 Tage (`SYNC_SPORT_DAYS`), Sendungen ab 3 Stunden vor dem Lauf.
- Strenge Erkennung nur über Titel, Untertitel und EPG-Kategorie. Die Beschreibung zählt nie (z. B. „Wer stiehlt mir die Show?“ bleibt draußen, „NFL: …“ auf RTL kommt rein). Teleshopping, Nachrichten, Magazine und Krimis werden aussortiert. Seit 0.2.12 zählt der Sendername nie: „Hausmeister Krause“ oder „Garagengold“ auf Sport1/Magenta bleiben draußen. „Team A – Team B“ zählt nur mit Sport-Kategorie oder erkennbaren Mannschaften (z. B. „FC …“, „Bayern – Dortmund“). Fußball bis runter zur Kreisliga ist drin. Dieselben Regeln nutzt der Player (gemeinsame Testfälle `tests/fixtures/sport_cases.json`).
- Begrenzt auf `SYNC_SPORT_BUDGET_SEC` (Standard 300 s) und `SYNC_SPORT_MAX_MB` (Standard 400 MB). Klappt etwas nicht, bleibt die alte Datei stehen. Der Lauf schlägt deshalb nicht fehl.
- Der Player lädt die Datei höchstens alle 30 Minuten. Er ordnet die Sendungen über `epg` (= `epg_channel_id`) oder den Sendernamen den eigenen Sendern zu. Ist die Datei älter als 36 Stunden, fehlt sie oder passt nichts, nimmt er sein lokales EPG.
- Lokal: `python3 scripts/sync_meta.py --data-dir data --sport-only`. Im TMDB-Abgleich lässt sich der Schritt mit `SYNC_SPORT=1` mitlaufen lassen; im nächtlichen Workflow ist er aus (`SYNC_SPORT: "0"`), damit nicht zwei Jobs dieselbe Datei pushen.

Format (Zeiten in UTC-Sekunden):

```json
{"v": 1, "generatedAt": 1794751200, "source": "one-xmltv", "days": 3,
 "items": [{"epg": "skysportbundesliga1.de", "ch": ["DE: Sky Sport Bundesliga 1 HD"],
            "title": "Bayern München – Borussia Dortmund", "start": 1794749400, "end": 1794759600,
            "kind": "bl", "league": "Bundesliga", "cat": "Sport"}]}
```

`cat` (optional) = EPG-Sportkategorie. `kind`: `bl`, `bl2`, `cl`, `dfb`, `pl`, `fb` (anderer Fußball), `f1`, `moto`, `nfl`, `nba`, `ih` (Eishockey), `tennis`, `box`, `hb`, `darts`, `other`.

## Hinweise

- Das Repository ist öffentlich, damit die App die Dateien ohne Token lesen kann. Lege hier **keine** Passwörter oder API-Schlüssel in Dateien ab.
- Das Skript schreibt Zugangsdaten niemals in die Protokolle.
- `premiumtv-news` bleibt unberührt.
