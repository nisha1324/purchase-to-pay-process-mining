"""What gets changed, cancelled or redone, and which hand work would repay fixing first.

    python -m scripts.rework

Writes results/REWORK.md, 2 CSV files and 2 charts. Every number comes from
sql/30_rework.sql and sql/rework.sql.
"""
import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker
import pandas as pd

from p2p.db import SQL_DIR, build, connect, named_queries, run
from p2p.source import BURST_MIN_ITEMS, CITATION, FOLLOW_UP_END, FOLLOW_UP_START, LICENCE, RESULTS
from scripts.compliance import AQUA
from scripts.profile_log import (BLUE, INK, INK_2, MUTED, ORANGE, SURFACE, headline, pct, save,
                                 style, table, whole)

KIND_COLOURS = {"Core": BLUE, "Rework": ORANGE, "Control": AQUA}
KIND_LABELS = {"Core": "Core step", "Rework": "Rework", "Control": "Control"}
# Steps the rework-delay table compares, in report order.
DELAY_GROUPS = ("Price changed after the invoice", "Price changed before the invoice", "Quantity changed",
                "Other PO change", "Other rework")
AREA_MIN_ITEMS = 1000  # spend areas with fewer items are left out of the area table
VENDOR_MIN_ITEMS = 100  # vendors with fewer items don't get a rework rate


def collect(con) -> dict:
    build(con, "10_item_facts.sql")
    build(con, "20_waits.sql")
    build(con, "30_rework.sql")
    data = {name: run(con, sql) for name, sql in named_queries(SQL_DIR / "rework.sql").items()}
    data["invoice_overrun"] = run(con, """
        select sum(overrun) filter (where held_by = 'Invoice recording') / sum(overrun) as share
        from waits""")
    data["late_by_area"] = run(con, """
        select 'All areas' as spend_area, count(*) as n_items, avg((held_by <> 'On time')::integer) as late_share
        from waits
        union all
        select coalesce(spend_area, '(blank)'), count(*), avg((held_by <> 'On time')::integer)
        from waits
        group by spend_area
        order by spend_area""")
    return data


def days(value) -> str:
    return f"{value:.0f} day" if round(value) == 1 else f"{value:.0f} days"


def signed(value: float, digits: int = 1) -> str:
    return f"{value:+.{digits}f}"


def candidate_table(data: dict) -> pd.DataFrame:
    """One row per step done one at a time, with the robustness check joined on."""
    frame = data["candidates"].merge(data["bulk_sensitivity"][["step", "under_5_docs", "single_doc"]],
                                     on="step")
    total = frame["n_touches"].sum()
    frame["share_of_touches"] = frame["n_touches"] / total
    frame["top10_vendor_share"] = frame["top10_touches"] / frame["n_touches"]
    columns = ["step", "kind", "n_touches", "share_of_touches", "n_accounts", "n_vendors", "n_documents",
               "top10_vendor_share", "under_5_docs", "single_doc"]
    frame = frame[columns].copy()
    for name in ("n_touches", "n_accounts", "n_vendors", "n_documents", "under_5_docs", "single_doc"):
        frame[name] = frame[name].astype(int)
    frame["share_of_touches"] = frame["share_of_touches"].round(4)
    frame["top10_vendor_share"] = frame["top10_vendor_share"].round(4)
    return frame


def area_table(data: dict) -> pd.DataFrame:
    frame = data["by_area"].copy()
    totals = data["area_totals"].iloc[0]
    frame["share_of_items"] = (frame["n_items"] / totals["n_items"]).round(4)
    frame["rework_rate"] = (frame["rework_items"] / frame["n_items"]).round(4)
    frame["rework_touches_per_1000"] = (1000 * frame["rework_touches"] / frame["n_items"]).round(1)
    frame["share_of_rework_touches"] = (frame["rework_touches"] / totals["rework_touches"]).round(4)
    return frame[["spend_area", "n_items", "share_of_items", "rework_items", "rework_rate",
                  "price_after_items", "rework_touches", "rework_touches_per_1000",
                  "share_of_rework_touches"]]


def delay_rows(data: dict) -> dict:
    frame = data["change_extra_days"]
    rows = {}
    for group in ("No rework",) + DELAY_GROUPS:
        part = frame[frame["rework"] == group]
        rows[group] = part.set_index(part["invoices"].fillna("All"))
    return rows


def build_report(data: dict) -> str:
    scope = data["scope"].iloc[0]
    n_items = int(scope["n_items"])
    modes = data["modes"].set_index("step")
    totals = data["touch_totals"].set_index("kind")["n_touches"]
    all_touches = int(totals.sum())
    rates = data["rework_rates"].set_index("step")
    bands = data["value_bands"]
    timing = data["change_timing"].set_index("activity")
    loop = data["price_loop"].iloc[0]
    base = data["price_loop_base"].iloc[0]
    delays = delay_rows(data)
    deleted = data["deletions"].iloc[0]
    creator = data["deleted_by_creator"].iloc[0]
    reentry = data["reentry"].set_index("what")
    areas = area_table(data)
    vendors = data["vendor_rework"]
    candidates = candidate_table(data).set_index("step")
    invoice_overrun = float(data["invoice_overrun"]["share"].iloc[0])

    rework_touches = int(totals["Rework"])
    core_touches = int(totals["Core"])
    control_touches = int(totals.get("Control", 0))
    two_steps = int(candidates.loc["Goods receipt", "n_touches"] + candidates.loc["Invoice recording", "n_touches"])
    price_after = delays["Price changed after the invoice"].loc["All"]
    price_before = delays["Price changed before the invoice"].loc["All"]
    quantity = delays["Quantity changed"].loc["All"]
    no_rework = delays["No rework"].loc["All"]
    loop_items = int(loop["n_items"])
    vendor_top10 = vendors["rework_touches"].iloc[:10].sum() / rework_touches
    sized = vendors[vendors["n_items"] >= VENDOR_MIN_ITEMS].copy()
    sized["rate"] = sized["rework_items"] / sized["n_items"]
    heavy = sized[sized["rate"] >= 0.5]
    invoice = candidates.loc["Invoice recording"]
    confirmation = candidates.loc["Order confirmation"]
    qty = candidates.loc["Quantity change"]
    goods = candidates.loc["Goods receipt"]
    po = candidates.loc["PO creation"]
    block = candidates.loc["Payment block"]
    unused = int(deleted["unused"])
    invoice_accounts = data["invoice_accounts"]
    top5_invoice = invoice_accounts["n_touches"].iloc[:5].sum() / invoice_accounts["n_touches"].sum()
    loop_area = data["loop_areas"].iloc[0]

    lines = [
        "# Rework and automation: what gets redone, and what's still done by hand",
        "",
        "Generated by `scripts/rework.py` from the committed snapshot. Don't edit by hand.",
        "",
        f"Source: {CITATION} Licence: {LICENCE}.",
        "",
        f"This covers the {whole(n_items)} items on the two 3-way flows created from {FOLLOW_UP_START} up to, "
        f"not including, {FOLLOW_UP_END}, as in the bottleneck report. Consignment has no invoices, and the 2-way "
        "flow is one login whose approval steps are part of its design.",
        "",
        "Every event is sorted by how it was done:",
        "",
        "- **System:** a batch account.",
        "- **Vendor or none:** no account, such as the vendor's own invoice date.",
        f"- **Bulk:** a named account doing {BURST_MIN_ITEMS}+ of the same thing in one minute, Part 1's rule for a run.",
        "- **One at a time:** every other named-account event. This is the closest the log gets to hand work.",
        "",
        "One-at-a-time work is counted in **touches**: one account handling one document in one minute counts "
        "once, however many lines it holds. A 10-line PO entered at once is one piece of work, not ten. The log "
        "has no task durations, so touches measure how often people step in, not hours.",
        "",
        "## 1. The answer",
        "",
        f"- **{pct(scope['rework_items'], n_items)} of items have some rework** "
        f"({whole(scope['rework_items'])} of {whole(n_items)}), holding "
        f"{pct(scope['rework_value'], scope['total_value'])} of value.",
        f"- **Most hand work is the core process, not rework.** There are {whole(all_touches)} one-at-a-time touches "
        f"in six months: {pct(core_touches, all_touches)} on core steps, {pct(rework_touches, all_touches)} on rework "
        f"and {pct(control_touches, all_touches)} on controls. Goods receipts and invoice recording alone are "
        f"{pct(two_steps, all_touches)}.",
        f"- **One kind of rework costs time: a price changed after the invoice is in.** On {whole(loop_items)} items, "
        f"{pct(price_after['block_rate'], 1)} of those measured were blocked, against {pct(no_rework['block_rate'], 1)} "
        f"with no rework, and they cleared {price_after['extra_days']:.1f} days later than comparable items. A price "
        f"changed before the invoice goes with {signed(price_before['extra_days'])} days and no extra blocking, and a "
        f"quantity change with {signed(quantity['extra_days'])}.",
        f"- **Some rework is pure waste.** {whole(unused)} items were deleted before anything was received or "
        f"invoiced, a median {days(deleted['median_days'])} after the PO, and "
        f"{whole(reentry.loc['Invoice', 'reentered'])} items had an invoice cancelled and then recorded again.",
        "",
        "## 2. How the work is done",
        "",
        "![Hand work by step](charts/10_hand_work_by_step.png)",
        "",
    ]
    rows = []
    for step, row in modes.iterrows():
        rows.append([step, row["kind"], whole(row["n_events"]), pct(row["system_events"], row["n_events"]),
                     pct(row["bulk_events"], row["n_events"]), pct(row["single_events"], row["n_events"]),
                     pct(row["vendor_events"], row["n_events"]), whole(row["n_touches"]), whole(row["n_accounts"])])
    lines += [
        table(["Step", "Kind", "Events", "System", "Bulk", "One at a time", "Vendor or none", "Touches",
               "Accounts"], rows),
        "",
        f"- **Clearing is already automated in practice:** {pct(modes.loc['Clearing', 'bulk_events'], modes.loc['Clearing', 'n_events'])} "
        "of clearing events are bulk runs, so the payment calendar, not people, sets its pace (Part 3).",
        f"- **Invoice recording is a small team's work.** {whole(invoice['n_touches'])} touches by "
        f"{whole(invoice['n_accounts'])} accounts, and 5 of them make {pct(top5_invoice, 1)}.",
        f"- **Goods receipts are spread out:** {whole(goods['n_touches'])} touches by {whole(goods['n_accounts'])} "
        "accounts, so they're likely done by receiving staff across the business rather than one team.",
        f"- **Payment blocks are mostly lifted in bulk.** {pct(modes.loc['Payment block', 'bulk_events'] + modes.loc['Payment block', 'system_events'], modes.loc['Payment block', 'n_events'])} "
        f"of removals are bulk or system, leaving {whole(block['n_touches'])} touches.",
        "",
        "## 3. How much is rework",
        "",
    ]
    rows = [[step, whole(row["n_items"]), pct(row["n_items"], row["of_items"]), pct(row["hit_value"], row["of_value"]),
             whole(modes.loc[step, "n_touches"]) if step in modes.index else "-"]
            for step, row in rates.iterrows()]
    lines += [
        table(["Rework", "Items", "Share of items", "Share of value", "Touches"], rows),
        "",
        "An item can have more than one kind, so the rows add up to more than the total. Debit memos come from "
        "the vendor and have no account, so they take no touches here, though someone has to deal with them.",
        "",
        "**Bigger items are reworked more,** which is why the value share is well above the item share:",
        "",
        table(["Value fifth", "Items", "Value range", "Items with rework"],
              [[int(row["band"]), whole(row["n_items"]), f"{row['low']:,.0f} to {row['high']:,.0f}",
                pct(row["rework_rate"], 1)] for _, row in bands.iterrows()]),
        "",
        "## 4. The rework that costs time: a price changed after the invoice",
        "",
    ]
    price, qty_timing = timing.loc["Change Price"], timing.loc["Change Quantity"]
    lines += [
        f"Most PO changes happen before the first recorded invoice: {pct(price['before_invoice'], price['n_events'])} of price changes "
        f"and {pct(qty_timing['before_invoice'], qty_timing['n_events'])} of quantity changes. The rest come after an "
        "invoice is recorded, and for prices that pattern is distinct.",
        "",
        f"**It looks like a match loop.** On {whole(loop_items)} items a price was changed at or after the first "
        f"recorded invoice. On {whole(loop['block_after'])} of them ({pct(loop['block_after'], loop_items)}) a payment "
        f"block came off afterwards, {pct(loop['same_day'], loop['block_after'])} of those on the same day and "
        f"{pct(loop['within_week'], loop['block_after'])} within a week. The likely reading is that the invoice price "
        "didn't match the PO, the invoice was blocked, someone corrected the PO, and the block was lifted. The log "
        "doesn't record why a block is set, so that's an inference. For comparison, "
        f"{pct(base['blocked'], base['n_items'])} of main-flow items with an invoice and no price change were blocked.",
        "",
        f"These items are {pct(loop_items, n_items)} of items but {pct(loop['loop_value'], scope['total_value'])} of value. "
        f"{loop_area['spend_area']} holds {pct(loop_area['n_items'], loop_items)} of them, and the 10 vendors with the "
        f"most hold {pct(data['loop_vendors']['n_items'].iloc[:10].sum(), loop_items)}.",
        "",
        "![Extra days by kind of change](charts/11_rework_delay.png)",
        "",
        "Extra days compare each item with items that had no rework at the same vendor with an invoice dated the "
        "same month, so term and run are about the same (the method from Part 3). Only items the wait breakdown "
        "measures are used, which leaves out corrected items, so reversals and debit memos can't be timed. Each item "
        "takes the first row that fits.",
        "",
    ]
    rows = []
    for group in DELAY_GROUPS:
        part = delays[group]
        rows.append([group, whole(part.loc["All", "n_items"]), pct(part.loc["All", "block_rate"], 1),
                     signed(part.loc["All", "extra_days"]), signed(part.loc["All", "extra_overrun"]),
                     signed(part.loc["Jan-Mar", "extra_days"]), signed(part.loc["Apr-Jun", "extra_days"])])
    rows.append(["No rework", whole(no_rework["n_items"]), pct(no_rework["block_rate"], 1), "-", "-", "-", "-"])
    lines += [
        table(["Change", "Items compared", "Blocked", "Extra days to clear", "Extra overrun days",
               "Invoices Jan-Mar", "Invoices Apr-Jun"], rows),
        "",
        f"- **A late price change goes with {price_after['extra_days']:.1f} more days,** "
        f"{price_after['extra_overrun']:.1f} of them overrun past the due day. The size moves between the halves "
        f"({signed(delays['Price changed after the invoice'].loc['Jan-Mar', 'extra_days'])} and "
        f"{signed(delays['Price changed after the invoice'].loc['Apr-Jun', 'extra_days'])}), but it's positive in both.",
        f"- **A price fixed before the invoice arrives isn't blocked more than usual** "
        f"({pct(price_before['block_rate'], 1)}) and adds almost no overrun ({signed(price_before['extra_overrun'])} days). "
        f"Its extra days to clear are smaller overall ({signed(price_before['extra_days'])}), but in Jan-Mar they match "
        f"the late change ({signed(delays['Price changed before the invoice'].loc['Jan-Mar', 'extra_days'])} against "
        f"{signed(delays['Price changed after the invoice'].loc['Jan-Mar', 'extra_days'])}). So the clearest difference is "
        "the blocks and the overrun, not the total days.",
        "- **Quantity changes show no added wait** in either half, though they're the most common rework.",
        "- These compare similar items but don't prove the change caused the difference.",
        "",
        "## 5. Rework that's wasted effort",
        "",
        f"**POs deleted before they were used.** {whole(deleted['n_deleted'])} items were deleted, and "
        f"{whole(unused)} of them ({pct(unused, deleted['n_deleted'])}) had no goods and no invoice. They went a median "
        f"{days(deleted['median_days'])} after the PO, {pct(deleted['within_week'], unused)} within a week. Where one "
        f"named account raised the item one at a time and a named account deleted it, it was the same account on "
        f"{whole(creator['same_account'])} of {whole(creator['n_items'])} items ({pct(creator['same_account'], creator['n_items'])}). "
        "That looks like buyers withdrawing their own POs soon after raising them, such as duplicates or wrong "
        "details, though the log doesn't say why.",
        "",
        f"**Invoices and receipts entered twice.** {whole(reentry.loc['Invoice', 'cancelled'])} items had an invoice "
        f"cancelled, and {whole(reentry.loc['Invoice', 'reentered'])} ({pct(reentry.loc['Invoice', 'reentered'], reentry.loc['Invoice', 'cancelled'])}) "
        f"had one recorded again afterwards. {whole(reentry.loc['Goods', 'cancelled'])} had a goods receipt cancelled, "
        f"and {whole(reentry.loc['Goods', 'reentered'])} ({pct(reentry.loc['Goods', 'reentered'], reentry.loc['Goods', 'cancelled'])}) "
        "had goods recorded again. Cancelling and entering again is how a keying error is usually fixed, so many of "
        "these are likely entry mistakes, though some may be a corrected invoice from the vendor or a real return. "
        "The log can't tell them apart.",
        "",
        "## 6. Where rework concentrates",
        "",
    ]
    rows = [[row["spend_area"], whole(row["n_items"]), pct(row["share_of_items"], 1), pct(row["rework_rate"], 1),
             whole(row["price_after_items"]), f"{row['rework_touches_per_1000']:.0f}", pct(row["share_of_rework_touches"], 1)]
            for _, row in areas.iterrows()]
    overall = 1000 * rework_touches / n_items
    trading = areas.set_index("spend_area").loc["Trading & End Products"]
    late_frame = data["late_by_area"]
    late = late_frame.set_index("spend_area")
    late_all = float(late.loc["All areas", "late_share"])
    lines += [
        f"Spend areas with at least {AREA_MIN_ITEMS:,} items:",
        "",
        table(["Spend area", "Items", "Share of items", "Items with rework", "Price changed after invoice",
               "Rework touches per 1,000 items", "Share of rework touches"], rows),
        "",
        f"Across all items there are {overall:.0f} rework touches per 1,000 items. Trading & End Products stands out: "
        f"{pct(trading['rework_rate'], 1)} of its items have rework and it takes {trading['rework_touches_per_1000']:.0f} "
        f"touches per 1,000. In the Part 3 wait breakdown, {pct(late.loc['Trading & End Products', 'late_share'], 1)} of "
        f"its measured items ran late, against {pct(late_all, 1)} across all areas.",
        "",
        f"**A few vendors drive much of it.** The 10 vendors with the most rework touches hold "
        f"{pct(vendor_top10, 1)} of them. Of {whole(len(sized))} vendors with at least {VENDOR_MIN_ITEMS} items, "
        f"{whole(len(heavy))} have rework on half or more of their items, and they take "
        f"{pct(heavy['rework_touches'].sum(), rework_touches)} of rework touches. Quantity changes are the most "
        f"concentrated rework: 10 vendors hold {pct(qty['top10_vendor_share'], 1)} of them.",
        "",
        "## 7. What to automate or fix first",
        "",
        "Every step done one at a time, by touches. The last two columns check how much of it is really one at a "
        "time: they count touches again with a stricter bulk rule, where an account handling 5 or more documents "
        "of the same step in a minute, or even 2, counts as bulk.",
        "",
    ]
    rows = [[step, row["kind"], whole(row["n_touches"]), pct(row["share_of_touches"], 1), whole(row["n_accounts"]),
             whole(row["n_vendors"]), pct(row["top10_vendor_share"], 1), whole(row["under_5_docs"]), whole(row["single_doc"])]
            for step, row in candidates.iterrows()]
    lines += [
        table(["Step", "Kind", "Touches", "Share", "Accounts", "Vendors", "Top 10 vendors' share",
               "Under 5 documents a minute", "One document a minute"], rows),
        "",
        "The stricter rule hardly changes the big steps, so their touches are real one-at-a-time work. Payment "
        f"blocks are the exception: {whole(block['n_touches'])} touches fall to {whole(block['single_doc'])}, so even "
        "these are mostly handled several at a time.",
        "",
        "The order below weighs three things the table shows: how much hand work a fix removes, whether it also "
        "cuts delay (Part 3), and how few vendors or people it needs to reach.",
        "",
        f"1. **Stop the late price-change loop.** {whole(loop_items)} items, {pct(loop['loop_value'], scope['total_value'])} "
        f"of value and {price_after['extra_days']:.1f} extra days, and {pct(loop['any_block'], loop_items)} have a block "
        "removed. Each one is a price change plus, usually, a block to lift. Fix the PO price before the invoice arrives, by keeping contract prices in "
        f"the system current or agreeing a price tolerance, starting with {loop_area['spend_area']}.",
        f"2. **Automate invoice recording.** {whole(invoice['n_touches'])} touches, {pct(invoice['share_of_touches'], 1)} "
        f"of all hand work, and late recording was {pct(invoice_overrun, 1)} of overrun days in Part 3. It's spread "
        f"over {whole(invoice['n_vendors'])} vendors, with the top 10 at only {pct(invoice['top10_vendor_share'], 1)}, so "
        "this means e-invoicing or invoice capture for the whole team, not a deal with a few vendors. It may also "
        f"cut some of the {whole(reentry.loc['Invoice', 'reentered'])} re-entered invoices.",
        f"3. **Take order confirmations electronically from the 10 vendors that send most of them.** "
        f"{whole(confirmation['n_touches'])} touches by {whole(confirmation['n_accounts'])} accounts, and 10 vendors "
        f"account for {pct(confirmation['top10_vendor_share'], 1)}. "
        f"{pct(modes.loc['Order confirmation', 'system_events'], modes.loc['Order confirmation', 'n_events'])} of "
        "confirmation events already come in through a system account, so the set-up may exist. It's small, but "
        "quick to reach.",
        f"4. **Find out why POs are withdrawn.** {whole(unused)} items were deleted unused, mostly by the buyer who "
        "raised them, within days. Ask buyers for the reason on a sample first. If many are duplicates, a duplicate "
        "check at entry would stop them.",
        f"5. **Ask whether quantity changes are an error at all.** They're the largest rework, {whole(qty['n_touches'])} "
        f"touches, but add no wait and are concentrated: 10 vendors hold {pct(qty['top10_vendor_share'], 1)}. If "
        "those vendors deliver against rolling call-offs, a scheduling agreement would likely remove most of the "
        "changes. If not, they point to an ordering or planning problem worth a look.",
        "",
        f"Not ranked: goods receipts ({whole(goods['n_touches'])} touches by {whole(goods['n_accounts'])} accounts) and PO "
        f"creation ({whole(po['n_touches'])} by {whole(po['n_accounts'])}, top 10 vendors {pct(po['top10_vendor_share'], 1)}) "
        "are the largest hand work, but they're spread across sites and buyers. Scanning at receiving, or catalogues for "
        "repeat buys, are operations projects to scope separately, not accounts-payable fixes.",
        "",
        "## 8. Limits",
        "",
        "- Touches count how often people step in, not how long it takes. A price change may take a minute or an "
        "hour of chasing a vendor, so any cost figure needs time studies or workflow data.",
        f"- \"One at a time\" is an inference from timing. A named account below {BURST_MIN_ITEMS} events a minute "
        "can still be running a tool, and section 7 checks how much that matters.",
        "- The log doesn't say why something was changed, deleted or cancelled, so the readings of the price loop, "
        "the deleted POs and the re-entries are inferences.",
        "- The value field is anonymised and stamped on each event (Part 1), so value shares are the only money "
        "measure, and a price change can't be sized.",
        "- Extra days compare items at the same vendor and invoice month. They show what goes with a change, not "
        "what it causes.",
        "",
    ]
    return "\n".join(lines)


def chart_hand_work(data: dict, path: Path) -> None:
    frame = candidate_table(data)
    frame = frame[frame["kind"].isin(KIND_COLOURS)].sort_values("n_touches")
    frame = frame[frame["n_touches"] >= 500]
    total = candidate_table(data)["n_touches"].sum()
    fig, ax = plt.subplots(figsize=(9.5, 5.4))
    fig.subplots_adjust(left=0.2, right=0.93, top=0.8, bottom=0.08)
    style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color="#e6e5e1", linewidth=0.8)
    y = range(len(frame))
    ax.barh(list(y), frame["n_touches"], height=0.66, color=[KIND_COLOURS[k] for k in frame["kind"]],
            edgecolor=SURFACE, linewidth=2)
    for position, (_, row) in zip(y, frame.iterrows()):
        ax.text(row["n_touches"] + total * 0.004, position,
                f"{row['n_touches']:,}  ({row['share_of_touches']:.0%})" if row["share_of_touches"] >= 0.01
                else f"{row['n_touches']:,}", va="center", fontsize=9, color=INK_2)
    ax.set_yticks(list(y), frame["step"])
    ax.set_xlim(0, frame["n_touches"].max() * 1.22)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    handles = [plt.Rectangle((0, 0), 1, 1, color=colour) for colour in KIND_COLOURS.values()]
    ax.legend(handles, [KIND_LABELS[k] for k in KIND_COLOURS], loc="lower right", frameon=False, fontsize=9.5,
              labelcolor=INK_2)
    two = frame.set_index("step").loc[["Goods receipt", "Invoice recording"], "n_touches"].sum()
    headline(fig, f"Goods receipts and invoice recording are {two / total:.0%} of the hand work",
             "One-at-a-time touches by step, 3-way items created Jan-Jun 2018. Core, rework and control steps with 500+.")
    save(fig, path)


def chart_delay(data: dict, path: Path) -> None:
    delays = delay_rows(data)
    groups = list(DELAY_GROUPS)[::-1]
    fig, ax = plt.subplots(figsize=(9.5, 4.4))
    fig.subplots_adjust(left=0.3, right=0.95, top=0.7, bottom=0.14)
    style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color="#e6e5e1", linewidth=0.8)
    y = list(range(len(groups)))
    values = [delays[g].loc["All", "extra_days"] for g in groups]
    ax.barh(y, values, height=0.56, color=[ORANGE if g == DELAY_GROUPS[0] else BLUE for g in groups],
            edgecolor=SURFACE, linewidth=2)
    for position, group in zip(y, groups):
        part = delays[group]
        for half, marker in (("Jan-Mar", "o"), ("Apr-Jun", "D")):
            ax.plot(part.loc[half, "extra_days"], position, marker=marker, markersize=8, color=INK,
                    markeredgecolor=SURFACE, markeredgewidth=2, linestyle="none")
    low = min(min(delays[g].loc[:, "extra_days"]) for g in groups)
    high = max(max(delays[g].loc[:, "extra_days"]) for g in groups)
    for position, group in zip(y, groups):
        part = delays[group]
        ax.text(high + 0.8, position, f"{part.loc['All', 'extra_days']:+.1f} days, {int(part.loc['All', 'n_items']):,} items",
                va="center", fontsize=9, color=INK_2)
    ax.axvline(0, color=MUTED, linewidth=1)
    ax.set_yticks(y, groups)
    ax.set_xlim(min(low, 0) - 0.5, high + 4.2)
    ax.set_xticks([tick for tick in ax.get_xticks() if min(low, 0) - 0.5 <= tick <= high + 0.5])
    ax.set_xlabel("Extra days to clear against comparable items with no rework", color=INK_2, fontsize=9.5)
    ax.plot([], [], marker="o", color=INK, linestyle="none", label="Invoices Jan-Mar")
    ax.plot([], [], marker="D", color=INK, linestyle="none", label="Invoices Apr-Jun")
    ax.legend(loc="upper left", bbox_to_anchor=(-0.02, 1.17), ncols=2, frameon=False, fontsize=9,
              labelcolor=INK_2)
    headline(fig, "A price fixed after the invoice goes with the most extra days",
             "Bars: mean extra days to first clearing against items at the same vendor and invoice month. Markers: each half.")
    save(fig, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=RESULTS)
    out = parser.parse_args().out
    (out / "charts").mkdir(parents=True, exist_ok=True)
    data = collect(connect())
    (out / "REWORK.md").write_text(build_report(data))
    candidate_table(data).to_csv(out / "automation_candidates.csv", index=False)
    area_table(data).to_csv(out / "rework_by_area.csv", index=False)
    chart_hand_work(data, out / "charts" / "10_hand_work_by_step.png")
    chart_delay(data, out / "charts" / "11_rework_delay.png")


if __name__ == "__main__":
    main()
