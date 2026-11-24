#!/usr/bin/env python3
"""
Build a daily, webserver-ready archive of the aggregated results that were
already sent to Grafana/Prometheus by post-json-to-grafana.py.

Reads   <sent_dir>/chaos_<service>__<IPv4|IPv6>_<YYYYMMDD>_<HHMM>_<HHMM>-agg.json.gz
Checks  each file is valid gzip-compressed JSON (corrupt files are skipped)

Writes one tar per root server letter per day, all in the same month folder:
    <out_dir>/YYYY/MM/<letter>-YYYY-MM-DD.tar     e.g. results/daily/2026/11/A-2026-11-18.tar
    <out_dir>/YYYY/MM/SHA256SUMS                  checksums of all tars in that month

Each tar holds that letter's files for the day (IPv4 and IPv6, all 30-minute
windows), as they are: the files are already gzipped and are not compressed again.

Notes:
- Source files are never modified or removed.
- Letter+day combinations whose tar already exists are skipped; use --force to rebuild.
- The current UTC day is skipped by default, since it is still being filled.
  Use --include-today to override.

Usage:
    python3 build_daily_archive.py
    python3 build_daily_archive.py --out-dir /srv/www/rootviz
    python3 build_daily_archive.py --date 20261118 --force
"""

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import sys
import tarfile
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

SENT_RE = re.compile(
    r"^chaos_(?P<service>.+)__(?P<ipv>IPv4|IPv6)_"
    r"(?P<date>\d{8})_(?P<start>\d{4})_(?P<stop>\d{4})-agg\.json\.gz$"
)
TAR_RE = re.compile(r"^.+-\d{4}-\d{2}-\d{2}\.tar$")
DEFAULT_SENT_DIR = "./results/agg/sent"
DEFAULT_OUT_DIR = "./results/daily"

logger = logging.getLogger("build_daily_archive")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_valid_json_gz(path: str) -> bool:
    """Read the whole file and confirm it is gzip-compressed JSON."""
    try:
        with gzip.open(path, "rb") as f:
            json.load(f)
        return True
    except (OSError, EOFError, ValueError) as e:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        logger.warning(f"Skipping {os.path.basename(path)}: not valid gzip JSON ({e})")
        return False


def collect_groups(sent_dir: str, only_date: Optional[str]) -> Dict[Tuple[str, str], List[str]]:
    """Return {(YYYYMMDD, letter): [paths of sent files]}."""
    groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for name in sorted(os.listdir(sent_dir)):
        m = SENT_RE.match(name)
        if not m:
            continue
        if only_date and m["date"] != only_date:
            continue
        groups[(m["date"], m["service"])].append(os.path.join(sent_dir, name))
    return groups


def atomic_write(dst: str, write_fn) -> None:
    """Write to a temp file in the same directory, then rename into place."""
    directory = os.path.dirname(dst)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    os.close(fd)
    try:
        write_fn(tmp)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def write_sha256sums(directory: str, name_re: re.Pattern) -> None:
    """Write SHA256SUMS in directory for every file whose name matches name_re."""
    lines = []
    for name in sorted(os.listdir(directory)):
        if name_re.match(name):
            lines.append(f"{sha256_file(os.path.join(directory, name))}  {name}\n")

    def write(tmp: str) -> None:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)

    atomic_write(os.path.join(directory, "SHA256SUMS"), write)


def write_bundle(tar_path: str, files: List[str]) -> None:
    def write(tmp: str) -> None:
        with tarfile.open(tmp, "w") as tar:
            for path in files:
                tar.add(path, arcname=os.path.basename(path))

    atomic_write(tar_path, write)


def tar_path_for(out_dir: str, date: str, letter: str) -> str:
    year, month, day = date[:4], date[4:6], date[6:8]
    return os.path.join(out_dir, year, month, f"{letter}-{year}-{month}-{day}.tar")


def process_group(date: str, letter: str, files: List[str], out_dir: str) -> None:
    """Write the tar for one letter and one day, then refresh that letter's SHA256SUMS."""
    valid = [f for f in files if is_valid_json_gz(f)]
    if not valid:
        logger.warning(f"[{date} {letter}] no valid files, nothing written")
        return

    tar_path = tar_path_for(out_dir, date, letter)
    write_bundle(tar_path, valid)
    write_sha256sums(os.path.dirname(tar_path), TAR_RE)
    logger.info(f"[{date} {letter}] {len(valid)} file(s) -> {tar_path}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sent-dir", default=DEFAULT_SENT_DIR, help="folder with sent *-agg.json.gz files")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where the YYYY/MM/<letter>/ tree is written")
    parser.add_argument("--date", help="only process this day (YYYYMMDD)")
    parser.add_argument("--include-today", action="store_true", help="include the current UTC day (may be incomplete)")
    parser.add_argument("--force", action="store_true", help="rebuild tars that already exist")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not os.path.isdir(args.sent_dir):
        logger.error(f"Sent directory not found: {args.sent_dir}")
        return 1
    if args.date and not re.fullmatch(r"\d{8}", args.date):
        logger.error("--date must be YYYYMMDD")
        return 1

    groups = collect_groups(args.sent_dir, args.date)
    if not groups:
        logger.info("No sent files found")
        return 0

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    built = 0
    for (date, letter) in sorted(groups):
        if date == today and not args.include_today:
            logger.info(f"[{date} {letter}] skipped: day still in progress (use --include-today to override)")
            continue
        if not args.force and os.path.exists(tar_path_for(args.out_dir, date, letter)):
            logger.debug(f"[{date} {letter}] already built, skipping")
            continue
        process_group(date, letter, groups[(date, letter)], args.out_dir)
        built += 1

    logger.info(f"Done: {built} letter/day archive(s) built in {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
