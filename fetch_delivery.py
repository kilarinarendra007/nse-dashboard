"""
fetch_delivery.py
------------------
Downloads NSE's official daily "Full Bhavcopy and Security Deliverable Data"
report (sec_bhavdata_full) and stores Delivery Quantity, Delivery %, and
Average Traded Price (= VWAP) for every listed equity, per day.

This is a long-standing, stable NSE report format (unaffected by the 2024
UDiFF migration that changed the main bhavcopy), so its column names are
consistent going back years.

Usage
-----
    python fetch_delivery.py                              # today
    python fetch_delivery.py --date 2026-07-29             # one day
    python fetch_delivery.py --start 2025-08-01 --end 2026-07-30   # backfill
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
from nse import NSE

from fetch_data import DATA_DIR, TMP_DOWNLOAD_DIR, db_path_for, daterange


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_data (
            date        TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            series      TEXT,
            deliv_qty   INTEGER,
            deliv_per   REAL,
            avg_price   REAL,
            PRIMARY KEY (date, symbol, series)
        )
        """
    )
    conn.commit()


def _find_col(df: pd.DataFrame, *candidates: str):
    # this report is known to have leading/trailing spaces in header names
    lower_map = {c.strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def normalize_delivery(raw: pd.DataFrame) -> pd.DataFrame:
    raw.columns = [c.strip() for c in raw.columns]

    symbol_col = _find_col(raw, "SYMBOL", "TckrSymb")
    series_col = _find_col(raw, "SERIES", "SctySrs")
    deliv_qty_col = _find_col(raw, "DELIV_QTY", "DelivQty")
    deliv_per_col = _find_col(raw, "DELIV_PER", "DelivPer")
    avg_price_col = _find_col(raw, "AVG_PRICE", "AvgPric")

    if symbol_col is None:
        raise ValueError(
            f"Could not find SYMBOL column in delivery report. Columns seen: {list(raw.columns)}"
        )

    out = pd.DataFrame()
    out["symbol"] = raw[symbol_col].astype(str).str.strip()
    out["series"] = raw[series_col].astype(str).str.strip() if series_col else ""
    out["deliv_qty"] = (
        pd.to_numeric(raw[deliv_qty_col], errors="coerce") if deliv_qty_col else None
    )
    # DELIV_PER is sometimes the literal string "-" for non-EQ series; coerce handles that
    out["deliv_per"] = (
        pd.to_numeric(raw[deliv_per_col], errors="coerce") if deliv_per_col else None
    )
    out["avg_price"] = (
        pd.to_numeric(raw[avg_price_col], errors="coerce") if avg_price_col else None
    )
    return out


def fetch_one_day(nse: NSE, day: datetime, conn: sqlite3.Connection) -> str:
    try:
        file_path = nse.deliveryBhavcopy(day, folder=TMP_DOWNLOAD_DIR)
    except RuntimeError as e:
        return f"SKIP {day.date()} (not available / holiday): {e}"
    except FileNotFoundError as e:
        return f"SKIP {day.date()} (download failed, likely no trading): {e}"

    raw = pd.read_csv(file_path)
    df = normalize_delivery(raw)
    df.insert(0, "date", day.strftime("%Y-%m-%d"))

    conn.executemany(
        """
        INSERT OR REPLACE INTO delivery_data
            (date, symbol, series, deliv_qty, deliv_per, avg_price)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        df[["date", "symbol", "series", "deliv_qty", "deliv_per", "avg_price"]]
        .itertuples(index=False, name=None),
    )
    conn.commit()

    try:
        file_path.unlink()
    except OSError:
        pass

    return f"OK   {day.date()}: {len(df)} securities loaded"


def main():
    parser = argparse.ArgumentParser(
        description="Fetch NSE daily delivery %% / VWAP into SQLite"
    )
    parser.add_argument("--date", help="Single date YYYY-MM-DD (default: today)")
    parser.add_argument("--start", help="Backfill start date YYYY-MM-DD")
    parser.add_argument("--end", help="Backfill end date YYYY-MM-DD")
    parser.add_argument(
        "--sleep", type=float, default=1.0, help="Seconds to wait between requests"
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d")
    elif args.date:
        start = end = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        start = end = datetime.today()

    open_conns = {}

    def get_conn(day: datetime) -> sqlite3.Connection:
        path = db_path_for(day)
        if path not in open_conns:
            c = sqlite3.connect(path)
            init_db(c)
            open_conns[path] = c
        return open_conns[path]

    with NSE(download_folder=TMP_DOWNLOAD_DIR) as nse:
        for day in daterange(start, end):
            if day.weekday() >= 5:
                print(f"SKIP {day.date()} (weekend)")
                continue
            conn = get_conn(day)
            status = fetch_one_day(nse, day, conn)
            print(status)
            time.sleep(args.sleep)

    for c in open_conns.values():
        c.close()
    print(f"Done. {len(open_conns)} quarterly database file(s) updated in {DATA_DIR}")


if __name__ == "__main__":
    sys.exit(main())
