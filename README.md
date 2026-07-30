# NSE Dashboard

A free, self-updating dashboard of all NSE-listed stocks: daily Open/High/Low/Close
and rolling 52-week High/Low. Built to be extended later.

## How it works
- `fetch_data.py` downloads NSE's official daily "bhavcopy" report and stores it
  in `data/nse_data.db` (SQLite — just a file, no external database needed).
- `.github/workflows/daily_fetch.yml` runs that script automatically every
  weekday evening and commits the updated database back to this repo, for free,
  using GitHub Actions.
- `app.py` is a Streamlit dashboard that reads `data/nse_data.db` and displays it,
  computing 52-week high/low on the fly from stored history.

## One-time setup (10 minutes)

### 1. Put this project on GitHub
Create a new repository on GitHub (public or private, either works) and upload
all these files to it, keeping the folder structure as-is.

### 2. Turn on the automatic daily fetch
GitHub Actions is already configured (`.github/workflows/daily_fetch.yml`) and
needs no extra setup — it will start running automatically once the repo exists,
on weekday evenings.

To load **history** (needed for 52-week high/low to mean anything), trigger it once
manually with a backfill:
1. Go to the **Actions** tab in your GitHub repo
2. Click **"Daily NSE data fetch"** in the left sidebar
3. Click **"Run workflow"**
4. Fill in `start_date` and `end_date`, e.g. `2025-08-01` to today
5. Click **Run workflow**

This will take a few minutes (it fetches one day at a time to be polite to NSE's
servers). You can check progress under the Actions tab.

### 3. Deploy the dashboard (free)
1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with your
   GitHub account
2. Click **"New app"**
3. Pick your repository, branch `main`, and main file path `app.py`
4. Click **Deploy**

That's it — you'll get a public URL for your dashboard. Every time the GitHub
Action updates `data/nse_data.db`, the Streamlit app picks up the new data
automatically (it refreshes every 10 minutes, or on redeploy).

## Running locally (optional)
```bash
pip install -r requirements.txt
python fetch_data.py --start 2025-08-01 --end 2026-07-30   # one-time backfill
streamlit run app.py
```

## Adding more data later
The database is a plain SQLite file with one table, `daily_prices`. To add new
data (indices, F&O, delivery %, sector/fundamental info, etc.):
1. Write a new fetch function (following the pattern in `fetch_data.py`) that
   writes to a new table
2. Add a new section to `app.py` to display it
3. Optionally add a step to the GitHub Action to run your new fetch function

This structure is intentionally simple so extending it is additive, not a rewrite.
