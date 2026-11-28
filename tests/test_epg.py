#!/usr/bin/env python3
"""EPG preprocessing (scripts/epg_build.py): fake data only, no network."""
import gzip, json, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import epg_build as b

NOW = 1796000000   # 2026-11-30 ~00:53 UTC
fails = []


def check(name, cond, val=None):
    if not cond:
        fails.append(name)
        print("FAIL", name, val)


def ts(t):   # epoch -> xmltv UTC
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t))


def xml(channels, progs):
    o = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for cid, names in channels:
        o.append(f'<channel id="{cid}">' + "".join(f"<display-name>{n}</display-name>" for n in names) + "</channel>")
    for cid, s, e, t, d in progs:
        o.append(f'<programme start="{ts(s)}" stop="{ts(e)}" channel="{cid}"><title>{t}</title><desc>{d}</desc></programme>')
    o.append("</tv>")
    return "\n".join(o).encode()


def shows(cid, titles, start=NOW - 3600, dur=3600):
    return [(cid, start + i * dur, start + (i + 1) * dur, t, "Beschreibung " + t) for i, t in enumerate(titles)]


# ---- 1. keys: shared fixture with the player test (www/js/extepg.js must give the same) ----
for name, key, tag in json.load(open(HERE / "fixtures" / "epg_keys.json", encoding="utf-8")):
    check("key:" + name, b.name_key(name) == key, (b.name_key(name), key))
    check("tag:" + name, b.country_tag(name) == tag, (b.country_tag(name), tag))
check("plus24_differs", b.name_key("DE: Sky Cinema Premiere +24 HD") != b.name_key("DE: Sky Cinema Premiere HD"))

# ---- 2. xmltv parse: window, timezone, overlap, duplicates ----
CH = [("RTL.de", ["RTL"]), ("ZDF.de", ["ZDF"]), ("Das.Erste.de", ["Das Erste"]), ("SAT.1.de", ["SAT.1"]),
      ("ORF.1.de", ["ORF 1"]), ("Prime.de", ["Prime"]), ("Prime.HD.de", ["Prime HD"]), ("kabel.eins.de", ["kabel eins"])]
T = ["Nachrichten", "Quiz", "Film A", "Serie B", "Magazin", "Film C", "Talk", "Show"]
P = []
for cid, _ in CH:
    P += shows(cid, T)
P.append(("RTL.de", NOW - 3600, NOW - 1800, "Dupe", "x"))                       # same start as first show: dropped
P.append(("ZDF.de", NOW + 100 * 3600, NOW + 101 * 3600, "Zu weit", "x"))        # outside the window
P.append(("ZDF.de", NOW - 20 * 3600, NOW - 19 * 3600, "Zu alt", "x"))
# local-time stamps with offset must become UTC
P.append(("SAT.1.de", 0, 0, "x", "x"))
raw = xml(CH, [p for p in P if p[1]])
names, progs, st = b.parse_xmltv([raw[:777], raw[777:]], NOW)    # chunked feed
check("parse_channels", len(names) == 8, len(names))
check("parse_window", all(p[0] > NOW - b.PAST_SEC - 7200 for l in progs.values() for p in l) and "Zu weit" not in str(progs["ZDF.de"]) and "Zu alt" not in str(progs["ZDF.de"]))
check("parse_dupe", [p[2] for p in progs["RTL.de"]][0] == "Nachrichten" and len(progs["RTL.de"]) == len(T), progs["RTL.de"][:2])
off = xml([("A.de", ["A"])], []).replace(b"</tv>", f'<programme start="20261130040000 +0100" stop="20261130050000 +0100" channel="A.de"><title>Zeitzone</title></programme></tv>'.encode())
_, pz, _ = b.parse_xmltv([off], 1796000000 - 4 * 3600, past=10 ** 8, ahead=10 ** 8)
check("parse_tz", pz["A.de"][0][0] == int(__import__("calendar").timegm((2026, 11, 30, 3, 0, 0))), pz)
ov_xml = xml([("B.de", ["B"])], [("B.de", NOW, NOW + 3600, "Eins", ""), ("B.de", NOW + 1800, NOW + 5400, "Zwei", "")])
_, po, _ = b.parse_xmltv([ov_xml], NOW)
check("parse_overlap_cut", po["B.de"][0][1] == NOW + 1800 and po["B.de"][1][0] == NOW + 1800, po)

# ---- 3. mapping ----
ov = {"map": {"ard": "Das.Erste.de", "kabel 1": "kabel.eins.de"}, "block": ["sat 1 gold"], "priority": ["RTL.de", "ZDF.de"]}
nm, amb = b.build_name_index(names, ov)
check("name_unique", nm.get("rtl") == "RTL.de" and nm.get("zdf") == "ZDF.de" and nm.get("sat1") == "SAT.1.de", nm)
check("name_ambiguous_dropped", "prime" not in nm and "prime" in amb, (nm.get("prime"), amb))
check("name_override", nm.get("ard") == "Das.Erste.de" and nm.get("kabel1") == "kabel.eins.de", nm)
check("name_never_base_for_plus24", nm.get(b.name_key("RTL +1")) is None)
ids = b.build_id_index(names)
check("id_index", ids.get("rtlde") == "RTL.de" and ids.get("kabeleinsde") == "kabel.eins.de", ids)

# ---- 4. One cross-check ----
ONE_CH = [("rtl.de", ["DE: RTL HD"]), ("zdf.de", ["DE: ZDF HD"]), ("daserste.de", ["DE: DAS ERSTE HD"]), ("sat1.de", ["DE: SAT.1 HD"]),
          ("kabeleins.de", ["DE: KABEL 1 HD"]), ("foo.de", ["DE: Foo TV"]), ("ard-id.de", ["VIP Irgendwas"]), ("orf1.at", ["AT: ORF 1 HD"])]
OP = shows("rtl.de", T, start=NOW)                                  # same schedule
OP += shows("zdf.de", ["Falsch %d" % i for i in range(8)], start=NOW)   # provider data wrong
OP += shows("daserste.de", T, start=NOW)
OP += shows("sat1.de", T, start=NOW)
OP += shows("ard-id.de", T, start=NOW)                              # schedule of ZDF-like? (same titles as RTL) -> ids don't match anything
on, opg, _ = b.parse_xmltv([xml(ONE_CH, OP)], NOW, want_desc=False, past=0)
by, rep = b.cross_check(names, progs, on, opg, nm, ids, NOW)
check("xc_rtl", by.get("rtl.de") == "RTL.de", by)
check("xc_zdf_strong_but_reported", by.get("zdf.de") == "ZDF.de" and any(x.startswith("zdf.de") for x in rep["disagree"]), (by, rep))
check("xc_daserste", by.get("daserste.de") == "Das.Erste.de", by)
check("xc_kabel", by.get("kabeleins.de") == "kabel.eins.de", by)
check("xc_foo_unmapped", "foo.de" not in by, by)
check("xc_no_foreign", "orf1.at" not in by, by)
# id says X, name says Y -> distrust both
on2 = {"zdf.de": ["DE: RTL HD"]}
by2, rep2 = b.cross_check(names, progs, on2, {}, nm, ids, NOW)
check("xc_conflict", not by2 and rep2["conflict"] == ["zdf.de"], (by2, rep2))
# only the name matches (odd id) -> needs title agreement
on3 = {"weird.de": ["DE: RTL HD"]}
by3, _ = b.cross_check(names, progs, on3, {"weird.de": [list(x[1:]) for x in shows("weird.de", T, start=NOW - 3600)]}, nm, ids, NOW)
by3b, _ = b.cross_check(names, progs, on3, {"weird.de": [list(x[1:]) for x in shows("weird.de", ["X%d" % i for i in range(8)], start=NOW - 3600)]}, nm, ids, NOW)
check("xc_name_only_verified_by_titles", by3.get("weird.de") == "RTL.de" and "weird.de" not in by3b, (by3, by3b))

# ---- 5. end to end + budget ----
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    (d / "e.xml.gz").write_bytes(gzip.compress(xml(CH * 1, P[:-1] * 130)))   # tiny file -> must be refused as implausible
    try:
        b.run(d / "e.xml.gz", d / "out.json", None, None, NOW, log=lambda *_: None)
        check("implausible_refused", False)
    except ValueError:
        check("implausible_refused", not (d / "out.json").exists())
    # plausible file: 120 channels x 200 shows
    ch = [(f"C{i}.de", [f"Kanal {i}"]) for i in range(120)] + CH
    pr = []
    for cid, _ in ch:
        pr += [(cid, NOW - 3600 + k * 1500, NOW - 3600 + (k + 1) * 1500, f"Sendung {cid} {k}", "Lange Beschreibung " * 30) for k in range(60)]
    (d / "big.xml.gz").write_bytes(gzip.compress(xml(ch, pr)))
    doc = b.run(d / "big.xml.gz", d / "out.json", HERE.parent / "epg" / "overrides.json", iter([xml(ONE_CH, OP)]), NOW, log=lambda *_: None)
    data = (d / "out.json").read_bytes()
    check("e2e_size_budget", len(data) <= b.MAX_BYTES, len(data))
    j = json.loads(data)
    check("e2e_format", j["v"] == 1 and j["generatedAt"] == NOW and "RTL.de" in j["ch"] and j["names"].get("rtl") == "RTL.de", list(j)[:6])
    check("e2e_desc_only_priority", len(j["ch"]["RTL.de"]["p"][0]) in (3, 4) and all(len(p) == 3 for p in j["ch"]["C5.de"]["p"]))
    check("e2e_times_int", all(isinstance(p[0], int) and isinstance(p[1], int) for l in j["ch"].values() for p in l["p"]))
    check("e2e_byepg", j["byEpg"].get("rtl.de") == "RTL.de", j["byEpg"])
    check("e2e_no_secret_shapes", "password" not in data.decode().lower() and "username" not in data.decode().lower())

if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("epg ok")
