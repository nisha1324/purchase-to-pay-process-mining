"""Where items wait, and whether that wait is the process, the terms or the payment calendar.

    python -m scripts.bottlenecks

Writes results/BOTTLENECKS.md, 3 CSV files and 4 charts. Every number comes from
sql/20_waits.sql, sql/bottlenecks.sql and p2p/survival.py.
"""
import argparse
from pathlib import Path

import matplotlib
import matplotlib.dates

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

from p2p.db import SQL_DIR, build, connect, named_queries, run
from p2p.source import (CHARTS, CITATION, FOLLOW_UP_END, FOLLOW_UP_START, LICENCE, LOG_END_DAY,
                        MAIN_RUN_MIN_ITEMS, RESULTS, RUN_MIN_ITEMS, RUN_TIE_MIN_ITEMS,
                        RUN_TIE_SHARE, TERM_MIN_ITEMS)
from p2p.survival import day_reached
from scripts.compliance import AFTER, AQUA, MAIN, YELLOW
from scripts.profile_log import (BLUE, GRID, INK, INK_2, MONTHS, MUTED, ORANGE, SURFACE, headline,
                                 pct, save, style, table, whole)

ALL_ITEMS, WEIGHTED = "All 3-way items", "Weighted by value"

# Stage, how long it took, and how long an unfinished item had been waiting when the log stopped.
STAGES = (
    ("PO to first goods receipt", "po_to_goods", "open_from_po"),
    ("PO to vendor invoice date", "po_to_invoice_date", "open_from_po"),
    ("Invoice date to invoice recorded", "invoice_date_to_recorded", "open_from_invoice_date"),
    ("Invoice recorded to first clearing", "recorded_to_clear", "open_from_recorded"),
    ("Goods receipt to first clearing", "goods_to_clear", "open_from_goods"),
    ("PO to first clearing", "po_to_clear", "open_from_po"),
)

PARTS = (("to_invoice", "PO to invoice date", BLUE, "#ffffff"), ("term", "Implied term", ORANGE, INK),
         ("overrun", "Process overrun", AQUA, INK), ("run_wait", "Waiting for a run", YELLOW, INK))

STEP_LABELS = {
    "Create Purchase Order Item": "PO created",
    "Vendor creates invoice": "Invoice dated",
    "Record Goods Receipt": "Goods received",
    "Record Invoice Receipt": "Invoice recorded",
    "Remove Payment Block": "Block removed",
    "Clear Invoice": "Cleared",
}
# Where each step sits on the map (x, y), left to right in the usual order of events.
MAP_LAYOUT = {
    "Create Purchase Order Item": (0.06, 0.5),
    "Vendor creates invoice": (0.30, 0.84),
    "Record Goods Receipt": (0.30, 0.16),
    "Record Invoice Receipt": (0.56, 0.5),
    "Remove Payment Block": (0.76, 0.14),
    "Clear Invoice": (0.93, 0.5),
}
MAP_MIN_SHARE = 0.05  # edges with at least this share of items are drawn
# Each drawn edge: how far it bends, and where its label sits.
MAP_EDGES = {
    ("Create Purchase Order Item", "Vendor creates invoice"): (-0.1, (0.135, 0.76)),
    ("Vendor creates invoice", "Create Purchase Order Item"): (-0.1, (0.235, 0.58)),
    ("Create Purchase Order Item", "Record Goods Receipt"): (0.1, (0.135, 0.24)),
    ("Vendor creates invoice", "Record Goods Receipt"): (-0.14, (0.345, 0.5)),
    ("Record Goods Receipt", "Vendor creates invoice"): (-0.14, (0.255, 0.42)),
    ("Vendor creates invoice", "Record Invoice Receipt"): (-0.08, (0.475, 0.72)),
    ("Record Goods Receipt", "Record Invoice Receipt"): (-0.14, (0.395, 0.36)),
    ("Record Invoice Receipt", "Record Goods Receipt"): (-0.14, (0.465, 0.205)),
    ("Record Invoice Receipt", "Clear Invoice"): (-0.1, (0.745, 0.59)),
    ("Record Invoice Receipt", "Remove Payment Block"): (0.1, (0.60, 0.29)),
    ("Record Goods Receipt", "Remove Payment Block"): (0.1, (0.53, 0.06)),
    ("Remove Payment Block", "Clear Invoice"): (0.1, (0.885, 0.25)),
}


def collect(con) -> dict:
    build(con, "10_item_facts.sql")
    build(con, "20_waits.sql")
    data = {name: run(con, sql) for name, sql in named_queries(SQL_DIR / "bottlenecks.sql").items()}
    data["stages"] = stage_table(data["durations"])
    data["months"] = month_table(data["durations"])
    data["vendor_terms"] = run(con, "select * from vendor_terms order by vendor")
    return data


def watched(frame: pd.DataFrame, took: str, waiting: str):
    """Days each item was watched, and whether the stage finished in that time."""
    rows = frame[frame[waiting].notna() & (frame[waiting] >= 0)]
    finished = rows[took].notna()
    return rows, rows[took].where(finished, rows[waiting]).to_numpy(dtype=float), finished.to_numpy()


def stage_table(durations: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for stage, took, waiting in STAGES:
        items, days, finished = watched(durations, took, waiting)
        rows.append({
            "stage": stage, "items": len(items), "unfinished": int((~finished).sum()),
            "finished_only_median": float(items[took].median()),
            "median_days": day_reached(days, finished, 0.5),
            "p75_days": day_reached(days, finished, 0.75),
            "p90_days": day_reached(days, finished, 0.9),
        })
    return pd.DataFrame(rows)


def month_table(durations: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for month, group in durations.groupby("po_month"):
        items, days, finished = watched(group, "po_to_clear", "open_from_po")
        rows.append({
            "po_month": int(month), "items": len(items), "cleared": int(finished.sum()),
            "cleared_only_median": float(items["po_to_clear"].median()),
            "median_days": day_reached(days, finished, 0.5),
        })
    return pd.DataFrame(rows)


def days_or_gap(value, digits=0) -> str:
    return "not reached" if value is None or pd.isna(value) else f"{value:.{digits}f}"


def map_edges(data: dict) -> pd.DataFrame:
    edges = data["map_edges"]
    total = data["map_nodes"]["n_items"].max()
    return edges[edges["n_items"] >= total * MAP_MIN_SHARE]


def build_report(data: dict) -> str:
    nodes = data["map_nodes"].set_index("activity")["n_items"]
    edges = data["map_edges"]
    drawn = map_edges(data)
    map_items = int(nodes["Create Purchase Order Item"])
    not_cleared = map_items - int(nodes["Clear Invoice"])
    invoice_to_clear = edges[(edges["from_step"] == "Record Invoice Receipt")
                             & (edges["to_step"] == "Clear Invoice")].iloc[0]
    stages = data["stages"].set_index("stage")
    clear_stage = stages.loc["PO to first clearing"]
    months = data["months"].set_index("po_month")
    steady = months.loc[1:9, "median_days"]
    bands = data["clearing_bands"].set_index("band")
    first = data["first_clearing_on_main_runs"].iloc[0]
    gaps = data["main_run_gaps"].iloc[0]
    ties = data["vendor_run_ties"].set_index("tie")
    cover = data["coverage"].iloc[0]
    parts = data["wait_parts"].set_index("view_name")
    every = parts.loc[ALL_ITEMS]
    weighted = parts.loc[WEIGHTED]
    medians = data["wait_medians"].iloc[0]
    terms = data["term_bands"]
    hold = data["term_holdout"].iloc[0]
    sens = data["term_sensitivity"].set_index("label")
    held = data["held_by"].set_index("held_by")
    overrun_days = held["overrun_days"].sum()
    held_items = held["n_items"].sum() - held.loc["On time", "n_items"]
    areas = data["overrun_by_area"].set_index("spend_area")
    top_areas = areas.head(3)
    conc = data["overrun_concentration"].iloc[0]
    reasons = data["block_reasons"].set_index("reason")
    n_blocked = reasons["n_blocked"].sum()
    block_totals = data["block_totals"].iloc[0]
    extra = data["block_extra_days"]
    whole_extra = extra[extra["invoices"].isna()].set_index("reason", drop=False)
    overall_extra = whole_extra[whole_extra["reason"].isna()].iloc[0]
    by_half = extra[extra["invoices"].notna() & extra["reason"].notna()].set_index(["reason", "invoices"])
    block_vendors = data["block_vendors"].iloc[0]
    block_accounts = data["block_accounts"].set_index("account_type")
    saved = every["run_wait"] - every["run_wait_weekly"]
    saved_value = weighted["run_wait"] - weighted["run_wait_weekly"]
    longest_tail = terms[terms["band"] == "84 and over"].iloc[0]

    edge_rows = [(STEP_LABELS[row["from_step"]], STEP_LABELS[row["to_step"]], whole(row["n_items"]),
                  pct(row["n_items"], map_items), f"{row['median_days']:.0f}", f"{row['p90_days']:.0f}")
                 for _, row in drawn.iterrows()]
    stage_rows = [(name, whole(row["items"]), pct(row["unfinished"], row["items"]),
                   f"{row['finished_only_median']:.0f}", days_or_gap(row["median_days"]),
                   days_or_gap(row["p90_days"])) for name, row in stages.iterrows()]
    month_rows = [(MONTHS[month - 1], whole(row["items"]), pct(row["cleared"], row["items"]),
                   f"{row['cleared_only_median']:.0f}", days_or_gap(row["median_days"]))
                  for month, row in months.iterrows()]
    band_rows = [(band, whole(row["n_days"]), whole(row["n_events"]),
                  pct(row["n_events"], bands["n_events"].sum()))
                 for band, row in bands.iterrows()]
    tie_rows = [(tie, whole(row["n_vendors"]), whole(row["n_items"])) for tie, row in ties.iterrows()]

    def part_row(label, row):
        return (label, whole(row["n_items"]), f"{row['total']:.1f}", f"{row['to_invoice']:.1f}",
                f"{row['term']:.1f}", f"{row['overrun']:.1f}", f"{row['run_wait']:.1f}")

    part_rows = [part_row(ALL_ITEMS, every), part_row("Main flow", parts.loc[MAIN]),
                 part_row("Invoice-after-GR flow", parts.loc[AFTER]), part_row(WEIGHTED, weighted)]
    term_rows = [(row["band"], whole(row["n_vendors"]), whole(row["n_items"]))
                 for _, row in terms.iterrows()]
    sens_rows = [(label, whole(row["n_items"]), f"{row['term']:.1f}", f"{row['overrun']:.1f}",
                  f"{row['run_wait']:.1f}")
                 for label, row in sens.iterrows()]
    held_rows = [(step, whole(row["n_items"]), pct(row["n_items"], every["n_items"]),
                  f"{row['mean_overrun']:.1f}", f"{row['median_overrun']:.0f}",
                  pct(row["overrun_days"], overrun_days))
                 for step, row in held.drop("On time").iterrows()]
    area_rows = [(area, whole(row["n_items"]), pct(row["n_items"], every["n_items"]),
                  pct(row["n_held"], row["n_items"]), pct(row["overrun_days"], overrun_days))
                 for area, row in top_areas.iterrows()]
    reason_rows = []
    for reason, row in reasons.iterrows():
        match = whole_extra.loc[reason]
        reason_rows.append((reason, whole(row["n_items"]), whole(row["n_blocked"]),
                            pct(row["n_blocked"], row["n_items"]), pct(row["n_blocked"], n_blocked),
                            f"{match['extra_days']:+.1f}", whole(match["n_items"])))
    half_rows = [(reason, f"{by_half.loc[(reason, 'Jan-Mar'), 'extra_days']:+.1f}",
                  f"{by_half.loc[(reason, 'Apr-Jun'), 'extra_days']:+.1f}")
                 for reason in reasons.index]

    no_reason, before_goods = "No reason visible in the log", "Invoice recorded before the goods"
    several = "Several invoices on the item"
    invoice_held = held.loc["Invoice recording"]
    block_held = held.loc["Payment block"]
    gap_began = pd.Timestamp(gaps["longest_began"]).strftime("%-d %B")
    gap_ended = pd.Timestamp(gaps["longest_ended"]).strftime("%-d %B")

    return f"""# Bottlenecks: where purchase items wait, and why

Generated by `scripts/bottlenecks.py` from the committed snapshot. Don't edit by hand.

Source: {CITATION} Licence: {LICENCE}.

This covers the two 3-way flows, which hold {pct(map_items, cover['all_cohort_items'], 1)} of the items
created from {FOLLOW_UP_START} up to, not including, {FOLLOW_UP_END}. Consignment has no invoices, and
the 2-way flow is covered in the compliance report. Times are Dutch calendar days, because vendor
dates have no time of day (3.2 in the source profile).

## 1. The answer

An item takes a mean {every['total']:.1f} days from PO to first clearing. On the
{whole(every['n_items'])} items that can be measured, those days split like this:

| Part | Mean days | Share |
|---|---|---|
| PO to the vendor's invoice date | {every['to_invoice']:.1f} | {pct(every['to_invoice'], every['total'])} |
| Implied payment term | {every['term']:.1f} | {pct(every['term'], every['total'])} |
| Process overrun: a step finished after the invoice was due | {every['overrun']:.1f} | {pct(every['overrun'], every['total'])} |
| Waiting for a payment run, already ready and due | {every['run_wait']:.1f} | {pct(every['run_wait'], every['total'])} |

**Most of the wait looks agreed or scheduled, not stuck.** Only
{pct(held_items, every['n_items'])} of items were still unfinished when the invoice fell due. The
implied term comes from the data, not from the contracts, so section 5 tests it.

## 2. The process map

![Process map](charts/06_process_map.png)

Each item's first time at six main steps, on {whole(map_items)} items. Repeats are left out, and an
invoice date goes first among same-day events, because the invoice exists before anyone records
it. Steps taken by at least {MAP_MIN_SHARE:.0%} of items:

{table(("From", "To", "Items", "Share of items", "Median days", "90th percentile days"), edge_rows)}

{whole(not_cleared)} items ({pct(not_cleared, map_items)}) have nothing cleared; the compliance report covers
why. The map shows where time passes, not why. The longest common step, invoice recorded to cleared,
is a median {invoice_to_clear['median_days']:.0f} days, but sections 4 and 5 show that's mostly the term and the run calendar.

## 3. Waiting times that count unfinished items

![Median days to first clearing by PO month](charts/07_days_to_clearing_by_month.png)

The log stops on {LOG_END_DAY}, so taking the median of items that have cleared favours the quickest
items in recent months. The Kaplan-Meier method (`p2p/survival.py`) keeps every item: one still open
when the log stops counts as waiting until that day, and deleted or never-invoiced items stay
counted as not cleared.

{table(("PO month", "Items", "Cleared by the log's end", "Median, cleared items only", "Median, all items"), month_rows)}

**There was no slowdown through September.** Counting all items, the median stays at
{steady.min():.0f}-{steady.max():.0f} days from January to September. Cleared items alone would show
{months.loc[9, 'cleared_only_median']:.0f} days in September falling to {months.loc[12, 'cleared_only_median']:.0f} in December, which is only the log
stopping. October is {months.loc[10, 'median_days']:.0f} days, a little higher. October POs reach clearing around the new
year, when main runs pause for {gaps['longest']:.0f} days, which may explain it. November and December can't be
measured, as under half of their items had cleared.

Every stage, on all {whole(stages['items'].max())} 3-way items created in the log's span:

{table(("Stage", "Items", "Unfinished", "Median, finished only", "Median, all items", "90th percentile, all items"), stage_rows)}

The all-item median of {clear_stage['median_days']:.0f} days from PO to clearing is above the {clear_stage['finished_only_median']:.0f} days for finished items because it
counts the items that clear slowly or not at all. It's above the 79.1 days in the source profile for the
same reason: that figure uses cleared items from the first half of 2018.

## 4. The payment calendar

![Clearing by day](charts/08_payment_calendar.png)

{table(("Days with clearing", "Days", "Clearing events", "Share"), band_rows)}

A main run clears at least {whole(MAIN_RUN_MIN_ITEMS)} events in a day, and a smaller run at least
{whole(RUN_MIN_ITEMS)}. Early in the year the runs look small because the log holds no POs from before 2018.

- **{bands.loc['Main runs', 'n_days']} days hold {pct(bands.loc['Main runs', 'n_events'], bands['n_events'].sum())} of all clearing events.** On the 3-way cohort,
  {pct(first['on_main_run'], first['n_items'])} of items first cleared on a main run, and
  {pct(first['on_thursday'], first['n_items'])} on a Thursday.
- **There are two main runs a month from late March:** one early in the month and one near the end.
  The break is at year-end, with no main run between {gap_began} and {gap_ended}, a gap of {gaps['longest']:.0f} days.
  Smaller runs fill most other Thursdays.
- **Most vendors belong to one of them.** Of {whole(ties['n_vendors'].sum())} vendors with at least {RUN_TIE_MIN_ITEMS} items on main
  runs, {whole(ties['n_vendors'].sum() - ties.loc['Both runs', 'n_vendors'])} have at least {RUN_TIE_SHARE:.0%} in one run.

{table(("Vendor pattern", "Vendors", "Items on main runs"), tie_rows)}

So a typical vendor's invoices wait for one run a month. An item arriving on a random day would wait
about 15 days for that, which is close to the {every['run_wait']:.1f} days measured below. That's a
cross-check, not an input.

## 5. Where the days go

![Where the days go](charts/09_where_the_days_go.png)

The log has no payment terms or due dates, so each vendor's term is implied: the time from invoice
date to first clearing on its fastest 5% of invoices. The steps are:

1. **PO to invoice date.** Can be negative, when the invoice is older than the PO.
2. **Implied term.** Invoice date to the implied due day, or to clearing if that came first.
3. **Process overrun.** If goods, invoice recording or the block removal finished after the due day, the days until the last of them did.
4. **Waiting for a run.** Ready and due, not yet cleared.

They add up exactly, item by item. Means are used because medians don't add up.

{table(("View", "Items", "Total", "PO to invoice date", "Implied term", "Process overrun", "Waiting for a run"), part_rows)}

Medians, for reference: {medians['total']:.0f} days in total, {medians['to_invoice']:.0f} to the invoice date, {medians['term']:.0f} for the term and
{medians['run_wait']:.0f} waiting for a run. Most items have no overrun. The value-weighted row shows larger
items spend longer before the invoice ({weighted['to_invoice']:.1f} days against {every['to_invoice']:.1f}), with the
later parts about the same.

**What's measured:** of {whole(cover['cohort_items'])} cohort items, {whole(cover['cleared'])} cleared. {whole(cover['corrected'])} of those have a
cancellation or debit memo and are left out, because their clearing may be a correction. {whole(cover['no_invoice_date'])} have no usable
invoice date. Another {whole(cover['timed'] - cover['measured'])} are at vendors with under {TERM_MIN_ITEMS} items, leaving {whole(cover['measured'])}
({pct(cover['measured'], cover['cohort_items'])} of the cohort) at {whole(cover['n_vendors'])} vendors.

### Is the implied term real?

Three checks, since this is the biggest number here and an inference:

- **It holds on later invoices.** With terms set on January to March invoices only, {whole(hold['n_vendors'])} vendors can be
  compared with April to June. The median change is {hold['median_gap']:.0f} days, and {whole(hold['within_a_week'])} vendors
  ({pct(hold['within_a_week'], hold['n_vendors'])}) move a week or less. Only {pct(hold['beat_term'], hold['later_items'])} of later
  invoices beat the earlier term, close to the 5% expected.
- **It clusters.** Implied terms bunch at 35-41 and 56-62 days, which is what 30- and 60-day terms
  would look like with a few days' handling. {whole(longest_tail['n_vendors'])} vendors at 84 days or more hold
  {pct(longest_tail['n_items'], every['n_items'])} of items. That's suggestive, not proof.

{table(("Implied term, days", "Vendors", "Items measured"), term_rows)}

- **The split moves by only a few days when it's set differently.** Mixed terms by spend area don't change it.

{table(("How the term is set", "Items", "Implied term", "Process overrun", "Waiting for a run"), sens_rows)}

## 6. What holds up the items that are late

{whole(held_items)} items ({pct(held_items, every['n_items'])}) weren't finished when the invoice fell due:

{table(("Last step to finish", "Items", "Share of measured items", "Mean days late", "Median days late", "Share of overrun days"), held_rows)}

**Payment blocks are the largest cause.** They're behind {pct(block_held['n_items'], held_items)} of late items but
{pct(block_held['overrun_days'], overrun_days)} of the days, as a blocked item runs later than one waiting for invoice recording.

The overrun is concentrated. {', '.join(top_areas.index[:2])} and {top_areas.index[2]} hold
{pct(top_areas['overrun_days'].sum(), overrun_days)} of overrun days, and 10 vendors hold
{pct(conc['top_ten_days'], conc['all_days'])} of them, out of {whole(conc['n_vendors_held'])} with any.

{table(("Spend area", "Items", "Share of items", "Items running late", "Share of overrun days"), area_rows)}

Sales stands out: {pct(areas.loc['Sales', 'n_items'], every['n_items'])} of items but {pct(areas.loc['Sales', 'overrun_days'], overrun_days)} of overrun days. More than 1 in 5 items run late in Sales and in
Trading & End Products, against {pct(areas.loc['Packaging', 'n_held'], areas.loc['Packaging', 'n_items'])} in Packaging.

## 7. What sets payment blocks?

A puzzle from the compliance work: {pct(block_totals['blocked'], block_totals['main_items'])} of main-flow items have a block removed, but only
{pct(block_totals['invoice_earlier_day'], block_totals['with_invoice'])} of items with an invoice have it recorded on an earlier day than the goods. Going by the minute instead of the day
finds {whole(reasons.loc[before_goods, 'n_items'])} items with the invoice first, and they're nearly always blocked. That
accounts for only {pct(reasons.loc[before_goods, 'n_blocked'], n_blocked)} of blocks.

The log shows a block being removed, but very rarely being set, so this sorts main-flow items with an invoice by what's visible.
Each takes the first row that fits. "Extra days" compares blocked items with unblocked items at the same vendor with invoices dated in the
same month, so term and run are about the same.

{table(("Signal", "Items", "Blocked", "Blocked share", "Share of blocks", "Extra days to clear", "Items compared"), reason_rows)}

- **Blocks for an early invoice show no added wait.** The block does its job, with {whole_extra.loc[before_goods, 'extra_days']:+.1f} days against
  comparable items.
- **{pct(reasons.loc[no_reason, 'n_blocked'], n_blocked)} of blocks have no visible reason,** and those add about {whole_extra.loc[no_reason, 'extra_days']:.0f} days. They may
  come from something the log doesn't record, such as a price tolerance. The unexplained block rate varies by vendor, from
  {100 * block_vendors['p10_rate']:.0f}% at the 10th percentile to {100 * block_vendors['p90_rate']:.0f}% at the 90th (median {100 * block_vendors['median_rate']:.0f}%, {whole(block_vendors['n_vendors'])} vendors with
  {RUN_TIE_MIN_ITEMS}+ items).
- **Items with several invoices show the largest gap,** but on only {whole(whole_extra.loc[several, 'n_items'])} items, and they differ from single-invoice items in other ways, so
  {whole_extra.loc[several, 'extra_days']:+.1f} days shouldn't be read as the block's effect.
- Across all compared items, a block goes with {overall_extra['extra_days']:+.1f} days.

The estimate moves between the two halves of the cohort, so treat these as sizes, not exact figures:

{table(("Signal", "Invoices Jan-Mar", "Invoices Apr-Jun"), half_rows)}

Accounts lifting blocks: named accounts make {whole(block_accounts.loc['named', 'n_events'])} removals, a median {block_accounts.loc['named', 'median_days']:.0f} days after the invoice was
recorded; batch accounts make {whole(block_accounts.loc['batch', 'n_events'])}, a median {block_accounts.loc['batch', 'median_days']:.0f} days. Part 1 showed that named accounts
often work in bulk, so that gap isn't a measure of manual effort.

## 8. What this means

- **Don't read invoice-to-clearing time as a bottleneck.** A median {medians['days_to_clear']:.0f} days from invoice date to clearing
  looks slow, but of the mean {every['term'] + every['overrun'] + every['run_wait']:.1f} days after the invoice date, {every['term']:.1f} are the implied term. Reporting that as a process problem
  would send a team to fix the wrong thing.
- **The payment calendar is a decision, and it has a price either way.** Weekly Thursday runs would cut the wait from {every['run_wait']:.1f} to
  {every['run_wait_weekly']:.1f} days, {saved:.1f} days sooner for vendors. But cash would leave {saved_value:.1f} days sooner too, weighted by value. Treasury should own that trade-off,
  not process improvement.
- **The real process fix is small and specific.** Overrun is {pct(every['overrun'], every['total'])} of the wait, on
  {pct(held_items, every['n_items'])} of items. Unexplained blocks and late invoice recording in Sales, Packaging and Trading are where to look.
- **Ask for one thing first:** the vendor master's payment terms. With real terms, sections 5 and 6 become exact.

## 9. Limits

- The implied term is an inference. If a vendor has been paid late on all but a few invoices, its real term is shorter than the implied one, and overrun is understated.
  If a vendor has mixed terms, its fastest 5% reflects the shortest, and the run wait is overstated. The sensitivity table bounds some of this.
- "Ready" means goods, invoice and block removal, as far as the log shows. Other checks, such as an approval outside this system, aren't visible.
- Vendor invoice dates are the vendor's, not the day the company received the invoice, so recording delays may include the post.
- The breakdown leaves out items that never cleared and corrected items. It describes normal items, and those are in the compliance report.
- Kaplan-Meier assumes items still open at the end behave like those that finished. Items from late in the year may not, given the new-year gap.
- Weekly what-if ignores public holidays and assumes each run would take every item that's ready and due.
- Extra days compare similar items but don't prove a block caused the difference.
"""


def chart_map(data: dict, path: Path) -> None:
    nodes = data["map_nodes"].set_index("activity")["n_items"]
    edges = map_edges(data)
    total = nodes.max()
    fig, ax = plt.subplots(figsize=(10.5, 5.6))
    fig.subplots_adjust(left=0.01, right=0.99, top=0.82, bottom=0.02)
    fig.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_xlim(-0.03, 1.03)
    ax.set_ylim(0, 1)
    ax.axis("off")
    width, height = 0.15, 0.13
    boxes = {}
    for step, (x, y) in MAP_LAYOUT.items():
        boxes[step] = FancyBboxPatch((x - width / 2, y - height / 2), width, height, zorder=2,
                                     boxstyle="round,pad=0.002,rounding_size=0.015",
                                     facecolor="#ffffff", edgecolor=MUTED, linewidth=1)
        ax.add_patch(boxes[step])
        ax.text(x, y + 0.022, STEP_LABELS[step], ha="center", va="center", fontsize=10,
                fontweight="bold", color=INK, zorder=3)
        ax.text(x, y - 0.03, f"{nodes[step] / total:.0%} of items", ha="center", va="center",
                fontsize=9, color=INK_2, zorder=3)
    for _, row in edges.iterrows():
        pair = (row["from_step"], row["to_step"])
        bend, (x, y) = MAP_EDGES[pair]
        arrow = FancyArrowPatch(
            MAP_LAYOUT[pair[0]], MAP_LAYOUT[pair[1]], connectionstyle=f"arc3,rad={bend}",
            patchA=boxes[pair[0]], patchB=boxes[pair[1]], shrinkA=3, shrinkB=4, arrowstyle="-|>",
            mutation_scale=13, linewidth=0.8 + 5 * row["n_items"] / total, zorder=1,
            color=ORANGE if row["to_step"] == "Clear Invoice" else BLUE)
        ax.add_patch(arrow)
        ax.text(x, y, f"{row['n_items'] / total:.0%}  ·  {row['median_days']:.0f}d", ha="center",
                va="center", fontsize=9.5, color=INK, zorder=3,
                bbox={"boxstyle": "round,pad=0.2", "facecolor": SURFACE, "edgecolor": "none"})
    headline(fig, "Time passes at the end of the process: the steps into clearing are the slow ones",
             "First time at each step, 3-way items, Jan-Jun 2018. Labels: share of items and median days. "
             "Orange: steps into clearing.")
    save(fig, path)


def chart_months(data: dict, path: Path) -> None:
    months = data["months"]
    x = list(range(len(months)))
    fig, ax = plt.subplots(figsize=(9, 4.6))
    fig.subplots_adjust(left=0.08, right=0.87, top=0.78, bottom=0.1)
    style(ax)
    measured = months["median_days"].astype(float)
    ax.plot(x, months["cleared_only_median"], color=MUTED, linewidth=2, marker="o", markersize=5,
            markeredgecolor=SURFACE, markeredgewidth=1, label="Cleared items only")
    ax.plot(x, measured, color=BLUE, linewidth=2, marker="o", markersize=5,
            markeredgecolor=SURFACE, markeredgewidth=1, label="All items, unfinished counted")
    last = measured.last_valid_index()
    ax.annotate(f"All items: {measured[last]:.0f}", (last, measured[last]), xytext=(8, 4),
                textcoords="offset points", color=INK, fontsize=10, va="bottom")
    end = months["cleared_only_median"].iloc[-1]
    ax.annotate(f"Cleared only: {end:.0f}", (x[-1], end), xytext=(8, 0), textcoords="offset points",
                color=INK, fontsize=10, va="center")
    ax.set_xticks(x, MONTHS)
    ax.set_ylim(0, 105)
    ax.set_ylabel("Median days, PO to clearing", color=INK_2, fontsize=10)
    ax.legend(loc="lower left", frameon=False, fontsize=10, labelcolor=INK_2)
    headline(fig, "Clearing didn't speed up late in 2018; the log just stopped",
             "Median days from PO to first clearing, by PO month, 3-way items. Nov and Dec can't be\n"
             "measured with unfinished items counted, as under half had cleared.")
    save(fig, path)


def chart_calendar(data: dict, path: Path) -> None:
    runs = data["run_calendar"].copy()
    runs["day"] = pd.to_datetime(runs["day"])
    main = runs[runs["is_main_run"]]
    small = runs[~runs["is_main_run"]]
    fig, ax = plt.subplots(figsize=(9.5, 4.6))
    fig.subplots_adjust(left=0.08, right=0.97, top=0.78, bottom=0.1)
    style(ax)
    ax.bar(main["day"], main["n_events"], width=2.2, color=BLUE, label="Main run")
    ax.bar(small["day"], small["n_events"], width=2.2, color=MUTED, label="Smaller run")
    ax.yaxis.set_major_formatter(lambda value, _: f"{value:,.0f}")
    ax.xaxis.set_major_locator(matplotlib.dates.MonthLocator())
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%b"))
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, 1.05), ncols=2, frameon=False, fontsize=10,
              labelcolor=INK_2)
    bands = data["clearing_bands"].set_index("band")
    share = bands.loc["Main runs", "n_events"] / bands["n_events"].sum()
    headline(fig, f"{bands.loc['Main runs', 'n_days']} days hold {share:.0%} of all invoice clearing",
             f"Clearing events per day, Jan 2018 to Jan 2019, on days with at least {RUN_MIN_ITEMS}. "
             "Early runs are small because\nthe log has no POs from before 2018.")
    save(fig, path)


def chart_parts(data: dict, path: Path) -> None:
    parts = data["wait_parts"].set_index("view_name").loc[[ALL_ITEMS, MAIN, AFTER, WEIGHTED]]
    labels = ["All 3-way items", "Main flow", "Invoice-after-GR flow", "All, weighted by value"]
    y = list(range(len(parts)))[::-1]
    fig, ax = plt.subplots(figsize=(9.5, 4.2))
    fig.subplots_adjust(left=0.22, right=0.96, top=0.72, bottom=0.05)
    fig.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    left = [0.0] * len(parts)
    for key, label, colour, ink in PARTS:
        ax.barh(y, parts[key], left=left, height=0.6, color=colour, label=label, edgecolor=SURFACE,
                linewidth=1.5)
        for position, start, value in zip(y, left, parts[key]):
            if value >= 3:
                ax.text(start + value / 2, position, f"{value:.0f}", ha="center", va="center",
                        fontsize=9.5, color=ink)
        left = [a + b for a, b in zip(left, parts[key])]
    for position, total in zip(y, parts["total"]):
        ax.text(total + 1, position, f"{total:.0f} days", va="center", fontsize=9.5, color=INK_2)
    ax.set_yticks(y, labels)
    ax.set_xticks([])
    ax.tick_params(colors=INK_2, length=0, labelsize=10)
    ax.set_xlim(0, max(left) * 1.1)
    ax.legend(loc="upper left", bbox_to_anchor=(-0.02, 1.16), ncols=4, frameon=False, fontsize=9.5,
              labelcolor=INK_2, handlelength=1.2, columnspacing=1.4)
    every = parts.loc[ALL_ITEMS]
    share = (every["term"] + every["run_wait"]) / every["total"]
    headline(fig, f"{share:.0%} of the wait is the payment term and the run calendar",
             "Mean days from PO to first clearing, split into four parts that add up, cohort items.")
    save(fig, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=RESULTS)
    out = parser.parse_args().out
    (out / "charts").mkdir(parents=True, exist_ok=True)
    data = collect(connect())
    (out / "BOTTLENECKS.md").write_text(build_report(data))
    data["stages"].to_csv(out / "wait_stages.csv", index=False)
    data["run_calendar"].to_csv(out / "run_calendar.csv", index=False)
    data["vendor_terms"].to_csv(out / "vendor_terms.csv", index=False)
    chart_map(data, out / "charts" / "06_process_map.png")
    chart_months(data, out / "charts" / "07_days_to_clearing_by_month.png")
    chart_calendar(data, out / "charts" / "08_payment_calendar.png")
    chart_parts(data, out / "charts" / "09_where_the_days_go.png")


if __name__ == "__main__":
    main()
