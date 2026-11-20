"""Maç öncesi modül.

* OddsBook: günün maç öncesi oranlarını (1X2, Üst/Alt 2.5, KG) tutar,
  ilk görülen ve son oranı karşılaştırarak oran düşüşlerini bulur.
* Sabah raporu: takım istatistiklerinden Poisson (Dixon-Coles düzeltmeli)
  modeliyle olasılıklar, API-Football'un kendi tahmini ve piyasa
  karşılaştırması. Değerli görünen seçimler kaydedilir, sonuçları sonraki
  oturumlarda otomatik işaretlenir.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math

from main import DATA, FINISHED, PRIORITY, TZ, log, notify

API = "https://v3.football.api-sports.io"
BOOKMAKER = 8  # Bet365
ODDS_DIR = DATA / "oranlar"
REPORT_DIR = DATA / "rapor"
PICKS_CSV = DATA / "maconcesi.csv"
PICK_FIELDS = ["tarih", "fixture_id", "saat", "lig", "mac", "secim", "model", "api",
               "piyasa", "oran", "sonuc", "skor", "kar"]
import os
DROP_PCT = 0.07                                                 # raporlarda "düşüş" sayılan oran
ALERT_DROP_PCT = float(os.environ.get("ALERT_DROP_PCT", "0.12"))  # anlık uyarı eşiği
MAX_REPORT_MATCHES = 40
VALUE_EDGE = 0.05      # harmanlanmış olasılık piyasadan en az bu kadar yüksek olmalı
MODEL_WEIGHT = 0.5     # harman: model %50 + piyasa %50 (piyasa genelde iyi bilgilidir)
MAX_RAW_GAP = 0.22     # model ile piyasa bundan fazla ayrışıyorsa veri şüphelidir, alma
SHRINK_GAMES = 6


def _load(path):
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


PARAMS = _load(DATA / "model" / "params.json")
RATINGS = _load(DATA / "model" / "takimlar.json")
if PARAMS.get("on_mac"):
    MODEL_WEIGHT = float(PARAMS["on_mac"].get("model_agirligi", MODEL_WEIGHT))
RATING_K = (PARAMS.get("on_mac") or {}).get("shrink", 10)


def rating_lambdas(fx: dict):
    """Geçmiş veriden hesaplanan takım puanlarıyla (zamanında tahmin modeliyle aynı formül)."""
    T = RATINGS.get(str(fx["lig_id"]))
    if not T:
        return None
    H, A = T.get(str(fx["ev_id"])), T.get(str(fx["dep_id"]))
    if not H or not A or H["nh"] < 3 or A["na"] < 3:
        return None
    k, mh, ma = RATING_K, T["_mh"], T["_ma"]
    att_h = (H["hf"] + k * mh) / (H["nh"] + k)
    def_a = (A["aa"] + k * mh) / (A["na"] + k)
    att_a = (A["af"] + k * ma) / (A["na"] + k)
    def_h = (H["ha"] + k * ma) / (H["nh"] + k)
    return max(0.15, att_h * def_a / mh), max(0.15, att_a * def_h / ma), "geçmiş veri"


def _f(x):
    try:
        return float(str(x).rstrip("%"))
    except (TypeError, ValueError):
        return None


def fair(odds: dict) -> dict:
    inv = {k: 1 / v for k, v in odds.items() if v}
    s = sum(inv.values())
    return {k: v / s for k, v in inv.items()} if s else {}


# ---------------------------------------------------------------- oran defteri
class OddsBook:
    def __init__(self, api, today: str):
        self.api = api
        self.today = today
        self.path = ODDS_DIR / f"{today}-v2.json"
        self.data: dict = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.fixtures: dict = {}  # fid -> {ev, dep, lig, lig_id, ts, durum, sezon}

    def load_fixtures(self):
        try:
            fxs = self.api.get("fixtures", date=self.today, timezone="Europe/Istanbul")
        except Exception as e:
            log("Fikstür alınamadı:", e)
            return
        for fx in fxs:
            self.fixtures[str(fx["fixture"]["id"])] = {
                "ev": fx["teams"]["home"]["name"], "dep": fx["teams"]["away"]["name"],
                "ev_id": fx["teams"]["home"]["id"], "dep_id": fx["teams"]["away"]["id"],
                "lig": f'{fx["league"]["country"]} - {fx["league"]["name"]}',
                "lig_id": fx["league"]["id"], "sezon": fx["league"].get("season"),
                "ts": fx["fixture"]["timestamp"], "durum": fx["fixture"]["status"]["short"],
                "hakem": fx["fixture"].get("referee"),
            }

    def refresh(self, max_pages: int = 80):
        page, pages, n = 1, 1, 0
        stamp = dt.datetime.now(TZ).strftime("%H:%M")
        while page <= pages and page <= max_pages:
            try:
                r = self.api.s.get(f"{API}/odds", params={
                    "date": self.today, "bookmaker": BOOKMAKER,
                    "timezone": "Europe/Istanbul", "page": page}, timeout=40)
                self.api.used += 1
                rem = r.headers.get("x-ratelimit-requests-remaining")
                if rem and rem.isdigit():
                    self.api.remaining = int(rem)
                body = r.json()
            except Exception as e:
                log("Oran sayfası hatası:", e)
                break
            if body.get("errors"):
                log("Oran API hatası:", body["errors"])
                break
            pages = (body.get("paging") or {}).get("total", 1) or 1
            for it in body.get("response") or []:
                fid = str(it["fixture"]["id"])
                cur = self._parse(it)
                if not cur:
                    continue
                rec = self.data.setdefault(fid, {"ilk": cur, "ilk_saat": stamp})
                for k, v in cur.items():  # sonradan gelen pazarları ilk'e ekle
                    rec["ilk"].setdefault(k, v)
                rec["son"] = cur
                rec["son_saat"] = stamp
                rec["ts"] = it["fixture"].get("timestamp")
                n += 1
            page += 1
        ODDS_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False))
        log(f"Oran defteri: {n} maç, {page - 1} sayfa")

    @staticmethod
    def _parse(it: dict) -> dict:
        out = {}
        for bm in it.get("bookmakers") or []:
            for bet in bm.get("bets") or []:
                vals = {v.get("value"): _f(v.get("odd")) for v in bet.get("values") or []}
                bid = bet.get("id")
                if bid == 1 and all(vals.get(k) for k in ("Home", "Draw", "Away")):
                    out["1x2"] = {"ev": vals["Home"], "x": vals["Draw"], "dep": vals["Away"]}
                elif bid == 5 and vals.get("Over 2.5") and vals.get("Under 2.5"):
                    out["ou"] = {"ust": vals["Over 2.5"], "alt": vals["Under 2.5"]}
                elif bid == 8 and vals.get("Yes") and vals.get("No"):
                    out["kg"] = {"var": vals["Yes"], "yok": vals["No"]}
            break
        return out

    # --- türetilmiş değerler
    def mu(self, fid) -> float | None:
        rec = self.data.get(str(fid))
        if not rec or "ou" not in rec.get("son", {}):
            return None
        from pro import mu_from_over25
        p = fair(rec["son"]["ou"])["ust"]
        return mu_from_over25(p)

    def drops(self, fid) -> dict:
        rec = self.data.get(str(fid))
        if not rec:
            return {}
        out = {}
        for mk, keys in (("ou", ("ust", "alt")), ("1x2", ("ev", "x", "dep")), ("kg", ("var", "yok"))):
            a, b = rec["ilk"].get(mk), rec.get("son", {}).get(mk)
            if not a or not b:
                continue
            for k in keys:
                if a.get(k) and b.get(k):
                    out[f"{mk}:{k}"] = round((a[k] - b[k]) / a[k], 3)
        return out

    def over_drop(self, fid) -> float:
        return self.drops(fid).get("ou:ust", 0.0)


LABELS = {"ou:ust": "Üst 2.5", "ou:alt": "Alt 2.5", "1x2:ev": "Ev sahibi", "1x2:x": "Beraberlik",
          "1x2:dep": "Deplasman", "kg:var": "KG Var", "kg:yok": "KG Yok"}


def drop_alerts(book: OddsBook, alerted: set) -> list[str]:
    """Başlamasına 3 saatten az kalan maçlarda yeni oran düşüşleri.
    Gönderilenler dosyada tutulur, böylece sonraki oturum aynı uyarıyı tekrarlamaz."""
    seen_path = ODDS_DIR / f"{book.today}-uyarilar.json"
    try:
        alerted |= set(json.loads(seen_path.read_text()))
    except Exception:
        pass
    now = dt.datetime.now(TZ).timestamp()
    lines = []
    for fid, rec in book.data.items():
        ts = rec.get("ts") or (book.fixtures.get(fid) or {}).get("ts")
        if not ts or not (0 < ts - now < 3 * 3600):
            continue
        for key, d in book.drops(fid).items():
            if d >= ALERT_DROP_PCT and f"{fid}:{key}" not in alerted:
                alerted.add(f"{fid}:{key}")
                fx = book.fixtures.get(fid, {})
                mk, k = key.split(":")
                a, b = rec["ilk"][mk][k], rec["son"][mk][k]
                t = dt.datetime.fromtimestamp(ts, TZ).strftime("%H:%M")
                lines.append(f"🕖 {t}  {fx.get('ev', '?')} - {fx.get('dep', '?')}\n"
                             f"   📉 {LABELS[key]}: {a:.2f} → {b:.2f} (▼%{round(d * 100)})")
    if lines:
        ODDS_DIR.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps(sorted(alerted)))
    return lines


# ---------------------------------------------------------------- Poisson modeli
def poisson_pmf(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def dc_tau(x, y, lh, la, rho=-0.08):
    if x == 0 and y == 0:
        return 1 - lh * la * rho
    if x == 0 and y == 1:
        return 1 + lh * rho
    if x == 1 and y == 0:
        return 1 + la * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


def score_matrix(lh, la, n=10):
    m = [[poisson_pmf(i, lh) * poisson_pmf(j, la) * dc_tau(i, j, lh, la) for j in range(n)]
         for i in range(n)]
    s = sum(map(sum, m))
    return [[v / s for v in row] for row in m]


def model_probs(lh, la) -> dict:
    m = score_matrix(lh, la)
    n = len(m)
    ev = sum(m[i][j] for i in range(n) for j in range(n) if i > j)
    x = sum(m[i][i] for i in range(n))
    dep = 1 - ev - x
    ust = sum(m[i][j] for i in range(n) for j in range(n) if i + j >= 3)
    kg = sum(m[i][j] for i in range(1, n) for j in range(1, n))
    scores = sorted(((m[i][j], f"{i}-{j}") for i in range(6) for j in range(6)), reverse=True)[:3]
    return {"1x2:ev": ev, "1x2:x": x, "1x2:dep": dep, "ou:ust": ust, "ou:alt": 1 - ust,
            "kg:var": kg, "kg:yok": 1 - kg, "skorlar": scores}


def team_lambdas(api, fx: dict, cache: dict) -> tuple[float, float, str]:
    def stats(team_id):
        key = (fx["lig_id"], fx["sezon"], team_id)
        if key not in cache:
            try:
                cache[key] = api.get("teams/statistics", league=fx["lig_id"],
                                     season=fx["sezon"], team=team_id)
            except Exception as e:
                log("Takım istatistiği hatası:", e)
                cache[key] = {}
        return cache[key] or {}

    h, a = stats(fx["ev_id"]), stats(fx["dep_id"])
    try:
        hp = h["fixtures"]["played"]["home"]
        ap = a["fixtures"]["played"]["away"]
        if (hp or 0) < 3 or (ap or 0) < 3:
            raise KeyError
        h_for = _f(h["goals"]["for"]["average"]["home"])
        h_against = _f(h["goals"]["against"]["average"]["home"])
        a_for = _f(a["goals"]["for"]["average"]["away"])
        a_against = _f(a["goals"]["against"]["average"]["away"])
        # Az maçlı takımlarda ortalamayı lig ortalamasına doğru çek (shrinkage).
        wh, wa = hp / (hp + SHRINK_GAMES), ap / (ap + SHRINK_GAMES)
        h_for = wh * h_for + (1 - wh) * 1.45
        h_against = wh * h_against + (1 - wh) * 1.15
        a_for = wa * a_for + (1 - wa) * 1.15
        a_against = wa * a_against + (1 - wa) * 1.45
        lh = max(0.2, (h_for + a_against) / 2)
        la = max(0.2, (a_for + h_against) / 2)
        return lh, la, "takım istatistiği"
    except (KeyError, TypeError):
        return 1.45, 1.15, "varsayılan"


# ---------------------------------------------------------------- tahmin kayıtları
def load_picks() -> list[dict]:
    if not PICKS_CSV.exists():
        return []
    with PICKS_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_picks(rows):
    DATA.mkdir(exist_ok=True)
    with PICKS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PICK_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in PICK_FIELDS})


def pick_won(secim: str, gh: int, ga: int) -> bool:
    return {"Ev sahibi": gh > ga, "Beraberlik": gh == ga, "Deplasman": gh < ga,
            "Üst 2.5": gh + ga >= 3, "Alt 2.5": gh + ga <= 2,
            "KG Var": gh > 0 and ga > 0, "KG Yok": gh == 0 or ga == 0}[secim]


def resolve_picks(api):
    rows = load_picks()
    pending = [r for r in rows if r["sonuc"] == "bekliyor"]
    if not pending:
        return
    ids = sorted({r["fixture_id"] for r in pending})
    results = {}
    for i in range(0, len(ids), 20):
        try:
            for it in api.get("fixtures", ids="-".join(ids[i:i + 20])):
                results[str(it["fixture"]["id"])] = it
        except Exception as e:
            log("Tahmin sonucu alınamadı:", e)
    changed = 0
    for r in pending:
        it = results.get(r["fixture_id"])
        if not it or it["fixture"]["status"]["short"] not in FINISHED:
            continue
        ft = it["score"].get("fulltime") or it["goals"]
        gh, ga = ft.get("home"), ft.get("away")
        if gh is None or ga is None:
            continue
        r["skor"] = f"{gh}-{ga}"
        r["sonuc"] = "isabet" if pick_won(r["secim"], gh, ga) else "ıska"
        r["kar"] = round(100 * (float(r["oran"]) - 1)) if r["sonuc"] == "isabet" else -100
        changed += 1
    if changed:
        save_picks(rows)
        log(f"{changed} maç öncesi tahminin sonucu işlendi")


# ---------------------------------------------------------------- sabah raporu
def morning_report(api, book: OddsBook):
    now = dt.datetime.now(TZ)
    cands = []
    for fid, fx in book.fixtures.items():
        rec = book.data.get(fid)
        if fx["durum"] != "NS" or not rec or "1x2" not in rec.get("son", {}):
            continue
        if fx["ts"] < now.timestamp():
            continue
        cands.append((fid, fx, rec))
    cands.sort(key=lambda c: (-PRIORITY.get(c[1]["lig_id"], 10), c[1]["ts"]))
    cands = cands[:MAX_REPORT_MATCHES]
    cache, rows, picks = {}, [], []
    for fid, fx, rec in cands:
        rl = rating_lambdas(fx)
        lh, la, src = rl if rl else team_lambdas(api, fx, cache)
        mp = model_probs(lh, la)
        try:
            pr = api.get("predictions", fixture=fid)
            pct = pr[0]["predictions"]["percent"] if pr else {}
            api_p = {"1x2:ev": (_f(pct.get("home")) or 0) / 100,
                     "1x2:x": (_f(pct.get("draw")) or 0) / 100,
                     "1x2:dep": (_f(pct.get("away")) or 0) / 100}
        except Exception:
            api_p = {}
        market = {}
        son = rec["son"]
        if "1x2" in son:
            market.update({f"1x2:{k}": v for k, v in fair(son["1x2"]).items()})
        if "ou" in son:
            market.update({f"ou:{k}": v for k, v in fair(son["ou"]).items()})
        if "kg" in son:
            market.update({f"kg:{k}": v for k, v in fair(son["kg"]).items()})
        odds_of = {f"{mk}:{k}": v for mk, d in son.items() for k, v in d.items()}
        t = dt.datetime.fromtimestamp(fx["ts"], TZ).strftime("%H:%M")
        name = f'{fx["ev"]} - {fx["dep"]}'
        mu = book.mu(fid)
        rows.append({"saat": t, "mac": name, "lig": fx["lig"], "lh": lh, "la": la, "kaynak": src,
                     "mp": mp, "api": api_p, "piyasa": market, "mu": mu, "drops": book.drops(fid)})
        if src == "varsayılan" or MODEL_WEIGHT <= 0:
            continue
        for key, p_mkt in market.items():
            p_mod = mp.get(key)
            odd = odds_of.get(key)
            if p_mod is None or not odd or not (1.4 <= odd <= 4.0):
                continue
            if abs(p_mod - p_mkt) > MAX_RAW_GAP:
                continue
            p_mod = MODEL_WEIGHT * p_mod + (1 - MODEL_WEIGHT) * p_mkt
            edge = p_mod - p_mkt
            if edge < VALUE_EDGE:
                continue
            if key.startswith("1x2") and api_p and api_p.get(key, 0) < p_mkt:
                continue  # API tahmini aynı yönde değilse alma
            picks.append({"tarih": book.today, "fixture_id": fid, "saat": t, "lig": fx["lig"],
                          "mac": name, "secim": LABELS[key], "model": round(p_mod, 3),
                          "api": round(api_p.get(key, 0), 3) if api_p.get(key) else "",
                          "piyasa": round(p_mkt, 3), "oran": odd, "sonuc": "bekliyor", "skor": "",
                          "_edge": edge})
    picks.sort(key=lambda p: -p["_edge"])
    picks = picks[:8]
    # kaydet
    old = load_picks()
    known = {(r["fixture_id"], r["secim"]) for r in old}
    save_picks(old + [p for p in picks if (p["fixture_id"], p["secim"]) not in known])
    write_report_md(book.today, rows, picks)
    # bildirim
    hi_mu = sorted([r for r in rows if r["mu"]], key=lambda r: -r["mu"])[:5]
    drops = sorted(((r, r["drops"].get("ou:ust", 0)) for r in rows), key=lambda x: -x[1])
    drops = [(r, d) for r, d in drops if d >= DROP_PCT][:4]
    msg = []
    if hi_mu:
        msg.append("⚽ Gol beklentisi en yüksek:")
        msg += [f"{r['saat']} {r['mac']} (beklenen {r['mu']:.1f} gol)" for r in hi_mu]
    if MODEL_WEIGHT <= 0:
        msg.append("\nℹ️ Geri testte maç öncesi model bahis piyasasını geçemedi; bu yüzden 'değerli seçim' "
                   "önerilmiyor. Oranlar esas alınıyor.")
    if picks:
        msg.append("\n💡 Modelin piyasadan yüksek gördükleri:")
        msg += [f"{p['saat']} {p['mac']}: {p['secim']} @{p['oran']} "
                f"(model %{round(p['model'] * 100)}, piyasa %{round(p['piyasa'] * 100)})" for p in picks[:5]]
    if drops:
        msg.append("\n📉 Üst 2.5 oranı düşenler:")
        msg += [f"{r['saat']} {r['mac']} (%{round(d * 100)})" for r, d in drops]
    hist = [r for r in load_picks() if r["sonuc"] in ("isabet", "ıska")]
    if hist:
        w = sum(r["sonuc"] == "isabet" for r in hist)
        kar = sum(float(r.get("kar") or 0) for r in hist)
        msg.append(f"\nGeçmiş maç öncesi seçimler: {w}/{len(hist)} (%{100 * w // len(hist)}), "
                   f"100 TL sabit bahisle sanal kâr: {kar:+.0f} TL")
    msg.append("Tam rapor: depoda data/rapor/")
    notify("☀️ Günün maç öncesi raporu", "\n".join(msg) if msg else "Bugün raporlanacak maç yok.",
           priority=3, tags=["sunny"])
    log(f"Sabah raporu: {len(rows)} maç, {len(picks)} seçim")


def write_report_md(day, rows, picks):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    pc = lambda v: f"%{round(v * 100)}" if isinstance(v, (int, float)) else "-"
    out = [f"# Maç öncesi rapor – {day}", "",
           "Model: geçmiş veriden zamanında hesaplanan takım hücum/savunma puanlarıyla Poisson (Dixon-Coles düzeltmeli); geçmiş verisi olmayan liglerde bu sezonun takım istatistikleri. "
           "Seçimlerdeki 'Model' sütunu model ile piyasanın %50-%50 harmanıdır. "
           "API: API-Football tahmini. Piyasa: Bet365 oranından marjı temizlenmiş olasılık.", ""]
    if picks:
        out += ["## Modelin piyasadan yüksek gördükleri", "",
                "| Saat | Maç | Seçim | Oran | Model | Piyasa | API |", "|---|---|---|---|---|---|---|"]
        out += [f"| {p['saat']} | {p['mac']} | {p['secim']} | {p['oran']} | {pc(p['model'])} | "
                f"{pc(p['piyasa'])} | {pc(p['api']) if p['api'] != '' else '-'} |" for p in picks]
        out.append("")
    out += ["## Tüm maçlar", "",
            "| Saat | Lig | Maç | Beklenen gol (model) | 1 / X / 2 (model) | 1 / X / 2 (piyasa) | Üst 2.5 model / piyasa | KG Var model / piyasa | Olası skorlar | Oran düşüşü |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["saat"]):
        mp, mk = r["mp"], r["piyasa"]
        dr = ", ".join(f"{LABELS[k]} %{round(v * 100)}" for k, v in r["drops"].items() if v >= DROP_PCT) or "-"
        out.append(
            f"| {r['saat']} | {r['lig']} | {r['mac']} | {r['lh'] + r['la']:.2f}"
            f"{' (vars.)' if r['kaynak'] == 'varsayılan' else ''} | "
            f"{pc(mp['1x2:ev'])} / {pc(mp['1x2:x'])} / {pc(mp['1x2:dep'])} | "
            f"{pc(mk.get('1x2:ev'))} / {pc(mk.get('1x2:x'))} / {pc(mk.get('1x2:dep'))} | "
            f"{pc(mp['ou:ust'])} / {pc(mk.get('ou:ust'))} | {pc(mp['kg:var'])} / {pc(mk.get('kg:var'))} | "
            f"{', '.join(s for _, s in mp['skorlar'])} | {dr} |")
    (REPORT_DIR / f"{day}.md").write_text("\n".join(out) + "\n", encoding="utf-8")
