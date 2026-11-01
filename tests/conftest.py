import pandas as pd
import pytest

from p2p.source import CASES_CSV, EVENTS_CSV


@pytest.fixture(scope="session")
def events() -> pd.DataFrame:
    """The committed events, read with pandas only, not through the project's SQL."""
    frame = pd.read_csv(EVENTS_CSV, dtype=str, keep_default_na=False)
    frame["seq"] = frame["seq"].astype(int)
    # Dates as old as 1948 fit pandas' nanosecond range, so none become NaT.
    frame["time"] = pd.to_datetime(frame["ts"].str[:19], format="%Y-%m-%dT%H:%M:%S")
    frame["value"] = frame["net_worth_eur"].astype(float)
    return frame


@pytest.fixture(scope="session")
def cases() -> pd.DataFrame:
    return pd.read_csv(CASES_CSV, dtype=str, keep_default_na=False)
