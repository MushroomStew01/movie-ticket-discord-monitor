from __future__ import annotations

import argparse
import copy
import hashlib
import html
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
ARTIFACT_DIR = ROOT / "artifacts"
SNAPSHOT_VERSION = 4

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
    alert_available_on_first_seen: bool | None = None


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


def event_id_for_transition(target_url: str, event: dict[str, Any], revision: int) -> str:
    return stable_hash(
        {
            "target": target_url,
            "transition_revision": revision,
            "title": event["title"],
            "description": event["description"],
        }
    )


def canonical_movie_key(title: str) -> str:
    """Return a stable key that does not depend on Cineplex link markup."""
    normalized = normalize_title(title)
    return f"title:{normalized.replace(' ', '-')}" if normalized else ""


def canonicalize_movies(movies: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge duplicate cards and key movies by title instead of transient URLs."""
    canonical: dict[str, dict[str, Any]] = {}
    for source_key, source_movie in movies.items():
        movie = copy.deepcopy(source_movie)
        title = normalize_space(str(movie.get("title", "")))
        key = canonical_movie_key(title)
        if not key:
            LOGGER.warning("Ignoring movie card without a usable title: %s", source_key)
            continue
        movie["title"] = title
        movie["formats"] = sorted(set(movie.get("formats", [])))
        movie["showtimes"] = sorted(set(movie.get("showtimes", [])))
        movie["dates"] = sorted(set(movie.get("dates", [])))
        movie["ticket_available"] = movie_available(movie)

        existing = canonical.get(key)
        if existing is None:
            canonical[key] = movie
            continue

        existing["ticket_available"] = movie_available(existing) or movie_available(movie)
        for field in ("formats", "showtimes", "dates"):
            existing[field] = sorted(set(existing.get(field, [])) | set(movie.get(field, [])))
        if not existing.get("url") and movie.get("url"):
            existing["url"] = movie["url"]
        if len(str(movie.get("context", ""))) > len(str(existing.get("context", ""))):
            existing["context"] = movie["context"]
    return dict(sorted(canonical.items()))


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


def discord_send_payload(
    webhook: str,
    payload: dict[str, Any],
    *,
    max_attempts: int = 4,
) -> None:
    """Deliver one Discord webhook payload with bounded retry handling."""
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
    discord_send_payload(webhook, payload, max_attempts=max_attempts)


def build_overview_batches(
    queued_events: list[dict[str, Any]],
) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """Create Discord-safe embed batches while retaining their source events."""
    batches: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
    embeds: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    character_count = 0

    ordered_events = sorted(
        queued_events,
        key=lambda item: (
            0 if int(item["event"]["color"]) == 0xE74C3C else 1,
            normalize_title(item["target_name"]),
            normalize_title(item["event"]["title"]),
        ),
    )
    for item in ordered_events:
        event = item["event"]
        title = normalize_space(f"{item['target_name']} — {event['title']}")[:256]
        target_link = f"[Open Cineplex page]({item['target_url']})"
        description = f"{target_link}\n\n{event['description']}"
        if len(description) > 4000:
            description = description[:3997].rstrip() + "…"
        embed = {
            "title": title,
            "description": description,
            "color": int(event["color"]),
            "timestamp": utc_now(),
            "footer": {"text": "Movie Ticket Monitor"},
        }
        embed_size = len(title) + len(description) + len("Movie Ticket Monitor")
        if embeds and (len(embeds) >= 10 or character_count + embed_size > 5700):
            batches.append((embeds, items))
            embeds = []
            items = []
            character_count = 0
        embeds.append(embed)
        items.append(item)
        character_count += embed_size

    if embeds:
        batches.append((embeds, items))
    return batches


def discord_post_overview_batch(
    webhook: str,
    embeds: list[dict[str, Any]],
    *,
    user_id: str = "",
    part: int = 1,
    total_parts: int = 1,
) -> None:
    heading = "🎟️ Cineplex update overview"
    if total_parts > 1:
        heading += f" — part {part}/{total_parts}"
    content = f"<@{user_id}>\n{heading}" if user_id else heading
    payload = {
        "username": "Movie Ticket Monitor",
        "content": content,
        "allowed_mentions": {"users": [user_id]} if user_id else {"parse": []},
        "embeds": embeds,
    }
    discord_send_payload(webhook, payload)


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


def html_to_readable_text(raw_html: str) -> str:
    cleaned = re.sub(
        r"<(?:script|style|noscript)\b[^>]*>.*?</(?:script|style|noscript)>",
        " ",
        raw_html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"</(?:p|div|section|article|li|h[1-6]|button|a|br)\s*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return "\n".join(normalized_lines(html.unescape(cleaned)))


def text_contains_title(text: str, expected_title: str) -> bool:
    expected = normalize_title(expected_title)
    actual = normalize_title(text)
    return bool(expected and (expected in actual or title_matches(expected_title, text[:500])))


def fetch_static_page_text(page: Page, expected_title: str) -> str:
    try:
        user_agent = page.evaluate("() => navigator.userAgent")
    except Exception:
        user_agent = "Mozilla/5.0 MovieTicketMonitor/1.0"
    try:
        response = requests.get(
            page.url,
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "en-CA,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=30,
        )
        response.raise_for_status()
        parsed = html_to_readable_text(response.text[:5_000_000])
        if len(normalize_space(parsed)) >= 80 and text_contains_title(parsed, expected_title):
            LOGGER.info("Using server-rendered HTML fallback for %s", expected_title)
            return parsed
    except requests.RequestException as exc:
        LOGGER.warning("Server-rendered fallback failed for %s: %s", expected_title, exc)
    return ""


def main_text(page: Page, expected_title: str = "") -> str:
    candidates: list[tuple[str, str]] = []
    try:
        candidates.append(("main", page.locator("main").inner_text(timeout=30_000)))
    except PlaywrightTimeoutError:
        pass

    main = candidates[0][1] if candidates else ""
    if len(normalize_space(main)) >= 80 and (
        not expected_title or text_contains_title(main, expected_title)
    ):
        return main

    try:
        candidates.append(("body", page.locator("body").inner_text(timeout=15_000)))
    except PlaywrightTimeoutError:
        pass

    valid = [
        (source, text)
        for source, text in candidates
        if len(normalize_space(text)) >= 80
        and (not expected_title or text_contains_title(text, expected_title))
    ]
    if valid:
        source, text = max(valid, key=lambda item: len(item[1]))
        if source != "main":
            LOGGER.info("Using full-body fallback for %s", expected_title or page.url)
        return text

    static_text = fetch_static_page_text(page, expected_title) if expected_title else ""
    if static_text:
        return static_text

    longest = max(candidates, key=lambda item: len(item[1]), default=("", ""))[1]
    if longest:
        return longest
    raise RuntimeError("The page content did not become readable.")


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


def extract_showtimes(text: str) -> list[str]:
    values: set[str] = set()
    for match in TIME_PATTERN.finditer(text or ""):
        value = normalize_space(match.group(0)).upper().replace(".", "")
        value = re.sub(r"\s*([AP]M)$", r" \1", value)
        values.add(value)
    return sorted(values)


def extract_dates(text: str) -> list[str]:
    return sorted(
        {
            normalize_space(match.group(0)).title()
            for match in DATE_PATTERN.finditer(text or "")
            if normalize_space(match.group(0))
        }
    )


def movie_available(movie: dict[str, Any]) -> bool:
    return bool(movie.get("ticket_available")) or movie.get("advance_tickets") == "True" or movie.get("get_tickets") == "True"


def movie_candidate(title: str, href: str, context: str) -> dict[str, Any]:
    return {
        "title": title,
        "ticket_available": TICKET_PATTERN.search(context) is not None,
        "formats": extract_formats(context),
        "showtimes": extract_showtimes(context),
        "dates": extract_dates(context),
        "context": normalize_space(context)[:1200],
        "url": href or None,
    }


def extract_link_movies(page: Page, base_url: str) -> dict[str, dict[str, Any]]:
    raw_items = page.evaluate(
        """() => Array.from(document.querySelectorAll('main a[href*="/movie/"]')).map(a => {
            let node = a;
            let bestText = (a.innerText || '').trim();
            for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                const movieLinks = Array.from(node.querySelectorAll('a[href*="/movie/"]'));
                const uniqueMovies = new Set(movieLinks.map(link => {
                    try { return new URL(link.href, document.baseURI).pathname; }
                    catch (_) { return link.getAttribute('href') || ''; }
                }));
                if (uniqueMovies.size > 1) break;
                const text = (node.innerText || '').trim();
                if (text.length >= bestText.length && text.length <= 4000) bestText = text;
            }
            return {
                href: a.href || a.getAttribute('href') || '',
                text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '').trim(),
                context: bestText
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
                movies[last_key]["showtimes"] = sorted(
                    set(movies[last_key].get("showtimes", [])) | set(extract_showtimes(line))
                )
                movies[last_key]["dates"] = sorted(
                    set(movies[last_key].get("dates", [])) | set(extract_dates(line))
                )
                movies[last_key]["formats"] = sorted(
                    set(movies[last_key].get("formats", [])) | set(extract_formats(line))
                )
                movies[last_key]["context"] = normalize_space(
                    f"{movies[last_key].get('context', '')} {line}"
                )[:1200]
            continue
        showtimes = extract_showtimes(line)
        dates = extract_dates(line)
        formats = extract_formats(line)
        if last_key is not None and (showtimes or dates):
            movies[last_key]["showtimes"] = sorted(
                set(movies[last_key].get("showtimes", [])) | set(showtimes)
            )
            movies[last_key]["dates"] = sorted(
                set(movies[last_key].get("dates", [])) | set(dates)
            )
            movies[last_key]["formats"] = sorted(
                set(movies[last_key].get("formats", [])) | set(formats)
            )
            movies[last_key]["context"] = normalize_space(
                f"{movies[last_key].get('context', '')} {line}"
            )[:1200]
            if showtimes:
                movies[last_key]["ticket_available"] = True
            continue
        if last_key is not None and formats and len(line) < 100:
            movies[last_key]["formats"] = sorted(set(movies[last_key].get("formats", [])) | set(formats))
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


def safe_artifact_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_title(value)).strip("-")
    return slug[:80] or "target"


def save_failure_artifacts(page: Page, target: Target, attempt: int, exc: Exception) -> None:
    """Keep bounded diagnostics for the final failed attempt only."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = ARTIFACT_DIR / f"{safe_artifact_name(target.name)}-{stamp}"
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        metadata = {
            "target": target.name,
            "target_url": target.url,
            "final_url": getattr(page, "url", ""),
            "attempt": attempt,
            "error": f"{type(exc).__name__}: {exc}",
            "captured_at_utc": utc_now(),
        }
        save_json(prefix.with_suffix(".json"), metadata)
        try:
            prefix.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        except Exception as content_exc:
            LOGGER.warning("Could not save failure HTML for %s: %s", target.name, content_exc)
        try:
            page.screenshot(path=str(prefix.with_suffix(".png")), full_page=True, timeout=15_000)
        except Exception as screenshot_exc:
            LOGGER.warning("Could not save failure screenshot for %s: %s", target.name, screenshot_exc)
        LOGGER.info("Saved failure diagnostics with prefix %s", prefix)
    except Exception as artifact_exc:
        LOGGER.warning("Could not save failure diagnostics for %s: %s", target.name, artifact_exc)


def collect_snapshot(page: Page, target: Target) -> dict[str, Any]:
    body = main_text(page, target.name)
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
        heading = page.locator("h1")
        for index in range(min(heading.count(), 10)):
            candidate = normalize_space(heading.nth(index).inner_text(timeout=5000))
            if candidate and title_matches(target.name, candidate):
                h1 = candidate
                break
    except PlaywrightTimeoutError:
        pass

    if not h1 and text_contains_title(body, target.name):
        # Some Cineplex movie templates server-render the correct title but do
        # not expose it through the hydrated main-region DOM on Linux runners.
        h1 = target.name

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
        "showtimes": extract_showtimes(body),
        "dates": extract_dates(body),
        "formats": extract_formats(body),
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
        movies = canonicalize_movies(movies)
        snapshot["movies"] = movies
        snapshot["ticket_available"] = any(movie_available(movie) for movie in movies.values())
    else:
        phrases = [line for line in lines if TICKET_PATTERN.search(line)]
        snapshot["ticket_phrases"] = phrases[:20]
        snapshot["ticket_available"] = bool(phrases)

    return snapshot


def collect_target_with_retry(
    context: BrowserContext,
    target: Target,
    *,
    wait_ms: int,
    timeout_seconds: int,
    scroll_passes: int,
    attempts: int,
    retry_wait_ms: int,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        page: Page | None = None
        try:
            page = context.new_page()
            open_target(
                page,
                target,
                wait_ms=wait_ms,
                timeout_seconds=timeout_seconds,
                scroll_passes=scroll_passes,
            )
            return collect_snapshot(page, target)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                if page is not None:
                    save_failure_artifacts(page, target, attempt, exc)
                raise
            LOGGER.warning(
                "Attempt %s/%s failed for %s: %s: %s; retrying",
                attempt,
                attempts,
                target.name,
                type(exc).__name__,
                exc,
            )
            time.sleep(max(0, retry_wait_ms) / 1000 * attempt)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    LOGGER.warning("Could not close a retry page for %s", target.name)
    raise RuntimeError(f"Target failed without an exception: {target.name}") from last_error


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
    target_type: str = "generic",
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
                "title": (
                    "🚨 Priority movie added to this theatre"
                    if has_priority and target_type == "theatre"
                    else "🚨 Priority movie listing detected"
                    if has_priority
                    else "New movie listing detected"
                ),
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

    inventory_lines: list[str] = []
    inventory_has_priority = False
    for key in sorted(set(previous_movies) & set(current_movies)):
        old_movie = previous_movies[key]
        new_movie = current_movies[key]
        title = new_movie.get("title") or key
        is_priority = is_priority_movie(title, new_movie, priority_titles, priority_formats)
        new_times = sorted(set(new_movie.get("showtimes", [])) - set(old_movie.get("showtimes", [])))
        new_dates = sorted(set(new_movie.get("dates", [])) - set(old_movie.get("dates", [])))
        new_formats = sorted(set(new_movie.get("formats", [])) - set(old_movie.get("formats", [])))
        if not (new_times or new_dates or new_formats):
            continue
        inventory_has_priority = inventory_has_priority or is_priority
        marker = "🚨" if is_priority else "•"
        inventory_lines.append(f"{marker} **{title}**")
        if new_dates:
            inventory_lines.append(f"  • New dates: {', '.join(new_dates[:8])}")
        if new_times:
            inventory_lines.append(f"  • New showtimes: {', '.join(new_times[:12])}")
        if new_formats:
            inventory_lines.append(f"  • New formats: {', '.join(new_formats[:8])}")
    if inventory_lines:
        events.append(
            {
                "title": (
                    "🚨 New priority showtime inventory"
                    if inventory_has_priority
                    else "New showtime inventory"
                ),
                "description": "\n".join(inventory_lines[:40]),
                "color": 0xE74C3C if inventory_has_priority else 0xE67E22,
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

    direct_inventory = False
    if target_is_priority and not current_movies and not direct_ticket_transition:
        new_times = sorted(set(current.get("showtimes", [])) - set(previous.get("showtimes", [])))
        new_dates = sorted(set(current.get("dates", [])) - set(previous.get("dates", [])))
        new_formats = sorted(set(current.get("formats", [])) - set(previous.get("formats", [])))
        if new_times or new_dates or new_formats:
            direct_inventory = True
            parts: list[str] = []
            if new_dates:
                parts.append(f"• New dates: {', '.join(new_dates[:8])}")
            if new_times:
                parts.append(f"• New showtimes: {', '.join(new_times[:12])}")
            if new_formats:
                parts.append(f"• New formats: {', '.join(new_formats[:8])}")
            events.append(
                {
                    "title": "🚨 New priority showtime inventory",
                    "description": "\n".join(parts),
                    "color": 0xE74C3C,
                }
            )

    if not direct_ticket_transition and not availability_changes and not inventory_lines and not direct_inventory:
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
                alert_available_on_first_seen=(
                    bool(item["alert_available_on_first_seen"])
                    if "alert_available_on_first_seen" in item
                    else None
                ),
            )
        )
    if not targets:
        raise RuntimeError("No targets are configured in targets.json.")
    return settings, targets


def make_entry(
    target: Target,
    snapshot: dict[str, Any],
    sent_ids: list[str] | None = None,
    *,
    revision: int = 0,
) -> dict[str, Any]:
    return {
        "name": target.name,
        "type": target.type,
        "snapshot_version": SNAPSHOT_VERSION,
        "hash": stable_hash(snapshot),
        "snapshot": snapshot,
        "sent_event_ids": (sent_ids or [])[-200:],
        "revision": max(0, revision),
    }


def heartbeat_due(meta: dict[str, Any], interval_hours: int, now: datetime | None = None) -> bool:
    if interval_hours <= 0:
        return False
    current = now or datetime.now(timezone.utc)
    raw = str(meta.get("last_heartbeat_at_utc", ""))
    if not raw:
        return True
    try:
        previous = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (current - previous).total_seconds() >= interval_hours * 3600


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


def prune_unconfigured_state(state: dict[str, Any], configured_urls: set[str]) -> list[str]:
    """Remove snapshots for targets that no longer exist in the configuration."""
    stale = [
        key
        for key in state
        if key.startswith("https://") and key not in configured_urls
    ]
    for key in stale:
        state.pop(key, None)
    return sorted(stale)


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
    target_attempts = max(1, int(settings.get("target_attempts", 3)))
    retry_wait_ms = max(0, int(settings.get("retry_wait_ms", 1500)))
    send_baseline_summary = bool(settings.get("send_baseline_summary", True))
    send_error_alerts = bool(settings.get("send_error_alerts", True))
    alert_available_on_first_seen = bool(settings.get("alert_available_on_first_seen", True))
    error_alert_every = max(2, int(settings.get("error_alert_every", 6)))
    heartbeat_interval_hours = max(0, int(settings.get("heartbeat_interval_hours", 24)))
    priority_titles = tuple(normalize_space(value) for value in config.get("priority_titles", []))
    priority_formats = tuple(normalize_space(value) for value in config.get("priority_formats", []))

    any_success = False
    any_failure = False
    successful_urls: set[str] = set()
    baseline_entries: list[tuple[Target, dict[str, Any]]] = []
    baseline_summary_sent = False
    queued_events: list[dict[str, Any]] = []
    pending_event_entries: dict[str, dict[str, Any]] = {}
    target_by_url = {target.url: target for target in targets}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = make_context(browser, timeout_seconds)
        try:
            for target in targets:
                try:
                    snapshot = collect_target_with_retry(
                        context,
                        target,
                        wait_ms=wait_ms,
                        timeout_seconds=timeout_seconds,
                        scroll_passes=scroll_passes,
                        attempts=target_attempts,
                        retry_wait_ms=retry_wait_ms,
                    )
                    current_hash = stable_hash(snapshot)
                    previous = state.get(target.url)
                    any_success = True
                    successful_urls.add(target.url)

                    if previous is None:
                        LOGGER.info("Baseline created: %s", target.name)
                        baseline_entries.append((target, snapshot))
                        entry = make_entry(target, snapshot)
                        first_seen_alert = (
                            target.alert_available_on_first_seen
                            if target.alert_available_on_first_seen is not None
                            else alert_available_on_first_seen
                        )
                        if first_seen_alert and snapshot.get("ticket_available"):
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
                            revision=int(previous.get("revision", 0)),
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
                        target_type=target.type,
                        priority_titles=priority_titles,
                        priority_formats=priority_formats,
                    )
                    transition_revision = int(previous.get("revision", 0)) + 1
                    sent_ids = list(entry.get("sent_event_ids", []))
                    unsent_count = 0
                    for event in events:
                        event_id = event_id_for_transition(
                            target.url,
                            event,
                            transition_revision,
                        )
                        if event_id in sent_ids:
                            LOGGER.info("Skipping already-delivered event: %s", event["title"])
                            continue
                        queued_events.append(
                            {
                                "target_url": target.url,
                                "target_name": target.name,
                                "event_id": event_id,
                                "event": event,
                            }
                        )
                        unsent_count += 1

                    if events:
                        LOGGER.info(
                            "Queued %s of %s alert event(s): %s",
                            unsent_count,
                            len(events),
                            target.name,
                        )
                    else:
                        LOGGER.info("Validated change without a ticket event: %s", target.name)
                    updated = make_entry(
                        target,
                        snapshot,
                        sent_ids,
                        revision=transition_revision,
                    )
                    if unsent_count:
                        # Keep the previous snapshot until its overview events are delivered.
                        state[target.url] = entry
                        pending_event_entries[target.url] = updated
                    else:
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
            context.close()
            browser.close()

    if queued_events:
        batches = build_overview_batches(queued_events)
        for batch_number, (embeds, batch_items) in enumerate(batches, start=1):
            try:
                discord_post_overview_batch(
                    webhook,
                    embeds,
                    user_id=user_id if batch_number == 1 else "",
                    part=batch_number,
                    total_parts=len(batches),
                )
            except Exception:
                any_failure = True
                LOGGER.exception("Could not deliver the Discord change overview.")
                break

            # Persist every successfully delivered batch before attempting the next.
            for item in batch_items:
                target_url = item["target_url"]
                entry = copy.deepcopy(state.get(target_url, {}))
                sent_ids = list(entry.get("sent_event_ids", []))
                if item["event_id"] not in sent_ids:
                    sent_ids.append(item["event_id"])
                entry["sent_event_ids"] = sent_ids[-200:]
                state[target_url] = entry
            save_json(STATE_PATH, state)

        queued_ids_by_target: dict[str, set[str]] = {}
        for item in queued_events:
            queued_ids_by_target.setdefault(item["target_url"], set()).add(item["event_id"])
        for target_url, pending_entry in pending_event_entries.items():
            delivered_list = list(state.get(target_url, {}).get("sent_event_ids", []))
            if queued_ids_by_target.get(target_url, set()) <= set(delivered_list):
                pending_entry["sent_event_ids"] = delivered_list[-200:]
                state[target_url] = pending_entry

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
            baseline_summary_sent = True
        except Exception:
            any_failure = True
            LOGGER.exception("Could not send the baseline summary.")

    meta = state.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        state["_meta"] = meta
    if baseline_summary_sent:
        # The baseline message already confirms that the monitor is healthy.
        meta["last_heartbeat_at_utc"] = utc_now()
    elif any_success and not any_failure and heartbeat_due(meta, heartbeat_interval_hours):
        try:
            discord_post(
                webhook,
                title="✅ Movie monitor heartbeat",
                description=(
                    f"The monitor completed successfully and checked **{len(successful_urls)}** "
                    "Cineplex target(s). Change alerts remain active."
                ),
                color=0x3498DB,
            )
            meta["last_heartbeat_at_utc"] = utc_now()
        except Exception:
            any_failure = True
            LOGGER.exception("Could not send the monitor heartbeat.")

    stale_urls = prune_unconfigured_state(state, set(target_by_url))
    if stale_urls:
        LOGGER.info("Removed %s unconfigured target snapshot(s) from state.", len(stale_urls))

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
