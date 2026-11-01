"""Turn the raw XES log into two committed gzipped CSVs.

    python -m scripts.build_snapshot

data/cases.csv.gz has one row per purchase item, data/events.csv.gz one row per
event. Nothing is filtered or corrected here, however odd it looks: values stay as
the source wrote them, and the data-quality calls are made, visibly, downstream.
Writing is deterministic (no gzip timestamp, fixed order), so a rebuild can be
compared byte for byte.
"""
import csv
import gzip
import hashlib
import json
import sys

from p2p.source import (CASES_CSV, CITATION, DOI, EVENTS_CSV, LICENCE, PUBLISHED, RAW_DIR,
                        SNAPSHOT_JSON, XES_MD5)
from p2p.xes import CASE_COLUMNS, EVENT_COLUMNS, XesError, iter_cases, md5_of_xes, open_xes
from scripts.download_data import existing_copy


def open_csv(path):
    raw = open(path, "wb")
    zipped = gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0)
    return raw, zipped


class HashingText:
    """A text sink for csv.writer: encodes, gzips and hashes the plain bytes."""

    def __init__(self, zipped):
        self.zipped = zipped
        self.digest = hashlib.sha256()

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        self.digest.update(data)
        self.zipped.write(data)


def write_snapshot(stream, cases_path, events_path) -> dict:
    """Parse the log into the two CSVs; return row counts and content hashes."""
    seen: set[str] = set()
    n_cases = n_events = 0
    cases_raw, cases_gz = open_csv(cases_path)
    events_raw, events_gz = open_csv(events_path)
    cases_sink, events_sink = HashingText(cases_gz), HashingText(events_gz)
    try:
        cases_out = csv.writer(cases_sink, lineterminator="\n")
        events_out = csv.writer(events_sink, lineterminator="\n")
        cases_out.writerow(CASE_COLUMNS)
        events_out.writerow(EVENT_COLUMNS)
        for case, events in iter_cases(stream):
            case_id = case["case_id"]
            if case_id in seen:
                raise XesError(f"case {case_id} appears twice")
            seen.add(case_id)
            cases_out.writerow(case[column] for column in CASE_COLUMNS)
            n_cases += 1
            for seq, event in enumerate(events):
                events_out.writerow(
                    (case_id, seq, event["activity"], event["ts"], event["user"],
                     event["net_worth_eur"])
                )
                n_events += 1
    finally:
        for handle in (cases_gz, cases_raw, events_gz, events_raw):
            handle.close()
    return {
        "cases": n_cases,
        "events": n_events,
        "cases_csv_sha256": cases_sink.digest.hexdigest(),
        "events_csv_sha256": events_sink.digest.hexdigest(),
    }


def main() -> None:
    raw = existing_copy(RAW_DIR)
    if raw is None:
        sys.exit("No raw log. Run python -m scripts.download_data first.")
    md5 = md5_of_xes(raw)
    if md5 != XES_MD5:
        sys.exit(f"{raw} has MD5 {md5}, expected {XES_MD5}.")
    with open_xes(raw) as stream:
        built = write_snapshot(stream, CASES_CSV, EVENTS_CSV)
    if built["cases"] != PUBLISHED["cases"] or built["events"] != PUBLISHED["events"]:
        sys.exit(f"Row counts differ from the dataset's description: {built}")
    SNAPSHOT_JSON.write_text(
        json.dumps(
            {"citation": CITATION, "doi": DOI, "licence": LICENCE, "xes_md5": md5, **built},
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(built, indent=2))


if __name__ == "__main__":
    main()
