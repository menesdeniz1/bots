# -*- coding: utf-8 -*-
"""
STOK + FİYAT TAKİP BOTU — PRO v2
=================================
Motor    : Playwright (Chromium, kalıcı profil, stealth) → JS'li Türk sitelerinde çalışır
Bildirim : 1) WhatsApp Web (gönderim DOĞRULANIR)  2) başarısızsa CallMeBot API (yedek kanal)
Durum    : state.json → bot yeniden başlasa da mükerrer bildirim atmaz (cooldown + tekrar-düşüş)
Geçmiş   : her okuma fiyat_gecmisi.csv'ye eklenir (zaman;urun;fiyat;stok;kaynak)

Modlar (products.yaml içinde ürün başına):
  price        → fiyat hedefin altına inince bildir (PC parçaları)
  stock        → beden/varyant seçilebiliyor + sepete ekle aktifse bildir (ayakkabı/kıyafet)
  price+stock  → ikisi birden sağlanınca bildir

v2 ile gelenler (eski stock-bot + takip_botu_pro birleşimi ve üzeri):
  • Fiyat okuma zinciri: JSON-LD (@graph/offers/lowPrice/TRY kontrolü) → siteye özel seçici
    → genel seçiciler → meta tag → gövde regex (düşük güven)
  • Şüpheli fiyat koruması: fiyat anormal düşükse (parse hatası ihtimali) hemen bildirmez,
    1-2 dk sonra İKİNCİ okumayla doğrular, sonra bildirir → yanlış alarm yok
  • Site bazlı kuyruk: aynı siteye istekler arka arkaya sıkışmaz (min aralık + jitter),
    bot koruması / captcha algılanırsa o siteye üstel geri çekilme (5 dk → 60 dk)
  • WhatsApp oturum düştüyse fark eder, CallMeBot üzerinden "QR okut" uyarısı yollar
  • Bir ürün üst üste N kez okunamazsa tek seferlik uyarı mesajı (seçici bozulmuş olabilir)
  • Günlük heartbeat: "bot yaşıyor" + ürünlerin son bilinen fiyat özeti + bayat okuma uyarısı
  • Her kontrolde sekme aç-kapat → RAM sızıntısı yok

Çalıştırma:
  pip install -r requirements.txt && playwright install chromium
  python takip_botu_pro.py           → normal çalışma (ilk açılışta WhatsApp QR okut)
  python takip_botu_pro.py test      → WhatsApp'a test mesajı gönder
  python takip_botu_pro.py once      → tüm ürünleri BİR KEZ kontrol et, sonucu yazdır, bildirim atma
                                       (seçici/parse hatası ayıklamak için birebir)
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
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import yaml
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Page, BrowserContext

# ===================== YOLLAR =====================
BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_YAML = BASE_DIR / "products.yaml"
SITES_YAML = BASE_DIR / "sites.yaml"
STATE_FILE = BASE_DIR / "state.json"
HISTORY_CSV = BASE_DIR / "fiyat_gecmisi.csv"
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

WA_SEND_LOCK = asyncio.Lock()

# Bot koruması / captcha sayfası işaretleri (başlık + gövdenin ilk kısmında aranır)
BLOCK_MARKERS = [
    "robot check", "captcha", "erişim engellendi", "access denied",
    "olağandışı trafik", "unusual traffic", "attention required",
    "checking your browser", "doğrulama gerekiyor",
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


# ===================== YAML / DURUM / GEÇMİŞ =====================

def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class State:
    """state.json — mükerrer bildirim engelleme + son iyi fiyat + hata serileri.
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


async def append_history(label: str, price: float | None, in_stock, source: str) -> None:
    async with HISTORY_LOCK:
        yeni = not HISTORY_CSV.exists()
        with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if yeni:
                w.writerow(["zaman", "urun", "fiyat", "stok", "kaynak"])
            w.writerow([
                datetime.now().isoformat(timespec="seconds"),
                label,
                f"{price:.2f}" if price is not None else "",
                {True: "1", False: "0"}.get(in_stock, ""),
                source,
            ])


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
    """JSON-LD içinde gezinir: listeler, @graph, iç içe yapılar."""
    if isinstance(node, list):
        for x in node:
            yield from _jsonld_iter(x)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement", "item", "offers"):
            if key in node:
                yield from _jsonld_iter(node[key])


def _price_from_jsonld_obj(obj: dict) -> float | None:
    """Bir JSON-LD düğümünden fiyat çeker. TRY dışındaki para birimlerini reddeder
    (bazı siteler USD fiyatı da gömer)."""
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
    try:
        body = await page.locator("body").inner_text(timeout=3000)
        m = re.search(r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+,\d{2})\s*(?:TL|₺)", body)
        if m:
            logging.debug("Fiyat gövde regex ile bulundu (düşük güven).")
            return parse_try_amount(m.group(1)), "regex"
    except Exception:
        pass
    return None, "yok"


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


# ===================== BİLDİRİM KATMANI =====================

class Notifier:
    """Önce WhatsApp Web dener ve gönderimi DOĞRULAR (buton tıklandı + kutu boşaldı);
    başarısızsa CallMeBot API'ye düşer. WhatsApp oturumu düşmüşse bunu fark eder ve
    CallMeBot mesajına 'QR okut' notu ekler. İkisi de patlarsa ERROR log — sessiz kayıp yok."""

    def __init__(self, context: BrowserContext, settings: dict):
        self.context = context
        self.wa_page: Page | None = None
        self.phone = str(settings.get("phone", "")).lstrip("+")
        self.use_wa_web = bool(settings.get("whatsapp_web", True))
        self.callmebot_key = str(settings.get("callmebot_apikey", "") or "")
        self.wa_logged_out = False

    async def prepare_whatsapp(self) -> None:
        if not self.use_wa_web:
            return
        self.wa_page = await self.context.new_page()
        await apply_stealth(self.wa_page)
        await self.wa_page.goto("https://web.whatsapp.com",
                                wait_until="domcontentloaded", timeout=60000)
        try:
            await self.wa_page.wait_for_selector(
                "canvas, div[aria-label='Sohbet listesi'], div[data-testid='chat-list'], #pane-side",
                timeout=45000)
        except PWTimeout:
            pass
        logging.info("WhatsApp Web açıldı. QR ekranı görüyorsan telefonla okut; "
                     "oturum .chrome-profile-bot içinde kalıcıdır.")

    async def _wa_composer(self):
        return self.wa_page.locator("footer div[contenteditable='true']").first

    async def _send_wa_web(self, text: str) -> bool:
        if not self.wa_page:
            return False
        try:
            url = f"https://web.whatsapp.com/send?phone={self.phone}&text={quote_plus(text)}"
            await self.wa_page.goto(url, wait_until="domcontentloaded", timeout=40000)

            # Mesaj kutusu gelene kadar bekle; gelmiyorsa oturum düşmüş olabilir (QR ekranı)
            try:
                await self.wa_page.wait_for_selector(
                    "footer div[contenteditable='true']", timeout=25000)
                self.wa_logged_out = False
            except PWTimeout:
                if await self.wa_page.locator("canvas").count() > 0:
                    self.wa_logged_out = True
                    logging.error("WhatsApp Web oturumu DÜŞMÜŞ — QR yeniden okutulmalı!")
                return False

            await asyncio.sleep(1.5)
            btn_sels = ["button span[data-icon='send']", "span[data-icon='send']",
                        "button[aria-label='Gönder']", "button[aria-label='Send']"]
            son = time.time() + 15
            clicked = False
            while time.time() < son and not clicked:
                for sel in btn_sels:
                    try:
                        loc = self.wa_page.locator(sel).first
                        if await loc.is_visible(timeout=800):
                            await loc.click()
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    await asyncio.sleep(1)
            if not clicked:
                await (await self._wa_composer()).press("Enter")

            # DOĞRULAMA: gönderim gerçekleştiyse mesaj kutusu boşalmış olmalı
            await asyncio.sleep(3)
            kutu = await self._wa_composer()
            if await kutu.count() > 0:
                kalan = (await kutu.inner_text()).strip()
                return kalan == ""
            return clicked
        except Exception as e:
            logging.warning(f"WhatsApp Web gönderimi başarısız: {e}")
            return False

    async def _send_callmebot(self, text: str) -> bool:
        if not self.callmebot_key or not self.phone:
            return False
        try:
            url = (f"https://api.callmebot.com/whatsapp.php?phone=%2B{self.phone}"
                   f"&text={quote_plus(text)}&apikey={quote_plus(self.callmebot_key)}")
            resp = await self.context.request.get(url, timeout=30000)
            if not resp.ok:
                return False
            body = (await resp.text()).lower()
            return "invalid" not in body and "error" not in body
        except Exception as e:
            logging.warning(f"CallMeBot gönderimi başarısız: {e}")
            return False

    async def send(self, text: str) -> bool:
        async with WA_SEND_LOCK:
            if self.use_wa_web and await self._send_wa_web(text):
                logging.info("Bildirim WhatsApp Web ile gönderildi ✔")
                return True
            yedek = text
            if self.wa_logged_out:
                yedek += "\n⚠️ WhatsApp Web oturumu düşmüş — bota QR okutman gerekiyor!"
            if await self._send_callmebot(yedek):
                logging.info("Bildirim CallMeBot (yedek kanal) ile gönderildi ✔")
                return True
            logging.error("BİLDİRİM GÖNDERİLEMEDİ! (WhatsApp Web + CallMeBot ikisi de başarısız)")
            return False


# ===================== KONTROL + KARAR =====================

async def check_once(context: BrowserContext, sites: Sites, throttle: HostThrottle,
                     prod: dict) -> dict:
    """Tek kontrol turu. Site kuyruğuna girer, sekme açar, okur, KAPATIR."""
    url = prod["url"]
    host = urlparse(url).netloc
    strat = sites.strategy(host)
    sonuc = {"price": None, "source": "yok", "in_stock": None,
             "variant_ok": True, "variant": "AUTO", "blocked": False}

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
            throttle.reward(host)
        finally:
            await page.close()
    return sonuc


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
    """Parse hatası ihtimali: regex kaynaklı okuma, hedefin yarısından da ucuz,
    veya son iyi fiyata göre %40'tan fazla ani düşüş → önce doğrula, sonra bildir."""
    fp = s["price"]
    if fp is None:
        return False
    if s["source"] == "regex":
        return True
    thr = float(prod.get("price_threshold_tl", 0))
    if thr and fp < thr * 0.5:
        return True
    son_iyi = st.get("last_good_price")
    if son_iyi and fp < son_iyi * 0.6:
        return True
    return False


# ===================== ÜRÜN İZLEYİCİ =====================

async def product_watcher(context: BrowserContext, sites: Sites, throttle: HostThrottle,
                          notifier: Notifier, prod: dict, state: State,
                          sem: asyncio.Semaphore, settings: dict) -> None:
    label = prod.get("label", "Ürün")
    key = prod["url"]
    cooldown = timedelta(minutes=int(prod.get("cooldown_minutes", 1440)))
    renotify_drop = float(prod.get("renotify_drop_pct",
                                   settings.get("renotify_drop_pct", 3)))
    sanity_guard = bool(settings.get("sanity_guard", True))
    error_alert_streak = int(settings.get("error_alert_streak", 5))
    quick_recheck = False

    while True:
        quick_recheck = False
        async with sem:
            st = state.get(key)
            try:
                s = await check_once(context, sites, throttle, prod)
                fp = s["price"]
                logging.info(f"[{label}] fiyat={tl(fp)} ({s['source']}) "
                             f"stok={s['in_stock']} varyant={s['variant']}"
                             f"{' [ENGEL]' if s['blocked'] else ''}")

                if not s["blocked"]:
                    st["error_streak"] = 0
                    if fp is not None:
                        await append_history(label, fp, s["in_stock"], s["source"])

                    gerekli, detay = alarm_gerekli(prod, s)
                    supheli = sanity_guard and gerekli and fiyat_suphali(prod, s, st)

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
                                            f"({tl(fp)}, kaynak={s['source']}) — "
                                            "1-2 dk içinde ikinci okumayla doğrulanacak.")
                    else:
                        st.pop("pending_price", None)

                    if fp is not None and not supheli:
                        st["last_good_price"] = fp
                        st["last_good_ts"] = time.time()

                    if gerekli and not supheli:
                        son_ts = st.get("last_notify_ts", 0)
                        son_fiyat = st.get("last_notify_price")
                        cooldown_gecti = (datetime.now()
                                          - datetime.fromtimestamp(son_ts) > cooldown)
                        daha_da_dustu = (fp is not None and son_fiyat
                                         and fp <= son_fiyat * (1 - renotify_drop / 100))
                        if cooldown_gecti or daha_da_dustu:
                            msg = f"🔥 {label}\n{detay}\n🔗 {key}"
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
                        f"Site değişmiş olabilir — sites.yaml seçicisini kontrol et.\n🔗 {key}")
                await state.save()

        if quick_recheck:
            await asyncio.sleep(random.uniform(60, 120))
        else:
            await asyncio.sleep(random.randint(int(prod.get("sleep_min", 300)),
                                               int(prod.get("sleep_max", 600))))


async def heartbeat(notifier: Notifier, settings: dict, products: list,
                    state: State) -> None:
    """Her gün belirli saatte 'bot yaşıyor' + fiyat özeti — sessiz ölümü fark et.
    12 saatten eski (bayat) okumalar ⚠️ ile işaretlenir."""
    saat = settings.get("heartbeat_hour")
    if saat is None:
        return
    while True:
        simdi = datetime.now()
        hedef = simdi.replace(hour=int(saat), minute=0, second=0, microsecond=0)
        if hedef <= simdi:
            hedef += timedelta(days=1)
        await asyncio.sleep((hedef - simdi).total_seconds())

        satirlar = [f"✅ Takip botu çalışıyor — {len(products)} ürün izleniyor."]
        for p in products:
            st = state.get(p["url"])
            fp = st.get("last_good_price")
            ts = st.get("last_good_ts")
            bayat = ""
            if ts and time.time() - ts > 12 * 3600:
                bayat = " ⚠️ (son okuma eski!)"
            elif not ts:
                bayat = " ⚠️ (hiç okunamadı!)"
            satirlar.append(f"• {p.get('label', '?')}: {tl(fp)}{bayat}")
        await notifier.send("\n".join(satirlar))


# ===================== ANA =====================

def load_config() -> tuple[dict, list, Sites]:
    cfg = load_yaml(PRODUCTS_YAML)
    settings = cfg.get("settings", {})
    products = [p for p in cfg.get("products", []) if p.get("enabled", True)]
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
    sem = asyncio.Semaphore(int(settings.get("max_concurrency", 3)))
    throttle = HostThrottle(float(settings.get("min_gap_per_host_seconds", 25)))

    async with async_playwright() as p:
        context = await launch_context(p, bool(settings.get("headless", False)))
        notifier = Notifier(context, settings)
        await notifier.prepare_whatsapp()

        tasks = [asyncio.create_task(product_watcher(
                     context, sites, throttle, notifier, prod, state, sem, settings))
                 for prod in products]
        hb = asyncio.create_task(heartbeat(notifier, settings, products, state))
        logging.info(f"{len(products)} ürün izleniyor. Durdurmak için Ctrl+C.")
        try:
            await asyncio.gather(*tasks, hb)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logging.info("Durduruluyor...")
            for t in [*tasks, hb]:
                t.cancel()
            await asyncio.gather(*tasks, hb, return_exceptions=True)
        finally:
            await context.close()


async def run_once() -> None:
    """Tüm ürünleri bir kez kontrol eder, tabloyu yazdırır, BİLDİRİM ATMAZ.
    Yeni ürün/site eklerken seçicileri denemek için kullan."""
    settings, products, sites = load_config()
    throttle = HostThrottle(float(settings.get("min_gap_per_host_seconds", 25)))
    async with async_playwright() as p:
        context = await launch_context(p, bool(settings.get("headless", False)))
        print(f"\n{'ÜRÜN':<45} {'FİYAT':>15} {'KAYNAK':>8} {'STOK':>6} VARYANT")
        print("-" * 90)
        for prod in products:
            try:
                s = await check_once(context, sites, throttle, prod)
                stok = {True: "VAR", False: "YOK", None: "?"}[s["in_stock"]]
                engel = "  ⛔ BOT KORUMASI!" if s["blocked"] else ""
                print(f"{prod.get('label', '?')[:44]:<45} {tl(s['price']):>15} "
                      f"{s['source']:>8} {stok:>6} {s['variant']}{engel}")
            except Exception as e:
                print(f"{prod.get('label', '?')[:44]:<45}  HATA: {type(e).__name__}: {e}")
        await context.close()


async def run_test() -> None:
    settings, _, _ = load_config()
    async with async_playwright() as p:
        context = await launch_context(p, headless=False)
        n = Notifier(context, settings)
        await n.prepare_whatsapp()
        try:
            input("WhatsApp'ta oturum açıksa Enter'a bas → test mesajı gönderilecek...")
        except EOFError:
            pass
        ok = await n.send("✅ Takip botu PRO v2 kurulumu tamam!")
        print("Sonuç:", "GÖNDERİLDİ ✔" if ok else "GÖNDERİLEMEDİ ✖ (log'a bak)")
        await context.close()


if __name__ == "__main__":
    komut = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    try:
        if komut == "test":
            asyncio.run(run_test())
        elif komut == "once":
            asyncio.run(run_once())
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        pass
