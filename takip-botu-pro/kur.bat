@echo off
rem =====================================================================
rem  TEK TIK KURULUM (Windows)
rem  Yapman gereken: bu dosyaya cift tikla. Gerisini sihirbaz halleder:
rem    1) Gerekli paketleri kurar (playwright, pyyaml, chromium)
rem    2) Telegram botunu baglar (token yapistir + botuna /start yaz)
rem    3) Istersen botu hemen baslatir
rem  Not: once python.org'dan Python kurulmus olmali ("Add to PATH" isaretli).
rem =====================================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo  HATA: Python bulunamadi!
    echo  https://www.python.org/downloads/ adresinden kur ve kurulumda
    echo  "Add Python to PATH" kutusunu isaretle, sonra tekrar dene.
    echo.
    pause
    exit /b 1
)

echo Gerekli paketler kuruluyor (ilk seferde birkac dakika surebilir)...
python -m pip install -r requirements.txt
python -m playwright install chromium

python takip_botu_pro.py kur
pause
