#!/usr/bin/env python3
"""
Build a daily, webserver-ready archive of the aggregated results that were
already sent to Grafana/Prometheus by post-json-to-grafana.py.

Reads   <sent_dir>/chaos_<service>__<IPv4|IPv6>_<YYYYMMDD>_<HHMM>_<HHMM>-agg.json.gz
Checks  each file is valid gzip-compressed JSON (corrupt files are skipped)

Writes, per day, one tar with that day's files:
    <out_dir>/YYYY/MM/YYYY-MM-DD.tar
    <out_dir>/YYYY/MM/SHA256SUMS          (checksums of the tars in that month)

Optional --with-folders also writes the unpacked files per day:
    <out_dir>/YYYY/MM/DD/<same file name>
    <out_dir>/YYYY/MM/DD/SHA256SUMS       (checksums of that day's files)

The tar holds the already-gzipped files as they are (not compressed again).

Notes:
- Source files are never modified or removed.
- Days whose output already exists are skipped; use --force to rebuild them.
- The current UTC day is skipped by default, since it is still being filled.
  Use --include-today to override.

Usage:
    python3 build_daily_archive.py
    python3 build_daily_archive.py --out-dir /srv/www/rootviz
    python3 build_daily_archive.py --date 20261118 --force
    python3 build_daily_archive.py --with-folders
"""

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

SENT_RE = re.compile(
    r"^chaos_(?P<service>.+)__(?P<ipv>IPv4|IPv6)_"
    r"(?P<date>\d{8})_(?P<start>\d{4})_(?P<stop>\d{4})-agg\.json\.gz$"
)
TAR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.tar$")
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


def collect_days(sent_dir: str, only_date: Optional[str]) -> Dict[str, List[str]]:
    """Return {YYYYMMDD: [paths of sent files for that day]}."""
    days: Dict[str, List[str]] = defaultdict(list)
    for name in sorted(os.listdir(sent_dir)):
        m = SENT_RE.match(name)
        if not m:
            continue
        if only_date and m["date"] != only_date:
            continue
        days[m["date"]].append(os.path.join(sent_dir, name))
    return days


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


def copy_if_changed(src: str, dst: str) -> bool:
    """Copy src to dst unless dst already has identical content. Returns True if copied."""
    if os.path.exists(dst) and sha256_file(dst) == sha256_file(src):
        return False
    atomic_write(dst, lambda tmp: shutil.copyfile(src, tmp))
    return True


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


def already_built(date: str, out_dir: str, with_folders: bool) -> bool:
    """True if the outputs this run would write for date already exist."""
    year, month, day = date[:4], date[4:6], date[6:8]
    tar_exists = os.path.exists(os.path.join(out_dir, year, month, f"{year}-{month}-{day}.tar"))
    if not with_folders:
        return tar_exists
    return tar_exists and os.path.isdir(os.path.join(out_dir, year, month, day))


def process_day(date: str, files: List[str], out_dir: str, with_folders: bool) -> None:
    """Write one day's tar (and optionally its unpacked folder) under out_dir/YYYY/MM/."""
    year, month, day = date[:4], date[4:6], date[6:8]
    month_dir = os.path.join(out_dir, year, month)
    day_dir = os.path.join(month_dir, day)

    valid = [f for f in files if is_valid_json_gz(f)]
    if not valid:
        logger.warning(f"[{date}] no valid files, nothing written")
        return

    write_bundle(os.path.join(month_dir, f"{year}-{month}-{day}.tar"), valid)
    write_sha256sums(month_dir, TAR_RE)

    if with_folders:
        os.makedirs(day_dir, exist_ok=True)
        for src in valid:
            copy_if_changed(src, os.path.join(day_dir, os.path.basename(src)))
        write_sha256sums(day_dir, SENT_RE)

    logger.info(f"[{date}] {len(valid)} file(s) -> {month_dir}/{year}-{month}-{day}.tar"
                f"{' + folder ' + day_dir if with_folders else ''}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sent-dir", default=DEFAULT_SENT_DIR, help="folder with sent *-agg.json.gz files")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where the YYYY/MM/ tree is written")
    parser.add_argument("--date", help="only process this day (YYYYMMDD)")
    parser.add_argument("--with-folders", action="store_true",
                        help="also write the unpacked files in YYYY/MM/DD/ folders")
    parser.add_argument("--bundle-only", action="store_true",
                        help="no longer needed: this is the default (kept so old commands still work)")
    parser.add_argument("--include-today", action="store_true", help="include the current UTC day (may be incomplete)")
    parser.add_argument("--force", action="store_true", help="rebuild days that already exist in the output")
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

    days = collect_days(args.sent_dir, args.date)
    if not days:
        logger.info("No sent files found")
        return 0

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    built = 0
    for date in sorted(days):
        if date == today and not args.include_today:
            logger.info(f"[{date}] skipped: day still in progress (use --include-today to override)")
            continue
        if not args.force and already_built(date, args.out_dir, args.with_folders):
            logger.debug(f"[{date}] already built, skipping")
            continue
        process_day(date, days[date], args.out_dir, args.with_folders)
        built += 1

    logger.info(f"Done: {built} day(s) built in {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
