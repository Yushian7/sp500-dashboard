"""
S&P 500 Durability / Valuation / Momentum data fetcher.

Pulls fundamental + price data for the S&P 500 universe via yfinance,
computes percentile-ranked scores in three categories, and writes a single
data.json file that the dashboard (Claude artifact) reads.

Run via GitHub Actions on a schedule (see .github/workflows/update-data.yml).
Can also be run locally: `python fetch_data.py`
"""

import json
import time
import math
import datetime
import urllib.request
import io

import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# 1. Get the current S&P 500 ticker list (scraped from Wikipedia, which keeps
#    an up-to-date constituent table). Falls back to a small hardcoded list
#    if the fetch fails, so the script never hard-crashes.
# ---------------------------------------------------------------------------

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

FALLBACK_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "BRK-B",
    "JPM", "LLY", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "NFLX", "JNJ",
]


def get_sp500_tickers() -> list[str]:
    try:
        req = urllib.request.Request(
            WIKI_URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        html = urllib.request.urlopen(req, timeout=20).read()
        tables = pd.read_html(io.BytesIO(html))
        df = tables[0]
        tickers = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        return sorted(set(tickers))
    except Exception as e:
        print(f"[warn] Could not fetch S&P 500 list from Wikipedia ({e}); using fallback list.")
        return FALLBACK_TICKERS


# ---------------------------------------------------------------------------
# 2. Per-ticker raw metric extraction.
#    Each function returns None for a metric if data is unavailable, so
#    downstream scoring can simply skip missing values.
# ---------------------------------------------------------------------------

def safe_get(d: dict, key, default=None):
    val = d.get(key, default)
    if val is None:
        return default
    if isinstance(val, float) and math.isnan(val):
        return default
    return val


def fetch_ticker_raw(ticker: str) -> dict | None:
    try:
        t = yf.Ticker(ticker)
        info = t.info  # fundamental snapshot
        hist = t.history(period="1y", interval="1d", auto_adjust=True)

        if hist is None or hist.empty or not info:
            return None

        close = hist["Close"]
        last_price = float(close.iloc[-1])

        # --- Momentum inputs ---
        ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
        ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

        # RSI (14-day, Wilder's smoothing)
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / 14, min_periods=14).mean()
        avg_loss = loss.ewm(alpha=1 / 14, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        rsi_series = 100 - (100 / (1 + rs))
        rsi14 = float(rsi_series.iloc[-1]) if not rsi_series.empty else None

        def pct_return(days):
            if len(close) > days:
                past = float(close.iloc[-days - 1])
                return (last_price / past - 1) * 100 if past else None
            return None

        ret_3m = pct_return(63)   # ~3 trading months
        ret_6m = pct_return(126)  # ~6 trading months

        week52_high = float(hist["High"].max())
        pct_from_52w_high = (last_price / week52_high - 1) * 100 if week52_high else None

        raw = {
            "ticker": ticker,
            "name": safe_get(info, "shortName", ticker),
            "sector": safe_get(info, "sector", "Unknown"),
            "industry": safe_get(info, "industry", "Unknown"),
            "price": last_price,
            "market_cap": safe_get(info, "marketCap"),

            # Valuation raw inputs
            "trailing_pe": safe_get(info, "trailingPE"),
            "forward_pe": safe_get(info, "forwardPE"),
            "price_to_book": safe_get(info, "priceToBook"),
            "ev_to_ebitda": safe_get(info, "enterpriseToEbitda"),
            "peg_ratio": safe_get(info, "trailingPegRatio") or safe_get(info, "pegRatio"),
            "price_to_fcf": None,  # derived below if possible

            # Durability raw inputs
            "debt_to_equity": safe_get(info, "debtToEquity"),
            "current_ratio": safe_get(info, "currentRatio"),
            "interest_coverage": None,  # derived below if possible
            "free_cashflow": safe_get(info, "freeCashflow"),
            "operating_cashflow": safe_get(info, "operatingCashflow"),
            "net_income": safe_get(info, "netIncomeToCommon"),
            "earnings_growth": safe_get(info, "earningsGrowth"),
            "revenue_growth": safe_get(info, "revenueGrowth"),
            "return_on_equity": safe_get(info, "returnOnEquity"),
            "profit_margin": safe_get(info, "profitMargins"),

            # Momentum raw inputs
            "ma50": ma50,
            "ma200": ma200,
            "rsi14": rsi14,
            "return_3m": ret_3m,
            "return_6m": ret_6m,
            "pct_from_52w_high": pct_from_52w_high,

            "fetched_at": datetime.datetime.utcnow().isoformat(),
        }

        # price-to-FCF if we have both market cap and FCF
        if raw["market_cap"] and raw["free_cashflow"] and raw["free_cashflow"] != 0:
            raw["price_to_fcf"] = raw["market_cap"] / raw["free_cashflow"]

        # crude interest coverage proxy: operating cashflow / |debt-related expense|
        # yfinance doesn't expose interest expense directly in `.info` reliably,
        # so this stays None for most tickers unless financials are pulled (slow).
        # Left as a placeholder for a future enhancement (see README).

        return raw

    except Exception as e:
        print(f"[error] {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# 3. Percentile-rank scoring helpers
# ---------------------------------------------------------------------------

def percentile_rank_series(series: pd.Series, higher_is_better: bool) -> pd.Series:
    """Rank values 0-100 by percentile within the universe. NaNs -> NaN."""
    ranked = series.rank(pct=True, na_option="keep") * 100
    if not higher_is_better:
        ranked = 100 - ranked
    return ranked


def compute_scores(df: pd.DataFrame) -> pd.DataFrame:
    # ---- Durability sub-scores ----
    df["s_debt_to_equity"] = percentile_rank_series(df["debt_to_equity"], higher_is_better=False)
    df["s_current_ratio"] = percentile_rank_series(df["current_ratio"], higher_is_better=True)
    df["s_roe"] = percentile_rank_series(df["return_on_equity"], higher_is_better=True)
    df["s_profit_margin"] = percentile_rank_series(df["profit_margin"], higher_is_better=True)
    df["s_fcf_positive"] = df["free_cashflow"].apply(
        lambda x: 100.0 if (x is not None and x > 0) else (0.0 if x is not None else float("nan"))
    )

    durability_components = [
        "s_debt_to_equity", "s_current_ratio", "s_roe", "s_profit_margin", "s_fcf_positive"
    ]
    df["durability_score"] = df[durability_components].mean(axis=1, skipna=True).round(1)

    # ---- Valuation sub-scores (lower multiple = better = higher score) ----
    df["s_pe"] = percentile_rank_series(df["trailing_pe"], higher_is_better=False)
    df["s_pb"] = percentile_rank_series(df["price_to_book"], higher_is_better=False)
    df["s_ev_ebitda"] = percentile_rank_series(df["ev_to_ebitda"], higher_is_better=False)
    df["s_peg"] = percentile_rank_series(df["peg_ratio"], higher_is_better=False)
    df["s_p_fcf"] = percentile_rank_series(df["price_to_fcf"], higher_is_better=False)

    valuation_components = ["s_pe", "s_pb", "s_ev_ebitda", "s_peg", "s_p_fcf"]
    df["valuation_score"] = df[valuation_components].mean(axis=1, skipna=True).round(1)

    # ---- Momentum sub-scores ----
    df["s_above_ma50"] = df.apply(
        lambda r: 100.0 if (r["ma50"] and r["price"] > r["ma50"]) else (0.0 if r["ma50"] else float("nan")),
        axis=1,
    )
    df["s_above_ma200"] = df.apply(
        lambda r: 100.0 if (r["ma200"] and r["price"] > r["ma200"]) else (0.0 if r["ma200"] else float("nan")),
        axis=1,
    )
    # RSI: score peaks near 50-65 (healthy momentum), penalize extreme overbought/oversold
    def rsi_score(rsi):
        if rsi is None or (isinstance(rsi, float) and math.isnan(rsi)):
            return float("nan")
        if rsi >= 80:
            return 60.0
        if rsi <= 20:
            return 20.0
        # linear-ish mapping favoring 50-70 zone
        return max(0.0, min(100.0, rsi * 1.1))

    df["s_rsi"] = df["rsi14"].apply(rsi_score)
    df["s_return_3m"] = percentile_rank_series(df["return_3m"], higher_is_better=True)
    df["s_return_6m"] = percentile_rank_series(df["return_6m"], higher_is_better=True)
    df["s_near_52w_high"] = percentile_rank_series(df["pct_from_52w_high"], higher_is_better=True)

    momentum_components = [
        "s_above_ma50", "s_above_ma200", "s_rsi", "s_return_3m", "s_return_6m", "s_near_52w_high"
    ]
    df["momentum_score"] = df[momentum_components].mean(axis=1, skipna=True).round(1)

    return df


# ---------------------------------------------------------------------------
# 4. Main
# ---------------------------------------------------------------------------

def main():
    tickers = get_sp500_tickers()
    print(f"Fetching data for {len(tickers)} tickers...")

    records = []
    for i, tk in enumerate(tickers):
        raw = fetch_ticker_raw(tk)
        if raw:
            records.append(raw)
        if (i + 1) % 25 == 0:
            print(f"  ...{i + 1}/{len(tickers)} done")
        time.sleep(0.3)  # be polite to the API, avoid throttling

    df = pd.DataFrame(records)
    print(f"Successfully fetched {len(df)} / {len(tickers)} tickers.")

    df = compute_scores(df)

    output_cols = [
        "ticker", "name", "sector", "industry", "price", "market_cap",
        "durability_score", "valuation_score", "momentum_score",
        "trailing_pe", "forward_pe", "price_to_book", "ev_to_ebitda", "peg_ratio", "price_to_fcf",
        "debt_to_equity", "current_ratio", "return_on_equity", "profit_margin", "free_cashflow",
        "ma50", "ma200", "rsi14", "return_3m", "return_6m", "pct_from_52w_high",
        "s_debt_to_equity", "s_current_ratio", "s_roe", "s_profit_margin", "s_fcf_positive",
        "s_pe", "s_pb", "s_ev_ebitda", "s_peg", "s_p_fcf",
        "s_above_ma50", "s_above_ma200", "s_rsi", "s_return_3m", "s_return_6m", "s_near_52w_high",
        "fetched_at",
    ]
    df_out = df[[c for c in output_cols if c in df.columns]]

    # Replace NaN with None for valid JSON
    records_out = df_out.where(pd.notnull(df_out), None).to_dict(orient="records")

    output = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "universe_size": len(records_out),
        "stocks": records_out,
    }

    with open("data.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"Wrote data.json with {len(records_out)} stocks.")


if __name__ == "__main__":
    main()
