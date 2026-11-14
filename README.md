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

Der Job hat bei GitHub ein Zeitlimit von 120 Minuten. Das Skript beendet neue TMDB-Abfragen nach 100 Minuten, schreibt den Zwischenstand und endet ohne Fehler, damit der Cache gespeichert wird. Zusätzlich wird alle 500 neuen Einträge zwischengespeichert. Der nächste Lauf macht bei den restlichen Titeln weiter.

Titel, die bei keinem der Anbieter mehr in den Listen stehen, werden aus dem Cache entfernt.

## Hinweise

- Das Repository ist öffentlich, damit die App die Dateien ohne Token lesen kann. Lege hier **keine** Passwörter oder API-Schlüssel in Dateien ab.
- Das Skript schreibt Zugangsdaten niemals in die Protokolle.
- `premiumtv-news` bleibt unberührt.
