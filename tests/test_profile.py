"""The source profile: recounted in pandas, with none of the project's SQL."""
import subprocess
import sys

import pandas as pd
import pytest

from p2p.source import RESULTS, ROOT

REPORT = (RESULTS / "SOURCE_PROFILE.md").read_text()
VENDOR = ("Vendor creates invoice", "Vendor creates debit memo")
START, END = pd.Timestamp("2018-01-01"), pd.Timestamp("2019-01-19")


def shown(text: str) -> None:
    assert text in " ".join(REPORT.split()), f"not in the report: {text}"


@pytest.fixture(scope="module")
def po(events):
    return events[events["activity"] == "Create Purchase Order Item"].set_index("case_id")


def test_total_item_value_and_the_naive_sum(events, po):
    shown(f"{po['value'].sum() / 1e9:.3f} billion if each item is counted once")
    shown(f"{events['value'].sum() / 1e9:.2f} billion")
    shown(f"{events['value'].sum() / po['value'].sum():.1f} times too much")


def test_flow_table(cases, po):
    frame = cases.set_index("case_id").join(po[["time", "value"]])
    assert frame.groupby("item_category").size()["2-way match"] == 1044
    share = frame.groupby("item_category")["value"].sum() / frame["value"].sum()
    shown(f"| 3-way match, invoice after GR | 15,182 | 6.0% | {100 * share['3-way match, invoice after GR']:.1f}% |")


def test_clearing_on_the_follow_up_cohort(cases, events, po):
    cleared = set(events.loc[events["activity"] == "Clear Invoice", "case_id"])
    frame = cases.set_index("case_id").join(po["time"])
    cohort = frame[(frame["time"] >= "2018-01-01") & (frame["time"] < "2018-07-01")]
    for flow, group in cohort.groupby("item_category"):
        done = group.index.isin(cleared).sum()
        shown(f"{done:,} of {len(group):,} ({100 * done / len(group):.1f}%)")


def test_every_activity_marked_as_copying_really_does(events, po):
    merged = events.join(po["value"].rename("po_value"), on="case_id")
    same = (merged["value"] - merged["po_value"]).abs() < 0.005
    by_activity = same.groupby(merged["activity"]).agg(["size", "sum"])
    common = by_activity[by_activity["size"] >= 1000]
    always = (common["size"] == common["sum"]).sum()
    shown(f"{always} of the {len(common)} activities with 1,000+ events always show the same value")
    for activity in ("Vendor creates invoice", "Clear Invoice", "Change Price"):
        assert by_activity.loc[activity, "size"] == by_activity.loc[activity, "sum"]


def test_out_of_window_dates(events):
    outside = events[(events["time"] < START) | (events["time"] >= END)]
    vendor = outside["activity"].isin(VENDOR).sum()
    other = outside[~outside["activity"].isin(VENDOR)]
    assert set(other["activity"]) == {"Create Purchase Requisition Item"}
    assert set(other["time"].dt.year) == {2017}
    shown(f"{len(outside)} events on {outside['case_id'].nunique()} items fall outside")
    shown(f"{vendor} of them are")
    shown(f"the other {len(other)} are requisitions from 2017")
    shown(f"the {(events['time'] < '2017-01-01').sum()} before 2017")


def test_vendor_dates_follow_dutch_summer_time_exactly(events):
    vendor = events[events["activity"].isin(VENDOR)
                    & (events["time"] >= START) & (events["time"] < END)]
    clock = vendor["time"].dt.strftime("%H:%M")
    assert set(clock) == {"21:59", "22:59"}
    summer = (vendor["time"] >= "2018-03-25") & (vendor["time"] < "2018-10-28")
    assert (clock[summer] == "21:59").all()
    assert (clock[~summer] == "22:59").all()


def test_clearing_with_no_account_is_date_only_too(events):
    rows = events[(events["activity"] == "Clear Invoice") & (events["user"] == "NONE")]
    assert rows["time"].dt.strftime("%H:%M").isin(["21:59", "22:59"]).all()
    shown(f"The {len(rows):,} `Clear Invoice` events with no account")


def test_the_requisition_break(events):
    rows = events[events["activity"] == "Create Purchase Requisition Item"]
    rows = rows[rows["time"].dt.year == 2018]
    monthly = rows.groupby(rows["time"].dt.month).size()
    assert monthly.loc[1:8].max() < 300 < 10_000 < monthly.loc[9:11].min()
    shown(f"{monthly.loc[9:].sum() / monthly.sum() * 100:.1f}% of the year's requisitions")


def test_cleared_share_by_po_month(cases, events, po):
    cleared = set(events.loc[events["activity"] == "Clear Invoice", "case_id"])
    three_way = cases.loc[cases["item_category"].str.startswith("3-way"), "case_id"]
    times = po.loc[three_way, "time"]
    times = times[times < "2019-01-01"]
    groups = times.groupby(times.dt.month)
    done = groups.apply(lambda group: group.index.isin(cleared).sum())
    for month, name in ((1, "Jan"), (6, "Jun"), (10, "Oct"), (12, "Dec")):
        size = groups.size()[month]
        shown(f"| {name} 2018 | {size:,} | {done[month]:,} | {100 * done[month] / size:.1f}% |")


def test_time_to_first_clearing(cases, events, po):
    first = events[events["activity"] == "Clear Invoice"].groupby("case_id")["time"].min()
    three_way = cases.loc[cases["item_category"].str.startswith("3-way"), "case_id"]
    start = po.loc[three_way, "time"]
    start = start[(start >= "2018-01-01") & (start < "2018-07-01")]
    days = (first.reindex(start.index) - start).dropna().dt.total_seconds() / 86400
    shown(f"is {days.median():.1f} days, and 10% take over {days.quantile(0.9):.1f} days")


def test_clearing_is_mostly_bulk_and_mostly_one_account(events):
    rows = events[events["activity"] == "Clear Invoice"]
    named = rows[rows["user"].str.startswith("user")]
    sizes = named.groupby(["user", "ts"])["seq"].transform("size")
    shown(f"| 0.0% | 4.1% | {100 * (sizes >= 50).mean():.1f}% |")
    top = rows["user"].value_counts()
    shown(f"`{top.index[0]}` alone clears {100 * top.iloc[0] / len(rows):.1f}% of all invoices")


def test_path_counts(events):
    ordered = events.sort_values(["case_id", "seq"])
    paths = ordered.groupby("case_id", sort=False)["activity"].agg(" > ".join)
    sizes = paths.value_counts()
    shown(f"{len(sizes):,} paths as logged")
    shown(f"{(sizes == 1).sum():,} of them were followed by only one item")
    shown(f"The top 10 cover {100 * sizes.iloc[:10].sum() / sizes.sum():.1f}% of items")
    shown(f"the top 100 {100 * sizes.iloc[:100].sum() / sizes.sum():.1f}%")


def test_collapsing_repeats_on_a_recount(events):
    ordered = events.sort_values(["case_id", "ts", "activity", "seq"], kind="stable")
    changed = (ordered["activity"] != ordered["activity"].shift()) | \
              (ordered["case_id"] != ordered["case_id"].shift())
    paths = ordered[changed].groupby("case_id", sort=False)["activity"].agg(" > ".join)
    shown(f"| Also collapsing immediate repeats | {paths.nunique():,} |")


def test_report_and_charts_arent_stale(tmp_path):
    subprocess.run([sys.executable, "-m", "scripts.profile_log", "--out", str(tmp_path)],
                   cwd=ROOT, check=True)
    assert (tmp_path / "SOURCE_PROFILE.md").read_text() == REPORT
    names = ("01_requisitions_by_month.png", "02_clearing_by_po_month.png", "03_path_coverage.png")
    for name in names:
        fresh = (tmp_path / "charts" / name).read_bytes()
        assert fresh == (RESULTS / "charts" / name).read_bytes(), name
