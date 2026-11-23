"""🧪 Deneme kuponları: günde bir kez, en fazla 4 maçlık kupon önerileri.

Seçim: bahis piyasasının yüksek ihtimal verdiği (düşük oranlı) bahisler arasından, iki takımın bu sezonki son 10
maçının ve son iki sezonda kendi aralarındaki maçların da aynı sonucu destekledikleri. Her kupon gruba ayrı mesaj olarak gider, maçlar bitince sonucu
o mesaja cevap olarak yazılır. Kayıt: data/kupon/kuponlar.json (karnede ayrı bölüm).

Deneme aşamasındadır: maç öncesi modelimizin piyasayı geçtiği henüz kanıtlanmadı.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading

import form as formmod
from main import DATA, FINISHED, PRIORITY, TZ, log, notify, telegram_reply, _tg, telegram_chat_id

KUPON_DIR = DATA / "kupon"
KUPON_JSON = KUPON_DIR / "kuponlar.json"
# kupon partileri: her saatten sonra bir kez; sonraki partiler önceki partilerde kullanılmamış, daha geç maçlardan
KUPON_SAATLER = sorted(int(x) for x in os.environ.get("KUPON_SAATLER", "10,18").split(",") if x.strip())
MAX_ADAY = int(os.environ.get("KUPON_ADAY", "120"))           # form verisi çekilecek en fazla maç
MIN_BACAK, MAX_BACAK = 2, 3   # 4 maçta şirket payı ~%21'e çıkıyor, 3 maçta ~%15, 2 maçta ~%10
MIN_FARK = float(os.environ.get("KUPON_FARK", "0.12"))  # form, piyasadan en az bu kadar iyimser olmalı
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
AILE = {"ust15": "gol", "ust25": "gol", "kg": "gol", "alt25": "alt", "alt35": "alt", "korner": "korner"}
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


def form_support(leg: dict, home: list, away: list, h2h: list | None = None) -> dict | None:
    """İki takımın bu sezonki son 10 maçında bu bahis kaç kez tutmuş? Şartı sağlamıyorsa None.
    Eşikler 5 maç için tanımlı (her takım en az `each`/5, toplam en az `total`/10); oranla uygulanır.
    Aralarındaki maçlar (son 2 sezon, en az 3) yarıdan az tutmuşsa da elenir."""
    _, crit, line, _, each, total = BACAKLAR[leg["tur"]]
    line = leg["cizgi"] if line is None else line
    res = []
    for games in (home, away):
        hs = [formmod.hit(crit, line, s) for s in games[:formmod.LAST_N]]
        hs = [h for h in hs if h is not None]
        if len(hs) < 5:
            return None
        res.append((sum(hs), len(hs)))
    if min(h / n for h, n in res) < each / 5 or (res[0][0] + res[1][0]) / (res[0][1] + res[1][1]) < total / 10:
        return None
    ratio = (res[0][0] + res[1][0]) / (res[0][1] + res[1][1])
    hh = None
    if h2h:
        hr = [formmod.hit(crit, line, s) for s in h2h]
        hr = [x for x in hr if x is not None]
        if hr:
            hh = [sum(hr), len(hr)]
            if len(hr) >= 3 and hh[0] / hh[1] < 0.5:
                return None
    return {"form_ev": list(res[0]), "form_dep": list(res[1]), "oran_form": ratio, "h2h": hh}


def build(api, book, fb, parti: int = 10, skip: set | None = None) -> list[dict]:
    now = dt.datetime.now(TZ)
    cands = []
    for fid, fx in book.fixtures.items():
        rec = book.data.get(fid)
        if fx["durum"] != "NS" or not rec or not fx.get("ev_id") or fid in (skip or ()):
            continue
        if not (now.timestamp() + 45 * 60 < fx["ts"] < now.timestamp() + 16 * 3600):
            continue
        legs = legs_for_match(fx, rec.get("son") or {})
        if legs:
            cands.append((fid, fx, legs))
    cands.sort(key=lambda c: (-PRIORITY.get(c[1]["lig_id"], 10), c[1]["ts"]))
    pool = []
    for fid, fx, legs in cands[:MAX_ADAY]:
        sz = fx.get("sezon")
        home, away = fb.team(fx["ev_id"], sz), fb.team(fx["dep_id"], sz)
        h2h = None
        for leg in legs:
            if h2h is None:
                h2h = fb.h2h(fx["ev_id"], fx["dep_id"], sz)
            sup = form_support(leg, home, away, h2h)
            if not sup or sup["oran_form"] - leg["p"] < MIN_FARK:  # herkesin gördüğü değil, piyasanın küçümsediği
                continue
            # sıralama: form piyasadan ne kadar iyimser + piyasanın kendi güveni
            score = (sup["oran_form"] - leg["p"]) + 0.3 * leg["p"]
            t = dt.datetime.fromtimestamp(fx["ts"], TZ).strftime("%H:%M")
            pool.append({**leg, **sup, "sira_puan": round(score, 4), "fixture_id": fid, "saat": t, "ts": fx["ts"],
                         "mac": f'{fx["ev"]} - {fx["dep"]}', "ev": fx["ev"], "dep": fx["dep"], "lig": fx["lig"],
                         "etiket": BACAKLAR[leg["tur"]][0].format(line=leg["cizgi"]), "sonuc": "bekliyor"})
    pool.sort(key=lambda x: -x["sira_puan"])
    used, coupons = set(), []
    for kid, title, types in KUPONLAR:
        legs = []
        for leg in pool:
            if leg["tur"] in types and leg["fixture_id"] not in used and leg["fixture_id"] not in {l["fixture_id"] for l in legs}:
                # karışık kupon gerçekten karışık olsun: aynı aileden (gol / alt / korner) en fazla 2 maç
                if kid == "karisik" and sum(AILE[l["tur"]] == AILE[leg["tur"]] for l in legs) >= 2:
                    continue
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
        sfx = "" if parti == KUPON_SAATLER[0] else f"-{parti}"
        coupons.append({"tarih": book.today, "id": f"{book.today}-{kid}{sfx}", "parti": parti, "tur": kid,
                        "baslik": title + ("" if parti == KUPON_SAATLER[0] else " (akşam)"),
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
              f"   📋 Bu sezon son maçlarında: {l['ev']} {hh}/{hn}, {l['dep']} {ah}/{an}",
              (f"   🤝 Aralarındaki son {l['h2h'][1]} maç (2 sezon): {l['h2h'][0]}/{l['h2h'][1]}" if l.get("h2h")
               else "   🤝 Son 2 sezonda aralarında maç yok"),
              f"   📈 İki takımın son {hn + an} maçından {hh + ah} tanesi bu sonuçla bitti "
              f"(%{round(l['oran_form'] * 100)}), piyasa %{round(l['p'] * 100)} ihtimal veriyor", ""]
    L += [f"💰 Toplam oran: {c['oran']:.2f}",
          f"📊 Piyasaya göre tutma ihtimali: ~%{round(c['p_piyasa'] * 100)}",
          "━━━━━━━━━━━━━━",
          "ℹ️ Deneme aşamasındadır: maç öncesi seçimlerin piyasayı geçtiği henüz kanıtlanmadı, sonuçlar ölçülüyor.",
          "ℹ️ Bahis tavsiyesi değildir."]
    return "\n".join(L)


def maybe_send(api, book, fb, force: bool = False):
    """Her kupon partisi (KUPON_SAATLER) için günde bir kez: o partinin kuponları yoksa hazırla ve gönder."""
    now = dt.datetime.now(TZ)
    due = [h for h in KUPON_SAATLER if now.hour >= h]
    if not due:
        return
    parti = due[-1]
    rows = load()
    today = [r for r in rows if r["tarih"] == book.today]
    if any(r.get("parti", KUPON_SAATLER[0]) == parti for r in today) and not force:
        return
    skip = {l["fixture_id"] for r in today for l in r.get("bacaklar") or []}
    try:
        coupons = build(api, book, fb, parti=parti, skip=skip)
    except Exception as e:
        log("Kupon hazırlanamadı:", e)
        return
    if not coupons:  # boş parti de işaretlensin ki her oturumda tekrar denenmesin
        coupons = [{"tarih": book.today, "id": f"{book.today}-yok-{parti}", "parti": parti, "tur": "yok",
                    "bacaklar": [], "sonuc": "yok", "tg": None}]
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


YENILE = DATA / "kontrol" / "kupon_yenile"


def maybe_rebuild(api, book, fb):
    """data/kontrol/kupon_yenile dosyasında bugünün tarihi varsa: bugünün henüz başlamamış kuponlarını gruptan
    sil, kayıttan çıkar ve güncel kurallarla yeniden hazırla. Bir kez yapılır (dosyaya 'yapildi' yazılır)."""
    try:
        cmd = YENILE.read_text().strip()
    except FileNotFoundError:
        return
    if cmd != book.today:
        return
    YENILE.write_text(f"{book.today} yapildi\n")
    rows = load()
    now = dt.datetime.now(TZ).timestamp()
    drop = [c for c in rows if c["tarih"] == book.today and c.get("bacaklar")
            and all(l["sonuc"] == "bekliyor" and l["ts"] > now for l in c["bacaklar"])]
    drop += [c for c in rows if c["tarih"] == book.today and not c.get("bacaklar")]  # "yok" işaretleri
    chat = telegram_chat_id()
    for c in drop:
        if c.get("tg") and chat:
            try:
                r = _tg("deleteMessage", chat_id=chat, message_id=c["tg"])
                log("Kupon mesajı silindi:", c["id"], r.get("ok"), r.get("description", ""))
            except Exception as e:
                log("Kupon mesajı silinemedi:", c["id"], e)
    ids = {c["id"] for c in drop}
    save([c for c in rows if c["id"] not in ids])
    log(f"Kupon yenileme: {len(ids)} kupon silindi, yeniden hazırlanıyor")
    if any(c.get("bacaklar") for c in drop):
        notify("♻️ Kuponlar güncellendi", "♻️ Bugünün kuponları yeni kurallarla (bu sezonun son 10 maçı + "
               "aralarındaki maçlar) yeniden hazırlandı. Önceki kuponlar geçersizdir.", priority=3)
    maybe_send(api, book, fb, force=True)


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


def _stats(it: dict) -> dict:
    """Maç istatistikleri: {'ev': {...}, 'dep': {...}}"""
    out = {}
    for side, b in zip(("ev", "dep"), it.get("statistics") or []):
        out[side] = {s.get("type"): formmod._num(s.get("value")) for s in b.get("statistics") or []}
    return out


def _goal_minutes(it: dict) -> list[str]:
    L = []
    for e in it.get("events") or []:
        if e.get("type") == "Goal" and "Missed" not in (e.get("detail") or ""):
            t = e.get("time") or {}
            m = f"{t.get('elapsed')}'" + (f"+{t['extra']}" if t.get("extra") else "")
            L.append(f"{m} {(e.get('team') or {}).get('name', '')}".strip())
    return L


def explain(leg: dict, it: dict) -> str:
    """Tutmayan bir bahsin neden tutmadığını kısa ve somut anlatır."""
    ft = (it.get("score") or {}).get("fulltime") or it.get("goals") or {}
    gh, ga = ft.get("home") or 0, ft.get("away") or 0
    tot = gh + ga
    st = _stats(it)
    goals = _goal_minutes(it)
    def shots():
        if not st:
            return ""
        e, d = st.get("ev", {}), st.get("dep", {})
        x = ""
        if e.get("expected_goals") or d.get("expected_goals"):
            x = f", xG {e.get('expected_goals', 0):.1f}-{d.get('expected_goals', 0):.1f}"
        return (f" Şutlar {int(e.get('Total Shots', 0))}-{int(d.get('Total Shots', 0))}, isabetli "
                f"{int(e.get('Shots on Goal', 0))}-{int(d.get('Shots on Goal', 0))}{x}.")
    t = leg["tur"]
    if t in ("ust15", "ust25"):
        need = 2 if t == "ust15" else 3
        why = f"{need} gol gerekiyordu, maçta {tot} gol oldu ({gh}-{ga})."
        return why + shots() + (" Takımlar pozisyona girdi ama golü bulamadı." if st and
                                (st.get('ev', {}).get('Shots on Goal', 0) + st.get('dep', {}).get('Shots on Goal', 0)) >= 8 else "")
    if t in ("alt25", "alt35"):
        mx = 2 if t == "alt25" else 3
        why = f"En fazla {mx} gol olmalıydı, maçta {tot} gol oldu ({gh}-{ga})."
        if goals:
            why += f" Gol dakikaları: {', '.join(goals)}."
            if len(goals) > mx:
                why += f" Bahsi kaybettiren {mx + 1}. gol {goals[mx].split()[0].rstrip(chr(39))}. dakikada geldi."
        return why
    if t == "kg":
        miss = [n for n, g in ((leg["ev"], gh), (leg["dep"], ga)) if g == 0]
        who = "İki takım da" if len(miss) == 2 else miss[0]
        return f"{who} gol atamadı ({gh}-{ga})." + shots()
    if t == "korner":
        e, d = st.get("ev", {}), st.get("dep", {})
        ce, cd = int(e.get("Corner Kicks", 0)), int(d.get("Corner Kicks", 0))
        need = int(leg["cizgi"] + 0.5)
        return f"En az {need} korner gerekiyordu, {ce + cd} korner oldu ({leg['ev']} {ce}, {leg['dep']} {cd})."
    return ""


ICON = {"isabet": "✅", "ıska": "❌", "bilinmiyor": "❔", "iptal": "➖", "bekliyor": "⏳"}


def _line(l: dict) -> str:
    return f"{ICON.get(l['sonuc'], '❔')} {l['mac']}: {l['etiket']} ({l.get('skor') or ('oynanıyor/başlamadı' if l['sonuc'] == 'bekliyor' else 'sonuç yok')})"


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
                it = res[l["fixture_id"]]
                r, skor = _leg_result(l, it)
                if r:
                    l["sonuc"], l["skor"] = r, skor
                    if r == "ıska":
                        try:
                            l["neden"] = explain(l, it)
                        except Exception as e:
                            l["neden"] = ""
                            log("Kupon açıklaması:", e)
                    changed = True
        pending = [l for l in c["bacaklar"] if l["sonuc"] == "bekliyor"]
        lost = [l for l in c["bacaklar"] if l["sonuc"] == "ıska"]
        if pending:
            if lost and not c.get("yatti"):  # kupon yattı: diğer maçlar beklenmeden hemen duyur
                c["yatti"] = True
                changed = True
                body = "\n".join(["❌ Kupon yattı", *[_line(l) for l in c["bacaklar"]], "",
                                  *[f"📉 {l['mac']}: {l['neden']}" for l in lost if l.get("neden")],
                                  "", f"⏳ Kalan {len(pending)} maç bitince tam özet gelecek."])
                telegram_reply(c.get("tg"), body, fallback=(f"🧪 {c['baslik']}: kupon yattı", body))
                notify(f"🧪 {c['baslik']}: kupon yattı", body, priority=3, telegram=False)
            continue
        live = [l for l in c["bacaklar"] if l["sonuc"] in ("isabet", "ıska")]
        if not live:
            c["sonuc"], c["kar"] = "iptal", 0
        else:
            odd = 1.0
            for l in live:
                odd *= l["oran"]
            ok = not lost
            c["sonuc"] = "isabet" if ok else "ıska"
            c["oran_gercek"] = round(odd, 2)
            c["kar"] = round(100 * (odd - 1)) if ok else -100
        changed = True
        n_hit = sum(l["sonuc"] == "isabet" for l in live)
        head = {"isabet": f"✅ KUPON TUTTU! Oran {c.get('oran_gercek', c['oran']):.2f}",
                "ıska": (f"📋 Kupon özeti: {len(live)} maçtan {n_hit} tanesi tuttu, kupon kaybetti" if n_hit
                        else f"📋 Kupon özeti: {len(live)} maçın hiçbiri tutmadı"),
                "iptal": "➖ Kupon iptal (maçlar oynanmadı)"}[c["sonuc"]]
        L = [head, *[_line(l) for l in c["bacaklar"]]]
        if any(l["sonuc"] in ("iptal", "bilinmiyor") for l in c["bacaklar"]) and c["sonuc"] != "iptal":
            L.append("➖ Oynanmayan/sonucu bilinmeyen maç kupondan düşüldü.")
        if lost:
            L += ["", "🔎 Neden kaybetti?"]
            L += [f"• {l['mac']} ({l['etiket']}): {l.get('neden') or 'tutmadı.'}" for l in lost]
            L.append(f"• Seçimde piyasa bu maç(lar)a %{', %'.join(str(round(l['p'] * 100)) for l in lost)} "
                     "ihtimal veriyordu; bu sezonki form ve aralarındaki maçlar da destekliyordu ama bu sefer tutmadı.")
        body = "\n".join(L)
        if c.get("yatti"):  # yattı mesajı zaten alıntıladı: özet yeni mesaj olarak
            notify(f"🧪 {c['baslik']}: tam özet", body, priority=3)
        else:
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
