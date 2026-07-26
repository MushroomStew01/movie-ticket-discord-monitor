from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "targets.json"
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
LOGGER = logging.getLogger("movie-ticket-monitor")

TIME_PATTERN = re.compile(
    r"\b(?:1[0-2]|0?[1-9]):[0-5][0-9]\s?(?:a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
DATE_PATTERN = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\b|"
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b",
    re.IGNORECASE,
)
TICKET_TERMS = (
    "get tickets",
    "advance tickets available",
    "showtime",
    "showtimes",
    "imax",
    "70mm",
    "vip",
    "4dx",
    "ultraavx",
    "screenx",
    "d-box",
    "dbox",
)


@dataclass(frozen=True)
class Target:
    name: str
    type: str
    url: str
    watch_keywords: tuple[str, ...]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path.name}: {exc}") from exc


def save_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_title(value: str, href: str) -> str:
    title = normalize_space(value)
    if title and len(title) <= 180:
        return title

    slug = urlparse(href).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"[-_]+", " ", slug)
    return slug.title() or href


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_environment() -> tuple[str, str]:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    user_id = os.environ.get("DISCORD_USER_ID", "").strip()

    if not webhook:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL is missing. Add it as an environment variable or GitHub Actions secret."
        )

    if not webhook.startswith("https://discord.com/api/webhooks/"):
        LOGGER.warning("The webhook URL does not use the expected Discord webhook prefix.")

    if user_id and not user_id.isdigit():
        raise RuntimeError("DISCORD_USER_ID must contain digits only.")

    return webhook, user_id


def discord_post(
    webhook: str,
    *,
    title: str,
    description: str,
    url: str | None = None,
    user_id: str = "",
    color: int = 0x2ECC71,
) -> None:
    content = f"<@{user_id}>" if user_id else ""
    embed: dict[str, Any] = {
        "title": title[:256],
        "description": description[:4096],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Movie Ticket Monitor"},
    }
    if url:
        embed["url"] = url

    payload = {
        "username": "Movie Ticket Monitor",
        "content": content,
        "allowed_mentions": {"users": [user_id]} if user_id else {"parse": []},
        "embeds": [embed],
    }

    response = requests.post(webhook, json=payload, timeout=30)
    if response.status_code == 429:
        retry_after = response.json().get("retry_after", "unknown")
        raise RuntimeError(f"Discord rate limit reached; retry_after={retry_after}")
    response.raise_for_status()


def make_page(browser: Browser) -> Page:
    page = browser.new_page(
        viewport={"width": 1440, "height": 1800},
        locale="en-CA",
        timezone_id="America/Toronto",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
    )
    page.set_default_timeout(30_000)
    return page


def open_target(page: Page, url: str, wait_ms: int) -> None:
    LOGGER.info("Opening %s", url)
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(wait_ms)

    # Cookie overlays can hide content. These clicks are best-effort only.
    for label in ("Accept All", "Accept", "I Agree", "Continue"):
        try:
            button = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if button.count() > 0 and button.first.is_visible():
                button.first.click(timeout=1500)
                page.wait_for_timeout(500)
                break
        except Exception:
            pass


def visible_body_text(page: Page) -> str:
    try:
        return normalize_space(page.locator("body").inner_text(timeout=30_000))
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("The page body did not become readable.") from exc


def relevant_lines(body_text: str, keywords: tuple[str, ...]) -> list[str]:
    lines = []
    seen: set[str] = set()
    all_keywords = tuple(k.lower() for k in keywords) + TICKET_TERMS

    for raw_line in re.split(r"[\r\n]+", body_text):
        line = normalize_space(raw_line)
        if not line or len(line) > 500:
            continue
        lowered = line.lower()
        is_relevant = (
            any(keyword in lowered for keyword in all_keywords)
            or TIME_PATTERN.search(line) is not None
            or DATE_PATTERN.search(line) is not None
        )
        if is_relevant and lowered not in seen:
            seen.add(lowered)
            lines.append(line)

    return sorted(lines, key=str.lower)[:500]


def extract_movie_links(page: Page, base_url: str) -> dict[str, dict[str, str]]:
    script = """
    () => Array.from(document.querySelectorAll('a[href*="/movie/"]')).map(a => {
        const parent = a.closest('article, li, section, div');
        return {
            href: a.href || a.getAttribute('href') || '',
            text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '').trim(),
            context: parent ? (parent.innerText || '').trim() : ''
        };
    })
    """
    raw_items = page.evaluate(script)
    movies: dict[str, dict[str, str]] = {}

    for raw in raw_items:
        href = urljoin(base_url, raw.get("href", ""))
        if "/movie/" not in urlparse(href).path:
            continue

        context = normalize_space(raw.get("context", ""))[:1200]
        title = clean_title(raw.get("text", ""), href)
        advance = "advance tickets available" in context.lower()
        get_tickets = "get tickets" in context.lower()
        formats = sorted(
            term.upper() if term == "imax" else term
            for term in ("imax", "70mm", "vip", "4dx", "ultraavx", "screenx", "d-box")
            if term in context.lower()
        )

        existing = movies.get(href)
        candidate = {
            "title": title,
            "advance_tickets": str(advance),
            "get_tickets": str(get_tickets),
            "formats": ", ".join(formats),
            "context": context,
        }
        if existing is None or len(candidate["context"]) > len(existing["context"]):
            movies[href] = candidate

    return dict(sorted(movies.items()))


def collect_snapshot(page: Page, target: Target) -> dict[str, Any]:
    body = visible_body_text(page)
    ticket_available = (
        "get tickets" in body.lower() or "advance tickets available" in body.lower()
    )

    snapshot: dict[str, Any] = {
        "ticket_available": ticket_available,
        "relevant_lines": relevant_lines(body, target.watch_keywords),
    }

    if target.type.lower() == "theatre":
        snapshot["movies"] = extract_movie_links(page, target.url)

    return snapshot


def format_new_movies(new_movies: list[tuple[str, dict[str, str]]]) -> str:
    lines = []
    for href, movie in new_movies[:15]:
        status = []
        if movie.get("advance_tickets") == "True":
            status.append("advance tickets")
        if movie.get("formats"):
            status.append(movie["formats"])
        suffix = f" — {', '.join(status)}" if status else ""
        lines.append(f"• [{movie['title']}]({href}){suffix}")
    if len(new_movies) > 15:
        lines.append(f"• …and {len(new_movies) - 15} more")
    return "\n".join(lines)


def compare_snapshots(previous: dict[str, Any], current: dict[str, Any]) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []

    previous_movies = previous.get("movies", {})
    current_movies = current.get("movies", {})
    new_urls = sorted(set(current_movies) - set(previous_movies))
    if new_urls:
        events.append(
            (
                "New movie listing detected",
                format_new_movies([(url, current_movies[url]) for url in new_urls]),
            )
        )

    availability_changes = []
    for url in sorted(set(previous_movies) & set(current_movies)):
        old = previous_movies[url]
        new = current_movies[url]
        old_available = old.get("advance_tickets") == "True" or old.get("get_tickets") == "True"
        new_available = new.get("advance_tickets") == "True" or new.get("get_tickets") == "True"
        if new_available and not old_available:
            availability_changes.append((url, new))
    if availability_changes:
        events.append(
            (
                "Tickets may now be available",
                format_new_movies(availability_changes),
            )
        )

    if current.get("ticket_available") and not previous.get("ticket_available"):
        events.append(
            (
                "Ticket status changed",
                "The page now contains **Get Tickets** or **Advance tickets available**.",
            )
        )

    previous_lines = set(previous.get("relevant_lines", []))
    current_lines = set(current.get("relevant_lines", []))
    added_lines = sorted(current_lines - previous_lines, key=str.lower)
    meaningful_added = [
        line
        for line in added_lines
        if any(term in line.lower() for term in TICKET_TERMS)
        or TIME_PATTERN.search(line)
    ]
    if meaningful_added:
        excerpt = "\n".join(f"• {line[:300]}" for line in meaningful_added[:12])
        events.append(("New ticket or showtime text detected", excerpt))

    return events


def parse_targets(config: dict[str, Any]) -> tuple[dict[str, Any], list[Target]]:
    settings = config.get("settings", {})
    targets = []
    for item in config.get("targets", []):
        target = Target(
            name=normalize_space(item["name"]),
            type=normalize_space(item.get("type", "generic")).lower(),
            url=normalize_space(item["url"]),
            watch_keywords=tuple(normalize_space(k).lower() for k in item.get("watch_keywords", [])),
        )
        if target.type not in {"theatre", "movie", "generic"}:
            raise RuntimeError(f"Unsupported target type for {target.name}: {target.type}")
        targets.append(target)
    if not targets:
        raise RuntimeError("No targets are configured in targets.json.")
    return settings, targets


def run_monitor(*, test_alert: bool = False) -> int:
    webhook, user_id = get_environment()

    if test_alert:
        discord_post(
            webhook,
            title="✅ Movie ticket monitor connected",
            description=(
                "Your Discord webhook works. Future alerts will be posted here.\n\n"
                "The first normal monitor run creates a baseline and does not report every existing listing."
            ),
            user_id=user_id,
            color=0x3498DB,
        )
        LOGGER.info("Test alert sent successfully.")
        return 0

    config = load_json(CONFIG_PATH, {})
    settings, targets = parse_targets(config)
    state = load_json(STATE_PATH, {})
    wait_ms = int(settings.get("page_wait_ms", 8000))
    alert_on_first_run = bool(settings.get("alert_on_first_run", False))
    send_error_alerts = bool(settings.get("send_error_alerts", True))

    any_success = False
    any_failure = False

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for target in targets:
                page = make_page(browser)
                try:
                    open_target(page, target.url, wait_ms)
                    current_snapshot = collect_snapshot(page, target)
                    previous_entry = state.get(target.url)
                    current_hash = stable_hash(current_snapshot)

                    if previous_entry is None:
                        LOGGER.info("Baseline created: %s", target.name)
                        if alert_on_first_run:
                            discord_post(
                                webhook,
                                title=f"Baseline created: {target.name}",
                                description="This target is now being monitored.",
                                url=target.url,
                                user_id=user_id,
                                color=0x95A5A6,
                            )
                    else:
                        previous_snapshot = previous_entry.get("snapshot", {})
                        if current_hash != previous_entry.get("hash"):
                            events = compare_snapshots(previous_snapshot, current_snapshot)
                            if events:
                                for event_title, event_description in events:
                                    discord_post(
                                        webhook,
                                        title=f"🎟️ {event_title}: {target.name}",
                                        description=event_description,
                                        url=target.url,
                                        user_id=user_id,
                                        color=0xE67E22,
                                    )
                                LOGGER.info("Alerted on %s change(s): %s", len(events), target.name)
                            else:
                                LOGGER.info("Page changed, but no meaningful ticket change: %s", target.name)
                        else:
                            LOGGER.info("No change: %s", target.name)

                    state[target.url] = {
                        "name": target.name,
                        "type": target.type,
                        "hash": current_hash,
                        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                        "snapshot": current_snapshot,
                    }
                    any_success = True
                except Exception as exc:
                    any_failure = True
                    LOGGER.exception("Failed target %s", target.name)
                    state.setdefault(target.url, {})["last_error"] = str(exc)
                    state[target.url]["last_error_at_utc"] = datetime.now(timezone.utc).isoformat()
                    if send_error_alerts:
                        try:
                            discord_post(
                                webhook,
                                title=f"⚠️ Monitor error: {target.name}",
                                description=f"`{type(exc).__name__}: {str(exc)[:1500]}`",
                                url=target.url,
                                user_id=user_id,
                                color=0xE74C3C,
                            )
                        except Exception:
                            LOGGER.exception("Could not send the Discord error alert.")
                finally:
                    page.close()
        finally:
            browser.close()

    save_json(STATE_PATH, state)
    if not any_success:
        return 1
    return 2 if any_failure else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Cineplex pages and send Discord alerts.")
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Send a Discord test message without opening Cineplex.",
    )
    args = parser.parse_args()

    try:
        return run_monitor(test_alert=args.test_alert)
    except Exception as exc:
        LOGGER.exception("Fatal monitor error")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
