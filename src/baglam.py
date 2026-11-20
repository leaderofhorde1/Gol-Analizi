"""Maç bağlamı: rövanş maçlarında toplam skor ve puan durumundaki yarış (şampiyonluk / küme hattı).

- Rövanş: ilk maçın skoru eklenerek "toplam skor durumu" bulunur. Model, skor durumuna göre gol hızını
  (eşit 0.88, 1 fark 1.08, 2+ fark 1.23 — arşivden ölçülmüş) bu toplam üzerinden hesaplar.
- Puan durumu: sezonun son %35'inde şampiyonluk/küme yarışındaki takımlar mesajda belirtilir
  ve kaydedilir; etkisi gece eğitimiyle ölçülecek.
"""
from __future__ import annotations

import re

CUP = re.compile(r"cup|copa|coupe|pokal|coppa|kupa|kupası|champions|europa|conference|libertadores|"
                 r"sudamericana|play-?off|beker|taça|taca|trophy|shield", re.I)
NOT_KO = re.compile(r"group|league stage|league phase|regular season", re.I)


class ContextBook:
    def __init__(self, api):
        self.api = api
        self.aggs: dict[int, tuple | None] = {}
        self.tables: dict[tuple, dict] = {}

    # ---------------------------------------------------------- rövanş
    def first_leg(self, m) -> tuple[int, int] | None:
        """(ev sahibinin ilk maçtaki golü, deplasmanın ilk maçtaki golü) — ikinci maç değilse None."""
        if m.id in self.aggs:
            return self.aggs[m.id]
        res = None
        rnd = getattr(m, "round", "") or ""
        if CUP.search(m.league) and not NOT_KO.search(rnd) and m.home_id and m.away_id:
            try:
                prev = self.api.get("fixtures/headtohead", h2h=f"{m.home_id}-{m.away_id}",
                                    league=m.league_id, season=m.season, last=3)
                for f in prev:
                    if f["fixture"]["id"] == m.id or (f["fixture"]["status"]["short"] not in ("FT", "AET", "PEN")):
                        continue
                    if (f["league"].get("round") or "") != rnd and "2nd" not in rnd.lower():
                        continue
                    g = f.get("goals") or {}
                    if f["teams"]["home"]["id"] == m.home_id:
                        res = (g.get("home") or 0, g.get("away") or 0)
                    else:
                        res = (g.get("away") or 0, g.get("home") or 0)
                    break
            except Exception as e:
                print("  [bağlam] ilk maç alınamadı:", e)
        self.aggs[m.id] = res
        return res

    def state_score(self, m) -> tuple[int, int]:
        """Modelin skor durumu için kullanacağı skor: rövanşta toplam skor, değilse maç skoru."""
        fl = self.aggs.get(m.id)
        if fl:
            return m.gh + fl[0], m.ga + fl[1]
        return m.gh, m.ga

    # ---------------------------------------------------------- puan durumu
    def table(self, league_id, season) -> dict:
        key = (league_id, season)
        if key not in self.tables:
            t = {}
            try:
                resp = self.api.get("standings", league=league_id, season=season)
                groups = (resp[0]["league"].get("standings") or []) if resp else []
                if len(groups) == 1:  # tek tablolu lig
                    rows = groups[0]
                    n = len(rows)
                    for r in rows:
                        t[r["team"]["id"]] = {"sira": r["rank"], "puan": r["points"], "oynanan": r["all"]["played"],
                                              "aciklama": r.get("description") or "", "n": n}
                    t["_satirlar"] = [(r["rank"], r["points"], r.get("description") or "") for r in rows]
            except Exception as e:
                print("  [bağlam] puan durumu alınamadı:", e)
            self.tables[key] = t
        return self.tables[key]

    def race(self, m, side) -> str | None:
        tid = m.home_id if side == "home" else m.away_id
        t = self.table(m.league_id, m.season)
        r = t.get(tid)
        if not r or r["n"] < 8:
            return None
        total = 2 * (r["n"] - 1)
        if r["oynanan"] < 0.65 * total:
            return None
        rows = t["_satirlar"]
        pts = {rk: p for rk, p, _ in rows}
        if r["sira"] <= 2 and abs(r["puan"] - pts.get(2 if r["sira"] == 1 else 1, r["puan"])) <= 3:
            return f"şampiyonluk yarışında ({r['sira']}., {r['puan']} puan)"
        rel = [rk for rk, _, d in rows if "relegation" in d.lower()]
        if rel:
            first_rel = min(rel)
            safe_pts = pts.get(first_rel - 1)
            if r["sira"] >= first_rel:
                return f"küme hattında ({r['sira']}.)"
            if safe_pts is not None and r["puan"] - pts.get(first_rel, 0) <= 3:
                return f"küme hattına yakın ({r['sira']}., hattın {r['puan'] - pts.get(first_rel, 0)} puan üstünde)"
        return None

    def lines(self, m) -> tuple[list[str], str]:
        """Mesaj satırları ve kayıt için kısa bağlam etiketi."""
        out, tags = [], []
        fl = self.aggs.get(m.id)
        if fl:
            ah, aa = m.gh + fl[0], m.ga + fl[1]
            lead = m.home if ah > aa else m.away if aa > ah else None
            out.append(f"🔁 Rövanş: ilk maçta {m.home} {fl[0]}, {m.away} {fl[1]} gol attı • toplam {ah}-{aa}"
                       + (f", şu an {lead} turu geçiyor" if lead else ", toplamda eşitlik"))
            tags.append(f"rovans:{ah}-{aa}")
        for side, name in (("home", m.home), ("away", m.away)):
            try:
                rc = self.race(m, side)
            except Exception:
                rc = None
            if rc:
                out.append(f"🏆 {name} {rc}")
                tags.append(f"{side}:{rc.split(' (')[0]}")
        return out, ";".join(tags)
