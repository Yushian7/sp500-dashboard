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


# ---------------------------------------------------------------------------
# SEC EDGAR integration (free, keyless) for Piotroski F-Score, Altman Z-Score,
# and an accruals-based earnings-quality flag. These power a value-trap-aware
# quality dimension that yfinance alone can't provide.
# ---------------------------------------------------------------------------

SEC_HEADERS = {"User-Agent": "sp500-dashboard research contact@example.com"}
_CIK_MAP = None


def load_cik_map() -> dict:
    """Ticker -> 10-digit zero-padded CIK, from SEC's official mapping file."""
    global _CIK_MAP
    if _CIK_MAP is not None:
        return _CIK_MAP
    try:
        req = urllib.request.Request(
            "https://www.sec.gov/files/company_tickers.json", headers=SEC_HEADERS
        )
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        m = {}
        for v in data.values():
            m[str(v["ticker"]).upper()] = str(v["cik_str"]).zfill(10)
        _CIK_MAP = m
        print(f"Loaded {len(m)} ticker->CIK mappings from SEC.")
    except Exception as e:
        print(f"[warn] could not load CIK map: {e}")
        _CIK_MAP = {}
    return _CIK_MAP


def _annual_values(facts: dict, tags: list[str], max_years: int = 2) -> list:
    """
    Return up to `max_years` most-recent annual values (10-K / full-year) for
    the first matching XBRL tag. Values are ordered newest-first.
    """
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        if tag not in usgaap:
            continue
        units = usgaap[tag].get("units", {})
        for unit_key in ("USD", "shares", "USD/shares", "pure"):
            if unit_key not in units:
                continue
            annual = {}
            for r in units[unit_key]:
                if r.get("form") == "10-K" and r.get("fp") == "FY" and "fy" in r:
                    fy = r["fy"]
                    if fy not in annual or r.get("filed", "") > annual[fy].get("filed", ""):
                        annual[fy] = r
            if annual:
                years = sorted(annual.keys(), reverse=True)
                return [annual[y].get("val") for y in years[:max_years]]
    return []


def compute_edgar_scores(ticker: str, market_cap) -> dict:
    """Fetch EDGAR companyfacts once and compute Piotroski F, Altman Z, accruals."""
    out = {"piotroski_f": None, "altman_z": None, "accruals_ratio": None}
    cik_map = load_cik_map()
    cik = cik_map.get(ticker.upper())
    if not cik:
        return out
    try:
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        req = urllib.request.Request(url, headers=SEC_HEADERS)
        facts = json.loads(urllib.request.urlopen(req, timeout=40).read())
    except Exception as e:
        print(f"[warn] EDGAR facts failed for {ticker} (CIK {cik}): {e}")
        return out

    def v(tags, n=2):
        return _annual_values(facts, tags, n)

    net_income = v(["NetIncomeLoss"])
    op_cf = v(["NetCashProvidedByUsedInOperatingActivities",
               "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"])
    total_assets = v(["Assets"])
    cur_assets = v(["AssetsCurrent"])
    cur_liab = v(["LiabilitiesCurrent"])
    total_liab = v(["Liabilities"])
    lt_debt = v(["LongTermDebtNoncurrent", "LongTermDebt"])
    revenue = v(["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                 "SalesRevenueNet"])
    gross_profit = v(["GrossProfit"])
    shares = v(["CommonStockSharesOutstanding",
                "WeightedAverageNumberOfDilutedSharesOutstanding",
                "WeightedAverageNumberOfSharesOutstandingBasic"])
    retained = v(["RetainedEarningsAccumulatedDeficit"])
    ebit = v(["OperatingIncomeLoss"])

    # ---- Piotroski F-Score (0-9) ----
    try:
        if len(total_assets) >= 2 and total_assets[0] and total_assets[1]:
            score = 0
            if net_income and net_income[0] is not None and net_income[0] / total_assets[0] > 0:
                score += 1
            if op_cf and op_cf[0] is not None and op_cf[0] > 0:
                score += 1
            if (len(net_income) >= 2 and net_income[0] is not None and net_income[1] is not None
                    and (net_income[0] / total_assets[0]) > (net_income[1] / total_assets[1])):
                score += 1
            if (op_cf and net_income and op_cf[0] is not None and net_income[0] is not None
                    and op_cf[0] > net_income[0]):
                score += 1
            if (len(lt_debt) >= 2 and lt_debt[0] is not None and lt_debt[1] is not None
                    and (lt_debt[0] / total_assets[0]) <= (lt_debt[1] / total_assets[1])):
                score += 1
            if (len(cur_assets) >= 2 and len(cur_liab) >= 2 and cur_liab[0] and cur_liab[1]
                    and (cur_assets[0] / cur_liab[0]) > (cur_assets[1] / cur_liab[1])):
                score += 1
            if len(shares) >= 2 and shares[0] is not None and shares[1] is not None and shares[0] <= shares[1] * 1.01:
                score += 1
            if (len(gross_profit) >= 2 and len(revenue) >= 2 and revenue[0] and revenue[1]
                    and (gross_profit[0] / revenue[0]) > (gross_profit[1] / revenue[1])):
                score += 1
            if (len(revenue) >= 2 and (revenue[0] / total_assets[0]) > (revenue[1] / total_assets[1])):
                score += 1
            out["piotroski_f"] = score

        if (op_cf and net_income and total_assets and op_cf[0] is not None
                and net_income[0] is not None and total_assets[0]):
            out["accruals_ratio"] = (net_income[0] - op_cf[0]) / total_assets[0]
    except Exception as e:
        print(f"[warn] Piotroski calc error {ticker}: {e}")

    # ---- Altman Z-Score ----
    # Z = 1.2*(WC/TA) + 1.4*(RE/TA) + 3.3*(EBIT/TA) + 0.6*(MktCap/TL) + 1.0*(Rev/TA)
    try:
        ta = total_assets[0] if total_assets else None
        if ta and market_cap and total_liab and total_liab[0]:
            wc = (cur_assets[0] - cur_liab[0]) if (cur_assets and cur_liab) else None
            re = retained[0] if retained else None
            eb = ebit[0] if ebit else None
            rev = revenue[0] if revenue else None
            if None not in (wc, re, eb, rev):
                z = (1.2 * (wc / ta) + 1.4 * (re / ta) + 3.3 * (eb / ta)
                     + 0.6 * (market_cap / total_liab[0]) + 1.0 * (rev / ta))
                out["altman_z"] = round(z, 2)
    except Exception as e:
        print(f"[warn] Altman calc error {ticker}: {e}")

    return out


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
        # recent-trajectory / deterioration signals
        "recent_rev_growth": None,      # latest year revenue YoY %
        "recent_earnings_growth": None, # latest year earnings YoY %
        "rev_trend_breaking": None,     # latest growth vs historical avg growth (negative = decelerating)
        "earnings_declining_recent": None,  # 1 if latest-year earnings fell, else 0
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
            # --- recent revenue trajectory: latest year YoY ---
            if revenue[-2] and revenue[-2] != 0:
                out["recent_rev_growth"] = (revenue[-1] / revenue[-2] - 1) * 100
            # --- trend breaking: latest YoY vs avg of earlier YoY growths ---
            yoy = [(revenue[i] / revenue[i - 1] - 1) * 100
                   for i in range(1, len(revenue)) if revenue[i - 1]]
            if len(yoy) >= 2:
                latest_yoy = yoy[-1]
                hist_avg = sum(yoy[:-1]) / len(yoy[:-1])
                out["rev_trend_breaking"] = latest_yoy - hist_avg  # negative = decelerating vs own history

        if net_income and len(net_income) >= 2:
            out["earnings_positive_years"] = sum(1 for x in net_income if x > 0) / len(net_income) * 100
            first, last = net_income[0], net_income[-1]
            n = len(net_income) - 1
            if first and first > 0 and last and last > 0:
                out["earnings_cagr"] = ((last / first) ** (1 / n) - 1) * 100
            # --- recent earnings trajectory: latest year YoY ---
            if net_income[-2] and net_income[-2] != 0:
                out["recent_earnings_growth"] = (net_income[-1] / abs(net_income[-2]) - 1) * 100 if net_income[-2] > 0 else None
            out["earnings_declining_recent"] = 1 if net_income[-1] < net_income[-2] else 0
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


def compute_quarterly_metrics(t: "yf.Ticker") -> dict:
    """
    Quarterly trajectory signals — catch deterioration months before annual
    statements reflect it. All comparisons are YoY-quarter (same quarter a
    year earlier) to control for seasonality, which is the correct way to
    read quarterly results and avoids false alarms from normal seasonal dips.
    """
    out = {
        "q_rev_growth_yoy": None,        # latest quarter revenue vs same quarter last year
        "q_earnings_growth_yoy": None,   # latest quarter net income vs same quarter last year
        "q_rev_decelerating": None,      # 1 if latest QoQ-YoY growth < prior quarter's YoY growth
        "q_earnings_negative": None,     # 1 if latest quarter posted a net loss
        "q_margin_compressing": None,    # 1 if latest quarter net margin < year-ago quarter's
        "quarters_of_data": None,
    }
    try:
        qf = t.quarterly_financials  # columns = quarters, newest first
        if qf is None or qf.empty:
            return out

        def qrow(df, *names):
            for n in names:
                if n in df.index:
                    s = df.loc[n].dropna()
                    if not s.empty:
                        # newest-first in yfinance; keep that order here
                        return list(s.values)
            return None

        revenue = qrow(qf, "Total Revenue", "TotalRevenue", "Operating Revenue")
        net_income = qrow(qf, "Net Income", "NetIncome", "Net Income Common Stockholders")

        # Need at least 5 quarters to do YoY-quarter comparisons for the
        # latest two quarters (q0 vs q4, q1 vs q5).
        if revenue:
            out["quarters_of_data"] = len(revenue)

        # YoY-quarter revenue growth: latest quarter (index 0) vs 4 quarters ago (index 4)
        if revenue and len(revenue) >= 5 and revenue[4]:
            out["q_rev_growth_yoy"] = (revenue[0] / revenue[4] - 1) * 100
            # prior quarter's YoY growth: index 1 vs index 5 (needs >=6)
            if len(revenue) >= 6 and revenue[5]:
                prev_yoy = (revenue[1] / revenue[5] - 1) * 100
                out["q_rev_decelerating"] = 1 if out["q_rev_growth_yoy"] < prev_yoy else 0

        if net_income and len(net_income) >= 5 and net_income[4]:
            if net_income[4] > 0:
                out["q_earnings_growth_yoy"] = (net_income[0] / abs(net_income[4]) - 1) * 100
            out["q_earnings_negative"] = 1 if net_income[0] < 0 else 0

        # Margin compression: latest quarter net margin vs year-ago quarter
        if (revenue and net_income and len(revenue) >= 5 and len(net_income) >= 5
                and revenue[0] and revenue[4]):
            margin_now = net_income[0] / revenue[0]
            margin_year_ago = net_income[4] / revenue[4]
            out["q_margin_compressing"] = 1 if margin_now < margin_year_ago else 0

    except Exception as e:
        print(f"[warn] quarterly metrics failed: {e}")

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

        # Quarterly trajectory signals (catch deterioration early, YoY-quarter)
        q = compute_quarterly_metrics(t)

        # SEC EDGAR quality scores (Piotroski F, Altman Z, accruals)
        mc_for_edgar = safe_get(info, "marketCap")
        edgar = compute_edgar_scores(ticker, mc_for_edgar)

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
            "recent_rev_growth": my["recent_rev_growth"],
            "recent_earnings_growth": my["recent_earnings_growth"],
            "rev_trend_breaking": my["rev_trend_breaking"],
            "earnings_declining_recent": my["earnings_declining_recent"],

            # Quarterly trajectory (YoY-quarter, seasonally correct)
            "q_rev_growth_yoy": q["q_rev_growth_yoy"],
            "q_earnings_growth_yoy": q["q_earnings_growth_yoy"],
            "q_rev_decelerating": q["q_rev_decelerating"],
            "q_earnings_negative": q["q_earnings_negative"],
            "q_margin_compressing": q["q_margin_compressing"],
            "quarters_of_data": q["quarters_of_data"],

            # SEC EDGAR quality scores
            "piotroski_f": edgar["piotroski_f"],
            "altman_z": edgar["altman_z"],
            "accruals_ratio": edgar["accruals_ratio"],

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

    # --- (c) EDGAR quality sub-scores (Piotroski 0-9 -> 0-100; Altman ranked) ---
    df["s_piotroski"] = df["piotroski_f"].apply(
        lambda x: (x / 9.0 * 100) if (x is not None and not (isinstance(x, float) and math.isnan(x))) else float("nan")
    )
    df["s_altman"] = percentile_rank_series(df["altman_z"], higher_is_better=True)
    # accruals: smaller/more-negative is better earnings quality (lower = better)
    df["s_accruals"] = percentile_rank_series(df["accruals_ratio"], higher_is_better=False)
    quality_components = ["s_piotroski", "s_altman", "s_accruals"]
    df["durability_quality"] = df[quality_components].mean(axis=1, skipna=True)

    # --- (d) recent-trajectory sub-scores (catches deterioration a value
    #         trap shows before the multi-year history reflects it) ---
    df["s_recent_rev"] = percentile_rank_series(df["recent_rev_growth"], higher_is_better=True)
    df["s_recent_eps"] = percentile_rank_series(df["recent_earnings_growth"], higher_is_better=True)
    df["s_trend_breaking"] = percentile_rank_series(df["rev_trend_breaking"], higher_is_better=True)
    # quarterly YoY growth sub-scores (earliest signal)
    df["s_q_rev"] = percentile_rank_series(df["q_rev_growth_yoy"], higher_is_better=True)
    df["s_q_eps"] = percentile_rank_series(df["q_earnings_growth_yoy"], higher_is_better=True)
    recent_components = ["s_recent_rev", "s_recent_eps", "s_trend_breaking", "s_q_rev", "s_q_eps"]
    df["durability_recent"] = df[recent_components].mean(axis=1, skipna=True)

    # --- combine: 25% snapshot, 30% multi-year trend, 20% EDGAR quality,
    #     25% recent trajectory. Recent trajectory now carries real weight so
    #     a deteriorating-but-historically-strong name (value trap) is pulled
    #     down rather than rewarded for its past. ---
    def blend_durability(row):
        parts, weights = [], []
        if pd.notnull(row["durability_snapshot"]):
            parts.append(row["durability_snapshot"]); weights.append(0.25)
        if pd.notnull(row["durability_trend"]):
            parts.append(row["durability_trend"]); weights.append(0.30)
        if pd.notnull(row["durability_quality"]):
            parts.append(row["durability_quality"]); weights.append(0.20)
        if pd.notnull(row["durability_recent"]):
            parts.append(row["durability_recent"]); weights.append(0.25)
        if not parts:
            return float("nan")
        base = sum(p * w for p, w in zip(parts, weights)) / sum(weights)

        # --- explicit deterioration penalty ---
        # Stacks hard red flags that a value trap exhibits. Each applies a
        # multiplicative haircut so multiple concurrent red flags compound,
        # mirroring how Trendlyne collapses durability for such names.
        # Quarterly flags are included because they're the earliest warning.
        penalty = 1.0
        if row.get("earnings_declining_recent") == 1:
            penalty *= 0.85  # latest fiscal-year earnings fell
        rb = row.get("rev_trend_breaking")
        if rb is not None and not (isinstance(rb, float) and math.isnan(rb)) and rb < -10:
            penalty *= 0.85  # annual revenue growth decelerating sharply vs own history
        reg = row.get("recent_earnings_growth")
        if reg is not None and not (isinstance(reg, float) and math.isnan(reg)) and reg < 0:
            penalty *= 0.85  # annual earnings shrinking outright
        pf = row.get("pct_from_52w_high")
        if pf is not None and not (isinstance(pf, float) and math.isnan(pf)) and pf < -35:
            penalty *= 0.90  # market pricing in trouble (deep below 52w high)
        # --- quarterly red flags (earliest signals) ---
        qeg = row.get("q_earnings_growth_yoy")
        if qeg is not None and not (isinstance(qeg, float) and math.isnan(qeg)) and qeg < 0:
            penalty *= 0.88  # latest quarter earnings down YoY
        qrg = row.get("q_rev_growth_yoy")
        if qrg is not None and not (isinstance(qrg, float) and math.isnan(qrg)) and qrg < 0:
            penalty *= 0.90  # latest quarter revenue down YoY
        if row.get("q_earnings_negative") == 1:
            penalty *= 0.88  # latest quarter posted a loss
        if row.get("q_margin_compressing") == 1:
            penalty *= 0.93  # net margin compressing YoY
        # floor the penalty so a single bad year can't zero out an otherwise
        # strong company (avoids over-penalizing cyclical troughs)
        penalty = max(penalty, 0.30)

        return base * penalty

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

    # ===== Composite Quality-Value score =====
    # Directly surfaces the "durable business at a cheap price" intersection.
    # 60% durability (quality) + 40% valuation (cheapness). A stock must score
    # on BOTH to rank highly — a cheap junk stock or an expensive great stock
    # both get pulled down. This is the headline score for the stated goal.
    def quality_value(row):
        d, val = row["durability_score"], row["valuation_score"]
        if pd.notnull(d) and pd.notnull(val):
            return round(0.60 * d + 0.40 * val, 1)
        return float("nan")

    df["quality_value_score"] = df.apply(quality_value, axis=1)

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

    # Preload SEC ticker->CIK map once so per-stock EDGAR calls are fast.
    load_cik_map()

    records = []
    for i, tk in enumerate(tickers):
        raw = fetch_ticker_raw(tk)
        if raw:
            records.append(raw)
        if (i + 1) % 25 == 0:
            print(f"  ...{i + 1}/{len(tickers)} done")
        # Each stock now makes a yfinance call set + one SEC EDGAR call.
        # SEC allows 10 req/sec; 0.4s/stock keeps us comfortably under that
        # and polite to Yahoo too.
        time.sleep(0.4)

    df = pd.DataFrame(records)
    print(f"Successfully fetched {len(df)} / {len(tickers)} tickers.")

    df = apply_eligibility(df)
    df = compute_scores(df)

    output_cols = [
        "ticker", "name", "sector", "industry", "price", "market_cap",
        "quality_value_score",
        "durability_score", "valuation_score", "momentum_score",
        "durability_snapshot", "durability_trend", "durability_quality", "durability_recent",
        "piotroski_f", "altman_z", "accruals_ratio",
        "trailing_pe", "forward_pe", "price_to_book", "ev_to_ebitda", "peg_ratio", "price_to_fcf",
        "debt_to_equity", "current_ratio", "return_on_equity", "profit_margin", "free_cashflow",
        "revenue_cagr", "earnings_cagr", "revenue_growth_consistency",
        "earnings_positive_years", "fcf_positive_years", "earnings_stability", "years_of_data",
        "recent_rev_growth", "recent_earnings_growth", "rev_trend_breaking", "earnings_declining_recent",
        "q_rev_growth_yoy", "q_earnings_growth_yoy", "q_rev_decelerating",
        "q_earnings_negative", "q_margin_compressing", "quarters_of_data",
        "ma50", "ma200", "rsi14", "return_3m", "return_6m", "pct_from_52w_high",
        "s_debt_to_equity", "s_current_ratio", "s_roe", "s_profit_margin", "s_fcf_positive",
        "s_rev_cagr", "s_eps_cagr", "s_rev_consistency", "s_earnings_positive",
        "s_fcf_streak", "s_earnings_stability",
        "s_recent_rev", "s_recent_eps", "s_trend_breaking", "s_q_rev", "s_q_eps",
        "s_piotroski", "s_altman", "s_accruals",
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
