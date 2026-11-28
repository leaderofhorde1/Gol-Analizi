# Canlı model eğitimi – 2026-11-28T04:08+03:00

Toplam örnek: 38217

Log kayıp: düşük = daha iyi. Yeni model ancak testte piyasadan iyiyse kullanılır.

## gol

| | Değer |
|---|---|
| Örneklem / test | 22531 / 6760 |
| Olay oranı | 0.259 |
| Log kayıp: piyasa | 0.56833 |
| Log kayıp: eski model | 0.60232 |
| Log kayıp: piyasa + maç durumu (B) | 0.56801 |
| Log kayıp: tam model (C) | 0.56679 |
| Brier (piyasa / B / C) | 0.19055 / 0.19031 / 0.19004 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 101 adet, isabet 0.307, piyasa ort. 0.27 |
| Kullanımda | evet |

Ağırlıklar: sabit 0.8644, logit(piyasa) 1.3167, model-piyasa farkı 0.771, log toplam baskı -0.3329, log en yüksek baskı 0.1046, dakika -0.3233, skor eşit 0.2737, 1 gol fark 0.038, kırmızı kart farkı 0.0247

## korner

| | Değer |
|---|---|
| Örneklem / test | 4007 / 1203 |
| Olay oranı | 0.172 |
| Log kayıp: piyasa | 0.86966 |
| Log kayıp: eski model | 0.42024 |
| Log kayıp: piyasa + maç durumu (B) | 0.44582 |
| Log kayıp: tam model (C) | 0.40938 |
| Brier (piyasa / B / C) | 0.26804 / 0.14011 / 0.13173 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 128 adet, isabet 0.227, piyasa ort. 0.284 |
| Kullanımda | hayır (sinyal anlarında piyasayı geçmiyor) |

Ağırlıklar: sabit -1.0529, logit(piyasa) 0.4791, model-piyasa farkı 0.4845, log toplam baskı 1.0209, log en yüksek baskı -0.1617, dakika -0.2658, skor eşit -0.1077, 1 gol fark -0.2415, kırmızı kart farkı 0.3795, log faul temposu -0.4165

## kart

| | Değer |
|---|---|
| Örneklem / test | 2409 / 723 |
| Olay oranı | 0.378 |
| Log kayıp: piyasa | 0.69413 |
| Log kayıp: eski model | 0.6851 |
| Log kayıp: piyasa + maç durumu (B) | 0.65851 |
| Log kayıp: tam model (C) | 0.65802 |
| Brier (piyasa / B / C) | 0.24674 / 0.23329 / 0.23302 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 6 adet, isabet 0.333, piyasa ort. 0.292 |
| Kullanımda | evet |

Ağırlıklar: sabit -1.0495, logit(piyasa) 0.5958, model-piyasa farkı -0.0762, log toplam baskı 0.1107, log en yüksek baskı -0.0672, dakika 0.8106, skor eşit -0.251, 1 gol fark -0.3389, kırmızı kart farkı -0.8252, log faul temposu 0.181

