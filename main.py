"""
One Piece Card Game – Event Monitor Bot

Two-level architecture:

  Level 1 – main /events/ page
    Finds "period" pages whose name or URL contains a keyword
    (e.g. "Championship 26-27 Regionals Season 2", "Treasure Cup August 2026").
    Notifies when a NEW period page is discovered.

  Level 2 – each period page (e.g. regional-season2-26-27.html)
    Parses individual events structured as:
      <h5>Organizer Name</h5>
      <dl><dd>Date: …  Venue: …  Link: <a href="…">Registration</a></dd></dl>
    Sends two types of notification per event:
      • "EVENT ANNOUNCED" – when the event first appears (even if registration is TBA)
      • "REGISTRATION OPEN" – when a registration link becomes available for a
        previously TBA event (second notification for the same event)
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

# Region header labels used inside period pages (h5 headings to skip)
REGION_LABELS = {
    "north america", "europe", "oceania", "latin america",
    "asia", "asia pacific", "middle east",
}

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
# seen_events.json structure:
# {
#   "periods":   [...],   # URLs of period pages we've notified about
#   "announced": [...],   # "period_url|region|organizer|date" – event announced
#   "reg_open":  [...],   # registration link URLs – registration-open notified
# }
def load_seen() -> dict:
    """Load seen data; migrate from old flat-list format if necessary."""
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {
                "periods": set(data.get("periods", [])),
                "announced": set(data.get("announced", [])),
                "reg_open": set(data.get("reg_open", [])),
                # legacy "events" key → fold into announced
                **({} if "events" not in data else {"announced": set(data["events"])}),
            }
        if isinstance(data, list):
            # very old format: flat list of event URLs
            return {"periods": set(), "announced": set(data), "reg_open": set()}
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.error("Could not read %s: %s", SEEN_FILE, exc)
    return {"periods": set(), "announced": set(), "reg_open": set()}


def save_seen(seen: dict) -> None:
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(
                {
                    "periods": sorted(seen["periods"]),
                    "announced": sorted(seen["announced"]),
                    "reg_open": sorted(seen["reg_open"]),
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
    return BASE_URL + (href if href.startswith("/") else "/" + href)


def _normalize(url: str) -> str:
    return url.rstrip("/")


def _has_keyword(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in KEYWORDS)


# ── Level 1 – Main page: discover period pages ────────────────────────────────
def scrape_period_pages() -> list[dict]:
    """
    Scrape the main /events/ page and return sub-pages that match a keyword
    (in their anchor name or URL). These are the "period" pages to monitor.
    """
    soup = fetch(EVENTS_URL)
    if not soup:
        return []

    results: list[dict] = []
    seen_urls: set[str] = set()
    norm_events = _normalize(EVENTS_URL)

    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()
        if not re.search(r"/events/[^?#]+", href):
            continue

        url = _normalize(abs_url(href))
        if url == norm_events or url in seen_urls:
            continue
        seen_urls.add(url)

        # Extract the best name from the anchor card
        heading = a.find(re.compile(r"^h[1-6]$"))
        name = (
            heading.get_text(" ", strip=True)
            if heading
            else (a.get_text(" ", strip=True).splitlines()[0].strip() if a.get_text(strip=True) else "")
        )

        is_keyword = _has_keyword(name) or _has_keyword(url)
        results.append({"name": name or url, "url": url, "keyword": is_keyword})
        log.info("Period page found%s: %s", " [keyword]" if is_keyword else "", name or url)

    log.info("Found %d period page(s) on main page", len(results))
    return results


# ── Level 2 – Period page: parse individual events ────────────────────────────
def _parse_dd(dd_text: str, dl: BeautifulSoup) -> tuple[str, str, str]:
    """
    Extract (date, venue, registration_link) from a <dl><dd> block.
    dd_text is the plain text; dl is the BeautifulSoup element for link extraction.
    """
    date_m = re.search(r"Date:\s*(.+?)(?:\s*Venue:|\s*Link:|$)", dd_text, re.S)
    date = date_m.group(1).strip() if date_m else ""

    venue_m = re.search(r"Venue:\s*(.+?)(?:\s*Link:|$)", dd_text, re.S)
    venue = venue_m.group(1).strip() if venue_m else ""

    reg_a = dl.find("a", href=True)
    reg_link = abs_url(reg_a["href"]) if reg_a else ""

    return date, venue, reg_link


def scrape_individual_events(period_url: str) -> list[dict]:
    """
    Parse individual events from a period page.

    Structure inside the page:
      <h5>Region Label</h5>          ← skip these
      <h5>Organizer Name</h5>
      <dl><dd>Date: …  Venue: …  Link: <a href="…"/></dd></dl>
    """
    soup = fetch(period_url)
    if not soup:
        return []

    results: list[dict] = []
    current_region = ""

    for h5 in soup.find_all("h5"):
        organizer = h5.get_text(strip=True)
        if not organizer:
            continue

        # Region header → just update current region
        if organizer.lower() in REGION_LABELS:
            current_region = organizer
            continue

        # Must be followed by a <dl> to be an event entry
        dl = h5.find_next_sibling("dl")
        if not dl:
            continue

        dd_text = dl.get_text(" ", strip=True)
        date, venue, reg_link = _parse_dd(dd_text, dl)

        # Stable announcement key (does not change when reg link is added later)
        announce_key = f"{period_url}|{current_region}|{organizer}|{date}"

        results.append({
            "organizer": organizer,
            "region": current_region,
            "date": date,
            "venue": venue,
            "registration_link": reg_link,
            "announce_key": announce_key,
            "period_url": period_url,
        })

    log.info("Period %s → %d individual event(s) parsed", period_url, len(results))
    return results


# ── Telegram notifications ────────────────────────────────────────────────────
_MD2_SPECIAL = re.compile(r"([\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!\\])")


def _esc(text: str) -> str:
    return _MD2_SPECIAL.sub(r"\\\1", text)


def build_period_message(period: dict) -> str:
    return "\n".join([
        "📅 *NEW EVENT PAGE PUBLISHED\\!*",
        "",
        f"📌 *Event:* {_esc(period['name'])}",
        f"🔗 *Link:* {_esc(period['url'])}",
    ])


def build_announced_message(ev: dict) -> str:
    reg = ev["registration_link"]
    reg_line = f"📝 *Register:* {_esc(reg)}" if reg else f"📝 *Register:* TBA — check {_esc(ev['period_url'])}"
    lines = [
        "📋 *NEW EVENT ANNOUNCED\\!*",
        "",
        f"📌 *Organizer:* {_esc(ev['organizer'])}",
    ]
    if ev["region"]:
        lines.append(f"🌍 *Region:* {_esc(ev['region'])}")
    if ev["date"]:
        lines.append(f"📅 *Date:* {_esc(ev['date'])}")
    if ev["venue"]:
        lines.append(f"📍 *Venue:* {_esc(ev['venue'])}")
    lines.append(reg_line)
    return "\n".join(lines)


def build_reg_open_message(ev: dict) -> str:
    lines = [
        "🏴‍☠️ *REGISTRATION NOW OPEN\\!*",
        "",
        f"📌 *Organizer:* {_esc(ev['organizer'])}",
    ]
    if ev["region"]:
        lines.append(f"🌍 *Region:* {_esc(ev['region'])}")
    if ev["date"]:
        lines.append(f"📅 *Date:* {_esc(ev['date'])}")
    if ev["venue"]:
        lines.append(f"📍 *Venue:* {_esc(ev['venue'])}")
    lines.append(f"📝 *Register:* {_esc(ev['registration_link'])}")
    return "\n".join(lines)


async def _send(bot: Bot, text: str, label: str) -> None:
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=False,
        )
        log.info("Sent: %s", label)
    except TelegramError as exc:
        log.error("Telegram error for '%s': %s", label, exc)


# ── Scheduled job ─────────────────────────────────────────────────────────────
async def check_events(bot: Bot) -> None:
    log.info("--- Event check started ---")
    seen = load_seen()

    period_pages = scrape_period_pages()
    if not period_pages:
        log.info("No period pages found on main page")
        return

    for period in period_pages:
        period_url = period["url"]

        # Notify if this is a newly discovered period page
        if period_url not in seen["periods"]:
            log.info("New period page: %s", period_url)
            await _send(bot, build_period_message(period), f"period: {period['name']}")
            seen["periods"].add(period_url)

        # Deep-scrape individual events only for keyword pages (Regional / Treasure Cup)
        if not period.get("keyword"):
            continue

        events = scrape_individual_events(period_url)
        for ev in events:
            # 1. New event announced (first time we see it, even if TBA)
            if ev["announce_key"] not in seen["announced"]:
                label = f"announced: {ev['organizer']} ({ev['region']})"
                await _send(bot, build_announced_message(ev), label)
                seen["announced"].add(ev["announce_key"])

            # 2. Registration just opened for a previously TBA event
            elif ev["registration_link"] and ev["registration_link"] not in seen["reg_open"]:
                label = f"reg open: {ev['organizer']} ({ev['region']})"
                await _send(bot, build_reg_open_message(ev), label)
                seen["reg_open"].add(ev["registration_link"])

            # Mark registration as seen even on first announcement
            if ev["registration_link"]:
                seen["reg_open"].add(ev["registration_link"])

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
