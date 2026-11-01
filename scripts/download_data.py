"""Fetch the BPI Challenge 2019 event log into data/raw/ and verify it.

    python -m scripts.download_data

The raw log isn't committed (data/raw/ is gitignored), and the analysis doesn't
need it: the snapshot in data/ is built from it by scripts/build_snapshot.py.

Hosts are tried in the order set in p2p/source.py. A copy is only kept if the MD5
of its plain XML matches the publisher's, so a wrong, partial or truncated file
is thrown away. (The Internet Archive's capture of this file, for example, is
only the first 1 MiB of it.)
"""
import sys
import urllib.request

from p2p.source import RAW_DIR, RAW_NAME, SOURCES, XES_MD5
from p2p.xes import md5_of_xes, open_xes

USER_AGENT = "Mozilla/5.0 (purchase-to-pay-process-mining; +https://github.com/nisha1324)"
CONNECT_TIMEOUT = 30


def fetch(url: str, target) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as response:
        with open(target, "wb") as out:
            while block := response.read(1 << 20):
                out.write(block)


def existing_copy(raw_dir=RAW_DIR):
    """The log already on disk, gzipped or not, if there is one."""
    for name in (RAW_NAME, RAW_NAME + ".gz"):
        if (raw_dir / name).exists():
            return raw_dir / name
    return None


def is_gzip(path) -> bool:
    with open(path, "rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def download(sources=SOURCES, expected_md5=XES_MD5, raw_dir=RAW_DIR, fetcher=fetch):
    """Return the path of a verified copy, trying each host until one checks out."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    found = existing_copy(raw_dir)
    if found is not None:
        actual = md5_of_xes(found)
        if actual != expected_md5:
            raise SystemExit(f"{found} has MD5 {actual}, expected {expected_md5}. Stopping.")
        print(f"verified existing copy: {found}")
        return found

    part = raw_dir / (RAW_NAME + ".part")
    for host, url in sources:
        print(f"trying {host} ...")
        try:
            fetcher(url, part)
            actual = md5_of_xes(part)
        except Exception as exc:  # a dead host or a corrupt file: try the next one
            print(f"  failed: {exc}", file=sys.stderr)
            part.unlink(missing_ok=True)
            continue
        if actual != expected_md5:
            print(f"  wrong file: MD5 {actual}", file=sys.stderr)
            part.unlink()
            continue
        target = raw_dir / (RAW_NAME + (".gz" if is_gzip(part) else ""))
        part.replace(target)
        print(f"verified {target} from {host}")
        return target
    raise SystemExit("No host served a copy matching the published MD5.")


def main() -> None:
    path = download()
    with open_xes(path) as stream:
        if b"<log" not in stream.read(200):
            raise SystemExit(f"{path} doesn't look like an XES log")


if __name__ == "__main__":
    main()
