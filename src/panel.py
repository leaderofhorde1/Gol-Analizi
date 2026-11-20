"""Telefon için kontrol paneli: docs/index.html (GitHub Pages ile yayınlanır)."""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
from collections import defaultdict

from main import DATA, ROOT, TZ

OUT = ROOT / "docs" / "index.html"


def _csv(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pct(x):
    return "-" if x is None else f"%{round(x * 100)}"


def stats(rows):
    done = [r for r in rows if r.get("sonuc") in ("isabet", "ıska")]
    n = len(done)
    hit = sum(r["sonuc"] == "isabet" for r in done)
    pm = [_f(r.get("p_model")) for r in done if _f(r.get("p_model")) is not None]
    pk = [_f(r.get("p_piyasa")) for r in done if _f(r.get("p_piyasa")) is not None]
    kar = [_f(r.get("kar")) for r in done if _f(r.get("kar")) is not None]
    mv = [(_f(r.get("q_sonra")), _f(r.get("q_once"))) for r in done]
    mv = [(a, b) for a, b in mv if a and b]
    return {
        "n": n, "hit": hit, "rate": hit / n if n else None,
        "pm": sum(pm) / len(pm) if pm else None, "pk": sum(pk) / len(pk) if pk else None,
        "kar": sum(kar) if kar else None, "kar_n": len(kar),
        "roi": sum(kar) / (100 * len(kar)) if kar else None,
        "mv": sum(a > b * 1.03 for a, b in mv) / len(mv) if mv else None, "mv_n": len(mv),
        "pending": len(rows) - n,
    }


def card(title, s, note=""):
    roi = "-" if s["roi"] is None else f"{s['roi'] * 100:+.1f}%"
    kar = "-" if s["kar"] is None else f"{s['kar']:+.0f} TL"
    cls = "pos" if (s["roi"] or 0) > 0 else "neg" if (s["roi"] or 0) < 0 else ""
    return f"""
<section class="card">
  <h2>{title}</h2>
  <div class="grid">
    <div><b>{s['n']}</b><span>sonuçlanan sinyal</span></div>
    <div><b>{pct(s['rate'])}</b><span>isabet</span></div>
    <div><b>{pct(s['pm'])}</b><span>model ort. olasılık</span></div>
    <div><b>{pct(s['pk'])}</b><span>piyasa ort. olasılık</span></div>
    <div><b class="{cls}">{kar}</b><span>sanal kâr ({s['kar_n']} bahis)</span></div>
    <div><b class="{cls}">{roi}</b><span>ROI</span></div>
    <div><b>{pct(s['mv'])}</b><span>piyasa bizim yönümüze döndü ({s['mv_n']})</span></div>
    <div><b>{s['pending']}</b><span>bekleyen</span></div>
  </div>
  {f'<p class="note">{note}</p>' if note else ''}
</section>"""


def table(head, rows):
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in head)
    b = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tw"><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'


def build():
    sig = _csv(DATA / "sinyaller.csv")
    picks = _csv(DATA / "maconcesi.csv")
    try:
        ozet = json.loads((DATA / "analiz" / "ozet.json").read_text())
    except Exception:
        ozet = {}
    for r in sig:
        r["tur"] = r.get("tur") or "gol"
    sent = [r for r in sig if r.get("gonderildi", "evet") == "evet"]
    parts = []
    note = ("Model olasılığı piyasanınkinden yüksek ve isabet oranı piyasa olasılığını aşıyorsa model piyasadan iyi demektir. "
            "Sanal kâr: piyasanın 10 dakikalık olasılığından %5 marjla hesaplanan oranla 100 TL'lik bahis.")
    for tur, label in (("gol", "⚽ Gol sinyalleri"), ("korner", "🚩 2+ Korner sinyalleri"), ("kart", "🟨 Kart sinyalleri"),
                       ("iy", "⏱ İlk yarı gol sinyalleri"), ("kg", "🤝 KG Var sinyalleri")):
        rows = [r for r in sent if r["tur"] == tur]
        parts.append(card(label, stats(rows), note if tur == "gol" else ""))
    # Claude etkisi
    g = [r for r in sig if r["tur"] == "gol" and r.get("ai_onay") in ("evet", "hayır")]
    if g:
        a, b = stats([r for r in g if r["ai_onay"] == "evet"]), stats([r for r in g if r["ai_onay"] == "hayır"])
        parts.append(f"""<section class="card"><h2>🤖 Claude filtresi</h2>
<p>Onayladıkları: {a['n']} sinyal, isabet {pct(a['rate'])} · Eledikleri: {b['n']} sinyal, isabet {pct(b['rate'])}</p>
<p class="note">Elenenlerin isabeti onaylananlardan düşükse Claude işe yarıyor demektir.</p></section>""")
    # güven düzeyine göre
    rows = []
    for tur in ("gol", "korner", "kart", "iy", "kg"):
        for conf in ("yüksek", "orta", "düşük"):
            s = stats([r for r in sent if r["tur"] == tur and r.get("guven") == conf])
            if s["n"]:
                rows.append([tur, conf, s["n"], pct(s["rate"]), pct(s["pk"]),
                             "-" if s["roi"] is None else f"{s['roi'] * 100:+.0f}%"])
    if rows:
        parts.append('<section class="card"><h2>Güven düzeyine göre</h2>'
                     + table(["Tür", "Güven", "Sinyal", "İsabet", "Piyasa", "ROI"], rows) + "</section>")
    # lige göre
    by = defaultdict(list)
    for r in sent:
        by[r.get("lig", "?")].append(r)
    rows = []
    for lig, rs in by.items():
        s = stats(rs)
        if s["n"] >= 3:
            rows.append([lig, s["n"], pct(s["rate"]), pct(s["pk"]), "-" if s["roi"] is None else f"{s['roi'] * 100:+.0f}%"])
    rows.sort(key=lambda r: -r[1])
    if rows:
        parts.append('<section class="card"><h2>Lige göre (en az 3 sinyal)</h2>'
                     + table(["Lig", "Sinyal", "İsabet", "Piyasa", "ROI"], rows[:15]) + "</section>")
    # maç öncesi
    done = [r for r in picks if r.get("sonuc") in ("isabet", "ıska")]
    if picks:
        w = sum(r["sonuc"] == "isabet" for r in done)
        kar = sum(_f(r.get("kar")) or 0 for r in done)
        last = [[r["tarih"], r["mac"], r["secim"], r["oran"], pct(_f(r["model"])), pct(_f(r["piyasa"])),
                 r["sonuc"], r.get("skor", "")] for r in picks[-15:][::-1]]
        parts.append(f"""<section class="card"><h2>☀️ Maç öncesi seçimler</h2>
<p>Sonuçlanan {len(done)} · isabet {w} · sanal kâr {kar:+.0f} TL · ROI {f'{kar / (100 * len(done)) * 100:+.1f}%' if done else '-'}</p>
{table(["Tarih", "Maç", "Seçim", "Oran", "Model", "Piyasa", "Sonuç", "Skor"], last)}</section>""")
    # son sinyaller
    last = [[r["tarih"][5:], r["tur"], r["mac"], f"{r['dakika']}'", r["skor"], pct(_f(r.get("p_model"))),
             pct(_f(r.get("p_piyasa"))), r["sonuc"]] for r in sent[-30:][::-1]]
    if last:
        parts.append('<section class="card"><h2>Son 30 sinyal</h2>'
                     + table(["Gün", "Tür", "Maç", "Dk", "Skor", "Model", "Piyasa", "Sonuç"], last) + "</section>")
    # analiz
    if ozet:
        gt = ozet.get("geri_test", {})
        mk = ozet.get("piyasa") or {}
        roi = (mk.get("roi") or {}).get("w0.5_e0.05") or {}
        p = ozet.get("params", {})
        parts.append(f"""<section class="card"><h2>🔬 Son model analizi</h2>
<p>{p.get('olusturma', '')} · {p.get('mac_sayisi', 0)} geçmiş maç · {ozet.get('hakem_sayisi', 0)} hakem</p>
<p>Maç öncesi model log kaybı (1X2): {gt.get('model', {}).get('1x2', 0):.4f} · sadece lig ortalaması: {gt.get('taban', {}).get('1x2', 0):.4f}</p>
<p>Son 7 gün piyasa karşılaştırması: {mk.get('mac', '-')} maç · en iyi model ağırlığı {mk.get('en_iyi_agirlik', '-')}
 · seçim kuralı: {roi.get('bahis', '-')} bahis, kâr {roi.get('kar', '-')} TL</p></section>""")
    now = dt.datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    page = f"""<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gol Sinyali Paneli</title>
<style>
:root {{ color-scheme: light dark; --bg:#f6f7f9; --fg:#1b1f24; --mut:#667085; --card:#fff; --line:#e4e7ec; --pos:#12805c; --neg:#c4320a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --fg:#e7e9ee; --mut:#98a2b3; --card:#181b21; --line:#2a2f37; --pos:#3ccb8f; --neg:#ff7a59; }} }}
* {{ box-sizing:border-box }} body {{ margin:0; font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--fg); }}
header {{ padding:18px 16px 6px }} h1 {{ font-size:20px; margin:0 }} header p {{ margin:4px 0 0; color:var(--mut); font-size:13px }}
main {{ padding:8px 12px 40px; max-width:900px; margin:auto }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; margin:12px 0 }}
.card h2 {{ font-size:16px; margin:0 0 10px }}
.grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:10px }}
@media (min-width:640px) {{ .grid {{ grid-template-columns:repeat(4,1fr) }} }}
.grid div {{ border:1px solid var(--line); border-radius:10px; padding:8px 10px }}
.grid b {{ display:block; font-size:19px; font-variant-numeric:tabular-nums }} .grid span {{ font-size:12px; color:var(--mut) }}
.pos {{ color:var(--pos) }} .neg {{ color:var(--neg) }} .note {{ color:var(--mut); font-size:12px; margin:10px 0 0 }}
.tw {{ overflow-x:auto }} table {{ border-collapse:collapse; width:100%; font-size:13px }}
th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap }} th {{ color:var(--mut); font-weight:600 }}
</style></head><body>
<header><h1>Gol Sinyali Paneli</h1><p>Son güncelleme: {now} · her oturum sonunda yenilenir</p></header>
<main>{''.join(parts) if parts else '<p>Henüz veri yok.</p>'}</main></body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    return OUT


if __name__ == "__main__":
    print(build())
