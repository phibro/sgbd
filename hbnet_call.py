"""
Holt Daten eines Handball-Teams direkt von der Schnittstelle, die handball.net selbst benutzt,
und schreibt sie als kompakte Textdatei, die man einem LLM geben kann.

Die Schnittstelle lehnt eigene Abrufe mit "403 Forbidden" ab (sie verlangt ein x-client-token, das nur die
Webseite selbst erzeugt). Deshalb werden die normalen Teamseiten wie von einem Besucher geöffnet und
gelesen (siehe seiten_leser.py und seiten_daten.py, beide müssen im selben Ordner liegen):
Spiele aus dem HTML der Seite, Tabelle und Kader durch Anklicken der Reiter.
Lokal öffnet sich ein Browserfenster (Cookie-Banner selbst schließen, dann im Terminal Enter).
In GitHub Actions (Umgebungsvariable CI) läuft der Browser ohne Fenster.

Ausgabe im Ordner "ausgabe":
  team_<ID>_<Name>.md   -> Tabelle, kompletter Spielplan, Kader mit Statistiken
  roh/                  -> unveränderte JSON-Antworten (zum Nachprüfen)
  gesamt.md             -> alle Teams in einer Datei

Aufruf:  python handball_abruf.py
"""
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

from seiten_leser import SeitenLeser

# ---------------------------------------------------------------- Einstellungen
SEASON_ID = 2627                       # Saison 2026/2027
SAISON_VON = "2026-07-01"              # Zeitraum für den Spielplan
SAISON_BIS = "2027-07-31"

# Teams (ID: Name). Die ID steht in der handball.net-URL: .../team/87727
TEAMS = {
    86611: "Erste",
    83554: "Zweite",
    85947: "Dritte",
    85963: "Vierte",
    88735: "A",
    88756: "B",
    88826: "C",
    87727: "D1",
    87743: "D2",
    87763: "E1",
    87781: "E2",
    87805: "EQ",
    87821: "Minis 1",
    87822: "Minis 2",
	88860: "JSGEWWD1"
}

# Verein (für "Spiele des Vereins am Spieltag"): ID steht in der URL .../club/1fwspnh
VEREIN_ID = "1fwspnh"
# Falls die Vereinsabfrage nicht klappt, werden stattdessen diese Mannschaften einzeln abgefragt:
VEREIN_TEAMS = {    
    86611: "Erste",
    83554: "Zweite",
    85947: "Dritte",
    85963: "Vierte",
    88735: "A",
    88756: "B",
    88826: "C",
    87727: "D1",
    87743: "D2",
    87763: "E1",
    87781: "E2",
    87805: "EQ",
    87821: "Minis 1",
    87822: "Minis 2"}
# Links der Startseite (index.html): (Gruppe, [(Name, Adresse), ...])
STARTSEITE_LINKS = [
    ("Herren", [
        ("Erste - Oberliga", "https://sgbdall.pages.dev/seite_86611_Erste.html"),
        ("Zwote - OWL Bezirksliga", "https://sgbdall.pages.dev/seite_83554_Zweite.html"),
        ("Dritte - 1. Kreisklasse", "https://sgbdall.pages.dev/seite_85947_Dritte.html"),
        ("Vierte - 3. Kreisklasse", "https://sgbdall.pages.dev/seite_85963_Vierte.html"),
    ]),
    ("Jugend", [
        ("A - OWL Bezirksliga", "https://sgbdall.pages.dev/seite_88735_A.html"),
        ("B - OWL Bezirksliga", "https://sgbdall.pages.dev/seite_88756_B.html"),
        ("C - OWL Kreisliga", "https://sgbdall.pages.dev/seite_88826_C.html"),
        ("D1 - Kreisliga", "https://sgbdall.pages.dev/seite_87727_D1.html"),
        ("D2 - Kreisklasse", "https://sgbdall.pages.dev/seite_87743_D2.html"),
        ("E1 - Kreisliga", "https://sgbdall.pages.dev/seite_87763_E1.html"),
        ("E2 - Kreisklasse", "https://sgbdall.pages.dev/seite_87781_E2.html"),
    ]),
]
# ----------------------------------------------------------------------------

AUSGABE = Path("ausgabe")
PAUSE_SEITE = 1.5   # Sekunden zwischen zwei Seitenaufrufen - bitte nicht verkleinern
# Lokal: False (sichtbares Fenster, Cookie-Banner kannst du selbst schließen; die Auswahl landet in "browser_profil").
# In GitHub Actions ist die Umgebungsvariable CI gesetzt, dann läuft der Browser automatisch ohne Fenster.
HEADLESS = bool(os.environ.get("CI"))


def dateiname(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:60]


_leser_obj = None


def leser():
    """Liest die Teamseiten von handball.net wie ein Besucher (siehe seiten_leser.py)."""
    global _leser_obj
    if _leser_obj is None:
        _leser_obj = SeitenLeser(SEASON_ID, headless=HEADLESS, pause=PAUSE_SEITE)
    return _leser_obj


def browser_beenden():
    if _leser_obj is not None:
        _leser_obj.close()


def speichern_roh(name, daten):
    (AUSGABE / "roh").mkdir(parents=True, exist_ok=True)
    with open(AUSGABE / "roh" / name, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, indent=1)


def zahl(x):
    if isinstance(x, dict):
        for k in ("goals", "total", "score", "final"):
            if k in x:
                return x[k]
        return None
    return x


def ergebnis(m):
    r = m.get("result") or {}
    l, v = zahl(r.get("local")), zahl(r.get("visitor"))
    return f"{l}:{v}" if l is not None and v is not None else "-"


def datum_zeit(iso):
    # Die Zeiten sind als "+00:00" markiert, entsprechen aber der Ortszeit (wie auf der Webseite).
    try:
        d = datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M")
        tage = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        return f"{tage[d.weekday()]} {d:%d.%m.%Y %H:%M}"
    except Exception:
        return iso or ""


_liga_cache = {}   # merkt sich Liga-Spielpläne, falls mehrere Teams in derselben Liga spielen


def team_holen(team_id, name):
    print(f"\n== {name} (Team {team_id}) ==")
    zeilen = [f"# {name} (Team-ID {team_id})",
              f"Quelle: handball.net, abgerufen am {datetime.now():%d.%m.%Y %H:%M}",
              "Hinweis: Zeiten sind Ortszeit. 'Heim' bedeutet: das Team steht in der Ansetzung vorne.", ""]

    info = {"team_name": name, "team_id": team_id, "eigen": name, "liga_name": "",
            "stand": f"{datetime.now():%d.%m.%Y %H:%M}",
            "tabelle": [], "spiele": [], "liga": [], "kader": [], "gegner": {}}
    liga_spiele = []

    # 1) Spielplan der ganzen Saison
    print("  Spielplan ...")
    td = leser().team(team_id)   # Teamseite öffnen: Spiele, Tabelle und Kader auf einmal
    spiele = [m for m in td["spiele"] if SAISON_VON <= (m.get("date") or "")[:10] <= SAISON_BIS]
    if not spiele:
        zeilen.append("Keine Spiele gefunden.")
        return "\n".join(zeilen), info

    phasen = Counter(m["phase"]["id"] for m in spiele if m.get("phase"))
    phase_id = phasen.most_common(1)[0][0]
    phase = next(m["phase"] for m in spiele if m.get("phase") and m["phase"]["id"] == phase_id)
    liga = (phase.get("competition") or {}).get("name") or phase.get("name", "")
    info["liga_name"] = liga
    zeilen += [f"Liga/Runde: {liga} (Phase {phase_id})", ""]

    # 2) Tabelle
    print("  Tabelle ...")
    try:
        if not td["tabelle"]:
            raise ValueError("die Seite hat keine Tabelle geliefert")
        tab = {"data": td["tabelle"]}
        zeilen += ["## Tabelle", "",
                   "| Platz | Mannschaft | Spiele | S | U | N | Tore | Diff | Punkte | Form |",
                   "|---|---|---|---|---|---|---|---|---|---|"]
        for z in tab["data"]:
            form = "".join(f["result"] for f in z.get("form", []))
            info["tabelle"].append([z["position"], z["team"]["name"], z["played"], z["won"], z["drawn"],
                                    z["lost"], f"{z['goals_for']}:{z['goals_against']}", z["goals_diff"], z["points"]])
            zeilen.append(f"| {z['position']} | {z['team']['name']} | {z['played']} | {z['won']} | "
                          f"{z['drawn']} | {z['lost']} | {z['goals_for']}:{z['goals_against']} | "
                          f"{z['goals_diff']} | {z['points']} | {form} |")
        zeilen.append("")
    except Exception as e:
        zeilen += [f"(Tabelle nicht abrufbar: {e})", ""]

    # 3) Spielplan als Liste
    zeilen += ["## Spielplan (alle Spiele der Saison)", "",
               "| Runde | Termin | Ansetzung | Heim/Auswärts | Ergebnis | Status | Halle |",
               "|---|---|---|---|---|---|---|"]
    for m in sorted(spiele, key=lambda x: x.get("date") or ""):
        loc, vis = m["local"], m["visitor"]
        heim = "Heim" if loc["id"] == team_id else "Auswärts"
        info["eigen"] = loc["name"] if heim == "Heim" else vis["name"]
        info["spiele"].append({"r": m.get("round"), "d": (m.get("date") or "")[:16], "h": heim,
                               "g": vis["name"] if heim == "Heim" else loc["name"], "e": ergebnis(m),
                               "f": bool((m.get("status") or {}).get("is_finished")),
                               "o": (m.get("field") or {}).get("name", "")})
        status = (m.get("status") or {})
        status_text = "beendet" if status.get("is_finished") else ("LIVE" if status.get("is_live") else "ausstehend")
        halle = (m.get("field") or {}).get("name", "")
        zeilen.append(f"| {m.get('round', '')} | {datum_zeit(m.get('date'))} | "
                      f"{loc['name']} - {vis['name']} | {heim} | {ergebnis(m)} | {status_text} | {halle} |")
    zeilen.append("")

    # 3b) Spielplan der gesamten Liga (alle Mannschaften)
    print("  Liga-Spielplan ...")
    try:
        if phase_id not in _liga_cache:
            # eigene Spiele plus die Spiele aller Gegner aus deren Teamseiten
            _liga_cache[phase_id] = leser().liga_spiele(team_id, phase_id)
            speichern_roh(f"liga_{phase_id}_spiele.json", _liga_cache[phase_id])
        liga_spiele = _liga_cache[phase_id]
        zeilen += [f"## Spielplan der gesamten Liga ({liga})", "",
                   f"Alle {len(liga_spiele)} Spiele aller Mannschaften, nach Termin sortiert.", "",
                   "| Runde | Termin | Ansetzung | Ergebnis | Status | Halle |",
                   "|---|---|---|---|---|---|"]
        for m in sorted(liga_spiele, key=lambda x: x.get("date") or ""):
            status = (m.get("status") or {})
            status_text = "beendet" if status.get("is_finished") else ("LIVE" if status.get("is_live") else "ausstehend")
            halle = (m.get("field") or {}).get("name", "")
            info["liga"].append({"r": m.get("round"), "d": (m.get("date") or "")[:16],
                                 "l": m["local"]["name"], "v": m["visitor"]["name"],
                                 "lid": m["local"]["id"], "vid": m["visitor"]["id"], "e": ergebnis(m),
                                 "f": bool(status.get("is_finished")), "o": halle})
            zeilen.append(f"| {m.get('round', '')} | {datum_zeit(m.get('date'))} | "
                          f"{m['local']['name']} - {m['visitor']['name']} | {ergebnis(m)} | "
                          f"{status_text} | {halle} |")
        zeilen.append("")
    except Exception as e:
        zeilen += [f"(Liga-Spielplan nicht abrufbar: {e})", ""]

    # 4) Kader mit Statistiken
    print("  Kader ...")
    try:
        spieler = list(td["kader"])
        if not spieler:
            raise ValueError("die Seite hat keinen Kader geliefert")
        spieler.sort(key=lambda s: -(s["stats"].get("goals") or 0))
        zeilen += ["## Kader und Spielerstatistik (Saison)", "",
                   "| Nr | Name | Spiele | Tore | davon 7m | 7m-Versuche | Verwarnungen | 2-Min | Disqualifikationen |",
                   "|---|---|---|---|---|---|---|---|---|"]
        for s in spieler:
            p, st = s["player"], s["stats"]
            info["kader"].append({"n": s.get("dorsal") or "", "name": f"{p['first_name']} {p['last_name']}",
                                  "sp": st.get("matches_played", 0), "t": st.get("goals", 0),
                                  "m7": st.get("seven_meters", 0), "v": st.get("warnings", 0),
                                  "z": st.get("suspensions", 0), "dq": st.get("disqualifications", 0)})
            zeilen.append(f"| {s.get('dorsal') or '-'} | {p['first_name']} {p['last_name']} | "
                          f"{st.get('matches_played', 0)} | {st.get('goals', 0)} | {st.get('seven_meters', 0)} | "
                          f"{st.get('seven_meter_attempts', 0)} | {st.get('warnings', 0)} | "
                          f"{st.get('suspensions', 0)} | {st.get('disqualifications', 0)} |")
        zeilen.append("")
    except Exception as e:
        zeilen += [f"(Kader nicht abrufbar: {e})", ""]

    # 5) Torschützen der anderen Mannschaften der Liga (für die Gegner-Seite)
    print("  Gegner-Torschützen ...")
    try:
        gegner_ids = {}
        for m in liga_spiele:
            for seite in ("local", "visitor"):
                t = m[seite]
                if t["id"] != team_id:
                    gegner_ids[t["id"]] = t["name"]
        zeilen += ["## Beste Torschützen der anderen Mannschaften der Liga", ""]
        for gid, gname in gegner_ids.items():
            try:
                sp = list(leser().team(gid, tabelle=False)["kader"])
                sp.sort(key=lambda s: -(s["stats"].get("goals") or 0))
                top = [{"n": s.get("dorsal") or "", "name": f"{s['player']['first_name']} {s['player']['last_name']}",
                        "sp": s["stats"].get("matches_played", 0), "t": s["stats"].get("goals", 0),
                        "m7": s["stats"].get("seven_meters", 0)}
                       for s in sp[:5] if (s["stats"].get("goals") or 0) > 0]
                info["gegner"][gname] = top
                zeilen.append(f"- {gname}: " + (", ".join(f"{('#' + str(t['n']) + ' ') if t.get('n') else ''}{t['name']} ({t['t']} Tore in {t['sp']} Spielen)" for t in top) or "keine Daten"))
            except Exception as e:
                zeilen.append(f"- {gname}: nicht abrufbar ({e})")
        zeilen.append("")
    except Exception as e:
        zeilen += [f"(Gegner-Torschützen nicht abrufbar: {e})", ""]

    return "\n".join(zeilen), info


_verein_cache = []


def _verein_eintrag(m):
    loc, vis = m["local"], m["visitor"]
    status = (m.get("status") or {})
    comp = ((m.get("phase") or {}).get("competition") or {}).get("name") or (m.get("phase") or {}).get("name", "")
    return {"id": m.get("id"), "d": (m.get("date") or "")[:16], "l": loc["name"], "v": vis["name"],
            "hl": (loc.get("club") or {}).get("id") == VEREIN_ID or loc.get("id") in VEREIN_TEAMS,
            "hv": (vis.get("club") or {}).get("id") == VEREIN_ID or vis.get("id") in VEREIN_TEAMS,
            "e": ergebnis(m), "f": bool(status.get("is_finished")),
            "o": (m.get("field") or {}).get("name", ""), "c": comp.replace("BIHF ", "")}


def vereinsspiele():
    """Alle Spiele aller Mannschaften des Vereins (für die Spieltags-Übersicht)."""
    if _verein_cache:
        return _verein_cache
    print("\n== Spiele des Vereins ==")
    # Die Teamseiten der Mannschaften sind meist schon geladen (Zwischenspeicher), sonst werden sie jetzt geöffnet.
    roh, gesehen = [], set()
    for tid, tname in VEREIN_TEAMS.items():
        try:
            for m in leser().team(tid, tabelle=False)["spiele"]:
                if m["id"] not in gesehen and SAISON_VON <= (m.get("date") or "")[:10] <= SAISON_BIS:
                    gesehen.add(m["id"])
                    roh.append(m)
        except Exception as e:
            print(f"    {tname}: {e}")
    speichern_roh("verein_spiele.json", roh)
    eintraege = {}
    for m in roh:
        eintraege[m["id"]] = _verein_eintrag(m)
    _verein_cache.extend(sorted(eintraege.values(), key=lambda x: x["d"]))
    print(f"  {len(_verein_cache)} Spiele des Vereins")
    return _verein_cache


HTML_VORLAGE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="favicon.png">
<link rel="apple-touch-icon" href="favicon.png">
<title>__TITEL__</title>
<style>
:root{--bg:#f2f4f8;--card:#fff;--text:#14181f;--muted:#6b7280;--line:#e8ebf0;--accent:#1d4ed8;--accent2:#0ea5e9;--win:#16a34a;--draw:#d99a06;--loss:#dc2626;--hi:#eaf1ff;--shadow:0 1px 2px rgba(16,24,40,.05),0 4px 14px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f14;--card:#151b23;--text:#eef2f7;--muted:#97a1b0;--line:#262e39;--accent:#60a5fa;--accent2:#38bdf8;--win:#34d399;--draw:#fbbf24;--loss:#f87171;--hi:#17263b;--shadow:none}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow-wrap:anywhere}
.wrap{max-width:640px;margin:0 auto;padding:calc(78px + env(safe-area-inset-top)) 12px 32px}
header{padding:6px 4px 14px}
h1{font-size:1.5rem;font-weight:800;letter-spacing:-.02em;margin:0}
.sub{color:var(--text);font-size:1.05rem;font-weight:650;line-height:1.35;margin:0}
.card{background:var(--card);border-radius:18px;padding:14px;margin-bottom:12px;box-shadow:var(--shadow);border:1px solid var(--line)}
.card h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 10px;font-weight:700}
.hero{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:0}
.hero h2{color:rgba(255,255,255,.8)}
.hero a{color:#fff;font-weight:600}
.hero .tag{border-color:rgba(255,255,255,.55);color:#fff}
.hero .muted{color:rgba(255,255,255,.85)}
.big{font-size:1.3rem;font-weight:750;line-height:1.2;letter-spacing:-.01em}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line)}
.row>div:first-child{min-width:0;flex:1}
.row>div:last-child:not(:first-child){flex:none;text-align:right}
.row:first-of-type{border-top:0;padding-top:0}
.tabs{position:fixed;left:0;right:0;top:0;z-index:5;display:flex;justify-content:center;padding:calc(6px + env(safe-area-inset-top)) 8px 6px;background:color-mix(in srgb,var(--card) 92%,transparent);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.tabs-in{display:flex;width:100%;max-width:640px;gap:2px}
.tabs button{flex:1;min-width:0;border:0;background:none;color:var(--muted);border-radius:12px;padding:5px 0 4px;font:inherit;font-size:.66rem;font-weight:600;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:1px}
.tabs button i{font-style:normal;font-size:1.2rem;line-height:1.2}
.tabs button.on{background:var(--hi);color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:.74rem;table-layout:auto}
th,td{padding:7px 2px;text-align:center;border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:600;border-top:0;font-size:.6rem;text-transform:uppercase}
th,td{white-space:nowrap;overflow-wrap:normal}
td:nth-child(2),th:nth-child(2){text-align:left;white-space:normal;overflow-wrap:break-word;width:100%}
td:first-child,th:first-child{padding-left:0;color:var(--muted)}
td:last-child,th:last-child{padding-right:0}
tr.me td{background:var(--hi);font-weight:700}
tr.me td:first-child{border-radius:8px 0 0 8px;padding-left:4px}
tr.me td:last-child{border-radius:0 8px 8px 0;padding-right:4px}
td.pk{font-weight:800}
.badge{display:inline-block;min-width:1.7em;text-align:center;border-radius:7px;padding:1px 6px;font-size:.78rem;font-weight:700;color:#fff}
.S{background:var(--win)}.U{background:var(--draw)}.N{background:var(--loss)}
.muted{color:var(--muted);font-size:.8rem}
.warn{color:var(--draw)!important}
a{color:var(--accent)}
.tag{font-size:.68rem;border:1px solid var(--line);border-radius:999px;padding:0 8px;color:var(--muted);vertical-align:middle;white-space:nowrap}
.day{font-size:.78rem;font-weight:700;color:var(--muted);margin:16px 4px 6px}
label.opt{display:flex;gap:8px;align-items:center;font-size:.88rem;margin:0 4px 6px}
.score{font-weight:750;white-space:nowrap}
.rank{display:inline-flex;align-items:center;justify-content:center;width:1.6em;height:1.6em;border-radius:50%;background:var(--hi);color:var(--accent);font-weight:800;font-size:.8rem;margin-right:8px;flex:none}
.nr{color:var(--accent);font-weight:800}
.goals{font-weight:800;font-size:1.05rem}
select{width:100%;font:inherit;font-weight:600;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--text)}
.stats{display:flex;gap:8px}
.stat{flex:1;background:var(--hi);border-radius:12px;padding:8px 4px;text-align:center}
.stat b{display:block;font-size:1.15rem;font-weight:800}
.stat span{font-size:.68rem;color:var(--muted);text-transform:uppercase}
@media (max-width:340px){table{font-size:.66rem}.wrap{padding-left:8px;padding-right:8px}.card{padding:12px 10px}}
</style></head><body><div class="wrap">
<header><div class="sub" id="sub"></div></header>
<div id="view"></div>
<p class="muted" id="foot"></p>
</div>
<nav class="tabs"><div class="tabs-in" id="tabs"></div></nav>
<script>
const D = __DATA__;
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const WT = ['So','Mo','Di','Mi','Do','Fr','Sa'];
const p2 = n => String(n).padStart(2, '0');
function pd(s){const m=/^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(s||'');return m?new Date(+m[1],+m[2]-1,+m[3],+m[4],+m[5]):null}
function noTime(s){return /T00:00/.test(s||'')}
function fdate(d){return WT[d.getDay()]+' '+p2(d.getDate())+'.'+p2(d.getMonth()+1)+'.'+d.getFullYear()}
function fd(s){const d=pd(s);if(!d)return 'Termin offen';return fdate(d)+(noTime(s)?' · Uhrzeit offen':' · '+p2(d.getHours())+':'+p2(d.getMinutes())+' Uhr')}
function rel(s){const d=pd(s);if(!d)return '';const n=new Date();const a=new Date(n.getFullYear(),n.getMonth(),n.getDate());const b=new Date(d.getFullYear(),d.getMonth(),d.getDate());const k=Math.round((b-a)/864e5);return k===0?'heute':k===1?'morgen':k>1?'in '+k+' Tagen':k===-1?'gestern':'vor '+(-k)+' Tagen'}
function mapLink(h){return 'https://www.google.com/maps/search/?api=1&query='+encodeURIComponent(h)}
function mine(g){const m=/^(\d+):(\d+)$/.exec(g.e||'');if(!g.f||!m)return null;const a=+m[1],b=+m[2];return g.h==='Heim'?[a,b]:[b,a]}
function badge(g){const r=mine(g);if(!r)return '';const k=r[0]>r[1]?'S':r[0]<r[1]?'N':'U';return '<span class="badge '+k+'">'+k+'</span> <span class="score">'+r[0]+':'+r[1]+'</span>'}
const byDate=(a,b)=>(a.d||'').localeCompare(b.d||'');
const isOurs=g=>g.l===D.eigen||g.v===D.eigen;

function viewUebersicht(){
  const now=new Date(), grace=new Date(now.getTime()-3*36e5);
  const offen=D.spiele.filter(g=>!g.f&&(noTime(g.d)||(pd(g.d)&&pd(g.d)<grace))).sort(byDate);
  const next=D.spiele.filter(g=>!g.f&&!noTime(g.d)&&pd(g.d)&&pd(g.d)>=grace).sort(byDate)[0];
  let h='';
  if(next){
    h+='<div class="card hero"><h2>Nächstes Spiel</h2><div class="big">'+esc(next.g)+'</div>'
      +'<div>'+fd(next.d)+' <span class="tag">'+rel(next.d)+'</span></div>'
      +'<div style="margin:6px 0"><span class="tag">'+esc(next.h)+'</span> Runde '+esc(next.r)+'</div>'
      +'<div style="margin:6px 0"><a href="#" id="zumGegner">Bisherige Ergebnisse &amp; Top-5-Torschützen von '+esc(next.g)+' →</a></div>'+(next.o?'<div class="muted">📍 <a href="'+mapLink(next.o)+'" target="_blank" rel="noopener">'+esc(next.o)+'</a></div>':'')+'</div>';
  } else h+='<div class="card"><h2>Nächstes Spiel</h2>Kein anstehendes Spiel mit Termin gefunden.</div>';
  const last=D.spiele.filter(g=>g.f).sort(byDate).slice(-3).reverse();
  if(last.length)h+='<div class="card"><h2>Letzte Ergebnisse</h2>'+last.map(g=>'<div class="row"><div><b>'+esc(g.g)+'</b> <span class="tag">'+esc(g.h)+'</span><div class="muted">'+fd(g.d)+'</div></div><div>'+badge(g)+'</div></div>').join('')+'</div>';
  const me=D.tabelle.find(r=>r[1]===D.eigen);
  if(me)h+='<div class="card"><h2>Tabelle</h2><div class="big">Platz '+me[0]+' · '+me[8]+' Punkte</div><div class="muted">'+me[2]+' Spiele · '+me[3]+' S / '+me[4]+' U / '+me[5]+' N · Tore '+esc(me[6])+'</div></div>';
  if(offen.length)h+='<div class="card"><h2 class="warn">Ohne Ergebnis oder ohne Uhrzeit</h2>'+offen.map(g=>'<div class="row"><div><b>'+esc(g.g)+'</b> <span class="tag">'+esc(g.h)+'</span><div class="muted">'+fd(g.d)+' · Runde '+esc(g.r)+'</div></div></div>').join('')+'</div>';
  return h;
}
function viewTabelle(){
  return '<div class="card"><h2>'+esc(D.liga_name)+'</h2><table><tr><th>#</th><th>Mannschaft</th><th>Sp</th><th>S-U-N</th><th>Tore</th><th>Diff</th><th>Pkt</th></tr>'
   +D.tabelle.map(r=>'<tr class="'+(r[1]===D.eigen?'me':'')+'"><td>'+r[0]+'</td><td>'+esc(r[1])+'</td><td>'+r[2]+'</td><td>'+r[3]+'-'+r[4]+'-'+r[5]+'</td><td>'+esc(r[6])+'</td><td>'+(r[7]>0?'+':'')+r[7]+'</td><td class="pk">'+r[8]+'</td></tr>').join('')+'</table>'
   +'<p class="muted" style="margin:10px 0 0">Nicht alle Teams haben gleich viele Spiele absolviert.</p></div>';
}
function viewSpielplan(){
  return '<div class="card"><h2>Alle Spiele der Saison</h2>'+D.spiele.slice().sort(byDate).map(g=>'<div class="row"><div><b>'+esc(g.g)+'</b> <span class="tag">'+esc(g.h)+'</span><div class="muted">'+fd(g.d)+' · R'+esc(g.r)+'<br>'+esc(g.o)+'</div></div><div>'+badge(g)+'</div></div>').join('')+'</div>';
}
let ligaAlle=false;
function viewLiga(){
  const now=new Date(new Date().getFullYear(),new Date().getMonth(),new Date().getDate());
  let list=D.liga.slice().sort(byDate);
  if(!ligaAlle)list=list.filter(g=>!g.f||(pd(g.d)&&pd(g.d)>=now));
  let h='<label class="opt"><input type="checkbox" id="ligaAlle" '+(ligaAlle?'checked':'')+'> auch bereits gespielte Spiele zeigen</label>';
  let day='';
  list.forEach(g=>{const d=pd(g.d);const key=d?fdate(d):'Ohne Termin';
    if(key!==day){day=key;h+='<div class="day">'+esc(key)+'</div>';}
    h+='<div class="card" style="padding:8px 14px;margin-bottom:6px'+(isOurs(g)?';background:var(--hi)':'')+'"><div class="row" style="padding:2px 0"><div>'+esc(g.l)+' – '+esc(g.v)+'<div class="muted">'+(noTime(g.d)?'Uhrzeit offen':(d?p2(d.getHours())+':'+p2(d.getMinutes())+' Uhr':''))+' · '+esc(g.o)+'</div></div><div class="score">'+(g.f?esc(g.e):'')+'</div></div></div>';});
  return h||'<div class="card">Keine Spiele.</div>';
}
function viewKader(){
  return '<div class="card"><h2>Kader und Statistik</h2><table><tr><th>Nr</th><th>Name</th><th>Sp</th><th>Tore</th><th>7m</th><th>Verw</th><th>2′</th></tr>'
   +D.kader.map(k=>'<tr><td>'+esc(k.n)+'</td><td>'+esc(k.name)+'</td><td>'+k.sp+'</td><td class="pk">'+k.t+'</td><td>'+k.m7+'</td><td>'+k.v+'</td><td>'+k.z+'</td></tr>').join('')+'</table></div>';
}
function gegnerListe(){return [...new Set(D.liga.flatMap(g=>[g.l,g.v]))].filter(n=>n!==D.eigen).sort()}
let gegnerSel=null;
function naechsterGegner(){const grace=new Date(Date.now()-3*36e5);const n=D.spiele.filter(g=>!g.f&&pd(g.d)&&pd(g.d)>=grace).sort(byDate)[0]||D.spiele.filter(g=>!g.f).sort(byDate)[0];return n?n.g:gegnerListe()[0]}
function viewGegner(){
  const liste=gegnerListe();
  if(!gegnerSel)gegnerSel=naechsterGegner();
  const gn=gegnerSel, nx=naechsterGegner();
  let h='<div class="card"><h2>Gegner (★ = nächster Gegner)</h2><select id="gsel">'
    +liste.map(n=>'<option '+(n===gn?'selected':'')+' value="'+esc(n)+'">'+(n===nx?'★ ':'')+esc(n)+'</option>').join('')+'</select></div>';
  const row=D.tabelle.find(r=>r[1]===gn);
  if(row)h+='<div class="card"><h2>Tabelle</h2><div class="big" style="margin-bottom:10px">Platz '+row[0]+'</div><div class="stats"><div class="stat"><b>'+row[8]+'</b><span>Punkte</span></div><div class="stat"><b>'+row[3]+'-'+row[4]+'-'+row[5]+'</b><span>S-U-N</span></div><div class="stat"><b>'+esc(row[6])+'</b><span>Tore</span></div></div></div>';
  const top=(D.gegner||{})[gn]||[];
  h+='<div class="card"><h2>Top 5 Torschützen</h2>'+(top.length?top.map((t,i)=>'<div class="row"><div style="display:flex;align-items:center"><span class="rank">'+(i+1)+'</span><div style="min-width:0"><b>'+(t.n?'<span class="nr">#'+esc(t.n)+'</span> ':'')+esc(t.name)+'</b><div class="muted">'+t.sp+' Spiele'+(t.m7?' · '+t.m7+' × 7m':'')+'</div></div></div><div class="goals">'+t.t+'<span class="muted"> Tore</span></div></div>').join(''):'<span class="muted">Keine Daten vorhanden.</span>')+'</div>';
  const spiele=D.liga.filter(g=>(g.l===gn||g.v===gn)&&g.f).sort(byDate);
  h+='<div class="card"><h2>Bisherige Ergebnisse</h2>'+(spiele.length?spiele.map(g=>{
    const heim=g.l===gn, m=/^(\d+):(\d+)$/.exec(g.e||'');
    let b='';if(m){const a=heim?+m[1]:+m[2],c=heim?+m[2]:+m[1];const k=a>c?'S':a<c?'N':'U';b='<span class="badge '+k+'">'+k+'</span> <span class="score">'+a+':'+c+'</span>'}
    const geg=heim?g.v:g.l;
    return '<div class="row"><div><b>'+(geg===D.eigen?'<span style="color:var(--accent)">'+esc(geg)+'</span>':esc(geg))+'</b> <span class="tag">'+(heim?'Heim':'Auswärts')+'</span><div class="muted">'+fd(g.d)+' · R'+esc(g.r)+'</div></div><div>'+b+'</div></div>'}).join(''):'<span class="muted">Noch keine Spiele.</span>')
    +'<p class="muted">S/U/N aus Sicht von '+esc(gn)+'.</p></div>';
  return h;
}
const TABS=[['Start',viewUebersicht,'🏠'],['Gegner',viewGegner,'🎯'],['Tabelle',viewTabelle,'🏆'],['Plan',viewSpielplan,'📅'],['Liga',viewLiga,'🗂️'],['Kader',viewKader,'👥']];
let cur=0;
function render(){
  $('#tabs').innerHTML=TABS.map((t,i)=>'<button data-i="'+i+'" class="'+(i===cur?'on':'')+'"><i>'+t[2]+'</i>'+t[0]+'</button>').join('');
  $('#view').innerHTML=TABS[cur][1]();
  document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{cur=+b.dataset.i;render();window.scrollTo(0,0)});
  const zg=$('#zumGegner');if(zg)zg.onclick=e=>{e.preventDefault();gegnerSel=null;cur=1;render();window.scrollTo(0,0)};
  const gs=$('#gsel');if(gs)gs.onchange=()=>{gegnerSel=gs.value;render()};
  const c=$('#ligaAlle');if(c)c.onchange=()=>{ligaAlle=c.checked;render()};
}
$('#sub').textContent=D.liga_name+' · Stand der Daten: '+D.stand;
$('#foot').textContent='Daten von handball.net, Stand '+D.stand+'. Nur für den Vereinsgebrauch.';
render();
</script></body></html>"""


def seite_html(info):
    """Baut eine einzelne HTML-Datei, die man per WhatsApp/E-Mail verschicken und am Handy öffnen kann."""
    daten = json.dumps(info, ensure_ascii=False).replace("</", "<\\/")
    titel = f"{info.get('team_name', 'Team')} - Übersicht"
    return HTML_VORLAGE.replace("__TITEL__", titel).replace("__DATA__", daten)

INDEX_VORLAGE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="favicon.png">
<link rel="apple-touch-icon" href="favicon.png">
<title>SG Bünde-Dünne</title>
<style>
:root{--bg:#f2f4f8;--card:#fff;--text:#14181f;--muted:#6b7280;--line:#e8ebf0;--accent:#1d4ed8;--accent2:#0ea5e9;--hi:#eaf1ff;--shadow:0 1px 2px rgba(16,24,40,.05),0 4px 14px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f14;--card:#151b23;--text:#eef2f7;--muted:#97a1b0;--line:#262e39;--accent:#60a5fa;--accent2:#38bdf8;--hi:#17263b;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow-wrap:anywhere}
.wrap{max-width:640px;margin:0 auto;padding:12px 12px 32px}
header{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border-radius:18px;padding:16px 18px;margin-bottom:12px}
h1{font-size:1.35rem;font-weight:800;letter-spacing:-.02em;margin:0}
header p{margin:2px 0 0;opacity:.9;font-size:.85rem}
h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 8px;font-weight:700}
.card{background:var(--card);border:1px solid var(--line);box-shadow:var(--shadow);border-radius:18px;padding:12px 14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.link{display:flex;justify-content:space-between;align-items:center;gap:6px;background:var(--bg);border-radius:12px;padding:11px 12px;color:var(--text);text-decoration:none;font-weight:650;font-size:.88rem;line-height:1.25}
.link:active,.link:hover{background:var(--hi);color:var(--accent)}
.link i{font-style:normal;color:var(--accent);font-size:1.2rem;line-height:1;flex:none}
.grp+.grp{margin-top:12px}
.seg{display:flex;background:var(--bg);border-radius:10px;padding:3px;margin-bottom:6px}
.seg button{flex:1;border:0;background:none;color:var(--muted);font:inherit;font-size:.82rem;font-weight:700;padding:6px 0;border-radius:8px;cursor:pointer}
.seg button.on{background:var(--card);color:var(--accent);box-shadow:var(--shadow)}
.day{font-size:.78rem;font-weight:700;color:var(--muted);margin:10px 0 2px}
.nav{display:flex;gap:6px;margin-bottom:8px}
.nav select{flex:1;min-width:0;font:inherit;font-weight:650;font-size:.88rem;padding:9px 8px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--text)}
.nav button{flex:none;width:2.6rem;border:1px solid var(--line);background:var(--card);color:var(--accent);border-radius:10px;font-size:1.4rem;line-height:1;cursor:pointer}
.nav button:disabled{opacity:.35}
a{color:var(--accent);font-size:.85rem;font-weight:600}
.gm{display:flex;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
.gm .t{flex:none;width:2.9rem;font-weight:750;font-size:.85rem;font-variant-numeric:tabular-nums}
.gm .b{flex:1;min-width:0;font-size:.88rem}
.gm .c{color:var(--muted);font-size:.72rem}
.gm .us{font-weight:750}
.score{font-weight:750;white-space:nowrap}
.muted{color:var(--muted);font-size:.8rem}
</style></head><body><div class="wrap">
<header><h1>SG Bünde-Dünne</h1><p>Mannschaften und Spieltag im Überblick</p></header>
<div class="card" id="links"></div>
<div id="spieltag"></div>
<p class="muted" id="foot"></p>
</div>
<script>
const D = __DATA__;
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const WT = ['So','Mo','Di','Mi','Do','Fr','Sa'];
const p2 = n => String(n).padStart(2, '0');
function pd(s){const m=/^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(s||'');return m?new Date(+m[1],+m[2]-1,+m[3],+m[4],+m[5]):null}
function noTime(s){return /T00:00/.test(s||'')}
function fdate(d){return WT[d.getDay()]+' '+p2(d.getDate())+'.'+p2(d.getMonth()+1)+'.'+d.getFullYear()}
const byDate=(a,b)=>(a.d||'').localeCompare(b.d||'');
$('#links').innerHTML=D.links.map(g=>'<div class="grp"><h2>'+esc(g[0])+'</h2><div class="grid">'+g[1].map(l=>'<a class="link" href="'+esc(l[1])+'" target="_blank" rel="noopener"><span>'+esc(l[0]).replace(/ - /g,'\u00a0- ').replace(/(\d\.) /g,'$1\u00a0')+'</span><i>›</i></a>').join('')+'</div></div>').join('');
let seg='alle', sel=null;
const mon=d=>{const x=new Date(d.getFullYear(),d.getMonth(),d.getDate());x.setDate(x.getDate()-((x.getDay()+6)%7));return x.getTime()};
function kw(t){const d=new Date(t);d.setDate(d.getDate()+3);const y=new Date(d.getFullYear(),0,4);return 1+Math.round(((d-y)/864e5-3+((y.getDay()+6)%7))/7)}
function spieltage(){
  const V=(D.verein||[]).filter(g=>pd(g.d));
  const weeks=[...new Set(V.map(g=>mon(pd(g.d))))].sort((a,b)=>a-b);
  const now=new Date(), t0=new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime();
  let cur=weeks.findIndex(k=>k+6*864e5>=t0);
  if(cur<0)cur=weeks.length-1;
  return {V,weeks,cur};
}
const rng=w=>{const a=new Date(w),e=new Date(w+6*864e5);return p2(a.getDate())+'.'+p2(a.getMonth()+1)+'. – '+p2(e.getDate())+'.'+p2(e.getMonth()+1)+'.'};
function render(){
  const st=spieltage();
  if(!st.weeks.length){$('#spieltag').innerHTML='';return;}
  if(sel===null||sel<0||sel>=st.weeks.length)sel=st.cur;
  const w=st.weeks[sel];
  const all=st.V.filter(g=>mon(pd(g.d))===w).sort(byDate);
  const games=all.filter(g=>seg==='alle'||g.hl);
  let h='<div class="card"><h2>Spieltag (★ = aktuell)</h2>'
   +'<div class="nav"><button id="prev" '+(sel===0?'disabled':'')+' aria-label="Vorheriger Spieltag">‹</button>'
   +'<select id="wsel">'+st.weeks.map((k,i)=>'<option value="'+i+'" '+(i===sel?'selected':'')+'>'+(i===st.cur?'★ ':'')+'KW '+kw(k)+' · '+rng(k)+'</option>').join('')+'</select>'
   +'<button id="next" '+(sel===st.weeks.length-1?'disabled':'')+' aria-label="Nächster Spieltag">›</button></div>'
   +(sel!==st.cur?'<div style="margin:-2px 0 8px"><a href="#" id="heute">↩ Zum aktuellen Spieltag</a></div>':'')
   +'<div class="seg"><button data-seg="alle" class="'+(seg==='alle'?'on':'')+'">Alle Spiele ('+all.length+')</button><button data-seg="heim" class="'+(seg==='heim'?'on':'')+'">Nur Heimspiele ('+all.filter(g=>g.hl).length+')</button></div>';
  if(!games.length)h+='<span class="muted">Keine Spiele.</span>';
  let day='';
  games.forEach(g=>{const d=pd(g.d),key=fdate(d);
    if(key!==day){day=key;h+='<div class="day">'+esc(key)+'</div>';}
    const nm=(x,us)=>us?'<span class="us">'+esc(x)+'</span>':esc(x);
    h+='<div class="gm"><div class="t">'+(noTime(g.d)?'offen':p2(d.getHours())+':'+p2(d.getMinutes()))+'</div><div class="b">'+nm(g.l,g.hl)+' – '+nm(g.v,g.hv)
      +'<div class="c">'+esc(g.c)+(g.o?' · '+esc(g.o):'')+'</div></div>'+(g.f?'<div class="score">'+esc(g.e)+'</div>':'')+'</div>';});
  $('#spieltag').innerHTML=h+'</div>';
  document.querySelectorAll('[data-seg]').forEach(b=>b.onclick=()=>{seg=b.dataset.seg;render()});
  $('#wsel').onchange=e=>{sel=+e.target.value;render()};
  $('#prev').onclick=()=>{sel--;render()};
  $('#next').onclick=()=>{sel++;render()};
  const hk=$('#heute');if(hk)hk.onclick=e=>{e.preventDefault();sel=st.cur;render()};
}
$('#foot').textContent='Daten von handball.net'+(D.stand?', Stand '+D.stand:'')+'. Nur für den Vereinsgebrauch.';
render();
</script></body></html>"""


def index_html(verein):
    """Startseite mit Links zu allen Mannschaftsseiten und dem Spieltag des Vereins."""
    daten = {"links": STARTSEITE_LINKS, "verein": verein, "stand": f"{datetime.now():%d.%m.%Y %H:%M}"}
    return INDEX_VORLAGE.replace("__DATA__", json.dumps(daten, ensure_ascii=False).replace("</", "<\\/"))


def main():
    AUSGABE.mkdir(exist_ok=True)
    icon = Path(__file__).resolve().parent / "favicon.png"
    if icon.exists():
        shutil.copyfile(icon, AUSGABE / "favicon.png")
    else:
        print("  Hinweis: favicon.png nicht neben dem Skript gefunden - kein Favicon.")
    alle = []
    for tid, name in TEAMS.items():
        try:
            text, info = team_holen(tid, name)
            seite = AUSGABE / f"seite_{tid}_{dateiname(name)}.html"
            seite.write_text(seite_html(info), encoding="utf-8")
            print(f"  Seite: {seite.name}")
        except Exception as e:
            text = f"# {name}\nFehler beim Abruf: {e}"
            print("  FEHLER:", e)
        (AUSGABE / f"team_{tid}_{dateiname(name)}.md").write_text(text, encoding="utf-8")
        alle.append(text)
    try:
        (AUSGABE / "index.html").write_text(index_html(vereinsspiele()), encoding="utf-8")
        print("  Startseite: index.html")
    except Exception as e:
        print("  Startseite konnte nicht erstellt werden:", e)
    browser_beenden()
    (AUSGABE / "gesamt.md").write_text("\n\n---\n\n".join(alle), encoding="utf-8")
    print(f"\nFertig. Dateien liegen in: {AUSGABE.resolve()}")


if __name__ == "__main__":
    main()
