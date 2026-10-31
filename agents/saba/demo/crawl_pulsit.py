import json
import time
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

BASE = "https://pulsit.se/"
HOST = urlparse(BASE).hostname
LIMIT = 30
AGENT = "SabaResearchBot/0.1"

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data

def normalize(url):
    url = urldefrag(url)[0]
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return None
    if p.hostname not in (HOST, "www." + HOST):
        return None
    if p.path.lower().endswith(
        (".pdf", ".jpg", ".png", ".webp", ".zip", ".svg")
    ):
        return None
    return url

robots = RobotFileParser()
robots.set_url(urljoin(BASE, "/robots.txt"))
try:
    robots.read()
except Exception:
    print("Kunde inte läsa robots.txt. Avbryter.")
    raise SystemExit(1)

queue = deque([BASE])
seen = set()
results = []

while queue and len(results) < LIMIT:
    url = queue.popleft()
    if url in seen:
        continue
    seen.add(url)

    if not robots.can_fetch(AGENT, url):
        print("Hoppar över (robots.txt):", url)
        continue

    record = {"url": url}
    try:
        req = Request(url, headers={"User-Agent": AGENT})
        with urlopen(req, timeout=15) as response:
            record["status"] = response.status
            record["final_url"] = response.url
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                record["note"] = "Inte HTML"
            elif urlparse(response.url).hostname not in (HOST, "www." + HOST):
                record["note"] = "Omdirigerad utanför webbplatsen"
            else:
                html = response.read(2_000_000).decode(
                    "utf-8", errors="replace"
                )
                parser = PageParser()
                parser.feed(html)
                record["title"] = parser.title.strip()
                links = set()
                for href in parser.links:
                    link = normalize(urljoin(response.url, href))
                    if link:
                        links.add(link)
                        if link not in seen:
                            queue.append(link)
                record["internal_links"] = sorted(links)
    except Exception as exc:
        record["error"] = str(exc)

    results.append(record)
    print(f"[{len(results):02d}/{LIMIT}] {url} "
          f"→ {record.get('status', record.get('error', '?'))}")
    time.sleep(1)

report = {
    "site": BASE,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "pages": results,
}
output = Path("demo/pulsit-crawl.json")
output.write_text(
    json.dumps(report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(f"\nSaba: {len(results)} sidor undersökta.")
print(f"Rapport: {output.resolve()}")
