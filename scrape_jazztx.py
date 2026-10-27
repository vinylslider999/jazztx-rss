#!/usr/bin/env python3
"""Scrape https://tickets.jazztx.com/ and write an RSS 2.0 feed.

Usage:
    python scrape_jazztx.py --output docs/feed.xml
    python scrape_jazztx.py --input saved_page.html --output feed.xml   # offline test
"""
import argparse
import copy
import re
import sys
from datetime import datetime
from email.utils import format_datetime
from urllib.parse import urljoin
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://tickets.jazztx.com/"
TZ = ZoneInfo("America/Chicago")
SHOW_HREF = re.compile(r"/shows/(\d+)/(\d{4}-\d{2}-\d{2})")
SHOWTIME = re.compile(r"(\(\s*SOLD OUT\s*\)\s*)?(\d{1,2}:\d{2}\s*[AP]M)\s*SHOW", re.I)
LOGO = "https://assets-prod.turntabletickets.com/media/jazztx/20230103-1934-jazztx-logo.png"


def fetch(url):
    """Plain download: only returns the first batch of shows."""
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (jazztx-rss-feed)"})
    r.raise_for_status()
    return r.text


def fetch_rendered(url, max_rounds=150):
    """Open the page in a real headless browser and keep scrolling until no
    more shows load (the site loads additional shows as you scroll)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(url, wait_until="networkidle", timeout=60000)
        last, stable = -1, 0
        for _ in range(max_rounds):
            count = page.locator("h2").count()
            if count == last:
                stable += 1
                if stable >= 4:      # nothing new after 4 tries -> done
                    break
            else:
                stable, last = 0, count
            # Click a "load more" style button if the site has one.
            try:
                btn = page.get_by_role("button", name=re.compile(r"(load|show|view) more", re.I))
                if btn.count():
                    btn.first.click(timeout=2000)
            except Exception:
                pass
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
        print(f"Browser scrolling finished with {last} headings on the page.")
        html = page.content()
        browser.close()
    return html


def find_block(h2):
    """Return a Tag holding everything that belongs to one show."""
    block, node = h2, h2.parent
    # Grow upward while the container still holds only this show's h2.
    while node is not None and node.name not in ("body", "html", "[document]") \
            and len(node.find_all("h2")) == 1:
        block, node = node, node.parent
    if block.find("a", href=SHOW_HREF):
        return block
    # Flat layout fallback: image before the h2, siblings after it until the next h2.
    wrapper = BeautifulSoup("<div></div>", "html.parser").div
    prev = h2.find_previous_sibling()
    if prev is not None and (prev.name == "img" or prev.find("img")):
        wrapper.append(copy.copy(prev))
    wrapper.append(copy.copy(h2))
    for sib in h2.find_next_siblings():
        if sib.name == "h2":
            break
        wrapper.append(copy.copy(sib))
    return wrapper


def parse_time(date_str, time_str):
    t = datetime.strptime(re.sub(r"\s+", " ", time_str.upper()).replace(" ", ""), "%I:%M%p")
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.replace(hour=t.hour, minute=t.minute, tzinfo=TZ)


def parse_shows(html):
    soup = BeautifulSoup(html, "html.parser")
    shows, seen = [], set()
    for h2 in soup.find_all("h2"):
        block = find_block(h2)
        link = block.find("a", href=SHOW_HREF)
        if not link:
            continue
        m = SHOW_HREF.search(link["href"])
        show_id, date_str = m.groups()
        url = urljoin(BASE, m.group(0))
        if url in seen:
            continue
        seen.add(url)

        title = h2.get_text(" ", strip=True)
        h3 = block.find("h3")
        date_label = h3.get_text(" ", strip=True) if h3 else date_str
        img = block.find("img")
        image = urljoin(BASE, img["src"]) if img and img.get("src") else None

        text = block.get_text(" ", strip=True)
        times = []
        for sold, t in SHOWTIME.findall(text):
            t = re.sub(r"\s+", " ", t.upper())
            if t not in [x[0] for x in times]:
                times.append((t, bool(sold)))

        paras = []
        for p in block.find_all("p"):
            ptxt = p.get_text(" ", strip=True)
            if ptxt and not SHOWTIME.fullmatch(ptxt):
                paras.append(ptxt)

        try:
            pub = parse_time(date_str, times[0][0]) if times else parse_time(date_str, "12:00 AM")
        except ValueError:
            pub = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=TZ)

        shows.append(dict(id=show_id, title=title, url=url, date_label=date_label,
                          image=image, times=times, paras=paras, pub=pub))
    return shows


def build_rss(shows, feed_url=None):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
           "<channel>",
           "<title>Jazz TX – Upcoming Shows</title>",
           f"<link>{BASE}</link>",
           "<description>Upcoming shows at Jazz TX.</description>",
           "<language>en-us</language>"]
    if feed_url:
        out.append(f'<atom:link href="{escape(feed_url)}" rel="self" type="application/rss+xml"/>')
    out.append(f"<image><url>{LOGO}</url><title>Jazz TX</title><link>{BASE}</link></image>")

    for s in shows:
        all_sold = s["times"] and all(sold for _, sold in s["times"])
        title = f'{s["title"]} – {s["date_label"]}' + (" (SOLD OUT)" if all_sold else "")
        times_txt = ", ".join(t + (" (SOLD OUT)" if sold else "") for t, sold in s["times"]) or "See website"
        body = ""
        if s["image"]:
            body += f'<p><img src="{escape(s["image"])}" alt="{escape(s["title"])}" width="400"></p>'
        body += f"<p><strong>Showtimes:</strong> {escape(times_txt)}</p>"
        body += "".join(f"<p>{escape(p)}</p>" for p in s["paras"])
        out += ["<item>",
                f"<title>{escape(title)}</title>",
                f'<link>{escape(s["url"])}</link>',
                f'<guid isPermaLink="true">{escape(s["url"])}</guid>',
                f'<pubDate>{format_datetime(s["pub"])}</pubDate>',
                f"<description>{escape(body)}</description>",
                "</item>"]
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=BASE)
    ap.add_argument("--input", help="Parse a saved HTML file instead of fetching")
    ap.add_argument("--output", default="feed.xml")
    ap.add_argument("--feed-url", help="Public URL of the feed (for atom:link self)")
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            html = f.read()
    else:
        try:
            html = fetch_rendered(args.url)
        except Exception as e:  # playwright missing or browser failed
            print(f"WARNING: browser fetch failed ({e!r}); falling back to plain download, "
                  "which only returns the first few shows.", file=sys.stderr)
            html = fetch(args.url)

    shows = parse_shows(html)
    if not shows:
        # Don't overwrite a good feed with an empty one if the page layout changed.
        sys.exit("No shows found - page layout may have changed. Feed not updated.")

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(build_rss(shows, args.feed_url))
    print(f"Wrote {len(shows)} shows to {args.output}")


if __name__ == "__main__":
    main()
