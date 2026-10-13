#!/usr/bin/env python3
"""Watch Apple Store pickup availability for iPhone 18 Pro Max 256GB (any color).

Runs headless real Chrome via Playwright (Apple 541s plain HTTP clients), calls
Apple's fulfillment endpoint from inside the page, logs to stdout, and sends a
push notification through ntfy when stock appears.

Config via env vars (or flags): see .env.example.
"""
import argparse
import logging
import os
import re
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

log = logging.getLogger("scraper")

BUY_URL = "https://www.apple.com/shop/buy-iphone/iphone-18-pro/6.9-inch-display-256gb-{color}-unlocked"
COLORS = ["black", "burgundy", "glacier", "silver"]
PART_RE = re.compile(r'name="product"[^>]*value="(M[A-Z0-9]{4}LL/A)"|value="(M[A-Z0-9]{4}LL/A)"[^>]*name="product"')

FETCH_JS = """async ([zip, parts]) => {
  const q = new URLSearchParams({pl: 'true', 'mts.0': 'regular', 'mts.1': 'compact', location: zip});
  parts.forEach((p, i) => q.set('parts.' + i, p));
  const r = await fetch('/shop/fulfillment-messages?' + q);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return (await r.json()).body.content.pickupMessage.stores;
}"""


def send_ntfy(text, urgent):
    """Real push notification via ntfy (https://ntfy.sh or self-hosted). No-op if unset."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    headers = {"Title": "iPhone 18 Pro Max in stock!" if urgent else "Apple scraper",
               "Priority": "urgent" if urgent else "default",
               "Tags": "iphone,rotating_light" if urgent else "information_source",
               "Click": BUY_URL.format(color=COLORS[0])}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    try:
        req = urllib.request.Request(f"{base}/{topic}", text.encode(), headers)
        urllib.request.urlopen(req, timeout=20).read()
        log.info("ntfy push sent")
    except Exception as e:
        log.error("ntfy send failed: %s", e)


def notify(text, urgent=False):
    """Send a notification."""
    send_ntfy(text, urgent)


def discover_parts(page):
    """Map color -> part number by reading each color's buy page."""
    parts = {}
    for color in COLORS:
        page.goto(BUY_URL.format(color=color), wait_until="domcontentloaded")
        page.wait_for_selector('input[name="product"][value*="LL/A"]', state="attached", timeout=30000)
        m = PART_RE.search(page.content())
        if m:
            parts[color] = m.group(1) or m.group(2)
    # let Apple's bot-protection scripts finish setting cookies before we call the API
    page.goto(BUY_URL.format(color=COLORS[0]), wait_until="load")
    page.wait_for_timeout(8000)
    return parts


def find_hits(page, zip_code, parts):
    stores = page.evaluate(FETCH_JS, [zip_code, list(parts.values())])
    by_part = {v: k for k, v in parts.items()}
    hits = {}
    for s in stores:
        for part, info in s["partsAvailability"].items():
            if info.get("pickupDisplay") == "available":
                hits[(by_part.get(part, part), s["storeNumber"])] = (
                    f"{by_part.get(part, part)} 256GB at Apple {s['storeName']} "
                    f"({s['city']}, {s['state']}, {s.get('storeDistanceWithUnit', '?')})")
    return len(stores), hits


def launch(p, headless):
    """Real Chrome; headless Chrome advertises 'HeadlessChrome' in its UA, which Apple blocks."""
    browser = p.chromium.launch(
        channel="chrome", headless=headless,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    probe = browser.new_page()
    ua = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
    probe.close()
    ctx = browser.new_context(user_agent=ua, viewport={"width": 1440, "height": 900},
                              locale="en-US", timezone_id="America/New_York")
    return browser, ctx


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=env("ZIP_CODE", "03103"))
    ap.add_argument("--interval", type=int, default=int(env("CHECK_INTERVAL", "120")),
                    help="seconds between checks; 0 = check once and exit")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    notified = {}        # (color, store) -> last notify time
    renotify = int(env("RENOTIFY_SECONDS", "1800"))
    fail_streak, alerted_down = 0, False

    with sync_playwright() as p:
        browser, ctx = launch(p, not args.headed)
        page = ctx.new_page()
        parts = None
        while True:
            try:
                if parts is None:
                    parts = discover_parts(page)
                    log.info("Tracking %s near %s", parts, args.zip)
                n, hits = find_hits(page, args.zip, parts)
                if fail_streak:
                    log.info("Recovered after %d failures", fail_streak)
                fail_streak, alerted_down = 0, False
                now = time.time()
                fresh = [t for k, t in hits.items() if now - notified.get(k, 0) > renotify]
                if hits:
                    log.info("AVAILABLE: %s", "; ".join(hits.values()))
                else:
                    log.info("Not available at any of %d stores", n)
                    if env("NOTIFY_ON_NOT_FOUND", "false").lower() in ("1", "true", "yes"):
                        notify(f"Check done: nothing available at {n} stores near {args.zip}.")
                if fresh:
                    for k in hits:
                        notified[k] = now
                    notify("iPhone 18 Pro Max 256GB available for pickup:\n- "
                           + "\n- ".join(fresh), urgent=True)
            except Exception as e:
                fail_streak += 1
                log.error("check failed (%d in a row): %s", fail_streak, e)
                if fail_streak >= 5 and not alerted_down:
                    alerted_down = True
                    notify("Apple scraper: 5 failed checks in a row (likely blocked).")
                if fail_streak % 3 == 0:  # reset browser state / profile
                    browser.close()
                    browser, ctx = launch(p, not args.headed)
                    page = ctx.new_page()
                    parts = None
                try:
                    page.goto(BUY_URL.format(color=COLORS[0]), wait_until="domcontentloaded")
                except Exception:
                    pass
            if not args.interval:
                break
            time.sleep(args.interval)
        browser.close()


if __name__ == "__main__":
    main()
