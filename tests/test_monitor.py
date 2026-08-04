import sys
import unittest
from pathlib import Path
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
