#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gözetmen + otomatik güncelleyici — git push ile uzaktan dağıtım
================================================================
Katmanlar (her katman altındakini ayağa kaldırır):
  launchd / systemd / calistir.bat  → gözetmeni ayakta tutar (hiç değişmez)
  guncelleyici.py (bu dosya)        → botu ayakta tutar + git'ten günceller
  takip_botu_pro.py                 → serbestçe değişir; bozulsa da sistem ölmez

Ne yapar:
  • Botu başlatır; çökerse üstel bekleme ile yeniden başlatır.
  • Her POLL saniyede origin'i yoklar. Yeni commit varsa:
      git reset --hard → (requirements değiştiyse pip install) → SMOKE testi
      → geçerse botu nazikçe yeniden başlatır, Telegram'a haber verir.
  • Smoke geçmeyen push GERİ ALINIR: eski sürüm kesintisiz devam eder,
    Telegram'a "push bozuk" uyarısı gider. Aynı bozuk commit bir daha
    denenmez; düzeltme push'lanınca otomatik alınır.
  • Smoke geçen ama açılışta art arda çöken sürüm, bilinen son iyi sürüme
    otomatik geri alınır (canary: CANARY sn yaşayan sürüm "iyi" sayılır).
  • Bot çökmüş haldeyken bile HER yeniden başlatma öncesi git kontrol edilir —
    düzeltme push'ladığın anda bir sonraki denemede sistem kendini toparlar.
  • Kendisi de repodan güncellenir: dosyası değişmişse önce derleme
    kontrolünden geçer, sonra kendini yeniden başlatır (os.execv).

Bağımlılık: SADECE stdlib (yaml/playwright yok) — venv bozulsa bile çalışır.
Bu yüzden sistem Python'uyla çalıştır: python3 guncelleyici.py
Bot ise venv'in Python'uyla başlatılır (venv/bin/python otomatik bulunur).

Kaynak gerçeği git'tir: bot makinesinde takip edilen dosyaları elle değiştirme
(Telegram komutları serbest — onlar git dışı overlay dosyalarına yazar).
Elle değişiklik algılanırsa güncelleme bekletilir ve Telegram'a uyarı gider.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent

# --- Ayarlar (testler için ortam değişkeniyle ezilebilir) ---
POLL = int(os.environ.get("TAKIP_POLL_SECONDS", "90"))        # git yoklama aralığı
CANARY = int(os.environ.get("TAKIP_CANARY_SECONDS", "300"))   # bu kadar yaşayan sürüm "iyi"
CRASH_HIZLI = int(os.environ.get("TAKIP_CRASH_SECONDS", "120"))  # bundan kısa yaşam = hızlı çökme
CRASH_LIMIT = int(os.environ.get("TAKIP_CRASH_LIMIT", "3"))   # art arda bu kadar hızlı çökme → geri al
BACKOFF_TABAN = float(os.environ.get("TAKIP_BACKOFF_BASE", "10"))
BACKOFF_TAVAN = float(os.environ.get("TAKIP_BACKOFF_MAX", "600"))

if os.name == "nt":
    _VENV_PY = BASE / "venv" / "Scripts" / "python.exe"
else:
    _VENV_PY = BASE / "venv" / "bin" / "python"
BOT_PY = str(_VENV_PY) if _VENV_PY.exists() else sys.executable
BOT_CMD = (os.environ.get("TAKIP_BOT_CMD", "").split()
           or [BOT_PY, str(BASE / "takip_botu_pro.py")])
SMOKE_CMD = BOT_CMD + ["smoke"]

STATE_DOSYA = BASE / "guncelleyici_state.json"
LOG_DOSYA = BASE / "guncelleyici.log"


def log(mesaj):
    satir = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [gözetmen] {mesaj}"
    print(satir, flush=True)
    try:
        with open(LOG_DOSYA, "a", encoding="utf-8") as f:
            f.write(satir + "\n")
    except OSError:
        pass


# ---------------- Telegram (stdlib ile, bot koddan bağımsız) ----------------

def _telegram_ayar():
    """kurulum.yaml / products.yaml'dan token + chat_id (yaml modülü OLMADAN —
    gözetmen venv bozukken de bildirim atabilmeli)."""
    for dosya in (BASE / "kurulum.yaml", BASE / "products.yaml"):
        try:
            metin = dosya.read_text(encoding="utf-8")
        except OSError:
            continue
        t = re.search(r"^\s*telegram_bot_token:\s*['\"]?([A-Za-z0-9:_-]{20,})",
                      metin, re.M)
        c = re.search(r"^\s*telegram_chat_id:\s*['\"]?(-?\d+)", metin, re.M)
        if t and c:
            return t.group(1), c.group(1)
    return None, None


def bildir(mesaj):
    """Telegram'a gözetmen mesajı; token yoksa sadece loglar. Asla exception atmaz."""
    log(f"Bildirim: {mesaj}")
    token, chat = _telegram_ayar()
    if not token or not chat:
        return
    try:
        veri = urllib.parse.urlencode({
            "chat_id": chat, "text": "🛠 Gözetmen: " + mesaj,
            "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", veri, timeout=15)
    except Exception as e:
        log(f"Telegram bildirimi gönderilemedi: {type(e).__name__}: {e}")


# ---------------- git yardımcıları ----------------

def git(*args, zamanasimi=300):
    try:
        return subprocess.run(["git"] + list(args), cwd=str(BASE),
                              capture_output=True, text=True, timeout=zamanasimi)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, returncode=124,
                                           stdout="", stderr="zaman aşımı")


def git_ok(*args):
    r = git(*args)
    if r.returncode != 0:
        log(f"git {' '.join(args)} başarısız: {(r.stderr or r.stdout).strip()[:200]}")
    return r.returncode == 0


def git_cikti(*args):
    r = git(*args)
    return r.stdout.strip() if r.returncode == 0 else ""


def elle_degisiklik_var():
    """Takip edilen dosyalarda yerel değişiklik var mı? (untracked '??' sayılmaz —
    state/log/kurulum dosyaları zaten gitignore'da)."""
    r = git("status", "--porcelain")
    return any(s and not s.startswith("??") for s in r.stdout.splitlines())


# ---------------- durum ----------------

def durum_yukle():
    try:
        return json.loads(STATE_DOSYA.read_text(encoding="utf-8"))
    except Exception:
        return {}


def durum_kaydet(d):
    try:
        tmp = STATE_DOSYA.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_DOSYA)
    except OSError as e:
        log(f"Durum dosyası yazılamadı: {e}")


# ---------------- güncelleme ----------------

def _smoke_gecti():
    try:
        r = subprocess.run(SMOKE_CMD, cwd=str(BASE), capture_output=True,
                           text=True, timeout=180)
        cikti = (r.stdout + r.stderr).strip()
        log(f"Smoke testi rc={r.returncode}: {cikti.splitlines()[-1][:160] if cikti else ''}")
        return r.returncode == 0
    except Exception as e:
        log(f"Smoke testi çalıştırılamadı: {type(e).__name__}: {e}")
        return False


def guncelle(dal, durum):
    """origin'de yeni commit varsa uygular. Dönüş: None = değişiklik yok/uygulanmadı,
    'kod' = bot kodu güncellendi, 'kendim' = gözetmenin dosyası da değişti
    (çağıran, çocuğu durdurup os.execv yapmalı)."""
    if not git_ok("fetch", "origin", dal):
        return None
    yerel = git_cikti("rev-parse", "HEAD")
    uzak = git_cikti("rev-parse", f"origin/{dal}")
    if not yerel or not uzak or yerel == uzak:
        return None
    if uzak == durum.get("kotu_surum"):
        return None  # bu commit'i denedik, bozuktu — yenisi gelene kadar bekle
    if elle_degisiklik_var():
        simdi = time.time()
        if simdi - durum.get("kirli_uyari_ts", 0) > 3600:
            durum["kirli_uyari_ts"] = simdi
            durum_kaydet(durum)
            bildir("⚠️ Bot makinesinde elle yapılmış değişiklik var — güncelleme "
                   "BEKLETİLİYOR. Değişikliği geri al (git checkout .) veya commit'le.")
        return None

    log(f"Yeni sürüm bulundu: {yerel[:9]} → {uzak[:9]}")
    if not git_ok("reset", "--hard", uzak):
        return None
    on_ek = git_cikti("rev-parse", "--show-prefix")  # örn. "takip-botu-pro/"
    degisenler = git_cikti("diff", "--name-only", yerel, uzak).splitlines()

    def geri_al(neden):
        git_ok("reset", "--hard", yerel)
        durum["kotu_surum"] = uzak
        durum_kaydet(durum)
        bildir(f"⛔ Push bozuk görünüyor ({neden}) — {uzak[:9]} GERİ ALINDI, "
               f"{yerel[:9]} ile kesintisiz devam. Düzeltmeyi push'layınca "
               "otomatik alırım.")

    if on_ek + "requirements.txt" in degisenler:
        log("requirements.txt değişti — pip install çalıştırılıyor...")
        r = subprocess.run(BOT_CMD[:1] + ["-m", "pip", "install", "-r",
                                          str(BASE / "requirements.txt")],
                           cwd=str(BASE), capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            geri_al("pip install hatası")
            return None
        # tarayıcı sürümü de değişmiş olabilir; hata olsa bile devam (uyarı yeter)
        subprocess.run(BOT_CMD[:1] + ["-m", "playwright", "install", "chromium"],
                       cwd=str(BASE), capture_output=True, text=True, timeout=1800)

    if not _smoke_gecti():
        geri_al("smoke testi geçmedi")
        return None

    kendim = on_ek + "guncelleyici.py" in degisenler
    if kendim:
        # yeni gözetmen en azından derlenebiliyor mu? (kendini bozan push koruması)
        r = subprocess.run([sys.executable, "-m", "py_compile", str(Path(__file__))],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            geri_al("guncelleyici.py derlenmiyor")
            return None

    ozet = git_cikti("log", "-1", "--format=%h %s")
    bildir(f"⬆️ Güncelleme alındı: {ozet} — bot yeniden başlatılıyor.")
    return "kendim" if kendim else "kod"


# ---------------- bot süreci ----------------

def bot_baslat():
    log(f"Bot başlatılıyor: {' '.join(BOT_CMD)}")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True   # terminaldeki Ctrl+C botu ezmesin
    return subprocess.Popen(BOT_CMD, cwd=str(BASE), **kwargs)


def bot_durdur(cocuk):
    """Nazikçe durdur (SIGINT → bot tarayıcıyı kapatır), 45 sn'de inmezse öldür.
    (45 sn: devam eden bir sayfa yüklemesi 45 sn zaman aşımına sahip; zorla
    kapanış yine de güvenlidir — state/CSV atomik yazılır.)"""
    if cocuk is None or cocuk.poll() is not None:
        return
    log("Bot durduruluyor (nazik)...")
    try:
        if os.name == "nt":
            cocuk.terminate()
        else:
            cocuk.send_signal(signal.SIGINT)
        cocuk.wait(timeout=45)
    except subprocess.TimeoutExpired:
        log("Bot 45 sn'de inmedi — zorla kapatılıyor.")
        cocuk.kill()
        try:
            cocuk.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    except Exception as e:
        log(f"Durdurma hatası: {type(e).__name__}: {e}")


def kendini_yeniden_baslat():
    log("Gözetmen kendini yeniden başlatıyor (yeni sürüm)...")
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])


# ---------------- ana döngü ----------------

def main():
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    dal = git_cikti("rev-parse", "--abbrev-ref", "HEAD")
    if not dal or dal == "HEAD":
        log("HATA: git dalı bulunamadı (repo mu, detached HEAD mi?) — çıkılıyor.")
        sys.exit(1)
    durum = durum_yukle()
    log(f"Gözetmen başladı — dal: {dal}, bot: {' '.join(BOT_CMD)}, "
        f"poll: {POLL} sn, canary: {CANARY} sn")

    cocuk = None
    baslama_ts = 0.0
    iyi_isaretlendi = False
    hizli_cokme = 0
    son_yoklama = 0.0

    try:
        while True:
            if cocuk is None:
                # Çökmüşken bile önce güncellemeye bak: düzeltme push'landıysa al
                sonuc = guncelle(dal, durum)
                son_yoklama = time.time()
                if sonuc == "kendim":
                    kendini_yeniden_baslat()
                cocuk = bot_baslat()
                baslama_ts = time.time()
                iyi_isaretlendi = False
                continue

            rc = cocuk.poll()
            if rc is not None:
                yasam = time.time() - baslama_ts
                cocuk = None
                hizli_cokme = hizli_cokme + 1 if yasam < CRASH_HIZLI else 0
                log(f"Bot kapandı (rc={rc}, {yasam:.0f} sn yaşadı, "
                    f"hızlı çökme serisi: {hizli_cokme})")

                if hizli_cokme >= CRASH_LIMIT:
                    simdiki = git_cikti("rev-parse", "HEAD")
                    iyi = durum.get("son_iyi")
                    if iyi and simdiki and iyi != simdiki:
                        log(f"Çökme döngüsü — bilinen son iyi sürüme dönülüyor: {iyi[:9]}")
                        if git_ok("reset", "--hard", iyi):
                            durum["kotu_surum"] = simdiki
                            durum_kaydet(durum)
                            bildir(f"⛔ Yeni sürüm ({simdiki[:9]}) art arda çöktü — "
                                   f"{iyi[:9]} sürümüne OTOMATİK geri döndüm. "
                                   "Düzeltmeyi push'layınca alırım.")
                            hizli_cokme = 0
                    elif hizli_cokme == CRASH_LIMIT:
                        bildir(f"⚠️ Bot art arda çöküyor (rc={rc}) ve dönülecek daha "
                               "eski iyi sürüm yok. guncelleyici.log ve takip.log'a bak. "
                               "Denemeye devam ediyorum.")

                bekle = min(BACKOFF_TAVAN, BACKOFF_TABAN * (2 ** min(hizli_cokme, 6)))
                log(f"{bekle:.0f} sn sonra yeniden denenecek.")
                time.sleep(bekle)
                continue

            # bot yaşıyor
            if not iyi_isaretlendi and time.time() - baslama_ts >= CANARY:
                iyi_isaretlendi = True
                bas = git_cikti("rev-parse", "HEAD")
                if bas and durum.get("son_iyi") != bas:
                    durum["son_iyi"] = bas
                    durum_kaydet(durum)
                    log(f"Sürüm 'iyi' işaretlendi: {bas[:9]}")

            if time.time() - son_yoklama >= POLL:
                son_yoklama = time.time()
                try:
                    sonuc = guncelle(dal, durum)
                except Exception as e:       # güncelleme asla gözetmeni öldürmesin
                    log(f"Güncelleme hatası: {type(e).__name__}: {e}")
                    sonuc = None
                if sonuc:
                    bot_durdur(cocuk)
                    cocuk = None
                    if sonuc == "kendim":
                        kendini_yeniden_baslat()
                    continue

            time.sleep(2)
    finally:
        bot_durdur(cocuk)
        log("Gözetmen kapandı.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
