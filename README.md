# NVDA Multi-Timeframe Rule of Thirds

This repo creates a GitHub Pages dashboard for NVIDIA stock (`NVDA`) using the Rule of Thirds calculation on these closed candle timeframes:

- 15 minutes
- 30 minutes
- 1 hour
- 4 hours

The page does **not** embed a graph. It only shows the Rule of Thirds results.

## Calculation

For each candle:

```text
range = high - low
one_third = range / 3
level_1 = low + one_third
level_2_middle = level_1 + one_third
level_3_high_average = level_2_middle + one_third
```

## Data source

The script uses Yahoo Finance chart data for NVDA regular-session candles.

For 4-hour candles, the script builds regular-session blocks from 60-minute data:

```text
09:30–13:30 ET
13:30–16:00 ET
```

The second block is shorter because the regular U.S. stock session closes at 4:00 PM ET.

## GoCharting

No GoCharting chart is embedded because you asked for results only. If you want a button that opens your GoCharting NVDA chart, edit the workflow and add your shared chart URL here:

```yaml
run: python calculate_rule_of_thirds.py --symbol NVDA --days 10 --recent-candles 10 --gocharting-url "PASTE_YOUR_GOCHARTING_LINK_HERE"
```

## Files generated

The workflow updates:

```text
index.html
results/latest.md
results/last_results.md
results/history.csv
results/data.json
```

## Setup

1. Create a new GitHub repo, for example:

```text
NVDA-Multi-Timeframe-Rule-of-Thirds
```

2. Upload the extracted files and folders.
3. Make sure the workflow file exists exactly here:

```text
.github/workflows/daily-rule-of-thirds.yml
```

4. Go to **Settings → Pages**.
5. Set Pages to **Deploy from branch → main → root**.
6. Go to **Actions → NVDA Multi-Timeframe Rule of Thirds → Run workflow**.

After the workflow succeeds, refresh your GitHub Pages site.
