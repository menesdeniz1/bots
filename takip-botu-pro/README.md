# Takip Botu PRO v3 — TR E-Ticaret Fiyat & Stok Takibi (Telegram + Akakçe)

Amazon.tr, Hepsiburada, N11, Trendyol, Akakçe, Tebilon vb. sitelerden PC parçası
fiyat/stok takibi yapar, hedef tutunca **Telegram'a** bildirim atar. Aynı ürünü
birden çok kaynaktan (Akakçe birincil) izleyip **en ucuzunu** bildirir.
Fiyat geçmişinden **HTML grafik** üretir.

## Kurulum

```bash
pip install -r requirements.txt
playwright install chromium
```

**Telegram botu kur (2 dakika):**
1. Telegram'da `@BotFather`'a `/newbot` yaz → verdiği token'ı `products.yaml` →
   `telegram_bot_token` alanına koy.
2. Oluşan botuna Telegram'dan `/start` yaz.
3. `python takip_botu_pro.py chatid` → çıkan id'yi `telegram_chat_id` alanına koy.
4. `python takip_botu_pro.py test` → telefona test mesajı gelmeli.

WhatsApp Web tamamen kalktı: QR yok, tarayıcı oturumu derdi yok, bot **tam
headless** çalışır. CallMeBot hâlâ yedek kanal olarak durur (isteğe bağlı).

## Çalıştırma

```bash
python takip_botu_pro.py          # normal çalışma
python takip_botu_pro.py once     # tüm ürünleri BİR KEZ oku, tabloyu bas, bildirim atma
python takip_botu_pro.py test     # Telegram'a test mesajı
python takip_botu_pro.py chatid   # chat_id'ni öğren
python takip_botu_pro.py grafik   # fiyat_grafigi.html üret (veya: python grafik.py)
```

`once` modu en önemli araçtır: yeni ürün/site eklediğinde önce bununla dene.
Fiyat `—` görünüyorsa veya KAYNAK sütunu `regex` diyorsa `sites.yaml`'a o site için
doğru `price_selector` yaz (sayfada sağ tık → İncele → fiyat elementinin class'ı).

## Telegram komutları (bot çalışırken)

| Komut | Ne yapar |
|---|---|
| `/durum` | Tüm ürünlerin son bilinen fiyatı + hedef + kaynak site |
| `/grafik` | Fiyat geçmişi grafiğini HTML dosyası olarak gönderir |
| `/csv` | Ham fiyat geçmişini gönderir |
| `/yardim` | Komut listesi |

Sadece `telegram_chat_id`'deki sohbetten gelen komutlar işlenir; yabancılar yok sayılır.

## Akakçe birincil kurgusu (çoklu kaynak)

Ürüne `url` yerine `urls` listesi ver — bot **hepsini** kontrol eder, **en ucuzunu**
bildirir. İlk sıraya Akakçe linkini koy: tüm satıcıların en ucuzu tek sayfadadır,
mağaza linki de yedek kaynak olur. Akakçe'de en ucuz satıcının adı bildirime eklenir.

```yaml
- label: "AMD Ryzen 7 7800X3D"
  urls:
    - "https://www.akakce.com/islemci/en-ucuz-amd-ryzen-7-7800x3d-fiyati,XXXXXXX.html"
    - "https://www.amazon.com.tr/dp/B0CJML6LQZ"
  mode: price
  price_threshold_tl: 13500
```

## Dosyalar

| Dosya | Ne işe yarar |
|---|---|
| `products.yaml` | Ürün listesi + genel ayarlar (Telegram, eşzamanlılık, heartbeat...) |
| `sites.yaml` | Siteye özel CSS seçicileri — site okumuyorsa burayı düzelt |
| `grafik.py` | `fiyat_gecmisi.csv` → `fiyat_grafigi.html` (koyu/açık tema, ürün başına grafik) |
| `state.json` | Bildirim durumu (otomatik oluşur) — mükerrer bildirim engeli buradan |
| `fiyat_gecmisi.csv` | Her okuma: `zaman;urun;site;fiyat;stok;kaynak` |
| `takip.log` | Çalışma logu |
| `.chrome-profile-bot/` | Kalıcı tarayıcı profili (çerezler) — **git'e girmez!** |

## Nasıl karar veriyor? (tasarım kararları)

1. **Fiyat okuma zinciri** — güvenden düşüğe doğru:
   `JSON-LD → siteye özel seçici → genel seçiciler → meta tag → gövde regex`.
   JSON-LD'de `@graph`, `offers`, `lowPrice` gezilir ve para birimi TRY değilse
   reddedilir (bazı siteler USD fiyat da gömer).

2. **TL parser** — `53.599 TL` (nokta=binlik) ile `53.599,50 TL` (virgül=ondalık)
   ayrımı: hem nokta hem virgül varsa *sondaki* işaret ondalıktır; tek işaret varsa
   ve sonrasında tam 3 hane geliyorsa binliktir.

3. **Şüpheli fiyat koruması (sanity guard)** — yanlış alarmın ana kaynağı parse
   hatasıdır. Fiyat şu üç durumdan birine giriyorsa *hemen bildirilmez*, 1-2 dk
   sonra ikinci okumayla (±%2 tutarlılık) doğrulanır:
   - kaynak `regex` (düşük güven),
   - hedef fiyatın yarısından da ucuz,
   - son iyi fiyata göre %40'tan fazla ani düşüş.

4. **Çoklu kaynak + en ucuz seçimi** — ürünün tüm kaynakları okunur, geçerli en
   düşük fiyat bildirime esas olur; her kaynak ayrı ayrı CSV'ye loglanır (grafikte
   siteler ayrı çizgi olur, siteler arası fark görünür).

5. **Mükerrer bildirim engeli** — `state.json` kalıcıdır; bot yeniden başlasa da
   cooldown (varsayılan 24 sa) devam eder. Ama cooldown içinde fiyat
   `renotify_drop_pct` (%3) kadar *daha da* düşerse yine bildirir.

6. **Site bazlı kuyruk + geri çekilme** — aynı siteye istekler serileştirilir
   (`min_gap_per_host_seconds` + jitter). Captcha algılanırsa o siteye üstel geri
   çekilme (5 dk → 60 dk), başarılı okumada sıfırlanır.

7. **Bildirim güvenilirliği** — Telegram 3 deneme + üstel bekleme; hepsi patlarsa
   CallMeBot yedeğine düşer; o da patlarsa ERROR log. Sessiz kayıp yok.

8. **Sessiz ölüm koruması** — günlük heartbeat (fiyat özeti dahil), 12 saatten
   eski okumalara ⚠️ işareti, üst üste 5 başarısız okumada tek seferlik uyarı.
   Fiyatın hiç okunamadığı turlar da artık hata serisine sayılır.

9. **RAM disiplini** — her kontrol kendi sekmesini açar ve `finally` ile kapatır.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| Fiyat hep `—` | `once` çalıştır; fiyat elementinin class'ını `sites.yaml`'a yaz |
| Kaynak `regex` | Çalışır ama güven düşük; siteye özel seçici ekle |
| `[ENGEL]` / `⛔` | Site bot koruması gösteriyor; bot kendini geri çeker. Sık oluyorsa `sleep_min/max` büyüt |
| Telegram gitmiyor | `test` çalıştır; token ve chat_id'yi kontrol et |
| Bot ölmüş, haberim yok | `heartbeat_hour` ayarlı olsun; istersen `callmebot_apikey` da doldur |
| Grafik boş | Bot en az bir fiyat okumuş olmalı (`fiyat_gecmisi.csv` oluşmalı) |
