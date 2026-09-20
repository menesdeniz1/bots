import asyncio, logging, random, re, os, signal, json, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set
from collections import defaultdict
from urllib.parse import urlparse, quote_plus

import yaml
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Page, BrowserContext, Browser

# ===================== PROJE AYARLARI =====================
BASE_DIR = Path(__file__).resolve().parent
HEADLESS = os.getenv("STOCKBOT_HEADLESS", "false").lower() == "true"  # İlk girişte QR için False kalsın
OWN_PHONE_E164 = os.getenv("STOCKBOT_PHONE_E164", "")
if not re.fullmatch(r"[1-9][0-9]{7,14}", OWN_PHONE_E164):
    raise RuntimeError("Set STOCKBOT_PHONE_E164 to your own destination number before running.")

# Dosya Yolları
PRODUCTS_YAML = str(BASE_DIR / "products.yaml")
SITES_YAML = str(BASE_DIR / "sites.yaml")
PRIVATE_DATA_DIR = Path(os.getenv("STOCKBOT_DATA_DIR", str(Path.home() / ".local" / "share" / "stock-bot"))).expanduser().resolve()
if PRIVATE_DATA_DIR.is_relative_to(BASE_DIR.parent):
    raise RuntimeError("STOCKBOT_DATA_DIR must be outside this repository")
PRIVATE_DATA_DIR.mkdir(parents=True, exist_ok=True)
USER_DATA_DIR = str(PRIVATE_DATA_DIR / "browser-profile")
LOG_FILE = str(PRIVATE_DATA_DIR / "stock_watch.log")

# Global Limitler
PAGE_TIMEOUT_MS = 45000
GLOBAL_MAX_CONCURRENCY = 3
WA_SEND_LOCK = asyncio.Lock()
CONCURRENCY_SEMAPHORE = asyncio.Semaphore(GLOBAL_MAX_CONCURRENCY)
# ==========================================================

# Gelişmiş Loglama
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")]
)

# Debug seviyesini sadece bizim loglarımız için kontrol edebiliriz
if os.getenv("STOCKBOT_DEBUG", "false").lower() == "true":
    logging.getLogger().setLevel(logging.DEBUG)

def _parse_try_amount(s: str) -> Optional[float]:
    if not s: return None
    clean = re.sub(r"[^\d\.,]", "", s).replace(" ", "")
    if "," in clean and "." in clean: # 1.250,50 formatı
        clean = clean.replace(".", "").replace(",", ".")
    elif "," in clean: # 1250,50 formatı
        clean = clean.replace(",", ".")
    try: return float(clean)
    except: return None

# --- Site strategies are loaded from external YAML for clarity and maintainability ---
with open(SITES_YAML, "r", encoding="utf-8") as _sf:
    _sites_raw = yaml.safe_load(_sf) or {}
SITE_STRATEGIES = _sites_raw.get("sites", {})
DEFAULT_STRATEGY = SITE_STRATEGIES.get("default", {})

def get_site_strategy(host: str) -> Dict[str, Any]:
    for domain, strat in SITE_STRATEGIES.items():
        if domain != "default" and domain in host:
            return strat or {}
    return DEFAULT_STRATEGY or {}

async def human_delay(min_s=0.5, max_s=1.5):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def apply_stealth(page: Page):
    """Applies advanced stealth techniques to bypass bot detection."""
    # Add stealth scripts and evasions
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['tr-TR', 'tr', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    """)
    # Set a more realistic user agent
    await page.set_extra_http_headers({
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })

async def select_variant_ruthless(page: Page, target_variants: List[str], host: str) -> Tuple[bool, str]:
    """Selects target size/variant using site strategy. Returns (found, chosen_label)."""
    if "*" in target_variants or "ANY" in target_variants:
        return True, "AUTO"

    strat = get_site_strategy(host)
    targets = [str(t).upper() for t in target_variants]

    # Open size drawer/trigger if provided
    trigger_sel = strat.get("size_trigger")
    if trigger_sel:
        try:
            trigger = page.locator(trigger_sel).first
            if await trigger.count() > 0 and await trigger.is_visible():
                await trigger.click()
                # Wait for the size list to appear
                size_items_sel = strat.get("size_items", "button")
                try:
                    await page.wait_for_selector(size_items_sel, timeout=3000)
                except:
                    await asyncio.sleep(1)
        except Exception:
            pass

    # Scan candidate items
    primary_sel = strat.get("size_items", "button, li, [role='button']")
    selectors = [primary_sel, "button", "span", "div", "li", "a", "[data-variant-id]", "[data-variant-name]"]
    for sel in selectors:
        try:
            locators = page.locator(sel)
            count = await locators.count()
            for i in range(count):
                try:
                    el = locators.nth(i)
                    if not await el.is_visible():
                        continue

                    # Beden metnini çekmeye çalışalım
                    text = (await el.inner_text()).strip()
                    if not text:
                        # Eğer inner_text yoksa attribute'lara bakalım (bazı siteler title veya data-value kullanır)
                        for attr in ["title", "data-value", "aria-label", "value"]:
                            text = (await el.get_attribute(attr) or "").strip()
                            if text: break
                    
                    if not text or len(text) > 50:
                        continue
                    
                    text_upper = text.upper()
                    # Daha esnek eşleşme
                    if any(t == text_upper or f" {t} " in f" {text_upper} " or text_upper.startswith(f"{t} ") or text_upper.endswith(f" {t}") or (t.isdigit() and t == re.sub(r"\D", "", text_upper)) for t in targets):
                        # Pasif/Tükendi kontrolü
                        class_attr = (await el.get_attribute("class") or "").lower()
                        parent_class = ""
                        try:
                            parent = el.locator("xpath=..")
                            if await parent.count() > 0:
                                parent_class = (await parent.get_attribute("class") or "").lower()
                        except: pass

                        if any(x in (class_attr + " " + parent_class) for x in ["disabled", "out-of-stock", "passive", "sold-out", "unavailable", "empty", "tükendi"]):
                            continue

                        if await el.is_enabled():
                            await el.scroll_into_view_if_needed()
                            await el.click(force=True, delay=random.randint(50, 150))
                            # Tıklama sonrası bir saniye bekle (sayfanın update olması için)
                            await asyncio.sleep(1)
                            return True, text
                except Exception:
                    continue
        except Exception:
            continue
    return False, ""

async def check_add_to_cart(page: Page, host: str) -> bool:
    """Checks whether the add-to-cart button is enabled/visible."""
    strat = get_site_strategy(host)
    sel = strat.get("add_button", "button:has-text('Sepete Ekle')")

    btn = page.locator(sel).first
    if await btn.count() == 0:
        # Alternatif butonları dene
        alt_buttons = ["button:has-text('SEPETE EKLE')", "button:has-text('ADD TO CART')", "button.add-to-basket", "button.add-to-cart"]
        for b_sel in alt_buttons:
            btn = page.locator(b_sel).first
            if await btn.count() > 0: break
    
    if await btn.count() == 0:
        return False

    is_visible = await btn.is_visible()
    is_disabled = await btn.is_disabled()

    # Some sites don't set disabled attribute but toggle classes
    class_attr = (await btn.get_attribute("class") or "").lower()
    has_disabled_class = any(x in class_attr for x in ["disabled", "not-available", "passive", "out-of-stock", "unavailable"])

    return is_visible and not is_disabled and not has_disabled_class

async def get_price_ruthless(page: Page, host: str) -> Optional[float]:
    """Extract price via JSON-LD first, then site selector/meta fallback."""
    # 1) JSON-LD
    scripts = await page.locator('script[type="application/ld+json"]').all()
    for s in scripts:
        try:
            content = await s.inner_text()
            data = json.loads(content)
            
            # Handle list of objects
            if isinstance(data, list):
                objects = data
            else:
                objects = [data]
                
            for obj in objects:
                # Direct price or in offers
                price = obj.get("price")
                if price is None:
                    offers = obj.get("offers")
                    if isinstance(offers, list):
                        offers = offers[0]
                    price = (offers or {}).get("price")
                
                if price is not None:
                    amt = _parse_try_amount(str(price))
                    if amt: return amt
        except Exception:
            continue

    # 2) Site specific selector
    strat = get_site_strategy(host)
    price_sel = strat.get("price_selector")
    
    # Konyalı Saat ve benzeri siteler için ekstra selektörler
    candidate_selectors = []
    if price_sel: candidate_selectors.append(price_sel)
    candidate_selectors.extend([
        ".product-price", "#product-price", ".price-value", 
        "span.price", ".current-price", "[data-price-amount]",
        ".price__current", ".price-current__amount"
    ])

    for sel in candidate_selectors:
        try:
            locs = page.locator(sel)
            count = await locs.count()
            for i in range(count):
                loc = locs.nth(i)
                if not await loc.is_visible(): continue
                
                # Text content
                price_text = (await loc.inner_text()).strip()
                amt = _parse_try_amount(price_text)
                if amt and amt > 0: return amt
                
                # Meta content
                content = await loc.get_attribute("content")
                if content:
                    amt = _parse_try_amount(content)
                    if amt and amt > 0: return amt
        except Exception:
            continue

    # 3) Generic meta fallback
    try:
        price_meta = await page.locator('meta[property="product:price:amount"]').get_attribute("content", timeout=3000)
        if price_meta:
            return _parse_try_amount(price_meta)
    except Exception:
        pass

    return None

async def send_wa_msg(wa_page: Page, text: str):
    """WhatsApp mesajı gönderimi - Gelişmiş hata yönetimi ve retry ile."""
    if not wa_page:
        logging.error("WhatsApp sayfası hazır değil!")
        return

    async with WA_SEND_LOCK:
        for attempt in range(3):
            try:
                # 1. URL'ye git
                url = f"https://web.whatsapp.com/send?phone={OWN_PHONE_E164}&text={quote_plus(text)}"
                await wa_page.goto(url, wait_until="domcontentloaded", timeout=40000)
                
                # 2. Sayfanın yüklenmesini bekle (Giriş alanı veya yükleme ekranı geçene kadar)
                # Bazen sayfa boş gelebilir, bir saniye nefes alalım
                await asyncio.sleep(3)

                # 3. Gönder butonunu bulmaya çalış (Birden fazla strateji)
                btn_selectors = [
                    "button span[data-icon='send']", 
                    "span[data-icon='send']",
                    "footer button:has(span[data-icon='send'])",
                    "button[aria-label='Gönder']",
                    "button[aria-label='Send']",
                    "div[role='button'] span[data-icon='send']"
                ]
                
                btn = None
                start_search = time.time()
                while time.time() - start_search < 15: # 15 saniye boyunca butonu ara
                    for sel in btn_selectors:
                        try:
                            loc = wa_page.locator(sel).first
                            if await loc.is_visible(timeout=1000):
                                btn = loc
                                break
                        except:
                            continue
                    if btn: break
                    
                    # Eğer buton gelmediyse Enter basmayı deneyelim (bazı durumlarda input odaklıdır)
                    if time.time() - start_search > 8:
                         await wa_page.keyboard.press("Enter")
                         await asyncio.sleep(1)
                    
                    await asyncio.sleep(1)
                
                if btn:
                    await btn.click()
                    logging.info("WhatsApp mesajı başarıyla tetiklendi.")
                    # Mesajın gerçekten gitmesi için (mavi tık beklemiyoruz ama socket'e düşmeli)
                    await asyncio.sleep(4)
                    return
                else:
                    # Alternatif: Enter tuşu ile göndermeyi bir kez daha zorla
                    await wa_page.keyboard.press("Enter")
                    await asyncio.sleep(3)
                    logging.warning("WhatsApp butonu bulunamadı, Enter ile denendi.")
                    return # Başarılı sayalım (Enter genellikle çalışır)
            except Exception as e:
                logging.warning(f"WhatsApp gönderim denemesi {attempt+1} başarısız: {e}")
                await asyncio.sleep(5)
        logging.error("WhatsApp mesajı 3 deneme sonrası gönderilemedi.")

async def product_watcher(context: BrowserContext, wa_page: Page, prod: Dict):
    label = prod.get("label", "Ürün")
    url = prod["url"]
    host = urlparse(url).netloc
    last_notify = datetime.min
    
    # Her ürün için ayrı sayfa yönetimi
    page = await context.new_page()
    await apply_stealth(page)

    try:
        while True:
            async with CONCURRENCY_SEMAPHORE:
                try:
                    logging.info(f"[{label}] Kontrol ediliyor...")
                    
                    # Rastgele bekleme (Anti-bot için)
                    await asyncio.sleep(random.uniform(1, 3))
                    
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                    except PWTimeout:
                        logging.warning(f"[{label}] Sayfa yükleme zaman aşımı, devam ediliyor...")
                    
                    # Sayfa yüklendikten sonra ufak bir insan davranışı
                    await page.mouse.move(random.randint(100, 500), random.randint(100, 500))
                    
                    wait_after = get_site_strategy(host).get("wait_after_page_load", 3)
                    await asyncio.sleep(wait_after)

                    # 1. Beden Seçimi
                    found_size, chosen_size = await select_variant_ruthless(page, prod["target_variants"], host)

                    # 2. Stok Kontrolü
                    in_stock = await check_add_to_cart(page, host)

                    # 3. Fiyat Kontrolü
                    price = await get_price_ruthless(page, host)
                    threshold = prod["price_threshold_tl"]

                    status_msg = f"Size: {found_size}({chosen_size}), Stock: {in_stock}, Price: {price}"
                    logging.info(f"[{label}] {status_msg}")

                    # KARAR ANI
                    if found_size and in_stock and price and price <= threshold:
                        cooldown = prod.get("cooldown_minutes", 60)
                        if datetime.now() - last_notify > timedelta(minutes=cooldown):
                            msg = f"🔥 ACİL STOK: {label}\n💰 Fiyat: {price} TL\n📏 Beden: {chosen_size}\n🔗 {url}"
                            await send_wa_msg(wa_page, msg)
                            last_notify = datetime.now()
                            logging.warning(f"!!! BİLDİRİM GÖNDERİLDİ: {label} !!!")
                    elif not found_size:
                        logging.debug(f"[{label}] Hedef beden bulunamadı veya tükendi.")
                    elif not in_stock:
                        logging.debug(f"[{label}] Sepete ekle butonu aktif değil.")
                    elif price and price > threshold:
                        logging.debug(f"[{label}] Fiyat ({price}) eşik değerin ({threshold}) üzerinde.")

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.error(f"[{label}] Beklenmedik Hata: {e}")
                
            # Semaphore dışında bekleme
            wait_time = random.randint(prod.get("sleep_min", 60), prod.get("sleep_max", 120))
            await asyncio.sleep(wait_time)
    except asyncio.CancelledError:
        logging.info(f"[{label}] İzleyici durduruluyor...")
    finally:
        await page.close()

async def main():
    # YAML Oku
    with open(PRODUCTS_YAML, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=HEADLESS,
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # WhatsApp Web'i hazırla
        wa_page = await context.new_page()
        await apply_stealth(wa_page)
        await wa_page.goto("https://web.whatsapp.com", wait_until="networkidle", timeout=60000)
        logging.info("WhatsApp Web yüklendi. (Eğer oturum açık değilse QR okutmanız gerekebilir)")

        tasks = []
        for prod in config["products"]:
            if prod.get("enabled", True):
                tasks.append(asyncio.create_task(product_watcher(context, wa_page, prod)))

        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logging.info("Sistem durduruluyor, görevler iptal ediliyor...")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logging.error(f"Sistem Hatası: {e}")
        finally:
            await context.close()

if __name__ == "__main__":
    asyncio.run(main())