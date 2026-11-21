"""Oran düşüşü uyarılarının sonuç takibi: data/dususler.csv (karnede ayrı bölüm).

Her uyarı, uyarı anındaki oranla kaydedilir; maç bitince "o sonuç gerçekleşti mi?" yazılır. Karşılaştırma
ölçütü piyasanın uyarı anındaki adil ihtimali: düşen oranlar bunun üstünde tutuyorsa düşüşler gerçek bilgi
taşıyor demektir.
"""
from __future__ import annotations

import csv
import datetime as dt
import json

from main import DATA, FINISHED, TZ, log

CSV = DATA / "dususler.csv"
FIELDS = ["tarih", "fixture_id", "saat", "lig", "mac", "secim", "etiket", "ilk_oran", "oran", "dusus",
          "p_ilk", "p_piyasa", "sonuc", "skor", "kar"]
KEYS = {"ou": ("ust", "alt"), "1x2": ("ev", "x", "dep"), "kg": ("var", "yok")}
VOID = {"PST", "CANC", "ABD", "AWD", "WO"}


def load() -> list[dict]:
    try:
        with open(CSV, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def save(rows):
    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _fair(market: dict, k: str, keys) -> float | None:
    try:
        inv = {j: 1 / market[j] for j in keys if market.get(j)}
        return inv[k] / sum(inv.values()) if len(inv) == len(keys) else None
    except Exception:
        return None


def make_row(day, fid, key, rec, fx) -> dict | None:
    from prematch import LABELS
    mk, k = key.split(":")
    a, b = (rec.get("ilk") or {}).get(mk), (rec.get("son") or {}).get(mk)
    if not a or not b or not a.get(k) or not b.get(k):
        return None
    ts = rec.get("ts") or fx.get("ts")
    p0, p1 = _fair(a, k, KEYS[mk]), _fair(b, k, KEYS[mk])
    return {"tarih": day, "fixture_id": str(fid),
            "saat": dt.datetime.fromtimestamp(ts, TZ).strftime("%H:%M") if ts else "",
            "lig": fx.get("lig", ""), "mac": f"{fx.get('ev', '?')} - {fx.get('dep', '?')}", "secim": key,
            "etiket": LABELS.get(key, key), "ilk_oran": a[k], "oran": b[k],
            "dusus": round((a[k] - b[k]) / a[k], 3), "p_ilk": round(p0, 3) if p0 else "",
            "p_piyasa": round(p1, 3) if p1 else "", "sonuc": "bekliyor", "skor": "", "kar": ""}


def record(book, fid, key):
    rows = load()
    if any(r["fixture_id"] == str(fid) and r["secim"] == key for r in rows):
        return
    row = make_row(book.today, fid, key, book.data.get(str(fid)) or {}, book.fixtures.get(str(fid)) or {})
    if row:
        rows.append(row)
        save(rows)


def backfill():
    """Takip başlamadan önce gönderilmiş uyarıları (uyarilar.json + oran dosyası) kayda geçirir."""
    from prematch import ODDS_DIR
    rows = load()
    have = {(r["fixture_id"], r["secim"]) for r in rows}
    added = 0
    for p in sorted(ODDS_DIR.glob("*-uyarilar.json")):
        day = p.name[:10]
        try:
            alerts = json.loads(p.read_text())
            data = json.loads((ODDS_DIR / f"{day}-v2.json").read_text())
        except Exception:
            continue
        try:
            fixtures = {str(f["fixture_id"]): f for f in json.loads((ODDS_DIR / f"{day}-maclar.json").read_text())}
        except Exception:
            fixtures = {}
        for a in alerts:
            fid, mk, k = a.split(":")
            if (fid, f"{mk}:{k}") in have or fid not in data:
                continue
            row = make_row(day, fid, f"{mk}:{k}", data[fid], fixtures.get(fid, {}))
            if row:
                rows.append(row)
                have.add((fid, row["secim"]))
                added += 1
    if added:
        save(rows)
        log(f"Oran düşüşü kaydı: {added} eski uyarı eklendi")


def _won(key, gh, ga) -> bool:
    return {"ou:ust": gh + ga >= 3, "ou:alt": gh + ga <= 2, "1x2:ev": gh > ga, "1x2:x": gh == ga,
            "1x2:dep": gh < ga, "kg:var": gh > 0 and ga > 0, "kg:yok": gh == 0 or ga == 0}[key]


def resolve(api):
    rows = load()
    todo = sorted({r["fixture_id"] for r in rows if r["sonuc"] == "bekliyor"})
    if not todo:
        return
    res = {}
    for i in range(0, len(todo), 20):
        try:
            for it in api.get("fixtures", ids="-".join(todo[i:i + 20])):
                res[str(it["fixture"]["id"])] = it
        except Exception as e:
            log("Oran düşüşü sonucu alınamadı:", e)
    changed = False
    for r in rows:
        it = res.get(r["fixture_id"])
        if r["sonuc"] != "bekliyor" or not it:
            continue
        teams = it.get("teams") or {}
        if r["mac"].startswith("?"):
            r["mac"] = f"{teams.get('home', {}).get('name', '?')} - {teams.get('away', {}).get('name', '?')}"
            r["lig"] = r["lig"] or f"{it.get('league', {}).get('country', '')} - {it.get('league', {}).get('name', '')}"
        st = it["fixture"]["status"]["short"]
        if st in VOID:
            r["sonuc"], r["kar"] = "iptal", 0
            changed = True
            continue
        if st not in FINISHED:
            continue
        ft = (it.get("score") or {}).get("fulltime") or it.get("goals") or {}
        gh, ga = ft.get("home"), ft.get("away")
        if gh is None or ga is None:
            continue
        ok = _won(r["secim"], gh, ga)
        r["sonuc"], r["skor"] = ("isabet" if ok else "ıska"), f"{gh}-{ga}"
        r["kar"] = round(100 * (float(r["oran"]) - 1)) if ok else -100
        changed = True
    if changed:
        save(rows)


def summary(since: str = "") -> list[tuple]:
    """(grup, n, isabet, beklenen, kâr) satırları."""
    rows = [r for r in load() if r["sonuc"] in ("isabet", "ıska") and r["tarih"] > since]
    groups = {"Hepsi": rows}
    for r in rows:
        groups.setdefault(r["etiket"], []).append(r)
    out = []
    for g, rs in groups.items():
        exp = sum(float(r["p_piyasa"] or 0) for r in rs)
        out.append((g, len(rs), sum(r["sonuc"] == "isabet" for r in rs), exp, sum(float(r["kar"] or 0) for r in rs)))
    return out
