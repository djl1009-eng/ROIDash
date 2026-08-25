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
    source anywhere in the view - it's a flat, genuinely fixed £3,500
    PER FTD MONTH (hardcoded as FIXED_MONTHLY_CHARGE_AMOUNT near the
    top of this file), split equally across that month's distinct new
    FTDs and written onto exactly one row per account (their own
    FTD-month row - every later Activity Month for that account gets
    £0). This is genuinely an acquisition cost, paid only in an
    account's first month, not spread across their whole lifetime
    activity - see allocate_fixed_monthly_charge(). Same
    multi-commission-in-one-month double-counting risk already found
    and fixed once for Spiros_Fixed_Fee is guarded against here the
    same way (exactly one row per account, picked by lowest Commission
    ID). Since it's genuinely row-level once allocated, it's correctly
    included in the Partner/Campaign/Commission ranking tables too.

The "30 Days % of Players Still Depositing" chart at the bottom of the
FTD Cohort View is fed by a SEPARATE query (see
load_deposit_lifecycle_data()) joining each account's FTD timestamp to
its most recent deposit, and is a SURVIVAL curve rather than a
per-period snapshot like the monthly charts - see
build_relative_day_retention()'s docstring, which is worth reading
before drawing conclusions from its shape.

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
import altair as alt
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

# A flat, genuinely fixed cost applied to every Activity Month - no UI
# to edit this, since it doesn't vary. If this ever needs to change,
# update the number here directly (and the app will pick it up on its
# next deploy) rather than via a database-backed editor.
FIXED_MONTHLY_CHARGE_AMOUNT = 3500.0

_MONTH_ABBR_ORDER = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]

# ── 30-DAY RETENTION CHART CONFIG ─────────────────────────────────────

RELATIVE_DAY_WINDOW = 30

# The timezone every timestamp is reduced to before its calendar date is
# taken. This is not cosmetic: "First deposit date" is timestamptz, so
# an FTD at 00:30 BST is the PREVIOUS day in UTC, which would shift that
# account a full day on every relative-day calculation and move accounts
# between cohorts at month boundaries. UK book, so London is the
# meaningful day boundary.
LOCAL_TIMEZONE = "Europe/London"

# Minimum number of still-observable accounts a (cohort, day) point
# needs before it's plotted at all. Below this, one account flipping
# from active to lapsed moves the line by 20+ percentage points, which
# reads as a retention cliff rather than as a sample-size artefact.
# Same intent as the sidebar's min_ftd_count, applied per-point.
MIN_OBSERVED_ACCOUNTS_PER_POINT = 5

# The 30-day chart sources its FTD timestamps from SOURCE_VIEW rather
# than the underlying "Affilka Data" table, so "Original player ID" is
# literally the same column here as in load_roi_dash_data() - the chart
# can't drift out of alignment with the sidebar filters it's scoped by.
# Per the view definition, that column is "Affilka Data"."Account ID"
# surfaced under a different name.
#
# That leaves customer_data as the only join with any uncertainty in it,
# and not much: the view itself already joins "Customer Trading Data
# Monthly" on "Wallet Code" = "Account ID", so wallet_code and
# "Original player ID" are the same identifier. Both sides are still
# cast to text, since "Account ID" is bigint and wallet_code may be
# text - Postgres would otherwise refuse the comparison outright.
#
# A wrong key here fails visibly rather than silently: every account
# ends up with a NULL last deposit and gets excluded, which the caption
# under the chart reports.
CUSTOMER_DATA_JOIN_KEY = "wallet_code"


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
    """
    This connection is cached (via st.cache_resource) and shared across
    EVERY query, EVERY rerun, and EVERY user session of the app - not
    per-session, genuinely global. autocommit=True is essential here:
    without it, any single failed query (a timeout, a bad connection,
    anything) leaves this ONE shared connection stuck in Postgres's
    "current transaction is aborted" state, and every subsequent query
    by every user then fails too, until the whole app process restarts.
    This is exactly what happened on 2026-08-14 - a customer_data query
    timed out, and the resulting cascade of "25P02" errors in the
    Supabase logs confirms the stuck-transaction pattern. With
    autocommit=True, each statement succeeds or fails independently, so
    one bad query can never poison every query after it. This app never
    needs multi-statement transactional consistency anyway (every query
    here is an independent read), so there's no downside to it.
    """
    conn = psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=st.secrets["db_password"],
        port=DB_PORT,
    )
    conn.autocommit = True
    return conn


def get_healthy_connection():
    """
    Returns the cached connection, transparently reconnecting if it's
    been closed or gone stale (e.g. after a network blip, or the
    Supabase pooler recycling it server-side - "connection to client
    lost" / "Broken pipe" both appeared in the Supabase logs during the
    same incident above). st.cache_resource caches the connection
    object indefinitely, so without this check, a dead connection would
    keep being returned - and keep failing every single query - until
    the whole app process restarts. Every query function in this app
    should call this instead of get_connection() directly.
    """
    conn = get_connection()
    try:
        if conn.closed:
            raise psycopg2.OperationalError("connection is closed")
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        get_connection.clear()
        conn = get_connection()
    return conn


@st.cache_data(ttl=600)
def load_roi_dash_data():
    """
    Pulls every column this dashboard needs from "Affilka ROI Dash".
    Cached for 10 minutes - the view itself only changes when
    upload_gaming_data.py rebuilds it, so this avoids re-querying
    Supabase on every filter change within that window.

    "Original player ID" (the account's own ID) and "Activity Month"
    are needed to identify each account's own FTD-month row, for
    allocating Fixed Monthly Charge - see allocate_fixed_monthly_charge().
    "Relative Month" and "Deposits count" feed the cumulative-by-cohort
    charts - see build_relative_month_series().

    Extends the statement timeout to 45s for this specific query (well
    above whatever short default the pooled connection uses), since
    this query is core to the whole dashboard, so a failure here can't
    just degrade gracefully to an empty result. Instead, a clear,
    actionable message is shown (rather than a raw Streamlit crash
    traceback) if it still fails - this exact error class
    (QueryCanceled / 57014) has previously been traced to Supabase-side
    resource contention on this project (confirmed by Supabase
    support), so that's the most likely cause if this recurs, not a
    bug in this query itself.
    """
    conn = get_healthy_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '45000'")  # milliseconds
        query = f'''
            SELECT
                "FTD Month", "Activity Month", "Original player ID", "Relative Month",
                "Partner ID", "Campaign ID", "Commission ID",
                "FTD Count", "Deposits sum", "Deposits count",
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
    except Exception as e:
        st.error(
            f"Couldn't load data from \"{SOURCE_VIEW}\" ({e}).\n\n"
            "This has previously been caused by Supabase-side resource "
            "contention on this project (confirmed by Supabase support) "
            "rather than a bug in the dashboard itself. If this keeps "
            "happening:\n\n"
            "1. Check the Supabase dashboard - if the database shows as "
            "unresponsive or unhealthy, try restarting the project from "
            "Project Settings.\n"
            "2. If it recurs frequently, the underlying compute tier may "
            "need upgrading - see the guidance Supabase support provided "
            "previously.\n\n"
            "Refreshing this page will retry once the underlying issue is "
            "resolved."
        )
        st.stop()


@st.cache_data(ttl=600)
def load_deposit_lifecycle_data():
    """
    One row per account: when they made their first deposit, and when
    they last made one. Feeds the 30-day retention chart only.

    FTD timestamps come from SOURCE_VIEW rather than the underlying
    "Affilka Data" table, so "Original player ID" is the identical
    column to the one load_roi_dash_data() returns - the chart is scoped
    by that ID against the sidebar-filtered frame, and sourcing both
    from the same place removes any chance of the two drifting apart.

    Deliberately a separate query from load_roi_dash_data() rather than
    extra columns on it. Two reasons: the view is at (account, activity
    month, commission) grain and both of these timestamps are
    account-level facts that would repeat across every one of an
    account's rows; and keeping it separate is what lets a failure here
    degrade to a missing chart instead of taking the dashboard down.

    Cached for 10 minutes to match load_roi_dash_data(), and uses the
    same extended statement timeout and get_healthy_connection() for the
    same reasons documented there. Unlike that query, a failure here
    degrades gracefully to an empty frame rather than st.stop() - this
    chart is one panel, not the whole dashboard, so it shouldn't be able
    to take the page down with it.
    """
    conn = get_healthy_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '45000'")  # milliseconds
        query = f'''
            WITH ftd AS (
                -- MIN() is the point of this CTE, not incidental: the
                -- view is at (account, Activity Month, Commission ID)
                -- grain, each row carrying "First deposit date", and the
                -- EARLIEST of those is the account's real FTD.
                -- Collapsing to one row per account here also stops the
                -- join below fanning out and counting the account once
                -- per commission-month in every denominator - the same
                -- multi-commission double-counting already found and
                -- fixed for Spiros_Fixed_Fee and Fixed Monthly Charge.
                --
                -- "Original player ID" is nullable upstream (it is
                -- "Affilka Data"."Account ID" surfaced under another
                -- name), and SQL would otherwise collapse every NULL
                -- into a single phantom account, so those rows are
                -- excluded outright.
                SELECT
                    "Original player ID" AS player_id,
                    MIN("First deposit date") AS ftd_at
                FROM "{SOURCE_VIEW}"
                WHERE "First deposit date" IS NOT NULL
                  AND "Original player ID" IS NOT NULL
                GROUP BY 1
            ),
            last_dep AS (
                -- Aggregated for the same reason: if customer_data ever
                -- holds more than one row per key, an un-aggregated join
                -- would silently double-weight those accounts in every
                -- percentage rather than erroring.
                SELECT
                    "{CUSTOMER_DATA_JOIN_KEY}"::text AS customer_key,
                    MAX("last_successful_deposit") AS last_deposit_at
                FROM "customer_data"
                GROUP BY 1
            )
            SELECT
                f.player_id,
                f.ftd_at,
                l.last_deposit_at
            FROM ftd f
            LEFT JOIN last_dep l
                   ON l.customer_key = f.player_id::text
        '''
        return pd.read_sql(query, conn)
    except Exception as e:
        st.warning(
            f"Couldn't load deposit lifecycle data ({e}) - the 30-day "
            "retention chart is unavailable. The rest of the dashboard is "
            "unaffected."
        )
        return pd.DataFrame(columns=["player_id", "ftd_at", "last_deposit_at"])


def allocate_fixed_monthly_charge(full_df, fixed_charge_amount):
    """
    Allocates a flat pool (fixed_charge_amount, e.g. £3,500) PER FTD
    MONTH, split EQUALLY across every distinct account whose FTD Month
    is that month - i.e. this is genuinely an acquisition cost, paid
    only in an account's own first month, not spread across their whole
    lifetime activity. Every OTHER row for that account (any month
    after their FTD month) gets £0.

    Uses full_df (the FULL, UNFILTERED "Affilka ROI Dash" dataset - not
    whatever subset the sidebar filters are currently narrowed to) so
    the £3,500-per-month pool is genuinely split across every real FTD
    that month, not just whichever subset a filter happens to leave in
    view. Callers should sum/filter the returned per-row Series AFTER
    allocation, not before.

    Written onto EXACTLY ONE row per account - the one with the lowest
    Commission ID among their FTD-month rows. An account can have
    multiple commissions active in their OWN FTD month, and "FTD Count"
    gets set to 1 on every one of those rows (see
    sync_monthly_activity_to_supabase.py's own FTD Count logic) - the
    same double-counting risk already found and fixed once for
    Spiros_Fixed_Fee, avoided here the same way: pick exactly one row
    per account, not one row per (account, commission) combination.

    Returns a pandas Series aligned with full_df's index - the
    allocated Fixed Monthly Charge for each individual row.
    """
    is_ftd_row = full_df["Activity Month"] == full_df["FTD Month"]
    ftd_rows = full_df[is_ftd_row].copy()

    # Exactly one row per account: lowest Commission ID among their
    # FTD-month rows, matching the same fix already applied to
    # Spiros_Fixed_Fee for the identical multi-commission-in-one-month
    # double-counting risk.
    ftd_rows = ftd_rows.sort_values("Commission ID")
    eligible_rows = ftd_rows.drop_duplicates(subset=["Original player ID"], keep="first")

    # Distinct account count per FTD Month - the true denominator, not
    # inflated by any account that had multiple commissions in their
    # own FTD month (already collapsed to one row above).
    ftd_count_by_month = eligible_rows.groupby("FTD Month").size()

    per_account_share = fixed_charge_amount / ftd_count_by_month.replace(0, pd.NA)
    eligible_rows["_allocated"] = eligible_rows["FTD Month"].map(per_account_share).fillna(0.0)

    allocated = pd.Series(0.0, index=full_df.index)
    allocated.loc[eligible_rows.index] = eligible_rows["_allocated"]
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
    Affiliate Costs, then Sum of Deductions/Profit/LTV last) rather
    than the original prototype's flat top-to-bottom list. See this
    module's docstring for the specific formula decisions that differ
    from the prototype.

    Sum of Deductions = Total Taxes & Duties + Total Other Fees &
    Adjustments + Total Affiliate Costs, but ONLY when
    include_affiliate_costs_in_ltv is True - otherwise Total Affiliate
    Costs is excluded from it, matching whatever the sidebar toggle
    says Profit/LTV should reflect. This means Profit's own formula
    simplifies to Total GGR - Total Bonus - Sum of Deductions with no
    separate Affiliate Costs subtraction step, since Sum of Deductions
    already accounts for it conditionally.

    Months whose total FTD Count (summed across every account in that
    cohort) is below min_ftd_count are dropped entirely from the result
    - a general filter for near-empty/junk months (e.g. a stray month
    with 1 FTD and negligible GGR right at the edge of the data) rather
    than a hardcoded exclusion of one specific month.

    Returns (table, months, total_rows, sections):
      total_rows: set of row labels that are SUMS of other rows (Total
        GGR, Total Bonus, Total Taxes & Duties, Total Other Fees &
        Adjustments, Total Affiliate Costs, Sum of Deductions, Profit,
        Player LTV) - styled distinctly (bold + shaded) from their
        component rows.
      sections: list of (parent_row_label, [detail_row_labels]) tuples,
        defining which detail rows belong under which top-level summary
        row - used to build the expand/collapse controls and to insert
        detail rows in the right position when a section is expanded.
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

    fixed_per_player = col_sum("Actual_Fixed_Fee")
    rev_share = col_sum("Actual_RS")
    fixed_monthly_charge = col_sum("Allocated Fixed Monthly Charge")
    vat = (fixed_per_player + rev_share + fixed_monthly_charge) * 0.2
    total_affiliate_costs = fixed_per_player + rev_share + fixed_monthly_charge + vat

    # Sum of Deductions - now includes Total Affiliate Costs, but ONLY
    # when the sidebar toggle says to include it in Profit/LTV. Moved
    # to sit just above Profit, rather than between Other Fees and
    # Affiliate Costs as it did before.
    if include_affiliate_costs_in_ltv:
        sum_of_deductions = total_taxes_and_duties + total_other_fees + total_affiliate_costs
        sum_of_deductions_label = "Sum of Deductions (incl. Affiliate Costs)"
        profit_label = "Profit (incl. Affiliate Costs)"
        ltv_label = "Player LTV (incl. Affiliate Costs)"
    else:
        sum_of_deductions = total_taxes_and_duties + total_other_fees
        sum_of_deductions_label = "Sum of Deductions (excl. Affiliate Costs)"
        profit_label = "Profit (excl. Affiliate Costs)"
        ltv_label = "Player LTV (excl. Affiliate Costs)"

    profit = total_ggr - total_bonus - sum_of_deductions
    player_ltv = (profit / ftd_count.replace(0, pd.NA)).fillna(0)

    rows = {}
    total_rows = set()
    sections = []

    # ── Volume ──
    rows["FTD Count"] = ftd_count
    rows["Deposits"] = deposits

    # ── Revenue ──
    rows["Total GGR"] = total_ggr
    total_rows.add("Total GGR")
    rows["  Casino GGR"] = casino_ggr
    rows["  SB GGR (incl. correction)"] = sb_ggr
    sections.append(("Total GGR", ["  Casino GGR", "  SB GGR (incl. correction)"]))

    # ── Bonuses ──
    rows["Total Bonus"] = total_bonus
    total_rows.add("Total Bonus")
    rows["  Free Casino Spins"] = free_casino_spins
    rows["  Free Sports Bets"] = free_sports_bets
    rows["  BOG Bonus"] = bog_bonus
    rows["  Lucky Bonus"] = lucky_bonus
    rows["  Bonus % of GGR"] = bonus_pct_of_ggr
    sections.append(("Total Bonus", ["  Free Casino Spins", "  Free Sports Bets", "  BOG Bonus", "  Lucky Bonus", "  Bonus % of GGR"]))

    # ── Taxes & Duties ──
    rows["Total Taxes & Duties"] = total_taxes_and_duties
    total_rows.add("Total Taxes & Duties")
    rows["  Casino Tax (RGD Duty)"] = casino_tax
    rows["  GBD Duty"] = gbd_duty
    rows["  HBLB Levy"] = hblb_levy
    rows["  Statutory Levy"] = statutory_levy
    sections.append(("Total Taxes & Duties", ["  Casino Tax (RGD Duty)", "  GBD Duty", "  HBLB Levy", "  Statutory Levy"]))

    # ── Other Fees & Adjustments ──
    rows["Total Other Fees & Adjustments"] = total_other_fees
    total_rows.add("Total Other Fees & Adjustments")
    rows["  Sportsbook Provider Fees"] = sportsbook_provider_fees
    rows["  Casino Provider Fees"] = casino_provider_fees
    rows["  Trading Adjustments"] = trading_adjustments
    rows["  Processing Fees"] = processing_fees
    rows["  Admin/Platform Fees"] = admin_platform_fees
    sections.append(("Total Other Fees & Adjustments", ["  Sportsbook Provider Fees", "  Casino Provider Fees", "  Trading Adjustments", "  Processing Fees", "  Admin/Platform Fees"]))

    # ── Affiliate Costs ──
    rows["Total Affiliate Costs"] = total_affiliate_costs
    total_rows.add("Total Affiliate Costs")
    rows["  fixed_per_player (FTD Month)"] = fixed_per_player
    rows["  Rev Share (FTD Month)"] = rev_share
    rows["  Fixed Monthly Charge"] = fixed_monthly_charge
    rows["  VAT"] = vat
    sections.append(("Total Affiliate Costs", ["  fixed_per_player (FTD Month)", "  Rev Share (FTD Month)", "  Fixed Monthly Charge", "  VAT"]))

    # ── Bottom line - always last ──
    rows[sum_of_deductions_label] = sum_of_deductions
    total_rows.add(sum_of_deductions_label)
    rows[profit_label] = profit
    total_rows.add(profit_label)
    rows[ltv_label] = player_ltv
    total_rows.add(ltv_label)

    table = pd.DataFrame(rows).T
    table = table[months]
    return table, months, total_rows, sections


def build_relative_month_series(df, include_affiliate_costs_in_ltv):
    """
    Computes CUMULATIVE metrics per (FTD Month cohort, Relative Month) -
    each cohort's running total up to and including that relative month
    (relative month 3's value already includes 1 and 2), for the
    "cumulative by cohort" line charts. One line per FTD Month cohort,
    x-axis is Relative Month (1 = the cohort's own FTD month, 2 = the
    month after, etc.).

    "Count of Players Depositing" is the one NON-cumulative metric here
    - a retention-style snapshot of how many distinct accounts in that
    cohort made at least one deposit specifically in that relative
    month, not a running total.

    Player LTV here divides cumulative Profit by each cohort's FTD
    Count, which is fixed per cohort (doesn't vary by relative month) -
    summed the same way build_cohort_table() does (raw "FTD Count"
    column, non-zero only on relative month 1 rows).

    Returns a dict of {metric_name: pivoted DataFrame}, each with
    Relative Month as the index and FTD Month as columns - ready to
    pass straight into st.line_chart().
    """
    d = df.copy()
    # Relative Month can be 0 or negative for a row representing
    # activity BEFORE an account's own FTD (see the "Pre or Post FTD"
    # column on "Affilka ROI Dash") - these don't belong in a chart
    # that's specifically about a cohort's economics SINCE acquisition,
    # so they're excluded here. Relative Month 1 is always an account's
    # own FTD month; it can never legitimately be lower than that for
    # this chart's purposes.
    d = d[d["Relative Month"] >= 1]

    d["Total GGR"] = d["Casino GGR"] + d["SB GGR"] + d["SB Correction"]
    d["Sports GGR"] = d["SB GGR"] + d["SB Correction"]

    per_period = d.groupby(["FTD Month", "Relative Month"]).agg(
        total_ggr=("Total GGR", "sum"),
        casino_ggr=("Casino GGR", "sum"),
        sports_ggr=("Sports GGR", "sum"),
        deposits=("Deposits sum", "sum"),
        free_spins=("Free Spins Payout", "sum"),
        free_bets=("Free Bet Payout", "sum"),
        bog=("BOG Bonus", "sum"),
        lucky=("Lucky Bonus", "sum"),
        rgd=("RGD Duty", "sum"),
        gbd=("GBD Duty", "sum"),
        hblb=("HBLB Levy", "sum"),
        statutory=("Statutory Levy", "sum"),
        dpf=("Data Provider Fees", "sum"),
        cpf=("Casino Provider Fee", "sum"),
        lcpf=("Live Casino Provider Fee", "sum"),
        vpf=("Virtuals Provider Fee", "sum"),
        trading_adj=("Trading Adjustments", "sum"),
        proc_fees=("Estimated Processing Fees", "sum"),
        admin_fees=("Admin/Platform Fees", "sum"),
        fpp=("Actual_Fixed_Fee", "sum"),
        rs=("Actual_RS", "sum"),
        fmc=("Allocated Fixed Monthly Charge", "sum"),
    ).reset_index()

    per_period["total_bonus"] = per_period["free_spins"] + per_period["free_bets"] + per_period["bog"] + per_period["lucky"]
    per_period["total_taxes"] = per_period["rgd"] + per_period["gbd"] + per_period["hblb"] + per_period["statutory"]
    per_period["total_other_fees"] = (
        per_period["dpf"] + per_period["cpf"] + per_period["lcpf"] + per_period["vpf"]
        + per_period["trading_adj"] + per_period["proc_fees"] + per_period["admin_fees"]
    )
    per_period["vat"] = (per_period["fpp"] + per_period["rs"] + per_period["fmc"]) * 0.2
    per_period["affiliate_costs"] = per_period["fpp"] + per_period["rs"] + per_period["fmc"] + per_period["vat"]

    if include_affiliate_costs_in_ltv:
        per_period["sum_of_deductions"] = per_period["total_taxes"] + per_period["total_other_fees"] + per_period["affiliate_costs"]
    else:
        per_period["sum_of_deductions"] = per_period["total_taxes"] + per_period["total_other_fees"]

    per_period["profit"] = per_period["total_ggr"] - per_period["total_bonus"] - per_period["sum_of_deductions"]

    # Cumulative sums, computed within each FTD Month cohort separately,
    # sorted by Relative Month first so cumsum() accumulates in the
    # right order even if a cohort has a gap (no rows) at some relative
    # month partway through.
    per_period = per_period.sort_values(["FTD Month", "Relative Month"])
    per_period["cum_total_ggr"] = per_period.groupby("FTD Month")["total_ggr"].cumsum()
    per_period["cum_casino_ggr"] = per_period.groupby("FTD Month")["casino_ggr"].cumsum()
    per_period["cum_sports_ggr"] = per_period.groupby("FTD Month")["sports_ggr"].cumsum()
    per_period["cum_deposits"] = per_period.groupby("FTD Month")["deposits"].cumsum()
    per_period["cum_profit"] = per_period.groupby("FTD Month")["profit"].cumsum()

    ftd_count_by_cohort = d.groupby("FTD Month")["FTD Count"].sum()
    per_period["ftd_count"] = per_period["FTD Month"].map(ftd_count_by_cohort)
    per_period["cum_player_ltv"] = (per_period["cum_profit"] / per_period["ftd_count"].replace(0, pd.NA)).fillna(0)

    # Count of Players Depositing - NOT cumulative, distinct accounts
    # with at least one deposit specifically in that relative month.
    depositing = (
        d[d["Deposits count"] > 0]
        .groupby(["FTD Month", "Relative Month"])["Original player ID"]
        .nunique()
        .reset_index(name="players_depositing")
    )

    # % of Players Still Depositing - each cohort's own Relative Month 1
    # depositing count as the 100% baseline (their own FTD month, when
    # by definition every FTD-ing account deposited), every later
    # relative month expressed as a % of that same cohort's own
    # baseline. NOT a % of the whole business or of FTD Count - purely
    # relative to that cohort's own starting depositing population, so
    # cohorts of very different sizes are still comparable on the same
    # 0-100% scale.
    baseline_by_cohort = (
        depositing[depositing["Relative Month"] == 1]
        .set_index("FTD Month")["players_depositing"]
    )
    depositing_pct = depositing.copy()
    depositing_pct["baseline"] = depositing_pct["FTD Month"].map(baseline_by_cohort)
    depositing_pct["pct_still_depositing"] = (
        100 * depositing_pct["players_depositing"] / depositing_pct["baseline"].replace(0, pd.NA)
    ).fillna(0)

    def pivot_metric(source_df, value_col):
        return source_df.pivot(index="Relative Month", columns="FTD Month", values=value_col).sort_index()

    return {
        "Cumulative Total GGR": pivot_metric(per_period, "cum_total_ggr"),
        "Cumulative Casino GGR": pivot_metric(per_period, "cum_casino_ggr"),
        "Cumulative Sports GGR": pivot_metric(per_period, "cum_sports_ggr"),
        "Cumulative Deposits": pivot_metric(per_period, "cum_deposits"),
        "Cumulative Player LTV": pivot_metric(per_period, "cum_player_ltv"),
        "Count of Players Depositing": pivot_metric(depositing, "players_depositing").fillna(0),
        # No .fillna(0) here, unlike every other chart's pivot - a
        # missing (Relative Month, FTD Month) combination genuinely
        # means that calendar month hasn't happened yet for that
        # cohort (e.g. an 08/26 FTD cohort has no 09/26 data at all
        # while it's still August), not that retention dropped to 0%.
        # Left as NaN, Altair breaks the line there instead of drawing
        # a misleading 0% point or interpolating across the gap - see
        # render_cumulative_chart().
        "% of Players Still Depositing": pivot_metric(depositing_pct, "pct_still_depositing"),
    }


def to_local_naive_date(series):
    """
    Reduces a timestamp column to a tz-naive calendar date in
    LOCAL_TIMEZONE, whatever it arrives as.

    Needed because the two timestamps feeding the retention chart come
    from different places and don't agree on tz-awareness: the view's
    "First deposit date" is timestamptz, while customer_data's
    last_successful_deposit lands naive. pandas refuses to compare or
    subtract across that boundary (TypeError: Invalid comparison between
    dtype=datetime64[ns, UTC] and Timestamp) rather than guessing, which
    is the right call - the two interpretations are a day apart for
    anything near midnight.

    Rules applied here:
      - tz-AWARE input is converted to LOCAL_TIMEZONE before the date is
        taken, so an FTD at 00:30 BST counts as that day rather than the
        one before.
      - tz-NAIVE input is assumed to ALREADY be local wall-clock time
        (it comes from Drive CSV exports of a UK-facing system) and is
        left where it is. Localising it to UTC first and converting
        would shift it by an hour in summer, in the wrong direction.
      - mixed UTC offsets in one column (which a London-local column
        spanning a DST change will have) make pandas RAISE
        "Mixed timezones detected" rather than return something usable,
        so that's caught and re-parsed via UTC. Verified by test rather
        than assumed - an earlier version of this checked the returned
        dtype instead, and the check was unreachable.
    """
    try:
        parsed = pd.to_datetime(series, errors="coerce")
    except ValueError:
        parsed = pd.to_datetime(series, errors="coerce", utc=True)
    if not pd.api.types.is_datetime64_any_dtype(parsed):
        parsed = pd.to_datetime(series, errors="coerce", utc=True)
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        parsed = parsed.dt.tz_convert(LOCAL_TIMEZONE).dt.tz_localize(None)
    return parsed.dt.normalize()


def build_relative_day_retention(
    lifecycle_df,
    cohort_months=None,
    window=RELATIVE_DAY_WINDOW,
    min_observed=MIN_OBSERVED_ACCOUNTS_PER_POINT,
    as_of=None,
):
    """
    Builds "% of Players Still Depositing" by RELATIVE DAY (1-window),
    one line per FTD Month cohort - the same cohort grouping and the
    same MM/YY labels as every other chart in this app, so colours and
    legend order stay consistent.

    Day 1 is the account's OWN FTD day. An account counts as "still
    depositing" at day N if its last successful deposit falls on or
    after its own day N.

    THIS IS A SURVIVAL CURVE, NOT A PER-DAY SNAPSHOT. customer_data's
    last_successful_deposit is ONE timestamp per account, not a deposit
    event log, so "did this account deposit ON day N" is not answerable
    from it. What IS answerable is "was this account's deposit lifetime
    still running at day N". Concretely, versus the monthly
    "% of Players Still Depositing" chart above:

      - Monthly chart: an account with no deposit in relative month 3
        but one in month 4 is absent from month 3 and present in month
        4. That line can go back up.
      - This chart: an account whose last deposit is day 25 counts as
        "still depositing" on every day 1-25, including days it made no
        deposit at all. This line can only go down.

    For a 30-day window that's the more readable chart regardless - a
    literal "% depositing ON day N" would fall to near-zero by day 3,
    since very few accounts deposit daily.

    THE DENOMINATOR IS PER-DAY, NOT PER-COHORT. At day N, only accounts
    that have actually had N days elapsed since their own FTD are
    counted. This matters more than it sounds: a monthly cohort spans up
    to 31 FTD dates, so mid-month a single cohort contains accounts with
    wildly different observation windows. Using the whole cohort as a
    fixed denominator would make every young cohort's curve slope
    downward purely because its late-in-the-month signups haven't had
    time to deposit again yet - a censoring artefact that looks
    identical to real churn. This is the day-level equivalent of the
    NaN-not-zero handling in build_relative_month_series().

    A consequence worth knowing: the denominator shrinks as N grows for
    any cohort still inside its own 30-day window, so those tails are
    built on fewer accounts. min_observed suppresses the points where
    that gets too thin.

    Accounts with no recorded last_successful_deposit are DROPPED, not
    counted. Two consequences to keep in mind: every cohort's
    denominator here is "accounts with a recorded last deposit" rather
    than its FTD Count, so cohort sizes won't tie back to the table
    above; and if those NULLs represent a customer_data coverage gap
    rather than genuinely inactive accounts, retention is overstated by
    exactly the accounts most likely to have lapsed. The call site
    reports how many were dropped so that's visible rather than assumed.

    An account whose last deposit is dated BEFORE its own FTD is a data
    inconsistency rather than a missing value, so it's clamped to the
    FTD date (i.e. counted as day-1-only) rather than dropped.

    as_of defaults to today, and is a parameter mainly so this is
    testable against a fixed date.

    Returns a wide DataFrame - index "Relative Day", one column per FTD
    Month, values 0-100. NaN is left in deliberately (no .fillna(0)) for
    (cohort, day) combinations that either haven't happened yet or fall
    below min_observed, so Altair breaks the line rather than drawing a
    0% point that reads as total churn.
    """
    empty = pd.DataFrame(index=pd.RangeIndex(1, window + 1, name="Relative Day"))
    if lifecycle_df.empty:
        return empty

    d = lifecycle_df.dropna(subset=["ftd_at"]).copy()
    if d.empty:
        return empty

    # Both reduced to tz-naive local calendar dates before anything is
    # compared or subtracted - see to_local_naive_date(). The two
    # columns come from different sources and disagree on tz-awareness,
    # which pandas treats as an error rather than silently picking an
    # interpretation.
    d["ftd_date"] = to_local_naive_date(d["ftd_at"])
    d["last_date"] = to_local_naive_date(d["last_deposit_at"])
    d = d.dropna(subset=["ftd_date"])

    # No recorded last deposit -> removed entirely, so these accounts
    # appear in no denominator at all. See docstring for what that does
    # to the interpretation of the curve.
    d = d.dropna(subset=["last_date"])
    if d.empty:
        return empty

    # Last deposit predating the account's own FTD is an inconsistency,
    # not a missing value - clamped rather than dropped.
    d.loc[d["last_date"] < d["ftd_date"], "last_date"] = d["ftd_date"]

    # Same MM/YY cohort label the rest of the app uses, derived here from
    # the FTD timestamp. If the ROI dash view derives its own "FTD Month"
    # any other way (e.g. from an Affilka-supplied month field with a
    # different timezone or cutoff), a handful of accounts near a month
    # boundary could land in a different cohort here than they do in the
    # table above.
    d["FTD Month"] = d["ftd_date"].dt.strftime("%m/%y")

    if cohort_months is not None:
        d = d[d["FTD Month"].isin(cohort_months)]
    if d.empty:
        return empty

    as_of_ts = pd.Timestamp(as_of).normalize() if as_of is not None else pd.Timestamp.today().normalize()

    d["last_relative_day"] = (d["last_date"] - d["ftd_date"]).dt.days + 1
    d["days_observed"] = (as_of_ts - d["ftd_date"]).dt.days + 1

    records = []
    for month, cohort in d.groupby("FTD Month"):
        observed = cohort["days_observed"].to_numpy()
        last_day = cohort["last_relative_day"].to_numpy()
        for day in range(1, window + 1):
            eligible = observed >= day
            n_eligible = int(eligible.sum())
            if n_eligible < min_observed:
                pct = float("nan")
            else:
                still = int((last_day[eligible] >= day).sum())
                pct = 100.0 * still / n_eligible
            records.append({"FTD Month": month, "Relative Day": day, "pct": pct})

    long = pd.DataFrame(records)
    wide = long.pivot(index="Relative Day", columns="FTD Month", values="pct").astype(float)
    return wide.reindex(range(1, window + 1)).sort_index()


def build_ranking_table(df, group_col, include_affiliate_costs_in_ltv):
    """
    Groups the (already-filtered) dataframe by group_col (Partner ID,
    Campaign ID, or Commission ID) and computes each group's LIFETIME
    Profit and Player LTV, sorted highest-LTV-first.

    Affiliate Costs here includes "Allocated Fixed Monthly Charge" -
    now that it's genuinely row-level (see allocate_fixed_monthly_charge()),
    summing it by group_col is exactly as valid as summing Casino GGR by
    group_col - matching Actual_Fixed_Fee and Actual_RS, which were
    always genuinely row-level in the source view.

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


def render_cumulative_chart(df_wide, is_percent=False, x_field="Relative Month", y_title=None, x_zero=True):
    """
    Renders a wide-format DataFrame (index = x_field, one column per FTD
    Month cohort) as a line chart with an auto-scaling Y-axis -
    st.line_chart() always forces the Y-axis to start at 0 with no way
    to override that, which compresses later, higher-value data toward
    the top of the chart and hides smaller month-to-month movement.
    Building the chart directly in Altair (alt.Scale(zero=False)) lets
    the axis scale to the data's actual range instead.

    is_percent=True formats the Y-axis with a "%" suffix (values are
    already expected to be on a 0-100 scale, not 0-1) and keeps the
    axis anchored at zero - unlike the currency charts, 0-100% is a
    natural, meaningful bound worth keeping visible rather than
    auto-scaling away.

    x_field names the index column, so the same renderer handles both
    the "Relative Month" series and the "Relative Day" retention chart.

    x_zero=False drops the 0 tick from the x-axis. Every series here
    starts at period 1, so a 0 tick is dead space that shifts the first
    real data point away from the axis. Defaults to True purely to keep
    the existing monthly charts looking as they always have - flip the
    call sites if you want them consistent with the daily one.
    """
    df_long = df_wide.reset_index().melt(
        id_vars=x_field, var_name="FTD Month", value_name="value"
    )
    # Altair sorts a nominal color field alphabetically as strings by
    # default (e.g. "01/26" before "11/25", since "0" < "1"
    # lexicographically) - explicitly sorting by the same
    # month_sort_key() used everywhere else in this app gives the
    # legend (and matching line/point colors) true chronological order
    # instead.
    cohort_order = sorted(df_long["FTD Month"].dropna().unique(), key=month_sort_key)
    if y_title is None:
        y_title = "% still depositing" if is_percent else ""
    y_axis = alt.Y(
        "value:Q",
        title=y_title,
        scale=alt.Scale(zero=True) if is_percent else alt.Scale(zero=False),
        axis=alt.Axis(format=".0f") if is_percent else alt.Axis(),
    )
    chart = (
        alt.Chart(df_long)
        .mark_line(point=True)
        .encode(
            x=alt.X(
                f"{x_field}:Q",
                title=x_field,
                scale=alt.Scale(zero=x_zero),
                axis=alt.Axis(tickMinStep=1, format="d"),
            ),
            y=y_axis,
            color=alt.Color("FTD Month:N", title="FTD Month", sort=cohort_order),
            tooltip=["FTD Month", x_field, "value"],
        )
        .properties(height=350)
    )
    st.altair_chart(chart, use_container_width=True)


def render_cohort_table_html(table, total_rows, visible_rows):
    """
    Renders the cohort table as a raw HTML <table> via st.markdown,
    rather than st.dataframe - Streamlit's native dataframe component
    (glide-data-grid) doesn't reliably support per-row hover tooltips
    even when the underlying pandas Styler has them set via
    .set_tooltips(), since it renders to a canvas rather than real HTML.
    A hand-built table with a native HTML title="..." attribute on each
    row label gives a reliable browser-native tooltip regardless of
    that limitation.

    visible_rows: ordered list of row labels to actually render (already
    filtered down to whichever top-level rows plus any expanded detail
    rows the caller wants shown - this function doesn't decide which
    rows appear, only how to render whichever list it's given).

    Total/subtotal rows (in total_rows) get bold text and a shaded
    background so it's visually obvious which rows are sums of the
    detail rows versus individual line items.
    """
    import html as html_module

    def format_cell(row_name, value):
        if row_name in PERCENT_ROWS:
            return format_pct(value)
        elif row_name in COUNT_ROWS:
            return "" if pd.isna(value) else f"{value:,.0f}"
        else:
            return format_currency(value)

    months = list(table.columns)

    header_cells = "".join(f"<th>{html_module.escape(str(m))}</th>" for m in months)
    header_row = f"<tr><th>Metric</th>{header_cells}</tr>"

    body_rows = []
    for row_name in visible_rows:
        if row_name not in table.index:
            continue
        is_total = row_name in total_rows
        row_class = "total-row" if is_total else "detail-row"
        explanation = ROW_EXPLANATIONS.get(row_name, "")
        title_attr = html_module.escape(explanation).replace("\n", "&#10;") if explanation else ""
        label_html = html_module.escape(row_name)

        cells = "".join(
            f"<td>{format_cell(row_name, table.loc[row_name, m])}</td>" for m in months
        )
        body_rows.append(
            f'<tr class="{row_class}">'
            f'<td class="row-label" title="{title_attr}">{label_html}</td>'
            f'{cells}'
            f'</tr>'
        )

    # Built as a SINGLE unbroken line with zero leading whitespace on
    # any line - Streamlit's markdown renderer treats 4+ leading spaces
    # on a line as a code block (standard Markdown behaviour), which
    # would escape these HTML tags into literal visible text instead of
    # rendering them. A dedented multi-line string can still trip this
    # if any inner line (e.g. indented CSS rules) keeps its indentation
    # - collapsing everything to one line sidesteps the issue entirely,
    # regardless of how the parser handles edge cases.
    style = (
        "<style>"
        ".roi-table-wrap{overflow-x:auto;}"
        ".roi-table{border-collapse:collapse;width:100%;font-size:0.9rem;}"
        ".roi-table th,.roi-table td{padding:6px 10px;text-align:right;white-space:nowrap;}"
        ".roi-table th:first-child,.roi-table td:first-child{text-align:left;}"
        ".roi-table thead th{border-bottom:1px solid rgba(120,120,120,0.4);font-weight:600;}"
        ".roi-table .total-row{font-weight:bold;background-color:rgba(120,120,120,0.18);}"
        ".roi-table .row-label{cursor:help;}"
        ".roi-table .detail-row .row-label{padding-left:24px;opacity:0.9;}"
        "</style>"
    )

    table_html = (
        f'{style}<div class="roi-table-wrap"><table class="roi-table">'
        f'<thead>{header_row}</thead><tbody>{"".join(body_rows)}</tbody></table></div>'
    )
    st.markdown(table_html, unsafe_allow_html=True)


PERCENT_ROWS = {"  Bonus % of GGR"}
COUNT_ROWS = {"FTD Count"}
# Every row not in PERCENT_ROWS or COUNT_ROWS is a currency row -
# formatting is now driven by exclusion rather than an explicit set,
# since row labels change dynamically (Profit/LTV's label depends on
# the affiliate-costs toggle).

# Hover-tooltip text for each row label, shown when hovering the row
# name in the first column. Sourced from the original prototype
# spreadsheet's Column B ("Where it comes from?"), adapted where this
# dashboard's actual formula differs from what that column originally
# described (e.g. SB GGR now folds in SB Correction; Fixed Monthly
# Charge is now a flat £3,500 per FTD Month rather than the
# spreadsheet's placeholder text; Admin/Platform Fees uses Combined
# Stake rather than Affilka's own stake columns).
ROW_EXPLANATIONS = {
    "FTD Count": "Source: Affilka API\n\nFTD count direct from Affilka.",
    "Deposits": "Source: Affilka API\n\nDeposits Sum direct from Affilka.",
    "Total GGR": "Casino GGR + SB GGR (SB GGR already includes SB Correction, so it isn't added again separately here).",
    "  Casino GGR": "Source: Affilka API\n\nCasino GGR direct from Affilka.",
    "  SB GGR (incl. correction)": (
        "Source: Affilka API + All Bets Master Log\n\n"
        "Affilka's own sb_ggr − (sb_bets_sum − sb_settled_bets_sum), PLUS the difference "
        "between that figure and the All Bets Master Log truth (SB Correction) - both "
        "folded into this one figure."
    ),
    "Total Bonus": "Free Casino Spins + Free Sports Bets + BOG Bonus + Lucky Bonus.",
    "  Free Casino Spins": "Source: Casino, Live Casino & Virtuals Drive reports\n\nSum of 'Free Spins Results' across all three products.",
    "  Free Sports Bets": "Source: All Bets Master Log\n\nSum of Payout for bets where Is Free Bet = Yes.",
    "  BOG Bonus": "Source: Trading Drive report\n\nAllocated by Player/Month/Commission, scaled by sb_bets_sum.",
    "  Lucky Bonus": "Source: Trading Drive report\n\nAllocated by Player/Month/Commission, scaled by sb_bets_sum.",
    "  Bonus % of GGR": "Total Bonus ÷ Total GGR.",
    "Total Taxes & Duties": "Casino Tax (RGD Duty) + GBD Duty + HBLB Levy + Statutory Levy.",
    "  Casino Tax (RGD Duty)": (
        "Source: Casino, Live Casino & Virtuals Drive reports\n\n"
        "combined_duty_base = SUM over {Casino, Live Casino, Virtuals} of (that product's "
        "monthly GGR + monthly Free Spins count × £0.10)\n"
        "combined_duty_pool = max(0, 40% × combined_duty_base)\n"
        "account's share = combined_duty_pool × (account's combined stake across all 3 "
        "products ÷ total combined stake across every account, that month)."
    ),
    "  GBD Duty": (
        "Source: Trading Drive report\n\n"
        "month_total_pnl = SUM(sports_bet_ngr) across every Wallet Code, that month\n"
        "month_total_fb_stake = SUM(Free Bet Stake), that month\n"
        "duty_pool = max(0, 15% × (month_total_pnl + month_total_fb_stake))\n"
        "account's share = duty_pool × (account's own sports_bet_ngr ÷ month_total_pnl)."
    ),
    "  HBLB Levy": (
        "Source: All Bets Master Log, Horse Racing bets only\n\n"
        "month_total_net = SUM(Total stake) − SUM(Payout), settled Horse Racing bets that month\n"
        "duty_pool = max(0, 10% × month_total_net)\n"
        "account's share = duty_pool × (account's own Horse Racing net ÷ month_total_net)."
    ),
    "  Statutory Levy": (
        "Source: Trading + Casino/Live Casino/Virtuals Drive reports\n\n"
        "combined_contribution = sports_bet_ngr + Free Bet Stake + Casino/Live Casino/Virtuals "
        "GGR (+ Free Spins × £0.10 each)\n"
        "duty_pool = max(0, 1.1% × month_total_contrib)\n"
        "account's share = duty_pool × (account's own combined_contribution ÷ month_total_contrib)."
    ),
    "Total Other Fees & Adjustments": (
        "Sportsbook Provider Fees + Casino Provider Fees + Trading Adjustments + "
        "Processing Fees + Admin/Platform Fees."
    ),
    "  Sportsbook Provider Fees": "Source: All Bets Master Log\n\nSum of the Data Provider Fees column.",
    "  Casino Provider Fees": (
        "Source: Casino, Live Casino & Virtuals Drive reports\n\n"
        "Calculated per product and summed:\n"
        "standalone_duty_base = that product's monthly GGR + Free Spins count × £0.10\n"
        "standalone_duty_pool = 40% × standalone_duty_base (not netted with other products)\n"
        "fee_pool = provider_fee_rate × (GGR − standalone_duty_pool)\n"
        "account's share = fee_pool × (account's stake in that product ÷ total stake, that "
        "product, that month)."
    ),
    "  Trading Adjustments": "Source: Trading Drive report\n\nDirect from the Trading Adjustments column.",
    "  Processing Fees": (
        "Source: Affilka API\n\n"
        "pool = 5% × greatest(0, that month's combined SB GGR + Casino GGR, summed across "
        "every account)\n"
        "account's share = pool × (this commission's Deposits Count + Cashouts Count ÷ that "
        "month's combined Deposits + Cashouts, across every account)."
    ),
    "  Admin/Platform Fees": (
        "Source: Affilka API + Customer Trading Data Monthly\n\n"
        "pool = £80,000 flat, per Activity Month, spanning the whole business\n"
        "account's share = pool × (this account's Combined Stake ÷ whole-business Combined "
        "Stake, that month)."
    ),
    "Total Affiliate Costs": "fixed_per_player + Rev Share + Fixed Monthly Charge + VAT.",
    "  fixed_per_player (FTD Month)": (
        "Source: CPA By Cohort spreadsheet\n\n"
        "For each (Partner ID, FTD Month) cohort with real cost data:\n"
        "fee_per_account = cohort's total CPA cost ÷ count of eligible accounts (FTD Count = 1, "
        "not frozen in their first month)\n"
        "written onto each eligible account's own FTD-month row.\n\n"
        "Falls back to Affilka's own reported fixed_per_player figure if no CPA By Cohort "
        "entry exists."
    ),
    "  Rev Share (FTD Month)": (
        "Source: RS By Cohort spreadsheet\n\n"
        "For each (Partner ID, FTD Month, Activity Month) triple with real cost data:\n"
        "pool = that triple's RS Amount\n"
        "each row's share = pool × (this row's GGR ÷ total GGR across every row in that triple)\n\n"
        "Falls back to Affilka's own reported ngr_percent if no RS By Cohort entry exists. "
        "Summed across every month the cohort has been active - lifetime revenue share to date."
    ),
    "  Fixed Monthly Charge": (
        "Source: hardcoded, £3,500 per FTD Month\n\n"
        "Split equally across that month's distinct new FTDs, written onto each account's own "
        "FTD-month row only - every later Activity Month for that account is £0."
    ),
    "  VAT": "20% × (fixed_per_player + Rev Share + Fixed Monthly Charge).",
    "Sum of Deductions (excl. Affiliate Costs)": "Total Taxes & Duties + Total Other Fees & Adjustments.",
    "Sum of Deductions (incl. Affiliate Costs)": (
        "Total Taxes & Duties + Total Other Fees & Adjustments + Total Affiliate Costs."
    ),
    "Profit (excl. Affiliate Costs)": "Total GGR − Total Bonus − Sum of Deductions.",
    "Profit (incl. Affiliate Costs)": "Total GGR − Total Bonus − Sum of Deductions.",
    "Player LTV (excl. Affiliate Costs)": "Profit ÷ FTD Count.",
    "Player LTV (incl. Affiliate Costs)": "Profit ÷ FTD Count.",
}


# ── MAIN APP ──────────────────────────────────────────────────────────

st.title("FTD Cohort ROI Dashboard")
st.caption(
    "Every column is an FTD-month cohort's activity to date - a player who signed up "
    "in Nov-25 contributes their Aug-26 spend to the Nov-25 column too."
)

with st.spinner("Loading data..."):
    df = load_roi_dash_data()

# Allocate Fixed Monthly Charge on the FULL, unfiltered dataset - see
# allocate_fixed_monthly_charge()'s docstring for why this must happen
# before any Partner/Campaign/Commission filtering, not after.
df["Allocated Fixed Monthly Charge"] = allocate_fixed_monthly_charge(df, FIXED_MONTHLY_CHARGE_AMOUNT)

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

st.sidebar.divider()
exclude_outlier_accounts = st.sidebar.checkbox(
    "Exclude top 5% and bottom 5% of accounts by Total GGR",
    value=False,
    help=(
        "Removes every row belonging to an account whose LIFETIME Total GGR "
        "(summed across all their rows, within whatever Partner/Campaign/"
        "Commission filter is currently applied) falls in the top or bottom "
        "5% of accounts. Removing an account removes ALL of its figures - "
        "GGR, bonuses, taxes, fees, affiliate costs - not just its GGR "
        "contribution, since every one of those is computed per-row and "
        "summed."
    ),
)

filtered = df.copy()
if selected_partners:
    filtered = filtered[filtered["Partner ID"].isin(selected_partners)]
if selected_campaigns:
    filtered = filtered[filtered["Campaign ID"].isin(selected_campaigns)]
if selected_commissions:
    filtered = filtered[filtered["Commission ID"].isin(selected_commissions)]

if exclude_outlier_accounts and not filtered.empty:
    # Total GGR per row, same formula used everywhere else in this app
    # (Casino GGR + SB GGR, which already includes SB Correction).
    row_total_ggr = filtered["Casino GGR"] + filtered["SB GGR"] + filtered["SB Correction"]
    account_lifetime_ggr = (
        pd.Series(row_total_ggr.values, index=filtered["Original player ID"])
        .groupby(level=0)
        .sum()
    )
    lower_threshold = account_lifetime_ggr.quantile(0.05)
    upper_threshold = account_lifetime_ggr.quantile(0.95)
    excluded_accounts = account_lifetime_ggr[
        (account_lifetime_ggr <= lower_threshold) | (account_lifetime_ggr >= upper_threshold)
    ].index

    before_count = filtered["Original player ID"].nunique()
    filtered = filtered[~filtered["Original player ID"].isin(excluded_accounts)]
    st.sidebar.caption(
        f"Excluded {len(excluded_accounts):,} of {before_count:,} accounts "
        f"(Total GGR outside £{lower_threshold:,.0f}-£{upper_threshold:,.0f})."
    )

if filtered.empty:
    st.warning("No data matches the selected filters.")
    st.stop()

# ── TABS ─────────────────────────────────────────────────────────────

tab_cohort, tab_partner, tab_campaign, tab_commission = st.tabs([
    "FTD Cohort View", "By Partner ID", "By Campaign ID", "By Commission ID",
])

with tab_cohort:
    table, months, total_rows, sections = build_cohort_table(filtered, include_affiliate_costs, min_ftd_count)

    # Map each detail row label back to its parent section, so a
    # section only shows its detail rows when explicitly expanded.
    detail_to_parent = {}
    for parent, details in sections:
        for d in details:
            detail_to_parent[d] = parent

    expanded_sections = st.multiselect(
        "Show detail rows for:",
        options=[s[0] for s in sections],
        default=[],
        help="Pick a section to reveal the individual line items that make up its total.",
    )

    visible_rows = [
        row_name for row_name in table.index
        if row_name not in detail_to_parent or detail_to_parent[row_name] in expanded_sections
    ]

    render_cohort_table_html(table, total_rows, visible_rows)

    # ── CUMULATIVE BY RELATIVE MONTH CHARTS ──
    # One line per FTD Month cohort, x-axis is Relative Month (1 =
    # the cohort's own FTD month). Uses `filtered` (not the full `df`)
    # so these respect the same Partner/Campaign/Commission sidebar
    # filters as the table above - and is further narrowed to `months`
    # (the same min_ftd_count-filtered cohort list the table above
    # uses), so a near-empty/junk cohort with a wildly distorted LTV
    # (e.g. a single account with FTD Count = 1) doesn't drag the
    # charts' Y-axis scale away from every other cohort's real values.
    st.divider()
    st.subheader("Cumulative by FTD cohort")
    chart_data = filtered[filtered["FTD Month"].isin(months)]
    relative_month_charts = build_relative_month_series(chart_data, include_affiliate_costs)

    chart_pairs = list(relative_month_charts.items())

    # ── 30 DAYS % OF PLAYERS STILL DEPOSITING ──
    # Appended to the same grid as the monthly charts rather than given
    # its own section, but fed by its own query (account-level FTD +
    # last deposit timestamps) rather than by the ROI dash view - see
    # load_deposit_lifecycle_data() and build_relative_day_retention().
    # Everything below is guarded so that a failure, a missing table or
    # a bad join key drops this one panel from the grid and leaves the
    # other six untouched.
    DAY_CHART_TITLE = f"{RELATIVE_DAY_WINDOW} Days % of Players Still Depositing"
    day_chart_note = None

    lifecycle = load_deposit_lifecycle_data()
    if not lifecycle.empty:
        # Restricting by player_id against `filtered` makes this chart
        # respect every sidebar control - Partner/Campaign/Commission and
        # the outlier exclusion - without reimplementing any of that
        # filtering logic here.
        #
        # Both sides are cast to str first: "Original player ID" and
        # customer_data's key can differ in dtype (int64 vs object)
        # between the two queries, and .isin() across mismatched dtypes
        # matches NOTHING silently rather than erroring - which would
        # render an empty chart that looks like a data problem upstream.
        filtered_ids = set(filtered["Original player ID"].dropna().astype(str))
        lifecycle_scoped = lifecycle[lifecycle["player_id"].astype(str).isin(filtered_ids)]

        if lifecycle_scoped.empty:
            day_chart_note = (
                "No accounts matched between the ROI dash and the deposit "
                f"lifecycle query - check that customer_data.{CUSTOMER_DATA_JOIN_KEY} "
                "is the same identifier as \"Original player ID\"."
            )
        else:
            # Reported rather than silent: these accounts are excluded
            # from every denominator, so if the number is large the
            # curves are describing a self-selected subset. A high figure
            # is also the first symptom of a wrong join key, since a bad
            # key produces NULLs rather than an error.
            n_matched = len(lifecycle_scoped)
            n_no_last_deposit = int(lifecycle_scoped["last_deposit_at"].isna().sum())
            if n_no_last_deposit:
                day_chart_note = (
                    f"Excluded {n_no_last_deposit:,} of {n_matched:,} accounts with no "
                    "recorded last successful deposit."
                )
            chart_pairs.append((
                DAY_CHART_TITLE,
                build_relative_day_retention(lifecycle_scoped, cohort_months=months),
            ))

    for i in range(0, len(chart_pairs), 2):
        cols = st.columns(2)
        for col, (chart_title, chart_df) in zip(cols, chart_pairs[i:i + 2]):
            with col:
                st.caption(chart_title)
                is_day_chart = chart_title == DAY_CHART_TITLE
                render_cumulative_chart(
                    chart_df,
                    is_percent=is_day_chart or chart_title == "% of Players Still Depositing",
                    x_field="Relative Day" if is_day_chart else "Relative Month",
                    # Only the daily chart drops the 0 tick, so the
                    # monthly charts keep the look they already had. Set
                    # this to False unconditionally if you'd rather all
                    # seven axes started at 1.
                    x_zero=not is_day_chart,
                )
                if is_day_chart and day_chart_note:
                    st.caption(day_chart_note)

    st.caption(f"Data loaded: {datetime.now().strftime('%Y-%m-%d %H:%M')} (cached for 10 minutes)")


def render_ranking_tab(tab, group_col, label):
    with tab:
        st.subheader(f"Ranked by Player LTV - {label}")
        st.caption(
            "Lifetime totals across every FTD cohort, ranked highest Player LTV first. "
            "Affiliate Costs includes Fixed Monthly Charge, split equally across each "
            "FTD Month's new signups and attributed to their own acquisition month."
        )
        result, profit_label, ltv_label = build_ranking_table(filtered, group_col, include_affiliate_costs)
        display = format_ranking_table(result, profit_label, ltv_label)
        st.dataframe(display, use_container_width=True, height=min(35 * len(display) + 80, 700))


render_ranking_tab(tab_partner, "Partner ID", "Partner ID")
render_ranking_tab(tab_campaign, "Campaign ID", "Campaign ID")
render_ranking_tab(tab_commission, "Commission ID", "Commission ID")
