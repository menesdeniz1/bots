# -*- coding: utf-8 -*-
"""
Fiyat geçmişi grafiği — fiyat_gecmisi.csv → fiyat_grafigi.html
Ürün başına bir çizgi grafik; aynı ürünü birden çok siteden izliyorsan her site
ayrı seri olur. Hedef fiyat kesikli çizgiyle gösterilir. Koyu/açık tema otomatik.

Kullanım:
  python grafik.py                  → fiyat_grafigi.html üretir
  Telegram'dan /grafik komutu       → bot üretip dosya olarak gönderir
"""

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
HISTORY_CSV = BASE_DIR / "fiyat_gecmisi.csv"
PRODUCTS_YAML = BASE_DIR / "products.yaml"
TELEGRAM_URUNLER = BASE_DIR / "telegram_urunler.yaml"
OUT_HTML = BASE_DIR / "fiyat_grafigi.html"
CHARTJS_LOCAL = BASE_DIR / "chartjs.umd.min.js"   # gömülürse HTML internetsiz açılır
CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"

# Doğrulanmış kategorik palet (scripts/validate_palette ile test edildi):
# açık modda en kötü komşu ΔE 24.2. Site → renk eşlemesi ilk görülme sırasına
# göre SABİTTİR: filtre/yeni site eklenince mevcut sitelerin rengi değişmez.
LIGHT = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
DARK = ["#3987e5", "#199e70", "#c98500", "#008300", "#9085e9", "#e66767", "#d55181", "#d95926"]


def _epoch_ms(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except ValueError:
        return None


def _read_history(csv_path: Path) -> list[dict]:
    """CSV'yi okur. Hem v2 (zaman;urun;fiyat;stok;kaynak) hem v3
    (zaman;urun;site;fiyat;stok;kaynak) düzenini tanır."""
    rows = []
    with open(csv_path, encoding="utf-8") as f:
        rd = csv.reader(f, delimiter=";")
        header = next(rd, None)
        if not header:
            return rows
        v3 = "site" in header
        for r in rd:
            try:
                if v3:
                    zaman, urun, site, fiyat = r[0], r[1], r[2], r[3]
                else:
                    zaman, urun, site, fiyat = r[0], r[1], "?", r[2]
                if not fiyat:
                    continue
                t = _epoch_ms(zaman)
                if t is None:
                    continue
                rows.append({"t": t, "zaman": zaman, "urun": urun,
                             "site": site, "fiyat": float(fiyat)})
            except (IndexError, ValueError):
                continue
    return rows


def _thresholds(products_yaml: Path) -> dict[str, float]:
    """Hedef fiyatlar: products.yaml + telegram_urunler.yaml birleşimi.
    Telegram'dan /ekle ile gelen ürünler ve /hedef değişiklikleri de
    grafikte hedef çizgisi olarak görünsün."""
    out: dict[str, float] = {}
    try:
        cfg = yaml.safe_load(products_yaml.read_text(encoding="utf-8")) or {}
        out = {p.get("label", ""): float(p["price_threshold_tl"])
               for p in cfg.get("products", []) if p.get("price_threshold_tl")}
    except Exception:
        pass
    try:
        tg_path = products_yaml.parent / TELEGRAM_URUNLER.name
        tg = yaml.safe_load(tg_path.read_text(encoding="utf-8")) or {}
        for p in tg.get("eklenen") or []:
            if p.get("price_threshold_tl"):
                out[p.get("label", "")] = float(p["price_threshold_tl"])
        for label, hedef in (tg.get("hedefler") or {}).items():
            out[label] = float(hedef)
    except Exception:
        pass
    return out


def generate(csv_path: Path = HISTORY_CSV, out_path: Path = OUT_HTML,
             products_yaml: Path = PRODUCTS_YAML) -> Path:
    if not csv_path.exists():
        raise FileNotFoundError(
            f"{csv_path} yok — bot en az bir fiyat okumadan grafik çıkmaz.")
    rows = _read_history(csv_path)
    if not rows:
        raise ValueError(f"{csv_path} içinde çizilecek fiyat kaydı yok.")
    hedefler = _thresholds(products_yaml)

    # Site → renk slotu: TÜM veri üzerinde ilk görülme sırası (kalıcı eşleme)
    site_slot: dict[str, int] = {}
    for r in rows:
        if r["site"] not in site_slot:
            site_slot[r["site"]] = len(site_slot) % len(LIGHT)

    # Ürün → site → noktalar (zaman sıralı)
    urun_sirasi: list[str] = []
    seriler: dict[str, dict[str, list]] = {}
    for r in sorted(rows, key=lambda x: x["t"]):
        if r["urun"] not in seriler:
            seriler[r["urun"]] = {}
            urun_sirasi.append(r["urun"])
        seriler[r["urun"]].setdefault(r["site"], []).append([r["t"], r["fiyat"]])

    data = []
    for urun in urun_sirasi:
        siteler = [{"site": s, "slot": site_slot[s], "points": pts}
                   for s, pts in seriler[urun].items()]
        tablo = sorted(
            ([r["zaman"], r["site"], r["fiyat"]] for r in rows if r["urun"] == urun),
            key=lambda x: x[0], reverse=True)[:30]
        son = max((p for s in siteler for p in s["points"]), key=lambda p: p[0])
        en_dusuk = min(p[1] for s in siteler for p in s["points"])
        data.append({"label": urun, "hedef": hedefler.get(urun),
                     "son": son[1], "enDusuk": en_dusuk,
                     "series": siteler, "table": tablo})

    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    # Chart.js yerelde varsa HTML'e gömülür → dosya tek başına, internetsiz açılır
    # (Telegram'dan indirip telefonda açarken de çalışır); yoksa CDN'e düşülür.
    if CHARTJS_LOCAL.exists():
        chartjs = "<script>" + CHARTJS_LOCAL.read_text(encoding="utf-8").replace(
            "</script", "<\\/script") + "</script>"
    else:
        chartjs = f'<script src="{CHARTJS_CDN}"></script>'
    html = _HTML.replace("__CHARTJS__", chartjs).replace("__DATA__", payload)
    out_path.write_text(html, encoding="utf-8")
    return out_path


_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fiyat Geçmişi</title>
__CHARTJS__
<style>
  :root {
    --surface: #fcfcfb; --page: #f9f9f7;
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface: #1a1a19; --page: #0d0d0d;
      --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; background: var(--page); color: var(--ink);
         font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: var(--ink-2); margin: 0 0 20px; }
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 16px 16px 8px; margin-bottom: 20px; }
  .card h2 { font-size: 15px; margin: 0 0 2px; }
  .stats { color: var(--ink-2); font-size: 13px; margin: 0 0 10px;
           font-variant-numeric: tabular-nums; }
  .stats b { color: var(--ink); font-weight: 600; }
  .plot { position: relative; height: 260px; }
  details { margin: 8px 0 6px; }
  summary { cursor: pointer; color: var(--ink-2); font-size: 13px; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px;
          font-variant-numeric: tabular-nums; font-size: 13px; }
  th, td { text-align: left; padding: 4px 10px 4px 0; color: var(--ink-2);
           border-bottom: 1px solid var(--grid); }
  th { color: var(--muted); font-weight: 500; }
  td.f { text-align: right; color: var(--ink); }
</style>
</head>
<body>
<h1>Fiyat Geçmişi</h1>
<p class="sub">Takip botu kayıtları — kesikli çizgi: hedef fiyat</p>
<div id="charts"></div>
<script>
const DATA = __DATA__;
const LIGHT = ["#2a78d6","#1baf7a","#eda100","#008300","#4a3aa7","#e34948","#e87ba4","#eb6834"];
const DARK  = ["#3987e5","#199e70","#c98500","#008300","#9085e9","#e66767","#d55181","#d95926"];
const mq = window.matchMedia("(prefers-color-scheme: dark)");
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmtTL = v => v.toLocaleString("tr-TR", {minimumFractionDigits: 0, maximumFractionDigits: 2}) + " TL";
const fmtTL2 = v => v.toLocaleString("tr-TR", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + " TL";
const fmtGun = t => new Date(t).toLocaleDateString("tr-TR", {day: "2-digit", month: "2-digit"});
const fmtTam = t => new Date(t).toLocaleString("tr-TR",
  {day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit"});

let charts = [];
function render() {
  charts.forEach(c => c.destroy()); charts = [];
  const root = document.getElementById("charts"); root.innerHTML = "";
  const pal = mq.matches ? DARK : LIGHT;

  for (const p of DATA) {
    const card = document.createElement("div"); card.className = "card";
    const hedefTxt = p.hedef ? ` &nbsp;·&nbsp; hedef <b>${fmtTL(p.hedef)}</b>` : "";
    card.innerHTML = `<h2>${p.label}</h2>
      <p class="stats">son <b>${fmtTL(p.son)}</b> &nbsp;·&nbsp; en düşük <b>${fmtTL(p.enDusuk)}</b>${hedefTxt}</p>
      <div class="plot"><canvas></canvas></div>`;
    root.appendChild(card);

    const ds = p.series.map(s => ({
      label: s.site,
      data: s.points.map(pt => ({x: pt[0], y: pt[1]})),
      borderColor: pal[s.slot], backgroundColor: pal[s.slot],
      borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
      pointHoverBorderWidth: 2, pointHoverBackgroundColor: css("--surface"),
      tension: 0.15, spanGaps: true,
    }));
    const ts = p.series.flatMap(s => s.points.map(pt => pt[0]));
    if (p.hedef) ds.push({
      label: "Hedef",
      data: [{x: Math.min(...ts), y: p.hedef}, {x: Math.max(...ts), y: p.hedef}],
      borderColor: css("--muted"), borderDash: [6, 4], borderWidth: 1.5,
      pointRadius: 0, pointHoverRadius: 0,
    });

    charts.push(new Chart(card.querySelector("canvas"), {
      type: "line",
      data: {datasets: ds},
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: "index", intersect: false},
        scales: {
          x: {type: "linear", grid: {color: css("--grid")}, border: {color: css("--grid")},
              ticks: {color: css("--muted"), maxTicksLimit: 8, callback: v => fmtGun(v)}},
          y: {grid: {color: css("--grid")}, border: {display: false},
              ticks: {color: css("--muted"), callback: v => fmtTL(v)}},
        },
        plugins: {
          legend: {display: ds.length > 1, position: "bottom",
                   labels: {color: css("--ink-2"), boxWidth: 12, boxHeight: 12, usePointStyle: false}},
          tooltip: {
            backgroundColor: css("--surface"), titleColor: css("--ink"),
            bodyColor: css("--ink-2"), borderColor: css("--grid"), borderWidth: 1,
            callbacks: {
              title: items => fmtTam(items[0].parsed.x),
              label: item => ` ${item.dataset.label}: ${fmtTL2(item.parsed.y)}`,
            },
          },
        },
      },
    }));

    const det = document.createElement("details");
    det.innerHTML = `<summary>Tablo görünümü (son ${p.table.length} okuma)</summary>
      <table><tr><th>Zaman</th><th>Site</th><th style="text-align:right">Fiyat</th></tr>
      ${p.table.map(r => `<tr><td>${r[0].replace("T", " ")}</td><td>${r[1]}</td>
        <td class="f">${fmtTL2(r[2])}</td></tr>`).join("")}</table>`;
    card.appendChild(det);
  }
}
mq.addEventListener("change", render);
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    try:
        yol = generate()
        print(f"Grafik üretildi: {yol}")
    except (FileNotFoundError, ValueError) as e:
        print(f"HATA: {e}")
        sys.exit(1)
