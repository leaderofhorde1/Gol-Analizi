# Canlı model eğitimi – 2026-11-25T04:08+03:00

Toplam örnek: 29452

Log kayıp: düşük = daha iyi. Yeni model ancak testte piyasadan iyiyse kullanılır.

## gol

| | Değer |
|---|---|
| Örneklem / test | 16489 / 4947 |
| Olay oranı | 0.261 |
| Log kayıp: piyasa | 0.56561 |
| Log kayıp: eski model | 0.59468 |
| Log kayıp: piyasa + maç durumu (B) | 0.56514 |
| Log kayıp: tam model (C) | 0.56419 |
| Brier (piyasa / B / C) | 0.18961 / 0.18945 / 0.18916 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 82 adet, isabet 0.305, piyasa ort. 0.217 |
| Kullanımda | evet |

Ağırlıklar: sabit 0.9846, logit(piyasa) 1.3226, model-piyasa farkı 0.7541, log toplam baskı -0.1464, log en yüksek baskı -0.0969, dakika -0.3927, skor eşit 0.1453, 1 gol fark -0.0762, kırmızı kart farkı -0.0076

## korner

| | Değer |
|---|---|
| Örneklem / test | 1317 / 392 |
| Olay oranı | 0.135 |
| Log kayıp: piyasa | 0.90141 |
| Log kayıp: eski model | 0.3617 |
| Log kayıp: piyasa + maç durumu (B) | 0.4283 |
| Log kayıp: tam model (C) | 0.39072 |
| Brier (piyasa / B / C) | 0.26553 / 0.12829 / 0.11891 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 71 adet, isabet 0.155, piyasa ort. 0.255 |
| Kullanımda | hayır |

Ağırlıklar: sabit -1.0679, logit(piyasa) 0.3638, model-piyasa farkı 0.2135, log toplam baskı 1.1505, log en yüksek baskı -0.1801, dakika -0.9078, skor eşit -0.0786, 1 gol fark -0.31, kırmızı kart farkı 0.5438, log faul temposu -0.0537

## kart

| | Değer |
|---|---|
| Örneklem / test | 1616 / 485 |
| Olay oranı | 0.402 |
| Log kayıp: piyasa | 0.70013 |
| Log kayıp: eski model | 0.66807 |
| Log kayıp: piyasa + maç durumu (B) | 0.65708 |
| Log kayıp: tam model (C) | 0.66086 |
| Brier (piyasa / B / C) | 0.25165 / 0.23219 / 0.23338 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 1 adet, isabet 0.0, piyasa ort. 0.277 |
| Kullanımda | hayır |

Ağırlıklar: sabit -1.1652, logit(piyasa) 0.732, model-piyasa farkı -0.1343, log toplam baskı -0.2488, log en yüksek baskı 0.1865, dakika 1.1224, skor eşit -0.1212, 1 gol fark -0.2951, kırmızı kart farkı -0.7133, log faul temposu 0.2146

