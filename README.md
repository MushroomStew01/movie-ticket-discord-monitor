# Movie Ticket Discord Monitor

## Theatre-first notification design

- Checks seven pages instead of the previous 31: four theatres, national discovery, and two Dune: Part 3 editions.
- Sends new movie, ticket, date, format, and showtime changes in one Discord overview per run.
- Automatically discovers movies from theatre and national listing pages instead of maintaining a large movie watchlist.
- Stable title-based movie identities prevent false “new movie” alerts when Cineplex changes links or card markup.
- A showtime that disappears and later returns can alert again; delivery deduplication still protects retries of the same transition.
- Failed targets preserve HTML, screenshot, and metadata diagnostics for the GitHub Actions run.
- A blue Discord health heartbeat is sent every 24 hours after a successful run.
- Parser tests run in a separate CI workflow instead of slowing every scheduled production check.
- Dune: Part 3 and its IMAX 70MM page silently baseline their already-available ticket status.

Replacing `state.json` with `{}` intentionally creates a fresh baseline. Existing availability is summarized, except targets configured with `"alert_available_on_first_seen": false`.

This project monitors selected Cineplex theatre and movie pages and posts alerts to a private Discord channel through a Discord incoming webhook.

It currently includes:

- Cineplex Cinemas Kitchener and VIP
- Cineplex Cinemas Cambridge
- Cineplex Cinemas Vaughan
- Cineplex Cinemas Mississauga Square One
- Cineplex national movie discovery
- Dune: Part 3 and Dune: Part 3 IMAX 70MM direct pages
- Detection of newly added movie links
- Detection when ticket wording appears
- Detection of new dates, formats, showtimes, and ticket-related text for every discovered movie
- Consolidated Discord change overviews grouped into one message whenever Discord limits allow
- Optional direct Discord mention using your Discord User ID

Important: Cineplex theatre and movie landing pages do not always expose individual session times. The monitor alerts on exact showtime/date/format additions whenever those values are present in readable page content, but it does not claim a showtime that the page did not expose. It never purchases, reserves, or holds seats.

## 1. Create a private Discord server

1. Open Discord.
2. Select the plus sign beside your server list.
3. Select **Create My Own**.
4. Select **For me and my friends** or skip the template choice.
5. Name the server `Movie Ticket Alerts`.
6. Create a text channel named `ticket-alerts`.

## 2. Create the Discord webhook

Desktop or browser is easiest.

1. Open your `Movie Ticket Alerts` server.
2. Click the server name at the upper left.
3. Select **Server Settings**.
4. Select **Integrations**.
5. Select **Webhooks**.
6. Select **New Webhook**.
7. Set the name to `Movie Ticket Monitor`.
8. Choose the `#ticket-alerts` channel.
9. Select **Copy Webhook URL**.

Depending on the Discord layout, you can also right-click `#ticket-alerts`, select **Edit Channel**, then **Integrations** and **Webhooks**.

The URL resembles this, but your numbers and token will be different:

```text
https://discord.com/api/webhooks/123456789012345678/a_long_private_token_created_by_discord
```

Never post the real URL publicly. Anyone with the complete URL can post messages into that channel. If it leaks, return to **Server Settings > Integrations > Webhooks**, delete the webhook, and create a new one.

## 3. Optional: obtain your Discord User ID

Using your User ID makes alerts mention you directly.

Desktop:

1. Open **User Settings** using the cogwheel.
2. Open **Advanced**.
3. Enable **Developer Mode**.
4. Find your own name in the Discord server member list.
5. Right-click your name and select **Copy User ID**.

Mobile:

1. Tap your avatar.
2. Tap the settings cogwheel.
3. Open **Advanced**.
4. Enable **Developer Mode**.
5. Open your profile in the server.
6. Tap the three dots and select **Copy User ID**.

The ID contains digits only, for example:

```text
123456789012345678
```

## 4. Cloud setup using GitHub Actions

GitHub scheduled workflows can run as often as every ten minutes, but GitHub warns scheduled jobs can sometimes be delayed during high load.

For a ten-minute monitor, use a **public repository** if you want standard GitHub-hosted Actions to be free. The webhook remains in GitHub Actions Secrets and is not placed in the files. A private repository consumes included Actions minutes and a ten-minute browser workflow can exceed the included monthly quota.

### Create the repository

1. Sign in at github.com.
2. Select **New repository**.
3. Repository name: `movie-ticket-discord-monitor`.
4. Choose **Public** for free standard Actions usage.
5. Do not add a README, .gitignore, or license because this project already contains those files.
6. Select **Create repository**.

### Upload the files

1. On the empty repository page, select **uploading an existing file**.
2. Upload all extracted project files and folders.
3. Make sure `.github/workflows/monitor.yml` is included.
4. Commit the files.

If the web uploader does not preserve the `.github/workflows` folder correctly, create the workflow manually:

1. Open the repository **Actions** tab.
2. Select **set up a workflow yourself**.
3. Replace the editor contents with the contents of `.github/workflows/monitor.yml`.
4. Commit the workflow.

### Add the webhook secret

1. Open the GitHub repository.
2. Select **Settings**.
3. In the sidebar, select **Secrets and variables**.
4. Select **Actions**.
5. Select **New repository secret**.
6. Name:

```text
DISCORD_WEBHOOK_URL
```

7. Value: paste the complete Discord webhook URL.
8. Select **Add secret**.

Optional direct mention secret:

1. Select **New repository secret** again.
2. Name:

```text
DISCORD_USER_ID
```

3. Value: paste your digits-only Discord User ID.
4. Select **Add secret**.

### Enable and test the workflow

1. Open the repository **Actions** tab.
2. Select **Movie Ticket Monitor**.
3. Select **Run workflow**.
4. Select the green **Run workflow** button.
5. Open the workflow run and inspect each step.

For a webhook-only test, start a manual workflow run and enable the **Send a Discord connection test before monitoring** checkbox. No workflow file edit is required.

## 5. Enable Discord phone notifications

1. Long-press the `#ticket-alerts` channel on mobile, or right-click it on desktop.
2. Open **Notification Settings**.
3. Select **All Messages**.
4. Ensure the server is not muted.
5. Ensure **Mobile Push Notifications** are enabled for the server.
6. In iPhone or Android system settings, allow Discord notifications, sounds, and lock-screen banners.

Using `DISCORD_USER_ID` is recommended because each alert directly mentions you.

## 6. Change monitored theatres

Edit `targets.json`. General movies are discovered automatically from the theatre and national listing pages, so they do not need individual targets. The only permanent movie targets are the two Dune: Part 3 editions.

Theatre example:

```json
{
  "name": "Galaxy Cinemas Waterloo",
  "type": "theatre",
  "url": "https://www.cineplex.com/theatre/galaxy-cinemas-waterloo",
  "watch_keywords": [
    "advance tickets available",
    "get tickets",
    "imax",
    "70mm",
    "ultraavx"
  ]
}
```

JSON rules:

- Every object except the last needs a comma after it.
- Text must use double quotation marks.
- Do not place comments inside the JSON file.
- Validate the file with a JSON validator if the monitor reports an invalid JSON error.

## 7. Reset the baseline

To treat the current pages as new baselines, replace `state.json` with:

```json
{}
```

Run the monitor once. It will rebuild the baseline without reporting all current listings.

## 8. Troubleshooting

### Discord returns 401 Unauthorized or 404 Not Found

The webhook URL is incorrect, incomplete, deleted, or regenerated. Copy it again from Discord and replace the stored value.

### Discord receives no phone notification

- Set the channel to **All Messages**.
- Confirm the server is not muted.
- Confirm operating-system notifications are allowed.
- Add your Discord User ID so the monitor directly mentions you.

### Playwright browser is missing

The GitHub Actions workflow installs Chromium automatically. Open the failed workflow run and inspect the **Install Playwright Chromium** step.

### GitHub workflow does not run every exact ten minutes

Scheduled GitHub Actions are best-effort and can occasionally be delayed. The workflow uses off-hour cron minutes to reduce congestion, but it cannot guarantee an exact interval.

### Too many false alerts

Cineplex can change text or page structure. Remove broad keywords from `targets.json`, or reduce the target list. The monitor already ignores ordinary page changes unless it finds new movie links, ticket availability, showtime-like text, or ticket-format wording.

### No alerts after setup

That is normal on the first run because it creates a baseline. Use `--test-alert` to test Discord. Then wait for an actual page change, or temporarily clear one target from `state.json` and make a controlled test.

## 9. Security rules

- Never commit the real webhook URL to GitHub.
- Never post a screenshot showing the complete webhook URL.
- Do not send the URL to anyone.
- If exposed, delete the webhook and create a new one.
- Keep automated checking moderate.
- Do not automate purchasing, seat holding, login bypasses, CAPTCHAs, or other access-control circumvention.

## Official references

- Discord webhooks: https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks
- Discord notification settings: https://support.discord.com/hc/en-us/articles/215253258-Notifications-Settings-101
- Discord User IDs: https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID
- GitHub Actions secrets: https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets
- GitHub scheduled workflows: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- GitHub Actions billing: https://docs.github.com/billing/managing-billing-for-github-actions/about-billing-for-github-actions
- Playwright Python installation: https://playwright.dev/python/docs/library
