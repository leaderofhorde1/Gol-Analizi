#!/usr/bin/env python3
"""Gol Sinyali motoru.

Canlı maçların istatistiklerini API-Football'dan çeker, son dakikalardaki
baskıyı puanlar, eşik aşılınca Claude'a onaylatır ve ntfy ile telefona
bildirim gönderir. Her sinyalin sonucunu (gol geldi mi) kaydeder.

Çalışma modları (MODE ortam değişkeni):
  run  -> takip penceresi boyunca canlı takip (varsayılan)
  test -> bağlantıları dener (ntfy, API-Football, Claude) ve çıkar
  sim  -> internetsiz simülasyon (geliştirme testi)
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- ayarlar
TZ = ZoneInfo("Europe/Istanbul")
API = "https://v3.football.api-sports.io"
API_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
MODE = os.environ.get("MODE", "run").strip() or "run"

WINDOW_END = os.environ.get("WINDOW_END", "23:45")  # TR saati
REQUEST_RESERVE = 2           # günlük kotadan dokunulmayan istek
MIN_INTERVAL = 60             # iki sorgu arası en az (sn)
MAX_INTERVAL = 420            # iki sorgu arası en çok (sn)
MAX_TRACK = int(os.environ.get("MAX_TRACK", "3"))  # günde takip edilecek en fazla maç
REQ_PER_MATCH = 30           # bir maçın tahmini istek maliyeti (ücretsiz plan)
MATCH_LEN = 115              # devre arası dahil maç süresi (dk)

THRESHOLD = float(os.environ.get("THRESHOLD", "13"))      # tek takım baskı eşiği
COMBINED_THRESHOLD = THRESHOLD * 1.55                      # iki takım toplamı
LOOKBACK_MIN = 10             # baskı penceresi (maç dakikası)
MIN_SPAN = 6                  # en az bu kadar dakikalık veri olmalı
COOLDOWN_MIN = 12             # aynı maçta iki sinyal arası
EVAL_WINDOW = 10              # sinyalden sonra kaç dakika içinde gol = isabet
MAX_CLAUDE_CALLS = int(os.environ.get("MAX_CLAUDE_CALLS", "40"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
USE_CLAUDE = os.environ.get("USE_CLAUDE", "1") == "1"

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SIGNALS_CSV = DATA / "sinyaller.csv"

LIVE = {"1H", "2H", "ET", "LIVE", "BT", "P", "INT", "HT", "SUSP"}
FINISHED = {"FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO"}

# Lig öncelikleri (API-Football lig id -> puan). Listede olmayanlar 10 puan.
PRIORITY = {
    203: 100,  # Süper Lig
    2: 96,     # Şampiyonlar Ligi
    39: 92,    # Premier Lig
    140: 90,   # La Liga
    135: 88,   # Serie A
    78: 88,    # Bundesliga
    61: 85,    # Ligue 1
    3: 84,     # Avrupa Ligi
    848: 80,   # Konferans Ligi
    206: 78,   # Türkiye Kupası
    204: 76,   # TFF 1. Lig
    88: 72,    # Eredivisie
    94: 72,    # Primeira Liga
    40: 70,    # Championship
    144: 66,   # Belçika
    179: 64,   # İskoçya
    197: 62,   # Yunanistan
    218: 60,   # Avusturya
    207: 60,   # İsviçre
    1: 99,     # Dünya Kupası
    4: 98,     # Avrupa Şampiyonası
    5: 90,     # Uluslar Ligi
    32: 85,    # DK Elemeleri Avrupa
}

CSV_FIELDS = [
    "tarih", "fixture_id", "lig", "mac", "dakika", "skor", "taraf", "baski",
    "baski_rakip", "guven", "ai_onay", "ai_yorum", "gonderildi", "sonuc",
    "gol_dakikasi", "p_model", "p_piyasa", "tur", "q_once", "q_sonra", "adil_oran", "kar",
    "tg_mesaj", "duyuruldu", "cizgi", "form", "piyasa_yon", "oran_akisi", "baglam",
]


def log(*a):
    print(dt.datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- API
class Api:
    def __init__(self, key: str):
        self.s = requests.Session()
        self.s.headers["x-apisports-key"] = key
        self.remaining: int | None = None
        self.used = 0

    def get(self, path: str, **params):
        r = self.s.get(f"{API}/{path}", params=params, timeout=30)
        self.used += 1
        rem = r.headers.get("x-ratelimit-requests-remaining")
        if rem is not None and rem.lstrip("-").isdigit():
            self.remaining = int(rem)
        r.raise_for_status()
        body = r.json()
        errs = body.get("errors")
        if errs:
            raise RuntimeError(f"API hatası: {errs}")
        return body.get("response", [])

    def status(self):
        r = self.s.get(f"{API}/status", timeout=30)  # kotadan düşmez
        r.raise_for_status()
        body = r.json()
        resp = body.get("response") or {}
        if isinstance(resp, list):
            resp = resp[0] if resp else {}
        if body.get("errors"):
            raise RuntimeError(f"API hatası: {body['errors']}")
        return resp


# ---------------------------------------------------------------- ntfy
def notify(title: str, message: str, priority: int = 4, tags=None, click=None, telegram: bool = True,
           on_tg=None):
    """Bildirimi ntfy'ye ve (istenirse) Telegram grubuna gönderir; Telegram mesaj numarasını döndürür.
    Telegram'a o an ulaşılamazsa mesaj kuyruğa alınır; gönderilince on_tg(mesaj_no) çağrılır."""
    tg_id = telegram_send(title, message, on_sent=on_tg) if telegram else None
    if not NTFY_TOPIC:
        log("[ntfy kapalı]", title, "|", message)
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    # Bildirime dokununca Gol Sinyali uygulaması açılsın
    payload["click"] = click or os.environ.get("APP_LINK", "golsinyali://canli")
    try:
        requests.post("https://ntfy.sh/", json=payload, timeout=15).raise_for_status()
    except Exception as e:  # bildirim hatası takibi durdurmasın
        log("ntfy hatası:", e)
    return tg_id


# ---------------------------------------------------------------- Telegram
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_FILE = ROOT / "data" / "telegram.json"
TG_SKIP = ("✅ Takip başladı", "🔔 Test bildirimi", "✅ Test başarılı", "⚠️ Testte sorun var")
_tg_chat = None


OUTBOX: list = []          # ulaşılamayan Telegram mesajları (sırayla tekrar denenir)


def _tg(method: str, **params):
    last = None
    for attempt in range(2):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=12)
            return r.json()
        except requests.RequestException as e:
            last = e
            time.sleep(2)
    raise last


def flush_outbox():
    """Kuyruktaki Telegram mesajlarını sırayla göndermeyi dener (ilk hatada durur)."""
    while OUTBOX:
        kind, a, b, cb = OUTBOX[0]
        try:
            if kind == "send":
                mid = _send_html(a, b)
            else:
                mid = _reply_html(a, b)
        except requests.RequestException:
            if len(OUTBOX) >= 15:
                _tg_alarm(f"Telegram'a ulaşılamıyor, {len(OUTBOX)} mesaj bekliyor (bağlantı gelince gönderilecek)")
            return
        OUTBOX.pop(0)
        if cb and mid:
            try:
                cb(mid)
            except Exception:
                pass


def telegram_chat_id():
    """Grup kimliği: TELEGRAM_CHAT_ID > data/telegram.json > botun eklendiği son grup."""
    global _tg_chat
    if _tg_chat:
        return _tg_chat
    if os.environ.get("TELEGRAM_CHAT_ID"):
        _tg_chat = os.environ["TELEGRAM_CHAT_ID"].strip()
        return _tg_chat
    try:
        _tg_chat = json.loads(TG_FILE.read_text()).get("chat_id")
    except Exception:
        _tg_chat = None
    try:  # bot yeni bir gruba eklendiyse onu kullan
        found, migrated = None, set()
        for u in _tg("getUpdates", allowed_updates=["message", "my_chat_member", "channel_post"]).get("result", []):
            obj = u.get("my_chat_member") or u.get("message") or u.get("channel_post") or {}
            chat = obj.get("chat") or {}
            if obj.get("migrate_to_chat_id"):  # grup süper gruba dönüştü
                migrated.add(str(chat.get("id")))
                found = {"id": obj["migrate_to_chat_id"], "title": chat.get("title"), "type": "supergroup"}
                continue
            if chat.get("type") in ("group", "supergroup", "channel") and str(chat.get("id")) not in migrated:
                found = chat
        # kayıtlı süper grup varken eski (normal) grup kimliğine geri dönülmez
        if found and found.get("type") == "group" and str(_tg_chat or "").startswith("-100"):
            found = None
        if found and str(found["id"]) != str(_tg_chat):
            _tg_chat = str(found["id"])
            TG_FILE.parent.mkdir(exist_ok=True)
            TG_FILE.write_text(json.dumps({"chat_id": _tg_chat, "baslik": found.get("title")}, ensure_ascii=False))
            log("Telegram grubu bulundu:", found.get("title"))
    except Exception as e:
        log("Telegram grup arama hatası:", e)
    return _tg_chat


def _esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_send(chat, **params):
    """sendMessage; grup süper gruba dönüştüyse yeni kimliği kaydedip tekrar dener."""
    global _tg_chat
    res = _tg("sendMessage", chat_id=chat, **params)
    new_id = (res.get("parameters") or {}).get("migrate_to_chat_id")
    if new_id:
        _tg_chat = str(new_id)
        TG_FILE.parent.mkdir(exist_ok=True)
        TG_FILE.write_text(json.dumps({"chat_id": _tg_chat}, ensure_ascii=False))
        log("Telegram grubu süper gruba geçti, yeni kimlik:", _tg_chat)
        res = _tg("sendMessage", chat_id=_tg_chat, **params)
    return res


def _send_html(title, message):
    """HTML biçimli mesajı gönderir; mesaj numarasını döndürür. Ağ hatasında istisna fırlatır."""
    global _tg_chat
    chat = telegram_chat_id()
    if not chat:
        log("Telegram: grup bulunamadı (botu gruba ekleyip grupta bir mesaj yazın).")
        return None
    body = "\n".join(f"<b>{_esc(l)}</b>" if l.startswith(("🎯", "✅", "❌", "⚠️")) else
                     f"<i>{_esc(l)}</i>" if l.startswith(("🤖", "ℹ️")) else _esc(l) for l in message.split("\n"))
    text = f"<b>{_esc(title)}</b>\n{body}"[:4000]
    res = _tg_send(chat, text=text, parse_mode="HTML", disable_web_page_preview=True)
    if not res.get("ok"):
        log("Telegram hatası:", res.get("description"))
        _tg_alarm(res.get("description"))
        return None
    return (res.get("result") or {}).get("message_id")


def _reply_html(message_id, text):
    chat = telegram_chat_id()
    if not chat:
        return None
    lines = text.split("\n")
    html = "\n".join([f"<b>{_esc(lines[0])}</b>", *map(_esc, lines[1:])])
    res = _tg_send(chat, text=html[:4000], parse_mode="HTML", disable_web_page_preview=True,
                   reply_parameters={"message_id": int(message_id), "allow_sending_without_reply": True})
    if not res.get("ok"):
        log("Telegram cevap hatası:", res.get("description"))
        _tg_alarm(res.get("description"))
        return None
    return (res.get("result") or {}).get("message_id")


def telegram_send(title: str, message: str, on_sent=None):
    if not TG_TOKEN or title.startswith(TG_SKIP):
        return None
    if OUTBOX:  # sıra bozulmasın: önce kuyruk
        flush_outbox()
        if OUTBOX:
            OUTBOX.append(("send", title, message, on_sent))
            return None
    try:
        return _send_html(title, message)
    except requests.RequestException as e:
        log("Telegram'a ulaşılamadı, kuyruğa alındı:", e)
        OUTBOX.append(("send", title, message, on_sent))
        return None


_tg_alarm_sent = False


def _tg_hint(desc) -> str:
    d = (desc or "").lower()
    if "unauthorized" in d or "not found" in d and "chat" not in d:
        return "Bot token'ı geçersiz: BotFather'dan token'ı alıp GitHub'daki TELEGRAM_TOKEN'ı güncelleyin."
    if "kicked" in d or "not a member" in d or "chat not found" in d:
        return "Bot gruptan çıkarılmış olabilir: botu gruba tekrar ekleyip grupta bir mesaj yazın."
    if "not enough rights" in d or "forbidden" in d:
        return "Botun grupta mesaj yazma izni yok: grup ayarlarından izin verin."
    return "Sistem bir sonraki mesajda otomatik olarak tekrar deneyecek."


def _tg_alarm(desc):
    """Telegram'a gönderim başarısızsa bir kez ntfy ile uyar."""
    global _tg_alarm_sent
    if _tg_alarm_sent or not NTFY_TOPIC:
        return
    _tg_alarm_sent = True
    try:
        requests.post("https://ntfy.sh/", json={
            "topic": NTFY_TOPIC, "title": "⚠️ Telegram'a mesaj gönderilemiyor", "priority": 4,
            "message": f"Hata: {desc}\n" + _tg_hint(desc)},
            timeout=15)
    except Exception:
        pass


def telegram_reply(message_id, text: str):
    """Önceki bir mesajı alıntılayarak cevap yazar (sinyal sonucu için)."""
    if not TG_TOKEN or not message_id:
        return
    if OUTBOX:
        flush_outbox()
        if OUTBOX:
            OUTBOX.append(("reply", message_id, text, None))
            return
    try:
        _reply_html(message_id, text)
    except requests.RequestException as e:
        log("Telegram'a ulaşılamadı, cevap kuyruğa alındı:", e)
        OUTBOX.append(("reply", message_id, text, None))


# ---------------------------------------------------------------- Claude
class Claude:
    def __init__(self):
        self.calls = 0

    def available(self) -> bool:
        return USE_CLAUDE and self.calls < MAX_CLAUDE_CALLS and bool(
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    def ask(self, prompt: str, timeout: int = 150) -> str:
        self.calls += 1
        out = subprocess.run(
            ["claude", "-p", prompt, "--model", CLAUDE_MODEL,
             "--output-format", "text", "--tools", ""],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout).strip()[:300])
        return out.stdout.strip()

    def judge(self, ctx: dict) -> dict | None:
        prompt = (
            "Sen canlı futbol analistisin. Aşağıda bir maçın anlık durumu ve son "
            "dakikalardaki istatistik artışları var. Bir istatistik modeli bu maçta "
            "yakın zamanda gol olabileceğine dair sinyal üretti. Görevin: bu sinyalin "
            f"önümüzdeki {EVAL_WINDOW} dakika için anlamlı olup olmadığını değerlendirmek. "
            "Baskının gerçekçi olup olmadığına (isabetli şut, ceza sahası içi şut, xG), "
            "skor durumuna, dakikaya ve kırmızı kartlara bak. Şüpheci ol; zayıf "
            "sinyalleri ele.\n\n"
            f"VERİ:\n{json.dumps(ctx, ensure_ascii=False, indent=1)}\n\n"
            "SADECE şu JSON ile cevap ver, başka hiçbir şey yazma:\n"
            '{"onay": true/false, "guven": "düşük"|"orta"|"yüksek", '
            '"taraf": "ev"|"deplasman"|"iki taraf", "yorum": "en fazla 20 kelime Türkçe"}'
        )
        txt = self.ask(prompt)
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            raise RuntimeError(f"Claude JSON dönmedi: {txt[:200]}")
        return json.loads(m.group(0))


# ---------------------------------------------------------------- veri işleme
def _num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip().rstrip("%")
    try:
        return float(v)
    except ValueError:
        return 0.0


STAT_KEYS = {
    "Shots on Goal": "sot",
    "Total Shots": "shots",
    "Shots insidebox": "inbox",
    "Corner Kicks": "corners",
    "expected_goals": "xg",
    "Ball Possession": "poss",
    "Red Cards": "red",
    "Dangerous Attacks": "danger",
    "Blocked Shots": "blocked",
    "Fouls": "fouls",
    "Yellow Cards": "yellow",
}


def parse_stats(item: dict) -> dict:
    """{'home': {...}, 'away': {...}, 'has_xg': bool} döndürür."""
    home_id = item["teams"]["home"]["id"]
    out = {"home": {k: 0.0 for k in STAT_KEYS.values()},
           "away": {k: 0.0 for k in STAT_KEYS.values()}, "has_xg": False}
    for block in item.get("statistics") or []:
        side = "home" if block["team"]["id"] == home_id else "away"
        for st in block.get("statistics") or []:
            key = STAT_KEYS.get(st.get("type"))
            if key:
                out[side][key] = _num(st.get("value"))
                if key == "xg" and st.get("value") is not None:
                    out["has_xg"] = True
    return out


def goal_events(item: dict) -> list[dict]:
    """Geçerli goller. VAR ile iptal edilen goller (aynı takım, ±3 dk) çıkarılır."""
    res = []
    home_id = item["teams"]["home"]["id"]
    events = item.get("events") or []
    cancelled = []
    for ev in events:
        det = (ev.get("detail") or "").lower()
        if ev.get("type") == "Var" and ("goal" in det and ("cancel" in det or "disallow" in det)):
            cancelled.append(((ev.get("time") or {}).get("elapsed") or 0, (ev.get("team") or {}).get("id")))
    for ev in events:
        if ev.get("type") == "Goal" and "Missed" not in (ev.get("detail") or ""):
            minute = (ev.get("time") or {}).get("elapsed") or 0
            tid = (ev.get("team") or {}).get("id")
            hit = next((c for c in cancelled if abs(c[0] - minute) <= 3), None)
            if hit:
                cancelled.remove(hit)
                continue
            side = "home" if tid == home_id else "away"
            res.append({"minute": minute, "side": side,
                        "own": "own" in (ev.get("detail") or "").lower()})
    return res


class Match:
    def __init__(self, fx: dict):
        self.id = fx["fixture"]["id"]
        self.league_id = fx["league"]["id"]
        self.league = f'{fx["league"]["country"]} - {fx["league"]["name"]}'
        self.home = fx["teams"]["home"]["name"]
        self.away = fx["teams"]["away"]["name"]
        self.home_id = fx["teams"]["home"].get("id")
        self.away_id = fx["teams"]["away"].get("id")
        self.season = fx["league"].get("season")
        self.round = fx["league"].get("round") or ""
        self.lineups: list = []
        self.kickoff = dt.datetime.fromtimestamp(fx["fixture"]["timestamp"], TZ)
        self.status = fx["fixture"]["status"]["short"]
        self.priority = PRIORITY.get(self.league_id, 10)
        self.snaps: list[dict] = []        # {'min','stats','gh','ga'}
        self.last_signal_min = -99
        self.goals: list[dict] = []
        self.minute = 0
        self.gh = self.ga = 0

    @property
    def name(self):
        return f"{self.home} - {self.away}"

    def update(self, item: dict):
        st = item["fixture"]["status"]
        self.referee = item["fixture"].get("referee") or getattr(self, "referee", None)
        self.events = item.get("events") or getattr(self, "events", [])
        self.lineups = item.get("lineups") or getattr(self, "lineups", [])
        self.status = st["short"]
        self.minute = st.get("elapsed") or self.minute
        self.gh = item["goals"]["home"] or 0
        self.ga = item["goals"]["away"] or 0
        self.goals = goal_events(item)
        stats = parse_stats(item)
        if self.status in LIVE and self.status != "HT":
            self.snaps.append({"min": self.minute, "stats": stats,
                               "gh": self.gh, "ga": self.ga})
            self.snaps = self.snaps[-240:]  # sık taramada da en az ~20 dk geçmiş kalsın
        return stats


def team_pressure(now: dict, then: dict, has_xg: bool, span: float) -> tuple[float, dict]:
    d = {k: max(0.0, now[k] - then[k]) for k in ("sot", "shots", "inbox", "corners", "xg", "danger")}
    p = (1.0 * d["shots"] + 1.5 * d["inbox"] + 3.0 * d["sot"]
         + 1.2 * d["corners"] + 8.0 * d["xg"] + 0.15 * d["danger"])
    if not has_xg:
        p *= 1.15
    p *= 10.0 / max(span, MIN_SPAN)
    return p, d


def evaluate_pressure(m: Match, lookback: int = None, min_span: int = None) -> dict | None:
    lookback = lookback or LOOKBACK_MIN
    min_span = min_span or MIN_SPAN
    if len(m.snaps) < 2 or m.status not in LIVE or m.status == "HT":
        return None
    cur = m.snaps[-1]
    base = None
    for s in reversed(m.snaps[:-1]):
        if cur["min"] - s["min"] >= lookback:
            base = s
            break
    if base is None:
        base = m.snaps[0]
    # Pencerede gol olduysa golden önceki baskı "harcanmış" sayılır: ölçüm golden sonraki
    # ilk anlık görüntüden başlar. Golden sonra en az MIN_SPAN dakikalık veri varsa sinyal verilebilir.
    goal_in_window = False
    if (cur["gh"], cur["ga"]) != (base["gh"], base["ga"]):
        after = [s for s in m.snaps if (s["gh"], s["ga"]) == (cur["gh"], cur["ga"])]
        if after and cur["min"] - after[0]["min"] >= min_span:
            base = after[0]
        else:
            goal_in_window = True
    span = cur["min"] - base["min"]
    if span < min_span and not goal_in_window:
        return None
    span = max(span, 1)
    has_xg = cur["stats"]["has_xg"]
    ph, dh = team_pressure(cur["stats"]["home"], base["stats"]["home"], has_xg, span)
    pa, da = team_pressure(cur["stats"]["away"], base["stats"]["away"], has_xg, span)

    def ctx_mult(side):
        own, opp = (m.gh, m.ga) if side == "home" else (m.ga, m.gh)
        mult = 1.0
        if own < opp:
            mult *= 1.15 if opp - own == 1 else 1.05
        elif own > opp:
            mult *= 0.9
        if m.minute >= 75:
            mult *= 1.1
        return mult

    ph *= ctx_mult("home")
    pa *= ctx_mult("away")
    return {"home": round(ph, 1), "away": round(pa, 1), "span": span, "goal_in_window": goal_in_window,
            "d_home": dh, "d_away": da, "cur": cur, "base": base}


def signal_side(pr: dict) -> str | None:
    if pr.get("goal_in_window"):
        return None
    if pr["home"] >= THRESHOLD and pr["home"] >= pr["away"]:
        return "ev"
    if pr["away"] >= THRESHOLD:
        return "deplasman"
    if pr["home"] + pr["away"] >= COMBINED_THRESHOLD:
        return "iki taraf"
    return None


def model_confidence(pr: dict, side: str) -> str:
    val = {"ev": pr["home"], "deplasman": pr["away"]}.get(side)
    ratio = (val / THRESHOLD) if val is not None else (pr["home"] + pr["away"]) / COMBINED_THRESHOLD
    return "yüksek" if ratio >= 1.6 else "orta" if ratio >= 1.25 else "düşük"


def fmt_delta(d: dict) -> str:
    parts = [f'{int(d["shots"])} şut', f'{int(d["sot"])} isabetli',
             f'{int(d["corners"])} korner']
    if d["xg"] > 0:
        parts.append(f'xG +{d["xg"]:.2f}')
    return ", ".join(parts)


# ---------------------------------------------------------------- kayıt
def load_signals() -> list[dict]:
    if not SIGNALS_CSV.exists():
        return []
    with SIGNALS_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_signals(rows: list[dict]):
    DATA.mkdir(exist_ok=True)
    with SIGNALS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def append_snapshot(day: str, record: dict):
    folder = DATA / "anlik"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- motor
class Engine:
    def __init__(self, api: Api, claude: Claude):
        self.api = api
        self.claude = claude
        self.matches: dict[int, Match] = {}
        self.today = dt.datetime.now(TZ).date().isoformat()
        self.all_rows = load_signals()
        self.rows_today: list[dict] = []  # bu çalışmada üretilen sinyaller

    # --- maç listesi
    def load_fixtures(self, end: dt.datetime):
        now = dt.datetime.now(TZ)
        fxs = self.api.get("fixtures", date=self.today, timezone="Europe/Istanbul")
        for fx in fxs:
            m = Match(fx)
            if m.status in FINISHED:
                continue
            if m.kickoff < now - dt.timedelta(minutes=130) or m.kickoff > end - dt.timedelta(minutes=20):
                continue
            self.matches[m.id] = m
        pool = sorted(self.matches.values(), key=lambda m: (-m.priority, m.kickoff))
        budget = (self.api.remaining if self.api.remaining is not None else 99) - REQUEST_RESERVE
        n = max(1, min(MAX_TRACK, budget // REQ_PER_MATCH))
        chosen = pool[:n]
        self.matches = {m.id: m for m in chosen}
        log(f"Bugün {len(fxs)} maç var, pencerede {len(pool)}, takip edilecek {len(chosen)}:")
        for m in chosen:
            log(f"  {m.kickoff:%H:%M} {m.name} ({m.league})")

    def live_candidates(self) -> list[Match]:
        now = dt.datetime.now(TZ)
        c = [m for m in self.matches.values()
             if m.status not in FINISHED and m.kickoff <= now + dt.timedelta(minutes=1)
             and now - m.kickoff < dt.timedelta(minutes=150)]
        c.sort(key=lambda m: (-m.priority, m.kickoff))
        return c

    def remaining_match_minutes(self) -> float:
        now = dt.datetime.now(TZ)
        tot = 0.0
        for m in self.matches.values():
            if m.status in FINISHED:
                continue
            end = m.kickoff + dt.timedelta(minutes=MATCH_LEN)
            start = max(now, m.kickoff)
            tot += max(0.0, (end - start).total_seconds() / 60)
        return tot

    def next_kickoff(self) -> dt.datetime | None:
        now = dt.datetime.now(TZ)
        future = [m.kickoff for m in self.matches.values()
                  if m.status not in FINISHED and m.kickoff > now]
        return min(future) if future else None

    # --- tek tur
    def poll(self, items: list[dict] | None = None):
        cands = self.live_candidates()
        if not cands:
            return 0
        if items is None:
            items = []
            for m in cands:
                try:
                    items.extend(self.api.get("fixtures", id=m.id)[:1])
                except Exception as e:
                    log(f"{m.name} sorgu hatası:", e)
        stamp = dt.datetime.now(TZ).strftime("%H:%M")
        for it in items:
            m = self.matches.get(it["fixture"]["id"])
            if not m:
                continue
            stats = m.update(it)
            append_snapshot(self.today, {"t": stamp, "id": m.id, "dk": m.minute,
                                         "durum": m.status, "skor": [m.gh, m.ga],
                                         "ist": stats})
            self.check_outcomes(m)
            self.check_signal(m)
        return len(items)

    def check_signal(self, m: Match):
        if m.minute - m.last_signal_min < COOLDOWN_MIN or m.minute >= 88:
            return
        pr = evaluate_pressure(m)
        if not pr:
            return
        side = signal_side(pr)
        if not side:
            return
        m.last_signal_min = m.minute
        conf = model_confidence(pr, side)
        ai = None
        if self.claude.available():
            ctx = {
                "lig": m.league, "mac": m.name, "dakika": m.minute,
                "skor": f"{m.gh}-{m.ga}",
                "pencere_dk": pr["span"],
                "ev_son_pencere": pr["d_home"], "dep_son_pencere": pr["d_away"],
                "ev_toplam": pr["cur"]["stats"]["home"], "dep_toplam": pr["cur"]["stats"]["away"],
                "model_baski": {"ev": pr["home"], "deplasman": pr["away"], "esik": THRESHOLD},
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
        }
        self.rows_today.append(row)
        log(f"SİNYAL {m.name} {m.minute}' {side} baskı={row['baski']} gönder={send}")
        if send:
            d = pr["d_home"] if side == "ev" else pr["d_away"] if side == "deplasman" else {
                k: pr["d_home"][k] + pr["d_away"][k] for k in pr["d_home"]}
            team = m.home if side == "ev" else m.away if side == "deplasman" else "İki takım"
            lines = [f"{m.league}", f"{m.minute}'  Skor {m.gh}-{m.ga}",
                     f"{team}: son {int(pr['span'])} dk {fmt_delta(d)}",
                     f"Güven: {conf}"]
            if ai and ai.get("yorum"):
                lines.append(f"🤖 {ai['yorum']}")
            elif ai is None and USE_CLAUDE:
                lines.append("(AI yorumu alınamadı)")
            notify(f"⚽ Gol sinyali: {m.name}", "\n".join(lines),
                   priority=5 if conf == "yüksek" else 4, tags=["soccer"])

    def check_outcomes(self, m: Match):
        for r in self.rows_today:
            if int(r["fixture_id"]) != m.id or r["sonuc"] != "bekliyor":
                continue
            smin = int(r["dakika"])
            hits = [g for g in m.goals if smin < g["minute"] <= smin + EVAL_WINDOW]
            # Sinyal anındaki skordan sonra gelen her gol (dakika bilgisi eksikse yedek)
            sh, sa = (int(x) for x in r["skor"].split("-"))
            if hits or ((m.gh + m.ga) > (sh + sa) and m.minute <= smin + EVAL_WINDOW):
                r["sonuc"] = "isabet"
                r["gol_dakikasi"] = hits[0]["minute"] if hits else m.minute
            elif m.minute > smin + EVAL_WINDOW or m.status in FINISHED:
                r["sonuc"] = "ıska"

    # --- kapanış
    def finish(self):
        for r in self.rows_today:
            if r["sonuc"] == "bekliyor":
                m = self.matches.get(int(r["fixture_id"]))
                if m:
                    self.check_outcomes(m)
        rows = self.all_rows + self.rows_today
        save_signals(rows)
        sent = [r for r in self.rows_today if r["gonderildi"] == "evet"]
        hit = [r for r in sent if r["sonuc"] == "isabet"]
        filtered = [r for r in self.rows_today if r["gonderildi"] == "hayır"]
        f_hit = [r for r in filtered if r["sonuc"] == "isabet"]
        all_sent = [r for r in rows if r.get("gonderildi") == "evet" and r.get("sonuc") in ("isabet", "ıska")]
        all_hit = [r for r in all_sent if r["sonuc"] == "isabet"]
        msg = [f"Bugün gönderilen sinyal: {len(sent)}, isabet: {len(hit)}"
               + (f" (%{100 * len(hit) // len(sent)})" if sent else "")]
        if filtered:
            msg.append(f"AI'nin elediği: {len(filtered)}, bunlardan gol gelen: {len(f_hit)}")
        if all_sent:
            msg.append(f"Tüm zamanlar: {len(all_hit)}/{len(all_sent)} "
                       f"(%{100 * len(all_hit) // len(all_sent)})")
        msg.append(f"Kullanılan API isteği: {self.api.used}, Claude çağrısı: {self.claude.calls}")
        log("ÖZET:", " | ".join(msg))
        notify("📊 Günün özeti", "\n".join(msg), priority=2, tags=["bar_chart"])

    # --- ana döngü
    def run(self):
        now = dt.datetime.now(TZ)
        hh, mm = (int(x) for x in WINDOW_END.split(":"))
        end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        end = min(end, now + dt.timedelta(minutes=int(os.environ.get("RUN_MINUTES", "350"))))
        if end <= now:
            log("Takip penceresi bitmiş, çıkılıyor.")
            return
        self.load_fixtures(end)
        if not self.matches:
            notify("Gol Sinyali", "Bugün takip penceresinde maç yok.", priority=2)
            return
        notify("✅ Takip başladı",
               f"{len(self.matches)} maç izleniyor, bitiş {WINDOW_END}.", priority=2,
               tags=["white_check_mark"])
        last_save = time.time()
        try:
            while dt.datetime.now(TZ) < end:
                rem = self.api.remaining
                if rem is not None and rem <= REQUEST_RESERVE:
                    log("Günlük istek kotası doldu.")
                    notify("Gol Sinyali", "Günlük API kotası doldu, takip durdu.", priority=3)
                    break
                if not self.live_candidates():
                    nk = self.next_kickoff()
                    if not nk or nk >= end:
                        if not any(m.status not in FINISHED and m.snaps for m in self.matches.values()):
                            log("Kalan canlı maç yok.")
                            break
                    wait = 120 if not nk else max(30, min(900, (nk - dt.datetime.now(TZ)).total_seconds()))
                    log(f"Canlı maç yok, {int(wait)} sn bekleniyor.")
                    time.sleep(wait)
                    continue
                try:
                    n = self.poll()
                    log(f"{n} maç güncellendi, kalan istek: {self.api.remaining}")
                except Exception as e:
                    log("Sorgu hatası:", e)
                budget = max(1, (self.api.remaining or 50) - REQUEST_RESERVE)
                interval = min(MAX_INTERVAL, max(MIN_INTERVAL,
                                                 self.remaining_match_minutes() * 60 / budget))
                if time.time() - last_save > 900:
                    save_signals(self.all_rows + self.rows_today)
                    last_save = time.time()
                time.sleep(interval)
        finally:
            self.finish()


# ---------------------------------------------------------------- test modu
def run_test():
    report = []
    ok = True
    notify("🔔 Test bildirimi", "Bu bildirimi görüyorsan ntfy kurulumu tamam.", priority=3)
    report.append("ntfy: gönderildi" if NTFY_TOPIC else "ntfy: NTFY_TOPIC eksik!")
    ok &= bool(NTFY_TOPIC)

    if not API_KEY:
        report.append("API-Football: API_FOOTBALL_KEY eksik!")
        ok = False
    else:
        api = Api(API_KEY)
        try:
            st = api.status()
            sub = st.get("subscription", {})
            req = st.get("requests", {})
            report.append(f"API-Football: bağlandı, plan {sub.get('plan')}, "
                          f"bugün {req.get('current')}/{req.get('limit_day')} istek")
            live = api.get("fixtures", live="all")
            report.append(f"Şu an canlı maç: {len(live)}")
            if (sub.get("plan") or "Free").lower() != "free":
                if len(live) >= 2:
                    try:
                        its = api.get("fixtures", ids="-".join(str(f["fixture"]["id"]) for f in live[:20]))
                        n_st = sum(1 for it in its if any(parse_stats(it)["home"].values()))
                        n_xg = sum(1 for it in its if parse_stats(it)["has_xg"])
                        report.append(f"Toplu istatistik: {len(its)} maç çekildi, {n_st} maçta istatistik, {n_xg} maçta xG var")
                    except Exception as e:
                        report.append(f"Toplu istatistik HATA: {e}")
                try:
                    od = api.get("odds/live")
                    report.append(f"Canlı oran: {len(od)} maçta var")
                except Exception as e:
                    report.append(f"Canlı oran HATA: {e}")
            if live:
                live.sort(key=lambda f: -PRIORITY.get(f["league"]["id"], 10))
                it = api.get("fixtures", id=live[0]["fixture"]["id"])[0]
                stats = parse_stats(it)
                has = any(v for v in stats["home"].values())
                report.append(f"İstatistik erişimi ({it['teams']['home']['name']}): "
                              + ("var" if has else "YOK (bu maçta veri gelmedi)")
                              + (", xG var" if stats["has_xg"] else ""))
            else:
                today = dt.datetime.now(TZ).date().isoformat()
                fx = api.get("fixtures", date=today, timezone="Europe/Istanbul")
                report.append(f"Bugünün fikstürü okundu: {len(fx)} maç (sezon erişimi tamam)")
        except Exception as e:
            ok = False
            report.append(f"API-Football HATA: {e}")

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        report.append("Claude: CLAUDE_CODE_OAUTH_TOKEN eksik (sinyaller AI'sız gider)")
    else:
        try:
            ans = Claude().ask("Sadece 'hazır' kelimesini yaz.", timeout=120)
            report.append(f"Claude: cevap verdi ({ans[:30]})")
        except Exception as e:
            ok = False
            report.append(f"Claude HATA: {e}")

    if TG_TOKEN:
        try:
            me_res = _tg("getMe")
            me = me_res.get("result") or {}
            if not me_res.get("ok"):
                report.append(f"Telegram getMe hatası: {me_res.get('error_code')} {me_res.get('description')}")
            chat = telegram_chat_id()
            report.append(f"Telegram: bot @{me.get('username')} " + (f"grup bağlı ({chat})" if chat else
                          "— grup bulunamadı, botu gruba ekleyip grupta bir mesaj yazın"))
            ok &= bool(chat)
        except Exception as e:
            report.append(f"Telegram HATA: {e}")
            ok = False
    text = "\n".join(report)
    log("TEST SONUCU\n" + text)
    notify("✅ Test başarılı" if ok else "⚠️ Testte sorun var", text, priority=3)
    return 0 if ok else 1


# ---------------------------------------------------------------- simülasyon
def run_sim():
    """İnternetsiz deneme: sahte bir maç üretip motoru çalıştırır."""
    import random
    random.seed(4)
    import tempfile
    global USE_CLAUDE, DATA
    USE_CLAUDE = False
    DATA = Path(tempfile.mkdtemp())
    eng = Engine(Api(""), Claude())
    now = dt.datetime.now(TZ)
    fx = {"fixture": {"id": 1, "timestamp": int((now - dt.timedelta(minutes=50)).timestamp()),
                      "status": {"short": "2H", "elapsed": 50}},
          "league": {"id": 203, "name": "Süper Lig", "country": "Turkey"},
          "teams": {"home": {"id": 10, "name": "Ev FK"}, "away": {"id": 20, "name": "Dep SK"}},
          "goals": {"home": 0, "away": 1}}
    eng.matches[1] = Match(fx)
    tot = {"home": dict(sot=2, shots=6, inbox=3, corners=2, xg=0.5),
           "away": dict(sot=3, shots=5, inbox=3, corners=1, xg=0.7)}
    goals = {"home": 0, "away": 1}
    events = [{"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 33}, "team": {"id": 20}}]
    for minute in range(50, 91, 3):
        press = 2.5 if 60 <= minute <= 72 else 0.6
        for k, w in (("shots", 1.0), ("inbox", 0.6), ("sot", 0.45), ("corners", 0.4), ("xg", 0.09)):
            tot["home"][k] += round(random.random() * press * w * 3, 2) if k == "xg" else int(random.random() * press * w * 3 + 0.3 * (press > 1))
            tot["away"][k] += round(random.random() * 0.5 * w, 2) if k == "xg" else int(random.random() * 0.5 * w * 2)
        if minute == 77:
            goals["home"] += 1
            events.append({"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 75}, "team": {"id": 10}})
        item = dict(fx)
        item["fixture"] = {"id": 1, "timestamp": fx["fixture"]["timestamp"],
                           "status": {"short": "2H" if minute < 90 else "FT", "elapsed": minute}}
        item["goals"] = dict(goals)
        item["events"] = list(events)
        item["statistics"] = [
            {"team": {"id": 10}, "statistics": [
                {"type": "Shots on Goal", "value": tot["home"]["sot"]},
                {"type": "Total Shots", "value": tot["home"]["shots"]},
                {"type": "Shots insidebox", "value": tot["home"]["inbox"]},
                {"type": "Corner Kicks", "value": tot["home"]["corners"]},
                {"type": "expected_goals", "value": f'{tot["home"]["xg"]:.2f}'},
                {"type": "Ball Possession", "value": "58%"}]},
            {"team": {"id": 20}, "statistics": [
                {"type": "Shots on Goal", "value": tot["away"]["sot"]},
                {"type": "Total Shots", "value": tot["away"]["shots"]},
                {"type": "Shots insidebox", "value": tot["away"]["inbox"]},
                {"type": "Corner Kicks", "value": tot["away"]["corners"]},
                {"type": "expected_goals", "value": f'{tot["away"]["xg"]:.2f}'},
                {"type": "Ball Possession", "value": "42%"}]},
        ]
        eng.poll(items=[item])
        pr = evaluate_pressure(eng.matches[1])
        if pr:
            log(f"{minute}' baskı ev={pr['home']} dep={pr['away']}")
    for r in eng.rows_today:
        log("Sinyal kaydı:", r["dakika"], r["taraf"], r["baski"], r["sonuc"], r["gol_dakikasi"])
    return 0


def run_diag():
    """Canlı oran ve istatistik biçimini loglara yazar (anahtar yazılmaz)."""
    api = Api(API_KEY)
    od = api.get("odds/live")
    print("odds/live maç sayısı:", len(od))
    names = {}
    for it in od:
        for mk in it.get("odds") or []:
            names.setdefault(mk.get("name"), mk.get("values", [])[:4])
    for n, v in sorted(names.items(), key=lambda x: str(x[0])):
        if any(w in str(n).lower() for w in ("card", "corner", "booking", "both", "half", "1st", "score")):
            print("PAZAR:", repr(n), v)
    for it in od[:2]:
        print("ÜST ALANLAR:", list(it.keys()), "status:", it.get("status"))
        for mk in (it.get("odds") or [])[:40]:
            vals = mk.get("values") or []
            print(f"  pazar id={mk.get('id')} ad={mk.get('name')!r} örnek={vals[:3]}")
    live = api.get("fixtures", live="all")
    if live:
        it = api.get("fixtures", ids=str(live[0]["fixture"]["id"]))[0]
        for b in it.get("statistics") or []:
            print("istatistik türleri:", [x.get("type") for x in b.get("statistics") or []])
            break
        print("olay örneği:", (it.get("events") or [])[:2])
    return 0


def main():
    if MODE == "diag":
        return run_diag()
    if MODE == "mac":  # tek maçın son durumunu yazdır (sure alanı = maç numarası)
        it = Api(API_KEY).get("fixtures", id=os.environ.get("RUN_MINUTES"))[0]
        st = parse_stats(it)
        print("MAÇ:", it["teams"]["home"]["name"], "-", it["teams"]["away"]["name"], it["fixture"]["status"],
              "skor", it["goals"], "korner", st["home"]["corners"], st["away"]["corners"],
              "kart", st["home"]["yellow"] + st["home"]["red"], st["away"]["yellow"] + st["away"]["red"])
        for ev in it.get("events") or []:
            print("OLAY:", (ev.get("time") or {}).get("elapsed"), ev.get("type"), ev.get("detail"), (ev.get("team") or {}).get("name"),
                  "| oyuncu:", (ev.get("player") or {}).get("name"), "| asist:", (ev.get("assist") or {}).get("name"))
        import kadro
        api = Api(API_KEY)
        m = Match(it)
        m.update(it)
        print("KADRO:", [(lu.get("team") or {}).get("name") + " " + str(lu.get("formation")) for lu in m.lineups])
        for sb in kadro.substitutions(m):
            print("DEĞİŞİKLİK:", sb)
        print("NET HÜCUM:", kadro.sub_shift(m), "KIRMIZI:", kadro.red_counts(m), "ÇARPAN:", kadro.goal_mults(m))
        inj = kadro.InjuryBook(api).get(m)
        print("EKSİK:", json.dumps(inj, ensure_ascii=False)[:1500])
        print("MESAJ SATIRLARI:", kadro.info_lines(m, inj))
        import baglam
        import profil
        ctx = baglam.ContextBook(api)
        print("TUR:", m.round, "| İLK MAÇ:", ctx.first_leg(m))
        print("PUAN DURUMU:", {k: v for k, v in list(ctx.table(m.league_id, m.season).items())[:3]})
        print("YARIŞ:", ctx.race(m, "home"), "/", ctx.race(m, "away"), "| BAĞLAM:", ctx.lines(m))
        pf = profil.Profiles()
        print("PROFİL:", pf.team(m.home_id), pf.team(m.away_id), "| ÇARPAN:", pf.late_mult(m))
        return 0
    if MODE == "kurtar":
        import kurtar
        kurtar.run()
        return 0
    if MODE == "test":
        return run_test()
    if MODE == "sim":
        return run_sim()
    if not API_KEY:
        log("API_FOOTBALL_KEY eksik.")
        notify("⚠️ Gol Sinyali", "API_FOOTBALL_KEY gizli ayarı eksik, takip başlamadı.")
        return 1
    now = dt.datetime.now(TZ)
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and 2 * 60 + 40 <= now.hour * 60 + now.minute < 9 * 60 + 40:
        log("Gece arası (02:40-09:40), oturum başlatılmadı.")
        return 0
    api = Api(API_KEY)
    try:
        plan = (api.status().get("subscription") or {}).get("plan", "Free")
    except Exception as e:
        log("Plan okunamadı:", e)
        plan = "Free"
    log("API planı:", plan)
    if plan and plan.lower() != "free":
        from pro import ProEngine
        ProEngine(api, Claude()).run()
        return 0
    # Ücretsiz plan: kota küçük, sadece akşam oturumu çalışır.
    if dt.datetime.now(TZ).hour < 15 and os.environ.get("FORCE") != "1":
        log("Ücretsiz planda yalnızca akşam oturumu çalışır, çıkılıyor.")
        return 0
    Engine(api, Claude()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
