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
   - `.github/workflows/update-data.yml`
   - `README.md` (this file)

   Easiest way: on GitHub, click "Add file" → "Upload files", drag all three
   (keep the `.github/workflows/` folder structure intact — GitHub's
   uploader preserves folder paths if you drag the whole folder).

3. **Run it once manually** to generate the first `data.json`:
   - Go to the repo → "Actions" tab → click the "Update S&P 500 Dashboard
     Data" workflow → click "Run workflow" button → confirm.
   - Takes roughly 5-10 minutes to fetch ~500 tickers. Watch the log to
     confirm it completes and commits `data.json`.

4. **Get the raw data URL** for the dashboard:
   - Once `data.json` exists in the repo, click on it, then click "Raw".
   - Copy that URL — it looks like:
     `https://raw.githubusercontent.com/<your-username>/<repo-name>/main/data.json`
   - Paste this URL into the dashboard artifact when prompted (there's a
     settings/URL field at the top of the dashboard).

5. **Done.** The workflow now runs automatically every Saturday and keeps
   `data.json` fresh. The dashboard always pulls the latest version when you
   open it.

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
- Momentum uses 1 year of daily price history to compute RSI, moving
  averages, and trailing returns.
- Scores are **percentile ranks within the current S&P 500 universe** —
  e.g., a Durability score of 80 means "healthier balance sheet than 80% of
  the S&P 500 right now," not an absolute pass/fail threshold. This mirrors
  how Trendlyne's scoring works.
- "Interest Coverage" is left as a placeholder (not populated) — Yahoo's
  `.info` endpoint doesn't reliably expose interest expense. Could be added
  later via the slower `.financials` statement pull if you want, at the
  cost of longer fetch times.
