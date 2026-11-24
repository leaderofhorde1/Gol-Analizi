# Canlı model eğitimi – 2026-11-24T04:02+03:00

Toplam örnek: 26295

Log kayıp: düşük = daha iyi. Yeni model ancak testte piyasadan iyiyse kullanılır.

## gol

| | Değer |
|---|---|
| Örneklem / test | 15556 / 4667 |
| Olay oranı | 0.26 |
| Log kayıp: piyasa | 0.56374 |
| Log kayıp: eski model | 0.5918 |
| Log kayıp: piyasa + maç durumu (B) | 0.5635 |
| Log kayıp: tam model (C) | 0.56164 |
| Brier (piyasa / B / C) | 0.18883 / 0.18883 / 0.18819 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 66 adet, isabet 0.318, piyasa ort. 0.196 |
| Kullanımda | evet |

Ağırlıklar: sabit 1.1088, logit(piyasa) 1.3846, model-piyasa farkı 0.8336, log toplam baskı -0.2099, log en yüksek baskı -0.0563, dakika -0.4099, skor eşit 0.1739, 1 gol fark -0.0804, kırmızı kart farkı 0.0013

## korner

| | Değer |
|---|---|
| Örneklem / test | 6208 / 1863 |
| Olay oranı | 0.244 |
| Log kayıp: piyasa | 0.77941 |
| Log kayıp: eski model | 0.5307 |
| Log kayıp: piyasa + maç durumu (B) | 0.55358 |
| Log kayıp: tam model (C) | 0.52257 |
| Brier (piyasa / B / C) | 0.25373 / 0.18605 / 0.17585 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 272 adet, isabet 0.268, piyasa ort. 0.243 |
| Kullanımda | evet |

Ağırlıklar: sabit -1.6844, logit(piyasa) 0.1759, model-piyasa farkı 0.3408, log toplam baskı -0.0427, log en yüksek baskı 0.3929, dakika 0.2988, skor eşit 0.0425, 1 gol fark -0.0768, kırmızı kart farkı 0.1211

## kart

| | Değer |
|---|---|
| Örneklem / test | 1549 / 465 |
| Olay oranı | 0.4 |
| Log kayıp: piyasa | 0.69761 |
| Log kayıp: eski model | 0.67419 |
| Log kayıp: piyasa + maç durumu (B) | 0.6599 |
| Log kayıp: tam model (C) | 0.6626 |
| Brier (piyasa / B / C) | 0.2503 / 0.23323 / 0.23393 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 5 adet, isabet 0.0, piyasa ort. 0.322 |
| Kullanımda | hayır |

Ağırlıklar: sabit -1.1018, logit(piyasa) 0.7183, model-piyasa farkı -0.1265, log toplam baskı -0.2179, log en yüksek baskı 0.1483, dakika 1.1706, skor eşit -0.1724, 1 gol fark -0.2993, kırmızı kart farkı -0.7297, log faul temposu 0.1866

