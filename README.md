# Personal automation experiments

**Archived:** an older collection of two experiments: `stock-bot` and `anti-idle-bot`. For separate portfolio projects, see [Tracker](https://github.com/menesdeniz1/tracker), [KeepMoney](https://github.com/menesdeniz1/keepmoney) and [KeepAwake](https://github.com/menesdeniz1/keepawake). These are related applications, not a claim that this repository is an identical copy of them.

Python experiments for product monitoring and Windows desktop idle management. This is a collection of prototypes, not a hosted service or a guaranteed retailer integration.

## Stock monitor

`stock-bot/stock-bot.py` uses asyncio and Playwright, site-specific YAML configuration, bounded concurrency and a serialized WhatsApp Web notification path. It requires your own browser login and destination number. Retailer page changes can break selectors.

Use Python 3.10 or later in a virtual environment:

```sh
python -m pip install PyYAML playwright
python -m playwright install chromium
```

Set `STOCKBOT_PHONE_E164` to your own number in international digits-only format. Optionally set `STOCKBOT_DATA_DIR` to an absolute directory **outside this checkout**. The default is `~/.local/share/stock-bot`. Review `stock-bot/products.yaml` and `stock-bot/sites.yaml`, then run:

```sh
python stock-bot/stock-bot.py
```

Running it opens a browser, requests external websites and can send notifications. Use only accounts and sites you are authorized to automate, respect their terms, and begin with a small configuration. No live automation was run during publication review.

## Desktop tools

`anti-idle-bot/` contains Windows-specific ctypes/Tkinter experiments and a PyInstaller build helper. They can affect mouse/keyboard activity and power behavior; inspect the selected script first and use only on a machine where this is permitted. They have not been cross-platform or packaged-build tested in this maintenance pass.

## Privacy and verification

Browser profiles, cookies, captured pages/images and personal phone defaults were removed from reachable history before publication. Main-branch browser profiles and logs are stored outside the checkout. Historical development branches are experiments and do not necessarily include the latest runtime guards; use main.

The history scan is a limited pattern check, not a security guarantee. Old provider sessions, external clones and cached commits are not invalidated by repository cleanup. Do not commit runtime data. See [maintenance notes](PUBLICATION_NOTES.md).
