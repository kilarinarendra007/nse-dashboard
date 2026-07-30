"""
NSE Dashboard - Streamlit app
------------------------------
Reads data/nse_data.db (populated by fetch_data.py, refreshed daily by
a GitHub Action) and displays an interactive dashboard of all NSE
listed equities: daily Open/High/Low/Close and rolling 52-week
High/Low.

Designed to be extended: this file is intentionally kept simple and
sectioned so more data (indices, sector info, fundamentals, etc.) can
be added later without restructuring everything.
"""

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path(__file__).parent / "data" / "nse_data.db"

st.set_page_config(page_title="NSE Dashboard", layout="wide")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def load_data() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM daily_prices", conn)
    conn.close()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def compute_52w_high_low(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """For every symbol, compute the high/low of `close` over the
    trailing 52 weeks (365 calendar days) up to as_of date."""
    window_start = as_of - pd.Timedelta(days=365)
    window = df[(df["date"] > window_start) & (df["date"] <= as_of)]
    agg = window.groupby(["symbol", "series"]).agg(
        w52_high=("high", "max"),
        w52_low=("low", "min"),
    )
    return agg.reset_index()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
st.title("📈 NSE Stock Dashboard")

data = load_data()

if data.empty:
    st.warning(
        "No data yet. Run `python fetch_data.py` (or wait for the scheduled "
        "GitHub Action) to populate the database, then reload this page."
    )
    st.stop()

latest_date = data["date"].max()

# --- Sidebar controls -------------------------------------------------
st.sidebar.header("Filters")

as_of = st.sidebar.date_input(
    "As-of date",
    value=latest_date.date(),
    min_value=data["date"].min().date(),
    max_value=latest_date.date(),
)
as_of = pd.Timestamp(as_of)

all_series = sorted(data["series"].dropna().unique().tolist())
default_series = ["EQ"] if "EQ" in all_series else all_series
series_filter = st.sidebar.multiselect(
    "Series", options=all_series, default=default_series
)

all_symbols = sorted(data.loc[data["series"].isin(series_filter), "symbol"].unique().tolist())
symbol_search = st.sidebar.multiselect(
    "Limit to specific symbols (optional, leave empty for all)",
    options=all_symbols,
)

# --- Header metrics -----------------------------------------------------
col1, col2, col3 = st.columns(3)
col1.metric("Data as of", latest_date.strftime("%d %b %Y"))
col2.metric("Trading days stored", data["date"].nunique())
col3.metric("Symbols tracked", data["symbol"].nunique())

# --- Build the table for the selected date ------------------------------
day_df = data[(data["date"] == as_of) & (data["series"].isin(series_filter))].copy()

if symbol_search:
    day_df = day_df[day_df["symbol"].isin(symbol_search)]

if day_df.empty:
    st.info(
        "No data for the selected date/filters. NSE is closed on weekends "
        "and market holidays — try a different date."
    )
    st.stop()

w52 = compute_52w_high_low(data[data["series"].isin(series_filter)], as_of)
day_df = day_df.merge(w52, on=["symbol", "series"], how="left")

day_df["change"] = day_df["close"] - day_df["prev_close"]
day_df["% change"] = (day_df["change"] / day_df["prev_close"] * 100).round(2)
day_df["% from 52W high"] = (
    (day_df["close"] - day_df["w52_high"]) / day_df["w52_high"] * 100
).round(2)

display_cols = {
    "symbol": "Symbol",
    "series": "Series",
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "prev_close": "Prev Close",
    "% change": "% Change",
    "w52_high": "52W High",
    "w52_low": "52W Low",
    "% from 52W high": "% From 52W High",
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

# --- Drill-down chart for one symbol -------------------------------------
st.subheader("Symbol history")
chosen = st.selectbox("Pick a symbol to chart", options=table["Symbol"].tolist())

hist = data[
    (data["symbol"] == chosen) & (data["series"].isin(series_filter)) & (data["date"] <= as_of)
].sort_values("date")

if not hist.empty:
    row = w52[w52["symbol"] == chosen]
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(x=hist["date"], y=hist["close"], mode="lines", name="Close")
    )
    if not row.empty:
        fig.add_hline(
            y=row["w52_high"].iloc[0],
            line_dash="dot",
            annotation_text="52W High",
            line_color="green",
        )
        fig.add_hline(
            y=row["w52_low"].iloc[0],
            line_dash="dot",
            annotation_text="52W Low",
            line_color="red",
        )
    fig.update_layout(height=400, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, use_container_width=True)

with st.expander("ℹ️ About this dashboard / how to extend it"):
    st.markdown(
        """
        - Data source: NSE's official daily bhavcopy, downloaded by `fetch_data.py`.
        - A GitHub Action refreshes `data/nse_data.db` automatically after each
          trading day's close.
        - 52-week High/Low here are computed live from stored history
          (trailing 365 calendar days), not NSE's separate report — so it
          stays accurate as long as this database has a year of history.
        - To add more data later (indices, F&O, delivery %, sector info,
          fundamentals, etc.): add a new fetch function + table, then add a
          new section to this file. The structure is intentionally simple
          so this is a small change, not a rewrite.
        """
    )
