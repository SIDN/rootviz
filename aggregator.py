#!/usr/bin/env python3
"""
Aggregate enriched CHAOS result files into one JSON summary per file.

Reads enriched files from agg_input_dir, writes <stem>-agg.json to
agg_output_dir and (by default) deletes the input file afterwards.
"""

import glob
import ipaddress
import json
import logging
import os
import sys
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict

import pandas as pd

from common import atomic_writer, compute_rtt_percentiles, parse_result_stem, read_table, setup_logging
from config import load_config

# === CONFIG ===============================================================

CONFIG = load_config()
INPUT_DIR = CONFIG.get("agg_input_dir", "results")
OUTPUT_DIR = CONFIG.get("agg_output_dir", "results/agg")
DELETE_INPUT = bool(CONFIG.get("agg_delete_input", True))
# Answers faster than this (ms) from a non-error probe are likely served by a
# local resolver/middlebox rather than the real service.
LOCAL_RTT_MS = float(CONFIG.get("agg_local_rtt_ms", 5))

REQUIRED_COLUMNS = {"prb_id", "src_addr", "rt", "rcode", "error_type", "asn", "country",
                    "continent", "v4_works", "v6_works", "chaos_valid"}
PROBE_KEY = ["prb_id", "src_addr", "asn", "country"]

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def is_routable(ip_str) -> bool:
    """True if ip_str is a valid, globally routable IP address."""
    try:
        return ipaddress.ip_address(str(ip_str)).is_global
    except ValueError:
        return False


def _as_bool(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series
    return series.astype(str).str.lower().isin(["true", "1"])


def _percentiles_by(df: pd.DataFrame, column: str) -> Dict[str, Dict[str, Any]]:
    known = df[df[column].notna() & (df[column] != "Unknown")]
    return {str(k): compute_rtt_percentiles(g["rt"]) for k, g in known.groupby(column, sort=True)}


def _n_unique_rows(df: pd.DataFrame) -> int:
    return int(len(df[PROBE_KEY].drop_duplicates()))


def aggregate(df: pd.DataFrame, ipv: str) -> Dict[str, Any]:
    """Compute the summary for one enriched result file."""
    for col in ("chaos_valid", "v4_works", "v6_works"):
        df[col] = _as_bool(df[col])
    df["rcode"] = pd.to_numeric(df["rcode"], errors="coerce")
    df["rt"] = pd.to_numeric(df["rt"], errors="coerce")

    stack_col = "v4_works" if ipv == "IPv4" else "v6_works"
    d = df[df[stack_col]]  # probes whose IP stack for this protocol works
    ok_all = df[(df["rcode"] == 0) & df["chaos_valid"]]
    ok = d[(d["rcode"] == 0) & d["chaos_valid"]]

    rcode_counts = {
        ("null" if pd.isna(k) else str(int(k))): int(v)
        for k, v in d["rcode"].value_counts(dropna=False).items()
    }
    error_counts = {str(k): int(v) for k, v in d["error_type"].dropna().value_counts().items()}

    # --- classify probes that did not get an rcode 0 answer ----------------
    failed = d[d["rcode"].ne(0)]  # includes rcode NULL (errors/timeouts)
    routable = failed["src_addr"].map(is_routable).astype(bool)
    no_error = failed["error_type"].isna()
    slow = failed["rt"] > LOCAL_RTT_MS  # missing rt counts as not slow -> maybe hijack

    valid_probes = failed[routable & no_error & slow]
    maybe_hijack = failed[routable & no_error & ~slow]
    error_rows = failed[routable & ~no_error]
    invalid_probes = failed[~routable]
    timeouts = error_rows[error_rows["error_type"] == "timeout"]

    # For each ASN with timing-out probes: do other probes in that ASN get valid answers?
    # {asn: {"timed_out": [prb_id, ...], "working": [other prb_ids with valid answers]}}
    working_by_asn = ok_all.dropna(subset=["asn"]).groupby("asn")["prb_id"].apply(set)
    asn_classification = {}
    for asn, g in timeouts.dropna(subset=["asn"]).groupby("asn", sort=True):
        timed_out = {int(p) for p in g["prb_id"]}
        working = {int(p) for p in working_by_asn.get(asn, set())} - timed_out
        asn_classification[str(int(asn))] = {"timed_out": sorted(timed_out), "working": sorted(working)}

    return {
        "total_queries": int(len(df)),
        "total_probes": int(d["prb_id"].nunique()),
        "total_addresses": int(d["src_addr"].nunique()),
        "rcode_counts": rcode_counts,
        "error_counts": error_counts,
        "valid_queries_rcode0_with_correct_chaos": int(len(ok)),
        "valid_probe_ids_rcode0_with_correct_chaos": int(ok["prb_id"].nunique()),
        "valid_addresses_rcode0_with_correct_chaos": int(ok["src_addr"].nunique()),
        "hijack_rcode0_wrong_chaos": int(((d["rcode"] == 0) & ~d["chaos_valid"]).sum()),
        "rtt_rcode0_correct_chaos": compute_rtt_percentiles(ok["rt"]),
        # key name typo kept for compatibility with existing consumers
        "rtt_rcode0_correct_cahos_per_cont": _percentiles_by(ok, "continent"),
        "rtt_rcode0_correct_chaos_per_country": _percentiles_by(ok, "country"),
        "failed_probes_valid": _n_unique_rows(valid_probes),
        "failed_probes_invalid": _n_unique_rows(invalid_probes),
        "failed_probes_maybe_hijack": _n_unique_rows(maybe_hijack),
        "failed_probes_error": int(error_rows["prb_id"].nunique()),
        "failed_probes_timeout_but_stack_works": _n_unique_rows(timeouts),
        "asn_failed_probes_classification": asn_classification,
        "asn_failed_probes_classification_len": sum(1 for v in asn_classification.values() if not v["working"]),
    }


def process_file(path: str) -> None:
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    meta = parse_result_stem(stem)
    if meta is None:
        logger.debug(f"Skipping {name}: not a result file")
        return

    df = read_table(path)
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        logger.info(f"Skipping {name}: not enriched yet (missing {sorted(missing)})")
        return

    dt = datetime.strptime(meta["date"] + meta["start"], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    output = {"file": name, "timestamp": int(dt.timestamp())}
    output.update(aggregate(df, meta["ipv"]))

    out_file = os.path.join(OUTPUT_DIR, f"{stem}-agg.json")
    with atomic_writer(out_file) as fout:
        json.dump(output, fout, indent=2)
    logger.info(f"Saved {out_file}")

    if DELETE_INPUT:
        os.remove(path)


def main() -> int:
    setup_logging(CONFIG.get("logging_level", "INFO"))
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    done = {os.path.basename(p)[: -len("-agg.json")] for p in glob.glob(os.path.join(OUTPUT_DIR, "*-agg.json"))}
    done |= {os.path.basename(p)[: -len("-agg.json.gz")] for p in glob.glob(os.path.join(OUTPUT_DIR, "sent", "*-agg.json.gz"))}

    for path in sorted(glob.glob(os.path.join(INPUT_DIR, "chaos_*.tsv"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem in done:
            logger.info(f"Skipping {stem}: already aggregated")
            continue
        try:
            process_file(path)
        except Exception:
            logger.exception(f"Error processing {path}")

    logger.info("Aggregation complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
