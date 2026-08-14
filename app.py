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

# A flat, genuinely fixed cost applied to every Activity Month - no UI
# to edit this, since it doesn't vary. If this ever needs to change,
# update the number here directly (and the app will pick it up on its
# next deploy) rather than via a database-backed editor.
FIXED_MONTHLY_CHARGE_AMOUNT = 3500.0

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

    "Original player ID" (the account's own ID) and "Activity Month"
    are needed to identify each account's own FTD-month row, for
    allocating Fixed Monthly Charge - see allocate_fixed_monthly_charge().
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
        profit_label = "Profit (incl. Affiliate Costs)"
        ltv_label = "Player LTV (incl. Affiliate Costs)"
    else:
        sum_of_deductions = total_taxes_and_duties + total_other_fees
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
    rows["Sum of Deductions"] = sum_of_deductions
    total_rows.add("Sum of Deductions")
    rows[profit_label] = profit
    total_rows.add(profit_label)
    rows[ltv_label] = player_ltv
    total_rows.add(ltv_label)

    table = pd.DataFrame(rows).T
    table = table[months]
    return table, months, total_rows, sections


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
    "Sum of Deductions": (
        "Total Taxes & Duties + Total Other Fees & Adjustments + Total Affiliate Costs "
        "(only when 'Include Affiliate Costs in Profit / Player LTV' is ticked)."
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

        st.bar_chart(result[ltv_label])


render_ranking_tab(tab_partner, "Partner ID", "Partner ID")
render_ranking_tab(tab_campaign, "Campaign ID", "Campaign ID")
render_ranking_tab(tab_commission, "Commission ID", "Commission ID")
