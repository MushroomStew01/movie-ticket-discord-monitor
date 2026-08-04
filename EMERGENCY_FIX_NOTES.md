# Movie Ticket Monitor — Emergency Fix

This package is a direct replacement for the current emergency-sensitive files in
`MushroomStew01/movie-ticket-discord-monitor`.

## Replace these files

- `monitor.py`
- `targets.json`
- `requirements.txt`
- `.github/workflows/monitor.yml`

Also add:

- `tests/test_monitor.py`

Do **not** delete or reset `state.json`. The new monitor detects the old snapshot
schema and migrates each existing target without treating every currently visible
movie as a new listing.

## What this fixes immediately

1. Preserves page line breaks so ticket, date and showtime text is actually checked.
2. Detects `Advance tickets AUG 14` as well as `Get Advance Tickets` and
   `Advance tickets available`.
3. Parses the visible Cineplex `Movies` section when theatre cards do not contain
   normal `/movie/` links.
4. Validates HTTP status, Cineplex hostname, page heading and minimum movie counts.
5. Preserves the last known-good snapshot when a page fails or parses incorrectly.
6. Fails GitHub Actions when even one target or notification delivery fails.
7. Retries Discord rate limits, timeouts and server failures.
8. Deduplicates delivered events in persistent state.
9. Alerts when a newly added target already has tickets available.
10. Stops changing `state.json` when nothing meaningful changed.
11. Adds Hamilton Mountain, The Odyssey, Odyssey IMAX 70MM and Dune IMAX 70MM.
12. Expands lazy-loaded listings and clicks `Show more` when available.
13. Retries each Cineplex target up to three times before reporting a partial failure.
14. Tracks new dates, showtimes and premium formats for priority movies.
15. Explicitly alerts when Dune 3 or another priority movie appears at a newly monitored theatre.

## First deployment

1. Upload the replacement files and the `tests` folder.
2. Commit the changes.
3. Open **Actions → Movie Ticket Monitor → Run workflow**.
4. The manual run will send a blue Discord connection message first.
5. Existing targets will migrate silently.
6. Newly added targets that already show tickets may produce one red consolidated
   availability message. This is intentional.
7. Confirm the action ends green and the log says all configured targets succeeded.

## Important limitation

GitHub scheduled workflows are best-effort. The revised schedule avoids the first
few minutes of each hour, but GitHub can still delay or drop scheduled runs. This
patch fixes the detector and makes failures visible; it cannot make GitHub Actions
an exact ten-minute scheduler.

For genuinely time-critical presales, run the same package from the existing
Windows Scheduled Task as the primary monitor and keep GitHub Actions as the backup.
Do not run two independent copies unless they share the same current `state.json`,
or both copies may send the same alert.
