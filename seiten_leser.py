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
        self.hist_tab = None   # Reiter, der die Spiele nachlädt (wird beim ersten Team ermittelt)

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

    def _matches_aus_antworten(self, gesammelt):
        res = {}
        for url, body in gesammelt:
            for m in (body.get("data") if isinstance(body, dict) else None) or []:
                if isinstance(m, dict) and m.get("id") and m.get("local") and m.get("visitor"):
                    res[m["id"]] = m
        return res

    def _weitere_reiter(self):
        """Klickt alle Reiter außer TABELLE/KADER an, damit nachgeladene Spiele (Historie) mitgelesen werden."""
        namen = [n.strip() for n in self.page.get_by_role("tab").all_text_contents()]
        print(f"    Reiter: {namen}")
        if self.hist_tab in namen:
            kandidaten = [self.hist_tab]          # Reiter ist schon bekannt: nur diesen einen klicken
        else:
            kandidaten = [n for n in namen if n and n.upper() not in ("TABELLE", "KADER")]
        for n in kandidaten:
            tab = self.page.get_by_role("tab", name=re.compile(rf"^{re.escape(n)}$", re.I))
            try:
                with self.page.expect_response(
                        lambda r: r.status == 200 and r.url.split("?")[0].endswith("/matches"), timeout=4000):
                    try:
                        tab.first.click(timeout=3000)
                    except Exception:
                        tab.first.dispatch_event("click")
                if self.hist_tab != n:
                    print(f"    Reiter mit Spielen gefunden: {n} (wird ab jetzt direkt geklickt)")
                self.hist_tab = n
                break
            except Exception:
                continue

    # ------------------------------------------------------------ Daten
    def team(self, tid, tabelle=True, historie=True):
        """Gibt {'spiele': [...], 'tabelle': [...] oder None, 'kader': [...]} zurück (mit Zwischenspeicher).
        historie=False: keine Reiter zum Nachladen vergangener Spiele klicken (für Gegner, deren Spiele
        kommen von der Liga-Seite)."""
        c = self.cache.get(tid)
        if c and (c["tabelle"] is not None or not tabelle) and (c.get("hist") or not historie):
            return c
        if self.page is None:
            self._start()
        time.sleep(self.pause)
        gesammelt = []

        def _h(r):
            try:
                if r.status == 200 and r.url.split("?")[0].endswith("/matches"):
                    gesammelt.append((r.url, r.json()))
            except Exception:
                pass

        self.page.on("response", _h)
        self._seite_laden(f"https://www.handball.net/team/{tid}?season_id={self.season_id}")
        self._banner()
        html_spiele = {m["id"]: m for m in spiele_aus_html(self.page.content())}
        if historie:
            self._weitere_reiter()
        self.page.remove_listener("response", _h)
        html_spiele.update(self._matches_aus_antworten(gesammelt))
        spiele = list(html_spiele.values())
        print(f"    /matches-Abfragen: {[u for u, _ in gesammelt]}")

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
                           "kader": kader, "hist": bool(historie or (c or {}).get("hist"))}
        print(f"    Team {tid}: {len(spiele)} Spiele, Tabelle: {len(tab_liste) if tab_liste else '-'}, Kader: {len(kader)}")
        return self.cache[tid]

    def liga_seite(self, comp_id, phase_id):
        """Alle Spiele einer Liga von der Liga-Seite: eine Seite, dann die Spieltage durchklicken."""
        if self.page is None:
            self._start()
        gesammelt = []

        def _h(r):
            try:
                if r.status == 200 and r.url.split("?")[0].endswith("/matches"):
                    gesammelt.append((r.url, r.json()))
            except Exception:
                pass

        self.page.on("response", _h)
        try:
            time.sleep(self.pause)
            self._seite_laden(f"https://www.handball.net/ligen/{comp_id}?phaseId={phase_id}&season_id={self.season_id}")
            self._banner()
            res = {m["id"]: m for m in spiele_aus_html(self.page.content())}
            chips = self.page.get_by_text(re.compile(r"^\s*\d+\.\s*Spieltag\s*$"))
            n = chips.count()
            print(f"    Liga {comp_id}: {n} Spieltag-Schaltflächen, {len(res)} Spiele im HTML")
            for i in range(n):
                el = chips.nth(i)
                try:
                    with self.page.expect_response(
                            lambda r: r.status == 200 and r.url.split("?")[0].endswith("/matches"), timeout=4000):
                        try:
                            el.click(timeout=3000)
                        except Exception:
                            el.dispatch_event("click")
                except Exception:
                    pass   # kam keine neue Antwort (z. B. der Spieltag war schon geladen)
        finally:
            self.page.remove_listener("response", _h)
        res.update(self._matches_aus_antworten(gesammelt))
        passend = [m for m in res.values() if (m.get("phase") or {}).get("id") == phase_id]
        fertig = sum(1 for m in passend if (m.get("status") or {}).get("is_finished"))
        print(f"    Liga {comp_id}: {len(res)} Spiele gelesen, {len(passend)} in Phase {phase_id}, davon beendet: {fertig}")
        print(f"    Liga {comp_id}: {len(gesammelt)} /matches-Antworten, Adressen (Auszug): {sorted({u for u, _ in gesammelt})[:4]}")
        return passend

    def liga_spiele(self, tid, phase_id):
        """Alle Spiele der Liga, von der Liga-Seite. Nur wenn das nicht klappt: Teamseiten der Gegner."""
        eigene = self.team(tid, tabelle=False)["spiele"]
        comp_id = next(((m["phase"].get("competition") or {}).get("id") for m in eigene
                        if (m.get("phase") or {}).get("id") == phase_id
                        and (m["phase"].get("competition") or {}).get("id")), None)
        if comp_id:
            spiele = self.liga_seite(comp_id, phase_id)
            if spiele:
                return spiele
            print("    Liga-Seite lieferte keine Spiele, weiche auf die Teamseiten der Gegner aus.")
        else:
            print("    Keine Liga-ID in den Spieldaten gefunden, nutze die Teamseiten der Gegner.")
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
