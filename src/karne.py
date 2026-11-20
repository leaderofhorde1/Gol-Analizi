"""Sinyal karnesi: tür, lig, Claude filtresi, form, güven ve piyasa hareketi bazında performans.

Çalıştırma: python karne.py [gün_sayısı]   (varsayılan: son 7 gün + tüm zamanlar)
Çıktı: data/rapor/karne.md (son hali) ve data/rapor/karne-YYYY-MM-DD.md
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import sys
from collections import defaultdict

from main import DATA, TZ, notify

RAPOR = DATA / "rapor"
MIN_KARAR = 30   # bir tür hakkında karar vermek için gereken en az sonuç
MIN_LIG = 5      # lig tablosuna girmek için en az sonuç

TUR_AD = {"gol": "⚽ Üst (gol)", "alt": "🧊 Alt", "korner": "🚩 Korner", "kart": "🟨 Kart",
          "iy": "⏱ İlk yarı", "kg": "🤝 KG", "baski": "🔥 Baskı alarmı"}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(path=DATA / "sinyaller.csv"):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def summ(rows):
    done = [r for r in rows if r.get("sonuc") in ("isabet", "ıska")]
    n = len(done)
    hit = sum(r["sonuc"] == "isabet" for r in done)
    pk = [_f(r.get("p_piyasa")) for r in done]
    pk = [x for x in pk if x is not None]
    kar = [_f(r.get("kar")) for r in done if _f(r.get("kar")) is not None]
    yon = [r.get("piyasa_yon") for r in done + [r for r in rows if r.get("sonuc") == "bekliyor"]]
    leh, al, no = yon.count("lehte"), yon.count("aleyhte"), yon.count("nötr")
    rate = hit / n if n else None
    exp = sum(pk) / len(pk) if pk else None
    # piyasa beklentisinden farkın anlamlılığı (kabaca 2 standart hata)
    sig = None
    if n >= 10 and exp is not None and rate is not None:
        se = math.sqrt(max(exp * (1 - exp), 1e-6) / n)
        sig = (rate - exp) / se
    return {"n": n, "hit": hit, "rate": rate, "exp": exp, "kar": sum(kar) if kar else None,
            "roi": sum(kar) / (100 * len(kar)) if kar else None, "leh": leh, "al": al, "no": no, "z": sig}


def pct(x):
    return "-" if x is None else f"%{round(x * 100)}"


def fark(s):
    if s["rate"] is None or s["exp"] is None:
        return "-"
    d = round((s["rate"] - s["exp"]) * 100)
    star = " ✱" if s["z"] is not None and abs(s["z"]) >= 2 else ""
    return f"{d:+d} puan{star}"


def kar_s(s):
    return "-" if s["kar"] is None else f"{s['kar']:+.0f} TL"


def yon_s(s):
    t = s["leh"] + s["al"] + s["no"]
    return "-" if not t else f"{pct(s['leh'] / t)} ({t})"


def karar(s):
    if s["n"] < MIN_KARAR:
        return f"⏳ veri az ({s['n']}/{MIN_KARAR})"
    d = (s["rate"] or 0) - (s["exp"] or 0)
    if d >= 0.03 and (s["kar"] or 0) > 0:
        return "✅ güçlü" if (s["z"] or 0) >= 2 else "🟢 umut verici"
    if d <= -0.03 and (s["kar"] or 0) < 0:
        return "⛔ kapatılmalı" if (s["z"] or 0) <= -2 else "🔴 zayıf"
    return "🟡 piyasa kadar"


def table(head, body):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
    return "\n".join(out)


def form_cat(r):
    f = r.get("form") or ""
    if "/" not in f:
        return None
    a, b = f.split("/")
    try:
        ratio = int(a) / int(b)
    except (ValueError, ZeroDivisionError):
        return None
    return "ters" if ratio <= 0.2 else "destek" if ratio >= 0.6 else "nötr"


def section(title, rows):
    sent = [r for r in rows if r.get("gonderildi") == "evet"]
    s = summ(sent)
    L = [f"## {title}", ""]
    if not s["n"]:
        return L + ["Sonuçlanmış sinyal yok.", ""]
    L += [f"**{s['n']} sinyal sonuçlandı: ✅ {s['hit']} tuttu, ❌ {s['n'] - s['hit']} tutmadı ({pct(s['rate'])})**", "",
          f"Piyasanın beklediği isabet: {pct(s['exp'])} → fark {fark(s)} • Sanal kâr: {kar_s(s)}"
          f" • Piyasa bizim yönümüze döndü: {yon_s(s)}", ""]
    # tür
    by = defaultdict(list)
    for r in sent:
        by[r.get("tur") or "gol"].append(r)
    body = []
    for tur in TUR_AD:
        if tur in by:
            t = summ(by[tur])
            body.append([TUR_AD[tur], t["n"], pct(t["rate"]), pct(t["exp"]), fark(t), kar_s(t), yon_s(t), karar(t)])
    L += ["### Tür bazında", "", table(["Tür", "Sonuç", "İsabet", "Piyasa", "Fark", "Sanal kâr",
                                         "Piyasa lehte", "Durum"], body), ""]
    # lig
    byl = defaultdict(list)
    for r in sent:
        byl[r.get("lig") or "?"].append(r)
    lig = [(k, summ(v)) for k, v in byl.items()]
    lig = [(k, t) for k, t in lig if t["n"] >= MIN_LIG]
    lig.sort(key=lambda kt: -(kt[1]["kar"] or 0))
    if lig:
        L += [f"### Lig bazında (en az {MIN_LIG} sonuç)", "",
              table(["Lig", "Sonuç", "İsabet", "Piyasa", "Sanal kâr"],
                    [[k, t["n"], pct(t["rate"]), pct(t["exp"]), kar_s(t)] for k, t in lig]), ""]
    # Claude filtresi (reddedilenler de dahil)
    gol = [r for r in rows if (r.get("tur") or "gol") == "gol" and r.get("ai_onay") in ("evet", "hayır")]
    if gol:
        on, red = summ([r for r in gol if r["ai_onay"] == "evet"]), summ([r for r in gol if r["ai_onay"] == "hayır"])
        L += ["### 🤖 Claude filtresi (gol sinyalleri)", "",
              table(["", "Sonuç", "İsabet", "Piyasa", "Sanal kâr"],
                    [["Onayladı (gönderildi)", on["n"], pct(on["rate"]), pct(on["exp"]), kar_s(on)],
                     ["Reddetti (gönderilmedi)", red["n"], pct(red["rate"]), pct(red["exp"]), kar_s(red)]]), ""]
        if on["n"] >= 20 and red["n"] >= 20 and on["rate"] is not None and red["rate"] is not None:
            L.append("➡️ Filtre işe yarıyor." if on["rate"] - red["rate"] >= 0.08 else
                     "➡️ Filtre fark yaratmıyor; kaldırılması düşünülebilir.")
            L.append("")
    # form
    fc = defaultdict(list)
    for r in sent:
        c = form_cat(r)
        if c:
            fc[c].append(r)
    if fc:
        L += ["### 📋 Son 5 maç formu", "",
              table(["Form", "Sonuç", "İsabet", "Piyasa", "Sanal kâr"],
                    [[{"destek": "✅ destekliyor", "nötr": "nötr", "ters": "⚠️ ters"}[c], t["n"], pct(t["rate"]),
                      pct(t["exp"]), kar_s(t)] for c in ("destek", "nötr", "ters") if c in fc
                     for t in [summ(fc[c])]]), ""]
    # canlı oran akışı (sinyal anında, son 5 dk)
    ak = defaultdict(list)
    yonler = {"gol": 1, "iy": 1, "kg": 1, "baski": 1, "alt": -1}
    for r in sent:
        v = (r.get("oran_akisi") or "").replace("%", "")
        tur = r.get("tur") or "gol"
        if not v or tur not in yonler:
            continue
        try:
            x = float(v) / 100 * yonler[tur]
        except ValueError:
            continue
        ak["aynı yönde" if x >= 0.15 else "ters yönde" if x <= -0.13 else "nötr"].append(r)
    if ak:
        L += ["### 💹 Canlı oran akışı (sinyal anında)", "",
              table(["Akış", "Sonuç", "İsabet", "Piyasa", "Sanal kâr"],
                    [[k, t["n"], pct(t["rate"]), pct(t["exp"]), kar_s(t)] for k in ("aynı yönde", "nötr", "ters yönde")
                     if k in ak for t in [summ(ak[k])]]), ""]
    # maç bağlamı
    bg = defaultdict(list)
    for r in sent:
        b = r.get("baglam") or ""
        if "rovans" in b:
            bg["🔁 rövanş"].append(r)
        if "şampiyonluk" in b or "küme" in b:
            bg["🏆 yarıştaki takım"].append(r)
    if bg:
        L += ["### 🏆 Maç bağlamı", "",
              table(["Bağlam", "Sonuç", "İsabet", "Piyasa", "Sanal kâr"],
                    [[k, t["n"], pct(t["rate"]), pct(t["exp"]), kar_s(t)] for k, v in bg.items() for t in [summ(v)]]), ""]
    # piyasa hareketi ↔ sonuç
    py = defaultdict(list)
    for r in sent:
        if r.get("piyasa_yon") in ("lehte", "aleyhte", "nötr"):
            py[r["piyasa_yon"]].append(r)
    if py:
        L += ["### 📈 Piyasa hareketi ve sonuç", "",
              "Sinyalden 5 dk sonra aynı çizgide piyasa bizim yönümüze döndüyse **lehte**. "
              "Lehte oranı %50'nin belirgin üstündeyse sistem piyasadan önce davranıyor demektir.", "",
              table(["Piyasa", "Sonuç", "İsabet", "Sanal kâr"],
                    [[k, t["n"], pct(t["rate"]), kar_s(t)] for k in ("lehte", "nötr", "aleyhte") if k in py
                     for t in [summ(py[k])]]), ""]
    # güven
    gv = defaultdict(list)
    for r in sent:
        gv[r.get("guven") or "-"].append(r)
    L += ["### 📶 Güven seviyesine göre", "",
          table(["Güven", "Sonuç", "İsabet", "Piyasa", "Sanal kâr"],
                [[k, t["n"], pct(t["rate"]), pct(t["exp"]), kar_s(t)] for k in ("yüksek", "orta", "düşük")
                 if k in gv for t in [summ(gv[k])]]), ""]
    return L


def build(days=7):
    rows = load()
    today = dt.datetime.now(TZ).date()
    since = (today - dt.timedelta(days=days)).isoformat()
    recent = [r for r in rows if (r.get("tarih") or "") > since]
    L = [f"# 📊 Sinyal karnesi – {today:%d.%m.%Y}", "",
         "Sadece gruba gönderilen sinyaller sayılır (Claude bölümü hariç). Sanal kâr: her sinyale 100 TL, "
         "piyasanın adil oranıyla. **Fark**: isabet oranı eksi piyasanın beklediği oran; ✱ = istatistiksel "
         "olarak anlamlı (yaklaşık 2 standart hata).", ""]
    L += section(f"Son {days} gün", recent)
    L += section("Tüm zamanlar", rows)
    text = "\n".join(L)
    RAPOR.mkdir(parents=True, exist_ok=True)
    (RAPOR / "karne.md").write_text(text, encoding="utf-8")
    (RAPOR / f"karne-{today.isoformat()}.md").write_text(text, encoding="utf-8")
    return text, summ([r for r in recent if r.get("gonderildi") == "evet"]), recent


def short_summary(s, recent, days):
    sent = [r for r in recent if r.get("gonderildi") == "evet"]
    by = defaultdict(list)
    for r in sent:
        by[r.get("tur") or "gol"].append(r)
    lines = [f"Son {days} gün: ✅ {s['hit']} / {s['n']} ({pct(s['rate'])}), piyasa {pct(s['exp'])}, "
             f"sanal kâr {kar_s(s)}"]
    for tur in TUR_AD:
        if tur in by:
            t = summ(by[tur])
            if t["n"]:
                lines.append(f"{TUR_AD[tur]}: {t['hit']}/{t['n']} ({fark(t)}) {karar(t)}")
    return "\n".join(lines)


if __name__ == "__main__":
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    days = int(nums[0]) if nums else 7
    text, s, recent = build(days)
    print(text)
    if "--bildir" in sys.argv:
        if s["n"]:
            notify("📊 Haftalık karne", short_summary(s, recent, days), priority=3, telegram=False,
                   click="https://github.com/leaderofhorde1/Gol-Analizi/blob/main/data/rapor/karne.md")
