"""Where the event log comes from and what a good copy of it looks like."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
RAW_NAME = "BPI_Challenge_2019.xes"
EVENTS_CSV = ROOT / "data" / "events.csv.gz"
CASES_CSV = ROOT / "data" / "cases.csv.gz"
SNAPSHOT_JSON = ROOT / "data" / "SNAPSHOT.json"
RESULTS = ROOT / "results"
CHARTS = RESULTS / "charts"

DOI = "10.4121/uuid:d06aff4b-79f0-45e6-8ec8-e19730c248f1"
LICENCE = "CC BY 4.0"
CITATION = (
    "van Dongen, Boudewijn (2019). BPI Challenge 2019. 4TU.ResearchData. "
    f"Dataset. https://doi.org/{DOI}"
)

# The publisher's MD5 of the plain .xes file (figshare record 12715853). Every
# copy is checked against this, so it doesn't matter which host served it.
XES_MD5 = "4eb909242351193a61e1c15b9c3cc814"

# Tried in order. The two official hosts come first; the third is a public
# GitHub copy, gzipped, which is used only because the MD5 above proves it holds
# the same bytes.
SOURCES = (
    (
        "4TU.ResearchData",
        "https://data.4tu.nl/file/35ed7122-966a-484e-a0e1-749b64e3366d/"
        "864493d1-3a58-47f6-ad6f-27f95f995828",
    ),
    ("figshare", "https://ndownloader.figshare.com/files/24072995"),
    (
        "GitHub copy",
        "https://media.githubusercontent.com/media/Gength/generalization_analysis/"
        "main/data/BPI-Challenge_2019/BPI_Challenge_2019.xes.gz",
    ),
)

# The counts stated in the dataset's description.
PUBLISHED = {
    "cases": 251_734,
    "events": 1_595_923,
    "purchase_documents": 76_349,
    "activities": 42,
    "users": 627,
}

# The log's real span. A handful of hand-typed dates fall outside it (see the
# source profile). WINDOW_END is exclusive: the last dense day is 2019-01-18.
WINDOW_START = "2018-01-01"
WINDOW_END = "2019-01-19"

# Items created in this span have at least 6.5 months of follow-up before the log
# stops, so their outcomes are close to final. FOLLOW_UP_END is exclusive.
FOLLOW_UP_START = "2018-01-01"
FOLLOW_UP_END = "2018-07-01"

# Same activity, same account, same minute, this many events or more: a bulk run.
BURST_MIN_ITEMS = 50

# A second window for the compliance rules, to see whether a rate holds outside the
# cohort. These items had 3.5 to 6.5 months of follow-up, so it's a check, not a result.
CHECK_START = "2018-07-01"
CHECK_END = "2018-10-01"

# Vendor invoice dates before this year are treated as typing errors (see 3.2 in the
# source profile). Late-2017 dates are kept, because they can be real.
VENDOR_DATE_FLOOR = "2017-01-01"

# An invoice this many days older than its PO counts as clearly retrospective.
RETRO_MIN_DAYS = 7

# A vendor needs this many items in the cohort to count as a cluster that never clears.
CLUSTER_MIN_ITEMS = 20
