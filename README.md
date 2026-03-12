# Macro Early Warning Dashboard

A professional macroeconomic monitoring dashboard that tracks Federal Reserve FRED data, classifies macro regimes, and generates tradable signals.

---

## What It Does

- Pulls 15 FRED indicators across labor, credit, financial conditions, lending, rates, and liquidity
- Computes a **z-score** for each indicator relative to 10-year history
- Runs a **logistic recession probability model** (0–100%)
- Classifies the economy into one of four **macro regimes**
- Generates **tradable signals** (bullish / bearish assets per regime)
- Renders everything in a static web dashboard updated weekly

---

## Architecture

```
FRED API
  ↓
scripts/update_macro.py   ← runs weekly via GitHub Actions
  ↓
public/macro.json         ← committed back to repo
  ↓
public/index.html         ← static dashboard (GitHub Pages)
public/dashboard.js       ← Chart.js + SVG gauge rendering
public/style.css
```

No server. No database. Fully static after the weekly data refresh.

---

## Dashboard Sections

| Section | Description |
|---------|-------------|
| **Current Conditions** | Composite health score (0–10), coincident indicators, key drivers |
| **Forward Recession Risk** | 6–12 month leading-indicator risk score, label, and drivers |
| **Deterioration Speed** | Rate of change across indicators; flags rapid multi-factor deterioration |
| **Recession Confirmation** | Coincident model confirming whether recession has started |
| **Global Liquidity** | Fed balance sheet + RRP + TGA composite, 1M and 3M change |
| **Macro Regime Panel** | Current regime name, risk score, triggered signals |
| **Recession Probability Gauge** | SVG semicircle gauge, 0–100%, logistic model output |
| **Credit Stress Level** | Low / Medium / High, bar indicator |
| **Liquidity Regime** | Loose / Neutral / Tight based on Fed balance sheet z-scores |
| **Credit Impulse** | Rate of change in revolving consumer credit |
| **Tradable Signals** | Auto-generated bullish/bearish asset calls |
| **Indicator Monitor** | Table with latest, period change, 3M trend, z-score bar |
| **Historical Charts** | Chart.js line charts, last 2 years, per indicator |

---

## FRED Indicators

| Group | Series | Description |
|-------|--------|-------------|
| Labor | ICSA | Initial Jobless Claims |
| Labor | CCSA | Continuing Jobless Claims |
| Labor | AWHAETP | Avg Weekly Hours (Private) |
| Credit | DRCCLACBS | Credit Card Delinquency Rate |
| Credit | DRCLACBS | Consumer Loan Delinquency Rate |
| Credit | REVOLSL | Revolving Consumer Credit |
| Financial | BAMLH0A0HYM2 | HY Option-Adjusted Spread |
| Financial | NFCI | National Financial Conditions Index |
| Lending | DRTSCLCC | Bank Tightening Standards – Credit Cards |
| Rates | DGS2 | 2-Year Treasury Yield |
| Rates | DGS10 | 10-Year Treasury Yield |
| Rates | T10Y2Y | 10Y–2Y Yield Curve Spread |
| Liquidity | WALCL | Fed Balance Sheet (Total Assets) |
| Liquidity | RRPONTSYD | Overnight Reverse Repo (RRP) |
| Liquidity | WTREGEN | Treasury General Account (TGA) |

---

## Macro Regime Classification

The regime is determined by a weighted risk-point scoring system:

| Signal | Threshold | Points |
|--------|-----------|--------|
| Initial claims | > 280,000 | +1 |
| Claims z-score | > 1.0 (elevated vs history) | +1 |
| Avg weekly hours z-score | < -1.0 | +1 |
| HY spread | > 500 bps | +2 |
| HY spread | > 400 bps | +1 |
| NFCI | > 0.5 (tightening) | +1 |
| Yield curve | < 0 (inverted) | +2 |
| Yield curve | < 0.5% (flat) | +1 |
| CC delinquency | > 3.0% | +1 |
| Lending standards z-score | > 1.0 | +1 |

| Score | Regime |
|-------|--------|
| 0–2 | Expansion |
| 3–4 | Slowdown |
| 5–6 | Late Cycle |
| 7+ | Recession Risk |

---

## Recession Probability Model

Simple logistic model using z-scores of five key inputs:

```
P(recession) = logistic( 0.30·z_ICSA
                        - 0.25·z_T10Y2Y
                        + 0.20·z_BAMLH0A0HYM2
                        + 0.15·z_NFCI
                        - 0.10·z_AWHAETP
                        - 1.5 )  × 100
```

- Base rate (all z=0): ~18%
- At moderate stress (all z=1.5): ~45%
- At severe stress (all z=3.0): ~79%

---

## Project Structure

```
macro-dashboard/
├── public/
│   ├── index.html        # Dashboard HTML shell
│   ├── style.css         # Dark professional theme
│   ├── dashboard.js      # All rendering logic (gauge, charts, signals, table)
│   └── macro.json        # Generated weekly by Python script
├── scripts/
│   └── update_macro.py   # FRED fetcher + model + signal generator
├── .github/
│   └── workflows/
│       ├── deploy-pages.yml  # GitHub Pages deployment
│       └── update.yml        # Weekly data refresh cron
├── requirements.txt
└── README.md
```

---

## Setup & Deployment

### 1. Get a FRED API Key

Register free at [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html).

### 2. Run Locally

```bash
# Install Python dependencies
pip install -r requirements.txt

# Set your API key
export FRED_API_KEY=your_key_here

# Pull FRED data and write public/macro.json
python scripts/update_macro.py

# Preview dashboard (Python built-in server)
cd public && python -m http.server 8080
# open http://localhost:8080
```

### 3. Deploy to GitHub Pages

1. Push this repo to GitHub
2. Go to repo **Settings** → **Pages**
3. Under **Source**, select **GitHub Actions**
4. The `deploy-pages.yml` workflow will run on every push to `main` and publish the `public/` directory

Your site is live at `https://<username>.github.io/<repo>/`.

### 4. Enable the Weekly GitHub Actions Cron

The script needs your FRED API key as a GitHub Actions secret:

1. GitHub repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
   - Name: `FRED_API_KEY`
   - Value: your FRED API key
3. Save

The workflow (`.github/workflows/update.yml`) runs every **Friday at 16:00 UTC** (11:00 AM ET). This timing catches weekly jobless claims (Thursday 8:30 AM ET), the Fed H.4.1 balance sheet release (Thursday 4:30 PM ET), and the NFCI (Friday 8:30 AM ET). It:
- Fetches fresh FRED data
- Runs the Python model
- Commits updated `public/macro.json` to `main`
- GitHub Pages auto-redeploys via the `deploy-pages.yml` workflow triggered on push

You can also trigger it manually: **Actions** tab → **Weekly Macro Data Update** → **Run workflow**.

---

## Tradable Signals Logic

| Regime | Bullish | Bearish |
|--------|---------|---------|
| Expansion | Equities (SPY), Small Caps (IWM), Cyclicals (XLI), Commodities | Long Duration (TLT), Defensives (XLU) |
| Slowdown | IG Bonds (LQD), Healthcare (XLV), Staples (XLP) | Cyclicals (XLY), Small Caps, Energy (XLE) |
| Late Cycle | Long Duration (TLT), Gold (GLD), Utilities (XLU) | HY Credit (HYG), Banks (XLF), Small Caps |
| Recession Risk | Long Duration (TLT), Gold (GLD), USD (UUP) | HY Credit (HYG), Small Caps (IWM), Commodities |

---

## Disclaimer

This dashboard is for informational and educational purposes only. It is not financial advice. All data is sourced from the Federal Reserve Bank of St. Louis (FRED). Macro signals are rule-based heuristics, not a trading system.
