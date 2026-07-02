@echo off
rem =====================================================================
rem  Takip Botu watchdog (Windows): bot cokerse 15 sn sonra yeniden baslar.
rem  7/24 otomatik baslatma icin:
rem    Win+R -> shell:startup -> acilan klasore bu dosyanin KISAYOLUNU koy.
rem  (Bot yeniden basladiginda Telegram'a "bot baslatildi" mesaji gelir;
rem   30 dk icinde tekrar tekrar basliyorsa mesaj atlanir, takip.log'a bak.)
rem =====================================================================
cd /d "%~dp0"
:dongu
python takip_botu_pro.py
echo.
echo Bot kapandi. 15 saniye icinde yeniden baslatilacak...
echo (Tamamen durdurmak icin bu pencereyi kapat)
timeout /t 15 /nobreak >nul
goto dongu
