"""The bottleneck report: recounted in pandas, with none of the project's SQL or report code."""
import math
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from p2p.source import RESULTS, ROOT

REPORT = " ".join((RESULTS / "BOTTLENECKS.md").read_text().split())
STAGES = pd.read_csv(RESULTS / "wait_stages.csv").set_index("stage")
CALENDAR = pd.read_csv(RESULTS / "run_calendar.csv")
TERMS = pd.read_csv(RESULTS / "vendor_terms.csv").set_index("vendor")["term_days"]
MAIN = "3-way match, invoice before GR"
AFTER = "3-way match, invoice after GR"
PO, GR, IR, CLEAR, BLOCK, VENDOR = ("Create Purchase Order Item", "Record Goods Receipt", "Record Invoice Receipt",
                                    "Clear Invoice", "Remove Payment Block", "Vendor creates invoice")
LOG_END = pd.Timestamp("2019-01-18")


def shown(text: str) -> None:
    assert text in REPORT, f"not in the report: {text}"


def row_shown(*cells) -> None:
    shown("| " + " | ".join(str(cell) for cell in cells) + " |")


@pytest.fixture(scope="module")
def days(events):
    local = events["time"].dt.tz_localize("UTC").dt.tz_convert("Europe/Amsterdam")
    return local.dt.tz_localize(None).dt.normalize()


@pytest.fixture(scope="module")
def items(events, cases, days):
    """One row per item, built from the raw columns with pandas only."""
    frame = cases.set_index("case_id")
    frame["flow"] = frame["item_category"]

    def first(activity, keep=None):
        chosen = events["activity"] == activity
        if keep is not None:
            chosen &= keep
        return days[chosen].groupby(events.loc[chosen, "case_id"]).min().reindex(frame.index)

    def count(*activities):
        hits = events.loc[events["activity"].isin(activities), "case_id"].value_counts()
        return hits.reindex(frame.index, fill_value=0)

    po = events[events["activity"] == PO].set_index("case_id")
    frame["po_time"] = po["time"]
    frame["po_value"] = po["value"]
    frame["po"], frame["gr"], frame["ir"], frame["clear"] = first(PO), first(GR), first(IR), first(CLEAR)
    frame["invoice"] = first(VENDOR, events["time"] >= "2017-01-01")
    frame["corrected"] = count("Cancel Invoice Receipt", "Cancel Subsequent Invoice",
                               "Vendor creates debit memo") > 0
    frame["n_clear"], frame["n_ir"] = count(CLEAR), count(IR)
    frame["n_block"] = count(BLOCK)
    return frame


@pytest.fixture(scope="module")
def cohort(items):
    return items[(items["po_time"] >= "2018-01-01") & (items["po_time"] < "2018-07-01")
                 & items["flow"].isin([MAIN, AFTER])]


@pytest.fixture(scope="module")
def timed(cohort, events, days):
    """Cohort items the breakdown can use, with the last step on or before first clearing."""
    frame = cohort[(cohort["n_clear"] > 0) & cohort["invoice"].notna() & ~cohort["corrected"]
                   & (cohort["clear"] >= cohort["invoice"])].copy()
    frame["days"] = (frame["clear"] - frame["invoice"]).dt.days
    steps = events.assign(day=days)[events["activity"].isin([GR, IR, BLOCK])]
    steps = steps[steps["case_id"].isin(frame.index)]
    steps = steps[steps["day"] <= frame["clear"].reindex(steps["case_id"]).to_numpy()]
    last = steps.pivot_table(index="case_id", columns="activity", values="day", aggfunc="max")
    for name, activity in (("last_goods", GR), ("last_invoice", IR), ("last_block", BLOCK)):
        frame[name] = last[activity].reindex(frame.index)
    frame["ready"] = frame[["last_goods", "last_invoice", "last_block"]].max(axis=1).fillna(frame["invoice"])
    return frame


def term_at_rank(values, share=0.05) -> int:
    ordered = np.sort(values)
    return int(ordered[math.ceil(len(ordered) * share) - 1])


@pytest.fixture(scope="module")
def waits(timed):
    groups = timed.groupby("vendor")["days"]
    terms = groups.apply(lambda s: term_at_rank(s.to_numpy()))[groups.size() >= 30]
    frame = timed[timed["vendor"].isin(terms.index)].copy()
    frame["term_days"] = frame["vendor"].map(terms)
    frame["due"] = (frame["invoice"] + pd.to_timedelta(frame["term_days"], unit="D")).where(
        frame["days"] >= frame["term_days"], frame["clear"])
    frame["free"] = frame[["due", "ready"]].max(axis=1)
    frame["to_invoice"] = (frame["invoice"] - frame["po"]).dt.days
    frame["term"] = (frame["due"] - frame["invoice"]).dt.days
    frame["overrun"] = (frame["free"] - frame["due"]).dt.days
    frame["run_wait"] = (frame["clear"] - frame["free"]).dt.days
    frame["total"] = (frame["clear"] - frame["po"]).dt.days
    return frame


def test_every_vendor_term_matches_a_recount(timed, waits):
    recount = waits.groupby("vendor")["term_days"].first().astype(int).sort_index()
    pd.testing.assert_series_equal(recount, TERMS.sort_index(), check_names=False)


def test_the_four_parts_add_up_item_by_item(waits):
    assert (waits["to_invoice"] + waits["term"] + waits["overrun"] + waits["run_wait"] == waits["total"]).all()


def test_no_part_after_the_invoice_is_negative(waits):
    assert (waits[["term", "overrun", "run_wait"]] >= 0).all().all()


def test_breakdown_recount(waits):
    def line(label, part):
        row_shown(label, f"{len(part):,}", f"{part['total'].mean():.1f}", f"{part['to_invoice'].mean():.1f}",
                  f"{part['term'].mean():.1f}", f"{part['overrun'].mean():.1f}", f"{part['run_wait'].mean():.1f}")

    line("All 3-way items", waits)
    line("Main flow", waits[waits["flow"] == MAIN])
    line("Invoice-after-GR flow", waits[waits["flow"] == AFTER])


def test_value_weighted_row_recount(waits):
    weighted = [np.average(waits[name], weights=waits["po_value"])
                for name in ("total", "to_invoice", "term", "overrun", "run_wait")]
    row_shown("Weighted by value", f"{len(waits):,}", *[f"{value:.1f}" for value in weighted])


def test_held_by_recount(waits):
    late = waits[waits["ready"] > waits["due"]]
    block = late[late["ready"] == late["last_block"]]
    invoice = late[(late["ready"] != late["last_block"]) & (late["ready"] == late["last_invoice"])]
    goods = late.drop(block.index).drop(invoice.index)
    shown(f"{len(late):,} items ({100 * len(late) / len(waits):.1f}%) weren't finished")
    total = late["overrun"].sum()
    for name, part in (("Payment block", block), ("Invoice recording", invoice), ("Goods receipt", goods)):
        row_shown(name, f"{len(part):,}", f"{100 * len(part) / len(waits):.1f}%", f"{part['overrun'].mean():.1f}",
                  f"{part['overrun'].median():.0f}", f"{100 * part['overrun'].sum() / total:.1f}%")


def test_weekly_what_if_recount(waits):
    to_thursday = (3 - waits["free"].dt.dayofweek) % 7  # Monday = 0 here
    weekly = np.minimum(waits["run_wait"], to_thursday)
    shown(f"cut the wait from {waits['run_wait'].mean():.1f} to {weekly.mean():.1f} days")
    shown(f"{waits['run_wait'].mean() - weekly.mean():.1f} days sooner for vendors")
    weights = waits["po_value"]
    saved = np.average(waits["run_wait"], weights=weights) - np.average(weekly, weights=weights)
    shown(f"cash would leave {saved:.1f} days sooner")


def test_terms_set_on_early_invoices_hold_on_later_ones(timed):
    frame = timed[(timed["invoice"] >= "2018-01-01") & (timed["invoice"] < "2018-07-01")]
    early = frame[frame["invoice"] < "2018-04-01"]
    late = frame[frame["invoice"] >= "2018-04-01"]
    def terms(part):
        sizes = part.groupby("vendor").size()
        chosen = part[part["vendor"].isin(sizes[sizes >= 30].index)]
        return chosen.groupby("vendor")["days"].apply(lambda s: term_at_rank(s.to_numpy()))
    first, second = terms(early), terms(late)
    both = first.index.intersection(second.index)
    change = (first[both] - second[both]).abs()
    later = late[late["vendor"].isin(both)]
    beat = (later["days"] < later["vendor"].map(first)).mean()
    shown(f"{len(both)} vendors can be compared")
    shown(f"The median change is {change.median():.0f} days")
    shown(f"{(change <= 7).sum()} vendors ({100 * (change <= 7).mean():.1f}%) move a week or less")
    shown(f"Only {100 * beat:.1f}% of later invoices beat the earlier term")
    assert 0.03 < beat < 0.07


def test_run_calendar_recount(events, days):
    clearing = days[events["activity"] == CLEAR].value_counts()
    runs = clearing[clearing >= 100].sort_index()
    assert list(pd.to_datetime(CALENDAR["day"])) == list(runs.index)
    assert CALENDAR["n_events"].tolist() == runs.tolist()
    main = clearing[clearing >= 4000]
    assert CALENDAR["is_main_run"].sum() == len(main)
    shown(f"{len(main)} days hold {100 * main.sum() / clearing.sum():.1f}% of all clearing events")
    row_shown("Other days", len(clearing) - len(runs), f"{clearing[clearing < 100].sum():,}",
              f"{100 * clearing[clearing < 100].sum() / clearing.sum():.1f}%")


def test_main_runs_are_split_by_the_day_of_month():
    main = CALENDAR[CALENDAR["is_main_run"]]
    early = pd.to_datetime(main["day"]).dt.day <= 14
    assert (main["main_run"] == early.map({True: "early", False: "late"})).all()


def test_no_day_falls_near_the_main_run_line(events, days):
    clearing = days[events["activity"] == CLEAR].value_counts()
    assert not clearing[(clearing >= 2_222) & (clearing < 4_250)].size


def test_first_clearing_recount(cohort, events, days):
    clearing = days[events["activity"] == CLEAR].value_counts()
    main_days = clearing[clearing >= 4000].index
    cleared = cohort[cohort["n_clear"] > 0]
    shown(f"{100 * cleared['clear'].isin(main_days).mean():.1f}% of items first cleared on a main run")
    shown(f"{100 * (cleared['clear'].dt.dayofweek == 3).mean():.1f}% on a Thursday")


def test_vendor_ties_recount(cohort, events, days):
    clearing = days[events["activity"] == CLEAR].value_counts()
    main_days = clearing[clearing >= 4000].index
    on_run = cohort[cohort["clear"].isin(main_days)]
    table = pd.DataFrame({"n": on_run.groupby("vendor").size(),
                          "early": on_run[on_run["clear"].dt.day <= 14].groupby("vendor").size()}).fillna(0)
    table = table[table["n"] >= 100]
    early, late = table["early"] >= 0.8 * table["n"], table["early"] <= 0.2 * table["n"]
    row_shown("Tied to the early run", early.sum(), f"{int(table.loc[early, 'n'].sum()):,}")
    row_shown("Tied to the late run", late.sum(), f"{int(table.loc[late, 'n'].sum()):,}")


def watched(took, waiting):
    frame = pd.DataFrame({"took": took, "waiting": waiting}).dropna(subset=["waiting"])
    frame = frame[frame["waiting"] >= 0]
    return frame["took"].where(frame["took"].notna(), frame["waiting"]).to_numpy(), frame["took"].notna().to_numpy()


def median_by_loop(days, finished) -> float | None:
    """The same estimate as p2p/survival.py, as a day-by-day loop, sharing no code with it."""
    share = 1.0
    for day in sorted(set(days[finished].tolist())):
        at_risk = int((days >= day).sum())
        share *= 1 - int(((days == day) & finished).sum()) / at_risk
        if share <= 0.5:
            return day
    return None


def test_monthly_medians_recount(items):
    frame = items[items["flow"].isin([MAIN, AFTER]) & (items["po_time"] >= "2018-01-01")
                  & (items["po_time"] < "2019-01-19")]
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    took = (frame["clear"] - frame["po"]).dt.days
    open_for = (LOG_END - frame["po"]).dt.days
    for month, part in frame.groupby(frame["po"].dt.month):
        days, finished = watched(took[part.index], open_for[part.index])
        median = median_by_loop(days, finished)
        shown_median = "not reached" if median is None else f"{median:.0f}"
        row_shown(names[month - 1], f"{len(part):,}", f"{100 * finished.mean():.1f}%",
                  f"{took[part.index].median():.0f}", shown_median)


def test_medians_hold_steady_through_september_and_only_the_plain_one_falls():
    table = {}
    for line in (RESULTS / "BOTTLENECKS.md").read_text().splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 5 and cells[0] in ("Jan", "Sep", "Dec"):
            table[cells[0]] = cells
    assert 80 <= int(table["Jan"][4]) <= 90 and 80 <= int(table["Sep"][4]) <= 90
    assert table["Dec"][4] == "not reached"
    assert int(table["Dec"][3]) < 40


def test_po_to_clearing_stage_recount(items):
    frame = items[items["flow"].isin([MAIN, AFTER]) & (items["po_time"] >= "2018-01-01")
                  & (items["po_time"] < "2019-01-19")]
    days, finished = watched((frame["clear"] - frame["po"]).dt.days, (LOG_END - frame["po"]).dt.days)
    row = STAGES.loc["PO to first clearing"]
    assert row["items"] == len(days)
    assert row["unfinished"] == (~finished).sum()
    assert row["median_days"] == median_by_loop(days, finished)


def test_block_reasons_recount(cohort, events):
    main = cohort[(cohort["flow"] == MAIN) & (cohort["n_ir"] > 0)].copy()
    keep = events[events["case_id"].isin(main.index)]

    def stamp(activity, how):
        return keep[keep["activity"] == activity].groupby("case_id")["time"].agg(how).reindex(main.index)

    first_goods, last_goods, first_invoice = stamp(GR, "min"), stamp(GR, "max"), stamp(IR, "min")
    changed = main.index.isin(keep.loc[keep["activity"].isin(["Change Price", "Change Quantity"]), "case_id"])
    conditions = [first_goods.isna() | (first_invoice < first_goods), first_invoice < last_goods,
                  main["n_ir"] > 1, changed]
    names = ["Invoice recorded before the goods", "Invoice recorded between deliveries",
             "Several invoices on the item", "PO price or quantity changed"]
    main["reason"] = np.select(conditions, names, "No reason visible in the log")
    main["blocked"] = main["n_block"] > 0
    table = main.groupby("reason")["blocked"].agg(["size", "sum"])
    for name, row in table.iterrows():
        cells = (name, f"{row['size']:,}", f"{row['sum']:,}", f"{100 * row['sum'] / row['size']:.1f}%",
                 f"{100 * row['sum'] / table['sum'].sum():.1f}%")
        shown("| " + " | ".join(cells) + " |")


def test_every_invoice_event_repeats_the_po_value_on_the_main_flow(cohort, events):
    # Why the block table has no "invoice amount differs" row: the log can't show one.
    main = cohort[cohort["flow"] == MAIN]
    invoices = events[(events["activity"] == IR) & events["case_id"].isin(main.index)]
    assert (invoices["value"].to_numpy() == main["po_value"].reindex(invoices["case_id"]).to_numpy()).all()


def test_extra_days_recount(waits, items):
    blocked_rows = waits[waits["flow"] == MAIN].copy()
    blocked_rows["month"] = blocked_rows["invoice"].dt.to_period("M")
    blocked_rows["blocked"] = blocked_rows["n_block"] > 0
    base = blocked_rows[~blocked_rows["blocked"]].groupby(["vendor", "month"])["days"].agg(["mean", "size"])
    base = base[base["size"] >= 5]["mean"].rename("usual")
    chosen = blocked_rows[blocked_rows["blocked"]
                          & blocked_rows["invoice"].between("2018-01-01", "2018-06-30")]
    joined = chosen.join(base, on=["vendor", "month"], how="inner")
    shown(f"a block goes with {(joined['days'] - joined['usual']).mean():+.1f} days")


def test_coverage_recount(cohort, timed, waits):
    cleared = cohort[cohort["n_clear"] > 0]
    shown(f"of {len(cohort):,} cohort items, {len(cleared):,} cleared")
    shown(f"{int(cleared['corrected'].sum()):,} of those have a cancellation or debit memo")
    shown(f"leaving {len(waits):,} ({100 * len(waits) / len(cohort):.1f}% of the cohort)")
    assert len(cleared) == int(cleared["corrected"].sum()) + len(timed) + 55


def test_map_counts_recount(cohort, events):
    keep = events[events["case_id"].isin(cohort.index)]
    cleared = keep.loc[keep["activity"] == CLEAR, "case_id"].nunique()
    gap = len(cohort) - cleared
    shown(f"{gap:,} items ({100 * gap / len(cohort):.1f}%) have nothing cleared")


def test_headline_shares_recount(waits):
    total = waits["total"].mean()
    for name, label in (("to_invoice", "PO to the vendor's invoice date"), ("term", "Implied payment term"),
                        ("overrun", "Process overrun: a step finished after the invoice was due"),
                        ("run_wait", "Waiting for a payment run, already ready and due")):
        row_shown(label, f"{waits[name].mean():.1f}", f"{100 * waits[name].mean() / total:.1f}%")


def test_medians_recount(waits):
    shown(f"{waits['total'].median():.0f} days in total, {waits['to_invoice'].median():.0f} to the invoice "
          f"date, {waits['term'].median():.0f} for the term and {waits['run_wait'].median():.0f} waiting for a run")


def test_spend_areas_recount(waits):
    late = waits[waits["overrun"] > 0]
    days = late.groupby("spend_area")["overrun"].sum().sort_values(ascending=False)
    for area in days.index[:3]:
        part = waits[waits["spend_area"] == area]
        row_shown(area, f"{len(part):,}", f"{100 * len(part) / len(waits):.1f}%",
                  f"{100 * (part['overrun'] > 0).mean():.1f}%", f"{100 * days[area] / days.sum():.1f}%")
    shown(f"hold {100 * days.iloc[:3].sum() / days.sum():.1f}% of overrun days")


def test_vendor_concentration_recount(waits):
    days = waits.groupby("vendor")["overrun"].sum().sort_values(ascending=False)
    shown(f"10 vendors hold {100 * days.iloc[:10].sum() / days.sum():.1f}% of them, out of {(days > 0).sum()} with any")


def test_term_bands_recount():
    bands = [("Under 28", 0, 28), ("28-34", 28, 35), ("35-41", 35, 42), ("42-55", 42, 56), ("56-62", 56, 63),
             ("63-83", 63, 84), ("84 and over", 84, 10_000)]
    for label, low, high in bands:
        chosen = TERMS[(TERMS >= low) & (TERMS < high)]
        assert f"| {label} | {len(chosen)} |" in REPORT


@pytest.mark.parametrize("label,share,by_area", [("Fastest 1%", 0.01, False), ("Fastest 5% (used)", 0.05, False),
                                                 ("Fastest 10%", 0.10, False),
                                                 ("Fastest 5%, per vendor and spend area", 0.05, True)])
def test_sensitivity_recount(timed, label, share, by_area):
    keys = ["vendor", "spend_area"] if by_area else ["vendor"]
    frame = timed.copy()
    groups = frame.groupby(keys, dropna=False)["days"]
    frame["n"] = groups.transform("size")
    frame = frame[frame["n"] >= 30].copy()
    frame["term_days"] = frame.groupby(keys, dropna=False)["days"].transform(
        lambda s: term_at_rank(s.to_numpy(), share))
    term = np.minimum(frame["term_days"], frame["days"])
    due = frame["invoice"] + pd.to_timedelta(term, unit="D")
    free = pd.concat([due, frame["ready"]], axis=1).max(axis=1)
    row_shown(label, f"{len(frame):,}", f"{term.mean():.1f}", f"{(free - due).dt.days.mean():.1f}",
              f"{(frame['clear'] - free).dt.days.mean():.1f}")


def test_year_end_gap_recount():
    main = pd.to_datetime(CALENDAR.loc[CALENDAR["is_main_run"], "day"]).reset_index(drop=True)
    gaps = main.diff().dt.days
    longest = gaps.idxmax()
    assert (main[longest - 1], main[longest]) == (pd.Timestamp("2018-12-07"), pd.Timestamp("2019-01-04"))
    shown(f"no main run between 7 December and 4 January, a gap of {gaps.max():.0f} days")


def test_unexplained_blocks_by_vendor_recount(cohort, events):
    main = cohort[(cohort["flow"] == MAIN) & (cohort["n_ir"] > 0)]
    keep = events[events["case_id"].isin(main.index)]
    goods = keep[keep["activity"] == GR].groupby("case_id")["time"].agg(["min", "max"]).reindex(main.index)
    invoice = keep[keep["activity"] == IR].groupby("case_id")["time"].min().reindex(main.index)
    changed = main.index.isin(keep.loc[keep["activity"].isin(["Change Price", "Change Quantity"]), "case_id"])
    plain = main[goods["min"].notna() & (invoice >= goods["max"]) & (main["n_ir"] == 1) & ~changed]
    rates = (plain["n_block"] > 0).groupby(plain["vendor"]).agg(["mean", "size"])
    rates = rates[rates["size"] >= 100]["mean"]
    shown(f"from {100 * rates.quantile(0.1):.0f}% at the 10th percentile to {100 * rates.quantile(0.9):.0f}% "
          f"at the 90th (median {100 * rates.median():.0f}%, {len(rates)} vendors")


def test_readme_part_3_matches_the_report(waits):
    readme = " ".join((ROOT / "README.md").read_text().split())
    total = waits["total"].mean()
    for label, name in (("PO to the vendor's invoice date", "to_invoice"), ("Implied payment term", "term"),
                        ("Process overrun", "overrun"), ("Waiting for a payment run", "run_wait")):
        assert f"| {label} | {waits[name].mean():.1f} | {100 * waits[name].mean() / total:.1f}% |" in readme, label
    assert f"{100 * (waits['ready'] > waits['due']).mean():.1f}% of items" in readme
    for phrase in ("20 days hold 79.9% of all clearing events", "81.2% of items first cleared on a main run",
                   "171 of 181 vendors", "cut the wait from 14.2 to 2.3 days", "48.4% of blocks",
                   "34.7% of blocks", "about 7 days", "81-87 days", "61.7% of overrun days",
                   "47.5% of overrun days", "28.4% of items", "126 of 148"):
        assert phrase in readme, phrase
        words = {"171 of 181 vendors": "171 vendors (94.5%)", "cut the wait from 14.2 to 2.3 days": "from 14.2 to 2.3 days",
                 "61.7% of overrun days": "61.7% of the days", "48.4% of blocks": "48.4% of blocks",
                 "34.7% of blocks": "34.7% of blocks", "126 of 148": "Of 148 vendors"}
        assert words.get(phrase, phrase) in REPORT, phrase


def test_report_and_tables_match_a_fresh_run(tmp_path):
    subprocess.run([sys.executable, "-m", "scripts.bottlenecks", "--out", str(tmp_path)],
                   cwd=ROOT, check=True)
    for name in ("BOTTLENECKS.md", "wait_stages.csv", "run_calendar.csv", "vendor_terms.csv",
                 "charts/06_process_map.png", "charts/07_days_to_clearing_by_month.png",
                 "charts/08_payment_calendar.png", "charts/09_where_the_days_go.png"):
        assert (tmp_path / name).read_bytes() == (RESULTS / name).read_bytes(), name
