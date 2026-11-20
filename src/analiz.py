"""Geçmiş veri indirme ve model ayarlama.

1. İndir: seçili liglerin son 3 sezonundaki bitmiş maçlar (skor, hakem, gol ve kart
   dakikaları, korner/kart/faul/şut/xG toplamları) -> data/gecmis/maclar.jsonl.gz
2. Analiz:
   * Lig ortalamaları (gol, korner, kart, faul)
   * Gol ve kart zaman eğrileri, skor durumuna göre gol hızı
   * Hakem kart veritabanı
   * Maç öncesi modelin zamanında-tahmin (point-in-time) geri testi ve
     küçültme parametresi ayarı
   * Son 7 günün Bet365 oranlarıyla model-piyasa karşılaştırması ve sanal kâr-zarar
3. Çıktılar: data/model/params.json, data/model/takimlar.json, data/hakemler.json,
   data/analiz/rapor.md ve telefona özet bildirimi.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import os
import time
from collections import defaultdict

from main import API_KEY, DATA, TZ, Api, log, notify

HIST = DATA / "gecmis" / "maclar.jsonl.gz"
MODEL_DIR = DATA / "model"
REPORT = DATA / "analiz" / "rapor.md"
SUMMARY = DATA / "analiz" / "ozet.json"
API = "https://v3.football.api-sports.io"

LEAGUES = [
    203, 204, 206,                      # Türkiye
    39, 40, 41, 42, 45,                 # İngiltere
    140, 141, 135, 136, 78, 79, 61, 62, # İspanya, İtalya, Almanya, Fransa
    88, 89, 94, 144, 179, 197, 218, 207, 119, 103, 113, 106, 345, 283, 333, 235, 210, 172,
    2, 3, 848,                          # Avrupa kupaları
    71, 128, 253, 262, 307, 98, 292, 188, 169,
]
FINISHED = {"FT", "AET", "PEN"}


def season_list(today: dt.date) -> list[int]:
    y = today.year
    return [y - 2, y - 1, y]


# ---------------------------------------------------------------- indirme
def compact(it: dict) -> dict | None:
    fx, lg = it["fixture"], it["league"]
    if fx["status"]["short"] not in FINISHED:
        return None
    hid, aid = it["teams"]["home"]["id"], it["teams"]["away"]["id"]
    ft = (it.get("score") or {}).get("fulltime") or it["goals"]
    if ft.get("home") is None:
        return None
    st = {}
    for b in it.get("statistics") or []:
        side = "h" if b["team"]["id"] == hid else "a"
        for s in b.get("statistics") or []:
            st[f"{side}:{s.get('type')}"] = s.get("value")

    def num(k):
        v = st.get(k)
        try:
            return float(str(v).rstrip("%")) if v is not None else None
        except ValueError:
            return None

    goals, cards = [], []
    for ev in it.get("events") or []:
        t = ev.get("time") or {}
        m = t.get("elapsed") or 0
        side = "h" if (ev.get("team") or {}).get("id") == hid else "a"
        if ev.get("type") == "Goal" and "Missed" not in (ev.get("detail") or ""):
            own = ev.get("detail") == "Own Goal"
            goals.append([m, t.get("extra") or 0, side])
        elif ev.get("type") == "Card":
            cards.append([m, t.get("extra") or 0, side, (ev.get("detail") or "")[:1]])
    return {
        "id": fx["id"], "ts": fx["timestamp"], "lig": lg["id"], "sezon": lg["season"],
        "hakem": (fx.get("referee") or "").split(",")[0].strip(),
        "ev": hid, "dep": aid, "ev_ad": it["teams"]["home"]["name"], "dep_ad": it["teams"]["away"]["name"],
        "gh": ft["home"], "ga": ft["away"],
        "korner": [num("h:Corner Kicks"), num("a:Corner Kicks")],
        "sari": [num("h:Yellow Cards"), num("a:Yellow Cards")],
        "kirmizi": [num("h:Red Cards"), num("a:Red Cards")],
        "faul": [num("h:Fouls"), num("a:Fouls")],
        "sut": [num("h:Total Shots"), num("a:Total Shots")],
        "xg": [num("h:expected_goals"), num("a:expected_goals")],
        "goller": goals, "kartlar": cards,
    }


def load_hist() -> dict:
    out = {}
    if HIST.exists():
        with gzip.open(HIST, "rt", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                out[r["id"]] = r
    return out


def save_hist(rows: dict):
    HIST.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(HIST, "wt", encoding="utf-8") as f:
        for r in sorted(rows.values(), key=lambda r: r["ts"]):
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def download(api: Api, max_requests: int) -> dict:
    hist = load_hist()
    start_used = api.used
    today = dt.datetime.now(TZ).date()
    todo = []
    for lid in LEAGUES:
        for season in season_list(today):
            if api.used - start_used >= max_requests:
                break
            try:
                fxs = api.get("fixtures", league=lid, season=season)
            except Exception as e:
                log(f"lig {lid} sezon {season}: {e}")
                continue
            for fx in fxs:
                if fx["fixture"]["status"]["short"] in FINISHED and fx["fixture"]["id"] not in hist:
                    todo.append(fx["fixture"]["id"])
            time.sleep(0.15)
    log(f"İndirilecek yeni maç: {len(todo)}")
    for i in range(0, len(todo), 20):
        if api.used - start_used >= max_requests:
            log("İstek sınırına ulaşıldı, indirme durduruldu.")
            break
        try:
            for it in api.get("fixtures", ids="-".join(map(str, todo[i:i + 20]))):
                c = compact(it)
                if c:
                    hist[c["id"]] = c
        except Exception as e:
            log("detay hatası:", e)
        time.sleep(0.15)
        if i % 2000 == 0 and i:
            log(f"  {i}/{len(todo)}")
    save_hist(hist)
    log(f"Geçmiş veri: {len(hist)} maç, kullanılan istek {api.used - start_used}")
    return hist


# ---------------------------------------------------------------- yardımcılar
def pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def dc_probs(lh, la, rho=-0.08, n=10):
    def tau(x, y):
        if x == 0 and y == 0:
            return 1 - lh * la * rho
        if x == 0 and y == 1:
            return 1 + lh * rho
        if x == 1 and y == 0:
            return 1 + la * rho
        if x == 1 and y == 1:
            return 1 - rho
        return 1.0
    m = [[pois(i, lh) * pois(j, la) * tau(i, j) for j in range(n)] for i in range(n)]
    s = sum(map(sum, m))
    ev = sum(m[i][j] for i in range(n) for j in range(n) if i > j) / s
    x = sum(m[i][i] for i in range(n)) / s
    ust = sum(m[i][j] for i in range(n) for j in range(n) if i + j >= 3) / s
    kg = sum(m[i][j] for i in range(1, n) for j in range(1, n)) / s
    return {"ev": ev, "x": x, "dep": 1 - ev - x, "ust": ust, "kg": kg}


def ll(p):
    return -math.log(min(max(p, 1e-6), 1 - 1e-6))


def bucket(minute: int) -> int:
    return min(17, max(0, (min(minute, 90) - 1) // 5))


# ---------------------------------------------------------------- analizler
def league_stats(hist):
    acc = defaultdict(lambda: defaultdict(float))
    for r in hist.values():
        a = acc[r["lig"]]
        a["mac"] += 1
        a["gol"] += r["gh"] + r["ga"]
        if None not in r["korner"]:
            a["k_mac"] += 1
            a["korner"] += sum(r["korner"])
        if None not in r["sari"]:
            a["c_mac"] += 1
            a["kart"] += sum(r["sari"]) + sum(x or 0 for x in r["kirmizi"])
            a["faul"] += sum(x or 0 for x in r["faul"])
    out = {}
    for lid, a in acc.items():
        out[str(lid)] = {
            "mac": int(a["mac"]), "gol": round(a["gol"] / a["mac"], 3),
            "korner": round(a["korner"] / a["k_mac"], 2) if a["k_mac"] >= 20 else None,
            "kart": round(a["kart"] / a["c_mac"], 2) if a["c_mac"] >= 20 else None,
            "faul": round(a["faul"] / a["c_mac"], 1) if a["c_mac"] >= 20 else None,
        }
    return out


def timing_curves(hist):
    g = [0] * 18
    c = [0] * 18
    n = 0
    state_min = defaultdict(float)
    state_goal = defaultdict(float)
    late_min = defaultdict(float)
    late_card = defaultdict(float)
    for r in hist.values():
        n += 1
        for m, ex, side in r["goller"]:
            g[bucket(m)] += 1
        for m, ex, side, kind in r["kartlar"]:
            c[bucket(m)] += 1
        # skor durumu: dakika dakika
        gm = sorted((min(m, 90), side) for m, ex, side in r["goller"])
        cm = [min(m, 90) for m, *_ in r["kartlar"]]
        h = a = 0
        gi = 0
        for minute in range(1, 91):
            d = abs(h - a)
            st = "esit" if d == 0 else "fark1" if d == 1 else "fark2+"
            state_min[st] += 1
            if minute >= 70:
                late_min["yakin" if d <= 1 else "acik"] += 1
                late_card["yakin" if d <= 1 else "acik"] += cm.count(minute)
            while gi < len(gm) and gm[gi][0] == minute:
                state_goal[st] += 1
                if gm[gi][1] == "h":
                    h += 1
                else:
                    a += 1
                gi += 1
    tot_g, tot_c = sum(g) or 1, sum(c) or 1
    curve_g = [round(x / (tot_g / 18), 3) for x in g]
    curve_c = [round(x / (tot_c / 18), 3) for x in c]
    overall = sum(state_goal.values()) / max(1, sum(state_min.values()))
    state_mult = {k: round((state_goal[k] / state_min[k]) / overall, 3) for k in state_min if state_min[k] > 500}
    lr = {k: late_card[k] / late_min[k] for k in late_min if late_min[k] > 0}
    late_close = round(lr.get("yakin", 1) / lr.get("acik", 1), 3) if lr.get("acik") else 1.0
    return curve_g, curve_c, state_mult, late_close, n


def referee_db(hist, live_db: dict) -> dict:
    db = {"hakem": {}, "lig": {}}
    for r in hist.values():
        if None in r["sari"]:
            continue
        cards = sum(r["sari"]) + sum(x or 0 for x in r["kirmizi"])
        for key, name in (("hakem", r["hakem"]), ("lig", str(r["lig"]))):
            if name:
                rec = db[key].setdefault(name, {"mac": 0, "kart": 0})
                rec["mac"] += 1
                rec["kart"] += cards
    db["kaynak"] = "gecmis+canli"
    return db


# ---------------------------------------------------------------- maç öncesi geri test
def rate_pass(hist, k, store_preds=False):
    """Zamanında tahmin: her maçı yalnızca önceki maçların verisiyle tahmin eder."""
    by_ls = defaultdict(list)
    for r in hist.values():
        by_ls[(r["lig"], r["sezon"])].append(r)
    prev_final = {}
    loss = defaultdict(float)
    cnt = 0
    preds = {}
    teams_now = {}
    for (lid, season) in sorted(by_ls, key=lambda x: (x[0], x[1])):
        games = sorted(by_ls[(lid, season)], key=lambda r: r["ts"])
        prev = prev_final.get(lid, {})
        mh, ma = prev.get("_mh", 1.45), prev.get("_ma", 1.15)
        T = {}

        def team(t):
            if t not in T:
                p = prev.get(t)
                if p:
                    w = 4
                    T[t] = {"hf": p["hf"] / max(p["nh"], 1) * w, "ha": p["ha"] / max(p["nh"], 1) * w, "nh": w,
                            "af": p["af"] / max(p["na"], 1) * w, "aa": p["aa"] / max(p["na"], 1) * w, "na": w}
                else:
                    T[t] = {"hf": 0, "ha": 0, "nh": 0, "af": 0, "aa": 0, "na": 0}
            return T[t]

        sh = sa = ng = 0
        for r in games:
            H, A = team(r["ev"]), team(r["dep"])
            if ng >= 30:
                mh, ma = sh / ng, sa / ng
            att_h = (H["hf"] + k * mh) / (H["nh"] + k)
            def_a = (A["aa"] + k * mh) / (A["na"] + k)
            att_a = (A["af"] + k * ma) / (A["na"] + k)
            def_h = (H["ha"] + k * ma) / (H["nh"] + k)
            lh = max(0.15, att_h * def_a / mh)
            la = max(0.15, att_a * def_h / ma)
            p = dc_probs(lh, la)
            gh, ga = r["gh"], r["ga"]
            res = "ev" if gh > ga else "x" if gh == ga else "dep"
            if H["nh"] + A["na"] >= 4:  # ilk haftaları değerlendirmeye katma
                loss["1x2"] += ll(p[res])
                loss["ust"] += ll(p["ust"] if gh + ga >= 3 else 1 - p["ust"])
                loss["kg"] += ll(p["kg"] if gh > 0 and ga > 0 else 1 - p["kg"])
                cnt += 1
            if store_preds:
                preds[r["id"]] = {"p": p, "lh": lh, "la": la}
            H["hf"] += gh; H["ha"] += ga; H["nh"] += 1
            A["af"] += ga; A["aa"] += gh; A["na"] += 1
            sh += gh; sa += ga; ng += 1
        T["_mh"], T["_ma"] = mh, ma
        prev_final[lid] = T
        teams_now[(lid, season)] = T
    avg = {key: v / max(cnt, 1) for key, v in loss.items()}
    return avg, cnt, preds, teams_now


def prematch_backtest(hist):
    results = {}
    for k in (2, 4, 6, 10, 15, 25):
        avg, cnt, _, _ = rate_pass(hist, k)
        results[k] = avg
        log(f"  k={k}: 1X2 {avg.get('1x2', 0):.4f}  Ü2.5 {avg.get('ust', 0):.4f}  KG {avg.get('kg', 0):.4f}  ({cnt} maç)")
    best_k = min(results, key=lambda k: results[k].get("1x2", 9) + results[k].get("ust", 9))
    avg, cnt, preds, teams_now = rate_pass(hist, best_k, store_preds=True)
    # taban: sadece lig ortalaması (k çok büyük)
    base, _, _, _ = rate_pass(hist, 10 ** 6)
    return best_k, avg, base, cnt, preds, teams_now


def market_compare(api: Api, hist, preds):
    """Son 7 günün Bet365 oranlarıyla karşılaştırma ve sanal kâr-zarar."""
    today = dt.datetime.now(TZ).date()
    odds = {}
    for d in range(1, 8):
        day = (today - dt.timedelta(days=d)).isoformat()
        page, pages = 1, 1
        while page <= pages and page <= 60:
            try:
                r = api.s.get(f"{API}/odds", params={"date": day, "bookmaker": 8, "page": page,
                                                     "timezone": "Europe/Istanbul"}, timeout=40)
                api.used += 1
                body = r.json()
            except Exception as e:
                log("oran geçmişi hatası:", e)
                break
            pages = (body.get("paging") or {}).get("total", 1) or 1
            for it in body.get("response") or []:
                o = {}
                for bm in it.get("bookmakers") or []:
                    for bet in bm.get("bets") or []:
                        v = {x.get("value"): float(x["odd"]) for x in bet.get("values") or [] if x.get("odd")}
                        if bet.get("id") == 1 and all(k in v for k in ("Home", "Draw", "Away")):
                            o["1x2"] = (v["Home"], v["Draw"], v["Away"])
                        elif bet.get("id") == 5 and "Over 2.5" in v and "Under 2.5" in v:
                            o["ou"] = (v["Over 2.5"], v["Under 2.5"])
                        elif bet.get("id") == 8 and "Yes" in v and "No" in v:
                            o["kg"] = (v["Yes"], v["No"])
                odds[it["fixture"]["id"]] = o
            page += 1
            time.sleep(0.1)
    # oranları biriktir: her hafta yeni 7 gün eklenir, örneklem büyür
    store_path = MODEL_DIR / "piyasa_oranlari.json"
    try:
        store = json.loads(store_path.read_text()) if store_path.exists() else {}
    except Exception:
        store = {}
    for fid, o in odds.items():
        if o:
            store[str(fid)] = o
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, separators=(",", ":")))
    rows = []
    for fid, o in store.items():
        r, pr = hist.get(int(fid)), preds.get(int(fid))
        if not r or not pr or "1x2" not in o:
            continue
        rows.append((r, pr["p"], o))
    log(f"Piyasa karşılaştırması: {len(rows)} maç (oran bulunan {len(odds)})")
    if not rows:
        return None
    # olasılıklar
    def fair(t):
        inv = [1 / x for x in t]
        s = sum(inv)
        return [x / s for x in inv]
    out = {"mac": len(rows), "agirlik": {}}
    for w in (0.0, 0.25, 0.5, 0.75, 1.0):
        L = defaultdict(float)
        n = defaultdict(int)
        for r, p, o in rows:
            gh, ga = r["gh"], r["ga"]
            fm = fair(o["1x2"])
            res = 0 if gh > ga else 1 if gh == ga else 2
            pm = [p["ev"], p["x"], p["dep"]]
            L["1x2"] += ll(w * pm[res] + (1 - w) * fm[res]); n["1x2"] += 1
            if "ou" in o:
                fo = fair(o["ou"])[0]
                pu = w * p["ust"] + (1 - w) * fo
                L["ust"] += ll(pu if gh + ga >= 3 else 1 - pu); n["ust"] += 1
        out["agirlik"][w] = {k: round(L[k] / n[k], 4) for k in L}
    best_w = min(out["agirlik"], key=lambda w: out["agirlik"][w]["1x2"] + out["agirlik"][w].get("ust", 0))
    out["en_iyi_agirlik"] = best_w
    # sanal kâr-zarar: farklı eşiklerde seçim kuralı
    roi = {}
    for w in (0.25, 0.5):
        for edge in (0.03, 0.05, 0.08):
            stake = profit = bets = wins = 0
            for r, p, o in rows:
                gh, ga = r["gh"], r["ga"]
                cands = []
                fm = fair(o["1x2"])
                for i, key in enumerate(("ev", "x", "dep")):
                    cands.append((key, w * p[key] + (1 - w) * fm[i], fm[i], o["1x2"][i],
                                  (gh > ga, gh == ga, gh < ga)[i]))
                if "ou" in o:
                    fo = fair(o["ou"])
                    cands.append(("ust", w * p["ust"] + (1 - w) * fo[0], fo[0], o["ou"][0], gh + ga >= 3))
                    cands.append(("alt", w * (1 - p["ust"]) + (1 - w) * fo[1], fo[1], o["ou"][1], gh + ga <= 2))
                for key, pb, pmk, odd, won in cands:
                    if pb - pmk >= edge and 1.4 <= odd <= 4.0 and abs((pb - pmk) / w) <= 0.22:
                        bets += 1
                        stake += 100
                        if won:
                            wins += 1
                            profit += 100 * (odd - 1)
                        else:
                            profit -= 100
            roi[f"w{w}_e{edge}"] = {"bahis": bets, "kazanan": wins,
                                    "kar": round(profit), "roi": round(profit / stake, 4) if stake else None}
    out["roi"] = roi
    return out


# ---------------------------------------------------------------- ana
def run():
    api = Api(API_KEY)
    st = api.status()
    log("Plan:", (st.get("subscription") or {}).get("plan"), "| bugün:", st.get("requests"))
    max_req = int(os.environ.get("BACKFILL_REQUESTS", "6000"))
    hist = download(api, max_req)
    goals_curve, cards_curve, state_mult, late_close, n = timing_curves(hist)
    leagues = league_stats(hist)
    refs = referee_db(hist, {})
    log("Maç öncesi geri test:")
    best_k, avg, base, cnt, preds, teams_now = prematch_backtest(hist)
    mkt = market_compare(api, hist, preds)
    # parametreleri yaz
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    params = {
        "olusturma": dt.datetime.now(TZ).isoformat(timespec="minutes"), "mac_sayisi": n,
        "lig": leagues, "gol_egrisi": goals_curve, "kart_egrisi": cards_curve,
        "gol_durum": state_mult, "kart_gec_yakin": late_close,
        "on_mac": {"shrink": best_k,
                   # Piyasa örneklemi küçükse modele güvenme: kanıtlanana kadar piyasa esas (0.0).
                   "model_agirligi": (mkt or {}).get("en_iyi_agirlik", 0.0) if (mkt or {}).get("mac", 0) >= 300 else 0.0,
                   "piyasa_orneklem": (mkt or {}).get("mac", 0)},
    }
    (MODEL_DIR / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=1))
    teams_out = {}
    for (lid, season), T in teams_now.items():
        if season == max(s for (l2, s) in teams_now if l2 == lid):
            teams_out[str(lid)] = {"_mh": T["_mh"], "_ma": T["_ma"],
                                   **{str(t): v for t, v in T.items() if not str(t).startswith("_")}}
    (MODEL_DIR / "takimlar.json").write_text(json.dumps(teams_out, separators=(",", ":")))
    (DATA / "hakemler.json").write_text(json.dumps(refs, ensure_ascii=False))
    summary = {"params": params, "geri_test": {"k": best_k, "model": avg, "taban": base, "mac": cnt},
               "piyasa": mkt, "hakem_sayisi": len(refs["hakem"])}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    write_report(summary, leagues)
    # bildirim
    msg = [f"{n} geçmiş maç analiz edildi, {len(refs['hakem'])} hakem."]
    imp = 1 - avg["1x2"] / base["1x2"] if base.get("1x2") else 0
    msg.append(f"Maç öncesi model, lig ortalamasına göre 1X2 hatasını %{imp * 100:.1f} azaltıyor (k={best_k}).")
    if mkt:
        msg.append(f"Son 7 gün {mkt['mac']} maç: en iyi model ağırlığı {mkt['en_iyi_agirlik']}.")
        r = mkt["roi"].get("w0.5_e0.05") or {}
        if r.get("bahis"):
            msg.append(f"Seçim kuralı sanal sonucu: {r['bahis']} bahis, {r['kazanan']} kazanan, "
                       f"kâr {r['kar']} TL (ROI %{r['roi'] * 100:.1f})")
    msg.append("Ayrıntı: data/analiz/rapor.md")
    notify("🔬 Model analizi tamamlandı", "\n".join(msg), priority=3, tags=["microscope"])
    try:
        import panel
        panel.build()
    except Exception as e:
        log("Panel oluşturulamadı:", e)
    log("ANALİZ ÖZETİ\n" + json.dumps(summary, ensure_ascii=False, indent=1, default=str)[:6000])


def write_report(s, leagues):
    p, g = s["params"], s["geri_test"]
    out = [f"# Model analizi – {p['olusturma']}", "",
           f"Geçmiş maç sayısı: **{p['mac_sayisi']}**, hakem: **{s['hakem_sayisi']}**", "",
           "## Maç öncesi model (zamanında tahmin geri testi)", "",
           f"Değerlendirilen maç: {g['mac']}, seçilen küçültme parametresi k = **{g['k']}**", "",
           "| Ölçüt (log kayıp, düşük = iyi) | Model | Sadece lig ortalaması |", "|---|---|---|"]
    for key, label in (("1x2", "1/X/2"), ("ust", "Üst/Alt 2.5"), ("kg", "KG Var/Yok")):
        out.append(f"| {label} | {g['model'].get(key, 0):.4f} | {g['taban'].get(key, 0):.4f} |")
    m = s.get("piyasa")
    if m:
        out += ["", f"## Piyasa karşılaştırması (son 7 gün, {m['mac']} maç, Bet365)", "",
                "Model ile piyasa olasılığının harmanı; ağırlık 0 = sadece piyasa, 1 = sadece model.", "",
                "| Model ağırlığı | 1/X/2 log kayıp | Üst/Alt log kayıp |", "|---|---|---|"]
        for w, v in m["agirlik"].items():
            out.append(f"| {w} | {v.get('1x2', '-')} | {v.get('ust', '-')} |")
        out += ["", f"En iyi ağırlık: **{m['en_iyi_agirlik']}**", "",
                "### Seçim kuralının sanal sonucu (100 TL sabit bahis)", "",
                "| Kural | Bahis | Kazanan | Kâr (TL) | ROI |", "|---|---|---|---|---|"]
        for k, v in m["roi"].items():
            roi = f"%{v['roi'] * 100:.1f}" if v["roi"] is not None else "-"
            out.append(f"| {k} | {v['bahis']} | {v['kazanan']} | {v['kar']} | {roi} |")
    out += ["", "## Canlı model parametreleri", "",
            "Gol zaman eğrisi (5 dakikalık dilimler, 1 = ortalama):", "",
            " ".join(f"{i * 5 + 1}-{i * 5 + 5}': {v}" for i, v in enumerate(p["gol_egrisi"])), "",
            "Kart zaman eğrisi:", "",
            " ".join(f"{i * 5 + 1}-{i * 5 + 5}': {v}" for i, v in enumerate(p["kart_egrisi"])), "",
            f"Skor durumuna göre gol hızı: {p['gol_durum']}", "",
            f"70. dakikadan sonra yakın skorlu maçlarda kart hızı çarpanı: {p['kart_gec_yakin']}", "",
            "## Lig ortalamaları", "", "| Lig | Maç | Gol | Korner | Kart | Faul |", "|---|---|---|---|---|---|"]
    for lid, v in sorted(leagues.items(), key=lambda x: -x[1]["mac"]):
        out.append(f"| {lid} | {v['mac']} | {v['gol']} | {v['korner'] or '-'} | {v['kart'] or '-'} | {v['faul'] or '-'} |")
    REPORT.write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
