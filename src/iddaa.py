"""İddaa (iddaa.com) canlı oranları: her sinyalin yanına aynı bahsin iddaa oranı yazılır ve kaydedilir.

Kaynak: iddaa.com'un kendi sitesinin kullandığı veri servisi (sportsbookv2.iddaa.com). Resmî bir geliştirici
servisi değildir; yapısı değişirse bu modül sessizce "alınamadı" döner, sinyaller etkilenmez.

Canlı pazar kodları (t=4):
  st 14  maç sonu Alt/Üst (sov = çizgi)     st 131 karşılıklı gol Var/Yok
  st 63  ilk yarı Alt/Üst (sov = çizgi)     korner ve kart için canlı pazar yok
Pazar s=1 açık, başka değer (ör. -2) = askıda.
"""
from __future__ import annotations

import re
import time
import unicodedata

import requests

BASE = "https://sportsbookv2.iddaa.com/sportsbook"
HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36",
           "Accept": "application/json", "Origin": "https://www.iddaa.com", "Referer": "https://www.iddaa.com/"}
STOP = {"fc", "cf", "sc", "ac", "afc", "cd", "ca", "club", "de", "fk", "sk", "bk", "if", "the", "united", "city",
        "women", "kadin", "k", "w", "u19", "u20", "u21", "u23", "ii", "b", "reserves", "sv", "ss", "us", "as", "rc"}
MARKETS = {"gol": (14, "Üst"), "alt": (14, "Alt"), "kg": (131, "Var"), "iy": (63, "Üst")}


def _norm(name: str) -> set[str]:
    s = unicodedata.normalize("NFKD", (name or "").lower().replace("ı", "i"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    toks = re.findall(r"[a-z0-9]+", s)
    return {t for t in toks if t not in STOP and len(t) > 1} or set(toks)


def _sim(a: str, b: str) -> float:
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return 0.0
    inter = len(x & y)
    # kısaltmalar için önek eşleşmesi (ör. "bud" ~ "budapest", "r" ~ "racing" değil)
    inter += sum(0.7 for t in x - y if len(t) >= 3 and any(u.startswith(t) or t.startswith(u) for u in y - x))
    return inter / min(len(x), len(y))


class Iddaa:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._list, self._list_t = [], 0.0
        self._map: dict[int, tuple[int | None, float]] = {}
        self._ev: dict[int, tuple[dict, float]] = {}
        self.errors = 0

    def _get(self, path: str):
        r = self.s.get(f"{BASE}/{path}", timeout=8)
        r.raise_for_status()
        js = r.json()
        return js.get("data") if isinstance(js, dict) else None

    def live_list(self) -> list[dict]:
        if time.time() - self._list_t > 90:
            data = self._get("events?st=1&type=1&version=0") or {}
            self._list, self._list_t = data.get("events") or [], time.time()
        return self._list

    def find(self, m) -> int | None:
        """API-Football maçını iddaa canlı bültenindeki maçla eşleştir (takım adları + başlama saati)."""
        hit = self._map.get(m.id)
        if hit and (hit[0] or time.time() - hit[1] < 600):
            return hit[0]
        best, score = None, 0.0
        ko = m.kickoff.timestamp() if getattr(m, "kickoff", None) else None
        for e in self.live_list():
            d = e.get("d") or 0
            d = d / 1000 if d > 1e11 else d
            if ko and d and abs(d - ko) > 3 * 3600:
                continue
            sc = min(_sim(m.home, e.get("hn", "")), _sim(m.away, e.get("an", "")))
            if sc > score:
                best, score = e.get("i"), sc
        eid = best if score >= 0.5 else None
        self._map[m.id] = (eid, time.time())
        return eid

    def event(self, eid: int) -> dict:
        c = self._ev.get(eid)
        if c and time.time() - c[1] < 20:
            return c[0]
        d = self._get(f"event/{eid}") or {}
        self._ev[eid] = (d, time.time())
        return d

    def lookup(self, m, tur: str, line) -> dict:
        """{'oran': float|None, 'durum': 'var'|'askıda'|'yok'|'eşleşmedi'|'hata', 'iddaa_id': ...}"""
        if tur not in MARKETS:
            return {"oran": None, "durum": "yok"}
        try:
            eid = self.find(m)
            if not eid:
                return {"oran": None, "durum": "eşleşmedi"}
            st, pick = MARKETS[tur]
            want = None if tur == "kg" else (f"{float(line):g}" if line not in (None, "") else None)
            for mk in self.event(eid).get("m") or []:
                if mk.get("st") != st:
                    continue
                if want is not None and str(mk.get("sov")) != want:
                    continue
                for o in mk.get("o") or []:
                    if o.get("n") == pick and o.get("odd"):
                        return {"oran": float(o["odd"]), "durum": "var" if mk.get("s") == 1 else "askıda",
                                "iddaa_id": eid}
            return {"oran": None, "durum": "yok", "iddaa_id": eid}
        except Exception as e:
            self.errors += 1
            return {"oran": None, "durum": "hata", "hata": str(e)[:80]}


def line_text(info: dict | None, our_odd=None) -> str | None:
    """Sinyal mesajındaki iddaa satırı."""
    if not info:
        return None
    d = info.get("durum")
    if d == "var":
        txt = f"🇹🇷 İddaa: {info['oran']:.2f}"
        try:
            if our_odd and float(info["oran"]) < float(our_odd) - 0.05:
                txt += f"  (piyasadan düşük, başa baş için %{round(100 / info['oran'])} isabet gerekir)"
        except (TypeError, ValueError):
            pass
        return txt
    if d == "askıda":
        return f"🇹🇷 İddaa: {info['oran']:.2f} (şu an askıda)"
    if d == "yok":
        return "🇹🇷 İddaa'da bu bahis canlıda açık değil"
    if d == "eşleşmedi":
        return "🇹🇷 Maç iddaa canlı bülteninde bulunamadı"
    return None


def short_text(info: dict | None) -> str | None:
    """Mesajın üst satırı için kısa iddaa bilgisi."""
    if not info:
        return None
    d = info.get("durum")
    if d == "var":
        return f"🇹🇷 İddaa: {info['oran']:.2f}"
    if d == "askıda":
        return f"🇹🇷 İddaa: {info['oran']:.2f} (askıda)"
    if d in ("yok", "eşleşmedi"):
        return "🇹🇷 İddaa: yok"
    return None
