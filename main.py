"""
One Piece Card Game – Event Monitor Bot

Two-level architecture:

  Level 1 – main /events/ page
    Finds "period" pages linked from the events list
    (e.g. "Championship 26-27 Regionals Season 2", "Treasure Cup August 2026").
    Notifies when a NEW period page is discovered.

  Level 2 – each keyword period page (Regional / Treasure Cup)
    Parses individual events. The site uses three different layouts:
      A) <h5>Organizer</h5><dl><dd>Date: … Venue: … Link: <a/></dd></dl>
      B) <div><h5>Organizer</h5> Date: …<br> Venue: …<br> Link: <a/></div>
      C) <dl><dt><h5>Organizer</h5></dt><dd>Date: … Venue: …</dd><dd>Link: <a/></dd></dl>
    Only blocks containing "Date:" or "Venue:" are treated as events, which filters
    out info headings such as "Capacity", "Format", "Tickets", "Event Period".
    Sends two types of notification per event:
      • "EVENT ANNOUNCED" – when the event first appears (even if registration is TBA)
      • "REGISTRATION OPEN" – when a registration link appears for a previously TBA event

Railway notes:
  • Railway "Serverless" puts a service to sleep after 10 minutes without outbound
    traffic, and a worker is never woken up again. A lightweight keep-alive call to
    the Telegram API every few minutes keeps the service awake.
  • The container filesystem is wiped on every restart. Attach a Railway Volume:
    Railway exposes its mount path as RAILWAY_VOLUME_MOUNT_PATH and the seen-events
    file is stored there automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup, Tag
from telegram import Bot
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.request import HTTPXRequest

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

EVENTS_URL = "https://en.onepiece-cardgame.com/events/"
BASE_URL = "https://en.onepiece-cardgame.com"

# Persist on the Railway Volume when one is attached, otherwise next to main.py
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
SEEN_FILE = os.environ.get("SEEN_FILE", os.path.join(DATA_DIR, "seen_events.json"))

KEYWORDS = ["regional", "treasure cup"]

# Region header labels used inside period pages (h5 headings to skip)
REGION_LABELS = {
    "north america", "europe", "oceania", "latin america",
    "asia", "asia pacific", "middle east",
}

CHECK_INTERVAL_MINUTES = 60
KEEPALIVE_INTERVAL_MINUTES = 4   # must stay well below Railway's 10-minute sleep threshold
SEND_INTERVAL_SECONDS = 3.0      # Telegram allows ~20 messages/minute in a group
MAX_SEND_ATTEMPTS = 3

REQUEST_TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Log to stdout so Railway does not flag every line as an error
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
# httpx logs every request URL, which contains the bot token – keep it quiet
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Avoid a log line for every keep-alive run
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


# ── Persistence ───────────────────────────────────────────────────────────────
# seen_events.json structure:
# {
#   "periods":   [...],   # URLs of period pages we've notified about
#   "announced": [...],   # "period_url|region|organizer|date" – event announced
#   "reg_open":  [...],   # registration link URLs – registration-open notified
# }
def _empty_seen() -> dict:
    return {"periods": set(), "announced": set(), "reg_open": set()}


def load_seen() -> dict | None:
    """Load seen data. Returns None if the file does not exist yet (first run)."""
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.error("Could not read %s: %s", SEEN_FILE, exc)
        return _empty_seen()

    if isinstance(data, dict):
        return {
            "periods": set(data.get("periods", [])),
            # legacy "events" key is folded into announced
            "announced": set(data.get("announced", [])) | set(data.get("events", [])),
            "reg_open": set(data.get("reg_open", [])),
        }
    if isinstance(data, list):
        # very old format: flat list of event URLs
        return {"periods": set(), "announced": set(data), "reg_open": set()}
    return _empty_seen()


def save_seen(seen: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SEEN_FILE) or ".", exist_ok=True)
        tmp = SEEN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: sorted(v) for k, v in seen.items()}, f, indent=2)
        os.replace(tmp, SEEN_FILE)  # atomic: never leaves a half-written file
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
    Scrape the main /events/ page and return the linked sub-pages ("periods").
    Pages whose name or URL contains a keyword are flagged for deep scraping.
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

        heading = a.find(re.compile(r"^h[1-6]$"))
        text = a.get_text(" ", strip=True)
        name = heading.get_text(" ", strip=True) if heading else (text.splitlines()[0].strip() if text else "")

        is_keyword = _has_keyword(name) or _has_keyword(url)
        results.append({"name": name or url, "url": url, "keyword": is_keyword})

    log.info(
        "Found %d period page(s) on main page (%d keyword)",
        len(results), sum(p["keyword"] for p in results),
    )
    return results


# ── Level 2 – Period page: parse individual events ────────────────────────────
_BLOCK_STOP = re.compile(r"^(h[1-6]|dt)$")
_HEADING = re.compile(r"^h[1-6]$")


def _event_block(h5: Tag) -> tuple[str, str]:
    """
    Return (text, first_link) of the content that belongs to an <h5> heading:
    everything after it up to the next heading. Handles layouts A, B and C.
    """
    start = h5.parent if h5.parent is not None and h5.parent.name == "dt" else h5
    parts: list[str] = []
    link = ""
    for sib in start.next_siblings:
        if isinstance(sib, Tag):
            if _BLOCK_STOP.match(sib.name) or sib.find(_HEADING):
                break
            parts.append(sib.get_text(" ", strip=True))
            if not link:
                a = sib if sib.name == "a" and sib.get("href") else sib.find("a", href=True)
                if a:
                    link = abs_url(a["href"].strip())
        else:
            parts.append(str(sib).strip())
    text = re.sub(r"\s+", " ", " ".join(parts)).replace("​", "").strip()
    return text, link


def _field(text: str, name: str, stops: tuple[str, ...]) -> str:
    stop = "|".join(rf"\s*{s}:" for s in stops)
    m = re.search(rf"{name}:\s*(.+?)(?:{stop}|$)", text, re.S)
    return m.group(1).strip() if m else ""


def scrape_individual_events(period_url: str) -> list[dict]:
    soup = fetch(period_url)
    if not soup:
        return []

    results: list[dict] = []
    current_region = ""

    for h5 in soup.find_all("h5"):
        organizer = h5.get_text(" ", strip=True).replace("​", "")
        if not organizer:
            continue

        if organizer.lower() in REGION_LABELS:
            current_region = organizer
            continue

        text, reg_link = _event_block(h5)
        # Real events always carry a date and/or venue; info headings don't
        if not re.search(r"\b(Date|Venue):", text):
            continue

        date = _field(text, "Date", ("Venue", "Link"))
        venue = _field(text, "Venue", ("Link",))

        results.append({
            "organizer": organizer,
            "region": current_region,
            "date": date,
            "venue": venue,
            "registration_link": reg_link,
            # Stable key: does not change when the registration link is added later
            "announce_key": f"{period_url}|{current_region}|{organizer}|{date}",
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


def _event_lines(ev: dict) -> list[str]:
    lines = [f"📌 *Organizer:* {_esc(ev['organizer'])}"]
    if ev["region"]:
        lines.append(f"🌍 *Region:* {_esc(ev['region'])}")
    if ev["date"]:
        lines.append(f"📅 *Date:* {_esc(ev['date'])}")
    if ev["venue"]:
        lines.append(f"📍 *Venue:* {_esc(ev['venue'])}")
    return lines


def build_announced_message(ev: dict) -> str:
    reg = ev["registration_link"]
    reg_line = (
        f"📝 *Register:* {_esc(reg)}" if reg
        else f"📝 *Register:* TBA — check {_esc(ev['period_url'])}"
    )
    return "\n".join(["📋 *NEW EVENT ANNOUNCED\\!*", "", *_event_lines(ev), reg_line])


def build_reg_open_message(ev: dict) -> str:
    return "\n".join([
        "🏴‍☠️ *REGISTRATION NOW OPEN\\!*", "", *_event_lines(ev),
        f"📝 *Register:* {_esc(ev['registration_link'])}",
    ])


async def _send(bot: Bot, text: str, label: str) -> bool:
    """Send a message, honouring Telegram flood control. Returns True on success."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode="MarkdownV2",
                disable_web_page_preview=False,
            )
            log.info("Sent: %s", label)
            await asyncio.sleep(SEND_INTERVAL_SECONDS)
            return True
        except RetryAfter as exc:
            wait = float(getattr(exc.retry_after, "total_seconds", lambda: exc.retry_after)()) + 1
            log.warning("Flood control for '%s': waiting %.0fs (attempt %d)", label, wait, attempt)
            await asyncio.sleep(wait)
        except TimedOut:
            # The message may or may not have been delivered: don't retry now to avoid
            # duplicates; it stays unseen and will be retried at the next check.
            log.warning("Telegram timed out for '%s', will retry next check", label)
            return False
        except TelegramError as exc:
            log.error("Telegram error for '%s': %s", label, exc)
            return False
    log.error("Giving up on '%s' after %d attempts", label, MAX_SEND_ATTEMPTS)
    return False


# ── Scheduled jobs ────────────────────────────────────────────────────────────
async def check_events(bot: Bot) -> None:
    log.info("--- Event check started ---")
    seen = load_seen()
    first_run = seen is None
    if first_run:
        # No state yet (fresh deploy / new volume): record everything silently instead
        # of flooding the group with every event already on the site.
        log.warning("No %s found: first run, recording current events without notifying", SEEN_FILE)
        seen = _empty_seen()

    period_pages = await asyncio.to_thread(scrape_period_pages)
    if not period_pages:
        log.info("No period pages found on main page")
        return

    try:
        for period in period_pages:
            period_url = period["url"]

            if period_url not in seen["periods"]:
                if first_run or await _send(bot, build_period_message(period), f"period: {period['name']}"):
                    seen["periods"].add(period_url)

            # Deep-scrape individual events only for keyword pages (Regional / Treasure Cup)
            if not period["keyword"]:
                continue

            events = await asyncio.to_thread(scrape_individual_events, period_url)
            for ev in events:
                reg = ev["registration_link"]

                # 1. New event announced (first time we see it, even if TBA)
                if ev["announce_key"] not in seen["announced"]:
                    label = f"announced: {ev['organizer']} ({ev['region']})"
                    if first_run or await _send(bot, build_announced_message(ev), label):
                        seen["announced"].add(ev["announce_key"])
                        if reg:
                            seen["reg_open"].add(reg)

                # 2. Registration just opened for a previously TBA event
                elif reg and reg not in seen["reg_open"]:
                    label = f"reg open: {ev['organizer']} ({ev['region']})"
                    if await _send(bot, build_reg_open_message(ev), label):
                        seen["reg_open"].add(reg)
    finally:
        save_seen(seen)

    if first_run:
        n_ev = len(seen["announced"])
        await _send(
            bot,
            "🤖 *Bot avviato*\n\n"
            f"Registrati {len(seen['periods'])} pagine e {n_ev} eventi già presenti sul sito\\. "
            "Da ora ti avviso solo delle novità\\.",
            "first-run summary",
        )
    log.info("--- Event check complete ---")


async def keepalive(bot: Bot) -> None:
    """Small outbound request so Railway Serverless never considers the service idle."""
    try:
        await bot.get_me()
    except Exception as exc:  # network hiccups must never kill the scheduler
        log.warning("Keep-alive failed: %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────
async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")

    bot = Bot(
        token=BOT_TOKEN,
        request=HTTPXRequest(connect_timeout=10, read_timeout=20, write_timeout=20),
    )
    # Right after a container starts the network may not be ready yet: retry instead
    # of crashing on the very first request.
    for attempt in range(1, 6):
        try:
            await bot.initialize()
            break
        except NetworkError as exc:
            if attempt == 5:
                raise
            log.warning("Telegram not reachable yet (%s), retrying in %ds", exc, attempt * 5)
            await asyncio.sleep(attempt * 5)
    log.info("One Piece Event Bot starting up (state file: %s)", SEEN_FILE)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        keepalive, "interval", minutes=KEEPALIVE_INTERVAL_MINUTES, args=[bot],
        id="keepalive", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        check_events, "interval", minutes=CHECK_INTERVAL_MINUTES, args=[bot],
        id="event_check", max_instances=1, coalesce=True,
    )
    # Start the scheduler first so keep-alive runs even during a long initial check
    scheduler.start()

    try:
        await check_events(bot)
    except Exception:
        # An error in the first check must not crash the process
        log.exception("Initial event check failed")

    log.info("Scheduler running – next check in %d minutes", CHECK_INTERVAL_MINUTES)

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()
        await bot.shutdown()
        log.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
