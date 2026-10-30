"""Visible Playwright demonstration with Bézier pointer movement and screenshot.

Run from agents/saba: python -m demo.run_demo
Requires: pip install playwright; python -m playwright install chromium
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from saba.motion import generate_path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"

async def main():
    OUTPUT.mkdir(exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False, slow_mo=100)
        page = await browser.new_page(viewport={"width": 1100, "height": 760}, record_video_dir=str(OUTPUT))
        try:
            await page.goto((ROOT / "test_page.html").as_uri())
            button = page.locator("#start")
            box = await button.bounding_box()
            if box is None:
                raise RuntimeError("Button not visible")
            target = (box["x"] + box["width"]/2, box["y"] + box["height"]/2)
            points = generate_path((120, 160), target, duration=1.3, fps=60, seed=42)
            for prev, point in zip(points, points[1:]):
                await page.mouse.move(point.x, point.y)
                await asyncio.sleep(point.t - prev.t)
            await page.mouse.click(*target)
            await page.locator("#status").get_by_text("Uppdrag slutfört!").wait_for()
            await page.screenshot(path=str(OUTPUT / "saba-result.png"))
            print("PASS: click verified; screenshot and video saved in", OUTPUT)
            await asyncio.sleep(1)
        finally:
            await page.close()
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
