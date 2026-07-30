"""
fetch_corporate_actions.py
----------------------------
Fetches bonus/stock-split corporate actions for the whole market (one bulk
NSE API call, not per-symbol — cheap enough to run daily) and stores them
so app.py can back-adjust historical prices before computing 52-week
high/low, moving averages, and RSI.

WHY THIS MATTERS
-----------------
NSE's daily bhavcopy (what fetch_data.py stores) shows the ACTUAL traded
price each day — it is never adjusted for bonus issues or stock splits.
That's correct as a record of what happened, but it breaks any
across-time comparison: if a stock did a 1:1 bonus (doubling share count,
roughly halving price) 6 months ago, the pre-bonus price is numerically
much higher than anything since, and a naive "52-week high" will pick up
that stale, non-comparable number.

This script identifies those events and computes a back-adjustment
factor (matching how Yahoo Finance/Google Finance/etc. handle this) so
historical prices before the ex-date can be scaled down to be comparable
with current prices — without touching the actual recorded OHLC for any
individual day (that stays exactly as NSE reported it).

IMPORTANT — like fetch_company_info.py, the ratio-parsing here is
best-effort: it applies regex patterns to NSE's free-text action
descriptions (e.g. "Bonus 1:1", "Face Value Split (Sub-Division) - From
Rs 10/- Per Share To Rs 2/- Per Share"), which are fairly standardized
but not guaranteed. Any action whose ratio can't be confidently parsed is
stored with adjustment_factor=NULL and simply isn't applied — it's safer
to under-adjust (visible, explainable) than to guess wrong silently. Run
with --debug to see exactly what's being parsed vs skipped.

Usage
-----
    python fetch_corporate_actions.py                  # last ~400 days, whole market
    python fetch_corporate_actions.py --days 500        # wider window
    python fetch_corporate_actions.py --debug            # print every row's parse result
"""

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from nse import NSE

from fetch_data import DATA_DIR, TMP_DOWNLOAD_DIR

ACTIONS_DB_PATH = DATA_DIR / "company_info.db"  # same file as company_info/quarterly_results


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corporate_actions (
            symbol              TEXT NOT NULL,
            series              TEXT,
            ex_date             TEXT NOT NULL,
            subject             TEXT,
            adjustment_factor   REAL,
            last_updated        TEXT,
            PRIMARY KEY (symbol, ex_date, subject)
        )
        """
    )
    conn.commit()


def _find_value(row: dict, *keywords):
    for k, v in row.items():
        if any(kw in k.lower() for kw in keywords):
            return v
    return None


BONUS_RE = re.compile(r"bonus.{0,30}?(\d+)\s*[:\-]\s*(\d+)", re.IGNORECASE | re.DOTALL)
SPLIT_RE = re.compile(
    r"(?:split|sub-?division).{0,40}?r[se]\.?\s*(\d+(?:\.\d+)?)\s*.{0,20}?r[se]\.?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)


def parse_adjustment_factor(subject: str):
    """Best-effort: returns a float < 1 to multiply pre-ex-date prices by,
    or None if the subject text doesn't match a recognized bonus/split
    pattern."""
    if not subject:
        return None

    m = BONUS_RE.search(subject)
    if m:
        new_shares, old_shares = int(m.group(1)), int(m.group(2))
        if new_shares > 0 and old_shares > 0:
            return old_shares / (old_shares + new_shares)

    m = SPLIT_RE.search(subject)
    if m:
        old_fv, new_fv = float(m.group(1)), float(m.group(2))
        if old_fv > 0 and new_fv > 0 and new_fv < old_fv:
            return new_fv / old_fv

    return None


def parse_date(value):
    """NSE action dates commonly come as 'DD-Mon-YYYY'; be forgiving."""
    if not value:
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Fetch bonus/split corporate actions for back-adjusting historical prices"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=400,
        help="How many days back to fetch (default 400, comfortably covers a 52-week lookback)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print every row's parse result instead of storing"
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=args.days)

    with NSE(download_folder=TMP_DOWNLOAD_DIR) as nse:
        rows = nse.actions(segment="equities", from_date=from_date, to_date=to_date)

    print(f"Fetched {len(rows)} corporate action rows from {from_date.date()} to {to_date.date()}")

    if args.debug:
        for row in rows:
            symbol = _find_value(row, "symbol")
            ex_date_raw = _find_value(row, "exdate", "ex_date")
            subject = _find_value(row, "subject", "purpose", "comp")
            factor = parse_adjustment_factor(subject or "")
            print(f"{symbol} | {ex_date_raw} | {subject} -> factor={factor}")
        return

    conn = sqlite3.connect(ACTIONS_DB_PATH)
    init_db(conn)

    stored = 0
    parsed = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for row in rows:
        symbol = _find_value(row, "symbol")
        series = _find_value(row, "series")
        ex_date_raw = _find_value(row, "exdate", "ex_date")
        subject = _find_value(row, "subject", "purpose", "comp")
        ex_date = parse_date(ex_date_raw)

        if not symbol or not ex_date:
            continue

        factor = parse_adjustment_factor(subject or "")
        if factor is not None:
            parsed += 1

        conn.execute(
            """
            INSERT INTO corporate_actions
                (symbol, series, ex_date, subject, adjustment_factor, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, ex_date, subject) DO UPDATE SET
                adjustment_factor=excluded.adjustment_factor,
                last_updated=excluded.last_updated
            """,
            (symbol, series, ex_date, subject, factor, today),
        )
        stored += 1

    conn.commit()
    conn.close()
    print(
        f"Done. Stored {stored} action rows, {parsed} with a recognized bonus/split ratio "
        f"(the rest were dividends/other actions that don't need price adjustment, or had "
        f"unrecognized wording). Database at: {ACTIONS_DB_PATH}"
    )


if __name__ == "__main__":
    sys.exit(main())
