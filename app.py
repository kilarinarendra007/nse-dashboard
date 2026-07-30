"""
NSE Dashboard - Streamlit app
------------------------------
Reads:
  - data/nse_data_*.db      (daily OHLC, one file per quarter — fetch_data.py)
  - data/nse_data_*.db      (delivery %, VWAP, same files, different table — fetch_delivery.py)
  - data/company_info.db    (sector/industry/shares outstanding — fetch_company_info.py)

...and displays an interactive dashboard: daily OHLC, rolling 52-week
High/Low, Delivery %, VWAP, market cap, moving averages/RSI, sector
filtering, and a small screener (top gainers/losers, near 52W high,
high delivery %).

Designed to be extended further: each data source is its own loader
function, merged together in one place, so adding another data source
later means adding one more loader + one more merge, not a rewrite.
"""

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DATA_DIR = Path(__file__).parent / "data"
COMPANY_DB_PATH = DATA_DIR / "company_info.db"

st.set_page_config(page_title="NSE Dashboard", layout="wide")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def load_prices() -> pd.DataFrame:
    """Daily OHLC, one SQLite file per quarter (nse_data_2026Q1.db, etc.)."""
    db_files = sorted(DATA_DIR.glob("nse_data_*.db"))
    if not db_files:
        return pd.DataFrame()

    frames = []
    for db_file in db_files:
        conn = sqlite3.connect(db_file)
        try:
            frames.append(pd.read_sql_query("SELECT * FROM daily_prices", conn))
        finally:
            conn.close()

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data(ttl=600, show_spinner=False)
def load_delivery() -> pd.DataFrame:
    """Delivery %/VWAP, same quarterly files as prices but a different table.
    Returns an empty DataFrame (not an error) if fetch_delivery.py hasn't
    been run yet, so the rest of the app degrades gracefully."""
    db_files = sorted(DATA_DIR.glob("nse_data_*.db"))
    frames = []
    for db_file in db_files:
        conn = sqlite3.connect(db_file)
        try:
            frames.append(pd.read_sql_query("SELECT * FROM delivery_data", conn))
        except pd.errors.DatabaseError:
            pass  # table doesn't exist in this file yet — fine
        finally:
            conn.close()

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def load_company_info() -> pd.DataFrame:
    """Sector/industry/shares-outstanding — one small file, not split by
    quarter since it changes rarely. Empty DataFrame if not populated yet."""
    if not COMPANY_DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(COMPANY_DB_PATH)
    try:
        df = pd.read_sql_query("SELECT * FROM company_info", conn)
    except pd.errors.DatabaseError:
        df = pd.DataFrame()
    finally:
        conn.close()
    return df


# ---------------------------------------------------------------------------
# Derived data
# ---------------------------------------------------------------------------
def compute_52w_high_low(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    window_start = as_of - pd.Timedelta(days=365)
    window = df[(df["date"] > window_start) & (df["date"] <= as_of)]
    agg = window.groupby(["symbol", "series"]).agg(
        w52_high=("high", "max"),
        w52_low=("low", "min"),
    )
    return agg.reset_index()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Simple (non-Wilder-smoothed) RSI — fine for screening purposes."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_technical_snapshot(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """SMA20/50/200 and RSI14 as of `as_of`, one row per symbol+series."""
    hist = df[df["date"] <= as_of].sort_values(["symbol", "series", "date"]).copy()
    if hist.empty:
        return pd.DataFrame(columns=["symbol", "series", "sma20", "sma50", "sma200", "rsi14"])
    grouped = hist.groupby(["symbol", "series"], group_keys=False)["close"]
    hist["sma20"] = grouped.transform(lambda s: s.rolling(20).mean())
    hist["sma50"] = grouped.transform(lambda s: s.rolling(50).mean())
    hist["sma200"] = grouped.transform(lambda s: s.rolling(200).mean())
    hist["rsi14"] = grouped.transform(_rsi)
    latest = hist.groupby(["symbol", "series"], as_index=False).tail(1)
    return latest[["symbol", "series", "sma20", "sma50", "sma200", "rsi14"]]


def compute_last5_trend(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Last up-to-5 trading days' up/down direction per symbol, as a
    small string of green/red squares (oldest -> newest, left to right)."""
    hist = df[df["date"] <= as_of].sort_values(["symbol", "series", "date"]).copy()
    if hist.empty:
        return pd.DataFrame(columns=["symbol", "series", "trend5"])
    hist["chg"] = hist.groupby(["symbol", "series"])["close"].diff()
    last5 = hist.groupby(["symbol", "series"], as_index=False).tail(5)

    def _emoji(x):
        if pd.isna(x):
            return "⬜"
        return "🟢" if x > 0 else ("🔴" if x < 0 else "⬜")

    trend = (
        last5.groupby(["symbol", "series"])["chg"]
        .apply(lambda s: "".join(_emoji(x) for x in s))
        .reset_index(name="trend5")
    )
    return trend


def compute_symbol_history_with_indicators(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Full time series + rolling indicators for one symbol, for the chart."""
    hist = df[df["symbol"] == symbol].sort_values("date").copy()
    hist["sma20"] = hist["close"].rolling(20).mean()
    hist["sma50"] = hist["close"].rolling(50).mean()
    hist["sma200"] = hist["close"].rolling(200).mean()
    hist["rsi14"] = _rsi(hist["close"])
    return hist


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
st.title("📈 NSE Stock Dashboard")

prices = load_prices()

if prices.empty:
    st.warning(
        "No price data yet. Run `python fetch_data.py` (or wait for the scheduled "
        "GitHub Action) to populate the database, then reload this page."
    )
    st.stop()

delivery = load_delivery()
company_info = load_company_info()

latest_date = prices["date"].max()

# --- Sidebar controls -------------------------------------------------
st.sidebar.header("Filters")

as_of = st.sidebar.date_input(
    "As-of date",
    value=latest_date.date(),
    min_value=prices["date"].min().date(),
    max_value=latest_date.date(),
)
as_of = pd.Timestamp(as_of)

all_series = sorted(prices["series"].dropna().unique().tolist())
default_series = ["EQ"] if "EQ" in all_series else all_series
series_filter = st.sidebar.multiselect("Series", options=all_series, default=default_series)

if not company_info.empty and "sector" in company_info.columns:
    all_sectors = sorted(company_info["sector"].dropna().unique().tolist())
    sector_filter = st.sidebar.multiselect(
        "Sector (leave empty for all)", options=all_sectors, default=[]
    )
else:
    sector_filter = []
    st.sidebar.caption(
        "Sector data not loaded yet — run `python fetch_company_info.py` once to enable "
        "sector filtering (see README)."
    )

all_symbols = sorted(prices.loc[prices["series"].isin(series_filter), "symbol"].unique().tolist())
symbol_search = st.sidebar.multiselect(
    "Limit to specific symbols (optional, leave empty for all)", options=all_symbols
)

# --- Header metrics -----------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
col1.metric("Data as of", latest_date.strftime("%d %b %Y"))
col2.metric("Trading days stored", prices["date"].nunique())
col3.metric("Symbols tracked", prices["symbol"].nunique())
col4.metric(
    "Sector-classified",
    f"{company_info['symbol'].nunique()}" if not company_info.empty else "0",
)

# --- Build the table for the selected date ------------------------------
day_df = prices[(prices["date"] == as_of) & (prices["series"].isin(series_filter))].copy()

if symbol_search:
    day_df = day_df[day_df["symbol"].isin(symbol_search)]

if day_df.empty:
    st.info(
        "No data for the selected date/filters. NSE is closed on weekends "
        "and market holidays — try a different date."
    )
    st.stop()

# 52W high/low
w52 = compute_52w_high_low(prices[prices["series"].isin(series_filter)], as_of)
day_df = day_df.merge(w52, on=["symbol", "series"], how="left")

# Delivery % / VWAP for this date
if not delivery.empty:
    deliv_day = delivery[delivery["date"] == as_of][
        ["symbol", "series", "deliv_qty", "deliv_per", "avg_price"]
    ]
    day_df = day_df.merge(deliv_day, on=["symbol", "series"], how="left")
else:
    day_df["deliv_qty"] = None
    day_df["deliv_per"] = None
    day_df["avg_price"] = None

# Technical indicators
tech = compute_technical_snapshot(prices[prices["series"].isin(series_filter)], as_of)
day_df = day_df.merge(tech, on=["symbol", "series"], how="left")

# Last 5 days up/down trend
trend5 = compute_last5_trend(prices[prices["series"].isin(series_filter)], as_of)
day_df = day_df.merge(trend5, on=["symbol", "series"], how="left")

# Sector / market cap
if not company_info.empty:
    day_df = day_df.merge(company_info, on="symbol", how="left")
    day_df["market_cap_cr"] = (
        day_df["shares_outstanding"] * day_df["close"] / 1e7
    ).round(1)  # in INR crores
else:
    day_df["company_name"] = None
    day_df["sector"] = None
    day_df["market_cap_cr"] = None

if sector_filter:
    day_df = day_df[day_df["sector"].isin(sector_filter)]

day_df["change"] = day_df["close"] - day_df["prev_close"]
day_df["% change"] = (day_df["change"] / day_df["prev_close"] * 100).round(2)
day_df["% from 52W high"] = ((day_df["close"] - day_df["w52_high"]) / day_df["w52_high"] * 100).round(2)

display_cols = {
    "symbol": "Symbol",
    "company_name": "Company Name",
    "series": "Series",
    "sector": "Sector",
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "prev_close": "Prev Close",
    "% change": "% Change",
    "trend5": "Last 5D",
    "w52_high": "52W High",
    "w52_low": "52W Low",
    "% from 52W high": "% From 52W High",
    "deliv_per": "Delivery %",
    "avg_price": "VWAP",
    "sma20": "SMA20",
    "sma50": "SMA50",
    "sma200": "SMA200",
    "rsi14": "RSI(14)",
    "market_cap_cr": "Market Cap (Cr)",
    "volume": "Volume",
    "trades": "Trades",
}
table = day_df[list(display_cols.keys())].rename(columns=display_cols)
table = table.sort_values("Symbol").reset_index(drop=True)

st.subheader(f"All {len(table)} stocks — {as_of.strftime('%d %b %Y')}")
st.dataframe(table, use_container_width=True, height=500)

st.download_button(
    "⬇️ Download this table as CSV",
    data=table.to_csv(index=False).encode("utf-8"),
    file_name=f"nse_{as_of.strftime('%Y%m%d')}.csv",
    mime="text/csv",
)

# --- Screener -------------------------------------------------------------
st.subheader("🔍 Screener")
screener_choice = st.selectbox(
    "Preset",
    [
        "Top Gainers",
        "Top Losers",
        "Near 52W High",
        "Near 52W Low",
        "High Delivery %",
        "Oversold (RSI < 30)",
        "Overbought (RSI > 70)",
    ],
)
n_results = st.slider("Number of results", 5, 100, 20)

screened = table.copy()
if screener_choice == "Top Gainers":
    screened = screened.sort_values("% Change", ascending=False)
elif screener_choice == "Top Losers":
    screened = screened.sort_values("% Change", ascending=True)
elif screener_choice == "Near 52W High":
    screened = screened.sort_values("% From 52W High", ascending=False)
elif screener_choice == "Near 52W Low":
    screened = screened.sort_values("% From 52W High", ascending=True)
elif screener_choice == "High Delivery %":
    screened = screened.sort_values("Delivery %", ascending=False)
elif screener_choice == "Oversold (RSI < 30)":
    screened = screened[screened["RSI(14)"] < 30].sort_values("RSI(14)", ascending=True)
elif screener_choice == "Overbought (RSI > 70)":
    screened = screened[screened["RSI(14)"] > 70].sort_values("RSI(14)", ascending=False)

st.dataframe(screened.head(n_results), use_container_width=True)

# --- Drill-down chart for one symbol -------------------------------------
st.subheader("Symbol history")
chosen = st.selectbox("Pick a symbol to chart", options=table["Symbol"].tolist())

hist = compute_symbol_history_with_indicators(
    prices[(prices["series"].isin(series_filter)) & (prices["date"] <= as_of)], chosen
)

if not hist.empty:
    row = w52[w52["symbol"] == chosen]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=hist["date"], y=hist["close"], mode="lines", name="Close"))
    fig.add_trace(go.Scatter(x=hist["date"], y=hist["sma20"], mode="lines", name="SMA20", line=dict(dash="dash")))
    fig.add_trace(go.Scatter(x=hist["date"], y=hist["sma50"], mode="lines", name="SMA50", line=dict(dash="dash")))
    fig.add_trace(go.Scatter(x=hist["date"], y=hist["sma200"], mode="lines", name="SMA200", line=dict(dash="dash")))
    if not row.empty:
        fig.add_hline(y=row["w52_high"].iloc[0], line_dash="dot", annotation_text="52W High", line_color="green")
        fig.add_hline(y=row["w52_low"].iloc[0], line_dash="dot", annotation_text="52W Low", line_color="red")
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True)

    rsi_fig = go.Figure()
    rsi_fig.add_trace(go.Scatter(x=hist["date"], y=hist["rsi14"], mode="lines", name="RSI(14)"))
    rsi_fig.add_hline(y=70, line_dash="dot", line_color="red")
    rsi_fig.add_hline(y=30, line_dash="dot", line_color="green")
    rsi_fig.update_layout(height=180, margin=dict(l=10, r=10, t=10, b=10), yaxis_range=[0, 100])
    st.plotly_chart(rsi_fig, use_container_width=True)

with st.expander("ℹ️ About this dashboard / how to extend it"):
    st.markdown(
        """
        **Data sources**
        - Daily OHLC: `fetch_data.py` → NSE's official bhavcopy
        - Delivery % / VWAP: `fetch_delivery.py` → NSE's `sec_bhavdata_full` report
        - Sector / industry / market cap basis: `fetch_company_info.py` → per-symbol
          NSE lookup, run occasionally (weekly/monthly) since this data changes rarely

        **Notes**
        - 52-week High/Low are computed live from stored history (trailing 365
          calendar days), not NSE's separate report.
        - RSI here is a simple (non-Wilder-smoothed) 14-period RSI — fine for
          screening, not identical to every charting platform's exact number.
        - Market Cap is estimated as shares outstanding × close price — treat it
          as approximate, not exchange-verified.

        **Adding more later**: each data source above is its own small script +
        its own loader function in this file. To add something new, follow that
        same pattern — a new fetch script, a new loader, one more merge.
        """
    )
