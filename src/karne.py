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
    c = clv(done + [r for r in rows if r.get("sonuc") == "bekliyor"])  # zamanın doğal etkisi ayıklanmış
    leh, al, no = c["leh"], c["al"], max(0, c["nm"] - c["leh"] - c["al"])
    rate = hit / n if n else None
    exp = sum(pk) / len(pk) if pk else None
    # piyasa beklentisinden farkın anlamlılığı (kabaca 2 standart hata)
    sig = None
    if n >= 10 and exp is not None and rate is not None:
        se = math.sqrt(max(exp * (1 - exp), 1e-6) / n)
        sig = (rate - exp) / se
    return {"n": n, "hit": hit, "rate": rate, "exp": exp, "kar": sum(kar) if kar else None,
            "roi": sum(kar) / (100 * len(kar)) if kar else None, "leh": leh, "al": al, "no": no, "z": sig}


def boot_ci(rows, n_boot=1000, seed=7):
    """Maç bazında kümelenmiş bootstrap: isabet − piyasa beklentisi farkı ve sanal kâr için %90 aralık.
    Aynı maçtaki sinyaller birlikte örneklenir (bağımsız sayılmaz)."""
    import random
    by = defaultdict(list)
    for r in rows:
        if r.get("sonuc") in ("isabet", "ıska") and _f(r.get("p_piyasa")) is not None:
            by[r.get("fixture_id")].append(r)
    keys = list(by)
    if len(keys) < 15:
        return None
    rnd = random.Random(seed)
    diffs, kars = [], []
    for _ in range(n_boot):
        smp = [r for k in (rnd.choice(keys) for _ in keys) for r in by[k]]
        n = len(smp)
        diffs.append(sum((r["sonuc"] == "isabet") - _f(r["p_piyasa"]) for r in smp) / n)
        kars.append(sum(_f(r.get("kar")) or 0 for r in smp))
    diffs.sort(); kars.sort()
    lo, hi = int(0.05 * n_boot), int(0.95 * n_boot) - 1
    return {"fark": (diffs[lo], diffs[hi]), "kar": (kars[lo], kars[hi]), "mac": len(keys)}


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


OVER_DIR = {"alt": -1}


def clv(rows) -> dict:
    """Öncü gösterge: sinyalden 5 dk sonra piyasanın aynı çizgideki gol/korner/kart hızı bizim yönümüze mi gitti?
    Sonuç beklemeden (gol/korner şansına kalmadan) sinyalin piyasadan önce davranıp davranmadığını ölçer."""
    try:
        from pro import natural_drift
    except Exception:
        natural_drift = lambda *a: 1.0
    leh = al = 0
    moves = []
    for r in rows:
        q0, q1 = _f(r.get("q_once")), _f(r.get("q_sonra"))
        if not (q0 and q1 and q0 > 0):
            continue
        tur = r.get("tur") or "gol"
        mv = OVER_DIR.get(tur, 1) * (q1 / (q0 * natural_drift(tur, int(_f(r.get("dakika")) or 0), 5)) - 1)
        moves.append(mv)
        leh += mv > 0.02
        al += mv < -0.02
    n = leh + al
    rate = leh / n if n else None
    z = (rate - 0.5) / math.sqrt(0.25 / n) if n >= 10 else None
    return {"n": n, "leh": leh, "al": al, "rate": rate, "z": z,
            "move": sum(moves) / len(moves) if moves else None, "nm": len(moves)}


def clv_verdict(c) -> str:
    if c["z"] is None:
        return "veri az"
    if c["z"] >= 2:
        return "✅ piyasadan önce"
    if c["z"] <= -2:
        return "❌ piyasanın gerisinde"
    return "➖ belirsiz"


def clv_block(rows) -> list[str]:
    body = []
    for tur in TUR_AD:
        for mode, label in (("evet", ""), ("gölge", " (gölge)")):
            rs = [r for r in rows if (r.get("tur") or "gol") == tur and r.get("gonderildi") == mode]
            c = clv(rs)
            if c["n"] or c["nm"]:
                body.append([TUR_AD[tur] + label, c["n"], pct(c["rate"]),
                             "–" if c["move"] is None else f"{c['move'] * 100:+.1f}%", clv_verdict(c)])
    if not body:
        return []
    return ["### 📈 Öncü gösterge: piyasa bizim yönümüze gitti mi?", "",
            "Sonuç şansa çok bağlıdır; piyasanın sinyalden sonraki hareketi çok daha hızlı ve güvenilir bir ölçüttür. "
            "**Lehte**: 5 dk sonra aynı çizgide piyasa da bizim beklediğimiz yöne döndü (zamanın doğal etkisi ayıklanmış olarak). %50'nin anlamlı üstü = sistem "
            "piyasadan önce davranıyor (gerçek üstünlük işareti). **Hız değişimi**: piyasanın gol/korner/kart hızının "
            "bizim yönümüzde ortalama değişimi.", "",
            table(["Tür", "Ölçülen", "Lehte", "Hız değişimi", "Gösterge"], body), ""]


def section(title, rows):
    sent = [r for r in rows if r.get("gonderildi") == "evet"]
    s = summ(sent)
    L = [f"## {title}", ""]
    if not s["n"]:
        return L + ["Sonuçlanmış sinyal yok.", ""]
    maclar = len({r.get("fixture_id") for r in sent if r.get("sonuc") in ("isabet", "ıska")})
    ci = boot_ci(sent)
    ci_txt = ("" if not ci else
              f"%90 güven aralığı (maç bazlı): piyasaya göre fark {ci['fark'][0] * 100:+.0f} ile {ci['fark'][1] * 100:+.0f} puan, "
              f"sanal kâr {ci['kar'][0]:+.0f} ile {ci['kar'][1]:+.0f} TL. "
              + ("**Aralık sıfırı içeriyor: henüz piyasadan iyi olduğu kanıtlanmadı.**"
                 if ci['fark'][0] <= 0 <= ci['fark'][1] else
                 ("**Piyasanın üstünde (anlamlı).**" if ci['fark'][0] > 0 else "**Piyasanın altında (anlamlı).**")))
    L += [f"**{s['n']} sinyal sonuçlandı ({maclar} farklı maç): ✅ {s['hit']} tuttu, ❌ {s['n'] - s['hit']} tutmadı ({pct(s['rate'])})**", "",
          "_Aynı maçtaki sinyaller birbirinden bağımsız değildir; gerçek kanıt gücü maç sayısına yakındır._", "",
          *([ci_txt, ""] if ci_txt else []),
          f"Piyasanın beklediği isabet: {pct(s['exp'])} → fark {fark(s)} • Sanal kâr: {kar_s(s)}"
          f" • Piyasa bizim yönümüze döndü: {yon_s(s)}", ""]
    L += clv_block(rows)
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
    # Üst/Alt: form şimdilik sadece kaydediliyor; işe yarıyorsa "zayıf" formda verilen sinyaller daha kötü tutmalı
    for tur, ad in (("gol", "⚽ Üst (dakikadan sonra gol oldu mu)"), ("alt", "🧊 Alt (dakikadan sonra gol olmadı mı)")):
        fx = defaultdict(list)
        for r in sent:
            if (r.get("tur") or "gol") == tur and r.get("tarih", "") >= "2026-11-21":
                c = form_cat(r)
                if c:
                    fx[c].append(r)
        if fx:
            L += [f"### 📋 Form ölçütü – {ad} (sadece kayıt, sinyali etkilemiyor)", "",
                  table(["Form", "Sonuç", "İsabet", "Piyasa", "Sanal kâr"],
                        [[{"destek": "güçlü (≥%60)", "nötr": "orta", "ters": "zayıf (≤%20)"}[c], t["n"], pct(t["rate"]),
                          pct(t["exp"]), kar_s(t)] for c in ("destek", "nötr", "ters") if c in fx
                         for t in [summ(fx[c])]]), ""]
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
        c = clv([r])
        if c["nm"]:
            py["lehte" if c["leh"] else "aleyhte" if c["al"] else "nötr"].append(r)
    if py:
        L += ["### 📈 Piyasa hareketi ve sonuç", "",
              "Sinyalden 5 dk sonra aynı çizgide piyasa bizim yönümüze döndüyse **lehte**. "
              "Lehte oranı %50'nin belirgin üstündeyse sistem piyasadan önce davranıyor demektir.", "",
              table(["Piyasa", "Sonuç", "İsabet", "Sanal kâr"],
                    [[k, t["n"], pct(t["rate"]), kar_s(t)] for k in ("lehte", "nötr", "aleyhte") if k in py
                     for t in [summ(py[k])]]), ""]
    # oran aralıkları: kayıp düşük oranlı "güvenli" sinyallerden mi geliyor?
    ob = []
    for lo, hi, ad in ((1.0, 1.5, "1.00-1.49"), (1.5, 2.0, "1.50-1.99"), (2.0, 3.0, "2.00-2.99"),
                       (3.0, 4.0, "3.00-3.99"), (4.0, 1e9, "4.00+")):
        g = [r for r in sent if (r.get("tur") or "gol") != "baski" and _f(r.get("oran"))
             and lo <= _f(r.get("oran")) < hi]
        if g:
            t = summ(g)
            if t["n"]:
                ob.append([ad, t["n"], pct(t["rate"]), pct(t["exp"]), kar_s(t)])
    if ob:
        L += ["### 🎚 Oran aralığına göre", "",
              "Sinyal anındaki gerçek orana göre. İsabet piyasanın beklentisinin altında kalan aralık, "
              "o oranlarda sistemin üstünlüğü olmadığını gösterir.", "",
              table(["Oran", "Sonuç", "İsabet", "Piyasa", "Sanal kâr (100 TL)"], ob), ""]
    # sinyal gücü dereceleri
    gc = defaultdict(list)
    for r in sent:
        if r.get("guc"):
            gc[r["guc"]].append(r)
    if gc:
        def pay_kar(rs):  # önerilen payla: kasa 100 birim, kâr birim cinsinden
            return sum((_f(r.get("pay")) or 0) * ((_f(r.get("oran")) or 1) - 1 if r.get("sonuc") == "isabet" else -1)
                       for r in rs if r.get("sonuc") in ("isabet", "ıska"))
        L += ["### 💪 Sinyal gücü", "",
              "Güç, modelin olasılığı ve gerçek orandan (Kelly'nin onda biri) hesaplanır. Güçlüler belirgin şekilde "
              "daha iyiyse derecelendirme işe yarıyor demektir. **Payla kâr**: kasa 100 birim, önerilen paylarla.", "",
              table(["Güç", "Sonuç", "İsabet", "Piyasa", "Sanal kâr (100 TL)", "Payla kâr"],
                    [[k, t["n"], pct(t["rate"]), pct(t["exp"]), kar_s(t), f"{pay_kar(gc[k]):+.1f} birim"]
                     for k in ("Güçlü", "Orta", "Hafif") if k in gc for t in [summ(gc[k])]]), ""]
    # beklenen getiri (gerçek oranla)
    ev = defaultdict(list)
    for r in sent:
        e = _f(r.get("ev"))
        if e is not None:
            ev["EV > +%2" if e > 0.02 else "EV 0 ile +%2" if e >= 0 else "EV negatif"].append(r)
    if ev:
        L += ["### 💰 Beklenen getiri (sunulan gerçek oranla)", "",
              "EV = model olasılığı × gerçek oran − 1. Negatif EV'li sinyal, model piyasadan iyimser olsa bile "
              "oranın marjı yüzünden zararlıdır.", "",
              table(["EV", "Sonuç", "İsabet", "Ort. oran", "Sanal kâr"],
                    [[k, t["n"], pct(t["rate"]),
                      f"{sum(_f(r.get('oran')) or 0 for r in ev[k]) / max(1, len(ev[k])):.2f}", kar_s(t)]
                     for k in ("EV > +%2", "EV 0 ile +%2", "EV negatif") if k in ev for t in [summ(ev[k])]]), ""]
    # gölge mod
    gl = defaultdict(list)
    for r in rows:
        if r.get("gonderildi") == "gölge":
            gl[r.get("tur") or "gol"].append(r)
    if gl:
        L += ["### 👻 Gölge moddaki türler (gruba gitmiyor, ölçülüyor)", "",
              table(["Tür", "Sonuç", "İsabet", "Piyasa", "Fark", "Sanal kâr", "Durum"],
                    [[TUR_AD.get(k, k), t["n"], pct(t["rate"]), pct(t["exp"]), fark(t), kar_s(t), karar(t)]
                     for k, v in gl.items() for t in [summ(v)]]), ""]
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
    try:
        import kupon
        for name, since_ in ((f"son {days} gün", since), ("tüm zamanlar", "")):
            k = kupon.summary(since=since_)
            if k["n"]:
                L += [f"## 🧪 Deneme kuponları ({name})", "",
                      f"Kupon: {k['w']}/{k['n']} tuttu (piyasanın beklediği ~{k['exp']:.1f}), "
                      f"kupon başına 100 TL ile sanal kâr: {k['kar']:+.0f} TL. "
                      f"Tek tek maçlar: {k['bacak_w']}/{k['bacak']} (piyasa beklentisi ~{k['bacak_exp']:.1f}).", ""]
    except Exception as e:
        L += [f"(Kupon özeti alınamadı: {e})", ""]
    try:
        import dusus
        for name, since_ in ((f"son {days} gün", since), ("tüm zamanlar", "")):
            sm = dusus.summary(since_)
            if sm and sm[0][1]:
                L += [f"## 📉 Oran düşüşü uyarıları ({name})", "",
                      "Düşen oran o sonucun gerçekleşmesi demek; piyasa = uyarı anındaki adil ihtimal. "
                      "Sanal kâr: her uyarıya uyarı anındaki oranla 100 TL.", "",
                      "| Uyarı | Sonuç | İsabet | Piyasa | Sanal kâr |", "|---|---|---|---|---|"]
                for g, n, w, exp, kar in sm:
                    L.append(f"| {g} | {n} | %{round(100 * w / n)} | %{round(100 * exp / n)} | {kar:+.0f} TL |")
                L.append("")
    except Exception as e:
        L += [f"(Oran düşüşü özeti alınamadı: {e})", ""]
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
