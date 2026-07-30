"""
fetch_company_info.py
----------------------
Populates sector/industry classification and share count (for computing
market cap) for stocks, stored in a single small database:
data/company_info.db (not split by quarter — this data changes rarely,
unlike daily prices).

Unlike the bhavcopy/delivery reports (one bulk file per day), NSE doesn't
publish sector or shares-outstanding data in bulk for free. The only way
to get it is one API call per stock (`getDetailedScripData`), which is
slow and best run occasionally (weekly/monthly), not daily.

To keep runtime reasonable and avoid hammering NSE's servers, this
defaults to NIFTY 500 constituents (covers the vast majority of actively
traded stocks) rather than all ~3400 listed securities. You can widen
this later — see --index below.

IMPORTANT — this script is best-effort: NSE's per-symbol response format
isn't officially documented, so this script searches the response
generically for fields that look like sector/industry/shares-outstanding
data rather than assuming an exact structure. If your first run reports
lots of "not found", paste the debug output back and we'll refine the
field lookup together.

Usage
-----
    python fetch_company_info.py                     # NIFTY 500, default
    python fetch_company_info.py --index "NIFTY 100"  # smaller/faster run
    python fetch_company_info.py --debug AAPL         # dump raw response for one symbol
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
    """Reject values that look like an internal index-name lookup (e.g.
    'NIFTY AUTO                    ') rather than a genuine
    sector/industry classification."""
    if value is None:
        return True
    s = str(value).strip()
    if not s or s == "-":
        return True
    if "NIFTY" in s.upper():
        return True
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
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with NSE(download_folder=TMP_DOWNLOAD_DIR) as nse:
        if args.debug:
            import json

            raw = nse.getDetailedScripData(args.debug)
            print(json.dumps(raw, indent=2)[:5000])
            return

        symbols = get_universe(nse, args.index)
        print(f"Found {len(symbols)} symbols in {args.index}")

        conn = sqlite3.connect(COMPANY_DB_PATH)
        init_db(conn)

        found_count = 0
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

        conn.close()
        print(
            f"Done. {found_count}/{len(symbols)} symbols had usable sector/shares data. "
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
