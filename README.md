# FTD Cohort ROI Dashboard

A Streamlit dashboard reading directly from the `"Affilka ROI Dash"`
materialized view in Supabase - reproduces the "FTD Month (Actual ROI
Dash)" cohort LTV report, with filters by Partner ID / Campaign ID /
Commission ID.

No Docker needed for this deployment path - Streamlit Community Cloud
builds and runs the app directly from a GitHub repo.

## Deploying (one-time setup)

1. **Create a new GitHub repo** (can be public or private - Streamlit
   Community Cloud can deploy from either, though a private repo needs
   you to connect your GitHub account with repo access when you set up
   the deployment).

2. **Push these files to it**: `app.py`, `requirements.txt`,
   `.gitignore`, and this `README.md`. **Do NOT push a real
   `.streamlit/secrets.toml`** - only the `.streamlit/secrets.toml.example`
   template. The `.gitignore` here already protects against this, but
   worth double-checking before your first push.

3. **Go to [share.streamlit.io](https://share.streamlit.io)** and sign
   in with GitHub.

4. **Click "New app"**, select the repo you just created, branch
   `main`, and set the main file path to `app.py`.

5. **Before clicking Deploy**, open **"Advanced settings" -> Secrets**
   and paste in (with your real values, not the placeholders):

   ```toml
   db_password = "your-actual-supabase-db-password"
   dashboard_password = "a-password-you-choose-for-colleagues"
   ```

6. **Click Deploy.** After a minute or two you'll get a URL like
   `https://your-app-name.streamlit.app` - share this with colleagues,
   along with the `dashboard_password` you set above.

## Updating the dashboard later

Push a new commit to the repo's `main` branch - Streamlit Community
Cloud automatically redeploys on every push, usually within a minute.
No manual redeploy step needed.

## How the numbers are calculated

Every column in the table is one FTD-month cohort's activity **to
date** - not just that calendar month. A player who first deposited in
November 2025 contributes their August 2026 spend to the November 2025
column too, since they're still part of that cohort. This matches the
lifetime cohort LTV framing of the original spreadsheet prototype this
dashboard replaces.

A few formulas were deliberately changed from that original prototype
after review - see the comment at the top of `app.py` for the specific
changes and why (SB GGR now includes SB Correction consistently, Total
GGR no longer double-counts it, Rev Share sums by FTD Month rather than
Activity Month, and "Fixed Monthly Charge" replaces the old placeholder
"Fixed fee" row with an editable, persisted figure since it has no
source in the data at all).

## The "Fixed Monthly Charge" editor

This is the one figure on the dashboard with no source in Supabase -
it's a genuinely external, manually-entered monthly cost. The dashboard
lets you edit it per FTD month, and it's stored in a small dedicated
table (`"Dashboard Fixed Monthly Charge"`) in the same Supabase
database, so it persists across app restarts and is shared by everyone
viewing the dashboard (not per-browser/session).

## Filters

The sidebar lets you filter by Partner ID, Campaign ID, and Commission
ID (multi-select - leave blank to include everything). All the cohort
math recalculates against whatever subset of rows matches your current
filter selection.

## Data freshness

The dashboard caches its query against Supabase for 10 minutes, so
changing a filter won't re-hit the database every time - it'll reflect
new data in `"Affilka ROI Dash"` within 10 minutes of that view being
rebuilt (see `upload_gaming_data.py`, which rebuilds both materialized
views automatically at the end of every run).
