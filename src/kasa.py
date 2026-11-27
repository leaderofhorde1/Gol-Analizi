"""Sanal günlük kasa: her gün 10.000 TL ile başlar, gruba giden her bahis sinyaline o anki nakdin %25'i basılır
(en az 1.000 TL). Nakit 1.000 TL'nin altına düşerse bahis yapılmaz, sinyalde uyarı yazılır. Kazanan bahis
(bahis × oran) nakde geri döner. Kayıt: data/kasa.json (gün → durum).
"""
from __future__ import annotations

import json
import os
import threading

from main import DATA

PATH = DATA / "kasa.json"
BASLANGIC = float(os.environ.get("KASA_BASLANGIC", "10000"))
ORAN = float(os.environ.get("KASA_ORAN", "0.25"))
MIN_BAHIS = float(os.environ.get("KASA_MIN", "1000"))
YENIDEN = float(os.environ.get("KASA_YENIDEN", "10000"))  # para bitince eklenen sanal bakiye
_lock = threading.Lock()


def tl(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".") + " TL"


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:
        return {}


def _save(d: dict):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def day_state(day: str, d: dict | None = None) -> dict:
    d = _load() if d is None else d
    return d.get(day) or {"baslangic": BASLANGIC, "nakit": BASLANGIC, "acik": 0.0, "bahis": 0, "kazanan": 0,
                          "kaybeden": 0, "atlanan": 0}


def _notify_topup(st: dict, cash: float):
    try:
        from main import notify
        toplam = st["nakit"] + st["acik"]
        fark = toplam - st["baslangic"]
        notify("♻️ Kasa yenilendi", "\n".join([
            f"♻️ Kasada para kalmadı (nakit {tl(cash)}).",
            f"💼 Kasaya {tl(YENIDEN)} sanal bakiye eklendi.",
            f"💵 Güncel nakit: {tl(st['nakit'])}" + (f" (açık bahislerde {tl(st['acik'])})" if st["acik"] else ""),
            f"📒 Bugün kasaya yatırılan toplam: {tl(st['baslangic'])}  •  Durum: {'+' if fark >= 0 else '−'}{tl(abs(fark))}"]),
            priority=3)
    except Exception as e:
        print("Kasa bildirimi gönderilemedi:", e)


def bet(row: dict) -> list[str]:
    """Gönderilen sinyal için bahis aç; mesaj satırlarını döndür."""
    if row.get("kasa_durum"):
        return []
    try:
        odd = float(row.get("oran"))
    except (TypeError, ValueError):
        return ["💼 Kasa: oran olmadığı için bu sinyale bahis yapılmadı"]
    day = row.get("tarih")
    with _lock:
        d = _load()
        st = day_state(day, d)
        cash = st["nakit"]
        topup = []
        if cash < MIN_BAHIS:
            # para bitti: kasaya otomatik 10.000 TL sanal bakiye eklenir ve gruba bildirilir
            n = sum(1 for k in st.get("ekler", []) if k.startswith("otomatik")) + 1
            st["nakit"] += YENIDEN
            st["baslangic"] += YENIDEN
            st.setdefault("ekler", []).append(f"otomatik-{n}")
            topup = [f"♻️ Kasa bitmişti: {tl(YENIDEN)} sanal bakiye eklendi"]
            _notify_topup(st, cash)
            cash = st["nakit"]
        stake = min(cash, max(MIN_BAHIS, round(cash * ORAN / 10) * 10))
        st["nakit"] = cash - stake
        st["acik"] += stake
        st["bahis"] += 1
        d[day] = st
        _save(d)
    row["kasa_bahis"], row["kasa_durum"] = round(stake), "acik"
    return [*topup, f"💼 Kasa: {tl(stake)} basıldı (olası dönüş {tl(stake * odd)})",
            f"💵 Kalan nakit: {tl(st['nakit'])}"]


def settle(row: dict) -> list[str]:
    """Sonuçlanan sinyalin bahsini kapat; sonuç mesajına eklenecek satırları döndür."""
    if row.get("kasa_durum") != "acik" or row.get("sonuc") not in ("isabet", "ıska"):
        return []
    stake = float(row.get("kasa_bahis") or 0)
    odd = float(row.get("oran") or 1)
    ret = stake * odd if row["sonuc"] == "isabet" else 0.0
    day = row.get("tarih")
    with _lock:
        d = _load()
        st = day_state(day, d)
        st["nakit"] += ret
        st["acik"] = max(0.0, st["acik"] - stake)
        st["kazanan" if ret else "kaybeden"] += 1
        d[day] = st
        _save(d)
    row["kasa_durum"], row["kasa_donus"] = "kapandi", round(ret)
    toplam = st["nakit"] + st["acik"]
    fark = toplam - st["baslangic"]
    head = f"💼 +{tl(ret - stake)} kazanç" if ret else f"💼 −{tl(stake)}"
    return [f"{head}  •  Nakit: {tl(st['nakit'])}" + (f" (açık bahislerde {tl(st['acik'])})" if st["acik"] else ""),
            f"📒 Günün kasası: {tl(toplam)} ({'+' if fark >= 0 else '−'}{tl(abs(fark))})"]


def summary_lines(day: str) -> list[str]:
    d = _load()
    if day not in d:
        return []
    st = d[day]
    toplam = st["nakit"] + st["acik"]
    fark = toplam - st["baslangic"]
    ek = len(st.get("ekler", []))
    L = [f"💼 Sanal kasa: {tl(st['baslangic'])} → {tl(toplam)} ({'+' if fark >= 0 else '−'}{tl(abs(fark))})"
         + (f"  (gün içinde {ek} kez para eklendi)" if ek else ""),
         f"   {st['bahis']} bahis: {st['kazanan']} kazandı, {st['kaybeden']} kaybetti"
         + (f", {st['bahis'] - st['kazanan'] - st['kaybeden']} açık" if st['bahis'] - st['kazanan'] - st['kaybeden'] else "")]
    if st.get("atlanan"):
        L.append(f"   ⚠️ Kasada para kalmadığı için {st['atlanan']} sinyale bahis yapılamadı")
    return L


def refund(row: dict) -> list[str]:
    """İptal edilen sinyalin bahsini iade et."""
    if row.get("kasa_durum") != "acik":
        return []
    stake = float(row.get("kasa_bahis") or 0)
    day = row.get("tarih")
    with _lock:
        d = _load()
        st = day_state(day, d)
        st["nakit"] += stake
        st["acik"] = max(0.0, st["acik"] - stake)
        st["bahis"] = max(0, st["bahis"] - 1)
        d[day] = st
        _save(d)
    row["kasa_durum"], row["kasa_donus"] = "iade", round(stake)
    return [f"💼 Basılan {tl(stake)} kasaya iade edildi  •  Nakit: {tl(st['nakit'])}"]


def reopen(row: dict) -> list[str]:
    """Yanlışlıkla iptal edilip iade edilen bahsi yeniden aç (aynı tutarla)."""
    if row.get("kasa_durum") != "iade":
        return []
    stake = float(row.get("kasa_bahis") or 0)
    day = row.get("tarih")
    with _lock:
        d = _load()
        st = day_state(day, d)
        st["nakit"] -= stake
        st["acik"] += stake
        st["bahis"] += 1
        d[day] = st
        _save(d)
    row["kasa_durum"], row["kasa_donus"] = "acik", ""
    return [f"💼 {tl(stake)}'lik bahis yeniden açıldı, sonuç kasaya işlenecek"]


def deposit(day: str, amount: float, key: str) -> list[str]:
    """Kasaya para ekle (bir kez; key ile). Kâr/zarar eklenen dahil toplam yatırılana göre hesaplanır."""
    with _lock:
        d = _load()
        st = day_state(day, d)
        if key in st.get("ekler", []):
            return []
        st["nakit"] += amount
        st["baslangic"] += amount
        st.setdefault("ekler", []).append(key)
        d[day] = st
        _save(d)
    return [f"💼 Kasaya {tl(amount)} eklendi", f"💵 Güncel nakit: {tl(st['nakit'])}",
            f"📒 Bugün kasaya yatırılan toplam: {tl(st['baslangic'])}"]


def unsettle(row: dict) -> list[str]:
    """Sonucu geri alınan (VAR) bahsin kapanışını geri al: dönen para kasadan düşülür, bahis yeniden açık olur."""
    if row.get("kasa_durum") != "kapandi":
        return []
    stake = float(row.get("kasa_bahis") or 0)
    ret = float(row.get("kasa_donus") or 0)
    day = row.get("tarih")
    with _lock:
        d = _load()
        st = day_state(day, d)
        st["nakit"] -= ret
        st["acik"] += stake
        st["kazanan" if ret else "kaybeden"] = max(0, st["kazanan" if ret else "kaybeden"] - 1)
        d[day] = st
        _save(d)
    row["kasa_durum"], row["kasa_donus"] = "acik", ""
    return [f"💼 Kasa düzeltildi: {tl(ret)} geri alındı, bahis yeniden açık"] if ret else []
