# Canlı model eğitimi – 2026-11-23T04:02+03:00

Toplam örnek: 24583

Log kayıp: düşük = daha iyi. Yeni model ancak testte piyasadan iyiyse kullanılır.

## gol

| | Değer |
|---|---|
| Örneklem / test | 14451 / 4336 |
| Olay oranı | 0.268 |
| Log kayıp: piyasa | 0.57134 |
| Log kayıp: eski model | 0.60681 |
| Log kayıp: piyasa + maç durumu (B) | 0.57364 |
| Log kayıp: tam model (C) | 0.57135 |
| Brier (piyasa / B / C) | 0.1923 / 0.19331 / 0.19253 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 72 adet, isabet 0.333, piyasa ort. 0.189 |
| Kullanımda | hayır |

Ağırlıklar: sabit 0.9166, logit(piyasa) 1.1708, model-piyasa farkı 0.8371, log toplam baskı -0.22, log en yüksek baskı -0.0629, dakika -0.4145, skor eşit 0.1298, 1 gol fark -0.1297, kırmızı kart farkı -0.009

## korner

| | Değer |
|---|---|
| Örneklem / test | 5686 / 1706 |
| Olay oranı | 0.22 |
| Log kayıp: piyasa | 0.80506 |
| Log kayıp: eski model | 0.50285 |
| Log kayıp: piyasa + maç durumu (B) | 0.51994 |
| Log kayıp: tam model (C) | 0.4957 |
| Brier (piyasa / B / C) | 0.25651 / 0.172 / 0.16494 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 293 adet, isabet 0.242, piyasa ort. 0.249 |
| Kullanımda | hayır (sinyal anlarında piyasayı geçmiyor) |

Ağırlıklar: sabit -1.9929, logit(piyasa) 0.0636, model-piyasa farkı 0.3539, log toplam baskı -0.1626, log en yüksek baskı 0.4938, dakika 0.6584, skor eşit 0.1597, 1 gol fark 0.0589, kırmızı kart farkı -0.2051

## kart

| | Değer |
|---|---|
| Örneklem / test | 1432 / 430 |
| Olay oranı | 0.36 |
| Log kayıp: piyasa | 0.69777 |
| Log kayıp: eski model | 0.64466 |
| Log kayıp: piyasa + maç durumu (B) | 0.64379 |
| Log kayıp: tam model (C) | 0.64667 |
| Brier (piyasa / B / C) | 0.24992 / 0.22583 / 0.2264 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 2 adet, isabet 0.0, piyasa ort. 0.273 |
| Kullanımda | hayır |

Ağırlıklar: sabit -1.2023, logit(piyasa) 0.6792, model-piyasa farkı -0.2721, log toplam baskı -0.1043, log en yüksek baskı 0.0323, dakika 1.1053, skor eşit -0.2379, 1 gol fark -0.3694, kırmızı kart farkı -0.7548, log faul temposu 0.2529

