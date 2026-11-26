# Canlı model eğitimi – 2026-11-26T04:08+03:00

Toplam örnek: 32390

Log kayıp: düşük = daha iyi. Yeni model ancak testte piyasadan iyiyse kullanılır.

## gol

| | Değer |
|---|---|
| Örneklem / test | 18432 / 5530 |
| Olay oranı | 0.258 |
| Log kayıp: piyasa | 0.56408 |
| Log kayıp: eski model | 0.59462 |
| Log kayıp: piyasa + maç durumu (B) | 0.5631 |
| Log kayıp: tam model (C) | 0.56094 |
| Brier (piyasa / B / C) | 0.18891 / 0.18848 / 0.18777 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 61 adet, isabet 0.279, piyasa ort. 0.218 |
| Kullanımda | evet |

Ağırlıklar: sabit 1.0103, logit(piyasa) 1.3432, model-piyasa farkı 0.7896, log toplam baskı -0.225, log en yüksek baskı -0.0309, dakika -0.377, skor eşit 0.1766, 1 gol fark -0.0572, kırmızı kart farkı -0.0207

## korner

| | Değer |
|---|---|
| Örneklem / test | 2089 / 627 |
| Olay oranı | 0.159 |
| Log kayıp: piyasa | 0.94172 |
| Log kayıp: eski model | 0.39581 |
| Log kayıp: piyasa + maç durumu (B) | 0.42685 |
| Log kayıp: tam model (C) | 0.38114 |
| Brier (piyasa / B / C) | 0.27999 / 0.13122 / 0.12029 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 74 adet, isabet 0.284, piyasa ort. 0.281 |
| Kullanımda | hayır (sinyal anlarında piyasayı geçmiyor) |

Ağırlıklar: sabit -0.8287, logit(piyasa) 0.6635, model-piyasa farkı 0.5405, log toplam baskı 1.2091, log en yüksek baskı -0.3354, dakika -0.325, skor eşit -0.0951, 1 gol fark -0.2183, kırmızı kart farkı 0.4778, log faul temposu -0.3077

## kart

| | Değer |
|---|---|
| Örneklem / test | 1848 / 555 |
| Olay oranı | 0.377 |
| Log kayıp: piyasa | 0.67995 |
| Log kayıp: eski model | 0.64436 |
| Log kayıp: piyasa + maç durumu (B) | 0.63563 |
| Log kayıp: tam model (C) | 0.63587 |
| Brier (piyasa / B / C) | 0.24063 / 0.22261 / 0.2225 |
| Simüle sinyal (yeni − piyasa ≥ 5 puan) | 2 adet, isabet 0.0, piyasa ort. 0.273 |
| Kullanımda | hayır |

Ağırlıklar: sabit -1.1763, logit(piyasa) 0.6475, model-piyasa farkı -0.1502, log toplam baskı -0.0016, log en yüksek baskı -0.0459, dakika 1.0204, skor eşit -0.0481, 1 gol fark -0.2042, kırmızı kart farkı -0.8517, log faul temposu 0.1714

