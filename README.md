# Gol Analizi – Gol Sinyali

Canlı maçları takip eder. Son dakikalarda baskı belirgin şekilde artınca ve modelin gol olasılığı bahis piyasasının fiyatladığından yüksekse Claude'a onaylatır, sonra telefona **ntfy** bildirimi gönderir. Her sinyalin sonucunu (gol geldi mi) kendisi kaydeder.

## Çalışma takvimi

Gün üç oturuma bölünür (TR saati): **10:00-15:50**, **16:00-21:50**, **22:00-03:00**. Her oturum GitHub Actions'ta otomatik başlar. GitHub bazen birkaç dakika geç başlatabilir.

API planı otomatik algılanır:
- **Pro / Ultra:** aşağıdaki tam sistem çalışır.
- **Ücretsiz:** sadece akşam en önemli 3 maç takip edilir (yedek mod).

## Tam sistem (Pro / Ultra)

1. **Maç öncesi oranlar:** Her oturum başında günün Üst/Alt 2.5 oranları (Bet365) çekilir.
   - Oranlardan her maçın **beklenen gol sayısı** hesaplanır.
   - Gün içinde Üst 2.5 oranı **%7 veya daha fazla düşen** maçlar işaretlenir ("akıllı para" izi). Bu maçlar takipte öne alınır.
2. **Canlı takip (30 saniyede bir):**
   - Tüm canlı maçlar tek istekle taranır.
   - İstatistikler 20'şer maçlık gruplar halinde çekilir. Kota yetmezse en değerli maçlar önceliklidir.
   - Canlı oranlar çekilir.
3. **Baskı puanı:** Her takım için son ~10 dakikada şut ×1, ceza sahası içi şut ×1,5, isabetli şut ×3, korner ×1,2, xG ×8 puanlanır. Skor durumu, kırmızı kart ve 75. dakika sonrası için düzeltme yapılır.
4. **Model ve piyasa karşılaştırması:**
   - **Model:** maç öncesi beklenen gol × baskı × dakika üzerinden "10 dakika içinde gol" olasılığı.
   - **Piyasa:** canlı Üst/Alt oranından marjı temizlenmiş "10 dakika içinde gol" olasılığı.
5. **Sinyal:** Baskı eşiği geçilmeli **ve** model olasılığı piyasadan en az **4 puan** yüksek olmalı. Sonra Claude değerlendirip onaylar ya da eler.
6. **Kayıt:**
   - `data/sinyaller.csv`: her sinyal ve sonucu (`isabet` = 10 dakika içinde gol, `ıska` = gol yok)
   - `data/ornekler/`: her canlı maçtan 5 dakikada bir örnek ve 10 dakika sonrası etiketi. Model kalibrasyonu için.
   - `data/oranlar/`: günlük maç öncesi oran geçmişi
7. Her oturum sonunda bir **özet bildirimi** gelir.

## Gizli anahtarlar

**Settings → Secrets and variables → Actions** altında:

| Ad | Değer |
|---|---|
| `API_FOOTBALL_KEY` | dashboard.api-football.com → Account → My Access |
| `NTFY_TOPIC` | ntfy konu adı |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude abonelik token'ı |

## Test

**Actions → Gol Sinyali → Run workflow** yoluna gir ve `test` seç. Bağlantıları, toplu istatistiği, xG'yi ve canlı oranları kontrol edip sonucu bildirim olarak gönderir.

Canlı takibi hemen başlatmak için `run` seç.

## Ayarlar

`.github/workflows/gol-sinyali.yml` içinde:

| Ayar | Anlamı |
|---|---|
| `INTERVAL` | Tur aralığı (sn). Ultra için 30. |
| `EDGE` | Model ile piyasa arasında aranan en az fark. Büyütürsen daha az ama daha seçici sinyal gelir. |
| `THRESHOLD` | Baskı eşiği. |
| `MAX_CLAUDE_CALLS` | Bir oturumda en fazla Claude çağrısı. |

## Uyarı

Sinyaller istatistiksel bir tahmindir, garanti değildir. İlk haftaları sadece izleyerek ve isabet oranına bakarak geçir. Model 2-3 haftalık veriyle yeniden ayarlanacak.
