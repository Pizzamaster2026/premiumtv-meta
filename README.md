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

## Erwachsenen-Cover (ThePornDB)

Für Titel, die der Abgleich als Erwachsenenfilm erkennt (gleiche Erkennung wie beim TMDB-Ausschluss: Katalog-Präfix `(XXX-)`, `title.looks_adult` / `sync_meta.is_adult_group`), sucht ein eigener Job **nach** dem TMDB-Abgleich passende Cover bei [ThePornDB](https://theporndb.net).

- Workflow `sync-meta.yml`, Job `adult-covers` (läuft nach dem Job `sync`, auch wenn der fehlschlägt; nicht nach Abbruch).
- Schlüssel: Repository-Secret `TPDB_API_KEY` (Bearer). Er steht nie in Dateien oder im Log.
- Höchstens ~1 Anfrage pro Sekunde, höchstens 1500 Anfragen und 28 Minuten pro Nacht (`TPDB_MIN_INTERVAL_SEC`, `TPDB_MAX_REQUESTS`, `TPDB_BUDGET_MIN`). Der Rest kommt in den nächsten Nächten dran.
- Nur sichere Treffer: Titelähnlichkeit ≥ 0,9, bei „Seite - Titel“ muss die Seite (oder die genannten Darsteller) passen, bei „seite.JJ.MM.TT.name“ das genaue Datum; Jahr ±1; zwei gleich gute verschiedene Treffer = kein Treffer. Namen, die nur aus IDs/Hashes bestehen, werden gar nicht gesucht.
- Darstellerbild ohne Bild in der Szene: `/performers?q=` und nur, wenn genau ein Darsteller exakt so heißt.
- Nie tödlich: Fehler (auch 401/429/5xx) werden geloggt (nur Zahlen, keine Titel), der Job endet immer erfolgreich, der Kunden-Export läuft weiter.
- Treffer werden nach 180 Tagen, Fehlversuche nach 30 Tagen, Darsteller-Abfragen nach 60 Tagen neu gefragt. Netzwerkfehler werden nicht gespeichert (nächste Nacht erneut).

### Dateien

| Datei | Wer schreibt | Inhalt |
| --- | --- | --- |
| `data/adult-titles.json` | `sync_meta.py` (Job `sync`) | `{"version":1,"generatedAt":ISO,"items":{"<provider>:<type>:<id>":"<Originalname>"}}` – nur Erwachsenen-Einträge |
| `data/adult-tpdb.json` | `adult_tpdb.py` (Job `adult-covers`) | interner Cache, siehe unten |
| `data/client-v1/adult-<bucket>-<sha16>.json` | `export_client.py` | Kundendaten für die App (Map `adult`) |

Interner Cache `data/adult-tpdb.json` (die App liest ihn **nicht**):

```json
{"version":1,"updatedAt":"2026-11-22T02:40:00Z",
 "titles":{"<suchschluessel>":{"status":"match","checkedAt":"…","source":"scene|movie","tpdbId":"…","title":"…","site":"…",
                               "year":"2024","poster":"https://…","background":"https://…|''","overview":"…",
                               "performers":[{"name":"…","image":"https://…|''"}],"score":0.97},
           "<suchschluessel>":{"status":"miss","checkedAt":"…"}},
 "performers":{"<name normalisiert>":{"image":"https://…|''","checkedAt":"…"}}}
```

### Schema für die Player-App (ab 0.2.18)

`data/client-v1/manifest.json` bekommt (nur wenn es mindestens einen Treffer gibt) die Map `maps.adult` und immer `counts.adult`:

```json
"maps":{"adult":{"count":64,"chunks":{"<bucket>":{"file":"adult-<bucket>-<sha16>.json","bytes":123,"sha256":"…"}}}},
"counts":{"adult":1234}
```

- Bucket = `bucket(key, 64)` – derselbe Hash wie bei `id`/`title`/`meta` (djb2-XOR: `h=5381; h=((h*33)^charCode) & 0xffffffff`, dann `h % 64`).
- `key` = `<provider>:<type>:<streamId>`, z. B. `premiumtv:movie:613276` (wie die Map `id`).
- Chunk-Datei: `{"version":1,"items":{"<key>":ENTRY}}`

ENTRY (alle Felder immer vorhanden):

| Feld | Typ | Bedeutung |
| --- | --- | --- |
| `adult` | `true` | immer `true`: nur hinter der PIN zeigen |
| `q` | string | normalisierter Suchbegriff (Name ohne `(XXX-)`, bereinigt wie `title.clean`, dann klein, ß→ss, ohne Akzente, Nicht-Alphanumerisch→Leerzeichen). Die App vergleicht ihn mit dem aktuellen Namen der Zeile, bevor sie den Eintrag nutzt (Stream-IDs können neu vergeben werden). |
| `src` | `"scene"` \| `"movie"` | Treffer aus `/scenes` oder `/movies` |
| `title` | string | Titel bei TPDB (≤ 200) |
| `site` | string | Studio/Seite (≤ 80, kann leer sein) |
| `year` | string | `JJJJ` oder `""` |
| `poster` | string | https-URL des Covers (immer gesetzt) |
| `backdrop` | string | https-URL Hintergrundbild oder `""` |
| `overview` | string | Kurzbeschreibung (≤ 600, englisch, kann leer sein) |
| `performers` | array | bis 6 × `{"name": string, "photo": https-URL oder ""}` |

Regeln für die App: Die Map `adult` nur lesen, wenn Erwachsenen-Inhalte per PIN freigeschaltet sind; nie für Bildschirmschoner, Empfehlungen, Startseite, Suche ohne PIN oder Vorschauen verwenden. Fehlt `maps.adult`, gibt es (noch) keine Cover – normal weiterarbeiten.

## Hinweise

- Das Repository ist öffentlich, damit die App die Dateien ohne Token lesen kann. Lege hier **keine** Passwörter oder API-Schlüssel in Dateien ab.
- Das Skript schreibt Zugangsdaten niemals in die Protokolle.
- `premiumtv-news` bleibt unberührt.
