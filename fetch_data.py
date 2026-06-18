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


def compute_multiyear_metrics(t: "yf.Ticker") -> dict:
    """
    Pull annual income-statement and cashflow history and derive the
    multi-period 'durability over time' signals Trendlyne emphasizes:
      - revenue CAGR (multi-year growth)
      - earnings (net income) CAGR
      - revenue growth consistency (how many years grew YoY)
      - earnings positivity (how many years net income was positive)
      - FCF positivity streak (how many years FCF was positive)
      - earnings stability (inverse of coefficient of variation)
    All values are None when the underlying statements aren't available,
    so scoring downstream can skip them cleanly.
    """
    out = {
        "revenue_cagr": None,
        "earnings_cagr": None,
        "revenue_growth_consistency": None,
        "earnings_positive_years": None,
        "fcf_positive_years": None,
        "earnings_stability": None,
        "years_of_data": None,
    }
    try:
        fin = t.financials  # annual income statement, columns = years (newest first)
        cf = t.cashflow      # annual cashflow statement

        def row(df, *names):
            if df is None or df.empty:
                return None
            for n in names:
                if n in df.index:
                    s = df.loc[n].dropna()
                    if not s.empty:
                        # reverse so oldest -> newest for trend math
                        return list(s.values)[::-1]
            return None

        revenue = row(fin, "Total Revenue", "TotalRevenue", "Operating Revenue")
        net_income = row(fin, "Net Income", "NetIncome", "Net Income Common Stockholders")
        fcf = row(cf, "Free Cash Flow", "FreeCashFlow")

        if revenue and len(revenue) >= 2:
            out["years_of_data"] = len(revenue)
            first, last = revenue[0], revenue[-1]
            n = len(revenue) - 1
            if first and first > 0 and last and last > 0:
                out["revenue_cagr"] = ((last / first) ** (1 / n) - 1) * 100
            # consistency: fraction of year-over-year periods that grew
            ups = sum(1 for i in range(1, len(revenue)) if revenue[i] > revenue[i - 1])
            out["revenue_growth_consistency"] = ups / (len(revenue) - 1) * 100

        if net_income and len(net_income) >= 2:
            out["earnings_positive_years"] = sum(1 for x in net_income if x > 0) / len(net_income) * 100
            first, last = net_income[0], net_income[-1]
            n = len(net_income) - 1
            if first and first > 0 and last and last > 0:
                out["earnings_cagr"] = ((last / first) ** (1 / n) - 1) * 100
            # stability = 1 - (stdev/|mean|), clamped to 0-100; higher = steadier
            mean = sum(net_income) / len(net_income)
            if mean != 0:
                var = sum((x - mean) ** 2 for x in net_income) / len(net_income)
                cv = (var ** 0.5) / abs(mean)
                out["earnings_stability"] = max(0.0, min(100.0, (1 - cv) * 100))

        if fcf and len(fcf) >= 1:
            out["fcf_positive_years"] = sum(1 for x in fcf if x > 0) / len(fcf) * 100

    except Exception as e:
        print(f"[warn] multiyear metrics failed: {e}")

    return out


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

        # Multi-year durability signals (Trendlyne models earnings "over time")
        my = compute_multiyear_metrics(t)

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

            # Multi-year durability signals (modeled over time)
            "revenue_cagr": my["revenue_cagr"],
            "earnings_cagr": my["earnings_cagr"],
            "revenue_growth_consistency": my["revenue_growth_consistency"],
            "earnings_positive_years": my["earnings_positive_years"],
            "fcf_positive_years": my["fcf_positive_years"],
            "earnings_stability": my["earnings_stability"],
            "years_of_data": my["years_of_data"],

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


def apply_eligibility(df: pd.DataFrame) -> pd.DataFrame:
    """
    Replicate Trendlyne's published eligibility rules for the durability
    score (the ones that are objective and computable from our data).
    Ineligible stocks get a null durability_score rather than a misleading
    number. Note: Trendlyne's revenue/market-cap thresholds are in INR
    crore for Indian stocks; for US stocks we apply analogous USD floors.
    """
    def eligible(row):
        # market cap floor (USD ~ $100M, analogous to their Rs 50cr India floor)
        mc = row.get("market_cap")
        if mc is not None and mc < 100_000_000:
            return False
        return True

    df["durability_eligible"] = df.apply(eligible, axis=1)
    return df


def compute_scores(df: pd.DataFrame) -> pd.DataFrame:
    # ===== Durability =====
    # Trendlyne models durability "over time" with emphasis on consistent
    # growth, stable earnings/cashflows and low debt. We therefore split
    # durability into two halves and weight them roughly equally:
    #   (a) current financial-health snapshot
    #   (b) multi-year growth consistency & stability
    #
    # --- (a) snapshot health sub-scores ---
    df["s_debt_to_equity"] = percentile_rank_series(df["debt_to_equity"], higher_is_better=False)
    df["s_current_ratio"] = percentile_rank_series(df["current_ratio"], higher_is_better=True)
    df["s_roe"] = percentile_rank_series(df["return_on_equity"], higher_is_better=True)
    df["s_profit_margin"] = percentile_rank_series(df["profit_margin"], higher_is_better=True)
    df["s_fcf_positive"] = df["free_cashflow"].apply(
        lambda x: 100.0 if (x is not None and x > 0) else (0.0 if x is not None else float("nan"))
    )
    snapshot_components = [
        "s_debt_to_equity", "s_current_ratio", "s_roe", "s_profit_margin", "s_fcf_positive"
    ]
    df["durability_snapshot"] = df[snapshot_components].mean(axis=1, skipna=True)

    # --- (b) multi-year consistency / trend sub-scores ---
    # Some of these are already 0-100 "fraction of years" style metrics;
    # CAGR figures get percentile-ranked across the universe.
    df["s_rev_cagr"] = percentile_rank_series(df["revenue_cagr"], higher_is_better=True)
    df["s_eps_cagr"] = percentile_rank_series(df["earnings_cagr"], higher_is_better=True)
    # already-normalized 0-100 metrics used directly:
    df["s_rev_consistency"] = df["revenue_growth_consistency"]
    df["s_earnings_positive"] = df["earnings_positive_years"]
    df["s_fcf_streak"] = df["fcf_positive_years"]
    df["s_earnings_stability"] = df["earnings_stability"]
    trend_components = [
        "s_rev_cagr", "s_eps_cagr", "s_rev_consistency",
        "s_earnings_positive", "s_fcf_streak", "s_earnings_stability"
    ]
    df["durability_trend"] = df[trend_components].mean(axis=1, skipna=True)

    # --- combine: 45% snapshot, 55% multi-year (tilts toward "over time") ---
    def blend_durability(row):
        snap = row["durability_snapshot"]
        trend = row["durability_trend"]
        snap_ok = pd.notnull(snap)
        trend_ok = pd.notnull(trend)
        if snap_ok and trend_ok:
            return 0.45 * snap + 0.55 * trend
        if snap_ok:
            return snap
        if trend_ok:
            return trend
        return float("nan")

    df["durability_score"] = df.apply(blend_durability, axis=1).round(1)

    # ===== Valuation (lower multiple = better = higher score) =====
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

    # Apply durability eligibility: ineligible -> null score
    if "durability_eligible" in df.columns:
        df.loc[~df["durability_eligible"], "durability_score"] = float("nan")

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

    df = apply_eligibility(df)
    df = compute_scores(df)

    output_cols = [
        "ticker", "name", "sector", "industry", "price", "market_cap",
        "durability_score", "valuation_score", "momentum_score",
        "durability_snapshot", "durability_trend",
        "trailing_pe", "forward_pe", "price_to_book", "ev_to_ebitda", "peg_ratio", "price_to_fcf",
        "debt_to_equity", "current_ratio", "return_on_equity", "profit_margin", "free_cashflow",
        "revenue_cagr", "earnings_cagr", "revenue_growth_consistency",
        "earnings_positive_years", "fcf_positive_years", "earnings_stability", "years_of_data",
        "ma50", "ma200", "rsi14", "return_3m", "return_6m", "pct_from_52w_high",
        "s_debt_to_equity", "s_current_ratio", "s_roe", "s_profit_margin", "s_fcf_positive",
        "s_rev_cagr", "s_eps_cagr", "s_rev_consistency", "s_earnings_positive",
        "s_fcf_streak", "s_earnings_stability",
        "s_pe", "s_pb", "s_ev_ebitda", "s_peg", "s_p_fcf",
        "s_above_ma50", "s_above_ma200", "s_rsi", "s_return_3m", "s_return_6m", "s_near_52w_high",
        "fetched_at",
    ]
    df_out = df[[c for c in output_cols if c in df.columns]]
    records_out = df_out.to_dict(orient="records")

    def sanitize(value):
        """Recursively replace NaN/Inf (which json.dump would otherwise emit
        as the invalid tokens NaN/Infinity) with None, so the output is
        strict, browser-parseable JSON."""
        if isinstance(value, dict):
            return {k: sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [sanitize(v) for v in value]
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        # numpy scalar types (e.g. numpy.float64('nan')) behave like floats
        # for isnan but aren't `isinstance(..., float)` on all platforms —
        # catch them via a duck-typed float() conversion as a fallback.
        try:
            if hasattr(value, "item"):
                native = value.item()
                if isinstance(native, float) and (math.isnan(native) or math.isinf(native)):
                    return None
                return native
        except (TypeError, ValueError):
            pass
        return value

    records_out = [sanitize(r) for r in records_out]

    output = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "universe_size": len(records_out),
        "stocks": records_out,
    }

    # allow_nan=False makes json.dump raise loudly here (caught early, in CI)
    # instead of silently writing invalid tokens that only fail later in the
    # browser — fail fast if sanitize() ever misses a value.
    with open("data.json", "w") as f:
        json.dump(output, f, indent=2, default=str, allow_nan=False)

    print(f"Wrote data.json with {len(records_out)} stocks.")


if __name__ == "__main__":
    main()
