"""Every rule on a small hand-written log, so a rule can be seen both failing and passing."""
import csv

import pytest

from p2p.db import build, connect

CASE_COLUMNS = ["case_id", "purchasing_document", "item", "item_type", "gr_based_iv", "goods_receipt",
                "source", "doc_category", "company", "spend_classification", "spend_area",
                "sub_spend_area", "vendor", "vendor_name", "document_type", "item_category"]
MAIN = "3-way match, invoice before GR"
AFTER = "3-way match, invoice after GR"
TWO_WAY = "2-way match"
PO, GR, IR, CLEAR = ("Create Purchase Order Item", "Record Goods Receipt", "Record Invoice Receipt",
                     "Clear Invoice")
BLOCK = "Remove Payment Block"
VENDOR = "Vendor creates invoice"
CANCEL_IR, CANCEL_GR = "Cancel Invoice Receipt", "Cancel Goods Receipt"
MEMO = "Vendor creates debit memo"
DELETE, REACTIVATE = "Delete Purchase Order Item", "Reactivate Purchase Order Item"
APPROVE = "Change Approval for Purchase Order"

# item: (flow, [(activity, day in Feb 2018 or a full timestamp, account)])
LOG = {
    # Clean main-flow item: fails nothing.
    "clean": (MAIN, [(VENDOR, 1, "NONE"), (PO, 2, "user_1"), (GR, 3, "user_2"), (IR, 4, "user_3"),
                     (BLOCK, 5, "user_4"), (CLEAR, 6, "user_5")]),
    # Goods cancelled, invoice stands: a real exception.
    "no_goods_real": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_2"), (CANCEL_GR, 3, "user_2"),
                             (IR, 4, "user_3"), (CLEAR, 5, "user_5")]),
    # No goods, invoice reversed: flagged, then taken out.
    "no_goods_reversed": (MAIN, [(PO, 1, "user_1"), (IR, 2, "user_3"), (CANCEL_IR, 3, "user_3"),
                                 (CLEAR, 4, "user_5")]),
    # Cleared the day before the goods arrive.
    "clear_early": (MAIN, [(PO, 1, "user_1"), (IR, 2, "user_3"), (CLEAR, 3, "user_5"), (GR, 4, "user_2")]),
    # Goods the same day as clearing, an hour after it: same day isn't "before".
    "clear_same_day": (MAIN, [(PO, 1, "user_1"), (IR, 2, "user_3"),
                              (CLEAR, "2018-02-03T09:00:00", "user_5"), (GR, "2018-02-03T10:00:00", "user_2")]),
    # 23:30 UTC on the 3rd is already the 4th in the Netherlands.
    "dutch_day": (MAIN, [(PO, 1, "user_1"), (IR, 2, "user_3"), (GR, "2018-02-03T23:30:00", "user_2"),
                         (CLEAR, "2018-02-04T08:00:00", "user_5")]),
    # Block removed before the goods.
    "block_early": (MAIN, [(PO, 1, "user_1"), (IR, 2, "user_3"), (BLOCK, 3, "user_4"), (GR, 4, "user_2"),
                           (CLEAR, 5, "user_5")]),
    # Block removed and goods never arrive.
    "block_no_goods": (MAIN, [(PO, 1, "user_1"), (IR, 2, "user_3"), (BLOCK, 3, "user_4")]),
    # Cleared before the invoice was recorded.
    "clear_before_ir": (AFTER, [(PO, 1, "user_1"), (GR, 2, "user_2"), (CLEAR, 3, "user_5"), (IR, 4, "user_3")]),
    # Deleted, then cleared.
    "deleted": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_2"), (IR, 3, "user_3"), (DELETE, 4, "user_1"),
                       (CLEAR, 5, "user_5")]),
    # Deleted, reactivated, cleared: fine.
    "reactivated": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_2"), (IR, 3, "user_3"), (DELETE, 4, "user_1"),
                           (REACTIVATE, 5, "user_1"), (CLEAR, 6, "user_5")]),
    # Two clearings, one invoice, nothing to explain it.
    "extra": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_2"), (IR, 3, "user_3"), (CLEAR, 4, "user_5"),
                     (CLEAR, 5, "user_5")]),
    # Two clearings, one invoice, and a debit memo: flagged, then taken out.
    "extra_memo": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_2"), (IR, 3, "user_3"), (MEMO, 3, "NONE"),
                          (CLEAR, 4, "user_5"), (CLEAR, 5, "user_5")]),
    # Vendor invoice 3 days older than the PO: flagged, under the 7-day line.
    "retro_short": (MAIN, [(VENDOR, 6, "NONE"), (PO, 9, "user_1"), (GR, 10, "user_2"), (IR, 11, "user_3")]),
    # Vendor invoice 8 days older.
    "retro_long": (MAIN, [(VENDOR, 1, "NONE"), (PO, 9, "user_1"), (GR, 10, "user_2"), (IR, 11, "user_3")]),
    # A vendor date from 1948 is a typing error, not a retrospective PO.
    "retro_typo": (MAIN, [(VENDOR, "1948-01-26T22:59:00", "NONE"), (PO, 9, "user_1"), (GR, 10, "user_2"),
                          (IR, 11, "user_3")]),
    # One account raises the PO and records the goods.
    "sod_goods": (MAIN, [(PO, 1, "user_9"), (GR, 2, "user_9"), (IR, 3, "user_3"), (CLEAR, 4, "user_5")]),
    # A batch account on both duties isn't a person.
    "sod_batch": (MAIN, [(PO, 1, "batch_1"), (GR, 2, "batch_1"), (IR, 3, "user_3"), (CLEAR, 4, "user_5")]),
    # One account records and clears, plain.
    "sod_clear": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_2"), (IR, 3, "user_8"), (CLEAR, 4, "user_8")]),
    # One account records and clears, on a cancelled invoice: taken out.
    "sod_clear_cancel": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_2"), (IR, 3, "user_8"), (CANCEL_IR, 4, "user_8"),
                                (IR, 5, "user_8"), (CLEAR, 6, "user_8")]),
    # 2-way approved before the invoice is recorded: passes, and older than the PO.
    "two_way_ok": (TWO_WAY, [(VENDOR, 1, "NONE"), (PO, 9, "user_6"), (APPROVE, 10, "user_7"), (IR, 11, "user_3")]),
    # 2-way approved after the invoice is recorded: fails.
    "two_way_late": (TWO_WAY, [(PO, 1, "user_6"), (IR, 2, "user_3"), (APPROVE, 3, "user_7")]),
    # A goods receipt on a flow that shouldn't have one.
    "two_way_goods": (TWO_WAY, [(PO, 1, "user_6"), (GR, 2, "user_2")]),
    # Invoice-after-GR item with its invoice first.
    "after_early": (AFTER, [(PO, 1, "user_1"), (IR, 2, "user_3"), (GR, 3, "user_2"), (CLEAR, 4, "user_5")]),
    # Invoice-after-GR item cleared with its goods cancelled.
    "after_no_goods": (AFTER, [(PO, 1, "user_1"), (GR, 2, "user_2"), (CANCEL_GR, 3, "user_2"), (IR, 4, "user_3"),
                               (CLEAR, 5, "user_5")]),
    # Separation of duties across the teams.
    "sod_po_invoice": (MAIN, [(PO, 1, "user_7"), (GR, 2, "user_2"), (IR, 3, "user_7")]),
    "sod_goods_invoice": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_7"), (IR, 3, "user_7")]),
    "sod_po_clear": (MAIN, [(PO, 1, "user_7"), (GR, 2, "user_2"), (IR, 3, "user_3"), (CLEAR, 4, "user_7")]),
    # One account records the invoice and removes its block, then the same on a bulk run.
    "sod_block": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_2"), (IR, 3, "user_4"), (BLOCK, 4, "user_4")]),
    "sod_block_bulk": (MAIN, [(PO, 1, "user_1"), (GR, 2, "user_2"), (IR, 3, "user_4"),
                              (BLOCK, "2018-02-20T09:00:00", "user_4")]),
    # Goods recorded in a bulk run by the account that raised the PO.
    "sod_goods_bulk": (MAIN, [(PO, 1, "user_9"), (GR, "2018-02-21T09:00:00", "user_9")]),
    # Consignment with an invoice.
    "consignment": ("Consignment", [(PO, 1, "user_1"), (GR, 2, "user_2"), (IR, 3, "user_3")]),
    # A cleared item outside both windows counts towards nothing.
    "too_late": (MAIN, [(PO, "2018-12-01T09:00:00", "user_1"), (IR, "2018-12-02T09:00:00", "user_3"),
                        (CLEAR, "2018-12-03T09:00:00", "user_5")]),
    # In the second window only.
    "later": (MAIN, [(PO, "2018-08-01T09:00:00", "user_1"), (IR, "2018-08-02T09:00:00", "user_3"),
                     (CLEAR, "2018-08-03T09:00:00", "user_5")]),
}


# Filler for the two bulk runs: a run needs 50 events by one account in one minute.
for number in range(49):
    LOG[f"bulk_block_{number}"] = (MAIN, [(PO, 1, "user_1"), (BLOCK, "2018-02-20T09:00:00", "user_4")])
    LOG[f"bulk_goods_{number}"] = (MAIN, [(PO, 1, "user_1"), (GR, "2018-02-21T09:00:00", "user_9")])


def stamp(when) -> str:
    return f"2018-02-{when:02d}T09:00:00" if isinstance(when, int) else when


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    folder = tmp_path_factory.mktemp("log")
    with open(folder / "cases.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CASE_COLUMNS)
        for number, (name, (flow, _)) in enumerate(LOG.items()):
            writer.writerow([name, f"doc{number}", "00001", "Standard", "false", "true", "s", "Purchase order",
                             "c0", "NPR", "Sales", "Sub", f"v{number}", f"vn{number}", "Standard PO", flow])
    with open(folder / "events.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "seq", "activity", "ts", "user", "net_worth_eur"])
        for name, (_, steps) in LOG.items():
            for seq, (activity, when, account) in enumerate(steps):
                writer.writerow([name, seq, activity, stamp(when), account, 100])
    connection = connect(folder / "cases.csv", folder / "events.csv")
    build(connection, "10_item_facts.sql")
    return connection


def hits(con, rule, column):
    rows = con.execute(f"select case_id from rule_items where rule = ? and {column} and period = 'cohort'",
                       [rule]).fetchall()
    return {row[0] for row in rows}


@pytest.mark.parametrize("rule,flagged,kept", [
    ("cleared_no_goods", {"no_goods_real", "no_goods_reversed"}, {"no_goods_real"}),
    ("cleared_before_goods", {"clear_early"}, {"clear_early"}),
    ("block_removed_before_goods", {"block_early", "block_no_goods"}, {"block_early", "block_no_goods"}),
    ("cleared_before_invoice", {"clear_before_ir"}, {"clear_before_ir"}),
    ("cleared_after_delete", {"deleted"}, {"deleted"}),
    ("extra_clearing", {"extra", "extra_memo"}, {"extra"}),
    ("retro_po_three_way", {"clean", "retro_short", "retro_long"}, {"retro_long"}),
    ("retro_po_two_way", {"two_way_ok"}, {"two_way_ok"}),
    ("two_way_approved_after_invoice", {"two_way_late"}, {"two_way_late"}),
    ("two_way_goods", {"two_way_goods"}, {"two_way_goods"}),
    ("after_invoice_before_goods", {"after_early"}, {"after_early"}),
    ("after_cleared_no_goods", {"after_no_goods"}, {"after_no_goods"}),
    ("consignment_invoiced", {"consignment"}, {"consignment"}),
    ("sod_po_goods", {"sod_goods", "sod_goods_bulk"}, {"sod_goods"}),
    ("sod_invoice_block", {"sod_block", "sod_block_bulk"}, {"sod_block"}),
    ("sod_goods_invoice", {"sod_goods_invoice"}, {"sod_goods_invoice"}),
    ("sod_po_clear", {"sod_po_clear"}, {"sod_po_clear"}),
    ("sod_invoice_clear", {"sod_clear", "sod_clear_cancel"}, {"sod_clear"}),
    ("sod_po_invoice", {"sod_po_invoice"}, {"sod_po_invoice"}),
])
def test_rule(con, rule, flagged, kept):
    real = lambda names: {name for name in names if not name.startswith("bulk_")}
    assert real(hits(con, rule, "flagged")) == flagged
    assert real(hits(con, rule, "kept")) == kept


def test_late_items_are_not_in_any_window(con):
    period = dict(con.execute("select case_id, period from facts").fetchall())
    assert period["too_late"] is None
    assert period["later"] == "check"
    assert period["clean"] == "cohort"


def test_dutch_day_rolls_over_at_midnight_local(con):
    gr_day = con.execute("select gr_day::varchar from facts where case_id = 'dutch_day'").fetchone()[0]
    assert gr_day == "2018-02-04"


def test_same_day_goods_are_not_before(con):
    assert "clear_same_day" not in hits(con, "cleared_before_goods", "flagged")


def test_a_cancelled_invoice_that_was_rerecorded_still_counts_as_standing(con):
    net = con.execute("select net_invoices from facts where case_id = 'sod_clear_cancel'").fetchone()[0]
    assert net == 1
