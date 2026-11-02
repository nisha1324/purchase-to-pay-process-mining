"""The wait breakdown on a small hand-written log, with every day worked out by hand."""
import csv
from datetime import datetime, timedelta

import pandas as pd
import pytest

from p2p import db
from p2p.db import SQL_DIR, build, connect, named_queries, run
from scripts.bottlenecks import month_table, stage_table
from tests.test_rules_small import (AFTER, BLOCK, CANCEL_IR, CASE_COLUMNS, CLEAR, GR, IR, MAIN, PO,
                                    TWO_WAY, VENDOR)

PRICE = "Change Price"


def day(number: int) -> str:
    return (datetime(2018, 2, 1, 9) + timedelta(days=number)).strftime("%Y-%m-%dT%H:%M:%S")


def path(*steps) -> list:
    return [(activity, day(when) if isinstance(when, int) else when) for activity, when in steps]


# item: (flow, vendor, steps). Vendor V's term is 30 days, set by "fastest" (the rule takes rank 1 of 6).
LOG = {
    "fastest": (MAIN, "V", path((PO, 0), (VENDOR, 5), (GR, 6), (IR, 7), (CLEAR, 35))),
    "waits_for_run": (MAIN, "V", path((PO, 0), (VENDOR, 5), (GR, 6), (IR, 7), (CLEAR, 45))),
    "late_block": (MAIN, "V", path((PO, 0), (VENDOR, 5), (GR, 6), (IR, 7), (BLOCK, 50), (CLEAR, 55))),
    "late_invoice": (MAIN, "V", path((VENDOR, 0), (PO, 3), (GR, 4), (IR, 40), (CLEAR, 50))),
    "late_goods": (MAIN, "V", path((PO, 0), (VENDOR, 1), (IR, 2), (GR, 41), (CLEAR, 45))),
    "goods_after_clear": (MAIN, "V", path((PO, 0), (VENDOR, 1), (IR, 2), (CLEAR, 40), (GR, 60))),
    # A corrected item is left out, so its 2 days can't set the term.
    "corrected": (MAIN, "V", path((PO, 0), (VENDOR, 1), (IR, 2), (CLEAR, 3), (CANCEL_IR, 4))),
    # A vendor with one item has no term.
    "lone": (MAIN, "L", path((PO, 0), (VENDOR, 1), (GR, 2), (IR, 3), (CLEAR, 30))),
    # Outside the timed flows, or the cohort.
    "two_way": (TWO_WAY, "V", path((PO, 0), (VENDOR, 1), (IR, 2), (CLEAR, 3))),
    "later_po": (MAIN, "V", path(("Create Purchase Order Item", "2018-08-01T09:00:00"),
                                 (VENDOR, "2018-08-02T09:00:00"), (IR, "2018-08-03T09:00:00"),
                                 (CLEAR, "2018-08-04T09:00:00"))),
    # Unfinished: the log's last day is 48 days after this PO.
    "open": (MAIN, "V", path((PO, "2018-12-01T09:00:00"))),
    # Same-day order: a vendor date sits at 23:59 Dutch time, but the invoice came first.
    "same_day": (AFTER, "S", path((PO, "2018-02-05T09:00:00"), (VENDOR, "2018-02-05T22:59:00"),
                                  (GR, 7), (GR, 8), (IR, 9), (CLEAR, 10))),
    # A vendor date from 1948 is a typing error, so it isn't a step on the map.
    "typo_date": (MAIN, "T", path((VENDOR, "1948-01-26T22:59:00"), (PO, 0), (GR, 1), (IR, 2))),
    # Payment blocks: each row of the reason table, on main-flow items.
    "b_before": (MAIN, "B", path((PO, 0), (IR, 2), (GR, 3), (BLOCK, 4))),
    "b_no_goods": (MAIN, "B", path((PO, 0), (IR, 2), (BLOCK, 4))),
    "b_between": (MAIN, "B", path((PO, 0), (GR, 1), (IR, 2), (GR, 3), (BLOCK, 4))),
    "b_several": (MAIN, "B", path((PO, 0), (GR, 1), (IR, 2), (IR, 3), (BLOCK, 4))),
    "b_changed": (MAIN, "B", path((PO, 0), (PRICE, 1), (GR, 2), (IR, 3), (BLOCK, 4))),
    "b_several_changed": (MAIN, "B", path((PO, 0), (PRICE, 1), (GR, 1), (IR, 2), (IR, 3), (BLOCK, 4))),
    "b_plain": (MAIN, "B", path((PO, 0), (GR, 1), (IR, 2), (BLOCK, 4))),
    "b_unblocked": (MAIN, "B", path((PO, 0), (GR, 1), (IR, 2))),
    "b_no_invoice": (MAIN, "B", path((PO, 0), (GR, 1))),
    "b_after_flow": (AFTER, "B", path((PO, 0), (GR, 1), (IR, 2), (BLOCK, 4))),
}
# Vendor W: 21 items, so the term is rank 2: 40 days. One clears in 10.
for number in range(20):
    LOG[f"w_{number}"] = (MAIN, "W", path((PO, 0), (VENDOR, 10), (GR, 11), (IR, 12), (CLEAR, 50)))
LOG["w_quick"] = (MAIN, "W", path((PO, 0), (VENDOR, 10), (GR, 11), (IR, 12), (CLEAR, 20)))
# Five clearings on an early-month day, for the run tests.
for number in range(5):
    LOG[f"r_{number}"] = (MAIN, "R", path((PO, 0), (VENDOR, 1), (IR, 2), ("Clear Invoice", "2018-03-05T09:00:00")))

ACCOUNT = {PO: "user_1", GR: "user_2", IR: "user_3", CLEAR: "user_5", BLOCK: "user_4", VENDOR: "NONE"}


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    folder = tmp_path_factory.mktemp("log")
    with open(folder / "cases.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CASE_COLUMNS)
        for number, (name, (flow, vendor, _)) in enumerate(LOG.items()):
            writer.writerow([name, f"doc{number}", "00001", "Standard", "false", "true", "s", "Purchase order",
                             "c0", "NPR", "Sales", "Sub", vendor, vendor, "Standard PO", flow])
    with open(folder / "events.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "seq", "activity", "ts", "user", "net_worth_eur"])
        for name, (_, _, steps) in LOG.items():
            for seq, (activity, when) in enumerate(steps):
                writer.writerow([name, seq, activity, when, ACCOUNT.get(activity, "user_1"), 100])
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(db.PARAMETERS, "term_min", 2)
        patch.setitem(db.PARAMETERS, "run_floor", 3)
        patch.setitem(db.PARAMETERS, "main_run_floor", 5)
        connection = connect(folder / "cases.csv", folder / "events.csv")
        build(connection, "10_item_facts.sql")
        build(connection, "20_waits.sql")
        yield connection


def row(con, case_id: str) -> dict:
    frame = con.execute("select * from waits where case_id = ?", [case_id]).df()
    return frame.iloc[0].to_dict() if len(frame) else {}


def test_the_vendor_term_is_its_fastest_invoice_at_rank_one(con):
    terms = dict(con.execute("select vendor, term_days from vendor_terms").fetchall())
    assert terms["V"] == 30


def test_the_rank_rounds_up(con):
    assert dict(con.execute("select vendor, term_days from vendor_terms").fetchall())["W"] == 40


def test_a_corrected_item_can_neither_set_the_term_nor_be_timed(con):
    assert not row(con, "corrected")


@pytest.mark.parametrize("name", ["lone", "two_way", "later_po", "open"])
def test_items_outside_the_measure_are_left_out(con, name):
    assert not row(con, name)


@pytest.mark.parametrize("name,to_invoice,term,overrun,run_wait,held_by", [
    ("fastest", 5, 30, 0, 0, "On time"),
    ("waits_for_run", 5, 30, 0, 10, "On time"),
    ("late_block", 5, 30, 15, 5, "Payment block"),
    ("late_invoice", -3, 30, 10, 10, "Invoice recording"),
    ("late_goods", 1, 30, 10, 4, "Goods receipt"),
    ("goods_after_clear", 1, 30, 0, 9, "On time"),
    ("w_quick", 10, 10, 0, 0, "On time"),
])
def test_parts_of_the_wait(con, name, to_invoice, term, overrun, run_wait, held_by):
    item = row(con, name)
    assert (item["to_invoice"], item["term"], item["overrun"], item["run_wait"]) == (
        to_invoice, term, overrun, run_wait)
    assert item["held_by"] == held_by
    assert item["to_invoice"] + item["term"] + item["overrun"] + item["run_wait"] == item["total"]


@pytest.mark.parametrize("name,weekly", [
    ("waits_for_run", 0),   # free on Thursday 8 March
    ("late_invoice", 2),    # free on Tuesday 13 March, so 2 days
    ("late_block", 5),      # free on Friday 23 March: 6 days to Thursday, but it cleared in 5
])
def test_weekly_what_if(con, name, weekly):
    assert row(con, name)["run_wait_weekly"] == weekly


def test_clearing_days_are_marked_as_runs(con):
    days = {str(day): rest for day, *rest in con.execute(
        "select day::varchar, is_run, is_main_run, main_run from clearing_days").fetchall()}
    assert days["2018-03-05"] == [True, True, "early"]     # 5 items, a main run on the 5th
    assert days["2018-03-23"][1:] == [True, "late"]        # 20 items, a main run on the 23rd
    assert days["2018-03-08"] == [False, False, None]      # one item


def steps(con, case_id):
    return [r[0] for r in con.execute("select activity from map_steps where case_id = ? order by step",
                                      [case_id]).fetchall()]


def test_a_vendor_date_goes_first_on_its_day(con):
    assert steps(con, "same_day")[:2] == [VENDOR, PO]


def test_the_map_keeps_only_the_first_time_at_a_step(con):
    assert steps(con, "same_day") == [VENDOR, PO, GR, IR, CLEAR]


def test_a_typing_error_in_a_vendor_date_is_not_a_step(con):
    assert steps(con, "typo_date") == [PO, GR, IR]


def test_the_map_is_on_cohort_three_way_items_only(con):
    assert steps(con, "two_way") == []
    assert steps(con, "later_po") == []


@pytest.mark.parametrize("name,reason,blocked", [
    ("b_before", "Invoice recorded before the goods", True),
    ("b_no_goods", "Invoice recorded before the goods", True),
    ("b_between", "Invoice recorded between deliveries", True),
    ("b_several", "Several invoices on the item", True),
    ("b_changed", "PO price or quantity changed", True),
    ("b_several_changed", "Several invoices on the item", True),
    ("b_plain", "No reason visible in the log", True),
    ("b_unblocked", "No reason visible in the log", False),
])
def test_block_reasons(con, name, reason, blocked):
    found = con.execute("select reason, blocked from block_items where case_id = ?", [name]).fetchone()
    assert found == (reason, blocked)


@pytest.mark.parametrize("name", ["b_no_invoice", "b_after_flow"])
def test_block_reasons_cover_main_flow_items_with_an_invoice(con, name):
    assert con.execute("select count(*) from block_items where case_id = ?", [name]).fetchone()[0] == 0


def test_held_by_totals(con):
    held = run(con, named_queries(SQL_DIR / "bottlenecks.sql")["held_by"]).set_index("held_by")
    assert held.loc["Payment block", "overrun_days"] == 15
    assert held.loc["Invoice recording", "overrun_days"] == 10
    assert held.loc["Goods receipt", "overrun_days"] == 10


def test_durations_mark_an_open_item_as_unfinished(con):
    frame = run(con, named_queries(SQL_DIR / "bottlenecks.sql")["durations"])
    december = frame[frame["po_month"] == 12].iloc[0]
    assert pd.isna(december["po_to_clear"]) and december["open_from_po"] == 48


def test_month_and_stage_tables_use_every_item():
    frame = pd.DataFrame({"po_month": [1, 1, 1, 2], "flow": "x",
                          "po_to_clear": [10.0, 20.0, None, None],
                          "open_from_po": [100, 100, 100, 5]})
    months = month_table(frame).set_index("po_month")
    assert months.loc[1, "median_days"] == 20
    assert months.loc[1, "cleared_only_median"] == 15
    assert months.loc[2, "median_days"] is None or pd.isna(months.loc[2, "median_days"])
    for column in ("po_to_goods", "po_to_invoice_date", "invoice_date_to_recorded", "recorded_to_clear",
                   "goods_to_clear", "open_from_goods", "open_from_invoice_date", "open_from_recorded"):
        frame[column] = frame["po_to_clear"] if column.startswith(("po", "inv", "rec", "goods")) else frame["open_from_po"]
    assert stage_table(frame).set_index("stage").loc["PO to first clearing", "unfinished"] == 2
