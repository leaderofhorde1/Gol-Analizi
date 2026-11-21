"""Yedekleme: ntfy'ye gönderilmiş sinyalleri geri okuyup data/sinyaller.csv'ye ekler.

Çalışan bir oturum yarıda kesilmeden önce kullanılır. Bitmiş maçların gol ve kart
sonuçları olaylardan hesaplanır; korner sonucu dakika verisi olmadığı için
'bilinmiyor' olarak işaretlenir. Hâlâ süren maçlardaki sinyaller 'bekliyor' kalır,
yeni oturum takibi devralır.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import requests

from main import (API_KEY, FINISHED, NTFY_TOPIC, TZ, Api, goal_events, load_signals, log,
                  save_signals)

TYPES = {"⚽ Gol sinyali: ": "gol", "🚩 Korner sinyali: ": "korner", "🟨 Kart sinyali: ": "kart",
         "⚽ GOL SİNYALİ: ": "gol", "🚩 KORNER SİNYALİ: ": "korner", "🟨 KART SİNYALİ: ": "kart",
         "⏱ İLK YARI GOL SİNYALİ: ": "iy", "🤝 KG SİNYALİ: ": "kg", "🔥 BASKI ALARMI: ": "baski",
         "🧊 ALT SİNYALİ: ": "alt"}


def parse(msg: dict) -> dict | None:
    title, body = msg.get("title") or "", msg.get("message") or ""
    tur = next((t for p, t in TYPES.items() if title.startswith(p)), None)
    if not tur:
        return None
    name = next(title[len(p):] for p in TYPES if title.startswith(p))
    lines = body.split("\n")
    m = re.search(r"(\d+)'[^\n]*?Skor (\d+)-(\d+)", body)
    pm = re.search(r"model %(\d+)", body, re.I)
    pk = re.search(r"piyasa %(\d+)", body, re.I)
    gv = re.search(r"(?:Güven|Sinyal gücü[^:]*): (?:[●○]+ )?(\S+)", body)
    ai = re.search(r"🤖 (.+)", body)
    cnt = re.search(r"(?:Maçtaki (?:korner|kart)|Şu an): (\d+)", body)
    line = re.search(r"(\d+\.5) (?:ÜST|ALT)", body)
    if not m:
        return None
    return {
        "tarih": dt.datetime.fromtimestamp(msg["time"], TZ).date().isoformat(),
        "lig": next((l[2:].strip() for l in lines if l.startswith("🏆")), lines[0] if lines else ""), "mac": name, "dakika": int(m.group(1)),
        "skor": f"{m.group(2)}-{m.group(3)}", "tur": tur,
        "p_model": int(pm.group(1)) / 100 if pm else "", "p_piyasa": int(pk.group(1)) / 100 if pk else "",
        "guven": ({"güçlü": "yüksek", "zayıf": "düşük"}.get(gv.group(1).lower(), gv.group(1).lower()) if gv else ""), "ai_onay": "evet" if ai else "", "ai_yorum": ai.group(1) if ai else "",
        "baski": cnt.group(1) if cnt else "", "cizgi": float(line.group(1)) if line else "", "gonderildi": "evet", "sonuc": "bekliyor", "gol_dakikasi": "",
        "_zaman": msg["time"],
    }


def run():
    r = requests.get(f"https://ntfy.sh/{NTFY_TOPIC}/json", params={"poll": 1, "since": "12h"}, timeout=30)
    msgs = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    sigs = [s for s in (parse(m) for m in msgs if m.get("event") == "message") if s]
    log(f"ntfy'de {len(sigs)} sinyal bulundu")
    rows = load_signals()
    known = {(x.get("mac"), str(x.get("dakika")), x.get("tur") or "gol") for x in rows}
    sigs = [s for s in sigs if (s["mac"], str(s["dakika"]), s["tur"]) not in known]
    if not sigs:
        log("Eklenecek yeni sinyal yok.")
        return
    api = Api(API_KEY)
    fx_by_name = {}
    for day in sorted({s["tarih"] for s in sigs}):
        for fx in api.get("fixtures", date=day, timezone="Europe/Istanbul"):
            fx_by_name[f'{fx["teams"]["home"]["name"]} - {fx["teams"]["away"]["name"]}'] = fx["fixture"]["id"]
    for s in sigs:
        s["fixture_id"] = fx_by_name.get(s["mac"], "")
    ids = sorted({str(s["fixture_id"]) for s in sigs if s["fixture_id"]})
    details = {}
    for i in range(0, len(ids), 20):
        for it in api.get("fixtures", ids="-".join(ids[i:i + 20])):
            details[it["fixture"]["id"]] = it
    for s in sigs:
        it = details.get(s["fixture_id"])
        if not it:
            continue
        status = it["fixture"]["status"]["short"]
        minute = it["fixture"]["status"].get("elapsed") or 0
        smin = s["dakika"]
        done = status in FINISHED or minute > smin + 10
        if s.get("cizgi") not in ("", None) and s["tur"] in ("gol", "alt", "korner", "kart"):
            # maç sonu çizgili sinyaller: sonucu canlı motor (devralma ile) belirler
            continue
        if s["tur"] in ("gol", "baski"):
            hits = [g["minute"] for g in goal_events(it) if smin < g["minute"] <= smin + 10]
        elif s["tur"] == "kart":
            hits = [(e.get("time") or {}).get("elapsed") or 0 for e in it.get("events") or []
                    if e.get("type") == "Card" and smin < ((e.get("time") or {}).get("elapsed") or 0) <= smin + 10]
        elif s["tur"] == "iy":
            hits = [g["minute"] for g in goal_events(it) if smin < g["minute"] <= 45]
            done = status in FINISHED or minute > 45 or status in ("HT", "2H")
        elif s["tur"] == "kg":
            gh, ga = it["goals"]["home"] or 0, it["goals"]["away"] or 0
            hits = [minute] if gh and ga else []
            done = status in FINISHED
        else:
            hits = None
        if hits:
            s["sonuc"], s["gol_dakikasi"] = "isabet", hits[0]
        elif hits is None:
            s["sonuc"] = "bilinmiyor" if done else "bekliyor"
        elif done:
            s["sonuc"] = "ıska"
        if s["sonuc"] in ("isabet", "ıska") and s["p_piyasa"] not in ("", None):
            odd = 0.95 / max(float(s["p_piyasa"]), 0.02)
            s["adil_oran"] = round(odd, 2)
            s["kar"] = round(100 * (odd - 1)) if s["sonuc"] == "isabet" else -100
    sigs.sort(key=lambda s: s["_zaman"])
    save_signals(rows + sigs)
    for s in sigs:
        log(f"  {s['tur']:6} {s['mac']} {s['dakika']}' → {s['sonuc']} {s['gol_dakikasi']}")
    log(f"{len(sigs)} sinyal yedeklendi")


if __name__ == "__main__":
    run()
