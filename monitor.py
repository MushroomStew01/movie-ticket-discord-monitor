from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "targets.json"
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "monitor.log"
SNAPSHOT_VERSION = 2

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
TICKET_PATTERN = re.compile(
    r"\b(?:get\s+(?:advance\s+)?tickets?|advance\s+tickets?|buy\s+tickets?|"
    r"select\s+showtimes?|book\s+now)\b",
    re.IGNORECASE,
)
FORMAT_TERMS = ("imax", "70mm", "vip", "4dx", "ultraavx", "screenx", "d-box", "dbox")
TICKET_TERMS = (
    "get tickets",
    "get advance tickets",
    "advance tickets",
    "buy tickets",
    "showtime",
    "showtimes",
    *FORMAT_TERMS,
)
PAGE_FAILURE_MARKERS = (
    "access denied",
    "verify you are human",
    "unusual traffic",
    "service unavailable",
    "page not found",
    "temporarily unavailable",
)
MOVIE_SECTION_STOPS = (
    "menu offers",
    "concessions & bites",
    "concessions and bites",
    "book your event",
    "additional information",
    "more at cineplex",
    "corporate information",
    "theatre information",
)


@dataclass(frozen=True)
class Target:
    name: str
    type: str
    url: str
    watch_keywords: tuple[str, ...]
    min_movies: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
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


def normalized_lines(value: str) -> list[str]:
    # Preserve line boundaries. The previous version flattened the entire page
    # before trying to detect ticket/showtime lines, which made the detector empty.
    return [
        line
        for raw in re.split(r"[\r\n]+", value or "")
        if (line := normalize_space(raw))
    ]


def normalize_title(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return normalize_space(value)


def title_matches(expected: str, actual: str) -> bool:
    expected_normalized = normalize_title(expected)
    actual_normalized = normalize_title(actual)
    if not expected_normalized or not actual_normalized:
        return False
    if expected_normalized in actual_normalized or actual_normalized in expected_normalized:
        return True
    expected_tokens = set(expected_normalized.split()) - {"the", "a", "an", "and"}
    actual_tokens = set(actual_normalized.split()) - {"the", "a", "an", "and"}
    if not expected_tokens:
        return False
    return len(expected_tokens & actual_tokens) / len(expected_tokens) >= 0.60


def canonicalize_url(value: str, base_url: str = "") -> str:
    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    path = re.sub(r"^/(?:en|fr)(?=/|$)", "", parsed.path, flags=re.IGNORECASE) or "/"
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=path.rstrip("/") or "/",
        query="",
        fragment="",
    ).geturl()


def clean_card_title(value: str, href: str = "") -> str:
    title = normalize_space(value)
    title = re.sub(r"\bwatch trailer\b", "", title, flags=re.IGNORECASE).strip()
    title = re.sub(
        r"^(?:advance\s+tickets?(?:\s+available)?|coming\s+soon)\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = normalize_space(title)
    if title and len(title) <= 180:
        return title
    if href:
        slug = urlparse(href).path.rstrip("/").split("/")[-1]
        return re.sub(r"[-_]+", " ", slug).title() or href
    return title


def priority_match(title: str, priority_titles: tuple[str, ...]) -> str | None:
    normalized = normalize_title(title)
    if not normalized:
        return None
    for priority_title in priority_titles:
        candidate = normalize_title(priority_title)
        if candidate and (candidate == normalized or candidate in normalized or normalized in candidate):
            return priority_title
    return None


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_environment() -> tuple[str, str]:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    user_id = os.environ.get("DISCORD_USER_ID", "").strip()
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL is missing.")
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
    max_attempts: int = 4,
) -> None:
    content = f"<@{user_id}>" if user_id else ""
    embed: dict[str, Any] = {
        "title": title[:256],
        "description": description[:4096],
        "color": color,
        "timestamp": utc_now(),
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

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(webhook, json=payload, timeout=30)
            if response.status_code == 429:
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except (TypeError, ValueError, requests.JSONDecodeError):
                    retry_after = 1.0
                if retry_after > 1000:
                    retry_after /= 1000
                if attempt < max_attempts:
                    time.sleep(max(0.5, min(retry_after, 30.0)))
                    continue
            if 500 <= response.status_code < 600 and attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            response.raise_for_status()
            return
        except requests.RequestException as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            raise RuntimeError(f"Discord delivery failed after {max_attempts} attempts: {exc}") from exc
    raise RuntimeError(f"Discord delivery failed: {last_error}")


def make_context(browser: Any, timeout_seconds: int) -> BrowserContext:
    context = browser.new_context(
        viewport={"width": 1440, "height": 1800},
        locale="en-CA",
        timezone_id="America/Toronto",
    )
    context.set_default_timeout(timeout_seconds * 1000)

    def route_handler(route: Any) -> None:
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
        else:
            route.continue_()

    context.route("**/*", route_handler)
    return context


def accept_cookie_prompt(page: Page) -> None:
    for label in ("Accept All", "Accept", "I Agree", "Continue"):
        try:
            button = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if button.count() and button.first.is_visible():
                button.first.click(timeout=1500)
                page.wait_for_timeout(300)
                return
        except Exception:
            continue


def expand_dynamic_content(page: Page, max_passes: int) -> None:
    stable_passes = 0
    previous_measure = -1
    for _ in range(max_passes):
        try:
            show_more = page.get_by_role("button", name=re.compile(r"^show more$", re.I))
            if show_more.count() and show_more.first.is_visible():
                show_more.first.click(timeout=2000)
                page.wait_for_timeout(700)
        except Exception:
            pass

        measure = page.evaluate(
            """() => {
                const main = document.querySelector('main') || document.body;
                window.scrollTo(0, document.body.scrollHeight);
                return (main.innerText || '').length +
                    document.querySelectorAll('a[href*="/movie/"]').length * 10000;
            }"""
        )
        page.wait_for_timeout(500)
        if measure == previous_measure:
            stable_passes += 1
            if stable_passes >= 2:
                break
        else:
            stable_passes = 0
            previous_measure = measure


def open_target(
    page: Page,
    target: Target,
    *,
    wait_ms: int,
    timeout_seconds: int,
    scroll_passes: int,
) -> None:
    LOGGER.info("Opening %s", target.url)
    response = page.goto(
        target.url,
        wait_until="domcontentloaded",
        timeout=timeout_seconds * 1000,
    )
    if response is None:
        raise RuntimeError("Navigation did not return an HTTP response.")
    if response.status >= 400:
        raise RuntimeError(f"Cineplex returned HTTP {response.status}.")
    page.locator("main").wait_for(state="attached", timeout=timeout_seconds * 1000)
    page.wait_for_timeout(wait_ms)
    accept_cookie_prompt(page)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass
    if target.type in {"theatre", "listing"}:
        expand_dynamic_content(page, scroll_passes)


def main_text(page: Page) -> str:
    try:
        # Do not normalize the complete text here; line boundaries are evidence.
        return page.locator("main").inner_text(timeout=30_000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("The main page content did not become readable.") from exc


def relevant_lines(body_text: str, keywords: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    all_keywords = tuple(k.casefold() for k in keywords) + TICKET_TERMS
    for line in normalized_lines(body_text):
        if len(line) > 500:
            continue
        lowered = line.casefold()
        is_relevant = (
            any(keyword in lowered for keyword in all_keywords)
            or TIME_PATTERN.search(line) is not None
            or DATE_PATTERN.search(line) is not None
        )
        if is_relevant and lowered not in seen:
            seen.add(lowered)
            result.append(line)
    return result[:500]


def extract_formats(text: str) -> list[str]:
    lowered = text.casefold()
    names = {
        "imax": "IMAX",
        "70mm": "70MM",
        "vip": "VIP",
        "4dx": "4DX",
        "ultraavx": "UltraAVX",
        "screenx": "ScreenX",
        "d-box": "D-BOX",
        "dbox": "D-BOX",
    }
    return sorted({label for term, label in names.items() if term in lowered})


def movie_available(movie: dict[str, Any]) -> bool:
    return bool(movie.get("ticket_available")) or movie.get("advance_tickets") == "True" or movie.get("get_tickets") == "True"


def movie_candidate(title: str, href: str, context: str) -> dict[str, Any]:
    return {
        "title": title,
        "ticket_available": TICKET_PATTERN.search(context) is not None,
        "formats": extract_formats(context),
        "context": normalize_space(context)[:1200],
        "url": href or None,
    }


def extract_link_movies(page: Page, base_url: str) -> dict[str, dict[str, Any]]:
    raw_items = page.evaluate(
        """() => Array.from(document.querySelectorAll('main a[href*="/movie/"]')).map(a => {
            const parent = a.closest('article, li, section, [data-testid*="movie"], div');
            return {
                href: a.href || a.getAttribute('href') || '',
                text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '').trim(),
                context: parent ? (parent.innerText || '').trim() : ''
            };
        })"""
    )
    movies: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        href = canonicalize_url(str(raw.get("href", "")), base_url)
        if "/movie/" not in urlparse(href).path.casefold():
            continue
        context = normalize_space(str(raw.get("context", "")))
        title = clean_card_title(str(raw.get("text", "")), href)
        candidate = movie_candidate(title, href, context)
        existing = movies.get(href)
        if existing is None or len(candidate["context"]) > len(existing.get("context", "")):
            movies[href] = candidate
    return movies


def extract_text_movies(body_text: str, existing: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    movies = copy.deepcopy(existing)
    title_index = {
        normalize_title(movie.get("title", "")): key
        for key, movie in movies.items()
        if normalize_title(movie.get("title", ""))
    }
    lines = normalized_lines(body_text)
    start = next((i for i, line in enumerate(lines) if normalize_title(line) == "movies"), None)
    if start is None:
        return movies

    last_key: str | None = None
    for line in lines[start + 1 :]:
        lowered = line.casefold()
        if any(lowered.startswith(stop) for stop in MOVIE_SECTION_STOPS):
            break
        if lowered in {"show more", "watch trailer", "movies"}:
            continue
        if TICKET_PATTERN.search(line):
            if last_key is not None:
                movies[last_key]["ticket_available"] = True
                movies[last_key]["context"] = normalize_space(
                    f"{movies[last_key].get('context', '')} {line}"
                )[:1200]
            continue
        formats = extract_formats(line)
        if last_key is not None and formats and len(line) < 100:
            movies[last_key]["formats"] = sorted(set(movies[last_key].get("formats", [])) | set(formats))
            continue
        if last_key is not None and (TIME_PATTERN.search(line) or DATE_PATTERN.fullmatch(line)):
            movies[last_key]["context"] = normalize_space(
                f"{movies[last_key].get('context', '')} {line}"
            )[:1200]
            if TIME_PATTERN.search(line):
                movies[last_key]["ticket_available"] = True
            continue
        if len(line) < 2 or len(line) > 180:
            continue

        title = clean_card_title(line)
        normalized = normalize_title(title)
        if not normalized:
            continue
        existing_key = title_index.get(normalized)
        if existing_key is None:
            existing_key = f"title:{normalized}"
            movies[existing_key] = movie_candidate(title, "", title)
            title_index[normalized] = existing_key
        last_key = existing_key
    return movies


def collect_snapshot(page: Page, target: Target) -> dict[str, Any]:
    body = main_text(page)
    lines = normalized_lines(body)
    lowered = normalize_space(body).casefold()
    if len(lowered) < 80:
        raise RuntimeError("The page returned too little readable content.")
    if any(marker in lowered for marker in PAGE_FAILURE_MARKERS):
        raise RuntimeError("The page appears to be an error or access-check page.")

    final_url = canonicalize_url(page.url)
    host = urlparse(final_url).hostname or ""
    if host != "cineplex.com" and not host.endswith(".cineplex.com"):
        raise RuntimeError(f"Unexpected redirect away from Cineplex: {page.url}")

    h1 = ""
    try:
        heading = page.locator("main h1")
        if heading.count():
            h1 = normalize_space(heading.first.inner_text(timeout=5000))
    except PlaywrightTimeoutError:
        pass

    if target.type in {"movie", "theatre"}:
        if not h1:
            raise RuntimeError("The expected Cineplex page heading is missing.")
        if not title_matches(target.name, h1):
            raise RuntimeError(f"Unexpected page heading: {h1!r}; expected {target.name!r}.")

    snapshot: dict[str, Any] = {
        "page_title": h1,
        "final_url": final_url,
        "ticket_available": False,
        "ticket_phrases": [],
        "relevant_lines": relevant_lines(body, target.watch_keywords),
    }

    if target.type in {"theatre", "listing"}:
        movies = extract_link_movies(page, target.url)
        # Theatre cards currently do not consistently expose /movie/ anchors.
        # Parse the visible Movies section as a validated fallback.
        if target.type == "theatre" or len(movies) < target.min_movies:
            movies = extract_text_movies(body, movies)
        if len(movies) < target.min_movies:
            raise RuntimeError(
                f"Parser found only {len(movies)} movie(s); expected at least {target.min_movies}."
            )
        snapshot["movies"] = dict(sorted(movies.items()))
        snapshot["ticket_available"] = any(movie_available(movie) for movie in movies.values())
    else:
        phrases = [line for line in lines if TICKET_PATTERN.search(line)]
        snapshot["ticket_phrases"] = phrases[:20]
        snapshot["ticket_available"] = bool(phrases)

    return snapshot


def is_priority_movie(
    title: str,
    movie: dict[str, Any],
    priority_titles: tuple[str, ...],
    priority_formats: tuple[str, ...],
) -> bool:
    if priority_match(title, priority_titles):
        return True
    formats = {normalize_title(value) for value in movie.get("formats", [])}
    wanted = {normalize_title(value) for value in priority_formats}
    return bool(formats & wanted)


def format_movies(
    entries: list[tuple[str, dict[str, Any]]],
    priority_titles: tuple[str, ...],
    priority_formats: tuple[str, ...],
) -> tuple[str, bool]:
    lines: list[str] = []
    has_priority = False
    for key, movie in entries[:20]:
        title = movie.get("title") or key
        priority = is_priority_movie(title, movie, priority_titles, priority_formats)
        has_priority = has_priority or priority
        marker = "🚨" if priority else "•"
        status: list[str] = []
        if movie_available(movie):
            status.append("tickets")
        if movie.get("formats"):
            status.append(", ".join(movie["formats"]))
        suffix = f" — {', '.join(status)}" if status else ""
        href = movie.get("url")
        label = f"[{title}]({href})" if href else f"**{title}**"
        lines.append(f"{marker} {label}{suffix}")
    if len(entries) > 20:
        lines.append(f"• …and {len(entries) - 20} more")
    return "\n".join(lines), has_priority


def compare_snapshots(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    target_name: str,
    priority_titles: tuple[str, ...],
    priority_formats: tuple[str, ...],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    target_is_priority = priority_match(target_name, priority_titles) is not None
    previous_movies = previous.get("movies", {})
    current_movies = current.get("movies", {})

    new_keys = sorted(set(current_movies) - set(previous_movies))
    if new_keys:
        description, has_priority = format_movies(
            [(key, current_movies[key]) for key in new_keys], priority_titles, priority_formats
        )
        events.append(
            {
                "title": "🚨 Priority movie listing detected" if has_priority else "New movie listing detected",
                "description": description,
                "color": 0xE74C3C if has_priority else 0xE67E22,
            }
        )

    availability_changes = [
        (key, current_movies[key])
        for key in sorted(set(previous_movies) & set(current_movies))
        if movie_available(current_movies[key]) and not movie_available(previous_movies[key])
    ]
    if availability_changes:
        description, has_priority = format_movies(
            availability_changes, priority_titles, priority_formats
        )
        events.append(
            {
                "title": "🚨 Priority tickets may now be available" if has_priority else "Tickets may now be available",
                "description": description,
                "color": 0xE74C3C if has_priority else 0xE67E22,
            }
        )

    direct_ticket_transition = bool(current.get("ticket_available")) and not bool(
        previous.get("ticket_available")
    )
    if direct_ticket_transition and not current_movies:
        phrases = current.get("ticket_phrases", [])
        description = "The movie page now shows a ticket-purchase option."
        if phrases:
            description += "\n\n" + "\n".join(f"• {line[:300]}" for line in phrases[:8])
        events.append(
            {
                "title": "🚨 Priority presale detected" if target_is_priority else "Ticket status changed",
                "description": description,
                "color": 0xE74C3C if target_is_priority else 0xE67E22,
            }
        )

    if not direct_ticket_transition and not availability_changes:
        old_lines = set(previous.get("relevant_lines", []))
        added_lines = [line for line in current.get("relevant_lines", []) if line not in old_lines]
        meaningful = [
            line
            for line in added_lines
            if TICKET_PATTERN.search(line) or TIME_PATTERN.search(line)
        ]
        if meaningful:
            events.append(
                {
                    "title": "🚨 Priority ticket/showtime change" if target_is_priority else "New ticket or showtime text detected",
                    "description": "\n".join(f"• {line[:300]}" for line in meaningful[:12]),
                    "color": 0xE74C3C if target_is_priority else 0xE67E22,
                }
            )
    return events


def parse_targets(config: dict[str, Any]) -> tuple[dict[str, Any], list[Target]]:
    settings = config.get("settings", {})
    targets: list[Target] = []
    seen_urls: set[str] = set()
    for index, item in enumerate(config.get("targets", []), start=1):
        try:
            name = normalize_space(item["name"])
            target_type = normalize_space(item.get("type", "generic")).casefold()
            url = normalize_space(item["url"])
        except KeyError as exc:
            raise RuntimeError(f"Target {index} is missing {exc.args[0]!r}.") from exc
        if target_type not in {"theatre", "movie", "listing", "generic"}:
            raise RuntimeError(f"Unsupported target type for {name}: {target_type}")
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or (host != "cineplex.com" and not host.endswith(".cineplex.com")):
            raise RuntimeError(f"Target URL must be an HTTPS Cineplex URL: {url}")
        canonical = canonicalize_url(url)
        if canonical in seen_urls:
            raise RuntimeError(f"Duplicate target URL after canonicalization: {url}")
        seen_urls.add(canonical)
        default_min = 10 if target_type == "listing" else 1 if target_type == "theatre" else 0
        targets.append(
            Target(
                name=name,
                type=target_type,
                url=url,
                watch_keywords=tuple(
                    normalize_space(str(keyword)).casefold()
                    for keyword in item.get("watch_keywords", [])
                    if normalize_space(str(keyword))
                ),
                min_movies=max(0, int(item.get("min_movies", default_min))),
            )
        )
    if not targets:
        raise RuntimeError("No targets are configured in targets.json.")
    return settings, targets


def make_entry(target: Target, snapshot: dict[str, Any], sent_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": target.name,
        "type": target.type,
        "snapshot_version": SNAPSHOT_VERSION,
        "hash": stable_hash(snapshot),
        "snapshot": snapshot,
        "sent_event_ids": (sent_ids or [])[-200:],
    }


def initial_availability_description(target: Target, snapshot: dict[str, Any]) -> str:
    movies = snapshot.get("movies", {})
    available = [movie for movie in movies.values() if movie_available(movie)]
    if available:
        detail = "\n".join(f"  • {movie.get('title', 'Unknown movie')}" for movie in available[:8])
        return f"• **[{target.name}]({target.url})**\n{detail}"
    return f"• **[{target.name}]({target.url})** — ticket button visible"


def baseline_summary(entries: list[tuple[Target, dict[str, Any]]]) -> str:
    lines = ["New targets are now being monitored:", ""]
    for target, snapshot in entries[:30]:
        if target.type == "movie":
            status = "tickets visible" if snapshot.get("ticket_available") else "no ticket button yet"
        else:
            status = f"{len(snapshot.get('movies', {}))} movies parsed"
        lines.append(f"• **{target.name}** — {status}")
    if len(entries) > 30:
        lines.append(f"• …and {len(entries) - 30} more")
    return "\n".join(lines)


def run_monitor(*, test_alert: bool = False) -> int:
    webhook, user_id = get_environment()
    if test_alert:
        discord_post(
            webhook,
            title="✅ Movie ticket monitor connected",
            description="Discord delivery and retry handling are working.",
            user_id=user_id,
            color=0x3498DB,
        )
        LOGGER.info("Test alert sent successfully.")
        return 0

    config = load_json(CONFIG_PATH, {})
    settings, targets = parse_targets(config)
    state: dict[str, Any] = load_json(STATE_PATH, {})
    original_state = copy.deepcopy(state)
    wait_ms = int(settings.get("page_wait_ms", 2500))
    timeout_seconds = int(settings.get("request_timeout_seconds", 45))
    scroll_passes = int(settings.get("scroll_passes", 8))
    send_baseline_summary = bool(settings.get("send_baseline_summary", True))
    send_error_alerts = bool(settings.get("send_error_alerts", True))
    alert_available_on_first_seen = bool(settings.get("alert_available_on_first_seen", True))
    error_alert_every = max(2, int(settings.get("error_alert_every", 6)))
    priority_titles = tuple(normalize_space(value) for value in config.get("priority_titles", []))
    priority_formats = tuple(normalize_space(value) for value in config.get("priority_formats", []))

    any_success = False
    any_failure = False
    successful_urls: set[str] = set()
    baseline_entries: list[tuple[Target, dict[str, Any]]] = []
    target_by_url = {target.url: target for target in targets}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = make_context(browser, timeout_seconds)
        try:
            for target in targets:
                page = context.new_page()
                try:
                    open_target(
                        page,
                        target,
                        wait_ms=wait_ms,
                        timeout_seconds=timeout_seconds,
                        scroll_passes=scroll_passes,
                    )
                    snapshot = collect_snapshot(page, target)
                    current_hash = stable_hash(snapshot)
                    previous = state.get(target.url)
                    any_success = True
                    successful_urls.add(target.url)

                    if previous is None:
                        LOGGER.info("Baseline created: %s", target.name)
                        baseline_entries.append((target, snapshot))
                        entry = make_entry(target, snapshot)
                        if alert_available_on_first_seen and snapshot.get("ticket_available"):
                            entry["pending_first_alert"] = True
                        state[target.url] = entry
                        continue

                    if previous.get("snapshot_version") != SNAPSHOT_VERSION:
                        # Safe one-time migration: replace the old broken snapshot without
                        # treating every currently visible theatre movie as a new listing.
                        LOGGER.info("Snapshot schema migrated: %s", target.name)
                        state[target.url] = make_entry(
                            target,
                            snapshot,
                            list(previous.get("sent_event_ids", [])),
                        )
                        continue

                    entry = copy.deepcopy(previous)
                    previous_failures = int(entry.get("consecutive_failures", 0))
                    if previous_failures:
                        discord_post(
                            webhook,
                            title=f"✅ Monitor recovered: {target.name}",
                            description=f"The target is readable again after {previous_failures} failed check(s).",
                            url=target.url,
                            color=0x2ECC71,
                        )
                        for key in (
                            "consecutive_failures",
                            "last_error",
                            "last_error_at_utc",
                        ):
                            entry.pop(key, None)

                    if current_hash == previous.get("hash"):
                        LOGGER.info("No change: %s", target.name)
                        state[target.url] = entry
                        continue

                    events = compare_snapshots(
                        previous.get("snapshot", {}),
                        snapshot,
                        target_name=target.name,
                        priority_titles=priority_titles,
                        priority_formats=priority_formats,
                    )
                    sent_ids = list(entry.get("sent_event_ids", []))
                    for event in events:
                        event_id = stable_hash(
                            {
                                "target": target.url,
                                "title": event["title"],
                                "description": event["description"],
                            }
                        )
                        if event_id in sent_ids:
                            LOGGER.info("Skipping already-delivered event: %s", event["title"])
                            continue
                        # Persist each successful delivery ID locally before the next event.
                        # This prevents duplicate event 1 if event 2 fails in the same run.
                        state[target.url] = entry
                        discord_post(
                            webhook,
                            title=f"🎟️ {event['title']}: {target.name}",
                            description=event["description"],
                            url=target.url,
                            user_id=user_id,
                            color=int(event["color"]),
                        )
                        sent_ids.append(event_id)
                        entry["sent_event_ids"] = sent_ids[-200:]
                        state[target.url] = entry
                        save_json(STATE_PATH, state)

                    if events:
                        LOGGER.info("Processed %s alert event(s): %s", len(events), target.name)
                    else:
                        LOGGER.info("Validated change without a ticket event: %s", target.name)
                    updated = make_entry(target, snapshot, sent_ids)
                    state[target.url] = updated
                except Exception as exc:
                    any_failure = True
                    LOGGER.exception("Failed target %s", target.name)
                    entry = copy.deepcopy(state.get(target.url, {}))
                    failures = int(entry.get("consecutive_failures", 0)) + 1
                    entry.update(
                        {
                            "name": target.name,
                            "type": target.type,
                            "consecutive_failures": failures,
                            "last_error": f"{type(exc).__name__}: {exc}",
                            "last_error_at_utc": utc_now(),
                        }
                    )
                    state[target.url] = entry
                    if send_error_alerts and (failures == 1 or failures % error_alert_every == 0):
                        try:
                            discord_post(
                                webhook,
                                title=f"⚠️ Monitor error: {target.name}",
                                description=(
                                    f"Check **{failures}** failed. The last known-good snapshot was preserved.\n\n"
                                    f"`{type(exc).__name__}: {str(exc)[:1300]}`"
                                ),
                                url=target.url,
                                user_id=user_id if failures == 1 else "",
                                color=0xE74C3C,
                            )
                        except Exception:
                            LOGGER.exception("Could not send the Discord error alert.")
                finally:
                    page.close()
        finally:
            context.close()
            browser.close()

    pending = [
        (target_by_url[url], state[url].get("snapshot", {}))
        for url in successful_urls
        if url in target_by_url and state.get(url, {}).get("pending_first_alert")
    ]
    if pending:
        try:
            description = "\n".join(
                initial_availability_description(target, snapshot)
                for target, snapshot in pending[:20]
            )
            discord_post(
                webhook,
                title=f"🚨 Tickets already available on {len(pending)} newly added target(s)",
                description=description,
                user_id=user_id,
                color=0xE74C3C,
            )
            for target, _ in pending:
                state[target.url].pop("pending_first_alert", None)
        except Exception:
            any_failure = True
            LOGGER.exception("Could not send the first-observation availability alert.")

    if baseline_entries and send_baseline_summary:
        try:
            discord_post(
                webhook,
                title=f"✅ Baseline created for {len(baseline_entries)} new target(s)",
                description=baseline_summary(baseline_entries),
                color=0x3498DB,
            )
        except Exception:
            any_failure = True
            LOGGER.exception("Could not send the baseline summary.")

    if state != original_state:
        save_json(STATE_PATH, state)
    else:
        LOGGER.info("State is unchanged; no state commit is needed.")

    LOGGER.info(
        "Run summary: %s successful target(s), %s failure(s)",
        len(successful_urls),
        len(targets) - len(successful_urls),
    )
    if not any_success:
        return 1
    return 2 if any_failure else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Cineplex pages and send Discord alerts.")
    parser.add_argument("--test-alert", action="store_true", help="Send a Discord test message only.")
    parser.add_argument("--validate-config", action="store_true", help="Validate targets.json and exit.")
    args = parser.parse_args()

    try:
        if args.validate_config:
            _, targets = parse_targets(load_json(CONFIG_PATH, {}))
            print(f"Configuration is valid: {len(targets)} target(s).")
            return 0
        return run_monitor(test_alert=args.test_alert)
    except Exception as exc:
        LOGGER.exception("Fatal monitor error")
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        user_id = os.environ.get("DISCORD_USER_ID", "").strip()
        if webhook:
            try:
                discord_post(
                    webhook,
                    title="🚨 Fatal movie-monitor error",
                    description=f"`{type(exc).__name__}: {str(exc)[:1500]}`",
                    user_id=user_id if user_id.isdigit() else "",
                    color=0xE74C3C,
                    max_attempts=2,
                )
            except Exception:
                LOGGER.exception("Could not send the fatal Discord alert.")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
