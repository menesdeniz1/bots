# -*- coding: utf-8 -*-
"""
STOK + FİYAT TAKİP BOTU — PRO v4 (Telegram'dan yönetim + 30-gün dibi + haftalık rapor)
=======================================================================================
Motor    : Playwright (Chromium, kalıcı profil, stealth) → JS'li Türk sitelerinde çalışır
Bildirim : 1) Telegram Bot API   2) başarısızsa CallMeBot API (WhatsApp'a düşen yedek)
Durum    : state.json → mükerrer bildirim yok (cooldown + tekrar-düşüş)
Geçmiş   : fiyat_gecmisi.csv → grafik.py ile HTML grafik + PNG rapor

v4 ile gelenler (v3 üzerine):
  • Telegram'dan ürün yönetimi: /liste /ekle /sil /hedef — bilgisayara dokunmadan.
    Değişiklikler telegram_urunler.yaml'a yazılır (products.yaml'ına dokunulmaz),
    izleyiciler CANLI güncellenir, yeniden başlatma gerekmez.
  • "30 günün en düşüğü" sinyali: fiyat hedefe inmese bile son 30 günün dibini
    görünce bilgi mesajı (state'te günlük minimumlar tutulur, CSV taranmaz)
  • /durum ve heartbeat'te 7 günlük değişim yüzdesi (↓%4,2/7g gibi)
  • Haftalık grafik: belirlenen günde heartbeat ile birlikte grafik PNG olarak gelir;
    /grafik komutu da artık PNG (hızlı bakış) + HTML (etkileşimli) gönderir
  • Watchdog: calistir.bat (Windows) ve takip-botu.service (Linux/RaspberryPi) —
    bot çökerse otomatik yeniden başlar; açılışta "bot başlatıldı" mesajı
    (çökme döngüsünde mesaj spam'i engellenir)

v3'ten gelenler: Telegram birincil kanal (QR yok, tam headless), çoklu kaynak
  (Akakçe birincil, en ucuz kaynak bildirilir), Akakçe satıcı adı.
v2'den gelenler: JSON-LD öncelikli fiyat zinciri, şüpheli fiyat için ikinci-okuma
  doğrulaması, site bazlı kuyruk + captcha'da üstel geri çekilme, heartbeat,
  hata serisi uyarısı, sekme aç-kapat (RAM disiplini).

Kurulum:
  pip install -r requirements.txt && playwright install chromium
  1) Telegram'da @BotFather'a /newbot yaz → token'ı products.yaml'a koy
  2) Botuna /start yaz → python takip_botu_pro.py chatid → çıkan id'yi yaml'a koy
  3) python takip_botu_pro.py test → test mesajı gelmeli

Çalıştırma:
  python takip_botu_pro.py           → normal çalışma
  python3 guncelleyici.py            → 7/24 ÖNERİLEN: watchdog + git'ten otomatik
                                       güncelleme + bozuk push geri alma (README)
  python takip_botu_pro.py once      → tüm ürünleri BİR KEZ kontrol et, bildirim atma
  python takip_botu_pro.py test      → Telegram'a test mesajı gönder
  python takip_botu_pro.py chatid    → chat_id'ni öğren
  python takip_botu_pro.py grafik    → fiyat_grafigi.html üret
  python takip_botu_pro.py smoke     → sağlık kontrolü (gözetmen kullanır; çıkış 0=OK)
"""

import asyncio
import csv
import json
import logging
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import yaml
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Page, BrowserContext

# ===================== YOLLAR =====================
BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_YAML = BASE_DIR / "products.yaml"
TELEGRAM_URUNLER = BASE_DIR / "telegram_urunler.yaml"   # /ekle /sil /hedef buraya yazar
KURULUM_YAML = BASE_DIR / "kurulum.yaml"                # kurulum sihirbazı buraya yazar
SITES_YAML = BASE_DIR / "sites.yaml"
STATE_FILE = BASE_DIR / "state.json"
HISTORY_CSV = BASE_DIR / "fiyat_gecmisi.csv"
GRAFIK_HTML = BASE_DIR / "fiyat_grafigi.html"
GRAFIK_PNG = BASE_DIR / "fiyat_grafigi.png"
USER_DATA_DIR = str(BASE_DIR / ".chrome-profile-bot")
LOG_FILE = BASE_DIR / "takip.log"
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
if os.getenv("STOCKBOT_DEBUG", "false").lower() == "true":
    logging.getLogger().setLevel(logging.DEBUG)

# Bot koruması / captcha sayfası işaretleri (başlık + gövdenin ilk kısmında aranır)
BLOCK_MARKERS = [
    "robot check", "captcha", "erişim engellendi", "access denied",
    "olağandışı trafik", "unusual traffic", "attention required",
    "checking your browser", "doğrulama gerekiyor",
    "just a moment",             # Cloudflare ara sayfası (tebilon vb.)
    "hepsiburada | güvenlik",    # Hepsiburada bot duvarı (başlık)
]


# ===================== TL FİYAT PARSER =====================

def parse_try_amount(s) -> float | None:
    """TL fiyat metnini sayıya çevirir. Kritik ayrım:
    '53.599 TL'    → nokta binlik ayracı  → 53599.0
    '53.599,50 TL' → virgül ondalık       → 53599.5
    '599.5 TL'     → nokta ondalık        → 599.5
    Kural: hem nokta hem virgül varsa SONDAKİ işaret ondalıktır;
    tek işaret varsa ve sonrasında tam 3 hane varsa binliktir."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        v = float(s)
        return v if 0 < v < 100_000_000 else None
    clean = re.sub(r"[^\d.,]", "", str(s))
    if not clean or not any(c.isdigit() for c in clean):
        return None
    if "," in clean and "." in clean:
        if clean.rfind(",") > clean.rfind("."):     # 53.599,50
            clean = clean.replace(".", "").replace(",", ".")
        else:                                       # 53,599.50 (EN yerelli site)
            clean = clean.replace(",", "")
    elif clean.count(",") > 1:                      # 1,053,599
        clean = clean.replace(",", "")
    elif "," in clean:                              # 53599,50 | 53,599
        head, tail = clean.split(",")
        clean = head + tail if len(tail) == 3 else head + "." + tail
    elif clean.count(".") > 1:                      # 1.053.599
        clean = clean.replace(".", "")
    elif "." in clean:                              # 53.599 → binlik | 599.5 → ondalık
        head, tail = clean.split(".")
        if len(tail) == 3:
            clean = head + tail
    try:
        v = float(clean)
    except ValueError:
        return None
    return v if 0 < v < 100_000_000 else None


def tl(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " TL"


def pct(v: float) -> str:
    """7 günlük değişim gösterimi: -4.23 → '↓%4,2', 2.1 → '↑%2,1'."""
    ok = "↓" if v < 0 else "↑"
    return f"{ok}%{abs(v):.1f}".replace(".", ",")


# ===================== YAML / ÜRÜN LİSTESİ =====================

def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def product_key(prod: dict) -> str:
    """State anahtarı: etiket (çoklu kaynakta URL tek başına ürünü temsil etmez)."""
    return prod.get("label") or product_urls(prod)[0]


def product_urls(prod: dict) -> list[str]:
    """'urls' listesi varsa onu, yoksa tekil 'url'i döndürür. Sıra önemlidir:
    Akakçe birincil kurgusunda ilk eleman Akakçe linkidir."""
    urls = [u for u in (prod.get("urls") or []) if u]
    return urls or [prod["url"]]


def load_products() -> list[dict]:
    """products.yaml + telegram_urunler.yaml birleşimi. Telegram'dan yapılan
    ekleme/silme/hedef değişikliği ayrı dosyada tutulur ki kullanıcının elle
    düzenlediği products.yaml (yorumlarıyla birlikte) hiç bozulmasın."""
    cfg = load_yaml(PRODUCTS_YAML)
    products = [dict(p) for p in cfg.get("products", []) if p.get("enabled", True)]
    tg = load_yaml(TELEGRAM_URUNLER) if TELEGRAM_URUNLER.exists() else {}
    for p in (tg.get("eklenen") or []):
        products.append(dict(p))
    kaldirilan = set(tg.get("kaldirilan") or [])
    products = [p for p in products if product_key(p) not in kaldirilan]
    hedefler = tg.get("hedefler") or {}
    kaynaklar = tg.get("ek_kaynaklar") or {}
    for p in products:
        k = product_key(p)
        if k in hedefler:
            p["price_threshold_tl"] = float(hedefler[k])
        # /akakce ile bağlanan ek kaynaklar LİSTENİN BAŞINA gelir (Akakçe birincil)
        if k in kaynaklar:
            ek = [u for u in kaynaklar[k] if u]
            p["urls"] = ek + [u for u in product_urls(p) if u not in ek]
    return products


def _tg_dosya() -> dict:
    d = load_yaml(TELEGRAM_URUNLER) if TELEGRAM_URUNLER.exists() else {}
    d.setdefault("eklenen", [])
    d.setdefault("kaldirilan", [])
    d.setdefault("hedefler", {})
    d.setdefault("ek_kaynaklar", {})
    return d


def tg_kaynak_ekle(key: str, url: str) -> None:
    """Ürüne ek kaynak bağlar (Akakçe linki başa gelir → birincil kaynak olur)."""
    d = _tg_dosya()
    lst = d["ek_kaynaklar"].setdefault(key, [])
    if url not in lst:
        lst.insert(0, url)
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def tg_urun_ekle(label: str, url: str, hedef: float) -> dict:
    d = _tg_dosya()
    prod = {"label": label, "url": url, "mode": "price",
            "price_threshold_tl": hedef, "cooldown_minutes": 1440,
            "sleep_min": 900, "sleep_max": 1800}
    d["eklenen"].append(prod)
    if label in d["kaldirilan"]:
        d["kaldirilan"].remove(label)
    save_yaml_atomic(TELEGRAM_URUNLER, d)
    return prod


def tg_urun_sil(key: str) -> None:
    """Telegram'dan eklenen ürünü listeden çıkarır; products.yaml ürünüyse
    'kaldirilan' listesine yazarak devre dışı bırakır (dosyaya dokunmadan)."""
    d = _tg_dosya()
    once = len(d["eklenen"])
    d["eklenen"] = [p for p in d["eklenen"] if product_key(p) != key]
    if len(d["eklenen"]) == once and key not in d["kaldirilan"]:
        d["kaldirilan"].append(key)
    d["hedefler"].pop(key, None)
    d["ek_kaynaklar"].pop(key, None)
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def tg_hedef_degistir(key: str, hedef: float) -> None:
    d = _tg_dosya()
    for p in d["eklenen"]:
        if product_key(p) == key:
            p["price_threshold_tl"] = hedef
            break
    else:
        d["hedefler"][key] = hedef
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def baslik_temizle(t: str) -> str:
    """Sayfa başlığını etikete çevirir: '|' / ' - ' sonrası site adı kırpılır."""
    for ayrac in (" | ", "|", " – ", " — ", " - "):
        if ayrac in t:
            t = t.split(ayrac)[0]
    return " ".join(t.split()).strip()[:60]


def etiket_uret(url: str) -> str:
    """/ekle'de etiket verilmezse URL'den okunaklı bir ad türetir."""
    pr = urlparse(url)
    seg = [s for s in pr.path.split("/") if s]
    ad = seg[-1] if seg else pr.netloc
    ad = re.sub(r"\.(html?|php|aspx?)$", "", ad)
    ad = re.sub(r",\d+$", "", ad)                 # akakce ",1234567" son eki
    ad = re.sub(r"[-_+]", " ", ad)
    ad = re.sub(r"\s+", " ", ad).strip()[:48]
    return ad or pr.netloc


# ===================== DURUM / GEÇMİŞ =====================

class State:
    """state.json — mükerrer bildirim engelleme + son iyi fiyat + günlük minimumlar.
    Atomik yazılır (tmp + replace): bot yazma sırasında ölse bile dosya bozulmaz."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    def get(self, key: str) -> dict:
        return self.data.setdefault(key, {})

    async def save(self) -> None:
        async with self.lock:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)


HISTORY_LOCK = asyncio.Lock()


async def append_history(label: str, site: str, price: float | None,
                         in_stock, source: str) -> None:
    async with HISTORY_LOCK:
        yeni = not HISTORY_CSV.exists()
        with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if yeni:
                w.writerow(["zaman", "urun", "site", "fiyat", "stok", "kaynak"])
            w.writerow([
                datetime.now().isoformat(timespec="seconds"),
                label,
                site,
                f"{price:.2f}" if price is not None else "",
                {True: "1", False: "0"}.get(in_stock, ""),
                source,
            ])


def csv_etiket_degistir(eski: str, yeni: str) -> None:
    """fiyat_gecmisi.csv'deki ürün adını günceller (atomik: tmp + replace)."""
    if not HISTORY_CSV.exists():
        return
    with open(HISTORY_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    for r in rows:
        if len(r) >= 2 and r[1] == eski:
            r[1] = yeni
    tmp = HISTORY_CSV.with_suffix(".csv.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter=";").writerows(rows)
    os.replace(tmp, HISTORY_CSV)


def etiket_gocu(state: "State", products: list[dict]) -> bool:
    """Etiket değişse de geçmiş kaybolmasın. state/CSV/hedefler etikete göre
    anahtarlıdır (çoklu kaynakta URL ürünü temsil etmez); ürünü yeniden
    adlandırmak cooldown'u, günlük minimumları ve grafik geçmişini sıfırlıyordu.
    Eşleştirme URL parmak iziyle: izleyici her turda ürünün URL'lerini state'e
    yazar; sahipsiz kalan eski kayıt, URL'leri kesişen ve kendi kaydı olmayan
    yeni ürüne aktarılır. True dönerse ürün listesi yeniden yüklenmelidir
    (taşınan hedef/ek-kaynak overlay'i uygulansın diye)."""
    mevcut = {product_key(p) for p in products}
    tasindi = False
    for eski in [k for k in state.data if not k.startswith("_") and k not in mevcut]:
        eski_urls = set(state.data[eski].get("urls") or [])
        if not eski_urls:
            continue
        for p in products:
            yeni = product_key(p)
            if yeni in state.data or not eski_urls & set(product_urls(p)):
                continue
            state.data[yeni] = state.data.pop(eski)
            csv_etiket_degistir(eski, yeni)
            d = _tg_dosya()
            degisti = False
            for alan in ("hedefler", "ek_kaynaklar"):
                if eski in d[alan]:
                    d[alan][yeni] = d[alan].pop(eski)
                    degisti = True
            if degisti:
                save_yaml_atomic(TELEGRAM_URUNLER, d)
            logging.info(f"Etiket değişikliği algılandı: '{eski}' → '{yeni}' — "
                         "geçmiş taşındı (state + CSV + hedef/ek-kaynak).")
            tasindi = True
            break
    return tasindi


# --- Günlük minimum takibi: 30-gün-dibi sinyali + 7 günlük trend buradan beslenir ---

def gunluk_min_guncelle(st: dict, fp: float) -> None:
    """Bugünün en düşük okumasını state'e işler, 35 günden eskiyi budar."""
    dmin = st.setdefault("daily_min", {})
    bugun = date.today().isoformat()
    dmin[bugun] = min(fp, dmin.get(bugun, fp))
    sinir = (date.today() - timedelta(days=35)).isoformat()
    for g in [g for g in dmin if g < sinir]:
        del dmin[g]


def dip30_oncesi(st: dict) -> tuple[float | None, int]:
    """Bugün HARİÇ son 30-35 günün en düşük fiyatı + kaç günlük veri olduğu."""
    dmin = st.get("daily_min") or {}
    bugun = date.today().isoformat()
    onceki = [v for g, v in dmin.items() if g != bugun]
    if not onceki:
        return None, 0
    return min(onceki), len(onceki)


def yedi_gun_degisim(st: dict) -> float | None:
    """Son iyi fiyatın ~7 gün önceki günlük minimuma göre % değişimi."""
    fp = st.get("last_good_price")
    dmin = st.get("daily_min") or {}
    if not fp or not dmin:
        return None
    bugun = date.today()
    adaylar = []
    for g, v in dmin.items():
        try:
            yas = (bugun - date.fromisoformat(g)).days
        except ValueError:
            continue
        if 4 <= yas <= 10:
            adaylar.append((abs(yas - 7), v))
    if not adaylar:
        return None
    eski = min(adaylar)[1]
    if eski <= 0:
        return None
    return (fp - eski) / eski * 100


# ===================== SİTE STRATEJİLERİ =====================

class Sites:
    def __init__(self, raw: dict):
        self.sites = raw.get("sites", {})
        self.default = self.sites.get("default", {})

    def strategy(self, host: str) -> dict:
        for domain, strat in self.sites.items():
            if domain != "default" and domain in host:
                return {**self.default, **(strat or {})}
        return dict(self.default)


async def apply_stealth(page: Page) -> None:
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['tr-TR','tr','en-US','en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    """)
    await page.set_extra_http_headers({
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })


# ===================== SİTE BAZLI KUYRUK + GERİ ÇEKİLME =====================

class HostThrottle:
    """Aynı siteye istekleri sıralar: 4 Amazon ürünü aynı anda gitmez, aralarında
    min_gap + jitter olur. Captcha/engel görülürse o siteye üstel geri çekilme."""

    def __init__(self, min_gap: float = 25.0):
        self.min_gap = min_gap
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.next_ok: dict[str, float] = defaultdict(float)      # monotonic zaman
        self.backoff: dict[str, float] = defaultdict(float)      # saniye

    def slot(self, host: str):
        return _ThrottleSlot(self, host)

    def penalize(self, host: str) -> float:
        """Engel algılandı → geri çekilmeyi büyüt (5 dk → 10 → ... → 60 dk)."""
        self.backoff[host] = min(max(self.backoff[host] * 2, 300), 3600)
        self.next_ok[host] = time.monotonic() + self.backoff[host]
        return self.backoff[host]

    def reward(self, host: str) -> None:
        """Başarılı okuma → geri çekilmeyi sıfırla."""
        self.backoff[host] = 0.0


class _ThrottleSlot:
    def __init__(self, throttle: HostThrottle, host: str):
        self.t = throttle
        self.host = host

    async def __aenter__(self):
        await self.t.locks[self.host].acquire()
        wait = self.t.next_ok[self.host] - time.monotonic()
        if wait > 0:
            logging.debug(f"[{self.host}] site kuyruğu: {wait:.0f} sn bekleniyor")
            await asyncio.sleep(wait)
        return self

    async def __aexit__(self, *exc):
        # bir sonraki aynı-site isteği için minimum aralık + jitter
        gap = self.t.min_gap * random.uniform(0.8, 1.6)
        self.t.next_ok[self.host] = max(
            self.t.next_ok[self.host], time.monotonic() + gap)
        self.t.locks[self.host].release()
        return False


# ===================== SAYFA OKUMA =====================

def _jsonld_iter(node):
    """JSON-LD içinde gezinir: listeler, @graph, iç içe yapılar.
    'object': mediamarkt gibi siteler Product'ı BuyAction.object içine gömer."""
    if isinstance(node, list):
        for x in node:
            yield from _jsonld_iter(x)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement", "item", "offers",
                    "object"):
            if key in node:
                yield from _jsonld_iter(node[key])


def _price_from_jsonld_obj(obj: dict) -> float | None:
    """Bir JSON-LD düğümünden fiyat çeker. TRY dışındaki para birimlerini reddeder
    (bazı siteler USD fiyat da gömer)."""
    for key in ("price", "lowPrice"):
        raw = obj.get(key)
        if raw is None:
            continue
        cur = str(obj.get("priceCurrency") or "").upper()
        if cur and cur not in ("TRY", "TL"):
            continue
        v = parse_try_amount(raw)
        if v:
            return v
    return None


async def get_price(page: Page, strat: dict) -> tuple[float | None, str]:
    """Öncelik: JSON-LD → siteye özel seçici → genel seçiciler → meta → gövde regex.
    Dönen 'kaynak' alanı güven seviyesidir; 'regex' düşük güven demektir ve
    bildirimden önce ikinci okumayla doğrulanır."""
    # 1) JSON-LD (en güvenilir: sitenin kendi yapılandırılmış verisi)
    try:
        for s in await page.locator('script[type="application/ld+json"]').all():
            try:
                data = json.loads(await s.inner_text())
            except Exception:
                continue
            for obj in _jsonld_iter(data):
                # Ana ürün düğümlerine öncelik: Product tipi veya offers taşıyan düğüm
                t = str(obj.get("@type") or "")
                if "Product" in t or "Offer" in t or "offers" in obj:
                    v = _price_from_jsonld_obj(obj)
                    if v:
                        return v, "json-ld"
    except Exception:
        pass

    # 2) Siteye özel + genel seçiciler
    adaylar = []
    if strat.get("price_selector"):
        adaylar.append(strat["price_selector"])
    adaylar += [".product-price", "#product-price", ".price-value", "span.price",
                ".current-price", "[data-price-amount]", "span[itemprop=price]"]
    for sel in adaylar:
        try:
            locs = page.locator(sel)
            for i in range(min(await locs.count(), 5)):
                el = locs.nth(i)
                if not await el.is_visible():
                    continue
                v = parse_try_amount(await el.inner_text())
                if v:
                    return v, "seçici"
                v = parse_try_amount(await el.get_attribute("content"))
                if v:
                    return v, "seçici"
        except Exception:
            continue

    # 3) Meta tag'ler
    for meta_sel in ('meta[property="product:price:amount"]',
                     'meta[property="og:price:amount"]',
                     'meta[itemprop="price"]'):
        try:
            meta = await page.locator(meta_sel).first.get_attribute("content", timeout=1500)
            v = parse_try_amount(meta)
            if v:
                return v, "meta"
        except Exception:
            continue

    # 4) Son çare: gövdedeki ilk '12.345,67 TL' kalıbı — DÜŞÜK GÜVEN
    # (önerilen ürün fiyatını yakalayabilir; bu yüzden bildirim öncesi doğrulanır)
    # sites.yaml → text_scope: regex'in bakacağı alanı daraltır (örn. Amazon'da
    # "#centerCol" — sponsorlu ürün karuselinin fiyatını ana fiyat sanmasın).
    # Kapsam elementi sayfada yoksa regex adımı ATLANIR: fiyatsız sayfada
    # (stok yok vb.) yanlış fiyat okumaktansa "yok" demek daha doğru.
    try:
        scope = strat.get("text_scope")
        body = await page.locator(scope or "body").first.inner_text(timeout=3000)
        m = re.search(r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+,\d{2})\s*(?:TL|₺)", body)
        if m:
            logging.debug("Fiyat gövde regex ile bulundu (düşük güven).")
            return parse_try_amount(m.group(1)), "regex"
    except Exception:
        pass
    return None, "yok"


async def get_seller(page: Page, strat: dict) -> str | None:
    """Akakçe gibi karşılaştırma sitelerinde en ucuz satıcının adını çeker.
    sites.yaml → seller_selector; bulunamazsa sessizce None."""
    sel = strat.get("seller_selector")
    if not sel:
        return None
    try:
        el = page.locator(sel).first
        if await el.count() == 0:
            return None
        text = (await el.inner_text() or "").strip()
        if not text:
            for attr in ("alt", "title", "aria-label"):
                text = (await el.get_attribute(attr) or "").strip()
                if text:
                    break
        text = " ".join(text.split())
        return text[:60] if text else None
    except Exception:
        return None


AKAKCE_ARAMA = "https://www.akakce.com/arama/?q={}"


async def _akakce_sonuc_ayikla(page: Page) -> list[dict]:
    """Açık Akakçe arama sayfasından ürün sayfası linklerini toplar.
    Sayfa yapısına değil, Akakçe'nin değişmez URL kalıbına dayanır:
    ürün sayfaları daima '...-fiyati,ID.html' biçimindedir."""
    out, gorulen = [], set()
    anchors = page.locator("a[href*='fiyati,']")
    for i in range(min(await anchors.count(), 25)):
        try:
            a = anchors.nth(i)
            href = await a.get_attribute("href") or ""
            if not re.search(r"fiyati,\d+\.html", href):
                continue
            if href.startswith("/"):
                href = "https://www.akakce.com" + href
            href = href.split("?")[0]
            if href in gorulen:
                continue
            ad = " ".join(((await a.inner_text()) or "").split()).strip()
            if not ad:
                ad = (await a.get_attribute("title") or "").strip()
            if not ad:
                continue
            gorulen.add(href)
            out.append({"ad": ad[:70], "url": href})
            if len(out) == 3:
                break
        except Exception:
            continue
    return out


async def akakce_ara(context: BrowserContext, throttle: HostThrottle,
                     query: str, arama_url: str | None = None) -> list[dict]:
    """Akakçe'de ürün adıyla arar, ilk 3 ürün sayfasını döndürür.
    /akakce komutu bunları buton yapar, kullanıcı doğrusunu seçer."""
    url = arama_url or AKAKCE_ARAMA.format(quote_plus(query))
    host = urlparse(url).netloc or "www.akakce.com"
    async with throttle.slot(host):
        page = await context.new_page()
        try:
            await apply_stealth(page)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PWTimeout:
                pass
            await asyncio.sleep(2.5)
            return await _akakce_sonuc_ayikla(page)
        finally:
            await page.close()


async def detect_block(page: Page) -> bool:
    """Captcha / bot koruması / erişim engeli sayfası mı?"""
    try:
        title = (await page.title() or "").lower()
        if any(m in title for m in BLOCK_MARKERS):
            return True
        body = (await page.locator("body").inner_text(timeout=2000))[:3000].lower()
        return any(m in body for m in BLOCK_MARKERS)
    except Exception:
        return False


async def check_add_to_cart(page: Page, strat: dict) -> bool | None:
    """True=stokta, False=pasif/tükendi, None=buton hiç bulunamadı (bilinmiyor)."""
    seciciler = [strat.get("add_button") or "button:has-text('Sepete Ekle')",
                 "button:has-text('SEPETE EKLE')", "button:has-text('Add to Cart')",
                 "#add-to-cart-button", "button.add-to-basket", "button.add-to-cart"]
    btn = None
    for sel in seciciler:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                btn = loc
                break
        except Exception:
            continue
    if btn is None:
        return None
    try:
        if not await btn.is_visible():
            return False
        if await btn.is_disabled():
            return False
        cls = (await btn.get_attribute("class") or "").lower()
        if any(x in cls for x in ["disabled", "passive", "sold-out", "out-of-stock",
                                  "unavailable", "not-available"]):
            return False
        return True
    except Exception:
        return None


async def select_variant(page: Page, targets: list, strat: dict) -> tuple[bool, str]:
    """Beden/varyant seçer. ['*'] veya boşsa atlanır. Pasif (tükendi) seçenekleri eler."""
    if not targets or "*" in targets or "ANY" in targets:
        return True, "AUTO"
    hedefler = [str(t).upper() for t in targets]

    trigger = strat.get("size_trigger")
    if trigger:
        try:
            t = page.locator(trigger).first
            if await t.count() > 0 and await t.is_visible():
                await t.click()
                await asyncio.sleep(1.2)
        except Exception:
            pass

    seciciler = [strat.get("size_items") or "button, li, [role='button']",
                 "button", "li", "span", "[data-variant-name]"]
    for sel in seciciler:
        try:
            locs = page.locator(sel)
            for i in range(min(await locs.count(), 80)):
                el = locs.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    text = (await el.inner_text()).strip()
                    if not text:
                        for attr in ("title", "data-value", "aria-label", "value"):
                            text = (await el.get_attribute(attr) or "").strip()
                            if text:
                                break
                    if not text or len(text) > 40:
                        continue
                    tu = text.upper()
                    eslesme = any(
                        h == tu or f" {h} " in f" {tu} "
                        or (h.replace(".", "").isdigit() and h == re.sub(r"[^\d.]", "", tu))
                        for h in hedefler
                    )
                    if not eslesme:
                        continue
                    cls = (await el.get_attribute("class") or "").lower()
                    if any(x in cls for x in ["disabled", "sold-out", "passive",
                                              "out-of-stock", "unavailable", "tukendi"]):
                        continue
                    if await el.is_enabled():
                        await el.scroll_into_view_if_needed()
                        await el.click(force=True, delay=random.randint(50, 150))
                        await asyncio.sleep(1)
                        return True, text
                except Exception:
                    continue
        except Exception:
            continue
    return False, ""


# ===================== BİLDİRİM KATMANI (TELEGRAM) =====================

class Notifier:
    """Birincil kanal: Telegram Bot API — düz HTTPS, tarayıcı/oturum derdi yok.
    3 deneme + üstel bekleme; hepsi patlarsa CallMeBot (WhatsApp) yedeğine düşer.
    İkisi de patlarsa ERROR log — sessiz kayıp yok."""

    def __init__(self, rq, settings: dict):
        self.rq = rq  # playwright APIRequestContext
        self.token = str(settings.get("telegram_bot_token", "") or "")
        self.chat_id = str(settings.get("telegram_chat_id", "") or "")
        self.phone = str(settings.get("phone", "")).lstrip("+")
        self.callmebot_key = str(settings.get("callmebot_apikey", "") or "")
        self.lock = asyncio.Lock()

    @property
    def api(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def tg(self, method: str, timeout_ms: int = 30000, **params):
        """Telegram API çağrısı. Başarıda 'result' döner, hatada None."""
        if not self.token:
            return None
        try:
            resp = await self.rq.post(f"{self.api}/{method}", data=params,
                                      timeout=timeout_ms)
            js = await resp.json()
            if not js.get("ok"):
                logging.warning(f"Telegram {method} hatası: {js.get('description')}")
                return None
            return js.get("result")
        except Exception as e:
            logging.warning(f"Telegram {method} isteği başarısız: {e}")
            return None

    async def check_bot(self) -> bool:
        me = await self.tg("getMe")
        if me:
            logging.info(f"Telegram botu hazır: @{me.get('username')}")
            if not self.chat_id:
                logging.warning("telegram_chat_id boş! Bota /start yaz, sonra: "
                                "python takip_botu_pro.py chatid")
            return True
        logging.error("Telegram botuna ulaşılamadı — products.yaml → "
                      "telegram_bot_token alanını kontrol et.")
        return False

    async def _send_telegram(self, text: str, chat_id: str | None = None) -> bool:
        cid = chat_id or self.chat_id
        if not self.token or not cid:
            return False
        for bekle in (0, 2, 4):
            if bekle:
                await asyncio.sleep(bekle)
            r = await self.tg("sendMessage", chat_id=cid, text=text,
                              disable_web_page_preview=True)
            if r:
                return True
        return False

    async def _send_callmebot(self, text: str) -> bool:
        if not self.callmebot_key or not self.phone:
            return False
        try:
            url = (f"https://api.callmebot.com/whatsapp.php?phone=%2B{self.phone}"
                   f"&text={quote_plus(text)}&apikey={quote_plus(self.callmebot_key)}")
            resp = await self.rq.get(url, timeout=30000)
            if not resp.ok:
                return False
            body = (await resp.text()).lower()
            return "invalid" not in body and "error" not in body
        except Exception as e:
            logging.warning(f"CallMeBot gönderimi başarısız: {e}")
            return False

    async def send(self, text: str) -> bool:
        async with self.lock:
            if await self._send_telegram(text):
                logging.info("Bildirim Telegram ile gönderildi ✔")
                return True
            if await self._send_callmebot(text):
                logging.info("Bildirim CallMeBot (yedek kanal) ile gönderildi ✔")
                return True
            logging.error("BİLDİRİM GÖNDERİLEMEDİ! (Telegram + CallMeBot ikisi de başarısız)")
            return False

    async def _send_file(self, method: str, field: str, path: Path,
                         mime: str, caption: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        try:
            resp = await self.rq.post(f"{self.api}/{method}", multipart={
                "chat_id": self.chat_id,
                "caption": caption,
                field: {"name": path.name, "mimeType": mime,
                        "buffer": path.read_bytes()},
            }, timeout=120000)
            js = await resp.json()
            if not js.get("ok"):
                logging.warning(f"Telegram {method} hatası: {js.get('description')}")
            return bool(js.get("ok"))
        except Exception as e:
            logging.warning(f"Telegram dosya gönderimi başarısız: {e}")
            return False

    async def send_document(self, path: Path, caption: str = "") -> bool:
        mime = {".csv": "text/csv", ".html": "text/html"}.get(
            path.suffix.lower(), "application/octet-stream")
        return await self._send_file("sendDocument", "document", path, mime, caption)

    async def send_photo(self, path: Path, caption: str = "") -> bool:
        return await self._send_file("sendPhoto", "photo", path, "image/png", caption)

    async def send_buttons(self, text: str, buttons: list[list[dict]]) -> bool:
        """Tıklanabilir buton satırlarıyla mesaj (hedef fiyat seçimi için)."""
        r = await self.tg("sendMessage", chat_id=self.chat_id, text=text,
                          disable_web_page_preview=True,
                          reply_markup={"inline_keyboard": buttons})
        return bool(r)


# ===================== KONTROL + KARAR =====================

async def check_once(context: BrowserContext, sites: Sites, throttle: HostThrottle,
                     prod: dict, url: str) -> dict:
    """Tek kaynağın tek kontrolü. Site kuyruğuna girer, sekme açar, okur, KAPATIR."""
    host = urlparse(url).netloc
    strat = sites.strategy(host)
    sonuc = {"url": url, "host": host.replace("www.", ""),
             "price": None, "source": "yok", "seller": None, "title": "",
             "in_stock": None, "variant_ok": True, "variant": "AUTO", "blocked": False}

    async with throttle.slot(host):
        page = await context.new_page()
        try:
            await apply_stealth(page)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PWTimeout:
                logging.warning(f"[{prod.get('label')}] sayfa yükleme zaman aşımı, "
                                "mevcut haliyle okunuyor...")
            await page.mouse.move(random.randint(80, 500), random.randint(80, 500))
            await asyncio.sleep(strat.get("wait_after_page_load", 3))
            try:
                sonuc["title"] = (await page.title() or "").strip()
            except Exception:
                pass

            if await detect_block(page):
                sonuc["blocked"] = True
                ceza = throttle.penalize(host)
                logging.warning(f"[{prod.get('label')}] {host} bot koruması algılandı! "
                                f"Bu siteye {ceza/60:.0f} dk geri çekilme uygulanıyor.")
                return sonuc

            mode = prod.get("mode", "price")
            if "stock" in mode:
                sonuc["variant_ok"], sonuc["variant"] = await select_variant(
                    page, prod.get("target_variants", ["*"]), strat)
                sonuc["in_stock"] = await check_add_to_cart(page, strat)
            if "price" in mode:
                sonuc["price"], sonuc["source"] = await get_price(page, strat)
                sonuc["seller"] = await get_seller(page, strat)
            throttle.reward(host)
        finally:
            await page.close()
    return sonuc


async def check_product(context: BrowserContext, sites: Sites, throttle: HostThrottle,
                        prod: dict) -> list[dict]:
    """Ürünün TÜM kaynaklarını kontrol eder, okuma başına geçmişe yazar.
    Bir kaynağın hatası diğerlerini iptal etmez (Akakçe birincil kurgusunda
    tek kaynağın arızası ürünü kör etmesin); AMA tüm kaynaklar hata verirse
    hata yukarı fırlatılır ki izleyicinin error_streak sayacı çalışsın."""
    label = prod.get("label", "Ürün")
    sonuclar = []
    son_hata: Exception | None = None
    for url in product_urls(prod):
        try:
            s = await check_once(context, sites, throttle, prod, url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            son_hata = e
            logging.warning(f"[{label}] kaynak okunamadı "
                            f"({urlparse(url).netloc}): {type(e).__name__}: {e}")
            continue
        sonuclar.append(s)
        if not s["blocked"] and s["price"] is not None:
            await append_history(label, s["host"], s["price"],
                                 s["in_stock"], s["source"])
    if not sonuclar and son_hata is not None:
        raise son_hata
    return sonuclar


def en_iyi_kaynak(prod: dict, sonuclar: list[dict]) -> dict | None:
    """Kaynaklar arasından bildirime esas olanı seçer: stok modunda stoklu olan,
    fiyat modunda EN UCUZ fiyatlı olan (Akakçe birincil kurgusunun kalbi)."""
    adaylar = [s for s in sonuclar if not s["blocked"]]
    if not adaylar:
        return None
    mode = prod.get("mode", "price")
    if "stock" in mode:
        stoklu = [s for s in adaylar if s["variant_ok"] and s["in_stock"]]
        havuz = stoklu or adaylar
    else:
        havuz = adaylar
    fiyatli = [s for s in havuz if s["price"] is not None]
    if fiyatli:
        return min(fiyatli, key=lambda s: s["price"])
    return havuz[0]


def alarm_gerekli(prod: dict, s: dict) -> tuple[bool, str]:
    mode = prod.get("mode", "price")
    thr = float(prod.get("price_threshold_tl", 0))
    if mode == "price":
        if s["price"] is not None and s["price"] <= thr:
            return True, f"💰 {tl(s['price'])} (hedef {tl(thr)})"
        return False, ""
    if mode == "stock":
        if s["variant_ok"] and s["in_stock"]:
            return True, f"📦 STOKTA — beden: {s['variant']}"
        return False, ""
    # price+stock
    if (s["variant_ok"] and s["in_stock"]
            and s["price"] is not None and s["price"] <= thr):
        return True, (f"📦 STOKTA + 💰 {tl(s['price'])} (hedef {tl(thr)}) "
                      f"— beden: {s['variant']}")
    return False, ""


def fiyat_suphali(prod: dict, s: dict, st: dict) -> bool:
    """Parse hatası ihtimali: doğrulanmamış regex okuması, hedefin yarısından da
    ucuz, veya son iyi fiyata göre %40'tan fazla ani düşüş → önce doğrula, sonra
    bildir. Sadece hedef alarmını değil, 30-gün-dibi sinyalini ve state'e yazılan
    fiyatı da korur (bozuk fiyat daily_min'e girerse 35 gün gerçek dibi maskeler)."""
    fp = s["price"]
    if fp is None:
        return False
    son_iyi = st.get("last_good_price")
    if s["source"] == "regex":
        # Son iyi fiyata ±%5 yakınsa geçmiş bu okumayı doğruluyor demektir;
        # sapıyorsa (veya ilk okumaysa) ikinci okuma şart. Böylece sürekli
        # regex'te kalan ürünler her turda yeniden doğrulanmaz.
        if not son_iyi or abs(fp - son_iyi) > son_iyi * 0.05:
            return True
    thr = float(prod.get("price_threshold_tl", 0))
    if thr and fp < thr * 0.5:
        return True
    if son_iyi and fp < son_iyi * 0.6:
        return True
    return False


# ===================== ÜRÜN İZLEYİCİ =====================

async def product_watcher(context: BrowserContext, sites: Sites, throttle: HostThrottle,
                          notifier: Notifier, prod: dict, state: State,
                          sem: asyncio.Semaphore, settings: dict) -> None:
    label = prod.get("label", "Ürün")
    key = product_key(prod)
    cooldown = timedelta(minutes=int(prod.get("cooldown_minutes", 1440)))
    renotify_drop = float(prod.get("renotify_drop_pct",
                                   settings.get("renotify_drop_pct", 3)))
    sanity_guard = bool(settings.get("sanity_guard", True))
    error_alert_streak = int(settings.get("error_alert_streak", 5))
    low30_alert = bool(settings.get("low30_alert", True))
    low30_min_days = int(settings.get("low30_min_days", 7))
    low30_cooldown = timedelta(minutes=int(settings.get("low30_cooldown_minutes", 1440)))

    while True:
        quick_recheck = False
        async with sem:
            st = state.get(key)
            st["urls"] = product_urls(prod)   # etiket göçü için URL parmak izi
            try:
                sonuclar = await check_product(context, sites, throttle, prod)
                best = en_iyi_kaynak(prod, sonuclar)
                hepsi_engelli = all(s["blocked"] for s in sonuclar)

                if best is not None and not hepsi_engelli:
                    fp = best["price"]
                    logging.info(f"[{label}] en iyi: {tl(fp)} ({best['host']}, "
                                 f"{best['source']}) stok={best['in_stock']} "
                                 f"varyant={best['variant']}")

                    # Fiyat modunda hiçbir kaynak fiyat okuyamadıysa bu da bir
                    # arıza belirtisidir → hata serisine say
                    mode = prod.get("mode", "price")
                    if "price" in mode and fp is None:
                        st["error_streak"] = int(st.get("error_streak", 0)) + 1
                        if st["error_streak"] == error_alert_streak:
                            await notifier.send(
                                f"⚠️ {label} üst üste {error_alert_streak} kontroldür "
                                "fiyat okunamıyor.\nSite değişmiş olabilir — "
                                "sites.yaml seçicisini kontrol et.")
                    else:
                        st["error_streak"] = 0

                    gerekli, detay = alarm_gerekli(prod, best)
                    # supheli bilerek 'gerekli'den bağımsız: şüpheli fiyat hedef
                    # alarmı tetiklemese de low30 sinyalini ve state'i kirletmesin
                    supheli = sanity_guard and fiyat_suphali(prod, best, st)

                    if supheli:
                        pend = st.get("pending_price")
                        # ikinci okuma öncekiyle ±%2 tutarlıysa fiyat gerçek kabul edilir
                        if pend and fp and abs(pend - fp) <= fp * 0.02:
                            st.pop("pending_price", None)
                            supheli = False
                            logging.info(f"[{label}] şüpheli fiyat ikinci okumayla "
                                         f"doğrulandı: {tl(fp)}")
                        else:
                            st["pending_price"] = fp
                            quick_recheck = True
                            logging.warning(f"[{label}] fiyat şüpheli görünüyor "
                                            f"({tl(fp)}, kaynak={best['source']}) — "
                                            "1-2 dk içinde ikinci okumayla doğrulanacak.")
                    else:
                        st.pop("pending_price", None)

                    if fp is not None and not supheli:
                        # 30-gün dibi bugünkü okuma işlenmeden ÖNCE hesaplanmalı
                        dip, gun_sayisi = dip30_oncesi(st)
                        gunluk_min_guncelle(st, fp)
                        st["last_good_price"] = fp
                        st["last_good_ts"] = time.time()
                        st["last_good_host"] = best["host"]

                        # 📉 Hedefe inmese bile "son 30 günün en düşüğü" bilgisi.
                        # Hedef alarmı zaten atılacaksa mükerrer mesaj atılmaz.
                        if (low30_alert and not gerekli and dip is not None
                                and gun_sayisi >= low30_min_days and fp < dip):
                            son_dip_ts = st.get("low30_notify_ts", 0)
                            if (datetime.now() - datetime.fromtimestamp(son_dip_ts)
                                    > low30_cooldown):
                                thr = prod.get("price_threshold_tl")
                                hedef_txt = f", hedef {tl(float(thr))}" if thr else ""
                                await notifier.send(
                                    f"📉 {label} son 30 günün en düşüğünde!\n"
                                    f"💰 {tl(fp)} (önceki dip {tl(dip)}{hedef_txt})\n"
                                    f"🌐 {best['host']}\n🔗 {best['url']}")
                                st["low30_notify_ts"] = time.time()
                                logging.info(f"[{label}] 30-gün dibi bildirildi: {tl(fp)}")

                    if gerekli and not supheli:
                        son_ts = st.get("last_notify_ts", 0)
                        son_fiyat = st.get("last_notify_price")
                        cooldown_gecti = (datetime.now()
                                          - datetime.fromtimestamp(son_ts) > cooldown)
                        daha_da_dustu = (fp is not None and son_fiyat
                                         and fp <= son_fiyat * (1 - renotify_drop / 100))
                        if cooldown_gecti or daha_da_dustu:
                            kaynak = best["host"]
                            if best.get("seller"):
                                kaynak += f" — satıcı: {best['seller']}"
                            msg = f"🔥 {label}\n{detay}\n🌐 {kaynak}\n🔗 {best['url']}"
                            if await notifier.send(msg):
                                st["last_notify_ts"] = time.time()
                                st["last_notify_price"] = fp
                                logging.warning(f"!!! BİLDİRİM: {label} !!!")
                        else:
                            logging.info(f"[{label}] hedefte ama cooldown sürüyor "
                                         "(mükerrer bildirim engellendi).")
                    await state.save()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"[{label}] beklenmedik hata: {type(e).__name__}: {e}")
                st["error_streak"] = int(st.get("error_streak", 0)) + 1
                # Üst üste N hata = büyük ihtimalle site değişti / seçici bozuldu → tek uyarı
                if st["error_streak"] == error_alert_streak:
                    await notifier.send(
                        f"⚠️ {label} üst üste {error_alert_streak} kontroldür okunamıyor.\n"
                        "Site değişmiş olabilir — sites.yaml seçicisini kontrol et.")
                await state.save()

        if quick_recheck:
            await asyncio.sleep(random.uniform(60, 120))
        else:
            await asyncio.sleep(random.randint(int(prod.get("sleep_min", 300)),
                                               int(prod.get("sleep_max", 600))))


class WatcherManager:
    """İzleyici görevlerini ürün anahtarına göre yönetir. Telegram'dan ürün
    eklenince/silinince/hedef değişince izleyiciler CANLI güncellenir —
    bot yeniden başlatılmaz."""

    def __init__(self, context: BrowserContext, sites: Sites, throttle: HostThrottle,
                 notifier: Notifier, state: State, settings: dict):
        self.context = context
        self.sites = sites
        self.throttle = throttle
        self.notifier = notifier
        self.state = state
        self.settings = settings
        self.sem = asyncio.Semaphore(int(settings.get("max_concurrency", 3)))
        self.tasks: dict[str, asyncio.Task] = {}

    def sync(self, products: list[dict], force: set[str] = frozenset()) -> None:
        """Ürün listesini görevlerle eşitler. force'taki anahtarlar yeniden
        başlatılır (hedef değişikliği yeni ayarla devam etsin diye)."""
        istenen = {product_key(p): p for p in products}
        for key in list(self.tasks):
            if key not in istenen or key in force:
                self.tasks.pop(key).cancel()
                logging.info(f"[{key}] izleyici durduruldu.")
        for key, p in istenen.items():
            if key not in self.tasks or self.tasks[key].done():
                self.tasks[key] = asyncio.create_task(product_watcher(
                    self.context, self.sites, self.throttle, self.notifier,
                    p, self.state, self.sem, self.settings))
                logging.info(f"[{key}] izleyici başlatıldı.")

    def cancel_all(self) -> list[asyncio.Task]:
        for t in self.tasks.values():
            t.cancel()
        return list(self.tasks.values())


# ===================== ÖZET / GRAFİK =====================

def durum_ozeti(products: list, state: State) -> str:
    """Ürün başına son bilinen fiyat + hedef + 7 günlük trend + bayatlık işareti."""
    satirlar = []
    for p in products:
        st = state.get(product_key(p))
        fp = st.get("last_good_price")
        ts = st.get("last_good_ts")
        host = st.get("last_good_host", "")
        thr = p.get("price_threshold_tl")
        bayat = ""
        if ts and time.time() - ts > 12 * 3600:
            bayat = " ⚠️ eski okuma!"
        elif not ts:
            bayat = " ⚠️ hiç okunamadı!"
        hedef = f" / hedef {tl(float(thr))}" if thr else ""
        kaynak = f" ({host})" if host else ""
        d7 = yedi_gun_degisim(st)
        trend = f" {pct(d7)}/7g" if d7 is not None else ""
        satirlar.append(f"• {p.get('label', '?')}: {tl(fp)}{hedef}{trend}{kaynak}{bayat}")
    return "\n".join(satirlar)


async def grafik_png(context: BrowserContext) -> Path:
    """fiyat_grafigi.html'i üretir, tarayıcıda açıp PNG'ye çeker (sendPhoto için)."""
    import grafik
    html = grafik.generate()
    page = await context.new_page()
    try:
        await page.set_viewport_size({"width": 900, "height": 700})
        await page.goto(html.as_uri())
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(GRAFIK_PNG), full_page=True)
    finally:
        await page.close()
    return GRAFIK_PNG


# ===================== TELEGRAM KOMUTLARI + HEARTBEAT =====================

def liste_metni(products: list) -> str:
    if not products:
        return "İzlenen ürün yok. Eklemek için:\n/ekle <link> <hedefTL> [etiket]"
    satirlar = []
    for i, p in enumerate(products, 1):
        thr = p.get("price_threshold_tl")
        hedef = f" — hedef {tl(float(thr))}" if thr else ""
        satirlar.append(f"{i}. {p.get('label', '?')}{hedef}")
    return "\n".join(satirlar)


YARDIM = ("🛒 Ürün eklemek için ürün linkini DİREKT GÖNDER yeter —\n"
          "fiyatı okur, hedefi butonla seçtiririm.\n\n"
          "Komutlar:\n"
          "/durum — son fiyatlar + 7g trend\n"
          "/liste — izlenen ürünler (numaralı)\n"
          "/sil <no> — ürünü izlemeden çıkar\n"
          "/hedef <no> <fiyatTL> — hedef fiyatı değiştir\n"
          "/akakce <no> — ürünü Akakçe'ye bağla (tüm satıcıların en ucuzu)\n"
          "/grafik — fiyat grafiği (PNG + HTML)\n"
          "/csv — ham fiyat geçmişi\n"
          "(/ekle <link> [hedefTL] [etiket] de çalışır)")


def _urun_no(parca: list[str], products: list) -> tuple[dict | None, str]:
    """'/sil 3' gibi komutlardaki numarayı ürüne çevirir; hata mesajı döndürür."""
    if len(parca) < 2 or not parca[1].isdigit():
        return None, "Numara gerekli. Önce /liste yaz, sonra örn: " + parca[0] + " 3"
    n = int(parca[1])
    if not 1 <= n <= len(products):
        return None, f"Geçersiz numara: {n}. /liste ile kontrol et (1-{len(products)})."
    return products[n - 1], ""


def _tekil_etiket(label: str, products: list) -> str:
    """Aynı etiket varsa '(2)' ekleyerek benzersizleştirir."""
    mevcut = {product_key(p) for p in products}
    if label not in mevcut:
        return label
    i = 2
    while f"{label} ({i})" in mevcut:
        i += 1
    return f"{label} ({i})"


async def urun_on_izleme(manager: WatcherManager, notifier: Notifier,
                         shared: dict, url: str) -> None:
    """Link geldi → sayfayı okur, adı/fiyatı çıkarır, hedefi butonla sordurur.
    Kullanıcıdan istenen tek şey linkti; gerisi bu akışta hallolur."""
    await notifier.send("🔎 Ürüne bakıyorum, 10-20 saniye...")
    try:
        s = await check_once(manager.context, manager.sites, manager.throttle,
                             {"label": "yeni ürün", "mode": "price"}, url)
    except Exception as e:
        await notifier.send(f"Sayfayı açamadım ({type(e).__name__}). "
                            "Linki kontrol edip tekrar gönder.")
        return
    label = _tekil_etiket(baslik_temizle(s.get("title") or "") or etiket_uret(url),
                          shared["products"])
    shared["bekleyen_urun"] = {"url": url, "fiyat": s["price"], "label": label}
    f = s["price"]
    if f and not s["blocked"]:
        rows = [[{"text": f"%3 altı → {tl(round(f * 0.97))}", "callback_data": "hedef|3"}],
                [{"text": f"%5 altı → {tl(round(f * 0.95))}", "callback_data": "hedef|5"}],
                [{"text": f"%10 altı → {tl(round(f * 0.90))}", "callback_data": "hedef|10"}],
                [{"text": "✍️ Kendim yazacağım", "callback_data": "elle"},
                 {"text": "❌ Vazgeç", "callback_data": "iptal"}]]
        ok = await notifier.send_buttons(
            f"🛒 {label}\n💰 Şu an: {tl(f)} ({s['host']})\n\nHedef fiyat ne olsun?", rows)
        if not ok:
            shared["bekleyen_urun"]["elle"] = True
            await notifier.send(f"🛒 {label} — şu an {tl(f)}.\n"
                                "Hedef fiyatı yaz (örn: 13500), vazgeçmek için 'iptal':")
    else:
        shared["bekleyen_urun"]["elle"] = True
        ek = " (site bot koruması gösterdi)" if s["blocked"] else ""
        await notifier.send(f"🛒 {label}\nFiyatı şu an okuyamadım{ek} — yine de "
                            "izlemeye alabilirim.\nHedef fiyatı yaz (örn: 13500), "
                            "vazgeçmek için 'iptal':")


async def urun_ekle_bitir(manager: WatcherManager, notifier: Notifier,
                          shared: dict, hedef: float) -> None:
    b = shared.pop("bekleyen_urun", None)
    if not b:
        return
    tg_urun_ekle(b["label"], b["url"], float(hedef))
    shared["products"] = load_products()
    manager.sync(shared["products"])
    await notifier.send(f"✅ Eklendi: {b['label']}\n🎯 Hedef: {tl(float(hedef))} — "
                        "izleme başladı. (/liste ile gör)")


async def telegram_listener(notifier: Notifier, shared: dict, state: State,
                            manager: WatcherManager, context: BrowserContext) -> None:
    """Uzun sorgulamayla (getUpdates) komut dinler. Sadece products.yaml'daki
    chat_id'den gelen komutlar işlenir; yabancı sohbetler yok sayılır."""
    if not notifier.token:
        return
    offset = 0
    while True:
        try:
            updates = await notifier.tg("getUpdates", timeout_ms=65000,
                                        offset=offset, timeout=50)
            if updates is None:
                await asyncio.sleep(10)
                continue
            for u in updates:
                offset = u["update_id"] + 1

                # --- Buton tıklamaları (hedef fiyat seçimi) ---
                cb = u.get("callback_query")
                if cb:
                    cb_chat = str(((cb.get("message") or {}).get("chat") or {})
                                  .get("id", ""))
                    await notifier.tg("answerCallbackQuery",
                                      callback_query_id=cb.get("id"))
                    if not notifier.chat_id or cb_chat != notifier.chat_id:
                        continue
                    data = cb.get("data", "")
                    bekleyen = shared.get("bekleyen_urun")
                    if data == "iptal":
                        shared.pop("bekleyen_urun", None)
                        shared.pop("bekleyen_akakce", None)
                        await notifier.send("Vazgeçildi.")
                    elif data == "elle" and bekleyen:
                        bekleyen["elle"] = True
                        await notifier.send("Hedef fiyatı yaz (örn: 13500), "
                                            "vazgeçmek için 'iptal':")
                    elif data.startswith("hedef|") and bekleyen and bekleyen.get("fiyat"):
                        yuzde = float(data.split("|", 1)[1])
                        hedef = round(bekleyen["fiyat"] * (1 - yuzde / 100))
                        await urun_ekle_bitir(manager, notifier, shared, hedef)
                    elif data.startswith("akakce|"):
                        ba = shared.get("bekleyen_akakce")
                        i = int(data.split("|", 1)[1])
                        if ba and 0 <= i < len(ba["adaylar"]):
                            aday = ba["adaylar"][i]
                            tg_kaynak_ekle(ba["key"], aday["url"])
                            shared["products"] = load_products()
                            manager.sync(shared["products"], force={ba["key"]})
                            shared.pop("bekleyen_akakce", None)
                            await notifier.send(
                                f"✅ {ba['key']} artık Akakçe'den de izleniyor "
                                "(en ucuz satıcı öncelikli).\n"
                                f"🌐 {aday['url']}\n"
                                "Kontrol için: /durum")
                    continue

                msg = u.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                if not text.startswith("/"):
                    if not notifier.chat_id or chat != notifier.chat_id:
                        continue
                    # Düz mesaj: link geldiyse ekleme akışını başlat;
                    # hedef fiyat bekleniyorsa sayıyı işle
                    link = re.search(r"https?://\S+", text)
                    bekleyen = shared.get("bekleyen_urun")
                    if link:
                        await urun_on_izleme(manager, notifier, shared, link.group(0))
                    elif bekleyen and bekleyen.get("elle"):
                        if text.lower() in ("iptal", "vazgeç", "vazgec"):
                            shared.pop("bekleyen_urun", None)
                            await notifier.send("Vazgeçildi.")
                            continue
                        hedef = parse_try_amount(text)
                        if hedef:
                            await urun_ekle_bitir(manager, notifier, shared, hedef)
                        else:
                            await notifier.send("Anlayamadım — sadece rakam yaz "
                                                "(örn: 13500) ya da 'iptal'.")
                    continue

                parca = text.split()
                cmd = parca[0].lower().split("@")[0]

                if not notifier.chat_id:
                    # kurulum kolaylığı: chat_id ayarlı değilken /start'a id ile cevap ver
                    if cmd == "/start":
                        await notifier._send_telegram(
                            f"chat_id'in: {chat}\nBunu products.yaml → "
                            "telegram_chat_id alanına yaz ve botu yeniden başlat.",
                            chat_id=chat)
                    continue
                if chat != notifier.chat_id:
                    continue  # yabancı sohbet — yok say

                products = shared["products"]
                if cmd in ("/start", "/yardim", "/help"):
                    await notifier.send(YARDIM)

                elif cmd == "/durum":
                    await notifier.send("📊 Durum:\n" + durum_ozeti(products, state))

                elif cmd == "/liste":
                    await notifier.send("📋 İzlenen ürünler:\n" + liste_metni(products))

                elif cmd == "/ekle":
                    url = parca[1] if len(parca) > 1 else ""
                    if not url.startswith("http"):
                        await notifier.send("Bana ürün linkini göndermen yeterli — "
                                            "komutsuz da olur.\nYa da: /ekle <link> "
                                            "[hedefTL] [etiket]")
                        continue
                    hedef = parse_try_amount(parca[2]) if len(parca) > 2 else None
                    if not hedef:
                        # hedef verilmedi → fiyatı okuyup butonla sordur
                        await urun_on_izleme(manager, notifier, shared, url)
                        continue
                    label = _tekil_etiket(" ".join(parca[3:]).strip() or etiket_uret(url),
                                          products)
                    tg_urun_ekle(label, url, hedef)
                    shared["products"] = load_products()
                    manager.sync(shared["products"])
                    await notifier.send(f"✅ Eklendi: {label}\n"
                                        f"🎯 Hedef: {tl(hedef)} — izleme başladı.")

                elif cmd == "/sil":
                    p, hata = _urun_no(parca, products)
                    if p is None:
                        await notifier.send(hata)
                        continue
                    tg_urun_sil(product_key(p))
                    shared["products"] = load_products()
                    manager.sync(shared["products"])
                    await notifier.send(f"🗑️ İzlemeden çıkarıldı: {p.get('label', '?')}")

                elif cmd == "/hedef":
                    p, hata = _urun_no(parca, products)
                    yeni = parse_try_amount(parca[2]) if len(parca) > 2 else None
                    if p is None or not yeni:
                        await notifier.send(hata or "Kullanım: /hedef <no> <fiyatTL>\n"
                                                    "Örn: /hedef 3 12750")
                        continue
                    tg_hedef_degistir(product_key(p), yeni)
                    shared["products"] = load_products()
                    manager.sync(shared["products"], force={product_key(p)})
                    await notifier.send(f"🎯 {p.get('label', '?')} yeni hedef: {tl(yeni)}")

                elif cmd == "/akakce":
                    p, hata = _urun_no(parca, products)
                    if p is None:
                        await notifier.send(hata if "Geçersiz" in hata else
                                            "Kullanım: /akakce <no>\nÖnce /liste ile "
                                            "numarayı bul. Örn: /akakce 3")
                        continue
                    await notifier.send(f"🔎 Akakçe'de aranıyor: {p.get('label', '?')} "
                                        "(10-20 sn)...")
                    adaylar = await akakce_ara(manager.context, manager.throttle,
                                               p.get("label", ""))
                    if not adaylar:
                        await notifier.send("Akakçe'de sonuç bulamadım. İstersen ürünün "
                                            "Akakçe linkini kendin bulup bana gönder — "
                                            "yeni kaynak olarak eklerim.")
                        continue
                    shared["bekleyen_akakce"] = {"key": product_key(p),
                                                 "adaylar": adaylar}
                    rows = [[{"text": a["ad"][:60], "callback_data": f"akakce|{i}"}]
                            for i, a in enumerate(adaylar)]
                    rows.append([{"text": "❌ Hiçbiri değil", "callback_data": "iptal"}])
                    await notifier.send_buttons(
                        f"Akakçe'de bulduklarım — hangisi '{p.get('label', '?')}'?", rows)

                elif cmd == "/csv":
                    if HISTORY_CSV.exists():
                        await notifier.send_document(HISTORY_CSV, "Ham fiyat geçmişi")
                    else:
                        await notifier.send("Henüz fiyat kaydı yok.")

                elif cmd == "/grafik":
                    try:
                        png = await grafik_png(context)
                        await notifier.send_photo(png, "Fiyat grafiği")
                        await notifier.send_document(
                            GRAFIK_HTML, "Etkileşimli sürüm — indirip tarayıcıda aç")
                    except Exception as e:
                        await notifier.send(f"Grafik üretilemedi: {e}")

                else:
                    await notifier.send("Bu komutu bilmiyorum.\n\n" + YARDIM)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"Telegram dinleyici hatası: {e}")
            await asyncio.sleep(10)


async def heartbeat(notifier: Notifier, settings: dict, shared: dict,
                    state: State, context: BrowserContext) -> None:
    """Her gün belirli saatte 'bot yaşıyor' + fiyat özeti + 7g trend.
    Haftada bir (weekly_chart_day) grafik PNG olarak da gelir."""
    saat = settings.get("heartbeat_hour")
    if saat is None:
        return
    while True:
        simdi = datetime.now()
        hedef = simdi.replace(hour=int(saat), minute=0, second=0, microsecond=0)
        if hedef <= simdi:
            hedef += timedelta(days=1)
        await asyncio.sleep((hedef - simdi).total_seconds())

        products = shared["products"]
        await notifier.send(f"✅ Takip botu çalışıyor — {len(products)} ürün izleniyor.\n"
                            + durum_ozeti(products, state))
        gun = settings.get("weekly_chart_day", 0)
        if gun is not None and datetime.now().weekday() == int(gun):
            try:
                png = await grafik_png(context)
                await notifier.send_photo(png, "📈 Haftalık fiyat grafiği")
            except Exception as e:
                logging.warning(f"Haftalık grafik gönderilemedi: {e}")


# ===================== ANA =====================

def load_config() -> tuple[dict, list, Sites]:
    cfg = load_yaml(PRODUCTS_YAML)
    settings = dict(cfg.get("settings", {}))
    # Kurulum sihirbazının yazdığı ayarlar (token, chat_id) products.yaml'ı ezmeden
    # ayrı dosyadan gelir — kullanıcı hiç YAML düzenlemek zorunda kalmaz
    if KURULUM_YAML.exists():
        settings.update(load_yaml(KURULUM_YAML))
    products = load_products()
    sites = Sites(load_yaml(SITES_YAML))
    return settings, products, sites


async def launch_context(p, headless: bool) -> BrowserContext:
    return await p.chromium.launch_persistent_context(
        USER_DATA_DIR,
        headless=headless,
        viewport={"width": 1280, "height": 800},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    )


async def main() -> None:
    settings, products, sites = load_config()
    if not products:
        logging.error("products.yaml içinde aktif ürün yok.")
        return
    state = State(STATE_FILE)
    # Etiket değiştirilmişse geçmişi yeni ada taşı (URL parmak iziyle eşleşir)
    if etiket_gocu(state, products):
        products = load_products()
        await state.save()
    throttle = HostThrottle(float(settings.get("min_gap_per_host_seconds", 25)))

    async with async_playwright() as p:
        rq = await p.request.new_context()
        notifier = Notifier(rq, settings)
        await notifier.check_bot()
        # Telegram sayesinde QR/oturum derdi yok → varsayılan headless
        context = await launch_context(p, bool(settings.get("headless", True)))

        # Açılış mesajı — watchdog yeniden başlatınca haber ver; ama bot kısa
        # aralıklarla arka arkaya başlıyorsa (çökme döngüsü) mesaj spam'i yapma
        meta = state.get("_meta")
        son_baslangic = meta.get("last_start_ts", 0)
        meta["last_start_ts"] = time.time()
        await state.save()
        if time.time() - son_baslangic > 1800:
            await notifier.send(f"🔄 Takip botu başlatıldı — {len(products)} ürün izleniyor. "
                                "(/yardim ile komutlar)")
        else:
            logging.warning("30 dk içinde ikinci başlatma — açılış mesajı atlandı "
                            "(çökme döngüsü koruması). Sık oluyorsa takip.log'a bak!")

        shared = {"products": products}
        manager = WatcherManager(context, sites, throttle, notifier, state, settings)
        manager.sync(products)
        hb = asyncio.create_task(heartbeat(notifier, settings, shared, state, context))
        lst = asyncio.create_task(telegram_listener(notifier, shared, state,
                                                    manager, context))
        logging.info(f"{len(products)} ürün izleniyor. Durdurmak için Ctrl+C. "
                     "Telegram'dan /yardim yazabilirsin.")
        try:
            await asyncio.gather(hb, lst)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logging.info("Durduruluyor...")
        finally:
            izleyiciler = manager.cancel_all()
            hb.cancel()
            lst.cancel()
            await asyncio.gather(*izleyiciler, hb, lst, return_exceptions=True)
            await context.close()
            await rq.dispose()


async def run_once() -> None:
    """Tüm ürünleri (tüm kaynaklarıyla) bir kez kontrol eder, tabloyu yazdırır,
    BİLDİRİM ATMAZ. Yeni ürün/site eklerken seçicileri denemek için kullan."""
    settings, products, sites = load_config()
    throttle = HostThrottle(float(settings.get("min_gap_per_host_seconds", 25)))
    async with async_playwright() as p:
        context = await launch_context(p, bool(settings.get("headless", True)))
        print(f"\n{'ÜRÜN':<38} {'SİTE':<18} {'FİYAT':>14} {'KAYNAK':>8} {'STOK':>5} VARYANT")
        print("-" * 100)
        for prod in products:
            for url in product_urls(prod):
                try:
                    s = await check_once(context, sites, throttle, prod, url)
                    stok = {True: "VAR", False: "YOK", None: "?"}[s["in_stock"]]
                    engel = "  ⛔ BOT KORUMASI!" if s["blocked"] else ""
                    satici = f"  ({s['seller']})" if s.get("seller") else ""
                    print(f"{prod.get('label', '?')[:37]:<38} {s['host'][:17]:<18} "
                          f"{tl(s['price']):>14} {s['source']:>8} {stok:>5} "
                          f"{s['variant']}{satici}{engel}")
                except Exception as e:
                    print(f"{prod.get('label', '?')[:37]:<38} "
                          f"HATA: {type(e).__name__}: {e}")
        await context.close()


async def run_test() -> None:
    settings, _, _ = load_config()
    async with async_playwright() as p:
        rq = await p.request.new_context()
        n = Notifier(rq, settings)
        if not await n.check_bot():
            print("Token hatalı veya boş — products.yaml → telegram_bot_token")
            await rq.dispose()
            return
        ok = await n.send("✅ Takip botu PRO v4 kurulumu tamam! /yardim ile komutları gör.")
        print("Sonuç:", "GÖNDERİLDİ ✔" if ok else
              "GÖNDERİLEMEDİ ✖ — chat_id ayarlı mı? (python takip_botu_pro.py chatid)")
        await rq.dispose()


async def run_chatid() -> None:
    """Bota Telegram'dan /start (veya herhangi bir mesaj) yaz, sonra bunu çalıştır:
    gelen sohbetlerin chat_id'lerini listeler."""
    settings, _, _ = load_config()
    async with async_playwright() as p:
        rq = await p.request.new_context()
        n = Notifier(rq, settings)
        if not await n.check_bot():
            await rq.dispose()
            return
        print("Son mesajlar taranıyor (bota bir mesaj yazmış olmalısın)...")
        updates = await n.tg("getUpdates", timeout_ms=35000, timeout=20) or []
        gorulen = {}
        for u in updates:
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                gorulen[chat["id"]] = chat.get("first_name") or chat.get("title") or "?"
        if gorulen:
            for cid, ad in gorulen.items():
                print(f"  chat_id: {cid}   ({ad})")
            print("Bu değeri products.yaml → telegram_chat_id alanına yaz.")
        else:
            print("Mesaj bulunamadı. Telegram'dan botuna /start yazıp tekrar dene.")
        await rq.dispose()


async def run_kur() -> bool:
    """Kurulum sihirbazı: YAML düzenlemeden soru-cevapla Telegram'ı bağlar.
    True dönerse kullanıcı botun hemen başlatılmasını istedi demektir."""
    print()
    print("=" * 58)
    print("  TAKİP BOTU KURULUM SİHİRBAZI")
    print("=" * 58)
    print()
    print("ADIM 1/2 — Telegram botu oluştur:")
    print("  • Telegram'da @BotFather'ı aç")
    print("  • /newbot yaz, bir isim ver")
    print("  • Sana verdiği token'ı (123456:ABC-DEF... gibi) buraya yapıştır")
    print()
    async with async_playwright() as p:
        rq = await p.request.new_context()
        try:
            token = ""
            while True:
                try:
                    token = input("Bot token: ").strip()
                except EOFError:
                    return False
                if not token:
                    continue
                n = Notifier(rq, {"telegram_bot_token": token})
                me = await n.tg("getMe")
                if me:
                    print(f"  ✔ Bot bulundu: @{me.get('username')}")
                    break
                print("  ✖ Token geçersiz görünüyor, tekrar dene.")

            print()
            print("ADIM 2/2 — Botunla eşleş:")
            print(f"  • Telegram'da @{me.get('username')} sohbetini aç ve /start yaz")
            print("  • Mesajını bekliyorum (2 dakika)...")
            chat_id = None
            ad = ""
            offset = 0
            son = time.time() + 120
            while time.time() < son and not chat_id:
                updates = await n.tg("getUpdates", timeout_ms=25000,
                                     offset=offset, timeout=15) or []
                for u in updates:
                    offset = u["update_id"] + 1
                    chat = ((u.get("message") or {}).get("chat") or {})
                    if chat.get("id"):
                        chat_id = str(chat["id"])
                        ad = chat.get("first_name") or chat.get("title") or ""
            if not chat_id:
                print("  ✖ Mesaj gelmedi. Tekrar denemek için: "
                      "python takip_botu_pro.py kur")
                return False
            print(f"  ✔ Eşleşti: {ad} (chat_id: {chat_id})")

            save_yaml_atomic(KURULUM_YAML, {
                "telegram_bot_token": token,
                "telegram_chat_id": chat_id,
            })
            n.chat_id = chat_id
            await n.send("🎉 Kurulum tamam! Artık ürün eklemek için bana ürün "
                         "linkini göndermen yeterli. /yardim ile komutları gör.")
            print()
            print("  ✔ Ayarlar kurulum.yaml'a kaydedildi, test mesajı gönderildi.")
            print()
            try:
                cevap = input("Bot şimdi başlatılsın mı? [E/h]: ").strip().lower()
            except EOFError:
                return False
            return cevap in ("", "e", "evet", "y", "yes")
        finally:
            await rq.dispose()


def _telegram_ayarli() -> bool:
    try:
        settings, _, _ = load_config()
        return bool(settings.get("telegram_bot_token"))
    except Exception:
        return True  # config okunamıyorsa main() kendi hatasını versin


if __name__ == "__main__":
    komut = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    try:
        if komut == "test":
            asyncio.run(run_test())
        elif komut == "once":
            asyncio.run(run_once())
        elif komut == "chatid":
            asyncio.run(run_chatid())
        elif komut == "kur":
            if asyncio.run(run_kur()):
                asyncio.run(main())
        elif komut == "grafik":
            import grafik
            print(f"Grafik üretildi: {grafik.generate()}")
        elif komut == "smoke":
            # Gözetmen (guncelleyici.py) güncelleme sonrası çağırır: kod import
            # edilebiliyor ve konfig yükleniyor mu? Çıkış 0 = sağlıklı; değilse
            # gözetmen push'u geri alır. Ağa/tarayıcıya dokunmaz, hızlıdır.
            try:
                settings, products, sites = load_config()
                import grafik  # noqa: F401 — grafik modülü de sağlam olsun
                print(f"SMOKE OK — {len(products)} ürün, "
                      f"{len(sites.sites)} site kuralı")
            except Exception as e:
                print(f"SMOKE FAIL: {type(e).__name__}: {e}")
                sys.exit(1)
        else:
            # İlk çalıştırma kolaylığı: Telegram hiç ayarlanmamışsa ve terminal
            # etkileşimliyse sihirbazı otomatik başlat
            if not _telegram_ayarli() and sys.stdin.isatty():
                print("Telegram ayarlı görünmüyor — kurulum sihirbazı başlıyor.")
                if asyncio.run(run_kur()):
                    asyncio.run(main())
            else:
                asyncio.run(main())
    except KeyboardInterrupt:
        pass
