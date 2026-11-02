"""Check each flow's rules on the items that had time to finish.

    python -m scripts.compliance

Writes results/COMPLIANCE.md, results/compliance_rules.csv, results/review_list.csv and 2 charts.
Every number comes from sql/10_item_facts.sql and sql/compliance.sql.
"""
import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from p2p.db import SQL_DIR, build, connect, named_queries, run
from p2p.source import (CHARTS, CHECK_END, CHECK_START, CITATION, CLUSTER_MIN_ITEMS, FOLLOW_UP_END,
                        FOLLOW_UP_START, LICENCE, RESULTS, RETRO_MIN_DAYS, VENDOR_DATE_FLOOR)
from scripts.profile_log import (BLUE, GRID, INK, INK_2, MUTED, ORANGE, SURFACE, headline, pct,
                                 save, table, whole)

AQUA, YELLOW = "#1baf7a", "#eda100"  # categorical slots 3 and 4

PUBLISHER, AUDIT = "Publisher", "Audit test"

# Should never fail if the system's controls work: (rule, what it checks, source).
CONTROLS = (
    ("after_invoice_before_goods", "Invoice-after-GR flow: invoice recorded before the goods", PUBLISHER),
    ("after_cleared_no_goods", "Invoice-after-GR flow: cleared before or without goods", PUBLISHER),
    ("consignment_invoiced", "Consignment: any invoice or clearing on the PO", PUBLISHER),
    ("two_way_goods", "2-way flow: any goods receipt", PUBLISHER),
    ("two_way_approved_after_invoice", "2-way flow: first approval after the invoice was recorded", AUDIT),
    ("goods_before_po", "Goods receipt dated before the PO", AUDIT),
    ("invoice_recorded_before_po", "Invoice recorded before the PO", AUDIT),
    ("sod_po_invoice", "Same account raised the PO and recorded the invoice", AUDIT),
    ("sod_goods_invoice", "Same account recorded the goods and the invoice", AUDIT),
    ("sod_po_clear", "Same account raised the PO and cleared the invoice", AUDIT),
)

# Fail on some items: (rule, what it checks, source, what "left to review" takes out).
REVIEWS = (
    ("cleared_no_goods", "Main flow: cleared with no goods standing", PUBLISHER, "invoice reversed too"),
    ("cleared_before_goods", "Main flow: cleared before the first goods receipt", PUBLISHER, "invoice reversed too"),
    ("block_removed_before_goods", "Main flow: payment block removed before goods", PUBLISHER, "invoice reversed too"),
    ("cleared_before_invoice", "Cleared before the invoice was recorded", AUDIT, "invoice reversed too"),
    ("cleared_after_delete", "Cleared after the item was flagged deleted", AUDIT, "invoice reversed too"),
    ("extra_clearing", "Cleared more times than invoices recorded", AUDIT, "cancellation or debit memo"),
    ("retro_po_three_way", "3-way flows: vendor invoice dated before the PO", AUDIT, f"{RETRO_MIN_DAYS} days or less"),
    ("retro_po_two_way", "2-way flow: vendor invoice dated before the PO", AUDIT, f"{RETRO_MIN_DAYS} days or less"),
    ("sod_po_goods", "Same account raised the PO and recorded the goods", AUDIT, "bulk runs"),
    ("sod_invoice_clear", "Same account recorded the invoice and cleared it", AUDIT, "cancellation or debit memo"),
    ("sod_invoice_block", "Same account recorded the invoice and removed its block", AUDIT, "bulk runs, cancellation or debit memo"),
)

# Report order, top to bottom on the chart.
SEGMENTS = (
    "Main flow: everything else",
    "Main flow: subcontractors never cleared",
    "Invoice after GR: goods",
    "Invoice after GR: other services",
    "Invoice after GR: services, vendors never invoiced",
    "Invoice after GR: subcontractors never cleared",
    "2-way: everything else",
    "2-way: government payments",
)
OUTCOMES = (("cleared", "Cleared", BLUE), ("invoice_open", "Invoice open", ORANGE),
            ("not_invoiced", "No invoice standing", AQUA), ("deleted", "Deleted", YELLOW))

MAIN = "3-way match, invoice before GR"
AFTER = "3-way match, invoice after GR"
TWO_WAY = "2-way match"


def collect(con) -> dict:
    build(con, "10_item_facts.sql")
    return {name: run(con, sql) for name, sql in named_queries(SQL_DIR / "compliance.sql").items()}


def rate(part, total, digits=2) -> str:
    return "n/a" if total == 0 else pct(part, total, digits)


def rule_row(data: dict, rule: str, period: str = "cohort"):
    totals = data["rule_totals"]
    return totals[(totals["rule"] == rule) & (totals["period"] == period)].iloc[0]


def rule_table(data: dict):
    """One tidy row per rule, for the CSV and the tests."""
    rows = []
    for rule, title, source, *rest in CONTROLS + REVIEWS:
        cohort, check = rule_row(data, rule), rule_row(data, rule, "check")
        rows.append({
            "rule": rule, "title": title, "source": source,
            "group": "control" if rule in {c[0] for c in CONTROLS} else "review",
            "removed": rest[0] if rest else "",
            "applies_to": int(cohort["population"]), "flagged": int(cohort["flagged"]),
            "left_to_review": int(cohort["kept"]), "documents": int(cohort["documents"]),
            "vendors": int(cohort["vendors"]), "top_vendor_items": int(cohort["top_vendor_items"]),
            "share_of_items_pct": round(100 * cohort["kept"] / cohort["population"], 3),
            "share_of_value_pct": round(100 * cohort["kept_value"] / cohort["population_value"], 3)
            if cohort["population_value"] else 0.0,
            "check_applies_to": int(check["population"]), "check_left": int(check["kept"]),
        })
    return pd.DataFrame(rows)


def build_report(data: dict) -> str:
    rules = rule_table(data).set_index("rule")
    flows = data["cohort_flows"].set_index("flow")
    cohort_items, cohort_value = data["cohort_value"].iloc[0]
    mix = data["reversal_mix"].set_index("kind")
    no_goods = rules.loc["cleared_no_goods"]
    reversed_items = no_goods["flagged"] - no_goods["left_to_review"]
    reversed_memo = int(mix.loc[[k for k in mix.index if "reversed" in k], "with_debit_memo"].sum())
    retro = data["retro_gaps"].set_index("flow")
    accounts = data["pair_accounts"].set_index("rule")
    overlap = data["role_overlap"].set_index("pair")
    segments = data["segments"].set_index("segment").loc[list(SEGMENTS)]
    gap = data["flow_gap"].set_index("flow")
    groups = data["quiet_vendor_groups"].set_index("item_type")
    later = data["quiet_out_of_sample"].set_index(["item_type", "period"])
    timing = data["subcontract_timing"].iloc[0]
    shape = data["quiet_service_shape"].iloc[0]
    invoice_accounts = data["quiet_invoice_accounts"].set_index("account_type")["n_items"]
    government = data["government"].set_index("government")
    gov, rest = government.loc[True], government.loc[False]
    open_two_way = data["oldest_open_two_way"].iloc[0]
    log_two_way = data["two_way_log"].iloc[0]
    deletes = data["delete_mix"].set_index("document_type")
    review = data["review_list"]
    repeat = int((review["rules_failed"] >= 2).sum())
    two_way_items = int(flows.loc[TWO_WAY, "n_items"])
    two_way_value = flows.loc[TWO_WAY, "po_value"]
    after_uncleared = gap.loc[AFTER, "n_items"] - gap.loc[AFTER, "cleared"]
    quiet_after = segments.loc[["Invoice after GR: services, vendors never invoiced",
                                "Invoice after GR: subcontractors never cleared"]]
    after_quiet = quiet_after["n_items"].sum()
    single_entity = (log_two_way["n_items"] == log_two_way["in_company"] == log_two_way["company_items"]
                     and log_two_way["po_accounts"] == 1)
    if not single_entity or log_two_way["with_approval"] < log_two_way["n_items"]:
        raise ValueError("section 8's description of the 2-way flow no longer fits the data")
    held = (rules["group"] == "control") & (rules["left_to_review"] == 0) & (rules["check_left"] == 0)
    controls_failed = rules[(rules["group"] == "control") & ~held]
    if len(controls_failed):
        raise ValueError(f"a control failed, so the report's wording is wrong: {list(controls_failed.index)}")

    def share_row(flow):
        return [flow, whole(flows.loc[flow, "n_items"]), pct(flows.loc[flow, "n_items"], cohort_items),
                pct(flows.loc[flow, "po_value"], cohort_value)]

    control_rows = [[row["title"], row["source"], whole(row["applies_to"]), whole(row["left_to_review"]),
                     f"{whole(row['check_left'])} of {whole(row['check_applies_to'])}"]
                    for _, row in rules[rules["group"] == "control"].iterrows()]
    review_rows = [[row["title"], row["source"], whole(row["applies_to"]), whole(row["flagged"]),
                    row["removed"], whole(row["left_to_review"]),
                    rate(row["left_to_review"], row["applies_to"]),
                    whole(row["documents"]), whole(row["vendors"]),
                    rate(row["top_vendor_items"], row["left_to_review"], 0),
                    f"{row['share_of_value_pct']:.2f}%",
                    rate(row["check_left"], row["check_applies_to"])]
                   for _, row in rules[rules["group"] == "review"].iterrows()]
    segment_rows = [[name, whole(row["n_items"]), whole(row["vendors"]), pct(row["cleared"], row["n_items"]),
                     whole(row["invoice_open"]), whole(row["not_invoiced"]), whole(row["deleted"])]
                    for name, row in segments.iterrows()]
    gap_rows = [[flow, pct(gap.loc[flow, "cleared"], gap.loc[flow, "n_items"]),
                 pct(gap.loc[flow, "rest_cleared"], gap.loc[flow, "rest"]), whole(gap.loc[flow, "rest"])]
                for flow in (MAIN, AFTER, TWO_WAY)]
    retro_rows = [[flow, whole(retro.loc[flow, "invoiced"]), whole(retro.loc[flow, "before_po"]),
                   pct(retro.loc[flow, "before_po"], retro.loc[flow, "invoiced"]),
                   pct(retro.loc[flow, "over_7"], retro.loc[flow, "invoiced"]),
                   pct(retro.loc[flow, "over_30"], retro.loc[flow, "invoiced"]),
                   f"{retro.loc[flow, 'median_gap']:.0f} days"]
                  for flow in (MAIN, AFTER, TWO_WAY)]
    role_rows = [[title, whole(overlap.loc[pair, "first_accounts"]), whole(overlap.loc[pair, "second_accounts"]),
                  whole(overlap.loc[pair, "in_both"])]
                 for pair, title in (("sod_po_invoice", "Raise PO / record invoice"),
                                     ("sod_goods_invoice", "Record goods / record invoice"),
                                     ("sod_po_clear", "Raise PO / clear invoice"),
                                     ("sod_po_goods", "Raise PO / record goods"),
                                     ("sod_invoice_clear", "Record invoice / clear invoice"),
                                     ("sod_invoice_block", "Record invoice / remove block"))]

    return f"""# Compliance: which rules held, and which need a closer look

Generated by `scripts/compliance.py` from the committed snapshot. Don't edit by hand.

Source: {CITATION} Licence: {LICENCE}.

## 1. How the rules were tested

- **Items, not events, on items with time to finish.** Rates use the {whole(cohort_items)} items created
  from {FOLLOW_UP_START} up to, not including, {FOLLOW_UP_END} (the cohort from the source profile).
  Items from {CHECK_START} up to {CHECK_END} are a second check, shown in the last column.
- **"Before" means an earlier calendar day.** Vendor dates and no-account clearing dates have no time of
  day, so a rule fails only when the day is earlier, in Dutch time.
- **Two sources of rule.** "Publisher" rules come from the dataset's description of each flow: on the
  main flow, invoices can arrive first but stay blocked until goods are received, and should be
  cleared only after that. Reading "blocked" as the log's payment block is an interpretation.
  "Audit test" rules are standard purchase-to-pay checks that the publisher doesn't state.
- **Flagged, then left to review.** "Flagged" is the plain reading of a rule. "Left to review" removes
  what the record itself explains, as listed for each rule. Removed items aren't proven fine.
- **Value is a share only** (see 3.1 in the source profile). The publisher's rule that values must match
  can't be tested, because the value field is stamped on each event.

| Flow | Items in the cohort | Share of items | Share of value |
|---|---|---|---|
{chr(10).join("| " + " | ".join(share_row(flow)) + " |" for flow in (MAIN, AFTER, "Consignment", TWO_WAY))}

## 2. Controls that held

These {len(control_rows)} rules had no failures, in the cohort or the later check.

{table(["Rule", "Source", "Items it applies to", "Failures", "Later items failing"], control_rows)}

Where the system can enforce a rule, it looks enforced: neither invoice-after-GR rule has a single
exception. The remaining risk sits where a person or a batch can override the sequence, and in the
items these checks can't see.

## 3. Rules that need review

{table(["Rule", "Source", "Applies to", "Flagged", "Taken out", "Left to review", "Share", "Documents", "Vendors",
        "Largest vendor", "Share of value", "Share, later items"], review_rows)}

"Largest vendor" is that vendor's share of the items left to review. "Share of value" is against the
value of the items the rule applies to.

![Flagged against left to review](charts/04_flagged_vs_review.png)

## 4. "Cleared with no goods" is mostly reversals

Taken at face value, {whole(no_goods['flagged'])} main-flow items were cleared with no goods standing. But
{whole(reversed_items)} of them ({pct(reversed_items, no_goods['flagged'], 0)}) have their invoice reversed as well, and {whole(reversed_memo)}
of those carry a debit memo.

{table(["What happened", "Items", "With a debit memo"], [[kind, whole(row["n_items"]), whole(row["with_debit_memo"])] for kind, row in mix.iterrows()])}

With the invoice gone there's nothing left to pay, so `Clear Invoice` on these items most likely
closes out a correction. That's an inference: the log has no payment amounts. It does mean
`Clear Invoice` shouldn't be read as "paid" without checking for reversals.

The other {whole(no_goods['left_to_review'])} items ({rate(no_goods['left_to_review'], no_goods['applies_to'])} of cleared main-flow items)
have an invoice standing and no goods. That's the real size of this exception on this log.

## 5. Invoices older than their PO

{table(["Flow", "Items with a vendor invoice date", "Dated before the PO", "Share", f"Over {RETRO_MIN_DAYS} days", "Over 30 days", "Median gap"], retro_rows)}

Vendor dates from {VENDOR_DATE_FLOOR} onwards are used; the earlier ones are treated as typing errors.
An invoice dated before its PO suggests the PO was raised to fit an invoice, so the purchase wasn't
committed through the system first, unless the vendor's date is wrong. On the 3-way flows it's rare, though one vendor holds
{rate(rules.loc['retro_po_three_way', 'top_vendor_items'], rules.loc['retro_po_three_way', 'left_to_review'], 0)} of the items over {RETRO_MIN_DAYS} days. On the 2-way flow it's the norm.

## 6. Separation of duties

Only named accounts count, because batch accounts are automated. The table shows named accounts active on
cohort items, by duty.

{table(["Pair of duties", "Accounts on the first", "Accounts on the second", "On both"], role_rows)}

Purchasing and invoice processing don't overlap at all: no account does both, so those rules can't
fail. Overlap appears only inside each side:

- **PO and goods:** {whole(rules.loc['sod_po_goods', 'left_to_review'])} items ({rate(rules.loc['sod_po_goods', 'left_to_review'], rules.loc['sod_po_goods', 'applies_to'])}), across {whole(accounts.loc['sod_po_goods', 'accounts'])} accounts, but
  {rules.loc['sod_po_goods', 'share_of_value_pct']:.1f}% of the value. `{accounts.loc['sod_po_goods', 'top_account']}` alone is {whole(accounts.loc['sod_po_goods', 'top_account_items'])} items. This is the one to look at:
  one person ordering and confirming receipt is the classic gap.
- **Invoice and clearing:** {whole(rules.loc['sod_invoice_clear', 'flagged'])} items flagged, but {whole(rules.loc['sod_invoice_clear', 'flagged'] - rules.loc['sod_invoice_clear', 'left_to_review'])} involve a cancellation or debit
  memo, which suggests a clerk closing out a correction. {whole(rules.loc['sod_invoice_clear', 'left_to_review'])} are left.
- **Invoice and block:** {whole(rules.loc['sod_invoice_block', 'left_to_review'])} items left, across {whole(accounts.loc['sod_invoice_block', 'accounts'])} accounts.

An account isn't proof of a person, and the log doesn't show any authorisation limits or sign-offs
behind these steps.

## 7. Why some flows clear less

The source profile left a question open: why do so few items clear on the 2-way and invoice-after-GR
flows? On the invoice-after-GR flow it's a few groups, not a general problem. The 2-way flow stays
low even without its largest group (section 8). Groups marked "never" were chosen because
none of their items cleared in the cohort, with at least {CLUSTER_MIN_ITEMS} items per vendor, so they sit at
0% by construction. They show where the uncleared items are.

{table(["Group", "Items", "Vendors", "Cleared", "Invoice open", "No invoice standing", "Deleted"], segment_rows)}

![Clearing outcome by group](charts/05_clearing_by_group.png)

{table(["Flow", "Cleared, all items", "Cleared, without the groups above", "Items left"], gap_rows)}

"Without the groups" leaves out the "never" groups and, on the 2-way flow, government payments.
{whole(after_quiet)} items at {whole(quiet_after['vendors'].sum())} vendors are {pct(after_quiet, after_uncleared, 0)} of the {whole(after_uncleared)} uncleared items on the
invoice-after-GR flow. Without them it clears at {pct(gap.loc[AFTER, 'rest_cleared'], gap.loc[AFTER, 'rest'])}, against {pct(gap.loc[MAIN, 'rest_cleared'], gap.loc[MAIN, 'rest'])} on the main flow.

- **Services that are received but never invoiced.** {whole(groups.loc['Service', 'vendors'])} vendors, {whole(shape['logistics'])} of {whole(shape['n_items'])} items in
  Logistics. All have service entry sheets (median {shape['median_sheets']:.0f}), and on {whole(shape['one_month'])} items they all fall in one
  calendar month. None has an invoice on the PO. **Later items agree:** chosen on the cohort alone, the same
  vendors show {whole(later.loc[('Service', 'check'), 'invoiced'])} invoices on {whole(later.loc[('Service', 'check'), 'n_items'])} items from {CHECK_START[:7]} to 2018-09. It looks like settlement outside
  this process, such as self-billing. The log can't confirm that.
- **Subcontractors with a late automated invoice.** {whole(groups.loc['Subcontracting', 'vendors'])} vendors. {whole(timing['invoiced'])} of their cohort items
  have an invoice, and `Record Invoice Receipt` on {whole(invoice_accounts.get('batch', 0))} of them is a batch account. It lands a
  median {timing['median_po_to_invoice']:.0f} days after the PO, and 90% land between {timing['p05_po_to_invoice']:.0f} and {timing['p95_po_to_invoice']:.0f} days, which looks like a timed
  process rather than a vendor's bill. {whole(timing['cleared'])} have cleared. **The log stopping explains only part of this:** on other
  subcontracting items, 90% clear within {timing['other_p90']:.0f} days of the invoice, and {whole(timing['older_than_p90'])} of these
  invoices are older than that.

## 8. The 2-way flow is a separate process

Across the whole log, all {whole(log_two_way['n_items'])} 2-way items belong to one company, and they're the only items
that company has. `{gov['top_account']}` raised every one, and every one has an approval change. So this
flow is one entity's process, and it often runs in reverse: {pct(rules.loc['retro_po_two_way', 'flagged'], rules.loc['retro_po_two_way', 'applies_to'], 0)} of cohort invoices are dated before
the PO. In the cohort and the later check, approval came before the invoice was recorded every
time (section 2), but by design there's no goods check to fall back on.

- **Government payments** are {whole(gov['n_items'])} of {whole(two_way_items)} cohort items and {pct(gov['value'], two_way_value, 0)} of the flow's value, across
  {whole(gov['vendors'])} vendors. {whole(gov['cleared'])} have cleared and {whole(gov['invoice_open'])} are open, with {whole(gov['invoiced_mar_apr'])} invoices recorded in March or April 2018.
- **Everything else** clears at {pct(rest['cleared'], rest['n_items'])}, with {whole(rest['invoice_open'])} invoices open.
- Open invoices on this flow have been waiting a median {open_two_way['median_days_open']:.0f} days at the end of the log.

Either these are unpaid, or they're settled outside this log, and the log can't tell which. So the
clearing rate on this flow says nothing yet about payment performance.

## 9. The review list

[`review_list.csv`](review_list.csv) has {whole(len(review))} cohort items ({pct(len(review), cohort_items)} of items) that any review rule
still flags, or {pct(review['po_value'].sum(), cohort_value)} of cohort value. {whole(repeat)} of them fail two or more rules. It's sorted so
items failing the most rules come first, then the largest. It lists items to check, not proven breaches.

`cleared_after_delete` is small by count but not by value: {whole(rules.loc['cleared_after_delete', 'left_to_review'])} items hold
{rules.loc['cleared_after_delete', 'share_of_value_pct']:.1f}% of the value of cleared items, and {whole(deletes.loc['EC Purchase order', 'n_items'])} of them are on EC purchase orders. A deletion flag
can close the rest of a line that's already been delivered, so a real invoice clearing after it can be
normal. They're worth a sample check because a few are very large.

## 10. What this means

- **The system controls work.** The invoice-after-GR flow's order is enforced, and no named account
  works on both purchasing and invoice processing.
- **Exceptions on the main flow are small.** All 3 main-flow rules fail on under 1% of the items they
  apply to, which calls for a targeted review, not a redesign.
- **Check reversals before reporting a breach.** On this log, most of the alarming counts were
  corrections. Without that step, "cleared with no goods" would be overstated about
  {no_goods['flagged'] / no_goods['left_to_review']:.0f} times.
- **Ask about what's outside the log.** Whether the {whole(after_quiet)} invoice-after-GR items and the 2-way
  government payments are settled elsewhere is one question for finance, and it decides most of the
  clearing gap.

## 11. Limits

- Value matching can't be tested, so a rule passing here says nothing about amounts.
- The log shows no approval limits, payment dates or bank payments.
- Same-day events count as in order, so these checks can miss a same-day breach.
- Item counts can overlap between rules. {whole(repeat)} items appear under more than one.
- The later check had 3.5 to 6.5 months of follow-up, so rates that depend on clearing can be low there.
"""


def chart_flagged(data: dict, path: Path) -> None:
    rows = rule_table(data).set_index("rule").loc[
        ["sod_invoice_clear", "extra_clearing", "cleared_no_goods", "block_removed_before_goods",
         "cleared_after_delete", "cleared_before_goods"]]
    labels = ["Same account records and clears invoice", "Cleared more times than invoices",
              "Main flow: cleared with no goods", "Main flow: block removed before goods",
              "Cleared after deletion flag", "Main flow: cleared before goods"]
    y = list(range(len(rows)))[::-1]
    fig, ax = plt.subplots(figsize=(9, 4.8))
    fig.subplots_adjust(left=0.36, right=0.95, top=0.78, bottom=0.08)
    ax.set_facecolor(SURFACE)
    fig.set_facecolor(SURFACE)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    height = 0.34
    ax.barh([v + height / 2 + 0.02 for v in y], rows["flagged"], height=height, color=MUTED,
            label="Flagged by the plain rule")
    ax.barh([v - height / 2 - 0.02 for v in y], rows["left_to_review"], height=height, color=BLUE,
            label="Left to review")
    for position, (_, row) in zip(y, rows.iterrows()):
        ax.annotate(f"{row['flagged']:,}", (row["flagged"], position + height / 2 + 0.02), xytext=(5, 0),
                    textcoords="offset points", va="center", fontsize=9.5, color=INK_2)
        ax.annotate(f"{row['left_to_review']:,}", (row["left_to_review"], position - height / 2 - 0.02),
                    xytext=(5, 0), textcoords="offset points", va="center", fontsize=9.5, color=INK)
    ax.set_yticks(y, labels)
    ax.set_xticks([])
    ax.tick_params(colors=INK_2, length=0, labelsize=10)
    ax.set_xlim(0, rows["flagged"].max() * 1.1)
    ax.legend(loc="lower right", frameon=False, fontsize=10, labelcolor=INK_2)
    headline(fig, "Most flags that look like breaches are corrections",
             "Six rules, on cohort items. 'Left to review' takes out items whose invoice was reversed or corrected.")
    save(fig, path)


def chart_clearing(data: dict, path: Path) -> None:
    segments = data["segments"].set_index("segment").loc[list(SEGMENTS)]
    shares = segments[[key for key, _, _ in OUTCOMES]].div(segments["n_items"], axis=0) * 100
    y = list(range(len(segments)))[::-1]
    fig, ax = plt.subplots(figsize=(9, 5.4))
    fig.subplots_adjust(left=0.40, right=0.98, top=0.76, bottom=0.05)
    ax.set_facecolor(SURFACE)
    fig.set_facecolor(SURFACE)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    left = [0.0] * len(segments)
    for key, label, colour in OUTCOMES:
        ax.barh(y, shares[key], left=left, height=0.62, color=colour, label=label,
                edgecolor=SURFACE, linewidth=1.5)
        left = [a + b for a, b in zip(left, shares[key])]
    for position, (name, row) in zip(y, segments.iterrows()):
        ax.text(101, position, whole(row["n_items"]), va="center", fontsize=9.5, color=INK_2)
    ax.text(101, len(segments) - 0.42, "Items", fontsize=9.5, color=INK_2, va="bottom")
    ax.set_yticks(y, list(segments.index))
    ax.set_xlim(0, 112)
    ax.set_xticks([])
    ax.tick_params(colors=INK_2, length=0, labelsize=9.5)
    ax.legend(loc="upper left", bbox_to_anchor=(-0.02, 1.13), ncols=4, frameon=False, fontsize=9.5,
              labelcolor=INK_2, handlelength=1.2, columnspacing=1.4)
    headline(fig, "A few groups explain why the smaller flows clear less",
             "Outcome by the end of the log of each item created Jan-Jun 2018. Consignment is left out.")
    save(fig, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=RESULTS)
    out = parser.parse_args().out
    (out / "charts").mkdir(parents=True, exist_ok=True)
    data = collect(connect())
    (out / "COMPLIANCE.md").write_text(build_report(data))
    rule_table(data).to_csv(out / "compliance_rules.csv", index=False)
    data["review_list"].to_csv(out / "review_list.csv", index=False)
    chart_flagged(data, out / "charts" / "04_flagged_vs_review.png")
    chart_clearing(data, out / "charts" / "05_clearing_by_group.png")


if __name__ == "__main__":
    main()
