import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import monitor


class DetectorRegressionTests(unittest.TestCase):
    def test_line_boundaries_are_preserved(self):
        text = "Movies\nThe Odyssey\nAdvance tickets available\nConcessions & Bites"
        self.assertEqual(
            monitor.normalized_lines(text),
            ["Movies", "The Odyssey", "Advance tickets available", "Concessions & Bites"],
        )

    def test_all_current_advance_ticket_phrases_match(self):
        phrases = (
            "Get Tickets",
            "Get Advance Tickets",
            "Advance tickets available",
            "Advance tickets AUG 14",
            "Buy Tickets",
            "Select Showtimes",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(monitor.TICKET_PATTERN.search(phrase))

    def test_locale_urls_are_canonicalized(self):
        self.assertEqual(
            monitor.canonicalize_url("https://www.cineplex.com/en/movie/the-odyssey?tracking=1"),
            "https://www.cineplex.com/movie/the-odyssey",
        )

    def test_theatre_text_fallback_extracts_titles_and_status(self):
        body = """
        Cineplex Cinemas Kitchener and VIP
        Get Tickets
        Movies
        Spider-Man: Brand New Day
        The Odyssey
        The End of Oak Street
        Advance tickets available
        Avengers: Doomsday
        Advance tickets AUG 4
        Menu Offers
        Tuesday - Tacos & Tequila
        """
        movies = monitor.extract_text_movies(body, {})
        by_title = {movie["title"]: movie for movie in movies.values()}
        self.assertEqual(len(by_title), 4)
        self.assertFalse(by_title["The Odyssey"]["ticket_available"])
        self.assertTrue(by_title["The End of Oak Street"]["ticket_available"])
        self.assertTrue(by_title["Avengers: Doomsday"]["ticket_available"])

    def test_direct_ticket_transition_creates_one_event(self):
        previous = {
            "ticket_available": False,
            "ticket_phrases": [],
            "relevant_lines": ["December 18, 2026"],
        }
        current = {
            "ticket_available": True,
            "ticket_phrases": ["Get Advance Tickets"],
            "relevant_lines": ["December 18, 2026", "Get Advance Tickets"],
        }
        events = monitor.compare_snapshots(
            previous,
            current,
            target_name="Avengers: Doomsday",
            priority_titles=("Avengers: Doomsday",),
            priority_formats=("IMAX", "70MM"),
        )
        self.assertEqual(len(events), 1)
        self.assertIn("Priority presale", events[0]["title"])

    def test_removal_does_not_generate_a_ticket_alert(self):
        previous = {
            "ticket_available": True,
            "ticket_phrases": ["Get Tickets"],
            "relevant_lines": ["Get Tickets"],
        }
        current = {
            "ticket_available": False,
            "ticket_phrases": [],
            "relevant_lines": [],
        }
        events = monitor.compare_snapshots(
            previous,
            current,
            target_name="Example Movie",
            priority_titles=(),
            priority_formats=(),
        )
        self.assertEqual(events, [])

    def test_showtime_values_are_normalized_and_deduplicated(self):
        self.assertEqual(
            monitor.extract_showtimes("7:00 p.m. 10:15 PM 7:00 PM"),
            ["10:15 PM", "7:00 PM"],
        )

    def test_html_fallback_removes_scripts_and_preserves_page_text(self):
        raw = """
        <html><body><script>fake Digger tickets</script>
        <main><h1>Digger</h1><p>October 2, 2026</p></main></body></html>
        """
        text = monitor.html_to_readable_text(raw)
        self.assertIn("Digger", text)
        self.assertIn("October 2, 2026", text)
        self.assertNotIn("fake Digger tickets", text)

    def test_main_text_uses_body_when_main_is_blank(self):
        class FakeLocator:
            def __init__(self, text):
                self.text = text

            def inner_text(self, timeout):
                return self.text

        class FakePage:
            url = "https://www.cineplex.com/movie/digger"

            def locator(self, selector):
                if selector == "main":
                    return FakeLocator("Digger")
                return FakeLocator(
                    "Digger\nOctober 2, 2026\nLanguage\nEnglish\nMore Info\n"
                    "Buy, share and refund tickets easily at Cineplex."
                )

        text = monitor.main_text(FakePage(), "Digger")
        self.assertIn("October 2, 2026", text)

    def test_new_priority_showtime_creates_inventory_event(self):
        previous = {
            "movies": {
                "title:dune-part-3": {
                    "title": "Dune: Part 3",
                    "showtimes": ["7:00 PM"],
                    "dates": ["Dec 18"],
                    "formats": ["IMAX"],
                }
            }
        }
        current = {
            "movies": {
                "title:dune-part-3": {
                    "title": "Dune: Part 3",
                    "showtimes": ["3:30 PM", "7:00 PM"],
                    "dates": ["Dec 18"],
                    "formats": ["IMAX"],
                }
            }
        }
        events = monitor.compare_snapshots(
            previous,
            current,
            target_name="Cineplex Cinemas Vaughan",
            target_type="theatre",
            priority_titles=("Dune: Part 3",),
            priority_formats=("IMAX", "70MM"),
        )
        self.assertEqual(len(events), 1)
        self.assertIn("showtime inventory", events[0]["title"])
        self.assertIn("3:30 PM", events[0]["description"])

    def test_new_nonpriority_showtime_also_creates_inventory_event(self):
        previous = {
            "movies": {
                "title:example-movie": {
                    "title": "Example Movie",
                    "showtimes": ["7:00 PM"],
                    "dates": ["Dec 18"],
                    "formats": [],
                }
            }
        }
        current = {
            "movies": {
                "title:example-movie": {
                    "title": "Example Movie",
                    "showtimes": ["3:30 PM", "7:00 PM"],
                    "dates": ["Dec 18"],
                    "formats": [],
                }
            }
        }
        events = monitor.compare_snapshots(
            previous,
            current,
            target_name="Cineplex Cinemas Cambridge",
            target_type="theatre",
            priority_titles=("Dune: Part 3",),
            priority_formats=("IMAX", "70MM"),
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "New showtime inventory")
        self.assertIn("3:30 PM", events[0]["description"])

    def test_theatre_text_fallback_attaches_showtimes_to_movie(self):
        body = """
        Movies
        Dune: Part 3
        IMAX 70MM
        Friday December 18
        3:30 PM
        7:00 p.m.
        Menu Offers
        """
        movies = monitor.extract_text_movies(body, {})
        dune = next(movie for movie in movies.values() if movie["title"] == "Dune: Part 3")
        self.assertEqual(dune["showtimes"], ["3:30 PM", "7:00 PM"])
        self.assertEqual(dune["formats"], ["70MM", "IMAX"])
        self.assertTrue(dune["ticket_available"])

    def test_priority_movie_added_to_theatre_is_explicit(self):
        current_movie = {
            "title": "Dune: Part 3",
            "ticket_available": True,
            "showtimes": ["7:00 PM"],
            "dates": ["Dec 18"],
            "formats": ["IMAX"],
            "url": "https://www.cineplex.com/movie/dune-part-3",
        }
        events = monitor.compare_snapshots(
            {"movies": {}},
            {"movies": {"dune": current_movie}},
            target_name="Cineplex Cinemas Kitchener and VIP",
            target_type="theatre",
            priority_titles=("Dune: Part 3",),
            priority_formats=("IMAX",),
        )
        self.assertEqual(len(events), 1)
        self.assertIn("added to this theatre", events[0]["title"])

    def test_movie_identity_is_stable_when_cineplex_link_markup_changes(self):
        movies = monitor.canonicalize_movies(
            {
                "https://www.cineplex.com/movie/dune-part-3": {
                    "title": "Dune: Part 3",
                    "ticket_available": True,
                    "showtimes": ["7:00 PM"],
                    "dates": ["Dec 18"],
                    "formats": ["IMAX"],
                    "context": "Dune: Part 3 IMAX",
                    "url": "https://www.cineplex.com/movie/dune-part-3",
                },
                "title:dune-part-3": {
                    "title": "Dune: Part 3",
                    "ticket_available": False,
                    "showtimes": ["3:30 PM"],
                    "dates": ["Dec 18"],
                    "formats": ["70MM"],
                    "context": "Dune: Part 3 IMAX 70MM",
                    "url": None,
                },
            }
        )
        self.assertEqual(list(movies), ["title:dune-part-3"])
        dune = movies["title:dune-part-3"]
        self.assertEqual(dune["showtimes"], ["3:30 PM", "7:00 PM"])
        self.assertEqual(dune["formats"], ["70MM", "IMAX"])
        self.assertTrue(dune["ticket_available"])

    def test_reappearing_inventory_gets_a_new_transition_id(self):
        event = {
            "title": "New priority showtime inventory",
            "description": "New showtime: 7:00 PM",
        }
        first = monitor.event_id_for_transition("https://example.test/dune", event, 4)
        reappeared = monitor.event_id_for_transition("https://example.test/dune", event, 6)
        self.assertNotEqual(first, reappeared)
        self.assertEqual(
            first,
            monitor.event_id_for_transition("https://example.test/dune", event, 4),
        )

    def test_daily_heartbeat_is_due_only_after_interval(self):
        now = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)
        recent = {"last_heartbeat_at_utc": (now - timedelta(hours=23)).isoformat()}
        stale = {"last_heartbeat_at_utc": (now - timedelta(hours=25)).isoformat()}
        self.assertFalse(monitor.heartbeat_due(recent, 24, now))
        self.assertTrue(monitor.heartbeat_due(stale, 24, now))
        self.assertFalse(monitor.heartbeat_due({}, 0, now))

    def test_overview_events_are_batched_with_discord_limits(self):
        events = [
            {
                "target_url": f"https://www.cineplex.com/theatre/example-{index}",
                "target_name": f"Example Theatre {index}",
                "event_id": str(index),
                "event": {
                    "title": "New movie listing detected",
                    "description": "• Example Movie",
                    "color": 0xE67E22,
                },
            }
            for index in range(11)
        ]
        batches = monitor.build_overview_batches(events)
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0][0]), 10)
        self.assertEqual(len(batches[1][0]), 1)
        self.assertEqual(sum(len(items) for _, items in batches), 11)

    def test_overview_places_priority_changes_first(self):
        normal = {
            "target_url": "https://www.cineplex.com/theatre/example",
            "target_name": "Example Theatre",
            "event_id": "normal",
            "event": {"title": "New movie", "description": "Example", "color": 0xE67E22},
        }
        priority = {
            "target_url": "https://www.cineplex.com/movie/dune-part-3",
            "target_name": "Dune: Part 3",
            "event_id": "priority",
            "event": {"title": "Priority update", "description": "Dune", "color": 0xE74C3C},
        }
        batches = monitor.build_overview_batches([normal, priority])
        self.assertEqual(batches[0][1][0]["event_id"], "priority")

    def test_overview_mentions_user_only_in_requested_batch(self):
        embeds = [{"title": "Example", "description": "Change", "color": 1}]
        with patch.object(monitor, "discord_send_payload") as send:
            monitor.discord_post_overview_batch(
                "https://discord.com/api/webhooks/example/token",
                embeds,
                user_id="123456789",
            )
        payload = send.call_args.args[1]
        self.assertIn("<@123456789>", payload["content"])
        self.assertEqual(payload["allowed_mentions"], {"users": ["123456789"]})
        self.assertEqual(payload["embeds"], embeds)

    def test_unconfigured_target_state_is_pruned(self):
        keep = "https://www.cineplex.com/theatre/keep"
        remove = "https://www.cineplex.com/movie/remove"
        state = {keep: {"snapshot": {}}, remove: {"snapshot": {}}, "_meta": {"ok": True}}
        removed = monitor.prune_unconfigured_state(state, {keep})
        self.assertEqual(removed, [remove])
        self.assertIn(keep, state)
        self.assertIn("_meta", state)
        self.assertNotIn(remove, state)

    def test_configuration_is_theatre_first_and_dune_only(self):
        config = monitor.load_json(monitor.CONFIG_PATH, {})
        _, targets = monitor.parse_targets(config)
        names = {target.name for target in targets}
        self.assertEqual(len(targets), 7)
        self.assertNotIn("Cineplex Cinemas Hamilton Mountain", names)
        self.assertFalse(any("Odyssey" in name for name in names))
        self.assertFalse(any("Doomsday" in name for name in names))
        self.assertEqual(
            {name for name in names if name.startswith("Dune: Part 3")},
            {"Dune: Part 3", "Dune: Part 3 — IMAX 70MM"},
        )

    def test_run_consolidates_change_events_into_one_overview(self):
        target = monitor.Target(
            "Cineplex Cinemas Cambridge",
            "theatre",
            "https://www.cineplex.com/theatre/cineplex-cinemas-cambridge",
            (),
            1,
        )
        previous_snapshot = {"movies": {}}
        current_snapshot = {
            "movies": {
                "title:example-movie": {
                    "title": "Example Movie",
                    "ticket_available": True,
                    "showtimes": [],
                    "dates": [],
                    "formats": [],
                    "url": None,
                }
            }
        }
        config = {
            "settings": {
                "send_baseline_summary": False,
                "send_error_alerts": False,
                "heartbeat_interval_hours": 0,
            },
            "priority_titles": ["Dune: Part 3"],
            "priority_formats": ["IMAX", "70MM"],
            "targets": [
                {
                    "name": target.name,
                    "type": target.type,
                    "url": target.url,
                    "min_movies": 1,
                }
            ],
        }
        state = {target.url: monitor.make_entry(target, previous_snapshot)}

        class DummyContext:
            def close(self):
                return None

        class DummyBrowser:
            def close(self):
                return None

        class DummyPlaywright:
            chromium = SimpleNamespace(launch=lambda headless: DummyBrowser())

        class DummyManager:
            def __enter__(self):
                return DummyPlaywright()

            def __exit__(self, exc_type, exc, traceback):
                return False

        def fake_load(path, default):
            if path == monitor.CONFIG_PATH:
                return config
            if path == monitor.STATE_PATH:
                return state
            return default

        with (
            patch.object(monitor, "get_environment", return_value=("webhook", "123456789")),
            patch.object(monitor, "load_json", side_effect=fake_load),
            patch.object(monitor, "sync_playwright", return_value=DummyManager()),
            patch.object(monitor, "make_context", return_value=DummyContext()),
            patch.object(monitor, "collect_target_with_retry", return_value=current_snapshot),
            patch.object(monitor, "save_json"),
            patch.object(monitor, "discord_post_overview_batch") as overview,
            patch.object(monitor, "discord_post") as individual,
        ):
            result = monitor.run_monitor()

        self.assertEqual(result, 0)
        overview.assert_called_once()
        individual.assert_not_called()

    def test_target_collection_retries_transient_render_failure(self):
        class FakePage:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

        context = FakeContext()
        target = monitor.Target("Digger", "movie", "https://www.cineplex.com/movie/digger", (), 0)
        with (
            patch.object(monitor, "open_target"),
            patch.object(
                monitor,
                "collect_snapshot",
                side_effect=[RuntimeError("blank"), RuntimeError("blank"), {"page_title": "Digger"}],
            ),
            patch.object(monitor.time, "sleep"),
        ):
            snapshot = monitor.collect_target_with_retry(
                context,
                target,
                wait_ms=0,
                timeout_seconds=1,
                scroll_passes=1,
                attempts=3,
                retry_wait_ms=0,
            )
        self.assertEqual(snapshot["page_title"], "Digger")
        self.assertEqual(len(context.pages), 3)
        self.assertTrue(all(page.closed for page in context.pages))


if __name__ == "__main__":
    unittest.main()
