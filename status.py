from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "targets.json"
STATE_PATH = ROOT / "state.json"
STATUS_PATH = ROOT / "status.json"
SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize_title(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


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


def movie_available(movie: dict[str, Any]) -> bool:
    return bool(movie.get("ticket_available") or movie.get("showtimes"))


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _matching_movie(snapshot: dict[str, Any], priority_title: str) -> dict[str, Any] | None:
    movies = snapshot.get("movies")
    if not isinstance(movies, dict):
        return None
    for movie in movies.values():
        if not isinstance(movie, dict):
            continue
        if title_matches(priority_title, str(movie.get("title") or "")):
            return movie
    return None


def _merge_priority(
    aggregate: dict[str, Any],
    movie: dict[str, Any],
    *,
    theatre_name: str | None = None,
    direct_target: str | None = None,
) -> None:
    aggregate["ticket_available"] = bool(aggregate["ticket_available"] or movie_available(movie))
    if theatre_name and theatre_name not in aggregate["theatres"]:
        aggregate["theatres"].append(theatre_name)
    if direct_target and direct_target not in aggregate["direct_targets"]:
        aggregate["direct_targets"].append(direct_target)
    for key in ("showtimes", "dates", "formats"):
        aggregate[key] = sorted(set(aggregate[key]) | set(_strings(movie.get(key))))


def build_status(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    targets = [item for item in config.get("targets", []) if isinstance(item, dict)]
    priority_titles = [
        str(value)
        for value in config.get("priority_titles", [])
        if str(value).strip()
    ]
    priority: dict[str, dict[str, Any]] = {
        title: {
            "title": title,
            "ticket_available": False,
            "theatres": [],
            "showtimes": [],
            "dates": [],
            "formats": [],
            "direct_targets": [],
        }
        for title in priority_titles
    }

    failing_targets: list[str] = []
    missing_targets: list[str] = []
    theatres: list[dict[str, Any]] = []

    for target in targets:
        name = str(target.get("name") or "Unnamed target")
        target_type = str(target.get("type") or "generic").casefold()
        url = str(target.get("url") or "")
        entry = state.get(url)
        if not isinstance(entry, dict):
            missing_targets.append(name)
            entry = {}
        failures = int(entry.get("consecutive_failures") or 0)
        if failures:
            failing_targets.append(name)
        snapshot = entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else {}

        if target_type == "theatre":
            movies = snapshot.get("movies") if isinstance(snapshot.get("movies"), dict) else {}
            theatre_priority: dict[str, dict[str, Any]] = {}
            for title in priority_titles:
                movie = _matching_movie(snapshot, title)
                if movie is None:
                    continue
                theatre_priority[title] = {
                    "ticket_available": movie_available(movie),
                    "showtimes": _strings(movie.get("showtimes")),
                    "dates": _strings(movie.get("dates")),
                    "formats": _strings(movie.get("formats")),
                }
                _merge_priority(priority[title], movie, theatre_name=name)
            theatres.append(
                {
                    "name": name,
                    "url": url,
                    "healthy": failures == 0 and bool(entry),
                    "movie_count": len(movies),
                    "priority": theatre_priority,
                }
            )
            continue

        if target_type == "movie":
            for title in priority_titles:
                if not title_matches(title, name):
                    continue
                direct_movie = {
                    "ticket_available": bool(snapshot.get("ticket_available")),
                    "showtimes": _strings(snapshot.get("showtimes")),
                    "dates": _strings(snapshot.get("dates")),
                    "formats": _strings(snapshot.get("formats")),
                }
                _merge_priority(priority[title], direct_movie, direct_target=name)

    failing_targets.sort()
    missing_targets.sort()
    theatres.sort(key=lambda item: item["name"])
    for item in priority.values():
        item["theatres"].sort()
        item["direct_targets"].sort()

    meta = state.get("_meta") if isinstance(state.get("_meta"), dict) else {}
    healthy = not failing_targets and not missing_targets and bool(targets)
    return {
        "healthy": healthy,
        "target_count": len(targets),
        "failing_targets": failing_targets,
        "missing_targets": missing_targets,
        "theatre_count": sum(
            1
            for target in targets
            if str(target.get("type", "")).casefold() == "theatre"
        ),
        "healthy_theatre_count": sum(1 for theatre in theatres if theatre["healthy"]),
        "heartbeat_at": meta.get("last_heartbeat_at_utc"),
        "priority_titles": priority_titles,
        "priority": priority,
        "theatres": theatres,
    }


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render_status(
    config: dict[str, Any],
    state: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = build_status(config, state)
    state_id = stable_hash(body)
    existing = existing or {}
    if existing.get("state_id") == state_id and existing.get("updated_at"):
        updated_at = existing["updated_at"]
    else:
        updated_at = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": updated_at,
        "state_id": state_id,
        **body,
    }


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    existing = load_json(STATUS_PATH, {})
    status = render_status(config, state, existing)
    if status == existing:
        print("Stewy OS status feed unchanged.")
        return 0
    save_json(STATUS_PATH, status)
    print(
        "Stewy OS status feed updated: "
        f"healthy={status['healthy']} targets={status['target_count']} "
        f"state={status['state_id'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
