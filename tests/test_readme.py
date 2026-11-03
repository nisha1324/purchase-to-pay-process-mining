"""The README's TL;DR and ranked recommendations, checked against the committed result files.

The reports themselves are recounted in pandas by the other test files. This file makes sure the
summary at the top of the README says what those reports and tables say, so a figure can't drift.
"""
import re

import pandas as pd

from p2p.source import RESULTS, ROOT

README = " ".join((ROOT / "README.md").read_text().split())
REPORTS = " ".join(" ".join((RESULTS / name).read_text().split())
                    for name in ("SOURCE_PROFILE.md", "COMPLIANCE.md", "BOTTLENECKS.md", "REWORK.md"))
RULES = pd.read_csv(RESULTS / "compliance_rules.csv").set_index("rule")
CANDIDATES = pd.read_csv(RESULTS / "automation_candidates.csv").set_index("step")
AREAS = pd.read_csv(RESULTS / "rework_by_area.csv").set_index("spend_area")
REVIEW = pd.read_csv(RESULTS / "review_list.csv")


def section(start: str, end: str) -> str:
    return README[README.index(start):README.index(end)]


TLDR = section("## TL;DR", "## Data")
RECOMMENDATIONS = section("## Recommendations, ranked", "## Limitations")


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def test_every_figure_in_the_summary_comes_from_a_report():
    """Any number with a decimal point, a thousands comma or a percent sign must appear in a report."""
    for name, text in (("TL;DR", TLDR), ("recommendations", RECOMMENDATIONS)):
        figures = set(re.findall(r"\d[\d,]*(?:\.\d+)?%?", text))
        checked = [f for f in figures if any(c in f for c in ".,%")]
        assert len(checked) >= 15, name
        missing = sorted(f for f in checked if f not in REPORTS)
        assert not missing, f"{name}: not in any report: {missing}"


def test_controls_that_held():
    controls = RULES[RULES["group"] == "control"]
    held = controls[(controls["flagged"] == 0) & (controls["check_left"] == 0)]
    assert f"{len(held)} of {len(RULES)} rules had no failures" in TLDR
    main_flow = RULES[RULES["title"].str.startswith("Main flow:")]
    assert len(main_flow) == 3 and (main_flow["share_of_items_pct"] < 1).all()
    assert "all 3 main-flow rules fail on under 1% of items" in TLDR
    no_goods = RULES.loc["cleared_no_goods"]
    assert f"({no_goods['flagged']} against {no_goods['left_to_review']})" in TLDR
    assert round(no_goods["flagged"] / no_goods["left_to_review"]) == 8
    assert "about 8 times too high" in TLDR


def test_review_list_row():
    assert f"{len(REVIEW):,} items" in RECOMMENDATIONS
    assert f"{(REVIEW['rules_failed'] >= 2).sum()} failing two or more rules" in RECOMMENDATIONS
    same = RULES.loc["sod_po_goods"]
    assert f"{same['share_of_items_pct']:.2f}% of items but {same['share_of_value_pct']:.2f}% of value" in RECOMMENDATIONS


def test_hand_work_figures():
    invoices = CANDIDATES.loc["Invoice recording"]
    phrase = f"{invoices['n_touches']:,} one-at-a-time touches by {invoices['n_accounts']} accounts, " \
             f"{pct(invoices['share_of_touches'])} of all hand work"
    assert phrase in TLDR
    assert f"{invoices['n_touches']:,} touches by {invoices['n_accounts']} accounts, " \
           f"{pct(invoices['share_of_touches'])} of hand work" in RECOMMENDATIONS
    assert f"spread over {invoices['n_vendors']:,} vendors" in RECOMMENDATIONS
    confirm = CANDIDATES.loc["Order confirmation"]
    assert f"{confirm['n_touches']:,} touches by {confirm['n_accounts']} accounts, " \
           f"{pct(confirm['top10_vendor_share'])} from 10 vendors" in RECOMMENDATIONS
    assert f"({CANDIDATES.loc['Quantity change', 'n_touches']:,} touches)" in RECOMMENDATIONS


def test_packaging_share_of_the_price_loop():
    late = 1_860
    assert f"{late:,} items" in TLDR and f"{late:,} items" in RECOMMENDATIONS
    assert f"Packaging holds {pct(AREAS.loc['Packaging', 'price_after_items'] / late)} of them" in RECOMMENDATIONS
    assert f"On {late:,} items a price was changed at or after the first recorded invoice" in REPORTS


def test_wait_breakdown_matches_the_report():
    for share, part in (("61.6%", "Implied payment term"), ("17.1%", "Waiting for a payment run"),
                        ("5.0%", "Process overrun")):
        assert share in TLDR
        assert re.search(rf"\| {re.escape(part)}[^|]* \| [\d.]+ \| {re.escape(share)} \|", REPORTS), part
    assert "mean 83.0 days" in TLDR and "a mean 83.0 days" in REPORTS
    assert "13.8% of items were unfinished" in TLDR and "13.8% of items were still unfinished" in REPORTS


def test_headline_phrases_match_the_reports():
    """Small figures can turn up in a report by chance, so the headline ones are tied to their exact source."""
    pairs = (
        (TLDR, "cleared 4.0 days later than comparable items", "| Price changed after the invoice | 953 | 92.2% | +4.0 |"),
        (TLDR, "92.2% were blocked, against 25.0% with no rework", "| No rework | 86,679 | 25.0% |"),
        (TLDR, "48.4% of blocks have no reason visible in the log, and those go with about 7 extra days",
         "| No reason visible in the log | 85,916 | 15,050 | 17.5% | 48.4% | +6.9 |"),
        (TLDR, "overstates it 28.6 times", "28.6 times too much"),
        (TLDR, "late recording is behind 38.1% of overrun days", "| Invoice recording | 7,916 | 7.5% | 20.8 | 11 | 38.1% |"),
        (TLDR, "They're behind 61.7% of overrun days", "| Payment block | 6,592 | 6.3% | 40.6 | 28 | 61.7% |"),
        (RECOMMENDATIONS, "cut the wait from 14.2 to 2.3 days, so vendors are paid 11.8 days sooner, but cash also leaves "
                          "11.6 days sooner", "cut the wait from 14.2 to 2.3 days, 11.8 days sooner for vendors. But cash "
                                              "would leave 11.6 days sooner"),
        (RECOMMENDATIONS, "0% to 40% across vendors with 100+ items", "0% at the 10th percentile to 40% at the 90th"),
        (RECOMMENDATIONS, "a median 1 day after the PO", "a median 1 day after the PO"),
        (RECOMMENDATIONS, "add no wait (+0.3 days)", "| Quantity changed | 4,720 | 31.2% | +0.3 |"),
    )
    for text, claim, source in pairs:
        assert claim in text, claim
        assert source in REPORTS, source


def test_the_ranking_is_complete():
    ranks = re.findall(r"\| (\d) \| \*\*", RECOMMENDATIONS)
    assert ranks == [str(i) for i in range(1, len(ranks) + 1)] and len(ranks) == 8
