"""Gece günlük özeti: önceki günün gruba giden sinyallerinin ve kuponlarının sonuçları (gece 01:00'den sonra bir kez)."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os

from main import DATA, TZ, log, notify

STATE = DATA / "kontrol" / "gunluk_ozet.json"
SAAT = int(os.environ.get("OZET_SAAT", "1"))
KAR_GOSTER = os.environ.get("OZET_KAR", "1") == "1"
AD = {"gol": "⚽ Üst", "alt": "🧊 Alt", "korner": "🚩 Korner", "kart": "🟨 Kart", "iy": "⏱ İlk yarı", "kg": "🤝 KG"}
AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build(day: str) -> str | None:
    try:
        with open(DATA / "sinyaller.csv", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("tarih") == day and r.get("gonderildi") == "evet"]
    except FileNotFoundError:
        rows = []
    bets = [r for r in rows if (r.get("tur") or "gol") != "baski"]
    done = [r for r in bets if r.get("sonuc") in ("isabet", "ıska")]
    pend = [r for r in bets if r.get("sonuc") == "bekliyor"]
    alarms = [r for r in rows if r.get("tur") == "baski" and r.get("sonuc") in ("isabet", "ıska")]
    try:
        coupons = [c for c in json.loads((DATA / "kupon" / "kuponlar.json").read_text())
                   if c.get("tarih") == day and c.get("bacaklar")]
    except Exception:
        coupons = []
    if not done and not coupons:
        return None
    d = dt.date.fromisoformat(day)
    L = [f"📊 GÜNLÜK ÖZET — {d.day} {AYLAR[d.month - 1]}", "━━━━━━━━━━━━━━"]
    if done:
        w = sum(r["sonuc"] == "isabet" for r in done)
        pm = [_f(r.get("p_piyasa")) for r in done]
        pm = [x for x in pm if x is not None]
        L += [f"✅ {w} tuttu  •  ❌ {len(done) - w} tutmadı  (%{round(100 * w / len(done))})"]
        if pm:
            L.append(f"📈 Piyasanın beklediği isabet: %{round(100 * sum(pm) / len(pm))}")
        if KAR_GOSTER:
            kar = sum(_f(r.get("kar")) or 0 for r in done)
            L.append(f"💰 Her sinyale 100 TL ile: {kar:+.0f} TL")
        L.append("")
        for tur, ad in AD.items():
            rs = [r for r in done if (r.get("tur") or "gol") == tur]
            if rs:
                w = sum(r["sonuc"] == "isabet" for r in rs)
                L.append(f"{ad}: {w}/{len(rs)}")
    if alarms:
        w = sum(r["sonuc"] == "isabet" for r in alarms)
        pm = [_f(r.get("p_piyasa")) for r in alarms]
        pm = [x for x in pm if x is not None]
        L += ["", f"🔥 Baskı uyarıları: {len(alarms)} uyarıdan {w} tanesinde gol geldi"
              + (f" (piyasa %{round(100 * sum(pm) / len(pm))} bekliyordu)" if pm else "")]
    if coupons:
        icon = {"isabet": "✅", "ıska": "❌", "iptal": "➖", "bekliyor": "⏳"}
        L += ["", "🧪 Deneme kuponları:"]
        for c in coupons:
            n_hit = sum(l["sonuc"] == "isabet" for l in c["bacaklar"])
            n_wait = sum(l["sonuc"] == "bekliyor" for l in c["bacaklar"])
            st = "ıska" if c.get("yatti") else c["sonuc"]
            L.append(f"{icon.get(st, '❔')} {c['baslik']} (oran {c['oran']:.2f}): "
                     f"{n_hit}/{len(c['bacaklar'])} maç tuttu" + (f", {n_wait} maç bekliyor" if n_wait else ""))
    if pend:
        L += ["", f"⏳ Sonucu bekleyen {len(pend)} sinyal var."]
    L += ["━━━━━━━━━━━━━━", "ℹ️ Tutanı da tutmayanı da gösteriyoruz. Bahis tavsiyesi değildir."]
    return "\n".join(L)


def maybe_send():
    now = dt.datetime.now(TZ)
    if not (SAAT <= now.hour < 10):
        return
    day = (now.date() - dt.timedelta(days=1)).isoformat()
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    if st.get("son") == day:
        return
    text = build(day)
    st["son"] = day
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st))
    if text:
        notify("📊 Günlük özet", text, priority=3, tags=["bar_chart"])
        log(f"Günlük özet gönderildi: {day}")
