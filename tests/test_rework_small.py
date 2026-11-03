"""The rework tables on a small hand-written log, with every answer worked out by hand."""
import csv
from datetime import datetime, timedelta

import pytest

from p2p import db
from p2p.db import SQL_DIR, build, connect, named_queries, run
from tests.test_rules_small import (AFTER, CANCEL_GR, CANCEL_IR, CASE_COLUMNS, CLEAR, DELETE, GR, IR,
                                    MAIN, MEMO, PO, TWO_WAY, BLOCK)

PRICE, QUANTITY = "Change Price", "Change Quantity"
CONSIGNMENT = "Consignment"


def at(day: int, minute: int = 0) -> str:
    return (datetime(2018, 2, 1, 9, minute) + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%S")


# item: (flow, document, [(activity, time, account)])
LOG = {
    # Two lines of one PO entered in the same minute: one touch. Cleared in a bulk run with a3.
    "a1": (MAIN, "D1", [(PO, at(0), "user_1"), (GR, at(1), "batch_1"), (IR, at(2), "user_3"),
                        (CLEAR, at(9), "user_5")]),
    "a2": (MAIN, "D1", [(PO, at(0), "user_1"), (GR, at(1), "batch_1"), (IR, at(2), "user_3"),
                        (CLEAR, at(9), "user_5")]),
    # Price changed after the invoice, and the block comes off the same day.
    "a3": (MAIN, "D2", [(PO, at(0, 5), "user_1"), (GR, at(1), "user_2"), (IR, at(2), "user_3"),
                        (PRICE, at(5), "user_6"), (BLOCK, at(5, 30), "user_4"), (CLEAR, at(9), "user_5")]),
    # 22:30 and 23:30 UTC in February are 23:30 and 00:30 in the Netherlands: the block comes off the next day.
    "loop_midnight": (MAIN, "D16", [(PO, at(0), "user_1"), (IR, at(2), "user_3"),
                                    (PRICE, "2018-02-10T22:30:00", "user_6"), (BLOCK, "2018-02-10T23:30:00", "user_4")]),
    # Price changed before the invoice: a change, but not the loop.
    "p_before": (MAIN, "D3", [(PO, at(0), "user_1"), (PRICE, at(1), "user_6"), (IR, at(3), "user_3")]),
    # Same minute as the invoice counts as at or after it.
    "p_same_minute": (MAIN, "D4", [(PO, at(0), "user_1"), (IR, at(2), "user_3"), (PRICE, at(2), "user_6")]),
    # Quantity changed after the invoice, with no block.
    "q_after": (MAIN, "D5", [(PO, at(0), "user_1"), (IR, at(2), "user_3"), (QUANTITY, at(4), "user_6")]),
    # Deleted two days after the PO, by the buyer who raised it, with nothing received or invoiced.
    "deleted": (MAIN, "D6", [(PO, at(0), "user_1"), (DELETE, at(2), "user_1")]),
    # Deleted after goods arrived: not unused.
    "deleted_used": (MAIN, "D7", [(PO, at(0), "user_1"), (GR, at(1), "user_2"), (DELETE, at(3), "user_7")]),
    # Deleted after an invoice but with no goods: not unused either.
    "deleted_invoiced": (MAIN, "D17", [(PO, at(0), "user_1"), (IR, at(1), "user_3"), (DELETE, at(3), "user_7")]),
    "reentered": (MAIN, "D8", [(PO, at(0), "user_1"), (IR, at(2), "user_3"), (CANCEL_IR, at(3), "user_3"),
                               (IR, at(4), "user_3")]),
    "cancel_only": (MAIN, "D9", [(PO, at(0), "user_1"), (IR, at(2), "user_3"), (CANCEL_IR, at(3), "user_3")]),
    "goods_redo": (AFTER, "D10", [(PO, at(0), "user_1"), (GR, at(1), "user_2"), (CANCEL_GR, at(2), "user_2"),
                                  (GR, at(3), "user_2")]),
    "memo": (MAIN, "D11", [(PO, at(0), "user_1"), (MEMO, at(3), "NONE")]),
    # A payment block is a control, not rework.
    "blocked_only": (MAIN, "D18", [(PO, at(0), "user_1"), (IR, at(1), "user_3"), (BLOCK, at(4), "user_4")]),
    "clean": (AFTER, "D12", [(PO, at(0), "user_1"), (GR, at(1), "user_2"), (IR, at(2), "user_3")]),
    # Outside the scope: other flows, or created after the cohort.
    "two_way": (TWO_WAY, "D13", [(PO, at(0), "user_1"), (PRICE, at(1), "user_6")]),
    "consigned": (CONSIGNMENT, "D14", [(PO, at(0), "user_1"), (QUANTITY, at(1), "user_6")]),
    "later": (MAIN, "D15", [(PO, "2018-08-01T09:00:00", "user_1"), (PRICE, "2018-08-02T09:00:00", "user_6")]),
}
IN_SCOPE = [name for name in LOG if name not in ("two_way", "consigned", "later")]


def shifted(name: str, number: int, activity: str, when: str) -> str:
    """Each item works in its own minute, so only the bulk run and D1's two lines share one."""
    if name == "a2" or (name in ("a1", "a3") and activity == CLEAR):
        return when
    moved = datetime.strptime(when, "%Y-%m-%dT%H:%M:%S") + timedelta(minutes=number)
    return moved.strftime("%Y-%m-%dT%H:%M:%S")


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    folder = tmp_path_factory.mktemp("log")
    with open(folder / "cases.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CASE_COLUMNS)
        for name, (flow, document, _) in LOG.items():
            # D1's two lines sit in different spend areas, so a touch on D1 has to pick one.
            area = {"a1": "Packaging", "a2": "Additives"}.get(name, "Sales")
            writer.writerow([name, document, "00001", "Standard", "false", "true", "s", "Purchase order",
                             "c0", "NPR", area, "Sub", "V", "V", "Standard PO", flow])
    with open(folder / "events.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "seq", "activity", "ts", "user", "net_worth_eur"])
        for number, (name, (_, _, steps)) in enumerate(LOG.items()):
            for seq, (activity, when, account) in enumerate(steps):
                writer.writerow([name, seq, activity, shifted(name, number, activity, when), account, 100])
    with pytest.MonkeyPatch.context() as patch:
        # Three of the same thing in one minute is a bulk run here.
        patch.setitem(db.PARAMETERS, "burst_min", 3)
        connection = connect(folder / "cases.csv", folder / "events.csv")
        build(connection, "10_item_facts.sql")
        build(connection, "30_rework.sql")
        yield connection


def item(con, name: str) -> dict:
    frame = con.execute("select * from item_rework where case_id = ?", [name]).df()
    assert len(frame) == 1, name
    return frame.iloc[0].to_dict()


def query(con, name: str):
    return run(con, named_queries(SQL_DIR / "rework.sql")[name])


def test_only_three_way_cohort_items_are_in_scope(con):
    names = {row[0] for row in con.execute("select case_id from item_rework").fetchall()}
    assert names == set(IN_SCOPE)
    events = {row[0] for row in con.execute("select distinct case_id from work_events").fetchall()}
    assert events == set(IN_SCOPE)


@pytest.mark.parametrize("name,activity,mode", [
    ("a1", GR, "System"),
    ("a1", CLEAR, "Bulk"),
    ("a3", CLEAR, "Bulk"),
    ("a1", IR, "One at a time"),
    ("a1", PO, "One at a time"),
    ("memo", MEMO, "Vendor or none"),
])
def test_how_each_event_was_done(con, name, activity, mode):
    found = con.execute("select mode from work_events where case_id = ? and activity = ?",
                        [name, activity]).fetchall()
    assert [row[0] for row in found] == [mode]


def test_two_lines_of_one_document_in_one_minute_are_one_touch(con):
    rows = con.execute("""select n_lines from touches
                          where activity = ? and purchasing_document = 'D1'""", [PO]).fetchall()
    assert rows == [(2,)]


def test_bulk_and_system_events_take_no_touches(con):
    assert con.execute("select count(*) from touches where activity in (?, ?)", [CLEAR, MEMO]).fetchone()[0] == 0
    assert con.execute("""select count(*) from touches
                          where activity = ? and purchasing_document = 'D1'""", [GR]).fetchone()[0] == 0


def test_a_touch_takes_the_spend_area_of_its_lowest_line(con):
    assert con.execute("select spend_area from touches where purchasing_document = 'D1' and activity = ?",
                       [PO]).fetchone()[0] == "Packaging"


@pytest.mark.parametrize("name,flags", [
    ("a3", {"price_change": True, "price_after_invoice": True, "any_rework": True}),
    ("p_before", {"price_change": True, "price_after_invoice": False}),
    ("p_same_minute", {"price_change": True, "price_after_invoice": True}),
    ("q_after", {"quantity_change": True, "quantity_after_invoice": True, "price_change": False}),
    ("deleted", {"deletion": True, "any_rework": True}),
    ("reentered", {"invoice_reversal": True, "invoice_cancelled": True, "invoice_reentered": True}),
    ("cancel_only", {"invoice_cancelled": True, "invoice_reentered": False}),
    ("goods_redo", {"goods_reversal": True, "goods_cancelled": True, "goods_reentered": True}),
    ("memo", {"debit_memo": True, "any_rework": True}),
    ("a1", {"any_rework": False, "price_change": False, "invoice_cancelled": False}),
    ("clean", {"any_rework": False, "goods_cancelled": False}),
    ("blocked_only", {"any_rework": False}),
])
def test_rework_flags(con, name, flags):
    row = item(con, name)
    for flag, expected in flags.items():
        assert bool(row[flag]) is expected, flag


def test_the_late_price_change_is_matched_to_the_block_after_it(con):
    loop = query(con, "price_loop").iloc[0]
    assert (loop["n_items"], loop["block_after"], loop["any_block"], loop["same_day"], loop["within_week"]) \
        == (3, 2, 2, 1, 2)


def test_deletions(con):
    deleted = query(con, "deletions").iloc[0]
    assert (deleted["n_deleted"], deleted["unused"], deleted["median_days"], deleted["within_week"]) == (3, 1, 2, 1)
    creator = query(con, "deleted_by_creator").iloc[0]
    assert (creator["n_items"], creator["same_account"]) == (1, 1)


def test_reentry(con):
    found = query(con, "reentry").set_index("what")
    assert tuple(found.loc["Invoice"]) == (2, 1)
    assert tuple(found.loc["Goods"]) == (1, 1)


def test_rework_rates_count_items_not_events(con):
    rates = query(con, "rework_rates").set_index("step")
    assert rates.loc["Price change", "n_items"] == 4
    assert rates.loc["Invoice reversal", "n_items"] == 2
    assert rates.loc["Any rework", "n_items"] == 12
    assert rates.loc["Deletion or block", "n_items"] == 3
    assert rates.loc["Any rework", "of_items"] == len(IN_SCOPE)


def test_change_timing(con):
    timing = query(con, "change_timing").set_index("activity")
    assert tuple(timing.loc[PRICE, ["before_invoice", "after_invoice", "no_invoice"]]) == (1, 3, 0)
    assert tuple(timing.loc[QUANTITY, ["before_invoice", "after_invoice", "no_invoice"]]) == (0, 1, 0)


def test_touch_counts_by_step(con):
    modes = query(con, "modes").set_index("step")
    # 16 POs on 15 documents: D1's two lines are one touch.
    assert modes.loc["PO creation", "n_touches"] == 15
    assert modes.loc["Clearing", "n_touches"] == 0
    assert modes.loc["Clearing", "bulk_events"] == 3
    assert modes.loc["Vendor debit memo", "vendor_events"] == 1
