"""Gol Sinyali – Pro motoru (API-Football ücretli plan).

Ücretsiz motordan farkları:
  * Tüm canlı maçlar tek istekle taranır, istatistikler 20'şer maçlık
    gruplar halinde çekilir (günlük kotaya göre otomatik ölçeklenir).
  * Maç öncesi oranlardan beklenen gol sayısı (mu) hesaplanır,
    gün içindeki oran düşüşleri ("akıllı para" izi) tespit edilir.
  * Canlı oranlardan piyasanın "10 dakika içinde gol" olasılığı çıkarılır.
    Sinyal, modelin olasılığı piyasanınkinden belirgin yüksekse gelir.
  * Her canlı maç için 5 dakikada bir örnek kaydedilir; 10 dakika sonra
    gol olup olmadığı etiketlenir. Bu veri ileride modeli kalibre etmek için.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import math
import os
import time

from main import (
    DATA, FINISHED, LIVE, PRIORITY, REQUEST_RESERVE, TZ,
    Api, Claude, Match, evaluate_pressure, goal_events, fmt_delta, load_signals, log, notify,
    save_signals, signal_side,
)

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "350"))
EDGE = float(os.environ.get("EDGE", "0.04"))           # model - piyasa farkı eşiği
TARGET_INTERVAL = int(os.environ.get("INTERVAL", "30"))  # hedef tur aralığı (sn)
MAX_BATCHES = 15                                      # tur başına en fazla 15x20 = 300 maç
SAMPLE_EVERY = 5                                      # örnek kaydı (maç dakikası)
BOOKMAKER = int(os.environ.get("BOOKMAKER", "8"))     # 8 = Bet365
DROP_PCT = float(os.environ.get("DROP_PCT", "0.07"))  # %7 ve üzeri düşüş
MAX_CLAUDE = int(os.environ.get("MAX_CLAUDE_CALLS", "25"))
TYPICAL_TOTAL_PRESSURE = 12.0
DEFAULT_MU = 2.6

ODDS_DIR = DATA / "oranlar"
SAMPLES_DIR = DATA / "ornekler"
SAMPLE_FIELDS = ["tarih", "fixture_id", "lig_id", "dakika", "skor_farki", "toplam_gol",
                 "baski_ev", "baski_dep", "kirmizi_farki", "mu", "dusus", "p_model",
                 "p_piyasa", "gol_10dk"]


# ---------------------------------------------------------------- olasılık
def poisson_over(mu: float, k: int) -> float:
    """P(N >= k) for Poisson(mu)."""
    s, term = 0.0, math.exp(-mu)
    for i in range(k):
        s += term
        term *= mu / (i + 1)
    return 1.0 - s


def mu_from_over25(p_over: float) -> float:
    lo, hi = 0.3, 7.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if poisson_over(mid, 3) < p_over:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)


def fair_prob(odd_a: float, odd_b: float) -> float:
    """İki seçenekli pazarda marjı temizlenmiş A olasılığı."""
    ia, ib = 1 / odd_a, 1 / odd_b
    return ia / (ia + ib)


def remaining_minutes(minute: int) -> float:
    return max(4.0, 94.0 - minute)


def p_next10_from_rest(p_rest: float, minute: int) -> float:
    rem = remaining_minutes(minute)
    p_rest = min(max(p_rest, 0.001), 0.999)
    q = -math.log(1 - p_rest) / rem
    return 1 - math.exp(-q * min(10.0, rem))


# ---------------------------------------------------------------- oran ayrıştırma
def parse_prematch(item: dict) -> dict | None:
    """odds?bet=5 cevabından Over/Under 2.5 oranları."""
    for bm in item.get("bookmakers") or []:
        for bet in bm.get("bets") or []:
            vals = {v.get("value"): v.get("odd") for v in bet.get("values") or []}
            o, u = vals.get("Over 2.5"), vals.get("Under 2.5")
            if o and u:
                try:
                    return {"over": float(o), "under": float(u)}
                except ValueError:
                    return None
    return None


def parse_live_goal_market(item: dict, total_goals: int) -> float | None:
    """Canlı oranlardan 'en az bir gol daha' olasılığı (maçın kalanı için)."""
    line = total_goals + 0.5
    best = None
    for mk in item.get("odds") or []:
        name = (mk.get("name") or "").lower()
        if "over/under" not in name and "match goals" not in name and "total" not in name:
            continue
        if any(w in name for w in ("1st", "2nd", "half", "corner", "card", "home", "away", "team", "asian")):
            continue
        over = under = None
        for v in mk.get("values") or []:
            if v.get("suspended"):
                continue
            try:
                h = float(v.get("handicap")) if v.get("handicap") not in (None, "") else None
            except ValueError:
                h = None
            label = (v.get("value") or "").strip()
            if h is None:  # "Over 2.5" biçimi
                parts = label.split()
                if len(parts) == 2:
                    try:
                        h = float(parts[1])
                    except ValueError:
                        continue
                    label = parts[0]
            if h is None or abs(h - line) > 1e-6:
                continue
            try:
                odd = float(v.get("odd"))
            except (TypeError, ValueError):
                continue
            if label.lower().startswith("over"):
                over = odd
            elif label.lower().startswith("under"):
                under = odd
        if over and under:
            best = fair_prob(over, under)
            break
        if over and best is None:
            best = min(0.99, 1 / over / 1.05)
    return best


# ---------------------------------------------------------------- motor
class ProEngine:
    def __init__(self, api: Api, claude: Claude):
        self.api = api
        self.claude = claude
        self.now0 = dt.datetime.now(TZ)
        self.today = self.now0.date().isoformat()
        self.matches: dict[int, Match] = {}
        self.meta: dict[int, dict] = {}        # mu, dusus, p_piyasa, son_ornek
        self.pending_samples: list[dict] = []
        self.done_samples: list[dict] = []
        self.all_rows = load_signals()
        self.rows_today: list[dict] = []
        self.odds_live_ok = True
        self.tick_cost = 0

    # --- zaman ve bütçe
    def end_time(self) -> dt.datetime:
        reset = (self.now0 + dt.timedelta(days=1 if self.now0.hour >= 3 else 0)).replace(
            hour=2, minute=55, second=0, microsecond=0)
        return min(self.now0 + dt.timedelta(minutes=RUN_MINUTES), reset)

    def per_minute_budget(self) -> float:
        now = dt.datetime.now(TZ)
        reset = (now + dt.timedelta(days=1 if now.hour >= 3 else 0)).replace(
            hour=2, minute=55, second=0, microsecond=0)
        mins = max(10.0, (reset - now).total_seconds() / 60)
        rem = (self.api.remaining if self.api.remaining is not None else 500) - REQUEST_RESERVE * 10
        return max(0.0, rem) / mins

    # --- maç öncesi oranlar
    def load_prematch_odds(self):
        ODDS_DIR.mkdir(parents=True, exist_ok=True)
        path = ODDS_DIR / f"{self.today}.json"
        store = json.loads(path.read_text()) if path.exists() else {}
        page, pages, n = 1, 1, 0
        stamp = dt.datetime.now(TZ).strftime("%H:%M")
        while page <= pages and page <= 60:
            try:
                r = self.api.s.get(f"https://v3.football.api-sports.io/odds",
                                   params={"date": self.today, "bet": 5, "bookmaker": BOOKMAKER,
                                           "timezone": "Europe/Istanbul", "page": page}, timeout=30)
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
                o = parse_prematch(it)
                if not o:
                    continue
                rec = store.setdefault(fid, {"ilk": o, "ilk_saat": stamp})
                rec["son"] = o
                rec["son_saat"] = stamp
                n += 1
            page += 1
        path.write_text(json.dumps(store, ensure_ascii=False))
        drops = 0
        for fid, rec in store.items():
            first, last = rec["ilk"], rec.get("son", rec["ilk"])
            p_over = fair_prob(last["over"], last["under"])
            drop = (first["over"] - last["over"]) / first["over"]
            self.meta.setdefault(int(fid), {}).update(
                {"mu": mu_from_over25(p_over), "dusus": round(drop, 3)})
            drops += drop >= DROP_PCT
        log(f"Maç öncesi oran: {n} maç güncellendi ({page - 1} sayfa), "
            f"oran düşüşü olan: {drops}")

    # --- seçim
    def score(self, m: Match) -> float:
        meta = self.meta.get(m.id, {})
        s = meta.get("mu", DEFAULT_MU) * 25 + PRIORITY.get(m.league_id, 10) / 10
        if meta.get("dusus", 0) >= DROP_PCT:
            s += 20
        return s

    def ingest_live(self, items: list[dict]):
        seen = set()
        for it in items:
            fid = it["fixture"]["id"]
            seen.add(fid)
            m = self.matches.get(fid)
            if not m:
                m = Match(it)
                self.matches[fid] = m
            st = it["fixture"]["status"]
            m.status = st["short"]
            m.minute = st.get("elapsed") or m.minute
            m.gh = it["goals"]["home"] or 0
            m.ga = it["goals"]["away"] or 0
            if it.get("events"):
                m.goals = goal_events(it)
        for fid, m in self.matches.items():
            if fid not in seen and m.status in LIVE:
                m.status = "FT"  # canlı listeden düştü -> bitti say

    def tick(self):
        cost = 0
        live = self.api.get("fixtures", live="all")
        cost += 1
        self.ingest_live(live)
        live_ms = [m for m in self.matches.values() if m.status in LIVE]
        live_ms.sort(key=self.score, reverse=True)

        budget = self.per_minute_budget() * (TARGET_INTERVAL / 60)
        batches = min(MAX_BATCHES, max(1, int(budget) - (2 if self.odds_live_ok else 1)))
        tracked = live_ms[: batches * 20]
        for i in range(0, len(tracked), 20):
            chunk = tracked[i:i + 20]
            try:
                items = self.api.get("fixtures", ids="-".join(str(m.id) for m in chunk))
            except Exception as e:
                log("İstatistik hatası:", e)
                continue
            finally:
                cost += 1
            for it in items:
                m = self.matches.get(it["fixture"]["id"])
                if m:
                    m.update(it)

        if self.odds_live_ok:
            try:
                odds = self.api.get("odds/live")
                cost += 1
                for it in odds:
                    m = self.matches.get(it["fixture"]["id"])
                    if m:
                        p = parse_live_goal_market(it, m.gh + m.ga)
                        self.meta.setdefault(m.id, {})["p_rest"] = p
            except Exception as e:
                log("Canlı oran kapatıldı:", e)
                self.odds_live_ok = False

        for m in tracked:
            self.check_outcomes(m)
            self.label_samples(m)
            self.evaluate(m)
        self.tick_cost = cost
        return len(live_ms), len(tracked)

    # --- model
    def model_probs(self, m: Match, pr: dict) -> tuple[float, float | None]:
        meta = self.meta.get(m.id, {})
        mu = meta.get("mu", DEFAULT_MU)
        rate = mu / 94.0
        total = pr["home"] + pr["away"]
        f = min(3.0, max(0.55, 0.55 + 0.45 * total / TYPICAL_TOTAL_PRESSURE))
        late = 1.15 if m.minute >= 75 else 1.0
        rem = remaining_minutes(m.minute)
        p_model = 1 - math.exp(-rate * min(10.0, rem) * f * late)
        p_rest = meta.get("p_rest")
        p_mkt = p_next10_from_rest(p_rest, m.minute) if p_rest else None
        return p_model, p_mkt

    def evaluate(self, m: Match):
        pr = evaluate_pressure(m)
        if not pr:
            return
        p_model, p_mkt = self.model_probs(m, pr)
        meta = self.meta.setdefault(m.id, {})
        # örnek kaydı
        last = meta.get("son_ornek", -99)
        if m.minute - last >= SAMPLE_EVERY:
            meta["son_ornek"] = m.minute
            st = pr["cur"]["stats"]
            self.pending_samples.append({
                "tarih": self.today, "fixture_id": m.id, "lig_id": m.league_id,
                "dakika": m.minute, "skor_farki": m.gh - m.ga, "toplam_gol": m.gh + m.ga,
                "baski_ev": pr["home"], "baski_dep": pr["away"],
                "kirmizi_farki": st["home"]["red"] - st["away"]["red"],
                "mu": meta.get("mu", ""), "dusus": meta.get("dusus", ""),
                "p_model": round(p_model, 3),
                "p_piyasa": "" if p_mkt is None else round(p_mkt, 3), "gol_10dk": "",
            })
        # sinyal
        if m.minute - m.last_signal_min < 12 or m.minute >= 88:
            return
        side = signal_side(pr)
        if not side:
            return
        if p_mkt is not None and p_model - p_mkt < EDGE:
            return
        m.last_signal_min = m.minute
        self.send_signal(m, pr, side, p_model, p_mkt)

    def send_signal(self, m, pr, side, p_model, p_mkt):
        meta = self.meta.get(m.id, {})
        ratio = p_model / p_mkt if p_mkt else None
        conf = "yüksek" if (ratio and ratio >= 1.4) else "orta" if (ratio and ratio >= 1.2) else "düşük"
        ai = None
        if self.claude.available() and self.claude.calls < MAX_CLAUDE:
            ctx = {
                "lig": m.league, "mac": m.name, "dakika": m.minute, "skor": f"{m.gh}-{m.ga}",
                "pencere_dk": pr["span"], "ev_son_pencere": pr["d_home"],
                "dep_son_pencere": pr["d_away"], "ev_toplam": pr["cur"]["stats"]["home"],
                "dep_toplam": pr["cur"]["stats"]["away"],
                "mac_oncesi_beklenen_gol": meta.get("mu"),
                "mac_oncesi_ust_oran_dususu": meta.get("dusus"),
                "model_10dk_gol_olasiligi": round(p_model, 3),
                "piyasa_10dk_gol_olasiligi": None if p_mkt is None else round(p_mkt, 3),
                "model_taraf": side,
            }
            try:
                ai = self.claude.judge(ctx)
            except Exception as e:
                log("Claude hatası:", e)
        send = True if ai is None else bool(ai.get("onay"))
        if ai:
            conf = ai.get("guven", conf)
            side = ai.get("taraf", side)
        row = {
            "tarih": self.today, "fixture_id": m.id, "lig": m.league, "mac": m.name,
            "dakika": m.minute, "skor": f"{m.gh}-{m.ga}", "taraf": side,
            "baski": max(pr["home"], pr["away"]), "baski_rakip": min(pr["home"], pr["away"]),
            "guven": conf, "ai_onay": "" if ai is None else ("evet" if ai.get("onay") else "hayır"),
            "ai_yorum": "" if ai is None else ai.get("yorum", ""),
            "gonderildi": "evet" if send else "hayır", "sonuc": "bekliyor", "gol_dakikasi": "",
            "p_model": round(p_model, 3), "p_piyasa": "" if p_mkt is None else round(p_mkt, 3),
        }
        self.rows_today.append(row)
        log(f"SİNYAL {m.name} {m.minute}' {side} model={p_model:.2f} piyasa={p_mkt} gönder={send}")
        if not send:
            return
        d = pr["d_home"] if side == "ev" else pr["d_away"] if side == "deplasman" else {
            k: pr["d_home"][k] + pr["d_away"][k] for k in pr["d_home"]}
        team = m.home if side == "ev" else m.away if side == "deplasman" else "İki takım"
        lines = [m.league, f"{m.minute}'  Skor {m.gh}-{m.ga}",
                 f"{team}: son {int(pr['span'])} dk {fmt_delta(d)}",
                 f"10 dk gol olasılığı → model %{round(p_model * 100)}"
                 + (f" | piyasa %{round(p_mkt * 100)}" if p_mkt is not None else " | piyasa: yok"),
                 f"Güven: {conf}"]
        if meta.get("dusus", 0) >= DROP_PCT:
            lines.append(f"📉 Maç öncesi Üst 2.5 oranı %{round(meta['dusus'] * 100)} düşmüştü")
        if ai and ai.get("yorum"):
            lines.append(f"🤖 {ai['yorum']}")
        notify(f"⚽ Gol sinyali: {m.name}", "\n".join(lines),
               priority=5 if conf == "yüksek" else 4, tags=["soccer"])

    # --- sonuç ve etiketleme
    def check_outcomes(self, m: Match):
        for r in self.rows_today:
            if int(r["fixture_id"]) != m.id or r["sonuc"] != "bekliyor":
                continue
            smin = int(r["dakika"])
            sh, sa = (int(x) for x in r["skor"].split("-"))
            hits = [g for g in m.goals if smin < g["minute"] <= smin + 10]
            if hits or ((m.gh + m.ga) > (sh + sa) and m.minute <= smin + 10):
                r["sonuc"] = "isabet"
                r["gol_dakikasi"] = hits[0]["minute"] if hits else m.minute
            elif m.minute > smin + 10 or m.status in FINISHED:
                r["sonuc"] = "ıska"

    def label_samples(self, m: Match):
        keep = []
        for s in self.pending_samples:
            if s["fixture_id"] != m.id:
                keep.append(s)
                continue
            smin = s["dakika"]
            goal = any(smin < g["minute"] <= smin + 10 for g in m.goals) or \
                (m.gh + m.ga > s["toplam_gol"] and m.minute <= smin + 10)
            if goal:
                s["gol_10dk"] = 1
                self.done_samples.append(s)
            elif m.minute > smin + 10 or m.status in FINISHED:
                s["gol_10dk"] = 0
                self.done_samples.append(s)
            else:
                keep.append(s)
        self.pending_samples = keep

    def flush_samples(self):
        if not self.done_samples:
            return
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        path = SAMPLES_DIR / f"{self.today}.csv.gz"
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=SAMPLE_FIELDS)
        if not path.exists():
            w.writeheader()
        for s in self.done_samples:
            w.writerow({k: s.get(k, "") for k in SAMPLE_FIELDS})
        with gzip.open(path, "at", encoding="utf-8") as f:
            f.write(buf.getvalue())
        self.done_samples = []

    def save(self):
        save_signals(self.all_rows + self.rows_today)
        self.flush_samples()

    def finish(self):
        for m in self.matches.values():
            if m.status in FINISHED:
                self.check_outcomes(m)
                self.label_samples(m)
        self.save()
        sent = [r for r in self.rows_today if r["gonderildi"] == "evet"]
        hit = [r for r in sent if r["sonuc"] == "isabet"]
        rows = self.all_rows + self.rows_today
        all_sent = [r for r in rows if r.get("gonderildi") == "evet" and r.get("sonuc") in ("isabet", "ıska")]
        all_hit = [r for r in all_sent if r["sonuc"] == "isabet"]
        msg = [f"Bu oturumda sinyal: {len(sent)}, isabet: {len(hit)}"
               + (f" (%{100 * len(hit) // len(sent)})" if sent else "")]
        if all_sent:
            msg.append(f"Tüm zamanlar: {len(all_hit)}/{len(all_sent)} (%{100 * len(all_hit) // len(all_sent)})")
        msg.append(f"API isteği: {self.api.used}, kalan: {self.api.remaining}, Claude: {self.claude.calls}")
        log("ÖZET:", " | ".join(msg))
        notify("📊 Oturum özeti", "\n".join(msg), priority=2, tags=["bar_chart"])

    # --- ana döngü
    def run(self):
        end = self.end_time()
        log(f"Pro motor başladı, bitiş {end:%H:%M}")
        try:
            self.load_prematch_odds()
        except Exception as e:
            log("Maç öncesi oranlar alınamadı:", e)
        notify("✅ Takip başladı", f"Pro mod, bitiş {end:%H:%M}.", priority=2, tags=["white_check_mark"])
        last_save = time.time()
        idle = 0
        try:
            while dt.datetime.now(TZ) < end:
                if self.api.remaining is not None and self.api.remaining <= REQUEST_RESERVE * 10:
                    notify("Gol Sinyali", "Günlük API kotası doldu, takip durdu.", priority=3)
                    break
                t0 = time.time()
                try:
                    n_live, n_tr = self.tick()
                    log(f"canlı {n_live}, izlenen {n_tr}, istek {self.tick_cost}, kalan {self.api.remaining}")
                except Exception as e:
                    log("Tur hatası:", e)
                    n_live = 0
                if time.time() - last_save > 900:
                    self.save()
                    last_save = time.time()
                idle = idle + 1 if n_live == 0 else 0
                wait = TARGET_INTERVAL if n_live else min(600, 120 * idle)
                # bütçe yetmiyorsa turu uzat
                pmb = self.per_minute_budget()
                if pmb > 0 and self.tick_cost / pmb * 60 > wait:
                    wait = min(300, self.tick_cost / pmb * 60)
                time.sleep(max(5, wait - (time.time() - t0)))
        finally:
            self.finish()
