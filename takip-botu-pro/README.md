# Takip Botu PRO v2 — TR E-Ticaret Fiyat & Stok Takibi

Amazon.tr, Hepsiburada, N11, Trendyol, Akakçe, Tebilon vb. sitelerden PC parçası
fiyat/stok takibi yapar, hedef tutunca WhatsApp'a bildirim atar.
Eski `stock-bot` (beden/stok takibi) ile `takip_botu_pro` (fiyat takibi) birleştirilip
zayıf noktaları kapatılmış hâlidir.

## Kurulum

```bash
pip install -r requirements.txt
playwright install chromium
```

## Çalıştırma

```bash
python takip_botu_pro.py          # normal çalışma — ilk açılışta WhatsApp QR okut
python takip_botu_pro.py test     # WhatsApp'a test mesajı gönder
python takip_botu_pro.py once     # tüm ürünleri BİR KEZ oku, tabloyu bas, bildirim atma
```

`once` modu en önemli araçtır: yeni ürün/site eklediğinde önce bununla dene.
Fiyat `—` görünüyorsa veya KAYNAK sütunu `regex` diyorsa `sites.yaml`'a o site için
doğru `price_selector` yaz (sayfada sağ tık → İncele → fiyat elementinin class'ı).

## Dosyalar

| Dosya | Ne işe yarar |
|---|---|
| `products.yaml` | Ürün listesi + genel ayarlar (telefon, eşzamanlılık, heartbeat...) |
| `sites.yaml` | Siteye özel CSS seçicileri — site okumuyorsa burayı düzelt |
| `state.json` | Bildirim durumu (otomatik oluşur) — mükerrer bildirim engeli buradan |
| `fiyat_gecmisi.csv` | Her okuma buraya loglanır: `zaman;urun;fiyat;stok;kaynak` |
| `takip.log` | Çalışma logu |
| `.chrome-profile-bot/` | Kalıcı tarayıcı profili (WhatsApp oturumu) — **git'e girmez!** |

## Nasıl karar veriyor? (tasarım kararları)

1. **Fiyat okuma zinciri** — güvenden düşüğe doğru:
   `JSON-LD → siteye özel seçici → genel seçiciler → meta tag → gövde regex`.
   JSON-LD'de `@graph`, `offers`, `lowPrice` gezilir ve para birimi TRY değilse
   reddedilir (bazı siteler USD fiyat da gömer).

2. **TL parser** — `53.599 TL` (nokta=binlik) ile `53.599,50 TL` (virgül=ondalık)
   ayrımı: hem nokta hem virgül varsa *sondaki* işaret ondalıktır; tek işaret varsa
   ve sonrasında tam 3 hane geliyorsa binliktir.

3. **Şüpheli fiyat koruması (sanity guard)** — yanlış alarmın ana kaynağı parse
   hatasıdır (önerilen ürünün fiyatını okumak, kuruş kayması...). Fiyat şu üç
   durumdan birine giriyorsa *hemen bildirilmez*, 1-2 dk sonra ikinci okumayla
   (±%2 tutarlılık) doğrulanır:
   - kaynak `regex` (düşük güven),
   - hedef fiyatın yarısından da ucuz,
   - son iyi fiyata göre %40'tan fazla ani düşüş.

4. **Mükerrer bildirim engeli** — `state.json` kalıcıdır; bot yeniden başlasa da
   cooldown (varsayılan 24 sa) devam eder. Ama cooldown içinde fiyat
   `renotify_drop_pct` (%3) kadar *daha da* düşerse yine bildirir — fırsatı kaçırmaz.

5. **Site bazlı kuyruk + geri çekilme** — aynı siteye istekler serileştirilir
   (`min_gap_per_host_seconds` + rastgele jitter). Captcha/bot-koruması sayfası
   algılanırsa o siteye üstel geri çekilme uygulanır (5 dk → 10 → ... → 60 dk),
   başarılı okumada sıfırlanır. Böylece 4 Amazon ürünü aynı anda vurup ban yemez.

6. **Bildirim doğrulaması** — WhatsApp Web'de gönder butonuna tıklandıktan sonra
   mesaj kutusunun *boşaldığı* kontrol edilir; boşalmadıysa gönderilmemiş sayılır ve
   CallMeBot yedek kanalına düşülür. WhatsApp oturumu düşmüşse (QR ekranı) bunu fark
   eder ve CallMeBot mesajına "QR okut" uyarısı ekler.

7. **Sessiz ölüm koruması** — üç katman:
   - günlük heartbeat: "bot yaşıyor" + tüm ürünlerin son bilinen fiyatı,
   - heartbeat'te 12 saatten eski okumalar ⚠️ ile işaretlenir (site sessizce
     engellediyse görürsün),
   - bir ürün üst üste 5 kez okunamazsa tek seferlik uyarı mesajı gelir.

8. **RAM disiplini** — her kontrol kendi sekmesini açar ve `finally` ile kapatır;
   günlerce çalışsa da sekme birikmez.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| Fiyat hep `—` | `once` çalıştır; siteyi tarayıcıda aç, fiyat elementinin class'ını `sites.yaml`'a yaz |
| Kaynak `regex` | Çalışır ama güven düşük; siteye özel seçici ekle |
| `[ENGEL]` logda | Site bot koruması gösteriyor; bot kendini geri çeker. Sık oluyorsa `sleep_min/max` büyüt |
| WhatsApp gitmiyor | `python takip_botu_pro.py test` çalıştır; QR ekranı varsa okut |
| Bot ölmüş, haberim yok | `heartbeat_hour` ayarlı olsun + `callmebot_apikey` doldur (yedek kanal) |

## Amazon fiyatı yanlış geliyor?

Amazon sayfasında birden çok fiyat olur (diğer satıcılar, önerilenler, abonelik).
Bu yüzden seçici ana fiyat bloğuna (`#corePriceDisplay_desktop_feature_div`)
sınırlandı. Yine de yanlışsa `once` moduyla kontrol edip seçiciyi daralt.
