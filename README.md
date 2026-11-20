# Gol Analizi – Gol Sinyali

Canlı maçları takip eder ve telefona **ntfy** ile üç tür sinyal gönderir: ⚽ **gol**, 🚩 **korner**, 🟨 **kart**. Her sinyalde modelin olasılığı bahis piyasasının fiyatladığıyla karşılaştırılır; sinyal yalnızca model belirgin şekilde yüksekse gelir. Her sabah ☀️ **maç öncesi rapor** gönderir, maç başlamadan önemli 📉 **oran düşüşlerini** bildirir. Tüm sinyallerin ve tahminlerin sonucunu kendisi kaydeder.

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
5. **Gol sinyali:** Baskı eşiği geçilmeli **ve** model olasılığı piyasadan en az **4 puan** yüksek olmalı. Sonra Claude değerlendirip onaylar ya da eler.
6. **🚩 Korner sinyali:** Son dakikalardaki şut, engellenen şut ve korner temposu normalin en az 1,5 katı olmalı. "10 dakikada korner" olasılığı da canlı korner pazarından (Total/Asian Corners) en az **7 puan** yüksek olmalı. Geride olan takımın son dakikalarda yüklenmesi hesaba katılır.
7. **🟨 Kart sinyali:** Faul temposu normalin en az 1,5 katı olmalı ve model canlı kart pazarından (Total Cards) en az **7 puan** yüksek olmalı. Hakemin geçmiş kart ortalaması, maçın evresi ve skor gerginliği hesaba katılır. Hakem verisi sistem çalıştıkça `data/hakemler.json`'da birikir; ilk haftalarda genel ortalama kullanılır.
   Korner ve kart sinyalleri Claude'a sorulmaz, böylece abonelik limitin gol sinyallerine kalır.
8. **Kayıt:**
   - `data/sinyaller.csv`: her sinyal (`tur`: gol/korner/kart) ve sonucu (`isabet` = 10 dakika içinde olay oldu, `ıska` = olmadı)
   - `data/ornekler/`: her canlı maçtan 5 dakikada bir örnek ve 10 dakika sonrası gol/korner/kart etiketi. Model kalibrasyonu için.
   - `data/oranlar/`: günlük maç öncesi oran geçmişi
9. Her oturum sonunda üç sinyal türünün isabetini gösteren bir **özet bildirimi** gelir.

## Maç öncesi modül

- **☀️ Sabah raporu (10:00 oturumu):** Günün en önemli 40 maçı için olasılıklar hesaplanır.
  - **Model:** Takımların bu sezonki iç saha ve deplasman gol ortalamalarından Poisson (Dixon-Coles düzeltmeli) tahmini: 1/X/2, Üst-Alt 2.5, KG Var-Yok, en olası skorlar.
  - **API:** API-Football'un kendi tahmini (ikinci görüş).
  - **Piyasa:** Bet365 oranından marjı temizlenmiş olasılık.
  - Model piyasadan en az **6 puan** yüksekse ve 1/X/2 seçimlerinde API de aynı yöndeyse seçim "değerli" olarak işaretlenir (oran 1,40-4,00 arası, en fazla 8 seçim).
  - Bildirimde gol beklentisi en yüksek maçlar, değerli görünen seçimler ve oranı düşen maçlar yer alır. Tam rapor: `data/rapor/GÜN.md`.
- **📉 Oran düşüşü uyarısı:** Oranlar saatte bir yenilenir. Başlamasına 3 saatten az kalan maçlarda bir oran (1/X/2, Üst-Alt, KG) günün ilk oranına göre **%7 veya daha fazla** düşmüşse bildirim gelir.
- **Sonuç takibi:** Seçimler `data/maconcesi.csv`'ye kaydedilir; maç bitince sonucu otomatik işaretlenir. Sabah raporu geçmiş isabet oranını da gösterir.

## Gizli anahtarlar

**Settings → Secrets and variables → Actions** altında:

| Ad | Değer |
|---|---|
| `API_FOOTBALL_KEY` | dashboard.api-football.com → Account → My Access |
| `NTFY_TOPIC` | ntfy konu adı |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude abonelik token'ı |

## Test

**Actions → Gol Sinyali → Run workflow** yoluna gir ve şunlardan birini seç:
- `test`: bağlantıları, toplu istatistiği, xG'yi ve canlı oranları kontrol eder, sonucu bildirim olarak gönderir.
- `kisa`: 6 dakikalık gerçek bir canlı deneme yapar ve sabah raporunu hemen üretir.
- `diag`: canlı oran pazarlarının adlarını loglara yazar.

Canlı takibi hemen başlatmak için `run` seç.

## Ayarlar

`.github/workflows/gol-sinyali.yml` içinde:

| Ayar | Anlamı |
|---|---|
| `INTERVAL` | Tur aralığı (sn). Ultra için 30. |
| `SIGNALS` | Açık sinyal türleri, örn. sadece gol ve korner için `gol,korner`. |
| `EDGE` / `EDGE_CORNER` / `EDGE_CARD` | Model ile piyasa arasında aranan en az fark. Büyütürsen daha az ama daha seçici sinyal gelir. |
| `THRESHOLD` | Baskı eşiği. |
| `MAX_CLAUDE_CALLS` | Bir oturumda en fazla Claude çağrısı. |

## Uyarı

Sinyaller istatistiksel bir tahmindir, garanti değildir. İlk haftaları sadece izleyerek ve isabet oranına bakarak geçir. Model 2-3 haftalık veriyle yeniden ayarlanacak.
