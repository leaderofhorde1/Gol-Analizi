# Canlı model eğitimi – 2026-11-27T04:13+03:00

Toplam örnek: 34442

Log kayıp: düşük = daha iyi. Yeni model ancak testte piyasadan iyiyse kullanılır.

## gol

| | Değer |
|---|---|
| Örneklem / test | 19845 / 5954 |
| Olay oranı | 0.261 |
| Log kayıp: piyasa | 0.56855 |
| Log kayıp: eski model | 0.60256 |
| Log kayıp: piyasa + maç durumu (B) | 0.5699 |
| Log kayıp: tam model (C) | 0.56941 |
| Brier (piyasa / B / C) | 0.19085 / 0.19132 / 0.19123 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 102 adet, isabet 0.294, piyasa ort. 0.246 |
| Kullanımda | hayır |

Ağırlıklar: sabit 1.0832, logit(piyasa) 1.3254, model-piyasa farkı 0.831, log toplam baskı -0.1256, log en yüksek baskı -0.163, dakika -0.4169, skor eşit 0.1352, 1 gol fark -0.1136, kırmızı kart farkı 0.0619

## korner

| | Değer |
|---|---|
| Örneklem / test | 2898 / 869 |
| Olay oranı | 0.261 |
| Log kayıp: piyasa | 0.68122 |
| Log kayıp: eski model | 0.56766 |
| Log kayıp: piyasa + maç durumu (B) | 0.57552 |
| Log kayıp: tam model (C) | 0.55492 |
| Brier (piyasa / B / C) | 0.22606 / 0.19423 / 0.1883 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 181 adet, isabet 0.293, piyasa ort. 0.291 |
| Kullanımda | hayır (sinyal anlarında piyasayı geçmiyor) |

Ağırlıklar: sabit -1.0377, logit(piyasa) 0.619, model-piyasa farkı 0.578, log toplam baskı 1.2849, log en yüksek baskı -0.2253, dakika -0.1864, skor eşit -0.0573, 1 gol fark -0.1184, kırmızı kart farkı 0.4126, log faul temposu -0.4389

## kart

| | Değer |
|---|---|
| Örneklem / test | 2237 / 672 |
| Olay oranı | 0.369 |
| Log kayıp: piyasa | 0.68722 |
| Log kayıp: eski model | 0.67525 |
| Log kayıp: piyasa + maç durumu (B) | 0.6464 |
| Log kayıp: tam model (C) | 0.64797 |
| Brier (piyasa / B / C) | 0.24318 / 0.22783 / 0.22848 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 8 adet, isabet 0.5, piyasa ort. 0.325 |
| Kullanımda | hayır |

Ağırlıklar: sabit -1.0872, logit(piyasa) 0.7672, model-piyasa farkı -0.0102, log toplam baskı -0.0622, log en yüksek baskı 0.1013, dakika 0.8349, skor eşit -0.1396, 1 gol fark -0.2489, kırmızı kart farkı -1.0475, log faul temposu 0.1842

