"""
One Piece Card Game – Event Monitor Bot
Two-level scraping:
  Level 1 – main /events/ page → period sub-pages
  Level 2 – each period page → individual keyword-matching events
Sends Telegram notifications for new periods and new events.
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
BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

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
def load_seen() -> dict:
    """Returns {"periods": set, "events": set}. Migrates old list format."""
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {
                "periods": set(data.get("periods", [])),
                "events": set(data.get("events", [])),
            }
        if isinstance(data, list):
            # Migrate from old format (flat list of event URLs)
            return {"periods": set(), "events": set(data)}
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.error("Could not read %s: %s", SEEN_FILE, exc)
    return {"periods": set(), "events": set()}


def save_seen(seen: dict) -> None:
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(
                {
                    "periods": sorted(seen["periods"]),
                    "events": sorted(seen["events"]),
                },
                f,
                indent=2,
            )
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


def _normalize_url(url: str) -> str:
    """Strip trailing slash for consistent comparison."""
    return url.rstrip("/")


# ── Text extraction helpers ───────────────────────────────────────────────────
def _extract_name(anchor: BeautifulSoup) -> str:
    heading = anchor.find(re.compile(r"^h[1-6]$"))
    if heading:
        return heading.get_text(" ", strip=True)
    title_el = anchor.find(class_=re.compile(r"title|name|heading|label", re.I))
    if title_el:
        return title_el.get_text(" ", strip=True)
    lines = [ln.strip() for ln in anchor.get_text("\n").splitlines() if ln.strip()]
    return lines[0] if lines else ""


def _extract_period(anchor: BeautifulSoup) -> str:
    for node in anchor.find_all(string=True):
        text = node.strip()
        if re.search(r"[Ee]vent\s+[Pp]eriod", text):
            return re.sub(r"^[Ee]vent\s+[Pp]eriod\s*[:\-]\s*", "", text).strip()
    for node in anchor.find_all(string=True):
        text = node.strip()
        if re.search(r"\b20\d{2}\b", text) and len(text) < 80:
            return text
    return ""


def _is_target(name: str) -> bool:
    low = name.lower()
    return any(kw in low for kw in KEYWORDS)


def _is_internal_events_link(href: str) -> bool:
    """True if the href points to a sub-page under /events/."""
    return bool(re.search(r"/events/[^?#]+", href))


# ── Level 1 – Discover period pages from the main events page ─────────────────
def scrape_period_links() -> list[dict]:
    """
    Scrapes the main /events/ page and returns all sub-page links as period
    candidates. No keyword filter – we want every period, not just matching ones.
    """
    soup = fetch(EVENTS_URL)
    if not soup:
        return []

    results: list[dict] = []
    seen_hrefs: set[str] = set()

    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()
        if not _is_internal_events_link(href):
            continue

        url = _normalize_url(abs_url(href))
        norm_events = _normalize_url(EVENTS_URL)
        if url == norm_events or url in seen_hrefs:
            continue

        name = _extract_name(a)
        results.append({"name": name or url, "url": url})
        seen_hrefs.add(url)
        log.info("Period candidate: %s → %s", name or "(no name)", url)

    log.info("Found %d period link(s) on main page", len(results))
    return results


# ── Level 2 – Scrape a period page for individual events ─────────────────────
def scrape_period_for_events(period_url: str) -> list[dict]:
    """
    Fetches a period page and returns keyword-matching event entries.
    Looks for any anchor whose visible text matches KEYWORDS.
    """
    soup = fetch(period_url)
    if not soup:
        return []

    results: list[dict] = []
    seen_hrefs: set[str] = set()

    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()
        if not href or href.startswith("#"):
            continue

        url = _normalize_url(abs_url(href))

        # Skip links that go outside the site or back to the period page itself
        if BASE_URL not in url or url == _normalize_url(period_url):
            continue

        if url in seen_hrefs:
            continue

        name = _extract_name(a)
        if not name or not _is_target(name):
            continue

        period_text = _extract_period(a)
        results.append({"name": name, "url": url, "period": period_text})
        seen_hrefs.add(url)
        log.info("Event found in period page: %s", name)

    log.info("Period %s → %d keyword event(s)", period_url, len(results))
    return results


# ── Level 3 – Event detail page scraping ─────────────────────────────────────
def scrape_detail(url: str) -> dict:
    soup = fetch(url)
    if not soup:
        return {}

    detail: dict = {}

    # Search by href pattern
    reg = soup.find("a", href=re.compile(r"register|apply|application|signup|sign.up|entry|entries", re.I))
    # Fallback: search by visible link text
    if not reg:
        reg_text_pattern = re.compile(r"register|apply|sign.up|entry|entries|how to enter", re.I)
        reg = next(
            (a for a in soup.find_all("a", href=True) if reg_text_pattern.search(a.get_text())),
            None,
        )
    if reg and reg.get("href"):
        detail["registration_link"] = abs_url(reg["href"])

    locations: list[str] = []
    region_pattern = re.compile(
        r"north\s+america|europe|oceania|latin\s+america|asia|japan", re.I
    )
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        if not region_pattern.search(heading.get_text()):
            continue
        sibling = heading.find_next_sibling()
        while sibling and sibling.name not in ("h1", "h2", "h3", "h4"):
            for td in sibling.find_all(["td", "li"]):
                text = td.get_text(strip=True)
                if 3 < len(text) < 60 and not re.match(r"^(regulation|standard|deck)", text, re.I):
                    locations.append(text)
            sibling = sibling.find_next_sibling()

    if locations:
        seen_loc: set[str] = set()
        unique: list[str] = []
        for loc in locations:
            if loc not in seen_loc:
                seen_loc.add(loc)
                unique.append(loc)
        detail["locations"] = unique[:6]

    return detail


# ── Telegram notifications ────────────────────────────────────────────────────
_MD2_SPECIAL = re.compile(r"([\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!\\])")


def _esc(text: str) -> str:
    return _MD2_SPECIAL.sub(r"\\\1", text)


def build_event_message(event: dict, detail: dict) -> str:
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
    lines.append(f"🔗 *Info:* {_esc(url)}")
    lines.append(f"📝 *Register:* {_esc(reg if reg else url)}")

    return "\n".join(lines)


def build_period_message(period: dict) -> str:
    name = period.get("name", "New Event Period")
    url = period.get("url", "")
    lines = [
        "📅 *NEW EVENT PERIOD PUBLISHED\\!*",
        "",
        f"📌 *Period:* {_esc(name)}",
        f"🔗 *Link:* {_esc(url)}",
    ]
    return "\n".join(lines)


async def _send(bot: Bot, text: str, label: str) -> None:
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=False,
        )
        log.info("Notification sent: %s", label)
    except TelegramError as exc:
        log.error("Telegram send failed for '%s': %s", label, exc)


# ── Scheduled job ─────────────────────────────────────────────────────────────
async def check_events(bot: Bot) -> None:
    log.info("--- Event check started ---")
    seen = load_seen()

    # Level 1: find period pages on the main events page
    period_candidates = scrape_period_links()
    if not period_candidates:
        log.info("No period links found on main page")
        return

    for period in period_candidates:
        period_url = period["url"]
        is_new_period = period_url not in seen["periods"]

        if is_new_period:
            log.info("New period discovered: %s", period_url)
            await _send(bot, build_period_message(period), f"period: {period['name']}")
            seen["periods"].add(period_url)

        # Level 2: scrape this period page for keyword-matching events
        events = scrape_period_for_events(period_url)
        new_events = [e for e in events if e["url"] not in seen["events"]]

        if not new_events:
            log.info("No new events in period: %s", period_url)
            continue

        log.info("%d new event(s) in period %s", len(new_events), period_url)
        for event in new_events:
            log.info("Fetching detail page: %s", event["url"])
            detail = scrape_detail(event["url"])
            await _send(bot, build_event_message(event, detail), event["name"])
            seen["events"].add(event["url"])

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

    await check_events(bot)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(check_events, "interval", hours=1, args=[bot], id="event_check")
    scheduler.start()
    log.info("Scheduler running – next check in 1 hour")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()
        log.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
