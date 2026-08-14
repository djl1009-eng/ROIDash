"""
FTD Cohort ROI Dashboard
=========================
Streamlit dashboard replicating the "FTD Month (Actual ROI Dash)" cohort
LTV report - reads directly from the "Affilka ROI Dash" materialized
view in Supabase, lets users filter by Partner ID / Campaign ID /
Commission ID, and displays the same month-by-month cohort economics
(GGR, bonuses, deductions, affiliate costs, Profit, Player LTV) as the
original spreadsheet prototype.

Every metric is grouped by "FTD Month" (the cohort's acquisition
month), summing that cohort's ENTIRE activity to date - i.e. this is a
lifetime-to-date cohort view, not a single-month snapshot. A player
whose FTD was in Nov-25 contributes their Aug-26 activity to the Nov-25
column too, since they're still part of that cohort.

Formula notes (confirmed with Dave, differ from the original prototype
in a few places - see commit history / conversation for why):
  - SB GGR = SUM("SB GGR") + SUM("SB Correction") - both included, per
    Dave's confirmation that the D-L columns in the original prototype
    were the correct version (not the column-C-only version).
  - Total GGR = Casino GGR + SB GGR - SB Correction is NOT added again
    separately, since it's already folded into SB GGR above. Adding it
    again (as the original prototype's row 8 formula did) would double
    count it.
  - Rev Share is summed by FTD Month, same as everything else (a
    cohort's lifetime revenue share to date), not by Activity Month as
    an earlier draft of the prototype had it.
  - Row 35 ("fixed_per_player (FTD Month)") stays as Actual_Fixed_Fee,
    summed by FTD Month - unchanged from the prototype.
  - "Fixed Monthly Charge" (renamed from the prototype's placeholder
    "Fixed fee" row, which was dummy incrementing test data) has no
    source anywhere in the view - it's a manually-entered, genuinely
    external monthly cost. Stored in and edited via a small dedicated
    Supabase table ("Dashboard Fixed Monthly Charge") so it persists
    across sessions instead of resetting every time the app restarts.

Deployment: Streamlit Community Cloud. Requires two secrets to be set
in the app's Settings -> Secrets (see .streamlit/secrets.toml.example
for the exact keys/format):
  - db_password: the Supabase Postgres password
  - dashboard_password: a shared password gate for viewers, since a
    Community Cloud app's URL is otherwise publicly reachable by anyone
    who has the link.
"""

import streamlit as st
import pandas as pd
import psycopg2
from datetime import datetime

# ── PAGE CONFIG (must be the first Streamlit call) ─────────────────────

st.set_page_config(page_title="FTD Cohort ROI Dashboard", layout="wide")

# ── CONFIG ────────────────────────────────────────────────────────────

DB_HOST = "aws-1-eu-west-1.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = "postgres.halmyuhieiymqwpddgpp"
DB_PORT = 5432

SOURCE_VIEW = "Affilka ROI Dash"
FIXED_CHARGE_TABLE = "Dashboard Fixed Monthly Charge"

_MONTH_ABBR_ORDER = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]


# ── PASSWORD GATE ────────────────────────────────────────────────────
# Community Cloud apps get a public URL by default - this is a simple
# shared-password gate, not real per-user auth. Good enough to keep the
# dashboard off search engines and casual stumbling, not a substitute
# for proper access control if that's ever needed.


def check_password():
    if st.session_state.get("password_correct", False):
        return True

    st.title("FTD Cohort ROI Dashboard")
    password = st.text_input("Password", type="password")
    if password:
        if password == st.secrets.get("dashboard_password"):
            st.session_state["password_correct"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


if not check_password():
    st.stop()


# ── DATA LOADING ──────────────────────────────────────────────────────


@st.cache_resource
def get_connection():
    return psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=st.secrets["db_password"],
        port=DB_PORT,
    )


@st.cache_data(ttl=600)
def load_roi_dash_data():
    """
    Pulls every column this dashboard needs from "Affilka ROI Dash".
    Cached for 10 minutes - the view itself only changes when
    upload_gaming_data.py rebuilds it, so this avoids re-querying
    Supabase on every filter change within that window.
    """
    conn = get_connection()
    query = f'''
        SELECT
            "FTD Month", "Partner ID", "Campaign ID", "Commission ID",
            "FTD Count", "Deposits sum",
            "Casino GGR", "SB GGR", "SB Correction",
            "Casino Bonus", "SB Bonus",
            "Free Spins Payout", "Free Bet Payout", "BOG Bonus", "Lucky Bonus",
            "RGD Duty", "GBD Duty", "HBLB Levy", "Statutory Levy",
            "Data Provider Fees",
            "Casino Provider Fee", "Live Casino Provider Fee", "Virtuals Provider Fee",
            "Trading Adjustments", "Estimated Processing Fees", "Admin/Platform Fees",
            "Actual_Fixed_Fee", "Actual_RS"
        FROM "{SOURCE_VIEW}"
        WHERE "FTD Month" IS NOT NULL
    '''
    df = pd.read_sql(query, conn)
    return df


@st.cache_data(ttl=600)
def load_fixed_charges():
    conn = get_connection()
    ensure_fixed_charge_table(conn)
    df = pd.read_sql(f'SELECT "FTD Month", "Amount" FROM "{FIXED_CHARGE_TABLE}"', conn)
    return dict(zip(df["FTD Month"], df["Amount"]))


def ensure_fixed_charge_table(conn):
    with conn.cursor() as cur:
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS "{FIXED_CHARGE_TABLE}" (
                "FTD Month" text PRIMARY KEY,
                "Amount" double precision NOT NULL DEFAULT 0
            );
        ''')
    conn.commit()


def save_fixed_charge(ftd_month, amount):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(f'''
            INSERT INTO "{FIXED_CHARGE_TABLE}" ("FTD Month", "Amount")
            VALUES (%s, %s)
            ON CONFLICT ("FTD Month") DO UPDATE SET "Amount" = EXCLUDED."Amount"
        ''', (ftd_month, amount))
    conn.commit()
    load_fixed_charges.clear()


# ── MONTH SORTING (MM/YY -> chronological) ──────────────────────────


def month_sort_key(mm_yy):
    """'07/26' -> (2026, 7) - so months sort chronologically, not alphabetically."""
    try:
        month, year = mm_yy.split("/")
        return (2000 + int(year), int(month))
    except (ValueError, AttributeError):
        return (0, 0)


# ── AGGREGATION ───────────────────────────────────────────────────────


def build_cohort_table(df, fixed_charges):
    """
    Groups the (already-filtered) dataframe by FTD Month and computes
    every row of the cohort report, matching the original prototype's
    structure - see this module's docstring for the specific formula
    decisions that differ from the prototype.
    """
    months = sorted(df["FTD Month"].dropna().unique(), key=month_sort_key, reverse=True)

    rows = {}
    grouped = df.groupby("FTD Month")

    def col_sum(col):
        return grouped[col].sum().reindex(months).fillna(0)

    ftd_count = col_sum("FTD Count")
    deposits = col_sum("Deposits sum")

    casino_ggr = col_sum("Casino GGR")
    sb_ggr = col_sum("SB GGR") + col_sum("SB Correction")
    total_ggr = casino_ggr + sb_ggr

    free_casino_spins = col_sum("Free Spins Payout")
    free_sports_bets = col_sum("Free Bet Payout")
    bog_bonus = col_sum("BOG Bonus")
    lucky_bonus = col_sum("Lucky Bonus")
    total_bonus = free_casino_spins + free_sports_bets + bog_bonus + lucky_bonus
    bonus_pct_of_ggr = (total_bonus / total_ggr.replace(0, pd.NA)).fillna(0)

    casino_tax = col_sum("RGD Duty")
    gbd_duty = col_sum("GBD Duty")
    hblb_levy = col_sum("HBLB Levy")
    statutory_levy = col_sum("Statutory Levy")
    sportsbook_provider_fees = col_sum("Data Provider Fees")
    casino_provider_fees = (
        col_sum("Casino Provider Fee")
        + col_sum("Live Casino Provider Fee")
        + col_sum("Virtuals Provider Fee")
    )
    trading_adjustments = col_sum("Trading Adjustments")
    processing_fees = col_sum("Estimated Processing Fees")
    admin_platform_fees = col_sum("Admin/Platform Fees")
    sum_of_deductions = (
        casino_tax + gbd_duty + hblb_levy + statutory_levy
        + sportsbook_provider_fees + casino_provider_fees
        + trading_adjustments + processing_fees + admin_platform_fees
    )

    fixed_per_player = col_sum("Actual_Fixed_Fee")
    rev_share = col_sum("Actual_RS")
    fixed_monthly_charge = pd.Series(
        {m: fixed_charges.get(m, 0.0) for m in months}
    )
    vat = (fixed_per_player + rev_share + fixed_monthly_charge) * 0.2
    affiliate_costs = fixed_per_player + rev_share + fixed_monthly_charge + vat

    profit = total_ggr - total_bonus - sum_of_deductions - affiliate_costs
    player_ltv = (profit / ftd_count.replace(0, pd.NA)).fillna(0)

    rows["FTD Count"] = ftd_count
    rows["Deposits"] = deposits
    rows["Total GGR"] = total_ggr
    rows["  Casino GGR"] = casino_ggr
    rows["  SB GGR (incl. correction)"] = sb_ggr
    rows["Total Bonus"] = total_bonus
    rows["  Free Casino Spins"] = free_casino_spins
    rows["  Free Sports Bets"] = free_sports_bets
    rows["  BOG Bonus"] = bog_bonus
    rows["  Lucky Bonus"] = lucky_bonus
    rows["Bonus % of GGR"] = bonus_pct_of_ggr
    rows["Sum of Deductions"] = sum_of_deductions
    rows["  Casino Tax (RGD Duty)"] = casino_tax
    rows["  GBD Duty"] = gbd_duty
    rows["  HBLB Levy"] = hblb_levy
    rows["  Statutory Levy"] = statutory_levy
    rows["  Sportsbook Provider Fees"] = sportsbook_provider_fees
    rows["  Casino Provider Fees"] = casino_provider_fees
    rows["  Trading Adjustments"] = trading_adjustments
    rows["  Processing Fees"] = processing_fees
    rows["  Admin/Platform Fees"] = admin_platform_fees
    rows["Profit"] = profit
    rows["Player LTV"] = player_ltv
    rows["fixed_per_player (FTD Month)"] = fixed_per_player
    rows["Rev Share (FTD Month)"] = rev_share
    rows["Fixed Monthly Charge"] = fixed_monthly_charge
    rows["VAT"] = vat
    rows["Affiliate Costs"] = affiliate_costs

    table = pd.DataFrame(rows).T
    table = table[months]
    return table, months


def format_currency(v):
    if pd.isna(v):
        return ""
    return f"£{v:,.0f}"


def format_pct(v):
    if pd.isna(v):
        return ""
    return f"{v:.1%}"


CURRENCY_ROWS = {
    "Deposits", "Total GGR", "  Casino GGR", "  SB GGR (incl. correction)",
    "Total Bonus", "  Free Casino Spins", "  Free Sports Bets", "  BOG Bonus", "  Lucky Bonus",
    "Sum of Deductions", "  Casino Tax (RGD Duty)", "  GBD Duty", "  HBLB Levy",
    "  Statutory Levy", "  Sportsbook Provider Fees", "  Casino Provider Fees",
    "  Trading Adjustments", "  Processing Fees", "  Admin/Platform Fees",
    "Profit", "Player LTV", "fixed_per_player (FTD Month)", "Rev Share (FTD Month)",
    "Fixed Monthly Charge", "VAT", "Affiliate Costs",
}
PERCENT_ROWS = {"Bonus % of GGR"}


# ── MAIN APP ──────────────────────────────────────────────────────────

st.title("FTD Cohort ROI Dashboard")
st.caption(
    "Every column is an FTD-month cohort's activity to date - a player who signed up "
    "in Nov-25 contributes their Aug-26 spend to the Nov-25 column too."
)

with st.spinner("Loading data..."):
    df = load_roi_dash_data()
    fixed_charges = load_fixed_charges()

# ── FILTERS ──────────────────────────────────────────────────────────

st.sidebar.header("Filters")

partner_ids = sorted(df["Partner ID"].dropna().unique().tolist())
campaign_ids = sorted(df["Campaign ID"].dropna().unique().tolist())
commission_ids = sorted(df["Commission ID"].dropna().unique().tolist())

selected_partners = st.sidebar.multiselect("Partner ID", partner_ids)
selected_campaigns = st.sidebar.multiselect("Campaign ID", campaign_ids)
selected_commissions = st.sidebar.multiselect("Commission ID", commission_ids)

filtered = df.copy()
if selected_partners:
    filtered = filtered[filtered["Partner ID"].isin(selected_partners)]
if selected_campaigns:
    filtered = filtered[filtered["Campaign ID"].isin(selected_campaigns)]
if selected_commissions:
    filtered = filtered[filtered["Commission ID"].isin(selected_commissions)]

if filtered.empty:
    st.warning("No data matches the selected filters.")
    st.stop()

# ── COHORT TABLE ─────────────────────────────────────────────────────

table, months = build_cohort_table(filtered, fixed_charges)

display_table = table.copy()
for row_name in display_table.index:
    if row_name in PERCENT_ROWS:
        display_table.loc[row_name] = table.loc[row_name].apply(format_pct)
    elif row_name in CURRENCY_ROWS:
        display_table.loc[row_name] = table.loc[row_name].apply(format_currency)
    else:
        display_table.loc[row_name] = table.loc[row_name].apply(lambda v: f"{v:,.0f}")

st.dataframe(display_table, use_container_width=True, height=min(35 * len(display_table) + 40, 900))

# ── PROFIT / LTV CHARTS ──────────────────────────────────────────────

col1, col2 = st.columns(2)
chart_months = list(reversed(months))  # chronological for charts
with col1:
    st.subheader("Profit by FTD cohort")
    st.bar_chart(table.loc["Profit", chart_months])
with col2:
    st.subheader("Player LTV by FTD cohort")
    st.bar_chart(table.loc["Player LTV", chart_months])

# ── FIXED MONTHLY CHARGE EDITOR ──────────────────────────────────────

st.divider()
st.subheader("Edit Fixed Monthly Charge")
st.caption(
    "This figure has no source in the data - it's entered manually per FTD month "
    "and persists here across sessions."
)

edit_month = st.selectbox("FTD Month", months, key="fixed_charge_month")
current_value = fixed_charges.get(edit_month, 0.0)
new_value = st.number_input(
    f"Fixed Monthly Charge for {edit_month} (£)",
    value=float(current_value),
    step=100.0,
)
if st.button("Save"):
    save_fixed_charge(edit_month, new_value)
    st.success(f"Saved £{new_value:,.2f} for {edit_month}.")
    st.rerun()

st.caption(f"Data loaded: {datetime.now().strftime('%Y-%m-%d %H:%M')} (cached for 10 minutes)")
