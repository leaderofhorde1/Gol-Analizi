# Gol Analizi – Gol Sinyali

Canlı maçların istatistiklerini takip eder. Son dakikalarda bir takımın baskısı belirgin şekilde artınca Claude'a onaylatır ve telefona **ntfy** bildirimi gönderir. Sonra her sinyali **gol geldi mi** diye kendisi kontrol edip kaydeder.

## Nasıl çalışır

1. Her gün **18:00**'de (TR) GitHub Actions başlar ve **23:45**'e kadar çalışır.
2. Başlangıçta bugünün fikstürünü çeker. Süper Lig ve büyük Avrupa ligleri önceliklidir.
3. Canlı maçlardan en önemli 20 tanesinin istatistiklerini **tek istekle** çeker. Sorgu aralığını günlük 100 isteklik ücretsiz kotaya göre kendisi ayarlar (yaklaşık 3-4 dakikada bir).
4. Her takım için son ~10 dakikanın **baskı puanını** hesaplar:
   - şut ×1, ceza sahası içi şut ×1,5, isabetli şut ×3, korner ×1,2, xG ×8
   - geride olan takım, rakibi 10 kişi kalan takım ve 75. dakikadan sonrası için puan artırılır
5. Puan eşiği (varsayılan **13**) geçerse Claude maçı değerlendirir ve sinyali onaylar ya da eler.
6. Onaylanan sinyal telefona gelir. Sinyalden sonraki **10 dakika** içinde gol olursa `isabet`, olmazsa `ıska` olarak kaydedilir.
7. Gün sonunda bir **özet bildirimi** gelir: kaç sinyal gönderildi, kaçı isabetli oldu.

Kayıtlar: `data/sinyaller.csv` (tüm sinyaller ve sonuçları), `data/anlik/` (ham anlık veriler; ileride eşikleri ayarlamak için).

## Kurulum: gizli anahtarlar

Depoda **Settings → Secrets and variables → Actions → New repository secret** yoluna gir ve şu üçünü ekle:

| Ad | Değer |
|---|---|
| `API_FOOTBALL_KEY` | dashboard.api-football.com → Account → My Access |
| `NTFY_TOPIC` | ntfy uygulamasında abone olduğun konu adı |
| `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token` çıktısı |

## Test

**Actions → Gol Sinyali → Run workflow** yoluna gir, `mod: test` seçili olarak çalıştır. Telefona iki bildirim gelmeli: bir test bildirimi ve test sonucu.

Canlı takibi hemen başlatmak için aynı yerden `mod: run` seç.

## Ayarlar

`.github/workflows/gol-sinyali.yml` içinde:

- `cron`: başlangıç saati (UTC; TR saati = UTC+3)
- `WINDOW_END`: takibin bitiş saati
- `THRESHOLD`: baskı eşiği. Daha az ama daha güçlü sinyal için büyüt, daha çok sinyal için küçült.

## Uyarı

Sinyaller istatistiksel bir tahmindir, garanti değildir. İlk 2-3 haftayı sadece izleyerek ve isabet oranına bakarak geçir.
