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

st.set_page_config(page_title="NSE Dashboard", page_icon="📈", layout="wide")

st.markdown(
    """
    <style>
    .block-container { padding-top: 2rem; padding-bottom: 3rem; }
    h1 { font-weight: 700; letter-spacing: -0.02em; }
    h2, h3 { font-weight: 600; letter-spacing: -0.01em; }
    [data-testid="stMetric"] {
        background: #F8FAFA;
        border: 1px solid #E2E8E7;
        border-radius: 10px;
        padding: 14px 16px 10px 16px;
    }
    [data-testid="stMetricLabel"] { font-size: 0.8rem; color: #5B6B69; }
    div[data-testid="stDataFrame"] { border: 1px solid #E2E8E7; border-radius: 10px; overflow: hidden; }
    .stTabs [data-baseweb="tab-list"] { gap: 4px; }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 8px 18px;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


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
    expected_cols = [
        "symbol",
        "company_name",
        "sector",
        "industry",
        "shares_outstanding",
        "face_value",
        "last_updated",
    ]
    if not COMPANY_DB_PATH.exists():
        return pd.DataFrame(columns=expected_cols)
    conn = sqlite3.connect(COMPANY_DB_PATH)
    try:
        df = pd.read_sql_query("SELECT * FROM company_info", conn)
    except pd.errors.DatabaseError:
        df = pd.DataFrame(columns=expected_cols)
    finally:
        conn.close()
    # Guard against an older company_info.db from before a column existed
    # (e.g. company_name added later) — always guarantee the shape app.py expects.
    for col in expected_cols:
        if col not in df.columns:
            df[col] = None
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def load_quarterly_results() -> pd.DataFrame:
    """Last ~5 quarters of revenue/net profit per symbol (NIFTY 500 scope,
    same as company_info). Empty DataFrame if not populated yet."""
    expected_cols = ["symbol", "quarter_end", "revenue_lakhs", "net_profit_lakhs", "last_updated"]
    if not COMPANY_DB_PATH.exists():
        return pd.DataFrame(columns=expected_cols)
    conn = sqlite3.connect(COMPANY_DB_PATH)
    try:
        df = pd.read_sql_query("SELECT * FROM quarterly_results", conn)
    except pd.errors.DatabaseError:
        df = pd.DataFrame(columns=expected_cols)
    finally:
        conn.close()
    for col in expected_cols:
        if col not in df.columns:
            df[col] = None
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def load_corporate_actions() -> pd.DataFrame:
    """Bonus/split events with a parsed adjustment_factor, whole market
    (not just Top 500) — fetch_corporate_actions.py. Empty DataFrame if
    not populated yet."""
    expected_cols = ["symbol", "series", "ex_date", "subject", "adjustment_factor", "last_updated"]
    if not COMPANY_DB_PATH.exists():
        return pd.DataFrame(columns=expected_cols)
    conn = sqlite3.connect(COMPANY_DB_PATH)
    try:
        df = pd.read_sql_query("SELECT * FROM corporate_actions", conn)
    except pd.errors.DatabaseError:
        df = pd.DataFrame(columns=expected_cols)
    finally:
        conn.close()
    for col in expected_cols:
        if col not in df.columns:
            df[col] = None
    if not df.empty:
        df["ex_date"] = pd.to_datetime(df["ex_date"])
        df = df.dropna(subset=["adjustment_factor"])  # only rows we could confidently parse
    return df


# ---------------------------------------------------------------------------
# Derived data
# ---------------------------------------------------------------------------
def build_adjusted_prices(prices: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Returns a copy of `prices` with open/high/low/close back-adjusted
    for bonus/split corporate actions, so 52-week high/low, moving
    averages, and RSI reflect prices that are actually comparable across
    time. This does NOT change what's shown for any single day's actual
    OHLC in the main table — only the cross-time comparisons that use
    this adjusted copy.

    Standard back-adjustment: today's price is unchanged; each historical
    price is multiplied by the cumulative product of every action's
    factor between that date and today.
    """
    if actions.empty:
        return prices

    df = prices.copy()
    df["_adj_factor"] = 1.0

    for symbol, grp in actions.groupby("symbol"):
        sym_mask = df["symbol"] == symbol
        if not sym_mask.any():
            continue
        sym_dates = df.loc[sym_mask, "date"]
        action_list = sorted(zip(grp["ex_date"], grp["adjustment_factor"]), reverse=True)

        multiplier = pd.Series(1.0, index=sym_dates.index)
        cum = 1.0
        for ex_date, factor in action_list:
            cum *= factor
            multiplier[sym_dates < ex_date] = cum
        df.loc[sym_mask, "_adj_factor"] = multiplier

    for col in ["open", "high", "low", "close"]:
        df[col] = df[col] * df["_adj_factor"]
    return df.drop(columns=["_adj_factor"])


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


def compute_results_trend(results: pd.DataFrame) -> pd.DataFrame:
    """Per symbol: latest quarter's net profit, QoQ % change, YoY % change
    (vs. the same quarter a year ago, i.e. 4 quarters back), and a
    Positive/Negative tag. YoY is preferred for the tag since it avoids
    seasonal noise; falls back to QoQ if a full year of history isn't
    stored yet."""
    cols = ["symbol", "latest_quarter_end", "latest_net_profit_cr", "qoq_change_pct", "yoy_change_pct", "results_trend"]
    if results.empty:
        return pd.DataFrame(columns=cols)

    df = results.copy()
    df["quarter_end_dt"] = pd.to_datetime(df["quarter_end"], format="%d-%b-%Y", errors="coerce")
    df = df.dropna(subset=["quarter_end_dt"])
    if df.empty:
        return pd.DataFrame(columns=cols)

    def _per_symbol(g: pd.DataFrame) -> pd.Series:
        g = g.sort_values("quarter_end_dt")
        latest = g.iloc[-1]
        qoq_prior = g.iloc[-2] if len(g) >= 2 else None
        yoy_prior = g.iloc[-5] if len(g) >= 5 else None

        qoq_pct = None
        if qoq_prior is not None and qoq_prior["net_profit_lakhs"]:
            qoq_pct = (latest["net_profit_lakhs"] - qoq_prior["net_profit_lakhs"]) / abs(
                qoq_prior["net_profit_lakhs"]
            ) * 100

        yoy_pct = None
        if yoy_prior is not None and yoy_prior["net_profit_lakhs"]:
            yoy_pct = (latest["net_profit_lakhs"] - yoy_prior["net_profit_lakhs"]) / abs(
                yoy_prior["net_profit_lakhs"]
            ) * 100

        if yoy_pct is not None:
            trend = "🟢 Positive" if yoy_pct > 0 else "🔴 Negative"
        elif qoq_pct is not None:
            trend = "🟢 Positive" if qoq_pct > 0 else "🔴 Negative"
        else:
            trend = None

        return pd.Series(
            {
                "latest_quarter_end": latest["quarter_end"],
                "latest_net_profit_cr": round(latest["net_profit_lakhs"] / 100, 1)
                if pd.notna(latest["net_profit_lakhs"])
                else None,
                "qoq_change_pct": round(qoq_pct, 1) if qoq_pct is not None else None,
                "yoy_change_pct": round(yoy_pct, 1) if yoy_pct is not None else None,
                "results_trend": trend,
            }
        )

    out = df.groupby("symbol").apply(_per_symbol).reset_index()
    return out


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


def style_pct_columns(df: pd.DataFrame):
    """Color numeric % columns green/red based on sign, and highlight the
    Results Trend text — pure visual polish, no data changes."""

    def _color(val):
        if pd.isna(val) or not isinstance(val, (int, float)):
            return ""
        if val > 0:
            return "color: #15803D; font-weight: 600"
        if val < 0:
            return "color: #B91C1C; font-weight: 600"
        return ""

    pct_cols = [
        c
        for c in ["% Change", "% From 52W High", "QoQ %", "YoY %"]
        if c in df.columns
    ]
    styler = df.style
    style_fn = styler.map if hasattr(styler, "map") else styler.applymap
    for col in pct_cols:
        styler = style_fn(_color, subset=[col])
        style_fn = styler.map if hasattr(styler, "map") else styler.applymap
    return styler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
st.title("📈 NSE Stock Dashboard")
st.caption("Daily prices, delivery %, sector data, technical indicators and a screener — built on NSE's official data.")

prices = load_prices()

if prices.empty:
    st.warning(
        "No price data yet. Run `python fetch_data.py` (or wait for the scheduled "
        "GitHub Action) to populate the database, then reload this page."
    )
    st.stop()

delivery = load_delivery()
company_info = load_company_info()
quarterly_results = load_quarterly_results()
corporate_actions = load_corporate_actions()

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

universe_scope = st.sidebar.radio(
    "Universe",
    ["Top 500 only", "All ~3400 listed stocks"],
    index=0,
    help=(
        "Top 500 = stocks with sector/company data (from fetch_company_info.py). "
        "The other ~2900 smaller/less-traded listings won't have sector or market cap "
        "until that enrichment is widened."
    ),
)

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

if not company_info.empty and "industry" in company_info.columns:
    industry_pool = company_info
    if sector_filter:
        industry_pool = industry_pool[industry_pool["sector"].isin(sector_filter)]
    all_industries = sorted(industry_pool["industry"].dropna().unique().tolist())
    industry_filter = st.sidebar.multiselect(
        "Industry (leave empty for all)", options=all_industries, default=[]
    )
else:
    industry_filter = []

st.sidebar.subheader("Market Cap (₹ Cr)")
mcap_min = st.sidebar.number_input("Min", min_value=0, value=0, step=1000)
mcap_max = st.sidebar.number_input("Max (0 = no limit)", min_value=0, value=0, step=1000)

if not quarterly_results.empty:
    results_trend_filter = st.sidebar.multiselect(
        "Latest results trend", options=["🟢 Positive", "🔴 Negative"], default=[]
    )
else:
    results_trend_filter = []

scoped_symbols_base = prices.loc[prices["series"].isin(series_filter), "symbol"].unique().tolist()
if universe_scope == "Top 500 only" and not company_info.empty:
    top500_set = set(company_info["symbol"].unique())
    scoped_symbols_base = [s for s in scoped_symbols_base if s in top500_set]
all_symbols = sorted(scoped_symbols_base)
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

# Back-adjusted for bonus/split — used for 52W high/low, moving averages, RSI,
# and the chart, so a stock's historical numbers stay comparable to today's
# price. The main table's own Open/High/Low/Close for a given day (below)
# intentionally stays UNADJUSTED — that's the actual price that day.
prices_adj = build_adjusted_prices(prices, corporate_actions)

# --- Build the table for the selected date ------------------------------
day_df = prices[(prices["date"] == as_of) & (prices["series"].isin(series_filter))].copy()

if universe_scope == "Top 500 only" and not company_info.empty:
    top500_symbols = set(company_info["symbol"].unique())
    day_df = day_df[day_df["symbol"].isin(top500_symbols)]

if symbol_search:
    day_df = day_df[day_df["symbol"].isin(symbol_search)]

if day_df.empty:
    st.info(
        "No data for the selected date/filters. NSE is closed on weekends "
        "and market holidays — try a different date."
    )
    st.stop()

# 52W high/low
w52 = compute_52w_high_low(prices_adj[prices_adj["series"].isin(series_filter)], as_of)
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
tech = compute_technical_snapshot(prices_adj[prices_adj["series"].isin(series_filter)], as_of)
day_df = day_df.merge(tech, on=["symbol", "series"], how="left")

# Last 5 days up/down trend
trend5 = compute_last5_trend(prices_adj[prices_adj["series"].isin(series_filter)], as_of)
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
    day_df["industry"] = None
    day_df["market_cap_cr"] = None

# Quarterly results trend
results_trend = compute_results_trend(quarterly_results)
day_df = day_df.merge(results_trend, on="symbol", how="left")

if sector_filter:
    day_df = day_df[day_df["sector"].isin(sector_filter)]

if industry_filter:
    day_df = day_df[day_df["industry"].isin(industry_filter)]

if mcap_min > 0:
    day_df = day_df[day_df["market_cap_cr"] >= mcap_min]
if mcap_max > 0:
    day_df = day_df[day_df["market_cap_cr"] <= mcap_max]

if results_trend_filter:
    day_df = day_df[day_df["results_trend"].isin(results_trend_filter)]

if day_df.empty:
    st.info("No stocks match the current filter combination — try widening one of them.")
    st.stop()

day_df["change"] = day_df["close"] - day_df["prev_close"]
day_df["% change"] = (day_df["change"] / day_df["prev_close"] * 100).round(2)
day_df["% from 52W high"] = ((day_df["close"] - day_df["w52_high"]) / day_df["w52_high"] * 100).round(2)

display_cols = {
    "symbol": "Symbol",
    "company_name": "Company Name",
    "series": "Series",
    "sector": "Sector",
    "industry": "Industry",
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
    "results_trend": "Results Trend",
    "latest_quarter_end": "Last Qtr End",
    "latest_net_profit_cr": "Last Qtr Net Profit (Cr)",
    "qoq_change_pct": "QoQ %",
    "yoy_change_pct": "YoY %",
    "volume": "Volume",
    "trades": "Trades",
}
table = day_df[list(display_cols.keys())].rename(columns=display_cols)
table = table.sort_values("Symbol").reset_index(drop=True)

tab_overview, tab_leaders, tab_screener, tab_chart, tab_about = st.tabs(
    ["📊 Overview", "🏆 Sector Leaders", "🔍 Screener", "📈 Chart", "ℹ️ About"]
)

with tab_overview:
    st.subheader(f"All {len(table)} stocks — {as_of.strftime('%d %b %Y')}")
    st.dataframe(style_pct_columns(table), use_container_width=True, height=500)

    st.download_button(
        "⬇️ Download this table as CSV",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name=f"nse_{as_of.strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )

with tab_leaders:
    if not company_info.empty and table["Sector"].notna().any():
        st.subheader("🏆 Sector Leaders (Top 5 by Market Cap)")
        leaders = table.dropna(subset=["Sector", "Market Cap (Cr)"]).copy()
        leaders = (
            leaders.sort_values("Market Cap (Cr)", ascending=False)
            .groupby("Sector", group_keys=False)
            .head(5)
            .sort_values(["Sector", "Market Cap (Cr)"], ascending=[True, False])
        )
        st.dataframe(
            style_pct_columns(
                leaders[["Sector", "Symbol", "Company Name", "Market Cap (Cr)", "Close", "% Change"]]
            ),
            use_container_width=True,
            height=500,
        )
    else:
        st.info(
            "No sector data loaded yet — run `python fetch_company_info.py` once to enable this "
            "(see README)."
        )

with tab_screener:
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
            "Results Improved YoY",
            "Results Declined YoY",
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
    elif screener_choice == "Results Improved YoY":
        screened = screened[screened["YoY %"] > 0].sort_values("YoY %", ascending=False)
    elif screener_choice == "Results Declined YoY":
        screened = screened[screened["YoY %"] < 0].sort_values("YoY %", ascending=True)

    st.dataframe(style_pct_columns(screened.head(n_results)), use_container_width=True, height=500)

with tab_chart:
    st.subheader("Symbol history")
    chosen = st.selectbox("Pick a symbol to chart", options=table["Symbol"].tolist())

    hist = compute_symbol_history_with_indicators(
        prices_adj[(prices_adj["series"].isin(series_filter)) & (prices_adj["date"] <= as_of)], chosen
    )

    if not hist.empty:
        row = w52[w52["symbol"] == chosen]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hist["date"], y=hist["close"], mode="lines", name="Close", line=dict(color="#0F766E", width=2)))
        fig.add_trace(go.Scatter(x=hist["date"], y=hist["sma20"], mode="lines", name="SMA20", line=dict(dash="dash")))
        fig.add_trace(go.Scatter(x=hist["date"], y=hist["sma50"], mode="lines", name="SMA50", line=dict(dash="dash")))
        fig.add_trace(go.Scatter(x=hist["date"], y=hist["sma200"], mode="lines", name="SMA200", line=dict(dash="dash")))
        if not row.empty:
            fig.add_hline(y=row["w52_high"].iloc[0], line_dash="dot", annotation_text="52W High", line_color="#15803D")
            fig.add_hline(y=row["w52_low"].iloc[0], line_dash="dot", annotation_text="52W Low", line_color="#B91C1C")
        fig.update_layout(
            height=420,
            margin=dict(l=10, r=10, t=30, b=10),
            legend=dict(orientation="h"),
            plot_bgcolor="white",
            paper_bgcolor="white",
        )
        st.plotly_chart(fig, use_container_width=True)

        rsi_fig = go.Figure()
        rsi_fig.add_trace(go.Scatter(x=hist["date"], y=hist["rsi14"], mode="lines", name="RSI(14)", line=dict(color="#0F766E")))
        rsi_fig.add_hline(y=70, line_dash="dot", line_color="#B91C1C")
        rsi_fig.add_hline(y=30, line_dash="dot", line_color="#15803D")
        rsi_fig.update_layout(
            height=180,
            margin=dict(l=10, r=10, t=10, b=10),
            yaxis_range=[0, 100],
            plot_bgcolor="white",
            paper_bgcolor="white",
        )
        st.plotly_chart(rsi_fig, use_container_width=True)

with tab_about:
    st.markdown(
        """
        **Data sources**
        - Daily OHLC: `fetch_data.py` → NSE's official bhavcopy
        - Delivery % / VWAP: `fetch_delivery.py` → NSE's `sec_bhavdata_full` report
        - Sector / industry / market cap basis / quarterly results: `fetch_company_info.py`
          → per-symbol NSE lookup, run occasionally (monthly) since this data changes
          rarely (NIFTY 500 scope — toggle "Universe" in the sidebar)
        - Bonus/split back-adjustment: `fetch_corporate_actions.py` → whole-market bulk
          fetch, run daily

        **Notes**
        - 52-week High/Low, moving averages, and RSI are back-adjusted for bonus/split
          corporate actions so they stay comparable across time. The main table's
          actual daily Open/High/Low/Close is never adjusted.
        - RSI here is a simple (non-Wilder-smoothed) 14-period RSI — fine for
          screening, not identical to every charting platform's exact number.
        - Market Cap is estimated as shares outstanding × close price — treat it
          as approximate, not exchange-verified.
        - Results Trend compares the latest reported quarter's net profit to the
          same quarter a year ago (YoY), falling back to the prior quarter (QoQ)
          if a full year of results history isn't stored yet. This reflects
          growth vs. the company's own history, not a beat/miss vs. analyst
          estimates (NSE doesn't publish those for free).

        **Adding more later**: each data source above is its own small script +
        its own loader function in this file. To add something new, follow that
        same pattern — a new fetch script, a new loader, one more merge.
        """
    )
