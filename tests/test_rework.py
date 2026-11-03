"""The rework report: recounted in pandas, with none of the project's SQL or report code."""
import subprocess
import sys

import pandas as pd
import pytest

from p2p.source import RESULTS, ROOT

REPORT = " ".join((RESULTS / "REWORK.md").read_text().split())
CANDIDATES = pd.read_csv(RESULTS / "automation_candidates.csv").set_index("step")
AREAS = pd.read_csv(RESULTS / "rework_by_area.csv").set_index("spend_area")
MAIN = "3-way match, invoice before GR"
AFTER = "3-way match, invoice after GR"
PO, GR, IR, BLOCK = ("Create Purchase Order Item", "Record Goods Receipt", "Record Invoice Receipt",
                     "Remove Payment Block")
REWORK = {
    "Change Price": "Price change", "Change Quantity": "Quantity change",
    "Change Delivery Indicator": "Other PO change", "Change Storage Location": "Other PO change",
    "Change Currency": "Other PO change", "Change payment term": "Other PO change",
    "Change Final Invoice Indicator": "Other PO change", "Change Rejection Indicator": "Other PO change",
    "Update Order Confirmation": "Other PO change",
    "Delete Purchase Order Item": "Deletion or block", "SRM: Deleted": "Deletion or block",
    "Block Purchase Order Item": "Deletion or block", "Reactivate Purchase Order Item": "Deletion or block",
    "Cancel Invoice Receipt": "Invoice reversal", "Cancel Subsequent Invoice": "Invoice reversal",
    "Cancel Goods Receipt": "Goods reversal", "Vendor creates debit memo": "Vendor debit memo",
}


def shown(text: str) -> None:
    assert text in REPORT, f"not in the report: {text}"


@pytest.fixture(scope="module")
def scoped(events, cases):
    """Events on 3-way items created in the cohort, with each event's Dutch day."""
    po = events[events["activity"] == PO].set_index("case_id")
    info = cases.set_index("case_id")
    keep = info.index[info["item_category"].isin([MAIN, AFTER])
                      & (po["time"].reindex(info.index) >= "2018-01-01")
                      & (po["time"].reindex(info.index) < "2018-07-01")]
    frame = events[events["case_id"].isin(keep)].copy()
    local = frame["time"].dt.tz_localize("UTC").dt.tz_convert("Europe/Amsterdam")
    frame["day"] = local.dt.tz_localize(None).dt.normalize()
    frame["document"] = info["purchasing_document"].reindex(frame["case_id"]).to_numpy()
    frame["vendor"] = info["vendor"].reindex(frame["case_id"]).to_numpy()
    frame["po_value"] = po["value"].reindex(frame["case_id"]).to_numpy()
    return frame


@pytest.fixture(scope="module")
def one_at_a_time(events, scoped):
    """Named-account events outside a burst, where a burst is counted on the whole log."""
    named = events[events["user"].str.startswith("user")]
    size = named.groupby(["activity", "user", "ts"])["case_id"].transform("size")
    bursts = set(map(tuple, named.loc[size >= 50, ["activity", "user", "ts"]].drop_duplicates().to_numpy()))
    chosen = scoped[scoped["user"].str.startswith("user")]
    keys = list(zip(chosen["activity"], chosen["user"], chosen["ts"]))
    return chosen[[key not in bursts for key in keys]]


@pytest.fixture(scope="module")
def items(scoped):
    frame = scoped.groupby("case_id").agg(po_value=("po_value", "first"))
    first_ir = scoped[scoped["activity"] == IR].groupby("case_id")["time"].min()
    frame["first_ir"] = first_ir
    rework = scoped[scoped["activity"].isin(REWORK)]
    frame["any_rework"] = frame.index.isin(rework["case_id"])
    price = scoped[scoped["activity"] == "Change Price"]
    late = price[price["time"] >= first_ir.reindex(price["case_id"]).to_numpy()]
    frame["late_price"] = late.groupby("case_id")["time"].min()
    blocks = scoped[scoped["activity"] == BLOCK]
    frame["n_block"] = blocks.groupby("case_id").size().reindex(frame.index, fill_value=0)
    return frame


def test_scope_and_rework_rate(items):
    assert len(items) == 126_440
    shown(f"**{100 * items['any_rework'].mean():.1f}% of items have some rework** "
          f"({items['any_rework'].sum():,} of {len(items):,})")
    value = items.loc[items["any_rework"], "po_value"].sum() / items["po_value"].sum()
    shown(f"holding {100 * value:.1f}% of value")


def test_touches_by_step(one_at_a_time):
    touches = one_at_a_time.drop_duplicates(["activity", "user", "document", "ts"])
    for step, activities in (("Goods receipt", [GR]), ("PO creation", [PO]),
                             ("Invoice recording", [IR, "Record Subsequent Invoice"]),
                             ("Quantity change", ["Change Quantity"]), ("Price change", ["Change Price"])):
        assert (touches["activity"].isin(activities)).sum() == CANDIDATES.loc[step, "n_touches"], step
    rework = touches["activity"].isin(REWORK).sum()
    total = len(touches)
    assert CANDIDATES["n_touches"].sum() == total
    shown(f"There are {total:,} one-at-a-time touches in six months")
    shown(f"{100 * rework / total:.1f}% on rework")
    invoice = touches[touches["activity"].isin([IR, "Record Subsequent Invoice"])]
    assert invoice["user"].nunique() == CANDIDATES.loc["Invoice recording", "n_accounts"]
    top5 = invoice["user"].value_counts().iloc[:5].sum() / len(invoice)
    shown(f"5 of them make {100 * top5:.1f}%")


def test_top_vendor_shares(one_at_a_time):
    touches = one_at_a_time.drop_duplicates(["activity", "user", "document", "ts"])
    confirmations = touches[touches["activity"] == "Receive Order Confirmation"]
    share = confirmations["vendor"].value_counts().iloc[:10].sum() / len(confirmations)
    assert round(share, 4) == CANDIDATES.loc["Order confirmation", "top10_vendor_share"]
    rework = touches[touches["activity"].isin(REWORK)]
    top10 = rework["vendor"].value_counts().iloc[:10].sum() / len(rework)
    shown(f"The 10 vendors with the most rework touches hold {100 * top10:.1f}% of them")


def test_the_price_loop(items, scoped):
    late = items[items["late_price"].notna()]
    blocks = scoped[scoped["activity"] == BLOCK]
    after = blocks[blocks["time"] >= late["late_price"].reindex(blocks["case_id"]).to_numpy()]
    first_after = after.sort_values(["time", "seq"]).groupby("case_id").first()
    price = scoped[scoped["activity"] == "Change Price"].set_index(["case_id", "time"])["day"]
    price_day = price[~price.index.duplicated()].reindex(list(zip(late.index, late["late_price"])))
    price_day.index = late.index
    gap = (first_after["day"] - price_day.reindex(first_after.index)).dt.days
    shown(f"On {len(late):,} items a price was changed at or after the first recorded invoice")
    shown(f"On {len(first_after):,} of them ({100 * len(first_after) / len(late):.1f}%) a payment block came off")
    shown(f"{100 * (gap == 0).mean():.1f}% of those on the same day and {100 * (gap <= 7).mean():.1f}% within a week")
    shown(f"{100 * (late['n_block'] > 0).mean():.1f}% have a block removed")
    shown(f"These items are {100 * len(late) / len(items):.1f}% of items but "
          f"{100 * late['po_value'].sum() / items['po_value'].sum():.1f}% of value")


def test_deletions(scoped):
    by_item = scoped.groupby("case_id")
    deleted_day = scoped[scoped["activity"].isin(["Delete Purchase Order Item", "SRM: Deleted"])] \
        .groupby("case_id")["day"].min()
    po_day = scoped[scoped["activity"] == PO].groupby("case_id")["day"].min()
    used = by_item["activity"].apply(lambda a: bool(set(a) & {GR, IR}))
    unused = deleted_day[~used.reindex(deleted_day.index)]
    waited = (unused - po_day.reindex(unused.index)).dt.days
    shown(f"{len(deleted_day):,} items were deleted, and {len(unused):,} of them "
          f"({100 * len(unused) / len(deleted_day):.1f}%) had no goods and no invoice")
    shown(f"a median {waited.median():.0f} day after the PO, {100 * (waited <= 7).mean():.1f}% within a week")


@pytest.mark.parametrize("cancel,record,label", [
    ("Cancel Invoice Receipt", IR, "items had an invoice cancelled"),
    ("Cancel Goods Receipt", GR, "had a goods receipt cancelled"),
])
def test_reentry(scoped, cancel, record, label):
    first_cancel = scoped[scoped["activity"] == cancel].groupby("case_id")["time"].min()
    later = scoped[(scoped["activity"] == record) & scoped["case_id"].isin(first_cancel.index)]
    later = later[later["time"] > first_cancel.reindex(later["case_id"]).to_numpy()]
    again = later["case_id"].nunique()
    shown(f"{len(first_cancel):,} {label}, and {again:,} ({100 * again / len(first_cancel):.1f}%)")


def test_area_table_adds_up():
    assert AREAS["n_items"].sum() <= 126_440
    assert (AREAS["rework_items"] <= AREAS["n_items"]).all()
    packaging = AREAS.loc["Packaging"]
    assert packaging["rework_rate"] == round(packaging["rework_items"] / packaging["n_items"], 4)


def test_readme_part_4_matches_the_report():
    readme = " ".join((ROOT / "README.md").read_text().split())
    for phrase in ("18.9% of items", "47.2% of value", "188,465", "64.6%", "1,860 items", "14.6% of value",
                   "92.2%", "25.0%", "4,448", "2,621", "58,364", "25 accounts", "75.7%", "38.1% of overrun days"):
        assert phrase in readme, phrase
        assert phrase in REPORT, phrase


def test_report_and_tables_match_a_fresh_run(tmp_path):
    subprocess.run([sys.executable, "-m", "scripts.rework", "--out", str(tmp_path)], cwd=ROOT, check=True)
    for name in ("REWORK.md", "automation_candidates.csv", "rework_by_area.csv",
                 "charts/10_hand_work_by_step.png", "charts/11_rework_delay.png"):
        assert (tmp_path / name).read_bytes() == (RESULTS / name).read_bytes(), name
