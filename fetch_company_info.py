"""
fetch_company_info.py
----------------------
Populates, for NIFTY 500 constituents, stored in a single small database
(data/company_info.db — not split by quarter, this data changes rarely):
  - sector/industry classification and share count (for market cap)
  - the last ~5 quarters of revenue/net profit (for a Positive/Negative
    results trend), via NSE's results_comparison() endpoint

Unlike the bhavcopy/delivery reports (one bulk file per day), NSE doesn't
publish this data in bulk for free. The only way to get it is one API
call per stock, which is slow and best run occasionally (monthly), not
daily.

To keep runtime reasonable and avoid hammering NSE's servers, this
defaults to NIFTY 500 constituents (covers the vast majority of actively
traded stocks) rather than all ~3400 listed securities. You can widen
this later — see --index below.

IMPORTANT — the sector/industry/shares-outstanding lookup is best-effort:
NSE's per-symbol response format isn't officially documented, so this
script searches the response generically for fields that look right
rather than assuming an exact structure. If your first run reports lots
of "not found", paste the debug output back and we'll refine the field
lookup together. The results_comparison() field names, by contrast, are
documented by the library and used as-is.

Usage
-----
    python fetch_company_info.py                        # NIFTY 500, default
    python fetch_company_info.py --index "NIFTY 100"     # smaller/faster run
    python fetch_company_info.py --skip-results          # sector/market cap only, faster
    python fetch_company_info.py --debug AAPL             # dump raw getDetailedScripData for one symbol
    python fetch_company_info.py --debug-results AAPL     # dump raw results_comparison for one symbol
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from nse import NSE

from fetch_data import DATA_DIR, TMP_DOWNLOAD_DIR

COMPANY_DB_PATH = DATA_DIR / "company_info.db"


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_info (
            symbol              TEXT PRIMARY KEY,
            company_name        TEXT,
            sector              TEXT,
            industry            TEXT,
            shares_outstanding  REAL,
            face_value          REAL,
            last_updated        TEXT
        )
        """
    )
    # migration for a company_info.db created before company_name existed
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(company_info)")}
    if "company_name" not in existing_cols:
        conn.execute("ALTER TABLE company_info ADD COLUMN company_name TEXT")
    conn.commit()


def init_results_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quarterly_results (
            symbol            TEXT NOT NULL,
            quarter_end       TEXT NOT NULL,
            revenue_lakhs     REAL,
            net_profit_lakhs  REAL,
            last_updated      TEXT,
            PRIMARY KEY (symbol, quarter_end)
        )
        """
    )
    conn.commit()


def _find_all(obj, keywords, path=""):
    """Recursively search a nested dict/list and yield EVERY (path, value)
    where the key name contains any of `keywords` (case-insensitive
    substring match) and the value is a plain scalar. Yields every match
    in the tree rather than stopping at the first — some NSE responses
    have more than one field whose name matches a keyword, and the first
    one found isn't always the useful one (e.g. a 'sector' key that's
    really an internal index-name lookup)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_lower = k.lower()
            if any(kw in k_lower for kw in keywords) and not isinstance(v, (dict, list)):
                yield f"{path}.{k}", v
        for k, v in obj.items():
            yield from _find_all(v, keywords, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _find_all(item, keywords, f"{path}[{i}]")


def _looks_like_junk(value) -> bool:
    """Reject values that look like an internal index-name lookup
    ('NIFTY AUTO              '), an internal numeric ID, a boolean flag,
    or anything else that isn't a genuine text classification."""
    if value is None:
        return True
    if isinstance(value, bool):
        return True
    s = str(value).strip()
    if not s or s == "-":
        return True
    if "NIFTY" in s.upper():
        return True
    try:
        float(s)
        return True  # purely numeric — a real sector/industry name is always text
    except ValueError:
        pass
    return False


def _first_clean_match(raw: dict, keywords) -> str | None:
    for _, value in _find_all(raw, keywords):
        if not _looks_like_junk(value):
            return str(value).strip()
    return None


def extract_company_fields(raw: dict) -> dict:
    company_name = _first_clean_match(raw, ["companyname"])
    sector = _first_clean_match(raw, ["sector", "macro"])
    industry = _first_clean_match(raw, ["industry", "basicindustry"])
    _, shares = next(
        iter(_find_all(raw, ["issuedsize", "sharesoutstanding", "issuedcap"])), (None, None)
    )
    _, face_value = next(iter(_find_all(raw, ["facevalue"])), (None, None))
    return {
        "company_name": company_name,
        "sector": sector,
        "industry": industry,
        "shares_outstanding": shares,
        "face_value": face_value,
    }


def get_universe(nse: NSE, index_name: str) -> list:
    result = nse.listEquityStocksByIndex(index_name)
    rows = result.get("data", []) if isinstance(result, dict) else []
    symbols = []
    for row in rows:
        sym = row.get("symbol")
        if sym and sym != index_name:
            symbols.append(sym)
    return symbols


def fetch_quarterly_results(nse: NSE, symbol: str, conn: sqlite3.Connection) -> int:
    """Store the last ~5 quarters of revenue/net profit for `symbol`.
    Returns the number of quarter rows stored."""
    data = nse.results_comparison(symbol)
    rows = data.get("resCmpData", []) if isinstance(data, dict) else []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    for row in rows:
        quarter_end = row.get("re_to_dt")
        if not quarter_end:
            continue
        conn.execute(
            """
            INSERT INTO quarterly_results
                (symbol, quarter_end, revenue_lakhs, net_profit_lakhs, last_updated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol, quarter_end) DO UPDATE SET
                revenue_lakhs=excluded.revenue_lakhs,
                net_profit_lakhs=excluded.net_profit_lakhs,
                last_updated=excluded.last_updated
            """,
            (symbol, quarter_end, row.get("re_total_inc"), row.get("re_net_profit"), today),
        )
        count += 1
    conn.commit()
    return count


def main():
    parser = argparse.ArgumentParser(
        description="Fetch sector/industry/shares-outstanding per symbol (slow, run occasionally)"
    )
    parser.add_argument(
        "--index",
        default="NIFTY 500",
        help="Which index's constituents to enrich (default: NIFTY 500)",
    )
    parser.add_argument(
        "--sleep", type=float, default=1.0, help="Seconds to wait between requests"
    )
    parser.add_argument(
        "--debug",
        metavar="SYMBOL",
        help="Print the raw NSE response for one symbol and exit (for troubleshooting field names)",
    )
    parser.add_argument(
        "--debug-results",
        metavar="SYMBOL",
        help="Print the raw results_comparison() response for one symbol and exit",
    )
    parser.add_argument(
        "--skip-results",
        action="store_true",
        help="Skip the quarterly results fetch (sector/market cap only, faster)",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with NSE(download_folder=TMP_DOWNLOAD_DIR) as nse:
        if args.debug:
            import json

            raw = nse.getDetailedScripData(args.debug)
            print(json.dumps(raw, indent=2)[:5000])
            return

        if args.debug_results:
            import json

            raw = nse.results_comparison(args.debug_results)
            print(json.dumps(raw, indent=2)[:5000])
            return

        symbols = get_universe(nse, args.index)
        print(f"Found {len(symbols)} symbols in {args.index}")

        conn = sqlite3.connect(COMPANY_DB_PATH)
        init_db(conn)
        init_results_db(conn)

        found_count = 0
        results_count = 0
        for i, symbol in enumerate(symbols, 1):
            try:
                raw = nse.getDetailedScripData(symbol)
                fields = extract_company_fields(raw)
                if fields["sector"] or fields["shares_outstanding"] or fields["company_name"]:
                    found_count += 1
                conn.execute(
                    """
                    INSERT INTO company_info
                        (symbol, company_name, sector, industry, shares_outstanding, face_value, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        company_name=excluded.company_name,
                        sector=excluded.sector,
                        industry=excluded.industry,
                        shares_outstanding=excluded.shares_outstanding,
                        face_value=excluded.face_value,
                        last_updated=excluded.last_updated
                    """,
                    (
                        symbol,
                        fields["company_name"],
                        fields["sector"],
                        fields["industry"],
                        fields["shares_outstanding"],
                        fields["face_value"],
                        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    ),
                )
                conn.commit()
                print(f"[{i}/{len(symbols)}] OK   {symbol}: {fields}")
            except Exception as e:  # noqa: BLE001 - keep going through a big batch
                print(f"[{i}/{len(symbols)}] SKIP {symbol}: {e}")
            time.sleep(args.sleep)

            if not args.skip_results:
                try:
                    n = fetch_quarterly_results(nse, symbol, conn)
                    if n:
                        results_count += 1
                    print(f"[{i}/{len(symbols)}]   -> results: {n} quarter(s) stored")
                except Exception as e:  # noqa: BLE001
                    print(f"[{i}/{len(symbols)}]   -> results SKIP: {e}")
                time.sleep(args.sleep)

        conn.close()
        print(
            f"Done. {found_count}/{len(symbols)} symbols had usable sector/shares data, "
            f"{results_count}/{len(symbols)} had quarterly results data. "
            f"Database at: {COMPANY_DB_PATH}"
        )
        if found_count == 0:
            print(
                "No usable fields were found for ANY symbol — the field-name guesses in "
                "extract_company_fields() likely don't match NSE's actual response shape. "
                "Run `python fetch_company_info.py --debug RELIANCE` and share the output "
                "so the lookup can be corrected."
            )


if __name__ == "__main__":
    sys.exit(main())
