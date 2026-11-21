"""Sinyal açıklaması: "Neden bu tahmin?" sorusuna sinyalin kendi verilerinden 1-2 cümlelik cevap.

Açıklama dil modeline yazdırılmaz; sadece hesaplanmış sayılardan kurulur (uydurma bilgi içermez, anında hazır).
"""
from __future__ import annotations


def _num(x, nd=0):
    return f"{x:.{nd}f}".replace(".", ",")


def _stats(d: dict) -> str:
    s = f"{int(d.get('shots', 0))} şut ({int(d.get('sot', 0))} isabetli)"
    if d.get("xg", 0) > 0.05:
        s += f" ve {_num(d['xg'], 2)} xG"
    return s


def _mkt(p, p_mkt, olay="bu sonuca") -> str:
    if p_mkt is None:
        return ""
    return f" Piyasa {olay} %{round(p_mkt * 100)} ihtimal veriyor, verilere göre %{round(p * 100)} olmalı."


def _rem(minute: int, half_end: int = 90) -> int:
    return max(2, half_end + 3 - minute)


def build(tur: str, m, p, p_mkt, **k) -> str:
    """tur: gol, alt, korner, kart, iy, kg, baski. k: d (istatistik farkları), span, team, line, now, f, ..."""
    d, span, team = k.get("d") or {}, int(k.get("span") or 10), k.get("team") or ""
    try:
        if tur == "gol":
            parts = [f"{team} son {span} dk'da {_stats(d)} ile rakip kaleyi zorluyor."]
            own, opp = k.get("own_goals"), k.get("opp_goals")
            if own is not None and opp is not None and own < opp:
                parts.append("Geride olan taraf baskı kuruyor; skoru eşitlemek için risk alacak.")
            if k.get("red"):
                parts.append(k["red"])
            parts.append(f"Kalan ~{_rem(m.minute)} dk'da en az bir gol daha bekleniyor.")
            return " ".join(parts) + _mkt(p, p_mkt, "yeni gole")
        if tur == "alt":
            return (f"Son {span} dk'da iki takım toplam sadece {_stats(d)} üretti; maçın temposu düştü. "
                    f"Kalan ~{_rem(m.minute)} dk'da gol gelmeme ihtimali piyasanın sandığından yüksek."
                    + _mkt(p, p_mkt, "golsüz devama"))
        if tur == "korner":
            return (f"Son {span} dk'da iki takım {_stats(d)} ve {int(d.get('corners', 0))} korner üretti; "
                    f"hücum temposu normalin {_num(k.get('f', 1), 1)} katı. Şu an {k.get('now')} korner var, "
                    f"bu tempoda {_num(k.get('line', 0), 1)} çizgisinin aşılması bekleniyor."
                    + _mkt(p, p_mkt, "Üst'e"))
        if tur == "kart":
            typ = k.get("typical") or 1
            parts = [f"Faul temposu 10 dk'da {_num(k.get('fouls', 0), 1)}, normalin {_num(k.get('fouls', 0) / typ, 1)} katı."]
            if k.get("ref"):
                parts.append(k["ref"][:1].upper() + k["ref"][1:] + ".")
            if k.get("late_close"):
                parts.append("Skor yakın ve maç sona yaklaşıyor; bu dönemde kartlar artar.")
            return " ".join(parts) + _mkt(p, p_mkt, "Üst'e")
        if tur == "iy":
            return (f"{team} son {span} dk'da {_stats(d)} ile baskı kuruyor. Devre arasına ~{max(2, 47 - m.minute)} dk var, "
                    f"bu tempoda devre bitmeden gol bekleniyor." + _mkt(p, p_mkt, "ilk yarı golüne"))
        if tur == "kg":
            tail = ("Diğer taraf zaten gol attı, karşılıklı gol için tek gol yetiyor." if k.get("need_n", 1) == 1
                    else "İki takımın da gol atması gerekiyor.")
            return (f"Gol atması gereken {k.get('need')} son {span} dk'da {_stats(d)} üretti; rakip kaleye yaklaşıyor. "
                    + tail + _mkt(p, p_mkt, "karşılıklı gole"))
        if tur == "baski":
            return (f"{team} son {span} dk'da {_stats(d)} üretti; baskısı alarm eşiğinin {_num(k.get('x', 1), 1)} katı. "
                    f"Bu yoğunluk kısa sürede gol getirebilir, ancak kesin değildir." + _mkt(p, p_mkt, "bu sürede gole"))
    except Exception:
        return ""
    return ""
