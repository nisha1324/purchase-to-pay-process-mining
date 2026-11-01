"""The downloader keeps only a copy that matches the publisher's MD5."""
import gzip
import hashlib

import pytest

from scripts.download_data import RAW_NAME, download, existing_copy

GOOD = b"<log>the real log</log>"
MD5 = hashlib.md5(GOOD, usedforsecurity=False).hexdigest()
SOURCES = (("first", "u1"), ("second", "u2"), ("third", "u3"))


def fake_fetcher(replies: dict):
    calls = []

    def fetch(url, target):
        calls.append(url)
        reply = replies[url]
        if isinstance(reply, Exception):
            raise reply
        target.write_bytes(reply)

    fetch.calls = calls
    return fetch


def test_falls_through_dead_and_wrong_hosts_to_a_gzipped_copy(tmp_path):
    fetcher = fake_fetcher({"u1": OSError("timed out"), "u2": GOOD[:9],
                            "u3": gzip.compress(GOOD)})
    path = download(SOURCES, MD5, tmp_path, fetcher)
    assert fetcher.calls == ["u1", "u2", "u3"]
    assert path == tmp_path / (RAW_NAME + ".gz")
    assert gzip.decompress(path.read_bytes()) == GOOD
    assert [item.name for item in tmp_path.iterdir()] == [path.name]  # no .part left


def test_stops_at_the_first_good_host_and_keeps_a_plain_file(tmp_path):
    fetcher = fake_fetcher({"u1": GOOD})
    path = download(SOURCES, MD5, tmp_path, fetcher)
    assert fetcher.calls == ["u1"]
    assert path.name == RAW_NAME and path.read_bytes() == GOOD


def test_truncated_gzip_doesnt_count_as_a_copy(tmp_path):
    fetcher = fake_fetcher({"u1": gzip.compress(GOOD)[:12], "u2": GOOD})
    assert download(SOURCES, MD5, tmp_path, fetcher).name == RAW_NAME


def test_fails_when_no_host_has_the_right_file(tmp_path):
    fetcher = fake_fetcher({"u1": b"x", "u2": b"y", "u3": OSError("gone")})
    with pytest.raises(SystemExit, match="No host"):
        download(SOURCES, MD5, tmp_path, fetcher)
    assert existing_copy(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_verified_copy_on_disk_isnt_fetched_again(tmp_path):
    (tmp_path / RAW_NAME).write_bytes(GOOD)
    fetcher = fake_fetcher({})
    assert download(SOURCES, MD5, tmp_path, fetcher).name == RAW_NAME
    assert fetcher.calls == []


def test_a_wrong_copy_on_disk_stops_the_run_and_stays_put(tmp_path):
    (tmp_path / RAW_NAME).write_bytes(b"changed")
    with pytest.raises(SystemExit, match="expected"):
        download(SOURCES, MD5, tmp_path, fake_fetcher({}))
    assert (tmp_path / RAW_NAME).read_bytes() == b"changed"
