"""Gol Sinyali – Pro/Ultra motoru.

* Tüm canlı maçlar 30 sn'de bir taranır; istatistikler 20'şer maçlık gruplarla.
* Üç canlı sinyal türü: ⚽ gol, 🚩 korner, 🟨 kart. Her biri için model
  olasılığı canlı oranlardan çıkarılan piyasa olasılığıyla karşılaştırılır.
* Maç öncesi modül (prematch.py): sabah raporu, oran düşüşü uyarıları,
  seçimlerin sonuç takibi.
* Her canlı maçtan 5 dakikada bir örnek kaydedilir (model kalibrasyonu için).
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import math
import os
import re
import time

from main import (
    DATA, FINISHED, LIVE, PRIORITY, REQUEST_RESERVE, TZ,
    Api, Claude, Match, evaluate_pressure, goal_events, fmt_delta, load_signals, log, notify,
    save_signals, signal_side,
)

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "350"))
EDGE = float(os.environ.get("EDGE", "0.04"))
EDGE_CORNER = float(os.environ.get("EDGE_CORNER", "0.07"))
EDGE_CARD = float(os.environ.get("EDGE_CARD", "0.07"))
TARGET_INTERVAL = int(os.environ.get("INTERVAL", "30"))
MAX_BATCHES = 15
SAMPLE_EVERY = 5
DROP_PCT = float(os.environ.get("DROP_PCT", "0.07"))
MAX_CLAUDE = int(os.environ.get("MAX_CLAUDE_CALLS", "25"))
SIGNALS_ON = set(os.environ.get("SIGNALS", "gol,korner,kart").split(","))
TYPICAL_TOTAL_PRESSURE = 12.0
DEFAULT_MU = 2.6
LEAGUE_CORNERS = 10.2     # maç başı ortalama korner
LEAGUE_CARDS = 4.3        # maç başı ortalama kart (sarı + kırmızı)
TYPICAL_FOULS_10 = 2.4    # iki takım toplam, 10 dakikada faul
TYPICAL_ATTACK_10 = 3.9   # şut + engellenen/2 + korner*0.8, iki takım, 10 dk

PARAMS_PATH = DATA / "model" / "params.json"
try:
    PARAMS = json.loads(PARAMS_PATH.read_text()) if PARAMS_PATH.exists() else {}
except Exception:
    PARAMS = {}
# Uzatma dakikaları 41-45 ve 86-90 dilimlerine yazıldığı için bu dilimlerin dakika başı
# hızı şişik görünür; maruz kalınan süreye göre düzelt (ilk yarı ~2,5, ikinci yarı ~5 dk uzatma).
for _key in ("gol_egrisi", "kart_egrisi"):
    if PARAMS.get(_key) and len(PARAMS[_key]) == 18:
        PARAMS[_key] = list(PARAMS[_key])
        PARAMS[_key][8] *= 5 / 7.5
        PARAMS[_key][17] *= 5 / 10

TRAINED_PATH = DATA / "model" / "canli.json"
try:
    TRAINED = json.loads(TRAINED_PATH.read_text()) if TRAINED_PATH.exists() else {}
except Exception:
    TRAINED = {}
REQUIRE_MARKET = os.environ.get("REQUIRE_MARKET", "1") == "1"
NO_XG_EXTRA_EDGE = 0.02
LOW_QUALITY = re.compile(r"\bU-?(1[5-9]|2[0-3])\b|youth|women|\bW\b|reserve|friendl|\bII\b|\bJong\b|primavera|juvenil",
                         re.IGNORECASE)

SAMPLES_DIR = DATA / "ornekler"
REFEREE_JSON = DATA / "hakemler.json"          # geçmiş veri (analiz yazar)
REFEREE_LIVE = DATA / "hakemler_canli.json"    # canlı oturumların eklediği maçlar
CHAIN_FLAG = "/tmp/zincir"
SAMPLE_FIELDS = ["tarih", "fixture_id", "lig_id", "dakika", "skor_farki", "toplam_gol",
                 "baski_ev", "baski_dep", "kirmizi_farki", "mu", "dusus", "p_model",
                 "p_piyasa", "gol_10dk", "korner", "p_korner", "p_korner_piyasa", "korner_10dk",
                 "kart", "faul_10", "p_kart", "p_kart_piyasa", "kart_10dk"]


# ---------------------------------------------------------------- olasılık yardımcıları
def curve_avg(curve, minute: int, span: int = 10) -> float:
    """Zaman eğrisinin (18 adet 5 dk'lık dilim) önümüzdeki 'span' dakikadaki ortalaması."""
    if not curve:
        return 1.0
    vals = [curve[min(17, max(0, (min(m, 90) - 1) // 5))] for m in range(minute + 1, minute + span + 1)]
    return sum(vals) / len(vals)


def league_param(lid, key, default):
    v = (PARAMS.get("lig") or {}).get(str(lid), {}).get(key)
    return v if v else default


def goal_state(gh, ga) -> str:
    d = abs(gh - ga)
    return "esit" if d == 0 else "fark1" if d == 1 else "fark2+"



def poisson_over(mu: float, k: int) -> float:
    s, term = 0.0, math.exp(-mu)
    for i in range(k):
        s += term
        term *= mu / (i + 1)
    return 1.0 - s


def solve_lambda(p_over: float, k: int) -> float:
    lo, hi = 0.001, 40.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if poisson_over(mid, k) < p_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def mu_from_over25(p_over: float) -> float:
    return round(solve_lambda(p_over, 3), 3)


def fair_prob(odd_a: float, odd_b: float) -> float:
    ia, ib = 1 / odd_a, 1 / odd_b
    return ia / (ia + ib)


def remaining_minutes(minute: int) -> float:
    return max(4.0, 94.0 - minute)


def p_next10_from_rest(p_rest: float, minute: int) -> float:
    rem = remaining_minutes(minute)
    p_rest = min(max(p_rest, 0.001), 0.999)
    q = -math.log(1 - p_rest) / rem
    return 1 - math.exp(-q * min(10.0, rem))


def p_next10_from_count_market(item: dict, names: tuple, current: int, minute: int) -> float | None:
    rate = count_market_rate(item, names, current, minute)
    if rate is None:
        return None
    return 1 - math.exp(-rate * min(10.0, remaining_minutes(minute)))


def count_market_rate(item: dict, names: tuple, current: int, minute: int) -> float | None:
    """Toplam (korner/kart) Üst-Alt pazarından piyasanın dakika başı olay hızı."""
    lines = {}
    for mk in item.get("odds") or []:
        if mk.get("name") not in names:
            continue
        for v in mk.get("values") or []:
            if v.get("suspended"):
                continue
            try:
                h = float(v.get("handicap"))
                odd = float(v.get("odd"))
            except (TypeError, ValueError):
                continue
            if abs(h - math.floor(h) - 0.5) > 1e-6 or h < current:
                continue
            side = (v.get("value") or "").lower()
            if side in ("over", "under"):
                lines.setdefault(h, {})[side] = odd
    best = None
    for h in sorted(lines):
        d = lines[h]
        if "over" in d and "under" in d:
            best = (h, fair_prob(d["over"], d["under"]))
            break
    if not best:
        return None
    h, p_over = best
    k = int(h - current + 0.5)
    if k < 1 or k > 8 or not (0.02 < p_over < 0.98):
        return None
    lam = solve_lambda(p_over, k)
    return lam / remaining_minutes(minute)


def parse_live_goal_market(item: dict, total_goals: int) -> float | None:
    line = total_goals + 0.5
    best = None
    for mk in item.get("odds") or []:
        name = (mk.get("name") or "").lower()
        if "over/under" not in name and "match goals" not in name:
            continue
        if any(w in name for w in ("1st", "2nd", "half", "corner", "card", "home", "away", "team", "asian")):
            continue
        over = under = None
        for v in mk.get("values") or []:
            if v.get("suspended"):
                continue
            try:
                h = float(v.get("handicap"))
                odd = float(v.get("odd"))
            except (TypeError, ValueError):
                continue
            if abs(h - line) > 1e-6:
                continue
            label = (v.get("value") or "").lower()
            if label.startswith("over"):
                over = odd
            elif label.startswith("under"):
                under = odd
        if over and under:
            best = fair_prob(over, under)
            break
    return best


def card_events(m: Match) -> list[int]:
    return [(ev.get("time") or {}).get("elapsed") or 0
            for ev in getattr(m, "events", []) or [] if ev.get("type") == "Card"]


# ---------------------------------------------------------------- motor
class ProEngine:
    def __init__(self, api: Api, claude: Claude):
        from prematch import OddsBook
        self.api = api
        self.claude = claude
        self.now0 = dt.datetime.now(TZ)
        self.today = self.now0.date().isoformat()
        self.book = OddsBook(api, self.today)
        self.matches: dict[int, Match] = {}
        self.meta: dict[int, dict] = {}
        self.pending_samples: list[dict] = []
        self.done_samples: list[dict] = []
        self.all_rows = load_signals()
        # Önceki oturumdan sonucu bekleyen bugünkü sinyaller bu oturumda takip edilmeye devam eder
        carry = [r for r in self.all_rows if r.get("sonuc") == "bekliyor"
                 and r.get("tarih") in (self.today, (self.now0 - dt.timedelta(days=1)).date().isoformat())]
        self.all_rows = [r for r in self.all_rows if r not in carry]
        self.rows_today: list[dict] = carry
        self.odds_live_ok = True
        self.tick_cost = 0
        self.alerted: set = set()
        self.referees = json.loads(REFEREE_JSON.read_text()) if REFEREE_JSON.exists() else {}
        self.ref_live = json.loads(REFEREE_LIVE.read_text()) if REFEREE_LIVE.exists() else {}
        for key, recs in self.ref_live.items():  # canlı eklemeleri geçmiş veriyle birleştir
            if isinstance(recs, dict):
                for name, v in recs.items():
                    r = self.referees.setdefault(key, {}).setdefault(name, {"mac": 0, "kart": 0})
                    r["mac"] += v["mac"]
                    r["kart"] += v["kart"]
        self.ref_done: set = set()

    # --- zaman ve bütçe
    def _reset(self, now):
        return (now + dt.timedelta(days=1 if now.hour >= 3 else 0)).replace(
            hour=2, minute=55, second=0, microsecond=0)

    def end_time(self) -> dt.datetime:
        return min(self.now0 + dt.timedelta(minutes=RUN_MINUTES), self._reset(self.now0))

    def per_minute_budget(self) -> float:
        now = dt.datetime.now(TZ)
        mins = max(10.0, (self._reset(now) - now).total_seconds() / 60)
        rem = (self.api.remaining if self.api.remaining is not None else 500) - REQUEST_RESERVE * 10
        return max(0.0, rem) / mins

    # --- maç öncesi
    def prematch_phase(self):
        import prematch
        self.book.load_fixtures()
        self.book.refresh()
        try:
            prematch.resolve_picks(self.api)
        except Exception as e:
            log("Tahmin sonuçları işlenemedi:", e)
        report = prematch.REPORT_DIR / f"{self.today}.md"
        if (self.now0.hour < 13 and not report.exists()) or os.environ.get("FORCE_REPORT") == "1":
            try:
                prematch.morning_report(self.api, self.book)
            except Exception as e:
                log("Sabah raporu hatası:", e)
        self.send_drop_alerts()

    def send_drop_alerts(self):
        import prematch
        lines = prematch.drop_alerts(self.book, self.alerted)
        if lines:
            notify("📉 Oran düşüşü", "\n".join(lines[:8]), priority=3, tags=["chart_with_downwards_trend"])

    def mu(self, fid) -> float:
        return self.book.mu(fid) or DEFAULT_MU

    # --- seçim
    def score(self, m: Match) -> float:
        s = self.mu(m.id) * 25 + PRIORITY.get(m.league_id, 10) / 10
        if self.book.over_drop(m.id) >= DROP_PCT:
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
                m.events = it["events"]
        for fid, m in self.matches.items():
            if fid not in seen and m.status in LIVE:
                m.status = "FT"

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
            cost += 1
            try:
                items = self.api.get("fixtures", ids="-".join(str(m.id) for m in chunk))
            except Exception as e:
                log("İstatistik hatası:", e)
                continue
            for it in items:
                m = self.matches.get(it["fixture"]["id"])
                if m:
                    m.update(it)
        if self.odds_live_ok:
            cost += 1
            try:
                for it in self.api.get("odds/live"):
                    m = self.matches.get(it["fixture"]["id"])
                    if m:
                        self.meta.setdefault(m.id, {})["odds"] = it
            except Exception as e:
                log("Canlı oran kapatıldı:", e)
                self.odds_live_ok = False
        for m in tracked:
            try:
                self.check_outcomes(m)
                self.label_samples(m)
                self.evaluate(m)
            except Exception as e:
                log(f"{m.name} değerlendirme hatası:", e)
        for m in self.matches.values():
            if m.status in FINISHED:
                self.check_outcomes(m)
                self.label_samples(m)
                self.learn_referee(m)
        self.tick_cost = cost
        return len(live_ms), len(tracked)

    # --- modeller
    def goal_model(self, m: Match, pr: dict):
        rate = self.mu(m.id) / 94.0
        total = pr["home"] + pr["away"]
        f = min(3.0, max(0.55, 0.55 + 0.45 * total / TYPICAL_TOTAL_PRESSURE))
        if PARAMS.get("gol_egrisi"):
            time_mult = curve_avg(PARAMS["gol_egrisi"], m.minute)
            time_mult *= (PARAMS.get("gol_durum") or {}).get(goal_state(m.gh, m.ga), 1.0)
        else:
            time_mult = 1.15 if m.minute >= 75 else 1.0
        p_model = 1 - math.exp(-rate * min(10.0, remaining_minutes(m.minute)) * f * time_mult)
        odds = self.meta.get(m.id, {}).get("odds")
        p_rest = parse_live_goal_market(odds, m.gh + m.ga) if odds else None
        p_mkt = p_next10_from_rest(p_rest, m.minute) if p_rest else None
        q = -math.log(1 - min(max(p_rest, 0.001), 0.999)) / remaining_minutes(m.minute) if p_rest else None
        return p_model, p_mkt, q

    def corner_model(self, m: Match, pr: dict):
        cur, base, span = pr["cur"]["stats"], pr["base"]["stats"], max(pr["span"], 6)
        corners = int(cur["home"]["corners"] + cur["away"]["corners"])
        d = lambda k: sum(max(0.0, cur[s][k] - base[s][k]) for s in ("home", "away"))
        attack = (d("shots") + 0.5 * d("blocked") + 0.8 * d("corners")) * 10 / span
        f = min(2.2, max(0.6, 0.6 + 0.4 * attack / TYPICAL_ATTACK_10))
        base_rate = league_param(m.league_id, "korner", LEAGUE_CORNERS) / 94
        if m.minute >= 20:
            base_rate = 0.6 * base_rate + 0.4 * corners / m.minute
        if abs(m.gh - m.ga) == 1 and m.minute >= 70:
            f *= 1.15
        # hedef: 10 dakikada en az 2 korner
        T = min(10.0, remaining_minutes(m.minute))
        p_model = poisson_over(base_rate * f * T, 2)
        odds = self.meta.get(m.id, {}).get("odds")
        q = count_market_rate(odds, ("Total Corners", "Asian Corners"), corners, m.minute) if odds else None
        p_mkt = poisson_over(q * T, 2) if q else None
        return p_model, p_mkt, f, corners, q

    def referee_avg(self, m: Match) -> tuple[float, str]:
        ref = (getattr(m, "referee", None) or "").split(",")[0].strip()
        r = self.referees.get("hakem", {}).get(ref)
        lg = self.referees.get("lig", {}).get(str(m.league_id))
        lg_avg = lg["kart"] / lg["mac"] if lg and lg["mac"] >= 10 else league_param(m.league_id, "kart", LEAGUE_CARDS)
        if ref and r and r["mac"] >= 3:
            avg = (r["kart"] + 5 * lg_avg) / (r["mac"] + 5)  # az maçlı hakemi lig ortalamasına çek
            return avg, f"hakem {ref}: maç başı {r['kart'] / r['mac']:.1f} kart ({r['mac']} maç)"
        return lg_avg, f"lig ortalaması {lg_avg:.1f} (hakem verisi yok)"

    def card_model(self, m: Match, pr: dict):
        cur, base, span = pr["cur"]["stats"], pr["base"]["stats"], max(pr["span"], 6)
        cards = int(sum(cur[s]["yellow"] + cur[s]["red"] for s in ("home", "away")))
        fouls10 = sum(max(0.0, cur[s]["fouls"] - base[s]["fouls"]) for s in ("home", "away")) * 10 / span
        avg, src = self.referee_avg(m)
        if PARAMS.get("kart_egrisi"):
            phase = curve_avg(PARAMS["kart_egrisi"], m.minute)
        else:
            phase = 0.6 if m.minute < 30 else 1.0 if m.minute < 60 else 1.35
        typical_fouls = league_param(m.league_id, "faul", TYPICAL_FOULS_10 / 10 * 94) * 10 / 94
        f = min(2.2, max(0.6, 0.6 + 0.4 * fouls10 / typical_fouls))
        if abs(m.gh - m.ga) <= 1 and m.minute >= 70:
            f *= PARAMS.get("kart_gec_yakin", 1.2)
        if m.minute >= 20:
            avg = 0.7 * avg + 0.3 * cards / m.minute * 94
        p_model = 1 - math.exp(-avg / 94 * phase * f * min(10.0, remaining_minutes(m.minute)))
        odds = self.meta.get(m.id, {}).get("odds")
        q = count_market_rate(odds, ("Total Cards",), cards, m.minute) if odds else None
        p_mkt = 1 - math.exp(-q * min(10.0, remaining_minutes(m.minute))) if q else None
        return p_model, p_mkt, f, cards, fouls10, src, q

    # --- değerlendirme
    def evaluate(self, m: Match):
        pr = evaluate_pressure(m)
        if not pr:
            return
        meta = self.meta.setdefault(m.id, {})
        pg, pg_m, qg = self.goal_model(m, pr)
        pc, pc_m, fc, corners, qc = self.corner_model(m, pr)
        pk, pk_m, fk, cards, fouls10, ref_src, qk = self.card_model(m, pr)
        meta["q"] = {"gol": qg, "korner": qc, "kart": qk}
        if m.minute - meta.get("son_ornek", -99) >= SAMPLE_EVERY:
            meta["son_ornek"] = m.minute
            st = pr["cur"]["stats"]
            r3 = lambda v: "" if v is None else round(v, 3)
            self.pending_samples.append({
                "tarih": self.today, "fixture_id": m.id, "lig_id": m.league_id,
                "dakika": m.minute, "skor_farki": m.gh - m.ga, "toplam_gol": m.gh + m.ga,
                "baski_ev": pr["home"], "baski_dep": pr["away"],
                "kirmizi_farki": st["home"]["red"] - st["away"]["red"],
                "mu": round(self.mu(m.id), 2), "dusus": self.book.over_drop(m.id),
                "p_model": r3(pg), "p_piyasa": r3(pg_m), "gol_10dk": "",
                "korner": corners, "p_korner": r3(pc), "p_korner_piyasa": r3(pc_m), "korner_10dk": "",
                "kart": cards, "faul_10": round(fouls10, 1), "p_kart": r3(pk),
                "p_kart_piyasa": r3(pk_m), "kart_10dk": "",
            })
        if m.minute >= 88:
            return
        # Kalite filtresi: gençlik/kadın/yedek/hazırlık maçlarında sinyal yok
        if LOW_QUALITY.search(f"{m.league} {m.home} {m.away}"):
            return
        # Eğitilmiş model piyasadan iyiyse onun olasılığı kullanılır
        sample_like = {"baski_ev": pr["home"], "baski_dep": pr["away"], "dakika": m.minute,
                           "skor_farki": m.gh - m.ga, "kirmizi_farki": pr["cur"]["stats"]["home"]["red"] - pr["cur"]["stats"]["away"]["red"],
                           "faul_10": fouls10, "p_model": pg, "p_piyasa": pg_m, "p_korner": pc,
                       "p_korner_piyasa": pc_m, "p_kart": pk, "p_kart_piyasa": pk_m}
        pg = self.trained_prob("gol", sample_like, pg)
        pc = self.trained_prob("korner", sample_like, pc)
        pk = self.trained_prob("kart", sample_like, pk)
        has_xg = pr["cur"]["stats"].get("has_xg")
        # ⚽ gol
        if "gol" in SIGNALS_ON and m.minute - m.last_signal_min >= 12 and not (REQUIRE_MARKET and pg_m is None):
            side = signal_side(pr)
            need = EDGE + (0 if has_xg else NO_XG_EXTRA_EDGE)
            if side and (pg_m is None or pg - pg_m >= need):
                m.last_signal_min = m.minute
                self.goal_signal(m, pr, side, pg, pg_m, qg)
        # 🚩 korner
        if "korner" in SIGNALS_ON and m.minute - meta.get("son_korner", -99) >= 10 and fc >= 1.4:
            if (pc_m is not None and pc - pc_m >= EDGE_CORNER) or (not REQUIRE_MARKET and pc_m is None and pc >= 0.6):
                meta["son_korner"] = m.minute
                d = {k: pr["d_home"][k] + pr["d_away"][k] for k in pr["d_home"]}
                self.simple_signal(m, "korner", pc, pc_m, corners, qc, [
                    f"Son {int(pr['span'])} dk: {fmt_delta(d)}",
                    f"Maçtaki korner: {corners}"])
        # 🟨 kart
        if "kart" in SIGNALS_ON and m.minute - meta.get("son_kart", -99) >= 12 and fk >= 1.5:
            if (pk_m is not None and pk - pk_m >= EDGE_CARD) or (not REQUIRE_MARKET and pk_m is None and pk >= 0.55):
                meta["son_kart"] = m.minute
                self.simple_signal(m, "kart", pk, pk_m, cards, qk, [
                    f"Son {int(pr['span'])} dk faul temposu: 10 dk'da {fouls10:.1f} (normali ~{TYPICAL_FOULS_10})",
                    f"Maçtaki kart: {cards}", f"Hakem verisi: {ref_src}"])

    def trained_prob(self, tur, s, default):
        t = TRAINED.get(tur) or {}
        if not t.get("kullan"):
            return default
        from egitim import features, sigmoid
        x = features(tur, s)
        if x is None:
            return default
        return sigmoid(sum(a * b for a, b in zip(t["agirliklar"], x)))

    def _row(self, m, tur, side, p, p_mkt, conf, extra=None):
        row = {"tarih": self.today, "fixture_id": m.id, "lig": m.league, "mac": m.name,
               "dakika": m.minute, "skor": f"{m.gh}-{m.ga}", "taraf": side, "baski": "",
               "baski_rakip": "", "guven": conf, "ai_onay": "", "ai_yorum": "",
               "gonderildi": "evet", "sonuc": "bekliyor", "gol_dakikasi": "",
               "p_model": round(p, 3), "p_piyasa": "" if p_mkt is None else round(p_mkt, 3), "tur": tur}
        if extra:
            row.update(extra)
        return row

    def simple_signal(self, m, tur, p, p_mkt, count, q, lines):
        ratio = p / p_mkt if p_mkt else None
        conf = "yüksek" if (ratio and ratio >= 1.25) else "orta" if (ratio and ratio >= 1.12) else "düşük"
        row = self._row(m, tur, "", p, p_mkt, conf, {"baski": count, "q_once": "" if q is None else round(q, 5)})
        self.rows_today.append(row)
        icon, label = ("🚩", "Korner") if tur == "korner" else ("🟨", "Kart")
        what = "2+ korner" if tur == "korner" else "kart"
        log(f"SİNYAL[{tur}] {m.name} {m.minute}' model={p:.2f} piyasa={p_mkt}")
        until = min(m.minute + 10, 90)
        tahmin = (f"🎯 TAHMİN: {m.minute}-{until}. dakikalar arasında en az 2 korner" if tur == "korner"
                  else f"🎯 TAHMİN: {m.minute}-{until}. dakikalar arasında kart çıkacak")
        body = [tahmin, m.league, f"{m.minute}'  Skor {m.gh}-{m.ga}", *lines,
                f"10 dk'da {what} olasılığı → model %{round(p * 100)}"
                + (f" | piyasa %{round(p_mkt * 100)}" if p_mkt is not None else " | piyasa: yok"),
                f"Güven: {conf}"]
        row["tg_mesaj"] = notify(f"{icon} {label} sinyali: {m.name}", "\n".join(body), priority=4,
                                 tags=["triangular_flag_on_post" if tur == "korner" else "yellow_square"]) or ""

    def goal_signal(self, m, pr, side, p_model, p_mkt, q=None):
        drop = self.book.over_drop(m.id)
        ratio = p_model / p_mkt if p_mkt else None
        conf = "yüksek" if (ratio and ratio >= 1.4) else "orta" if (ratio and ratio >= 1.2) else "düşük"
        ai = None
        if self.claude.available() and self.claude.calls < MAX_CLAUDE:
            ctx = {"lig": m.league, "mac": m.name, "dakika": m.minute, "skor": f"{m.gh}-{m.ga}",
                   "pencere_dk": pr["span"], "ev_son_pencere": pr["d_home"], "dep_son_pencere": pr["d_away"],
                   "ev_toplam": pr["cur"]["stats"]["home"], "dep_toplam": pr["cur"]["stats"]["away"],
                   "mac_oncesi_beklenen_gol": round(self.mu(m.id), 2), "mac_oncesi_ust_oran_dususu": drop,
                   "model_10dk_gol_olasiligi": round(p_model, 3),
                   "piyasa_10dk_gol_olasiligi": None if p_mkt is None else round(p_mkt, 3),
                   "model_taraf": side}
            try:
                ai = self.claude.judge(ctx)
            except Exception as e:
                log("Claude hatası:", e)
        send = True if ai is None else bool(ai.get("onay"))
        if ai:
            conf = ai.get("guven", conf)
            side = ai.get("taraf", side)
        row = self._row(m, "gol", side, p_model, p_mkt, conf, {
            "baski": max(pr["home"], pr["away"]), "baski_rakip": min(pr["home"], pr["away"]),
            "ai_onay": "" if ai is None else ("evet" if ai.get("onay") else "hayır"),
            "ai_yorum": "" if ai is None else ai.get("yorum", ""),
            "gonderildi": "evet" if send else "hayır", "q_once": "" if q is None else round(q, 5)})
        self.rows_today.append(row)
        log(f"SİNYAL[gol] {m.name} {m.minute}' {side} model={p_model:.2f} piyasa={p_mkt} gönder={send}")
        if not send:
            return
        d = pr["d_home"] if side == "ev" else pr["d_away"] if side == "deplasman" else {
            k: pr["d_home"][k] + pr["d_away"][k] for k in pr["d_home"]}
        team = m.home if side == "ev" else m.away if side == "deplasman" else "İki takım"
        until = min(m.minute + 10, 90)
        kim = f" (golü atması en olası: {team})" if side in ("ev", "deplasman") else ""
        lines = [f"🎯 TAHMİN: {m.minute}-{until}. dakikalar arasında gol olacak{kim}",
                 m.league, f"{m.minute}'  Skor {m.gh}-{m.ga}",
                 f"{team}: son {int(pr['span'])} dk {fmt_delta(d)}",
                 f"10 dk gol olasılığı → model %{round(p_model * 100)}"
                 + (f" | piyasa %{round(p_mkt * 100)}" if p_mkt is not None else " | piyasa: yok"),
                 f"Güven: {conf}"]
        if drop >= DROP_PCT:
            lines.append(f"📉 Maç öncesi Üst 2.5 oranı %{round(drop * 100)} düşmüştü")
        if ai and ai.get("yorum"):
            lines.append(f"🤖 {ai['yorum']}")
        row["tg_mesaj"] = notify(f"⚽ Gol sinyali: {m.name}", "\n".join(lines),
                                 priority=5 if conf == "yüksek" else 4, tags=["soccer"]) or ""

    # --- sonuçlar
    def count_after(self, m: Match, key: str, smin: int) -> int | None:
        """smin+10'a kadarki en son anlık görüntüdeki toplam sayı (korner için)."""
        last = None
        for s in m.snaps:
            if s["min"] <= smin + 10:
                st = s["stats"]
                last = int(st["home"][key] + st["away"][key])
        return last

    def check_outcomes(self, m: Match):
        for r in self.rows_today:
            if int(r["fixture_id"]) != m.id or r["sonuc"] != "bekliyor":
                continue
            smin, tur = int(r["dakika"]), r.get("tur") or "gol"
            # piyasa hareketi: sinyalden 3+ dk sonra, olay olmadan önceki piyasa hızı
            if r.get("q_once") not in ("", None) and r.get("q_sonra") in ("", None) and m.minute >= smin + 3:
                q_now = (self.meta.get(m.id, {}).get("q") or {}).get(tur)
                if q_now:
                    r["q_sonra"] = round(q_now, 5)
            over = m.minute > smin + 10 or m.status in FINISHED
            if tur == "gol":
                sh, sa = (int(x) for x in r["skor"].split("-"))
                hits = [g for g in m.goals if smin < g["minute"] <= smin + 10]
                if hits or ((m.gh + m.ga) > (sh + sa) and m.minute <= smin + 10):
                    r["sonuc"], r["gol_dakikasi"] = "isabet", (hits[0]["minute"] if hits else m.minute)
                elif over:
                    r["sonuc"] = "ıska"
            elif tur == "korner":
                last = self.count_after(m, "corners", smin)
                if last is not None and r.get("baski") not in ("", None) and last >= int(float(r["baski"])) + 2:
                    r["sonuc"] = "isabet"
                elif over:
                    # pencere içinde hiç anlık veri yoksa (oturum değişimi) sonuç bilinemez
                    r["sonuc"] = "ıska" if last is not None else "bilinmiyor"
            elif tur == "kart":
                hits = [t for t in card_events(m) if smin < t <= smin + 10]
                if hits:
                    r["sonuc"], r["gol_dakikasi"] = "isabet", hits[0]
                elif over:
                    r["sonuc"] = "ıska"
            if r["sonuc"] != "bekliyor":
                self.announce(r, m)
            # sanal kâr-zarar: piyasanın 10 dk olasılığından, %5 marjla adil oran
            if r["sonuc"] != "bekliyor" and r.get("p_piyasa") not in ("", None) and r.get("kar") in ("", None):
                odd = 0.95 / max(float(r["p_piyasa"]), 0.02)
                r["adil_oran"] = round(odd, 2)
                r["kar"] = round(100 * (odd - 1)) if r["sonuc"] == "isabet" else -100

    def announce(self, r: dict, m: Match):
        """Sinyalin sonucunu Telegram'da sinyal mesajını alıntılayarak bildirir."""
        if r.get("duyuruldu") or not r.get("tg_mesaj") or r.get("gonderildi") != "evet":
            return
        r["duyuruldu"] = "evet"
        tur = r.get("tur") or "gol"
        olay = {"gol": "gol", "korner": "2+ korner", "kart": "kart"}[tur]
        if r["sonuc"] == "isabet":
            when = f"{r['gol_dakikasi']}' " if r.get("gol_dakikasi") not in ("", None) else ""
            text = f"✅ TUTTU — {when}{olay} geldi (sinyal {r['dakika']}'). Skor: {m.gh}-{m.ga}"
        else:
            text = f"❌ Tutmadı — sinyalden sonraki 10 dakikada {olay} olmadı. Skor: {m.gh}-{m.ga}"
        from main import telegram_reply
        telegram_reply(r["tg_mesaj"], text)

    def label_samples(self, m: Match):
        keep = []
        cards_t = card_events(m)
        for s in self.pending_samples:
            if s["fixture_id"] != m.id:
                keep.append(s)
                continue
            smin = s["dakika"]
            if not (m.minute > smin + 10 or m.status in FINISHED):
                keep.append(s)
                continue
            s["gol_10dk"] = int(any(smin < g["minute"] <= smin + 10 for g in m.goals))
            last_c = self.count_after(m, "corners", smin)
            s["korner_10dk"] = "" if last_c is None else max(0, last_c - int(s["korner"]))
            s["kart_10dk"] = int(any(smin < t <= smin + 10 for t in cards_t))
            self.done_samples.append(s)
        self.pending_samples = keep

    def learn_referee(self, m: Match):
        if m.id in self.ref_done or not m.snaps:
            return
        self.ref_done.add(m.id)
        if m.snaps[-1]["min"] < 80:
            return
        st = m.snaps[-1]["stats"]
        cards = int(sum(st[s]["yellow"] + st[s]["red"] for s in ("home", "away")))
        ref = (getattr(m, "referee", None) or "").split(",")[0].strip()
        for key, name in (("lig", str(m.league_id)), ("hakem", ref)):
            if not name:
                continue
            for db in (self.referees, self.ref_live):
                rec = db.setdefault(key, {}).setdefault(name, {"mac": 0, "kart": 0})
                rec["mac"] += 1
                rec["kart"] += cards

    # --- kayıt
    def flush_samples(self):
        if not self.done_samples:
            return
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        path = SAMPLES_DIR / f"{self.today}-v2.csv.gz"
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
        DATA.mkdir(exist_ok=True)
        REFEREE_LIVE.write_text(json.dumps(self.ref_live, ensure_ascii=False))

    def finish(self):
        for m in self.matches.values():
            if m.status in FINISHED:
                self.check_outcomes(m)
                self.label_samples(m)
                self.learn_referee(m)
        self.save()
        rows = self.all_rows + self.rows_today
        msg = []
        for tur, label in (("gol", "⚽ Gol"), ("korner", "🚩 Korner"), ("kart", "🟨 Kart")):
            sent = [r for r in self.rows_today if (r.get("tur") or "gol") == tur and r["gonderildi"] == "evet"]
            hit = [r for r in sent if r["sonuc"] == "isabet"]
            hist = [r for r in rows if (r.get("tur") or "gol") == tur and r.get("gonderildi") == "evet"
                    and r.get("sonuc") in ("isabet", "ıska")]
            hh = sum(r["sonuc"] == "isabet" for r in hist)
            if sent or hist:
                msg.append(f"{label}: bu oturum {len(hit)}/{len(sent)}"
                           + (f", tüm zamanlar {hh}/{len(hist)} (%{100 * hh // len(hist)})" if hist else ""))
        msg.append(f"API isteği: {self.api.used}, kalan: {self.api.remaining}, Claude: {self.claude.calls}")
        log("ÖZET:", " | ".join(msg))
        try:
            import panel
            panel.build()
            msg.append("Panel: https://leaderofhorde1.github.io/Gol-Analizi/")
        except Exception as e:
            log("Panel hatası:", e)
        notify("📊 Oturum özeti", "\n".join(msg), priority=2, tags=["bar_chart"])

    # --- ana döngü
    def run(self):
        end = self.end_time()
        log(f"Pro motor başladı, bitiş {end:%H:%M}")
        try:
            self.prematch_phase()
        except Exception as e:
            log("Maç öncesi aşama hatası:", e)
        notify("✅ Takip başladı", f"Pro mod ({', '.join(sorted(SIGNALS_ON))}), bitiş {end:%H:%M}.",
               priority=2, tags=["white_check_mark"])
        last_save = last_odds = time.time()
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
                if time.time() - last_odds > 3600:
                    last_odds = time.time()
                    try:
                        self.book.refresh()
                        self.send_drop_alerts()
                    except Exception as e:
                        log("Oran yenileme hatası:", e)
                if time.time() - last_save > 900:
                    self.save()
                    last_save = time.time()
                idle = idle + 1 if n_live == 0 else 0
                wait = TARGET_INTERVAL if n_live else min(600, 120 * idle)
                pmb = self.per_minute_budget()
                if pmb > 0 and self.tick_cost / pmb * 60 > wait:
                    wait = min(300, self.tick_cost / pmb * 60)
                time.sleep(max(5, wait - (time.time() - t0)))
            # Süre dolduysa ve gün bitmediyse bir sonraki oturumu iş akışı başlatsın
            now = dt.datetime.now(TZ)
            if now >= end and not (2 * 60 + 30 <= now.hour * 60 + now.minute < 9 * 60 + 40):
                open(CHAIN_FLAG, "w").write(now.isoformat())
        finally:
            self.finish()
