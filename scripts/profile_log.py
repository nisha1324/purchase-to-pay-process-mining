"""Profile the event log: what it holds and which readings of it would mislead.

    python -m scripts.profile_log

Writes results/SOURCE_PROFILE.md and 3 charts. Every number comes from
sql/profile.sql, and the report is regenerated rather than edited by hand.
"""
import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from p2p.db import SQL_DIR, connect, named_queries, run
from p2p.source import (BURST_MIN_ITEMS, CITATION, FOLLOW_UP_END, FOLLOW_UP_START, LICENCE,
                        PUBLISHED, RESULTS)

BLUE, ORANGE = "#2a78d6", "#eb6834"  # categorical slots 1 and 2
INK, INK_2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#fcfcfb"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def pct(part, total, digits=1) -> str:
    return f"{100 * part / total:.{digits}f}%"


def whole(number) -> str:
    return f"{int(number):,}"


def table(headers, rows) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def collect(con) -> dict:
    return {name: run(con, sql) for name, sql in named_queries(SQL_DIR / "profile.sql").items()}


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(MUTED)
    ax.tick_params(colors=INK_2, length=0, labelsize=10)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def headline(fig, title: str, subtitle: str) -> None:
    fig.text(0.02, 0.965, title, fontsize=13.5, fontweight="bold", color=INK, va="top")
    fig.text(0.02, 0.905, subtitle, fontsize=10, color=INK_2, va="top")


def save(fig, path: Path) -> None:
    fig.savefig(path, dpi=150, facecolor=SURFACE, metadata={"Software": None})
    plt.close(fig)


def chart_requisitions(data: dict, path: Path) -> None:
    monthly = data["monthly"]
    x = range(len(monthly))
    fig, ax = plt.subplots(figsize=(9, 4.6))
    fig.subplots_adjust(left=0.08, right=0.9, top=0.8, bottom=0.1)
    style(ax)
    for column, colour, label in (("po_items", BLUE, "PO items"),
                                  ("requisitions", ORANGE, "Requisition items")):
        ax.plot(x, monthly[column], color=colour, linewidth=2, marker="o", markersize=5,
                markeredgecolor=SURFACE, markeredgewidth=1, label=label)
    ax.annotate("PO items", (11, monthly["po_items"].iloc[-1]), xytext=(8, 0),
                textcoords="offset points", color=INK, fontsize=10, va="center")
    ax.annotate("Requisitions", (11, monthly["requisitions"].iloc[-1]), xytext=(8, 0),
                textcoords="offset points", color=INK, fontsize=10, va="center")
    ax.set_xticks(list(x), MONTHS)
    ax.set_ylim(0, None)
    ax.yaxis.set_major_formatter(lambda value, _: f"{value:,.0f}")
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, 1.03), ncols=2, frameon=False,
              fontsize=10, labelcolor=INK_2)
    aug, sep = monthly["requisitions"].iloc[7], monthly["requisitions"].iloc[8]
    headline(fig, "Requisition records jump in September 2018; POs don't",
             f"Items created per month, 2018. Requisitions go from {aug:,.0f} in Aug to {sep:,.0f} in Sep,"
             " so the year isn't comparable.")
    save(fig, path)


def chart_clearing(data: dict, path: Path) -> None:
    months = data["cleared_by_month"]
    share = 100 * months["cleared"] / months["n_items"]
    fig, ax = plt.subplots(figsize=(9, 4.6))
    fig.subplots_adjust(left=0.08, right=0.97, top=0.8, bottom=0.1)
    style(ax)
    bars = ax.bar(range(len(months)), share, width=0.62, color=BLUE)
    for bar, value in zip(bars, share):
        ax.annotate(f"{value:.0f}%", (bar.get_x() + bar.get_width() / 2, value), xytext=(0, 4),
                    textcoords="offset points", ha="center", color=INK_2, fontsize=9.5)
    ax.set_xticks(range(len(months)), MONTHS)
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    headline(fig, "The last months aren't comparable: those items had little time to clear",
             "Share of 3-way-match items with at least one cleared invoice by Jan 2019, "
             "by month the PO was created")
    save(fig, path)


def chart_variants(data: dict, path: Path) -> None:
    sizes = data["variant_sizes"]["n_items"]
    covered = 100 * sizes.cumsum() / sizes.sum()
    rank = range(1, len(sizes) + 1)
    fig, ax = plt.subplots(figsize=(9, 4.6))
    fig.subplots_adjust(left=0.08, right=0.97, top=0.8, bottom=0.14)
    style(ax)
    ax.plot(rank, covered, color=BLUE, linewidth=2)
    ax.set_xscale("log")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    for top in (10, 100, 1000):
        value = covered.iloc[top - 1]
        ax.plot([top], [value], marker="o", markersize=7, color=BLUE, markeredgecolor=SURFACE,
                markeredgewidth=1.5)
        ax.annotate(f"Top {top:,}: {value:.1f}%", (top, value), xytext=(8, -14),
                    textcoords="offset points", color=INK, fontsize=10)
    ax.set_xlim(1, len(sizes))
    ax.set_ylim(0, 105)
    ax.set_yticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    ax.xaxis.set_major_formatter(lambda value, _: f"{value:,.0f}")
    ax.set_xlabel("Number of paths, most common first (log scale)", color=INK_2, fontsize=10)
    headline(fig, f"{len(sizes):,} different paths, but a few carry most of the work",
             "Cumulative share of purchase items covered by the most common paths, as logged")
    save(fig, path)


def build_report(data: dict) -> str:
    t = data["totals"].iloc[0]
    flows = data["flows"]
    out = data["out_of_window"].iloc[0]
    edges = data["window_edges"].iloc[0]
    invoices = data["invoice_sums"].iloc[0]
    clear_none = data["date_only_clearing"].iloc[0]
    zero = data["zero_value"].iloc[0]
    monthly = data["monthly"]
    cleared = data["cleared_by_month"]
    clearing = data["clearing_time"].iloc[0]
    accounts = data["accounts_by_activity"].set_index("activity")
    bursts = data["bursts"].set_index("activity")
    top = data["clearing_accounts"]
    repeats = data["repeats"].iloc[0]
    sheets = data["service_sheets"].iloc[0]
    ties = data["ties"].iloc[0]
    variants = data["variants"].iloc[0]
    sizes = data["variant_sizes"]
    copies = data["value_copies"]
    always = copies[copies["n_same"] == copies["n_events"]]
    sometimes = copies[copies["n_same"] != copies["n_events"]]
    coverage = sizes["n_items"].cumsum() / sizes["n_items"].sum()
    singletons = int((sizes["n_items"] == 1).sum())

    flow_rows = [
        (row.flow, whole(row.n_items), pct(row.n_items, t.n_items),
         pct(row.po_value, t.po_value), whole(row.median_value), whole(row.median_events),
         f"{whole(row.cohort_cleared)} of {whole(row.cohort)} ({pct(row.cohort_cleared, row.cohort)})")
        for row in flows.itertuples()
    ]
    copy_rows = [(row.activity, whole(row.n_events), pct(row.n_same, row.n_events))
                 for row in sometimes.itertuples()]
    account_rows = [
        (activity, whole(row.n_events), pct(row.named, row.n_events), pct(row.batch, row.n_events),
         pct(row.no_account, row.n_events), pct(bursts.loc[activity, "in_bursts"],
                                                bursts.loc[activity, "named_events"]))
        for activity, row in accounts.iterrows()
    ]
    month_rows = [
        (f"{MONTHS[row.month.month - 1]} {row.month.year}", whole(row.n_items), whole(row.cleared),
         pct(row.cleared, row.n_items))
        for row in cleared.itertuples()
    ]
    path_rows = [
        (rank + 1, row.path, whole(row.n_items), pct(row.n_items, t.n_items))
        for rank, row in enumerate(sizes.head(5).itertuples())
    ]
    sep_share = monthly["requisitions"].iloc[8:].sum() / monthly["requisitions"].sum()
    published = {
        "Purchase items (cases)": (t.n_items, PUBLISHED["cases"]),
        "Events": (t.n_events, PUBLISHED["events"]),
        "Purchase documents": (t.documents, PUBLISHED["purchase_documents"]),
        "Activities": (t.activities, PUBLISHED["activities"]),
        "Accounts (named + batch)": (t.named_accounts + t.batch_accounts, PUBLISHED["users"]),
    }
    check_rows = [(name, whole(mine), whole(theirs), "yes" if mine == theirs else "**NO**")
                  for name, (mine, theirs) in published.items()]

    return f"""# Source profile: BPI Challenge 2019 event log

Generated by `scripts/profile_log.py` from the committed snapshot. Don't edit by hand.

Source: {CITATION} Licence: {LICENCE}.

## 1. The log at a glance

The log records the purchase-order handling of a large multinational in coatings and paints,
based in the Netherlands. One case is one purchase item (a line on a purchase order).

| | |
|---|---|
| Purchase items (cases) | {whole(t.n_items)} |
| Purchase documents | {whole(t.documents)} |
| Events | {whole(t.n_events)} |
| Activities | {whole(t.activities)} |
| Accounts | {whole(t.named_accounts)} named, {whole(t.batch_accounts)} batch, plus `NONE` |
| Vendors | {whole(t.vendors)} |
| Companies | {whole(t.companies)}; one holds {pct(t.items_top_company, t.n_items)} of items |
| Main span | {edges.first_ts:%Y-%m-%d %H:%M} to {edges.last_ts:%Y-%m-%d %H:%M} UTC |
| Total item value | {t.po_value / 1e6:,.1f} million (anonymised scale, see 3.1) |
| Events per item | median {whole(t.median_events)}, maximum {whole(t.max_events)} |

Items with more than 100 events: {whole(t.long_items)} ({pct(t.long_items, t.n_items)} of items),
holding {pct(t.long_item_events, t.n_events)} of all events.

The log reconciles with the publisher's description:

{table(("Count", "This snapshot", "Published", "Match"), check_rows)}

## 2. The four flows

Each item follows one of four routes, and each has its own rule for what must happen before an
invoice can be paid. The last column uses items created from {FOLLOW_UP_START} up to, not
including, {FOLLOW_UP_END}, which have at least 6.5 months of follow-up.

{table(("Flow", "Items", "Share of items", "Share of value", "Median item value",
        "Median events", "Cleared at least once"), flow_rows)}

Consignment has no invoices on the PO by design, so 0 cleared is expected. Its items are also
{whole(zero.zero_consignment)} of the {whole(zero.zero_items)} items with a value of exactly 0.
The low clearing share on the other flows is not explained here. It's a lead for the next part.

## 3. Seven ways to misread this log

### 3.1 The value field isn't real euros, and isn't cumulative

The field is called `Cumulative net worth (EUR)`, but:

- **It's anonymised.** The publisher applied a linear change that keeps 0 at 0. Shares and ratios
  hold; absolute amounts aren't real euros.
- **It doesn't accumulate, and it's repeated.** Adding it up across events gives
  {t.naive_value_sum / 1e9:,.2f} billion, against {t.po_value / 1e9:,.3f} billion if each item is
  counted once: {t.naive_value_sum / t.po_value:.1f} times too much.
- **{len(always)} of the {len(copies)} activities with 1,000+ events always show the same value as
  the item's PO creation event**, including `Vendor creates invoice` and `Clear Invoice`. So those
  events say nothing about how much was invoiced or paid.
- **Even `Change Price` shows that same value.** It looks like one item-level figure stamped on
  each event, not a history of the price, so the effect of a price change can't be measured.

Among the same {len(copies)} activities, only these ever carry a different value:

{table(("Activity", "Events", "Same as PO value"), copy_rows)}

The publisher says invoices on one item should add up to the item's worth. Summing
`Record Invoice Receipt` does match the PO value on {whole(invoices.sum_matches)} of
{whole(invoices.invoiced_items)} invoiced items ({pct(invoices.sum_matches, invoices.invoiced_items)}).
But on {whole(invoices.multi_same_value)} of the {whole(invoices.multi_invoice)} items with several
receipts, every receipt shows the same value, so a sum would overshoot on those.

**Rule:** item value means the value on the PO creation event, counted once per item, and
results are given as shares. Any amount-matching check has to allow for repeated values.

### 3.2 Some vendor dates are wrong, and none has a time of day

{whole(out.n_events)} events on {whole(out.affected_items)} items fall outside the main span:
{whole(out.before_start)} before it and {whole(out.after_end)} after, from
{out.earliest:%Y-%m-%d} to {out.latest:%Y-%m-%d}. {whole(out.vendor_dated)} of them are
`Vendor creates invoice` or `Vendor creates debit memo`, and the other
{whole(out.n_events - out.vendor_dated)} are requisitions from 2017. A late-2017 date can be real,
but the {whole(out.before_2017)} before 2017 are very unlikely to be, on purchase orders from 2018.

Inside the span, every one of those vendor events falls at one of two UTC times:

{table(("UTC time", "Events", "First day", "Last day"),
       [(row.utc_time, whole(row.n_events), row.first_day.date(), row.last_day.date())
        for row in data["date_only"].itertuples()])}

21:59 appears only during 2018 summer time, which began on 25 March and ended on 28 October,
and 22:59 only outside it. So both are 23:59 in the Netherlands: these are dates, not times.

The {whole(clear_none.n_events)} `Clear Invoice` events with no account have the same shape:
{whole(clear_none.at_2359_local)} of them sit at those times, on {whole(clear_none.n_days)} days.

**Rule:** treat these events as dates. They can sort after a same-day event that really came
later, and they can't be used for durations shorter than a day.

### 3.3 Requisition records change in September 2018

![Requisitions by month](charts/01_requisitions_by_month.png)

PO items stay between {whole(monthly["po_items"].iloc[:-1].min())} and
{whole(monthly["po_items"].iloc[:-1].max())} a month through November, then fall to
{whole(monthly["po_items"].iloc[-1])} in December. Requisition items run at
{whole(monthly["requisitions"].iloc[:8].min())}-{whole(monthly["requisitions"].iloc[:8].max())}
a month to August, then {whole(monthly["requisitions"].iloc[8])} in September.
September to December hold {pct(sep_share, 1)} of the year's requisitions.

The log doesn't say why. It could be a real policy change or a change in what was captured.

**Rule:** don't compare "PO raised without a requisition" across this break, and don't report
it as a compliance trend without asking the process owner.

### 3.4 The log stops, so recent items look unfinished

![Clearing by PO month](charts/02_clearing_by_po_month.png)

{table(("PO month", "3-way items", "Cleared", "Share"), month_rows)}

On 3-way items from the first half of 2018, the median time from PO to first clearing is
{clearing.median_days:.1f} days, and 10% take over {clearing.p90_days:.1f} days. Items created
late in the year hadn't had that long when the log stopped.

**Rule:** measure outcomes and durations on items with enough follow-up, or use a method that
handles unfinished items. Don't read the fall as a slowdown.

### 3.5 A named account isn't always a person

The publisher describes named accounts as people and batch accounts as automated. The pattern in
the data is less clean. The last column counts named-account events sharing an activity, an
account and a minute with at least {BURST_MIN_ITEMS} events in all.

{table(("Activity", "Events", "Named", "Batch", "No account",
        f"Named events in {BURST_MIN_ITEMS}+ bursts"), account_rows)}

`{top.account.iloc[0]}` alone clears {pct(top.n_events.iloc[0], top.all_clearing.iloc[0])} of all
invoices, on {whole(top.active_days.iloc[0])} days. That looks like bulk payment runs under one
login, not {whole(top.n_events.iloc[0])} manual steps, though the log can't confirm it.

**Rule:** "named" doesn't mean "manual". Estimates of manual workload have to separate bulk
runs from one-at-a-time work. If clearing happens in runs, its dates reflect the run calendar as
well as the invoice, which matters when timing the steps before it.

### 3.6 Same-minute ties and repeats inflate the number of paths

Timestamps stop at the minute, and {whole(ties.tied_steps)} of {whole(ties.all_steps)} steps
({pct(ties.tied_steps, ties.all_steps)}) share their minute with the step before.

| Paths counted | Number |
|---|---|
| As logged | {whole(variants.as_logged)} |
| Same-minute events in a fixed order | {whole(variants.tie_fixed)} |
| Also collapsing immediate repeats | {whole(variants.collapsed)} |

**Rule:** process-map counts are only a guide. Ties alone change little; collapsing repeats removes
{pct(variants.as_logged - variants.collapsed, variants.as_logged, 0)} of the paths.

### 3.7 Service entry sheets dominate event counts on a few items

`Record Service Entry Sheet` has {whole(sheets.n_events)} events on only {whole(sheets.n_items)}
items. {whole(repeats.repeat_groups)} sets of rows are identical on item, activity, minute and
value, giving {whole(repeats.extra_rows)} extra rows, and {pct(repeats.extra_service_rows, repeats.extra_rows)}
of those are service entry sheets. No rows were dropped: several lines on one sheet can
legitimately look like this.

**Rule:** count items, not events, and don't deduplicate rows.

## 4. How varied is the process?

![Path coverage](charts/03_path_coverage.png)

{whole(len(sizes))} paths as logged. {whole(singletons)} of them were followed by only one item.
The top 10 cover {pct(coverage.iloc[9], 1)} of items and the top 100 {pct(coverage.iloc[99], 1)}.

{table(("Rank", "Path", "Items", "Share"), path_rows)}

Ranks 1 and 2 differ only in whether `Vendor creates invoice` sits before or after the goods
receipt. Given 3.2, that order is partly an artefact of a date-only field.

## 5. What this means for the analysis

The log can answer **which rules were followed, where items wait and how often work is redone**,
as counts and shares of items. It can't answer **how many real euros are at stake**, and its
clearing events don't show what was actually paid.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=RESULTS)
    args = parser.parse_args()
    charts = args.out / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    data = collect(connect())
    chart_requisitions(data, charts / "01_requisitions_by_month.png")
    chart_clearing(data, charts / "02_clearing_by_po_month.png")
    chart_variants(data, charts / "03_path_coverage.png")
    (args.out / "SOURCE_PROFILE.md").write_text(build_report(data))


if __name__ == "__main__":
    main()
