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
import random
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

log = logging.getLogger("scraper")

BUY_URL = "https://www.apple.com/shop/buy-iphone/iphone-18-pro/6.9-inch-display-256gb-{color}-unlocked"
COLORS = ["black", "burgundy", "glacier", "silver"]
# 256GB iPhone 18 Pro Max part numbers (looked up from each color's buy page)
PARTS = {"black": "MJW44LL/A", "burgundy": "MJW64LL/A", "glacier": "MJW74LL/A", "silver": "MJW54LL/A"}

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
    proxy = None
    if os.environ.get("PROXY_URL"):  # e.g. http://user:pass@host:port (residential proxy)
        u = urllib.parse.urlparse(os.environ["PROXY_URL"])
        proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            proxy.update(username=urllib.parse.unquote(u.username),
                         password=urllib.parse.unquote(u.password or ""))
    browser = p.chromium.launch(
        channel="chrome", headless=headless, proxy=proxy,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    probe = browser.new_page()
    ua = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
    probe.close()
    ctx = browser.new_context(user_agent=ua, viewport={"width": 1440, "height": 900},
                              locale="en-US", timezone_id="America/New_York")
    return browser, ctx


class Session:
    """A browser session warmed up on an Apple page so bot-protection cookies are set."""

    def __init__(self, p, headless):
        self.p, self.headless = p, headless
        self.browser = self.page = None

    def start(self):
        self.close()
        self.browser, ctx = launch(self.p, self.headless)
        self.page = ctx.new_page()
        self.page.goto(BUY_URL.format(color=COLORS[0]), wait_until="load")
        self.page.wait_for_timeout(8000)

    def close(self):
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = self.page = None


def check_with_retries(sess, zip_code, parts, attempts=3):
    """Apple randomly 541s; retry on a brand-new browser session before giving up."""
    for i in range(1, attempts + 1):
        try:
            if sess.page is None:
                sess.start()
            return find_hits(sess.page, zip_code, parts)
        except Exception as e:
            log.warning("attempt %d/%d failed: %s", i, attempts, str(e).splitlines()[0])
            sess.close()
            if i == attempts:
                raise
            time.sleep(random.uniform(10, 30) * i)


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
    session_started = 0.0

    with sync_playwright() as p:
        sess = Session(p, not args.headed)
        while True:
            try:
                if time.time() - session_started > 1800:   # refresh cookies every 30 min
                    sess.close()
                    session_started = time.time()
                n, hits = check_with_retries(sess, args.zip, PARTS)
                if fail_streak:
                    log.info("Recovered after %d failed cycles", fail_streak)
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
                log.error("cycle failed (%d in a row): %s", fail_streak, str(e).splitlines()[0])
                if fail_streak >= 5 and not alerted_down:
                    alerted_down = True
                    notify("Apple scraper: 5 failed cycles in a row (likely blocked).")
            if not args.interval:
                break
            # jitter so requests don't look machine-regular; back off while failing
            wait = args.interval * random.uniform(0.8, 1.3) * min(1 + fail_streak, 5)
            time.sleep(wait)
        sess.close()


if __name__ == "__main__":
    main()
