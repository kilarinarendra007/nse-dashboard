# NSE Dashboard

A free, self-updating dashboard of all NSE-listed stocks: daily Open/High/Low/Close,
rolling 52-week High/Low, Delivery %, VWAP, sector classification, market cap,
moving averages/RSI, and a small screener.

## How it works

| Script | What it fetches | Runs |
|---|---|---|
| `fetch_data.py` | Daily Open/High/Low/Close, volume, trades | Every weekday evening (GitHub Action) |
| `fetch_delivery.py` | Delivery quantity/%, VWAP | Every weekday evening (same Action) |
| `fetch_corporate_actions.py` | Bonus/split events (for back-adjusting historical prices) | Every weekday evening (same Action) |
| `fetch_company_info.py` | Sector/industry/market cap basis (NSE) + quarterly revenue/net profit (**BSE**) | Once a month (separate Action — slow, this data changes rarely) |

**Why BSE for quarterly results, not NSE:** NSE's own results endpoint was found to
serve stale/frozen data for some symbols — e.g. showing a company's latest quarter
as over a year old despite them having clearly reported more recently. BSE (a
completely separate data pipeline) was verified to show genuinely current
quarters, so it's used as the primary source, with NSE as a fallback if BSE has
no data for a given symbol. Any quarter more than ~200 days old still gets a ⚠️
in the dashboard regardless of source, since staleness can in principle recur.

Prices and delivery data are stored as one SQLite file per **quarter**
(`data/nse_data_2026Q1.db`, etc.) to stay under GitHub's 100MB per-file limit.
Company info is stored in a single small file, `data/company_info.db`.

`app.py` is a Streamlit dashboard that loads all of the above, merges them, and
computes 52-week high/low, moving averages, and RSI live from stored history.

## One-time setup (10 minutes)

### 1. Put this project on GitHub
Upload all these files to a GitHub repo, keeping the folder structure as-is
(including the hidden `.github` folder).

### 2. Backfill daily price + delivery history
1. Go to the **Actions** tab → **"Daily NSE data fetch"** → **Run workflow**
2. Fill in `start_date` / `end_date` (e.g. a year back to today)
3. Run it — takes a while since it fetches one day at a time

### 3. Populate sector / market cap (optional but recommended)
1. Go to **Actions** → **"Monthly sector/market-cap enrichment"** → **Run workflow**
2. Leave `index` blank for the default (NIFTY 500 constituents — covers the vast
   majority of actively traded stocks; smaller/illiquid stocks won't have a
   sector tag unless you widen this later)
3. This is slower than the price fetch (one request per stock) — expect it to
   take a while for ~500 symbols

**Note:** this script's field-lookup for sector/shares-outstanding is
best-effort, since NSE doesn't officially document that response's exact
shape. If the run reports "0 symbols had usable sector/shares data", run
locally:
```bash
python fetch_company_info.py --debug RELIANCE
```
and share the printed output — the field lookup can be corrected from that.

### 4. Deploy the dashboard (free)
1. [share.streamlit.io](https://share.streamlit.io) → sign in with GitHub
2. **Create app** → pick your repo, branch `main`, main file `app.py`
3. Deploy

## Running locally (optional)
```bash
pip install -r requirements.txt
python fetch_data.py --start 2025-08-01 --end 2026-07-30
python fetch_delivery.py --start 2025-08-01 --end 2026-07-30
python fetch_company_info.py
streamlit run app.py
```

## What's in the dashboard
- **Main table**: every stock, Open/High/Low/Close, % change, 52W high/low,
  Delivery %, VWAP, SMA20/50/200, RSI(14), sector, estimated market cap
- **Filters**: series, sector, specific symbols, as-of date
- **Screener**: Top Gainers/Losers, Near 52W High/Low, High Delivery %,
  Oversold/Overbought (RSI)
- **Symbol drill-down**: price chart with moving averages + a separate RSI panel

## Caveats worth knowing
- RSI is a simple (non-Wilder-smoothed) 14-period calculation — good enough for
  screening, but won't exactly match every charting platform's number.
- Market Cap = shares outstanding × close price — an estimate, not
  exchange-verified.
- Sector coverage depends on step 3 above and defaults to NIFTY 500 — stocks
  outside that won't show a sector until you widen `--index` or run it against
  a broader list.
- 52-week High/Low, moving averages, and RSI are back-adjusted for bonus/split
  corporate actions (`fetch_corporate_actions.py`), so a stock that did a
  bonus/split in the last year won't show a stale, non-comparable pre-action
  price as its "high." The main table's actual daily Open/High/Low/Close is
  never adjusted — that always reflects the real price on that specific day.
  This adjustment is best-effort text parsing of NSE's action descriptions;
  anything it can't confidently parse is simply left unadjusted rather than
  guessed at (run `python fetch_corporate_actions.py --debug` to see what
  parsed vs. didn't).

## Adding more data later
Each data source follows the same pattern: a small fetch script writing to its
own table, plus a loader function in `app.py` that gets merged into the main
table. To add something new (F&O data, index membership, corporate actions,
etc.), follow that same shape — it's additive, not a rewrite.
