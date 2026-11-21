# Canlı model eğitimi – 2026-11-21T04:02+03:00

Toplam örnek: 1596

Log kayıp: düşük = daha iyi. Yeni model ancak testte piyasadan iyiyse kullanılır.

## gol

| | Değer |
|---|---|
| Örneklem / test | 1191 / 358 |
| Olay oranı | 0.274 |
| Log kayıp: piyasa | 0.59449 |
| Log kayıp: eski model | 0.62554 |
| Log kayıp: piyasa + maç durumu (B) | 0.58895 |
| Log kayıp: tam model (C) | 0.59331 |
| Brier (piyasa / B / C) | 0.20242 / 0.19987 / 0.20201 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 12 adet, isabet 0.167, piyasa ort. 0.203 |
| Kullanımda | hayır |

Ağırlıklar: sabit 0.5091, logit(piyasa) 1.0556, model-piyasa farkı 1.04, log toplam baskı -0.0729, log en yüksek baskı -0.1704, dakika -0.4241, skor eşit 0.5087, 1 gol fark 0.3592, kırmızı kart farkı 0.7934

## korner

| | Değer |
|---|---|
| Örneklem / test | 551 / 166 |
| Olay oranı | 0.217 |
| Log kayıp: piyasa | 0.80264 |
| Log kayıp: eski model | 0.52035 |
| Log kayıp: piyasa + maç durumu (B) | 3.66194 |
| Log kayıp: tam model (C) | 2.99614 |
| Brier (piyasa / B / C) | 0.25197 / 0.26506 / 0.21687 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 0 adet, isabet None, piyasa ort. None |
| Kullanımda | hayır |

Ağırlıklar: sabit -281.553, logit(piyasa) 93.1318, model-piyasa farkı 410.259, log toplam baskı -351.358, log en yüksek baskı -322.2345, dakika -152.2471, skor eşit -91.9995, 1 gol fark -126.4495, kırmızı kart farkı -4.0

## kart

örneklem yetersiz (örneklem 59)

