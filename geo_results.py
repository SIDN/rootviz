#!/usr/bin/env python3
"""
Enrich raw CHAOS result files with probe metadata and CHAOS validation.

For every un-enriched result TSV in save_dir this adds:
  asn, country, continent, tags, v4_works, v6_works, chaos_valid
and rewrites the file in place (still tab-separated).
"""

import glob
import logging
import os
import re
import sys
from typing import Callable, Optional

import pandas as pd
import pycountry_convert as pc

from common import atomic_writer, parse_result_stem, read_table, setup_logging
from config import load_config

CONFIG = load_config()
MEASUREMENTS = CONFIG.get("measurements") or {}
RESULTS_DIR = CONFIG.get("save_dir", "results")
PROBES_DIR = CONFIG.get("probes_dir", "probes")

ENRICHED_COLUMNS = ("asn", "country", "continent", "chaos_valid")

CONTINENTS = {
    "AF": "Africa",
    "NA": "North America",
    "OC": "Oceania",
    "AN": "Antarctica",
    "AS": "Asia",
    "EU": "Europe",
    "SA": "South America",
}

logger = logging.getLogger(__name__)


def build_validator(name: str) -> Optional[Callable[[str], bool]]:
    """Return a function that checks a CHAOS string for service `name`.

    Uses the service's `regex` (Python re.match semantics, i.e. anchored at
    the start) if set, otherwise an exact match against the comma-separated
    `chaos_string` list. Returns None if the service has neither.
    """
    entry = MEASUREMENTS.get(name)
    if not isinstance(entry, dict):
        return None
    if entry.get("regex"):
        pattern = re.compile(entry["regex"])
        return lambda s: bool(pattern.match(s))
    if entry.get("chaos_string"):
        allowed = {x.strip() for x in str(entry["chaos_string"]).split(",") if x.strip()}
        return lambda s: s in allowed
    return None


def validate_chaos_string(validator: Callable[[str], bool], chaos_str) -> bool:
    if not isinstance(chaos_str, str):
        return False  # missing answer (NaN/None)
    return validator(chaos_str)


def country_to_region(country_code) -> str:
    """Convert ISO alpha-2 country code to continent name."""
    if not isinstance(country_code, str) or not country_code:
        return "Unknown"
    try:
        return CONTINENTS.get(pc.country_alpha2_to_continent_code(country_code.upper()), "Unknown")
    except (KeyError, ValueError):
        return "Unknown"


def load_latest_probes(probes_dir: str = PROBES_DIR) -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(probes_dir, "probes-[0-9]*.csv")))
    if not files:
        raise FileNotFoundError(f"No probes-YYYYMMDD.csv found in {probes_dir}; run probes_metadata.py first")
    logger.info(f"Using probes file: {files[-1]}")
    # keep_default_na=False so Namibia ("NA") is not read as missing
    probes = pd.read_csv(files[-1], keep_default_na=False, na_values=[""])
    probes = probes.drop_duplicates("id").set_index("id")
    return probes[["asn_v4", "asn_v6", "country_code", "tags"]]


def enrich(df: pd.DataFrame, probes: pd.DataFrame, ipv: str, validator: Callable[[str], bool]) -> pd.DataFrame:
    df = df.merge(probes, how="left", left_on="prb_id", right_index=True)

    asn = df["asn_v4"] if ipv == "IPv4" else df["asn_v6"]
    df["asn"] = pd.to_numeric(asn, errors="coerce").astype("Int64")
    df["country"] = df["country_code"].fillna("Unknown")
    df["continent"] = df["country"].map(country_to_region)
    df["tags"] = df["tags"].fillna("Unknown").astype(str)
    df["v4_works"] = ~df["tags"].str.contains("system-ipv4-doesnt-work", regex=False)
    df["v6_works"] = ~df["tags"].str.contains("system-ipv6-doesnt-work", regex=False)

    # validate each distinct answer once
    verdicts = {v: validate_chaos_string(validator, v) for v in df["rdata"].dropna().unique()}
    df["chaos_valid"] = df["rdata"].map(verdicts).fillna(False).astype(bool)
    return df.drop(columns=["asn_v4", "asn_v6", "country_code"])


def process_file(fpath: str, probes: pd.DataFrame) -> bool:
    fname = os.path.basename(fpath)
    meta = parse_result_stem(os.path.splitext(fname)[0])
    if meta is None:
        logger.debug(f"Skipping {fname}: not a result file")
        return True

    df = read_table(fpath)
    if "prb_id" not in df.columns:
        logger.error(f"'prb_id' column not found in {fname}. Columns: {df.columns.tolist()}")
        return False
    if all(col in df.columns for col in ENRICHED_COLUMNS):
        logger.debug(f"Skipping {fname}: already enriched")
        return True

    validator = build_validator(meta["service"])
    if validator is None:
        logger.error(f"Skipping {fname}: no regex/chaos_string configured for service '{meta['service']}'")
        return False

    df = enrich(df, probes, meta["ipv"], validator)
    with atomic_writer(fpath) as f:
        df.to_csv(f, sep="\t", index=False)
    logger.info(f"Enriched {fname} ({len(df)} rows, {int(df['chaos_valid'].sum())} valid)")
    return True


def main() -> int:
    setup_logging(CONFIG.get("logging_level", "INFO"))
    probes = load_latest_probes()
    for fpath in sorted(glob.glob(os.path.join(RESULTS_DIR, "chaos_*.tsv"))):
        # Per-file problems are logged but don't block the other files or later stages.
        try:
            process_file(fpath, probes)
        except Exception:
            logger.exception(f"Failed to enrich {fpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
