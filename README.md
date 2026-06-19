# S&P 500 Durability / Valuation / Momentum Dashboard

Fully free, fully automated. Data refreshes every Saturday via GitHub Actions;
the dashboard (a Claude artifact) reads the latest `data.json` straight from
this repo whenever you open it.

## One-time setup (~15-20 minutes)

1. **Create a new GitHub repository** (public — needed so the dashboard can
   fetch `data.json` without authentication). Name it anything, e.g.
   `sp500-dashboard`.

2. **Upload these files** to the repo, keeping the folder structure:
   - `fetch_data.py`
   - `index.html`
   - `.github/workflows/update-data.yml`
   - `README.md` (this file)

   Easiest way: on GitHub, click "Add file" → "Upload files", drag all of
   them (keep the `.github/workflows/` folder structure intact — GitHub's
   uploader preserves folder paths if you drag the whole folder).

3. **Run the data workflow once manually** to generate the first
   `data.json`:
   - Go to the repo → "Actions" tab → click the "Update S&P 500 Dashboard
     Data" workflow → click "Run workflow" button → confirm.
   - Takes roughly 5-10 minutes to fetch ~500 tickers. Watch the log to
     confirm it completes and commits `data.json`.

4. **Turn on GitHub Pages** so `index.html` becomes a real public webpage:
   - Repo → "Settings" tab → "Pages" (left sidebar) → under "Build and
     deployment", set Source to "Deploy from a branch" → Branch: `main`,
     folder: `/ (root)` → Save.
   - GitHub will show you the live URL after a minute or two, something
     like `https://<your-username>.github.io/<repo-name>/`. Bookmark this —
     it's your permanent dashboard link, open it in any browser anytime.

5. **Connect the page to your data**:
   - Open your GitHub Pages URL from step 4.
   - In the "Connect your data source" field, just enter your repo as
     **`owner/repo`** (e.g. `Yushian7/sp500-dashboard`) and click "Load
     data". You do NOT need to paste a long jsdelivr URL anymore.
   - The dashboard automatically asks GitHub for the latest commit and
     loads the freshest `data.json` every time — no cache issues, no manual
     URL/SHA updates ever. It remembers your repo for next time.
   - (A full jsdelivr or GitHub URL still works if you paste one, for
     backward compatibility.)

6. **Done.** Bookmark the GitHub Pages URL. The data workflow runs
   automatically every Saturday; the page always auto-resolves and loads
   the newest committed data when you open it or hit refresh.

## Re-running manually anytime

Repo → Actions tab → "Update S&P 500 Dashboard Data" → "Run workflow".
Useful if you want fresher data before the next scheduled Saturday run.

## Changing the schedule

Edit the `cron` line in `.github/workflows/update-data.yml`. Current setting
is every Saturday 06:00 UTC. Cron format: `minute hour day month weekday`.

## Notes on data quality

- **Quality-Value score (the headline metric):** combines durability (60%)
  and valuation (40%) so the top of the list is durable businesses trading
  cheap — your stated goal. A cheap-but-deteriorating stock (value trap) or
  a great-but-expensive stock both get pulled down because they only score
  on one dimension. Sort by this and/or set the "Min quality-value" slider.
- **Durability now has three parts:** 35% current financial-health snapshot
  (debt/equity, current ratio, ROE, margin, FCF), 40% multi-year trend
  (revenue/earnings CAGR, growth consistency, profitable & FCF-positive
  years, earnings stability), and 25% earnings-quality/solvency from SEC
  EDGAR filings (Piotroski F-Score, Altman Z-Score, accruals ratio).
- **SEC EDGAR data (free, keyless):** the script pulls each company's
  filings from data.sec.gov to compute the Piotroski F-Score (a 0-9 quality
  score — higher means improving fundamentals) and Altman Z-Score (bankruptcy
  risk — above 3 is safe, below 1.8 is distress). These are the strongest
  free value-trap filters available and are the main reason scores now track
  closer to Trendlyne's quality judgment.
- Fundamentals (valuation, snapshot health) come from Yahoo Finance via
  `yfinance`. Missing fields are excluded from a stock's score rather than
  treated as zero.
- Trendlyne's published score bands (reference): Durability good ≥55 / bad
  <35; Valuation good ≥50 / bad <30; Momentum good ≥59 / bad <30. Their exact
  formula is proprietary, so absolute numbers still differ — but ranking and
  direction are much closer now.
- **Fetch time is ~25-35 min** for the full S&P 500 because each stock now
  makes yfinance calls plus one SEC EDGAR filing pull. Fine for the weekly
  GitHub Actions run; well within free-tier limits and SEC's 10 req/sec rule.
- Scores are percentile ranks within the current S&P 500 universe.

## Optional: add analyst estimates (forward P/E, price targets)

This requires a free API key (unlike the keyless sources above), so it's not
wired in by default. If you want it later: sign up for a free tier at
Financial Modeling Prep, Finnhub, or Alpha Vantage, add the key as a GitHub
repo secret (Settings → Secrets and variables → Actions → New repository
secret), and we can extend `fetch_data.py` to pull consensus estimates and
fold a forward-looking valuation signal into the score. Free tiers are rate-
limited (250 calls/day on FMP), so estimates would refresh on a slower
cadence than the weekly price/fundamental data. Ask and we'll add it.
