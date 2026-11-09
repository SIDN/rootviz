#!/usr/bin/env python3
"""
RIPE Atlas Probes Metadata Downloader
-------------------------------------
- Downloads the daily probe archive from ftp.ripe.net (today, falling back
  to the previous two days)
- Keeps the columns needed for enrichment and saves probes/probes-YYYYMMDD.csv
- Deletes probe files older than days_to_keep
"""

import bz2
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from common import atomic_writer, setup_logging
from config import load_config

# === CONFIG ===============================================================

CONFIG = load_config()
PROBES_DIR = CONFIG.get("probes_dir", "probes")
DAYS_TO_KEEP = int(CONFIG.get("days_to_keep", 5))
PROBES_URL = "https://ftp.ripe.net/ripe/atlas/probes/archive/{y}/{m}/{ymd}.json.bz2"
COLUMNS = [
    "id", "address_v4", "address_v6", "asn_v4", "asn_v6", "prefix_v4", "prefix_v6",
    "is_anchor", "tags", "country_code", "longitude", "latitude",
]
_PROBE_FILE_RE = re.compile(r"^probes-(\d{8})\.csv$")

logger = logging.getLogger(__name__)

# === FUNCTIONS ============================================================


def probes_csv_path(probes_dir: str, day) -> str:
    return os.path.join(probes_dir, f"probes-{day:%Y%m%d}.csv")


def cleanup_old_probes(probes_dir: str = PROBES_DIR, days_old: int = DAYS_TO_KEEP) -> None:
    """Delete probes-YYYYMMDD.csv files whose date is older than days_old."""
    if not os.path.isdir(probes_dir):
        return
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days_old)
    for filename in os.listdir(probes_dir):
        m = _PROBE_FILE_RE.match(filename)
        if not m:
            continue
        file_date = datetime.strptime(m.group(1), "%Y%m%d").date()
        if file_date < cutoff:
            os.remove(os.path.join(probes_dir, filename))
            logger.info(f"Deleted old probe file {filename}")


def _tags_to_str(tags) -> str:
    """Flatten the archive's tag list to a comma-separated slug string."""
    if isinstance(tags, list):
        return ",".join(t.get("slug", "") if isinstance(t, dict) else str(t) for t in tags)
    return "" if tags is None else str(tags)


def download_and_process_probes(probes_dir: str = PROBES_DIR, probes_db=None) -> str:
    """Ensure a recent probes CSV exists and return its path.

    Tries today, yesterday and 2 days ago. ``probes_db`` is accepted for
    backwards compatibility and ignored.
    """
    os.makedirs(probes_dir, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    last_error = None

    for offset in (0, 1, 2):
        day = today - timedelta(days=offset)
        csv_file = probes_csv_path(probes_dir, day)
        if os.path.exists(csv_file):
            logger.info(f"Probe file for {day} already exists: {csv_file}")
            return csv_file

        url = PROBES_URL.format(y=f"{day:%Y}", m=f"{day:%m}", ymd=f"{day:%Y%m%d}")
        try:
            logger.info(f"Downloading {url}")
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            objects = json.loads(bz2.decompress(resp.content))["objects"]
        except Exception as e:
            logger.warning(f"Could not fetch probes for {day}: {e}")
            last_error = e
            continue

        df = pd.DataFrame(objects).reindex(columns=COLUMNS)
        df["asn_v4"] = pd.to_numeric(df["asn_v4"], errors="coerce").astype("Int64")
        df["asn_v6"] = pd.to_numeric(df["asn_v6"], errors="coerce").astype("Int64")
        df["tags"] = df["tags"].map(_tags_to_str)

        with atomic_writer(csv_file) as f:
            df.to_csv(f, index=False)
        logger.info(f"Saved {len(df)} probes to {csv_file}")
        return csv_file

    raise RuntimeError(f"Failed to download probe metadata for the last 3 days: {last_error}")


def main() -> int:
    setup_logging(CONFIG.get("logging_level", "INFO"))
    download_and_process_probes(PROBES_DIR)
    cleanup_old_probes(PROBES_DIR, DAYS_TO_KEEP)
    return 0


# === ENTRYPOINT ===========================================================
if __name__ == "__main__":
    sys.exit(main())
