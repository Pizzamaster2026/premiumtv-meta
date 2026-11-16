#!/usr/bin/env python3
"""Sport schedule (data/sport.json) for the PremiumTV player's "Sport live" row.

Built from the One provider's XMLTV EPG (one request: xmltv.php), streamed and
parsed incrementally, strictly time- and size-bounded. Only the programme
TITLE, sub-title and XMLTV <category> are used for the decision – never the
description (a show that only mentions "Fußball" in its text is not sport) and
never the channel name (Hausmeister Krause on Sport1 is not sport).

Format (UTF-8 JSON, times = UTC epoch seconds):
{
  "v": 1, "generatedAt": 1794550000, "source": "one-xmltv", "days": 3,
  "items": [
    {"epg": "skysportbundesliga1.de", "ch": ["Sky Sport Bundesliga 1 HD"],
     "title": "Bayern München – Borussia Dortmund", "start": 1794580200,
     "end": 1794587400, "kind": "bl", "league": "Bundesliga", "cat": "Sport"}
  ]
}
kind: bl, bl2, cl, dfb, pl, fb (other football), f1, moto, nfl, nba, ih
(ice hockey / NHL), tennis, box (boxing / UFC / MMA), hb, darts, other.
"cat" (optional, v0.2.12) = the XMLTV sport category when there is one.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

SPORT_FORMAT_VERSION = 1
DEFAULT_DAYS = 3
DEFAULT_BUDGET_SEC = 300          # whole step (download + parse)
DEFAULT_MAX_MB = 400              # decompressed XMLTV safety cap
MAX_ITEMS = 6000
PAST_SEC = 3 * 3600               # keep shows that started up to 3 h ago

# ---------------------------------------------------------------------------
# Classification – v0.2.12 rules (Tom 16.11.): a show is sport ONLY by its own title / sub-title and the
# XMLTV <category>; NEVER by the channel name (the channel is only used to drop a placeholder title that just
# repeats the channel name). Same patterns as the player's www/js/extras.js SportRules (generated from it);
# shared cases: tests/fixtures/sport_cases.json (= player tests/unit/sport_cases0212.json).
# Fixes: "Hausmeister Krause - Ordnung muss sein" / "Garagengold" on Sport1 / Magenta (dash + sport channel),
# "SpongeBob" (bob), "Dr. Pol" (del), "Magenta Sport 7" placeholders (bare "sport").
# ---------------------------------------------------------------------------
_I = re.I
KINDS: list[tuple[str, str, re.Pattern]] = [
    ("other", "Sport", re.compile(r'tischtennis|table tennis|volleyball|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])volley(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("fb", "Fußball", re.compile(r'junioren-bundesliga|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])u ?1[579]-bundesliga|'
                    r'a-junioren|b-junioren', _I)),
    ("hb", "Handball", re.compile(r'handball|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])rukomet(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(ehf|thw)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("nba", "Basketball", re.compile(r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])nba(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'basketball|basketbol|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])bbl(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'euroleague|eurocup basket|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])fiba(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(košarka|'
                    r'kosarka|baschet|baloncesto|pallacanestro)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'μπάσκετ', _I)),
    ("bl2", "2. Bundesliga", re.compile(r'(2\.|zweite)\s*(fu(ß|ss)ball-?)?bundesliga|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])2\.\s*liga(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|bundesliga 2(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("bl", "Bundesliga", re.compile(r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(fu(ß|ss)ball-)?bundesliga|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])dfl(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("cl", "Champions League", re.compile(r'champions league|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])uefa cl(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'königsklasse', _I)),
    ("dfb", "DFB-Pokal", re.compile(r'dfb[- ]?pokal', _I)),
    ("pl", "Premier League", re.compile(r'premier league|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])epl(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("nfl", "NFL", re.compile(r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])nfl(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'american football|super bowl|redzone|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])gfl(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|college football|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])ncaa(f|'
                    r' football)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|ran football', _I)),
    ("box", "Boxen / UFC", re.compile(r'boxen|boxing|boxkampf|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(ufc|mma|wwe|'
                    r'aew|pfl)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|wrestling|fight night|kampfsport|'
                    r'kickbox|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])judo(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])ringen(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])bellator(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])oktagon(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("darts", "Darts", re.compile(r'darts|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])pdc(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("tennis", "Tennis", re.compile(r'(^|[^h])tennis|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])tenis(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'τένις|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(atp|wta)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'wimbledon|us open(?! cup)|french open|roland garros|australian open|davis cup|billie jean king|united cup(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("ih", "Eishockey", re.compile(r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])nhl(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'eishockey|ice hockey|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])del2?(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω]).{0,30}(eishockey|'
                    r'hockey|spieltag|playoffs)|penny del|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(ishockey|'
                    r'hockey|jégkorong|hokej)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("moto", "Motorsport", re.compile(r'motogp|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])moto ?[23](?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'superbike|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])dtm(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'formel e(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|formula e(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'nascar|indycar|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])wec(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'le mans|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])rall(y|ye)|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])wrc(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|rallye? dakar|dakar rall|motorsport|adac gt|'
                    r'gt masters|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])24h|24 stunden von|(?:^|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])motocross|speedway|supercross|f2 (?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'formel 2(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("fb", "Fußball", re.compile(r'regionalliga|oberliga|verbandsliga|landesliga|bezirksliga|kreisliga|kreisklasse|kreisoberliga|landespokal|'
                    r'verbandspokal|kreispokal|frauen-?bundesliga|google pixel frauen|a-junioren|b-junioren|u ?1[579]-bundesliga|'
                    r'junioren-bundesliga|dfb-junioren|dfb-frauen|3\. ?liga|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])dritte liga(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|amateurfu(ß|ss)ball|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])u ?(17|'
                    r'19|21)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω]).{0,25}(fu(ß|ss)ball|em|wm|länderspiel)|'
                    r'europa league|conference league|la ?liga|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])serie a(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|ligue 1(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'eredivisie|s(ü|u)per lig(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|länderspiel|'
                    r'(wm|em)-qualifikation|nations league|liga (nacija|das nações|natiunilor)|fu(ß|ss)ball|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])soccer(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])uefa(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])fifa(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|copa del rey|copa libertadores|sudamericana|'
                    r'sul-americana|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])fa cup(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])efl(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])mls(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(fotbal|fudbal|nogomet|fotboll|voetbal|'
                    r'calcio|futbol|fútbol|futebol)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|ποδόσφαιρο|'
                    r'supercup(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|super league|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])derby(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(?! county)|revierderby', _I)),
    ("f1", "Formel 1", re.compile(r'formel ?1(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|formula ?1(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])f1(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("other", "Sport", re.compile(r'snooker|snuker|volleyball|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])volley(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])odbojka(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|biathlon|ski[ -]?alpin|ski-weltcup|skiweltcup|'
                    r'(esquí|sci|ski) alpin|alpsko skijanje|skispringen|ski jumping|skiflug|vierschanzentournee|langlauf|'
                    r'cross-country skiing|nordische kombination|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])rodeln(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|rodel-weltcup|zweierbob|viererbob|monobob|'
                    r'bobsport|bob[- ]weltcup|bob (und|&) skeleton|skeleton[- ]weltcup|eiskunstlauf|figure skating|eisschnelllauf|'
                    r'shorttrack|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])slalom(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'riesenslalom|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])super-g(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])abfahrt(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω]).{0,20}(herren|'
                    r'damen|ski)|radsport|cycling|tour de france|giro d.italia|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])vuelta(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|paris-roubaix|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])uci(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|leichtathletik|athletics|diamond league|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])golf(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(?! von|'
                    r' of)|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])pga(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'ryder cup|dp world tour|olympi(a|sch|c|que|cs)|paralympi|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])rugby(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])cricket(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|pferderennen|galopp|trabrennen|horse racing|'
                    r'feldhockey|wasserball|sportschau|sportstudio|sportclub|doppelpass|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])ran (racing|'
                    r'fighting)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])sport live(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])live-?sport(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|wintersport|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])weltcup(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|world cup(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)),
    ("f1", "Formel 1", re.compile(r'grand prix(?! der| de la chanson| eurovision)', _I)),
]
# never sport, whatever channel or category: sitcoms, docs / reality, car magazines, films, teleshopping, news …
NEVER_RE = re.compile(r'teleshopping|dauerwerbe|shopping|infomercial|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])shop(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(qvc|'
                    r'hse)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])poker(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|casino|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])lotto(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])quiz|'
                    r'tagesschau|nachrichten|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])news(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])wetter(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'heute[- ]journal|sendepause|programmpause|programmvorschau|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])promo(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])trailer(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|werbung|spielfilm|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])film:|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])serie:|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])krimi(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])doku|'
                    r'dokumentation|documentary|reportage|reality|sitcom|gottesdienst|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])kinder|'
                    r'cartoon|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])anime(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'kochen|hausmeister krause|garagengold|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])grip(?=$|'
                    r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|auto mobil|motor mobil|auto motor (und )?sport|'
                    r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])magazin(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'automagazin|motormagazin|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])ps[- ]profis|'
                    r'steel buddies|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])tuning(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                    r'oldtimer|autodoktoren|abenteuer auto|sim racing|spongebob|schwammkopf|svampe ?bob|sponge|bob.s burgers|'
                    r'bob hearts|volksmusik|eurovision|wer stiehlt mir die show', _I)
# XMLTV category says film / series / doc / news … -> not sport
CAT_NON_RE = re.compile(r'film|movie|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])serie|series|sitcom|(?:^|'
                      r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])doku|documentar|dokumentation|reportage|'
                      r'reality|shopping|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])kinder|children|(?:^|'
                      r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])kids(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                      r'animation|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])soap(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                      r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])news(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                      r'nachrichten|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])krimi(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                      r'thriller|comedy|spielshow', _I)
# XMLTV category is sport
CAT_SPORT_RE = re.compile(r'sport|fu(ß|ss)ball|football|soccer|tennis|handball|darts|boxing|boxen|racing|hockey|basketball|volleyball|'
                        r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])golf(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                        r'cricket|rugby|athletics|leichtathletik|radsport|cycling|snooker|wrestling|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])mma(?=$|'
                        r'[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)
# weak title words, only together with a sport category
WEAK_RE = re.compile(r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])live(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'highlights|spieltag|finale(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(halb|'
                   r'viertel|achtel)?finale|weltcup|world cup|meisterschaft|championship|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])pokal|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])cup(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])open(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'masters(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|turnier|rennen(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])race(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])qualifying(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])match(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])runde(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])round(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])liga(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])league(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])tour(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])gp(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])wm(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])em(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)
# particles allowed in a team name
PART_RE = re.compile(r'^(de|da|do|del|della|di|la|le|les|los|el|of|the|and|und|u|i|e|y|am|an|im|&|x|[0-9]+\.?|[ivx]+\.?)$', _I)
# known clubs / club words
CLUB_RE = re.compile(r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(fc|sv|vfb|vfl|vfr|tsv|tsg|tus|sc|sg|'
                   r'spvgg|fsv|ssv|bsc|ksc|bvb|hsv|rb|svw|sge|kfc|afc|cf|cd|ud|fk|nk|sk|bk|ik|if|aik|psv|az|ssc|as|ac|'
                   r'ogc|osc|rc|ca)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])1\. ?(fc|'
                   r'fsv|sc|ffc)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|spor(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(kickers|borussia|eintracht|fortuna|'
                   r'alemannia|arminia|dynamo|hansa|energie|union|werder|hertha|schalke|bayern|dortmund|leverkusen|gladbach|'
                   r'wolfsburg|hoffenheim|freiburg|augsburg|mainz|bochum|heidenheim|darmstadt|stuttgart|köln|hamburg|'
                   r'bremen|frankfurt|leipzig|kiel|st\. pauli|nürnberg|kaiserslautern|düsseldorf|hannover|paderborn|karlsruhe|'
                   r'elversberg|magdeburg|braunschweig|regensburg|fürth|münster|ulm|united|city|rovers|wanderers|athletic|'
                   r'albion|hotspur|arsenal|chelsea|liverpool|everton|real|atlético|atletico|barcelona|sevilla|valencia|'
                   r'juventus|(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])inter(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])|'
                   r'milan|napoli|roma|lazio|atalanta|psg|marseille|lyon|monaco|ajax|feyenoord|benfica|porto|sporting|'
                   r'galatasaray|fenerbah(ç|c)e|beşiktaş|besiktas|celtic|rangers|salzburg|rapid|austria wien|sturm graz|'
                   r'basel|young boys|zürich)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)
# national teams
NATION_RE = re.compile(r'(?:^|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])(deutschland|germany|österreich|austria|'
                     r'schweiz|switzerland|spanien|spain|england|frankreich|france|italien|italy|niederlande|netherlands|'
                     r'holland|belgien|belgium|portugal|kroatien|croatia|polen|poland|türkei|turkey|brasilien|brazil|argentinien|'
                     r'argentina|usa|dänemark|denmark|schweden|sweden|norwegen|norway|tschechien|ungarn|hungary|schottland|'
                     r'scotland|wales|irland|ireland|serbien|serbia|schweiz|ukraine|griechenland|greece|japan|mexiko|mexico|'
                     r'kanada|canada|marokko|uruguay|kolumbien|slowakei|slowenien|rumänien|albanien|bosnien|island|finnland|'
                     r'georgien)(?=$|[^a-z0-9äöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿăîșțşţćčđšžğıα-ω])', _I)
# club abbreviation (case-sensitive) or a country code in brackets: "FC Thun", "Boca Juniors (ARG)"
PFX_RE = re.compile(r'(^|[^A-Za-z])(FC|SV|VfB|VfL|VfR|TSV|TSG|TuS|SC|SG|SpVgg|FSV|SSV|BSC|KSC|BVB|HSV|RB|AFC|CF|CD|UD|'
                  r'FK|NK|SK|BK|IK|IF|AIK|PSV|AZ|SSC|AS|AC|OGC|OSC|RC|CA)(?=$|[^A-Za-z])|\([A-Z]{3}\)', 0)
# "Team A – Team B", "A vs. B", "A x B", "A gegen B"
SPLIT_RE = re.compile(r'^(.{2,40}?)\s+(?:[-–—:]|vs\.?|v\.|gegen|x)\s+(.{2,40})$', _I)
_LOWER_START = re.compile(r"^[a-zäöüßàáâãåæçèéêëìíîïñòóôõøùúûýÿ]")
_LIVE_PREFIX = re.compile(r"^live(?![a-z])\s*[:\-–]?\s*", re.I)


def _team_like(s: str) -> bool:
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s or "").strip()
    if not s or len(s) > 35 or re.search(r"[!?„“\"«»…]", s) or not re.search(r"[^\s\d.]", s):
        return False
    w = s.split()
    if len(w) > 5:
        return False
    return all(PART_RE.search(x) or not _LOWER_START.search(x) for x in w)


def _known(s: str) -> bool:
    return bool(CLUB_RE.search(s) or NATION_RE.search(s))


def _ch_key(n: str) -> str:
    s = (n or "").lower()
    s = re.sub(r"^[\s┃|\[(]*[a-z]{2,3}[\s┃|\])]*[:|┃\]-]?\s*", "", s)
    s = re.sub(r"\b(fhd|uhd|hd|sd|4k|hevc|raw|h265|50fps|60fps|backup|vip)\b", " ", s, flags=re.ASCII)
    return re.sub(r"[^a-z0-9äöüß]+", "", s)


def _by_kinds(t: str) -> Optional[tuple[str, str]]:
    for kind, label, rx in KINDS:
        if rx.search(t):
            return kind, label
    return None


def classify(title: str, sub: str = "", cats: Iterable[str] = (), channel: "str | Iterable[str]" = "") -> Optional[tuple[str, str]]:
    """(kind, league) for a sport programme, None for everything else.
    Only title / sub-title / XMLTV category count – never the description, never the channel name."""
    t = re.sub(r"\s+", " ", " ".join(x for x in (title or "", sub or "") if x)).strip()
    c = " / ".join(x for x in (cats or ()) if x)
    if not t or NEVER_RE.search(t):
        return None
    tk = _ch_key(title)
    chans = [channel] if isinstance(channel, str) else list(channel or ())
    if tk and any(tk == _ch_key(x) for x in chans if x):
        return None                                   # placeholder title = channel name
    cat_sport = bool(CAT_SPORT_RE.search(c))
    if c and CAT_NON_RE.search(c):
        return None
    k = _by_kinds(t)
    if k:
        return k
    m = SPLIT_RE.search(_LIVE_PREFIX.sub("", re.sub(r"\s+", " ", title or "").strip(), count=1))
    if m and _team_like(m.group(1)) and _team_like(m.group(2)):
        if (_known(m.group(1)) and _known(m.group(2))) or PFX_RE.search(m.group(1)) or PFX_RE.search(m.group(2)):
            return _by_kinds(c) or ("fb", "Fußball")
        if cat_sport:
            return _by_kinds(c) or ("other", "Sport")
    if cat_sport and WEAK_RE.search(t):
        return _by_kinds(c) or ("other", "Sport")
    return None


def sport_cat(cats: Iterable[str]) -> str:
    """first XMLTV category that says sport (for the player's own re-check), '' if none"""
    for x in cats or ():
        if x and CAT_SPORT_RE.search(x):
            return x[:40]
    return ""


# ---------------------------------------------------------------------------
# XMLTV
# ---------------------------------------------------------------------------
_TS_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?\s*([+-]\d{4}|Z|UTC|GMT)?")


def xmltv_ts(s: str) -> Optional[int]:
    m = _TS_RE.match((s or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, se, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se or 0), tzinfo=timezone.utc)
    off = 0
    if tz and tz[0] in "+-":
        off = (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60) * (1 if tz[0] == "+" else -1)
    return int(dt.timestamp()) - off


def parse_xmltv(chunks: Iterable[bytes], now: int, days: float = DEFAULT_DAYS, deadline: Optional[float] = None,
                max_bytes: int = DEFAULT_MAX_MB * 1024 * 1024) -> tuple[list[dict], dict]:
    """Incremental parse. Returns (items, stats). Raises TimeoutError / ValueError on budget / size limits."""
    parser = ET.XMLPullParser(events=("end",))
    names: dict[str, list[str]] = {}
    items: list[dict] = []
    seen = set()
    lo, hi = now - PAST_SEC, now + int(days * 86400)
    st = {"bytes": 0, "programmes": 0, "channels": 0, "sport": 0}
    for chunk in chunks:
        st["bytes"] += len(chunk)
        if st["bytes"] > max_bytes:
            raise ValueError(f"XMLTV größer als {max_bytes // (1024 * 1024)} MB")
        if deadline and time.monotonic() > deadline:
            raise TimeoutError("Zeitbudget für den Sportplan erreicht")
        parser.feed(chunk)
        for _ev, el in parser.read_events():
            tag = el.tag
            if tag == "channel":
                cid = (el.get("id") or "").strip()
                dn = [(x.text or "").strip() for x in el.findall("display-name") if (x.text or "").strip()]
                if cid:
                    names[cid] = dn[:3]
                st["channels"] += 1
                el.clear()
            elif tag == "programme":
                st["programmes"] += 1
                s, e = xmltv_ts(el.get("start") or ""), xmltv_ts(el.get("stop") or "")
                if s and e and e > lo and s < hi and e > s:
                    cid = (el.get("channel") or "").strip()
                    title = (el.findtext("title") or "").strip()
                    sub = (el.findtext("sub-title") or "").strip()
                    cats = [(c.text or "").strip() for c in el.findall("category")]
                    k = classify(title, sub, cats, names.get(cid, []) + [cid])   # channel only to drop placeholder titles
                    if k and (cid, s) not in seen:
                        seen.add((cid, s))
                        it = {"epg": cid, "ch": names.get(cid, [])[:3], "title": title[:160], "start": s, "end": e,
                              "kind": k[0], "league": k[1]}
                        sc = sport_cat(cats)
                        if sc:
                            it["cat"] = sc   # v0.2.12: optional, the player re-checks title + category (never the channel)
                        items.append(it)
                el.clear()
            elif tag not in ("title", "sub-title", "desc", "category", "display-name", "icon", "url", "episode-num", "rating", "value", "credits"):
                el.clear()
    try:
        parser.close()
    except ET.ParseError:
        pass   # cut-off tail: keep what was parsed
    for it in items:   # channel names declared after their programmes
        if not it["ch"] and it["epg"] in names:
            it["ch"] = names[it["epg"]][:3]
    items.sort(key=lambda x: (x["start"], x["epg"]))
    st["sport"] = len(items)
    return items[:MAX_ITEMS], st


def build_doc(items: list[dict], now: int, days: float = DEFAULT_DAYS, source: str = "one-xmltv") -> dict:
    return {"v": SPORT_FORMAT_VERSION, "generatedAt": int(now), "source": source, "days": days, "items": items}


# ---------------------------------------------------------------------------
# Download (streamed, gzip-aware, time-bounded)
# ---------------------------------------------------------------------------

def xmltv_url(base: str, user: str, password: str) -> str:
    return f"{base}/xmltv.php?" + urllib.parse.urlencode({"username": user, "password": password})


def stream_url(url: str, deadline: float, user_agent: str, chunk: int = 256 * 1024) -> Iterable[bytes]:
    left = max(5.0, deadline - time.monotonic())
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip", "Accept": "application/xml,text/xml,*/*"})
    with urllib.request.urlopen(req, timeout=min(120.0, left)) as resp:
        gz = "gzip" in (resp.headers.get("Content-Encoding") or "").lower()
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if gz else None
        first = True
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("Zeitbudget für den Sportplan erreicht")
            b = resp.read(chunk)
            if not b:
                break
            if first and not gz and b[:2] == b"\x1f\x8b":   # gzip body without header
                dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
            first = False
            yield dec.decompress(b) if dec else b
        if dec:
            tail = dec.flush()
            if tail:
                yield tail


def write_sport(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def run_step(data_dir: Path, provider: Optional[dict], base_fn: Callable[[str], str], log: Callable[[str], None], mask_url: Callable[[str], str],
             user_agent: str, budget_sec: float = DEFAULT_BUDGET_SEC, days: float = DEFAULT_DAYS, max_mb: float = DEFAULT_MAX_MB,
             now: Optional[int] = None, opener: Optional[Callable] = None) -> str:
    """Never raises. Returns 'ok', 'skipped' or 'failed'. On failure the old sport.json stays untouched."""
    t0 = time.monotonic()
    try:
        if not provider:
            log("Sportplan: kein One-Zugang hinterlegt – übersprungen.")
            return "skipped"
        base = base_fn(provider["url"])
        url = xmltv_url(base, provider["user"], provider["pass"])
        log(f"Sportplan: EPG von {mask_url(url)} (höchstens {budget_sec:g}s, {max_mb:g} MB) …")
        deadline = t0 + budget_sec
        now = int(now or time.time())
        chunks = (opener or stream_url)(url, deadline, user_agent)
        items, st = parse_xmltv(chunks, now, days=days, deadline=deadline, max_bytes=int(max_mb * 1024 * 1024))
        if st["programmes"] == 0:
            log("Sportplan: EPG leer oder kein XMLTV – alte sport.json bleibt.")
            return "failed"
        write_sport(Path(data_dir) / "sport.json", build_doc(items, now, days))
        log(f"Sportplan: {len(items)} Sportsendungen aus {st['programmes']} Sendungen / {st['channels']} Sendern "
            f"({st['bytes'] / 1048576:.1f} MB, {time.monotonic() - t0:.1f}s) → sport.json.")
        return "ok"
    except Exception as e:   # noqa: BLE001 – this step must never fail the sync
        msg = str(e)
        if provider:
            for secret in (provider.get("user"), provider.get("pass")):
                if secret:
                    for v in {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}:
                        msg = msg.replace(v, "********")
        log(f"Sportplan: übersprungen ({type(e).__name__}: {msg[:200]}) – alte sport.json bleibt.")
        return "failed"
