"""
fetch_data.py
--------------
Downloads NSE's official daily "Bhavcopy" (end-of-day report for every
listed equity: Open, High, Low, Close, Prev Close, Volume, Trades) and
stores it in a local SQLite database (data/nse_data.db).

Data source: nseindia.com official bhavcopy files, accessed via the
open-source `nse` python package (https://pypi.org/project/nse/), which
handles NSE's session/cookie requirements for you.

Usage
-----
Fetch just one day (defaults to today):
    python fetch_data.py

Fetch a specific day:
    python fetch_data.py --date 2026-07-29

Backfill a range (e.g. to build up a full year of history for 52W high/low):
    python fetch_data.py --start 2025-08-01 --end 2026-07-30

Notes
-----
- NSE has no data on weekends/market holidays. Those dates are skipped
  with a log message, not treated as an error.
- Re-running for a date you already have is safe (it overwrites that
  date's rows rather than duplicating them).
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from nse import NSE

DB_PATH = Path(__file__).parent / "data" / "nse_data.db"
TMP_DOWNLOAD_DIR = Path(__file__).parent / "data" / "_tmp_downloads"


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_prices (
            date        TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            series      TEXT,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            prev_close  REAL,
            volume      INTEGER,
            trades      INTEGER,
            PRIMARY KEY (date, symbol, series)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbol_date ON daily_prices(symbol, date)"
    )
    conn.commit()


def _find_col(df: pd.DataFrame, *candidates: str):
    """Case-insensitive column lookup. NSE has used different column
    names in the old bhavcopy format vs. the newer UDiFF format, so we
    check both."""
    lower_map = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def normalize_bhavcopy(raw: pd.DataFrame) -> pd.DataFrame:
    """Map either the legacy or the new UDiFF bhavcopy column names onto
    a single consistent schema."""
    symbol_col = _find_col(raw, "TckrSymb", "SYMBOL")
    series_col = _find_col(raw, "SctySrs", "SERIES")
    open_col = _find_col(raw, "OpnPric", "OPEN")
    high_col = _find_col(raw, "HghPric", "HIGH")
    low_col = _find_col(raw, "LwPric", "LOW")
    close_col = _find_col(raw, "ClsPric", "CLOSE")
    prevclose_col = _find_col(raw, "PrvsClsgPric", "PREVCLOSE")
    vol_col = _find_col(raw, "TtlTradgVol", "TOTTRDQTY")
    trades_col = _find_col(raw, "TtlNbOfTxsExctd", "TOTALTRADES")

    if symbol_col is None or close_col is None:
        raise ValueError(
            f"Could not find expected columns in bhavcopy. Columns seen: {list(raw.columns)}"
        )

    out = pd.DataFrame()
    out["symbol"] = raw[symbol_col].astype(str).str.strip()
    out["series"] = raw[series_col].astype(str).str.strip() if series_col else ""
    out["open"] = pd.to_numeric(raw[open_col], errors="coerce") if open_col else None
    out["high"] = pd.to_numeric(raw[high_col], errors="coerce") if high_col else None
    out["low"] = pd.to_numeric(raw[low_col], errors="coerce") if low_col else None
    out["close"] = pd.to_numeric(raw[close_col], errors="coerce")
    out["prev_close"] = (
        pd.to_numeric(raw[prevclose_col], errors="coerce") if prevclose_col else None
    )
    out["volume"] = pd.to_numeric(raw[vol_col], errors="coerce") if vol_col else None
    out["trades"] = (
        pd.to_numeric(raw[trades_col], errors="coerce") if trades_col else None
    )
    return out


def fetch_one_day(nse: NSE, day: datetime, conn: sqlite3.Connection) -> str:
    """Returns a short status string for logging."""
    try:
        file_path = nse.equityBhavcopy(day, folder=TMP_DOWNLOAD_DIR)
    except RuntimeError as e:
        return f"SKIP {day.date()} (not available / holiday): {e}"
    except FileNotFoundError as e:
        return f"SKIP {day.date()} (download failed, likely no trading): {e}"

    raw = pd.read_csv(file_path)
    df = normalize_bhavcopy(raw)
    df.insert(0, "date", day.strftime("%Y-%m-%d"))

    conn.executemany(
        """
        INSERT OR REPLACE INTO daily_prices
            (date, symbol, series, open, high, low, close, prev_close, volume, trades)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        df[
            [
                "date",
                "symbol",
                "series",
                "open",
                "high",
                "low",
                "close",
                "prev_close",
                "volume",
                "trades",
            ]
        ].itertuples(index=False, name=None),
    )
    conn.commit()

    # clean up the downloaded raw file, we only need what's in the DB
    try:
        file_path.unlink()
    except OSError:
        pass

    return f"OK   {day.date()}: {len(df)} securities loaded"


def daterange(start: datetime, end: datetime):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description="Fetch NSE daily bhavcopy into SQLite")
    parser.add_argument("--date", help="Single date YYYY-MM-DD (default: today)")
    parser.add_argument("--start", help="Backfill start date YYYY-MM-DD")
    parser.add_argument("--end", help="Backfill end date YYYY-MM-DD")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to SQLite db file")
    parser.add_argument(
        "--sleep", type=float, default=1.0, help="Seconds to wait between requests"
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    TMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d")
    elif args.date:
        start = end = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        start = end = datetime.today()

    conn = sqlite3.connect(db_path)
    init_db(conn)

    with NSE(download_folder=TMP_DOWNLOAD_DIR) as nse:
        for day in daterange(start, end):
            if day.weekday() >= 5:  # Sat/Sun, NSE is closed
                print(f"SKIP {day.date()} (weekend)")
                continue
            status = fetch_one_day(nse, day, conn)
            print(status)
            time.sleep(args.sleep)

    conn.close()
    print(f"Done. Database at: {db_path}")


if __name__ == "__main__":
    sys.exit(main())
