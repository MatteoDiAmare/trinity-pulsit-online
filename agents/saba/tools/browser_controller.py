"""Playwright-backed browser controller; optional dependency.

Install: pip install playwright && playwright install chromium
Use only on services where automation is authorized.
"""
class BrowserController:
    def __init__(self, page):
        self.page = page

    async def move(self, x, y):
        await self.page.mouse.move(x, y)

    async def click(self, x, y):
        await self.page.mouse.click(x, y)

    async def screenshot(self, path):
        await self.page.screenshot(path=path)
