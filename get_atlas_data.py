#!/usr/bin/env python3
"""
RIPE Atlas CHAOS TXT batch downloader
-------------------------------------
- Fetches complete BATCH_SECS windows of CHAOS TXT DNS results for every
  configured service (IPv4 and IPv6 measurement)
- Extracts prb_id, timestamp, src_addr, rt, rdata, rcode and error_type
- Writes one TSV per service / IP version / window
- Catches up on missed windows (up to max_catchup_windows) using a state file
- Only advances the state when a window was fetched successfully
- Retries transient errors with a configurable backoff schedule
"""

import base64
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import dns.message
import requests

from common import RAW_COLUMNS, atomic_writer, result_stem, sanitize_field, setup_logging
from config import load_config

# === CONFIG ===============================================================

CONFIG = load_config()
STATE_FILE = CONFIG.get("state_file", "chaos_state.json")
SAVE_DIR = CONFIG.get("save_dir", "./results")
LOG_FILE = CONFIG.get("log_file")
API_BASE = CONFIG.get("api_base", "https://atlas.ripe.net/api/v2/measurements").rstrip("/")
BATCH_SECS = int(CONFIG.get("batch_secs", 1800))
MAX_WORKERS = int(CONFIG.get("max_workers", 4))
MAX_CATCHUP_WINDOWS = int(CONFIG.get("max_catchup_windows", 48))
RETRY_DELAYS = list(CONFIG.get("retry_delays", [60, 180, 540, 900]))
REQUEST_TIMEOUT = int(CONFIG.get("request_timeout", 90))
MEASUREMENTS = CONFIG.get("measurements") or {}
USER_AGENT = "rootviz (+https://github.com/SIDN/rootviz)"

IP_VERSIONS = (("ipv4", "IPv4"), ("ipv6", "IPv6"))

logger = logging.getLogger(__name__)

# === UTILITIES ============================================================


def get_last_complete_window(now: Optional[int] = None) -> Tuple[int, int]:
    """Return (start, stop) of the most recent complete, aligned window."""
    now = int(time.time()) if now is None else now
    stop = (now // BATCH_SECS) * BATCH_SECS
    return stop - BATCH_SECS, stop


def windows_to_fetch(last_done: Optional[int], current_stop: int) -> List[Tuple[int, int]]:
    """All windows after last_done up to current_stop, capped to MAX_CATCHUP_WINDOWS."""
    earliest = current_stop - MAX_CATCHUP_WINDOWS * BATCH_SECS
    first_stop = current_stop if last_done is None else max(last_done + BATCH_SECS, earliest + BATCH_SECS)
    first_stop = (first_stop // BATCH_SECS) * BATCH_SECS
    return [(stop - BATCH_SECS, stop) for stop in range(first_stop, current_stop + 1, BATCH_SECS)]


def load_state() -> Dict[str, int]:
    """Load last successfully fetched window stop per 'service/IPvX'."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load state ({e}); starting fresh")
        return {}
    state = {}
    for key, value in raw.items():
        if "/" in key:
            state[key] = int(value)
        else:  # legacy format: {service: stop}
            for _, ipv in IP_VERSIONS:
                state[f"{key}/{ipv}"] = int(value)
    return state


def save_state(state: Dict[str, int]) -> None:
    with atomic_writer(STATE_FILE) as f:
        json.dump(state, f, indent=1, sort_keys=True)


def get_dns_rcode_from_base64(b64_str: str) -> int:
    """Decode base64 DNS message and extract rcode."""
    return int(dns.message.from_wire(base64.b64decode(b64_str)).rcode())


def extract_info(data: dict) -> dict:
    """Extract one row from a RIPE Atlas DNS result object."""
    prb_id = data.get("prb_id")
    timestamp = data.get("timestamp")
    rdata = rt = rcode = error_type = None

    result = data.get("result")
    if isinstance(result, dict):
        rt = result.get("rt")
        if "abuf" in result:
            try:
                rcode = get_dns_rcode_from_base64(result["abuf"])
            except Exception as e:
                logger.debug(f"Failed to decode DNS message for probe {prb_id} at {timestamp}: {e}")
        answers = result.get("answers")
        if isinstance(answers, list) and answers and isinstance(answers[0], dict):
            rdata_list = answers[0].get("RDATA")
            if isinstance(rdata_list, list) and rdata_list:
                rdata = rdata_list[0]
    elif isinstance(data.get("error"), dict) and data["error"]:
        error_type = next(iter(data["error"]))

    return {
        "prb_id": prb_id,
        "timestamp": timestamp,
        "src_addr": data.get("from"),  # may be NATed
        "rt": rt,
        "rdata": rdata,
        "rcode": rcode,
        "error_type": error_type,
    }


def fetch_results(session: requests.Session, msm_id: int, start: int, stop: int) -> Optional[List[Dict[str, Any]]]:
    """Fetch the full JSON result array for one window.

    Returns a list (possibly empty) on success, or None if every attempt failed.
    Client errors other than 429 are not retried.
    """
    params = {"format": "json", "start": start, "stop": stop}
    url = f"{API_BASE}/{int(msm_id)}/results/"
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if 400 <= r.status_code < 500 and r.status_code != 429:
                logger.error(f"msm {msm_id}: HTTP {r.status_code}, not retrying: {r.text[:200]}")
                return None
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise ValueError(f"unexpected response type {type(data).__name__}")
            return data
        except Exception as e:
            logger.warning(f"Attempt {attempt}/{attempts} failed for msm {msm_id}: {e}")
            if attempt < attempts:
                time.sleep(RETRY_DELAYS[attempt - 1])
    logger.error(f"Giving up on msm {msm_id} window {start}-{stop} after {attempts} attempts")
    return None


def write_results(filename: str, results: List[Dict[str, Any]]) -> None:
    with atomic_writer(filename) as out:
        out.write("\t".join(RAW_COLUMNS) + "\n")
        for res in results:
            info = extract_info(res)
            out.write("\t".join(sanitize_field(info[c]) for c in RAW_COLUMNS) + "\n")


def process_measurement(name: str, msm_id: int, ipv: str, windows: List[Tuple[int, int]]) -> Tuple[str, int, Optional[int]]:
    """Fetch all pending windows for one measurement, oldest first.

    Returns (state_key, rows_written, last_successful_stop). Stops at the
    first failed window so it is retried next run.
    """
    key = f"{name}/{ipv}"
    total = 0
    last_ok = None
    with requests.Session() as session:
        session.headers["User-Agent"] = USER_AGENT
        for start, stop in windows:
            filename = os.path.join(SAVE_DIR, result_stem(name, ipv, start, stop) + ".tsv")
            if os.path.exists(filename):
                logger.info(f"[{key}] {os.path.basename(filename)} already exists, skipping")
                last_ok = stop
                continue
            results = fetch_results(session, msm_id, start, stop)
            if results is None:
                break
            if results:
                write_results(filename, results)
                logger.info(f"[{key}] wrote {len(results)} results to {filename}")
            else:
                logger.info(f"[{key}] no results for {start}-{stop}")
            total += len(results)
            last_ok = stop
    return key, total, last_ok


# === MAIN LOGIC ===========================================================


def main() -> int:
    setup_logging(CONFIG.get("logging_level", "INFO"), LOG_FILE)
    os.makedirs(SAVE_DIR, exist_ok=True)
    if not MEASUREMENTS:
        logger.error("No measurements configured")
        return 1

    state = load_state()
    _, current_stop = get_last_complete_window()
    logger.info(f"=== RIPE Atlas CHAOS TXT batch up to {time.strftime('%Y-%m-%d %H:%M', time.gmtime(current_stop))} UTC ===")

    jobs = []
    for name, meta in MEASUREMENTS.items():
        for cfg_key, ipv in IP_VERSIONS:
            msm_id = (meta or {}).get(cfg_key)
            if msm_id is None:
                logger.warning(f"[{name}] no {cfg_key} measurement configured, skipping")
                continue
            windows = windows_to_fetch(state.get(f"{name}/{ipv}"), current_stop)
            if windows:
                jobs.append((name, msm_id, ipv, windows))

    total = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_measurement, *job) for job in jobs]
        for future in as_completed(futures):
            key, count, last_ok = future.result()
            total += count
            if last_ok is not None:
                state[key] = max(last_ok, state.get(key, 0))
            if last_ok != current_stop:
                failed += 1

    save_state(state)
    logger.info(f"=== Batch complete: {total} results, {failed} measurement(s) incomplete ===")
    return 0


# === ENTRYPOINT ===========================================================
if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)
