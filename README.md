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

- Fundamentals (Durability, Valuation) come from Yahoo Finance via the
  `yfinance` library — generally reliable but occasionally a field is
  missing for a given company (e.g., REITs/financials report some ratios
  differently). Missing values are excluded from that stock's score average
  rather than treated as zero.
- **Durability methodology** (updated to track Trendlyne more closely):
  the score now blends two halves — 45% a current financial-health snapshot
  (debt/equity, current ratio, ROE, profit margin, FCF-positive) and 55% a
  multi-year consistency & trend block (revenue CAGR, earnings CAGR, revenue
  growth consistency, % of profitable years, % of FCF-positive years, and
  earnings stability). This mirrors Trendlyne's emphasis on modeling
  earnings "over time," so a company with a clean current balance sheet but
  erratic multi-year earnings (common for biotech/pharma) now scores lower,
  closer to Trendlyne's reading. Trendlyne's exact formula and weights are
  proprietary and not published, so scores will still differ in absolute
  terms — but the ranking logic and direction are much closer now.
- Trendlyne's published score bands (for reference when reading results):
  Durability good ≥55 / bad <35; Valuation good ≥50 / bad <30; Momentum
  good ≥59 / bad <30.
- Eligibility: stocks under ~$100M market cap get a null durability score,
  matching Trendlyne's practice of not scoring companies it can't reliably
  validate.
- **Fetch time is now longer** (~15-25 min for the full S&P 500) because the
  script pulls annual income-statement and cashflow history per stock for
  the multi-year metrics, not just the quick info snapshot. This is well
  within GitHub Actions' free tier for a weekly run.
- Momentum uses 1 year of daily price history to compute RSI, moving
  averages, and trailing returns.
- Scores are **percentile ranks within the current S&P 500 universe** — e.g.
  a Durability score of 80 means "ranks better than 80% of the S&P 500 on
  these metrics," not an absolute pass/fail threshold.
