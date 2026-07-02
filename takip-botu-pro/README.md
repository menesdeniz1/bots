# Takip Botu PRO v4 — TR E-Ticaret Fiyat & Stok Takibi (Telegram + Akakçe)

Amazon.tr, Hepsiburada, N11, Trendyol, Akakçe, Tebilon vb. sitelerden PC parçası
fiyat/stok takibi yapar, hedef tutunca **Telegram'a** bildirim atar. Aynı ürünü
birden çok kaynaktan (Akakçe birincil) izleyip **en ucuzunu** bildirir.
Ürünler **Telegram'dan yönetilir** (/ekle /sil /hedef), fiyat hedefe inmese bile
**son 30 günün dibini** haber verir, haftada bir **grafik raporu** gönderir.

## Hızlı başlangıç (minimum girdi)

**Windows:** `kur.bat`'a çift tıkla — paketleri kurar, sihirbaz Telegram'ı
soru-cevapla bağlar (token yapıştır + botuna `/start` yaz, o kadar), istersen
botu hemen başlatır. Hiçbir YAML dosyası düzenlemen gerekmez.

**Linux/Mac:**
```bash
pip install -r requirements.txt && playwright install chromium
python takip_botu_pro.py kur     # sihirbaz — token yapıştır + botuna /start yaz
```

Ondan sonrası telefondan: **bota ürün linkini gönder** → fiyatı okur, adı
sayfadan alır, hedefi butonla seçtirir (%3 / %5 / %10 altı ya da kendin yaz).
Bitti — izleme başlar.

Sihirbaz ayarları `kurulum.yaml`'a yazar (git'e girmez); `products.yaml`'ı elle
düzenlemek tamamen isteğe bağlıdır. WhatsApp Web yok: QR yok, oturum derdi yok,
bot **tam headless** çalışır. CallMeBot yedek kanal olarak ayarlanabilir.

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
| **ürün linki gönder** | En kolay yol: fiyatı okur, hedefi butonla seçtirir, izlemeye alır |
| `/durum` | Son fiyatlar + hedefler + 7 günlük trend (↓%4,2/7g) + kaynak site |
| `/liste` | İzlenen ürünler, numaralı (sil/hedef için numara buradan) |
| `/ekle <link> [hedefTL] [etiket]` | Hedefsiz verirsen butonlu akış açılır |
| `/sil <no>` | Ürünü izlemeden çıkar |
| `/hedef <no> <fiyatTL>` | Hedef fiyatı değiştir |
| `/akakce <no>` | Ürünü adıyla Akakçe'de arar, butonla onaylarsın → Akakçe birincil kaynak olur |
| `/grafik` | Grafik: PNG (hızlı bakış) + HTML (etkileşimli) gönderir |
| `/csv` | Ham fiyat geçmişini gönderir |
| `/yardim` | Komut listesi |

Sadece `telegram_chat_id`'deki sohbetten gelen komutlar işlenir; yabancılar yok sayılır.

Telegram'dan yapılan ekleme/silme/hedef değişiklikleri `telegram_urunler.yaml`'a
yazılır — senin elle düzenlediğin `products.yaml` hiç bozulmaz; açılışta ikisi
birleştirilir. Bot yeniden başlatma gerektirmez, izleyiciler canlı güncellenir.

## 30 günün en düşüğü sinyali

Sabit hedef bazen fırsat kaçırtır: fiyat hedefe inmese bile **son 30 günün
dibini** gördüğünde bot 📉 bilgi mesajı atar. Günlük minimumlar `state.json`'da
tutulur (CSV taranmaz, hafiftir). Ayarlar: `low30_alert`, `low30_min_days`
(ilk günlerde spam olmasın diye en az 7 günlük veri ister), `low30_cooldown_minutes`.
Hedef alarmı zaten atılacaksa ayrıca dip mesajı atılmaz.

## 7/24 çalıştırma + git push ile uzaktan güncelleme

Botu doğrudan değil, **gözetmeni** çalıştır:

```bash
python3 guncelleyici.py
```

Gözetmen üç iş yapar (ayrıntı: `guncelleyici.py` docstring):

1. **Watchdog** — botu başlatır, çökerse üstel beklemeyle yeniden başlatır.
2. **Otomatik güncelleme** — ~90 sn'de bir git'i yoklar. Sen uzaktan push'larsın;
   gözetmen çeker, `requirements.txt` değiştiyse pip kurar, **smoke testinden**
   geçirir, botu nazikçe yeniden başlatır ve Telegram'a "⬆️ güncellendi" yazar.
3. **Bozuk push koruması** — üç ayrı ağ:
   - Açılmayan/derlenmeyen kod → smoke geçmez, push **geri alınır**, eski sürüm
     hiç kesintiye uğramadan devam eder, telefona "⛔ push bozuk" düşer.
   - Açılıp hemen çöken kod → 3 hızlı çökmeden sonra **bilinen son iyi sürüme**
     otomatik dönülür (5 dk yaşayan sürüm "iyi" işaretlenir).
   - Bot bozuk/kapalı haldeyken düzeltme push'larsan → her yeniden başlatma
     öncesi git kontrol edilir, düzeltme **bir sonraki denemede** alınır.
   Aynı bozuk commit tekrar tekrar denenmez; yeni commit gelince tekrar bakılır.

Katmanlar: işletim sistemi gözetmeni ayakta tutar, gözetmen botu. Gözetmen
bilerek **sadece stdlib** kullanır ve sistem Python'uyla çalışır — venv bozulsa
bile güncelleme mekanizması ölmez. Gözetmenin kendisi de repodan güncellenir
(önce derleme kontrolünden geçer).

**macOS:** `com.takip-botu.plist` içindeki yolları kontrol et, sonra:
```bash
cp com.takip-botu.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.takip-botu.plist
```

**Linux / Raspberry Pi:** `takip-botu.service` şablonunu düzenleyip systemd'ye kur:
```bash
sudo cp takip-botu.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now takip-botu
journalctl -u takip-botu -f     # canlı log
```

**Windows:** `calistir.bat` gözetmeni çalıştırır. Otomatik başlasın:
`Win+R` → `shell:startup` → açılan klasöre `calistir.bat`'ın kısayolunu koy.

ÖNEMLİ: Bu kurguda kaynak gerçeği git'tir — bot makinesinde `products.yaml`
gibi takip edilen dosyaları **elle değiştirme**, değişikliği push'la gönder.
(Telegram komutları serbest: onlar git dışı overlay dosyalarına yazar.)
Elle değişiklik algılanırsa gözetmen güncellemeyi bekletir ve Telegram'a uyarır.

Bot her (yeniden) başlayışta Telegram'a 🔄 mesajı atar; 30 dk içinde arka arkaya
başlıyorsa (çökme döngüsü) mesaj spam'i yapmaz, `takip.log`'a bakman gerektiğini
loglar.

## Akakçe birincil kurgusu (çoklu kaynak)

ÖNEMLİ: Bot ürünleri **kendiliğinden Akakçe'de aramaz** — sadece listedeki
linklere bakar. Bir ürünü Akakçe'ye bağlamanın iki yolu var:

1. **Telefondan (kolay):** `/akakce <no>` — bot ürün adıyla Akakçe'de arar,
   bulduğu ilk 3 ürün sayfasını buton yapar, doğrusuna tıklarsın. O andan
   itibaren Akakçe o ürünün **birincil** kaynağı olur (tüm satıcıların en ucuzu),
   mevcut mağaza linki yedek kaynak olarak kalır.
2. **Elle:** ürüne `url` yerine `urls` listesi ver, ilk sıraya Akakçe linkini koy.

Bot her turda ürünün tüm kaynaklarını kontrol eder ve **en ucuzunu** bildirir;
Akakçe'de en ucuz satıcının adı da bildirime eklenir.

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
| `telegram_urunler.yaml` | /ekle /sil /hedef değişiklikleri (bot yazar, otomatik oluşur) |
| `sites.yaml` | Siteye özel CSS seçicileri — site okumuyorsa burayı düzelt |
| `grafik.py` | `fiyat_gecmisi.csv` → `fiyat_grafigi.html` (koyu/açık tema, ürün başına grafik) |
| `guncelleyici.py` | Gözetmen: watchdog + git'ten otomatik güncelleme + bozuk push geri alma |
| `com.takip-botu.plist` | macOS launchd şablonu (gözetmeni ayakta tutar) |
| `calistir.bat` | Windows başlatıcı (gözetmeni ayakta tutar) |
| `takip-botu.service` | Linux/RaspberryPi systemd şablonu (gözetmeni ayakta tutar) |
| `guncelleyici_state.json` | Gözetmen durumu: son iyi sürüm + bozuk commit (otomatik) |
| `state.json` | Bildirim durumu + günlük minimumlar (otomatik oluşur) |
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

10. **Etiket değişikliğinde geçmiş göçü** — state/CSV/hedefler etikete göre
    anahtarlıdır. Ürünü `products.yaml`'da yeniden adlandırırsan bot açılışta
    URL parmak iziyle eski kaydı bulur ve geçmişi (cooldown, günlük minimumlar,
    grafik verisi, hedef/ek-kaynak) yeni ada taşır — hiçbir şey sıfırlanmaz.
    Not: etiketi VE tüm linkleri aynı anda değiştirirsen eşleşme yapılamaz.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| Fiyat hep `—` | `once` çalıştır; fiyat elementinin class'ını `sites.yaml`'a yaz |
| Kaynak `regex` | Çalışır ama güven düşük; siteye özel seçici ekle |
| `[ENGEL]` / `⛔` | Site bot koruması gösteriyor; bot kendini geri çeker. Sık oluyorsa `sleep_min/max` büyüt |
| Telegram gitmiyor | `test` çalıştır; token ve chat_id'yi kontrol et |
| Bot ölmüş, haberim yok | `heartbeat_hour` ayarlı olsun; istersen `callmebot_apikey` da doldur |
| Grafik boş | Bot en az bir fiyat okumuş olmalı (`fiyat_gecmisi.csv` oluşmalı) |
