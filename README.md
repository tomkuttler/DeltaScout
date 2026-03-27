# DeltaScout

DeltaScout is a Python URL monitor that stores a snapshot of each page on every run and alerts by email when content changes.

It compares normalized visible text (not raw HTML) and sends one digest email with git-style unified diffs for all changed URLs.

## Features

- Monitors a list of URLs from `urls.yaml`
- Supports JavaScript-rendered pages using Playwright (`render_js: true`)
- Saves timestamped snapshots in `.deltascout/snapshots/`
- Tracks baselines in `.deltascout/state.json`
- Writes per-run metadata in `.deltascout/runs/`
- Sends one digest email for changes and fetch errors
- Uses Gmail SMTP with app password from `.env`

## Project Files

- `deltascout.py`: main script
- `urls.yaml`: monitored URL list
- `.env`: local secrets and runtime config (not committed)
- `.env.example`: required env var template
- `requirements.txt`: Python dependency list

## Setup

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

3. Install Playwright browser binaries (required for `render_js: true` URLs):

```bash
python -m playwright install chromium
```

4. Create your local env file:

```bash
cp .env.example .env
```

5. Edit `.env`:

- `GMAIL_SMTP_USER`: sender Gmail address
- `GMAIL_APP_PASSWORD`: Gmail app password (Google account with 2FA required)
- `ALERT_EMAIL_TO`: recipient email (or comma-separated list)
- Optional: `REQUEST_TIMEOUT_SECONDS`, `USER_AGENT` (used for both static and JS fetches)

6. Edit `urls.yaml`:

```yaml
- name: Example
  url: https://example.com/
  render_js: false

- name: Job Board
  url: https://jobs.example.com/
  render_js: true
  wait_selector: ".job-list-item"
  wait_timeout_seconds: 30
```

`urls.yaml` fields:

- `name` (required): label used in reports
- `url` (required): `http`/`https` URL
- `render_js` (optional, default `false`): enable headless Chromium rendering
- `wait_selector` (optional): CSS selector to wait for before snapshotting (only valid with `render_js: true`)
- `wait_timeout_seconds` (optional, default `20`): timeout for page load/selector wait in JS mode

## Run Manually

```bash
./deltascout.py
```

or

```bash
.venv/bin/python deltascout.py
```

First run creates initial baselines and sends no email.

## Exit Codes

- `0`: run completed successfully
- `1`: configuration/runtime error before completion
- `2`: monitoring completed but email was required and failed to send

## Cron Setup

Use absolute paths in cron jobs.

### 1. Open your crontab

```bash
crontab -e
```

### 2. Add a job

Example: run every 30 minutes and append logs to a file.

```cron
*/30 * * * * /path/to/DeltaScout/.venv/bin/python /path/to/DeltaScout/deltascout.py >> /path/to/DeltaScout/cron.log 2>&1
```

Example: run every day at 08:00.

```cron
0 8 * * * /path/to/DeltaScout/.venv/bin/python /path/to/DeltaScout/deltascout.py >> /path/to/DeltaScout/cron.log 2>&1
```

### 3. Verify cron entry

```bash
crontab -l
```

### 4. Check logs

```bash
tail -f /path/to/DeltaScout/cron.log
```

## Notes

- Static mode uses direct HTTP fetches; JS mode uses Playwright headless Chromium.
- Baseline snapshots are updated for changed URLs only after a successful alert email send.
- `.deltascout/` is intentionally ignored by git.
