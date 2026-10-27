"""
Holt Spiele, Tabelle und Kader über die normalen Teamseiten von handball.net, so wie ein Besucher:
  - Spiele:   stecken im HTML der Seite (serverseitig eingebettet)
  - Tabelle:  Reiter TABELLE anklicken, die Antwort der Seite mitlesen
  - Kader:    Reiter KADER anklicken, die Antwort der Seite mitlesen
Es gibt keine eigenen API-Abrufe und kein x-client-token.

Cookie-Banner: Beim ersten Start wird das Browserfenster sichtbar geöffnet und du schließt den
Banner selbst. Die Entscheidung wird im Ordner "browser_profil" gespeichert, danach klappt der
Start auch ohne Fenster (HEADLESS = True).

Aufruf zum Testen:  python seiten_leser.py
"""
import json
import re
import time
from pathlib import Path

from seiten_daten import spiele_aus_html

ROH = Path("ausgabe") / "roh"


class SeitenLeser:
    def __init__(self, season_id, headless=False, profil="browser_profil", pause=1.5):
        self.season_id, self.headless, self.profil, self.pause = season_id, headless, profil, pause
        self.pw = self.ctx = self.page = None
        self.cache = {}

    # ------------------------------------------------------------ Browser
    def _start(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(self.profil, headless=self.headless, locale="de-DE")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def close(self):
        if self.ctx:
            self.ctx.close()
            self.pw.stop()
            self.ctx = self.pw = self.page = None

    def _banner(self):
        try:
            sichtbar = self.page.locator("#cmpbox").is_visible()
        except Exception:
            return
        if not sichtbar:
            return
        if self.headless:
            # Ohne Fenster (z. B. GitHub Actions) kann niemand den Banner schließen. Er bleibt unbeantwortet,
            # es wird also keine Einwilligung erteilt. Die Reiter werden in _tab() direkt am Element angeklickt.
            print("    Hinweis: Cookie-Banner bleibt ungeklickt (kein Fenster).")
            return
        input("\nBitte schließe den Cookie-Banner im Browserfenster und drücke hier Enter ... ")
        time.sleep(1)

    def _seite_laden(self, url, versuche=3):
        """Öffnet die Seite. Nicht auf 'networkidle' warten: Live-Ticker, Werbung und Tracking sorgen dafür,
        dass das Netzwerk nie zur Ruhe kommt. Stattdessen: Seite geladen, Reiter sichtbar, kurz Luft holen."""
        for versuch in range(1, versuche + 1):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                self.page.get_by_role("tab").first.wait_for(timeout=30000)
                try:
                    self.page.wait_for_load_state("networkidle", timeout=8000)   # nur ein Versuch, kein Muss
                except Exception:
                    time.sleep(2)   # Angular braucht einen Moment, bis die Reiter klickbar sind
                return
            except Exception as e:
                print(f"    Seite laden, Versuch {versuch}/{versuche} fehlgeschlagen: {str(e).splitlines()[0]}")
                if versuch == versuche:
                    raise
                time.sleep(5 * versuch)

    def _tab(self, name, url_teil):
        """Klickt einen Reiter an und gibt die JSON-Antwort zurück, die die Seite dabei selbst lädt."""
        tab = self.page.get_by_role("tab", name=re.compile(rf"^{name}$", re.I))
        if not tab.count():
            print(f"    Reiter {name} nicht gefunden")
            return None
        try:
            with self.page.expect_response(lambda r: url_teil in r.url and r.status == 200, timeout=15000) as info:
                try:
                    tab.first.click(timeout=3000)
                except Exception:
                    # Der Cookie-Banner liegt über der Seite und fängt Mausklicks ab: Klick direkt am Element auslösen.
                    tab.first.dispatch_event("click")
            return info.value.json()
        except Exception as e:
            print(f"    Reiter {name}: keine Antwort ({str(e).splitlines()[0]})")
            return None

    # ------------------------------------------------------------ Daten
    def team(self, tid, tabelle=True):
        """Gibt {'spiele': [...], 'tabelle': [...] oder None, 'kader': [...]} zurück (mit Zwischenspeicher)."""
        c = self.cache.get(tid)
        if c and (c["tabelle"] is not None or not tabelle):
            return c
        if self.page is None:
            self._start()
        time.sleep(self.pause)
        self._seite_laden(f"https://www.handball.net/team/{tid}?season_id={self.season_id}")
        self._banner()
        spiele = spiele_aus_html(self.page.content())

        tab_roh = self._tab("TABELLE", "/standings") if tabelle else None
        kader_roh = self._tab("KADER", "/stats/player-stats")
        tab_liste = tab_roh.get("data") if isinstance(tab_roh, dict) else None
        kader = (kader_roh.get("data") if isinstance(kader_roh, dict) else None) or []
        pag = (kader_roh or {}).get("pagination") or {}
        if pag.get("last_page", 1) > 1:
            print(f"    Hinweis: Kader von {tid} hat mehrere Seiten, nur die erste wurde gelesen.")

        ROH.mkdir(parents=True, exist_ok=True)
        if tab_roh is not None:
            (ROH / f"team_{tid}_tabelle.json").write_text(json.dumps(tab_roh, ensure_ascii=False, indent=1), encoding="utf-8")
        if kader_roh is not None:
            (ROH / f"team_{tid}_kader.json").write_text(json.dumps(kader_roh, ensure_ascii=False, indent=1), encoding="utf-8")
        (ROH / f"team_{tid}_spiele.json").write_text(json.dumps(spiele, ensure_ascii=False, indent=1), encoding="utf-8")

        self.cache[tid] = {"spiele": spiele, "tabelle": tab_liste if tabelle else (c or {}).get("tabelle"),
                           "kader": kader}
        print(f"    Team {tid}: {len(spiele)} Spiele, Tabelle: {len(tab_liste) if tab_liste else '-'}, Kader: {len(kader)}")
        return self.cache[tid]

    def liga_spiele(self, tid, phase_id):
        """Alle Spiele der Liga: eigene Spiele plus die Spiele aller Gegner (aus deren Teamseiten)."""
        eigene = self.team(tid, tabelle=False)["spiele"]
        gegner = {m[s]["id"] for m in eigene for s in ("local", "visitor")} - {tid}
        alle = {m["id"]: m for m in eigene}
        for g in sorted(gegner):
            for m in self.team(g, tabelle=False)["spiele"]:
                alle[m["id"]] = m
        return [m for m in alle.values() if (m.get("phase") or {}).get("id") == phase_id]


if __name__ == "__main__":
    leser = SeitenLeser(2627, headless=False)
    try:
        d = leser.team(86611)
        print("\nErste:", len(d["spiele"]), "Spiele,", len(d["tabelle"] or []), "Tabellenzeilen,", len(d["kader"]), "Spieler")
        if d["tabelle"]:
            print("Erste Tabellenzeile:", d["tabelle"][0])
        if d["kader"]:
            print("Erster Spieler:", d["kader"][0])
    finally:
        leser.close()
