"""Open the snapshot in DuckDB and run the named queries in sql/."""
import re
from pathlib import Path

import duckdb

from p2p.source import (BURST_MIN_ITEMS, CASES_CSV, CHECK_END, CHECK_START, CLUSTER_MIN_ITEMS,
                        EVENTS_CSV, FOLLOW_UP_END, FOLLOW_UP_START, RETRO_MIN_DAYS, ROOT,
                        VENDOR_DATE_FLOOR, WINDOW_END, WINDOW_START)

SQL_DIR = ROOT / "sql"
PARAMETERS = {
    "window_start": WINDOW_START,
    "window_end": WINDOW_END,
    "follow_start": FOLLOW_UP_START,
    "follow_end": FOLLOW_UP_END,
    "burst_min": BURST_MIN_ITEMS,
    "check_start": CHECK_START,
    "check_end": CHECK_END,
    "vendor_floor": VENDOR_DATE_FLOOR,
    "retro_days": RETRO_MIN_DAYS,
    "cluster_min": CLUSTER_MIN_ITEMS,
}


def connect(cases=CASES_CSV, events=EVENTS_CSV) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("set threads to 4")
    for statement in _statements((SQL_DIR / "00_load.sql").read_text()):
        used = {name: value for name, value in
                {"cases": str(cases), "events": str(events)}.items() if f"${name}" in statement}
        con.execute(statement, used)
    return con


def _statements(text: str) -> list[str]:
    return [part for part in text.split(";\n") if part.strip()]


def named_queries(path: Path) -> dict[str, str]:
    """Split a .sql file on its '-- name: x' lines."""
    parts = re.split(r"^-- name: (\w+)\n", path.read_text(), flags=re.M)
    return {name: body.rstrip().rstrip(";") for name, body in zip(parts[1::2], parts[2::2])}


def build(con, filename: str) -> None:
    """Run a .sql file of table definitions, statement by statement."""
    for statement in _statements((SQL_DIR / filename).read_text()):
        used = {name: value for name, value in PARAMETERS.items() if f"${name}" in statement}
        con.execute(statement, used)


def run(con, sql: str):
    """Run one query, passing only the parameters it mentions, into a DataFrame."""
    used = {name: value for name, value in PARAMETERS.items() if f"${name}" in sql}
    return con.execute(sql, used).df()
