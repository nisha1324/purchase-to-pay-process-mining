"""The committed snapshot: complete, unchanged and rebuildable from the raw log."""
import gzip
import hashlib
import io
import json

import pandas as pd
import pytest

from p2p.source import (CASES_CSV, EVENTS_CSV, PUBLISHED, RAW_DIR, SNAPSHOT_JSON, XES_MD5)
from p2p.xes import XesError, open_xes
from scripts.build_snapshot import write_snapshot
from scripts.download_data import existing_copy
from tests.test_xes import event, log, trace

SNAPSHOT = json.loads(SNAPSHOT_JSON.read_text())


def plain_sha(path) -> str:
    return hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest()


def test_committed_files_match_their_recorded_hashes():
    assert plain_sha(CASES_CSV) == SNAPSHOT["cases_csv_sha256"]
    assert plain_sha(EVENTS_CSV) == SNAPSHOT["events_csv_sha256"]
    assert SNAPSHOT["xes_md5"] == XES_MD5


def test_snapshot_matches_the_published_counts(cases, events):
    assert len(cases) == PUBLISHED["cases"] == SNAPSHOT["cases"]
    assert len(events) == PUBLISHED["events"] == SNAPSHOT["events"]
    assert cases["purchasing_document"].nunique() == PUBLISHED["purchase_documents"]
    assert events["activity"].nunique() == PUBLISHED["activities"]
    accounts = events.loc[events["user"] != "NONE", "user"].nunique()
    assert accounts == PUBLISHED["users"]


def test_case_ids_are_unique_and_join_both_ways(cases, events):
    assert cases["case_id"].is_unique
    assert (cases["purchasing_document"] + "_" + cases["item"] == cases["case_id"]).all()
    assert set(cases["case_id"]) == set(events["case_id"])


def test_every_item_has_exactly_one_po_creation(events):
    created = events[events["activity"] == "Create Purchase Order Item"]
    assert len(created) == created["case_id"].nunique() == PUBLISHED["cases"]


def test_seq_counts_from_zero_within_each_case_and_time_never_goes_back(events):
    previous = events.groupby("case_id", sort=False)["seq"].shift(-1) - events["seq"]
    assert previous.dropna().eq(1).all()
    assert events.groupby("case_id", sort=False)["seq"].min().eq(0).all()
    gap = events.groupby("case_id", sort=False)["time"].diff().dropna()
    assert (gap >= pd.Timedelta(0)).all()


def test_writer_on_a_small_log_is_repeatable(tmp_path):
    def write(folder):
        folder.mkdir()
        built = write_snapshot(
            log(trace(event("A"), event("B")), trace(event("C"), case="2_00001")),
            folder / "cases.csv.gz", folder / "events.csv.gz")
        return built, (folder / "events.csv.gz").read_bytes(), (folder / "cases.csv.gz").read_bytes()

    first, second = write(tmp_path / "a"), write(tmp_path / "b")
    assert first == second
    assert first[0]["cases"] == 2 and first[0]["events"] == 3
    rows = gzip.decompress(first[1]).decode().splitlines()
    assert rows[0] == "case_id,seq,activity,ts,user,net_worth_eur"
    assert rows[2] == "1_00001,1,B,2018-01-02T12:53:00.000Z,user_001,10.5"
    assert hashlib.sha256(gzip.decompress(first[1])).hexdigest() == first[0]["events_csv_sha256"]


def test_writer_refuses_a_case_seen_twice(tmp_path):
    with pytest.raises(XesError, match="twice"):
        write_snapshot(log(trace(event()), trace(event())), tmp_path / "c.gz", tmp_path / "e.gz")


@pytest.mark.skipif(existing_copy(RAW_DIR) is None, reason="raw log not downloaded")
def test_rebuilding_from_the_raw_log_gives_the_committed_bytes(tmp_path):
    with open_xes(existing_copy(RAW_DIR)) as stream:
        built = write_snapshot(stream, tmp_path / "c.csv.gz", tmp_path / "e.csv.gz")
    assert built["events_csv_sha256"] == SNAPSHOT["events_csv_sha256"]
    assert built["cases_csv_sha256"] == SNAPSHOT["cases_csv_sha256"]
    assert (tmp_path / "c.csv.gz").read_bytes() == CASES_CSV.read_bytes()
    assert (tmp_path / "e.csv.gz").read_bytes() == EVENTS_CSV.read_bytes()
