#!/usr/bin/env python3
"""
Decathlon repair-slot watcher.

Loads the Decathlon "select a time slot" booking page in a REAL headless browser
(Chromium via Playwright), reads the slot dates straight from the rendered DOM
(reliable, unlike a plain HTTP fetch which races the page's JavaScript), works out
the earliest available date, and — if that date is earlier than a target you set —
pushes an alert to your phone (ntfy) and optionally emails you.

Configure with environment variables (all optional except NTFY_TOPIC):

  WATCH_URL      The booking page URL to watch. Defaults to the store-243
                 e-bike (VAE) repair page.
  TARGET_BEFORE  Alert when the earliest available date is STRICTLY BEFORE this
                 date (format YYYY-MM-DD). Default 2026-09-07 (i.e. anything in
                 August or earlier than the current first opening of 7 Sept).
  NTFY_TOPIC     Your ntfy topic name, e.g. "decathlon-rdv-ahmed-7x2k9".
                 Subscribe to it in the free ntfy app to get phone pushes.
  NTFY_SERVER    ntfy server. Default https://ntfy.sh
  NTFY_EMAIL     Optional: an email address ntfy will ALSO send the alert to.
  STATE_FILE     Where the last-alerted date is remembered (to avoid repeat
                 pings for the same slot). Default state/last_earliest.txt
"""

import os
import re
import sys
import datetime
import urllib.request

from playwright.sync_api import sync_playwright

# Note: GitHub Actions passes an EMPTY string (not "unset") for a ${{ vars.X }}
# that isn't defined, so we use `or default` rather than get(key, default).
URL = os.environ.get("WATCH_URL") or (
    "https://booking.decathlon.net/country/FR/steps/time-slots"
    "?mode=storeLocator&storeId=243&shop=a4f4a59a-92ed-46a4-ab7a-446f1e41a332"
    "&category=REPAIR&sku=fc0003fc-41f9-4bd0-a153-809e0ef14866"
)
TARGET_BEFORE = (os.environ.get("TARGET_BEFORE") or "2026-09-07").strip()
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_EMAIL = os.environ.get("NTFY_EMAIL", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state/last_earliest.txt")

# Weekday / month names in English and French (the widget follows the browser
# language; we accept both so it works either way).
WEEKDAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche",
}
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "décembre": 12, "decembre": 12,
}

DATE_RE = re.compile(r"^\s*(\w+)\s+(\d{1,2})\s+(\w+)\s*$", re.UNICODE)
TIME_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*$")


def parse_dates(text: str, today: datetime.date):
    """Return an ordered list of (date, 'Original heading') found in the page text."""
    found = []
    for line in text.splitlines():
        m = DATE_RE.match(line)
        if not m:
            continue
        wd, day, mon = m.group(1).lower(), int(m.group(2)), m.group(3).lower()
        if wd not in WEEKDAYS or mon not in MONTHS:
            continue
        month = MONTHS[mon]
        # No year on the page: pick the next occurrence of that day/month.
        year = today.year
        if (month, day) < (today.month, today.day):
            year += 1
        try:
            d = datetime.date(year, month, day)
        except ValueError:
            continue
        found.append((d, line.strip()))
    return found


def times_under(text: str, heading: str):
    """Collect the HH:MM times listed under a given date heading."""
    times, capture = [], False
    for line in text.splitlines():
        s = line.strip()
        if s == heading:
            capture = True
            continue
        if capture:
            if DATE_RE.match(line) and line.strip().split()[0].lower() in WEEKDAYS:
                break  # reached the next date
            tm = TIME_RE.match(line)
            if tm:
                times.append(tm.group(1))
    return times


def read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_state(value: str):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(value)


def _ntfy_post(title: str, message: str, click: str, email: str = "") -> int:
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
    )
    # Header values must be ASCII — keep title/tags plain, details go in the body.
    req.add_header("Title", title)
    req.add_header("Priority", "high")
    req.add_header("Tags", "bell,bike")
    req.add_header("Click", click)
    if email:
        req.add_header("Email", email)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def notify(title: str, message: str, click: str):
    if not NTFY_TOPIC:
        print("!! NTFY_TOPIC not set — cannot send push. Message was:\n" + message)
        return
    # 1) Phone push — the reliable channel (no email header).
    try:
        print(f"   ntfy push status: {_ntfy_post(title, message, click)}")
    except Exception as e:
        print(f"   ntfy push FAILED: {e}")
    # 2) Email copy — best-effort. ntfy.sh anonymous email can be rate-limited or
    #    rejected (HTTP 400), so never let it break the push or fail the job.
    if NTFY_EMAIL:
        try:
            print(f"   ntfy email status: {_ntfy_post(title, message, click, NTFY_EMAIL)}")
        except Exception as e:
            print(f"   ntfy email skipped (push still sent): {e}")


def fetch_page_text() -> str:
    """Load the page in a real browser and return the fully-rendered body text."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(
            locale="en-GB",
            extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        # Best-effort: dismiss a cookie/consent banner if one appears.
        for label in ["Refuser", "Reject", "Tout refuser", "Accepter", "Accept", "OK", "J'accepte"]:
            try:
                page.get_by_role("button", name=re.compile(label, re.I)).first.click(timeout=1500)
                break
            except Exception:
                pass

        # Wait (up to ~40s) for the JS-rendered date headings to appear.
        text = ""
        for _ in range(40):
            text = page.evaluate("document.body ? document.body.innerText : ''") or ""
            if parse_dates(text, datetime.date.today()):
                break
            page.wait_for_timeout(1000)

        browser.close()
        return text


def main():
    today = datetime.date.today()
    try:
        target = datetime.date.fromisoformat(TARGET_BEFORE)
    except ValueError:
        print(f"Bad TARGET_BEFORE={TARGET_BEFORE!r} (want YYYY-MM-DD)")
        sys.exit(2)

    print(f"[{today}] Watching {URL}")
    print(f"          Alert if earliest available date is BEFORE {target}")

    text = fetch_page_text()
    dates = parse_dates(text, today)

    if not dates:
        # No slots visible (fully booked, or the page didn't render this run).
        # Stay quiet so a transient blank doesn't spam you.
        print("No date headings found this run (no availability, or page didn't render). No alert.")
        return

    earliest, heading = dates[0]
    times = times_under(text, heading)
    all_dates = ", ".join(d.isoformat() for d, _ in dates[:8])
    print(f"Earliest available: {earliest} ({heading})")
    print(f"Times under earliest: {', '.join(times) if times else '(collapsed / none read)'}")
    print(f"Next dates seen: {all_dates}")

    last_alerted = read_state()

    if earliest < target and earliest.isoformat() != last_alerted:
        when = earliest.strftime("%A %d %B %Y")
        t = (" — times: " + ", ".join(times)) if times else ""
        body = (
            f"An earlier repair slot opened: {when}{t}.\n"
            f"(Target was before {target}.)\n"
            f"Book here: {URL}"
        )
        print("ALERT — sending notification.")
        notify("Earlier Decathlon repair slot", body, URL)
        write_state(earliest.isoformat())
    elif earliest >= target:
        # Back to no-improvement; reset so a future earlier slot re-alerts.
        if last_alerted:
            write_state("")
        print("No earlier slot than target. No alert.")
    else:
        print(f"Earliest ({earliest}) already alerted. No repeat.")


if __name__ == "__main__":
    main()
