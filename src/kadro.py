"""Kadro bilgisi: kırmızı kart, oyuncu değişiklikleri ve eksik (sakat/cezalı) oyuncular.

- Kırmızı kart çarpanları 3.644 kırmızılı maçtan ölçüldü (data/gecmis): kırmızıdan sonra
  rakibi eksik kalan takım ~1.77 kat, eksik kalan takım ~0.53 kat gol atıyor.
- Değişiklik etkisi için henüz geçmiş veri yok: hafif bir ön kabul kullanılır ve
  her örnekte kaydedilir; gece eğitimi gerçek etkiyi ölçecek.
- Eksik oyuncular bilgi amaçlıdır: maç öncesi oranlar (modelin başlangıç noktası) bunu zaten fiyatlar.
"""
from __future__ import annotations

import os

RED_ADV = float(os.environ.get("KIRMIZI_AVANTAJ", "1.77"))
RED_DIS = float(os.environ.get("KIRMIZI_EKSIK", "0.53"))
SUB_STEP = float(os.environ.get("DEGISIKLIK_ETKI", "0.05"))   # net hücum değişikliği başına
SUB_CAP = 0.12
POS_VAL = {"G": 0, "D": 1, "M": 2, "F": 3}
POS_AD = {"G": "kaleci", "D": "defans", "M": "orta saha", "F": "forvet"}


# ------------------------------------------------------------------ kırmızı kart
def red_counts(m) -> tuple[int, int]:
    """Kırmızı kart sayısı: olaylar ve istatistikten büyük olanı."""
    ev = {"home": 0, "away": 0}
    hid = getattr(m, "home_id", None)
    for e in getattr(m, "events", None) or []:
        if e.get("type") == "Card" and "red" in (e.get("detail") or "").lower():
            side = "home" if (e.get("team") or {}).get("id") == hid else "away"
            ev[side] += 1
    st = m.snaps[-1]["stats"] if getattr(m, "snaps", None) else None
    h = max(ev["home"], int(st["home"]["red"]) if st else 0)
    a = max(ev["away"], int(st["away"]["red"]) if st else 0)
    return h, a


def red_mults(m) -> tuple[float, float]:
    h, a = red_counts(m)
    d = h - a
    if d == 0:
        return 1.0, 1.0
    adv, dis = RED_ADV ** min(abs(d), 2) ** 0.5, RED_DIS ** min(abs(d), 2) ** 0.5
    return (dis, adv) if d > 0 else (adv, dis)


# ------------------------------------------------------------------ değişiklikler
def _lineup_maps(m):
    pos, start = {}, {"home": set(), "away": set()}
    hid = getattr(m, "home_id", None)
    for lu in getattr(m, "lineups", None) or []:
        side = "home" if (lu.get("team") or {}).get("id") == hid else "away"
        for key in ("startXI", "substitutes"):
            for p in lu.get(key) or []:
                pl = p.get("player") or {}
                if pl.get("id") is not None:
                    pos[pl["id"]] = pl.get("pos")
                    if key == "startXI":
                        start[side].add(pl["id"])
    return pos, start


def substitutions(m) -> list[dict]:
    """Oyundan çıkan/giren ve mevkileri. Kadro yoksa boş liste."""
    pos, onfield = _lineup_maps(m)
    if not pos:
        return []
    hid = getattr(m, "home_id", None)
    subs = [e for e in getattr(m, "events", None) or [] if (e.get("type") or "").lower() == "subst"]
    subs.sort(key=lambda e: ((e.get("time") or {}).get("elapsed") or 0, (e.get("time") or {}).get("extra") or 0))
    out = []
    for e in subs:
        side = "home" if (e.get("team") or {}).get("id") == hid else "away"
        a, b = (e.get("player") or {}).get("id"), (e.get("assist") or {}).get("id")
        # API'de çıkan/giren sırası net değil: sahada olan çıkandır
        if a in onfield[side]:
            out_id, in_id = a, b
        elif b in onfield[side]:
            out_id, in_id = b, a
        else:
            continue
        onfield[side].discard(out_id)
        onfield[side].add(in_id)
        po, pi = pos.get(out_id), pos.get(in_id)
        delta = (POS_VAL.get(pi, 2) - POS_VAL.get(po, 2)) if po and pi else 0
        pa, pb = e.get("player") or {}, e.get("assist") or {}
        out.append({"side": side, "minute": (e.get("time") or {}).get("elapsed") or 0,
                    "out": po, "in": pi, "delta": delta,
                    "out_name": (pa if out_id == a else pb).get("name"),
                    "in_name": (pb if out_id == a else pa).get("name")})
    return out


def sub_shift(m, since: int = 46) -> dict:
    """Takım başına net hücum değişikliği (+: daha hücumcu, -: daha savunmacı).
    İlk yarı değişiklikleri genelde sakatlık kaynaklı olduğundan varsayılan olarak sayılmaz."""
    net = {"home": 0, "away": 0}
    for s in substitutions(m):
        if s["minute"] >= since and s["delta"]:
            net[s["side"]] += 1 if s["delta"] > 0 else -1
    return net


def sub_mults(m) -> tuple[float, float]:
    net = sub_shift(m)
    f = lambda n: 1 + max(-SUB_CAP, min(SUB_CAP, SUB_STEP * n))
    return f(net["home"]), f(net["away"])


def goal_mults(m) -> tuple[float, float]:
    """Ev ve deplasman için gol hızı çarpanı (kırmızı kart × değişiklik)."""
    rh, ra = red_mults(m)
    sh, sa = sub_mults(m)
    return rh * sh, ra * sa


# ------------------------------------------------------------------ mesaj satırları
def info_lines(m, injuries: dict | None = None) -> list[str]:
    out = []
    h, a = red_counts(m)
    if h != a:
        who = m.home if h > a else m.away
        out.append(f"🟥 {who} {11 - abs(h - a)} kişi")
    net = sub_shift(m)
    parts = []
    for side, name in (("home", m.home), ("away", m.away)):
        n = net[side]
        if n > 0:
            parts.append(f"{name} hücuma çıktı (+{n})")
        elif n < 0:
            parts.append(f"{name} savunmaya çekildi ({n})")
    if parts:
        out.append("🔄 Değişiklik: " + " • ".join(parts))
    if injuries:
        txt = injuries_text(injuries, m)
        if txt:
            out.append(txt)
    return out


# ------------------------------------------------------------------ eksik oyuncular
class InjuryBook:
    """Maç başına eksik oyuncular ve ligin gol/asist krallığı (günlük önbellek, bellekte)."""

    def __init__(self, api):
        self.api = api
        self.fixtures: dict[int, dict] = {}
        self.stars: dict[tuple, dict] = {}

    def league_stars(self, league_id, season) -> dict:
        key = (league_id, season)
        if key not in self.stars:
            st = {}
            try:
                for path in ("players/topscorers", "players/topassists"):
                    for p in self.api.get(path, league=league_id, season=season)[:20]:
                        pl, s = p.get("player") or {}, (p.get("statistics") or [{}])[0]
                        g = (s.get("goals") or {})
                        cur = st.setdefault(pl.get("id"), {"gol": 0, "asist": 0})
                        cur["gol"] = max(cur["gol"], g.get("total") or 0)
                        cur["asist"] = max(cur["asist"], g.get("assists") or 0)
            except Exception as e:
                print("  [kadro] krallık alınamadı:", e)
            self.stars[key] = st
        return self.stars[key]

    def get(self, m) -> dict:
        if m.id in self.fixtures:
            return self.fixtures[m.id]
        res = {"home": [], "away": []}
        try:
            stars = self.league_stars(m.league_id, getattr(m, "season", None)) if getattr(m, "season", None) else {}
            for it in self.api.get("injuries", fixture=m.id):
                pl = it.get("player") or {}
                if (pl.get("type") or "") != "Missing Fixture":
                    continue
                side = "home" if (it.get("team") or {}).get("id") == m.home_id else "away"
                star = stars.get(pl.get("id"))
                res[side].append({"ad": pl.get("name"), "neden": pl.get("reason"),
                                  "gol": star["gol"] if star else 0, "asist": star["asist"] if star else 0,
                                  "yildiz": bool(star)})
        except Exception as e:
            print("  [kadro] eksikler alınamadı:", e)
        self.fixtures[m.id] = res
        return res


def injuries_text(inj: dict, m) -> str | None:
    parts = []
    for side, name in (("home", m.home), ("away", m.away)):
        lst = inj.get(side) or []
        if not lst:
            continue
        stars = sorted((p for p in lst if p["yildiz"]), key=lambda p: -(p["gol"] + p["asist"]))
        s = f"{name} {len(lst)}"
        if stars:
            p = stars[0]
            s += f" ({p['ad']} ⭐ {p['gol']} gol {p['asist']} asist)"
        parts.append(s)
    return ("🚑 Eksik: " + " • ".join(parts)) if parts else None
