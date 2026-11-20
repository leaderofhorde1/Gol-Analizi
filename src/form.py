"""Takım formu: iki takımın son 5 maçında, verilen tahmine benzer sonuç kaç kez çıkmış?

Veri günde bir kez takım başına çekilir (2 istek) ve data/form/{gün}.json dosyasında saklanır.
"""
from __future__ import annotations

import json
import pathlib

FORM_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "form"
LAST_N = 5


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
    return {"id": fx["fixture"]["id"], "gf": gf, "ga": ga, "toplam": gf + ga, "iy": ht_total,
            "kg": gf > 0 and ga > 0, "korner": corners, "kart": cards}


class FormBook:
    def __init__(self, api, day: str):
        self.api = api
        self.path = FORM_DIR / f"{day}.json"
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}
        for old in FORM_DIR.glob("*.json"):  # eski günleri temizle
            if old.name < f"{day}.json":
                try:
                    old.unlink()
                except OSError:
                    pass

    def team(self, team_id: int) -> list[dict]:
        key = str(team_id)
        if key in self.data:
            return self.data[key]
        out: list[dict] = []
        try:
            last = self.api.get("fixtures", team=team_id, last=LAST_N)
            ids = [str(f["fixture"]["id"]) for f in last]
            full = self.api.get("fixtures", ids="-".join(ids)) if ids else []
            by_id = {f["fixture"]["id"]: f for f in full}
            for f in last:
                s = summarize(by_id.get(f["fixture"]["id"], f), team_id)
                if s:
                    out.append(s)
        except Exception as e:  # form yoksa sinyal yine de çalışır
            print("  [form] veri alınamadı:", team_id, e)
            return []
        self.data[key] = out
        try:
            FORM_DIR.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False))
        except OSError:
            pass
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
    if tur == "baski":  # baskıyı kuran takım gol atmış mı
        return s["gf"] > 0
    return None


TUR_ETIKET = {"gol": "{l} üst", "alt": "{l} alt", "iy": "ilk yarı {l} üst", "kg": "KG var",
              "korner": "korner {l} üst", "kart": "kart {l} üst", "baski": "gol attı"}


def fmt_l(x) -> str:
    return f"{float(x):.1f}"


def check(book: FormBook, tur: str, line, home: tuple, away: tuple, only=None) -> dict | None:
    """home/away: (takım_id, ad). only: 'home'/'away' ise sadece o takım (baskı alarmı).
    Dönüş: {'hits','n','text','ratio'}; yeterli veri yoksa None."""
    sides = [("home", home), ("away", away)]
    if only:
        sides = [s for s in sides if s[0] == only]
    parts, hits, n = [], 0, 0
    for _, (tid, name) in sides:
        if not tid:
            continue
        res = [hit(tur, line, s) for s in book.team(tid)]
        res = [r for r in res if r is not None]
        if not res:
            continue
        h = sum(res)
        hits += h
        n += len(res)
        parts.append(f"{name} {h}/{len(res)}")
    if n < 3:
        return None
    label = TUR_ETIKET[tur].format(l=fmt_l(line) if line is not None else "")
    return {"hits": hits, "n": n, "ratio": hits / n,
            "text": f"📋 Son 5 maç ({label}): " + " • ".join(parts)}
