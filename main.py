"""
One Piece Card Game – Event Monitor Bot
Scrapes the official events page every hour, filters for Regional and Treasure Cup
events, and posts Telegram notifications for any new entries.
"""

import asyncio
import json
import logging
import os
import re

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

EVENTS_URL = "https://en.onepiece-cardgame.com/events/"
BASE_URL = "https://en.onepiece-cardgame.com"
SEEN_FILE = "seen_events.json"

KEYWORDS = ["regional", "treasure cup"]

REQUEST_TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# ── Persistence ───────────────────────────────────────────────────────────────
def load_seen() -> set[str]:
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except Exception as exc:
        log.error("Could not read %s: %s", SEEN_FILE, exc)
        return set()


def save_seen(seen: set[str]) -> None:
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(sorted(seen), f, indent=2)
    except Exception as exc:
        log.error("Could not write %s: %s", SEEN_FILE, exc)


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def fetch(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        log.error("Fetch failed for %s: %s", url, exc)
        return None


def abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE_URL + href
    return BASE_URL + "/" + href


# ── Level 1 – Main page scraping ──────────────────────────────────────────────
def _is_target(name: str) -> bool:
    low = name.lower()
    return any(kw in low for kw in KEYWORDS)


def _extract_name(anchor: BeautifulSoup) -> str:
    """Pull the best candidate for the event name out of an anchor tag."""
    # 1. Any heading element inside the anchor
    heading = anchor.find(re.compile(r"^h[1-6]$"))
    if heading:
        return heading.get_text(" ", strip=True)

    # 2. Element whose class suggests it is a title
    title_el = anchor.find(class_=re.compile(r"title|name|heading|label", re.I))
    if title_el:
        return title_el.get_text(" ", strip=True)

    # 3. First non-empty line of all text in the anchor
    lines = [ln.strip() for ln in anchor.get_text("\n").splitlines() if ln.strip()]
    return lines[0] if lines else ""


def _extract_period(anchor: BeautifulSoup) -> str:
    """Find 'Event Period: …' or a date-shaped string inside the anchor."""
    for node in anchor.find_all(string=True):
        text = node.strip()
        if re.search(r"[Ee]vent\s+[Pp]eriod", text):
            return re.sub(r"^[Ee]vent\s+[Pp]eriod\s*[:\-]\s*", "", text).strip()

    # Fallback: any text containing a 4-digit year
    for node in anchor.find_all(string=True):
        text = node.strip()
        if re.search(r"\b20\d{2}\b", text) and len(text) < 80:
            return text

    return ""


def scrape_main_page() -> list[dict]:
    soup = fetch(EVENTS_URL)
    if not soup:
        return []

    results: list[dict] = []
    seen_hrefs: set[str] = set()

    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()

        # Must point deeper than /events/ itself
        if not re.search(r"/events/.+", href):
            continue
        if href in seen_hrefs:
            continue

        name = _extract_name(a)
        if not name or not _is_target(name):
            continue

        url = abs_url(href)
        period = _extract_period(a)

        results.append({"name": name, "period": period, "url": url})
        seen_hrefs.add(href)
        log.info("Found target event: %s", name)

    log.info("Scrape complete – %d target event(s) found", len(results))
    return results


# ── Level 2 – Event detail page scraping ─────────────────────────────────────
def scrape_detail(url: str) -> dict:
    soup = fetch(url)
    if not soup:
        return {}

    detail: dict = {}

    # Registration / application link
    reg = soup.find("a", href=re.compile(r"register|apply|application|signup|sign.up", re.I))
    if reg and reg.get("href"):
        detail["registration_link"] = abs_url(reg["href"])

    # Locations: harvest city/venue names from table cells or list items that
    # appear under recognisable regional headings.
    locations: list[str] = []
    region_pattern = re.compile(
        r"north\s+america|europe|oceania|latin\s+america|asia|japan", re.I
    )

    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        if not region_pattern.search(heading.get_text()):
            continue
        sibling = heading.find_next_sibling()
        while sibling and sibling.name not in ("h1", "h2", "h3", "h4"):
            # Grab table cells that look like a venue/city (short, capitalised)
            for td in sibling.find_all(["td", "li"]):
                text = td.get_text(strip=True)
                if 3 < len(text) < 60 and not re.match(r"^(regulation|standard|deck)", text, re.I):
                    locations.append(text)
            sibling = sibling.find_next_sibling()

    if locations:
        # Deduplicate while preserving order
        seen_loc: set[str] = set()
        unique: list[str] = []
        for loc in locations:
            if loc not in seen_loc:
                seen_loc.add(loc)
                unique.append(loc)
        detail["locations"] = unique[:6]

    return detail


# ── Telegram notification ─────────────────────────────────────────────────────
_MD2_SPECIAL = re.compile(r"([\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!\\])")


def _esc(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    return _MD2_SPECIAL.sub(r"\\\1", text)


def build_message(event: dict, detail: dict) -> str:
    name = event.get("name", "Unknown Event")
    period = event.get("period", "")
    url = event.get("url", "")

    locations = detail.get("locations", [])
    location_str = ", ".join(locations[:3]) if locations else "See link"

    reg = detail.get("registration_link", "")

    lines = [
        "🏴‍☠️ *NEW EVENT DETECTED\\!*",
        "",
        f"📌 *Name:* {_esc(name)}",
    ]
    if period:
        lines.append(f"📅 *Period:* {_esc(period)}")
    lines.append(f"📍 *Location:* {_esc(location_str)}")
    lines.append(f"🔗 *Link:* {_esc(url)}")
    if reg:
        lines.append(f"📝 *Register:* {_esc(reg)}")

    return "\n".join(lines)


async def notify(bot: Bot, event: dict, detail: dict) -> None:
    text = build_message(event, detail)
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=False,
        )
        log.info("Notification sent: %s", event["name"])
    except TelegramError as exc:
        log.error("Telegram send failed for '%s': %s", event["name"], exc)


# ── Scheduled job ─────────────────────────────────────────────────────────────
async def check_events(bot: Bot) -> None:
    log.info("--- Event check started ---")

    events = scrape_main_page()
    if not events:
        log.info("No target events returned from main page scrape")
        return

    seen = load_seen()
    new_events = [e for e in events if e["url"] not in seen]

    if not new_events:
        log.info("No new events (all %d already seen)", len(events))
        return

    log.info("%d new event(s) to process", len(new_events))

    for event in new_events:
        log.info("Fetching detail page: %s", event["url"])
        detail = scrape_detail(event["url"])
        await notify(bot, event, detail)
        seen.add(event["url"])

    save_seen(seen)
    log.info("--- Event check complete ---")


# ── Entry point ───────────────────────────────────────────────────────────────
async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")

    bot = Bot(token=BOT_TOKEN)
    log.info("One Piece Event Bot starting up")

    # Run immediately on startup so we don't miss anything on first deploy
    await check_events(bot)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(check_events, "interval", hours=1, args=[bot], id="event_check")
    scheduler.start()
    log.info("Scheduler running – next check in 1 hour")

    # Keep the process alive
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()
        log.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
