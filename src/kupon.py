"""🧪 Deneme kuponları: günde bir kez, en fazla 4 maçlık kupon önerileri.

Seçim: bahis piyasasının yüksek ihtimal verdiği (düşük oranlı) bahisler arasından, iki takımın son 5
maçının da aynı sonucu destekledikleri. Her kupon gruba ayrı mesaj olarak gider, maçlar bitince sonucu
o mesaja cevap olarak yazılır. Kayıt: data/kupon/kuponlar.json (karnede ayrı bölüm).

Deneme aşamasındadır: maç öncesi modelimizin piyasayı geçtiği henüz kanıtlanmadı.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading

import form as formmod
from main import DATA, FINISHED, PRIORITY, TZ, log, notify, telegram_reply

KUPON_DIR = DATA / "kupon"
KUPON_JSON = KUPON_DIR / "kuponlar.json"
KUPON_SAAT = int(os.environ.get("KUPON_SAAT", "10"))        # bu saatten sonra günün kuponları hazırlanır
MAX_ADAY = int(os.environ.get("KUPON_ADAY", "45"))           # form verisi çekilecek en fazla maç
MIN_BACAK, MAX_BACAK = 3, 4
VOID = {"PST", "CANC", "ABD", "AWD", "WO"}
_lock = threading.Lock()

# tür: (etiket, form ölçütü, form çizgisi, oran aralığı, her takım en az, iki takım toplam en az)
BACAKLAR = {
    "ust15": ("1.5 Üst", "gol", 1.5, (1.18, 1.45), 4, 8),
    "ust25": ("2.5 Üst", "gol", 2.5, (1.40, 1.90), 3, 7),
    "alt25": ("2.5 Alt", "alt", 2.5, (1.45, 1.95), 3, 7),
    "alt35": ("3.5 Alt", "alt", 3.5, (1.18, 1.45), 4, 8),
    "kg":    ("KG Var", "kg", None, (1.45, 1.90), 3, 7),
    "korner": ("Korner {line} Üst", "korner", None, (1.40, 1.85), 3, 7),
}
KUPONLAR = [  # sıra: önce dar havuzlu kuponlar doldurulur; bir maç tek kuponda yer alır
    ("korner", "🚩 Korner kuponu", ("korner",)),
    ("alt", "🧊 Alt kuponu", ("alt25", "alt35")),
    ("gol", "🎯 Gol kuponu", ("ust15", "kg")),
    ("ust", "⚽ Üst kuponu", ("ust25",)),
    ("karisik", "🎲 Karışık kupon", tuple(BACAKLAR)),
]


def load() -> list[dict]:
    try:
        return json.loads(KUPON_JSON.read_text())
    except Exception:
        return []


def save(rows: list[dict]):
    with _lock:
        KUPON_DIR.mkdir(parents=True, exist_ok=True)
        KUPON_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=1))


def fair2(a, b):
    ia, ib = 1 / a, 1 / b
    return ia / (ia + ib)


def legs_for_match(fx: dict, son: dict) -> list[dict]:
    """Bir maçın oran aralığına uyan bahisleri (piyasa adil ihtimaliyle)."""
    out = []
    def add(key, odd, other, line=None):
        lo, hi = BACAKLAR[key][3]
        if odd and other and lo <= odd <= hi:
            out.append({"tur": key, "oran": odd, "p": fair2(odd, other), "cizgi": line})
    if "ou15" in son:
        add("ust15", son["ou15"]["ust"], son["ou15"]["alt"], 1.5)
    if "ou" in son:
        add("ust25", son["ou"]["ust"], son["ou"]["alt"], 2.5)
        add("alt25", son["ou"]["alt"], son["ou"]["ust"], 2.5)
    if "ou35" in son:
        add("alt35", son["ou35"]["alt"], son["ou35"]["ust"], 3.5)
    if "kg" in son:
        add("kg", son["kg"]["var"], son["kg"]["yok"])
    best = None
    for line, d in (son.get("korner") or {}).items():
        lo, hi = BACAKLAR["korner"][3]
        if d.get("ust") and d.get("alt") and lo <= d["ust"] <= hi:
            cand = {"tur": "korner", "oran": d["ust"], "p": fair2(d["ust"], d["alt"]), "cizgi": float(line)}
            if best is None or abs(cand["oran"] - 1.6) < abs(best["oran"] - 1.6):
                best = cand
    if best:
        out.append(best)
    return out


def form_support(leg: dict, home: list, away: list) -> dict | None:
    """İki takımın son 5 maçında bu bahis kaç kez tutmuş? Şartı sağlamıyorsa None."""
    _, crit, line, _, each, total = BACAKLAR[leg["tur"]]
    line = leg["cizgi"] if line is None else line
    res = []
    for games in (home, away):
        hs = [formmod.hit(crit, line, s) for s in games[:5]]
        hs = [h for h in hs if h is not None]
        if len(hs) < 4:
            return None
        res.append((sum(hs), len(hs)))
    if min(h for h, _ in res) < each or res[0][0] + res[1][0] < total:
        return None
    ratio = (res[0][0] + res[1][0]) / (res[0][1] + res[1][1])
    return {"form_ev": list(res[0]), "form_dep": list(res[1]), "oran_form": ratio}


def build(api, book, fb) -> list[dict]:
    now = dt.datetime.now(TZ)
    cands = []
    for fid, fx in book.fixtures.items():
        rec = book.data.get(fid)
        if fx["durum"] != "NS" or not rec or not fx.get("ev_id"):
            continue
        if not (now.timestamp() + 45 * 60 < fx["ts"] < now.timestamp() + 16 * 3600):
            continue
        legs = legs_for_match(fx, rec.get("son") or {})
        if legs:
            cands.append((fid, fx, legs))
    cands.sort(key=lambda c: (-PRIORITY.get(c[1]["lig_id"], 10), c[1]["ts"]))
    pool = []
    for fid, fx, legs in cands[:MAX_ADAY]:
        home, away = fb.team(fx["ev_id"]), fb.team(fx["dep_id"])
        for leg in legs:
            sup = form_support(leg, home, away)
            if not sup:
                continue
            # sıralama: form piyasadan ne kadar iyimser + piyasanın kendi güveni
            score = (sup["oran_form"] - leg["p"]) + 0.5 * leg["p"]
            t = dt.datetime.fromtimestamp(fx["ts"], TZ).strftime("%H:%M")
            pool.append({**leg, **sup, "skor": round(score, 4), "fixture_id": fid, "saat": t, "ts": fx["ts"],
                         "mac": f'{fx["ev"]} - {fx["dep"]}', "ev": fx["ev"], "dep": fx["dep"], "lig": fx["lig"],
                         "etiket": BACAKLAR[leg["tur"]][0].format(line=leg["cizgi"]), "sonuc": "bekliyor"})
    pool.sort(key=lambda x: -x["skor"])
    used, coupons = set(), []
    for kid, title, types in KUPONLAR:
        legs = []
        for leg in pool:
            if leg["tur"] in types and leg["fixture_id"] not in used and leg["fixture_id"] not in {l["fixture_id"] for l in legs}:
                legs.append(leg)
            if len(legs) == MAX_BACAK:
                break
        if len(legs) < MIN_BACAK:
            continue
        legs.sort(key=lambda x: x["ts"])
        used |= {l["fixture_id"] for l in legs}
        odd = 1.0
        p = 1.0
        for l in legs:
            odd *= l["oran"]
            p *= l["p"]
        coupons.append({"tarih": book.today, "id": f"{book.today}-{kid}", "tur": kid, "baslik": title,
                        "oran": round(odd, 2), "p_piyasa": round(p, 3), "bacaklar": legs,
                        "sonuc": "bekliyor", "tg": None})
    log(f"Kupon: {len(cands)} aday maç, {len(pool)} uygun bahis, {len(coupons)} kupon")
    return coupons


def text(c: dict) -> str:
    L = ["🧪 DENEME KUPONU", "━━━━━━━━━━━━━━"]
    for l in c["bacaklar"]:
        (hh, hn), (ah, an) = l["form_ev"], l["form_dep"]
        L += [f"🕖 {l['saat']}  {l['mac']}", f"   🏆 {l['lig']}",
              f"   👉 {l['etiket']} @{l['oran']:.2f}",
              f"   📋 Son maçlarında bu sonuç: {l['ev']} {hh}/{hn}, {l['dep']} {ah}/{an}", ""]
    L += [f"💰 Toplam oran: {c['oran']:.2f}",
          f"📊 Piyasaya göre tutma ihtimali: ~%{round(c['p_piyasa'] * 100)}",
          "━━━━━━━━━━━━━━",
          "ℹ️ Deneme aşamasındadır: maç öncesi seçimlerin piyasayı geçtiği henüz kanıtlanmadı, sonuçlar ölçülüyor.",
          "ℹ️ Bahis tavsiyesi değildir."]
    return "\n".join(L)


def maybe_send(api, book, fb, force: bool = False):
    """Günde bir kez: bugünün kuponları yoksa hazırla ve gönder."""
    now = dt.datetime.now(TZ)
    if not force and now.hour < KUPON_SAAT:
        return
    rows = load()
    if any(r["tarih"] == book.today for r in rows) and not force:
        return
    try:
        coupons = build(api, book, fb)
    except Exception as e:
        log("Kupon hazırlanamadı:", e)
        return
    if not coupons:  # boş gün de işaretlensin ki her oturumda tekrar denenmesin
        coupons = [{"tarih": book.today, "id": f"{book.today}-yok", "tur": "yok", "bacaklar": [],
                    "sonuc": "yok", "tg": None}]
    rows += coupons
    save(rows)
    for c in coupons:
        if not c["bacaklar"]:
            continue
        def on_tg(mid, cid=c["id"]):
            rs = load()
            for r in rs:
                if r["id"] == cid:
                    r["tg"] = mid
            save(rs)
        notify(f"🧪 {c['baslik']} (oran {c['oran']:.2f})", text(c), priority=3, tags=["ticket"], on_tg=on_tg)


def _leg_result(leg: dict, it: dict):
    st = it["fixture"]["status"]["short"]
    if st in VOID:
        return "iptal", ""
    if st not in FINISHED:
        return None, ""
    ft = (it.get("score") or {}).get("fulltime") or it.get("goals") or {}
    gh, ga = ft.get("home"), ft.get("away")
    if gh is None or ga is None:
        return None, ""
    skor = f"{gh}-{ga}"
    t = leg["tur"]
    if t == "korner":
        stats = it.get("statistics") or []
        c = None
        if len(stats) == 2:
            c = sum(formmod._num(s.get("value")) for b in stats for s in b.get("statistics") or []
                    if s.get("type") == "Corner Kicks")
        if not c:
            ended = (it["fixture"].get("timestamp") or 0) + 3 * 3600
            return ("bilinmiyor", skor) if dt.datetime.now(TZ).timestamp() > ended + 2 * 3600 else (None, skor)
        return ("isabet" if c > leg["cizgi"] else "ıska"), f"{skor}, {int(c)} korner"
    won = {"ust15": gh + ga >= 2, "ust25": gh + ga >= 3, "alt25": gh + ga <= 2, "alt35": gh + ga <= 3,
           "kg": gh > 0 and ga > 0}[t]
    return ("isabet" if won else "ıska"), skor


def resolve(api):
    rows = load()
    open_ = [c for c in rows if c.get("sonuc") == "bekliyor"]
    if not open_:
        return
    ids = sorted({l["fixture_id"] for c in open_ for l in c["bacaklar"] if l["sonuc"] == "bekliyor"})
    res = {}
    for i in range(0, len(ids), 20):
        try:
            for it in api.get("fixtures", ids="-".join(ids[i:i + 20])):
                res[str(it["fixture"]["id"])] = it
        except Exception as e:
            log("Kupon sonucu alınamadı:", e)
    changed = False
    for c in open_:
        for l in c["bacaklar"]:
            if l["sonuc"] == "bekliyor" and l["fixture_id"] in res:
                r, skor = _leg_result(l, res[l["fixture_id"]])
                if r:
                    l["sonuc"], l["skor"] = r, skor
                    changed = True
        if any(l["sonuc"] == "bekliyor" for l in c["bacaklar"]):
            continue
        live = [l for l in c["bacaklar"] if l["sonuc"] in ("isabet", "ıska")]
        if not live:
            c["sonuc"], c["kar"] = "iptal", 0
        else:
            odd = 1.0
            for l in live:
                odd *= l["oran"]
            ok = all(l["sonuc"] == "isabet" for l in live)
            c["sonuc"] = "isabet" if ok else "ıska"
            c["oran_gercek"] = round(odd, 2)
            c["kar"] = round(100 * (odd - 1)) if ok else -100
        changed = True
        icon = {"isabet": "✅", "ıska": "❌", "bilinmiyor": "❔", "iptal": "➖"}
        lines = [f"{icon.get(l['sonuc'], '❔')} {l['mac']}: {l['etiket']} ({l.get('skor') or 'sonuç yok'})"
                 for l in c["bacaklar"]]
        head = {"isabet": f"✅ KUPON TUTTU! Oran {c.get('oran_gercek', c['oran']):.2f}",
                "ıska": "❌ Kupon tutmadı", "iptal": "➖ Kupon iptal (maçlar oynanmadı)"}[c["sonuc"]]
        if any(l["sonuc"] in ("iptal", "bilinmiyor") for l in c["bacaklar"]) and c["sonuc"] != "iptal":
            head += " (oynanmayan/sonucu bilinmeyen maç düşüldü)"
        body = "\n".join([head, *lines])
        telegram_reply(c.get("tg"), body, fallback=(f"🧪 {c['baslik']} sonucu", body))
        notify(f"🧪 {c['baslik']} sonucu", body, priority=3, telegram=False)
    if changed:
        save(rows)


def summary(rows=None, since: str = "") -> dict:
    rows = [c for c in (rows or load()) if c.get("sonuc") in ("isabet", "ıska") and c["tarih"] > since]
    n = len(rows)
    w = sum(c["sonuc"] == "isabet" for c in rows)
    exp = sum(c.get("p_piyasa") or 0 for c in rows)
    kar = sum(c.get("kar") or 0 for c in rows)
    legs = [l for c in rows for l in c["bacaklar"] if l["sonuc"] in ("isabet", "ıska")]
    return {"n": n, "w": w, "exp": exp, "kar": kar, "bacak": len(legs),
            "bacak_w": sum(l["sonuc"] == "isabet" for l in legs), "bacak_exp": sum(l["p"] for l in legs)}
