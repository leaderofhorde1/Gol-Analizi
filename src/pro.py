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

from form import FormBook, check as form_check
import kadro
import baglam
import profil

from main import (
    COMBINED_THRESHOLD, DATA, FINISHED, LIVE, PRIORITY, REQUEST_RESERVE, THRESHOLD, TZ,
    Api, Claude, Match, evaluate_pressure, goal_events, fmt_delta, load_signals, log, notify,
    save_signals, signal_side,
)

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "350"))
EDGE = float(os.environ.get("EDGE", "0.04"))
EDGE_CORNER = float(os.environ.get("EDGE_CORNER", "0.07"))
EDGE_CARD = float(os.environ.get("EDGE_CARD", "0.07"))
EDGE_IY = float(os.environ.get("EDGE_IY", "0.05"))
EDGE_UST = float(os.environ.get("EDGE_UST", "0.05"))
EDGE_ALT = float(os.environ.get("EDGE_ALT", "0.06"))
EDGE_KG = float(os.environ.get("EDGE_KG", "0.06"))
TARGET_INTERVAL = int(os.environ.get("INTERVAL", "30"))
HOT_INTERVAL = int(os.environ.get("HOT_INTERVAL", "10"))   # sn: sinyale yakın maçlar bu sıklıkla taranır
HOT_RATIO = 0.7                                              # eşiğin bu oranını geçen baskı "sıcak" sayılır
HOT_MAX = 20
MAX_BATCHES = 15
SAMPLE_EVERY = 5
DROP_PCT = float(os.environ.get("DROP_PCT", "0.07"))
MAX_CLAUDE = int(os.environ.get("MAX_CLAUDE_CALLS", "25"))
SIGNALS_ON = set(os.environ.get("SIGNALS", "gol,alt,korner,kart,iy,kg,baski").split(","))
ALARM_MULT = float(os.environ.get("ALARM_MULT", "1.8"))  # baskı alarmı: tek takım baskısı eşiğin bu katı
ALARM_WINDOW = 15  # dk: alarmın "gol yakın" iddiasının ölçüldüğü süre (baskının etkisi ~15 dk)
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
FORM_ON = os.environ.get("FORM", "1") == "1"
FORM_WEAK = float(os.environ.get("FORM_WEAK", "0.2"))      # bu oranın altı "form ters"
FORM_STRONG = float(os.environ.get("FORM_STRONG", "0.6"))  # bu oranın üstü "form destekliyor"
FORM_EXTRA = float(os.environ.get("FORM_EXTRA", "0.03"))   # form tersse istenen ek fark
STEAM_MIN = 5            # dk: oran akışı bu kadar öncesiyle karşılaştırılır
STEAM_UP, STEAM_DOWN = 1.15, 0.87  # gol hızında %15 artış / %13 düşüş "akış" sayılır
STEAM_EXTRA = float(os.environ.get("STEAM_EXTRA", "0.03"))
STEAM_DIR = {"gol": 1, "iy": 1, "kg": 1, "baski": 1, "alt": -1}  # +1: gol yönlü tahmin

LOW_QUALITY = re.compile(r"\bU-?(1[5-9]|2[0-3])\b|youth|women|\bW\b|reserve|friendl|\bII\b|\bJong\b|primavera|juvenil",
                         re.IGNORECASE)

STALE_MIN = int(os.environ.get("STALE_MIN", "8"))
CUM_KEYS = ("sot", "shots", "inbox", "corners", "xg", "red", "danger", "blocked", "fouls", "yellow")


EVENT_COOLDOWN = 2  # dk


def recent_event(m) -> str | None:
    """Son EVENT_COOLDOWN dakikada sinyali bozabilecek bir olay var mı?"""
    for e in reversed(getattr(m, "events", None) or []):
        t = (e.get("time") or {}).get("elapsed") or 0
        if m.minute - t > EVENT_COOLDOWN:
            continue
        typ, det = (e.get("type") or "").lower(), (e.get("detail") or "").lower()
        if typ == "goal" and "missed" not in det:
            return "gol"
        if typ == "goal" and "missed" in det:
            return "kaçan penaltı"
        if typ == "var":
            return "VAR incelemesi"
        if typ == "card" and "red" in det:
            return "kırmızı kart"
    return None


def data_health(m) -> str | None:
    """Veri sorunluysa sebebini döndürür (o maçta sinyal verilmez), sağlıklıysa None."""
    snaps = m.snaps
    if len(snaps) < 2:
        return "az veri"
    cur = snaps[-1]["stats"]
    h, a = cur["home"], cur["away"]
    # 1) Tutarlılık: isabetli/ceza sahası şut toplam şuttan fazla olamaz
    for t in (h, a):
        if t["shots"] > 0 and (t["sot"] > t["shots"] or t["inbox"] > t["shots"]):
            return "tutarsız şut verisi"
        if t["shots"] == 0 and t["sot"] > 0:
            return "tutarsız şut verisi"
    # 2) Boş veri: 15. dakikadan sonra hiç şut/faul/korner yoksa istatistik akmıyor demektir
    if m.minute >= 15 and sum(t[k] for t in (h, a) for k in ("shots", "fouls", "corners")) == 0:
        return "istatistik yok"
    # 3) Geriye gidiş: birikimli değerler azalmamalı (son iki ölçüm)
    prev = snaps[-2]["stats"]
    for side in ("home", "away"):
        for k in CUM_KEYS:
            if cur[side].get(k, 0) + 1e-9 < prev[side].get(k, 0) - (0.05 if k == "xg" else 0):
                return "veri geriye gitti"
    # 4) Donma: dakika ilerliyor ama STALE_MIN dakikadır hiçbir değer değişmiyor
    sig = lambda st: tuple(st[s].get(k, 0) for s in ("home", "away") for k in CUM_KEYS)
    now_sig, frozen_since = sig(cur), snaps[-1]["min"]
    for sn in reversed(snaps[:-1]):
        if sig(sn["stats"]) != now_sig:
            break
        frozen_since = sn["min"]
    if snaps[-1]["min"] - frozen_since >= STALE_MIN:
        return "veri donmuş"
    # 5) Gol üzerinden 4+ dk geçti ama şut sayısı hiç artmadı → istatistik geride kalmış
    #    (kendi kalesine goller şut sayılmaz, onlar hariç)
    last = snaps[-1]["min"]
    shot_goals = [g for g in (getattr(m, "goals", None) or [])
                  if not g.get("own") and last - 10 <= g["minute"] <= last - 4]
    base = next((sn for sn in snaps if sn["min"] >= min(g["minute"] for g in shot_goals) - 1), None) \
        if shot_goals else None
    if base and base is not snaps[-1]:
        if sum(cur[s]["shots"] for s in ("home", "away")) <= sum(base["stats"][s]["shots"] for s in ("home", "away")):
            return "gol var, şut yok"
    return None


SAMPLES_DIR = DATA / "ornekler"
REFEREE_JSON = DATA / "hakemler.json"          # geçmiş veri (analiz yazar)
REFEREE_LIVE = DATA / "hakemler_canli.json"    # canlı oturumların eklediği maçlar
CHAIN_FLAG = "/tmp/zincir"
CHECKPOINT_EVERY = 600   # sn: açık sinyaller ve veriler depoya yazılır (yeniden başlatmada kaybolmaz)
STOP_CHECK_EVERY = 120   # sn: depoda "dur" isteği var mı
SAMPLE_FIELDS = ["tarih", "fixture_id", "lig_id", "dakika", "skor_farki", "toplam_gol",
                 "baski_ev", "baski_dep", "kirmizi_farki", "mu", "dusus", "p_model",
                 "p_piyasa", "gol_10dk", "korner", "p_korner", "p_korner_piyasa", "korner_10dk",
                 "kart", "faul_10", "p_kart", "p_kart_piyasa", "kart_10dk",
                 "durum", "p_iy", "p_iy_piyasa", "p_kg", "p_kg_piyasa", "hucum_ev", "hucum_dep", "kirmizi_ev", "kirmizi_dep"]


# ---------------------------------------------------------------- olasılık yardımcıları
try:
    CALIB = json.loads((DATA / "model" / "kalibrasyon.json").read_text())
except Exception:
    CALIB = {}


def calibrate(tur: str, p, p_mkt):
    """Gece eğitimiyle öğrenilen kalibrasyon (yeterli sonuç birikip testte işe yaradıysa)."""
    c = CALIB.get(tur) or {}
    if p is None or p_mkt is None or not c.get("kullan"):
        return p
    w = c["agirliklar"]
    lm = math.log(max(min(p_mkt, 0.9999), 1e-4) / (1 - max(min(p_mkt, 0.9999), 1e-4)))
    lp = math.log(max(min(p, 0.9999), 1e-4) / (1 - max(min(p, 0.9999), 1e-4)))
    z = w[0] + w[1] * lm + w[2] * (lp - lm)
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


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


# ---- piyasa hareketi (CLV): sinyalden CLV_AFTER dk sonra aynı çizginin piyasa hızı
CLV_AFTER = int(os.environ.get("CLV_AFTER", "5"))
CLV_TOL = 1.03  # %3'ten küçük değişim "nötr"
OVER_DIR = {"gol": 1, "baski": 1, "iy": 1, "korner": 1, "kart": 1, "kg": 1, "alt": -1}


def goal_line_prob(item: dict | None, line: float) -> float | None:
    """Maç sonu gol Üst-Alt pazarında verilen çizginin Üst olasılığı."""
    for mk in (item or {}).get("odds") or []:
        name = (mk.get("name") or "").lower()
        if "over/under" not in name and "match goals" not in name:
            continue
        if any(w in name for w in ("1st", "2nd", "half", "corner", "card", "home", "away", "team", "asian")):
            continue
        over = under = None
        for v in mk.get("values") or []:
            try:
                if v.get("suspended") or abs(float(v.get("handicap")) - line) > 1e-6:
                    continue
                odd = float(v.get("odd"))
            except (TypeError, ValueError):
                continue
            lab = (v.get("value") or "").lower()
            if lab.startswith("over"):
                over = odd
            elif lab.startswith("under"):
                under = odd
        if over and under:
            return fair_prob(over, under)
    return None


def market_rate(m, odds, tur: str, line) -> tuple[float, int] | None:
    """Piyasanın bu sinyal için ima ettiği dakika başı olay hızı ve o anki sayı.
    Zaman geçtikçe olasılığın doğal düşüşünü ayıklamak için olasılık yerine hız kullanılır."""
    if not odds:
        return None
    if tur in ("gol", "alt", "baski"):
        count = m.gh + m.ga
        p, rem = goal_line_prob(odds, line), remaining_minutes(m.minute)
    elif tur == "iy":
        count = m.gh + m.ga
        p, rem = ou_line_prob(odds, "Over/Under (1st Half)", line), max(2.0, 47.5 - m.minute)
    elif tur in ("korner", "kart"):
        snap = m.snaps[-1]["stats"] if m.snaps else None
        if not snap:
            return None
        keys = ("corners",) if tur == "korner" else ("yellow", "red")
        count = int(sum(snap[s][k] for s in ("home", "away") for k in keys))
        names = ("Total Corners", "Asian Corners") if tur == "korner" else ("Total Cards",)
        p = next((x for x in (ou_line_prob(odds, nm, line) for nm in names) if x is not None), None)
        rem = remaining_minutes(m.minute)
    elif tur == "kg":
        count = int(m.gh > 0) + int(m.ga > 0)
        p, rem = two_way_prob(odds, "Both Teams to Score"), remaining_minutes(m.minute)
        if p is None or count == 2 or not (0.01 < p < 0.99):
            return None
        per = p if count == 1 else math.sqrt(p)  # gol atması gereken takım başına olasılık
        return -math.log(1 - per) / rem, count
    else:
        return None
    if p is None or not (0.01 < p < 0.99):
        return None
    k = int(line - count + 0.5)
    if k < 1 or k > 8:
        return None
    return solve_lambda(p, k) / rem, count


LINE = "━━━━━━━━━━━━━━"
CONF_ICON = {"yüksek": "●●● Yüksek", "orta": "●●○ Orta", "düşük": "●○○ Düşük"}


def stat_bullets(d: dict) -> list[str]:
    out = [f"   • {int(d['shots'])} şut ({int(d['sot'])} isabetli)", f"   • {int(d['corners'])} korner"]
    if d.get("xg", 0) > 0:
        out.append(f"   • xG +{d['xg']:.2f}")
    return out


def prob_lines(p, p_mkt, conf) -> list[str]:
    mk = f"%{round(p_mkt * 100)}" if p_mkt is not None else "yok"
    return [f"🔢 Model %{round(p * 100)}  •  Piyasa {mk}", f"📶 Güven: {CONF_ICON.get(conf, conf)}"]


def two_way_prob(item: dict | None, name: str, yes="Yes", no="No") -> float | None:
    for mk in (item or {}).get("odds") or []:
        if mk.get("name") != name:
            continue
        v = {x.get("value"): x for x in mk.get("values") or []}
        a, b = v.get(yes), v.get(no)
        if a and b and not a.get("suspended") and not b.get("suspended"):
            try:
                return fair_prob(float(a["odd"]), float(b["odd"]))
            except (TypeError, ValueError, ZeroDivisionError):
                return None
    return None


def ou_line_prob(item: dict | None, name: str, line: float) -> float | None:
    over = under = None
    for mk in (item or {}).get("odds") or []:
        if mk.get("name") != name:
            continue
        for v in mk.get("values") or []:
            try:
                if v.get("suspended") or abs(float(v.get("handicap")) - line) > 1e-6:
                    continue
                odd = float(v.get("odd"))
            except (TypeError, ValueError):
                continue
            if (v.get("value") or "").lower() == "over":
                over = odd
            elif (v.get("value") or "").lower() == "under":
                under = odd
    return fair_prob(over, under) if over and under else None


def count_line_market(item: dict | None, names: tuple, current: int):
    """Toplam (korner/kart) pazarında mevcut sayının üstündeki en yakın X.5 çizgisi ve Üst olasılığı."""
    lines = {}
    for mk in (item or {}).get("odds") or []:
        if mk.get("name") not in names:
            continue
        for v in mk.get("values") or []:
            if v.get("suspended"):
                continue
            try:
                h, odd = float(v.get("handicap")), float(v.get("odd"))
            except (TypeError, ValueError):
                continue
            if abs(h - math.floor(h) - 0.5) > 1e-6 or h < current:
                continue
            side = (v.get("value") or "").lower()
            if side in ("over", "under"):
                lines.setdefault(h, {})[side] = odd
    for h in sorted(lines):
        d = lines[h]
        if "over" in d and "under" in d:
            return h, fair_prob(d["over"], d["under"])
    return None, None


def fmt_line(x: float) -> str:
    return f"{x:.1f}"


def damp(f: float, horizon: float) -> float:
    """Baskının etkisi ~15 dakika sürer; uzun ufuklarda etkisini azalt."""
    return 1 + (f - 1) * min(1.0, 15.0 / max(horizon, 1.0))


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
        # Bugün önceki oturumlarda gönderilen sinyalleri hatırla: aynı maça aynı sinyal tekrar gitmesin
        flags = {"alt": "alt_gonderildi", "iy": "iy_gonderildi", "kg": "kg_gonderildi", "baski": "alarm_gonderildi"}
        for r in self.all_rows + self.rows_today:
            if r.get("tarih") != self.today or not r.get("fixture_id"):
                continue
            meta = self.meta.setdefault(int(r["fixture_id"]), {})
            tur = r.get("tur") or "gol"
            if tur in flags:
                meta[flags[tur]] = True
            if r.get("cizgi") not in ("", None):
                meta.setdefault("cizgiler", set()).add(f"{tur}:{float(r['cizgi'])}")
        self.odds_live_ok = True
        self.form = FormBook(self.api, self.today)
        self.injuries = kadro.InjuryBook(self.api)
        self.ctx = baglam.ContextBook(self.api)
        self.prof = profil.Profiles()
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
            notify("📉 ORAN DÜŞÜŞÜ", "\n\n".join(lines[:8]) + "\n" + LINE + "\nℹ️ Başlamasına 3 saatten az kalan maçlar",
                   priority=3, tags=["chart_with_downwards_trend"])

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
        from main import flush_outbox
        flush_outbox()
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
        # Sonucu bekleyen sinyali olan maçlar izleme listesinde olmasa bile her turda kontrol edilir
        pending_ids = {int(r["fixture_id"]) for r in self.rows_today if r.get("sonuc") == "bekliyor" and r.get("fixture_id")}
        tracked_ids = {m.id for m in tracked}
        to_final = [m for m in self.matches.values()
                    if m.id in pending_ids and m.status in FINISHED and not getattr(m, "finalized", False)]
        if to_final:
            self.finalize(to_final)
        for m in self.matches.values():
            if m.status in FINISHED:
                self.check_outcomes(m)
                self.label_samples(m)
                self.learn_referee(m)
            elif m.id in pending_ids and m.id not in tracked_ids:
                self.check_outcomes(m)
        self.tick_cost = cost
        return len(live_ms), len(tracked)

    def hot_tick(self):
        """Sinyale yakın (sıcak) maçları ana turlar arasında hızlıca yeniden tarar."""
        hot = [m for m in self.matches.values() if m.status in LIVE and m.status != "HT"
               and self.meta.get(m.id, {}).get("sicak")]
        if not hot:
            return 0
        pmb = self.per_minute_budget()
        if pmb and pmb < 8:  # kota sıkışıksa hızlı tarama yapılmaz
            return 0
        hot.sort(key=lambda m: -self.meta[m.id]["sicak"])
        hot = hot[:HOT_MAX]
        try:
            for it in self.api.get("fixtures", ids="-".join(str(m.id) for m in hot)):
                m = self.matches.get(it["fixture"]["id"])
                if m:
                    m.update(it)
            if self.odds_live_ok:
                for it in self.api.get("odds/live"):
                    m = self.matches.get(it["fixture"]["id"])
                    if m:
                        self.meta.setdefault(m.id, {})["odds"] = it
        except Exception as e:
            log("Hızlı tarama hatası:", e)
            return 0
        for m in hot:
            try:
                self.check_outcomes(m)
                self.evaluate(m)
            except Exception as e:
                log(f"{m.name} hızlı değerlendirme hatası:", e)
        return len(hot)

    # --- modeller
    def goal_model(self, m: Match, pr: dict):
        rate = self.mu(m.id) / 94.0
        total = pr["home"] + pr["away"]
        f = min(3.0, max(0.55, 0.55 + 0.45 * total / TYPICAL_TOTAL_PRESSURE))
        if PARAMS.get("gol_egrisi"):
            time_mult = curve_avg(PARAMS["gol_egrisi"], m.minute)
            time_mult *= (PARAMS.get("gol_durum") or {}).get(goal_state(*self.ctx.state_score(m)), 1.0)
        else:
            time_mult = 1.15 if m.minute >= 75 else 1.0
        p_model = 1 - math.exp(-rate * min(10.0, remaining_minutes(m.minute)) * f * time_mult * self.squad_total(m))
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

    @staticmethod
    def squad_sample(m) -> dict:
        try:
            net = kadro.sub_shift(m)
            rh, ra = kadro.red_counts(m)
            return {"hucum_ev": net["home"], "hucum_dep": net["away"], "kirmizi_ev": rh, "kirmizi_dep": ra}
        except Exception:
            return {}

    def squad_lines(self, m) -> list[str]:
        """Mesaj için kırmızı kart, değişiklik ve eksik oyuncu satırları."""
        out = []
        try:
            out += kadro.info_lines(m, self.injuries.get(m))
        except Exception as e:
            log("Kadro bilgisi hatası:", e)
        try:
            out += self.ctx.lines(m)[0]
        except Exception as e:
            log("Bağlam hatası:", e)
        try:
            out += self.prof.lines(m)
        except Exception as e:
            log("Profil hatası:", e)
        return out

    def context_tag(self, m) -> str:
        try:
            return self.ctx.lines(m)[1]
        except Exception:
            return ""

    def squad_total(self, m) -> float:
        """Kırmızı kart, oyuncu değişiklikleri ve (60. dk sonrası) takım profillerinin toplam gol hızına etkisi."""
        try:
            h, a = kadro.goal_mults(m)
            mult = (h + a) / 2
        except Exception:
            mult = 1.0
        if m.minute >= 60:
            try:
                mult *= self.prof.late_mult(m)
            except Exception:
                pass
        return mult

    def time_mult(self, m: Match, minute: int, span: int) -> float:
        if PARAMS.get("gol_egrisi"):
            tm = curve_avg(PARAMS["gol_egrisi"], minute, max(1, span))
            return tm * (PARAMS.get("gol_durum") or {}).get(goal_state(*self.ctx.state_score(m)), 1.0)
        return 1.0

    def rest_goal(self, m: Match, pr: dict):
        """Maç sonuna kadar en az bir gol daha (Üst toplam+0.5)."""
        rem = remaining_minutes(m.minute)
        f = min(3.0, max(0.55, 0.55 + 0.45 * (pr["home"] + pr["away"]) / TYPICAL_TOTAL_PRESSURE))
        rate = self.mu(m.id) / 94.0 * self.time_mult(m, m.minute, int(rem)) * self.squad_total(m)
        p_model = 1 - math.exp(-rate * rem * damp(f, rem))
        odds = self.meta.get(m.id, {}).get("odds")
        p_mkt = parse_live_goal_market(odds, m.gh + m.ga) if odds else None
        return p_model, p_mkt, m.gh + m.ga + 0.5

    def corner_total(self, m: Match, pr: dict):
        cur, base, span = pr["cur"]["stats"], pr["base"]["stats"], max(pr["span"], 6)
        corners = int(cur["home"]["corners"] + cur["away"]["corners"])
        d = lambda k: sum(max(0.0, cur[s][k] - base[s][k]) for s in ("home", "away"))
        attack = (d("shots") + 0.5 * d("blocked") + 0.8 * d("corners")) * 10 / span
        f = min(2.2, max(0.6, 0.6 + 0.4 * attack / TYPICAL_ATTACK_10))
        rate = league_param(m.league_id, "korner", LEAGUE_CORNERS) / 94
        if m.minute >= 20:
            rate = 0.6 * rate + 0.4 * corners / m.minute
        if abs(m.gh - m.ga) == 1 and m.minute >= 70:
            f *= 1.15
        line, p_mkt = count_line_market(self.meta.get(m.id, {}).get("odds"), ("Total Corners", "Asian Corners"), corners)
        if line is None:
            return None, None, None, f, corners
        rem = remaining_minutes(m.minute)
        p_model = poisson_over(rate * rem * damp(f, rem), int(line - corners + 0.5))
        return p_model, p_mkt, line, f, corners

    def card_total(self, m: Match, pr: dict):
        cur, base, span = pr["cur"]["stats"], pr["base"]["stats"], max(pr["span"], 6)
        cards = int(sum(cur[s]["yellow"] + cur[s]["red"] for s in ("home", "away")))
        fouls10 = sum(max(0.0, cur[s]["fouls"] - base[s]["fouls"]) for s in ("home", "away")) * 10 / span
        avg, src = self.referee_avg(m)
        rem = remaining_minutes(m.minute)
        phase = curve_avg(PARAMS["kart_egrisi"], m.minute, int(rem)) if PARAMS.get("kart_egrisi") else 1.2
        typical = league_param(m.league_id, "faul", TYPICAL_FOULS_10 / 10 * 94) * 10 / 94
        f = min(2.2, max(0.6, 0.6 + 0.4 * fouls10 / typical))
        if abs(m.gh - m.ga) <= 1 and m.minute >= 70:
            f *= PARAMS.get("kart_gec_yakin", 1.2)
        if m.minute >= 20:
            avg = 0.7 * avg + 0.3 * cards / m.minute * 94
        line, p_mkt = count_line_market(self.meta.get(m.id, {}).get("odds"), ("Total Cards",), cards)
        if line is None:
            return None, None, None, f, cards, fouls10, src
        p_model = poisson_over(avg / 94 * phase * rem * damp(f, rem), int(line - cards + 0.5))
        return p_model, p_mkt, line, f, cards, fouls10, src

    def iy_model(self, m: Match, pr: dict):
        """Devre arasına kadar en az bir gol."""
        rem = max(2.0, 47.5 - m.minute)
        f = min(3.0, max(0.55, 0.55 + 0.45 * (pr["home"] + pr["away"]) / TYPICAL_TOTAL_PRESSURE))
        rate = self.mu(m.id) / 94.0 * self.time_mult(m, m.minute, int(rem)) * self.squad_total(m)
        p_model = 1 - math.exp(-rate * rem * damp(f, rem))
        p_mkt = ou_line_prob(self.meta.get(m.id, {}).get("odds"), "Over/Under (1st Half)", m.gh + m.ga + 0.5)
        return p_model, p_mkt

    def kg_model(self, m: Match, pr: dict):
        """Karşılıklı gol: henüz gol atmamış takım(lar)ın maç sonuna kadar gol atma olasılığı."""
        rem = remaining_minutes(m.minute)
        need = [s for s, g in (("home", m.gh), ("away", m.ga)) if g == 0]
        tm = self.time_mult(m, m.minute, int(min(rem, 40)))
        p = 1.0
        try:
            gm = dict(zip(("home", "away"), kadro.goal_mults(m)))
        except Exception:
            gm = {"home": 1.0, "away": 1.0}
        for side in need:
            f = min(3.0, max(0.55, 0.55 + 0.45 * pr[side] / (TYPICAL_TOTAL_PRESSURE / 2)))
            rate = self.mu(m.id) / 2 / 94.0 * tm * gm[side]
            p *= 1 - math.exp(-rate * rem * damp(f, rem))
        p_mkt = two_way_prob(self.meta.get(m.id, {}).get("odds"), "Both Teams to Score")
        return p, p_mkt, need

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
        self.record_odds(m)
        if m.id not in self.ctx.aggs:
            self.ctx.first_leg(m)
        heat = max(max(pr["home"], pr["away"]) / THRESHOLD, (pr["home"] + pr["away"]) / COMBINED_THRESHOLD)
        meta["sicak"] = heat if heat >= HOT_RATIO and m.minute < 88 else 0
        pg, pg_m, qg = self.goal_model(m, pr)
        pc, pc_m, fc, corners, qc = self.corner_model(m, pr)
        pk, pk_m, fk, cards, fouls10, ref_src, qk = self.card_model(m, pr)
        meta["q"] = {"gol": qg, "korner": qc, "kart": qk}
        piy, piy_m = self.iy_model(m, pr) if m.status == "1H" else (None, None)
        piy = calibrate("iy", piy, piy_m)
        pkg, pkg_m, kg_need = self.kg_model(m, pr) if not (m.gh and m.ga) else (None, None, [])
        pkg = calibrate("kg", pkg, pkg_m)
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
                "durum": m.status, "p_iy": r3(piy), "p_iy_piyasa": r3(piy_m),
                "p_kg": r3(pkg), "p_kg_piyasa": r3(pkg_m),
                **self.squad_sample(m),
            })
        if m.minute >= 88:
            return
        # Kalite filtresi: gençlik/kadın/yedek/hazırlık maçlarında sinyal yok
        if LOW_QUALITY.search(f"{m.league} {m.home} {m.away}"):
            return
        # Olay soğuması: gol, VAR/penaltı ya da kırmızı karttan sonraki 2 dk oranlar ve istatistik eski kalabilir
        olay = recent_event(m)
        if olay:
            if meta.get("son_olay") != olay:
                meta["son_olay"] = olay
                log(f"  [olay] {m.name} {m.minute}': {olay} → 2 dk sinyal yok")
            return
        # Veri sağlığı: donmuş/tutarsız/eksik istatistikle sinyal verilmez
        sorun = data_health(m)
        if sorun:
            if meta.get("veri_sorunu") != sorun:
                meta["veri_sorunu"] = sorun
                print(f"  [veri] {m.name} {m.minute}': {sorun} → sinyal yok")
            return
        meta.pop("veri_sorunu", None)
        # Eğitilmiş model piyasadan iyiyse onun olasılığı kullanılır
        sample_like = {"baski_ev": pr["home"], "baski_dep": pr["away"], "dakika": m.minute,
                           "skor_farki": m.gh - m.ga, "kirmizi_farki": pr["cur"]["stats"]["home"]["red"] - pr["cur"]["stats"]["away"]["red"],
                           "faul_10": fouls10, "p_model": pg, "p_piyasa": pg_m, "p_korner": pc,
                       "p_korner_piyasa": pc_m, "p_kart": pk, "p_kart_piyasa": pk_m}
        pg = self.trained_prob("gol", sample_like, pg)
        pc = self.trained_prob("korner", sample_like, pc)
        pk = self.trained_prob("kart", sample_like, pk)
        has_xg = pr["cur"]["stats"].get("has_xg")
        extra = 0 if has_xg else NO_XG_EXTRA_EDGE
        pu, pu_m, ust_line = self.rest_goal(m, pr)
        pu = calibrate("gol", pu, pu_m)
        # ⚽ maç sonu Üst (toplam + 0.5): anlık baskı tetikler, tahmin maç sonunu kapsar
        sent = meta.setdefault("cizgiler", set())  # her tür ve çizgi için maç başına bir sinyal
        # Zıt yön kilidi: bekleyen Alt varken gol yönlü sinyal, bekleyen gol yönlü sinyal varken Alt gitmez
        alt_acik = lambda: "alt" in self.pending_types(m.id)
        gol_acik = lambda: bool(self.pending_types(m.id) & set(self.GOL_YONLU))
        if ("gol" in SIGNALS_ON and not alt_acik() and m.minute - m.last_signal_min >= 12 and pu_m is not None
                and f"gol:{float(ust_line)}" not in sent):
            side = signal_side(pr)
            if side and pu - pu_m >= EDGE_UST + extra:
                ok, fi = self.form_gate(m, "gol", ust_line, pu - pu_m, EDGE_UST + extra)
                if ok:
                    m.last_signal_min = m.minute
                    sent.add(f"gol:{float(ust_line)}")
                    self.goal_signal(m, pr, side, pu, pu_m, qg, ust_line, form=fi)
        # 🧊 maç sonu Alt: son 15 dakikada iki takımın da baskısı çok düşükse
        # (Alt riskli bir tercih: sadece xG verisi olan maçlarda; uzun sessizlik ve isabetli şut/xG şartı)
        if ("alt" in SIGNALS_ON and has_xg and not gol_acik() and not meta.get("alt_gonderildi") and pu_m is not None
                and remaining_minutes(m.minute) <= 40):
            pr15 = evaluate_pressure(m, lookback=15, min_span=14)
            if pr15 and not pr15.get("goal_in_window"):
                d = {k: pr15["d_home"][k] + pr15["d_away"][k] for k in pr15["d_home"]}
                p_alt, p_alt_m = 1 - self.rest_goal(m, pr15)[0], 1 - pu_m
                p_alt = calibrate("alt", p_alt, p_alt_m)
                quiet = (pr15["home"] + pr15["away"] <= COMBINED_THRESHOLD * 0.35
                         and d["sot"] <= 1 and d["xg"] <= 0.25 and d["inbox"] <= 2)
                ok, fi = (self.form_gate(m, "alt", ust_line, p_alt - p_alt_m, EDGE_ALT)
                          if quiet and p_alt - p_alt_m >= EDGE_ALT else (False, None))
                if ok:
                    meta["alt_gonderildi"] = True
                    self.simple_signal(m, "alt", p_alt, p_alt_m, None, None, [
                        f"🧊 Son {int(pr15['span'])} dk iki takım toplam:", *stat_bullets(d),
                        f"   • ceza sahası içi şut: {int(d['inbox'])}"], cizgi=ust_line, form=fi)
        # 🚩 maç sonu korner Üst
        pcu, pcu_m, c_line, fcu, corners_now = self.corner_total(m, pr)
        pcu = calibrate("korner", pcu, pcu_m)
        if ("korner" in SIGNALS_ON and pcu is not None and m.minute - meta.get("son_korner", -99) >= 15
                and f"korner:{float(c_line)}" not in sent and fcu >= 1.4 and pcu - pcu_m >= EDGE_CORNER
                and (fk_ := self.form_gate(m, "korner", c_line, pcu - pcu_m, EDGE_CORNER))[0]):
            meta["son_korner"] = m.minute
            sent.add(f"korner:{float(c_line)}")
            d = {k: pr["d_home"][k] + pr["d_away"][k] for k in pr["d_home"]}
            self.simple_signal(m, "korner", pcu, pcu_m, corners_now, None, [
                f"🚩 Şu an: {corners_now} korner",
                f"📈 Son {int(pr['span'])} dk (iki takım):", *stat_bullets(d)], cizgi=c_line, form=fk_[1])
        # ⏱ ilk yarı golü
        if ("iy" in SIGNALS_ON and not alt_acik() and piy is not None and piy_m is not None and 15 <= m.minute <= 40
                and not meta.get("iy_gonderildi") and not pr.get("goal_in_window")
                and (signal_side(pr) or pr["home"] + pr["away"] >= COMBINED_THRESHOLD * 0.8)
                and piy - piy_m >= EDGE_IY + extra
                and (fi_ := self.form_gate(m, "iy", m.gh + m.ga + 0.5, piy - piy_m, EDGE_IY + extra))[0]):
            meta["iy_gonderildi"] = True
            meta["son_gol_turu"] = m.minute
            side = signal_side(pr)
            team = m.home if side == "ev" else m.away if side == "deplasman" else "İki takım"
            dd = pr["d_home"] if side == "ev" else pr["d_away"] if side == "deplasman" else {
                k: pr["d_home"][k] + pr["d_away"][k] for k in pr["d_home"]}
            self.simple_signal(m, "iy", piy, piy_m, m.gh + m.ga, None, [
                f"📈 Son {int(pr['span'])} dk ({team}):", *stat_bullets(dd)], cizgi=m.gh + m.ga + 0.5, form=fi_[1])
        # 🤝 karşılıklı gol
        if ("kg" in SIGNALS_ON and not alt_acik() and pkg is not None and pkg_m is not None and 25 <= m.minute <= 80
                and not meta.get("kg_gonderildi")
                and any(pr[sd] >= THRESHOLD * 0.8 for sd in kg_need)
                and pkg - pkg_m >= EDGE_KG + extra
                and (fg_ := self.form_gate(m, "kg", None, pkg - pkg_m, EDGE_KG + extra))[0]):
            meta["kg_gonderildi"] = True
            meta["son_gol_turu"] = m.minute
            names = [m.home if sd == "home" else m.away for sd in kg_need]
            sd0 = max(kg_need, key=lambda sd: pr[sd])
            self.simple_signal(m, "kg", pkg, pkg_m, f"{m.gh}-{m.ga}", None, [
                f"👉 Gol atması gereken: {' ve '.join(names)}",
                f"📈 Son {int(pr['span'])} dk ({m.home if sd0 == 'home' else m.away}):",
                *stat_bullets(pr["d_home"] if sd0 == "home" else pr["d_away"])], form=fg_[1])
        # 🔥 baskı alarmı: piyasa şartı olmadan, çok yüksek baskıda bilgi amaçlı
        # (aynı maçta son 12 dakikada gol sinyali gittiyse alarm tekrar etmez)
        if ("baski" in SIGNALS_ON and not alt_acik() and not meta.get("alarm_gonderildi") and 10 <= m.minute <= 85
                and m.minute - m.last_signal_min >= 12 and m.minute - meta.get("son_gol_turu", -99) >= 12
                and not pr.get("goal_in_window") and max(pr["home"], pr["away"]) >= THRESHOLD * ALARM_MULT
                and (fb_ := self.form_gate(m, "baski", None, only="home" if pr["home"] >= pr["away"] else "away"))[0]):
            meta["alarm_gonderildi"] = True
            sd = "home" if pr["home"] >= pr["away"] else "away"
            team = m.home if sd == "home" else m.away
            lines = [f"📈 Son {int(pr['span'])} dk ({team}):", *stat_bullets(pr["d_home"] if sd == "home" else pr["d_away"])]
            if pg_m is not None and pg - pg_m < EDGE:
                lines.append("⚠️ Piyasa da bu baskıyı fiyatlıyor, oranlar düşük")
            end = min(m.minute + ALARM_WINDOW, 45 if m.status == "1H" else 90)  # ilk yarıda devre sonunu geçmez
            self.simple_signal(m, "baski", pg, pg_m, team, qg, lines, cizgi=end, form=fb_[1])
        # 🟨 maç sonu kart Üst
        pku, pku_m, k_line, fku, cards_now, fouls_now, ref_now = self.card_total(m, pr)
        pku = calibrate("kart", pku, pku_m)
        if ("kart" in SIGNALS_ON and pku is not None and m.minute - meta.get("son_kart", -99) >= 15
                and f"kart:{float(k_line)}" not in sent and fku >= 1.5 and pku - pku_m >= EDGE_CARD
                and (fkk_ := self.form_gate(m, "kart", k_line, pku - pku_m, EDGE_CARD))[0]):
            meta["son_kart"] = m.minute
            sent.add(f"kart:{float(k_line)}")
            self.simple_signal(m, "kart", pku, pku_m, cards_now, None, [
                f"🟨 Şu an: {cards_now} kart",
                f"🦵 Faul temposu: 10 dk'da {fouls_now:.1f} (normali ~{TYPICAL_FOULS_10})",
                f"👨‍⚖️ Hakem: {ref_now}"], cizgi=k_line, form=fkk_[1])

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

    GOL_YONLU = ("gol", "iy", "kg", "baski")

    def clv_start(self, m, row, tur, line):
        """Sinyal anındaki piyasa hızını kaydeder (sonra lehte/aleyhte dönüşü ölçmek için)."""
        if tur == "baski" or line is None:
            line = m.gh + m.ga + 0.5
            tur_m = "baski"
        else:
            tur_m = tur
        # son dakikalarda kalan süre çok kısa, hız ölçümü yanıltıcı olur
        late = m.minute + CLV_AFTER > (40 if tur_m == "iy" else 84)
        try:
            r = None if late else market_rate(m, self.meta.get(m.id, {}).get("odds"), tur_m, float(line))
        except Exception:
            r = None
        row["q_once"] = round(r[0], 5) if r else ""
        row["q_sonra"] = ""
        row["_clv"] = (tur_m, float(line), r[1] if r else None)

    def clv_check(self, m, r):
        """Sinyalden CLV_AFTER dk sonra piyasa bizim yönümüze döndü mü?"""
        if r.get("piyasa_yon") or r.get("q_once") in ("", None) or "_clv" not in r:
            return
        if m.minute < int(r["dakika"]) + CLV_AFTER:
            return
        tur_m, line, count0 = r["_clv"]
        try:
            now = market_rate(m, self.meta.get(m.id, {}).get("odds"), tur_m, line)
        except Exception:
            now = None
        if now is None:
            if m.minute >= int(r["dakika"]) + CLV_AFTER + 5:
                r["piyasa_yon"] = "veri yok"
            return
        rate, count = now
        if count0 is not None and count != count0:  # arada olay oldu: hız karşılaştırılamaz
            r["piyasa_yon"] = "olay"
            return
        q0 = float(r["q_once"])
        r["q_sonra"] = round(rate, 5)
        d = OVER_DIR.get(r.get("tur") or "gol", 1)
        if (rate > q0 * CLV_TOL and d > 0) or (rate < q0 / CLV_TOL and d < 0):
            r["piyasa_yon"] = "lehte"
        elif (rate < q0 / CLV_TOL and d > 0) or (rate > q0 * CLV_TOL and d < 0):
            r["piyasa_yon"] = "aleyhte"
        else:
            r["piyasa_yon"] = "nötr"

    def form_gate(self, m, tur, line, edge=None, need=None, only=None):
        """Sinyal kapısı: son 5 maç formu ve canlı oran akışı tahmine ters düşüyorsa daha güçlü kanıt ister.
        Dönüş: (gönderilsin_mi, bilgi) — bilgi: {'lines': [...], 'hits','n' (form), 'akis' (oran akışı)}."""
        info, extra, why = {"lines": []}, 0.0, []
        if FORM_ON:
            try:
                fi = form_check(self.form, tur, line, (getattr(m, "home_id", None), m.home),
                                (getattr(m, "away_id", None), m.away), only=only)
            except Exception as e:
                log("Form hatası:", e)
                fi = None
            if fi:
                r = fi["ratio"]
                etki = "ters" if r <= FORM_WEAK else "destek" if r >= FORM_STRONG else "nötr"
                info.update(hits=fi["hits"], n=fi["n"], etki=etki)
                info["lines"].append(fi["text"] + {"ters": "  ⚠️", "destek": "  ✅", "nötr": ""}[etki])
                if etki == "ters":
                    extra += FORM_EXTRA
                    why.append(f"form ters ({fi['hits']}/{fi['n']})")
        st = self.steam(m)
        if st is not None and tur in STEAM_DIR:
            info["akis"] = st
            yon = STEAM_DIR[tur] * (1 if st >= STEAM_UP else -1 if st <= STEAM_DOWN else 0)
            pc = round((st - 1) * 100)
            if yon > 0:
                info["lines"].append(f"💹 Piyasa son {STEAM_MIN} dk'da gol beklentisini %{abs(pc)} "
                                     f"{'artırdı' if pc > 0 else 'azalttı'} (aynı yönde)  ✅")
            elif yon < 0:
                info["lines"].append(f"💹 Piyasa son {STEAM_MIN} dk'da gol beklentisini %{abs(pc)} "
                                     f"{'artırdı' if pc > 0 else 'azalttı'} (ters yönde)  ⚠️")
                extra += STEAM_EXTRA
                why.append(f"oran akışı ters ({pc:+d}%)")
        if extra > 0 and (edge is None or need is None or edge < need + extra):
            key = f"{tur}:{line}"
            red = self.meta.setdefault(m.id, {}).setdefault("form_red", set())
            if key not in red:
                red.add(key)
                log(f"  [kapı] {m.name} {tur} {line}: {', '.join(why)} → sinyal yok")
            return False, info
        return True, info

    def steam(self, m) -> float | None:
        """Olaysız oran akışı: gol pazarının ima ettiği hız, STEAM_MIN dk önceye göre kaç kat?"""
        hist = self.meta.get(m.id, {}).get("oran_gecmis") or []
        if len(hist) < 2:
            return None
        mn, goals, rate = hist[-1]
        past = [h for h in hist if mn - STEAM_MIN - 3 <= h[0] <= mn - STEAM_MIN and h[1] == goals]
        if not past or not past[-1][2]:
            return None
        return rate / past[-1][2]

    def record_odds(self, m):
        odds = self.meta.get(m.id, {}).get("odds")
        p = parse_live_goal_market(odds, m.gh + m.ga) if odds else None
        if p is None or not (0.01 < p < 0.99) or m.minute > 85:
            return
        rate = -math.log(1 - p) / remaining_minutes(m.minute)
        hist = self.meta.setdefault(m.id, {}).setdefault("oran_gecmis", [])
        if hist and hist[-1][0] == m.minute and hist[-1][1] == m.gh + m.ga:
            hist[-1] = (m.minute, m.gh + m.ga, rate)
        else:
            hist.append((m.minute, m.gh + m.ga, rate))
        del hist[:-40]

    def pending_types(self, mid) -> set:
        """Bu maçta sonucu hâlâ bekleyen sinyal türleri (zıt yönlü sinyal gitmesin diye)."""
        return {r.get("tur") or "gol" for r in self.rows_today
                if str(r.get("fixture_id")) == str(mid) and r.get("sonuc") == "bekliyor"}

    def simple_signal(self, m, tur, p, p_mkt, count, q, lines, cizgi=None, form=None):
        ratio = p / p_mkt if p_mkt else None
        conf = "yüksek" if (ratio and ratio >= 1.25) else "orta" if (ratio and ratio >= 1.12) else "düşük"
        row = self._row(m, tur, "", p, p_mkt, conf, {"baski": "" if count is None else count,
                                                       "q_once": "" if q is None else round(q, 5),
                                                       "cizgi": "" if cizgi is None else cizgi,
                                                       "form": f"{form['hits']}/{form['n']}" if form and "hits" in form else "",
                                                       "oran_akisi": f"{(form['akis'] - 1) * 100:+.0f}%" if form and form.get("akis") else "",
                                                       "baglam": self.context_tag(m)})
        self.clv_start(m, row, tur, cizgi)
        self.rows_today.append(row)
        icon, label = {"korner": ("🚩", "KORNER SİNYALİ"), "kart": ("🟨", "KART SİNYALİ"),
                       "iy": ("⏱", "İLK YARI GOL SİNYALİ"), "kg": ("🤝", "KG SİNYALİ"),
                       "baski": ("🔥", "BASKI ALARMI"), "alt": ("🧊", "ALT SİNYALİ")}[tur]
        log(f"SİNYAL[{tur}] {m.name} {m.minute}' model={p:.2f} piyasa={p_mkt} çizgi={cizgi}")
        ln = fmt_line(cizgi) if cizgi is not None and tur != "baski" else ""
        tahmin = {"korner": f"🎯 TAHMİN: MAÇ SONU KORNER {ln} ÜST",
                  "kart": f"🎯 TAHMİN: MAÇ SONU KART {ln} ÜST",
                  "alt": f"🎯 TAHMİN: MAÇ SONU {ln} ALT",
                  "iy": f"🎯 TAHMİN: İLK YARI {ln} ÜST (devreden önce gol)",
                  "kg": "🎯 TAHMİN: KARŞILIKLI GOL (KG VAR)",
                  "baski": (f"🎯 DİKKAT: {count} yoğun baskıda, devre bitmeden gol bekleniyor" if cizgi == 45 else
                            f"🎯 DİKKAT: {count} yoğun baskıda, {m.minute}'-{int(cizgi or m.minute + ALARM_WINDOW)}' arası gol bekleniyor")}[tur]
        body = [tahmin, LINE, f"🏆 {m.league}", f"⏱ {m.minute}'  •  📊 Skor {m.gh}-{m.ga}", "",
                *lines, *(form["lines"] if form else []), *self.squad_lines(m), "", *prob_lines(p, p_mkt, conf), LINE,
                "ℹ️ Bahis tavsiyesi değildir."]
        row["tg_mesaj"] = notify(f"{icon} {label}: {m.name}", "\n".join(body), priority=4,
                                 on_tg=lambda mid, r=row: r.__setitem__("tg_mesaj", mid),
                                 tags=[{"korner": "triangular_flag_on_post", "kart": "yellow_square", "alt": "ice_cube",
                                        "iy": "stopwatch", "kg": "handshake", "baski": "fire"}[tur]]) or ""

    def goal_signal(self, m, pr, side, p_model, p_mkt, q=None, line=None, form=None):
        drop = self.book.over_drop(m.id)
        ratio = p_model / p_mkt if p_mkt else None
        conf = "yüksek" if (ratio and ratio >= 1.4) else "orta" if (ratio and ratio >= 1.2) else "düşük"
        ai = None
        if self.claude.available() and self.claude.calls < MAX_CLAUDE:
            ctx = {"lig": m.league, "mac": m.name, "dakika": m.minute, "skor": f"{m.gh}-{m.ga}",
                   "pencere_dk": pr["span"], "ev_son_pencere": pr["d_home"], "dep_son_pencere": pr["d_away"],
                   "ev_toplam": pr["cur"]["stats"]["home"], "dep_toplam": pr["cur"]["stats"]["away"],
                   "mac_oncesi_beklenen_gol": round(self.mu(m.id), 2), "mac_oncesi_ust_oran_dususu": drop,
                   "tahmin": f"maç sonu {line} üst (en az bir gol daha)",
                   "model_mac_sonu_gol_olasiligi": round(p_model, 3),
                   "piyasa_mac_sonu_gol_olasiligi": None if p_mkt is None else round(p_mkt, 3),
                   "model_taraf": side,
                   "son5_benzer_sonuc": f"{form['hits']}/{form['n']} maçta" if form and "hits" in form else None,
                   "piyasa_akisi_5dk": f"{(form['akis'] - 1) * 100:+.0f}%" if form and form.get("akis") else None}
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
            "gonderildi": "evet" if send else "hayır", "q_once": "" if q is None else round(q, 5),
            "cizgi": line if line is not None else "",
            "form": f"{form['hits']}/{form['n']}" if form and "hits" in form else "",
            "oran_akisi": f"{(form['akis'] - 1) * 100:+.0f}%" if form and form.get("akis") else "",
            "baglam": self.context_tag(m)})
        self.clv_start(m, row, "gol", line)
        self.rows_today.append(row)
        log(f"SİNYAL[gol] {m.name} {m.minute}' {side} model={p_model:.2f} piyasa={p_mkt} gönder={send}")
        if not send:
            return
        d = pr["d_home"] if side == "ev" else pr["d_away"] if side == "deplasman" else {
            k: pr["d_home"][k] + pr["d_away"][k] for k in pr["d_home"]}
        team = m.home if side == "ev" else m.away if side == "deplasman" else "İki takım"
        lines = [f"🎯 TAHMİN: MAÇ SONU {fmt_line(line)} ÜST"]
        if side in ("ev", "deplasman"):
            lines.append(f"👉 Baskıyı kuran: {team}")
        lines += [LINE, f"🏆 {m.league}", f"⏱ {m.minute}'  •  📊 Skor {m.gh}-{m.ga}", "",
                  f"📈 Son {int(pr['span'])} dk ({team}):", *stat_bullets(d),
                  *(form["lines"] if form else []), *self.squad_lines(m), "",
                  *prob_lines(p_model, p_mkt, conf)]
        if drop >= DROP_PCT:
            lines.append(f"📉 Maç öncesi Üst 2.5 oranı %{round(drop * 100)} düşmüştü")
        if ai and ai.get("yorum"):
            lines += ["", f"🤖 {ai['yorum']}"]
        lines += [LINE, "ℹ️ Bahis tavsiyesi değildir."]
        row["tg_mesaj"] = notify(f"⚽ GOL SİNYALİ: {m.name}", "\n".join(lines),
                                 on_tg=lambda mid, r=row: r.__setitem__("tg_mesaj", mid),
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

    def current_count(self, m: Match, key: str) -> int | None:
        final = getattr(m, "final", None)
        if final:
            return int(final["home"][key] + final["away"][key])
        if m.snaps:
            st = m.snaps[-1]["stats"]
            return int(st["home"][key] + st["away"][key])
        return None

    def finalize(self, matches: list):
        """Biten maçların son skorunu, olaylarını ve istatistiklerini çeker (sonuçlar için)."""
        from main import parse_stats
        ids = [str(m.id) for m in matches]
        for i in range(0, len(ids), 20):
            try:
                items = self.api.get("fixtures", ids="-".join(ids[i:i + 20]))
            except Exception as e:
                log("Bitiş verisi alınamadı:", e)
                continue
            for it in items:
                m = self.matches.get(it["fixture"]["id"])
                if not m:
                    continue
                m.status = it["fixture"]["status"]["short"]
                m.minute = it["fixture"]["status"].get("elapsed") or m.minute
                m.gh, m.ga = it["goals"]["home"] or 0, it["goals"]["away"] or 0
                m.goals = goal_events(it)
                m.events = it.get("events") or []
                if m.status in FINISHED:
                    m.final = parse_stats(it)
                m.finalized = m.status in FINISHED

    def resolve_carried(self):
        """Önceki oturumdan devralınan sinyallerin maçlarını yükler ve bitenleri sonuçlandırır."""
        ids = {int(r["fixture_id"]) for r in self.rows_today if r.get("fixture_id") and r["sonuc"] == "bekliyor"}
        if not ids:
            return
        for fid in ids:
            if fid not in self.matches:
                try:
                    it = self.api.get("fixtures", ids=str(fid))[0]
                    self.matches[fid] = Match(it)
                except Exception:
                    continue
        self.finalize([self.matches[f] for f in ids if f in self.matches])
        for f in ids:
            if f in self.matches:
                self.check_outcomes(self.matches[f])

    def recheck_goal_results(self, m: Match):
        """Gol sayesinde sonuçlanan sinyalleri, gol VAR ile iptal edilirse yeniden açar."""
        if m.status in FINISHED:
            return
        for r in self.rows_today:
            if int(r["fixture_id"]) != m.id or r["sonuc"] not in ("isabet", "ıska"):
                continue
            tur = r.get("tur") or "gol"
            sh, sa = (int(x) for x in r["skor"].split("-"))
            total_now, total_then = m.gh + m.ga, sh + sa
            by_goal = ((tur in ("gol", "baski", "iy") and r["sonuc"] == "isabet") or
                       (tur == "alt" and r["sonuc"] == "ıska") or (tur == "kg" and r["sonuc"] == "isabet"))
            if not by_goal:
                continue
            undone = total_now <= total_then if tur != "kg" else not (m.gh > 0 and m.ga > 0)
            if tur == "iy" and m.status not in ("1H", "HT"):
                undone = False
            if not undone:
                continue
            log(f"VAR düzeltmesi: {m.name} {tur} {r['dakika']}' → yeniden açık")
            r.update({"sonuc": "bekliyor", "gol_dakikasi": "", "kar": "", "adil_oran": "", "duyuruldu": ""})
            if r.get("tg_mesaj"):
                from main import telegram_reply
                telegram_reply(r["tg_mesaj"], f"⚠️ DÜZELTME\n🖥 Gol VAR ile iptal edildi, sinyal yeniden açık\n📊 Skor: {m.gh}-{m.ga}")

    def check_outcomes(self, m: Match):
        self.recheck_goal_results(m)
        for r in self.rows_today:
            if int(r["fixture_id"]) != m.id or r["sonuc"] != "bekliyor":
                continue
            smin, tur = int(r["dakika"]), r.get("tur") or "gol"
            # piyasa hareketi: sinyalden CLV_AFTER dk sonra aynı çizgide piyasa bizim yönümüze döndü mü
            self.clv_check(m, r)
            over = m.minute > smin + 10 or m.status in FINISHED
            done = m.status in FINISHED
            sh, sa = (int(x) for x in r["skor"].split("-"))
            after = sorted(g["minute"] for g in m.goals if g["minute"] >= smin)
            more_goals = (m.gh + m.ga) > (sh + sa)
            line = float(r["cizgi"]) if r.get("cizgi") not in ("", None) else None
            if tur == "gol":  # maç sonu Üst
                if more_goals:
                    r["sonuc"], r["gol_dakikasi"] = "isabet", (after[0] if after else m.minute)
                elif done:
                    r["sonuc"] = "ıska"
            elif tur == "alt":
                if more_goals:
                    r["sonuc"], r["gol_dakikasi"] = "ıska", (after[0] if after else m.minute)
                elif done:
                    r["sonuc"] = "isabet"
            elif tur == "baski":  # ALARM_WINDOW dakika içinde gol
                end = int(float(r["cizgi"])) if r.get("cizgi") not in ("", None) else smin + ALARM_WINDOW
                hits = [g for g in m.goals if smin < g["minute"] <= end]
                if hits or (more_goals and m.minute <= end):
                    r["sonuc"], r["gol_dakikasi"] = "isabet", (hits[0]["minute"] if hits else m.minute)
                elif m.minute > end or done or (end == 45 and m.status in ("HT", "2H")):
                    r["sonuc"] = "ıska"
            elif tur == "korner":
                now = self.current_count(m, "corners")
                if line is None:  # eski tür (10 dk'da 2+) kayıtları
                    if done:
                        r["sonuc"] = "bilinmiyor"
                elif now is not None and now > line:
                    r["sonuc"], r["gol_dakikasi"] = "isabet", now
                elif done:
                    r["sonuc"] = "ıska" if getattr(m, "final", None) else "bilinmiyor"
                    r["gol_dakikasi"] = "" if now is None else now
            elif tur == "kart":
                st_y, st_r = self.current_count(m, "yellow"), self.current_count(m, "red")
                now = max(len(card_events(m)), (st_y or 0) + (st_r or 0))
                if line is None:
                    if done:
                        r["sonuc"] = "bilinmiyor"
                elif now > line:
                    r["sonuc"], r["gol_dakikasi"] = "isabet", now
                elif done:
                    r["sonuc"], r["gol_dakikasi"] = "ıska", now
            elif tur == "iy":
                hits = [g["minute"] for g in m.goals if smin < g["minute"] <= 45]
                if hits:
                    r["sonuc"], r["gol_dakikasi"] = "isabet", hits[0]
                elif m.status not in ("1H",) and (m.minute > 45 or m.status in FINISHED or m.status == "HT"):
                    r["sonuc"] = "ıska"
            elif tur == "kg":
                if m.gh > 0 and m.ga > 0:
                    r["sonuc"] = "isabet"
                    later = [g["minute"] for g in m.goals if g["minute"] > smin]
                    r["gol_dakikasi"] = max(later) if later else m.minute
                elif m.status in FINISHED:
                    r["sonuc"] = "ıska"
            if r["sonuc"] in ("isabet", "ıska"):
                self.announce(r, m)
            # sanal kâr-zarar: piyasanın 10 dk olasılığından, %5 marjla adil oran
            if r["sonuc"] in ("isabet", "ıska") and r.get("p_piyasa") not in ("", None) and r.get("kar") in ("", None):
                odd = 0.95 / max(float(r["p_piyasa"]), 0.02)
                r["adil_oran"] = round(odd, 2)
                r["kar"] = round(100 * (odd - 1)) if r["sonuc"] == "isabet" else -100

    def announce(self, r: dict, m: Match):
        """Sinyalin sonucunu Telegram'da sinyal mesajını alıntılayarak bildirir."""
        if r.get("duyuruldu") or r.get("gonderildi") != "evet":
            return
        r["duyuruldu"] = "evet"
        tur = r.get("tur") or "gol"
        smin = int(r["dakika"])
        ln = fmt_line(float(r["cizgi"])) if r.get("cizgi") not in ("", None) else ""
        dk = r.get("gol_dakikasi")
        skor = f"📊 Skor: {m.gh}-{m.ga}"
        ok = r["sonuc"] == "isabet"
        if tur == "gol":
            text = (f"✅ TUTTU!\n⚽ {dk}' gol geldi, {ln} ÜST oldu\n{skor}" if ok else
                    f"❌ TUTMADI\n⚽ Maç {m.gh}-{m.ga} bitti, {ln} üst olmadı")
        elif tur == "alt":
            text = (f"✅ TUTTU!\n🧊 Maç {m.gh}-{m.ga} bitti, {ln} ALT" if ok else
                    f"❌ TUTMADI\n⚽ {dk}' gol geldi, {ln} alt bozuldu\n{skor}")
        elif tur == "korner":
            text = (f"✅ TUTTU!\n🚩 Korner sayısı {dk} oldu, {ln} ÜST\n{skor}" if ok else
                    f"❌ TUTMADI\n🚩 Maç {dk} kornerle bitti, {ln} üst olmadı")
        elif tur == "kart":
            text = (f"✅ TUTTU!\n🟨 Kart sayısı {dk} oldu, {ln} ÜST\n{skor}" if ok else
                    f"❌ TUTMADI\n🟨 Maç {dk} kartla bitti, {ln} üst olmadı")
        elif tur == "iy":
            text = (f"✅ TUTTU!\n⏱ {dk}' ilk yarı golü geldi\n{skor}" if ok else
                    f"❌ TUTMADI\n⏱ İlk yarıda gol olmadı\n📊 Devre skoru: {m.gh}-{m.ga}")
        elif tur == "kg":
            text = (f"✅ TUTTU!\n🤝 {dk}' karşılıklı gol oldu\n{skor}" if ok else
                    f"❌ TUTMADI\n🤝 Maç karşılıklı golsüz bitti\n📊 Maç sonu: {m.gh}-{m.ga}")
        else:  # baskı alarmı
            end = int(float(r["cizgi"])) if r.get("cizgi") not in ("", None) else min(smin + ALARM_WINDOW, 90)
            if ok:
                text = f"✅ TUTTU!\n🔥 {dk}' gol geldi\n{skor}"
            else:
                sh, sa = r["skor"].split("-")
                later = [g["minute"] for g in m.goals if g["minute"] > end]
                span_txt = "devre bitene kadar" if end == 45 else f"{smin}'-{end}' arası"
                text = (f"❌ TUTMADI\n🔥 {span_txt} gol olmadı\n📊 {'Devre' if end == 45 else str(end) + chr(39) + ' itibarıyla'} skor: {sh}-{sa}"
                        + (f"\nℹ️ Gol {later[0]}. dakikada, süre dolduktan sonra geldi" if later else ""))
        from main import telegram_reply, telegram_send
        if r.get("tg_mesaj"):
            telegram_reply(r["tg_mesaj"], text)
        else:  # sinyal mesajının numarası yoksa (oturum değişimi) sonucu ayrı mesaj olarak yaz
            names = {"gol": "MAÇ SONU ÜST", "alt": "MAÇ SONU ALT", "korner": "KORNER ÜST", "kart": "KART ÜST",
                     "iy": "İLK YARI GOL", "kg": "KG VAR", "baski": "BASKI ALARMI"}
            head, rest = text.split("\n", 1) if "\n" in text else (text, "")
            telegram_send(f"{head} | {r['mac']}", f"↪️ {r['dakika']}' {names.get(tur, tur)} {ln} sinyali\n{rest}".strip())

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
        path = SAMPLES_DIR / f"{self.today}-v4.csv.gz"
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
        for tur, label in (("gol", "⚽ Maç sonu Üst"), ("alt", "🧊 Maç sonu Alt"), ("korner", "🚩 Korner Üst"),
                           ("kart", "🟨 Kart Üst"), ("iy", "⏱ İlk yarı gol"), ("kg", "🤝 KG Var"),
                           ("baski", "🔥 Baskı alarmı")):
            sent = [r for r in self.rows_today if (r.get("tur") or "gol") == tur and r["gonderildi"] == "evet"]
            hit = [r for r in sent if r["sonuc"] == "isabet"]
            hist = [r for r in rows if (r.get("tur") or "gol") == tur and r.get("gonderildi") == "evet"
                    and r.get("sonuc") in ("isabet", "ıska")]
            hh = sum(r["sonuc"] == "isabet" for r in hist)
            if sent or hist:
                msg.append(f"{label}: {len(hit)}/{len(sent)} tuttu"
                           + (f"  •  genel %{100 * hh // len(hist)} ({hh}/{len(hist)})" if hist else ""))
        if not msg:
            msg.append("Bu oturumda sinyal oluşmadı.")
        msg = [LINE, *msg, LINE]
        log(f"API isteği: {self.api.used}, kalan: {self.api.remaining}, Claude: {self.claude.calls}")
        log("ÖZET:", " | ".join(msg))
        try:
            import panel
            panel.build()
            msg.append("Panel: https://leaderofhorde1.github.io/Gol-Analizi/")
        except Exception as e:
            log("Panel hatası:", e)
        try:
            import karne
            karne.build()
        except Exception as e:
            log("Karne hatası:", e)
        if not getattr(self, "stopping", False):  # güncelleme için kapanışta gruba özet gönderme
            notify("📊 OTURUM ÖZETİ", "\n".join(msg), priority=2, tags=["bar_chart"])

    # --- ara kayıt ve nazik durdurma
    def _git(self, *args, check=False):
        import subprocess
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(DATA.parent), timeout=60)

    def checkpoint(self):
        """Açık sinyalleri (Telegram mesaj numaralarıyla) ve verileri depoya yazar."""
        self.save()
        try:
            self._git("config", "user.name", "gol-sinyali-bot")
            self._git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
            self._git("add", "-A", "data")
            if self._git("diff", "--cached", "--quiet").returncode != 0:
                self._git("commit", "-q", "-m", f"Ara kayıt: {dt.datetime.now(TZ):%Y-%m-%d %H:%M}")
                for _ in range(3):
                    self._git("pull", "-q", "--rebase", "-X", "theirs")
                    if self._git("push", "-q").returncode == 0:
                        break
                    self._git("rebase", "--abort")
            log("Ara kayıt depoya yazıldı")
        except Exception as e:
            log("Ara kayıt hatası:", e)

    def stop_requested(self) -> bool:
        """Depoda bu oturumdan sonra yazılmış bir data/kontrol/dur dosyası varsa oturum nazikçe biter."""
        try:
            self._git("fetch", "-q", "origin", "main")
            r = self._git("show", "origin/main:data/kontrol/dur")
            if r.returncode == 0:
                ts = dt.datetime.fromisoformat(r.stdout.strip())
                return ts > self.now0
        except Exception:
            pass
        return False

    # --- ana döngü
    def run(self):
        end = self.end_time()
        log(f"Pro motor başladı, bitiş {end:%H:%M}")
        try:
            self.prematch_phase()
        except Exception as e:
            log("Maç öncesi aşama hatası:", e)
        try:
            self.resolve_carried()
        except Exception as e:
            log("Devralınan sinyaller işlenemedi:", e)
        notify("✅ Takip başladı", f"Pro mod ({', '.join(sorted(SIGNALS_ON))}), bitiş {end:%H:%M}.",
               priority=2, tags=["white_check_mark"])
        last_save = last_odds = last_stop = time.time()
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
                if time.time() - last_save > CHECKPOINT_EVERY:
                    self.checkpoint()
                    last_save = time.time()
                if time.time() - last_stop > STOP_CHECK_EVERY:
                    last_stop = time.time()
                    if self.stop_requested():
                        log("Durdurma isteği alındı, oturum kayıtla kapanıyor.")
                        self.stopping = True
                        notify("🔄 Güncelleme", "Oturum yeni sürüm için kapanıyor.", priority=1, telegram=False)
                        break
                idle = idle + 1 if n_live == 0 else 0
                wait = TARGET_INTERVAL if n_live else min(600, 120 * idle)
                pmb = self.per_minute_budget()
                if pmb > 0 and self.tick_cost / pmb * 60 > wait:
                    wait = min(300, self.tick_cost / pmb * 60)
                deadline = t0 + max(5, wait)
                while True:  # bekleme süresinde sıcak maçlar sık taranır
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    time.sleep(min(HOT_INTERVAL, left))
                    if n_live and deadline - time.time() > 2:
                        self.hot_tick()
            # Süre dolduysa ve gün bitmediyse bir sonraki oturumu iş akışı başlatsın
            now = dt.datetime.now(TZ)
            if now >= end and not (2 * 60 + 30 <= now.hour * 60 + now.minute < 9 * 60 + 40):
                open(CHAIN_FLAG, "w").write(now.isoformat())
        finally:
            self.finish()
