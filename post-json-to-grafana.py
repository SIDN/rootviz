#!/usr/bin/env python3
"""
Push aggregated JSON summaries to an OTLP/HTTP metrics endpoint (e.g. Grafana).

Files are grouped per (window, service). For each group a single OTLP payload
with all metrics (IPv4 and IPv6 as a `protocol` attribute) is POSTed; only on
success are the files gzipped and moved to <input_dir>/sent/.

Authentication: if the GRAFANA_API_KEY environment variable is set it is sent
as "Authorization: Bearer <key>".
"""

import glob
import gzip
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from common import setup_logging
from config import load_config

# === CONFIG ===============================================================

CONFIG = load_config()
INPUT_DIR = CONFIG.get("grafana_input_dir", "./results/agg")
URL = CONFIG.get("grafana_url")
METRIC_PREFIX = CONFIG.get("grafana_metric_prefix", "rootviz")
DELAY_BETWEEN_POSTS = float(CONFIG.get("grafana_delay_between_posts", 0))
REQUEST_TIMEOUT = int(CONFIG.get("grafana_timeout", 30))
# Send a window even if one protocol's file is missing once it is this old.
INCOMPLETE_MAX_AGE_SECS = int(CONFIG.get("grafana_incomplete_max_age_hours", 6)) * 3600
API_KEY = os.environ.get("GRAFANA_API_KEY")

FILE_RE = re.compile(r"^chaos_(?P<service>.+)__(?P<ipv>IPv4|IPv6)_(?P<ts>\d{8}_\d{4}_\d{4})-agg\.json$")
PROTOCOLS = {"IPv4": "v4", "IPv6": "v6"}

logger = logging.getLogger(__name__)

# (metric suffix, description, unit, value type, extractor)
METRICS = [
    ("timeout_probes", "Number of Atlas Probes that timed out but are valid by IP version", "1", "asInt",
     lambda d: d.get("failed_probes_timeout_but_stack_works")),
    ("total_queries", "Total number of queries by IP version", "1", "asInt",
     lambda d: d.get("total_queries")),
    ("total_probes", "Total number of Atlas Probes by IP version", "1", "asInt",
     lambda d: d.get("total_probes")),
    ("valid_rcode0_queries_with_correct_chaos", "Number of valid queries (rcode 0 and correct chaos) by IP version", "1", "asInt",
     lambda d: d.get("valid_queries_rcode0_with_correct_chaos")),
    ("valid_probes_with_errors_timeout_etc", "Number of Atlas Probes with Errors (timeout, socket, senderror) by IP version", "1", "asInt",
     lambda d: d.get("failed_probes_error")),
    ("valid_probes_failed_no_error", "Number of Atlas Probes that failed with no error by IP version", "1", "asInt",
     lambda d: d.get("failed_probes_valid")),
    ("other_rcode_queries", "Number of queries without rcode 0 (including no response) by IP version", "1", "asInt",
     lambda d: other_rcode_count(d.get("rcode_counts") or {})),
    ("median_rtt_rcode0_correct_chaos", "Median RTT (ms) for valid queries (rcode 0 and correct chaos) by IP version", "ms", "asDouble",
     lambda d: (d.get("rtt_rcode0_correct_chaos") or {}).get("p50")),
    ("p99_rtt_rcode0_correct_chaos", "99th percentile RTT (ms) for valid queries (rcode 0 and correct chaos) by IP version", "ms", "asDouble",
     lambda d: (d.get("rtt_rcode0_correct_chaos") or {}).get("p99")),
    ("p75_rtt_rcode0_correct_chaos", "75th percentile RTT (ms) for valid queries (rcode 0 and correct chaos) by IP version", "ms", "asDouble",
     lambda d: (d.get("rtt_rcode0_correct_chaos") or {}).get("p75")),
    ("failed_asns_valid_probes", "Number of ASNs whose timing-out probes have no other working probe, by IP version", "1", "asInt",
     lambda d: d.get("asn_failed_probes_classification_len")),
]


def other_rcode_count(rcode_counts: Dict[str, int]) -> int:
    """Queries that did not get rcode 0, including those with no DNS response
    ("null": timeouts/errors)."""
    total = 0
    for rcode, count in rcode_counts.items():
        try:
            is_zero = int(float(rcode)) == 0
        except (TypeError, ValueError):
            is_zero = False  # "null": no DNS response
        if not is_zero:
            total += int(count)
    return total


def find_groups(input_dir: str = INPUT_DIR) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Return {window: {service: {"IPv4": path, "IPv6": path}}}."""
    groups: Dict[str, Dict[str, Dict[str, str]]] = {}
    for path in glob.glob(os.path.join(input_dir, "*-agg.json")):
        m = FILE_RE.match(os.path.basename(path))
        if m:
            groups.setdefault(m["ts"], {}).setdefault(m["service"], {})[m["ipv"]] = path
    return groups


def build_payload(service: str, data: Dict[str, dict], time_unix_nano: int) -> dict:
    """One OTLP ExportMetricsServiceRequest with all metrics for one service/window."""
    metrics = []
    for suffix, description, unit, value_type, extract in METRICS:
        points = []
        for ipv, summary in data.items():
            value = extract(summary)
            if value is None:
                continue
            points.append({
                value_type: int(value) if value_type == "asInt" else float(value),
                "timeUnixNano": str(time_unix_nano),
                "attributes": [{"key": "protocol", "value": {"stringValue": PROTOCOLS[ipv]}}],
            })
        if points:
            metrics.append({
                "name": f"{METRIC_PREFIX}_{service}_{suffix}",
                "unit": unit,
                "description": description,
                "gauge": {"dataPoints": points},
            })
    return {"resourceMetrics": [{"scopeMetrics": [{"metrics": metrics}]}]}


def post_data(session: requests.Session, payload: dict) -> bool:
    if DELAY_BETWEEN_POSTS:
        time.sleep(DELAY_BETWEEN_POSTS)
    try:
        response = session.post(URL, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        logger.error(f"POST failed: {e}")
        return False
    if response.status_code >= 300:
        logger.error(f"POST returned HTTP {response.status_code}: {response.text[:500]}")
        return False
    return True


def gzip_and_move(file_path: str) -> None:
    sent_dir = os.path.join(os.path.dirname(file_path), "sent")
    os.makedirs(sent_dir, exist_ok=True)
    gzipped_file = os.path.join(sent_dir, os.path.basename(file_path) + ".gz")
    with open(file_path, "rb") as f_in, gzip.open(gzipped_file, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(file_path)
    logger.debug(f"Archived {file_path} -> {gzipped_file}")


def window_age_secs(ts: str) -> Optional[float]:
    try:
        start = datetime.strptime(ts[:13], "%Y%m%d_%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return time.time() - start.timestamp()


def process_groups(groups, session: requests.Session) -> int:
    failures = 0
    for ts in sorted(groups):
        for service, files in sorted(groups[ts].items()):
            if len(files) < len(PROTOCOLS):
                age = window_age_secs(ts)
                if age is None or age < INCOMPLETE_MAX_AGE_SECS:
                    logger.info(f"[{service} {ts}] waiting for missing protocol file")
                    continue
                logger.warning(f"[{service} {ts}] sending incomplete window (only {sorted(files)})")

            data = {}
            for ipv, path in files.items():
                with open(path, encoding="utf-8") as f:
                    data[ipv] = json.load(f)

            timestamp = next((d.get("timestamp") for d in data.values() if d.get("timestamp")), None)
            if not timestamp:
                logger.warning(f"[{service} {ts}] no timestamp in files, skipping")
                continue

            payload = build_payload(service, data, int(timestamp) * 1_000_000_000)
            if post_data(session, payload):
                logger.info(f"[{service} {ts}] posted {len(payload['resourceMetrics'][0]['scopeMetrics'][0]['metrics'])} metrics")
                for path in files.values():
                    gzip_and_move(path)
            else:
                failures += 1
    return failures


def main() -> int:
    setup_logging(CONFIG.get("logging_level", "INFO"))
    if not URL:
        logger.warning("grafana_url not configured; skipping metrics export")
        return 0
    groups = find_groups()
    if not groups:
        logger.info("No aggregated files to send")
        return 0
    with requests.Session() as session:
        session.headers["Content-Type"] = "application/json"
        if API_KEY:
            session.headers["Authorization"] = f"Bearer {API_KEY}"
        failures = process_groups(groups, session)
    logger.info(f"Export complete ({failures} failed group(s), will retry next run)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
