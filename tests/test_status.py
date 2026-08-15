from __future__ import annotations

import unittest

from status import build_status, render_status


def sample_config() -> dict:
    return {
        "priority_titles": ["Dune: Part 3"],
        "targets": [
            {"name": "Kitchener", "type": "theatre", "url": "https://example.test/kitchener"},
            {"name": "Cambridge", "type": "theatre", "url": "https://example.test/cambridge"},
            {"name": "Dune: Part 3", "type": "movie", "url": "https://example.test/dune"},
        ],
    }


def sample_state() -> dict:
    return {
        "https://example.test/kitchener": {
            "snapshot": {
                "movies": {
                    "title:dune-part-3": {
                        "title": "Dune: Part 3",
                        "ticket_available": True,
                        "showtimes": ["7:00 pm"],
                        "dates": ["Dec 18"],
                        "formats": ["IMAX"],
                    }
                }
            }
        },
        "https://example.test/cambridge": {"snapshot": {"movies": {}}},
        "https://example.test/dune": {
            "snapshot": {
                "ticket_available": True,
                "showtimes": ["10:15 pm"],
                "dates": ["Dec 18"],
                "formats": ["70MM"],
            }
        },
        "_meta": {"last_heartbeat_at_utc": "2026-08-15T20:00:00+00:00"},
    }


class StatusFeedTests(unittest.TestCase):
    def test_build_status_aggregates_priority_inventory(self) -> None:
        status = build_status(sample_config(), sample_state())
        dune = status["priority"]["Dune: Part 3"]
        self.assertTrue(status["healthy"])
        self.assertEqual(status["theatre_count"], 2)
        self.assertTrue(dune["ticket_available"])
        self.assertEqual(dune["theatres"], ["Kitchener"])
        self.assertEqual(dune["showtimes"], ["10:15 pm", "7:00 pm"])
        self.assertEqual(dune["formats"], ["70MM", "IMAX"])

    def test_render_status_preserves_timestamp_when_state_is_unchanged(self) -> None:
        first = render_status(sample_config(), sample_state())
        existing = {**first, "updated_at": "2026-08-15T20:00:00+00:00"}
        second = render_status(sample_config(), sample_state(), existing)
        self.assertEqual(second["state_id"], first["state_id"])
        self.assertEqual(second["updated_at"], "2026-08-15T20:00:00+00:00")

    def test_failed_target_marks_feed_degraded(self) -> None:
        state = sample_state()
        state["https://example.test/cambridge"]["consecutive_failures"] = 1
        status = build_status(sample_config(), state)
        self.assertFalse(status["healthy"])
        self.assertEqual(status["failing_targets"], ["Cambridge"])


if __name__ == "__main__":
    unittest.main()
