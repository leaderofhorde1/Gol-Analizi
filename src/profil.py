"""Takım profilleri: arşivdeki maçlardan takımların kendine özgü alışkanlıkları.

- gec: maç başı 76+ dakika gollerine katılım (attığı + yediği)
- gec_atilan: maç başı 76+ dakika attığı gol
- donus: geriye düştüğü maçlarda puan alma oranı (beraberlik veya galibiyet)
- koruma: 70. dakikada tek farkla öndeyken hiç gol yemeden bitirme oranı
Lig ortalamaları da yazılır. Çıktı: data/model/takim_profil.json
"""
from __future__ import annotations

import gzip
import json
from collections import defaultdict

from main import DATA

ARSIV = DATA / "gecmis" / "maclar.jsonl.gz"
OUT = DATA / "model" / "takim_profil.json"
LATE = 76


def _empty():
    return {"mac": 0, "gec": 0, "gec_atilan": 0, "geri": 0, "geri_puan": 0, "onde70": 0, "onde70_tut": 0}


def build() -> dict:
    teams, leagues = defaultdict(_empty), defaultdict(_empty)
    team_league = {}
    with gzip.open(ARSIV, "rt", encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            goals = sorted(m.get("goller") or [], key=lambda g: (g[0], g[1]))
            for side, tid in (("h", m["ev"]), ("a", m["dep"])):
                own = "h" if side == "h" else "a"
                t, lg = teams[tid], leagues[m["lig"]]
                team_league[tid] = m["lig"]
                for rec in (t, lg):
                    rec["mac"] += 1
                    rec["gec"] += sum(1 for g in goals if g[0] >= LATE)
                    rec["gec_atilan"] += sum(1 for g in goals if g[0] >= LATE and g[2] == own)
                # geriye düştü mü
                s_own = s_opp = 0
                trailed = False
                for g in goals:
                    if g[2] == own:
                        s_own += 1
                    else:
                        s_opp += 1
                    if s_opp > s_own:
                        trailed = True
                fo, fa = (m["gh"], m["ga"]) if side == "h" else (m["ga"], m["gh"])
                if trailed:
                    for rec in (t, lg):
                        rec["geri"] += 1
                        rec["geri_puan"] += 1 if fo >= fa else 0
                # 70'de tek farkla önde
                o70 = sum(1 for g in goals if g[0] <= 70 and g[2] == own)
                a70 = sum(1 for g in goals if g[0] <= 70 and g[2] != own)
                if o70 - a70 == 1:
                    held = fa == a70
                    for rec in (t, lg):
                        rec["onde70"] += 1
                        rec["onde70_tut"] += 1 if held else 0

    def fin(r):
        n = r["mac"] or 1
        return {"mac": r["mac"], "gec": round(r["gec"] / n, 3), "gec_atilan": round(r["gec_atilan"] / n, 3),
                "donus": round(r["geri_puan"] / r["geri"], 3) if r["geri"] else None, "donus_n": r["geri"],
                "koruma": round(r["onde70_tut"] / r["onde70"], 3) if r["onde70"] else None, "koruma_n": r["onde70"]}

    out = {"takim": {str(k): {**fin(v), "lig": team_league[k]} for k, v in teams.items() if v["mac"] >= 10},
           "lig": {str(k): fin(v) for k, v in leagues.items()}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    return out


# ------------------------------------------------------------------ canlı kullanım
K_SHRINK = 25          # az maçlı takımı lig ortalamasına çekme gücü
LATE_CAP = 0.15


class Profiles:
    def __init__(self):
        try:
            self.d = json.loads(OUT.read_text())
        except Exception:
            self.d = {"takim": {}, "lig": {}}

    def team(self, tid):
        return self.d["takim"].get(str(tid))

    def league(self, lid):
        return self.d["lig"].get(str(lid))

    def _shr(self, val, n, base, k=K_SHRINK):
        if val is None or base is None:
            return None
        return (val * n + base * k) / (n + k)

    def late_mult(self, m) -> float:
        """Geç dakikalarda bu iki takımın maçlarında gol temposu lig ortalamasına göre kaç kat?"""
        lg = self.league(m.league_id)
        if not lg or not lg["gec"]:
            return 1.0
        vals = []
        for tid in (m.home_id, m.away_id):
            t = self.team(tid)
            if t and t["mac"] >= 10:
                vals.append(self._shr(t["gec"], t["mac"], lg["gec"]) / lg["gec"])
        if not vals:
            return 1.0
        r = sum(vals) / len(vals)
        return max(1 - LATE_CAP, min(1 + LATE_CAP, 1 + 0.5 * (r - 1)))  # yarısı kadar: piyasa da kısmen biliyor

    def lines(self, m) -> list[str]:
        out = []
        lg = self.league(m.league_id)
        if not lg:
            return out
        for tid, name, own, opp in ((m.home_id, m.home, m.gh, m.ga), (m.away_id, m.away, m.ga, m.gh)):
            t = self.team(tid)
            if not t or t["mac"] < 20:
                continue
            if lg["gec_atilan"] and t["gec_atilan"] >= 1.3 * lg["gec_atilan"] and m.minute >= 60:
                out.append(f"📊 {name} son 15 dk'da maç başı {t['gec_atilan']:.2f} gol atıyor "
                           f"(lig ort. {lg['gec_atilan']:.2f})")
            if own < opp and t.get("donus") is not None and t["donus_n"] >= 10 and lg.get("donus"):
                if t["donus"] >= lg["donus"] + 0.08:
                    out.append(f"📊 {name} geriye düştüğü maçlarda puan alma oranı %{round(t['donus'] * 100)} "
                               f"(lig %{round(lg['donus'] * 100)})")
            if own == opp + 1 and m.minute >= 60 and t.get("koruma") is not None and t["koruma_n"] >= 10 and lg.get("koruma"):
                if t["koruma"] <= lg["koruma"] - 0.08:
                    out.append(f"📊 {name} 70'te tek farkla öndeyken gol yemeden bitirme oranı sadece "
                               f"%{round(t['koruma'] * 100)} (lig %{round(lg['koruma'] * 100)})")
        return out[:2]


if __name__ == "__main__":
    d = build()
    print("takım:", len(d["takim"]), "lig:", len(d["lig"]))
