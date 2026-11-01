"""The parser on small hand-written logs: strict about anything unexpected."""
import gzip
import hashlib
import io

import pytest

from p2p.xes import XesError, iter_cases, md5_of_xes, open_xes

CASE = """
<string key="concept:name" value="{case}"/>
<string key="Purchasing Document" value="1"/><string key="Item" value="00001"/>
<string key="Item Type" value="Standard"/><boolean key="GR-Based Inv. Verif." value="false"/>
<boolean key="Goods Receipt" value="true"/><string key="Source" value="s"/>
<string key="Purch. Doc. Category name" value="Purchase order"/>
<string key="Company" value="c"/><string key="Spend classification text" value="PR"/>
<string key="Spend area text" value="a &amp; b"/><string key="Sub spend area text" value=""/>
<string key="Vendor" value="v"/><string key="Name" value="n"/>
<string key="Document Type" value="Standard PO"/>
<string key="Item Category" value="3-way match, invoice before GR"/>
"""
EVENT = """<event>
<string key="concept:name" value="{activity}"/><date key="time:timestamp" value="{ts}"/>
<string key="org:resource" value="{resource}"/><string key="User" value="{user}"/>
<float key="Cumulative net worth (EUR)" value="10.5"/>{extra}</event>"""


def event(activity="Create Purchase Order Item", ts="2018-01-02T12:53:00.000Z",
          resource="user_001", user="user_001", extra=""):
    return EVENT.format(activity=activity, ts=ts, resource=resource, user=user, extra=extra)


def log(*traces) -> io.BytesIO:
    # The log-level concept:name is real XES, and mustn't leak into a case.
    body = '<string key="concept:name" value="the log"/>' + "".join(traces)
    return io.BytesIO(f'<?xml version="1.0"?><log>{body}</log>'.encode())


def trace(*events, case="1_00001", head=None):
    return f"<trace>{head if head is not None else CASE.format(case=case)}{''.join(events)}</trace>"


def test_reads_cases_and_events_in_file_order():
    cases = list(iter_cases(log(
        trace(event("A"), event("B", ts="2018-01-03T00:00:00.000Z")),
        trace(event("C"), case="2_00001"),
    )))
    assert [case["case_id"] for case, _ in cases] == ["1_00001", "2_00001"]
    assert [row["activity"] for row in cases[0][1]] == ["A", "B"]
    first = cases[0][1][0]
    assert first == {"activity": "A", "ts": "2018-01-02T12:53:00.000Z", "user": "user_001",
                     "net_worth_eur": "10.5"}
    assert cases[0][0]["spend_area"] == "a & b"
    assert cases[0][0]["sub_spend_area"] == ""


def test_an_event_doesnt_inherit_the_case_name():
    (case, events), = iter_cases(log(trace(event("A"))))
    assert case["case_id"] == "1_00001" and events[0]["activity"] == "A"


def test_rejects_an_unknown_event_attribute():
    extra = '<string key="Surprise" value="x"/>'
    with pytest.raises(XesError, match="Surprise"):
        list(iter_cases(log(trace(event(extra=extra)))))


def test_rejects_a_missing_case_attribute():
    head = CASE.format(case="1_00001").replace('<string key="Vendor" value="v"/>', "")
    with pytest.raises(XesError, match="Vendor"):
        list(iter_cases(log(trace(event(), head=head))))


def test_rejects_a_repeated_attribute():
    extra = '<string key="User" value="user_001"/>'
    with pytest.raises(XesError, match="repeated"):
        list(iter_cases(log(trace(event(extra=extra)))))


def test_rejects_user_different_from_resource():
    with pytest.raises(XesError, match="differs"):
        list(iter_cases(log(trace(event(resource="user_001", user="user_002")))))


def test_rejects_a_case_with_no_events():
    with pytest.raises(XesError, match="no events"):
        list(iter_cases(log(trace())))


def test_md5_is_of_the_plain_xml_whether_or_not_it_is_gzipped(tmp_path):
    payload = b"<log></log>" * 1000
    plain, zipped = tmp_path / "a.xes", tmp_path / "a.xes.gz"
    plain.write_bytes(payload)
    zipped.write_bytes(gzip.compress(payload))
    expected = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    assert md5_of_xes(plain) == md5_of_xes(zipped) == expected
    with open_xes(zipped) as stream:
        assert stream.read() == payload
