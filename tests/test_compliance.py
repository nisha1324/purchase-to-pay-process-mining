"""The compliance report: recounted in pandas, with none of the project's SQL or report code."""
import subprocess
import sys

import pandas as pd
import pytest

from p2p.source import RESULTS, ROOT

REPORT = (RESULTS / "COMPLIANCE.md").read_text()
RULES = pd.read_csv(RESULTS / "compliance_rules.csv").set_index("rule")
MAIN = "3-way match, invoice before GR"
AFTER = "3-way match, invoice after GR"
TWO_WAY = "2-way match"
DELETED = None  # set by the fixture below


@pytest.fixture(scope="module", autouse=True)
def deleted_items(events):
    global DELETED
    DELETED = set(events.loc[events["activity"].isin(["Delete Purchase Order Item", "SRM: Deleted"]), "case_id"])


def shown(text: str) -> None:
    assert text in " ".join(REPORT.split()), f"not in the report: {text}"


@pytest.fixture(scope="module")
def days(events):
    """Each event's Dutch calendar day, worked out with pandas' own time-zone data."""
    local = events["time"].dt.tz_localize("UTC").dt.tz_convert("Europe/Amsterdam")
    return local.dt.tz_localize(None).dt.normalize()


@pytest.fixture(scope="module")
def items(events, cases, days):
    frame = cases.set_index("case_id")
    frame["po_time"] = events[events["activity"] == "Create Purchase Order Item"].set_index("case_id")["time"]

    def count(activity):
        hits = events.loc[events["activity"] == activity, "case_id"].value_counts()
        return hits.reindex(frame.index, fill_value=0)

    def first(activity, keep=None):
        chosen = events["activity"] == activity
        if keep is not None:
            chosen &= keep
        return days[chosen].groupby(events.loc[chosen, "case_id"]).min().reindex(frame.index)

    for name, activity in (("gr", "Record Goods Receipt"), ("gr_cancel", "Cancel Goods Receipt"),
                           ("ir", "Record Invoice Receipt"), ("ir_cancel", "Cancel Invoice Receipt"),
                           ("sub", "Record Subsequent Invoice"), ("sub_cancel", "Cancel Subsequent Invoice"),
                           ("clear", "Clear Invoice"), ("memo", "Vendor creates debit memo"),
                           ("block", "Remove Payment Block")):
        frame[f"n_{name}"] = count(activity)
    frame["po_day"] = first("Create Purchase Order Item")
    frame["gr_day"] = first("Record Goods Receipt")
    frame["ir_day"] = first("Record Invoice Receipt")
    frame["clear_day"] = first("Clear Invoice")
    frame["block_day"] = first("Remove Payment Block")
    frame["vendor_day"] = first("Vendor creates invoice", events["time"] >= "2017-01-01")
    frame["net_goods"] = frame["n_gr"] - frame["n_gr_cancel"]
    frame["net_invoices"] = frame["n_ir"] + frame["n_sub"] - frame["n_ir_cancel"] - frame["n_sub_cancel"]
    frame["corrected"] = frame["n_ir_cancel"] + frame["n_sub_cancel"] + frame["n_memo"] > 0
    return frame


@pytest.fixture(scope="module")
def cohort(items):
    return items[(items["po_time"] >= "2018-01-01") & (items["po_time"] < "2018-07-01")]


def matches(rule, applies, flagged, kept):
    row = RULES.loc[rule]
    assert (row["applies_to"], row["flagged"], row["left_to_review"]) == (
        applies.sum(), (applies & flagged).sum(), (applies & kept).sum())


def test_cleared_no_goods(cohort):
    applies = (cohort["item_category"] == MAIN) & (cohort["n_clear"] > 0)
    flagged = cohort["net_goods"] <= 0
    matches("cleared_no_goods", applies, flagged, flagged & (cohort["net_invoices"] > 0))


def test_cleared_before_goods(cohort):
    applies = (cohort["item_category"] == MAIN) & (cohort["n_clear"] > 0) & (cohort["n_gr"] > 0)
    flagged = cohort["clear_day"] < cohort["gr_day"]
    matches("cleared_before_goods", applies, flagged, flagged & (cohort["net_invoices"] > 0))


def test_block_removed_before_goods(cohort):
    applies = (cohort["item_category"] == MAIN) & (cohort["n_block"] > 0)
    flagged = cohort["gr_day"].isna() | (cohort["block_day"] < cohort["gr_day"])
    matches("block_removed_before_goods", applies, flagged, flagged & (cohort["net_invoices"] > 0))


def test_cleared_before_invoice(cohort):
    applies = (cohort["n_clear"] > 0) & (cohort["n_ir"] > 0)
    flagged = cohort["clear_day"] < cohort["ir_day"]
    matches("cleared_before_invoice", applies, flagged, flagged & (cohort["net_invoices"] > 0))


def test_extra_clearing(cohort):
    applies = cohort["n_clear"] > 0
    flagged = cohort["n_clear"] > cohort["n_ir"] + cohort["n_sub"]
    matches("extra_clearing", applies, flagged, flagged & ~cohort["corrected"])


def test_cleared_after_delete(events, cohort):
    marks = events[events["activity"].isin(["Delete Purchase Order Item", "SRM: Deleted",
                                             "Reactivate Purchase Order Item", "Clear Invoice"])]
    marks = marks.sort_values(["case_id", "time", "seq"])
    previous = marks.groupby("case_id")["activity"].shift()
    hit = marks.loc[(marks["activity"] == "Clear Invoice")
                    & previous.isin(["Delete Purchase Order Item", "SRM: Deleted"]), "case_id"]
    flagged = cohort.index.isin(set(hit))
    applies = cohort["n_clear"] > 0
    matches("cleared_after_delete", applies, flagged, flagged & (cohort["net_invoices"] > 0))


@pytest.mark.parametrize("rule,flows", [("retro_po_three_way", (MAIN, AFTER)), ("retro_po_two_way", (TWO_WAY,))])
def test_invoice_older_than_po(cohort, rule, flows):
    applies = cohort["item_category"].isin(flows) & cohort["vendor_day"].notna()
    gap = (cohort["po_day"] - cohort["vendor_day"]).dt.days
    matches(rule, applies, gap > 0, gap > 7)


def separated(events, cohort, first, second):
    named = events[events["named"]]
    one = named.loc[named["activity"] == first, ["case_id", "user"]].drop_duplicates()
    two = named.loc[named["activity"] == second, ["case_id", "user"]].drop_duplicates()
    same = one.merge(two, on=["case_id", "user"])["case_id"]
    return cohort.index.isin(set(same))


@pytest.fixture(scope="module")
def named(events):
    return events.assign(named=events["user"].str.startswith("user"))


@pytest.mark.parametrize("rule,first,second", [
    ("sod_po_invoice", "Create Purchase Order Item", "Record Invoice Receipt"),
    ("sod_goods_invoice", "Record Goods Receipt", "Record Invoice Receipt"),
    ("sod_po_clear", "Create Purchase Order Item", "Clear Invoice"),
    ("sod_po_goods", "Create Purchase Order Item", "Record Goods Receipt"),
    ("sod_invoice_clear", "Record Invoice Receipt", "Clear Invoice"),
    ("sod_invoice_block", "Record Invoice Receipt", "Remove Payment Block"),
])
def test_separation_of_duties_flagged(named, cohort, rule, first, second):
    assert separated(named, cohort, first, second).sum() == RULES.loc[rule, "flagged"]


def test_invoice_and_clearing_overlap_is_nearly_all_corrections(named, cohort):
    same = separated(named, cohort, "Record Invoice Receipt", "Clear Invoice")
    assert (same & ~cohort["corrected"]).sum() == RULES.loc["sod_invoice_clear", "left_to_review"]


def test_no_account_does_both_purchasing_and_invoicing(named, cohort):
    active = named[named["case_id"].isin(cohort.index) & named["named"]]
    raised = set(active.loc[active["activity"] == "Create Purchase Order Item", "user"])
    invoiced = set(active.loc[active["activity"] == "Record Invoice Receipt", "user"])
    assert raised and invoiced and not raised & invoiced


def test_controls_are_all_zero_in_both_windows():
    controls = RULES[RULES["group"] == "control"]
    assert len(controls) == 10
    assert controls["flagged"].sum() + controls["check_left"].sum() == 0
    assert (controls["applies_to"] > 0).all() and (controls["check_applies_to"] > 0).all()


def test_controls_recounted(cohort):
    after = cohort[cohort["item_category"] == AFTER]
    invoiced = after[(after["n_ir"] > 0) & (after["n_gr"] > 0)]
    assert len(invoiced) == RULES.loc["after_invoice_before_goods", "applies_to"]
    assert (invoiced["ir_day"] < invoiced["gr_day"]).sum() == 0
    cleared = after[after["n_clear"] > 0]
    assert ((cleared["net_goods"] <= 0) | (cleared["clear_day"] < cleared["gr_day"])).sum() == 0
    assert (cohort["gr_day"] < cohort["po_day"]).sum() == 0
    assert (cohort["ir_day"] < cohort["po_day"]).sum() == 0
    assert (cohort.loc[cohort["item_category"] == TWO_WAY, "n_gr"] > 0).sum() == 0


def test_reversal_story(cohort):
    flagged = cohort[(cohort["item_category"] == MAIN) & (cohort["n_clear"] > 0) & (cohort["net_goods"] <= 0)]
    stands = (flagged["net_invoices"] > 0).sum()
    shown(f"{len(flagged) - stands:,} of them ({100 * (len(flagged) - stands) / len(flagged):.0f}%) have their invoice reversed")
    shown(f"The other {stands:,} items")


def test_clearing_gap_without_the_groups(cohort, events):
    # Chosen again here from the raw counts, on the cohort alone.
    typed = cohort[cohort["item_type"].isin(["Service", "Subcontracting"])]
    groups = typed.groupby(["vendor", "item_type", "document_type"])["n_clear"]
    quiet = groups.agg(n="size", cleared=lambda v: (v > 0).sum())
    quiet = quiet[(quiet["n"] >= 20) & (quiet["cleared"] == 0)]
    keys = pd.MultiIndex.from_frame(cohort[["vendor", "item_type", "document_type"]])
    rest = cohort[~keys.isin(quiet.index)]
    after = rest[rest["item_category"] == AFTER]
    shown(f"it clears at {100 * (after['n_clear'] > 0).mean():.1f}%")
    main = rest[rest["item_category"] == MAIN]
    shown(f"against {100 * (main['n_clear'] > 0).mean():.1f}% on the main flow")
    assert len(quiet) == 13


def test_quiet_service_vendors_stay_quiet_later(items, cohort):
    service = cohort[(cohort["item_type"] == "Service") & (cohort["document_type"] == "Standard PO")]
    by_vendor = service.groupby("vendor")["n_clear"].agg(["size", lambda v: (v > 0).sum()])
    by_vendor.columns = ["n", "cleared"]
    quiet = by_vendor[(by_vendor["n"] >= 20) & (by_vendor["cleared"] == 0)].index
    later = items[(items["po_time"] >= "2018-07-01") & (items["po_time"] < "2018-10-01")
                  & items["vendor"].isin(quiet) & (items["item_type"] == "Service")
                  & (items["document_type"] == "Standard PO")]
    assert len(later) > 0 and later["n_ir"].sum() == 0
    shown(f"0 invoices on {len(later):,} items")


def test_two_way_is_one_company_and_one_account(items, events):
    two_way = items[items["item_category"] == TWO_WAY]
    assert two_way["company"].nunique() == 1
    assert (items["company"] == two_way["company"].iloc[0]).sum() == len(two_way)
    raised = events[(events["activity"] == "Create Purchase Order Item") & events["case_id"].isin(two_way.index)]
    assert raised["user"].nunique() == 1
    shown(f"`{raised['user'].iloc[0]}` raised every one")
    approved = events.loc[events["activity"] == "Change Approval for Purchase Order", "case_id"]
    assert two_way.index.isin(set(approved)).all()


def test_government_payments(cohort):
    government = cohort[(cohort["item_category"] == TWO_WAY) & (cohort["sub_spend_area"] == "Government payments")]
    shown(f"{(government['n_clear'] > 0).sum()} have cleared")
    shown(f"{len(government)} of {(cohort['item_category'] == TWO_WAY).sum()} cohort items")


def test_every_item_lands_in_one_outcome_and_the_group_table_recounts(cohort):
    def outcomes(group):
        cleared = group["n_clear"] > 0
        deleted = ~cleared & group.index.isin(DELETED)
        standing = group["net_invoices"] > 0
        return (f"{100 * cleared.mean():.1f}% | {(~cleared & ~deleted & standing).sum():,} | "
                f"{(~cleared & ~deleted & ~standing).sum():,} | {deleted.sum():,} |")

    two_way = cohort[cohort["item_category"] == TWO_WAY]
    government = two_way["sub_spend_area"] == "Government payments"
    shown(outcomes(two_way[government]))
    shown(outcomes(two_way[~government]))
    typed = cohort[cohort["item_type"].isin(["Service", "Subcontracting"])]
    quiet = typed.groupby(["vendor", "item_type", "document_type"])["n_clear"].agg(["size", "max"])
    quiet = quiet[(quiet["size"] >= 20) & (quiet["max"] == 0)]
    keys = pd.MultiIndex.from_frame(cohort[["vendor", "item_type", "document_type"]])
    rest = cohort[~keys.isin(quiet.index)]
    shown(outcomes(rest[rest["item_category"] == MAIN]))
    shown(outcomes(rest[(rest["item_category"] == AFTER) & (rest["item_type"] == "Service")]))


def test_main_flow_rules_are_all_under_one_percent():
    assert (RULES.loc[["cleared_no_goods", "cleared_before_goods", "block_removed_before_goods"],
                      "share_of_items_pct"] < 1).all()


def test_readme_part_2_matches_the_results():
    readme = " ".join((ROOT / "README.md").read_text().split())
    review = pd.read_csv(RESULTS / "review_list.csv")
    assert f"tests {len(RULES)} rules" in readme
    assert f"{(RULES['group'] == 'control').sum()} of the {len(RULES)} rules have no failures" in readme
    assert f"{len(review):,} items ({100 * len(review) / 134633:.1f}% of items" in readme
    assert f"{(review['rules_failed'] >= 2).sum()} of them fail two or more rules" in readme
    wording = {"cleared_no_goods": "Main flow: cleared with no goods standing",
               "cleared_before_goods": "Main flow: cleared before the first goods receipt",
               "block_removed_before_goods": "Main flow: payment block removed before goods",
               "cleared_after_delete": "Cleared after the item was flagged deleted",
               "extra_clearing": "Cleared more times than invoices recorded",
               "retro_po_three_way": "3-way flows: vendor invoice over 7 days older than the PO",
               "sod_po_goods": "Same account raised the PO and recorded the goods"}
    for rule, title in wording.items():
        row = RULES.loc[rule]
        assert (f"| {title} | {row['flagged']:,} | {row['left_to_review']:,} | "
                f"{row['share_of_items_pct']:.2f}% | {row['share_of_value_pct']:.2f}% |") in readme, rule


def test_review_list_is_every_kept_item_once():
    review = pd.read_csv(RESULTS / "review_list.csv")
    assert review["case_id"].is_unique
    assert review["rules_failed"].sum() == RULES["left_to_review"].sum()
    assert review["rules_failed"].is_monotonic_decreasing
    assert set(" ".join(review["rules"]).split()) <= set(RULES[RULES["group"] == "review"].index)
    shown(f"has {len(review):,} cohort items")


def test_report_and_tables_match_a_fresh_run(tmp_path):
    subprocess.run([sys.executable, "-m", "scripts.compliance", "--out", str(tmp_path)],
                   cwd=ROOT, check=True)
    for name in ("COMPLIANCE.md", "compliance_rules.csv", "review_list.csv",
                 "charts/04_flagged_vs_review.png", "charts/05_clearing_by_group.png"):
        assert (tmp_path / name).read_bytes() == (RESULTS / name).read_bytes(), name
