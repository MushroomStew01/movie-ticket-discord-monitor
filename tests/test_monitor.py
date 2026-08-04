import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
