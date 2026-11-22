# Canlı model eğitimi – 2026-11-22T09:41+03:00

Toplam örnek: 14284

Log kayıp: düşük = daha iyi. Yeni model ancak testte piyasadan iyiyse kullanılır.

## gol

| | Değer |
|---|---|
| Örneklem / test | 9042 / 2713 |
| Olay oranı | 0.231 |
| Log kayıp: piyasa | 0.53647 |
| Log kayıp: eski model | 0.55028 |
| Log kayıp: piyasa + maç durumu (B) | 0.5353 |
| Log kayıp: tam model (C) | 0.53304 |
| Brier (piyasa / B / C) | 0.17621 / 0.17584 / 0.17515 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 85 adet, isabet 0.247, piyasa ort. 0.17 |
| Kullanımda | evet |

Ağırlıklar: sabit 1.1108, logit(piyasa) 1.2307, model-piyasa farkı 0.9107, log toplam baskı -0.2779, log en yüksek baskı -0.0244, dakika -0.4632, skor eşit 0.0824, 1 gol fark -0.1862, kırmızı kart farkı -0.0139

## korner

| | Değer |
|---|---|
| Örneklem / test | 3690 / 1107 |
| Olay oranı | 0.244 |
| Log kayıp: piyasa | 0.82823 |
| Log kayıp: eski model | 0.53835 |
| Log kayıp: piyasa + maç durumu (B) | 0.54195 |
| Log kayıp: tam model (C) | 0.52316 |
| Brier (piyasa / B / C) | 0.26459 / 0.18204 / 0.17731 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 161 adet, isabet 0.255, piyasa ort. 0.267 |
| Kullanımda | hayır (sinyal anlarında piyasayı geçmiyor) |

Ağırlıklar: sabit -1.8997, logit(piyasa) 0.0139, model-piyasa farkı 0.2919, log toplam baskı -0.047, log en yüksek baskı 0.4493, dakika 0.2703, skor eşit 0.0099, 1 gol fark 0.0182, kırmızı kart farkı -0.1739

## kart

| | Değer |
|---|---|
| Örneklem / test | 966 / 290 |
| Olay oranı | 0.272 |
| Log kayıp: piyasa | 0.66509 |
| Log kayıp: eski model | 0.58389 |
| Log kayıp: piyasa + maç durumu (B) | 0.5827 |
| Log kayıp: tam model (C) | 0.58798 |
| Brier (piyasa / B / C) | 0.23653 / 0.19748 / 0.20006 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 1 adet, isabet 0.0, piyasa ort. 0.277 |
| Kullanımda | hayır |

Ağırlıklar: sabit -1.4384, logit(piyasa) 0.5347, model-piyasa farkı -0.3884, log toplam baskı -0.2112, log en yüksek baskı 0.209, dakika 1.1716, skor eşit -0.1805, 1 gol fark -0.3475, kırmızı kart farkı -0.6474, log faul temposu 0.3308

