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
    source anywhere in the view - the pool itself is a manually-entered,
    genuinely external monthly cost. Entered per Activity Month (not FTD
    Month - it's a monthly business cost, not an acquisition cost) via a
    small dedicated Supabase table ("Dashboard Fixed Monthly Charge"),
    then allocated per-account by Combined Stake share - same method as
    Admin/Platform Fees, but restricted to affiliate accounts only,
    since this charge is only paid by accounts with an affiliate (see
    allocate_fixed_monthly_charge()). This also means it can now be
    correctly included in the Partner/Campaign/Commission ranking
    tables, unlike before when the lump-sum-per-FTD-Month version had
    no way to attribute to a specific group.

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

    "Original player ID" (the account's own ID - same as "Wallet Code"
    on "Customer Trading Data Monthly") and "Activity Month" are needed
    to allocate Fixed Monthly Charge by Combined Stake share - see
    allocate_fixed_monthly_charge().
    """
    conn = get_connection()
    query = f'''
        SELECT
            "FTD Month", "Activity Month", "Original player ID",
            "Partner ID", "Campaign ID", "Commission ID",
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
def load_combined_stake():
    """
    Pulls Combined Stake per (Wallet Code, Activity Month) from
    "Customer Trading Data Monthly" - built for every account by
    upload_gaming_data.py, affiliate or not. Used as the allocation
    basis for Fixed Monthly Charge below, restricted to affiliate
    accounts only (i.e. the ones present in "Affilka ROI Dash") since
    that charge is only paid by accounts with an affiliate - unlike
    Admin/Platform Fees, which spans the whole business.
    """
    conn = get_connection()
    query = '''
        SELECT "Wallet Code", "Activity Month", "Combined Stake"
        FROM "Customer Trading Data Monthly"
        WHERE "Combined Stake" IS NOT NULL
    '''
    return pd.read_sql(query, conn)


@st.cache_data(ttl=600)
def load_fixed_charges():
    conn = get_connection()
    ensure_fixed_charge_table(conn)
    df = pd.read_sql(f'SELECT "Activity Month", "Amount" FROM "{FIXED_CHARGE_TABLE}"', conn)
    return dict(zip(df["Activity Month"], df["Amount"]))


def ensure_fixed_charge_table(conn):
    with conn.cursor() as cur:
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS "{FIXED_CHARGE_TABLE}" (
                "Activity Month" text PRIMARY KEY,
                "Amount" double precision NOT NULL DEFAULT 0
            );
        ''')
        # Migration: this table was originally keyed by "FTD Month" (a
        # per-cohort lump sum) before Fixed Monthly Charge was redesigned
        # to be a per-Activity-Month pool allocated by Combined Stake
        # share. If an older version of the table exists with that
        # column, rename it in place rather than losing whatever values
        # were already entered.
        cur.execute('''
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s
        ''', (FIXED_CHARGE_TABLE,))
        existing_columns = {row[0] for row in cur.fetchall()}
        if "FTD Month" in existing_columns and "Activity Month" not in existing_columns:
            cur.execute(f'''
                ALTER TABLE "{FIXED_CHARGE_TABLE}" RENAME COLUMN "FTD Month" TO "Activity Month"
            ''')
    conn.commit()


def save_fixed_charge(activity_month, amount):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(f'''
            INSERT INTO "{FIXED_CHARGE_TABLE}" ("Activity Month", "Amount")
            VALUES (%s, %s)
            ON CONFLICT ("Activity Month") DO UPDATE SET "Amount" = EXCLUDED."Amount"
        ''', (activity_month, amount))
    conn.commit()
    load_fixed_charges.clear()


def allocate_fixed_monthly_charge(full_df, combined_stake_df, fixed_charges):
    """
    Allocates each Activity Month's manually-entered Fixed Monthly
    Charge pool across every row in full_df (the FULL, UNFILTERED
    "Affilka ROI Dash" dataset - not whatever subset the sidebar
    filters are currently narrowed to), by that row's account's own
    Combined Stake share of the total Combined Stake across every
    DISTINCT affiliate account active that month.

    Deliberately uses full_df rather than a filtered subset: the true
    cost-sharing basis is "every affiliate account", so allocating
    against a filtered-down denominator would make whichever partner is
    currently selected look like it's absorbing the whole charge, which
    would be wrong. Callers should sum/filter the returned per-row
    Series AFTER allocation, not before.

    Same two-step pattern as Admin/Platform Fees: each account's overall
    share of the pool, then split evenly across however many rows that
    account has in that specific Activity Month (multiple commissions
    active simultaneously) - and the same "deduplicate before summing"
    care taken there, to avoid the double-counting bug already found
    and fixed twice elsewhere in this project.

    Fully vectorised (map/groupby, no row-wise .apply) - this dataset
    can be 30k+ rows and gets recomputed on every filter/toggle change,
    so a Python-level loop over every row would be noticeably slow.

    Returns a pandas Series aligned with full_df's index - the
    allocated Fixed Monthly Charge for each individual row.
    """
    merged = full_df.merge(
        combined_stake_df.rename(columns={"Wallet Code": "Original player ID"}),
        on=["Original player ID", "Activity Month"],
        how="left",
    )
    merged["Combined Stake"] = merged["Combined Stake"].fillna(0.0)

    # Denominator: SUM of Combined Stake per Activity Month, but ONCE
    # per distinct account - not once per row - since an affiliate
    # account can have multiple commission rows in the same month.
    distinct_account_stake = merged.drop_duplicates(subset=["Original player ID", "Activity Month"])
    month_total_stake = distinct_account_stake.groupby("Activity Month")["Combined Stake"].sum()

    # Each account's own row count per Activity Month, for splitting
    # their share evenly across multiple commission rows.
    account_row_count = merged.groupby(["Original player ID", "Activity Month"]).size()
    account_row_count.name = "_row_count"

    # Total row count per Activity Month, for the £0-stake fallback.
    month_row_count = merged.groupby("Activity Month").size()

    merged = merged.join(account_row_count, on=["Original player ID", "Activity Month"])
    merged["_pool"] = merged["Activity Month"].map(fixed_charges).fillna(0.0)
    merged["_month_total_stake"] = merged["Activity Month"].map(month_total_stake).fillna(0.0)
    merged["_month_row_count"] = merged["Activity Month"].map(month_row_count).fillna(1)

    has_stake = merged["_month_total_stake"] > 0
    account_share = merged["Combined Stake"] / merged["_month_total_stake"].replace(0, pd.NA)
    proportional_allocation = merged["_pool"] * account_share / merged["_row_count"]
    equal_split_fallback = merged["_pool"] / merged["_month_row_count"]

    allocated = proportional_allocation.where(has_stake, equal_split_fallback)
    allocated = allocated.fillna(0.0)
    allocated.index = full_df.index
    return allocated


# ── MONTH SORTING (MM/YY -> chronological) ──────────────────────────


def month_sort_key(mm_yy):
    """'07/26' -> (2026, 7) - so months sort chronologically, not alphabetically."""
    try:
        month, year = mm_yy.split("/")
        return (2000 + int(year), int(month))
    except (ValueError, AttributeError):
        return (0, 0)


# ── AGGREGATION ───────────────────────────────────────────────────────


def build_cohort_table(df, include_affiliate_costs_in_ltv, min_ftd_count=0):
    """
    Groups the (already-filtered) dataframe by FTD Month and computes
    every row of the cohort report, organised into clear labeled
    sections (Volume, Revenue, Bonuses, Taxes & Duties, Other Fees,
    Affiliate Costs, then Profit/LTV last) rather than the original
    prototype's flat top-to-bottom list. See this module's docstring
    for the specific formula decisions that differ from the prototype.

    Months whose total FTD Count (summed across every account in that
    cohort) is below min_ftd_count are dropped entirely from the result
    - a general filter for near-empty/junk months (e.g. a stray month
    with 1 FTD and negligible GGR right at the edge of the data) rather
    than a hardcoded exclusion of one specific month.

    Returns (table, months, total_rows) - total_rows is the set of row
    labels that are SUMS of other rows in the table (Total GGR, Total
    Bonus, Total Taxes & Duties, Total Other Fees, Sum of Deductions,
    Affiliate Costs, Profit, Player LTV), used by the display code to
    style them distinctly (bold + shaded) from their component rows.
    """
    all_months = sorted(df["FTD Month"].dropna().unique(), key=month_sort_key, reverse=True)

    if min_ftd_count > 0:
        month_ftd_totals = df.groupby("FTD Month")["FTD Count"].sum()
        months = [m for m in all_months if month_ftd_totals.get(m, 0) >= min_ftd_count]
    else:
        months = all_months

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

    # Taxes & Duties - the genuinely regulatory/statutory items, grouped
    # together per Dave's request (previously scattered among a single
    # flat "Sum of Deductions" list alongside provider fees etc).
    casino_tax = col_sum("RGD Duty")
    gbd_duty = col_sum("GBD Duty")
    hblb_levy = col_sum("HBLB Levy")
    statutory_levy = col_sum("Statutory Levy")
    total_taxes_and_duties = casino_tax + gbd_duty + hblb_levy + statutory_levy

    # Other Fees & Adjustments - everything else that used to sit in the
    # same flat "Sum of Deductions" list, but isn't a tax/duty.
    sportsbook_provider_fees = col_sum("Data Provider Fees")
    casino_provider_fees = (
        col_sum("Casino Provider Fee")
        + col_sum("Live Casino Provider Fee")
        + col_sum("Virtuals Provider Fee")
    )
    trading_adjustments = col_sum("Trading Adjustments")
    processing_fees = col_sum("Estimated Processing Fees")
    admin_platform_fees = col_sum("Admin/Platform Fees")
    total_other_fees = (
        sportsbook_provider_fees + casino_provider_fees
        + trading_adjustments + processing_fees + admin_platform_fees
    )

    sum_of_deductions = total_taxes_and_duties + total_other_fees

    fixed_per_player = col_sum("Actual_Fixed_Fee")
    rev_share = col_sum("Actual_RS")
    fixed_monthly_charge = col_sum("Allocated Fixed Monthly Charge")
    vat = (fixed_per_player + rev_share + fixed_monthly_charge) * 0.2
    affiliate_costs = fixed_per_player + rev_share + fixed_monthly_charge + vat

    # Profit / Player LTV - always the LAST rows in the table. Whether
    # Affiliate Costs are subtracted depends on the sidebar toggle
    # (default: excluded) - Profit and LTV move together, since LTV is
    # just Profit divided by FTD Count, so it wouldn't make sense for
    # one to include Affiliate Costs and the other not to.
    if include_affiliate_costs_in_ltv:
        profit = total_ggr - total_bonus - sum_of_deductions - affiliate_costs
        profit_label = "Profit (incl. Affiliate Costs)"
        ltv_label = "Player LTV (incl. Affiliate Costs)"
    else:
        profit = total_ggr - total_bonus - sum_of_deductions
        profit_label = "Profit (excl. Affiliate Costs)"
        ltv_label = "Player LTV (excl. Affiliate Costs)"
    player_ltv = (profit / ftd_count.replace(0, pd.NA)).fillna(0)

    rows = {}
    total_rows = set()

    # ── Volume ──
    rows["FTD Count"] = ftd_count
    rows["Deposits"] = deposits

    # ── Revenue ──
    rows["Total GGR"] = total_ggr
    total_rows.add("Total GGR")
    rows["  Casino GGR"] = casino_ggr
    rows["  SB GGR (incl. correction)"] = sb_ggr

    # ── Bonuses ──
    rows["Total Bonus"] = total_bonus
    total_rows.add("Total Bonus")
    rows["  Free Casino Spins"] = free_casino_spins
    rows["  Free Sports Bets"] = free_sports_bets
    rows["  BOG Bonus"] = bog_bonus
    rows["  Lucky Bonus"] = lucky_bonus
    rows["Bonus % of GGR"] = bonus_pct_of_ggr

    # ── Taxes & Duties ──
    rows["Total Taxes & Duties"] = total_taxes_and_duties
    total_rows.add("Total Taxes & Duties")
    rows["  Casino Tax (RGD Duty)"] = casino_tax
    rows["  GBD Duty"] = gbd_duty
    rows["  HBLB Levy"] = hblb_levy
    rows["  Statutory Levy"] = statutory_levy

    # ── Other Fees & Adjustments ──
    rows["Total Other Fees & Adjustments"] = total_other_fees
    total_rows.add("Total Other Fees & Adjustments")
    rows["  Sportsbook Provider Fees"] = sportsbook_provider_fees
    rows["  Casino Provider Fees"] = casino_provider_fees
    rows["  Trading Adjustments"] = trading_adjustments
    rows["  Processing Fees"] = processing_fees
    rows["  Admin/Platform Fees"] = admin_platform_fees

    # ── Combined deductions total (Taxes & Duties + Other Fees) ──
    rows["Sum of Deductions"] = sum_of_deductions
    total_rows.add("Sum of Deductions")

    # ── Affiliate Costs ──
    rows["Affiliate Costs"] = affiliate_costs
    total_rows.add("Affiliate Costs")
    rows["  fixed_per_player (FTD Month)"] = fixed_per_player
    rows["  Rev Share (FTD Month)"] = rev_share
    rows["  Fixed Monthly Charge"] = fixed_monthly_charge
    rows["  VAT"] = vat

    # ── Bottom line - always last ──
    rows[profit_label] = profit
    total_rows.add(profit_label)
    rows[ltv_label] = player_ltv
    total_rows.add(ltv_label)

    table = pd.DataFrame(rows).T
    table = table[months]
    return table, months, total_rows


def build_ranking_table(df, group_col, include_affiliate_costs_in_ltv):
    """
    Groups the (already-filtered) dataframe by group_col (Partner ID,
    Campaign ID, or Commission ID) and computes each group's LIFETIME
    Profit and Player LTV, sorted highest-LTV-first.

    Affiliate Costs here now includes "Allocated Fixed Monthly Charge"
    (previously excluded, since the old per-FTD-Month lump sum had no
    way to be attributed to a specific Partner/Campaign/Commission).
    Now that it's allocated per-row by Combined Stake share (see
    allocate_fixed_monthly_charge()), summing it by group_col is exactly
    as valid as summing Casino GGR by group_col - matching Actual_Fixed_Fee
    and Actual_RS, which were always genuinely row-level in the source view.

    Returns a DataFrame with one row per group_col value, sorted by
    Player LTV descending (highest LTV first, matching "top to bottom"
    ranking).
    """
    grouped = df.groupby(group_col)

    def col_sum(col):
        return grouped[col].sum()

    ftd_count = col_sum("FTD Count")
    casino_ggr = col_sum("Casino GGR")
    sb_ggr = col_sum("SB GGR") + col_sum("SB Correction")
    total_ggr = casino_ggr + sb_ggr

    total_bonus = (
        col_sum("Free Spins Payout") + col_sum("Free Bet Payout")
        + col_sum("BOG Bonus") + col_sum("Lucky Bonus")
    )

    total_taxes_and_duties = (
        col_sum("RGD Duty") + col_sum("GBD Duty")
        + col_sum("HBLB Levy") + col_sum("Statutory Levy")
    )
    total_other_fees = (
        col_sum("Data Provider Fees")
        + col_sum("Casino Provider Fee") + col_sum("Live Casino Provider Fee") + col_sum("Virtuals Provider Fee")
        + col_sum("Trading Adjustments") + col_sum("Estimated Processing Fees") + col_sum("Admin/Platform Fees")
    )
    sum_of_deductions = total_taxes_and_duties + total_other_fees

    fixed_per_player = col_sum("Actual_Fixed_Fee")
    rev_share = col_sum("Actual_RS")
    fixed_monthly_charge = col_sum("Allocated Fixed Monthly Charge")
    vat = (fixed_per_player + rev_share + fixed_monthly_charge) * 0.2
    affiliate_costs = fixed_per_player + rev_share + fixed_monthly_charge + vat

    if include_affiliate_costs_in_ltv:
        profit = total_ggr - total_bonus - sum_of_deductions - affiliate_costs
        profit_label = "Profit (incl. Affiliate Costs)"
        ltv_label = "Player LTV (incl. Affiliate Costs)"
    else:
        profit = total_ggr - total_bonus - sum_of_deductions
        profit_label = "Profit (excl. Affiliate Costs)"
        ltv_label = "Player LTV (excl. Affiliate Costs)"
    player_ltv = (profit / ftd_count.replace(0, pd.NA)).fillna(0)

    result = pd.DataFrame({
        "FTD Count": ftd_count,
        "Total GGR": total_ggr,
        "Total Bonus": total_bonus,
        "Sum of Deductions": sum_of_deductions,
        "Affiliate Costs": affiliate_costs,
        profit_label: profit,
        ltv_label: player_ltv,
    })
    result = result.sort_values(ltv_label, ascending=False)
    result.index.name = group_col
    return result, profit_label, ltv_label


def format_ranking_table(result, profit_label, ltv_label):
    """
    Currency-formats every column except FTD Count (a plain integer
    count), returning a display-ready copy. Same .astype(object)
    upfront pattern as the cohort table, for the same reason (newer
    pandas rejects writing formatted strings into a float64 column).
    """
    display = result.astype(object)
    for col in display.columns:
        if col == "FTD Count":
            display[col] = result[col].apply(lambda v: f"{v:,.0f}")
        else:
            display[col] = result[col].apply(format_currency)
    return display


def format_currency(v):
    if pd.isna(v):
        return ""
    return f"£{v:,.0f}"


def format_pct(v):
    if pd.isna(v):
        return ""
    return f"{v:.1%}"


PERCENT_ROWS = {"Bonus % of GGR"}
COUNT_ROWS = {"FTD Count"}
# Every row not in PERCENT_ROWS or COUNT_ROWS is a currency row -
# formatting is now driven by exclusion rather than an explicit set,
# since row labels change dynamically (Profit/LTV's label depends on
# the affiliate-costs toggle).


# ── MAIN APP ──────────────────────────────────────────────────────────

st.title("FTD Cohort ROI Dashboard")
st.caption(
    "Every column is an FTD-month cohort's activity to date - a player who signed up "
    "in Nov-25 contributes their Aug-26 spend to the Nov-25 column too."
)

with st.spinner("Loading data..."):
    df = load_roi_dash_data()
    fixed_charges = load_fixed_charges()
    combined_stake_df = load_combined_stake()

# Allocate Fixed Monthly Charge on the FULL, unfiltered dataset - see
# allocate_fixed_monthly_charge()'s docstring for why this must happen
# before any Partner/Campaign/Commission filtering, not after.
df["Allocated Fixed Monthly Charge"] = allocate_fixed_monthly_charge(df, combined_stake_df, fixed_charges)

# ── FILTERS ──────────────────────────────────────────────────────────

st.sidebar.header("Filters")

partner_ids = sorted(df["Partner ID"].dropna().unique().tolist())
campaign_ids = sorted(df["Campaign ID"].dropna().unique().tolist())
commission_ids = sorted(df["Commission ID"].dropna().unique().tolist())

selected_partners = st.sidebar.multiselect("Partner ID", partner_ids)
selected_campaigns = st.sidebar.multiselect("Campaign ID", campaign_ids)
selected_commissions = st.sidebar.multiselect("Commission ID", commission_ids)

st.sidebar.divider()
include_affiliate_costs = st.sidebar.checkbox(
    "Include Affiliate Costs in Profit / Player LTV",
    value=False,
    help=(
        "Off by default: Profit and Player LTV show core unit economics "
        "(revenue minus bonuses and deductions) before affiliate acquisition "
        "cost. Turn on to see the fully-loaded figure after Affiliate Costs "
        "as well."
    ),
)

st.sidebar.divider()
min_ftd_count = st.sidebar.number_input(
    "Hide FTD-cohort months below this FTD Count",
    min_value=0,
    value=5,
    step=1,
    help=(
        "Filters out near-empty/junk months from the FTD Cohort View's "
        "columns (e.g. a month with only 1 FTD and negligible GGR) - "
        "applies to the FTD Cohort View tab only, not the Partner/Campaign/"
        "Commission ranking tabs, which aren't organised by month at all."
    ),
)

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

# ── TABS ─────────────────────────────────────────────────────────────

tab_cohort, tab_partner, tab_campaign, tab_commission = st.tabs([
    "FTD Cohort View", "By Partner ID", "By Campaign ID", "By Commission ID",
])

with tab_cohort:
    table, months, total_rows = build_cohort_table(filtered, include_affiliate_costs, min_ftd_count)

    # .astype(object) first - newer pandas versions raise a TypeError when
    # assigning formatted strings (e.g. "£1,234") into a column pandas still
    # considers float64, rather than silently upcasting like older versions
    # did. Converting the whole table to object dtype upfront means every
    # cell can hold either a number or a string without that strict check
    # kicking in.
    display_table = table.astype(object)
    for row_name in display_table.index:
        if row_name in PERCENT_ROWS:
            display_table.loc[row_name] = table.loc[row_name].apply(format_pct)
        elif row_name in COUNT_ROWS:
            display_table.loc[row_name] = table.loc[row_name].apply(lambda v: f"{v:,.0f}")
        else:
            display_table.loc[row_name] = table.loc[row_name].apply(format_currency)

    def style_total_rows(row):
        """
        Bold + shaded background for total/subtotal rows (Total GGR, Total
        Bonus, Total Taxes & Duties, Total Other Fees & Adjustments, Sum of
        Deductions, Affiliate Costs, Profit, Player LTV) so it's visually
        obvious at a glance which rows are sums of the detail rows sitting
        underneath them, versus the individual line items themselves.
        """
        if row.name in total_rows:
            return ["font-weight: bold; background-color: rgba(120, 120, 120, 0.18)"] * len(row)
        return [""] * len(row)

    styled_table = display_table.style.apply(style_total_rows, axis=1)
    st.dataframe(styled_table, use_container_width=True, height=min(35 * len(display_table) + 40, 900))

    with st.expander("Where do the Affiliate Costs figures come from?"):
        st.markdown("""
**fixed_per_player (FTD Month)**

Source: `Affilka Data` (`Actual_Fixed_Fee` column) + the `CPA By Cohort` spreadsheet

```
For each (Partner ID, FTD Month) cohort with real cost data in CPA By Cohort:
    fee_per_account = that cohort's total CPA cost ÷ count of eligible accounts
                       (FTD Count = 1, FTD Month matches, not frozen in their first month)
    written onto each eligible account's own FTD-month row, £0 elsewhere

For any (Partner ID, FTD Month) with no CPA By Cohort entry:
    falls back to Affilka's own reported fixed_per_player figure
```

Summed across every row belonging to that FTD Month cohort.

---

**Rev Share (FTD Month)**

Source: `Affilka Data` (`Actual_RS` column) + the `RS By Cohort` spreadsheet

```
For each (Partner ID, FTD Month, Activity Month) triple with real cost data in RS By Cohort:
    pool = that triple's RS Amount
    each row's share = pool × (GREATEST(this row's SB GGR + Casino GGR, 0)
                                ÷ SUM of GREATEST(SB GGR + Casino GGR, 0)
                                  across every row in that exact triple)

For any triple with no RS By Cohort entry:
    falls back to Affilka's own reported ngr_percent figure
```

Summed across every Activity Month this cohort has been active in - i.e. the
cohort's lifetime revenue share to date, not just their FTD month.

---

**Fixed Monthly Charge**

Source: entered manually, per Activity Month, directly in this dashboard;
allocated using Combined Stake from `Customer Trading Data Monthly`

```
The pool itself (£X for a given Activity Month) has no source anywhere in
Affilka or the underlying reports - a genuinely external monthly cost.

Allocated the same way as Admin/Platform Fees, but restricted to affiliate
accounts only (this charge is only paid by accounts with an affiliate):

each account's share = pool × (that account's Combined Stake that Activity Month
                                ÷ SUM of Combined Stake across every DISTINCT
                                  affiliate account active that month)

then split evenly across however many commission rows that account has
that specific month, same as everywhere else this pattern is used.
```

Stored in Supabase so the pool figure persists across sessions (see
"Edit Fixed Monthly Charge" below). Summed by FTD Month cohort for
display here, same as everything else.

---

**VAT**

Source: calculated in this dashboard

```
VAT = 20% × (fixed_per_player + Rev Share + Fixed Monthly Charge)
```

for that FTD Month cohort.

---

**Affiliate Costs**

Source: calculated in this dashboard

```
Affiliate Costs = fixed_per_player + Rev Share + Fixed Monthly Charge + VAT
```

for that FTD Month cohort.
""")

    # ── PROFIT / LTV CHARTS ──
    profit_row = next(r for r in table.index if r.startswith("Profit"))
    ltv_row = next(r for r in table.index if r.startswith("Player LTV"))

    col1, col2 = st.columns(2)
    chart_months = list(reversed(months))  # chronological for charts
    with col1:
        st.subheader(profit_row)
        st.bar_chart(table.loc[profit_row, chart_months])
    with col2:
        st.subheader(ltv_row)
        st.bar_chart(table.loc[ltv_row, chart_months])

    # ── FIXED MONTHLY CHARGE EDITOR ──
    st.divider()
    st.subheader("Edit Fixed Monthly Charge")
    st.caption(
        "This figure has no source in the data - it's a whole-business monthly cost "
        "entered manually per Activity Month, then allocated across every affiliate "
        "account by their share of Combined Stake that month (same method as "
        "Admin/Platform Fees, but restricted to accounts with an affiliate - see the "
        "explanation above). Persists here across sessions."
    )

    activity_months = sorted(df["Activity Month"].dropna().unique(), key=month_sort_key, reverse=True)
    edit_month = st.selectbox("Activity Month", activity_months, key="fixed_charge_month")
    current_value = fixed_charges.get(edit_month, 0.0)
    new_value = st.number_input(
        f"Fixed Monthly Charge pool for {edit_month} (£)",
        value=float(current_value),
        step=100.0,
    )
    if st.button("Save"):
        save_fixed_charge(edit_month, new_value)
        st.success(f"Saved £{new_value:,.2f} for {edit_month}.")
        st.rerun()

    st.caption(f"Data loaded: {datetime.now().strftime('%Y-%m-%d %H:%M')} (cached for 10 minutes)")


def render_ranking_tab(tab, group_col, label):
    with tab:
        st.subheader(f"Ranked by Player LTV - {label}")
        st.caption(
            "Lifetime totals across every FTD cohort, ranked highest Player LTV first. "
            "Affiliate Costs includes Fixed Monthly Charge, allocated by each account's "
            "share of Combined Stake across all affiliate accounts that month, then "
            "summed here the same way as every other cost."
        )
        result, profit_label, ltv_label = build_ranking_table(filtered, group_col, include_affiliate_costs)
        display = format_ranking_table(result, profit_label, ltv_label)
        st.dataframe(display, use_container_width=True, height=min(35 * len(display) + 80, 700))

        st.bar_chart(result[ltv_label])


render_ranking_tab(tab_partner, "Partner ID", "Partner ID")
render_ranking_tab(tab_campaign, "Campaign ID", "Campaign ID")
render_ranking_tab(tab_commission, "Commission ID", "Commission ID")
