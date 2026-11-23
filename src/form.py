"""Takım formu: iki takımın bu sezondaki son 10 maçında ve son iki sezonda kendi aralarında oynadıkları maçlarda,
verilen tahmine benzer sonuç kaç kez çıkmış?

Veri günde bir kez takım başına (2 istek) ve eşleşme başına (2 istek) çekilir, data/form/{gün}-v4.json'da saklanır.
"""
from __future__ import annotations

import json
import pathlib

FORM_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "form"
LAST_N = 10          # bu sezonun son N maçı (sezon başında azsa önceki maçlarla tamamlanır)
MIN_SEASON = 5       # sezonda bundan az maç varsa son N maç (sezon fark etmeksizin) alınır
H2H_N = 10
DONE = {"FT", "AET", "PEN"}


def _num(v):
    try:
        return float(str(v).replace("%", "")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def summarize(fx: dict, team_id: int) -> dict | None:
    """Bitmiş bir maçın form özetini çıkarır."""
    g = fx.get("goals") or {}
    if g.get("home") is None or g.get("away") is None:
        return None
    home = fx["teams"]["home"]["id"] == team_id
    gf, ga = (g["home"], g["away"]) if home else (g["away"], g["home"])
    ht = (fx.get("score") or {}).get("halftime") or {}
    ht_total = None if ht.get("home") is None else ht["home"] + (ht.get("away") or 0)
    if ht.get("home") is None:
        iy_gf = iy_ga = None
    else:
        iy_gf, iy_ga = (ht["home"], ht.get("away") or 0) if home else (ht.get("away") or 0, ht["home"])
    corners = cards = None
    stats = fx.get("statistics") or []
    if len(stats) == 2:
        c = k = 0.0
        for block in stats:
            for st in block.get("statistics") or []:
                t = st.get("type")
                if t == "Corner Kicks":
                    c += _num(st.get("value"))
                elif t in ("Yellow Cards", "Red Cards"):
                    k += _num(st.get("value"))
        corners, cards = (c if c > 0 else None), k
    gol_dk = sorted(((e.get("time") or {}).get("elapsed") or 0) for e in fx.get("events") or []
                    if (e.get("type") or "").lower() == "goal" and "missed" not in (e.get("detail") or "").lower())
    if len(gol_dk) != gf + ga:  # olay listesi eksik/tutarsızsa dakika ölçütü kullanılmaz
        gol_dk = None
    return {"id": fx["fixture"]["id"], "gf": gf, "ga": ga, "toplam": gf + ga, "iy": ht_total, "gol_dk": gol_dk,
            "iy_gf": iy_gf, "iy_ga": iy_ga,
            "kg": gf > 0 and ga > 0, "korner": corners, "kart": cards}


class FormBook:
    def __init__(self, api, day: str):
        self.api = api
        self.path = FORM_DIR / f"{day}-v4.json"
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}
        for old in FORM_DIR.glob("*.json"):  # eski günleri temizle
            if old.name < f"{day}-v4.json" or not old.name.endswith("-v4.json"):
                try:
                    old.unlink()
                except OSError:
                    pass

    def _save(self):
        try:
            FORM_DIR.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False))
        except OSError:
            pass

    def _summaries(self, fixtures: list, team_id: int) -> list[dict]:
        ids = [str(f["fixture"]["id"]) for f in fixtures]
        full = self.api.get("fixtures", ids="-".join(ids)) if ids else []
        by_id = {f["fixture"]["id"]: f for f in full}
        out = []
        for f in fixtures:
            s = summarize(by_id.get(f["fixture"]["id"], f), team_id)
            if s:
                s["sezon"] = (f.get("league") or {}).get("season")
                out.append(s)
        return out

    def team(self, team_id: int, season=None) -> list[dict]:
        """Takımın bu sezondaki (tüm turnuvalar) son LAST_N bitmiş maçı, yeniden eskiye."""
        key = f"{team_id}:{season}" if season else str(team_id)
        if key in self.data:
            return self.data[key]
        try:
            fx = []
            if season:
                fx = [f for f in self.api.get("fixtures", team=team_id, season=season)
                      if f["fixture"]["status"]["short"] in DONE]
                fx.sort(key=lambda f: -(f["fixture"].get("timestamp") or 0))
            if len(fx) < MIN_SEASON:
                fx = self.api.get("fixtures", team=team_id, last=LAST_N)
            out = self._summaries(fx[:LAST_N], team_id)
        except Exception as e:  # form yoksa sinyal yine de çalışır
            print("  [form] veri alınamadı:", team_id, e)
            return []
        self.data[key] = out
        self._save()
        return out

    def h2h(self, home_id: int, away_id: int, season=None) -> list[dict]:
        """İki takımın son iki sezonda (bu ve geçen) kendi aralarında oynadığı maçlar, ev sahibinin gözünden."""
        key = f"h2h:{home_id}-{away_id}:{season}"
        if key in self.data:
            return self.data[key]
        try:
            fx = [f for f in self.api.get("fixtures/headtohead", h2h=f"{home_id}-{away_id}", last=20)
                  if f["fixture"]["status"]["short"] in DONE]
            if season:
                fx = [f for f in fx if (f.get("league") or {}).get("season") in (season, season - 1)]
            fx.sort(key=lambda f: -(f["fixture"].get("timestamp") or 0))
            out = self._summaries(fx[:H2H_N], home_id)
        except Exception as e:
            print("  [form] aralarındaki maçlar alınamadı:", home_id, away_id, e)
            return []
        self.data[key] = out
        self._save()
        return out


def hit(tur: str, line, s: dict) -> bool | None:
    """Bu geçmiş maçta tahmine benzer sonuç çıkmış mı? Veri yoksa None."""
    if tur == "gol":
        return s["toplam"] > line
    if tur == "alt":
        return s["toplam"] < line
    if tur == "iy":
        return None if s["iy"] is None else s["iy"] > line
    if tur == "kg":
        return s["kg"]
    if tur == "korner":
        return None if s["korner"] is None else s["korner"] > line
    if tur == "kart":
        return None if s["kart"] is None else s["kart"] > line
    if tur == "gol_sonra":  # bu dakikadan sonra (herhangi bir takımdan) gol olmuş mu
        return None if s.get("gol_dk") is None else any(g > line for g in s["gol_dk"])
    if tur == "alt_sonra":  # bu dakikadan sonra hiç gol olmamış mı
        return None if s.get("gol_dk") is None else not any(g > line for g in s["gol_dk"])
    if tur == "kg_atar":  # gol atması gereken takım gol atmış mı
        return s["gf"] > 0
    if tur == "baski":  # baskıyı kuran takım alarmın verildiği yarıda gol atmış mı
        if s.get("iy_gf") is None:
            return None
        if line == "1H":
            return s["iy_gf"] > 0
        return s["gf"] - s["iy_gf"] > 0
    return None


TUR_ETIKET = {"gol": "{l} üst", "alt": "{l} alt", "iy": "ilk yarı {l} üst", "kg": "KG var",
              "korner": "korner {l} üst", "kart": "kart {l} üst", "baski": "{l} gol attı",
              "gol_sonra": "{l}. dakikadan sonra gol oldu", "alt_sonra": "{l}. dakikadan sonra gol olmadı",
              "kg_atar": "gol attı"}


def fmt_l(x) -> str:
    return f"{float(x):.1f}"


H2H_TURLER = {"gol", "alt", "iy", "kg", "korner", "kart", "gol_sonra", "alt_sonra"}


def check(book: FormBook, tur: str, line, home: tuple, away: tuple, only=None, season=None) -> dict | None:
    """home/away: (takım_id, ad). only: 'home'/'away' ise sadece o takım (baskı alarmı).
    Dönüş: {'hits','n','text','ratio','teams','h2h'}; yeterli veri yoksa None."""
    sides = [("home", home), ("away", away)]
    if only:
        sides = [s for s in sides if s[0] == only]
    parts, hits, n, teams = [], 0, 0, []
    for _, (tid, name) in sides:
        if not tid:
            continue
        games = book.team(tid, season)
        res = [hit(tur, line, s) for s in games]
        res = [r for r in res if r is not None]
        if not res:
            continue
        h = sum(res)
        hits += h
        n += len(res)
        teams.append((name, h, len(res)))
        part = f"{name} {h}/{len(res)}"
        if tur in ("baski", "kg", "kg_atar", "iy"):
            ms = [s for s in games if s.get("iy_gf") is not None]
            if ms:
                g = sum(s["gf"] for s in ms)
                g1 = sum(s["iy_gf"] for s in ms)
                part += f" (son {len(ms)} maçta {g} gol: İY {g1}, 2Y {g - g1})"
        parts.append(part)
    if n < 3:
        return None
    if tur == "baski":
        label = TUR_ETIKET[tur].format(l="ilk yarıda" if line == "1H" else "ikinci yarıda")
    elif tur in ("gol_sonra", "alt_sonra"):
        label = TUR_ETIKET[tur].format(l=int(line))
    else:
        label = TUR_ETIKET[tur].format(l=fmt_l(line) if line is not None else "")
    text = f"📋 Bu sezon son {max(k for _, _, k in teams)} maç ({label}): " + " • ".join(parts)
    hh = None
    if tur in H2H_TURLER and not only and home[0] and away[0]:
        try:
            hr = [hit(tur, line, s) for s in book.h2h(home[0], away[0], season)]
            hr = [r for r in hr if r is not None]
        except Exception:
            hr = []
        if hr:
            hh = (sum(hr), len(hr))
            text += f"\n🤝 Aralarındaki son {len(hr)} maç (2 sezon): {hh[0]}/{hh[1]}"
    return {"hits": hits, "n": n, "ratio": hits / n, "teams": teams, "text": text, "h2h": hh}
