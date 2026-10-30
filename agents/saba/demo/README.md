# First visible Saba test

From the repository root:

```bash
cd agents/saba
python -m pip install playwright
python -m playwright install chromium
python -m unittest discover -s tests -v
python -m demo.run_demo
```

A Chromium window opens a local HTML page. Saba's demo moves the cursor along a reproducible cubic Bézier path, clicks a button, verifies the text change and saves a screenshot and Playwright video in `demo/output/`.

Requires a graphical desktop (Windows, macOS, Linux with display). For a headless server, configure a virtual display or remote desktop first. This is a scripted end-to-end demonstration, **not yet an autonomous LLM-driven agent**. No logins, accounts, or trading.
