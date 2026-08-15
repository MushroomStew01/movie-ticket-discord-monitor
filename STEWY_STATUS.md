# Stewy OS status feed

`status.py` converts the monitor's existing `state.json` into a compact, public
`status.json` designed for Stewy OS and other read-only consumers.

The feed contains monitor health, configured theatre health, the configured
priority titles, and aggregated ticket/showtime/date/format inventory. It does
not duplicate scraping logic and it contains no Discord secrets.

The scheduled monitor workflow regenerates the feed after every run and commits
it only when the meaningful status changes. The `updated_at` timestamp is
preserved when the state fingerprint is unchanged, preventing a Git commit every
10 minutes just to advance a clock.
