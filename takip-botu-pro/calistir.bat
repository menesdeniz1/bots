@echo off
rem =====================================================================
rem  Takip Botu 7/24 (Windows): bu dosya GOZETMENI (guncelleyici.py) ayakta
rem  tutar; gozetmen botu ayakta tutar VE git'ten otomatik gunceller
rem  (bozuk push'u geri alir, duzeltme gelince kendini toparlar).
rem  Otomatik baslatma icin:
rem    Win+R -> shell:startup -> acilan klasore bu dosyanin KISAYOLUNU koy.
rem  Loglar: guncelleyici.log (gozetmen) + takip.log (bot)
rem =====================================================================
cd /d "%~dp0"
:dongu
python guncelleyici.py
echo.
echo Gozetmen kapandi. 15 saniye icinde yeniden baslatilacak...
echo (Tamamen durdurmak icin bu pencereyi kapat)
timeout /t 15 /nobreak >nul
goto dongu
