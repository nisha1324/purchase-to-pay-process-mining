"""Stream an XES event log into plain rows.

XES is XML, and this file is 700+ MB, so it's read with iterparse and each
element is cleared as soon as it's used. The parser is strict on purpose: an
attribute it doesn't know about raises instead of being dropped.
"""
import gzip
import hashlib
from typing import IO, Iterator
from xml.etree import ElementTree as ET

ATTRIBUTE_TAGS = {"string", "boolean", "float", "date", "int"}

CASE_FIELDS = {
    "concept:name": "case_id",
    "Purchasing Document": "purchasing_document",
    "Item": "item",
    "Item Type": "item_type",
    "GR-Based Inv. Verif.": "gr_based_iv",
    "Goods Receipt": "goods_receipt",
    "Source": "source",
    "Purch. Doc. Category name": "doc_category",
    "Company": "company",
    "Spend classification text": "spend_classification",
    "Spend area text": "spend_area",
    "Sub spend area text": "sub_spend_area",
    "Vendor": "vendor",
    "Name": "vendor_name",
    "Document Type": "document_type",
    "Item Category": "item_category",
}
EVENT_FIELDS = {
    "concept:name": "activity",
    "time:timestamp": "ts",
    "org:resource": "resource",
    "User": "user",
    "Cumulative net worth (EUR)": "net_worth_eur",
}
CASE_COLUMNS = tuple(CASE_FIELDS.values())
# org:resource is not kept: the parser checks that it always equals User.
EVENT_COLUMNS = ("case_id", "seq", "activity", "ts", "user", "net_worth_eur")


class XesError(ValueError):
    """The file isn't the log we expected."""


def open_xes(path) -> IO[bytes]:
    """Open a .xes file, gzipped or not, as a stream of the plain XML bytes."""
    with open(path, "rb") as handle:
        magic = handle.read(2)
    return gzip.open(path, "rb") if magic == b"\x1f\x8b" else open(path, "rb")


def md5_of_xes(path, chunk: int = 1 << 20) -> str:
    """MD5 of the plain XML, so a gzipped copy can be checked against the original."""
    digest = hashlib.md5(usedforsecurity=False)
    with open_xes(path) as stream:
        while block := stream.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _rename(raw: dict, fields: dict, where: str) -> dict:
    unknown = set(raw) - set(fields)
    missing = set(fields) - set(raw)
    if unknown or missing:
        raise XesError(f"{where}: unknown {sorted(unknown)}, missing {sorted(missing)}")
    return {fields[key]: value for key, value in raw.items()}


def iter_cases(stream: IO[bytes]) -> Iterator[tuple[dict, list[dict]]]:
    """Yield (case attributes, events in file order), one case at a time."""
    path: list[str] = []
    case: dict = {}
    event: dict = {}
    events: list[dict] = []
    for action, element in ET.iterparse(stream, events=("start", "end")):
        if action == "start":
            path.append(element.tag)
            if element.tag == "trace":
                case, events = {}, []
            elif element.tag == "event":
                event = {}
            continue

        path.pop()
        parent = path[-1] if path else None
        if element.tag in ATTRIBUTE_TAGS and parent in ("trace", "event"):
            target = case if parent == "trace" else event
            key = element.get("key")
            if key in target:
                raise XesError(f"attribute {key!r} repeated in one {parent}")
            target[key] = element.get("value")
        elif element.tag == "event":
            row = _rename(event, EVENT_FIELDS, f"event in {case.get('concept:name')}")
            if row.pop("resource") != row["user"]:
                raise XesError(f"User differs from org:resource in {case.get('concept:name')}")
            events.append(row)
            element.clear()
        elif element.tag == "trace":
            attributes = _rename(case, CASE_FIELDS, "trace")
            if not events:
                raise XesError(f"case {attributes['case_id']} has no events")
            yield attributes, events
            element.clear()
