"""
Liest Spiele aus den Teamseiten von handball.net, statt die Schnittstelle selbst abzufragen.
Die Seite liefert ihre Daten serverseitig im HTML mit (Angular TransferState). Pro Mannschaft
wird genau eine normale Seite geöffnet. Kein eigener API-Abruf, kein x-client-token.

Nutzung im Hauptskript:
    from seiten_daten import vereinsspiele_aus_seiten
    ... vereinsspiele_aus_seiten(VEREIN_TEAMS, VEREIN_ID, SEASON_ID)
"""
import json
import re
import time
from pathlib import Path

PAUSE = 1.5          # Sekunden zwischen zwei Seitenaufrufen
HEADLESS = True      # bei Problemen auf False setzen
ROH = Path("ausgabe") / "roh"

_ANGULAR = [("&q;", '"'), ("&s;", "'"), ("&l;", "<"), ("&g;", ">"), ("&a;", "&")]


def _entpacken(text):
    try:
        return json.loads(text)
    except Exception:
        for a, b in _ANGULAR:
            text = text.replace(a, b)
        return json.loads(text)


def _eintraege(obj):
    if isinstance(obj, dict):
        if "b" in obj and "u" in obj:
            yield obj
            return
        for v in obj.values():
            yield from _eintraege(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _eintraege(v)


def spiele_aus_html(html):
    """Alle Spiele, die im eingebetteten Datenblock der Seite stecken (doppelte werden entfernt)."""
    spiele = {}
    for text in re.findall(r"<script[^>]*type=\"application/json\"[^>]*>(.*?)</script>", html, re.S):
        try:
            daten = _entpacken(text)
        except Exception:
            continue
        for e in _eintraege(daten):
            if not str(e.get("u", "")).split("?")[0].endswith("/matches"):
                continue
            body = e.get("b")
            for m in (body.get("data") if isinstance(body, dict) else None) or []:
                if isinstance(m, dict) and m.get("id") and m.get("local") and m.get("visitor"):
                    spiele[m["id"]] = m
    return list(spiele.values())


def _zahl(x):
    if isinstance(x, dict):
        for k in ("goals", "total", "score", "final"):
            if k in x:
                return x[k]
        return None
    return x


def _ergebnis(m):
    r = m.get("result") or {}
    l, v = _zahl(r.get("local")), _zahl(r.get("visitor"))
    return f"{l}:{v}" if l is not None and v is not None else "-"


def _eintrag(m, verein_id, team_ids):
    loc, vis = m["local"], m["visitor"]
    status = m.get("status") or {}
    phase = m.get("phase") or {}
    comp = (phase.get("competition") or {}).get("name") or phase.get("name", "")
    return {"id": m.get("id"), "d": (m.get("date") or "")[:16], "l": loc["name"], "v": vis["name"],
            "hl": (loc.get("club") or {}).get("id") == verein_id or loc.get("id") in team_ids,
            "hv": (vis.get("club") or {}).get("id") == verein_id or vis.get("id") in team_ids,
            "e": _ergebnis(m), "f": bool(status.get("is_finished")),
            "o": (m.get("field") or {}).get("name", ""), "c": comp.replace("BIHF ", "")}


def vereinsspiele_aus_seiten(verein_teams, verein_id, season_id):
    """verein_teams: {team_id: name}. Gibt dieselbe Liste zurück wie vereinsspiele() im Hauptskript."""
    from playwright.sync_api import sync_playwright
    ROH.mkdir(parents=True, exist_ok=True)
    alle, team_ids = {}, set(verein_teams)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        page = browser.new_context(locale="de-DE").new_page()
        for tid, name in verein_teams.items():
            url = f"https://www.handball.net/team/{tid}?season_id={season_id}"
            try:
                time.sleep(PAUSE)
                page.goto(url, wait_until="networkidle")
                spiele = spiele_aus_html(page.content())
                print(f"  {name}: {len(spiele)} Spiele")
                for m in spiele:
                    alle[m["id"]] = m
            except Exception as e:
                print(f"  {name}: FEHLER {e}")
        browser.close()
    (ROH / "verein_spiele_seiten.json").write_text(json.dumps(list(alle.values()), ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
    eintraege = sorted((_eintrag(m, verein_id, team_ids) for m in alle.values()), key=lambda x: x["d"])
    print(f"  {len(eintraege)} Spiele des Vereins")
    return eintraege


if __name__ == "__main__":
    # Schnelltest mit zwei Mannschaften
    ergebnis = vereinsspiele_aus_seiten({86611: "Erste", 83554: "Zweite"}, "1fwspnh", 2627)
    for z in ergebnis[:5]:
        print(z)
