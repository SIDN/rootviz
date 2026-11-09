#!/usr/bin/env python3
"""Helpers shared by the pipeline scripts."""

import logging
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd

# chaos_<service>__<IPv4|IPv6>_<YYYYMMDD>_<HHMM>_<HHMM>
RESULT_STEM_RE = re.compile(
    r"^chaos_(?P<service>.+)__(?P<ipv>IPv4|IPv6)_"
    r"(?P<date>\d{8})_(?P<start>\d{4})_(?P<stop>\d{4})$"
)

RAW_COLUMNS = ["prb_id", "timestamp", "src_addr", "rt", "rdata", "rcode", "error_type"]

# Values read back as missing. Deliberately excludes pandas defaults such as
# "NA" (Namibia's country code), "nan" and "null", which can be legitimate
# CHAOS strings or country codes. "None" is kept for files written by older
# versions of get_atlas_data.py.
NA_VALUES = ["", "None"]

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode="a", encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


def result_stem(service: str, ipv: str, start: int, stop: int) -> str:
    """Canonical stem for a measurement window file (no extension)."""
    date_str = time.strftime("%Y%m%d", time.gmtime(start))
    start_str = time.strftime("%H%M", time.gmtime(start))
    stop_str = time.strftime("%H%M", time.gmtime(stop))
    return f"chaos_{service}__{ipv}_{date_str}_{start_str}_{stop_str}"


def parse_result_stem(stem: str) -> Optional[Dict[str, str]]:
    """Parse a result file stem (also accepts a trailing '-agg')."""
    if stem.endswith("-agg"):
        stem = stem[: -len("-agg")]
    m = RESULT_STEM_RE.match(stem)
    return m.groupdict() if m else None


def sanitize_field(value) -> str:
    """Render a value for a TSV cell.

    CHAOS TXT answers come from whatever resolver answered the probe,
    including hijacking resolvers, so they must not be able to inject
    tabs/newlines that break or forge rows.
    """
    if value is None:
        return ""
    return _CONTROL_CHARS_RE.sub(" ", str(value))


@contextmanager
def atomic_writer(path: str, mode: str = "w", encoding: Optional[str] = "utf-8"):
    """Write to a temp file in the same directory and rename into place.

    A crash mid-write never leaves a truncated file behind (which later
    stages would otherwise treat as complete).
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, mode, encoding=encoding if "b" not in mode else None) as fh:
            yield fh
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_table(path: str) -> pd.DataFrame:
    """Read a raw (TSV) or enriched result file, sniffing the delimiter."""
    with open(path, "r", encoding="utf-8") as f:
        first_line = f.readline()
    sep = "\t" if "\t" in first_line else ","
    text_cols = {"rdata", "src_addr", "error_type", "country", "continent", "tags"}
    header = [c.strip() for c in first_line.rstrip("\r\n").split(sep)]
    df = pd.read_csv(path, sep=sep, keep_default_na=False, na_values=NA_VALUES,
                     dtype={c: str for c in header if c in text_cols})
    df.columns = df.columns.str.strip()
    return df


def compute_rtt_percentiles(rtts: Iterable[float]) -> Dict[str, Optional[float]]:
    arr = np.asarray([r for r in rtts if r is not None and not pd.isna(r)], dtype=float)
    if arr.size == 0:
        return {"p25": None, "p50": None, "p75": None, "p99": None}
    p25, p50, p75, p99 = np.percentile(arr, [25, 50, 75, 99])
    return {
        "p25": round(float(p25), 3),
        "p50": round(float(p50), 3),
        "p75": round(float(p75), 3),
        "p99": round(float(p99), 3),
    }
