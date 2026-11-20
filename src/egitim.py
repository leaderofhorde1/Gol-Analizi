"""Canlı modelin gece eğitimi.

Canlı oturumların 5 dakikada bir kaydettiği örneklerle (data/ornekler/*-v2.csv.gz)
gol, 2+ korner ve kart için lojistik regresyon eğitir. Piyasanın olasılığı modele
girdi olarak verilir; böylece asıl soru test edilir: "Baskı verisi piyasanın
bildiğinin ötesinde bir şey söylüyor mu?"

Zaman sıralı bölme: ilk %70 eğitim, son %30 test. Yeni model yalnızca testte
piyasadan daha iyi tahmin ediyorsa kullanılmaya başlanır (data/model/canli.json).
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import gzip
import json
import math
import os

from main import DATA, TZ, log, notify

OUT = DATA / "model" / "canli.json"
REPORT = DATA / "analiz" / "canli_egitim.md"
MIN_SAMPLES = 1500

TARGETS = {
    # tür: (hedef sütun, hedef eşiği, piyasa sütunu, model sütunu)
    "gol": ("gol_10dk", 1, "p_piyasa", "p_model"),
    "korner": ("korner_10dk", 2, "p_korner_piyasa", "p_korner"),
    "kart": ("kart_10dk", 1, "p_kart_piyasa", "p_kart"),
}


def _f(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sigmoid(z):
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def features(tur: str, s: dict) -> list[float] | None:
    """Örnek satırından özellik vektörü. Canlı motor da aynı fonksiyonu kullanır."""
    mkt = _f(s.get(TARGETS[tur][2]))
    mod = _f(s.get(TARGETS[tur][3]))
    if mkt is None or mod is None:
        return None
    be, bd = _f(s.get("baski_ev"), 0.0), _f(s.get("baski_dep"), 0.0)
    minute = _f(s.get("dakika"), 45.0)
    diff = abs(_f(s.get("skor_farki"), 0.0))
    common = [1.0, logit(mkt), logit(mod) - logit(mkt), math.log1p(be + bd), math.log1p(max(be, bd)),
              minute / 90, 1.0 if diff == 0 else 0.0, 1.0 if diff == 1 else 0.0,
              abs(_f(s.get("kirmizi_farki"), 0.0))]
    if tur == "kart":
        common.append(math.log1p(_f(s.get("faul_10"), 0.0)))
    return common


FEATURE_NAMES = ["sabit", "logit(piyasa)", "model-piyasa farkı", "log toplam baskı", "log en yüksek baskı",
                 "dakika", "skor eşit", "1 gol fark", "kırmızı kart farkı", "log faul temposu"]


def fit_logistic(X, y, l2=1.0, iters=30):
    """Newton-Raphson (IRLS) ile L2 düzenlemeli lojistik regresyon; numpy gerektirmez."""
    k = len(X[0])
    w = [0.0] * k
    w[1] = 1.0  # piyasadan başla
    for _ in range(iters):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            p = sigmoid(sum(a * b for a, b in zip(w, xi)))
            r = p - yi
            wgt = p * (1 - p)
            for a in range(k):
                g[a] += r * xi[a]
                for b in range(a, k):
                    H[a][b] += wgt * xi[a] * xi[b]
        for a in range(k):
            g[a] += l2 * (w[a] - (1.0 if a == 1 else 0.0))
            H[a][a] += l2
            for b in range(a):
                H[a][b] = H[b][a]
        step = solve(H, g)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for j in range(c, n + 1):
                    M[r][j] -= f * M[c][j]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def logloss(ps, ys):
    return sum(-(y * math.log(min(max(p, 1e-6), 1 - 1e-6)) + (1 - y) * math.log(min(max(1 - p, 1e-6), 1 - 1e-6)))
               for p, y in zip(ps, ys)) / max(len(ys), 1)


def load_samples():
    rows = []
    for path in sorted(glob.glob(str(DATA / "ornekler" / "*-v2.csv.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            text = f.read()
        # aynı dosyaya birden çok oturum ekleyince başlık satırı tekrarlanabilir
        lines = [l for i, l in enumerate(text.splitlines()) if i == 0 or not l.startswith("tarih,")]
        rows += list(csv.DictReader(lines))
    return rows


def train_one(tur, rows):
    tcol, thr, mcol, _ = TARGETS[tur]
    data = []
    for s in rows:
        y = _f(s.get(tcol))
        x = features(tur, s)
        if y is None or x is None:
            continue
        data.append((x, 1.0 if y >= thr else 0.0, _f(s[mcol]), s))
    n = len(data)
    res = {"orneklem": n, "kullan": False}
    if n < 200:
        res["not"] = "örneklem yetersiz"
        return res
    cut = int(n * 0.7)
    tr, te = data[:cut], data[cut:]
    w = fit_logistic([d[0] for d in tr], [d[1] for d in tr])
    p_new = [sigmoid(sum(a * b for a, b in zip(w, d[0]))) for d in te]
    p_mkt = [d[2] for d in te]
    p_old = [sigmoid(d[0][1] + d[0][2]) for d in te]  # eski el yapımı model
    ys = [d[1] for d in te]
    ll_m, ll_o, ll_n = logloss(p_mkt, ys), logloss(p_old, ys), logloss(p_new, ys)
    # sinyal simülasyonu: yeni model piyasadan en az 5 puan yüksekse
    sig = [(pn, pm, y) for pn, pm, y in zip(p_new, p_mkt, ys) if pn - pm >= 0.05]
    res.update({
        "test": len(te), "oran": round(sum(ys) / len(ys), 3),
        "logloss_piyasa": round(ll_m, 5), "logloss_eski_model": round(ll_o, 5), "logloss_yeni_model": round(ll_n, 5),
        "agirliklar": [round(v, 4) for v in w],
        "sinyal_sim": {"adet": len(sig), "isabet": round(sum(y for *_, y in sig) / len(sig), 3) if sig else None,
                       "piyasa_ort": round(sum(pm for _, pm, _ in sig) / len(sig), 3) if sig else None},
    })
    res["kullan"] = n >= MIN_SAMPLES and ll_n < ll_m - 0.0005
    if res["kullan"]:  # son model tüm veriyle
        res["agirliklar"] = [round(v, 4) for v in fit_logistic([d[0] for d in data], [d[1] for d in data])]
    return res


def run():
    rows = load_samples()
    log(f"Eğitim örneği: {len(rows)}")
    out = {"olusturma": dt.datetime.now(TZ).isoformat(timespec="minutes")}
    for tur in TARGETS:
        out[tur] = train_one(tur, rows)
        log(tur, json.dumps(out[tur], ensure_ascii=False))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    write_report(out, len(rows))
    msg = [f"{len(rows)} canlı örnek."]
    for tur, label in (("gol", "⚽ Gol"), ("korner", "🚩 2+ korner"), ("kart", "🟨 Kart")):
        r = out[tur]
        if "logloss_piyasa" not in r:
            msg.append(f"{label}: {r.get('not', '-')} ({r['orneklem']})")
            continue
        better = r["logloss_yeni_model"] < r["logloss_piyasa"]
        s = r["sinyal_sim"]
        msg.append(f"{label}: model piyasadan {'İYİ' if better else 'iyi değil'} "
                   f"({r['logloss_yeni_model']:.4f} / {r['logloss_piyasa']:.4f}), "
                   f"sim. sinyal {s['adet']} isabet {s['isabet']} vs piyasa {s['piyasa_ort']}"
                   + (" → KULLANIMDA" if r["kullan"] else ""))
    notify("🧠 Gece eğitimi", "\n".join(msg), priority=2, tags=["brain"], telegram=False)


def write_report(out, n):
    lines = [f"# Canlı model eğitimi – {out['olusturma']}", "", f"Toplam örnek: {n}", "",
             "Log kayıp: düşük = daha iyi. Yeni model ancak testte piyasadan iyiyse kullanılır.", ""]
    for tur in TARGETS:
        r = out[tur]
        lines += [f"## {tur}", ""]
        if "logloss_piyasa" not in r:
            lines += [f"{r.get('not', '-')} (örneklem {r['orneklem']})", ""]
            continue
        lines += ["| | Değer |", "|---|---|",
                  f"| Örneklem / test | {r['orneklem']} / {r['test']} |",
                  f"| Olay oranı | {r['oran']} |",
                  f"| Log kayıp: piyasa | {r['logloss_piyasa']} |",
                  f"| Log kayıp: eski model | {r['logloss_eski_model']} |",
                  f"| Log kayıp: yeni model | {r['logloss_yeni_model']} |",
                  f"| Simüle sinyal (yeni − piyasa ≥ 5 puan) | {r['sinyal_sim']['adet']} adet, isabet {r['sinyal_sim']['isabet']}, piyasa ort. {r['sinyal_sim']['piyasa_ort']} |",
                  f"| Kullanımda | {'evet' if r['kullan'] else 'hayır'} |", "",
                  "Ağırlıklar: " + ", ".join(f"{nm} {w}" for nm, w in zip(FEATURE_NAMES, r["agirliklar"])), ""]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
