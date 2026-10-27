"""
Liest die von Angular in die Seite eingebetteten Daten (TransferState) aus einer gespeicherten
Teamseite und listet auf, welche Abfragen mit welchen Antworten darin stecken.
Kein Netzwerk, kein Browser nötig.

Aufruf:  python state_lesen.py [pfad/zur/seite.html]
Standard: ausgabe/erkundung2/team_seite.html
Ergebnis: ausgabe/erkundung2/state_uebersicht.txt und state/*.json
"""
import json
import re
import sys
from pathlib import Path

QUELLE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ausgabe/erkundung2/team_seite.html")
ZIEL = QUELLE.parent / "state"

# Angular ersetzt Zeichen im eingebetteten JSON durch Kürzel
ANGULAR = [("&q;", '"'), ("&s;", "'"), ("&l;", "<"), ("&g;", ">"), ("&a;", "&")]


def entpacken(text):
    try:
        return json.loads(text)
    except Exception:
        for a, b in ANGULAR:
            text = text.replace(a, b)
        return json.loads(text)


def eintraege(obj, pfad=""):
    """Sucht rekursiv nach Einträgen mit Antwort-Inhalt ('b') und Adresse ('u')."""
    if isinstance(obj, dict):
        if "b" in obj and "u" in obj:
            yield pfad, obj
            return
        for k, v in obj.items():
            yield from eintraege(v, f"{pfad}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from eintraege(v, f"{pfad}[{i}]")


def main():
    html = QUELLE.read_text(encoding="utf-8")
    bloecke = re.findall(r"<script[^>]*type=\"application/json\"[^>]*>(.*?)</script>", html, re.S)
    zeilen = [f"Datei: {QUELLE}  ({len(html)} Zeichen), JSON-Blöcke: {len(bloecke)}"]
    if not bloecke:
        zeilen.append("Kein application/json-Block gefunden.")
    ZIEL.mkdir(parents=True, exist_ok=True)
    n = 0
    for i, text in enumerate(bloecke, 1):
        zeilen.append(f"\nBlock {i}: {len(text)} Zeichen, Anfang: {text[:120]!r}")
        try:
            daten = entpacken(text)
        except Exception as e:
            zeilen.append(f"  nicht lesbar: {e}")
            continue
        for pfad, e in eintraege(daten):
            n += 1
            body = e["b"]
            if isinstance(body, dict):
                inhalt = body.get("data")
                anzahl = len(inhalt) if isinstance(inhalt, list) else "-"
                schluessel = list(body.keys())[:6]
            else:
                anzahl, schluessel = "-", type(body).__name__
            extra = ""
            if isinstance(body, dict) and isinstance(body.get("data"), list) and body["data"] \
                    and isinstance(body["data"][0], dict) and "date" in body["data"][0]:
                daten_liste = sorted((m.get("date") or "")[:10] for m in body["data"])
                fertig = sum(1 for m in body["data"] if (m.get("status") or {}).get("is_finished"))
                phasen = sorted({(m.get("phase") or {}).get("id") for m in body["data"]}, key=str)
                extra = f"  Zeitraum {daten_liste[0]} bis {daten_liste[-1]}, beendet: {fertig}, Phasen: {phasen}"
            zeilen.append(f"  {e.get('u')}  Status {e.get('s')}  Einträge: {anzahl}  Felder: {schluessel}{extra}")
            (ZIEL / f"{n:02d}.json").write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")
    zeilen.append(f"\n{n} Abfrage-Antworten gefunden.")

    # Links zu Unterseiten (Tabelle, Kader usw.)
    links = sorted(set(re.findall(r'href="([^"]*)"', html)))
    team_links = [l for l in links if re.search(r"/team/\d+", l) or "tabelle" in l.lower()
                  or "standing" in l.lower() or "kader" in l.lower() or "roster" in l.lower()]
    zeilen.append("\nLinks mit Bezug zu Team/Tabelle/Kader:")
    zeilen += [f"  {l}" for l in team_links[:40]]

    # Textstellen rund um 'standings'
    zeilen.append("\nStellen rund um 'standings' (je 160 Zeichen, die ersten 4 verschiedenen):")
    gesehen = set()
    for m in re.finditer("standings", html):
        stelle = html[max(0, m.start() - 80): m.end() + 80].replace("\n", " ")
        if stelle not in gesehen:
            gesehen.add(stelle)
            zeilen.append(f"  ...{stelle}...")
        if len(gesehen) >= 4:
            break

    # Steht die Tabelle / der Kader als Text im HTML?
    zeilen.append(f"\nVorkommen im HTML: 'first_name' {html.count('first_name')}x, "
                  f"'dorsal' {html.count('dorsal')}x, 'goals_for' {html.count('goals_for')}x, "
                  f"'<table' {html.count('<table')}x")
    i = html.find("/team/86609")
    if i >= 0:
        zeilen.append("\nUmgebung des Links /team/86609 (je 500 Zeichen davor und danach):")
        zeilen.append(html[max(0, i - 500): i + 500].replace("\n", " "))
    (QUELLE.parent / "state_uebersicht.txt").write_text("\n".join(zeilen), encoding="utf-8")
    print("\n".join(zeilen))


if __name__ == "__main__":
    main()
